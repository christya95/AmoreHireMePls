from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from amorehiremepls.core.config import Settings
from amorehiremepls.core.runtime import Runtime
from amorehiremepls.web.web import create_app
from tests.conftest import TEST_MONGO_URI

pytestmark = pytest.mark.mongo


@pytest.fixture
def client(store, tmp_path):
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
    rt = Runtime(s, store=store)
    return TestClient(create_app(rt), follow_redirects=False)


def _login(client):
    r = client.post("/login", data={"password": "correct-horse-battery"})
    assert r.status_code == 303
    page = client.get("/")
    assert page.status_code == 200
    import re

    return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)


def test_health_is_public_and_pages_require_login(client):
    assert client.get("/health").status_code == 200
    assert client.get("/").status_code == 303 and client.get("/").headers["location"] == "/login"
    assert client.post("/login", data={"password": "wrong"}).status_code == 401


def test_csrf_required_on_state_changes_and_no_browser_control_routes(client):
    csrf = _login(client)
    assert client.post("/controls/emergency-stop", data={"active": "on"}).status_code == 422  # missing token
    assert client.post("/controls/emergency-stop", data={"active": "on", "csrf": "bogus"}).status_code == 403
    assert client.post("/controls/emergency-stop", data={"active": "on", "csrf": csrf}).status_code == 303
    assert "Emergency stop active" in client.get("/").text
    assert client.post("/controls/emergency-stop", data={"active": "off", "csrf": csrf}).status_code == 303
    paths = {r.path for r in client.app.routes}
    assert not any("browser" in p or "cdp" in p or "screenshot" in p for p in paths)


def test_import_job_authorize_flow_and_pages_render(client):
    csrf = _login(client)
    r = client.post(
        "/jobs/import",
        data={
            "csrf": csrf,
            "url": "https://careers.acme.example/jobs/R77",
            "employer": "Acme",
            "title": "Backend Developer",
            "requisition_id": "R77",
            "location": "Toronto, Ontario",
            "posting_text": "Permanent backend developer, Java, Toronto. CAD 110,000 to 130,000.",
        },
    )
    assert r.status_code == 303
    job_url = r.headers["location"]
    assert client.get(job_url).status_code == 200
    r = client.post(f"{job_url}/run", data={"csrf": csrf})
    assert r.status_code == 303 and r.headers["location"].startswith("/runs/")
    assert client.post(f"{job_url}/run", data={"csrf": csrf}).status_code == 409  # no duplicate active run
    for path in ("/", "/profile", "/history", "/jobs", "/runs", "/authorizations", "/outreach", "/sync", "/usage", r.headers["location"]):
        assert client.get(path).status_code == 200, path
    r = client.post("/authorizations/batch", data={"csrf": csrf, "kind": "application_submit", "employers": "Acme", "max_fit_rank": 2, "max_count": 2, "hours": 2})
    assert r.status_code == 303 and "Several jobs" in client.get("/authorizations").text
    r = client.post("/outreach/recipients", data={"csrf": csrf, "name": "Sam Recruiter", "employer": "Acme", "profile_url": "https://linkedin.com/in/sam-r", "remit": "TA"})
    assert r.status_code == 303 and "Sam Recruiter" in client.get("/outreach").text
    # drafting with an unconfirmed applied role is refused
    rid = client.app.state.rt.store.c("recipients").find_one({})["_id"]
    assert client.post(f"/outreach/recipients/{rid}/draft", data={"csrf": csrf, "kind": "connection_request", "applied_role": "Backend Developer"}).status_code == 400
    assert client.post(f"/outreach/recipients/{rid}/draft", data={"csrf": csrf, "kind": "connection_request", "applied_role": ""}).status_code == 303
    assert client.app.state.rt.store.c("outreach").find_one({})["status"] == "draft"


