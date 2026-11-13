"""What the browser showed and sent when an attempt stopped short of a confirmed success.

The owner chose a complete record: nothing is masked, so it can hold a site password, a typed code and session
cookies. It is therefore kept apart from everything else the app stores:

- Files live only in the job's private artifact folder (`jobs/<job_id>/stop/<attempt>/`), hashed and registered in
  `artifacts` as kind `stop_record`, and are served only through the signed-in, hash-verified download route.
- Nothing captured here goes into the application's evidence, a run event, a log line, the tracker, a backup or a
  model input. The database holds counts, sizes and hashes only.
- A new attempt for the job deletes the earlier record. A sent application, a saved receipt and an unclear result
  keep theirs.

Capture is best effort. A part that fails is named in the record and never changes why the attempt stopped.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any

from amorehiremepls.core.util import new_id, sha256_file, utcnow

log = logging.getLogger(__name__)

KIND = "stop_record"
# Pictures of a page where an unsent attempt stopped. They are cleared with the stop record. Proof pictures are not.
DIAGNOSTIC_SHOTS = ("handoff", "failed", "validation", "challenge")
BODY_CAP = 512 * 1024  # bytes of one text body copied into the request list; the archive keeps the whole body
HTML_CAP = 8 * 1024 * 1024
ROW_CAP = 5000
CONSOLE_CAP = 2000
FILE_CAP = 200 * 1024 * 1024  # an archive or trace larger than this is not kept
ORPHAN_MINUTES = 30
HAR_NAME = "attempt.har.zip"
TRACE_NAME = "trace.zip"
RECORD_NAME = "record.json"
SAVED_LINE = "Saved what the page showed and sent when this attempt stopped."
# name -> (label, title on the page, media type)
FILES: dict[str, tuple[str, str, str]] = {
    "view.png": ("view", "The screen", "image/png"),
    "page.png": ("page", "The whole page", "image/png"),
    "page.html": ("html", "Page source", "text/plain"),
    "network.json": ("network", "Request list", "application/json"),
    "console.json": ("console", "Console and page errors", "application/json"),
    HAR_NAME: ("har", "Network archive (HAR, zipped)", "application/zip"),
    TRACE_NAME: ("trace", "Playwright trace", "application/zip"),
    RECORD_NAME: ("record", "Record summary", "application/json"),
}
_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")
_TEXT = re.compile(r"(?i)^text/|json|xml|javascript|ecmascript|x-www-form-urlencoded|graphql|csv|html|svg")


def _iso() -> str:
    return utcnow().isoformat()


class StopRecorder:
    """One attempt's capture. Every method swallows its own failure and names the part in `errors`."""

    def __init__(self, root: Path, job_id: str, trace: bool = True) -> None:
        self.job_id = job_id
        self.attempt = utcnow().strftime("%Y%m%dT%H%M%S") + "_" + new_id("s")[-6:]
        self.dir = Path(root) / "jobs" / _SAFE.sub("_", job_id) / "stop" / self.attempt
        self.started_at = _iso()
        self.trace = trace
        self.errors: list[str] = []
        self.notes: list[str] = []
        self.ready = False
        self._console: list[dict] = []
        self._console_dropped = 0
        self._blocked: list[dict] = []
        self._page: dict[str, Any] = {}
        self._kept = False
        self._tracing = False

    def _fail(self, part: str, exc: BaseException) -> None:
        note = f"{part}: {type(exc).__name__}"
        if note not in self.errors:
            self.errors.append(note)

    # -- while the attempt runs ------------------------------------------------------------------------
    def context_options(self) -> dict[str, Any]:
        """Browser context options that record every request, with bodies, into one archive."""
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            self.dir.chmod(0o700)
        except Exception as exc:
            self._fail("folder", exc)
            return {}
        self.ready = True
        return {"record_har_path": str(self.dir / HAR_NAME), "record_har_content": "attach"}

    def attach(self, context: Any) -> None:
        if not self.ready:
            return
        try:
            context.on("console", self._on_console)
            context.on("weberror", self._on_error)
        except Exception as exc:
            self._fail("console", exc)
        if self.trace:
            try:
                context.tracing.start(screenshots=True, snapshots=True)
                self._tracing = True
            except Exception as exc:
                self._fail("trace", exc)

    def _line(self, row: dict) -> None:
        if len(self._console) >= CONSOLE_CAP:
            self._console_dropped += 1
            return
        self._console.append(row)

    def _on_console(self, message: Any) -> None:
        try:
            where = message.location or {}
            self._line({"at": _iso(), "type": str(message.type), "text": str(message.text)[:20000], "url": str(where.get("url") or ""), "line": where.get("lineNumber")})
        except Exception as exc:
            self._fail("console", exc)

    def _on_error(self, web_error: Any) -> None:
        try:
            error = web_error.error
            text = str(getattr(error, "stack", "") or getattr(error, "message", "") or error)
            self._line({"at": _iso(), "type": "pageerror", "text": text[:20000], "url": "", "line": None})
        except Exception as exc:
            self._fail("console", exc)

    def blocked(self, request: Any) -> None:
        """A request the app's own list of allowed sites refused."""
        try:
            if len(self._blocked) < ROW_CAP:
                self._blocked.append({"at": _iso(), "method": str(request.method), "url": str(request.url), "type": str(request.resource_type)})
        except Exception as exc:
            self._fail("blocked", exc)

    # -- when it stops ---------------------------------------------------------------------------------
    def keep(self, page: Any) -> None:
        """The page as it is right now: the screen, the whole page, its source and its address. First call wins."""
        if self._kept or not self.ready:
            return
        self._kept = True
        info: dict[str, Any] = {"captured_at": _iso(), "url": "", "title": "", "frames": [], "pages": []}
        self._page = info
        try:
            if page.is_closed():
                self.errors.append("page: closed before it could be recorded")
                return
            page.set_default_timeout(8000)
        except Exception as exc:
            self._fail("page", exc)
        for name, read in (("url", lambda: page.url), ("title", lambda: page.title())):
            try:
                info[name] = str(read() or "")
            except Exception as exc:
                self._fail(name, exc)
        try:
            info["frames"] = [{"url": str(f.url), "name": str(f.name or "")} for f in page.frames][:200]
            info["pages"] = [str(p.url) for p in page.context.pages][:50]
        except Exception as exc:
            self._fail("frames", exc)
        for name, full in (("view.png", False), ("page.png", True)):
            try:
                page.screenshot(path=str(self.dir / name), full_page=full, timeout=15000)
            except Exception as exc:
                self._fail(name, exc)
        try:
            raw = (page.content() or "").encode("utf-8", "replace")
            if len(raw) > HTML_CAP:
                self.notes.append(f"The page source was {len(raw)} bytes. The first {HTML_CAP} are kept.")
                raw = raw[:HTML_CAP]
            (self.dir / "page.html").write_bytes(raw)
        except Exception as exc:
            self._fail("page.html", exc)

    def stop(self, context: Any, keep: bool) -> None:
        """End the trace while the browser is still open. A successful attempt throws it away."""
        if not self._tracing:
            return
        self._tracing = False
        try:
            if keep:
                context.tracing.stop(path=str(self.dir / TRACE_NAME))
            else:
                context.tracing.stop()
        except Exception as exc:
            self._fail("trace", exc)

    def discard(self) -> None:
        """A confirmed success keeps no stop record."""
        if self.ready:
            shutil.rmtree(self.dir, ignore_errors=True)

    def finish(self) -> dict | None:
        """After the browser closed: build the request list from the archive, hash the files, write the summary."""
        if not self.ready:
            return None
        try:
            rows, counts = self._network()
        except Exception as exc:
            self._fail("network", exc)
            rows, counts = [], {"requests": 0, "failed": 0, "blocked": 0, "errors": 0, "bodies_cut": 0, "rows_left_out": 0}
        counts["console"] = len(self._console)
        counts["page_errors"] = sum(1 for row in self._console if row.get("type") == "pageerror")
        if self._console_dropped:
            self.notes.append(f"{self._console_dropped} more console lines were not kept (limit {CONSOLE_CAP}).")
        self._write("network.json", {"rows": rows, "blocked_by_app": self._blocked})
        self._write("console.json", {"lines": self._console})
        for name in (HAR_NAME, TRACE_NAME):
            path = self.dir / name
            try:
                if path.is_file() and path.stat().st_size > FILE_CAP:
                    size = path.stat().st_size
                    path.unlink()
                    self.notes.append(f"{FILES[name][1]} was {size} bytes, over the {FILE_CAP} byte limit, so it was not kept.")
            except Exception as exc:
                self._fail(name, exc)
        manifest: dict[str, Any] = {
            "attempt": self.attempt,
            "job_id": self.job_id,
            "started_at": self.started_at,
            "captured_at": self._page.get("captured_at") or _iso(),
            "page": self._page,
            "counts": counts,
            "caps": {"body_bytes": BODY_CAP, "rows": ROW_CAP, "console_lines": CONSOLE_CAP, "file_bytes": FILE_CAP, "page_source_bytes": HTML_CAP},
            "notes": self.notes,
            "errors": self.errors,
            "files": [],
        }
        manifest["files"] = self._hashes([name for name in FILES if name != RECORD_NAME])
        self._write(RECORD_NAME, manifest)
        manifest["files"] += self._hashes([RECORD_NAME])
        manifest["dir"] = str(self.dir)
        return manifest

    def _hashes(self, names: list[str]) -> list[dict]:
        found = []
        for name in names:
            path = self.dir / name
            try:
                if path.is_file():
                    path.chmod(0o600)
                    found.append({"name": name, "sha256": sha256_file(path), "size": path.stat().st_size})
            except Exception as exc:
                self._fail(name, exc)
        return found

    def _write(self, name: str, data: Any) -> None:
        try:
            (self.dir / name).write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
        except Exception as exc:
            self._fail(name, exc)

    # -- the request list ------------------------------------------------------------------------------
    def _network(self) -> tuple[list[dict], dict[str, int]]:
        counts = {"requests": 0, "failed": 0, "blocked": 0, "errors": 0, "bodies_cut": 0, "rows_left_out": 0}
        refused = {(row["method"], row["url"]) for row in self._blocked}
        seen: set[tuple[str, str]] = set()
        rows: list[dict] = []
        path = self.dir / HAR_NAME
        if path.is_file():
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
                entries = json.loads(archive.read("har.har")).get("log", {}).get("entries", []) if "har.har" in names else []
                counts["requests"] = len(entries)
                for index, entry in enumerate(entries):
                    if index >= ROW_CAP:
                        counts["rows_left_out"] = len(entries) - ROW_CAP
                        self.notes.append(f"{counts['rows_left_out']} more requests are only in the network archive (the list keeps {ROW_CAP}).")
                        break
                    row = self._row(index + 1, entry, archive, names, refused, counts)
                    seen.add((row["method"], row["url"]))
                    rows.append(row)
        else:
            self.errors.append("network: the archive was not written")
        for item in self._blocked:  # a refused request the archive did not list is still shown
            if (item["method"], item["url"]) in seen:
                continue
            seen.add((item["method"], item["url"]))
            counts["requests"] += 1
            counts["blocked"] += 1
            rows.append(
                {"n": len(rows) + 1, "started": item["at"], "method": item["method"], "url": item["url"], "type": item["type"], "status": None, "blocked": True, "failed": True}
            )
            rows[-1]["failure"] = "refused by the app: the site is not on the list it may open"
        links = {row["redirect_to"]: row["n"] for row in rows if row.get("redirect_to")}
        for row in rows:
            if row["url"] in links and links[row["url"]] < row["n"]:
                row["redirected_from"] = links[row["url"]]
        return rows, counts

    def _row(self, n: int, entry: dict, archive: zipfile.ZipFile, names: set[str], refused: set, counts: dict[str, int]) -> dict:
        request, response = entry.get("request") or {}, entry.get("response") or {}
        method, url = str(request.get("method") or ""), str(request.get("url") or "")
        status = response.get("status")
        status = status if isinstance(status, int) and status >= 0 else None
        failure = str(response.get("_failureText") or "")
        blocked = (method, url) in refused and status is None
        failed = status is None
        if blocked:
            counts["blocked"] += 1
            failure = f"refused by the app: the site is not on the list it may open ({failure})" if failure else "refused by the app"
        elif failed:
            counts["failed"] += 1
        elif status >= 400:
            counts["errors"] += 1
        req_headers = [[str(h.get("name")), str(h.get("value"))] for h in request.get("headers") or []]
        res_headers = [[str(h.get("name")), str(h.get("value"))] for h in response.get("headers") or []]
        sent = [[str(c.get("name")), str(c.get("value"))] for c in request.get("cookies") or []]
        if not sent:
            for name, value in req_headers:
                if name.lower() == "cookie":
                    sent += [[part.split("=", 1)[0].strip(), part.split("=", 1)[1] if "=" in part else ""] for part in value.split(";") if part.strip()]
        took = entry.get("time")
        row: dict[str, Any] = {
            "n": n,
            "started": str(entry.get("startedDateTime") or ""),
            "time_ms": round(float(took), 1) if isinstance(took, (int, float)) and took >= 0 else None,
            "timings": entry.get("timings") or {},
            "method": method,
            "url": url,
            "type": str(entry.get("_resourceType") or ""),
            "http": str(request.get("httpVersion") or ""),
            "status": status,
            "status_text": str(response.get("statusText") or ""),
            "failed": failed,
            "blocked": blocked,
            "failure": failure,
            "redirect_to": str(response.get("redirectURL") or ""),
            "server": ":".join(str(x) for x in (entry.get("serverIPAddress"), entry.get("_serverPort")) if x),
            "request_headers": req_headers,
            "response_headers": res_headers,
            "cookies_sent": sent,
            "cookies_set": [value for name, value in res_headers if name.lower() == "set-cookie"],
            "request_body": self._body(request.get("postData") or {}, archive, names, counts),
            "response_body": self._body(response.get("content") or {}, archive, names, counts),
        }
        params = (request.get("postData") or {}).get("params")
        if params:
            row["form_fields"] = [[str(p.get("name")), str(p.get("value", ""))] for p in params]
        return row

    def _body(self, part: dict, archive: zipfile.ZipFile, names: set[str], counts: dict[str, int]) -> dict:
        """A text body up to the cap. A binary one as its size and type; the archive holds the bytes."""
        mime = str(part.get("mimeType") or "")
        attached = str(part.get("_file") or "")
        inline = part.get("text")
        body: dict[str, Any] = {"mime": mime, "size": part.get("size") if isinstance(part.get("size"), int) and part.get("size") >= 0 else None, "text": "", "state": "none"}
        if attached:
            body["file"] = attached
        try:
            if attached and attached in names:
                size = archive.getinfo(attached).file_size
                body["size"] = size
                if not mime or _TEXT.search(mime):
                    with archive.open(attached) as handle:
                        body["text"] = handle.read(BODY_CAP).decode("utf-8", "replace")
                    body["state"] = "cut" if size > BODY_CAP else "whole"
                else:
                    body["state"] = "binary"
            elif isinstance(inline, str) and inline:
                raw = inline.encode("utf-8", "replace")
                body["size"] = body["size"] or len(raw)
                body["text"] = raw[:BODY_CAP].decode("utf-8", "replace")
                body["state"] = "cut" if len(raw) > BODY_CAP else "whole"
        except Exception as exc:
            body["state"] = "unreadable"
            self._fail("body", exc)
        if body["state"] == "cut":
            counts["bodies_cut"] += 1
        return body


