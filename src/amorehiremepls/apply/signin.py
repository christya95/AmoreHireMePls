"""The one sign-in step every job site gets.

A sign-in form is found by its structure: one password box, a username or email box before it, and a button to
press. No site is named here. The step runs only when the owner switched it on, and only inside an attempt that can
wait for the owner (a hold).

Order of work:
  1. wait for the form to render, a bounded time, before deciding what the page is
  2. ask the owner on the application page; their press is what makes the password manager show its own window
  3. open the sealed login, check the page is still the same site and a secure page, save the intent, then type
  4. give the page real time to settle, then read what it says: signed in, a code is wanted, the password was
     refused, the account is locked, a human check, or nothing clear
  5. a verification code is asked for on the application page and typed into one box or split boxes; a refused code
     is asked for again a fixed number of times
  6. on success the session is saved so later attempts skip the form

A refused password is never typed again by the app itself. The step keeps the page, shows the owner the site's own
words with a reset link and the sign-in link, and waits. Only the owner's press reads the saved login again, and a
saved login that has not changed is sent again only after a second, explicit confirmation on the dashboard. Refusals
are counted and capped, and a lock or a throttle ends the attempt with the wait the site named. The app never opens
or submits a reset flow. The secret is held in local variables only: it is never put on the evidence, in a note, in
an error, or in a log line.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from amorehiremepls.core.credentials import same_site, secure_page, site_host
from amorehiremepls.core.human_check import run_hold
from amorehiremepls.core.sealed import HandoffKey, SealError, sign_public
from amorehiremepls.core.site_signin import CODE_LABEL, REJECT_CYCLES, SignInMemory, ask_context, safe_link, stop_for
from amorehiremepls.core.util import new_id

SIGNIN_MARK = "data-amore-signin"
_REJECTED = re.compile(
    r"incorrect|invalid|wrong|not recogni[sz]ed|did(n.t| not) match|does(n.t| not) match|do(n.t| not) match|could(n.t| not) (find|sign|log)|"
    r"unable to (sign|log)|not (valid|correct)|try again|failed",
    re.I,
)
_LOCKED = re.compile(
    r"account (is |has been |was )?(temporarily )?(locked|disabled|suspended|blocked)|too many (failed |unsuccessful )?(attempts|tries|sign|log)|"
    r"temporarily (locked|disabled)|locked out|try again (later|in \d+)|wait \d+ (second|minute|hour)",
    re.I,
)
_WAIT = re.compile(r"(\d{1,4})\s*(second|sec|minute|min|hour|hr)s?\b", re.I)
_CODE_REJECTED = re.compile(r"incorrect|invalid|wrong|expired|not (valid|correct)|did(n.t| not) match|does(n.t| not) match|try again|failed", re.I)
_RESEND = re.compile(r"resend|send (me )?(a |the )?(new|another) code|send (it |the code )?again|new code", re.I)
_CODE_BUTTON = re.compile(r"verify|confirm|submit|continue|next|sign ?in|log ?in|done", re.I)
_VISIBLE = (
    "(el) => { const s = window.getComputedStyle(el); if (el.hidden || s.display === 'none' || s.visibility === 'hidden') return false;"
    " const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; }"
)
# Marks the username box, the password box and the button, and says what else the page offers.
_FORM_JS = """() => {
  const visible = __VISIBLE__;
  const MARK = '__MARK__';
  for (const el of document.querySelectorAll('[' + MARK + ']')) el.removeAttribute(MARK);
  const text = (el) => ((el.innerText || el.value || el.getAttribute('aria-label') || '') + '').replace(/\\s+/g, ' ').trim();
  const inputs = [...document.querySelectorAll('input')].filter(visible);
  const passwords = inputs.filter(el => el.type === 'password');
  const fresh = passwords.some(el => (el.getAttribute('autocomplete') || '').toLowerCase() === 'new-password');
  const hint = (el) => [el.name, el.id, el.getAttribute('autocomplete'), el.getAttribute('aria-label'), el.placeholder,
    ...(el.labels ? [...el.labels].map(l => l.innerText) : [])].join(' ').toLowerCase();
  const typed = (el) => ['text', 'email', 'tel', ''].includes(el.type || '');
  const userish = (el) => typed(el) && /user|e-?mail|login|identifier|account|member|sign.?in/.test(hint(el));
  const pass = passwords[0] || null;
  const root = pass && pass.form ? pass.form : document;
  const scoped = inputs.filter(el => typed(el) && (root === document || root.contains(el)));
  let user = null;
  if (pass) {
    const before = scoped.filter(el => el.compareDocumentPosition(pass) & Node.DOCUMENT_POSITION_FOLLOWING).reverse();
    user = before.find(userish) || before[0] || null;
  } else {
    const named = scoped.filter(userish);
    user = named.length === 1 && scoped.length <= 2 ? named[0] : null;
  }
  const up = /sign ?up|create (an |your |my |a new )?account|register|join now/i;
  const buttons = [...root.querySelectorAll('button, input[type=submit], [role=button]')].filter(el => visible(el) && !el.disabled);
  const submit = buttons.find(el => /sign ?in|log ?in/i.test(text(el)) && !up.test(text(el)))
    || buttons.find(el => /^(continue|next|submit|verify|enter)\\b/i.test(text(el)))
    || buttons.find(el => (el.getAttribute('type') || '').toLowerCase() === 'submit' && !up.test(text(el))) || null;
  const controls = [...document.querySelectorAll('a, button, [role=button], input[type=submit]')].filter(visible);
  if (user) user.setAttribute(MARK, 'user');
  if (pass) pass.setAttribute(MARK, 'pass');
  if (submit) submit.setAttribute(MARK, 'submit');
  return {
    passwords: passwords.length, fresh: fresh, user: !!user, submit: !!submit,
    signup: controls.some(el => up.test(text(el))),
    signin: controls.some(el => /sign ?in|log ?in/i.test(text(el)) && !up.test(text(el))),
    passkey: controls.some(el => /passkey|security key/i.test(text(el))),
  };
}""".replace("__VISIBLE__", _VISIBLE).replace("__MARK__", SIGNIN_MARK)
_STATE_JS = """() => {
  const visible = __VISIBLE__;
  const inputs = [...document.querySelectorAll('input')].filter(visible);
  return {passwords: inputs.filter(el => el.type === 'password').length, text: (document.body ? document.body.innerText : '').slice(0, 8000)};
}""".replace("__VISIBLE__", _VISIBLE)
# The site's own forgot or reset password link on the sign-in page. Only a real link is read; nothing is pressed.
_RESET_JS = """() => {
  const visible = __VISIBLE__;
  const want = /forgot|reset|change (your |my |the )?password|can.?t (sign|log) ?in|trouble (signing|logging)/i;
  const name = (el) => ((el.innerText || el.getAttribute('aria-label') || el.title || '') + '').replace(/\\s+/g, ' ').trim();
  const links = [...document.querySelectorAll('a[href]')].filter(visible);
  const found = links.find(el => want.test(name(el))) || links.find(el => want.test(el.getAttribute('href') || ''));
  return found ? found.href : '';
}""".replace("__VISIBLE__", _VISIBLE)
_BLANK_JS = "() => { for (const el of document.querySelectorAll('input[type=password]')) el.value = ''; }"


class SignInStop(Exception):
    """The sign-in ended without signing in. `code` names the reason; the message is the code and nothing else."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass
