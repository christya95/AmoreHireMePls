"""Verification mail: the consent, where the grant is kept, the narrow lookup, the wait, and the sealed answer.

The mail service is a local double behind `httpx.MockTransport` and the mailbox is a list of made-up messages.
No network, no real mailbox, no real 1Password. Every address, site, token and code is fictional."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import stat
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from typer.testing import CliRunner

from amorehiremepls.core.backup import backup
from amorehiremepls.core.credentials import ApprovalNotGiven, OpCliSource
from amorehiremepls.core.mail_access import (
    API_URL,
    APP_TAG,
    AUTH_URL,
    MAIL_TAG,
    REVOKE_URL,
    SCOPE,
    TOKEN_URL,
    FileGrantStore,
    MailAccess,
    MailError,
    MailGrant,
    MailLocked,
    MailNotConnected,
    MailReconnect,
    MailTransient,
    OpGrantStore,
    grant_store,
    read_client_file,
    run_consent,
)
from amorehiremepls.core.mail_codes import (
    MAX_MESSAGES,
    GmailReader,
    MailMessage,
    answer_mail_ask,
    check_offsets,
    find_codes,
    find_links,
    find_verification,
    next_check_at,
    wait_for_verification,
)
from amorehiremepls.core.sealed import HandoffKey, sign_public
from amorehiremepls.core.site_signin import ask_context
from amorehiremepls.web import cli
from tests.fixtures.fake_op import VAULT, FakeOp

SECRET = "s" * 40
ADDRESS = "avery.fictional@example.test"
SITE = "careers.northwindtraders.test"
CLIENT_ID, CLIENT_SECRET = "fixture-client-id.apps.example.test", "fixture-client-secret-value"
REFRESH, ACCESS = "fixture-refresh-token-1", "fixture-access-token-"
SENT = datetime(2026, 11, 13, 15, 0, 0, tzinfo=UTC)
GRANT = MailGrant(CLIENT_ID, CLIENT_SECRET, REFRESH, ADDRESS, "2026-11-13")
SECRETS = (CLIENT_SECRET, REFRESH, ACCESS)
M1, M2 = "19a0f3c2b4d5e6f7", "19a0f3c2b4d5e6f8"  # message ids the way the mail service writes them


@pytest.fixture(autouse=True)
def no_real_browser(monkeypatch):
    """No test here may open the browser of the computer it runs on."""

    def refuse(url, *args, **kwargs):
        raise AssertionError("a test tried to open a real browser")

    monkeypatch.setattr("webbrowser.open", refuse)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def gmail_message(ref: str, sender: str, at: datetime, text: str = "", rich: str = "", to: str = ADDRESS, signed: str | None = "northwindtraders.test") -> dict:
    """One message in the shape the mail service's `messages.get` returns."""
    headers = [{"name": "From", "value": f"Northwind Careers <{sender}>"}, {"name": "To", "value": to}, {"name": "Subject", "value": "A subject the app never reads"}]
    if signed:
        headers.insert(0, {"name": "Authentication-Results", "value": f"mx.google.com; dkim=pass header.i=@{signed} header.s=s1; spf=pass smtp.mailfrom={sender}"})
    parts = []
    if text:
        parts.append({"mimeType": "text/plain", "headers": [{"name": "Content-Type", "value": "text/plain; charset=UTF-8"}], "body": {"data": _b64(text)}})
    if rich:
        parts.append({"mimeType": "text/html", "headers": [{"name": "Content-Type", "value": 'text/html; charset="utf-8"'}], "body": {"data": _b64(rich)}})
    return {"id": ref, "internalDate": str(int(at.timestamp() * 1000)), "payload": {"mimeType": "multipart/alternative", "headers": headers, "parts": parts}}


