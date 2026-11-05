"""Tracker synchronization: one coordinator, durable outbox, local clone of the GitHub ledger.

States per outbox item: sync_pending -> committing -> committed_local -> synced
                        failures: sync_pending (retry later) | local_only (committed, push failed) | conflict
Database and git are separate systems; `reconcile()` repairs the state after a crash between them by
inspecting the clone (record present? commit present? pushed?). The remote is never force-pushed.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any

from .browser import normalize_screenshots
from .db import Store
from .util import new_id, sha256_file, utcnow

log = logging.getLogger(__name__)
TRACKER_PATHS = ["data", "artifacts", "records", "APPLICATIONS.md", "applications.csv"]
SUMMARY_ARTIFACT_KIND = "summary_screenshot"  # tracker artifact kind for the pre-submit review page (receipt keeps `receipt`)


class GitError(Exception):
    pass


def git(root: Path, *args: str, check: bool = True) -> str:
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": str(Path.home()),
        "GIT_AUTHOR_NAME": "AmoreHireMePls",
        "GIT_AUTHOR_EMAIL": "amore@localhost",
        "GIT_COMMITTER_NAME": "AmoreHireMePls",
        "GIT_COMMITTER_EMAIL": "amore@localhost",
    }
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, env=env, timeout=120)
    if check and r.returncode != 0:
        raise GitError((r.stderr or r.stdout).strip()[:500])
    return r.stdout.strip()


class SyncOutbox:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.coll = store.c("sync_outbox")

    def enqueue(self, application_id: str, kind: str = "record_submission") -> dict:
        doc = {
            "_id": new_id("sync"),
            "application_id": application_id,
            "kind": kind,
            "status": "sync_pending",
            "attempts": 0,
            "next_attempt_at": utcnow(),
            "created_at": utcnow(),
            "updated_at": utcnow(),
            "history": [],
        }
        if not self.store.insert_unique("sync_outbox", doc):
            return self.coll.find_one({"application_id": application_id, "kind": kind})
        return doc

    def pending(self, limit: int = 10) -> list[dict]:
        return list(
            self.coll.find({"status": {"$in": ["sync_pending", "local_only", "committing", "committed_local"]}, "next_attempt_at": {"$lte": utcnow()}})
            .sort("created_at", 1)
            .limit(limit)
        )

    def set(self, item_id: str, status: str, **fields: Any) -> None:
        self.coll.update_one(
            {"_id": item_id},
            {
                "$set": {"status": status, "updated_at": utcnow(), **fields},
                "$push": {"history": {"at": utcnow(), "status": status, "note": fields.get("error") or fields.get("note", "")}},
            },
        )

    def summary(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for row in self.coll.aggregate([{"$group": {"_id": "$status", "n": {"$sum": 1}}}]):
            out[row["_id"]] = row["n"]
        return out


def build_record(app: dict, job: dict) -> dict:
    """Map an application to the tracker's record schema. Only existing fields; evidence stays honest."""
    ev = app.get("evidence") or {}
    pkg = app.get("review_package") or {}
    rid = app.get("tracker_record_id") or (f"li-{job['linkedin_job_id']}" if job.get("linkedin_job_id") else None)
    if rid is None:
        base = (job.get("employer_norm") or "employer").replace(" ", "-")
        req = (job.get("requisition_id") or job["_id"][-8:]).replace("/", "-")
        rid = f"{base}-{req}"[:120]
    confirmation = (ev.get("receipt_text") or ev.get("confirmation") or "").strip()
    if confirmation:
        # keep the first meaningful line of the receipt
        confirmation = next((ln.strip() for ln in confirmation.splitlines() if ln.strip()), confirmation)[:300]
    doc_name = (pkg.get("document") or {}).get("filename")
    return {
        "id": rid,
        "employer": job["employer"],
        "role": job["title"],
        "requisition_id": job.get("requisition_id") or "",
        "linkedin_job_id": job.get("linkedin_job_id") or "",
        "posting_url": job.get("posting_url") or pkg.get("apply_url") or "",
        "status": "submitted",
        "application_date": app.get("application_date"),
        "date_basis": app.get("date_basis", ""),
        "confirmation": confirmation,
        "evidence_basis": app.get("evidence_basis", ""),
        "resume_match_basis": f"exact file uploaded by AmoreHireMePls: {doc_name}"
        if doc_name and ev.get("basis") == "receipt_observed"
        else ("user-reported upload" if doc_name else "not recovered"),
        "source_log": f"AmoreHireMePls application {app['_id']}",
        "notes": "Recorded by AmoreHireMePls from observed evidence; review summary and receipt screenshots are archived with this record when captured.",
        "artifacts": [],
    }


