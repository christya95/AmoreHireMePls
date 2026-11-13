"""Find the one verification code or link a job site just emailed after a sign-up.

A narrow lookup, done in code with no model: mail from the site's registrable domain (or a sender domain the site's
own page named), to the account's address, received in the few minutes after the form was sent, and signed by that
domain. From such a message exactly one thing is taken: a short code that stands next to words like "code", or one
https link on the site's own registrable domain whose address says verify, confirm or activate.

Mail is untrusted. A message cannot change what the app does beyond supplying that one value. A link to any other
registrable domain is never returned. Two different candidates, in one message or across several, are "ambiguous":
the owner is asked and nothing is guessed. Nothing of a message is kept: no body, no subject, no message id. What is
returned is the value, the sender's domain, the time, and a digest that lets a caller skip the same message later.

The reader runs where the mail grant is (`core/mail_access.py`). A task on another computer asks for it the way it
asks for a saved login, and the value travels sealed to that task's one-time key (`answer_mail_ask`).
"""

from __future__ import annotations

import base64
import hashlib
import html
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import getaddresses, parseaddr
from html.parser import HTMLParser
from typing import Any, Protocol
from urllib.parse import urlsplit

from amorehiremepls.core.credentials import registrable_domain, same_site, site_host
from amorehiremepls.core.mail_access import MailAccess, MailError, MailTransient
from amorehiremepls.core.sealed import SealError, public_is_ours, seal
from amorehiremepls.core.site_signin import ask_context
from amorehiremepls.core.util import utcnow

MAX_MESSAGES = 5  # how many messages one lookup may open
MAX_SENDERS = 3  # sender domains besides the site's own
QUERY_SLACK_SECONDS = 60  # the search starts this long before the form was sent, because its clock is coarse
SKEW_SECONDS = 5  # a message is used only if it arrived after the form was sent, less this much clock difference
WINDOW_SECONDS = 600  # and no later than this after it
MAX_AGE_SECONDS = 1800  # a sign-up older than this is not looked for at all
BODY_CAP = 200_000
POLL_FIRST = (5, 10, 20, 30)  # seconds after the start for the first looks; then every POLL_EVERY
POLL_EVERY = 30
RECEIVER = "mx.google.com"  # the server whose check of the sender's signature is trusted

_ADDRESS = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$")
_DOMAIN = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
# "code" and its kin, but not a postal, area, country or discount code
_CODE_WORD = r"\b(?<!zip\s)(?<!postal\s)(?<!area\s)(?<!country\s)(?<!promo\s)(?<!discount\s)(?<!coupon\s)(?:code|passcode|otp|pin)\b"
_TOKEN = r"(?:\d{3}[ -]\d{3}|\d{4,8}|(?=[A-Z0-9]{0,7}\d)(?=[A-Z0-9]{0,7}[A-Z])[A-Z0-9]{5,8})"
_EDGE_BEFORE, _EDGE_AFTER = r"(?<![\w@#$/.:=+-])", r"(?![\w@/=+-]|\.\w)"
# "your code is 481516", "code: 481516"
_AFTER_WORD = re.compile(rf"(?i:{_CODE_WORD})[^\n.!?\d]{{0,40}}?{_EDGE_BEFORE}({_TOKEN}){_EDGE_AFTER}")
# "481516 is your verification code"
_BEFORE_WORD = re.compile(rf"{_EDGE_BEFORE}({_TOKEN}){_EDGE_AFTER}[^\n.!?\d]{{0,40}}?(?i:{_CODE_WORD})")
_ALONE = re.compile(rf"^[ \t]*({_TOKEN})[ \t]*$", re.M)  # a code on a line of its own
_WORD_NEAR = re.compile(_CODE_WORD, re.I)
_LINK_WORD = re.compile(r"verif|confirm|activat|validat", re.I)
_URL = re.compile(r"https://[^\s<>\"'\])]+")
_BLOCKS = frozenset({"p", "div", "br", "tr", "td", "th", "li", "h1", "h2", "h3", "h4", "table", "section", "center", "blockquote"})
_HIDDEN = frozenset({"script", "style", "head", "title"})


