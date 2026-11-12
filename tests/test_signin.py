"""Site sign-in with a saved login: matching, the sealed handoff, the owner's approval, the page step and its limits.

Every site here is the local fixture and every login is fictional. Nothing leaves this computer."""

from __future__ import annotations

import json
import logging
import re
import socket
import subprocess
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from amorehiremepls.apply.browser import BrowserAdapter, BrowserConfig, HandoffRequired, Plan, Step
from amorehiremepls.apply.signin import SignInLimits, SiteSignIn, secure_page
from amorehiremepls.core.actions import ActionStore
from amorehiremepls.core.config import Settings
from amorehiremepls.core.credentials import (
    ApprovalNotGiven,
    Credential,
    LookupTransient,
    NoSavedLogin,
    OpCliSource,
    SavedLogin,
    SeveralSavedLogins,
    SourceUnavailable,
    choose_login,
    fingerprint,
    registrable_domain,
    same_site,
    with_retries,
)
from amorehiremepls.core.human_check import HumanHold, _field_hold_fields
from amorehiremepls.core.runtime import Runtime
from amorehiremepls.core.sealed import HandoffKey, SealError, public_is_ours, seal, sign_public
from amorehiremepls.core.site_signin import APPROVAL_ASKS, STOPS, WALLS, SignInMemory, answer_ask, ask_context
from amorehiremepls.core.util import utcnow
from amorehiremepls.web.chrome import _HIDDEN_WORDS, needed_now
from amorehiremepls.web.web import create_app
from tests.conftest import TEST_MONGO_URI
from tests.fixtures import signin_site
from tests.fixtures.signin_site import CODE, NEW_CODE, PASSWORD, USERNAME, SignInSite
from tests.test_applications_and_sync import _job
from tests.test_browser_adapter import skip_no_browser

SECRET = "s" * 40
HOST = "127.0.0.1"
FAST = SignInLimits(form_wait_ms=4000, settle_ms=6000, stable_ms=600, max_seconds=60, code_attempts=3)


# -- doubles ---------------------------------------------------------------------------------------------
class FakeSource:
    """Deterministic credential source. `fail` is a queue of errors the next calls raise, in order."""

    def __init__(self, rows: list[dict] | None = None, fail: list[Exception] | None = None, here: bool = True) -> None:
        default = {"ref": "item-1", "hosts": (HOST,), "username": USERNAME, "password": PASSWORD, "otp": CODE, "version": 1, "title": "Fixture careers login"}
        self.rows = rows if rows is not None else [default]
        self.fail = list(fail or [])
        self.here = here
        self.calls: list[str] = []

    def available(self) -> bool:
        return self.here

    def _step(self, name: str) -> None:
        self.calls.append(name)
        if self.fail:
            raise self.fail.pop(0)

    def logins(self) -> list[SavedLogin]:
        self._step("logins")
        return [
            SavedLogin(ref=r["ref"], fingerprint=fingerprint(r["ref"], r.get("version", 1)), username=r.get("username", ""), hosts=tuple(r["hosts"]), title=r.get("title", ""))
            for r in self.rows
        ]

    def _row(self, login: SavedLogin) -> dict:
        return next(r for r in self.rows if r["ref"] == login.ref)

    def secret(self, login: SavedLogin) -> Credential:
        self._step("secret")
        row = self._row(login)
        return Credential(login=login, username=row.get("username", ""), password=row["password"], has_one_time_code=bool(row.get("otp")))

    def one_time_code(self, login: SavedLogin) -> str:
        self._step("code")
        code = self._row(login).get("otp")
        if not code:
            raise NoSavedLogin()
        return code


class Owner:
    """Plays the person at the dashboard for a held page. Nothing is read from the source until `press` is called.

    `fixes` is what the owner does after the site refused the saved login, one entry per press: `changed` (they changed
    the password, saved it, and pressed), `same` (they pressed without changing anything), `confirm` (they confirmed
    the same login may be sent once more), `wait` (they walked away)."""

    def __init__(
        self,
        source: FakeSource | None = None,
        memory: SignInMemory | None = None,
        codes: list[str] | None = None,
        allow: bool = True,
        email: str = "",
        fixes: list[str] | None = None,
    ) -> None:
        self.source = source or FakeSource()
        self.memory = memory
        self.codes = list(codes or [])
        self.allow = allow
        self.email = email
        self.fixes = list(fixes or [])
        self.presses = 0
        self.flashes: list[str] = []
        self.unchanged = False

    def answer(self, held: dict) -> tuple[dict | None, str]:
        """Returns (reply, typed answer) for what the page shows, or (None, "") to keep waiting."""
        ask = held.get("signin") or {}
        if held.get("purpose") == "signin":
            if ask.get("fix") or self.unchanged:
                if not self.fixes or self.fixes[0] == "wait":
                    return None, ""
                act = self.fixes.pop(0)
                if act == "changed":
                    self.source.rows[0].update(password=PASSWORD, version=self.source.rows[0].get("version", 1) + 1)
                return self._press(ask, confirm=act == "confirm"), ""
            if not self.allow:
                return None, ""
            return self._press(ask), ""
        if not self.codes:
            return None, ""
        nxt = self.codes.pop(0)
        if nxt == "saved":
            return self._press(ask), ""
        if nxt == "resend":
            return {"kind": "resend"}, ""
        return None, nxt

    def _press(self, ask: dict, confirm: bool = False) -> dict | None:
        self.presses += 1
        got = answer_ask(ask, self.source, self.memory, SECRET, prefer_username=self.email, sleep=lambda seconds: None, confirm_same=confirm)
        self.flashes.append(got.flash)
        self.unchanged = bool(got.unchanged)
        return got.reply


class FakeHold:
    """The hold a browser attempt waits in, without a database. It keeps only what the real record would keep."""

    def __init__(self, owner: Owner, minutes: float = 0.5) -> None:
        self.owner = owner
        self.minutes = minutes
        self.poll_seconds = 0.05
        self.asked: list[dict] = []
        self.finished: list[str] = []
        self._held: dict | None = None
        self._reply: dict | None = None
        self._answer = ""
        self._go = False

    def heartbeat(self, evidence=None) -> None:
        held = _field_hold_fields(evidence or {})
        if self._held is None:
            self.asked.append(held)
        self._held = held

    def continue_requested(self) -> bool:
        if not self._go and self._held is not None:
            reply, typed = self.owner.answer(self._held)
            if reply is not None or typed:
                self._reply, self._answer, self._go = reply, typed, True
        return self._go

    def reply(self):
        got, self._reply = self._reply, None
        return got

    def supplied_answer(self) -> str:
        return self._answer

    def forget_answer(self) -> None:
        self._answer = ""

    def finish(self, status: str) -> None:
        self.finished.append(status)
        self._held, self._go, self._reply, self._answer = None, False, None, ""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


@pytest.fixture
def site():
    with SignInSite(_free_port()) as srv:
        yield srv


def _attempt(site, sc: str, tmp_path, owner: Owner | None = None, memory: SignInMemory | None = None, on: bool = True, limits: SignInLimits = FAST, minutes: float = 0.5):
    """One browser attempt against the fixture. Returns (evidence or the handoff, notes, hold)."""
    notes: list[str] = []
    owner = owner or Owner(memory=memory)
    owner.memory = memory
    hold = FakeHold(owner, minutes=minutes)
    adapter = BrowserAdapter(BrowserConfig(allowed_hosts={HOST}, session_dir=tmp_path / "sessions", artifact_dir=tmp_path / "artifacts"))
    plan = Plan(
        start_url=f"{site.base}/acct/{sc}/job",
        steps=[Step("fill", "#full_name", "Casey Fixture"), Step("click", "#submit", final=True)],
        receipt_text="Application received",
        receipt_must_include=[f"R-{sc}"],
        job_id=f"job_{sc}",
    )
    signin = SiteSignIn(SECRET, memory, limits, job_id=plan.job_id) if on else None
    try:
        result = adapter.run_plan(plan, site_key=HOST, hold=hold, progress=notes.append, signin=signin)
    except HandoffRequired as stop:
        result = stop
    return result, notes, hold


