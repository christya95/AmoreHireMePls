"""Read-only access to the owner's mailbox: the one-time consent, where the grant is kept, and a fresh access token.

Gmail API, the read-only scope and nothing else. The consent is Google's flow for an installed desktop app: the
system browser, a loopback redirect on this computer, and PKCE. It runs where the owner sits (`amore mail connect`),
and the grant never leaves that computer: another computer's task asks this one for a code, as it asks for a saved
login.

Where the grant is kept (`AMORE_MAIL_GRANT_STORE`):
  1password  one item in the vault for new logins. Reading it shows 1Password's own window, so it is read only at the
             owner's press or from the command line, and then held in this process's memory.
  file       owner-only files under the private folder's `mail/`, which backups leave out.

A grant Google no longer honours (`invalid_grant`: it expired, was revoked, or the account password changed) is not
an error to crash on. The state becomes `needs_reconnect` and the caller asks the owner for the code instead. Nothing
here logs, prints or returns a token, the client secret, or anything read from the mailbox.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from amorehiremepls.core.credentials import ApprovalNotGiven, LoginLookupError, OpCliSource

SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API_URL = "https://gmail.googleapis.com/gmail/v1/users/me"
_READ_PATH = re.compile(r"^/(profile|messages(/[0-9a-f]{8,40})?)$")  # the only calls this module can make; a message is named by its hex id
_DEAD_GRANT = ("invalid_grant", "invalid_client", "unauthorized_client", "deleted_client")
APP_TAG = "AmoreHireMePls"
MAIL_TAG = "mail-access"
CONNECT_WAIT_SECONDS = 300

STATES = {
    "connected": "Mail access is connected.",
    "not_connected": "Mail access is not connected.",
    "needs_reconnect": "Mail access needs to be connected again.",
    "locked": "Mail access is saved in 1Password and has not been read yet.",
    "unavailable": "The saved mail access could not be read here.",
}


class MailError(Exception):
    """Mail could not be read. `kind` is a short category; the message never carries a token or any mail content."""

    kind = "failed"

    def __init__(self, kind: str | None = None) -> None:
        self.kind = kind or self.kind
        super().__init__(self.kind)


class MailNotConnected(MailError):
    kind = "not_connected"


class MailReconnect(MailError):
    kind = "needs_reconnect"


class MailLocked(MailError):
    kind = "locked"


class MailTransient(MailError):
    kind = "transient"


@dataclass(frozen=True)
class MailGrant:
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    refresh_token: str = field(repr=False)
    address: str = field(repr=False, default="")
    connected_on: str = ""


def read_client_file(path: Path) -> tuple[str, str]:
    """The client id and secret from Google's file for a Desktop app client. Its contents are never shown."""
    try:
        data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise MailError("client_file") from None
    client = data.get("installed") if isinstance(data, dict) else None
    if not isinstance(client, dict) or not client.get("client_id") or not client.get("client_secret"):
        raise MailError("client_file")
    return str(client["client_id"]), str(client["client_secret"])


# -- where the grant is kept ---------------------------------------------------------------------------------
class GrantStore(Protocol):
    prompts: bool  # reading shows the password manager's own window, so it happens only at the owner's press

    def load(self) -> MailGrant | None: ...

    def save(self, grant: MailGrant) -> None: ...

    def delete(self) -> bool: ...


def _grant_from(data: Any) -> MailGrant:
    if not isinstance(data, dict) or not all(data.get(name) for name in ("client_id", "client_secret", "refresh_token")):
        raise MailError("grant_unreadable")
    return MailGrant(str(data["client_id"]), str(data["client_secret"]), str(data["refresh_token"]), str(data.get("address") or ""), str(data.get("connected_on") or ""))


