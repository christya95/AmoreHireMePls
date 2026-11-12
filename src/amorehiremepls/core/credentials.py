"""Saved site logins, read from the owner's password manager by the page's host.

A `CredentialSource` lists saved logins (website hosts and usernames, never secrets) and reads one login's
password or one-time code. `OpCliSource` talks to the 1Password command line tool, which asks the owner for
Touch ID or the account password in its own window. Tests use a deterministic double with the same three methods.

Matching: a login saved for the page's exact host wins. Otherwise a login saved for another host of the same
registrable domain is used (a sign-in host next to a careers host). A login for a different registrable domain is
never used. Several matches are narrowed by the profile email; if more than one is left, nothing is guessed.

Nothing here logs, prints, or stores a secret. Error text from the tool is reduced to a category.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

LOOKUP_ATTEMPTS = 3  # a lookup that fails for a passing reason is tried this many times in all
LOOKUP_BACKOFF_SECONDS = (1.0, 3.0)
# Second-level labels that are public suffixes under a two-letter country code (example.co.uk is the site, not co.uk).
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
_COUNTRY_SECOND_LEVEL = frozenset({"co", "com", "org", "net", "gov", "edu", "ac", "or", "ne", "go", "gob", "nom"})


class LoginLookupError(Exception):
    """A lookup that did not return one login. `kind` is a short category; the message never carries tool output."""

    kind = "failed"

    def __init__(self, kind: str | None = None) -> None:
        self.kind = kind or self.kind
        super().__init__(self.kind)


class NoSavedLogin(LoginLookupError):
    kind = "no_login"


class SeveralSavedLogins(LoginLookupError):
    kind = "several"


class ApprovalNotGiven(LoginLookupError):
    """The password manager's own prompt was missed, dismissed, or timed out. Asking again is always safe."""

    kind = "not_approved"


class SourceUnavailable(LoginLookupError):
    """The password manager cannot be read here: `not_installed`, `not_signed_in`, `integration_off`, or `failed`."""

    kind = "failed"


class LookupTransient(LoginLookupError):
    kind = "transient"


@dataclass(frozen=True)
class SavedLogin:
    """One saved login without its secret. `fingerprint` changes whenever the saved login is edited."""

    ref: str = field(repr=False)
    fingerprint: str
    username: str = field(repr=False, default="")
    hosts: tuple[str, ...] = ()
    vault: str = field(repr=False, default="")
    title: str = ""  # the saved login's own name, shown so the owner knows which one to edit


@dataclass(frozen=True)
class Credential:
    login: SavedLogin
    username: str = field(repr=False)
    password: str = field(repr=False)
    has_one_time_code: bool = False


class CredentialSource(Protocol):
    def available(self) -> bool: ...

    def logins(self) -> list[SavedLogin]: ...

    def secret(self, login: SavedLogin) -> Credential: ...

    def one_time_code(self, login: SavedLogin) -> str: ...


def site_host(value: str) -> str:
    """Lower-case host of a URL or of a bare host. The port and any user part are dropped."""
    text = (value or "").strip().lower()
    if not text:
        return ""
    parsed = urlsplit(text if "://" in text else f"//{text}")
    return (parsed.hostname or "").strip(".")


def secure_page(url: str) -> bool:
    """https, or a page on this computer (the only place plain http is ever accepted)."""
    text = (url or "").strip().lower()
    return text.startswith("https://") or (text.startswith("http://") and site_host(text) in _LOOPBACK)


def registrable_domain(host: str) -> str:
    """The part of a host that one organisation registers. An address or a single label is returned as it is."""
    name = site_host(host)
    if not name:
        return ""
    try:
        ipaddress.ip_address(name)
        return name
    except ValueError:
        pass
    labels = name.split(".")
    if len(labels) <= 2:
        return name
    if len(labels[-1]) == 2 and labels[-2] in _COUNTRY_SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def same_site(left: str, right: str) -> bool:
    a, b = registrable_domain(left), registrable_domain(right)
    return bool(a) and a == b


def fingerprint(ref: str, version: Any = "", updated: Any = "") -> str:
    """Identifies one saved login in one state. Not reversible, and it holds no part of the secret."""
    return hashlib.sha256(f"{ref}|{version}|{updated}".encode()).hexdigest()


def choose_login(logins: list[SavedLogin], host: str, prefer_username: str = "") -> SavedLogin:
    """The one saved login for this host. Raises `NoSavedLogin` or `SeveralSavedLogins`; it never guesses."""
    want = site_host(host)
    exact = [item for item in logins if want and want in item.hosts]
    found = exact or [item for item in logins if any(same_site(want, saved) for saved in item.hosts)]
    if not found:
        raise NoSavedLogin()
    if len(found) > 1 and prefer_username.strip():
        mine = [item for item in found if item.username.strip().lower() == prefer_username.strip().lower()]
        if mine:
            found = mine
    if len(found) > 1:
        raise SeveralSavedLogins()
    return found[0]