def _no_secret(*things) -> None:
    text = "\n".join(json.dumps(thing, default=str) if not isinstance(thing, str) else thing for thing in things)
    for secret in (PASSWORD, CODE, NEW_CODE):
        assert secret not in text


# -- matching a saved login to a page ------------------------------------------------------------------------
def _login(ref: str, *hosts: str, username: str = "") -> SavedLogin:
    return SavedLogin(ref=ref, fingerprint=fingerprint(ref, 1), username=username, hosts=tuple(hosts))


def test_a_saved_login_matches_its_own_site_and_never_another_site():
    assert registrable_domain("careers.example.com") == "example.com" and registrable_domain("jobs.example.co.uk") == "example.co.uk"
    assert registrable_domain("192.168.1.9") == "192.168.1.9" and registrable_domain("localhost") == "localhost"
    assert same_site("login.example.com", "careers.example.com") and not same_site("example.com", "example.org")
    assert not same_site("a.co.uk", "b.co.uk")  # two different sites under one public suffix
    exact, parent, other = _login("a", "careers.example.com"), _login("b", "login.example.com"), _login("c", "example.org")
    assert choose_login([parent, exact, other], "careers.example.com").ref == "a"  # the exact host wins
    assert choose_login([parent, other], "careers.example.com").ref == "b"  # another host of the same site
    with pytest.raises(NoSavedLogin):
        choose_login([other], "careers.example.com")  # a different site is never used
    with pytest.raises(NoSavedLogin):
        choose_login([_login("d", "notexample.com")], "example.com")


def test_several_saved_logins_prefer_the_profile_email_and_never_guess():
    one, two = _login("a", "example.com", username="casey@example.net"), _login("b", "example.com", username="other@example.net")
    assert choose_login([one, two], "example.com", prefer_username="Casey@Example.net").ref == "a"
    with pytest.raises(SeveralSavedLogins):
        choose_login([one, two], "example.com")
    with pytest.raises(SeveralSavedLogins):
        choose_login([one, two], "example.com", prefer_username="nobody@example.net")


def test_a_passing_lookup_failure_is_tried_again_and_a_final_answer_is_not():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise LookupTransient()
        return "ok"

    assert with_retries(flaky, sleep=lambda s: None) == "ok" and calls["n"] == 2
    calls["n"] = 0

    def missing():
        calls["n"] += 1
        raise NoSavedLogin()

    with pytest.raises(NoSavedLogin):
        with_retries(missing, sleep=lambda s: None)
    assert calls["n"] == 1

    def always():
        calls["n"] += 1
        raise LookupTransient()

    calls["n"] = 0
    with pytest.raises(LookupTransient):
        with_retries(always, sleep=lambda s: None)
    assert calls["n"] == 3  # bounded


def test_the_command_line_source_reads_logins_and_reports_only_a_category():
    listing = [
        {
            "id": "item-1",
            "version": 4,
            "updated_at": "2026-01-01",
            "additional_information": USERNAME,
            "vault": {"id": "v1"},
            "urls": [{"href": "https://careers.example.com/login"}],
        }
    ]
    item = {"fields": [{"purpose": "USERNAME", "value": USERNAME}, {"purpose": "PASSWORD", "value": PASSWORD}, {"type": "OTP", "value": "otpauth://x", "totp": CODE}]}
    seen: list[list[str]] = []

    def run(command, **kwargs):
        seen.append(command)
        assert kwargs.get("stdin") is subprocess.DEVNULL  # the tool can never wait on typed input
        if command[1:3] == ["item", "list"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps(listing), stderr="")
        if "--otp" in command:
            return SimpleNamespace(returncode=0, stdout=CODE + "\n", stderr="")
        return SimpleNamespace(returncode=0, stdout=json.dumps(item), stderr="")

    source = OpCliSource(binary="python3", run=run)
    logins = source.logins()
    assert [login.hosts for login in logins] == [("careers.example.com",)] and PASSWORD not in repr(logins) and USERNAME not in repr(logins)
    found = source.secret(logins[0])
    assert found.password == PASSWORD and found.has_one_time_code and PASSWORD not in repr(found)
    assert source.one_time_code(logins[0]) == CODE
    assert len(seen) == 3

    def timed_out(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 1)

    with pytest.raises(ApprovalNotGiven):
        OpCliSource(binary="python3", run=timed_out).logins()
    for stderr, kind in (
        ("[ERROR] authorization prompt dismissed, please try again", ApprovalNotGiven),
        ("[ERROR] account is not signed in", SourceUnavailable),
        ("[ERROR] connecting to desktop app: 1Password CLI couldn't connect to the 1Password desktop app", SourceUnavailable),
    ):
        with pytest.raises(kind) as caught:
            OpCliSource(binary="python3", run=lambda command, **kw: SimpleNamespace(returncode=1, stdout="", stderr=stderr + " " + PASSWORD)).logins()  # noqa: B023
        assert PASSWORD not in str(caught.value) and "ERROR" not in str(caught.value)
    assert not OpCliSource(binary="no-such-tool-here").available()
    with pytest.raises(SourceUnavailable):
        OpCliSource(binary="no-such-tool-here").logins()


# -- the sealed handoff --------------------------------------------------------------------------------------
def test_a_sealed_login_opens_only_with_the_asking_key_and_the_same_ask():
    key, other = HandoffKey(), HandoffKey()
    box = seal(key.public, {"password": PASSWORD}, "ask_1|example.com|login")
    assert PASSWORD not in json.dumps(box)
    assert key.open(box, "ask_1|example.com|login") == {"password": PASSWORD}
    with pytest.raises(SealError):
        other.open(box, "ask_1|example.com|login")
    with pytest.raises(SealError):
        key.open(box, "ask_2|example.com|login")  # a box for one ask is no use to another
    with pytest.raises(SealError):
        key.open({**box, "ct": box["ct"][:-4] + "AAAA"}, "ask_1|example.com|login")
    sig = sign_public(SECRET, key.public, "ctx")
    assert public_is_ours(SECRET, key.public, "ctx", sig)
    assert not public_is_ours(SECRET, other.public, "ctx", sig) and not public_is_ours("z" * 40, key.public, "ctx", sig) and not public_is_ours(SECRET, key.public, "other", sig)


def _ask(step: str = "login", host: str = HOST, **extra) -> tuple[HandoffKey, dict]:
    key = HandoffKey()
    ask = {"ask_id": "ask_t", "host": host, "step": step, "key": key.public, **extra}
    ask["key_sig"] = sign_public(SECRET, key.public, ask_context(ask))
    return key, ask


def test_an_ask_is_answered_only_for_a_key_this_app_made():
    source = FakeSource()
    key, ask = _ask()
    forged = {**ask, "key": HandoffKey().public}  # someone swapped the key in the database
    got = answer_ask(forged, source, None, SECRET)
    assert got.flash == "signin_mismatch" and got.reply == {"kind": "error", "error": "mismatch"} and source.calls == []
    moved = {**ask, "host": "example.org"}  # or pointed the ask at another site
    assert answer_ask(moved, source, None, SECRET).flash == "signin_mismatch" and source.calls == []
    good = answer_ask(ask, source, None, SECRET)
    assert good.ok and good.flash == "signin_allowed" and PASSWORD not in json.dumps(good.reply)
    opened = key.open(good.reply["box"], ask_context(ask))
    assert opened["password"] == PASSWORD and opened["username"] == USERNAME and opened["has_code"] is True and opened["host"] == HOST


