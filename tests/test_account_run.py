"""Creating a job-site account: the approval, the one send, the stops, and the page.

The site is the local fixture, the 1Password tool is the in-memory stand-in, and the mailbox is not the owner's.
Nothing here opens a window, calls the real tool, or contacts a real host.
"""

from __future__ import annotations

import re
import socket
import threading

import pytest
from fastapi.testclient import TestClient

from amorehiremepls.apply.account_run import SiteSignUp
from amorehiremepls.apply.browser import BrowserAdapter, BrowserConfig, HandoffRequired, Plan, Step
from amorehiremepls.apply.signin import SignInLimits, SiteSignIn
from amorehiremepls.apply.signup import PasswordRules
from amorehiremepls.core.account_items import OpCliWriter
from amorehiremepls.core.config import Settings
from amorehiremepls.core.credentials import OpCliSource
from amorehiremepls.core.runtime import Runtime
from amorehiremepls.core.sealed import HandoffKey, seal, sign_public
from amorehiremepls.core.site_accounts import AccountStore, approve
from amorehiremepls.core.site_signin import SignInMemory, ask_context
from amorehiremepls.web.web import create_app
from tests.conftest import TEST_MONGO_URI
from tests.fixtures.account_site import CODE, EMAIL, STATE, AccountSite
from tests.fixtures.fake_op import VAULT, FakeOp
from tests.test_browser_adapter import skip_no_browser
from tests.test_signin import SECRET, FakeHold

HOST = "127.0.0.1"
LIMITS = SignInLimits(form_wait_ms=800, settle_ms=4000, stable_ms=400, max_seconds=40)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind((HOST, 0))
        return sock.getsockname()[1]


@pytest.fixture
def site():
    with AccountSite(_free_port()) as srv:
        yield srv


def _runtime(store, tmp_path):
    settings = Settings(
        mongodb_uri=TEST_MONGO_URI,
        mongodb_database=store.db.name,
        artifact_dir=tmp_path / "a",
        private_dir=tmp_path / "p",
        log_dir=tmp_path / "l",
        app_password="correct-horse-battery",
        app_secret=SECRET,
        site_account_email=EMAIL,
        site_account_vault=VAULT["name"],
        tracker_clone_dir=tmp_path / "clone",
        _env_file=None,
    )
    rt = Runtime(settings, store=store)
    rt.store.set_control("site_signin", True)
    rt.store.set_control("site_account_create", True)
    op = FakeOp()
    source = OpCliSource(binary="python3", vault=VAULT["name"], run=op)
    rt.item_writer = lambda: OpCliWriter(source)  # type: ignore[method-assign]
    rt.credential_source = lambda: source  # type: ignore[method-assign]
    return rt, op


def _job(store) -> dict:
    job = {"_id": "job_acct", "employer": "Northwind Traders", "title": "Account executive", "status": "qualified", "requisition_id": "R-acct"}
    store.c("jobs").insert_one(job)
    store.c("applications").insert_one({"_id": "app_acct", "job_id": job["_id"], "status": "executing", "material_hash": "hash_acct"})
    return job


class _Owner:
    """Stands in for the press on the application page. It uses the real approval, which saves the login first."""

    def __init__(self, rt, job, code: str = "", link: str = "") -> None:
        self.rt, self.job, self.code, self.link = rt, job, code, link
        self.presses = 0

    def answer(self, held: dict):
        ask = (held or {}).get("signin") or {}
        step = str(ask.get("step") or "")
        if step in ("account", "password"):
            row = AccountStore(self.rt.store).get(str(ask.get("account") or ""))
            press = approve(self.rt, ask, row or {}, self.job, str(ask.get("offer") or ""))
            self.presses += 1
            return press.reply, ""
        if step == "mail_code" and self.code:
            return {"kind": "box", "box": seal(str(ask.get("key") or ""), {"host": ask.get("host"), "code": self.code}, ask_context(ask))}, ""
        if step == "mail_link" and self.link:
            return {"kind": "box", "box": seal(str(ask.get("key") or ""), {"host": ask.get("host"), "link": self.link}, ask_context(ask))}, ""
        return None, ""


