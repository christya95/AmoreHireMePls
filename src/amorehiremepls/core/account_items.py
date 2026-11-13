"""Logins the app saves in 1Password for accounts it creates on job sites.

The login is saved, marked unconfirmed, before any sign-up form is sent, so a new password never exists only in a
browser. 1Password makes the password (`--generate-password`); the app never makes one and never puts one on a
command line. Everything else about the item goes to the tool as a JSON template on standard input.

What the owner sees in 1Password:
  title     "<Employer> job application account (<host>)" for an employer's own site,
            "<Site> job site account (<host>)" for a site many employers share
  website   the sign-in page, so the sign-in lookup finds it by host
  username  the account email
  tags      the app's tag, the kind tag, and one state tag (unconfirmed, confirmed, not created)
  section   "Created by AmoreHireMePls": employer, job title, requisition, job link, date, status, app record

Rules kept here:
  - no login is created while any login exists for the site's registrable domain (`existing_logins`)
  - one item per account; a site that refuses the password for its rules gets a new password on the same item, a
    fixed number of passwords in all
  - an item is renamed, annotated or archived only when it carries this module's own marks: both tags, the section,
    and the app record the caller holds. No other item is ever touched.
  - every call here runs from the owner's press on the dashboard, because 1Password may show its own window. A missed
    window is `ApprovalNotGiven`: ask again, nothing failed.
  - the secret leaves this process only sealed to the asking task's one-time key (`answer_account_ask`)
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from amorehiremepls.core.credentials import (
    NOT_CREATED_TAG,
    ApprovalNotGiven,
    Credential,
    CredentialSource,
    LoginLookupError,
    LookupTransient,
    OpCliSource,
    SavedLogin,
    SourceUnavailable,
    fingerprint,
    registrable_domain,
    same_site,
    secure_page,
    site_host,
    with_retries,
)
from amorehiremepls.core.sealed import SealError, public_is_ours, seal
from amorehiremepls.core.site_signin import ask_context
from amorehiremepls.core.util import local_now, normalize_employer

APP_TAG = "AmoreHireMePls"
KIND_TAG = "job-site-account"
UNCONFIRMED_TAG = "account-unconfirmed"
CONFIRMED_TAG = "account-confirmed"
STATE_TAGS = (UNCONFIRMED_TAG, CONFIRMED_TAG, NOT_CREATED_TAG)
SECTION = "Created by AmoreHireMePls"
SECTION_ID = "created_by_app"
F_EMPLOYER, F_JOB, F_REQUISITION, F_LINK = "Employer", "Job title", "Requisition", "Job link"
F_DATE, F_STATUS, F_RECORD, F_RECIPE, F_PASSWORDS = "Created on", "Status", "App record", "Password rules used", "Passwords made"

PASSWORD_TRIES = 3  # passwords one sign-up may use in all: the first and two replacements
GENERATOR_SYMBOLS = "!@.-_*"  # the only symbols 1Password's generator uses
DEFAULT_LENGTH, MIN_LENGTH, MAX_LENGTH = 32, 8, 64

STATUS_UNCONFIRMED = "Unconfirmed, saved before the form was sent"
STATUS_NOT_CREATED = "Not created, the site refused"
STATUS_UNCLEAR = "Unclear, check the site"


def status_confirmed(day: str) -> str:
    return f"Confirmed on {day}"


class ItemError(Exception):
    """An item could not be written. `kind` is a short category; the message never carries tool output or a secret."""

    kind = "failed"

    def __init__(self, kind: str | None = None) -> None:
        self.kind = kind or self.kind
        super().__init__(self.kind)


class NotOurItem(ItemError):
    """The item does not carry this module's marks. It is left exactly as it is."""

    kind = "not_ours"


class PasswordLimit(ItemError):
    kind = "password_limit"


class RulesImpossible(ItemError):
    """The site's password rules cannot be met by 1Password's generator. The owner is asked; nothing is made up."""

    kind = "password_rules"


# -- password rules ------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class PasswordRules:
    """What a sign-up page says about passwords. Zero or empty means the page did not say."""

    min_length: int = 0
    max_length: int = 0
    need_lower: bool = False
    need_upper: bool = False
    need_digit: bool = False
    need_symbol: bool = False
    no_symbols: bool = False  # the page allows letters and digits only
    forbidden: str = ""  # characters the page names as not allowed
    allowed_symbols: str = ""  # when the page names the only symbols it accepts

    def symbols_usable(self) -> bool:
        """Whether the generator's own symbols are all acceptable. It cannot be told to use a different set."""
        if self.no_symbols or any(mark in self.forbidden for mark in GENERATOR_SYMBOLS):
            return False
        return not self.allowed_symbols or all(mark in self.allowed_symbols for mark in GENERATOR_SYMBOLS)


