"""A required field that will not complete waits on the same page for one dashboard answer."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from amorehiremepls.apply.browser import PAGE_NOT_CONTINUED, BrowserAdapter, BrowserConfig, Plan, Step
from amorehiremepls.core.actions import ActionFailed
from amorehiremepls.core.config import Settings
from amorehiremepls.core.runtime import Runtime
from amorehiremepls.web.web import create_app
from tests.conftest import TEST_MONGO_URI

SECRET = "hidden-choice"


class _Empty:
    def count(self):
        return 0

    @property
    def first(self):
        return self

    def wait_for(self, **kwargs):
        raise TimeoutError("no list")

    def all_inner_texts(self):
        return []


class _Field:
    def __init__(self) -> None:
        self.typed: list[str] = []
        self.current = ""
        self.allow = False
        self.navigated = False

    def count(self):
        return 1

    @property
    def first(self):
        return self

    def nth(self, index):
        return self

    def evaluate(self, script):
        return "combobox"

    def click(self, timeout=None):
        return None

    def press(self, key):
        return None

    def press_sequentially(self, text, delay=0, timeout=None):
        self.typed.append(text)
        self.current = text

    def wait_for(self, state=None, timeout=None):
        if not self.allow:
            raise TimeoutError("no list")

    def all_inner_texts(self):
        return [self.current] if self.allow and self.current else []

    def locator(self, selector):
        return _Empty()


class _Scope:
    def __init__(self, field: _Field) -> None:
        self.field = field
        self.frame_selector = None
        self.page = self

    def locator(self, selector):
        if "option" in selector or "select__option" in selector:
            if self.field.allow and self.field.current:
                return self.field
            return _Empty()
        return self.field

    def get_by_label(self, *args, **kwargs):
        return self.field

    def body_text(self):
        return ""

    def current_url(self):
        return "https://boards.example/apply"

    def wait_for_timeout(self, ms):
        return None

    def goto(self, *args, **kwargs):
        self.field.navigated = True
        raise AssertionError("the page was navigated")


class _Hold:
    def __init__(self, answer: str, field: _Field, minutes: float = 5) -> None:
        self.answer = answer
        self.field = field
        self.minutes = minutes
        self.poll_seconds = 0.01
        self.beats: list[dict] = []
        self.finished = None

    def heartbeat(self, evidence=None):
        self.beats.append(dict(evidence or {}))
        self.field.allow = True

    def continue_requested(self):
        return bool(self.beats)

    def finish(self, status):
        self.finished = status

    def supplied_answer(self):
        return self.answer


def _adapter() -> tuple[BrowserAdapter, list[str]]:
    notes: list[str] = []
    adapter = BrowserAdapter(BrowserConfig(allowed_hosts=set()))
    adapter._say = notes.append  # type: ignore[method-assign]
    adapter._await_receipt = lambda *args, **kwargs: None  # type: ignore[method-assign]
    return adapter, notes


def _plan(step: Step) -> Plan:
    return Plan(start_url="https://boards.example/apply", steps=[step], receipt_text="thank you")


def test_location_tries_the_full_place_then_the_city_then_fallbacks_and_only_then_asks():
    field = _Field()
    hold = _Hold("West Harbor", field)
    adapter, notes = _adapter()
    step = Step("autocomplete", "#location", "Lakeside, Northland", label="Location", fallbacks=["Harbor"])
    adapter._run_steps(_Scope(field), _plan(step), {"steps_done": 0, "final_clicked": False}, adapter._shot_target(), None, hold)
    assert field.typed == ["Lakeside, Northland", "Lakeside", "Harbor", "West Harbor"]
    assert field.navigated is False
    text = " ".join(notes)
    assert "Lakeside, Northland" in text and "Lakeside" in text and "Harbor" in text
    assert "No list appeared" in text
    assert "West Harbor" in text
    assert hold.beats and hold.beats[-1]["purpose"] == "field"
    assert hold.beats[-1]["field_label"] == "Location"
    assert "No list appeared" in hold.beats[-1]["field_reason"]
    assert field.typed.index("West Harbor") == 3


def test_a_sensitive_answer_is_not_copied_into_the_log():
    field = _Field()
    hold = _Hold(SECRET, field)
    adapter, notes = _adapter()
    step = Step("autocomplete", "#identity", SECRET, label="Identity", sensitive=True)
    adapter._run_steps(_Scope(field), _plan(step), {"steps_done": 0, "final_clicked": False}, adapter._shot_target(), None, hold)
    text = " ".join(notes)
    assert SECRET not in text
    assert "Answer received for Identity" in text
    assert hold.beats[-1]["field_reason"] == "Identity needs an answer."
    assert SECRET not in hold.beats[-1]["field_reason"]
    assert field.typed[-1] == SECRET


def test_a_hold_that_cannot_stay_open_does_not_pretend_to_continue():
    field = _Field()
    hold = _Hold("West Harbor", field, minutes=0)
    adapter, notes = _adapter()
    step = Step("autocomplete", "#location", "Lakeside, Northland", label="Location", fallbacks=["Harbor"])
    with pytest.raises(ActionFailed) as caught:
        adapter._run_steps(_Scope(field), _plan(step), {"steps_done": 0, "final_clicked": False}, adapter._shot_target(), None, hold)
    assert str(caught.value) == PAGE_NOT_CONTINUED
    assert "does not continue this form" in " ".join(notes)
    assert "West Harbor" not in field.typed


def test_an_optional_field_is_not_asked():
    field = _Field()
    hold = _Hold("West Harbor", field)
    adapter, _notes = _adapter()
    step = Step("autocomplete", "#location", "Lakeside", label="Location", optional=True)
    with pytest.raises(ActionFailed):
        adapter._run_steps(_Scope(field), _plan(step), {"steps_done": 0, "final_clicked": False}, adapter._shot_target(), None, hold)
    assert hold.beats == []
    assert field.typed == ["Lakeside"]


def test_watch_browser_ui_is_absent():
    from amorehiremepls.apply import browser as browser_mod

    web = Path(browser_mod.__file__).resolve().parents[1] / "web"
    settings = (web / "templates" / "settings.html").read_text()
    page = (web / "templates" / "application.html").read_text()
    script = (web / "static" / "app.js").read_text()
    routes = (web / "routes" / "settings.py").read_text()
    assert "Show the browser while applying" not in settings
    assert "browser_watch" not in settings and "browser_watch" not in routes
    assert "/settings/browser" not in routes
    assert "browser-card" not in page and "browser-shot" not in script


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
def test_the_dashboard_asks_with_one_input_and_does_not_log_a_sensitive_answer(client):
    rt = client.app.state.rt
    assert client.post("/login", data={"password": "correct-horse-battery"}).status_code == 303
    now = datetime.now(UTC)
    rt.store.c("jobs").insert_one(
        {
            "_id": "job_field",
            "employer": "Acme",
            "employer_norm": "acme",
            "title": "Account Executive",
            "status": "executing",
            "posting_url": "https://boards.example/jobs/1",
            "apply_url": "https://boards.example/jobs/1",
            "sources": ["manual"],
            "created_at": now,
            "updated_at": now,
        }
    )
    rt.store.c("applications").insert_one(
        {
            "_id": "app_field",
            "job_id": "job_field",
            "status": "executing",
            "provenance": {"origin": "local"},
            "material_hash": "m-field",
            "review_package": {"apply_url": "https://boards.example/jobs/1", "posting_url": "https://boards.example/jobs/1", "answers": [], "gaps": []},
            "created_at": now,
            "updated_at": now,
        }
    )
    rt.store.c("actions").insert_one(
        {
            "_id": "act_field",
            "kind": "application_submit",
            "target_id": "job_field",
            "idempotency_key": "submit:job_field",
            "status": "executing",
            "material_hash": "m-field",
            "created_at": now,
            "updated_at": now,
        }
    )
    for status in ("ready_for_review", "failed", "authorized", "submitted", "executing"):
        rt.store.c("applications").update_one({"_id": "app_field"}, {"$set": {"status": status}})
        quiet = client.get("/applications/app_field")
        assert quiet.status_code == 200
        assert 'id="field-answer"' not in quiet.text
        assert "Use this answer" not in quiet.text
        assert "This field" not in quiet.text
    rt.store.c("human_holds").insert_one(
        {
            "_id": "hold_field",
            "application_id": "app_field",
            "job_id": "job_field",
            "action_id": "act_field",
            "idempotency_key": "submit:job_field",
            "material_hash": "m-field",
            "status": "holding",
            "continue_requested": False,
            "purpose": "field",
            "field_label": "Identity",
            "field_reason": "Identity needs an answer.",
            "sensitive": True,
            "heartbeat_at": now,
            "started_at": now,
        }
    )
    page = client.get("/applications/app_field")
    assert page.status_code == 200
    assert 'id="field-answer"' in page.text
    assert 'id="field-answer-label">Identity<' in page.text
    assert "Identity needs an answer." in page.text
    assert "This field" not in page.text
    assert 'name="answer"' in page.text
    assert "The same page is still open" in page.text
    assert "Show the browser while applying" not in page.text
    assert "browser-card" not in page.text
    csrf = page.text.split('name="csrf" value="', 1)[1].split('"', 1)[0]
    saved = client.post("/applications/app_field/field-answer", data={"csrf": csrf, "material_hash": "m-field", "answer": SECRET})
    assert saved.status_code == 303 and "ok=field_ok" in saved.headers["location"]
    hold = rt.store.c("human_holds").find_one({"_id": "hold_field"})
    assert hold["answer"] == SECRET and hold["continue_requested"] is True
    again = client.get("/applications/app_field").text
    assert SECRET not in again
    activity = client.get("/applications/app_field/activity").json()
    assert SECRET not in str(activity)
    assert activity["field"]["label"] == "Identity"
    assert all(SECRET not in str(row) for row in rt.store.c("runs").find())
    assert all(SECRET not in str(row) for row in rt.store.c("audit").find())
