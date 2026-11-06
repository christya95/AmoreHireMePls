"""How the Background tasks page groups runs.

Still waiting: queued, starting, running, or paused for the owner. Already finished: completed, cancelled, or failed.
Hiding a finished row is a flag on the run. It does not delete the job, the application, or the history.
"""

from __future__ import annotations

from datetime import datetime

from amorehiremepls.core.accounts import account_is_active

IN_QUEUE = frozenset({"queued", "claimed", "running", "waiting", "paused"})
PAUSED = frozenset({"waiting", "paused"})
RUNNING = frozenset({"claimed", "running"})

SEND_KINDS = frozenset({"application_submit", "outreach_first_contact", "connection_request", "follow_up", "email_send"})


def in_queue(status: str | None) -> bool:
    return (status or "") in IN_QUEUE


def can_cancel(run: dict) -> bool:
    """A task can be cancelled from the list only before it has started."""
    return run.get("status") == "queued" and run.get("requested") != "cancel"


def offer_cancel(run: dict, started: bool) -> bool:
    """The task page offers Cancel until a submission or a message send is underway."""
    return in_queue(run.get("status")) and not started


def can_hide(run: dict) -> bool:
    """Finished, cancelled, and failed rows can leave the list. A waiting row cannot."""
    return not run.get("dismissed") and not in_queue(run.get("status"))


def partition(runs: list[dict]) -> tuple[list[dict], list[dict]]:
    """Visible rows, newest first, split into still waiting and already finished."""
    visible = [r for r in runs if not r.get("dismissed")]
    waiting = [r for r in visible if in_queue(r.get("status"))]
    earlier = [r for r in visible if not in_queue(r.get("status"))]
    return waiting, earlier


def next_application(waiting: list[dict]) -> dict | None:
    """The oldest queued application. That is the one started next. A job search request is not an application."""
    queued = [r for r in waiting if r.get("status") == "queued" and r.get("kind") == "application" and r.get("requested") != "cancel"]
    if not queued:
        return None
    return min(queued, key=lambda r: r.get("created_at") or "")


def queue_summary(waiting: list[dict], next_label: str | None) -> str:
    """One line: how many are waiting to start, running, and paused, plus the name of the next application."""
    if not waiting:
        return "Nothing is waiting."
    to_start = sum(1 for r in waiting if r.get("status") == "queued")
    running = sum(1 for r in waiting if r.get("status") in RUNNING)
    paused = sum(1 for r in waiting if r.get("status") in PAUSED)
    line = f"{_phrase(to_start, 'waiting to start')}, {_phrase(running, 'running')}, {_phrase(paused, 'paused for you')}."
    if next_label:
        line += f" Next: {next_label}."
    elif to_start and not any(r.get("kind") == "application" and r.get("status") == "queued" for r in waiting):
        line += " A job search is waiting. It does not apply to a job."
    return line


def next_name(run: dict | None, jobs: dict) -> str | None:
    if run is None:
        return None
    job = jobs.get((run.get("params") or {}).get("job_id"))
    if not job:
        return "the oldest application"
    employer = (job.get("employer") or "").strip()
    title = (job.get("title") or "").strip()
    if employer and title:
        return f"{employer}, {title}"
    return employer or title or "the oldest application"


def side_effect_started(store, run: dict) -> bool:
    """True once a submission or a message send for this task is in progress. Cancel is refused then."""
    if store.c("actions").find_one({"run_id": run["_id"], "status": "executing", "kind": {"$in": sorted(SEND_KINDS)}}):
        return True
    job_id = (run.get("params") or {}).get("job_id")
    if not job_id:
        return False
    return store.c("applications").find_one({"job_id": job_id, "status": "executing", "provenance.origin": "local"}) is not None


def owner_state_for(run: dict, user: dict | None, accounts_exist: bool) -> str:
    """active when the worker will claim this task, missing when nobody owns it, inactive when that person is skipped."""
    if not run.get("user_id"):
        return "missing" if accounts_exist else "active"
    if not account_is_active(user):
        return "inactive"
    return "active"


def queued_reason(run: dict, *, now: datetime, worker_at: datetime | None, owner_state: str, emergency: bool) -> str | None:
    """One sentence under a queued task. owner_state is active, missing, or inactive.

    A missing owner is why a task sits in Queued while the worker is still checking in: the claim only
    sees tasks that belong to someone it is allowed to run."""
    if run.get("status") != "queued":
        return None
    waited = _ago(now, run.get("created_at"), waiting=True)
    seen = _worker_seen(now, worker_at)
    if emergency:
        return f"It has not started. The emergency stop is on, so nothing is being picked up. It has been waiting {waited}. The worker {seen}."
    if run.get("kind") != "application":
        return f"It has not started. The worker only picks up application tasks. It has been waiting {waited}. The worker {seen}."
    if owner_state == "missing":
        return f"It has not started. This task has no owner, so the worker cannot pick it up. It has been waiting {waited}. The worker {seen}."
    if owner_state == "inactive":
        return f"It has not started. This task belongs to someone the worker is not running. It has been waiting {waited}. The worker {seen}."
    until = run.get("reserved_until")
    if isinstance(until, datetime) and until > now:
        return f"It has not started. A worker is holding it. It has been waiting {waited}. The worker {seen}."
    return f"It has not started. It has been waiting {waited}. The worker {seen}."


def _worker_seen(now: datetime, at: datetime | None) -> str:
    if at is None:
        return "has not checked in"
    seconds = max(0, int((now - at).total_seconds()))
    if seconds < 20:
        return "just checked in"
    if seconds < 90:
        return f"last checked in {seconds} seconds ago"
    minutes = max(1, seconds // 60)
    unit = "minute" if minutes == 1 else "minutes"
    return f"last checked in {minutes} {unit} ago"


def _ago(now: datetime, at: datetime | None, *, waiting: bool) -> str:
    if not isinstance(at, datetime):
        return "a while" if waiting else "some time"
    seconds = max(0, int((now - at).total_seconds()))
    if seconds < 20:
        return "a few seconds" if waiting else "just now"
    if seconds < 90:
        return f"{seconds} seconds"
    minutes = max(1, seconds // 60)
    unit = "minute" if minutes == 1 else "minutes"
    return f"{minutes} {unit}"


def _phrase(n: int, words: str) -> str:
    """'none running', '1 task waiting to start', '2 tasks waiting to start', '1 paused for you'."""
    if words == "waiting to start":
        if n == 0:
            return "none waiting to start"
        if n == 1:
            return "1 task waiting to start"
        return f"{n} tasks waiting to start"
    if n == 0:
        return f"none {words}"
    return f"{n} {words}"