def _run(site, sc: str, tmp_path, rt, job, owner: _Owner):
    hold = FakeHold(owner, minutes=0.4)
    adapter = BrowserAdapter(BrowserConfig(allowed_hosts={HOST}, session_dir=tmp_path / "sessions", artifact_dir=tmp_path / "artifacts"))
    plan = Plan(
        start_url=f"{site.base}/join/{sc}/job",
        steps=[Step("fill", "#full_name", "Casey Fixture"), Step("click", "#submit", final=True)],
        receipt_text="Application received",
        receipt_must_include=[f"R-{sc}"],
        job_id=job["_id"],
    )
    signin = SiteSignIn(rt.settings.app_secret.get_secret_value(), SignInMemory(rt.store), LIMITS, job_id=job["_id"])
    accounts = SiteSignUp(rt, job, "app_acct", "w1")
    try:
        result = adapter.run_plan(plan, site_key=HOST, hold=hold, signin=signin, accounts=accounts)
    except HandoffRequired as stop:
        result = stop
    return result, hold


@pytest.mark.mongo
def test_one_claim_sends_and_a_second_worker_does_not(store):
    accounts = AccountStore(store)
    row = accounts.open(accounts.key_for(HOST, EMAIL), HOST, f"http://{HOST}/join", "job_acct", "app_acct")
    accounts.update(row["_id"], status="item_saved", approved="offer-1")
    wins = []

    def claim():
        wins.append(accounts.claim_send(row["_id"], "offer-1"))

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert wins.count(True) == 1 and wins.count(False) == 1


@pytest.mark.mongo
def test_a_changed_offer_is_refused_and_a_matching_one_saves_the_login_first(store, tmp_path):
    rt, op = _runtime(store, tmp_path)
    job = _job(store)
    accounts = AccountStore(store)
    row = accounts.open(accounts.key_for(HOST, EMAIL), HOST, f"http://{HOST}/signup", job["_id"], "app_acct")
    accounts.offer(row["_id"], {"state": "ready", "host": HOST}, "offer-a", PasswordRules(), 1, "account")
    ask = {"ask_id": "ask_1", "host": HOST, "step": "account", "account": row["_id"], "offer": "offer-b", "key": "not-used"}
    refused = approve(rt, ask, accounts.get(row["_id"]), job, "offer-b")
    assert refused.flash == "account_changed" and refused.reply is None and op.items == {}
    accounts.offer(row["_id"], {"state": "ready", "host": HOST}, "offer-a", PasswordRules(), 1, "account")
    ask["offer"] = "offer-a"
    key = HandoffKey()
    ask["key"] = key.public
    ask["key_sig"] = sign_public(SECRET, key.public, ask_context(ask))
    saved = approve(rt, ask, accounts.get(row["_id"]), job, "offer-a")
    assert saved.ok and saved.reply and saved.reply.get("kind") == "box"
    assert op.items and "password" not in str(saved.reply)
    action = rt.actions.by_key(row["key"])
    calls = []
    first = rt.actions.execute(action["_id"], "w1", lambda doc: calls.append("w1") or {"verified": True, "signup": "signed_in"})
    second = rt.actions.execute(action["_id"], "w2", lambda doc: calls.append("w2") or {"verified": True, "signup": "signed_in"})
    assert first.status == "succeeded" and second.status == "succeeded" and calls == ["w1"]


@pytest.mark.mongo
def test_settings_shows_both_switches_off_and_the_connect_command(store, tmp_path):
    rt, _op = _runtime(store, tmp_path)
    rt.store.set_control("site_account_create", False)
    rt.store.set_control("mail_verification", False)
    client = TestClient(create_app(rt), follow_redirects=False)
    assert client.post("/login", data={"password": "correct-horse-battery"}).status_code == 303
    page = client.get("/settings").text
    assert "Create an account when a job site needs one" in page
    assert "Read the verification email for me" in page
    assert "amore mail connect" in page
    assert "Fixture Vault" in page
    assert EMAIL in page
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    turned = client.post("/settings/account", data={"csrf": csrf, "create": "on"}, follow_redirects=False)
    assert turned.status_code == 303 and rt.site_account_create_enabled()


