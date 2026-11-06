"""The page says what was typed last time when that differs from the answer on the page."""

import re
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from amorehiremepls.core.authorization import AuthorizationStore, ScopeBinding
from amorehiremepls.core.config import Settings
from amorehiremepls.core.runtime import Runtime
from amorehiremepls.web.chrome import FILLING_LINE, IDLE_LINE, answer_gap_line, application_timeline, form_filling_now
from amorehiremepls.web.web import create_app
from tests.conftest import TEST_MONGO_URI

GAP_A = "Last time, Location was filled in as Lakeside. This page now says Harbor. That new answer has not been typed yet."
GAP_B = "Last time, Location was filled in as Westport. This page now says Capital. That new answer has not been typed yet."


def test_the_sentence_names_the_typed_value_and_the_answer_on_the_page():
    app = {
        "material_hash": "m-now",
        "review_package": {"answers": [{"field_id": "location", "label": "Location", "value": "Harbor"}]},
    }
    action = {"status": "failed", "material_hash": "m-then", "evidence": {"value": "Lakeside"}}
    assert answer_gap_line(app, action) == GAP_A
    assert answer_gap_line(app, {"status": "failed", "evidence": {"location_typed": "Harbor"}}) == ""
    same = {"status": "authorized", "material_hash": "m-now"}
    running = [{"status": "running", "state": {"outcome": {"adapter_ran": False}}}]
    assert form_filling_now(same, running, {"status": "failed", "material_hash": "m-now"}) is False
    when = datetime(2026, 11, 6, tzinfo=UTC)
    rows = application_timeline(
        {
            **same,
            "history": [
                {"at": when, "status": "failed", "reason": "no suggestions appeared for Lakeside"},
                {"at": when, "status": "failed", "reason": "recorded outcome (replay)"},
            ],
        },
        [
            {
                "status": "completed",
                "events": [
                    {"at": when, "message": "completed"},
                    {"at": when, "message": "running"},
                    {"at": when, "message": "Picked up"},
                    {"at": when, "message": "hidden from the task list"},
                    {"at": when, "message": "Applying on the job site"},
                ],
                "state": {"outcome": {"adapter_ran": False}},
            }
        ],
    )
    texts = [row["text"] for row in rows]
    assert "The form was not opened. Showing the result already saved." in texts
    assert "Applying failed. no suggestions appeared for Lakeside" in texts
    assert not any(piece in " ".join(texts) for piece in ("recorded outcome", "Task started", "Task finished", "Picked up", "hidden from the task list"))


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


def _job(store, job_id, title):
    now = datetime(2026, 11, 6, 15, tzinfo=UTC)
    store.c("jobs").insert_one(
        {
            "_id": job_id,
            "employer": "Acme",
            "employer_norm": "acme",
            "title": title,
            "status": "qualified",
            "qualification": {"decision": "fit"},
            "posting_url": "https://boards.example/jobs/1",
            "apply_url": "https://boards.example/jobs/1",
            "sources": ["manual"],
            "created_at": now,
            "updated_at": now,
        }
    )


def _app(store, app_id, job_id, status, material, place, typed):
    now = datetime(2026, 11, 6, 15, tzinfo=UTC)
    store.c("applications").insert_one(
        {
            "_id": app_id,
            "job_id": job_id,
            "status": status,
            "reason": "no suggestions appeared",
            "provenance": {"origin": "local"},
            "material_hash": material,
            "review_package": {
                "apply_url": "https://boards.example/jobs/1",
                "posting_url": "https://boards.example/jobs/1",
                "answers": [{"field_id": "location", "label": "Location", "value": place, "fact_ids": ["user:input"], "reason": "edited by you"}],
                "gaps": ["Knows the first tool", "Knows the second tool"] if job_id == "job_a" else [],
                "outstanding": [
                    "answer needed: Resume/CV",
                    "approved base resume not selected",
                    "base_resume",
                    "email",
                    "legal_first_name",
                    "mystery_field",
                ]
                if job_id == "job_a"
                else [],
                "unsupported_requirements_note": "The prepared file has no line for the first tool. A second sentence stays out of the short note.",
            },
            "history": [
                {"at": now, "status": "failed", "reason": "no suggestions appeared"},
                {"at": now, "status": "failed", "reason": "recorded outcome (replay)"},
            ],
            "created_at": now,
            "updated_at": now,
        }
    )
    store.c("actions").insert_one(
        {
            "_id": f"act_{app_id}",
            "kind": "application_submit",
            "target_id": job_id,
            "idempotency_key": f"submit:{job_id}",
            "status": "failed",
            "material_hash": "m-old",
            "outcome_reason": "no suggestions appeared",
            "evidence": {"location_typed": typed, "value": typed, "final_clicked": False},
            "finished_at": now,
        }
    )


