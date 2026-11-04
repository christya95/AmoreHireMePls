"""Import of the private GitHub tracker (christya95/job-application-records) from a local clone.

The tracker stays the durable ledger. Import reads `data/applications.json` at a specific commit,
previews counts/duplicates/conflicts/missing artifacts, and upserts applications + jobs into MongoDB
with provenance (source commit, path, record hash). Re-import is idempotent: unchanged records are
left alone, changed tracker records update the imported copy, and local edits that disagree with the
tracker are recorded as conflicts rather than overwritten.
"""

from __future__ import annotations

import json
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .db import Store
from .jobs import JobStore, dedup_keys
from .util import sha256_file, sha256_json, utcnow

TRACKER_STATUSES = ("submitted", "prepared", "blocked", "researched", "skipped", "withdrawn")
DATA_PATH = "data/applications.json"


@dataclass
class TrackerClone:
    root: Path

    def head(self) -> str:
        try:
            return subprocess.run(["git", "-C", str(self.root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            return "unknown"

    def records(self) -> list[dict]:
        path = self.root / DATA_PATH
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("applications.json must be a list")
        return data

    def data_hash(self) -> str:
        return sha256_file(self.root / DATA_PATH)

    def artifact_status(self, record: dict) -> list[dict]:
        out = []
        for a in record.get("artifacts", []):
            p = self.root / a.get("path", "")
            exists = p.is_file()
            out.append({"path": a.get("path"), "kind": a.get("kind"), "exists": exists, "hash_ok": exists and sha256_file(p) == a.get("sha256"), "sha256": a.get("sha256")})
        return out


@dataclass
class ImportPreview:
    source_commit: str
    source_path: str
    source_sha256: str
    total: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    new: int = 0
    unchanged: int = 0
    changed: int = 0
    conflicts: list[dict] = field(default_factory=list)
    duplicates: list[dict] = field(default_factory=list)  # tracker records that share a job identity
    missing_artifacts: list[dict] = field(default_factory=list)
    invalid: list[dict] = field(default_factory=list)
    unknown_fields: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


KNOWN_FIELDS = {
    "id",
    "employer",
    "role",
    "requisition_id",
    "linkedin_job_id",
    "posting_url",
    "status",
    "application_date",
    "date_basis",
    "confirmation",
    "evidence_basis",
    "resume_match_basis",
    "source_log",
    "notes",
    "artifacts",
    "history",
    "resume_unavailable_reason",
}


class TrackerImporter:
    def __init__(self, store: Store, clone: TrackerClone) -> None:
        self.store = store
        self.clone = clone
        self.jobs = JobStore(store)
        self.apps = store.c("applications")

    def preview(self) -> ImportPreview:
        records = self.clone.records()
        pv = ImportPreview(self.clone.head(), DATA_PATH, self.clone.data_hash(), total=len(records))
        groups: dict[str, list[str]] = defaultdict(list)
        unknown: set[str] = set()
        for r in records:
            unknown |= set(r) - KNOWN_FIELDS
            rid = r.get("id")
            if not rid or not r.get("employer") or not r.get("role"):
                pv.invalid.append({"id": rid, "reason": "missing id/employer/role"})
                continue
            pv.by_status[r.get("status", "?")] = pv.by_status.get(r.get("status", "?"), 0) + 1
            for k in dedup_keys(r.get("employer", ""), r.get("posting_url"), r.get("requisition_id"), r.get("linkedin_job_id")):
                groups[k].append(rid)
            for a in self.clone.artifact_status(r):
                if not a["exists"] or not a["hash_ok"]:
                    pv.missing_artifacts.append({"record_id": rid, **a})
            rhash = sha256_json(r)
            existing = self.apps.find_one({"tracker_record_id": rid})
            if existing is None:
                pv.new += 1
            elif existing.get("provenance", {}).get("record_sha256") == rhash:
                pv.unchanged += 1
            else:
                pv.changed += 1
                if existing.get("local_changes"):
                    pv.conflicts.append({"record_id": rid, "reason": "tracker record changed and local edits exist", "local_changes": existing["local_changes"]})
        for k, ids in groups.items():
            if len(set(ids)) > 1:
                pv.duplicates.append({"key": k, "record_ids": sorted(set(ids))})
        pv.unknown_fields = sorted(unknown)
        return pv

    def import_all(self) -> dict[str, Any]:
        pv = self.preview()
        records = self.clone.records()
        now = utcnow()
        counts = {"inserted": 0, "updated": 0, "unchanged": 0, "conflicts": 0, "jobs_created": 0}
        for r in records:
            rid = r.get("id")
            if not rid or not r.get("employer") or not r.get("role"):
                continue
            rhash = sha256_json(r)
            job, created = self.jobs.upsert(
                employer=r["employer"],
                title=r["role"],
                posting_url=r.get("posting_url"),
                requisition_id=r.get("requisition_id") or None,
                linkedin_job_id=r.get("linkedin_job_id") or None,
                source="tracker",
                extra={"tracker_status": r.get("status")},
            )
            counts["jobs_created"] += int(created)
            if r.get("status") == "submitted":
                self.jobs.set_status(job["_id"], "applied_historical")
            provenance = {"origin": "tracker_import", "source_commit": pv.source_commit, "source_path": pv.source_path, "record_sha256": rhash, "imported_at": now}
            existing = self.apps.find_one({"tracker_record_id": rid})
            artifacts = self.clone.artifact_status(r)
            if existing is None:
                self.apps.insert_one(
                    {
                        "_id": f"app_{rid}",
                        "tracker_record_id": rid,
                        "job_id": job["_id"],
                        "status": r.get("status"),
                        "record": r,
                        "provenance": provenance,
                        "artifact_status": artifacts,
                        "sync": {"state": "synced", "tracker_commit": pv.source_commit},
                        "created_at": now,
                        "updated_at": now,
                        "evidence_basis": r.get("evidence_basis"),
                        "date_basis": r.get("date_basis"),
                    }
                )
                counts["inserted"] += 1
            elif existing.get("provenance", {}).get("record_sha256") == rhash:
                counts["unchanged"] += 1
                self.apps.update_one({"_id": existing["_id"]}, {"$set": {"artifact_status": artifacts, "provenance.last_seen_commit": pv.source_commit}})
            elif existing.get("local_changes"):
                counts["conflicts"] += 1
                self.apps.update_one(
                    {"_id": existing["_id"]},
                    {"$set": {"conflict": {"tracker_record": r, "tracker_commit": pv.source_commit, "detected_at": now}, "sync.state": "conflict", "updated_at": now}},
                )
            else:
                counts["updated"] += 1
                self.apps.update_one(
                    {"_id": existing["_id"]},
                    {
                        "$set": {
                            "record": r,
                            "status": r.get("status"),
                            "provenance": provenance,
                            "artifact_status": artifacts,
                            "sync": {"state": "synced", "tracker_commit": pv.source_commit},
                            "updated_at": now,
                            "evidence_basis": r.get("evidence_basis"),
                            "date_basis": r.get("date_basis"),
                        },
                        "$push": {"import_history": {"previous_sha256": existing["provenance"].get("record_sha256"), "at": now}},
                    },
                )
        self.store.c("imports").insert_one(
            {"source_commit": pv.source_commit, "source_path": pv.source_path, "source_sha256": pv.source_sha256, "counts": counts, "preview": pv.as_dict(), "created_at": now}
        )
        self.store.audit("tracker_import", None, **counts, commit=pv.source_commit)
        return {"preview": pv.as_dict(), "counts": counts}

    def has_history(self) -> bool:
        return self.apps.count_documents({"provenance.origin": "tracker_import"}) > 0