@dataclass(frozen=True)
class MailMessage:
    """One message as it was read. It lives only while it is looked at and is never stored or logged."""

    ref: str = field(repr=False)
    sender: str = field(repr=False)
    to: tuple[str, ...] = field(repr=False)
    received_at: datetime
    signed_by: tuple[str, ...] = ()  # domains whose signature the receiving server checked and passed
    text: str = field(repr=False, default="")
    html: str = field(repr=False, default="")


class MailReader(Protocol):
    def recent(self, sender_domains: tuple[str, ...], to: str, since: datetime, limit: int) -> list[MailMessage]: ...


# -- reading Gmail -------------------------------------------------------------------------------------------
def _decode(part: dict) -> str:
    data = str((part.get("body") or {}).get("data") or "")
    if not data:
        return ""
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except ValueError:
        return ""
    kind = next((str(h.get("value") or "") for h in part.get("headers") or [] if str(h.get("name") or "").lower() == "content-type"), "")
    charset = re.search(r"charset=\"?([\w-]+)", kind, re.I)
    try:
        return raw.decode(charset.group(1) if charset else "utf-8", errors="replace")[:BODY_CAP]
    except LookupError:
        return raw.decode("utf-8", errors="replace")[:BODY_CAP]


def _bodies(part: dict, depth: int = 0) -> tuple[str, str]:
    """The plain and the HTML text of a message. Attachments are not read."""
    if not isinstance(part, dict) or depth > 8 or part.get("filename"):
        return "", ""
    kind = str(part.get("mimeType") or "").lower()
    if kind == "text/plain":
        return _decode(part), ""
    if kind == "text/html":
        return "", _decode(part)
    plain, rich = "", ""
    for child in part.get("parts") or []:
        a, b = _bodies(child, depth + 1)
        plain, rich = (plain + "\n" + a)[:BODY_CAP] if a else plain, (rich + "\n" + b)[:BODY_CAP] if b else rich
    return plain, rich


def _signed_by(results: list[str]) -> tuple[str, ...]:
    """Domains with `dkim=pass` in the receiving server's own result header: the topmost one, added on arrival."""
    top = next(iter(results), "")
    if not top.strip().lower().startswith(RECEIVER + ";"):
        return ()
    found = []
    for clause in top.split(";"):
        if re.search(r"\bdkim=pass\b", clause, re.I):
            found += [registrable_domain(d) for d in re.findall(r"header\.(?:i=[^\s@]*@|d=)([A-Za-z0-9.-]+)", clause)]
    return tuple(dict.fromkeys(d for d in found if d))


class GmailReader:
    """Reads messages through the Gmail API with the read-only grant. It lists and gets; it can do nothing else."""

    def __init__(self, access: MailAccess) -> None:
        self.access = access

    def recent(self, sender_domains: tuple[str, ...], to: str, since: datetime, limit: int) -> list[MailMessage]:
        if not _ADDRESS.match(to) or to.lower() != self.access.ready().lower():
            raise MailError("wrong_mailbox")  # the connected mailbox is not the one the account was registered with
        domains = [d for d in sender_domains if _DOMAIN.match(d)][: MAX_SENDERS + 1]
        if not domains:
            return []
        query = f"from:({' OR '.join(domains)}) to:{to} after:{int(since.timestamp()) - QUERY_SLACK_SECONDS}"
        count = max(1, min(int(limit), MAX_MESSAGES))
        listing = self.access.get("/messages", {"q": query, "maxResults": count})
        found = []
        for row in (listing.get("messages") or [])[:count]:
            ref = str(row.get("id") or "") if isinstance(row, dict) else ""
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", ref):
                continue
            found.append(self._message(ref, self.access.get(f"/messages/{ref}", {"format": "full"})))
        return found

    def _message(self, ref: str, body: dict) -> MailMessage:
        payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
        headers: dict[str, list[str]] = {}
        for row in payload.get("headers") or []:
            if isinstance(row, dict):
                headers.setdefault(str(row.get("name") or "").lower(), []).append(str(row.get("value") or ""))
        to = [addr.lower() for _, addr in getaddresses(headers.get("to", []) + headers.get("delivered-to", [])) if addr]
        try:
            received = datetime.fromtimestamp(int(body.get("internalDate") or 0) / 1000, UTC)
        except (ValueError, OverflowError, OSError):
            received = datetime.fromtimestamp(0, UTC)
        plain, rich = _bodies(payload)
        return MailMessage(
            ref, parseaddr(next(iter(headers.get("from", [])), ""))[1].lower(), tuple(to), received, _signed_by(headers.get("authentication-results", [])), plain, rich
        )