def test_a_missed_approval_keeps_the_ask_open_and_a_second_press_succeeds():
    source = FakeSource(fail=[ApprovalNotGiven()])
    key, ask = _ask()
    first = answer_ask(ask, source, None, SECRET, sleep=lambda s: None)
    assert first.missed and first.reply is None and first.flash == "signin_not_approved"  # nothing is sent to the waiting task
    second = answer_ask(ask, source, None, SECRET, sleep=lambda s: None)
    assert second.ok and key.open(second.reply["box"], ask_context(ask))["password"] == PASSWORD


def test_the_password_and_the_one_time_code_are_two_separate_reads():
    source = FakeSource()
    key, ask = _ask()
    assert answer_ask(ask, source, None, SECRET).ok
    assert source.calls == ["logins", "secret"]  # the code was not read with the password
    login_fingerprint = fingerprint("item-1", 1)
    code_key, code_ask = _ask("code", offer_code=True, fingerprint=login_fingerprint)
    source.fail = [ApprovalNotGiven()]  # the second approval can be missed too
    assert answer_ask(code_ask, source, None, SECRET, sleep=lambda s: None).missed
    got = answer_ask(code_ask, source, None, SECRET)
    assert got.flash == "signin_code_sent" and code_key.open(got.reply["box"], ask_context(code_ask)) == {"host": HOST, "code": CODE}
    assert source.calls[-1] == "code" and "secret" not in source.calls[2:]
    changed = {**code_ask}
    changed_key = HandoffKey()
    changed.update(key=changed_key.public, fingerprint="another-login")
    changed["key_sig"] = sign_public(SECRET, changed_key.public, ask_context(changed))
    assert answer_ask(changed, source, None, SECRET).flash == "signin_changed"


def test_an_approved_lookup_that_finds_nothing_says_which_and_is_not_retried():
    _, ask = _ask()
    none = FakeSource(rows=[])
    got = answer_ask(ask, none, None, SECRET, sleep=lambda s: None)
    assert got.flash == "signin_no_login" and got.reply == {"kind": "error", "error": "no_login"} and none.calls == ["logins"]
    cross = FakeSource(rows=[{"ref": "x", "hosts": ("example.org",), "password": PASSWORD}])
    assert answer_ask(ask, cross, None, SECRET).flash == "signin_no_login"  # a login for another site is not used
    two = FakeSource(
        rows=[{"ref": "a", "hosts": (HOST,), "password": PASSWORD, "username": "a@example.com"}, {"ref": "b", "hosts": (HOST,), "password": "p2", "username": "b@example.com"}]
    )
    assert answer_ask(ask, two, None, SECRET).flash == "signin_several" and "secret" not in two.calls
    assert answer_ask(ask, two, None, SECRET, prefer_username="b@example.com").ok  # the profile email settles it
    for error, flash in ((SourceUnavailable("not_signed_in"), "signin_reader_not_signed_in"), (SourceUnavailable("integration_off"), "signin_reader_integration_off")):
        assert answer_ask(ask, FakeSource(fail=[error]), None, SECRET).flash == flash
    away = FakeSource(here=False)
    got = answer_ask(ask, away, None, SECRET)
    assert got.flash == "signin_not_here" and got.reply is None and away.calls == []  # a dashboard without the reader leaves the ask open
    flaky = FakeSource(fail=[LookupTransient()])
    assert answer_ask(ask, flaky, None, SECRET, sleep=lambda s: None).ok and flaky.calls.count("logins") == 2


def test_every_stop_has_its_own_plain_sentence_and_none_is_hidden_from_the_timeline():
    for code, stop in {**STOPS, **WALLS}.items():
        line = stop.reason.format(site="jobs.example.com")
        assert line.endswith(".") and not _HIDDEN_WORDS.search(line), (code, line)
        assert "—" not in line + stop.title + stop.text and " - " not in line + stop.title + stop.text
        assert "Nothing was sent" in stop.text, code
    assert len({STOPS[c].reason for c in ("off", "no_login", "several", "rejected", "locked", "cycle_cap", "captcha", "passkey", "account_needed", "code_timeout")}) == 10
    assert {WALLS[w].reason for w in ("login", "code", "checkpoint", "blocked")}.__len__() == 4  # the old combined sentence is gone
    assert secure_page("https://example.com/login") and secure_page("http://127.0.0.1:8000/login") and not secure_page("http://example.com/login")


# -- the page step, against the local fixture ------------------------------------------------------------------


