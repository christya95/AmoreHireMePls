"""Cancel a live attempt from the dashboard. Fictional employers only. Nothing here submits.

The browser's progress hook is the check point: before each form step it reads the task's cancel flag. A cancel seen
before the final click closes the page with nothing sent; a cancel after the final step's note is left to the receipt wait.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from amorehiremepls.apply.applications import (
    CANCEL_LATE_NOTE,
    CANCEL_REASON,
    AttemptCancelled,
    CancelWatch,
    final_click_noted,
    request_cancel,
    settle_late_cancel,
)
from amorehiremepls.apply.browser import BrowserAdapter, BrowserConfig, Plan, Step, step_note
from amorehiremepls.core.config import Settings
from amorehiremepls.core.runtime import Runtime
from amorehiremepls.core.util import utcnow
from amorehiremepls.web.web import create_app
from tests.conftest import TEST_MONGO_URI
from tests.fixtures import greenhouse_site
from tests.test_greenhouse import _prepared, _runtime, board  # noqa: F401  (the board fixture is used by name)

NOW = datetime.now(UTC)


class _Store:
    """Only what CancelWatch reads and writes on a run."""

    def __init__(self, requested: str | None = None) -> None:
        self.requested = requested
        self.events: list[str] = []

    def run_requested_state(self, run_id):
        return self.requested

    def update_run(self, run_id, **fields):
        if "requested" in fields:
            self.requested = fields["requested"]

    def run_event(self, run_id, message):
        self.events.append(message)


class _Page:
    url = "https://boards.example/apply"


class _Scope:
    page = _Page()


def _adapter(store: _Store, plan: Plan, on_step=None) -> tuple[BrowserAdapter, list[Step], list[str]]:
    """A browser whose steps are recorded instead of run. The real `_say` and the real step loop stay."""
    adapter = BrowserAdapter(BrowserConfig(allowed_hosts=set()))
    notes: list[str] = []
    done: list[Step] = []

    def do_step(scope, step, plan_, evidence=None, shots=None):
        done.append(step)
        if on_step:
            on_step(step)
        return scope

    adapter._do_step = do_step  # type: ignore[method-assign]
    adapter._preflight = lambda *a, **k: None  # type: ignore[method-assign]
    adapter._snapshot = lambda *a, **k: None  # type: ignore[method-assign]
    adapter._detect_login = lambda *a, **k: None  # type: ignore[method-assign]
    adapter._pass_wall = lambda *a, **k: None  # type: ignore[method-assign]
    adapter._detect_validation_errors = lambda *a, **k: None  # type: ignore[method-assign]
    adapter._await_receipt = lambda *a, **k: adapter._say("Waiting for the confirmation")  # type: ignore[method-assign]
    adapter._progress = CancelWatch(store, "run_x", plan, notes.append)
    return adapter, done, notes


def _plan() -> Plan:
    return Plan(
        start_url="https://boards.example/apply",
        steps=[Step("fill", "#first", "Ada", label="First name"), Step("click", "button.send", final=True)],
        receipt_text="thank you",
    )


def test_a_cancel_before_a_step_closes_the_form_with_nothing_sent():
    store = _Store(requested="cancel")
    plan = _plan()
    adapter, done, notes = _adapter(store, plan)
    evidence = {"steps_done": 0, "final_clicked": False}
    with pytest.raises(AttemptCancelled):
        adapter._run_steps(_Scope(), plan, evidence, adapter._shot_target(), None, None)
    assert done == []  # the first step was never run, so the final click never happened
    assert evidence["final_clicked"] is False
    assert store.requested is None and store.events == [CANCEL_REASON]
    assert notes == ["Filling First name"]  # the step note is still saved before the stop


def test_a_cancel_seen_at_the_final_step_note_still_stops_before_the_click():
    """The final step's note is written before its click, so a cancel read there is honoured."""
    plan = _plan()
    store = _Store()
    adapter, done, _notes = _adapter(store, plan)
    original = adapter._progress.note

    def flip_then_note(message):
        original(message)
        if message == step_note(plan.steps[-1]):
            store.requested = "cancel"  # the person pressed Cancel just as the app reached the last step

    adapter._progress.note = flip_then_note
    with pytest.raises(AttemptCancelled):
        adapter._run_steps(_Scope(), plan, {"steps_done": 0, "final_clicked": False}, adapter._shot_target(), None, None)
    assert [s.type for s in done] == ["fill"] and store.events == [CANCEL_REASON]