class SignInLimits:
    """Every wait and every repeat has a ceiling. Time spent waiting for the owner is not counted in `max_seconds`."""

    form_wait_ms: int = 10000  # a form that has not rendered yet
    settle_ms: int = 30000  # the page after the password or the code is sent
    stable_ms: int = 1500  # how long the page must stay quiet to count as signed in
    max_seconds: int = 240  # all page work for one sign-in
    code_attempts: int = 3  # how many codes may be typed
    resend_limit: int = 2  # how many times the site's own resend control may be pressed, each at the owner's request


@dataclass
class SignInTools:
    """What the browser adapter lends this step."""

    say: Callable[[str], None]
    wall_kind: Callable[[Any], str | None]
    challenge: Callable[[Any], dict | None]
    code_boxes: Callable[[Any], Any]
    type_code: Callable[[Any, str], None]
    save_session: Callable[[], bool]
    hide: Callable[[str], None]  # keeps a value out of the saved network rows
    cdp_port: int | None = None
    keep: Callable[[Any], None] = lambda page: None  # the stop record takes the page as it is, before boxes are emptied


@dataclass
class _Form:
    frame: Any
    info: dict


class _Clock:
    def __init__(self, seconds: float) -> None:
        self.limit = float(seconds)
        self.start = time.monotonic()
        self.waited = 0.0

    def restart(self) -> None:
        self.start, self.waited = time.monotonic(), 0.0

    def left(self) -> float:
        return self.limit - (time.monotonic() - self.start - self.waited)

    def check(self) -> None:
        if self.left() <= 0:
            raise SignInStop("too_long")