def with_retries(call: Callable[[], Any], attempts: int = LOOKUP_ATTEMPTS, sleep: Callable[[float], None] = time.sleep) -> Any:
    """Run one lookup call again after a passing failure, a fixed number of times. Other failures are not retried."""
    tries = max(1, attempts)
    for index in range(tries):
        try:
            return call()
        except LookupTransient:
            if index + 1 >= tries:
                raise
            sleep(LOOKUP_BACKOFF_SECONDS[min(index, len(LOOKUP_BACKOFF_SECONDS) - 1)])
    raise LookupTransient()


def find_login(source: CredentialSource, host: str, prefer_username: str = "", attempts: int = LOOKUP_ATTEMPTS, sleep: Callable[[float], None] = time.sleep) -> SavedLogin:
    return choose_login(with_retries(source.logins, attempts, sleep), host, prefer_username)


_NOT_APPROVED = ("authorization prompt dismissed", "authorization timeout", "authorization denied", "prompt was dismissed", "user cancelled", "user canceled")
_INTEGRATION = ("connecting to desktop app", "connect to the 1password desktop app", "desktop app integration", "couldn't connect to the 1password", "app integration")
_SIGNED_OUT = ("not currently signed in", "not signed in", "no accounts configured", "no account found", "sign in to", "session expired", "invalid service account")
_PASSING = ("timeout", "timed out", "connection re", "network", "dial tcp", "temporar", "too many requests", "rate limit", " 502", " 503", " 504")


def _category(stderr: str) -> LoginLookupError:
    """Turn the tool's error text into a category. The text itself is dropped here and goes nowhere else."""
    text = (stderr or "").lower()
    if any(mark in text for mark in _NOT_APPROVED):
        return ApprovalNotGiven()
    if any(mark in text for mark in _INTEGRATION):
        return SourceUnavailable("integration_off")
    if any(mark in text for mark in _SIGNED_OUT):
        return SourceUnavailable("not_signed_in")
    if any(mark in text for mark in _PASSING):
        return LookupTransient()
    return SourceUnavailable("failed")


class OpCliSource:
    """Saved logins from the 1Password command line tool.

    The tool asks for approval in the password manager's own window. This class never sees that approval and never
    asks for the account password. `account` and `vault` narrow the search when they are set; empty searches
    everything the signed-in person can read.
    """

    def __init__(self, account: str = "", vault: str = "", binary: str = "op", timeout_seconds: float = 120.0, run: Callable[..., Any] | None = None) -> None:
        self.account = account.strip()
        self.vault = vault.strip()
        self.binary = binary
        self.timeout_seconds = timeout_seconds
        self._run = run or subprocess.run

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def _call(self, args: list[str], vault: str = "") -> str:
        if not self.available():
            raise SourceUnavailable("not_installed")
        command = [self.binary, *args]
        if vault or self.vault:
            command += ["--vault", vault or self.vault]
        if self.account:
            command += ["--account", self.account]
        try:
            done = self._run(command, capture_output=True, text=True, timeout=self.timeout_seconds, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            # The tool was still waiting for its approval window. That is a missed prompt, not a broken lookup.
            raise ApprovalNotGiven() from None
        except OSError:
            raise SourceUnavailable("not_installed") from None
        if done.returncode != 0:
            raise _category(done.stderr or "")
        return done.stdout or ""

    def logins(self) -> list[SavedLogin]:
        raw = self._call(["item", "list", "--categories", "Login", "--format", "json"])
        try:
            rows = json.loads(raw or "[]")
        except ValueError:
            raise SourceUnavailable("failed") from None
        found: list[SavedLogin] = []
        for row in rows if isinstance(rows, list) else []:
            ref = str(row.get("id") or "")
            hosts = tuple(dict.fromkeys(h for h in (site_host(str(url.get("href") or "")) for url in row.get("urls") or [] if isinstance(url, dict)) if h))
            if not ref or not hosts:
                continue
            found.append(
                SavedLogin(
                    ref=ref,
                    fingerprint=fingerprint(ref, row.get("version"), row.get("updated_at")),
                    username=str(row.get("additional_information") or ""),
                    hosts=hosts,
                    vault=str((row.get("vault") or {}).get("id") or ""),
                    title=str(row.get("title") or "")[:120],
                )
            )
        return found

    def secret(self, login: SavedLogin) -> Credential:
        raw = self._call(["item", "get", login.ref, "--format", "json"], vault=login.vault)
        try:
            item = json.loads(raw or "{}")
        except ValueError:
            raise SourceUnavailable("failed") from None
        username, password, has_code = login.username, "", False
        for row in item.get("fields") or []:
            if not isinstance(row, dict):
                continue
            purpose, kind, value = str(row.get("purpose") or ""), str(row.get("type") or ""), str(row.get("value") or "")
            if purpose == "USERNAME" and value:
                username = value
            elif purpose == "PASSWORD" and value:
                password = value
            elif kind == "OTP":
                has_code = True
        if not password:
            raise NoSavedLogin()
        return Credential(login=login, username=username, password=password, has_one_time_code=has_code)

    def one_time_code(self, login: SavedLogin) -> str:
        code = "".join(self._call(["item", "get", login.ref, "--otp"], vault=login.vault).split())
        if not code:
            raise NoSavedLogin()
        return code