def evidence_screenshots(app: dict) -> dict[str, Path]:
    """Latest existing screenshot file per label (`summary`, `receipt`, `ambiguous`, ...) from the application evidence."""
    out: dict[str, Path] = {}
    for shot in normalize_screenshots((app.get("evidence") or {}).get("screenshots")):
        p = Path(shot["path"])
        if p.is_file():
            out[shot["label"]] = p
    return out


class SyncCoordinator:
    def __init__(self, store: Store, clone_dir: Path, remote: str = "origin", branch: str = "main", push_enabled: bool = True, python: str | None = None) -> None:
        self.store = store
        self.root = clone_dir
        self.remote = remote
        self.branch = branch
        self.push_enabled = push_enabled
        self.python = python or sys.executable
        self.outbox = SyncOutbox(store)

    # -- lock: one coordinator -----------------------------------------------------------------
    def _acquire(self, owner: str) -> bool:
        now = utcnow()
        res = self.store.c("controls").find_one_and_update(
            {"key": "sync_coordinator_lock", "$or": [{"value.until": {"$lt": now}}, {"value": None}, {"value.owner": owner}]},
            {"$set": {"value": {"owner": owner, "until": now + timedelta(minutes=10)}, "updated_at": now}},
            upsert=False,
        )
        if res is None:
            existing = self.store.c("controls").find_one({"key": "sync_coordinator_lock"})
            if existing is None:
                return self.store.insert_unique("controls", {"key": "sync_coordinator_lock", "value": {"owner": owner, "until": now + timedelta(minutes=10)}, "updated_at": now})
            return False
        return True

    def _release(self, owner: str) -> None:
        self.store.c("controls").update_one({"key": "sync_coordinator_lock", "value.owner": owner}, {"$set": {"value": None}})

    # -- processing ----------------------------------------------------------------------------------
    def process_pending(self, owner: str = "coordinator", limit: int = 5) -> list[dict]:
        if not self._acquire(owner):
            return []
        try:
            results = []
            for item in self.outbox.pending(limit):
                results.append(self.process_item(item))
            return results
        finally:
            self._release(owner)

    def process_item(self, item: dict) -> dict:
        app = self.store.c("applications").find_one({"_id": item["application_id"]})
        job = self.store.c("jobs").find_one({"_id": app["job_id"]}) if app else None
        if not app or not job or app.get("status") != "submitted":
            self.outbox.set(item["_id"], "skipped", error="application not submitted")
            return {"id": item["_id"], "status": "skipped"}
        try:
            if not (self.root / ".git").exists():
                raise GitError(f"tracker clone missing at {self.root}")
            if item["status"] in ("sync_pending",):
                self._fetch_and_fast_forward()
                record = build_record(app, job)
                self.outbox.set(item["_id"], "committing", record_id=record["id"])
                sha = self._write_and_commit(app, record)
                self.store.c("applications").update_one(
                    {"_id": app["_id"]}, {"$set": {"tracker_record_id": record["id"], "sync.commit_sha": sha, "sync.state": "committed_local", "updated_at": utcnow()}}
                )
                self.outbox.set(item["_id"], "committed_local", commit_sha=sha, record_id=record["id"])
                item = self.outbox.coll.find_one({"_id": item["_id"]})
            if item["status"] in ("committing",):
                return self.reconcile_item(item)
            if item["status"] in ("committed_local", "local_only"):
                return self._push(item, app)
            return {"id": item["_id"], "status": item["status"]}
        except RecordConflict as exc:
            self.outbox.set(item["_id"], "conflict", error=str(exc))
            self.store.c("applications").update_one({"_id": app["_id"]}, {"$set": {"sync.state": "conflict", "sync.error": str(exc)}})
            return {"id": item["_id"], "status": "conflict", "error": str(exc)}
        except (GitError, subprocess.SubprocessError, OSError) as exc:
            attempts = item.get("attempts", 0) + 1
            delay = min(2**attempts, 60)
            status = "local_only" if item.get("commit_sha") else "sync_pending"
            self.outbox.set(item["_id"], status, error=str(exc)[:400], attempts=attempts, next_attempt_at=utcnow() + timedelta(minutes=delay))
            self.store.c("applications").update_one({"_id": app["_id"]}, {"$set": {"sync.state": status, "sync.error": str(exc)[:400]}})
            return {"id": item["_id"], "status": status, "error": str(exc)[:200]}

    def _fetch_and_fast_forward(self) -> None:
        try:
            git(self.root, "fetch", self.remote, self.branch)
        except GitError as exc:
            log.warning("fetch failed (continuing with local clone): %s", exc)
            return
        dirty = git(self.root, "status", "--porcelain", "--", *TRACKER_PATHS)
        if dirty:
            raise GitError("tracker clone has uncommitted changes in ledger paths; refusing to proceed")
        ahead_behind = git(self.root, "rev-list", "--left-right", "--count", f"HEAD...{self.remote}/{self.branch}")
        ahead, behind = (int(x) for x in ahead_behind.split())
        if behind and not ahead:
            git(self.root, "merge", "--ff-only", f"{self.remote}/{self.branch}")
        elif behind and ahead:
            self._rebase_or_conflict()

    def _rebase_or_conflict(self) -> None:
        """Rebase local ledger commits onto the remote; a conflict aborts cleanly and is surfaced, never left half-applied."""
        try:
            git(self.root, "rebase", f"{self.remote}/{self.branch}")
        except GitError as exc:
            git(self.root, "rebase", "--abort", check=False)
            raise RecordConflict(f"rebase conflict against {self.remote}/{self.branch}; resolve in the clone: {str(exc)[-200:]}") from exc

    def _record_exists_with_same_artifacts(self, record_id: str, resume_sha: str | None) -> bool:
        data_path = self.root / "data/applications.json"
        if not data_path.exists():
            return False
        for r in json.loads(data_path.read_text(encoding="utf-8")):
            if r.get("id") == record_id:
                if resume_sha is None:
                    return True
                return any(a.get("sha256") == resume_sha for a in r.get("artifacts", []))
        return False

    def _write_and_commit(self, app: dict, record: dict) -> str:
        pkg = app.get("review_package") or {}
        doc = pkg.get("document") or {}
        resume_path = Path(doc["path"]) if doc.get("path") and Path(doc["path"]).exists() else None
        resume_sha = sha256_file(resume_path) if resume_path else None
        shots = evidence_screenshots(app)
        receipt, summary = shots.get("receipt"), shots.get("summary")
        if not self._record_exists_with_same_artifacts(record["id"], resume_sha):
            # duplicate employer/requisition under a different id is a conflict, not a second record
            for r in json.loads((self.root / "data/applications.json").read_text(encoding="utf-8")):
                if (
                    r["id"] != record["id"]
                    and record.get("requisition_id")
                    and r.get("employer", "").casefold() == record["employer"].casefold()
                    and r.get("requisition_id") == record["requisition_id"]
                ):
                    raise RecordConflict(f"employer/requisition already recorded as {r['id']}")
            if resume_path is None:
                record["resume_unavailable_reason"] = "tailored file not present in artifact storage at sync time"
            with tempfile.TemporaryDirectory() as td:
                # Tracker convention: artifacts/<record id>/<sha12>-<original filename>. The copies are named
                # `<record id>_<label>.png` so the archived filename itself carries the record id and label.
                summary_copy = self._stage_summary(record, summary)
                rec_file = Path(td) / "record.json"
                rec_file.write_text(json.dumps(record, indent=2), encoding="utf-8")
                cmd = [self.python, str(self.root / "scripts/records.py"), "add", str(rec_file)]
                if resume_path:
                    cmd += ["--resume", str(resume_path)]
                if receipt:
                    receipt_copy = Path(td) / f"{record['id']}_receipt.png"
                    shutil.copy2(receipt, receipt_copy)
                    cmd += ["--receipt", str(receipt_copy)]
                r = subprocess.run(cmd, capture_output=True, text=True, cwd=self.root, timeout=120)
                if r.returncode != 0:
                    if summary_copy and summary_copy.exists():
                        summary_copy.unlink()  # leave no untracked file behind for the next attempt
                    msg = (r.stderr or r.stdout).strip()
                    if "already recorded" in msg:
                        raise RecordConflict(msg[-300:])
                    raise GitError(f"records.py add failed: {msg[-300:]}")
        v = subprocess.run([self.python, str(self.root / "scripts/records.py"), "validate"], capture_output=True, text=True, cwd=self.root, timeout=120)
        if v.returncode != 0:
            raise GitError(f"records.py validate failed: {(v.stderr or v.stdout)[-300:]}")
        git(self.root, "add", "--", *[p for p in TRACKER_PATHS if (self.root / p).exists()])
        staged = git(self.root, "diff", "--cached", "--name-only")
        if not staged:
            return git(self.root, "rev-parse", "HEAD")  # already committed (crash after commit)
        for name in staged.splitlines():
            if not any(name == p or name.startswith(p + "/") for p in TRACKER_PATHS):
                raise GitError(f"refusing to commit unrelated path {name}")
        git(self.root, "commit", "-q", "-m", f"Record confirmed application {record['id']}")
        return git(self.root, "rev-parse", "HEAD")

    def _stage_summary(self, record: dict, summary: Path | None) -> Path | None:
        """Copy the pre-submit review screenshot into the clone under the tracker's artifact layout and list it on the
        record (`records.py add` only knows --resume/--receipt; its validator still checks path confinement and hash)."""
        if summary is None:
            return None
        sha = sha256_file(summary)
        name = f"{record['id']}_summary.png"
        target = self.root / "artifacts" / record["id"] / f"{sha[:12]}-{name}"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(summary, target)
        entry = {"kind": SUMMARY_ARTIFACT_KIND, "path": target.relative_to(self.root).as_posix(), "sha256": sha, "original_filename": name}
        if entry not in record.setdefault("artifacts", []):
            record["artifacts"].append(entry)
        return target

    def _push(self, item: dict, app: dict) -> dict:
        if not self.push_enabled:
            self.outbox.set(item["_id"], "local_only", error="push disabled by configuration (AMORE_TRACKER_PUSH_ENABLED=false)")
            self.store.c("applications").update_one({"_id": app["_id"]}, {"$set": {"sync.state": "local_only"}})
            return {"id": item["_id"], "status": "local_only"}
        try:
            git(self.root, "push", self.remote, f"HEAD:{self.branch}")
        except GitError as exc:
            if "rejected" in str(exc) or "non-fast-forward" in str(exc) or "fetch first" in str(exc):
                git(self.root, "fetch", self.remote, self.branch)
                self._rebase_or_conflict()
                git(self.root, "push", self.remote, f"HEAD:{self.branch}")
            else:
                raise
        sha = git(self.root, "rev-parse", "HEAD")
        self.outbox.set(item["_id"], "synced", commit_sha=sha)
        self.store.c("applications").update_one({"_id": app["_id"]}, {"$set": {"sync.state": "synced", "sync.commit_sha": sha, "sync.synced_at": utcnow()}})
        self.store.audit("tracker_synced", app["_id"], commit=sha, record_id=item.get("record_id"))
        return {"id": item["_id"], "status": "synced", "commit": sha}

    # -- crash recovery ---------------------------------------------------------------------------------
    def reconcile_item(self, item: dict) -> dict:
        """Called for items left in `committing` by a crash: decide from the clone what actually happened."""
        record_id = item.get("record_id")
        app = self.store.c("applications").find_one({"_id": item["application_id"]})
        in_data = self._record_exists_with_same_artifacts(record_id, None) if record_id else False
        committed = False
        if in_data:
            committed = not git(self.root, "status", "--porcelain", "--", *TRACKER_PATHS)
        if in_data and committed:
            sha = git(self.root, "log", "-1", "--format=%H", "--", f"records/{record_id}.md") or git(self.root, "rev-parse", "HEAD")
            self.outbox.set(item["_id"], "committed_local", commit_sha=sha, note="recovered: commit found in clone")
            self.store.c("applications").update_one({"_id": app["_id"]}, {"$set": {"tracker_record_id": record_id, "sync.commit_sha": sha, "sync.state": "committed_local"}})
            return self._push(self.outbox.coll.find_one({"_id": item["_id"]}), app)
        if in_data and not committed:
            # files written but not committed: finish the commit without re-adding the record
            git(self.root, "add", "--", *[p for p in TRACKER_PATHS if (self.root / p).exists()])
            git(self.root, "commit", "-q", "-m", f"Record confirmed application {record_id}")
            sha = git(self.root, "rev-parse", "HEAD")
            self.outbox.set(item["_id"], "committed_local", commit_sha=sha, note="recovered: completed interrupted commit")
            return self._push(self.outbox.coll.find_one({"_id": item["_id"]}), app)
        # nothing landed: discard partial working-tree changes to tracker paths and retry from scratch
        if git(self.root, "status", "--porcelain", "--", *TRACKER_PATHS):
            git(self.root, "checkout", "--", *[p for p in TRACKER_PATHS if (self.root / p).exists()])
            if record_id:  # remove only this record's untracked files, never other untracked work in the clone
                git(self.root, "clean", "-fdq", "--", f"artifacts/{record_id}", f"records/{record_id}.md", check=False)
        self.outbox.set(item["_id"], "sync_pending", note="recovered: nothing committed; retrying")
        return {"id": item["_id"], "status": "sync_pending"}

    def reconcile_all(self) -> list[dict]:
        out = []
        for item in self.outbox.coll.find({"status": "committing"}):
            out.append(self.reconcile_item(item))
        for item in self.outbox.coll.find({"status": {"$in": ["local_only", "committed_local"]}}):
            sha = item.get("commit_sha")
            if sha and self._remote_has(sha):
                self.outbox.set(item["_id"], "synced", note="recovered: remote already has the commit")
                self.store.c("applications").update_one({"_id": item["application_id"]}, {"$set": {"sync.state": "synced", "sync.commit_sha": sha}})
                out.append({"id": item["_id"], "status": "synced"})
        return out

    def _remote_has(self, sha: str) -> bool:
        try:
            git(self.root, "fetch", self.remote, self.branch)
            return bool(git(self.root, "branch", "-r", "--contains", sha, check=False))
        except GitError:
            return False

    def status(self) -> dict[str, Any]:
        info: dict[str, Any] = {"clone": str(self.root), "clone_present": (self.root / ".git").exists(), "push_enabled": self.push_enabled, "outbox": self.outbox.summary()}
        if info["clone_present"]:
            try:
                info["head"] = git(self.root, "rev-parse", "--short", "HEAD")
                info["dirty"] = bool(git(self.root, "status", "--porcelain"))
                info["ahead"] = git(self.root, "rev-list", "--count", f"{self.remote}/{self.branch}..HEAD", check=False) or "?"
            except GitError as exc:
                info["error"] = str(exc)
        return info


class RecordConflict(Exception):
    pass


def ensure_clone(clone_dir: Path, url: str) -> None:
    if (clone_dir / ".git").exists():
        return
    clone_dir.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--quiet", url, str(clone_dir)],
        check=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "GIT_TERMINAL_PROMPT": "0", "HOME": str(Path.home())},
    )


def remove_clone(clone_dir: Path) -> None:
    shutil.rmtree(clone_dir, ignore_errors=True)
