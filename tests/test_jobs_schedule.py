"""Checking the watched boards at set times of day: which time is due, what one due time starts, what a repeated
check costs and moves, and what the Jobs page says about it.

Boards, employers and jobs are fictional, no test reaches a real board, and the worker's clock is set by the test."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from amorehiremepls.core import worker as worker_module
from amorehiremepls.core.ai import FakeProvider
from amorehiremepls.core.config import Settings
from amorehiremepls.core.runtime import Runtime
from amorehiremepls.core.schedule import GRACE, due_slot, ignored_entries, next_time, parse_times, slot_key
from amorehiremepls.core.worker import SCHEDULE_CONTROL, Worker, next_slot_description, published_schedule
from amorehiremepls.jobs.discovery import RawJob
from amorehiremepls.web.jobs_refresh import automatic, day_time
from amorehiremepls.web.web import create_app
from tests.conftest import TEST_MONGO_URI
from tests.test_copy import DENYLIST, visible_text

ZONE = ZoneInfo("America/Toronto")  # the zone the test environment sets
TWICE = [(8, 0), (16, 30)]


def _at(hour, minute=0, day=13):
    return datetime(2026, 11, day, hour, minute, tzinfo=ZONE)


# -- which time of day is due ---------------------------------------------------------------------------------


def test_the_setting_is_a_list_of_times_of_day_and_mistakes_are_left_out():
    assert parse_times("08:00,16:30") == TWICE and parse_times(" 16:30 , 08:00,08:00 ") == TWICE and parse_times("8:00") == [(8, 0)]
    assert parse_times("") == [] and parse_times(None) == [] and parse_times("24:00, 12:60, noon, 9, 9:5, :30, -1:00") == []
    assert ignored_entries("08:00,16:30") == 0 and ignored_entries("08:00, noon ,, 25:00") == 2
    assert Settings(_env_file=None).discovery_schedule == ""  # no schedule unless the operator sets one


def test_a_time_of_day_is_due_once_it_passes_and_only_for_a_few_hours():
    assert due_slot(TWICE, _at(7, 59)) is None
    assert due_slot(TWICE, _at(8)) == _at(8) and slot_key(_at(8)) == "discovery:2026-11-13T08:00"
    assert due_slot(TWICE, _at(13, 59)) == _at(8) and due_slot(TWICE, _at(14)) is None and GRACE == timedelta(hours=6)
    assert due_slot(TWICE, _at(16, 30)) == _at(16, 30) and due_slot(TWICE, _at(22, 29)) == _at(16, 30) and due_slot(TWICE, _at(22, 30)) is None
    assert due_slot([(23, 30)], _at(0, 10)) == _at(23, 30, day=12)  # a late time of day is still due just after midnight
    assert due_slot([], _at(9)) is None


def test_several_missed_times_are_made_up_for_by_one_check_not_one_each():
    every_two_hours = [(8, 0), (10, 0), (12, 0)]
    assert due_slot(every_two_hours, _at(12, 5)) == _at(12)
    assert due_slot(every_two_hours, _at(11, 59)) == _at(10)


def test_the_next_time_is_today_or_tomorrow():
    assert next_time(TWICE, _at(7)) == _at(8) and next_time(TWICE, _at(8)) == _at(16, 30)
    assert next_time(TWICE, _at(17)) == _at(8, day=14) and next_time([], _at(7)) is None


# -- what the strip says --------------------------------------------------------------------------------------


def test_the_strip_says_when_the_next_automatic_check_is_due_and_when_the_last_one_ran():
    now = _at(9, 15).astimezone(UTC)
    ran = {"status": "completed", "finished_at": _at(8, 0).astimezone(UTC)}
    assert automatic(TWICE, ZONE, ran, now=now, worker_ok=True) == "Next automatic check today at 16:30. The last one ran today at 08:00."
    evening = _at(18).astimezone(UTC)
    late = {"status": "completed", "finished_at": _at(16, 31).astimezone(UTC)}
    assert automatic(TWICE, ZONE, late, now=evening, worker_ok=True) == "Next automatic check tomorrow at 08:00. The last one ran today at 16:31."
    assert automatic(TWICE, ZONE, None, now=now, worker_ok=True) == "Next automatic check today at 16:30. None has run yet."
    stopped = {"status": "failed", "updated_at": _at(16, 32, day=12).astimezone(UTC)}
    assert automatic(TWICE, ZONE, stopped, now=now, worker_ok=True) == "Next automatic check today at 16:30. The last one, yesterday at 16:32, stopped before it finished."
    assert automatic(TWICE, ZONE, ran, now=now, worker_ok=False) == "Automatic checks are on hold because background tasks are not running."
    assert automatic([], ZONE, ran, now=now, worker_ok=True) == ""  # no schedule: the strip says nothing about it
    assert day_time(_at(8, 0, day=9), now) == "on Nov 9 at 08:00" and day_time(None, now) == ""
    # times set in another zone are shown in the dashboard's own zone
    assert automatic([(8, 0)], ZoneInfo("Europe/London"), None, now=_at(1).astimezone(UTC), worker_ok=True).startswith("Next automatic check today at 03:00.")


# -- the worker -----------------------------------------------------------------------------------------------


class _Board:
    calls = 0
    text = {1: "Permanent role selling to mid sized companies. " * 8, 2: "Permanent role leading a regional sales team. " * 8}

    def __init__(self, *args, **kwargs) -> None:
        pass

    def fetch(self, key, employer=None):
        type(self).calls += 1
        return [
            RawJob("greenhouse", employer or key, title, f"https://boards.example/{key}/{n}", requisition_id=str(n), location="Lakeside", content_text=type(self).text[n])
            for n, title in ((1, "Account Executive"), (2, "Sales Manager"))
        ]


class _Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    clock = _Clock(_at(7, 59))
    monkeypatch.setattr(worker_module, "local_now", clock)
    monkeypatch.setattr(worker_module, "SOURCES", {"greenhouse": _Board})
    monkeypatch.setattr(_Board, "calls", 0)
    monkeypatch.setattr(_Board, "text", dict(_Board.text))
    return clock


def _runtime(store, tmp_path, schedule="08:00,16:30", provider=None):
    s = Settings(
        mongodb_uri=TEST_MONGO_URI,
        mongodb_database=store.db.name,
        artifact_dir=tmp_path / "a",
        private_dir=tmp_path / "p",
        log_dir=tmp_path / "l",
        browser_session_dir=tmp_path / "s",
        tracker_clone_dir=tmp_path / "clone",
        app_password="correct-horse-battery",
        app_secret="x" * 40,
        discovery_schedule=schedule,
        _env_file=None,
    )
    rt = Runtime(s, store=store, provider=provider)
    store.set_control("discovery_sources", [{"type": "greenhouse", "key": "examplecorp", "employer": "Example Corp"}])
    return rt


def _order(store):
    return [j["title"] for j in store.c("jobs").find({}).sort("updated_at", -1)]


@pytest.mark.mongo
def test_a_due_time_starts_one_check_and_a_restart_does_not_start_it_again(store, tmp_path, clock):
    rt = _runtime(store, tmp_path)
    worker = Worker(rt)
    assert worker.tick()["scheduled"] == 0 and _Board.calls == 0 and store.c("runs").count_documents({}) == 0  # 07:59
    clock.now = _at(8)
    done = worker.tick()
    assert (done["scheduled"], done["searches"], _Board.calls) == (1, 1, 1)
    run = store.c("runs").find_one({})
    assert run["kind"] == "discovery" and run["status"] == "completed" and run["params"] == {"trigger": "discovery:2026-11-13T08:00", "schedule_slot": "discovery:2026-11-13T08:00"}
    assert run["summary"]["new"] == 2 and run["started_at"] and run["finished_at"]
    clock.now = _at(8, 1)
    assert worker.tick()["scheduled"] == 0 and _Board.calls == 1
    # the worker is replaced (a deploy, a crash): the slot is already taken, so nothing is read again
    clock.now = _at(8, 5)
    restarted = Worker(rt)
    assert restarted.tick()["scheduled"] == 0 and restarted.tick()["searches"] == 0 and _Board.calls == 1
    assert store.c("runs").count_documents({}) == 1 and store.db["scheduler_slots"].count_documents({}) == 1
    clock.now = _at(16, 30)
    assert restarted.tick()["scheduled"] == 1 and _Board.calls == 2 and store.c("runs").count_documents({"status": "completed"}) == 2


@pytest.mark.mongo
def test_a_worker_that_was_off_through_several_times_runs_one_check_not_a_burst(store, tmp_path, clock):
    rt = _runtime(store, tmp_path, schedule="08:00,10:00,12:00")
    clock.now = _at(12, 5)  # first pass after being off since early morning
    worker = Worker(rt)
    assert worker.due_slots() == ["discovery:2026-11-13T12:00"]
    assert [worker.tick()["scheduled"] for _ in range(3)] == [1, 0, 0] and _Board.calls == 1
    clock.now = _at(12, 6)
    assert Worker(rt).tick()["scheduled"] == 0 and _Board.calls == 1 and store.c("runs").count_documents({}) == 1


@pytest.mark.mongo
def test_nothing_is_started_while_no_board_is_watched(store, tmp_path, clock):
    rt = _runtime(store, tmp_path)
    store.set_control("discovery_sources", [])
    clock.now = _at(8)
    worker = Worker(rt)
    assert worker.tick()["scheduled"] == 0 and store.c("runs").count_documents({}) == 0 and store.db["scheduler_slots"].count_documents({}) == 0
    # a board added within the grace time is read once for the time of day that passed
    store.set_control("discovery_sources", [{"type": "greenhouse", "key": "examplecorp", "employer": "Example Corp"}])
    clock.now = _at(9)
    assert [worker.tick()["scheduled"] for _ in range(2)] == [1, 0] and _Board.calls == 1


@pytest.mark.mongo
def test_a_scheduled_check_and_a_pressed_button_are_one_pass(store, tmp_path, clock):
    rt = _runtime(store, tmp_path)
    worker = Worker(rt)
    # the button was pressed just before the time came due: the request answers the slot, nothing is queued next to it
    asked = store.create_run("discovery_request", {"trigger": "dashboard"})
    clock.now = _at(8)
    done = worker.tick()
    assert (done["scheduled"], done["searches"], _Board.calls) == (0, 1, 1) and store.c("runs").count_documents({}) == 1
    assert store.c("runs").find_one({"_id": asked["_id"]})["params"]["answers_slot"] == "discovery:2026-11-13T08:00"
    # the time came due first and the button was pressed before the worker got to it: still one pass for both
    clock.now = _at(16, 30)
    assert worker.run_schedule() == 1
    pressed = store.create_run("discovery_request", {"trigger": "dashboard"})
    assert worker.run_requested_discovery() == 1 and _Board.calls == 2
    assert store.c("runs").find_one({"_id": pressed["_id"]})["summary"]["answered_by"] == store.c("runs").find_one({"params.schedule_slot": {"$exists": True}})["_id"]
    assert worker.tick()["searches"] == 0 and _Board.calls == 2


@pytest.mark.mongo
def test_a_scheduled_check_does_not_start_while_another_check_is_reading_the_boards(store, tmp_path, clock):
    rt = _runtime(store, tmp_path)
    busy = store.create_run("discovery_request", {"trigger": "dashboard"})
    now = datetime.now(UTC)
    store.update_run(busy["_id"], status="running", started_at=now, reserved_by="another-worker", reserved_until=now + timedelta(minutes=20))
    clock.now = _at(8)
    worker = Worker(rt)
    done = worker.tick()
    assert (done["scheduled"], done["searches"], _Board.calls) == (0, 0, 0) and store.c("runs").count_documents({}) == 1
    # and it is not started later for the same time of day either: the check under way answered it
    store.update_run(busy["_id"], status="completed", finished_at=datetime.now(UTC))
    clock.now = _at(8, 2)
    assert worker.tick()["scheduled"] == 0 and _Board.calls == 0


@pytest.mark.mongo
def test_a_check_that_finds_nothing_new_costs_no_model_call_and_moves_no_job(store, tmp_path, clock):
    provider = FakeProvider({"ExtractOutput": lambda _user: {"employment_type": "permanent"}})
    rt = _runtime(store, tmp_path, provider=provider)
    worker = Worker(rt)
    clock.now = _at(8)
    assert worker.tick()["searches"] == 1 and store.c("jobs").count_documents({}) == 2
    first_pass_calls = len(provider.calls)
    assert first_pass_calls >= 2  # each new posting was read by the model once
    # a job pasted by hand after the morning check sits on top of the list
    pasted, created = rt.discovery().import_link("https://careers.northwind.example/roles/77", "Northwind", "Regional Lead")
    assert created and _order(store) == ["Regional Lead", "Sales Manager", "Account Executive"]
    before = {j["_id"]: j for j in store.c("jobs").find({})}

    clock.now = _at(16, 30)
    assert worker.tick()["searches"] == 1 and _Board.calls == 2
    run = store.c("runs").find_one({"params.schedule_slot": "discovery:2026-11-13T16:30"})
    assert run["summary"]["new"] == 0 and run["summary"]["ingested"] == 2 and run["summary"]["new_job_ids"] == []
    assert len(provider.calls) == first_pass_calls  # nothing new: no model call, so no cost
    after = {j["_id"]: j for j in store.c("jobs").find({})}
    assert _order(store) == ["Regional Lead", "Sales Manager", "Account Executive"]  # the pasted job did not sink
    for job_id, job in after.items():
        assert job["updated_at"] == before[job_id]["updated_at"] and job["status"] == before[job_id]["status"]
        assert (job.get("latest_snapshot") or {}).get("fetched_at") == (before[job_id].get("latest_snapshot") or {}).get("fetched_at")
    seen_again = [j for j in after.values() if j["_id"] != pasted["_id"]]
    assert all(j["last_seen_at"] > j["updated_at"] for j in seen_again)  # when the board last listed it is still kept

    # a posting whose text changed does rise, and the one that did not change stays where it was
    _Board.text[1] = "Permanent role selling to large companies, with travel. " * 8
    store.create_run("discovery_request", {"trigger": "dashboard"})
    assert worker.tick()["searches"] == 1
    assert _order(store) == ["Account Executive", "Regional Lead", "Sales Manager"]
    changed = store.c("jobs").find_one({"title": "Account Executive"})
    assert "large companies" in changed["latest_snapshot"]["text"] and changed["updated_at"] > before[changed["_id"]]["updated_at"]
    assert store.c("jobs").find_one({"title": "Sales Manager"})["updated_at"] == before[store.c("jobs").find_one({"title": "Sales Manager"})["_id"]]["updated_at"]


# -- the page -------------------------------------------------------------------------------------------------


def _login(client) -> str:
    assert client.post("/login", data={"password": "correct-horse-battery"}).status_code == 303
    return re.search(r'name="csrf" value="([^"]+)"', client.get("/").text).group(1)


@pytest.mark.mongo
def test_the_page_says_nothing_about_automatic_checks_when_no_time_is_set(store, tmp_path, clock):
    rt = _runtime(store, tmp_path, schedule="")
    client = TestClient(create_app(rt), follow_redirects=False)
    _login(client)
    Worker(rt).tick()
    assert store.get_control(SCHEDULE_CONTROL)["times"] == [] and published_schedule(rt)[0] == [] and next_slot_description(rt) == "disabled"
    page = client.get("/jobs").text
    assert 'id="jr-auto-wrap" hidden' in page and "automatic" not in visible_text(page).lower()
    assert client.get("/jobs/refresh").json()["auto"] == ""
    assert store.c("runs").count_documents({}) == 0 and _Board.calls == 0  # and nothing is checked by itself


@pytest.mark.mongo
def test_the_page_says_when_the_next_automatic_check_is_due_even_from_a_dashboard_with_no_schedule_of_its_own(store, tmp_path, clock):
    rt = _runtime(store, tmp_path)  # the worker's settings
    worker = Worker(rt)
    clock.now = _at(8)
    assert worker.tick()["searches"] == 1
    saved = store.get_control(SCHEDULE_CONTROL)
    assert saved["times"] == ["08:00", "16:30"] and saved["timezone"] == "America/Toronto"
    # a dashboard on another computer, where the schedule setting is empty, reads what the worker saved
    elsewhere = _runtime(store, tmp_path, schedule="")
    assert published_schedule(elsewhere) == (TWICE, ZONE) and next_slot_description(elsewhere) == "daily at 08:00,16:30 America/Toronto"
    client = TestClient(create_app(elsewhere), follow_redirects=False)
    _login(client)
    sentence = r"Next automatic check (today|tomorrow) at (08:00|16:30)\. The last one ran (today|yesterday) at \d\d:\d\d\."
    state = client.get("/jobs/refresh").json()
    assert re.fullmatch(sentence, state["auto"]) and state["line"] == "2 new jobs."
    page = client.get("/jobs").text
    assert re.search(r'<span id="jr-auto-wrap"><br><small id="jr-auto">' + sentence + "</small></span>", page)
    assert not [pat for pat in DENYLIST if re.search(pat, visible_text(page), flags=re.I)]
    # background tasks stop: the page says the automatic checks are on hold instead of promising a time
    store.set_control("worker_heartbeat", {"at": datetime.now(UTC) - timedelta(minutes=10)})
    assert client.get("/jobs/refresh").json()["auto"] == "Automatic checks are on hold because background tasks are not running."
    js = Path("src/amorehiremepls/web/static/jobs.js").read_text()
    assert 'byId("jr-auto-wrap")' in js and "state.auto" in js


# -- the NAS stack passes the setting through -----------------------------------------------------------------


def test_the_stack_passes_the_schedule_to_web_and_worker_and_leaves_it_empty_by_default():
    compose = Path("deploy/nas/compose.yaml").read_text()
    shared = compose[compose.index("x-app: &app") : compose.index("services:")]
    assert 'AMORE_DISCOVERY_SCHEDULE: "${AMORE_DISCOVERY_SCHEDULE:-}"' in shared  # web and worker both merge this block
    assert "#   AMORE_DISCOVERY_SCHEDULE" in compose
    assert re.search(r"^AMORE_DISCOVERY_SCHEDULE=$", Path(".env.example").read_text(), flags=re.M)
    assert "--set AMORE_DISCOVERY_SCHEDULE=" in Path("scripts/deploy_nas.sh").read_text() and "AMORE_DISCOVERY_SCHEDULE" in Path("docs/nas-deployment.md").read_text()