@pytest.mark.mongo
def test_two_jobs_show_their_own_typed_value_and_an_idle_form_does_not_look_busy(client):
    rt = client.app.state.rt
    assert client.post("/login", data={"password": "correct-horse-battery"}).status_code == 303
    _job(rt.store, "job_a", "First role")
    _job(rt.store, "job_b", "Second role")
    _job(rt.store, "job_run", "Third role")
    _app(rt.store, "app_a", "job_a", "failed", "m-a", "Harbor", "Lakeside")
    _app(rt.store, "app_b", "job_b", "authorized", "m-b", "Capital", "Westport")
    AuthorizationStore(rt.store).grant("application_submit", ScopeBinding(target_id="job_b", material_hash="m-b"))
    now = datetime(2026, 11, 6, 16, tzinfo=UTC)
    rt.store.c("applications").insert_one(
        {
            "_id": "app_run",
            "job_id": "job_run",
            "status": "executing",
            "provenance": {"origin": "local"},
            "material_hash": "m-run",
            "review_package": {"apply_url": "https://boards.example/jobs/1", "posting_url": "https://boards.example/jobs/1", "answers": [], "gaps": [], "outstanding": []},
            "history": [],
            "created_at": now,
            "updated_at": now,
        }
    )
    rt.store.c("actions").insert_one(
        {"_id": "act_run", "kind": "application_submit", "target_id": "job_run", "idempotency_key": "submit:job_run", "status": "executing", "material_hash": "m-run"}
    )
    run = rt.store.create_run("application", {"job_id": "job_run"})
    rt.store.c("runs").update_one({"_id": run["_id"]}, {"$set": {"status": "running"}})
    replay = rt.store.create_run("application", {"job_id": "job_a"})
    rt.store.c("runs").update_one(
        {"_id": replay["_id"]},
        {
            "$set": {
                "status": "running",
                "state": {"outcome": {"adapter_ran": False}},
                "events": [
                    {"at": now, "message": "completed"},
                    {"at": now, "message": "running"},
                    {"at": now, "message": "Picked up"},
                    {"at": now, "message": "hidden from the task list"},
                    {"at": now, "message": "Applying on the job site"},
                ],
            }
        },
    )

    page_a = client.get("/applications/app_a").text
    page_b = client.get("/applications/app_b").text
    page_run = client.get("/applications/app_run").text
    assert GAP_A in page_a and GAP_A in page_a.split("What happened")[-1]
    assert "What would be sent" in page_a and "<summary>Network Tab</summary>" in page_a
    assert page_a.count('class="gap-item') == 2
    assert "Knows the first tool" in page_a and "Knows the second tool" in page_a
    assert "second sentence stays out" not in page_a
    gaps_html = page_a.split('id="gaps"', 1)[1].split('<div class="stack">', 1)[0]
    assert "base_resume" not in gaps_html and "legal_first_name" not in gaps_html and "work_authorization" not in gaps_html
    assert "A resume still needs to be selected." in page_a and "Email is still needed." in page_a
    assert "A legal first name is still needed." in page_a and "Resume/CV is still needed." in page_a
    assert "Something else is still needed." in page_a
    assert "answer needed:" not in page_a and "mystery_field" not in page_a and "legal_first_name" not in page_a and "base_resume" not in page_a
    assert "Everything that happened to this application" not in page_a
    assert "Nothing was recorded for the last time." in page_a
    assert "The form was not opened. Showing the result already saved." in page_a
    assert "recorded outcome" not in page_a and "Task started" not in page_a and "Picked up" not in page_a and "hidden from the task list" not in page_a
    assert 'id="field-answer"' not in page_a and 'id="network-tab" open' not in page_a
    assert IDLE_LINE in page_a and FILLING_LINE not in page_a
    assert "Task finished" not in page_a
    assert "Westport" not in page_a and "Capital" not in page_a
    assert "Next visit" not in page_a and "Chromium" not in page_a
    assert GAP_B in page_b and "Lakeside" not in page_b and "Harbor" not in page_b
    assert "Approved for this job" in page_b and "scope_" not in page_b and "used 0 of 1" in page_b
    assert IDLE_LINE in page_b and ">Fill in the form</button>" in page_b
    assert FILLING_LINE in page_run and IDLE_LINE not in page_run and "Lakeside" not in page_run and "Westport" not in page_run
    assert 'id="network-tab" open' in page_run and 'id="network-tab" open' not in page_a

    job_a = client.get("/jobs/job_a").text
    job_run = client.get("/jobs/job_run").text
    assert IDLE_LINE in job_a and GAP_A in job_a and FILLING_LINE not in job_a and "applying now" not in job_a.lower()
    assert FILLING_LINE in job_run and "Westport" not in job_run and "Lakeside" not in job_run

    csrf = re.search(r'name="csrf" value="([^"]+)"', page_b).group(1)
    saved = client.post("/applications/app_b/fill", data={"csrf": csrf})
    assert saved.status_code == 303
    queued = rt.store.c("runs").find_one({"params.application_id": "app_b", "params.phase": "execute", "status": "queued"})
    assert queued is not None and queued["params"]["material_hash"] == "m-b"
    assert 'id="network-tab" open' in client.get("/applications/app_b").text
    assert client.post("/applications/app_b/fill", data={"csrf": csrf}).status_code == 303
    assert rt.store.c("runs").count_documents({"params.application_id": "app_b", "params.phase": "execute"}) == 1