class FakeGoogle:
    """The token endpoint and the three read-only mail calls. Every request is recorded."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.messages: dict[str, dict] = {}
        self.address = ADDRESS
        self.dead = False  # the grant was revoked or has expired
        self.down = 0  # how many next calls answer 503
        self.scope = SCOPE
        self.code, self.challenge = "fixture-one-time-code", ""
        self.issued = 0
        self.revoked: list[str] = []
        self.stale_tokens = 0  # how many next mail calls refuse the access token

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url.copy_with(query=None))
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.calls.append((request.method, url, form or dict(request.url.params)))
        if self.down:
            self.down -= 1
            return httpx.Response(503, json={"error": "unavailable"})
        if url == TOKEN_URL and request.method == "POST":
            if form.get("grant_type") == "authorization_code":
                proof = base64.urlsafe_b64encode(hashlib.sha256(form.get("code_verifier", "").encode()).digest()).decode().rstrip("=")
                if form.get("code") != self.code or proof != self.challenge or form.get("client_secret") != CLIENT_SECRET:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                return httpx.Response(200, json={"access_token": self._access(), "refresh_token": REFRESH, "expires_in": 3600, "scope": self.scope, "token_type": "Bearer"})
            if self.dead or form.get("refresh_token") != REFRESH:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
            return httpx.Response(200, json={"access_token": self._access(), "expires_in": 3600, "scope": SCOPE, "token_type": "Bearer"})
        if url == REVOKE_URL and request.method == "POST":
            self.revoked.append(form.get("token", ""))
            return httpx.Response(200, json={})
        if request.method != "GET" or request.headers.get("authorization") != f"Bearer {ACCESS}{self.issued}":
            return httpx.Response(401, json={"error": "unauthenticated"})
        if self.stale_tokens:
            self.stale_tokens -= 1
            return httpx.Response(401, json={"error": "unauthenticated"})
        if url == f"{API_URL}/profile":
            return httpx.Response(200, json={"emailAddress": self.address, "messagesTotal": 12345})
        if url == f"{API_URL}/messages":
            count = int(request.url.params.get("maxResults", "100"))
            return httpx.Response(200, json={"messages": [{"id": ref, "threadId": ref} for ref in list(self.messages)[:count]]})
        if url.startswith(f"{API_URL}/messages/") and url.rsplit("/", 1)[1] in self.messages:
            return httpx.Response(200, json=self.messages[url.rsplit("/", 1)[1]])
        return httpx.Response(404, json={"error": "not found"})

    def _access(self) -> str:
        self.issued += 1
        return f"{ACCESS}{self.issued}"

    def browser(self, answer: str = "allow", noise: bool = False):
        """Plays the owner's browser: reads the consent address, then comes back to the loopback address it names."""
        seen: dict = {}

        def open_browser(url: str) -> bool:
            query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
            seen.update(query, base=url.split("?")[0])
            self.challenge = query["code_challenge"]
            if answer == "none":
                return True

            def back() -> None:
                if noise:  # something else on this computer pokes the port first: wrong state, then no state
                    for bad in ("/?code=stolen&state=wrong", "/favicon.ico"):
                        try:
                            urllib.request.urlopen(query["redirect_uri"] + bad, timeout=5)
                        except urllib.error.HTTPError as exc:
                            seen.setdefault("refused", []).append(exc.code)
                tail = f"/?code={self.code}&state={query['state']}" if answer == "allow" else f"/?error=access_denied&state={query['state']}"
                seen["page"] = urllib.request.urlopen(query["redirect_uri"] + tail, timeout=5).read().decode()

            seen["thread"] = threading.Thread(target=back, daemon=True)
            seen["thread"].start()
            return True

        return open_browser, seen


class FakeMailbox:
    """A mailbox that returns whatever it holds, so every narrowing rule is proven to live in the lookup itself."""

    def __init__(self, messages: list[MailMessage] | None = None, error: Exception | None = None) -> None:
        self.messages = list(messages or [])
        self.error = error
        self.asked: list[tuple] = []

    def recent(self, sender_domains, to, since, limit):
        self.asked.append((sender_domains, to, since, limit))
        if self.error:
            raise self.error
        return list(self.messages)


def mail(
    ref: str = "m1",
    text: str = "",
    rich: str = "",
    sender: str = "no-reply@mail.northwindtraders.test",
    at: datetime | None = None,
    to: str = ADDRESS,
    signed=("northwindtraders.test",),
) -> MailMessage:
    return MailMessage(ref, sender, (to.lower(),), at or SENT + timedelta(seconds=20), tuple(signed), text, rich)


def _look(messages, want="code", **kw) -> tuple:
    box = FakeMailbox(messages)
    return find_verification(box, SITE, ADDRESS, SENT, want=want, now=kw.pop("now", SENT + timedelta(seconds=40)), **kw), box


def _access(tmp_path, google: FakeGoogle, grant: MailGrant | None = GRANT, states: list | None = None) -> MailAccess:
    store = FileGrantStore(tmp_path / "private" / "mail")
    if grant is not None:
        store.save(grant)
    return MailAccess(store, google.client(), on_state=(lambda state, address: states.append((state, address))) if states is not None else None)


