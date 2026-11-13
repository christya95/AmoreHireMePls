"""Pasting several job links into "Add a job" on the Jobs page.

`split_links()` turns whatever was pasted (one link per line, or separated by spaces or commas, in any mix) into the
links to add and the entries that are not web links. `banner()` turns what then happened into the one banner the page
shows. Nothing here reads the database or the network, so every sentence can be tested with plain values. Each link is
then added by the code path a single link always used (`Discovery.import_link`), so job identity, duplicates and
LinkedIn job ids are decided there and nothing is fetched.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from amorehiremepls.core.util import extract_linkedin_job_id, normalize_url
from amorehiremepls.web.jobs_refresh import _aware, _names, _plural, ended_at

# How many links one paste may add. More than this and the rest are left for a second paste.
MAX_LINKS = 50
# Longer than this is not read at all: fifty long links fit several times over.
MAX_PASTE_CHARS = 60_000
MAX_LINK_CHARS = 2_000
# How much of an entry that could not be read is shown and kept.
SHORT_CHARS = 48
FAILED_KEPT = 20
# The activity entry one paste leaves behind. The banner and the New marks are read from it.
EVENT = "job_links_added"
PASTE_ID_RE = re.compile(r"^paste_[0-9a-f]{16}$")
# Jobs from the latest paste keep the New mark until the next check for new jobs ends, and for a day at most.
NEW_MARK_HOURS = 24

# White space always separates entries. A comma does only when a new address starts right after it, because an
# address may hold a comma of its own.
_SPLIT_RE = re.compile(r"\s+|,(?=\s*[A-Za-z][A-Za-z0-9+.-]*:)")
_WRAPPING = ",;<>\"'()[]{}"
_WEB_RE = re.compile(r"^https?://", re.I)
# Something a person meant as an address: another scheme, or a bare site name. Plain words around links are not entries.
_ADDRESS_RE = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.-]*:.+|www\..+|[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}(?:[/?#:].*)?)$")


def short(entry: str) -> str:
    """An entry as the banner names it: without the scheme of a web link, cut to a readable length."""
    text = re.sub(r"^https?://(?:www\.)?", "", entry.strip(), flags=re.I)
    return text if len(text) <= SHORT_CHARS else text[: SHORT_CHARS - 1] + "…"


def _is_web_link(entry: str) -> bool:
    if not _WEB_RE.match(entry) or len(entry) > MAX_LINK_CHARS:
        return False
    try:
        host = urlsplit(entry).hostname or ""
    except ValueError:
        return False
    named = "." in host.strip(".") or host == "localhost"
    return named and all(ch.isalnum() or ch in ".-" for ch in host)


def _same_job_key(link: str) -> str:
    """Two entries with this key are the same posting: the LinkedIn job id, else the address without tracking parts."""
    li = extract_linkedin_job_id(link)
    return f"li:{li}" if li else normalize_url(link)


def split_links(text: str | None) -> dict[str, Any]:
    """{"links": web links to add, in pasted order, each once, at most MAX_LINKS; "unreadable": entries that look like
    an address but are not an http(s) link; "left_out": how many links were over the limit; "too_long": the paste was
    not read}. Blank lines, repeated links and plain words are ignored."""
    out: dict[str, Any] = {"links": [], "unreadable": [], "left_out": 0, "too_long": False}
    text = text or ""
    if len(text) > MAX_PASTE_CHARS:
        out["too_long"] = True
        return out
    seen: set[str] = set()
    for raw in _SPLIT_RE.split(text):
        entry = raw.strip(_WRAPPING).rstrip(".")
        if not entry:
            continue
        if _is_web_link(entry):
            key = _same_job_key(entry)
            if key in seen:
                continue
            seen.add(key)
            if len(out["links"]) < MAX_LINKS:
                out["links"].append(entry)
            else:
                out["left_out"] += 1
        elif (_WEB_RE.match(entry) or _ADDRESS_RE.match(entry)) and entry not in seen:
            seen.add(entry)
            out["unreadable"].append(entry)
    return out


def banner(result: dict) -> dict[str, str]:
    """{"text", "tone"} for one paste. `result` holds added, known, failed (shortened names), failed_count, left_out,
    too_long, hint (an entry was not an http(s) link) and extras_ignored. Tones: ok, warn (some links were added or
    known and something else needs a look), bad (nothing could be used)."""
    if result.get("too_long"):
        return {"tone": "bad", "text": f"That was too much text to read as links. Paste up to {MAX_LINKS} links at a time. Nothing was added."}
    added, known, left_out = int(result.get("added") or 0), int(result.get("known") or 0), int(result.get("left_out") or 0)
    names = [str(n) for n in (result.get("failed") or [])]
    failed = max(int(result.get("failed_count") or 0), len(names))
    rest = failed - len(names[:3])
    listed = f"{', '.join(names[:3])} and {rest} more" if rest > 0 else _names(names)
    hint = " A link starts with https://." if result.get("hint") else ""
    if not added and not known and not failed:
        return {"tone": "bad", "text": "No link was found in what you pasted. Paste one or more links that start with https://. Nothing was added."}
    parts: list[str] = []
    if added:
        parts.append(
            f"{_plural(added, 'job')} {'was' if added == 1 else 'were'} added and checked against your rules. {'It is' if added == 1 else 'They are'} marked New in the list."
        )
    if known and (added or failed):
        parts.append(f"{known} {'was' if known == 1 else 'were'} already in your list.")
    elif known:
        parts.append("That job was already in your list." if known == 1 else f"All {known} jobs were already in your list.")
    if failed and (added or known):
        parts.append(f"{_plural(failed, 'link')} could not be read: {listed}.{hint}")
    elif failed:
        parts.append(f"{'That link could not be read' if failed == 1 else f'None of the {failed} links could be read'}: {listed}.{hint}")
    if not added:
        parts.append("Nothing was added.")
    if left_out:
        parts.append(
            f"Only the first {MAX_LINKS} links were read. {left_out} more {'was' if left_out == 1 else 'were'} left out, so paste {'it' if left_out == 1 else 'those'} again."
        )
    if result.get("extras_ignored"):
        parts.append("The employer, title, job ID, location and ad text were not used, because they fit one link only.")
    tone = "bad" if not added and not known else ("warn" if failed or left_out or result.get("extras_ignored") else "ok")
    return {"tone": tone, "text": " ".join(parts)}


def pasted_job_ids(pastes: list[dict], last_search: dict | None, now: datetime) -> set[str]:
    """Jobs added by the latest paste that added any, newest paste first in `pastes`. They carry the same New mark as
    jobs a check found and lose it the same way, when the next check for new jobs ends, or after a day."""
    since = now - timedelta(hours=NEW_MARK_HOURS)
    ended = ended_at(last_search)
    if ended is not None and ended > since:
        since = ended
    for paste in pastes:
        ids = {str(i) for i in ((paste.get("data") or {}).get("new_job_ids") or [])}
        at = _aware(paste.get("created_at"))
        if ids:
            return ids if at is not None and at > since else set()
    return set()