def test_review_page_and_single_scope_authorization(client, tmp_path):
    from amorehiremepls.core.worker import Worker
    from tests.fixtures.make_resume import make_base_resume
    from tests.fixtures.profile_fixture import build_test_profile
    from tests.test_profile import FAKE_APPLY, FAKE_FACTS, FAKE_SALARY

    csrf = _login(client)
    rt = client.app.state.rt
    rt.profiles.save(build_test_profile(FAKE_FACTS, FAKE_SALARY, apply_text=FAKE_APPLY), actor="test")
    base = make_base_resume(tmp_path / "base.docx")
    rt.profiles.select_base_resume(base, "2026-10-01", "test", rt.settings.private_dir)
    rt.reload_profile()
    r = client.post(
        "/jobs/import",
        data={
            "csrf": csrf,
            "url": "https://careers.acme.example/jobs/R78",
            "employer": "Acme",
            "title": "Backend Developer",
            "requisition_id": "R78",
            "location": "Toronto, Ontario",
            "posting_text": "Permanent backend developer, Java, Toronto. CAD 110,000 to 130,000.",
        },
    )
    job_id = r.headers["location"].rsplit("/", 1)[1]
    run_url = client.post(f"/jobs/{job_id}/run", data={"csrf": csrf}).headers["location"]
    Worker(rt).tick()
    run = rt.store.c("runs").find_one({"_id": run_url.rsplit("/", 1)[1]})
    assert run["status"] == "waiting" and run["interrupt"]["type"] == "awaiting_authorization", run
    app_id = run["interrupt"]["application_id"]
    page = client.get(f"/applications/{app_id}")
    assert page.status_code == 200 and "Approve this exact application" in page.text
    a = rt.store.c("applications").find_one({"_id": app_id})
    # stale material hash is refused; the real one grants a single scope and requests resume
    assert client.post(f"/applications/{app_id}/authorize", data={"csrf": csrf, "material_hash": "stale", "hours": 1}).status_code == 409
    assert client.post(f"/applications/{app_id}/authorize", data={"csrf": csrf, "material_hash": a["material_hash"], "hours": 1}).status_code == 303
    scopes = rt.auth.active("application_submit")
    assert len(scopes) == 1 and scopes[0].binding.target_id == job_id and scopes[0].binding.material_hash == a["material_hash"]
    assert rt.store.c("runs").find_one({"_id": run["_id"]})["requested"] == "resume"
    Worker(rt).tick()
    run = rt.store.c("runs").find_one({"_id": run["_id"]})
    # no adapter/form plan for this host -> resumable handoff, never a claimed submission
    assert run["status"] == "waiting" and run["interrupt"]["type"] == "handoff", run
    assert rt.store.c("applications").find_one({"_id": app_id})["status"] == "handoff"
    assert "I applied on the site" in client.get(f"/applications/{app_id}").text


def test_approving_after_the_task_finished_queues_a_send_and_the_page_shows_the_steps(client, tmp_path):
    from amorehiremepls.core.util import utcnow
    from amorehiremepls.core.worker import Worker
    from tests.fixtures.make_resume import make_base_resume
    from tests.fixtures.profile_fixture import build_test_profile
    from tests.test_profile import FAKE_APPLY, FAKE_FACTS, FAKE_SALARY

    csrf = _login(client)
    rt = client.app.state.rt
    rt.profiles.save(build_test_profile(FAKE_FACTS, FAKE_SALARY, apply_text=FAKE_APPLY), actor="test")
    base = make_base_resume(tmp_path / "base.docx")
    rt.profiles.select_base_resume(base, "2026-10-01", "test", rt.settings.private_dir)
    rt.reload_profile()
    r = client.post(
        "/jobs/import",
        data={
            "csrf": csrf,
            "url": "https://careers.acme.example/jobs/R80",
            "employer": "Acme",
            "title": "Backend Developer",
            "requisition_id": "R80",
            "location": "Toronto, Ontario",
            "posting_text": "Permanent backend developer, Java, Toronto. CAD 110,000 to 130,000.",
        },
    )
    job_id = r.headers["location"].rsplit("/", 1)[1]
    run_url = client.post(f"/jobs/{job_id}/run", data={"csrf": csrf}).headers["location"]
    Worker(rt).tick()
    run = rt.store.c("runs").find_one({"_id": run_url.rsplit("/", 1)[1]})
    app_id = run["interrupt"]["application_id"]
    rt.store.update_run(run["_id"], status="completed", interrupt=None, finished_at=utcnow())
    a = rt.store.c("applications").find_one({"_id": app_id})
    assert client.post(f"/applications/{app_id}/authorize", data={"csrf": csrf, "material_hash": a["material_hash"], "hours": 1}).status_code == 303
    queued = rt.store.c("runs").find_one({"params.application_id": app_id, "params.phase": "execute", "status": "queued"})
    assert queued is not None
    page = client.get(f"/applications/{app_id}")
    assert page.status_code == 200 and "What is happening" in page.text and "You approved this application" in page.text
    assert "The form opens within a minute." in page.text and "The form is being filled in now." not in page.text
    activity = client.get(f"/applications/{app_id}/activity")
    body = activity.json()
    assert activity.status_code == 200 and body["live"] is True and body["working"] is False
    assert body["state"] == "The form opens within a minute." and body["now"] == ""