@pytest.mark.browser
@skip_no_browser
def test_a_sign_in_form_is_filled_from_the_saved_login_and_the_same_run_continues(site, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    owner = Owner()
    evidence, notes, hold = _attempt(site, "ok", tmp_path, owner)
    state = signin_site.STATE["ok"]
    assert not isinstance(evidence, Exception), evidence
    assert state["password_posts"] == 1 and state["passwords_ok"] == 1 and state["applied"] == 1  # signed in once, then the form was sent
    assert evidence["signin"]["outcome"] == "signed_in" and evidence["signin"]["session_saved"] is True and evidence["signin"]["codes_asked"] == 0
    assert [held["purpose"] for held in hold.asked] == ["signin"] and owner.presses == 1  # no code prompt, so nothing else was asked
    assert any("Waiting for you to allow 1Password" in n for n in notes) and any(n.startswith(f"Signed in to {HOST}") for n in notes)
    _no_secret(evidence, notes, hold.asked, caplog.text)
    assert USERNAME not in json.dumps(evidence) + "\n".join(notes)
    saved = tmp_path / "sessions" / f"{HOST}.storage.json"
    assert saved.exists() and oct(saved.stat().st_mode)[-3:] == "600"
    # The saved session is used next time: no sign-in page, no ask, no lookup.
    again_owner = Owner()
    again, _, again_hold = _attempt(site, "ok", tmp_path, again_owner)
    assert not isinstance(again, Exception) and state["password_posts"] == 1 and again_hold.asked == [] and again_owner.source.calls == []


@pytest.mark.browser
@skip_no_browser
def test_nothing_is_looked_up_until_the_owner_presses_allow(site, tmp_path):
    owner = Owner(allow=False)
    stop, notes, hold = _attempt(site, "ok", tmp_path, owner, minutes=0.03)
    assert isinstance(stop, HandoffRequired) and "1Password was not approved in time" in str(stop)
    assert owner.source.calls == [] and owner.presses == 0 and signin_site.STATE["ok"]["password_posts"] == 0
    assert stop.evidence["signin"]["outcome"] == "not_approved" and stop.evidence["signin"]["retry_safe"] is True and stop.evidence["final_clicked"] is False
    assert hold.finished == ["timeout"]


@pytest.mark.browser
@skip_no_browser
def test_a_missed_approval_is_asked_again_and_the_second_one_signs_in(site, tmp_path):
    owner = Owner(FakeSource(fail=[ApprovalNotGiven()]))
    evidence, notes, hold = _attempt(site, "ok", tmp_path, owner)
    assert not isinstance(evidence, Exception), evidence
    assert owner.presses == 2 and owner.flashes == ["signin_not_approved", "signin_allowed"]
    assert len(hold.asked) == 1 and signin_site.STATE["ok"]["password_posts"] == 1  # the same ask stayed open


@pytest.mark.browser
@skip_no_browser
@pytest.mark.parametrize("scenario", ["code", "split"])
def test_a_verification_code_is_asked_for_and_typed_into_one_box_or_split_boxes(site, tmp_path, scenario, caplog):
    caplog.set_level(logging.DEBUG)
    owner = Owner(codes=[CODE])
    evidence, notes, hold = _attempt(site, scenario, tmp_path, owner)
    state = signin_site.STATE[scenario]
    assert not isinstance(evidence, Exception), evidence
    assert state["codes"] == [CODE] and state["password_posts"] == 1 and state["applied"] == 1
    assert [held["purpose"] for held in hold.asked] == ["signin", "field"] and hold.asked[1]["field_label"] == "Verification code"
    assert evidence["signin"]["codes_asked"] == 1 and any("asked for a verification code" in n for n in notes) and "Typing the verification code." in notes
    _no_secret(evidence, notes, hold.asked, caplog.text)


@pytest.mark.browser
@skip_no_browser
def test_a_refused_code_is_asked_for_again_then_accepted(site, tmp_path):
    owner = Owner(codes=["111111", CODE])
    evidence, notes, hold = _attempt(site, "badcode", tmp_path, owner)
    assert not isinstance(evidence, Exception), evidence
    assert signin_site.STATE["badcode"]["codes"] == ["111111", CODE] and signin_site.STATE["badcode"]["password_posts"] == 1
    assert "The last code was not accepted. Asking again (2 of 3)." in notes
    assert "The last code was not accepted" in hold.asked[2]["field_reason"] and evidence["signin"]["codes_asked"] == 2


@pytest.mark.browser
@skip_no_browser
def test_codes_stop_at_the_ceiling_and_the_password_is_not_sent_again(site, tmp_path):
    owner = Owner(codes=["111111", "222222", "333333", "444444"])
    stop, notes, hold = _attempt(site, "code", tmp_path, owner)
    state = signin_site.STATE["code"]
    assert isinstance(stop, HandoffRequired) and "did not accept the verification codes" in str(stop)
    assert state["codes"] == ["111111", "222222", "333333"] and state["password_posts"] == 1 and state["applied"] == 0
    assert stop.evidence["wall"] == "code" and stop.evidence["signin"]["retry_safe"] is True


@pytest.mark.browser
@skip_no_browser
def test_the_saved_one_time_code_needs_its_own_press_and_is_said_in_the_timeline(site, tmp_path):
    owner = Owner(codes=["saved"])
    evidence, notes, hold = _attempt(site, "saved", tmp_path, owner)
    assert not isinstance(evidence, Exception), evidence
    assert owner.presses == 2 and owner.source.calls == ["logins", "secret", "logins", "code"]
    assert hold.asked[1]["signin"]["offer_code"] is True and "Typing the one-time code from 1Password." in notes
    assert evidence["signin"]["saved_code_used"] is True and signin_site.STATE["saved"]["codes"] == [CODE]
    _no_secret(evidence, notes, hold.asked)


@pytest.mark.browser
@skip_no_browser
def test_a_new_code_is_requested_only_when_the_owner_asks(site, tmp_path):
    owner = Owner(codes=["resend", NEW_CODE])
    evidence, notes, hold = _attempt(site, "resend", tmp_path, owner)
    assert not isinstance(evidence, Exception), evidence
    assert signin_site.STATE["resend"]["resends"] == 1 and signin_site.STATE["resend"]["codes"] == [NEW_CODE]
    assert hold.asked[1]["signin"]["resend"] is True and f"Asked {HOST} to send a new code, as you requested." in notes
    quiet = Owner(codes=[CODE])
    _attempt(site, "code", tmp_path, quiet)
    assert signin_site.STATE["code"]["resends"] == 0


@pytest.mark.browser
@skip_no_browser
@pytest.mark.parametrize("scenario", ["late", "hops", "userfirst"])
def test_a_slow_form_a_redirect_chain_and_a_username_first_page_all_settle(site, tmp_path, scenario):
    evidence, notes, hold = _attempt(site, scenario, tmp_path, Owner())
    state = signin_site.STATE[scenario]
    assert not isinstance(evidence, Exception), evidence
    assert state["password_posts"] == 1 and state["applied"] == 1 and evidence["signin"]["outcome"] == "signed_in"


@pytest.mark.browser
@skip_no_browser
@pytest.mark.parametrize(
    "scenario,rows,words,posts,retry_safe",
    [
        ("ok", [], "No saved login for 127.0.0.1 was found in 1Password", 0, True),
        ("ok", [{"ref": "x", "hosts": ("example.org",), "password": PASSWORD}], "No saved login for 127.0.0.1", 0, True),
        ("ok", [{"ref": "a", "hosts": (HOST,), "password": PASSWORD}, {"ref": "b", "hosts": (HOST,), "password": "p2"}], "more than one saved login for 127.0.0.1", 0, True),
        ("checkpoint", None, "asked you to verify it is you", 1, False),
        ("captcha", None, "asked for a human check on its sign-in page", 0, False),
        ("signup", None, "needs an account", 0, False),
        ("passkey", None, "only offers a passkey", 0, False),
        ("never", None, "did not finish signing in before the wait ended", 1, True),
    ],
)
def test_each_way_a_sign_in_can_stop_has_its_own_sentence(site, tmp_path, scenario, rows, words, posts, retry_safe):
    owner = Owner(FakeSource(rows=rows) if rows is not None else None)
    stop, notes, hold = _attempt(site, scenario, tmp_path, owner, limits=SignInLimits(form_wait_ms=1500, settle_ms=3000, stable_ms=600, max_seconds=60))
    state = signin_site.STATE[scenario]
    assert isinstance(stop, HandoffRequired) and words in str(stop), str(stop)
    assert state["password_posts"] == posts and state["applied"] == 0
    assert stop.evidence["signin"]["retry_safe"] is retry_safe and stop.evidence["final_clicked"] is False
    assert not (tmp_path / "sessions" / f"{HOST}.storage.json").exists()
    if scenario in ("captcha", "signup", "passkey"):
        assert owner.presses == 0 and owner.source.calls == []  # the owner is not asked when the app cannot sign in anyway
    _no_secret(stop.evidence, notes, str(stop))


@pytest.mark.browser
@skip_no_browser
def test_a_block_page_stops_and_is_never_signed_in_to(site, tmp_path):
    owner = Owner()
    stop, notes, hold = _attempt(site, "blocked", tmp_path, owner)
    assert isinstance(stop, HandoffRequired) and str(stop) == f"{HOST} blocked the app from opening the page."
    assert stop.evidence["wall"] == "blocked" and hold.asked == [] and owner.source.calls == []


@pytest.mark.browser
@skip_no_browser
def test_with_the_switch_off_a_sign_in_page_still_stops_for_the_owner(site, tmp_path):
    owner = Owner()
    stop, notes, hold = _attempt(site, "ok", tmp_path, owner, on=False)
    assert isinstance(stop, HandoffRequired) and str(stop) == f"{HOST} asked you to sign in."
    assert stop.evidence["wall"] == "login" and stop.evidence["signin"]["outcome"] == "off"
    assert hold.asked == [] and owner.source.calls == [] and signin_site.STATE["ok"]["password_posts"] == 0


def _wrong_source() -> FakeSource:
    source = FakeSource()
    source.rows[0]["password"] = "Old-Fixture-Pass-0"
    return source


@pytest.mark.browser
@pytest.mark.mongo
@skip_no_browser
def test_a_refused_password_holds_for_the_owner_with_the_sites_words_and_the_links(site, tmp_path, store, caplog):
    caplog.set_level(logging.DEBUG)
    memory = SignInMemory(store)
    owner = Owner(_wrong_source(), fixes=["wait"])
    stop, notes, hold = _attempt(site, "fixme", tmp_path, owner, memory=memory, minutes=0.05)
    state = signin_site.STATE["fixme"]
    assert isinstance(stop, HandoffRequired) and str(stop) == f"{HOST} did not accept the saved password."
    assert state["password_posts"] == 1 and state["applied"] == 0 and state["forgot_opened"] == 0  # the app never opens the reset page
    # The same attempt kept the page and asked the owner: a hold, not a dead end.
    assert [held["signin"]["fix"] for held in hold.asked] == [False, True] and owner.presses == 1
    assert f"{HOST} did not accept the saved password. Waiting for you to change it." in notes
    seen = stop.evidence["signin"]
    assert seen["outcome"] == "rejected" and seen["refused"] == 1 and stop.evidence["final_clicked"] is False
    assert seen["said"].startswith("Incorrect email or password.") and seen["item_title"] == "Fixture careers login"
    assert seen["reset_url"] == f"{site.base}/acct/fixme/forgot?tenant=fixme"  # the site's own link, with its tenant intact
    assert seen["signin_url"] == f"{site.base}/acct/fixme/job"  # the address the attempt opened, not a bare login page
    facts = memory.fix_facts(HOST)
    assert facts["status"] == "rejected" and facts["reset_url"] == seen["reset_url"] and facts["signin_url"] == seen["signin_url"] and facts["count"] == 1 and not facts["capped"]
    row = store.c("site_signins").find_one({})
    allowed = {"_id", "host", "domain", "fingerprint", "job_id", "status", "started_at", "updated_at", "finished_at", "user_id", "said", "reset_url", "signin_url", "item_title"}
    assert row["status"] == "rejected" and set(row) <= allowed
    rows = json.dumps(list(store.c("site_signins").find({})), default=str)
    for text in (json.dumps(stop.evidence, default=str), "\n".join(notes), rows, caplog.text, json.dumps(hold.asked)):
        assert "Old-Fixture-Pass-0" not in text and PASSWORD not in text and USERNAME not in text


@pytest.mark.browser
@skip_no_browser
@pytest.mark.parametrize("scenario", ["offsite", "noreset"])
def test_a_reset_link_on_another_site_is_not_offered(site, tmp_path, scenario):
    stop, notes, hold = _attempt(site, scenario, tmp_path, Owner(_wrong_source(), fixes=["wait"]), minutes=0.05)
    assert isinstance(stop, HandoffRequired) and stop.evidence["signin"]["outcome"] == "rejected"
    assert "reset_url" not in stop.evidence["signin"] and stop.evidence["signin"]["signin_url"] == f"{site.base}/acct/{scenario}/job"
    assert "example.org" not in json.dumps(stop.evidence)


@pytest.mark.browser
@pytest.mark.mongo
@skip_no_browser
def test_continue_after_the_saved_login_changed_signs_in_and_carries_on_into_the_form(site, tmp_path, store):
    memory = SignInMemory(store)
    owner = Owner(_wrong_source(), fixes=["changed"])
    evidence, notes, hold = _attempt(site, "fixme", tmp_path, owner, memory=memory)
    state = signin_site.STATE["fixme"]
    assert not isinstance(evidence, Exception), evidence
    assert state["password_posts"] == 2 and state["passwords_ok"] == 1 and state["applied"] == 1  # the same run went on and sent the form
    assert owner.presses == 2 and owner.flashes == ["signin_allowed", "signin_allowed"] and owner.source.calls.count("secret") == 2  # each try was the owner's press
    assert evidence["signin"]["outcome"] == "signed_in" and evidence["signin"]["refused"] == 1
    # A sign-in that worked clears the remembered refusal by itself.
    assert memory.fix_facts(HOST) is None and memory.blocked(HOST, fingerprint("item-1", 1)) is None and memory.rejections(HOST) == 0
    assert [row["status"] for row in store.c("site_signins").find({}).sort("started_at", 1)] == ["rejected", "signed_in"]


@pytest.mark.browser
@pytest.mark.mongo
@skip_no_browser
def test_continue_with_an_unchanged_saved_login_asks_again_and_sends_nothing_without_the_second_yes(site, tmp_path, store):
    memory = SignInMemory(store)
    owner = Owner(_wrong_source(), fixes=["same", "wait"])
    stop, notes, hold = _attempt(site, "fixme", tmp_path, owner, memory=memory, minutes=0.05)
    state = signin_site.STATE["fixme"]
    assert isinstance(stop, HandoffRequired) and stop.evidence["signin"]["outcome"] == "rejected"
    assert owner.flashes == ["signin_allowed", "signin_unchanged"] and state["password_posts"] == 1
    assert owner.source.calls == ["logins", "secret", "logins"]  # the unchanged login was not even read again
    # A later attempt is the same: the owner's Allow finds the unchanged login and nothing is typed.
    later = Owner(owner.source, fixes=["wait"])
    again, _, later_hold = _attempt(site, "fixme", tmp_path, later, memory=memory, minutes=0.05)
    assert isinstance(again, HandoffRequired) and later.flashes == ["signin_unchanged"] and state["password_posts"] == 1 and "secret" not in later.source.calls[3:]
    # Once the saved login changes, the remembered refusal no longer applies and a plain Allow signs in.
    owner.source.rows[0].update(password=PASSWORD, version=2)
    fixed = Owner(owner.source)
    done, _, _ = _attempt(site, "fixme", tmp_path, fixed, memory=memory)
    assert not isinstance(done, Exception) and fixed.flashes == ["signin_allowed"] and state["password_posts"] == 2 and state["applied"] == 1


@pytest.mark.browser
@pytest.mark.mongo
@skip_no_browser
def test_the_same_login_is_sent_again_only_on_the_second_yes_and_the_cycles_are_capped(site, tmp_path, store):
    memory = SignInMemory(store)
    source = FakeSource()
    owner = Owner(source, fixes=["same", "confirm", "same", "confirm", "confirm"])
    stop, notes, hold = _attempt(site, "wrong", tmp_path, owner, memory=memory)
    state = signin_site.STATE["wrong"]
    assert isinstance(stop, HandoffRequired) and str(stop) == f"{HOST} refused the saved login several times, so the app stopped trying for now."
    assert state["password_posts"] == 3 and stop.evidence["signin"]["refused"] == 3  # the ceiling, and each resend had its own explicit yes
    assert owner.flashes == ["signin_allowed", "signin_unchanged", "signin_allowed", "signin_unchanged", "signin_allowed"]
    facts = memory.fix_facts(HOST)
    assert facts["capped"] and facts["count"] == 3 and facts["cap_until"] > utcnow() + timedelta(hours=23) and facts["reset_url"]  # the links stay
    # No further attempt is made inside the window, with or without a yes.
    later = Owner(source, fixes=["confirm"])
    again, _, _ = _attempt(site, "wrong", tmp_path, later, memory=memory)
    assert isinstance(again, HandoffRequired) and "stopped trying for now" in str(again) and later.flashes == ["signin_cycle_cap"]
    assert state["password_posts"] == 3 and source.calls.count("secret") == 3
    # After the window the count starts again.
    assert memory.rejections(HOST, now=utcnow() + timedelta(hours=25)) == 0


@pytest.mark.browser
@pytest.mark.mongo
@skip_no_browser
@pytest.mark.parametrize("scenario,minutes,said", [("locked", 15, True), ("throttled", 30, False)])
def test_a_locked_or_throttled_account_is_left_alone_until_its_wait_has_passed(site, tmp_path, store, scenario, minutes, said):
    memory = SignInMemory(store)
    owner = Owner(fixes=["confirm"])
    stop, notes, hold = _attempt(site, scenario, tmp_path, owner, memory=memory)
    state = signin_site.STATE[scenario]
    assert isinstance(stop, HandoffRequired) and str(stop) == f"{HOST} says the account is locked or has had too many attempts."
    assert state["password_posts"] == 1 and len(hold.asked) == 1  # no hold that offers an immediate retry
    assert stop.evidence["signin"]["wait_minutes"] == (minutes if said else 0) and stop.evidence["signin"]["retry_safe"] is False
    facts = memory.fix_facts(HOST)
    assert facts["status"] == "locked" and facts["wait_said"] is said and abs((facts["wait_until"] - utcnow()).total_seconds() - minutes * 60) < 30
    assert memory.rejections(HOST) == 0  # a lock is not counted as a wrong password
    # A new attempt inside the wait reads nothing and types nothing.
    later = Owner(owner.source)
    again, _, _ = _attempt(site, scenario, tmp_path, later, memory=memory)
    assert isinstance(again, HandoffRequired) and later.flashes == ["signin_locked"] and state["password_posts"] == 1 and later.source.calls.count("secret") == 1
    app = {"status": "handoff", "handoff_kind": "login", "evidence": {"signin": {"host": HOST, "outcome": "locked"}}}
    waiting = needed_now(app, "handoff", None, can_retry=True, signin_fix=facts)
    assert waiting["kind"] == "" and "locked or has had too many attempts" in waiting["title"] and ("as the site said" in waiting["text"]) is said
    assert waiting["links"] and "Nothing was sent" in waiting["text"]
    after = memory.fix_facts(HOST, now=utcnow() + timedelta(minutes=minutes + 1))
    offered = needed_now(app, "handoff", None, can_retry=True, signin_fix=after)
    assert after["wait_until"] is None and offered["kind"] == "retry" and offered["label"] == "The account is unlocked, try again"
    assert memory.blocked(HOST, fingerprint("item-1", 1), now=utcnow() + timedelta(minutes=minutes + 1)) is None


@pytest.mark.browser
@pytest.mark.mongo
@skip_no_browser
def test_sign_ins_that_never_show_a_result_stop_being_offered(site, tmp_path, store):
    memory = SignInMemory(store)
    limits = SignInLimits(form_wait_ms=1500, settle_ms=2000, stable_ms=600, max_seconds=60)
    for _ in range(2):
        stop, _, _ = _attempt(site, "never", tmp_path, Owner(), memory=memory, limits=limits)
        assert "did not finish signing in" in str(stop)
    # The third time the owner's Allow is answered with a question instead of a sign-in.
    owner = Owner(fixes=["wait"])
    third, _, _ = _attempt(site, "never", tmp_path, owner, memory=memory, limits=limits, minutes=0.05)
    assert isinstance(third, HandoffRequired) and owner.flashes == ["signin_unchanged"] and signin_site.STATE["never"]["password_posts"] == 2
    asked = needed_now({}, "executing", {"purpose": "signin", "signin": {"host": HOST}, "signin_unchanged": "unclear_limit"}, working=True)
    assert asked["confirm_same"] and "never showed a result" in asked["text"]
    # An attempt cut short by a restart counts the same way: the row was saved before the password was sent.
    assert store.c("site_signins").count_documents({"status": "unclear"}) == 2


@pytest.mark.browser
@skip_no_browser
def test_a_stale_saved_session_signs_in_once_and_replaces_the_session(site, tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    stale = {
        "cookies": [{"name": "sid_ok", "value": "expired-session", "domain": HOST, "path": "/", "expires": -1, "httpOnly": True, "secure": False, "sameSite": "Lax"}],
        "origins": [],
    }
    (sessions / f"{HOST}.storage.json").write_text(json.dumps(stale))
    evidence, notes, hold = _attempt(site, "ok", tmp_path, Owner())
    state = signin_site.STATE["ok"]
    assert not isinstance(evidence, Exception), evidence
    assert state["password_posts"] == 1 and state["applied"] == 1
    saved = json.loads((sessions / f"{HOST}.storage.json").read_text())
    values = {cookie["value"] for cookie in saved["cookies"] if cookie["name"] == "sid_ok"}
    assert values and "expired-session" not in values and values <= state["sessions"]


# -- the dashboard side: the real hold record and the routes ---------------------------------------------------
@pytest.fixture
def web(store, tmp_path):
    settings = Settings(
        mongodb_uri=TEST_MONGO_URI,
        mongodb_database=store.db.name,
        artifact_dir=tmp_path / "a",
        private_dir=tmp_path / "p",
        log_dir=tmp_path / "l",
        app_password="correct-horse-battery",
        app_secret=SECRET,
        tracker_clone_dir=tmp_path / "clone",
        _env_file=None,
    )
    rt = Runtime(settings, store=store)
    source = FakeSource()
    rt.credential_source = lambda: source  # type: ignore[method-assign]
    client = TestClient(create_app(rt), follow_redirects=False)
    assert client.post("/login", data={"password": "correct-horse-battery"}).status_code == 303
    csrf = re.search(r'name="csrf" value="([^"]+)"', client.get("/").text).group(1)
    return SimpleNamespace(client=client, csrf=csrf, rt=rt, source=source, store=store)


def _held(store, purpose: str = "signin", **ask_extra) -> tuple[HumanHold, HandoffKey, dict]:
    job = _job(store)
    store.c("applications").insert_one({"_id": "app_s", "job_id": job["_id"], "status": "authorized", "material_hash": "hash_s", "updated_at": utcnow()})
    store.c("actions").insert_one(
        {
            "_id": "act_s",
            "kind": "application_submit",
            "target_id": job["_id"],
            "idempotency_key": f"submit:{job['_id']}",
            "status": "executing",
            "reserved_by": "w1",
            "reserved_until": utcnow() + timedelta(seconds=30),
            "material_hash": "hash_s",
        }
    )
    store.c("jobs").update_one({"_id": job["_id"]}, {"$set": {"status": "executing", "reserved_until": utcnow() + timedelta(seconds=30)}})
    hold = HumanHold(store, ActionStore(store), Settings(_env_file=None), "app_s", job["_id"], "w1")
    key, ask = _ask("login" if purpose == "signin" else "code", host="jobs.example.com", **ask_extra)
    fields = (
        {"purpose": "signin", "signin": ask}
        if purpose == "signin"
        else {"purpose": "field", "field_label": "Verification code", "field_reason": "jobs.example.com asked for a verification code.", "signin": ask}
    )
    hold.heartbeat(fields)
    return hold, key, ask


def _database_text(store) -> str:
    return "\n".join(json.dumps(doc, default=str) for name in store.db.list_collection_names() for doc in store.db[name].find({}))


@pytest.mark.mongo
def test_the_page_asks_first_and_only_the_press_reads_the_saved_login(web, caplog):
    caplog.set_level(logging.DEBUG)
    web.source.rows[0]["hosts"] = ("jobs.example.com",)
    hold, key, ask = _held(web.store)
    page = web.client.get("/applications/app_s").text
    assert "jobs.example.com asks you to sign in." in page and "Allow 1Password" in page and "Touch ID or your account password" in page
    assert "The app never sees that password" in page and 'action="/applications/app_s/signin/allow"' in page
    assert "Human check needed" not in page and web.client.get("/applications/app_s/activity").json()["hold"].startswith("signin:ask_t")
    assert web.source.calls == []  # showing the page, and polling it, reads nothing
    assert web.client.post("/applications/app_s/signin/allow", data={"material_hash": "hash_s"}).status_code == 422
    assert web.client.post("/applications/app_s/signin/allow", data={"csrf": "bogus", "material_hash": "hash_s"}).status_code == 403
    assert web.client.post("/applications/app_s/signin/allow", data={"csrf": web.csrf, "material_hash": "stale"}).status_code == 409
    assert web.source.calls == [] and not hold.continue_requested()
    done = web.client.post("/applications/app_s/signin/allow", data={"csrf": web.csrf, "material_hash": "hash_s"})
    assert done.status_code == 303 and "ok=signin_allowed" in done.headers["location"]
    assert "The saved login is being used on the sign-in page now" in web.client.get(done.headers["location"]).text
    assert web.source.calls == ["logins", "secret"] and hold.continue_requested()
    # What crossed the database is a sealed box. Only the waiting task's key opens it, and reading it removes it.
    stored = web.store.c("human_holds").find_one({"_id": hold.hold_id})
    assert stored["signin_reply"]["kind"] == "box" and set(stored["signin_reply"]["box"]) == {"epk", "nonce", "ct"}
    _no_secret(_database_text(web.store), caplog.text)
    assert USERNAME not in _database_text(web.store)
    reply = hold.reply()
    assert key.open(reply["box"], ask_context(ask))["password"] == PASSWORD
    assert "signin_reply" not in web.store.c("human_holds").find_one({"_id": hold.hold_id}) and hold.reply() is None
    # A second press on an answered ask changes nothing.
    again = web.client.post("/applications/app_s/signin/allow", data={"csrf": web.csrf, "material_hash": "hash_s"})
    assert "err=signin_missing" in again.headers["location"]
    audit = [row for row in web.store.db["audit"].find({"event": "site_signin_asked"})]
    assert audit and all(set(row["data"]) == {"host", "step", "result"} for row in audit)


@pytest.mark.mongo
def test_a_missed_prompt_keeps_the_page_waiting_with_the_same_button_until_the_ceiling(web):
    web.source.rows[0]["hosts"] = ("jobs.example.com",)
    hold, key, ask = _held(web.store)
    web.source.fail = [ApprovalNotGiven()]
    missed = web.client.post("/applications/app_s/signin/allow", data={"csrf": web.csrf, "material_hash": "hash_s"})
    assert "err=signin_not_approved" in missed.headers["location"] and not hold.continue_requested()
    page = web.client.get(missed.headers["location"]).text
    assert "1Password was not approved in time" in page and "Nothing failed" in page and "Allow 1Password" in page
    ok = web.client.post("/applications/app_s/signin/allow", data={"csrf": web.csrf, "material_hash": "hash_s"})
    assert "ok=signin_allowed" in ok.headers["location"] and key.open(hold.reply()["box"], ask_context(ask))["password"] == PASSWORD
    # Generous, and finite: after the last allowed miss the attempt is told to stop.
    web.store.c("human_holds").update_one({"_id": hold.hold_id}, {"$set": {"continue_requested": False, "signin_missed": 0}})
    web.source.fail = [ApprovalNotGiven() for _ in range(APPROVAL_ASKS)]
    flashes = [web.client.post("/applications/app_s/signin/allow", data={"csrf": web.csrf, "material_hash": "hash_s"}).headers["location"] for _ in range(APPROVAL_ASKS)]
    assert all("signin_not_approved" in f for f in flashes[:-1]) and "signin_gave_up" in flashes[-1]
    assert hold.reply() == {"kind": "error", "error": "not_approved"}


@pytest.mark.mongo
def test_an_approved_lookup_with_no_saved_login_says_so_and_a_dashboard_without_the_reader_leaves_the_ask_open(web):
    hold, key, ask = _held(web.store)  # the only saved login is for another site
    web.source.here = False
    away = web.client.post("/applications/app_s/signin/allow", data={"csrf": web.csrf, "material_hash": "hash_s"})
    assert "err=signin_not_here" in away.headers["location"] and not hold.continue_requested() and web.source.calls == []
    assert "on the computer where 1Password is installed" in web.client.get(away.headers["location"]).text
    web.source.here = True
    none = web.client.post("/applications/app_s/signin/allow", data={"csrf": web.csrf, "material_hash": "hash_s"})
    assert "err=signin_no_login" in none.headers["location"] and hold.reply() == {"kind": "error", "error": "no_login"}
    assert "it has no saved login for this site" in web.client.get(none.headers["location"]).text


@pytest.mark.mongo
def test_declining_reads_nothing_and_stops_the_attempt(web):
    hold, key, ask = _held(web.store)
    page = web.client.get("/applications/app_s").text
    assert "Do not sign in this time" in page
    done = web.client.post("/applications/app_s/signin/decline", data={"csrf": web.csrf, "material_hash": "hash_s"})
    assert "ok=signin_declined" in done.headers["location"] and hold.reply() == {"kind": "declined"} and web.source.calls == []


@pytest.mark.mongo
def test_a_code_prompt_takes_a_typed_code_or_a_separately_approved_saved_code(web):
    web.source.rows[0]["hosts"] = ("jobs.example.com",)
    hold, key, ask = _held(web.store, purpose="field", offer_code=True, resend=True, fingerprint=fingerprint("item-1", 1))
    page = web.client.get("/applications/app_s").text
    assert "Verification code" in page and 'autocomplete="one-time-code"' in page and "autofocus" in page
    assert "Use the code from 1Password" in page and "Send a new code" in page and "jobs.example.com asked for a verification code." in page
    assert web.source.calls == []
    typed = web.client.post("/applications/app_s/field-answer", data={"csrf": web.csrf, "material_hash": "hash_s", "answer": " 123 456 "})
    assert "ok=field_ok" in typed.headers["location"] and hold.supplied_answer() == "123 456"
    hold.forget_answer()
    assert "answer" not in web.store.c("human_holds").find_one({"_id": hold.hold_id})
    # The next ask in the same attempt is its own record. The saved code is read only on its own press.
    hold.finish("continued")
    key2, ask2 = _ask("code", host="jobs.example.com", offer_code=True, fingerprint=fingerprint("item-1", 1))
    ask2["ask_id"] = "ask_u"
    ask2["key_sig"] = sign_public(SECRET, key2.public, ask_context(ask2))
    hold.heartbeat({"purpose": "field", "field_label": "Verification code", "field_reason": "The last code was not accepted.", "signin": ask2})
    assert web.store.c("human_holds").count_documents({"application_id": "app_s"}) == 2
    saved = web.client.post("/applications/app_s/signin/allow", data={"csrf": web.csrf, "material_hash": "hash_s"})
    assert "ok=signin_code_sent" in saved.headers["location"] and web.source.calls == ["logins", "code"]
    assert key2.open(hold.reply()["box"], ask_context(ask2)) == {"host": "jobs.example.com", "code": CODE}
    _no_secret(_database_text(web.store))
    # A code prompt that does not offer a saved code refuses the press without reading anything.
    hold.finish("continued")
    key3, ask3 = _ask("code", host="jobs.example.com", resend=True)
    hold.heartbeat({"purpose": "field", "field_label": "Verification code", "field_reason": "x", "signin": ask3})
    calls = list(web.source.calls)
    refused = web.client.post("/applications/app_s/signin/allow", data={"csrf": web.csrf, "material_hash": "hash_s"})
    assert "err=signin_no_code" in refused.headers["location"] and web.source.calls == calls
    resend = web.client.post("/applications/app_s/signin/resend", data={"csrf": web.csrf, "material_hash": "hash_s"})
    assert "ok=signin_resend" in resend.headers["location"] and hold.reply() == {"kind": "resend"}


@pytest.mark.mongo
def test_the_switch_is_off_until_the_owner_turns_it_on(web):
    assert web.rt.site_signin_enabled() is False and web.rt.site_signin("job_x") is None
    page = web.client.get("/settings").text
    assert "Sign in with saved logins" in page and "The app never sees that password" in page and "1Password can be read from this dashboard" in page
    assert web.client.post("/settings/signin", data={"signin": "on"}).status_code == 422
    on = web.client.post("/settings/signin", data={"csrf": web.csrf, "signin": "on"})
    assert "ok=signin_on" in on.headers["location"] and web.rt.site_signin_enabled() is True and web.rt.site_signin("job_x") is not None
    off = web.client.post("/settings/signin", data={"csrf": web.csrf})
    assert "ok=signin_off" in off.headers["location"] and web.rt.site_signin_enabled() is False


def _refused(store, host: str = "jobs.example.com", item: str = "item-1", **facts) -> SignInMemory:
    memory = SignInMemory(store)
    details = {
        "said": "Incorrect email or password. <script>window.pwned = 1</script>",
        "reset_url": f"https://{host}/tenant-9/forgot?return=%2Fjobs%2F42",
        "signin_url": "https://careers.example.com/tenant-9/jobs/42/apply",
        "item_title": "Example careers <b>login</b>",
        **facts,
    }
    memory.finish(memory.begin(host, fingerprint(item, 1), "job_x"), "rejected", facts=details)
    return memory


@pytest.mark.mongo
def test_a_refused_login_is_flagged_in_the_status_block_with_escaped_words_and_checked_links(web):
    web.source.rows[0]["hosts"] = ("jobs.example.com",)
    memory = _refused(web.store)
    hold, key, ask = _held(web.store, fix=True)
    page = web.client.get("/applications/app_s").text
    assert "jobs.example.com did not accept the saved login." in page and "Nothing was sent." in page
    # The site's words and the item title are shown as text, never as markup.
    assert "Incorrect email or password. &lt;script&gt;window.pwned = 1&lt;/script&gt;" in page and "<script>window.pwned" not in page
    assert "Example careers &lt;b&gt;login&lt;/b&gt;" in page and "<b>login</b>" not in page
    reset = '<a href="https://jobs.example.com/tenant-9/forgot?return=%2Fjobs%2F42" target="_blank" rel="noopener noreferrer">Reset the password on jobs.example.com</a>'
    back = '<a href="https://careers.example.com/tenant-9/jobs/42/apply" target="_blank" rel="noopener noreferrer">Open the sign-in page for this job</a>'
    assert reset in page and back in page and "The app never changes a password." in page
    assert "Save the new password in the same 1Password item" in page and "I changed the password, try again" in page and "Not now" in page
    assert web.source.calls == []
    # Continue with a saved login that has not changed: nothing is read, and the page asks the second question.
    same = web.client.post("/applications/app_s/signin/allow", data={"csrf": web.csrf, "material_hash": "hash_s"})
    assert "err=signin_unchanged" in same.headers["location"] and web.source.calls == ["logins"] and not hold.continue_requested()
    asked = web.client.get(same.headers["location"]).text
    assert "The saved login has not changed." in asked and "already refused this password" in asked and "Send the same login once more" in asked
    assert 'name="same" value="yes"' in asked and reset in asked and "I changed the password, try again" in asked
    assert web.client.get("/applications/app_s/activity").json()["hold"].endswith(":remembered")
    # The explicit second yes sends it once. The earlier refusal still counts toward the ceiling.
    yes = web.client.post("/applications/app_s/signin/allow", data={"csrf": web.csrf, "material_hash": "hash_s", "same": "yes"})
    assert "ok=signin_allowed" in yes.headers["location"] and web.source.calls == ["logins", "logins", "secret"]
    opened = key.open(hold.reply()["box"], ask_context(ask))
    assert opened["password"] == PASSWORD and opened["title"] == "Fixture careers login" and memory.rejections("jobs.example.com") == 1
    _no_secret(_database_text(web.store))


@pytest.mark.mongo
def test_links_that_are_not_safe_to_show_are_left_out_and_the_page_says_so(web):
    memory = _refused(web.store, reset_url="https://reset.example.org/forgot", signin_url="javascript:alert(1)")
    facts = memory.fix_facts("jobs.example.com")
    assert facts["reset_url"] == "" and facts["signin_url"] == ""
    _refused(web.store, reset_url="http://jobs.example.com/forgot")  # not a secure page
    assert memory.fix_facts("jobs.example.com")["reset_url"] == ""
    hold, key, ask = _held(web.store, fix=True)
    page = web.client.get("/applications/app_s").text
    assert "The reset link could not be found on the sign-in page." in page and "example.org" not in page and "javascript:" not in page


@pytest.mark.mongo
def test_a_later_attempt_shows_the_open_refusal_and_a_changed_login_needs_no_second_question(web):
    web.source.rows[0]["hosts"] = ("jobs.example.com",)
    _refused(web.store)
    hold, key, ask = _held(web.store)  # a new attempt reached the sign-in page; this ask does not follow a refusal in this attempt
    page = web.client.get("/applications/app_s").text
    assert "jobs.example.com did not accept the saved login." in page and "Allow 1Password" in page and "Reset the password on jobs.example.com" in page
    web.source.rows[0]["version"] = 2  # the owner saved a new password in the same item
    done = web.client.post("/applications/app_s/signin/allow", data={"csrf": web.csrf, "material_hash": "hash_s"})
    assert "ok=signin_allowed" in done.headers["location"] and web.source.calls == ["logins", "secret"]


@pytest.mark.mongo
def test_the_stopped_application_keeps_continue_as_its_one_action_and_the_cap_removes_it(web):
    memory = _refused(web.store)
    app = {"status": "handoff", "handoff_kind": "login", "evidence": {"signin": {"host": "jobs.example.com", "outcome": "rejected"}}}
    one = needed_now(app, "handoff", None, can_retry=True, signin_fix=memory.fix_facts("jobs.example.com"))
    assert one["kind"] == "retry" and one["label"] == "I changed the password, try again" and "did not accept the saved login" in one["title"]
    assert "will not send that password again by itself" in one["text"] and len(one["links"]) == 2 and one["quote"].startswith("Incorrect email or password.")
    assert 'named "Example careers <b>login</b>"' in one["aside"]
    # The hold ran out on a missed approval: the refusal is still what the owner has to fix.
    missed = {**app, "evidence": {"signin": {"host": "jobs.example.com", "outcome": "not_approved"}}}
    assert needed_now(missed, "handoff", None, can_retry=True, signin_fix=memory.fix_facts("jobs.example.com"))["label"] == "I changed the password, try again"
    _refused(web.store)
    _refused(web.store)
    capped = needed_now(app, "handoff", None, can_retry=True, signin_fix=memory.fix_facts("jobs.example.com"))
    assert capped["kind"] == "" and "refused the saved login 3 times" in capped["title"] and "Another try is offered after" in capped["text"] and capped["links"]
    # A sign-in that worked ends all of it.
    memory.finish(memory.begin("jobs.example.com", "fp-new", "job_x"), "signed_in")
    assert memory.fix_facts("jobs.example.com") is None
    plain = needed_now(app, "handoff", None, can_retry=True, signin_fix=None)
    assert plain["kind"] == "retry" and plain["title"] == "Change the password on the job site, then continue."


def test_the_status_block_names_each_sign_in_stop_and_keeps_retry_where_it_is_safe():
    def need(outcome: str, wall: str = "login", can_retry: bool = True, **signin) -> dict:
        app = {"status": "handoff", "handoff_kind": wall, "evidence": {"signin": {"host": "jobs.example.com", "outcome": outcome, **signin}}}
        return needed_now(app, "handoff", None, can_retry=can_retry)

    off = need("off")
    assert off["kind"] == "retry" and "turned off under Settings" in off["text"] and "will not sign in for you" not in off["text"]
    assert need("no_login")["kind"] == "retry" and "Save a login for this site in 1Password" in need("no_login")["title"]
    assert need("several")["kind"] == "retry" and need("not_approved")["kind"] == "retry" and need("code_timeout", wall="code")["kind"] == "retry"
    assert need("rejected")["kind"] == "retry" and need("unclear_limit")["kind"] == "retry"
    waiting = needed_now({"status": "executing"}, "executing", {"purpose": "signin", "signin": {"host": "jobs.example.com"}, "signin_missed": 1}, working=True)
    assert waiting["kind"] == "signin" and "was not approved in time" in waiting["text"]
