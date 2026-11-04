import pytest
from fastapi.testclient import TestClient

from amorehiremepls.config import Settings
from amorehiremepls.runtime import Runtime
from amorehiremepls.web import create_app
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
    assert r.status_code == 303 and "batch" in client.get("/authorizations").text
    r = client.post("/outreach/recipients", data={"csrf": csrf, "name": "Sam Recruiter", "employer": "Acme", "profile_url": "https://linkedin.com/in/sam-r", "remit": "TA"})
    assert r.status_code == 303 and "Sam Recruiter" in client.get("/outreach").text
    # drafting with an unconfirmed applied role is refused
    rid = client.app.state.rt.store.c("recipients").find_one({})["_id"]
    assert client.post(f"/outreach/recipients/{rid}/draft", data={"csrf": csrf, "kind": "connection_request", "applied_role": "Backend Developer"}).status_code == 400
    assert client.post(f"/outreach/recipients/{rid}/draft", data={"csrf": csrf, "kind": "connection_request", "applied_role": ""}).status_code == 303
    assert client.app.state.rt.store.c("outreach").find_one({})["status"] == "draft"


def test_review_page_and_single_scope_authorization(client, tmp_path):
    from amorehiremepls.profile import build_profile_from_skills
    from amorehiremepls.worker import Worker
    from tests.fixtures.make_resume import make_base_resume
    from tests.test_profile import FAKE_FACTS, FAKE_SALARY

    csrf = _login(client)
    rt = client.app.state.rt
    rt.profiles.save(build_profile_from_skills(FAKE_FACTS, FAKE_SALARY), actor="test")
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
    assert page.status_code == 200 and "Authorize this exact submission" in page.text
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
    assert "Record manual submission" in client.get(f"/applications/{app_id}").text