def test_a_cancel_after_the_final_click_is_ignored_and_settled_later():
    plan = _plan()
    store = _Store()

    def on_step(step):
        if step.final:
            store.requested = "cancel"  # arrives while the click is happening

    adapter, done, notes = _adapter(store, plan, on_step)
    evidence = {"steps_done": 0, "final_clicked": False}
    adapter._run_steps(_Scope(), plan, evidence, adapter._shot_target(), None, None)  # no exception: the receipt wait decides
    assert [s.type for s in done] == ["fill", "click"] and evidence["final_clicked"] is True
    assert notes[-1] == "Waiting for the confirmation" and store.events == []
    assert store.requested == "cancel"
    settle_late_cancel(store, "run_x")
    assert store.requested is None and store.events == [CANCEL_LATE_NOTE]
    settle_late_cancel(store, "run_x")  # nothing left to settle
    assert store.events == [CANCEL_LATE_NOTE]


def test_final_click_noted_reads_the_saved_step_line():
    assert final_click_noted(None) is False
    assert final_click_noted({"events": [{"message": "Filling First name"}]}) is False
    assert final_click_noted({"events": [{"message": "Filling First name"}, {"message": "Sending the application"}]}) is True


@pytest.fixture
def client(store, tmp_path):
    settings = Settings(
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
    return TestClient(create_app(Runtime(settings, store=store)), follow_redirects=False)


def _live(store, job_id="job_live", app_id="app_live", run_status="running"):
    store.c("jobs").insert_one(
        {
            "_id": job_id,
            "employer": "Northwind",
            "employer_norm": "northwind",
            "title": "Account Executive",
            "status": "executing",
            "posting_url": "https://boards.example/jobs/1",
            "apply_url": "https://boards.example/jobs/1",
            "sources": ["manual"],
            "created_at": NOW,
            "updated_at": NOW,
        }
    )
    store.c("applications").insert_one(
        {
            "_id": app_id,
            "job_id": job_id,
            "status": "executing",
            "provenance": {"origin": "local"},
            "material_hash": "m-live",
            "review_package": {"apply_url": "https://boards.example/jobs/1", "answers": [], "gaps": []},
            "created_at": NOW,
            "updated_at": NOW,
        }
    )
    run = store.create_run("application", {"job_id": job_id})
    store.update_run(run["_id"], status=run_status, started_at=utcnow())
    store.run_event(run["_id"], "Filling First name")
    return run["_id"]


@pytest.mark.mongo
def test_the_page_offers_cancel_only_while_the_form_is_being_filled(client, store):
    run_id = _live(store)
    assert client.post("/login", data={"password": "correct-horse-battery"}).status_code == 303
    page = client.get("/applications/app_live")
    assert page.status_code == 200 and 'id="cancel-attempt"' in page.text and "Cancel this attempt" in page.text
    assert "stop this attempt. Nothing is sent, and you can try again later." in page.text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    assert client.post("/applications/app_live/cancel", data={}).status_code == 422
    assert store.run_requested_state(run_id) is None
    sent = client.post("/applications/app_live/cancel", data={"csrf": csrf})
    assert sent.status_code == 303 and sent.headers["location"] == "/applications/app_live?ok=cancel_requested"
    run = store.c("runs").find_one({"_id": run_id})
    assert run["requested"] == "cancel" and any(e["message"] == "Cancel requested from the dashboard" for e in run["events"])
    assert store.c("audit").find_one({"event": "application_cancel_requested", "subject_id": "app_live"})["data"]["runs"] == [run_id]
    assert store.c("applications").find_one({"_id": "app_live"})["status"] == "executing"  # the task records the stop, not the route
    again = client.post("/applications/app_live/cancel", data={"csrf": csrf})
    assert again.status_code == 303 and "ok=cancel_requested" in again.headers["location"]
    assert sum(1 for e in store.c("runs").find_one({"_id": run_id})["events"] if e["message"] == "Cancel requested from the dashboard") == 1
    # once the final step's note is saved the click may have happened: the control goes and the route refuses
    store.update_run(run_id, requested=None)
    store.run_event(run_id, "Sending the application")
    late_page = client.get("/applications/app_live").text
    assert 'id="cancel-attempt"' not in late_page
    late = client.post("/applications/app_live/cancel", data={"csrf": csrf})
    assert late.status_code == 303 and late.headers["location"].endswith("err=cancel_too_late")
    assert store.run_requested_state(run_id) is None
    # no live task: nothing to stop
    store.update_run(run_id, status="completed", finished_at=utcnow())
    missing = client.post("/applications/app_live/cancel", data={"csrf": csrf})
    assert missing.status_code == 303 and missing.headers["location"].endswith("err=cancel_missing")
    assert 'id="cancel-attempt"' not in client.get("/applications/app_live").text
    assert client.post("/applications/app_none/cancel", data={"csrf": csrf}).status_code == 404


@pytest.mark.mongo
def test_request_cancel_needs_a_live_task_and_an_application_in_progress(store):
    run_id = _live(store)
    app = store.c("applications").find_one({"_id": "app_live"})
    assert request_cancel(store, {**app, "status": "failed"}) == "missing"
    assert request_cancel(store, app) == "ok"
    assert store.run_requested_state(run_id) == "cancel"


@pytest.mark.mongo
def test_a_cancelled_attempt_is_recorded_as_a_retryable_failure_with_nothing_sent(store, tmp_path, board):  # noqa: F811
    """The whole path from the dashboard flag to the application record, with the browser replaced at `run_plan`.

    The fake browser does what the real one does around a step: it calls the progress hook with the step's note. The
    cancel raised there passes through to `RealServices.execute`, which turns it into a retryable failure."""
    from amorehiremepls.core.runtime import RealServices

    svc, job, app = _prepared(store, tmp_path, board, variant="check")
    rt = _runtime(store, tmp_path, challenge_retry_minutes=0)
    run = store.create_run("application", {"job_id": job["_id"]})
    store.update_run(run["_id"], status="running", requested="cancel", started_at=utcnow())
    before = len(greenhouse_site.GH_SUBMISSIONS)
    seen: dict = {}

    def run_plan(plan, site_key="default", observe=True, captcha_solver=None, hold=None, progress=None):
        seen["steps"] = len(plan.steps)
        for step in plan.steps:  # the real loop notes each step first; the cancel is raised from that note
            progress(step_note(step))
            seen["ran"] = seen.get("ran", 0) + 1
        raise AssertionError("the final click must not be reached")

    rt.browser.run_plan = run_plan
    out = RealServices(rt).execute(app["_id"], "w1")
    assert out["status"] == "failed" and out["reason"] == CANCEL_REASON and out["adapter_ran"] is True
    assert seen.get("ran", 0) == 0 and seen["steps"] > 1
    assert len(greenhouse_site.GH_SUBMISSIONS) == before
    saved = store.c("applications").find_one({"_id": app["_id"]})
    assert saved["status"] == "failed" and saved["reason"] == CANCEL_REASON and saved["retryable"] is True
    assert "Nothing was sent" in saved["next_action"] and "Retry with the app is safe" in saved["next_action"]
    action = store.c("actions").find_one({"idempotency_key": f"submit:{job['_id']}"})
    assert action["status"] == "ready" and action["last_failure"] == CANCEL_REASON  # a later try may run the form again
    assert store.c("jobs").find_one({"_id": job["_id"]})["status"] == "qualified"
    run = store.c("runs").find_one({"_id": run["_id"]})
    assert run["requested"] is None and [e["message"] for e in run["events"]][-1] == CANCEL_REASON
