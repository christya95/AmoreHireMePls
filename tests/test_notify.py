"""Owner notifications: fake SMTP (no network), webhook JSON, dedupe, and the settings card."""

from __future__ import annotations

import re
from email.message import EmailMessage
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from amorehiremepls.apply.applications import ApplicationService
from amorehiremepls.apply.browser import HandoffRequired, Plan
from amorehiremepls.core.actions import ActionStore, AmbiguousOutcome
from amorehiremepls.core.authorization import AuthorizationStore, ScopeBinding, material_hash
from amorehiremepls.core.config import Settings
from amorehiremepls.core.notify import Notifier
from amorehiremepls.core.runtime import Runtime
from amorehiremepls.web.web import create_app
from tests.conftest import TEST_MONGO_URI
from tests.test_applications_and_sync import _job

pytestmark = pytest.mark.mongo

SECRET = "mailbox-secret-not-for-logs"
ANSWER = "SECRET_ANSWER_VALUE resume body should never be copied"


def _settings(store, **over) -> Settings:
    base = dict(
        mongodb_uri=TEST_MONGO_URI,
        mongodb_database=store.db.name,
        app_password="correct-horse-battery",
        app_secret="x" * 40,
        notify_email_to="owner@example.com",
        smtp_host="smtp.example",
        smtp_port=587,
        smtp_user="mailbox-user",
        smtp_password=SecretStr(SECRET),
        smtp_from="app@example.com",
        publish_url="http://dash.example:8790",
        _env_file=None,
    )
    base.update(over)
    return Settings(**base)


def _notifier(store, settings, sent, posts=None):
    return Notifier(store, settings, ActionStore(store), smtp_send=sent.append, http_post=None if posts is None else (lambda url, payload: posts.append((url, payload))))


def _job_app(store):
    job = _job(store)
    h = material_hash(answers=[("why", ANSWER)], extra={"job_id": job["_id"]})
    app = {
        "_id": "app_notify",
        "job_id": job["_id"],
        "status": "authorized",
        "provenance": {"origin": "local"},
        "material_hash": h,
        "review_package": {
            "employer": job["employer"],
            "title": job["title"],
            "apply_url": job["apply_url"],
            "posting_url": job["posting_url"],
            "requisition_id": job["requisition_id"],
            "answers": [{"field_id": "why", "label": "Why", "value": ANSWER, "needs_input": False, "fact_ids": []}],
            "outstanding": [],
            "document": {
                "filename": "Alex_Example_Resume.pdf",
                "path": "/tmp/Alex_Example_Resume.pdf",
                "pdf_path": "/tmp/Alex_Example_Resume.pdf",
                "page_limit_verified": True,
                "pages": 1,
            },
        },
        "history": [],
    }
    store.c("applications").insert_one(app)
    store.c("runs").insert_one({"_id": "run_notify", "kind": "application", "params": {"job_id": job["_id"]}, "status": "running", "thread_id": "t_notify"})
    return job, app


def test_email_uses_starttls_and_does_not_log_the_password(store, caplog):
    settings = _settings(store)
    notifier = Notifier(store, settings, ActionStore(store))
    with patch("smtplib.SMTP") as smtp, caplog.at_level("WARNING"):
        client = smtp.return_value.__enter__.return_value
        notifier._send_email("Hello", "Body only")
    client.ehlo.assert_called()
    client.starttls.assert_called_once()
    client.login.assert_called_once_with("mailbox-user", SECRET)
    client.send_message.assert_called_once()
    assert SECRET not in caplog.text


def test_body_is_job_and_reason_only_and_a_repeat_is_deduped(store):
    sent: list[EmailMessage] = []
    settings = _settings(store, notify_webhook_url="https://hooks.example/notify")
    posts: list[tuple] = []
    notifier = _notifier(store, settings, sent, posts)
    job, app = _job_app(store)
    first = notifier.notify(state="human_check", job=job, application=app, run_id="run_notify")
    second = notifier.notify(state="human_check", job=job, application=app, run_id="run_notify")
    assert first["sent"] and not first["deduped"]
    assert second["deduped"] and not second["sent"]
    assert len(sent) == 1 and len(posts) == 1
    body = sent[0].get_content()
    assert "Backend Developer" in body and "Acme" in body
    assert "human check" in body.lower()
    assert "http://dash.example:8790/applications/app_notify" in body
    assert ANSWER not in body and SECRET not in body and "op://" not in body
    url, payload = posts[0]
    assert url == "https://hooks.example/notify"
    assert payload["event"] == "human_check" and payload["employer"] == "Acme" and ANSWER not in str(payload) and SECRET not in str(payload)
    assert store.c("notifications").count_documents({"idempotency_key": "notify:run_notify:human_check"}) == 1
    # a different state for the same run is a new note
    assert notifier.notify(state="submitted", job=job, application=app, run_id="run_notify")["sent"]
    assert len(sent) == 2


def test_unconfigured_channels_do_not_send_or_record(store):
    sent: list = []
    settings = _settings(store, notify_email_to="", smtp_host="", smtp_password=SecretStr(""), notify_webhook_url="")
    notifier = _notifier(store, settings, sent)
    job, app = _job_app(store)
    result = notifier.notify(state="handoff", job=job, application=app, run_id="run_notify")
    assert result["reason"] == "not_configured" and not sent
    assert store.c("notifications").count_documents({}) == 0


