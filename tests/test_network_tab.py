"""Each job has its own network list. A request is stored when Chromium sees it, before the form run returns."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from amorehiremepls.apply.browser import BrowserAdapter, BrowserConfig, Plan
from amorehiremepls.apply.network_log import BODY_LIMIT, NetworkLog, watch_network
from amorehiremepls.core.config import Settings
from amorehiremepls.core.runtime import Runtime
from amorehiremepls.web.chrome import activity_payload
from amorehiremepls.web.web import create_app
from tests.conftest import TEST_MONGO_URI

ROOT = Path(__file__).resolve().parents[1]
COOKIE = "sid=blue-session-marker"
PERSONAL = "prefer-not-to-say-answer"


class _Req:
    def __init__(self, url, method="GET", resource_type="xhr", headers=None, post_data=None, timing=None):
        self.url = url
        self.method = method
        self.resource_type = resource_type
        self.headers = headers or {}
        self.post_data = post_data
        self.timing = timing if timing is not None else {"requestStart": 10, "responseEnd": 28}


class _Resp:
    def __init__(self, request, status=200, body="", headers=None):
        self.request = request
        self.status = status
        self.headers = headers or {}
        self._body = body

    def text(self):
        return self._body


class _Ctx:
    def __init__(self):
        self.handlers = {}

    def on(self, event, handler):
        self.handlers[event] = handler


def test_the_application_page_does_not_poll_a_network_tab():
    text = (ROOT / "src/amorehiremepls/web/static/app.js").read_text()
    html = (ROOT / "src/amorehiremepls/web/templates/application.html").read_text()
    assert "Network Tab" not in html and "network-lines" not in html and "network-tab" not in html
    assert "network-lines" not in text and "data.network" not in text and "network_open" not in text
    assert "activity-lines" in text and "data-copy" in text
    assert "if (!data.live) clearInterval(activityTimer);" in text


def test_capture_starts_before_the_first_page_load():
    text = (ROOT / "src/amorehiremepls/apply/browser.py").read_text()
    body = text.split("def run_plan", 1)[1].split("def scan_form", 1)[0]
    assert body.index("watch_network(") < body.index("new_page(")
    assert body.index("watch_network(") < body.index(".goto(")


@pytest.mark.mongo
def test_two_jobs_stay_separate_and_clear_removes_one(store):
    recorder = NetworkLog(store)
    one = _Req("https://boards.example/jobs/one/suggest")
    two = _Req("https://boards.example/jobs/two/suggest", resource_type="fetch")
    one_id = recorder.begin("job_one", one)
    recorder.finish("job_one", one_id, one, _Resp(one, body='{"choices":["North","South"]}'))
    two_id = recorder.begin("job_two", two)
    recorder.finish("job_two", two_id, two, _Resp(two, body='{"choices":["East"]}'))
    rows = recorder.for_job("job_one")
    assert len(rows) == 1 and rows[0]["url"].endswith("/jobs/one/suggest")
    assert rows[0]["status"] == 200 and rows[0]["timing"] == "18 ms" and "North" in rows[0]["body"]
    assert "East" not in json.dumps(rows, default=str)
    payload = activity_payload({"status": "executing", "history": [], "job_id": "job_one"}, [], network=rows)
    assert payload["network"][0]["url"].endswith("/jobs/one/suggest")
    assert all("jobs/two" not in row["url"] for row in payload["network"])
    recorder.clear("job_one")
    assert recorder.for_job("job_one") == []
    assert recorder.for_job("job_two")[0]["url"].endswith("/jobs/two/suggest")


@pytest.mark.mongo
def test_a_cookie_is_not_stored_and_a_personal_answer_is_left_out(store):
    recorder = NetworkLog(store)
    ctx = _Ctx()
    watch_network(ctx, recorder, "job_one", sensitive=[PERSONAL])
    req = _Req(
        "https://boards.example/jobs/one/suggest",
        headers={"Cookie": COOKIE, "Authorization": f"Bearer {COOKIE}", "Accept": "application/json"},
    )
    ctx.handlers["request"](req)
    mid = activity_payload({"status": "executing", "history": []}, [], network=recorder.for_job("job_one"))
    assert mid["network"] and mid["network"][0]["method"] == "GET"
    assert COOKIE not in json.dumps(mid)
    ctx.handlers["response"](_Resp(req, body='{"choices":["North","South"]}', headers={"set-cookie": COOKIE}))
    saved = json.dumps(store.db["network"].find_one({"job_id": "job_one"}), default=str)
    assert COOKIE not in saved and "North" in recorder.for_job("job_one")[0]["body"]

    posted = _Req("https://boards.example/submit", method="POST", post_data=f"answer={PERSONAL}")
    ctx.handlers["request"](posted)
    ctx.handlers["response"](_Resp(posted, body=f"echo {PERSONAL}"))
    blob = json.dumps(store.db["network"].find_one({"job_id": "job_one"}), default=str)
    assert PERSONAL not in blob
    echo = [row for row in recorder.for_job("job_one") if row["url"].endswith("/submit")]
    assert echo and echo[0]["body"] == ""


@pytest.mark.mongo
def test_bodies_are_short_and_only_the_newest_rows_stay(store):
    recorder = NetworkLog(store)
    wide = _Req("https://boards.example/suggest", resource_type="fetch")
    row_id = recorder.begin("job_wide", wide)
    recorder.finish("job_wide", row_id, wide, _Resp(wide, body="x" * (BODY_LIMIT + 400)))
    assert len(recorder.for_job("job_wide")[0]["body"]) == BODY_LIMIT
    for i in range(205):
        recorder.begin("job_cap", _Req(f"https://boards.example/n/{i}", resource_type="script"))
    rows = recorder.for_job("job_cap")
    assert len(rows) == 200
    assert rows[0]["url"].endswith("/n/204")
    assert all(not row["url"].endswith("/n/0") for row in rows)
    assert any(row["url"].endswith("/n/5") for row in rows)


@pytest.mark.mongo
def test_a_request_is_visible_before_run_plan_returns(store, monkeypatch):
    import playwright.sync_api as pwapi

    recorder = NetworkLog(store)
    seen = {}

    class Context:
        def __init__(self):
            self.handlers = {}

        def set_default_timeout(self, _ms):
            return None

        def route(self, _pattern, _handler):
            return None

        def on(self, event, handler):
            self.handlers[event] = handler

        def new_page(self):
            return Page(self)

        def close(self):
            return None

    class Page:
        def __init__(self, ctx):
            self.ctx = ctx
            self.url = "https://boards.example/apply/1"

        def goto(self, _url, wait_until=None):
            req = _Req("https://boards.example/suggest?q=place", headers={"cookie": COOKIE})
            self.ctx.handlers["request"](req)
            mid = activity_payload({"status": "executing", "history": []}, [], network=recorder.for_job("job_live"))
            assert mid["network"][0]["url"].endswith("/suggest?q=place")
            assert mid["network"][0]["status"] is None and COOKIE not in json.dumps(mid)
            self.ctx.handlers["response"](_Resp(req, body='{"choices":["North","South"]}'))
            seen["payload"] = activity_payload({"status": "executing", "history": []}, [], network=recorder.for_job("job_live"))
            raise RuntimeError("still inside the run")

    class Browser:
        def new_context(self, **_kwargs):
            return Context()

        def close(self):
            return None

    class PW:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def launch(self, **_kwargs):
            return Browser()

        @property
        def chromium(self):
            return self

    monkeypatch.setattr(pwapi, "sync_playwright", lambda: PW())
    adapter = BrowserAdapter(BrowserConfig(allowed_hosts={"boards.example"}), network=recorder)
    plan = Plan(start_url="https://boards.example/apply/1", steps=[], receipt_text="done", job_id="job_live")
    with pytest.raises(RuntimeError, match="still inside the run"):
        adapter.run_plan(plan)
    row = seen["payload"]["network"][0]
    assert row["status"] == 200 and row["timing"] == "18 ms" and "North" in row["body"]
    raw = json.dumps(store.db["network"].find_one({"job_id": "job_live"}), default=str)
    assert COOKIE not in raw


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


def _app(store, job_id, app_id):
    now = datetime(2026, 11, 6, 15, tzinfo=UTC)
    store.c("jobs").insert_one(
        {
            "_id": job_id,
            "employer": "Acme",
            "employer_norm": "acme",
            "title": "Account Executive",
            "status": "qualified",
            "posting_url": "https://boards.example/jobs/1",
            "apply_url": "https://boards.example/jobs/1",
            "sources": ["manual"],
            "created_at": now,
            "updated_at": now,
        }
    )
    store.c("applications").insert_one(
        {
            "_id": app_id,
            "job_id": job_id,
            "status": "failed",
            "provenance": {"origin": "local"},
            "material_hash": "m-net",
            "review_package": {"apply_url": "https://boards.example/jobs/1", "posting_url": "https://boards.example/jobs/1", "answers": [], "gaps": [], "outstanding": []},
            "evidence": {"screenshots": [{"label": "summary", "path": f"jobs/{job_id}/screenshots/summary.png"}]},
            "history": [],
            "created_at": now,
            "updated_at": now,
        }
    )
    store.c("artifacts").insert_one({"_id": f"art_{job_id}", "owner_id": job_id, "kind": "screenshot", "sha256": "abc"})


@pytest.mark.mongo
def test_the_page_does_not_show_a_network_tab(client):
    rt = client.app.state.rt
    assert client.post("/login", data={"password": "correct-horse-battery"}).status_code == 303
    _app(rt.store, "job_one", "app_one")
    _app(rt.store, "job_two", "app_two")
    recorder = NetworkLog(rt.store)
    recorder.begin("job_one", _Req("https://boards.example/jobs/one/suggest"))
    recorder.begin("job_two", _Req("https://boards.example/jobs/two/suggest"))
    page = client.get("/applications/app_one").text
    assert "Network Tab" not in page and 'id="network-tab"' not in page and 'id="network"' not in page
    assert "Nothing was recorded for the last time." not in page
    assert ">Clear</button>" not in page
    assert "jobs/one/suggest" not in page and "jobs/two/suggest" not in page
    assert 'data-copy="application-details"' in page and ">Copy</button>" in page
    activity = client.get("/applications/app_one/activity").json()
    blob = json.dumps(activity)
    assert "jobs/one/suggest" not in blob and "jobs/two/suggest" not in blob
    assert "lines" in activity
    overview = client.get("/").text
    assert "jobs/one/suggest" not in overview and "jobs/two/suggest" not in overview and "Network Tab" not in overview
    assert recorder.for_job("job_one") and recorder.for_job("job_two")
    kept = rt.store.c("applications").find_one({"_id": "app_one"})
    assert kept["status"] == "failed" and kept["evidence"]["screenshots"]
    assert rt.store.c("artifacts").find_one({"_id": "art_job_one"})["kind"] == "screenshot"