def recipe_for(rules: PasswordRules | None = None) -> str:
    """The generator recipe for these rules: character kinds and a length, as the tool takes them."""
    rules = rules or PasswordRules()
    symbols = rules.symbols_usable()
    if rules.need_symbol and not symbols:
        raise RulesImpossible()
    length = DEFAULT_LENGTH if not rules.max_length else min(DEFAULT_LENGTH, rules.max_length)
    length = max(length, rules.min_length)
    if length < MIN_LENGTH or length > MAX_LENGTH or (rules.max_length and length > rules.max_length):
        raise RulesImpossible()
    return ",".join(["letters", "digits", *(["symbols"] if symbols else []), str(length)])


def fits(rules: PasswordRules | None, value: str) -> bool:
    """Whether a generated password meets the page's stated rules. Checked in memory, before anything is typed."""
    rules = rules or PasswordRules()
    if not value or len(value) < rules.min_length or (rules.max_length and len(value) > rules.max_length):
        return False
    marks = [char for char in value if not char.isalnum()]
    if any(char in rules.forbidden for char in value) or (rules.no_symbols and marks):
        return False
    if rules.allowed_symbols and any(char not in rules.allowed_symbols for char in marks):
        return False
    wanted = ((rules.need_lower, str.islower), (rules.need_upper, str.isupper), (rules.need_digit, str.isdigit))
    if any(need and not any(test(char) for char in value) for need, test in wanted):
        return False
    return bool(marks) or not rules.need_symbol


# -- what an item says ---------------------------------------------------------------------------------------
def account_email(settings: Any, profile: Any) -> str:
    """The address new accounts are registered with: the setting when it is set, else the profile's email."""
    override = str(getattr(settings, "site_account_email", "") or "").strip()
    return override or str(profile.fact_value("email") or "").strip()


def site_label(host: str) -> str:
    """A readable name for a site from its registrable domain: `open-roles.example` becomes `Open Roles`."""
    first = registrable_domain(host).split(".")[0]
    return " ".join(word.capitalize() for word in re.split(r"[-_]+", first) if word)


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def item_title(employer: str, host: str, shared: bool | None = None, site_name: str = "") -> str:
    """The item's name in 1Password. `shared` says one account serves many employers; None decides from the host:
    the employer form is used only when the host contains the employer's name."""
    name, where = " ".join((employer or "").split()), site_host(host)
    if shared is None:
        squashed = _squash(normalize_employer(name))
        shared = not (len(squashed) >= 3 and squashed in _squash(where))
    if shared or not name:
        return f"{' '.join(site_name.split()) or site_label(where)} job site account ({where})"[:200]
    return f"{name} job application account ({where})"[:200]


@dataclass(frozen=True)
class ItemSpec:
    """Everything one new item says. No secret: the password is made by 1Password when the item is created."""

    title: str
    signin_url: str
    username: str = field(repr=False)
    record: str  # the app's own record of this account; the mark that later proves the item is the app's
    vault: str = ""
    employer: str = ""
    job_title: str = ""
    requisition: str = ""
    job_url: str = ""
    careers_url: str = ""
    created_on: str = ""

    def notes(self) -> str:
        what = " ".join(part for part in (f"to apply for {self.job_title}" if self.job_title else "", f"at {self.employer}" if self.employer else "") if part)
        return f"Created by {APP_TAG} on {self.created_on}{' ' + what if what else ''}. The app made this account because 1Password had no login for {site_host(self.signin_url)}."


def item_spec(
    job: dict, signin_url: str, email: str, record: str, *, vault: str = "", careers_url: str = "", shared: bool | None = None, site_name: str = "", today: str = ""
) -> ItemSpec:
    """The item for one job's sign-up, from the job record and the settings. Nothing here is a fixed name."""
    if not secure_page(signin_url) or "@" not in (email or "") or not (record or "").strip():
        raise ItemError("bad_spec")
    employer = " ".join(str(job.get("employer") or "").split())
    return ItemSpec(
        title=item_title(employer, signin_url, shared, site_name),
        signin_url=signin_url.strip(),
        username=email.strip(),
        record=record.strip(),
        vault=vault.strip(),
        employer=employer,
        job_title=" ".join(str(job.get("title") or "").split())[:200],
        requisition=str(job.get("requisition_id") or "")[:120],
        job_url=str(job.get("posting_url") or job.get("apply_url") or "")[:2000],
        careers_url=careers_url.strip(),
        created_on=today or local_now().date().isoformat(),
    )