class FileGrantStore:
    """One owner-only file in an owner-only folder under the private data root. Backups leave the folder out."""

    prompts = False

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.path = self.directory / "grant.json"

    def load(self) -> MailGrant | None:
        if not self.path.exists():
            return None
        try:
            os.chmod(self.directory, 0o700)
            os.chmod(self.path, 0o600)
            return _grant_from(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            raise MailError("grant_unreadable") from None

    def save(self, grant: MailGrant) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        body = {
            "client_id": grant.client_id,
            "client_secret": grant.client_secret,
            "refresh_token": grant.refresh_token,
            "address": grant.address,
            "connected_on": grant.connected_on,
        }
        draft = self.directory / f".grant.{secrets.token_hex(4)}.tmp"
        handle = os.open(draft, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                json.dump(body, out)
            os.replace(draft, self.path)
        finally:
            draft.unlink(missing_ok=True)

    def delete(self) -> bool:
        found = self.path.exists()
        self.path.unlink(missing_ok=True)
        return found


class OpGrantStore:
    """One item in 1Password, written and read through the same tool the sign-in lookup uses.

    The item is an API credential tagged as this app's mail access. Its values go to the tool as a template on standard
    input, never as arguments. `op` should be an `OpCliSource` whose vault is the one the app's own items go to.
    """

    prompts = True

    def __init__(self, op: OpCliSource) -> None:
        self.op = op

    def _rows(self) -> list[dict]:
        try:
            rows = json.loads(self.op.call(["item", "list", "--tags", MAIL_TAG, "--format", "json"]) or "[]")
        except ValueError:
            raise MailError("grant_unreadable") from None
        mine = [row for row in rows if isinstance(row, dict) and row.get("id") and {APP_TAG, MAIL_TAG} <= set(row.get("tags") or [])]
        return sorted(mine, key=lambda row: str(row.get("updated_at") or ""), reverse=True)

    def _vault(self, row: dict) -> str:
        return str((row.get("vault") or {}).get("id") or "")

    def load(self) -> MailGrant | None:
        rows = self._rows()
        if not rows:
            return None
        try:
            item = json.loads(self.op.call(["item", "get", str(rows[0]["id"]), "--format", "json"], vault=self._vault(rows[0])) or "{}")
        except ValueError:
            raise MailError("grant_unreadable") from None
        values = {str(row.get("id") or ""): str(row.get("value") or "") for row in item.get("fields") or [] if isinstance(row, dict)}
        return _grant_from(
            {
                "client_id": values.get("client_id"),
                "client_secret": values.get("client_secret"),
                "refresh_token": values.get("credential"),
                "address": values.get("username"),
                "connected_on": values.get("connected_on"),
            }
        )

    def save(self, grant: MailGrant) -> None:
        """Save the new grant, then remove the app's earlier mail access items. Only items with both tags are removed."""
        earlier = self._rows()
        section = {"id": "client", "label": "Client"}
        template = {
            "title": f"{APP_TAG} mail access ({grant.address})" if grant.address else f"{APP_TAG} mail access",
            "category": "API_CREDENTIAL",
            "tags": [APP_TAG, MAIL_TAG],
            "sections": [section],
            "fields": [
                {"id": "username", "type": "STRING", "label": "username", "value": grant.address},
                {"id": "credential", "type": "CONCEALED", "label": "credential", "value": grant.refresh_token},
                {
                    "id": "notesPlain",
                    "type": "STRING",
                    "purpose": "NOTES",
                    "label": "notesPlain",
                    "value": f"Read-only mail access for {APP_TAG}. Remove it with the app's mail disconnect command.",
                },
                {"id": "client_id", "section": {"id": "client"}, "type": "CONCEALED", "label": "Client id", "value": grant.client_id},
                {"id": "client_secret", "section": {"id": "client"}, "type": "CONCEALED", "label": "Client secret", "value": grant.client_secret},
                {"id": "connected_on", "section": {"id": "client"}, "type": "STRING", "label": "Connected on", "value": grant.connected_on},
            ],
        }
        self.op.call(["item", "create", "-", "--format", "json"], stdin_text=json.dumps(template))
        for row in earlier:
            self.op.call(["item", "delete", str(row["id"])], vault=self._vault(row))

    def delete(self) -> bool:
        rows = self._rows()
        for row in rows:
            self.op.call(["item", "delete", str(row["id"])], vault=self._vault(row))
        return bool(rows)


def grant_store(settings: Any) -> GrantStore:
    if settings.mail_grant_store == "file":
        return FileGrantStore(Path(settings.private_dir) / "mail")
    return OpGrantStore(OpCliSource(account=settings.site_signin_account, vault=settings.site_account_vault))


# -- the consent ---------------------------------------------------------------------------------------------
def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode().rstrip("=")


_DONE_PAGE = "<!doctype html><meta charset=utf-8><title>Mail access</title><p>{}</p>"


def _wait_for_redirect(server: HTTPServer, state: str, seconds: float) -> str:
    """Serve this computer's loopback address until Google's redirect arrives. Returns the one-time code."""
    got: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # the request line holds the one-time code: never logged
            return

        def do_GET(self) -> None:  # noqa: N802
            query = parse_qs(urlsplit(self.path).query)
            code, error, back = (query.get(name, [""])[0] for name in ("code", "error", "state"))
            if urlsplit(self.path).path != "/" or not (code or error) or not hmac.compare_digest(back, state):
                self.send_error(404)
                return
            got.update(code=code, error=error)
            body = _DONE_PAGE.format(
                "Mail access was not granted. You can close this window." if error else "Done. You can close this window and go back to the terminal."
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server.RequestHandlerClass = Handler
    server.timeout = 0.5
    deadline = time.monotonic() + seconds
    while not got and time.monotonic() < deadline:
        server.handle_request()
    if not got:
        raise MailError("consent_timeout")
    if got.get("error") or not got.get("code"):
        raise MailError("consent_denied")
    return got["code"]


def run_consent(
    client_id: str,
    client_secret: str,
    http: httpx.Client,
    *,
    login_hint: str = "",
    open_browser: Callable[[str], Any] | None = None,
    wait_seconds: float = CONNECT_WAIT_SECONDS,
    today: str = "",
) -> MailGrant:
    """Ask the owner for read-only mail access in their browser and return the grant. Nothing is saved here.

    The browser comes back to a loopback address on this computer that only this call is listening on. The request
    carries a PKCE challenge and a state value; an answer without that state is ignored.
    """
    verifier, state = secrets.token_urlsafe(64), secrets.token_urlsafe(24)
    server = HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    try:
        redirect = f"http://127.0.0.1:{server.server_address[1]}"
        query = {
            "client_id": client_id,
            "redirect_uri": redirect,
            "response_type": "code",
            "scope": SCOPE,
            "code_challenge": _challenge(verifier),
            "code_challenge_method": "S256",
            "state": state,
        }
        if login_hint:
            query["login_hint"] = login_hint
        if not (open_browser or webbrowser.open)(f"{AUTH_URL}?{urlencode(query)}"):
            raise MailError("no_browser")
        code = _wait_for_redirect(server, state, wait_seconds)
    finally:
        server.server_close()
    form = {"client_id": client_id, "client_secret": client_secret, "code": code, "code_verifier": verifier, "grant_type": "authorization_code", "redirect_uri": redirect}
    try:
        answer = http.post(TOKEN_URL, data=form)
        tokens = answer.json() if answer.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        raise MailTransient() from None
    if not isinstance(tokens, dict) or not tokens.get("access_token") or not tokens.get("refresh_token"):
        raise MailError("consent_exchange")
    if SCOPE not in str(tokens.get("scope") or "").split():
        raise MailError("consent_scope")  # the owner left the box unticked: there is nothing to read with
    try:
        profile = http.get(f"{API_URL}/profile", headers={"Authorization": f"Bearer {tokens['access_token']}"})
        address = str(profile.json().get("emailAddress") or "") if profile.status_code == 200 else ""
    except (httpx.HTTPError, ValueError):
        raise MailTransient() from None
    if not address:
        raise MailError("consent_profile")
    return MailGrant(client_id, client_secret, str(tokens["refresh_token"]), address, today)


# -- using the grant -----------------------------------------------------------------------------------------
class MailAccess:
    """The grant in use by one process: its state, the mailbox address, and read-only calls with a fresh token.

    `on_state(state, address)` is told every change, so a page can say connected or not without touching the grant.
    """

    def __init__(self, store: GrantStore, http: httpx.Client | None = None, on_state: Callable[[str, str], None] | None = None, clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self._http = http
        self._on_state = on_state
        self._clock = clock
        self._grant: MailGrant | None = None
        self._token = ""
        self._expires = 0.0
        self._state = ""

    def __repr__(self) -> str:
        return f"MailAccess(state={self._state or 'unknown'})"

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=15.0)
        return self._http

    def _set(self, state: str) -> str:
        if state != self._state:
            self._state = state
            if self._on_state is not None:
                try:
                    self._on_state(state, self._grant.address if self._grant else "")
                except Exception:
                    pass
        return state

    def unlock(self) -> str:
        """Read the grant from where it is kept. With the 1Password store this shows 1Password's own window, so it is
        called only from the owner's press or the command line. Returns the state."""
        self._token, self._expires = "", 0.0
        try:
            self._grant = self.store.load()
        except ApprovalNotGiven:
            self._grant = None
            return self._set("locked")
        except (LoginLookupError, MailError):
            self._grant = None
            return self._set("unavailable")
        return self._set("connected" if self._grant else "not_connected")

    def state(self) -> str:
        """`connected`, `not_connected`, `needs_reconnect`, `locked` or `unavailable`. Never shows a window."""
        if self._grant is not None or self._state == "needs_reconnect":
            return self._state
        if self.store.prompts:
            return self._state or "locked"
        return self.unlock()

    def address(self) -> str:
        return self._grant.address if self._grant else ""

    def ready(self) -> str:
        """The connected mailbox's address, or the error that says why mail cannot be read right now."""
        return self._need().address

    def _need(self) -> MailGrant:
        state = self.state()
        if state == "connected" and self._grant is not None:
            return self._grant
        raise {"needs_reconnect": MailReconnect, "locked": MailLocked, "not_connected": MailNotConnected}.get(state, MailError)()

    def _dead(self) -> MailReconnect:
        self._token, self._expires = "", 0.0
        self._set("needs_reconnect")
        return MailReconnect()

    def token(self) -> str:
        """A fresh access token, kept in memory. A grant Google refuses becomes `needs_reconnect`."""
        grant = self._need()
        if self._token and self._clock() < self._expires - 60:
            return self._token
        form = {"client_id": grant.client_id, "client_secret": grant.client_secret, "grant_type": "refresh_token", "refresh_token": grant.refresh_token}
        try:
            answer = self.http.post(TOKEN_URL, data=form)
            body = answer.json() if answer.content else {}
        except (httpx.HTTPError, ValueError):
            raise MailTransient() from None
        body = body if isinstance(body, dict) else {}
        if answer.status_code == 200 and body.get("access_token"):
            self._token, self._expires = str(body["access_token"]), self._clock() + float(body.get("expires_in") or 0)
            return self._token
        if answer.status_code in (400, 401) and body.get("error") in _DEAD_GRANT:
            raise self._dead()
        if answer.status_code == 429 or answer.status_code >= 500:
            raise MailTransient()
        raise MailError("token")

    def get(self, path: str, params: dict | None = None) -> dict:
        """One read-only call. Only the profile, the message list and one message can be asked for."""
        if not _READ_PATH.match(path):
            raise MailError("not_allowed")
        for attempt in (1, 2):
            token = self.token()
            try:
                answer = self.http.get(f"{API_URL}{path}", params=params or {}, headers={"Authorization": f"Bearer {token}"})
            except httpx.HTTPError:
                raise MailTransient() from None
            if answer.status_code == 401:
                self._token, self._expires = "", 0.0  # the token aged out early: one fresh token, then give up
                if attempt == 1:
                    continue
                raise self._dead()
            if answer.status_code == 429 or answer.status_code >= 500:
                raise MailTransient()
            if answer.status_code != 200:
                raise MailError("forbidden" if answer.status_code == 403 else "failed")
            try:
                body = answer.json()
            except ValueError:
                raise MailTransient() from None
            return body if isinstance(body, dict) else {}
        raise MailError("failed")

    def saved_client(self) -> tuple[str, str] | None:
        """The client of the last connection, to connect again without the client file. May show 1Password's window."""
        self.unlock()
        return (self._grant.client_id, self._grant.client_secret) if self._grant else None

    def _revoke(self, token: str) -> bool:
        try:
            return self.http.post(REVOKE_URL, data={"token": token}).status_code == 200
        except httpx.HTTPError:
            return False

    def connect(self, client_id: str, client_secret: str, **consent: Any) -> str:
        """Run the consent and save the grant. Returns the connected address."""
        grant = run_consent(client_id, client_secret, self.http, **consent)
        try:
            self.store.save(grant)
        except Exception:
            self._revoke(grant.refresh_token)  # a grant that could not be saved is withdrawn, not left behind
            raise
        self._grant, self._token, self._expires = grant, "", 0.0
        self._state = ""
        self._set("connected")
        return grant.address

    def check(self) -> str:
        """Prove the grant still works by asking for the profile. Returns the state after the call."""
        try:
            self.get("/profile")
        except MailTransient:
            raise
        except MailError:
            pass
        return self.state()

    def disconnect(self) -> dict:
        """Withdraw the grant at Google and remove it from where it is kept."""
        self.unlock()
        revoked = self._revoke(self._grant.refresh_token) if self._grant is not None else False
        removed = self.store.delete()
        self._grant, self._token, self._expires = None, "", 0.0
        self._set("not_connected")
        return {"revoked": revoked, "removed": removed}