# -- the stored side -------------------------------------------------------------------------------------
def _kept_as_evidence(store: Any, app: dict) -> bool:
    """A sent application and an unclear result keep everything the app saw."""
    if app.get("status") in ("submitted", "needs_verification"):
        return True
    action = store.c("actions").find_one({"idempotency_key": f"submit:{app.get('job_id')}"}, {"status": 1})
    return bool(action and action.get("status") in ("succeeded", "needs_verification"))


def _job_file(raw: Any, job_id: str, folder: str) -> Path | None:
    """A path is deleted only when it sits in this job's own `folder` under an artifact `jobs` directory."""
    if not raw:
        return None
    path = Path(str(raw))
    parts = path.parts
    safe = _SAFE.sub("_", job_id)
    for i in range(len(parts) - 3):
        if parts[i] == "jobs" and parts[i + 1] in (job_id, safe) and parts[i + 2] == folder:
            return path
    return None


def clear_stop_records(store: Any, app: dict | None) -> int:
    """Delete this application's stop records and the pictures of where an unsent attempt stopped.

    Called when a new attempt is queued or starts, and by Start over and Remove. Records and files go together; a
    file that lives on another computer is removed there when that computer next starts an attempt. Proof of a
    sent application, a receipt, and everything from an unclear result are never touched. Returns how many
    records were removed."""
    if not app or not app.get("job_id") or not app.get("_id") or _kept_as_evidence(store, app):
        return 0
    job_id = str(app["job_id"])
    arts = store.c("artifacts")
    query = {"job_id": job_id, "$or": [{"kind": KIND}, {"kind": "screenshot", "application_id": app["_id"], "label": {"$in": list(DIAGNOSTIC_SHOTS)}}]}
    removed = 0
    folders: set[Path] = set()
    for art in list(arts.find(query, {"path": 1, "kind": 1})):
        path = _job_file(art.get("path"), job_id, "stop" if art.get("kind") == KIND else "screenshots")
        try:
            if path is not None and path.is_file():
                path.unlink()
            if path is not None and art.get("kind") == KIND:
                folders.add(path.parent)
        except OSError:
            pass
        arts.delete_one({"_id": art["_id"]})
        removed += 1
    for folder in folders:
        shutil.rmtree(folder, ignore_errors=True)
    store.c("applications").update_one(
        {"_id": app["_id"], "evidence.screenshots": {"$type": "array"}},
        {"$pull": {"evidence.screenshots": {"label": {"$in": list(DIAGNOSTIC_SHOTS)}}}},
    )
    if removed:
        store.audit("stop_record_cleared", app["_id"], records=removed)
    return removed