def item_template(spec: ItemSpec) -> dict:
    """The JSON the tool reads on standard input. The password field is left empty for the generator to fill."""
    urls = [{"label": "website", "primary": True, "href": spec.signin_url}]
    if spec.careers_url and site_host(spec.careers_url) != site_host(spec.signin_url):
        urls.append({"label": "careers", "href": spec.careers_url})
    rows = (
        (F_EMPLOYER, spec.employer),
        (F_JOB, spec.job_title),
        (F_REQUISITION, spec.requisition),
        (F_LINK, spec.job_url),
        (F_DATE, spec.created_on),
        (F_STATUS, STATUS_UNCONFIRMED),
        (F_RECORD, spec.record),
        (F_PASSWORDS, "1"),
    )
    return {
        "title": spec.title,
        "category": "LOGIN",
        "tags": [APP_TAG, KIND_TAG, UNCONFIRMED_TAG],
        "urls": urls,
        "sections": [{"id": SECTION_ID, "label": SECTION}],
        "fields": [
            {"id": "username", "type": "STRING", "purpose": "USERNAME", "label": "username", "value": spec.username},
            {"id": "password", "type": "CONCEALED", "purpose": "PASSWORD", "label": "password", "value": ""},
            {"id": "notesPlain", "type": "STRING", "purpose": "NOTES", "label": "notesPlain", "value": spec.notes()},
            *({"id": _squash(label), "section": {"id": SECTION_ID}, "type": "STRING", "label": label, "value": value} for label, value in rows if value),
        ],
    }


# -- finding what is already saved ---------------------------------------------------------------------------
def existing_logins(source: CredentialSource, site: str, sleep: Callable[[float], None] | None = None) -> list[SavedLogin]:
    """Every saved login for this site's registrable domain, whoever made it. Any one of them means: do not create."""
    kwargs = {"sleep": sleep} if sleep is not None else {}
    return [login for login in with_retries(source.logins, **kwargs) if any(same_site(site, host) for host in login.hosts)]


def made_by_app(login: SavedLogin) -> bool:
    """Whether the list entry carries both tags. The full check, with the section and the record, is `ItemWriter.owns`."""
    return APP_TAG in login.tags and KIND_TAG in login.tags


# -- writing -------------------------------------------------------------------------------------------------
class ItemWriter(Protocol):
    def available(self) -> bool: ...

    def create(self, spec: ItemSpec, recipe: str) -> Credential: ...

    def regenerate(self, login: SavedLogin, record: str, recipe: str, made: int) -> Credential: ...

    def annotate(self, login: SavedLogin, record: str, state_tag: str, status: str) -> SavedLogin: ...

    def rename(self, login: SavedLogin, record: str, title: str) -> SavedLogin: ...

    def archive(self, login: SavedLogin, record: str) -> None: ...

    def owns(self, login: SavedLogin, record: str) -> bool: ...