def test_regenerate_documents_supersedes_artifacts_and_invalidates_approval(client, tmp_path):
    from amorehiremepls.core.worker import Worker
    from tests.fixtures.make_resume import make_base_resume
    from tests.fixtures.profile_fixture import build_test_profile
    from tests.test_profile import FAKE_APPLY, FAKE_FACTS, FAKE_SALARY

    csrf = _login(client)
    rt = client.app.state.rt
    r = client.post("/jobs/import", data={"csrf": csrf, "url": "https://careers.acme.example/jobs/R79", "employer": "Acme", "title": "Backend Developer", "requisition_id": "R79"})
    job_id = r.headers["location"].rsplit("/", 1)[1]
    # no approved base resume yet -> refused with a clear reason, nothing written
    r = client.post(f"/jobs/{job_id}/regenerate", data={"csrf": csrf})
    assert r.status_code == 303 and r.headers["location"].endswith("err=no_base")
    assert rt.store.c("artifacts").count_documents({"owner_id": job_id}) == 0
    assert client.post("/jobs/job_missing/regenerate", data={"csrf": csrf}).status_code == 404
    assert client.post(f"/jobs/{job_id}/regenerate", data={}).status_code == 422

    rt.profiles.save(build_test_profile(FAKE_FACTS, FAKE_SALARY, apply_text=FAKE_APPLY), actor="test")
    base = make_base_resume(tmp_path / "base.docx")
    rt.profiles.select_base_resume(base, "2026-10-01", "test", rt.settings.private_dir)
    rt.reload_profile()
    run_url = client.post(f"/jobs/{job_id}/run", data={"csrf": csrf}).headers["location"]
    # while the run is queued the documents are left alone
    r = client.post(f"/jobs/{job_id}/regenerate", data={"csrf": csrf})
    assert r.headers["location"].endswith("err=busy")
    Worker(rt).tick()
    run = rt.store.c("runs").find_one({"_id": run_url.rsplit("/", 1)[1]})
    assert run["interrupt"]["type"] == "awaiting_authorization"
    app_id = run["interrupt"]["application_id"]
    a0 = rt.store.c("applications").find_one({"_id": app_id})
    first = rt.store.c("artifacts").find_one({"owner_id": job_id, "kind": "resume_tailored"})
    assert client.post(f"/applications/{app_id}/authorize", data={"csrf": csrf, "material_hash": a0["material_hash"], "hours": 1}).status_code == 303
    assert len([s for s in rt.auth.active("application_submit") if s.binding.target_id == job_id]) == 1
    # the job page shows the stepper and the regenerate action with its warning
    page = client.get(f"/jobs/{job_id}").text
    assert "Rebuild the resume" in page and "The old approval no longer applies" in page and "Approve" in page and "Contact recruiters" in page

    r = client.post(f"/jobs/{job_id}/regenerate", data={"csrf": csrf})
    assert r.status_code == 303 and r.headers["location"].endswith("ok=regenerated"), r.headers["location"]
    arts = list(rt.store.c("artifacts").find({"owner_id": job_id, "kind": "resume_tailored"}).sort("created_at", 1))
    assert len(arts) == 2 and arts[0]["_id"] == first["_id"] and arts[0].get("superseded") is True and not arts[1].get("superseded")
    assert Path(arts[1]["path"]).is_file()
    a1 = rt.store.c("applications").find_one({"_id": app_id})
    assert a1["status"] in ("ready_for_review", "needs_input") and a1["artifact_id"] == arts[1]["_id"] and a1["material_hash"] != a0["material_hash"]
    # the old single approval no longer covers the new files
    assert not [s for s in rt.auth.active("application_submit") if s.binding.target_id == job_id]
    assert rt.store.c("audit").find_one({"event": "documents_regenerated", "subject_id": job_id})
    page = client.get(f"/jobs/{job_id}").text
    assert "replaced by a newer version" in page
    # a submitted application is never re-tailored from here
    rt.store.c("applications").update_one({"_id": app_id}, {"$set": {"status": "submitted"}})
    r = client.post(f"/jobs/{job_id}/regenerate", data={"csrf": csrf})
    assert r.headers["location"].endswith("err=already_submitted")
    assert rt.store.c("artifacts").count_documents({"owner_id": job_id, "kind": "resume_tailored"}) == 2


