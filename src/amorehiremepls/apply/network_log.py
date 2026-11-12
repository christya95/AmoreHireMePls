"""Per-job browser requests, saved as Chromium sees each one.

One job's rows never include another job. Secret headers are dropped before the row is stored.
A self-identification answer is not kept. A short response from a data request is.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from amorehiremepls.core.logging_config import redact
from amorehiremepls.core.util import new_id, utcnow

log = logging.getLogger(__name__)

BODY_LIMIT = 2048
ROW_CAP = 200
MAX_URL = 2000
_XHR = {"xhr", "fetch"}
_SECRET_HEADERS = {"cookie", "set-cookie", "authorization", "proxy-authorization"}
_TOKEN_NAME = re.compile(r"(?i)token|secret|password|api[-_]?key")
_TOKEN_VALUE = re.compile(r"(?i)^bearer\s+\S{8,}$|^(?:sk-|gh[pousr]_|ops_)[A-Za-z0-9_-]{8,}$|^eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")


def sensitive_values(plan: Any) -> list[str]:
    """Answers marked personal on this plan. Location text is not included."""
    found: list[str] = []
    for step in getattr(plan, "steps", None) or []:
        if not getattr(step, "sensitive", False):
            continue
        items = [getattr(step, "value", ""), getattr(step, "text", ""), *(getattr(step, "options", None) or [])]
        for item in items:
            text = str(item or "").strip()
            if len(text) >= 2 and text not in found:
                found.append(text)
    return found


def _get(obj: Any, name: str, default: Any = "") -> Any:
    try:
        value = getattr(obj, name)
    except Exception:
        return default
    return default if value is None else value


def _headers(obj: Any) -> dict[str, str]:
    try:
        raw = getattr(obj, "headers", {}) or {}
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items() if value is not None}


def _post_data(request: Any) -> str:
    try:
        data = request.post_data
    except Exception:
        return ""
    return data or ""


def _looks_like_token(value: str) -> bool:
    text = value.strip()
    if _TOKEN_VALUE.search(text):
        return True
    return " " not in text and re.fullmatch(r"[A-Za-z0-9._~+/=-]{24,}", text) is not None


def _header_secrets(headers: dict[str, str]) -> list[str]:
    """Values that must never be stored: cookie, authorization, and anything that looks like a token."""
    found: list[str] = []
    for name, value in headers.items():
        text = value.strip()
        if len(text) < 4:
            continue
        if name.lower() in _SECRET_HEADERS or _TOKEN_NAME.search(name) or _looks_like_token(text):
            if text not in found:
                found.append(text)
    return found


def _scrub(text: str, secrets: list[str]) -> str:
    cleaned = redact(text or "")
    for secret in secrets:
        if secret and secret in cleaned:
            cleaned = cleaned.replace(secret, "***")
    return cleaned


def _read_text(response: Any) -> str:
    try:
        reader = getattr(response, "text", None)
        if reader is None:
            return ""
        text = reader() if callable(reader) else reader
    except Exception:
        return ""
    return text or ""


def _response_text(response: Any, resource: str, post: str, sensitive: list[str], header_secrets: list[str]) -> str:
    """Short text for a data request. Personal answers and secret header values are left out."""
    if resource not in _XHR:
        return ""
    if any(secret and secret in (post or "") for secret in sensitive):
        return ""
    text = _read_text(response)
    stripped = text.strip()
    for secret in sensitive:
        if not secret:
            continue
        if stripped == secret or (len(secret) >= 12 and secret in text):
            return ""
    return _scrub(text, header_secrets)[:BODY_LIMIT]


def _timing(request: Any) -> str:
    try:
        timing = request.timing or {}
    except Exception:
        return ""
    if not isinstance(timing, dict):
        return ""
    try:
        start = float(timing.get("requestStart", -1))
        end = float(timing.get("responseEnd", -1))
    except (TypeError, ValueError):
        return ""
    if start < 0 or end < 0 or end < start:
        return ""
    return f"{int(end - start)} ms"


def _status(response: Any) -> int | None:
    try:
        return int(response.status)
    except Exception:
        return None


class NetworkLog:
    """Rows for one job, capped to the newest ones. Each call writes before it returns."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def begin(self, job_id: str, request: Any, sensitive: list[str] | None = None) -> str:
        """Store the request now, before the response and before the form run returns."""
        extra = _header_secrets(_headers(request))
        personal = [item for item in (sensitive or []) if len(item) >= 4]
        url = _scrub(str(_get(request, "url"))[:MAX_URL], extra + personal)
        row_id = new_id("net")
        row = {
            "id": row_id,
            "at": utcnow(),
            "method": (str(_get(request, "method")) or "GET")[:16],
            "url": url,
            "resource_type": str(_get(request, "resource_type"))[:32],
            "status": None,
            "timing": "",
            "body": "",
        }
        self._push(job_id, row)
        return row_id

    def finish(self, job_id: str, row_id: str | None, request: Any, response: Any, sensitive: list[str] | None = None) -> None:
        """Fill in status, timing, and a short data response on the row saved when the request started."""
        secrets = list(sensitive or [])
        headers = {**_headers(request), **_headers(response)}
        extra = _header_secrets(headers)
        resource = str(_get(request, "resource_type"))[:32]
        if not row_id:
            row_id = self.begin(job_id, request, secrets)
        body = _response_text(response, resource, _post_data(request), secrets, extra)
        fields = {
            "rows.$.status": _status(response),
            "rows.$.timing": _scrub(_timing(request), extra)[:80],
            "rows.$.body": body,
            "rows.$.resource_type": resource,
            "updated_at": utcnow(),
        }
        self.store.c("network").update_one({"job_id": job_id, "rows.id": row_id}, {"$set": fields})

    def fail(self, job_id: str, row_id: str | None, request: Any, sensitive: list[str] | None = None) -> None:
        if not row_id:
            row_id = self.begin(job_id, request, sensitive)
        fields = {"rows.$.timing": "did not finish", "updated_at": utcnow()}
        self.store.c("network").update_one({"job_id": job_id, "rows.id": row_id}, {"$set": fields})

    def for_job(self, job_id: str) -> list[dict]:
        """Newest first. Header secrets are removed again here, before anything is shown."""
        doc = self.store.c("network").find_one({"job_id": job_id}, {"rows": {"$slice": -ROW_CAP}})
        rows = list((doc or {}).get("rows") or [])
        rows.reverse()
        shown: list[dict] = []
        for row in rows:
            shown.append(
                {
                    "id": row.get("id") or "",
                    "at": row.get("at"),
                    "method": row.get("method") or "",
                    "url": redact(str(row.get("url") or ""))[:MAX_URL],
                    "type": row.get("resource_type") or "",
                    "status": row.get("status"),
                    "timing": redact(str(row.get("timing") or ""))[:80],
                    "body": redact(str(row.get("body") or ""))[:BODY_LIMIT],
                }
            )
        return shown

    def clear(self, job_id: str) -> None:
        """Delete this job's rows only. The application, resume, and screenshots stay."""
        self.store.c("network").delete_many({"job_id": job_id})

    def _push(self, job_id: str, row: dict) -> None:
        self.store.c("network").update_one(
            {"job_id": job_id},
            {"$push": {"rows": {"$each": [row], "$slice": -ROW_CAP}}, "$set": {"updated_at": utcnow()}, "$setOnInsert": {"job_id": job_id}},
            upsert=True,
        )


def watch_network(context: Any, recorder: NetworkLog, job_id: str, sensitive: list[str] | None = None) -> None:
    """Attach to this browser context. Each request is written before the callback returns."""
    # The caller's own list is kept, so a value it adds during the run (a sign-in secret) is left out from then on.
    secrets = sensitive if isinstance(sensitive, list) else list(sensitive or [])
    ids: dict[int, str] = {}

    def on_request(request: Any) -> None:
        try:
            ids[id(request)] = recorder.begin(job_id, request, secrets)
        except Exception:
            log.exception("could not record a network row")

    def on_response(response: Any) -> None:
        try:
            request = response.request
            recorder.finish(job_id, ids.get(id(request)), request, response, secrets)
        except Exception:
            log.exception("could not record a network row")

    def on_failed(request: Any) -> None:
        try:
            recorder.fail(job_id, ids.get(id(request)), request, secrets)
        except Exception:
            log.exception("could not record a network row")

    context.on("request", on_request)
    context.on("response", on_response)
    context.on("requestfailed", on_failed)