# -- taking one value out of a message -----------------------------------------------------------------------
class _Page(HTMLParser):
    """Visible text and link targets of an HTML body. Nothing is fetched and no script runs."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._skip = 0
        self._href: str | None = None
        self._name: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _HIDDEN:
            self._skip += 1
        if tag in _BLOCKS:
            self.out.append("\n")
        if tag == "a":
            self._href, self._name = dict(attrs).get("href") or "", []

    def handle_endtag(self, tag: str) -> None:
        if tag in _HIDDEN and self._skip:
            self._skip -= 1
        if tag in _BLOCKS:
            self.out.append("\n")
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._name).split())))
            self._href = None

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        self.out.append(data)
        if self._href is not None:
            self._name.append(data)


def _read_html(source: str) -> tuple[str, list[tuple[str, str]]]:
    page = _Page()
    try:
        page.feed(source[:BODY_CAP])
        page.close()
    except Exception:
        return "", []
    return "".join(page.out), page.links


def find_codes(text: str) -> list[str]:
    """Codes in a message's text: 4 to 8 digits, or 5 to 8 capital letters and digits, next to a word like "code".

    A number only counts when it follows such a word in the same sentence, comes just before it, or stands on a line
    of its own near it. Spaces and hyphens inside a code are dropped. The order is the order found, without repeats.
    """
    text = text.replace("\r", "").replace("\xa0", " ")
    found = [m.group(1) for pattern in (_AFTER_WORD, _BEFORE_WORD) for m in pattern.finditer(text)]
    for match in _ALONE.finditer(text):
        if _WORD_NEAR.search(text[max(0, match.start() - 300) : match.end() + 150]):
            found.append(match.group(1))
    return list(dict.fromkeys(re.sub(r"[ -]", "", code) for code in found))


def _clean_link(url: str) -> str:
    url = html.unescape(url or "").strip().rstrip(".,;:!?")
    if not url.lower().startswith("https://") or len(url) > 2000 or re.search(r"[\s\x00-\x1f]", url):
        return ""
    netloc = urlsplit(url).netloc
    return url if netloc and "@" not in netloc and site_host(url) else ""


def find_links(message: MailMessage, site: str) -> tuple[list[str], list[str]]:
    """Verification links in a message: those on the site's own registrable domain, and the hosts of the others.

    A link counts when its address, or the words it is shown under, say verify, confirm or activate. Only https links
    are read. The second list is hosts only: a link elsewhere is never returned, only named so the owner can be told.
    """
    candidates = _read_html(message.html)[1] + [(url, "") for url in _URL.findall(message.text)]
    ours: list[str] = []
    elsewhere: list[str] = []
    for raw, name in candidates:
        url = _clean_link(raw)
        if not url:
            continue
        parts = urlsplit(url)
        if not (_LINK_WORD.search(parts.path + "?" + parts.query) or _LINK_WORD.search(name)):
            continue
        if same_site(site_host(url), site):
            ours.append(url)
        else:
            elsewhere.append(site_host(url))
    return list(dict.fromkeys(ours)), list(dict.fromkeys(elsewhere))


# -- the lookup ----------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class MailFinding:
    """The answer to one lookup. `value` is the code or the link and is the only part of any message that is kept."""

    status: str  # found | none | ambiguous | elsewhere | timeout | stale | failed | wrong_mailbox | not_connected | needs_reconnect | locked
    kind: str = ""  # code | link
    value: str = field(repr=False, default="")
    sender_domain: str = ""
    received_at: datetime | None = None
    link_host: str = ""
    seen: str = ""  # a digest of the message, so a refused code's message can be skipped; it is not the message id
    looks: int = 0

    def note(self) -> dict:
        """What may be saved as evidence: where it came from and when. Never the value."""
        return {"status": self.status, "kind": self.kind, "sender_domain": self.sender_domain, "received_at": self.received_at, "link_host": self.link_host, "looks": self.looks}


def _digest(message: MailMessage) -> str:
    return hashlib.sha256(f"{message.ref}|{message.received_at.isoformat()}".encode()).hexdigest()[:16]


def _senders(site: str, sender_domains: Iterable[str]) -> tuple[str, ...]:
    extra = [registrable_domain(str(d)) for d in list(sender_domains)[:MAX_SENDERS]]
    return tuple(dict.fromkeys(d for d in [registrable_domain(site), *extra] if d and _DOMAIN.match(d)))


def find_verification(
    reader: MailReader,
    site: str,
    account_email: str,
    since: datetime,
    *,
    want: str = "code",
    sender_domains: Iterable[str] = (),
    window_seconds: int = WINDOW_SECONDS,
    skip: Iterable[str] = (),
    now: datetime | None = None,
) -> MailFinding:
    """One look for the code or the link `site` emailed to `account_email` after `since`.

    `since` is when the sign-up form was sent (or a new code was asked for). `sender_domains` are domains the site's
    own page named as the sender. `skip` holds digests of messages already used. The search is never widened: an
    older sign-up, another address, another sender or an unsigned message all end as `none` or `stale`.
    """
    now = now or utcnow()
    allowed = _senders(site, sender_domains)
    address = (account_email or "").strip()
    if want not in ("code", "link") or not allowed or not _ADDRESS.match(address) or since.tzinfo is None:
        return MailFinding("failed")
    if now - since > timedelta(seconds=MAX_AGE_SECONDS) or since > now + timedelta(seconds=SKEW_SECONDS):
        return MailFinding("stale")
    until = since + timedelta(seconds=max(60, min(int(window_seconds), 900)))
    try:
        messages = reader.recent(allowed, address, since, MAX_MESSAGES)[:MAX_MESSAGES]
    except MailTransient:
        return MailFinding("failed")
    except MailError as exc:
        return MailFinding(exc.kind if exc.kind in ("wrong_mailbox", "not_connected", "needs_reconnect", "locked") else "failed")
    skipped = set(skip)
    values: dict[str, MailMessage] = {}
    elsewhere: list[str] = []
    for message in messages:
        if not (since - timedelta(seconds=SKEW_SECONDS) <= message.received_at <= until) or _digest(message) in skipped:
            continue
        if address.lower() not in message.to or registrable_domain(message.sender.rpartition("@")[2]) not in allowed:
            continue
        if not set(message.signed_by) & set(allowed):
            continue
        if want == "code":
            found = find_codes(message.text + "\n" + _read_html(message.html)[0])
        else:
            found, other = find_links(message, site)
            elsewhere += other
        for value in found:
            values.setdefault(value, message)
    if len(values) > 1:
        return MailFinding("ambiguous", want)
    if not values:
        return MailFinding("elsewhere", want, link_host=elsewhere[0]) if elsewhere else MailFinding("none", want)
    value, message = next(iter(values.items()))
    return MailFinding(
        "found", want, value, registrable_domain(message.sender.rpartition("@")[2]), message.received_at, site_host(value) if want == "link" else "", _digest(message)
    )


def check_offsets(wait_seconds: int) -> list[int]:
    """When to look, in seconds after the start: soon and often at first, then at a steady pace, never past the wait."""
    limit = max(0, int(wait_seconds))
    steady = range(POLL_FIRST[-1] + POLL_EVERY, limit + 1, POLL_EVERY)
    return [offset for offset in (*POLL_FIRST, *steady) if offset <= limit]


def next_check_at(started: datetime, looks_done: int, wait_seconds: int) -> datetime | None:
    """When the next look is due for a page that polls, or None when the wait is over and the owner is asked."""
    offsets = check_offsets(wait_seconds)
    return started + timedelta(seconds=offsets[looks_done]) if 0 <= looks_done < len(offsets) else None


def wait_for_verification(
    reader: MailReader,
    site: str,
    account_email: str,
    since: datetime,
    *,
    wait_seconds: int = 300,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = utcnow,
    **lookup: Any,
) -> MailFinding:
    """Look for a mail that may not have arrived yet, a bounded number of times with growing pauses.

    Every look is the same narrow lookup. A mail that is not there yet, or a passing failure, is looked for again;
    anything else ends the wait at once. When the time is up the answer is `timeout` and the owner is asked.
    """
    started = now()
    last = MailFinding("timeout")
    for looks, offset in enumerate(check_offsets(wait_seconds), start=1):
        pause = (started + timedelta(seconds=offset) - now()).total_seconds()
        if pause > 0:
            sleep(pause)
        last = find_verification(reader, site, account_email, since, now=now(), **lookup)
        if last.status not in ("none", "failed"):
            return MailFinding(last.status, last.kind, last.value, last.sender_domain, last.received_at, last.link_host, last.seen, looks)
    return MailFinding("timeout", last.kind, looks=len(check_offsets(wait_seconds)))


# -- the dashboard's side of one ask -------------------------------------------------------------------------
@dataclass(frozen=True)
class MailAnswer:
    """What the dashboard does with one look. `reply` goes to the waiting task; None keeps it waiting for the owner."""

    flash: str
    ok: bool
    reply: dict | None = None
    finding: MailFinding | None = None


def answer_mail_ask(ask: dict, reader: MailReader, app_secret: str, account_email: str, *, skip: Iterable[str] = (), now: datetime | None = None) -> MailAnswer:
    """Look once for the mail one ask is waiting for and seal what was found to the asking task.

    The ask is the task's: `step` is `mail_code` or `mail_link`, `host` is the sign-up site, `sent_at` is when the
    form was sent, `senders` are sender domains the site's page named. Anything but one clear value leaves the ask
    open, so the owner can type the code or open the link themselves. The value is never returned in the clear.
    """
    context = ask_context(ask)
    host = site_host(str(ask.get("host") or ""))
    step = str(ask.get("step") or "")
    if not host or step not in ("mail_code", "mail_link") or not public_is_ours(app_secret, str(ask.get("key") or ""), context, str(ask.get("key_sig") or "")):
        return MailAnswer("mail_mismatch", False, {"kind": "error", "error": "mismatch"})
    sent = ask.get("sent_at")
    try:
        since = sent if isinstance(sent, datetime) else datetime.fromisoformat(str(sent))
    except ValueError:
        return MailAnswer("mail_mismatch", False, {"kind": "error", "error": "mismatch"})
    since = since if since.tzinfo else since.replace(tzinfo=UTC)
    senders = [str(d) for d in ask.get("senders") or [] if isinstance(d, str)] if isinstance(ask.get("senders"), list) else []
    finding = find_verification(reader, host, account_email, since, want=step.removeprefix("mail_"), sender_domains=senders, skip=skip, now=now)
    if finding.status != "found":
        return MailAnswer(f"mail_{finding.status}", False, None, finding)
    try:
        box = seal(str(ask.get("key") or ""), {"host": host, finding.kind: finding.value}, context)
    except SealError:
        return MailAnswer("mail_mismatch", False, {"kind": "error", "error": "mismatch"})
    return MailAnswer(f"mail_{finding.kind}_found", True, {"kind": "box", "box": box}, finding)