def _no_secret(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text


# -- the consent ---------------------------------------------------------------------------------------------
def test_the_consent_asks_for_read_only_access_with_pkce_on_a_loopback_address(caplog):
    caplog.set_level(logging.DEBUG)
    google = FakeGoogle()
    open_browser, seen = google.browser(noise=True)
    grant = run_consent(CLIENT_ID, CLIENT_SECRET, google.client(), login_hint=ADDRESS, open_browser=open_browser, wait_seconds=10, today="2026-11-13")
    seen["thread"].join(5)
    assert grant.address == ADDRESS and grant.refresh_token == REFRESH and grant.connected_on == "2026-11-13"
    assert seen["base"] == AUTH_URL and seen["scope"] == SCOPE and seen["response_type"] == "code" and seen["code_challenge_method"] == "S256"
    assert seen["redirect_uri"].startswith("http://127.0.0.1:") and seen["login_hint"] == ADDRESS and len(seen["state"]) >= 24 and "client_secret" not in seen
    assert seen["refused"] == [404, 404] and "close this window" in seen["page"]  # an answer without the right state is not accepted
    exchange = next(form for method, url, form in google.calls if url == TOKEN_URL)
    assert exchange["grant_type"] == "authorization_code" and exchange["redirect_uri"] == seen["redirect_uri"] and 43 <= len(exchange["code_verifier"]) <= 128
    assert REFRESH not in repr(grant) and CLIENT_SECRET not in repr(grant) and ADDRESS not in repr(grant)
    _no_secret(caplog.text)
    assert google.code not in caplog.text


def test_a_consent_that_is_refused_times_out_or_lacks_the_scope_saves_nothing():
    google = FakeGoogle()
    for answer, kind in (("deny", "consent_denied"), ("none", "consent_timeout")):
        open_browser, _ = google.browser(answer)
        with pytest.raises(MailError) as caught:
            run_consent(CLIENT_ID, CLIENT_SECRET, google.client(), open_browser=open_browser, wait_seconds=1.2)
        assert caught.value.kind == kind
    google.scope = "openid"
    with pytest.raises(MailError) as caught:
        run_consent(CLIENT_ID, CLIENT_SECRET, google.client(), open_browser=google.browser()[0], wait_seconds=10)
    assert caught.value.kind == "consent_scope"
    with pytest.raises(MailError) as caught:
        run_consent(CLIENT_ID, CLIENT_SECRET, google.client(), open_browser=lambda url: False, wait_seconds=1)
    assert caught.value.kind == "no_browser"
    with pytest.raises(AssertionError):  # the guard above holds: the default opener is looked up at the call, so it can be replaced
        run_consent(CLIENT_ID, CLIENT_SECRET, google.client(), wait_seconds=1)


def test_the_client_file_is_read_without_showing_it(tmp_path):
    good = tmp_path / "client.json"
    good.write_text(json.dumps({"installed": {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "redirect_uris": ["http://localhost"]}}))
    assert read_client_file(good) == (CLIENT_ID, CLIENT_SECRET)
    for body in ('{"web": {"client_id": "x", "client_secret": "y"}}', "not json", '{"installed": {"client_id": "x"}}'):
        bad = tmp_path / "bad.json"
        bad.write_text(body)
        with pytest.raises(MailError) as caught:
            read_client_file(bad)
        assert caught.value.kind == "client_file" and str(caught.value) == "client_file"
    with pytest.raises(MailError):
        read_client_file(tmp_path / "missing.json")


# -- where the grant is kept ---------------------------------------------------------------------------------
def test_the_file_store_keeps_the_grant_in_owner_only_files(tmp_path):
    store = FileGrantStore(tmp_path / "private" / "mail")
    assert store.load() is None and not store.prompts and not store.delete()
    store.save(GRANT)
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600 and stat.S_IMODE(store.directory.stat().st_mode) == 0o700
    assert store.load() == GRANT and [p.name for p in store.directory.iterdir()] == ["grant.json"]
    store.path.chmod(0o644)
    assert store.load() == GRANT and stat.S_IMODE(store.path.stat().st_mode) == 0o600  # loosened permissions are tightened again
    store.path.write_text("{}")
    with pytest.raises(MailError):
        store.load()
    assert store.delete() and store.load() is None
    assert isinstance(grant_store(SimpleNamespace(mail_grant_store="file", private_dir=tmp_path)), FileGrantStore)
    assert isinstance(grant_store(SimpleNamespace(mail_grant_store="1password", private_dir=tmp_path, site_signin_account="", site_account_vault="")), OpGrantStore)


def test_the_1password_store_pipes_the_grant_and_touches_only_its_own_item():
    fake = FakeOp()
    fake.add_login("owner-item-1", "The owner's own mail login", ("mail.example.test",), username=ADDRESS, tags=(MAIL_TAG,))
    store = OpGrantStore(OpCliSource(binary="python3", vault=VAULT["name"], run=fake))
    assert store.prompts and store.load() is None
    store.save(GRANT)
    args, piped = next((a, p) for a, p in fake.calls if a[1:3] == ["item", "create"])
    assert args[1:4] == ["item", "create", "-"] and args[-2:] == ["--vault", VAULT["name"]] and REFRESH in piped
    _no_secret(json.dumps([a for a, _ in fake.calls]))  # nothing secret is ever an argument
    assert CLIENT_ID not in json.dumps([a for a, _ in fake.calls])
    item = next(i for i in fake.items.values() if i["category"] == "API_CREDENTIAL")
    assert set(item["tags"]) == {APP_TAG, MAIL_TAG} and item["title"] == f"{APP_TAG} mail access ({ADDRESS})"
    assert {row["id"]: row["type"] for row in item["fields"] if row["id"] in ("credential", "client_secret", "client_id")} == {
        "credential": "CONCEALED",
        "client_secret": "CONCEALED",
        "client_id": "CONCEALED",
    }
    assert store.load() == GRANT
    store.save(MailGrant(CLIENT_ID, CLIENT_SECRET, REFRESH, ADDRESS, "2026-11-20"))  # connecting again replaces the app's item
    assert sum(1 for i in fake.items.values() if i["category"] == "API_CREDENTIAL") == 1 and store.load().connected_on == "2026-11-20"
    assert store.delete() and store.load() is None and not store.delete()
    assert list(fake.items) == ["owner-item-1"]  # an item that only shares a tag was never the app's to remove


# -- using the grant -----------------------------------------------------------------------------------------
def test_a_fresh_token_is_kept_in_memory_and_only_read_calls_can_be_made(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    google, states = FakeGoogle(), []
    access = _access(tmp_path, google, states=states)
    assert access.state() == "connected" and access.ready() == ADDRESS
    assert access.get("/profile")["emailAddress"] == ADDRESS and access.get("/profile") and google.issued == 1  # one refresh, then the token is reused
    google.stale_tokens = 1
    assert access.get("/profile") and google.issued == 2  # a token that aged out early is replaced once
    for path in ("/messages/send", "/messages/abc/modify", "/labels", "/messages/abc/trash", "/../drafts", "/settings/forwardingAddresses"):
        with pytest.raises(MailError) as caught:
            access.get(path)
        assert caught.value.kind == "not_allowed"
    assert {method for method, url, _ in google.calls if url.startswith(API_URL)} == {"GET"}
    assert states == [("connected", ADDRESS)] and "MailAccess(state=connected)" == repr(access)
    _no_secret(caplog.text + repr(access) + json.dumps(states))


def test_a_revoked_or_expired_grant_becomes_connect_again_and_never_a_crash(tmp_path):
    google, states = FakeGoogle(), []
    access = _access(tmp_path, google, states=states)
    google.dead = True
    with pytest.raises(MailReconnect):
        access.get("/profile")
    assert access.state() == "needs_reconnect" and states[-1] == ("needs_reconnect", ADDRESS)
    before = len(google.calls)
    with pytest.raises(MailReconnect):
        access.token()
    assert len(google.calls) == before  # a dead grant is not tried again and again
    reader = GmailReader(access)
    assert find_verification(reader, SITE, ADDRESS, SENT, now=SENT + timedelta(seconds=30)).status == "needs_reconnect"
    # connecting again clears it
    open_browser, _ = google.browser()
    google.dead = False
    assert access.connect(CLIENT_ID, CLIENT_SECRET, open_browser=open_browser, wait_seconds=10) == ADDRESS and access.state() == "connected"
    assert access.check() == "connected"


def test_a_passing_failure_is_not_mistaken_for_a_dead_grant(tmp_path):
    google = FakeGoogle()
    access = _access(tmp_path, google)
    google.down = 1
    with pytest.raises(MailTransient):
        access.get("/profile")
    assert access.state() == "connected" and access.get("/profile")["emailAddress"] == ADDRESS
    google.down = 1
    with pytest.raises(MailTransient):
        access.get("/profile")  # the mail call itself failed, after a good token
    assert access.state() == "connected"
    assert find_verification(FakeMailbox(error=MailTransient()), SITE, ADDRESS, SENT, now=SENT + timedelta(seconds=30)).status == "failed"


def test_not_connected_and_locked_are_states_not_errors(tmp_path):
    google = FakeGoogle()
    empty = _access(tmp_path, google, grant=None)
    assert empty.state() == "not_connected" and empty.address() == ""
    with pytest.raises(MailNotConnected):
        empty.token()
    assert find_verification(GmailReader(empty), SITE, ADDRESS, SENT, now=SENT + timedelta(seconds=30)).status == "not_connected"
    # a grant kept in 1Password is read only when asked to: looking at the state never opens 1Password's window
    fake = FakeOp()
    store = OpGrantStore(OpCliSource(binary="python3", vault=VAULT["name"], run=fake))
    store.save(GRANT)
    fake.calls.clear()
    access = MailAccess(store, google.client())
    assert access.state() == "locked" and not fake.calls
    with pytest.raises(MailLocked):
        access.token()
    assert find_verification(GmailReader(access), SITE, ADDRESS, SENT, now=SENT + timedelta(seconds=30)).status == "locked" and not fake.calls
    fake.fail = [ApprovalNotGiven()]
    assert access.unlock() == "locked"  # the window was missed: ask again, nothing failed
    assert access.unlock() == "connected" and access.get("/profile")["emailAddress"] == ADDRESS
    fake.fail = ["[ERROR] account is not signed in"]
    assert access.unlock() == "unavailable"


def test_connect_saves_the_grant_and_a_grant_that_cannot_be_saved_is_withdrawn(tmp_path):
    google = FakeGoogle()
    access = _access(tmp_path, google, grant=None)
    assert access.saved_client() is None
    assert access.connect(CLIENT_ID, CLIENT_SECRET, open_browser=google.browser()[0], wait_seconds=10, today="2026-11-13") == ADDRESS
    assert access.store.load() == GRANT and access.saved_client() == (CLIENT_ID, CLIENT_SECRET)

    class Broken(FileGrantStore):
        def save(self, grant):
            raise ApprovalNotGiven()

    broken = MailAccess(Broken(tmp_path / "elsewhere"), google.client())
    with pytest.raises(ApprovalNotGiven):
        broken.connect(CLIENT_ID, CLIENT_SECRET, open_browser=google.browser()[0], wait_seconds=10)
    assert google.revoked == [REFRESH] and broken.state() == "not_connected"


def test_disconnect_withdraws_the_grant_and_deletes_it(tmp_path):
    google, states = FakeGoogle(), []
    access = _access(tmp_path, google, states=states)
    assert access.disconnect() == {"revoked": True, "removed": True}
    assert google.revoked == [REFRESH] and access.state() == "not_connected" and access.store.load() is None and states[-1] == ("not_connected", "")
    assert access.disconnect() == {"revoked": False, "removed": False}


# -- reading the mail service --------------------------------------------------------------------------------
def test_the_reader_asks_a_narrow_question_and_only_lists_and_gets(tmp_path):
    google = FakeGoogle()
    google.messages = {
        M1: gmail_message(M1, "no-reply@mail.northwindtraders.test", SENT + timedelta(seconds=25), text="Your verification code is 481516.\nIt expires in 10 minutes."),
        M2: gmail_message(M2, "news@mail.northwindtraders.test", SENT - timedelta(hours=2), text="Your code is 111222."),
    }
    access = _access(tmp_path, google)
    finding = find_verification(GmailReader(access), SITE, ADDRESS, SENT, now=SENT + timedelta(seconds=40))
    assert finding.status == "found" and finding.kind == "code" and finding.value == "481516" and finding.sender_domain == "northwindtraders.test"
    assert finding.received_at == SENT + timedelta(seconds=25) and "481516" not in repr(finding) and "481516" not in json.dumps(finding.note(), default=str)
    listing = next(params for method, url, params in google.calls if url == f"{API_URL}/messages")
    assert listing == {"q": f"from:(northwindtraders.test) to:{ADDRESS} after:{int(SENT.timestamp()) - 60}", "maxResults": str(MAX_MESSAGES)}
    mail_calls = [(method, url) for method, url, _ in google.calls if url.startswith(API_URL)]
    assert all(method == "GET" for method, _ in mail_calls) and {url.rsplit("/", 1)[0] for _, url in mail_calls} <= {API_URL, f"{API_URL}/messages"}
    # the connected mailbox must be the address the account was registered with
    assert find_verification(GmailReader(access), SITE, "someone.else@example.test", SENT, now=SENT + timedelta(seconds=40)).status == "wrong_mailbox"


def test_the_reader_reads_the_html_part_and_the_receiving_servers_signature_check(tmp_path):
    google = FakeGoogle()
    rich = "<html><head><style>.c{color:#123456}</style></head><body><p>Use this code to finish signing up:</p><table><tr><td><b>731 904</b></td></tr></table></body></html>"
    google.messages = {M1: gmail_message(M1, "no-reply@northwindtraders.test", SENT + timedelta(seconds=10), rich=rich)}
    access = _access(tmp_path, google)
    assert find_verification(GmailReader(access), SITE, ADDRESS, SENT, now=SENT + timedelta(seconds=40)).value == "731904"
    # the same message without a passed signature for the site is not used
    google.messages = {M1: gmail_message(M1, "no-reply@northwindtraders.test", SENT + timedelta(seconds=10), rich=rich, signed=None)}
    assert find_verification(GmailReader(access), SITE, ADDRESS, SENT, now=SENT + timedelta(seconds=40)).status == "none"
    google.messages = {M1: gmail_message(M1, "no-reply@northwindtraders.test", SENT + timedelta(seconds=10), rich=rich, signed="bulk-sender.test")}
    assert find_verification(GmailReader(access), SITE, ADDRESS, SENT, now=SENT + timedelta(seconds=40)).status == "none"


# -- taking one value out of a message -----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "codes"),
    [
        ("Your verification code is 481516. It expires in 10 minutes.", ["481516"]),
        ("Code: 4815", ["4815"]),
        ("Enter this code to continue:\n\n  731 904\n\nThanks", ["731904"]),
        ("731-904 is your Northwind verification code", ["731904"]),
        ("Your one-time passcode: K7M2Q9", ["K7M2Q9"]),
        ("Use the code below.\n\n48151623\n", ["48151623"]),
        ("Your code is 481516.\n\nNorthwind Traders, 500 Market Street, Springfield 94105\n(c) 2026 Northwind Traders", ["481516"]),
        ("Your verification code is 481516. Call 555 0100 if you did not ask for it. Order 99817 shipped.", ["481516"]),
        ("ZIP code 94105. Promo code 20261. Thanks for joining in 2026.", []),
        ("Welcome to Northwind Traders. Confirm your address with the button below. Reference 20261113.", []),
        ("Your code is 123456789012.", []),  # too long to be a code
        ("Your code is 481516. If that fails, your code is 990011.", ["481516", "990011"]),
        ("Your verification code is hello.", []),
    ],
)
def test_codes_are_taken_only_from_next_to_the_word(text, codes):
    assert find_codes(text) == codes


def test_links_are_taken_only_from_the_sites_own_domain():
    good = "https://account.northwindtraders.test/verify?token=abc123"
    message = mail(
        text=f"Confirm your address: {good}.\nUnsubscribe: https://northwindtraders.test/prefs",
        rich=(
            f'<a href="{good}">Verify my email</a>'
            '<a href="https://click.bulk-sender.test/track?u=https%3A%2F%2Faccount.northwindtraders.test%2Fverify">Confirm your email</a>'
            '<a href="http://account.northwindtraders.test/verify?token=plain">Verify (not secure)</a>'
            '<a href="https://account.northwindtraders.test@elsewhere.test/verify">Verify</a>'
            '<a href="https://northwindtraders.test.elsewhere.test/confirm">Confirm</a>'
            '<a href="https://northwindtraders.test/jobs">See jobs</a>'
            '<a href="javascript:alert(1)">Verify</a>'
        ),
    )
    ours, elsewhere = find_links(message, SITE)
    assert ours == [good] and elsewhere == ["click.bulk-sender.test", "northwindtraders.test.elsewhere.test"]  # an address with a name before an @ is dropped outright
    # a link shown under the words, with an address that does not say so
    named = mail(rich='<a href="https://account.northwindtraders.test/e/9f3c">Activate your account</a>')
    assert find_links(named, SITE) == (["https://account.northwindtraders.test/e/9f3c"], [])


# -- the lookup ----------------------------------------------------------------------------------------------
def test_one_code_from_the_site_in_the_window_is_found_and_nothing_else_is_kept():
    finding, box = _look([mail(text="Your verification code is 481516.")])
    assert finding.status == "found" and finding.value == "481516" and finding.kind == "code" and finding.sender_domain == "northwindtraders.test"
    assert box.asked == [(("northwindtraders.test",), ADDRESS, SENT, MAX_MESSAGES)]
    assert set(finding.note()) == {"status", "kind", "sender_domain", "received_at", "link_host", "looks"} and len(finding.seen) == 16 and finding.seen != "m1"


@pytest.mark.parametrize(
    "message",
    [
        mail(text="Your verification code is 481516.", at=SENT - timedelta(minutes=3)),  # an older mail, from before the form was sent
        mail(text="Your verification code is 481516.", at=SENT + timedelta(minutes=30)),  # outside the window
        mail(text="Your verification code is 481516.", to="someone.else@example.test"),
        mail(text="Your verification code is 481516.", sender="no-reply@northwindtraders.test.elsewhere.test", signed=("elsewhere.test",)),
        mail(text="Your verification code is 481516.", sender="no-reply@lookalike-northwindtraders.test", signed=("lookalike-northwindtraders.test",)),
        mail(text="Your verification code is 481516.", signed=()),  # the site's name on the envelope, no signature that passed
        mail(text="Your verification code is 481516.", signed=("bulk-sender.test",)),
        mail(text="Thanks for signing up. We will be in touch."),
    ],
)
def test_mail_outside_the_rules_is_not_used(message):
    assert _look([message])[0].status == "none"


def test_a_sender_the_sites_own_page_named_is_accepted_and_no_other():
    message = mail(text="Your verification code is 481516.", sender="no-reply@hiring-desk.test", signed=("hiring-desk.test",))
    assert _look([message])[0].status == "none"
    finding, box = _look([message], sender_domains=["mail.hiring-desk.test"])
    assert finding.status == "found" and finding.sender_domain == "hiring-desk.test" and box.asked[0][0] == ("northwindtraders.test", "hiring-desk.test")
    many, box = _look([message], sender_domains=["a.test", "b.test", "c.test", "hiring-desk.test", "not a domain"])
    assert many.status == "none" and box.asked[0][0] == ("northwindtraders.test", "a.test", "b.test", "c.test")  # at most three, never widened


def test_two_candidates_mean_ask_the_owner_never_a_guess():
    two = [mail("m1", text="Your verification code is 481516."), mail("m2", text="Your verification code is 990011.", at=SENT + timedelta(seconds=50))]
    finding, _ = _look(two, now=SENT + timedelta(seconds=60))
    assert finding.status == "ambiguous" and finding.value == "" and finding.sender_domain == ""
    assert _look([mail(text="Your code is 481516. If that fails, your code is 990011.")])[0].status == "ambiguous"
    # the same code in two messages is one candidate
    same = [mail("m1", text="Your verification code is 481516."), mail("m2", text="Reminder: your code is 481516", at=SENT + timedelta(seconds=50))]
    assert _look(same, now=SENT + timedelta(seconds=60))[0].value == "481516"
    # a message already used (its code was refused) is skipped, so the new one is clear
    first = _look(two[:1])[0]
    assert _look(two, now=SENT + timedelta(seconds=60), skip=[first.seen])[0].value == "990011"


def test_a_link_is_returned_only_when_it_is_on_the_site_and_another_host_is_only_named():
    good = "https://account.northwindtraders.test/confirm/9f3c"
    finding, _ = _look([mail(rich=f'<p>Welcome.</p><a href="{good}">Confirm your email</a>')], want="link")
    assert finding.status == "found" and finding.kind == "link" and finding.value == good and finding.link_host == "account.northwindtraders.test" and good not in repr(finding)
    tracked, _ = _look([mail(rich='<a href="https://click.bulk-sender.test/t/abc">Confirm your email</a>')], want="link")
    assert tracked.status == "elsewhere" and tracked.link_host == "click.bulk-sender.test" and tracked.value == ""
    both, _ = _look([mail(rich=f'<a href="{good}">Confirm</a><a href="https://account.northwindtraders.test/confirm/other">Confirm</a>')], want="link")
    assert both.status == "ambiguous" and both.value == ""
    assert _look([mail(text="Your verification code is 481516.")], want="link")[0].status == "none"  # a code is not offered when a link was asked for


def test_an_old_sign_up_a_bad_address_or_a_bad_request_is_never_looked_up():
    box = FakeMailbox([mail(text="Your verification code is 481516.")])
    assert find_verification(box, SITE, ADDRESS, SENT, now=SENT + timedelta(hours=2)).status == "stale"
    assert find_verification(box, SITE, ADDRESS, SENT + timedelta(minutes=5), now=SENT).status == "stale"
    assert find_verification(box, SITE, "not an address", SENT, now=SENT + timedelta(seconds=30)).status == "failed"
    assert find_verification(box, SITE, ADDRESS, SENT.replace(tzinfo=None), now=SENT + timedelta(seconds=30)).status == "failed"
    assert find_verification(box, "", ADDRESS, SENT, now=SENT + timedelta(seconds=30)).status == "failed"
    assert find_verification(box, SITE, ADDRESS, SENT, want="anything", now=SENT + timedelta(seconds=30)).status == "failed"
    assert not box.asked


# -- waiting -------------------------------------------------------------------------------------------------
class Clock:
    def __init__(self, start: datetime) -> None:
        self.at = start
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.at

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.at += timedelta(seconds=seconds)


def test_looks_are_soon_and_often_at_first_then_steady_and_never_past_the_wait():
    assert check_offsets(300) == [5, 10, 20, 30, 60, 90, 120, 150, 180, 210, 240, 270, 300]
    assert check_offsets(45) == [5, 10, 20, 30] and check_offsets(0) == [] and check_offsets(12) == [5, 10]
    assert next_check_at(SENT, 0, 300) == SENT + timedelta(seconds=5) and next_check_at(SENT, 4, 300) == SENT + timedelta(seconds=60)
    assert next_check_at(SENT, 13, 300) is None and next_check_at(SENT, -1, 300) is None


def test_a_mail_that_has_not_arrived_yet_is_waited_for_with_the_same_narrow_lookup():
    clock = Clock(SENT + timedelta(seconds=2))
    box = FakeMailbox()

    class Arrives(FakeMailbox):
        def recent(self, *args):
            box.asked.append(args)
            return [mail(text="Your verification code is 481516.", at=SENT + timedelta(seconds=15))] if len(box.asked) >= 3 else []

    finding = wait_for_verification(Arrives(), SITE, ADDRESS, SENT, wait_seconds=300, sleep=clock.sleep, now=clock.now)
    assert finding.status == "found" and finding.value == "481516" and finding.looks == 3 and clock.sleeps == [5, 5, 10]
    assert len(set(box.asked)) == 1  # every look asked the same question: no retry reads wider


def test_the_wait_ends_as_a_timeout_and_stops_at_once_when_looking_again_cannot_help():
    clock = Clock(SENT)
    box = FakeMailbox()
    gone = wait_for_verification(box, SITE, ADDRESS, SENT, wait_seconds=90, sleep=clock.sleep, now=clock.now)
    assert gone.status == "timeout" and gone.looks == 6 and len(box.asked) == 6 and sum(clock.sleeps) == 90
    for error, status in ((MailReconnect(), "needs_reconnect"), (MailNotConnected(), "not_connected"), (MailLocked(), "locked"), (MailError("wrong_mailbox"), "wrong_mailbox")):
        clock, box = Clock(SENT), FakeMailbox(error=error)
        assert wait_for_verification(box, SITE, ADDRESS, SENT, wait_seconds=300, sleep=clock.sleep, now=clock.now).status == status and len(box.asked) == 1
    clock, box = Clock(SENT), FakeMailbox([mail("m1", text="Your code is 481516."), mail("m2", text="Your code is 990011.")])
    assert wait_for_verification(box, SITE, ADDRESS, SENT, wait_seconds=300, sleep=clock.sleep, now=clock.now).status == "ambiguous" and len(box.asked) == 1
    clock, box = Clock(SENT), FakeMailbox(error=MailTransient())  # a passing failure is looked for again, inside the same wait
    assert wait_for_verification(box, SITE, ADDRESS, SENT, wait_seconds=30, sleep=clock.sleep, now=clock.now).status == "timeout" and len(box.asked) == 4


# -- the sealed answer ---------------------------------------------------------------------------------------
def _ask(step: str = "mail_code", **extra) -> tuple[HandoffKey, dict]:
    key = HandoffKey()
    ask = {"ask_id": "ask_fixture_9", "host": SITE, "step": step, "key": key.public, "sent_at": SENT.isoformat(), **extra}
    ask["key_sig"] = sign_public(SECRET, key.public, ask_context(ask))
    return key, ask


def test_the_code_reaches_the_asking_task_sealed_and_nowhere_else(caplog):
    caplog.set_level(logging.DEBUG)
    key, ask = _ask()
    answer = answer_mail_ask(ask, FakeMailbox([mail(text="Your verification code is 481516.")]), SECRET, ADDRESS, now=SENT + timedelta(seconds=40))
    assert answer.ok and answer.flash == "mail_code_found" and answer.reply["kind"] == "box"
    assert "481516" not in json.dumps(answer.reply) and "481516" not in repr(answer) and "481516" not in caplog.text
    assert key.open(answer.reply["box"], ask_context(ask)) == {"host": SITE, "code": "481516"}
    assert answer.finding.note()["sender_domain"] == "northwindtraders.test"
    link = "https://account.northwindtraders.test/verify/9f3c"
    key, ask = _ask("mail_link")
    found = answer_mail_ask(ask, FakeMailbox([mail(rich=f'<a href="{link}">Verify</a>')]), SECRET, ADDRESS, now=SENT + timedelta(seconds=40))
    assert found.flash == "mail_link_found" and key.open(found.reply["box"], ask_context(ask)) == {"host": SITE, "link": link}


def test_anything_but_one_clear_value_leaves_the_ask_open_for_the_owner():
    key, ask = _ask()
    for messages, flash in (([], "mail_none"), ([mail("m1", text="Your code is 481516."), mail("m2", text="Your code is 990011.")], "mail_ambiguous")):
        answer = answer_mail_ask(ask, FakeMailbox(messages), SECRET, ADDRESS, now=SENT + timedelta(seconds=40))
        assert not answer.ok and answer.reply is None and answer.flash == flash
    assert answer_mail_ask(ask, FakeMailbox(error=MailReconnect()), SECRET, ADDRESS, now=SENT + timedelta(seconds=40)).flash == "mail_needs_reconnect"
    # the sender the page named travels with the ask
    named = mail(text="Your verification code is 481516.", sender="no-reply@hiring-desk.test", signed=("hiring-desk.test",))
    assert answer_mail_ask(_ask(senders=["hiring-desk.test"])[1], FakeMailbox([named]), SECRET, ADDRESS, now=SENT + timedelta(seconds=40)).ok
    # an ask this app did not sign, an unknown step, or a missing send time is not answered
    box = FakeMailbox([mail(text="Your verification code is 481516.")])
    for bad in (dict(ask, key_sig="0" * 64), _ask("code")[1], dict(_ask()[1], sent_at="yesterday")):
        refused = answer_mail_ask(bad, box, SECRET, ADDRESS, now=SENT + timedelta(seconds=40))
        assert refused.reply == {"kind": "error", "error": "mismatch"} and not refused.ok
    assert answer_mail_ask(ask, box, "t" * 40, ADDRESS, now=SENT + timedelta(seconds=40)).flash == "mail_mismatch" and not box.asked


# -- the command line ----------------------------------------------------------------------------------------
def _cli(tmp_path, monkeypatch, google: FakeGoogle, grant: MailGrant | None = GRANT, email: str = ADDRESS):
    access = _access(tmp_path, google, grant=grant)
    rt = SimpleNamespace(settings=SimpleNamespace(mail_grant_store="file"), mail_access=lambda: access, account_email=lambda: email, mail_verification_enabled=lambda: False)
    monkeypatch.setattr(cli, "_rt", lambda settings=None: rt)
    return CliRunner(), access


def test_status_says_connected_or_not_and_which_address_and_never_a_token(tmp_path, monkeypatch):
    google = FakeGoogle()
    runner, access = _cli(tmp_path, monkeypatch, google)
    shown = runner.invoke(cli.app, ["mail", "status"])
    out = json.loads(shown.output)
    assert shown.exit_code == 0 and out == {
        "state": "connected",
        "meaning": "Mail access is connected.",
        "address": ADDRESS,
        "kept_in": "file",
        "reading_is_on": False,
        "is_account_email": True,
    }
    _no_secret(shown.output)
    google.dead = True
    again = json.loads(runner.invoke(cli.app, ["mail", "status"]).output)
    assert again["state"] == "needs_reconnect" and again["meaning"] == "Mail access needs to be connected again."
    google.dead, google.down = False, 5
    away = json.loads(runner.invoke(cli.app, ["mail", "status"]).output)
    assert away["state"] == "connected" and "could not be reached" in away["note"]
    runner, _ = _cli(tmp_path / "other", monkeypatch, FakeGoogle(), grant=None)
    none = json.loads(runner.invoke(cli.app, ["mail", "status"]).output)
    assert none["state"] == "not_connected" and none["address"] == "" and "is_account_email" not in none


def test_connect_and_disconnect_from_the_command_line(tmp_path, monkeypatch):
    google = FakeGoogle()
    runner, access = _cli(tmp_path, monkeypatch, google, grant=None, email="other.address@example.test")
    assert runner.invoke(cli.app, ["mail", "connect"]).exit_code == 1  # nothing saved yet and no client file
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    refused = runner.invoke(cli.app, ["mail", "connect", "--client-file", str(bad)])
    assert refused.exit_code == 1 and "client file" in refused.output and access.store.load() is None
    good = tmp_path / "client.json"
    good.write_text(json.dumps({"installed": {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}}))
    monkeypatch.setattr("amorehiremepls.core.mail_access.webbrowser.open", google.browser()[0])
    done = runner.invoke(cli.app, ["mail", "connect", "--client-file", str(good)])
    out = json.loads(done.output)
    assert done.exit_code == 0 and out["state"] == "connected" and out["address"] == ADDRESS and out["is_account_email"] is False and out["kept_in"] == "file"
    _no_secret(done.output)
    assert CLIENT_ID not in done.output and access.store.load().refresh_token == REFRESH
    # connecting again needs no client file: the saved client is used
    monkeypatch.setattr("amorehiremepls.core.mail_access.webbrowser.open", google.browser()[0])
    assert runner.invoke(cli.app, ["mail", "connect"]).exit_code == 0
    gone = runner.invoke(cli.app, ["mail", "disconnect"])
    assert json.loads(gone.output) == {"state": "not_connected", "meaning": "Mail access is not connected.", "revoked": True, "removed": True}
    assert access.store.load() is None and google.revoked == [REFRESH]


# -- backups -------------------------------------------------------------------------------------------------
@pytest.mark.mongo
def test_the_mail_grant_is_left_out_of_backups(store, tmp_path):
    import tarfile

    private = tmp_path / "private"
    FileGrantStore(private / "mail").save(GRANT)
    (private / "sessions").mkdir()
    (private / "sessions" / "site.storage.json").write_text("{}")
    manifest = backup(store, tmp_path / "artifacts", private, tmp_path / "backup.tgz")
    with tarfile.open(tmp_path / "backup.tgz") as tar:
        names = tar.getnames()
        assert "private/sessions/site.storage.json" in names and not any("mail" in name for name in names)
        for member in tar.getmembers():
            if member.isfile():
                _no_secret(tar.extractfile(member).read().decode(errors="ignore"))
    assert manifest["private"] == 1
