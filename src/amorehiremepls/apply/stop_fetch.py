"""Bring a stop record's files onto the computer that is showing them.

The computer that ran the attempt keeps the files. Another dashboard reads them through that machine's
hash-verified download route (`AMORE_STOP_RECORD_ORIGIN`) and stores them under its own artifact folder,
at `jobs/<job_id>/stop/<attempt>/`. The hash stored with the record has to match before a file is kept.
The bytes stay in that folder: they are not logged, not put in the database, and not added to a backup.
"""

from __future__ import annotations

import hashlib
import http.cookiejar
import logging
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlsplit

from amorehiremepls.apply.stop_record import _SAFE, FILE_CAP, KIND, attempt_dir
from amorehiremepls.core.util import sha256_file

log = logging.getLogger(__name__)

BROUGHT = "brought"
FAILED = "The files could not be brought over from the computer that ran this attempt."
_BUDGET_S = 20.0
_NAME = re.compile(r"[A-Za-z0-9_.-]+")
_openers: dict[str, urllib.request.OpenerDirector] = {}
_opener_guard = threading.Lock()
_attempt_locks: dict[str, threading.Lock] = {}
_attempt_guard = threading.Lock()


class Opened(NamedTuple):
    path: Path | None
    mismatch: bool


def reset_brought_sessions() -> None:
    """Drop cached sign-ins to the other dashboard. Tests use this between servers."""
    with _opener_guard:
        _openers.clear()


def _attempt_lock(key: str) -> threading.Lock:
    with _attempt_guard:
        lock = _attempt_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _attempt_locks[key] = lock
        return lock


def _origin(value: str) -> str | None:
    raw = (value or "").strip().rstrip("/")
    if not raw:
        return None
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        return None
    if parts.query or parts.fragment or parts.path not in ("", "/"):
        return None
    return f"{parts.scheme}://{parts.netloc}"


def _same_dashboard(settings: Any, origin: str) -> bool:
    """True when `origin` is this process, so a fetch would ask this process for a file it does not have."""
    parts = urlsplit(origin)
    host = (parts.hostname or "").lower()
    port = parts.port or (443 if parts.scheme == "https" else 80)
    own = {(settings.bind_host or "").lower(), "127.0.0.1", "localhost", "::1"}
    if getattr(settings, "publish_host", ""):
        own.add(str(settings.publish_host).strip().lower())
    base = urlsplit(settings.dashboard_base_url())
    if base.hostname:
        own.add(base.hostname.lower())
    return host in own and port == (base.port or settings.port)


def mirror_file(artifact_dir: Path, art: dict) -> Path | None:
    """Where this computer keeps a file that was recorded on another one."""
    name = str(art.get("filename") or "")
    folder = attempt_dir(artifact_dir, str(art.get("job_id") or ""), str(art.get("attempt") or ""))
    if folder is None or not _NAME.fullmatch(name):
        return None
    path = folder / name
    try:
        if not path.resolve().is_relative_to(Path(artifact_dir).resolve()):
            return None
    except OSError:
        return None
    return path


def _recorded(artifact_dir: Path, art: dict) -> Path | None:
    """The recorded path when it is a file inside this artifact folder. The hash is not checked."""
    raw = art.get("path")
    if not raw:
        return None
    root = Path(artifact_dir).resolve()
    path = Path(str(raw))
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        path = path.resolve()
    except OSError:
        return None
    if not path.is_relative_to(root) or not path.is_file():
        return None
    return path


def _matches(path: Path | None, art: dict) -> bool:
    return bool(path is not None and path.is_file() and art.get("sha256") and sha256_file(path) == art.get("sha256"))


def verified_copy(artifact_dir: Path, art: dict) -> Path | None:
    """A file on this computer whose hash still matches the record."""
    recorded = _recorded(artifact_dir, art)
    if _matches(recorded, art):
        return recorded
    mirror = mirror_file(artifact_dir, art)
    if recorded is not None or not _matches(mirror, art) or mirror is None:
        return None
    _mark(mirror.parent)
    return mirror


def open_for_download(rt: Any, art: dict) -> Opened:
    """The file a download may send. A local file whose hash changed is a mismatch and is not replaced."""
    root = rt.settings.artifact_dir
    recorded = _recorded(root, art)
    if recorded is not None and not _matches(recorded, art):
        return Opened(None, True)
    path = verified_copy(root, art)
    if path is None:
        bring_stop_files(rt, [art])
        path = verified_copy(root, art)
    return Opened(path, False)


def drop_brought_copies(store: Any, artifact_dir: Path, job_id: str | None) -> None:
    """A new attempt deleted the record. Remove the copy this computer brought over for that job."""
    if not job_id:
        return
    root = Path(artifact_dir).resolve()
    base = root / "jobs" / _SAFE.sub("_", str(job_id)) / "stop"
    if not base.is_dir():
        return
    for attempt in list(base.iterdir()):
        try:
            resolved = attempt.resolve()
        except OSError:
            continue
        if not attempt.is_dir() or not resolved.is_relative_to(root) or not (resolved / BROUGHT).is_file():
            continue
        if store.c("artifacts").find_one({"kind": KIND, "attempt": attempt.name}, {"_id": 1}):
            continue
        shutil.rmtree(resolved, ignore_errors=True)


