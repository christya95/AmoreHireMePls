"""How the Background tasks page groups runs.

Still waiting: queued, starting, running, or paused for the owner. Already finished: completed, cancelled, or failed.
Hiding a finished row is a flag on the run. It does not delete the job, the application, or the history.
"""

from __future__ import annotations

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