def test_application_page_copy_buttons_are_wired_to_each_details_block(client):
    import html
    import json
    import re
    from datetime import UTC, datetime

    _login(client)
    rt = client.app.state.rt
    now = datetime.now(UTC)
    technical = {"verified": True, "seen": "the page after submit"}
    application = {"kept": "local note"}
    history = [{"status": "submitted", "text": "saved here"}]
    rt.store.c("jobs").insert_one(
        {
            "_id": "job_copy",
            "employer": "Acme",
            "employer_norm": "acme",
            "title": "Backend Developer",
            "status": "applied",
            "posting_url": "https://careers.example/jobs/1",
            "apply_url": "https://careers.example/jobs/1",
            "sources": ["manual"],
            "created_at": now,
            "updated_at": now,
        }
    )
    rt.store.c("applications").insert_one(
        {
            "_id": "app_copy",
            "job_id": "job_copy",
            "status": "needs_verification",
            "provenance": {"origin": "local"},
            "material_hash": "m-copy",
            "review_package": {"apply_url": "https://careers.example/jobs/1", "posting_url": "https://careers.example/jobs/1", "answers": [], "gaps": []},
            "evidence": application,
            "history": history,
            "created_at": now,
            "updated_at": now,
        }
    )
    rt.store.c("actions").insert_one(
        {
            "_id": "act_copy",
            "kind": "application_submit",
            "target_id": "job_copy",
            "idempotency_key": "submit:job_copy",
            "status": "needs_verification",
            "material_hash": "m-copy",
            "evidence": technical,
            "created_at": now,
            "updated_at": now,
        }
    )
    page = client.get("/applications/app_copy")
    assert page.status_code == 200
    expected = {
        "technical-details": json.dumps(technical, indent=1),
        "application-details": json.dumps(application, indent=1),
    }
    assert "application-history" not in page.text and "Everything that happened to this application" not in page.text
    for pre_id, body in expected.items():
        match = re.search(
            rf'<button type="button" class="secondary sm" data-copy="{pre_id}">Copy</button>\s*<pre id="{pre_id}">(.*?)</pre>',
            page.text,
            flags=re.S,
        )
        assert match, pre_id
        assert html.unescape(match.group(1)) == body
    script = client.get("/static/app.js").text
    assert "isSecureContext" in script and "writeText" in script and 'execCommand("copy")' in script
    assert "Copied" in script and "Copy failed" in script


def test_stale_authorization_asks_to_approve_again(client):
    """An approval for an older package asks for this version again. A later exact approval does not."""
    from datetime import UTC, datetime

    from amorehiremepls.core.authorization import MATERIAL_CHANGED, AuthorizationStore, ScopeBinding
    from amorehiremepls.web.chrome import NOTHING_NEEDED, STALE_APPROVAL_LINE

    csrf = _login(client)
    rt = client.app.state.rt
    now = datetime.now(UTC)

    def seed(app_id: str, job_id: str, reason: str = ""):
        rt.store.c("jobs").insert_one(
            {
                "_id": job_id,
                "employer": "Northwind",
                "employer_norm": "northwind",
                "title": "Account Executive",
                "status": "qualified",
                "posting_url": "https://jobs.example/northwind",
                "apply_url": "https://jobs.example/northwind",
                "sources": ["manual"],
                "created_at": now,
                "updated_at": now,
            }
        )
        rt.store.c("applications").insert_one(
            {
                "_id": app_id,
                "job_id": job_id,
                "status": "authorized",
                "reason": reason,
                "provenance": {"origin": "local"},
                "material_hash": "pkg-now",
                "review_package": {
                    "apply_url": "https://jobs.example/northwind",
                    "posting_url": "https://jobs.example/northwind",
                    "answers": [],
                    "outstanding": [],
                    "gaps": [],
                },
                "history": [{"at": now, "status": "authorized"}],
                "created_at": now,
                "updated_at": now,
            }
        )

    seed("app_stale", "job_stale", MATERIAL_CHANGED)
    AuthorizationStore(rt.store).grant("application_submit", ScopeBinding(target_id="job_stale", material_hash="pkg-old"))
    page = client.get("/applications/app_stale").text
    head = page.split("What is happening", 1)[0]
    assert STALE_APPROVAL_LINE in head
    assert NOTHING_NEEDED not in head
    assert "Apply with the app" not in head
    assert "Approve this exact application" in head
    assert 'action="/applications/app_stale/authorize"' in head
    assert 'name="csrf"' in head and 'name="material_hash" value="pkg-now"' in head
    assert f'<code class="raw">{MATERIAL_CHANGED}</code>' in head
    assert client.post("/applications/app_stale/authorize", data={"csrf": csrf, "material_hash": "pkg-other", "hours": 1}).status_code == 409
    assert client.post("/applications/app_stale/authorize", data={"csrf": csrf, "material_hash": "pkg-now", "hours": 1}).status_code == 303
    renewed = client.get("/applications/app_stale").text.split("What is happening", 1)[0]
    assert STALE_APPROVAL_LINE not in renewed and NOTHING_NEEDED in renewed

    seed("app_covered", "job_covered", MATERIAL_CHANGED)
    AuthorizationStore(rt.store).grant("application_submit", ScopeBinding(target_id="job_covered", material_hash="pkg-old"))
    AuthorizationStore(rt.store).grant("application_submit", ScopeBinding(target_id="job_covered", material_hash="pkg-now"))
    covered = client.get("/applications/app_covered").text.split("What is happening", 1)[0]
    assert STALE_APPROVAL_LINE not in covered
    assert MATERIAL_CHANGED not in covered
    assert "Approve this exact application" not in covered
    assert "Apply with the app" in covered
    assert client.post("/applications/app_covered/authorize", data={"csrf": csrf, "material_hash": "pkg-now", "hours": 1}).status_code == 409