@pytest.mark.browser
@skip_no_browser
@pytest.mark.mongo
@pytest.mark.parametrize("scenario", ["ok", "code", "link", "weak"])
def test_an_approved_sign_up_is_sent_once_and_the_application_continues(site, scenario, store, tmp_path):
    rt, op = _runtime(store, tmp_path)
    job = _job(store)
    link = f"{site.base}/join/link/open"
    owner = _Owner(rt, job, code=CODE, link=link)
    result, _hold = _run(site, scenario, tmp_path, rt, job, owner)
    assert not isinstance(result, HandoffRequired), getattr(result, "reason", result)
    assert result["signin"]["outcome"] == "signed_in"
    assert STATE[scenario]["applied"] == 1
    assert STATE[scenario]["posts"] == (2 if scenario == "weak" else 1)
    assert owner.presses == (2 if scenario == "weak" else 1)
    assert op.items
    row = AccountStore(store).by_key(AccountStore(store).key_for(HOST, EMAIL))
    assert row["status"] == "confirmed" and row["item_pending"] == "confirmed"


@pytest.mark.browser
@skip_no_browser
@pytest.mark.mongo
@pytest.mark.parametrize(
    ("scenario", "code"),
    [("exists", "signup_email_exists"), ("captcha", "signup_captcha"), ("phone", "signup_phone"), ("security", "signup_security"), ("blank", "signup_unclear")],
)
def test_each_stop_sends_nothing_or_never_sends_again(site, scenario, code, store, tmp_path):
    rt, _op = _runtime(store, tmp_path)
    job = _job(store)
    result, _hold = _run(site, scenario, tmp_path, rt, job, _Owner(rt, job))
    assert isinstance(result, HandoffRequired)
    assert (result.evidence.get("account") or {}).get("outcome") == code
    if scenario == "blank":
        assert STATE[scenario]["posts"] == 1
        again, _hold = _run(site, scenario, tmp_path, rt, job, _Owner(rt, job))
        assert STATE[scenario]["posts"] == 1
        assert isinstance(again, HandoffRequired)
    elif scenario == "exists":
        assert STATE[scenario]["posts"] == 1
    else:
        assert STATE[scenario]["posts"] == 0


@pytest.mark.mongo
def test_the_application_page_shows_the_card_and_the_unclear_choice(store, tmp_path):
    rt, _op = _runtime(store, tmp_path)
    job = _job(store)
    accounts = AccountStore(store)
    row = accounts.open(accounts.key_for(HOST, EMAIL), HOST, f"http://{HOST}/signup", job["_id"], "app_acct")
    accounts.offer(
        row["_id"],
        {
            "state": "ready",
            "host": HOST,
            "typed": [{"label": "Email", "value": EMAIL, "role": "email"}],
            "ticked": [{"label": "Terms", "links": [["terms", "https://jobs.example.test/terms"]]}],
            "not_ticked": [{"label": "Marketing"}],
            "terms": [["terms", "https://jobs.example.test/terms"]],
            "questions": [],
        },
        "offer-a",
        PasswordRules(),
        1,
        "account",
    )
    from datetime import timedelta

    from amorehiremepls.core.util import utcnow

    rt.store.c("human_holds").insert_one(
        {
            "_id": "hold_acct",
            "application_id": "app_acct",
            "job_id": job["_id"],
            "status": "holding",
            "purpose": "account",
            "started_at": utcnow(),
            "heartbeat_at": utcnow(),
            "expires_at": utcnow() + timedelta(minutes=10),
            "signin": {"ask_id": "ask_1", "host": HOST, "step": "account", "offer": "offer-a", "account": row["_id"]},
        }
    )
    client = TestClient(create_app(rt), follow_redirects=False)
    assert client.post("/login", data={"password": "correct-horse-battery"}).status_code == 303
    page = client.get("/applications/app_acct").text
    assert "Create this account" in page
    assert EMAIL in page
    assert "Fixture Vault" in page
    assert "https://jobs.example.test/terms" in page
    assert "Not ticked" in page