def test_publish_host_is_used_when_publish_url_is_empty(store):
    sent: list[EmailMessage] = []
    settings = _settings(store, publish_url="", publish_host="192.168.2.49", port=8790)
    notifier = _notifier(store, settings, sent)
    job, app = _job_app(store)
    assert notifier.notify(state="needs_verification", job=job, application=app, run_id="run_notify")["sent"]
    assert "http://192.168.2.49:8790/applications/app_notify" in sent[0].get_content()


def test_application_outcomes_notify_once_each(store, tmp_path):
    from amorehiremepls.candidate.profile import Profile

    sent: list[EmailMessage] = []
    settings = _settings(store)
    notifier = _notifier(store, settings, sent)
    job, app = _job_app(store)
    AuthorizationStore(store).grant("application_submit", ScopeBinding(target_id=job["_id"], material_hash=app["material_hash"]))
    plan = lambda job, pkg, answers: Plan(start_url=pkg["apply_url"], steps=[], receipt_text="Thank you")  # noqa: E731
    svc = ApplicationService(store, Profile(), None, ActionStore(store), {"ats.example": plan}, notifier=notifier)  # type: ignore[arg-type]

    def unclear(_plan):
        raise AmbiguousOutcome("receipt not observed", {"final_clicked": True})

    out = svc.execute(app["_id"], "w1", unclear)
    assert out.status == "needs_verification"
    assert any("could not tell" in m.get_content() for m in sent)

    store.c("applications").update_one({"_id": app["_id"]}, {"$set": {"status": "authorized"}})
    store.c("actions").delete_many({})
    store.c("authorizations").delete_many({})
    AuthorizationStore(store).grant("application_submit", ScopeBinding(target_id=job["_id"], material_hash=app["material_hash"]))

    def challenge(_plan):
        raise HandoffRequired("the job site asked for a human check", {"challenge": "text_captcha"})

    out = svc.execute(app["_id"], "w1", challenge)
    assert out.status == "handoff" and svc.apps.find_one({"_id": app["_id"]})["handoff_kind"] == "human_check"
    assert any("human check" in m.get_content().lower() for m in sent)
    before = len(sent)
    store.c("applications").update_one({"_id": app["_id"]}, {"$set": {"status": "authorized"}})
    assert svc.execute(app["_id"], "w1", challenge).status == "handoff"
    assert len(sent) == before  # same run + human_check state

    store.c("applications").update_one({"_id": app["_id"]}, {"$set": {"status": "authorized"}})
    bare = ApplicationService(store, Profile(), None, ActionStore(store), {}, notifier=notifier)  # type: ignore[arg-type]
    assert bare.execute(app["_id"], "w1", lambda _plan: {"verified": True}).status == "handoff"
    assert any("finish it on the job site" in m.get_content() for m in sent)

    store.c("applications").update_one({"_id": app["_id"]}, {"$set": {"status": "authorized"}})
    store.c("actions").delete_many({})
    AuthorizationStore(store).grant("application_submit", ScopeBinding(target_id=job["_id"], material_hash=app["material_hash"]))

    def confirmed(_plan):
        return {"verified": True, "receipt_text": "Thank you for applying", "final_url": "https://ats.example/thanks", "matched_identifiers": []}

    out = svc.execute(app["_id"], "w1", confirmed)
    assert out.status == "succeeded"
    assert any("confirmation" in m.get_content().lower() for m in sent)
    again = len(sent)
    assert svc.execute(app["_id"], "w2", lambda _plan: pytest.fail("must not send again")).status == "succeeded"
    assert len(sent) == again
    for message in sent:
        assert ANSWER not in message.get_content() and SECRET not in message.get_content()


def test_settings_card_and_test_button(store, tmp_path):
    settings = _settings(store, artifact_dir=tmp_path / "a", private_dir=tmp_path / "p", log_dir=tmp_path / "l", tracker_clone_dir=tmp_path / "clone")
    sent: list[EmailMessage] = []
    rt = Runtime(settings, store=store)
    rt.notifier = lambda: _notifier(store, settings, sent)  # type: ignore[method-assign]
    client = TestClient(create_app(rt), follow_redirects=False)
    assert client.post("/login", data={"password": "correct-horse-battery"}).status_code == 303
    page = client.get("/settings")
    assert page.status_code == 200
    assert "Notifications" in page.text and "Set up" in page.text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    assert client.post("/settings/notify-test", data={"csrf": csrf}).status_code == 303
    assert len(sent) == 1 and "test notification" in sent[0].get_content().lower()
    assert SECRET not in sent[0].get_content()
    off = _settings(store, notify_email_to="", smtp_password=SecretStr(""), notify_webhook_url="")
    rt.notifier = lambda: _notifier(store, off, sent)  # type: ignore[method-assign]
    csrf = re.search(r'name="csrf" value="([^"]+)"', client.get("/settings").text).group(1)
    refused = client.post("/settings/notify-test", data={"csrf": csrf}, follow_redirects=False)
    assert refused.status_code == 303 and "notify_off" in refused.headers["location"]
    assert len(sent) == 1