def test_an_approved_idle_package_can_be_applied_without_a_new_approval(client):
    """An approved package with nothing in progress offers one control. It queues the existing action."""
    from datetime import UTC, datetime, timedelta

    from amorehiremepls.core.authorization import AuthorizationStore, ScopeBinding

    csrf = _login(client)
    rt = client.app.state.rt
    now = datetime.now(UTC)
    app_id, job_id = "app_idle", "job_idle"
    rt.store.c("jobs").insert_one(
        {
            "_id": job_id,
            "employer": "Northwind",
            "employer_norm": "northwind",
            "title": "Account Executive",
            "status": "qualified",
            "posting_url": "https://jobs.example/northwind",
            "apply_url": "https://jobs.example/northwind",
            "sources": ["manual"],
            "created_at": now,
            "updated_at": now,
        }
    )
    rt.store.c("applications").insert_one(
        {
            "_id": app_id,
            "job_id": job_id,
            "status": "authorized",
            "provenance": {"origin": "local"},
            "material_hash": "pkg-now",
            "fill_for": "pkg-now",
            "send_queued_at": now - timedelta(hours=1),
            "review_package": {"apply_url": "https://jobs.example/northwind", "posting_url": "https://jobs.example/northwind", "answers": [], "outstanding": [], "gaps": []},
            "history": [{"at": now - timedelta(hours=2), "status": "authorized"}],
            "created_at": now,
            "updated_at": now,
        }
    )
    rt.store.c("actions").insert_one(
        {"_id": "act_idle", "idempotency_key": f"submit:{job_id}", "kind": "application_submit", "target_id": job_id, "status": "failed", "material_hash": "pkg-now"}
    )
    AuthorizationStore(rt.store).grant("application_submit", ScopeBinding(target_id=job_id, material_hash="pkg-now"))
    page = client.get(f"/applications/{app_id}").text.split("What is happening", 1)[0]
    assert "This application is approved." in page
    assert "Your approval already covers them." in page
    assert ">Apply with the app</button>" in page
    assert f'action="/applications/{app_id}/retry"' in page
    assert 'name="csrf"' in page
    assert "Retry with the app" not in page
    assert "Approve this exact application" not in page
    approvals = rt.store.c("authorizations").count_documents({"binding.target_id": job_id})
    posted = client.post(f"/applications/{app_id}/retry", data={"csrf": csrf})
    assert posted.status_code == 303
    assert rt.store.c("authorizations").count_documents({"binding.target_id": job_id}) == approvals
    runs = list(rt.store.c("runs").find({"params.application_id": app_id}))
    assert len(runs) == 1 and runs[0]["status"] == "queued" and runs[0]["params"]["phase"] == "execute"
    retired = rt.store.c("actions").find_one({"_id": "act_idle"})
    assert retired["idempotency_key"] == f"submit:{job_id}:prior:act_idle"
    assert "Apply with the app" not in client.get(f"/applications/{app_id}").text
    again = client.post(f"/applications/{app_id}/retry", data={"csrf": csrf})
    assert again.status_code == 303
    assert rt.store.c("runs").count_documents({"params.application_id": app_id}) == 1
    rt.store.c("applications").insert_one(
        {
            "_id": "app_removed",
            "job_id": job_id,
            "status": "removed",
            "tombstone": True,
            "reason": "removed before anything was sent",
            "provenance": {"origin": "removed"},
            "history": [{"at": now, "status": "authorized"}],
            "created_at": now,
            "updated_at": now,
        }
    )
    removed = client.get("/applications/app_removed")
    assert removed.status_code == 200
    assert "Apply with the app" not in removed.text
