"""Small shared helpers: time, hashing, IDs, URL normalization."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gh_src",
    "lever-source",
    "source",
    "src",
    "ref",
    "refid",
    "trk",
    "trackingid",
    "fbclid",
    "gclid",
    "position",
    "pageNum",
    "refId",
    "originalSubdomain",
    "eBP",
    "currentJobId",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def local_zone() -> ZoneInfo:
    """The candidate's time zone (`AMORE_TIMEZONE`). An unknown name falls back to UTC rather than failing a page."""
    from .config import get_settings

    name = (get_settings().timezone or "UTC").strip()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def zone_label() -> str:
    """Short name for the configured zone in copy: 'Europe/London' -> 'London', 'America/Vancouver' -> 'Vancouver'."""
    key = local_zone().key
    return key.rsplit("/", 1)[-1].replace("_", " ")


def local_now() -> datetime:
    return datetime.now(local_zone())


def to_local(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(local_zone())


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_json(obj: Any) -> str:
    return sha256_bytes(json.dumps(obj, sort_keys=True, default=str, ensure_ascii=False).encode())


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def slugify(text: str, max_len: int = 60) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")
    return text[:max_len] or "x"


def normalize_url(url: str) -> str:
    """Canonical form for dedup: lowercase scheme/host, no fragment, no tracking params, no trailing slash."""
    url = url.strip()
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/+", "/", parts.path).rstrip("/")
    tracking = {p.lower() for p in _TRACKING_PARAMS}
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False) if k.lower() not in tracking]
    query.sort()
    return urlunsplit(("https", host, path, urlencode(query), ""))


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", text.lower())).strip()


def normalize_employer(name: str) -> str:
    n = normalize_text(name)
    for suffix in (" inc", " incorporated", " ltd", " limited", " llc", " corp", " corporation", " co", " plc", " canada"):
        if n.endswith(suffix):
            n = n[: -len(suffix)].strip()
    return n


def redact_free(text: str) -> str:
    from .logging_config import redact

    return redact(text)


LINKEDIN_JOB_RE = re.compile(r"linkedin\.com/jobs/view/(?:[^/]*-)?(\d{6,})|currentJobId=(\d{6,})")


def extract_linkedin_job_id(url: str) -> str | None:
    m = LINKEDIN_JOB_RE.search(url)
    if not m:
        return None
    return m.group(1) or m.group(2)