def wait_minutes(said: str) -> int:
    """How long the site said to wait, in minutes, or 0 when it did not say. Never more than a day."""
    found = _WAIT.search(said or "")
    if not found:
        return 0
    amount, unit = int(found.group(1)), found.group(2).lower()
    minutes = amount / 60 if unit.startswith("s") else amount * 60 if unit.startswith("h") else amount
    return max(1, min(int(math.ceil(minutes)), 1440))


class SiteSignIn:
    """One instance per attempt. It remembers which sites it already signed in to during that attempt."""

    def __init__(self, app_secret: str, memory: SignInMemory | None = None, limits: SignInLimits | None = None, job_id: str = "") -> None:
        self._secret = app_secret
        self.memory = memory
        self.limits = limits or SignInLimits()
        self.job_id = job_id
        self._done: set[str] = set()
        self._said = ""

    # -- entry ------------------------------------------------------------------------------------------
    def run(self, page: Any, hold: Any, tools: SignInTools, evidence: dict, start: str = "login", came_from: str = "") -> None:
        """Sign in on this page, or raise `SignInStop`. On return the page is past the sign-in.

        `came_from` is the address the attempt opened that led here. It is the link the owner is given back to this
        sign-in page, and the way a fresh copy of the form is opened before a second try."""
        host = site_host(page.url or "")
        record: dict[str, Any] = {"host": host, "outcome": "started", "codes_asked": 0}
        evidence["signin"] = record
        clock = _Clock(self.limits.max_seconds)
        attempt: str | None = None
        try:
            if host in self._done:
                raise SignInStop("again")
            if start == "code":
                self._codes(page, hold, tools, clock, record, host, offer=False, login_fingerprint="")
            else:
                attempt = self._password(page, hold, tools, clock, record, host, came_from)
            self._done.add(host)
            if self.memory is not None:
                self.memory.finish(attempt, "signed_in")
            record["outcome"] = "signed_in"
            record["session_saved"] = bool(tools.save_session())
            tools.say(f"Signed in to {host}." + (" The session is saved for next time." if record["session_saved"] else ""))
        except SignInStop as stop:
            record["outcome"] = stop.code
            record["retry_safe"] = stop_for(stop.code).retry_safe
            try:
                tools.keep(page)
            except Exception:
                pass
            self._blank(page)
            raise

    # -- password ---------------------------------------------------------------------------------------
    def _password(self, page: Any, hold: Any, tools: SignInTools, clock: _Clock, record: dict, host: str, came_from: str) -> str | None:
        form = self._wait_form(page, tools, clock)
        if not secure_page(form.frame.url or page.url or ""):
            raise SignInStop("not_secure")
        back = came_from or page.url or ""  # how the attempt reached this sign-in page
        refused = 0  # passwords this site refused during this attempt
        while True:
            login = self._ask_login(page, hold, tools, clock, host, fix=refused > 0)
            clock.restart()
            password = str(login.pop("password", "") or "")
            username = str(login.pop("username", "") or "")
            login_fingerprint = str(login.get("fingerprint") or "")
            if not password or site_host(str(login.get("host") or "")) != host:
                raise SignInStop("mismatch")
            tools.hide(password)
            if self.memory is not None and login_fingerprint:
                if barred := self.memory.blocked(host, login_fingerprint):
                    raise SignInStop(barred)
                if self.memory.rejections(host) >= REJECT_CYCLES:
                    raise SignInStop("cycle_cap")
            if refused:
                # A clean copy of the form, opened the way the attempt first reached it. Never a reload: that could send the refused form again.
                self._reopen(page, back)
            # The owner may have taken a while. Read the page again before anything is typed.
            form = self._wait_form(page, tools, clock)
            if site_host(page.url or "") != host or not same_site(site_host(form.frame.url or page.url or ""), host):
                raise SignInStop("moved")
            if not secure_page(form.frame.url or page.url or ""):
                raise SignInStop("not_secure")
            if not form.info.get("passwords"):
                form = self._username_first(page, tools, clock, form, username)
            elif form.info.get("user") and username:
                form.frame.locator(f'[{SIGNIN_MARK}="user"]').first.fill(username, timeout=5000)
            before = self._lines(page)
            attempt = self.memory.begin(host, login_fingerprint, self.job_id) if self.memory is not None else None
            self._done.add(host)
            tools.say(f"Signing in to {host} with the saved login.")
            box = form.frame.locator(f'[{SIGNIN_MARK}="pass"]').first
            box.fill(password, timeout=5000)
            self._press(form, box)
            outcome = self._settle(page, tools, clock, before, host, after="password")
            if outcome == "code":
                record["password"] = "accepted"
                self._codes(page, hold, tools, clock, record, host, offer=bool(login.get("has_code")), login_fingerprint=login_fingerprint, attempt=attempt)
                return attempt
            if outcome == "signed_in":
                return attempt
            if outcome not in ("rejected", "locked"):
                self._finish(attempt, outcome if outcome == "unclear" else "stopped")
                raise SignInStop(outcome)
            # The site said no. Keep what the owner needs to fix it; the secret and the username are cut out of the site's words.
            said = self._said
            for hidden in (password, username):
                said = said.replace(hidden, "[hidden]") if hidden else said
            password = ""
            facts = {
                "said": said[:300],
                "reset_url": self._reset_link(page, host, back),
                "signin_url": safe_link(back, site_host(back)),
                "item_title": str(login.get("title") or "")[:120],
            }
            wait = wait_minutes(said) if outcome == "locked" else 0
            record.update({name: value for name, value in facts.items() if value})
            if self.memory is not None:
                self.memory.finish(attempt, outcome, facts=facts, wait_minutes=wait)
            if outcome == "locked":
                record["wait_minutes"] = wait
                raise SignInStop("locked")
            refused += 1
            record["refused"] = refused
            if (self.memory.rejections(host) if self.memory is not None else refused) >= REJECT_CYCLES:
                raise SignInStop("cycle_cap")
            tools.say(f"{host} did not accept the saved password. Waiting for you to change it.")

    def _reopen(self, page: Any, url: str) -> None:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=15000)
        except Exception:
            raise SignInStop("no_form") from None

    def _reset_link(self, page: Any, host: str, back: str) -> str:
        """The site's own reset link, when it is a secure page of the same site. It is only read, never opened."""
        for frame in self._frames(page):
            try:
                found = safe_link(frame.evaluate(_RESET_JS), host, site_host(back))
            except Exception:
                continue
            if found:
                return found
        return ""

    def _username_first(self, page: Any, tools: SignInTools, clock: _Clock, form: _Form, username: str) -> _Form:
        """A page that asks for the username alone, then shows the password box."""
        if not (form.info.get("user") and form.info.get("submit") and username):
            raise SignInStop("no_form")
        field = form.frame.locator(f'[{SIGNIN_MARK}="user"]').first
        field.fill(username, timeout=5000)
        self._press(form, field)
        deadline = time.monotonic() + self.limits.form_wait_ms / 1000
        while True:
            clock.check()
            self._walls(page, tools)
            found = self._form(page)
            if found is not None and found.info.get("passwords") == 1:
                return found
            if time.monotonic() >= deadline:
                raise SignInStop("no_form")
            page.wait_for_timeout(400)

    def _press(self, form: _Form, field: Any) -> None:
        button = form.frame.locator(f'[{SIGNIN_MARK}="submit"]').first
        try:
            if button.count():
                button.click(timeout=5000)
                return
        except Exception:
            pass
        field.press("Enter")

    # -- the page ---------------------------------------------------------------------------------------
    def _frames(self, page: Any) -> list[Any]:
        """The page itself and frames of the same site. A frame from another site is never read or typed into."""
        top = site_host(page.url or "")
        try:
            frames = list(page.frames)
        except Exception:
            return []
        return [frame for index, frame in enumerate(frames) if index == 0 or same_site(site_host(frame.url or ""), top)]

    def _form(self, page: Any) -> _Form | None:
        first: _Form | None = None
        for frame in self._frames(page):
            try:
                info = frame.evaluate(_FORM_JS) or {}
            except Exception:
                continue
            found = _Form(frame, info)
            if info.get("passwords") or (info.get("user") and info.get("submit")):
                return found
            first = first or found
        return first

    def _walls(self, page: Any, tools: SignInTools) -> None:
        if tools.challenge(page):
            raise SignInStop("captcha")
        kind = tools.wall_kind(page)
        if kind in ("blocked", "checkpoint"):
            raise SignInStop(kind)

    def _wait_form(self, page: Any, tools: SignInTools, clock: _Clock) -> _Form:
        """A sign-in form may still be rendering. Look again with a growing pause before deciding what the page is."""
        deadline = time.monotonic() + self.limits.form_wait_ms / 1000
        pause, last, said = 200, None, False
        while True:
            clock.check()
            self._walls(page, tools)
            last = self._form(page)
            info = last.info if last is not None else {}
            if info.get("passwords") == 1 and not info.get("fresh"):
                return last
            if not info.get("passwords") and info.get("user") and info.get("submit"):
                return last
            if time.monotonic() >= deadline:
                break
            if not said and time.monotonic() > deadline - self.limits.form_wait_ms / 1000 + 3:
                said = True
                tools.say("Waiting for the sign-in form to appear.")
            page.wait_for_timeout(pause)
            pause = min(pause * 2, 1000)
        info = last.info if last is not None else {}
        if info.get("passwords", 0) >= 2 or info.get("fresh") or (info.get("signup") and not info.get("signin") and not info.get("passwords")):
            raise SignInStop("account_needed")
        if info.get("passkey"):
            raise SignInStop("passkey")
        raise SignInStop("no_form")

    def _lines(self, page: Any) -> set[str]:
        return {line for line in self._state(page)["text"].splitlines() if line.strip()}

    def _state(self, page: Any) -> dict:
        passwords, text = 0, ""
        for frame in self._frames(page):
            try:
                got = frame.evaluate(_STATE_JS) or {}
            except Exception:
                return {"passwords": 0, "text": "", "busy": True}
            passwords += int(got.get("passwords") or 0)
            text += "\n" + str(got.get("text") or "")
        return {"passwords": passwords, "text": text, "busy": False}

    def _settle(self, page: Any, tools: SignInTools, clock: _Clock, before: set[str], host: str, after: str) -> str:
        """Read the page until it says something clear. A redirect chain gets time; a quiet page counts as signed in."""
        deadline = time.monotonic() + min(self.limits.settle_ms / 1000, max(clock.left(), 1))
        started = time.monotonic()
        quiet_since: float | None = None
        last_url, said, checkpoint_seen, boxes = "", False, 0, False
        while True:
            page.wait_for_timeout(300)
            state = self._state(page)
            url = page.url or ""
            if not state.get("busy"):
                if tools.challenge(page):
                    return "captcha"
                kind = tools.wall_kind(page)
                if kind == "blocked":
                    return "blocked"
                new_lines = [" ".join(line.split()) for line in state["text"].splitlines() if line.strip() and line not in before]
                fresh = "\n".join(new_lines)
                boxes = tools.code_boxes(page) is not None
                if _LOCKED.search(fresh):
                    self._said = " ".join(line for line in new_lines if _LOCKED.search(line) or _WAIT.search(line))[:300]
                    return "locked"
                if after == "password":
                    if boxes and not state["passwords"]:
                        return "code"
                    # The form is still showing and the page now says no. A page that is only slow says nothing new.
                    if state["passwords"] and _REJECTED.search(fresh):
                        self._said = " ".join(line for line in new_lines if _REJECTED.search(line))[:300]
                        return "rejected"
                elif boxes and _CODE_REJECTED.search(fresh):
                    return "code_rejected"
                # A verify-it's-you page may draw its code box a moment later, so it must be seen a few times first.
                checkpoint_seen = checkpoint_seen + 1 if kind == "checkpoint" else 0
                if checkpoint_seen >= 5:
                    return "checkpoint"
                quiet = not state["passwords"] and not boxes and kind is None and url == last_url
                if quiet:
                    quiet_since = quiet_since or time.monotonic()
                    if (time.monotonic() - quiet_since) * 1000 >= self.limits.stable_ms:
                        return "signed_in"
                else:
                    quiet_since = None
            else:
                quiet_since = None
            last_url = url
            if time.monotonic() >= deadline:
                # A code box that is still there after the whole wait did not take the code.
                return "code_rejected" if after == "code" and boxes else "unclear"
            if not said and time.monotonic() - started > 5:
                said = True
                tools.say(f"Waiting for {host} to finish signing in.")

    def _blank(self, page: Any) -> None:
        """Empty every password box before the page is photographed or left."""
        for frame in self._frames(page):
            try:
                frame.evaluate(_BLANK_JS)
            except Exception:
                continue

    # -- asking the owner -------------------------------------------------------------------------------
    def _hold(self, page: Any, hold: Any, tools: SignInTools, clock: _Clock, fields: dict) -> str:
        began = time.monotonic()
        try:
            return run_hold(
                minutes=hold.minutes,
                poll_seconds=hold.poll_seconds,
                sleep=lambda seconds: page.wait_for_timeout(int(seconds * 1000)),
                heartbeat=lambda: hold.heartbeat({"cdp_port": tools.cdp_port, **fields}),
                continue_requested=hold.continue_requested,
                receipt_visible=lambda: False,
            )
        finally:
            clock.waited += time.monotonic() - began

    def _new_ask(self, host: str, step: str, **extra: Any) -> tuple[HandoffKey, dict]:
        key = HandoffKey()
        ask = {"ask_id": new_id("ask"), "host": host, "step": step, "key": key.public, **extra}
        ask["key_sig"] = sign_public(self._secret, key.public, ask_context(ask))
        return key, ask

    def _reply(self, hold: Any) -> dict | None:
        reader = getattr(hold, "reply", None)
        got = reader() if callable(reader) else None
        return got if isinstance(got, dict) else None

    def _ask_login(self, page: Any, hold: Any, tools: SignInTools, clock: _Clock, host: str, fix: bool = False) -> dict:
        """Wait for the owner to allow the lookup. Returns the opened login; the caller keeps it in local variables.

        `fix` marks an ask that follows a refused password in this attempt: the page then shows the fix-it links and
        the button reads as a retry. If the owner does not come back, the application stays at the refusal."""
        key, ask = self._new_ask(host, "login", fix=fix)
        if not fix:
            tools.say(f"{host} asks you to sign in. Waiting for you to allow 1Password.")
        outcome = self._hold(page, hold, tools, clock, {"purpose": "signin", "signin": ask})
        if outcome != "continue":
            hold.finish("timeout")
            raise SignInStop("rejected" if fix else "not_approved")
        reply = self._reply(hold)
        hold.finish("continued")
        if reply is None or reply.get("kind") == "declined":
            raise SignInStop("rejected" if fix else "declined")
        if reply.get("kind") != "box":
            raise SignInStop(str(reply.get("error") or "reader_failed"))
        try:
            login = key.open(reply.get("box"), ask_context(ask))
        except SealError:
            raise SignInStop("mismatch") from None
        tools.say(f"1Password was approved. A saved login for {host} was found.")
        return login

    # -- verification code ------------------------------------------------------------------------------
    def _codes(self, page: Any, hold: Any, tools: SignInTools, clock: _Clock, record: dict, host: str, offer: bool, login_fingerprint: str, attempt: str | None = None) -> None:
        """Ask for the code, type it, read the result. A refused code is asked for again up to the ceiling."""
        reason = f"{host} asked for a verification code. Type the code from your authenticator app, text message or email."
        for number in range(1, self.limits.code_attempts + 1):
            clock.check()
            boxes = tools.code_boxes(page)
            if boxes is None:
                break
            tools.say(
                f"{host} asked for a verification code. Waiting for you to type it."
                if number == 1
                else f"The last code was not accepted. Asking again ({number} of {self.limits.code_attempts})."
            )
            record["codes_asked"] = number
            code, saved = self._ask_code(page, hold, tools, clock, host, reason, offer, login_fingerprint)
            if not code:
                self._finish(attempt, "code_failed")
                raise SignInStop("code_timeout")
            tools.hide(code)
            tools.say("Typing the one-time code from 1Password." if saved else "Typing the verification code.")
            if saved:
                record["saved_code_used"] = True
            boxes = tools.code_boxes(page)
            if boxes is None:
                break
            before = self._lines(page)
            tools.type_code(boxes, code)
            code = ""
            page.wait_for_timeout(400)
            self._press_code(boxes)
            outcome = self._settle(page, tools, clock, before, host, after="code")
            if outcome == "signed_in":
                return
            if outcome != "code_rejected":
                self._finish(attempt, "code_failed")
                raise SignInStop(outcome)
            reason = f"The last code was not accepted. Type a new one. This is try {number + 1} of {self.limits.code_attempts}."
        else:
            self._finish(attempt, "code_failed")
            raise SignInStop("code_rejected")
        # The code box went away on its own. Let the page settle and read it once more.
        outcome = self._settle(page, tools, clock, self._lines(page), host, after="code")
        if outcome != "signed_in":
            self._finish(attempt, "code_failed")
            raise SignInStop(outcome)

    def _finish(self, attempt: str | None, status: str) -> None:
        if self.memory is not None:
            self.memory.finish(attempt, status)

    def _ask_code(self, page: Any, hold: Any, tools: SignInTools, clock: _Clock, host: str, reason: str, offer: bool, login_fingerprint: str) -> tuple[str, bool]:
        """One code from the owner: typed on the application page, or read from the saved login at their press.

        Returns the code and whether it came from the saved login. An empty code means the wait ended.
        """
        resends = 0
        while True:
            can_resend = resends < self.limits.resend_limit and self._resend_control(page) is not None
            key, ask = self._new_ask(host, "code", offer_code=bool(offer), fingerprint=login_fingerprint, resend=can_resend)
            fields = {"purpose": "field", "field_label": CODE_LABEL, "field_reason": reason, "sensitive": False, "signin": ask}
            outcome = self._hold(page, hold, tools, clock, fields)
            if outcome != "continue":
                hold.finish("timeout")
                return "", False
            reply = self._reply(hold)
            typed = re.sub(r"\s+", "", str(getattr(hold, "supplied_answer", lambda: "")() or ""))
            forget = getattr(hold, "forget_answer", None)
            if callable(forget):
                forget()
            hold.finish("continued")
            if reply and reply.get("kind") == "box":
                try:
                    return re.sub(r"\s+", "", str(key.open(reply.get("box"), ask_context(ask)).get("code") or "")), True
                except SealError:
                    raise SignInStop("mismatch") from None
            if reply and reply.get("kind") == "resend" and can_resend:
                control = self._resend_control(page)
                if control is not None:
                    try:
                        control.click(timeout=5000)
                        resends += 1
                        tools.say(f"Asked {host} to send a new code, as you requested.")
                        reason = "A new code was requested. Type it here when it arrives."
                        page.wait_for_timeout(600)
                    except Exception:
                        pass
                continue
            return typed, False

    def _resend_control(self, page: Any) -> Any:
        for frame in self._frames(page):
            for role in ("button", "link"):
                try:
                    found = frame.get_by_role(role, name=_RESEND)
                    if found.count() and found.first.is_visible():
                        return found.first
                except Exception:
                    continue
        return None

    def _press_code(self, boxes: Any) -> None:
        """The button in the same form as the code box, else Enter in the box."""
        first = boxes.first
        try:
            buttons = first.locator("xpath=ancestor::form[1]").locator("button, input[type=submit]")
            for index in range(min(buttons.count(), 8)):
                button = buttons.nth(index)
                name = " ".join((button.inner_text() or button.get_attribute("value") or "").split())
                if button.is_visible() and _CODE_BUTTON.search(name) and not _RESEND.search(name):
                    button.click(timeout=5000)
                    return
        except Exception:
            pass
        try:
            first.press("Enter")
        except Exception:
            pass
