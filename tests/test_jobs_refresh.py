"""Jobs page: checking the watched boards, what the strip above the list says, and the rows that are swapped in.

Boards, employers and jobs here are fictional. No test reaches a real job board."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from amorehiremepls.core import worker as worker_module
from amorehiremepls.core.config import Settings
from amorehiremepls.core.runtime import Runtime
from amorehiremepls.core.worker import Worker
from amorehiremepls.jobs.discovery import RawJob, SourceError
from amorehiremepls.web.jobs_refresh import board_name, is_active, new_job_ids, outcome, pick, progress, refresh_state, relative
from amorehiremepls.web.web import create_app
from tests.conftest import TEST_MONGO_URI
from tests.test_copy import DENYLIST, visible_text

NOW = datetime(2026, 11, 12, 18, 0, tzinfo=UTC)
STATIC = Path("src/amorehiremepls/web/static")


def _row(employer, kind="greenhouse", found=3, new=0, error=None):
    return {"type": kind, "key": employer.lower().replace(" ", ""), "employer": employer, "found": found, "new": new, "error": error}


def _done(rows, **extra):
    summary = {"ingested": sum(r["found"] for r in rows), "new": sum(r["new"] for r in rows), "errors": [], "sources": rows, **extra}
    return {"_id": "run_done", "kind": "discovery_request", "status": "completed", "summary": summary, "finished_at": NOW - timedelta(minutes=3)}


# -- the summary line -------------------------------------------------------------------------------------


def test_new_jobs_are_counted_and_the_boards_they_came_from_are_named():
    two = outcome(_done([_row("Example Corp", new=2), _row("Northwind", "lever", new=1), _row("Fabrikam", "ashby")]), watched=3)
    assert two == {"tone": "ok", "line": "3 new jobs.", "detail": "2 from Example Corp and 1 from Northwind. All 3 boards were read.", "new": 3, "failed": []}
    one = outcome(_done([_row("Example Corp", new=1)]), watched=1)
    assert one["line"] == "1 new job." and one["detail"] == "From Example Corp. The one board the app watches was read."


def test_nothing_new_still_reads_as_a_finished_check():
    quiet = outcome(_done([_row("Example Corp"), _row("Northwind", "lever")]), watched=2)
    assert quiet["tone"] == "ok" and quiet["line"] == "No new jobs." and quiet["detail"] == "All 2 boards were read."
    state = refresh_state([_done([_row("Example Corp")])], 1, now=NOW, worker_ok=True)
    assert state["checking"] is False and state["when_label"] == "Last checked" and state["checked_ago"] == "3 minutes ago"
    assert state["checked_at"].startswith("2026-11-12T17:57") and state["checked_title"]


def test_one_board_failing_is_a_warning_that_names_it_and_all_failing_is_a_failure():
    partial = outcome(_done([_row("Example Corp", new=1), _row("Northwind", "lever", found=0, error="unreachable")]), watched=2)
    assert partial["tone"] == "warn" and partial["line"] == "1 new job."
    assert "Northwind (Lever board) did not answer, so it was skipped this time." in partial["detail"]
    assert "1 of 2 boards were read." in partial["detail"] and partial["failed"] == ["Northwind (Lever board)"]
    wrong_name = outcome(_done([_row("Example Corp"), _row("Northwind", "ashby", found=0, error="not_found")]), watched=2)
    assert wrong_name["tone"] == "warn" and "Northwind (Ashby board) was not found. Check the company name on the board." in wrong_name["detail"]
    failed = outcome(_done([_row("Example Corp", found=0, error="unreachable"), _row("Northwind", "lever", found=0, error="unreachable")]), watched=2)
    assert failed["tone"] == "bad" and failed["line"] == "The check failed."
    assert failed["detail"] == "Example Corp (Greenhouse board) and Northwind (Lever board) did not answer, so they were skipped this time. The list is as it was."
    assert refresh_state([_done([_row("Example Corp", found=0, error="unreachable")])], 1, now=NOW, worker_ok=True)["when_label"] == "Last tried"


def test_never_checked_no_boards_and_a_check_that_stopped_early():
    assert outcome(None, watched=0)["line"] == "No job boards are being watched yet."
    assert outcome(None, watched=2) == {"tone": "neutral", "line": "These boards have not been checked yet.", "detail": "Check for new jobs to read them now.", "new": 0}
    assert outcome(_done([]), watched=0)["line"] == "There were no boards to check."
    stopped = outcome({"status": "failed", "summary": {"new": 2, "sources": [_row("Example Corp", new=2)]}}, watched=1)
    assert stopped["tone"] == "bad" and stopped["line"] == "The last check stopped before it finished." and "2 new jobs had already been added." in stopped["detail"]
    # a search saved before each board's outcome was kept
    assert outcome({"status": "completed", "summary": {"ingested": 4, "new": 1, "errors": []}}, watched=1) == {"tone": "ok", "line": "1 new job.", "detail": "", "new": 1}
    assert outcome({"status": "completed", "summary": {"ingested": 0, "new": 0, "errors": ["x"]}}, watched=1)["tone"] == "bad"


def test_long_lists_of_boards_are_cut_short():
    rows = [_row(f"Employer {i}", found=0, error="unreachable") for i in range(1, 6)] + [_row("Example Corp")]
    detail = outcome(_done(rows), watched=6)["detail"]
    assert "Employer 1 (Greenhouse board), Employer 2 (Greenhouse board), Employer 3 (Greenhouse board) and 2 more did not answer" in detail


def test_progress_says_which_board_is_being_read_and_why_a_check_has_not_started():
    queued = {"_id": "run_q", "kind": "discovery_request", "status": "queued", "created_at": NOW - timedelta(seconds=4)}
    assert progress(queued, NOW, worker_ok=True) == {"tone": "info", "line": "Starting the check.", "detail": "The app picks it up within a few seconds.", "stalled": False}
    off = progress(queued, NOW, worker_ok=False)
    assert off["tone"] == "warn" and off["stalled"] and off["line"] == "The check has not started." and "Background tasks are not running" in off["detail"]
    slow = progress({**queued, "created_at": NOW - timedelta(minutes=5)}, NOW, worker_ok=True)
    assert slow["stalled"] and slow["line"] == "The check has not started yet."
    step = {"index": 2, "total": 5, "source": {"type": "lever", "key": "northwind", "employer": "Northwind"}, "new": 1}
    running = {**queued, "status": "running", "reserved_until": NOW + timedelta(minutes=20), "progress": step}
    assert progress(running, NOW, worker_ok=True) == {"tone": "info", "line": "Checking Northwind (Lever board).", "detail": "Board 2 of 5. 1 new job so far.", "stalled": False}
    assert board_name({"type": "greenhouse", "key": "examplecorp"}) == "examplecorp (Greenhouse board)"
    state = refresh_state([running, _done([_row("Example Corp")])], 5, now=NOW, worker_ok=True)
    assert state["checking"] and state["line"] == "Checking Northwind (Lever board)." and state["checked_ago"] == "3 minutes ago"


def test_a_running_check_wins_over_a_queued_one_and_an_expired_hold_reads_as_stopped():
    queued = {"_id": "run_q", "status": "queued", "created_at": NOW}
    running = {"_id": "run_r", "status": "running", "created_at": NOW - timedelta(minutes=1), "reserved_until": NOW + timedelta(minutes=5)}
    assert pick([queued, running], NOW)[0]["_id"] == "run_r"
    expired = {**running, "reserved_until": NOW - timedelta(minutes=1), "updated_at": NOW - timedelta(minutes=40)}
    assert not is_active(expired, NOW) and not is_active({**queued, "requested": "cancel"}, NOW)
    active, last = pick([expired], NOW)
    assert active is None and last["_id"] == "run_r" and outcome(last, 1)["line"] == "The last check stopped before it finished."
    assert new_job_ids({"summary": {"new_job_ids": ["job_a", "job_b"]}}) == {"job_a", "job_b"} and new_job_ids(None) == set()


def test_relative_time_words():
    for seconds, words in (
        (5, "just now"),
        (60, "1 minute ago"),
        (60 * 14, "14 minutes ago"),
        (3600, "1 hour ago"),
        (3600 * 5, "5 hours ago"),
        (86400, "yesterday"),
        (86400 * 4, "4 days ago"),
    ):
        assert relative(NOW - timedelta(seconds=seconds), NOW) == words
    assert relative(None, NOW) == ""


# -- the page, the two new routes and the worker ------------------------------------------------------------


class _Board:
    def __init__(self, *args, **kwargs) -> None:
        pass


class _GoodBoard(_Board):
    calls = 0

    def fetch(self, key, employer=None):
        type(self).calls += 1
        text = "Permanent role selling to mid sized companies. " * 8
        return [
            RawJob("greenhouse", employer or key, title, f"https://boards.example/{key}/{n}", requisition_id=str(n), location="Lakeside", content_text=text)
            for n, title in ((1, "Account Executive"), (2, "Sales Manager"))
        ]


class _DownBoard(_Board):
    def fetch(self, key, employer=None):
        raise SourceError(f"ConnectError: https://boards.example/{key}")


@pytest.fixture
def client(store, tmp_path, monkeypatch):
    monkeypatch.setattr(worker_module, "SOURCES", {"greenhouse": _GoodBoard, "lever": _DownBoard})
    _GoodBoard.calls = 0
    s = Settings(
        mongodb_uri=TEST_MONGO_URI,
        mongodb_database=store.db.name,
        artifact_dir=tmp_path / "a",
        private_dir=tmp_path / "p",
        log_dir=tmp_path / "l",
        app_password="correct-horse-battery",
        app_secret="x" * 40,
        tracker_clone_dir=tmp_path / "clone",
        _env_file=None,
    )
    return TestClient(create_app(Runtime(s, store=store)), follow_redirects=False)


def _login(client) -> str:
    assert client.post("/login", data={"password": "correct-horse-battery"}).status_code == 303
    return re.search(r'name="csrf" value="([^"]+)"', client.get("/").text).group(1)


def _watch(client, *boards):
    client.app.state.rt.store.set_control("discovery_sources", [{"type": kind, "key": key, "employer": employer} for kind, key, employer in boards])


@pytest.mark.mongo
def test_signed_out_requests_are_sent_to_the_login_page(client):
    for path in ("/jobs/refresh", "/jobs/list"):
        r = client.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.post("/jobs/discover", data={"csrf": "x"}).status_code == 303


@pytest.mark.mongo
def test_empty_list_prompts_a_check_and_the_page_loads_its_own_script(client):
    _login(client)
    page = client.get("/jobs").text
    assert '<script src="/static/jobs.js" defer></script>' in page and 'id="jobs-refresh"' in page and 'id="jobs-list"' in page
    assert "No job boards are being watched yet." in page and "Check for new jobs" in page
    assert 'class="empty"' in page and "add a company's board under" in page
    assert page.count("data-jobs-check") == 1  # no board yet: the empty block has no second button
    assert "/static/jobs.js" not in client.get("/applications").text
    _watch(client, ("greenhouse", "examplecorp", "Example Corp"))
    page = client.get("/jobs").text
    assert "These boards have not been checked yet." in page and "No jobs yet. Check your job boards for new jobs" in page
    assert page.count("data-jobs-check") == 2
    fragment = client.get("/jobs/list").text
    assert "<html" not in fragment and fragment.lstrip().startswith('<div class="table-wrap">') and 'class="empty"' in fragment
    assert 'No jobs are "Good fit" right now.' in client.get("/jobs/list?status_filter=qualified").text


@pytest.mark.mongo
def test_pressing_check_queues_one_search_and_needs_the_form_token(client):
    csrf = _login(client)
    _watch(client, ("greenhouse", "examplecorp", "Example Corp"))
    store = client.app.state.rt.store
    assert client.post("/jobs/discover", data={"csrf": "wrong"}).status_code == 403
    assert client.post("/jobs/discover", data={}).status_code == 422
    assert store.c("runs").count_documents({}) == 0
    plain = client.post("/jobs/discover", data={"csrf": csrf, "status_filter": "qualified"})
    assert plain.status_code == 303 and plain.headers["location"] == "/jobs?ok=search_started&status_filter=qualified"
    again = client.post("/jobs/discover", data={"csrf": csrf}, headers={"Accept": "application/json"})
    assert again.status_code == 200 and again.json()["checking"] is True and again.headers["cache-control"] == "no-store"
    assert store.c("runs").count_documents({"kind": "discovery_request", "status": "queued"}) == 1  # the second press added nothing
    # nothing is running background tasks in this test, and the page says so instead of pretending to work
    state = client.get("/jobs/refresh").json()
    assert state["checking"] and state["stalled"] and state["line"] == "The check has not started."
    page = client.get("/jobs?ok=search_started").text
    assert "The app is checking your job boards for new jobs." in page and 'data-checking="1"' in page and 'class="jobs-list refreshing"' in page
    assert re.search(r"<button[^>]*disabled[^>]*>.*?Checking…", page, flags=re.S) and "<noscript>Reload this page" in page
    text = visible_text(page)
    assert not [pat for pat in DENYLIST if re.search(pat, text, flags=re.I)]


@pytest.mark.mongo
def test_one_pass_answers_every_waiting_request_and_marks_what_it_found(client):
    csrf = _login(client)
    _watch(client, ("greenhouse", "examplecorp", "Example Corp"), ("lever", "northwind", "Northwind"))
    rt = client.app.state.rt
    first = rt.store.create_run("discovery_request", {"trigger": "dashboard"})
    second = rt.store.create_run("discovery_request", {"trigger": "dashboard"})
    worker = Worker(rt)
    assert worker.tick()["searches"] == 1 and _GoodBoard.calls == 1
    runs = {r["_id"]: r for r in rt.store.c("runs").find({})}
    assert {r["status"] for r in runs.values()} == {"completed"} and len(runs) == 2  # no third task was made for the pass
    summary = runs[first["_id"]]["summary"]
    assert summary["new"] == 2 and len(summary["new_job_ids"]) == 2 and runs[first["_id"]].get("progress") is None
    assert [(r["employer"], r["found"], r["new"], r["error"]) for r in summary["sources"]] == [("Example Corp", 2, 2, None), ("Northwind", 0, 0, "unreachable")]
    assert runs[second["_id"]]["summary"]["answered_by"] == first["_id"]
    assert worker.tick()["searches"] == 0 and _GoodBoard.calls == 1  # not read again without a new request

    state = client.get("/jobs/refresh").json()
    assert state["checking"] is False and state["tone"] == "warn" and state["line"] == "2 new jobs." and state["new"] == 2
    assert "From Example Corp." in state["detail"] and "Northwind (Lever board) did not answer" in state["detail"] and state["checked_ago"] == "just now"
    fragment = client.get("/jobs/list").text
    assert fragment.count(" is-new") == 2 and fragment.count('<span class="pill accent new-mark">New</span>') == 2
    assert not re.search(r"\b[0-9a-f]{24}\b", fragment)  # no long ids in the rows
    page = client.get("/jobs").text
    assert 'data-tone="warn"' in page and 'data-checking="0"' in page and "2 new jobs." in page and 'href="#i-alert"' in page
    assert not [pat for pat in DENYLIST if re.search(pat, visible_text(page), flags=re.I)]

    # the next check finds the same two jobs: nothing is new, the marks are gone, and it still reads as a finished check
    rt.store.set_control("discovery_sources", [{"type": "greenhouse", "key": "examplecorp", "employer": "Example Corp"}])
    assert client.post("/jobs/discover", data={"csrf": csrf}, headers={"Accept": "application/json"}).json()["checking"] is True
    rt.store.set_control("worker_heartbeat", {"at": datetime.now(UTC)})
    assert client.get("/jobs/refresh").json()["line"] == "Starting the check."
    assert worker.tick()["searches"] == 1
    state = client.get("/jobs/refresh").json()
    assert (state["tone"], state["line"], state["detail"]) == ("ok", "No new jobs.", "The one board the app watches was read.")
    assert " is-new" not in client.get("/jobs/list").text and rt.store.c("jobs").count_documents({}) == 2


@pytest.mark.mongo
def test_a_scheduled_pass_keeps_each_boards_outcome_and_a_broken_board_does_not_leave_it_running(client, monkeypatch):
    _login(client)
    _watch(client, ("lever", "northwind", "Northwind"), ("ashby", "fabrikam", "Fabrikam"))
    rt = client.app.state.rt
    summary = Worker(rt).run_discovery(trigger="discovery:2026-11-12T09:00")
    assert [r["error"] for r in summary["sources"]] == ["unreachable", "unsupported"]
    run = rt.store.c("runs").find_one({"kind": "discovery"})
    assert run["status"] == "completed" and run["started_at"] and run["finished_at"]
    assert client.get("/jobs/refresh").json()["tone"] == "bad"

    class _Broken(_Board):
        def fetch(self, key, employer=None):
            raise RuntimeError("unexpected board answer")

    monkeypatch.setattr(worker_module, "SOURCES", {"lever": _Broken})
    rt.store.create_run("discovery_request", {"trigger": "dashboard"})
    assert Worker(rt).tick()["searches"] == 1
    stopped = rt.store.c("runs").find_one({"kind": "discovery_request"})
    assert stopped["status"] == "failed" and stopped["error"] == "stopped early: RuntimeError"
    state = client.get("/jobs/refresh").json()
    assert state["checking"] is False and state["line"] == "The last check stopped before it finished." and state["when_label"] == "Last tried"


# -- the stylesheet block and the page script ----------------------------------------------------------------


def test_motion_is_local_short_and_off_under_reduced_motion():
    css = (STATIC / "app.css").read_text()
    start, end = css.index("/* ---------- Jobs page: check for new jobs"), css.index("/* ---------- end of Jobs page: check for new jobs")
    block = css[start:end]
    assert css.count("/* ---------- Jobs page: check for new jobs") == 1 and start < css.index("/* ---------- lists ---------- */")
    assert "animation: jobs-row-in 220ms var(--ease) backwards" in block and "calc(var(--i, 0) * 50ms)" in block
    reduced = block[block.index("@media (prefers-reduced-motion: reduce)") :]
    for selector in (".jr-track i", ".jobs-list.reloaded tbody tr", "tr.is-new td", "button .icon"):
        assert selector in reduced
    assert "animation: none !important" in reduced
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", block)  # design tokens only
    js = (STATIC / "jobs.js").read_text()
    assert "ROW_STEP_CAP = 12" in js and "innerHTML = html" in js
    assert "http://" not in js and "https://" not in js and "cdn." not in js.lower()
    assert "form[data-jobs-check]" in js and "ev.preventDefault()" in js