class OpCliWriter:
    """Writes through the 1Password command line tool, with the same runner and error categories as the lookup.

    `op` should be an `OpCliSource` whose vault is the one new logins go to. `tries` is how many passwords one item
    may have in all.
    """

    def __init__(self, op: OpCliSource, tries: int = PASSWORD_TRIES) -> None:
        self.op = op
        self.tries = max(1, min(int(tries), PASSWORD_TRIES))

    def available(self) -> bool:
        return self.op.available()

    def _item(self, raw: str) -> dict:
        try:
            item = json.loads(raw or "{}")
        except ValueError:
            raise SourceUnavailable("failed") from None
        if not isinstance(item, dict) or not item.get("id"):
            raise SourceUnavailable("failed")
        return item

    def _login(self, item: dict, username: str = "") -> SavedLogin:
        ref = str(item["id"])
        hosts = tuple(dict.fromkeys(h for h in (site_host(str(url.get("href") or "")) for url in item.get("urls") or [] if isinstance(url, dict)) if h))
        return SavedLogin(
            ref=ref,
            fingerprint=fingerprint(ref, item.get("version"), item.get("updated_at")),
            username=username,
            hosts=hosts,
            vault=str((item.get("vault") or {}).get("id") or ""),
            title=str(item.get("title") or "")[:120],
            tags=tuple(str(tag) for tag in item.get("tags") or [] if tag),
        )

    def _credential(self, raw: str, username: str) -> Credential:
        """The tool's answer to a create or an edit. If it left the password out, the item is read once more."""
        item = self._item(raw)
        login = self._login(item, username)
        password = next((str(row.get("value") or "") for row in item.get("fields") or [] if isinstance(row, dict) and row.get("purpose") == "PASSWORD"), "")
        if not password:
            return self.op.secret(login)
        return Credential(login=login, username=username, password=password)

    def create(self, spec: ItemSpec, recipe: str) -> Credential:
        """Save one new login with a generated password. Not repeated by itself: a second call could save a second item."""
        if not spec.record or not spec.username or not secure_page(spec.signin_url):
            raise ItemError("bad_spec")
        if not (spec.vault or self.op.vault):
            raise ItemError("vault_missing")  # the tool would pick a vault by itself; the owner names where new logins go
        raw = self.op.call(["item", "create", "-", f"--generate-password={recipe}", "--format", "json"], vault=spec.vault, stdin_text=json.dumps(item_template(spec)))
        return self._credential(raw, spec.username)

    def _marks(self, login: SavedLogin) -> tuple[tuple[str, ...], str]:
        """The item's tags and its app record, read from the item itself. Everything else in the answer is dropped."""
        item = self._item(self.op.call(["item", "get", login.ref, "--format", "json"], vault=login.vault))
        tags = tuple(str(tag) for tag in item.get("tags") or [] if tag)
        record = ""
        for row in item.get("fields") or []:
            section = row.get("section") if isinstance(row, dict) else None
            if isinstance(section, dict) and section.get("label") == SECTION and row.get("label") == F_RECORD:
                record = str(row.get("value") or "")
        return tags, record

    def _ours(self, login: SavedLogin, record: str) -> tuple[str, ...]:
        tags, saved = self._marks(login)
        if not record or saved != record or APP_TAG not in tags or KIND_TAG not in tags:
            raise NotOurItem()
        return tags

    def owns(self, login: SavedLogin, record: str) -> bool:
        try:
            self._ours(login, record)
        except NotOurItem:
            return False
        return True

    def _edit(self, login: SavedLogin, args: list[str]) -> str:
        return self.op.call(["item", "edit", login.ref, *args, "--format", "json"], vault=login.vault)

    def regenerate(self, login: SavedLogin, record: str, recipe: str, made: int) -> Credential:
        """A new generated password on the same item. `made` is how many passwords the item has had so far."""
        if made >= self.tries:
            raise PasswordLimit()
        self._ours(login, record)
        raw = self._edit(login, [f"{SECTION}.{F_PASSWORDS}[text]={made + 1}", f"{SECTION}.{F_RECIPE}[text]={recipe}", f"--generate-password={recipe}"])
        return self._credential(raw, login.username)

    def annotate(self, login: SavedLogin, record: str, state_tag: str, status: str) -> SavedLogin:
        """Set the state tag and the status line. Other tags the owner added are kept."""
        if state_tag not in STATE_TAGS:
            raise ItemError("bad_state")
        tags = [tag for tag in self._ours(login, record) if tag not in STATE_TAGS and "," not in tag] + [state_tag]
        raw = self._edit(login, [f"{SECTION}.{F_STATUS}[text]={' '.join(status.split())[:200]}", "--tags", ",".join(tags)])
        return self._login(self._item(raw), login.username)

    def rename(self, login: SavedLogin, record: str, title: str) -> SavedLogin:
        self._ours(login, record)
        return self._login(self._item(self._edit(login, ["--title", " ".join(title.split())[:200]])), login.username)

    def archive(self, login: SavedLogin, record: str) -> None:
        """Move the app's own unused login to 1Password's Archive. Never a permanent delete."""
        self._ours(login, record)
        self.op.call(["item", "delete", login.ref, "--archive"], vault=login.vault)


def mark_confirmed(writer: ItemWriter, login: SavedLogin, record: str, day: str = "") -> SavedLogin:
    return writer.annotate(login, record, CONFIRMED_TAG, status_confirmed(day or local_now().date().isoformat()))


def mark_not_created(writer: ItemWriter, login: SavedLogin, record: str) -> SavedLogin:
    return writer.annotate(login, record, NOT_CREATED_TAG, STATUS_NOT_CREATED)


