"""The application Log is a closed list of saved steps, and a failure note names the field rather than a typed value."""

import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from amorehiremepls.apply.browser import BrowserAdapter, BrowserConfig, Plan, Step, timeout_failure_note
from amorehiremepls.core.actions import REPLAY_LABEL
from amorehiremepls.core.config import Settings
from amorehiremepls.core.orchestration import Runner, build_application_graph, evidence_failure_line, make_checkpointer
from amorehiremepls.core.runtime import Runtime
from amorehiremepls.web.chrome import event_log
from amorehiremepls.web.web import create_app
from tests.conftest import TEST_MONGO_URI

SECRET = "super-secret-value"
FAILURE = "the form did not respond in time at step 14 (Location); nothing was sent"


def test_a_step_timeout_names_the_field_and_not_the_typed_value():
    plan = Plan(
        start_url="https://boards.example/jobs/1",
        steps=[Step("autocomplete", "#loc", "Lakeside", label="Location"), Step("upload", "#file", label="Resume")],
        receipt_text="done",
    )
    note = timeout_failure_note(plan, {"steps_done": 0, "final_clicked": False})
    assert note == "the form did not respond in time at step 1 (Location); nothing was sent"
    assert "Lakeside" not in note


def test_a_saved_step_keeps_the_failure_and_drops_a_secret():
    adapter = BrowserAdapter(BrowserConfig(allowed_hosts=set()))
    seen: list[str] = []
    adapter._progress = seen.append
    adapter._say(FAILURE + '\nCall log:\n  - waiting for locator("#loc")')
    adapter._say(f"password={SECRET}")
    assert seen[0] == FAILURE
    assert SECRET not in seen[1] and seen[1] == "password=***"


def test_a_replay_label_keeps_the_saved_stop_line_and_not_the_typed_value():
    evidence = {"error": "timeout waiting for Location\nCall log:\n  - waiting", "value": "secret-city", "steps_done": 13, "final_clicked": False}
    line = evidence_failure_line(REPLAY_LABEL, evidence)
    assert "Location" in line and "secret-city" not in line and "Call log" not in line
    assert evidence_failure_line(REPLAY_LABEL, {"value": "secret-city", "steps_done": 13}) == ""
    assert evidence_failure_line(FAILURE, evidence) == ""


def test_the_log_is_newest_first_capped_and_redacted():
    base = datetime(2026, 11, 6, 15, tzinfo=UTC)
    events = [{"at": base, "message": "oldest step kept out"}]
    events += [{"at": base + timedelta(seconds=i + 1), "message": f"filler {i}"} for i in range(39)]
    events.append({"at": base + timedelta(seconds=50), "message": f"password={SECRET}"})
    events.append({"at": base + timedelta(seconds=51), "message": FAILURE})
    events.append({"at": base + timedelta(seconds=52), "message": REPLAY_LABEL})
    rows = event_log([{"events": events}], saved=FAILURE, saved_at=base + timedelta(seconds=53))
    assert len(rows) == 40
    assert rows[0]["text"] == FAILURE
    assert rows[1]["text"] == "password=***"
    assert SECRET not in " ".join(row["text"] for row in rows)
    assert "oldest step kept out" not in " ".join(row["text"] for row in rows)
    assert REPLAY_LABEL not in " ".join(row["text"] for row in rows)
    assert sum(1 for row in rows if row["text"] == FAILURE) == 1


class _Replay:
    def __init__(self) -> None:
        self.apps: dict = {}

    def qualify(self, job_id):
        return {"_id": job_id, "status": "qualified"}

    def scan_fields(self, job):
        return []

    def prepare(self, job_id, fields):
        app = {"_id": f"app_{job_id}", "status": "ready_for_review", "material_hash": "m", "review_package": {"outstanding": [], "answers": []}}
        self.apps[app["_id"]] = app
        return app

    def get_application(self, app_id):
        return self.apps[app_id]

    def supply_inputs(self, app_id, values):
        return self.apps[app_id]

    def has_authorization(self, app):
        return True

    def execute(self, app_id, worker):
        return {
            "status": "failed",
            "reason": REPLAY_LABEL,
            "evidence": {"error": FAILURE, "value": "secret-city", "steps_done": 13, "final_clicked": False},
            "action_id": None,
        }

    def record_manual(self, app_id, manual):
        return self.apps[app_id]

    def after_submission(self, app_id):
        return {}


@pytest.mark.mongo
def test_a_replay_records_the_saved_stop_line(store):
    runner = Runner(store, build_application_graph(_Replay(), store, make_checkpointer(store)))
    run = runner.start_application_run("job-log")
    messages = [event["message"] for event in run["events"]]
    assert any(FAILURE in message for message in messages)
    assert all("secret-city" not in message for message in messages)
    assert REPLAY_LABEL not in messages


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


@pytest.mark.mongo
def test_the_application_page_log_is_closed_and_hides_a_secret(client):
    rt = client.app.state.rt
    assert client.post("/login", data={"password": "correct-horse-battery"}).status_code == 303
    now = datetime(2026, 11, 6, 15, tzinfo=UTC)
    rt.store.c("jobs").insert_one(
        {
            "_id": "job_log",
            "employer": "Acme",
            "employer_norm": "acme",
            "title": "Backend Developer",
            "status": "qualified",
            "posting_url": "https://boards.example/jobs/1",
            "apply_url": "https://boards.example/jobs/1",
            "sources": ["manual"],
            "created_at": now,
            "updated_at": now,
        }
    )
    rt.store.c("applications").insert_one(
        {
            "_id": "app_log",
            "job_id": "job_log",
            "status": "failed",
            "reason": REPLAY_LABEL,
            "provenance": {"origin": "local"},
            "material_hash": "m-log",
            "review_package": {"apply_url": "https://boards.example/jobs/1", "posting_url": "https://boards.example/jobs/1", "answers": [], "gaps": [], "outstanding": []},
            "history": [{"at": now, "status": "failed", "reason": REPLAY_LABEL}],
            "created_at": now,
            "updated_at": now,
        }
    )
    rt.store.c("actions").insert_one(
        {
            "_id": "act_log",
            "idempotency_key": "submit:job_log",
            "status": "failed",
            "outcome_reason": FAILURE,
            "finished_at": now + timedelta(minutes=5),
            "evidence": {"steps_done": 13},
        }
    )
    events = [{"at": now + timedelta(seconds=i), "message": f"filler {i}"} for i in range(39)]
    events.insert(0, {"at": now - timedelta(minutes=5), "message": "oldest step kept out"})
    events.append({"at": now + timedelta(minutes=1), "message": f"password={SECRET}"})
    events.append({"at": now + timedelta(minutes=2), "message": FAILURE})
    run = rt.store.create_run("application", {"job_id": "job_log"})
    rt.store.c("runs").update_one({"_id": run["_id"]}, {"$set": {"events": events}})
    page = client.get("/applications/app_log").text
    assert "Network Tab" not in page and 'id="network-tab"' not in page and ">Clear</button>" not in page
    assert "<summary>Log</summary>" not in page
    assert SECRET not in page
    overview = client.get("/").text
    assert "<summary>Log</summary>" in overview and SECRET not in overview
    assert re.search(r'<details class="disclosure(?: mt)?">\s*<summary>Log</summary>', overview)
    assert 'disclosure mt" open' not in overview and 'disclosure" open' not in overview