class StopRecords:
    """The browser asks this for a recorder before an attempt and hands the finished record back afterwards."""

    def __init__(self, store: Any, artifact_dir: Path, enabled: bool = True, trace: bool = True) -> None:
        self.store = store
        self.artifact_dir = Path(artifact_dir)
        self.enabled = enabled
        self.trace = trace

    def _app(self, job_id: str) -> dict | None:
        return self.store.c("applications").find_one({"job_id": job_id, "status": {"$nin": ["removed", "withdrawn"]}}, sort=[("updated_at", -1)])

    def begin(self, job_id: str) -> StopRecorder | None:
        """A new attempt for this job: the earlier record goes, then a new recorder is handed out."""
        if not self.enabled or not job_id:
            return None
        try:
            clear_stop_records(self.store, self._app(job_id))
            self.sweep(job_id)
        except Exception as exc:
            log.warning("could not clear an earlier stop record: %s", type(exc).__name__)
        return StopRecorder(self.artifact_dir, job_id, self.trace)

    def landed(self, job_id: str, manifest: dict | None) -> list[str]:
        """Register each file of a finished record. The database gets names, sizes, hashes and counts only."""
        if not manifest or not manifest.get("files"):
            return []
        app = self._app(job_id) or {}
        folder = Path(str(manifest["dir"]))
        now = utcnow()
        ids = []
        for item in manifest["files"]:
            label, _title, media = FILES.get(item["name"], (item["name"], item["name"], "application/octet-stream"))
            art = {
                "_id": new_id("art"),
                "owner_id": job_id,
                "job_id": job_id,
                "application_id": app.get("_id"),
                "kind": KIND,
                "label": label,
                "attempt": manifest["attempt"],
                "path": str(folder / item["name"]),
                "filename": item["name"],
                "sha256": item["sha256"],
                "size": item["size"],
                "media_type": media,
                "captured_at": manifest.get("captured_at"),
                "created_at": now,
            }
            if label == "record":
                art["summary"] = {
                    "counts": manifest.get("counts") or {},
                    "caps": manifest.get("caps") or {},
                    "notes": manifest.get("notes") or [],
                    "errors": manifest.get("errors") or [],
                }
            self.store.c("artifacts").insert_one(art)
            ids.append(art["_id"])
        self.store.audit("stop_record_saved", app.get("_id") or job_id, files=len(ids))
        run = self.store.c("runs").find_one({"kind": "application", "params.job_id": job_id, "status": "running"}, {"_id": 1}, sort=[("updated_at", -1)])
        if run:
            self.store.run_event(run["_id"], SAVED_LINE)
        return ids

    def sweep(self, job_id: str = "") -> int:
        """Remove files whose record is gone: cleared from another computer, or never registered."""
        removed = 0
        cutoff = time.time() - ORPHAN_MINUTES * 60
        arts = self.store.c("artifacts")
        try:
            for folder in list(self.artifact_dir.glob("jobs/*/stop/*"))[:500]:
                if not folder.is_dir() or folder.stat().st_mtime > cutoff:
                    continue
                if arts.find_one({"kind": KIND, "attempt": folder.name}, {"_id": 1}) is None:
                    shutil.rmtree(folder, ignore_errors=True)
                    removed += 1
            if job_id:
                shots = self.artifact_dir / "jobs" / _SAFE.sub("_", job_id) / "screenshots"
                for label in DIAGNOSTIC_SHOTS:
                    for picture in shots.glob(f"*_{label}.png"):
                        if picture.stat().st_mtime <= cutoff and arts.find_one({"kind": "screenshot", "filename": picture.name}, {"_id": 1}) is None:
                            picture.unlink()
                            removed += 1
        except OSError:
            pass
        return removed