def mark_unclear(writer: ItemWriter, login: SavedLogin, record: str) -> SavedLogin:
    return writer.annotate(login, record, UNCONFIRMED_TAG, STATUS_UNCLEAR)


# -- the dashboard's side of one ask -------------------------------------------------------------------------
@dataclass(frozen=True)
class AccountAnswer:
    """What the dashboard does with one press. `reply` goes to the waiting task; None keeps it waiting."""

    flash: str
    ok: bool
    reply: dict | None = None
    missed: bool = False  # 1Password's window was missed or the tool had a passing failure; the same ask stays open
    title: str = ""  # the item's name, for the page
    made: int = 0  # passwords the item has had, to save on the app's record


def _error(flash: str, code: str) -> AccountAnswer:
    return AccountAnswer(flash, False, {"kind": "error", "error": code})


def answer_account_ask(
    ask: dict,
    writer: ItemWriter,
    source: CredentialSource,
    app_secret: str,
    spec: ItemSpec,
    rules: PasswordRules | None = None,
    *,
    login: SavedLogin | None = None,
    made: int = 0,
    tries: int = PASSWORD_TRIES,
    sleep: Callable[[float], None] | None = None,
) -> AccountAnswer:
    """Save the login for one sign-up, or give it a new password, and seal it to the asking task.

    Called only from the owner's button press, on the dashboard that runs where 1Password is. `ask["step"]` is
    `account` (save a new login; refused when any login for the site exists) or `password` (the site refused the
    password for its rules: a new one on `login`, the same item). `made` is how many passwords the item has had.

    The reply is an error code or a sealed box with the same fields a sign-in box has. The password is never returned
    in the clear. A missed 1Password window returns `missed` and leaves the ask open.
    """
    context = ask_context(ask)
    host = site_host(str(ask.get("host") or ""))
    step = str(ask.get("step") or "")
    if not host or step not in ("account", "password") or not public_is_ours(app_secret, str(ask.get("key") or ""), context, str(ask.get("key_sig") or "")):
        return _error("account_mismatch", "mismatch")
    if not same_site(host, spec.signin_url) or not secure_page(spec.signin_url):
        return _error("account_mismatch", "mismatch")
    if not writer.available() or not source.available():
        return AccountAnswer("account_not_here", False)
    tries = max(1, min(int(tries), PASSWORD_TRIES))
    try:
        recipe = recipe_for(rules)
        if step == "password":
            if login is None:
                return _error("account_mismatch", "mismatch")
            if made >= tries:
                return _error("account_password_limit", "password_limit")
            found, made = writer.regenerate(login, spec.record, recipe, made), made + 1
        else:
            saved = existing_logins(source, host, sleep)
            mine = [item for item in saved if made_by_app(item) and UNCONFIRMED_TAG in item.tags]
            if len(saved) == 1 and len(mine) == 1 and writer.owns(mine[0], spec.record):
                # An earlier press saved this login and then lost its answer. It is used; a second item is never made.
                found, made = source.secret(mine[0]), max(made, 1)
            elif saved:
                return _error("account_login_exists", "login_exists")
            else:
                found, made = writer.create(spec, recipe), 1
        while not fits(rules, found.password):
            if made >= tries:
                return AccountAnswer("account_password_rules", False, {"kind": "error", "error": "password_rules"}, title=found.login.title, made=made)
            found, made = writer.regenerate(found.login, spec.record, recipe, made), made + 1
    except ApprovalNotGiven:
        return AccountAnswer("account_not_approved", False, missed=True)
    except LookupTransient:
        return AccountAnswer("account_save_failed", False, missed=True)
    except RulesImpossible:
        return _error("account_password_rules", "password_rules")
    except PasswordLimit:
        return _error("account_password_limit", "password_limit")
    except NotOurItem:
        return _error("account_not_ours", "not_ours")
    except (ItemError, LoginLookupError) as exc:
        return _error("account_vault_missing" if "vault_missing" in (getattr(exc, "detail", ""), getattr(exc, "kind", "")) else "account_cannot_save", "cannot_save")
    payload = {"host": host, "username": found.username, "password": found.password, "has_code": False, "fingerprint": found.login.fingerprint, "title": found.login.title}
    try:
        box = seal(str(ask.get("key") or ""), payload, context)
    except SealError:
        return _error("account_mismatch", "mismatch")
    return AccountAnswer("account_password_made" if step == "password" else "account_saved", True, {"kind": "box", "box": box}, title=found.login.title, made=made)
