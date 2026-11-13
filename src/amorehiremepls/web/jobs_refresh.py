"""What the Jobs page says about checking the watched job boards.

One search task reads every watched board: either the one asked for on the page, or the one the schedule starts.
This module turns the latest of those tasks into the sentences, the tone and the "new" marks the page shows.
Nothing here reads the database or the network, so every sentence can be tested with plain dicts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from amorehiremepls.core.util import to_local, zone_label
from amorehiremepls.web.ui_settings import status_label

SEARCH_KINDS = ("discovery", "discovery_request")
ACTIVE = frozenset({"queued", "claimed", "running"})
FINISHED = frozenset({"completed", "failed"})
# A started search with no lease that is older than this reads as stopped, not as still checking.
STALE_RUN_SECONDS = 1800
# After this long in the queue the page stops saying "within a few seconds".
SLOW_START_SECONDS = 90
NAMES_SHOWN = 3


def _aware(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _names(names: list[str]) -> str:
    """'A', 'A and B', 'A, B and C', 'A, B, C and 2 more'."""
    shown, rest = names[:NAMES_SHOWN], len(names) - NAMES_SHOWN
    if rest > 0:
        return f"{', '.join(shown)} and {rest} more"
    if len(shown) <= 1:
        return "".join(shown)
    return f"{', '.join(shown[:-1])} and {shown[-1]}"


def board_who(src: dict) -> str:
    """The employer when one was saved, else the company name on the board."""
    return str(src.get("employer") or src.get("key") or "").strip() or "A board"


def board_name(src: dict) -> str:
    """'Example Corp (Greenhouse board)': enough to find the row under Job boards the app watches."""
    kind = status_label(src.get("type")) if src.get("type") else ""
    return f"{board_who(src)} ({kind})" if kind else board_who(src)


def relative(at: Any, now: datetime) -> str:
    """'just now', '1 minute ago', '3 hours ago', 'yesterday', '4 days ago'. jobs.js keeps the same words current."""
    when = _aware(at)
    if when is None:
        return ""
    seconds = max(0, int((now - when).total_seconds()))
    if seconds < 45:
        return "just now"
    minutes = max(1, round(seconds / 60))
    if minutes < 60:
        return f"{_plural(minutes, 'minute')} ago"
    hours = round(minutes / 60)
    if hours < 24:
        return f"{_plural(hours, 'hour')} ago"
    days = round(hours / 24)
    return "yesterday" if days == 1 else f"{days} days ago"


def is_active(run: dict | None, now: datetime) -> bool:
    """Waiting to start, or being read right now. A started search whose hold ran out is not active: it stopped."""
    if not run or run.get("status") not in ACTIVE:
        return False
    if run["status"] == "queued":
        return run.get("requested") != "cancel"
    until = _aware(run.get("reserved_until"))
    if until is not None:
        return until > now
    started = _aware(run.get("started_at") or run.get("created_at"))
    return started is None or (now - started).total_seconds() < STALE_RUN_SECONDS


def pick(runs: list[dict], now: datetime) -> tuple[dict | None, dict | None]:
    """(the search in progress, the latest search that ended) from this person's search tasks."""
    active = [r for r in runs if is_active(r, now)]
    # A running search wins over a queued one; among equals the oldest is the one being worked on.
    active.sort(key=lambda r: (r.get("status") == "queued", _aware(r.get("created_at")) or now))
    ended = [r for r in runs if r.get("status") in FINISHED or (r.get("status") in ACTIVE and r.get("status") != "queued" and not is_active(r, now))]
    ended.sort(key=lambda r: _aware(r.get("created_at")) or now, reverse=True)
    return (active[0] if active else None), (ended[0] if ended else None)


def ended_at(run: dict | None) -> datetime | None:
    if not run:
        return None
    return _aware(run.get("finished_at") or run.get("updated_at") or run.get("created_at"))


def new_job_ids(run: dict | None) -> set[str]:
    """Jobs first seen by the latest search. They keep the New mark until the next search ends."""
    if not run:
        return set()
    return {str(i) for i in ((run.get("summary") or {}).get("new_job_ids") or [])}