def bring_stop_files(rt: Any, arts: list[dict]) -> str | None:
    """Store missing files under this computer's artifact folder. One sentence when any file is still missing."""
    groups: dict[str, list[dict]] = {}
    for art in arts:
        if art.get("kind") == KIND:
            groups.setdefault(f"{art.get('job_id')}:{art.get('attempt')}", []).append(art)
    missing = False
    for key, group in groups.items():
        with _attempt_lock(key):
            if not _bring_group(rt, group):
                missing = True
    return FAILED if missing else None


def _mark(folder: Path) -> None:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        os.chmod(folder, 0o700)
        marker = folder / BROUGHT
        if not marker.exists():
            marker.write_bytes(b"")
            os.chmod(marker, 0o600)
    except OSError:
        pass


def _bring_group(rt: Any, arts: list[dict]) -> bool:
    root = rt.settings.artifact_dir
    needed: list[dict] = []
    ok = True
    for art in arts:
        recorded = _recorded(root, art)
        if recorded is not None:
            ok = ok and _matches(recorded, art)
            continue
        mirror = mirror_file(root, art)
        if _matches(mirror, art) and mirror is not None:
            _mark(mirror.parent)
            continue
        needed.append(art)
    if not needed:
        return ok
    origin = _origin(getattr(rt.settings, "stop_record_origin", ""))
    if origin is None or _same_dashboard(rt.settings, origin):
        return False
    deadline = time.monotonic() + _BUDGET_S
    opener = _session(origin)
    password = rt.settings.app_password.get_secret_value()
    if not _signed_in(opener, origin, password, deadline):
        return False
    for art in needed:
        dest = mirror_file(root, art)
        if _fetch(opener, origin, art, dest, deadline) and dest is not None:
            _mark(dest.parent)
        else:
            ok = False
    return ok


class _Guard(urllib.request.HTTPRedirectHandler):
    def __init__(self, origin: str) -> None:
        super().__init__()
        self._host = urlsplit(origin).netloc

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        if urlsplit(newurl).netloc != self._host:
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _session(origin: str) -> urllib.request.OpenerDirector:
    with _opener_guard:
        opener = _openers.get(origin)
        if opener is None:
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), _Guard(origin))
            _openers[origin] = opener
        return opener


def _discard(resp: Any, limit: int = 2_000_000) -> None:
    """Read and drop a response body so it is never kept or logged."""
    total = 0
    while total < limit:
        chunk = resp.read(65536)
        if not chunk:
            return
        total += len(chunk)
    resp.close()


def _signed_in(opener: urllib.request.OpenerDirector, origin: str, password: str, deadline: float) -> bool:
    data = urllib.parse.urlencode({"password": password}).encode()
    req = urllib.request.Request(f"{origin}/login", data=data, method="POST")
    try:
        with opener.open(req, timeout=max(1.0, deadline - time.monotonic())) as resp:
            _discard(resp)
            return urlsplit(resp.geturl()).path != "/login"
    except urllib.error.HTTPError as exc:
        _discard(exc)
        exc.close()
        log.warning("stop record was not brought over: %s", type(exc).__name__)
        return False
    except (OSError, urllib.error.URLError) as exc:
        log.warning("stop record was not brought over: %s", type(exc).__name__)
        return False


def _drop_empty(folder: Path | None) -> None:
    if folder is None:
        return
    try:
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
    except OSError:
        pass


def _fetch(opener: urllib.request.OpenerDirector, origin: str, art: dict, dest: Path | None, deadline: float) -> bool:
    size = art.get("size")
    if dest is None or not isinstance(size, int) or size < 0 or size > FILE_CAP or not art.get("sha256") or not art.get("_id"):
        return False
    kind = "png" if str(art.get("media_type") or "").startswith("image/") else "file"
    quoted = urllib.parse.quote(str(art["_id"]), safe="")
    req = urllib.request.Request(f"{origin}/documents/{quoted}/download?kind={kind}", method="GET")
    part = dest.with_name(dest.name + ".part")
    try:
        with opener.open(req, timeout=max(1.0, deadline - time.monotonic())) as resp:
            if urlsplit(resp.geturl()).path.startswith("/login"):
                _discard(resp)
                return False
            return _save(resp, part, dest, int(size), str(art["sha256"]))
    except urllib.error.HTTPError as exc:
        _discard(exc)
        exc.close()
        part.unlink(missing_ok=True)
        _drop_empty(dest.parent if dest is not None else None)
        log.warning("stop record was not brought over: %s", type(exc).__name__)
        return False
    except (OSError, urllib.error.URLError) as exc:
        part.unlink(missing_ok=True)
        _drop_empty(dest.parent if dest is not None else None)
        log.warning("stop record was not brought over: %s", type(exc).__name__)
        return False


def _save(resp: Any, part: Path, dest: Path, expected: int, sha: str) -> bool:
    try:
        part.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(part.parent, 0o700)
        digest = hashlib.sha256()
        got = 0
        with part.open("wb") as out:
            while got <= expected:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                got += len(chunk)
                if got > expected:
                    break
                digest.update(chunk)
                out.write(chunk)
        if got != expected or digest.hexdigest() != sha:
            part.unlink(missing_ok=True)
            _drop_empty(part.parent)
            return False
        os.chmod(part, 0o600)
        os.replace(part, dest)
        return True
    except OSError:
        part.unlink(missing_ok=True)
        _drop_empty(part.parent)
        return False