def outcome(run: dict | None, watched: int) -> dict:
    """Tone, headline and one more sentence for the latest search that ended. `watched` is how many boards are on
    the list now. Tones: ok (it finished, with or without new jobs), warn (some boards were skipped), bad (nothing
    could be read, or it stopped early), neutral (never checked)."""
    if run is None:
        if not watched:
            return {"tone": "neutral", "line": "No job boards are being watched yet.", "detail": "Add a company's board under Job boards the app watches.", "new": 0}
        return {"tone": "neutral", "line": "These boards have not been checked yet.", "detail": "Check for new jobs to read them now.", "new": 0}
    summary = run.get("summary") or {}
    new = int(summary.get("new") or 0)
    headline = "No new jobs." if not new else f"{_plural(new, 'new job')}."
    if run.get("status") != "completed":
        kept = f"{_plural(new, 'new job')} had already been added." if new else "The list is as it was."
        return {"tone": "bad", "line": "The last check stopped before it finished.", "detail": f"{kept} Check again to read every board.", "new": new}
    rows = summary.get("sources")
    if rows is None:
        # A search saved before each board's outcome was kept: only the totals are known.
        if summary.get("errors") and not summary.get("ingested"):
            return {"tone": "bad", "line": "The check failed.", "detail": "No board could be read. The list is as it was.", "new": new}
        if summary.get("errors"):
            return {"tone": "warn", "line": headline, "detail": "Some boards could not be read and were skipped this time.", "new": new}
        return {"tone": "ok", "line": headline, "detail": "", "new": new}
    if not rows:
        return {"tone": "warn", "line": "There were no boards to check.", "detail": "Add a company's board under Job boards the app watches.", "new": 0}
    missed = [r for r in rows if r.get("error")]
    not_found = [board_name(r) for r in missed if r.get("error") == "not_found"]
    silent = [board_name(r) for r in missed if r.get("error") != "not_found"]
    problems: list[str] = []
    if silent:
        problems.append(f"{_names(silent)} did not answer, so {'it was' if len(silent) == 1 else 'they were'} skipped this time.")
    if not_found:
        problems.append(f"{_names(not_found)} {'was' if len(not_found) == 1 else 'were'} not found. Check the company name on the board.")
    if len(missed) == len(rows):
        return {"tone": "bad", "line": "The check failed.", "detail": " ".join([*problems, "The list is as it was."]), "new": new, "failed": [board_name(r) for r in missed]}
    parts: list[str] = []
    found_in = [r for r in rows if int(r.get("new") or 0) > 0]
    if len(found_in) == 1:
        parts.append(f"From {board_who(found_in[0])}.")
    elif found_in:
        parts.append(f"{_names([f'{r.get("new")} from {board_who(r)}' for r in found_in])}.")
    read = len(rows) - len(missed)
    if not missed:
        parts.append("The one board the app watches was read." if len(rows) == 1 else f"All {len(rows)} boards were read.")
    else:
        parts.append(f"{read} of {len(rows)} boards were read.")
    return {"tone": "warn" if missed else "ok", "line": headline, "detail": " ".join([*parts, *problems]), "new": new, "failed": [board_name(r) for r in missed]}


def progress(run: dict, now: datetime, worker_ok: bool) -> dict:
    """Headline and one more sentence while a search is waiting or reading boards."""
    if run.get("status") == "queued":
        waited = (now - (_aware(run.get("created_at")) or now)).total_seconds()
        if not worker_ok:
            detail = "Background tasks are not running, so nothing is picking it up. It starts by itself once they are."
            return {"tone": "warn", "line": "The check has not started.", "detail": detail, "stalled": True}
        if waited > SLOW_START_SECONDS:
            return {"tone": "warn", "line": "The check has not started yet.", "detail": "The app may be finishing another task. It starts when that is done.", "stalled": True}
        return {"tone": "info", "line": "Starting the check.", "detail": "The app picks it up within a few seconds.", "stalled": False}
    step = run.get("progress") or {}
    if step.get("source"):
        found = int(step.get("new") or 0)
        so_far = f" {_plural(found, 'new job')} so far." if found else ""
        return {"tone": "info", "line": f"Checking {board_name(step['source'])}.", "detail": f"Board {step.get('index')} of {step.get('total')}.{so_far}", "stalled": False}
    return {"tone": "info", "line": "Checking your job boards.", "detail": "", "stalled": False}


def refresh_state(runs: list[dict], watched: int, *, now: datetime, worker_ok: bool) -> dict:
    """Everything the Jobs page strip shows, as plain values. `checking` turning false is how an open page knows
    the check ended and the rows are worth loading again."""
    active, last = pick(runs, now)
    when = ended_at(last)
    body = progress(active, now, worker_ok) if active else {**outcome(last, watched), "stalled": False}
    stopped = bool(last) and outcome(last, watched)["tone"] == "bad"
    local = to_local(when)
    return {
        "checking": active is not None,
        "stalled": bool(body.get("stalled")),
        "tone": body["tone"],
        "line": body["line"],
        "detail": body.get("detail") or "",
        "new": int(body.get("new") or 0),
        "when_label": "Last tried" if stopped else "Last checked",
        "checked_at": when.isoformat() if when else "",
        "checked_ago": relative(when, now),
        "checked_title": f"{local.strftime('%Y-%m-%d %H:%M')} {zone_label()}" if local else "",
    }
