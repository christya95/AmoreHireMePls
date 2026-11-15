"""Create one job-site account during an application, then continue that same run into the form.

Called only when sign-in ended with no saved login, or the page only offers sign up, and the owner has turned
account creation on. The owner approves on the application page. The login is saved in 1Password before the form
is sent. The form is sent once per approval, through `ActionStore.execute`. A replay, a second worker, or an
outcome that cannot be read does not send it again.

The password and any emailed code or link travel sealed, the same way a saved login does. This module never
writes them down. `tools.hide` keeps them out of the network list. The stop record is a separate, unmasked
archive the owner asked for; it can still hold a submitted password or code.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from amorehiremepls.apply.signup import PAGE_JS, SIGNUP_MARK, page_lines, plan_signup, profile_values, read_page, signup_control
from amorehiremepls.core.actions import ActionFailed, AmbiguousOutcome
from amorehiremepls.core.credentials import registrable_domain, secure_page, site_host
from amorehiremepls.core.human_check import run_hold
from amorehiremepls.core.sealed import HandoffKey, SealError, sign_public
from amorehiremepls.core.site_accounts import AccountStore, offer_hash
from amorehiremepls.core.site_signin import ask_context, safe_link
from amorehiremepls.core.util import new_id, utcnow

_ACCEPTED = ("email_code", "code_prompt", "email_link")
_STOPS = {
    "captcha": "signup_captcha",
    "phone_verification": "signup_phone",
    "blocked": "signup_blocked",
    "email_exists": "signup_email_exists",
    "password_rules": "signup_password_rules",
    "too_many_questions": "signup_questions",
    "email_first": "signup_not_form",
    "social_only": "signup_not_form",
    "not_signup": "signup_not_form",
    "no_submit": "signup_not_form",
    "not_secure": "signup_not_secure",
    "no_email": "signup_no_email",
}
_MAIL_TYPE = {"mail_off": "off", "mail_locked": "locked", "mail_reconnect": "reconnect", "mail_none": "off", "mail_timeout": "off", "mail_failed": "off"}
_RESEND = re.compile(r"resend|send (the |a )?(code|link) again|new code|send again", re.I)
_RESET = re.compile(r"reset|forgot|sign in|log in", re.I)


class SignUpStop(Exception):
    """The account was not created, or the result could not be read. Nothing here is a secret."""

    def __init__(self, code: str, reset_url: str = "") -> None:
        self.code = code
        self.reset_url = reset_url
        super().__init__(code)


class SiteSignUp:
    """One attempt. It remembers nothing that is a secret once the page has it."""

    def __init__(self, rt: Any, job: dict, application_id: str, worker: str) -> None:
        self.rt = rt
        self.job = job or {}
        self.job_id = str(self.job.get("_id") or "")
        self.application_id = application_id
        self.worker = worker or "account"
        self._secret = rt.settings.app_secret.get_secret_value()
        self.accounts = AccountStore(rt.store)

    def run(self, page: Any, hold: Any, tools: Any, evidence: dict, why: str) -> None:
        """Return with the page signed in, or raise `SignUpStop`. The caller then continues the application."""
        host = site_host(page.url or "")
        record = evidence.setdefault("account", {"host": host})
        record["why"] = why
        self._open_form(page, why)
        if not secure_page(page.url or ""):
            raise SignUpStop("signup_not_secure")
        host = site_host(page.url or "")
        email = self.rt.account_email()
        if "@" not in (email or ""):
            raise SignUpStop("signup_no_email")
        key = self.accounts.key_for(host, email)
        row = self.accounts.open(key, host, page.url or "", self.job_id, self.application_id)
        action = self.rt.actions.by_key(key)
        status = (action or {}).get("status")
        if status in ("succeeded", "needs_verification", "executing"):
            self._resume(page, hold, tools, evidence, row, action)
            return
        if status == "failed":
            self._from_failure(page, hold, tools, evidence, row, action)
            return
        self._approve_and_send(page, hold, tools, evidence, row, host, password_step=False)

    # -- the form ----------------------------------------------------------------------------------------
    def _open_form(self, page: Any, why: str) -> None:
        description = self._describe(page)
        kind = read_page(description).kind
        if kind == "signup_form" or kind in _STOPS:
            return
        control = signup_control(description)
        if control is None:
            raise SignUpStop("signup_not_form")
        if control.get("href"):
            page.goto(control["href"], wait_until="domcontentloaded", timeout=15000)
        elif control.get("key"):
            page.locator(f'[{SIGNUP_MARK}="{control["key"]}"]').first.click(timeout=5000)
            page.wait_for_timeout(400)
        else:
            raise SignUpStop("signup_not_form")

    def _describe(self, page: Any) -> dict:
        try:
            found = page.evaluate(PAGE_JS) or {}
        except Exception:
            raise SignUpStop("signup_not_form") from None
        return found if isinstance(found, dict) else {}

    def _plan(self, page: Any, answers: dict) -> Any:
        description = self._describe(page)
        seen = read_page(description, account_email=self.rt.account_email())
        if seen.kind == "password_rejected":
            description = {**description, "alerts": []}
        if seen.kind in _STOPS and seen.kind != "signup_form":
            if seen.stop or seen.kind in ("captcha", "phone_verification", "blocked", "email_exists"):
                raise SignUpStop(_STOPS.get(seen.kind, "signup_not_form"), self._reset(description, seen.host))
        plan = plan_signup(description, profile_values(self.rt.profile), self.rt.account_email(), answers)
        if plan.state == "stop":
            raise SignUpStop(_STOPS.get(plan.stop, "signup_not_form"), self._reset(description, plan.host))
        if any(question.why == "security_question" for question in plan.questions):
            # A concealed answer would have to be written into 1Password. That edit was not verified, so the owner does this one.
            raise SignUpStop("signup_security")
        return plan

    def _approve_and_send(self, page: Any, hold: Any, tools: Any, evidence: dict, row: dict, host: str, password_step: bool) -> None:
        answers = dict(row.get("answers") or {})
        while True:
            plan = self._plan(page, answers)
            made = int(row.get("passwords_made") or 0)
            number = made + 1 if password_step or made == 0 else made
            offer = offer_hash(plan.material(), row["key"], number)
            step = "account_ask" if plan.state == "ask" else ("password" if password_step else "account")
            self.accounts.offer(row["_id"], plan.card(), offer, plan.rules, number, step, changed=bool(row.get("approved")) and row.get("approved") != offer)
            tools.say(f"Waiting for you to approve an account on {host}.")
            key, reply = self._ask(page, hold, tools, host, step, extra={"offer": offer, "account": row["_id"]})
            row = self.accounts.get(row["_id"]) or row
            kind = str(reply.get("kind") or "")
            if kind == "declined":
                raise SignUpStop("signup_declined")
            if kind == "answers":
                answers = {str(k): str(v) for k, v in (row.get("answers") or {}).items()}
                continue
            if kind == "error":
                codes = {"login_exists": "signup_login_exists", "password_limit": "signup_password_limit", "password_rules": "signup_password_rules"}
                raise SignUpStop(codes.get(str(reply.get("error")), "signup_not_form"))
            if kind != "box":
                raise SignUpStop("signup_not_form")
            login = self._open_box(key, reply, host, step)
            fresh = self._plan(page, answers)
            now_offer = offer_hash(fresh.material(), row["key"], number)
            if now_offer != offer or row.get("approved") != offer:
                continue  # the page changed: that approval does not cover a send
            outcome = self._send(page, tools, fresh, login, row, offer)
            login = {}
            self._finish_send(page, hold, tools, evidence, row, fresh, outcome)
            return

    def _open_box(self, key: HandoffKey, reply: dict, host: str, step: str) -> dict:
        ask_id = str(reply.get("ask_id") or "")
        try:
            opened = key.open(reply.get("box"), ask_context({"ask_id": ask_id, "host": host, "step": step}))
        except SealError:
            raise SignUpStop("signup_not_form") from None
        return opened if isinstance(opened, dict) else {}

    def _send(self, page: Any, tools: Any, plan: Any, login: dict, row: dict, offer: str) -> Any:
        action = self.rt.actions.by_key(row["key"])
        if not action or action.get("status") != "ready" or action.get("material_hash") != offer:
            current = action or {}
            if current.get("status") in ("succeeded", "needs_verification", "executing", "failed"):
                return SimpleNamespace(status=current.get("status"), evidence=current.get("evidence") or {})
            raise SignUpStop("signup_unclear")
        password = str(login.get("password") or "")
        tools.hide(password)
        before = page_lines(self._describe(page))

        def adapter(_action: dict) -> dict:
            if not self.accounts.claim_send(row["_id"], offer):
                raise AmbiguousOutcome("the sign-up form was already sent", {"signup": "unclear"})
            self._type(page, plan, password)
            self._press(page, plan)
            kind, reset = self._outcome(page, before)
            if kind in _ACCEPTED or kind == "signed_in":
                return {"verified": True, "signup": kind}
            if kind == "password_rejected":
                raise ActionFailed("the site refused the password", {"signup": kind}, retryable=False)
            if kind == "email_exists":
                raise ActionFailed("the email already has an account", {"signup": kind, "reset_url": reset}, retryable=False)
            raise AmbiguousOutcome("the sign-up result could not be read", {"signup": kind or "unclear", "reset_url": reset})

        outcome = self.rt.actions.execute(action["_id"], self.worker, adapter)
        password = ""
        return outcome

    def _type(self, page: Any, plan: Any, password: str) -> None:
        for item in plan.typed:
            value = password if item.role in ("password", "password_confirm") else item.value
            if not value:
                continue
            page.locator(f'[{SIGNUP_MARK}="{item.key}"]').first.fill(value, timeout=5000)
        for box in plan.boxes:
            loc = page.locator(f'[{SIGNUP_MARK}="{box.key}"]').first
            try:
                checked = loc.is_checked()
            except Exception:
                continue
            if box.tick and not checked:
                loc.check(timeout=5000)
            elif not box.tick and checked:
                loc.uncheck(timeout=5000)

    def _press(self, page: Any, plan: Any) -> None:
        page.locator(f'[{SIGNUP_MARK}="{plan.submit}"]').first.click(timeout=5000)
        page.wait_for_timeout(400)

    def _outcome(self, page: Any, before: set[str]) -> tuple[str, str]:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        description = self._describe(page)
        seen = read_page(description, before=before, account_email=self.rt.account_email())
        if seen.kind == "signup_form" and not before:
            return "unclear", ""
        if seen.kind == "signup_form":
            return "form_refused", self._reset(description, seen.host)
        if seen.kind == "signin_form":
            return "signed_in", ""
        if seen.kind == "unknown" and not page.locator("input[type=password]").count():
            text = str(description.get("text") or "").strip()
            if len(text) < 12 and not page.locator("form, input, button, a").count():
                return "unclear", ""
            return "signed_in", ""
        return seen.kind, self._reset(description, seen.host)

    def _finish_send(self, page: Any, hold: Any, tools: Any, evidence: dict, row: dict, plan: Any, outcome: Any) -> None:
        status, evidence_out = outcome.status, outcome.evidence or {}
        kind = str(evidence_out.get("signup") or "")
        row = self.accounts.get(row["_id"]) or row
        if status == "succeeded" and kind in _ACCEPTED:
            self.accounts.update(row["_id"], status="awaiting_email", sent_at=utcnow())
            self._verify(page, hold, tools, evidence, self.accounts.get(row["_id"]) or row, kind)
            return
        if status == "succeeded":
            self._confirm(page, tools, evidence, row)
            return
        if status == "failed" and kind == "password_rejected":
            made = int(row.get("passwords_made") or 0)
            self.accounts.update(row["_id"], status="password_rejected", said=str(plan.message or "")[:300])
            if made >= int(self.rt.settings.site_account_password_tries):
                raise SignUpStop("signup_password_limit")
            host = site_host(page.url or "")
            self._approve_and_send(page, hold, tools, evidence, self.accounts.get(row["_id"]) or row, host, password_step=True)
            return
        if status == "failed" and kind == "email_exists":
            self.accounts.update(row["_id"], status="exists", item_pending="not_created")
            raise SignUpStop("signup_email_exists", str(evidence_out.get("reset_url") or ""))
        self._unclear(row)
        raise SignUpStop("signup_unclear", str(evidence_out.get("reset_url") or ""))

    def _from_failure(self, page: Any, hold: Any, tools: Any, evidence: dict, row: dict, action: dict) -> None:
        kind = str((action.get("evidence") or {}).get("signup") or "")
        if kind == "password_rejected" and row.get("status") == "password_rejected":
            self._approve_and_send(page, hold, tools, evidence, row, site_host(page.url or ""), password_step=True)
            return
        if kind == "email_exists" or row.get("status") == "exists":
            raise SignUpStop("signup_email_exists", str((action.get("evidence") or {}).get("reset_url") or ""))
        self._unclear(row)
        raise SignUpStop("signup_unclear")

    def _resume(self, page: Any, hold: Any, tools: Any, evidence: dict, row: dict, action: dict) -> None:
        if action.get("status") != "succeeded":
            self._unclear(row)
            raise SignUpStop("signup_unclear")
        description = self._describe(page)
        seen = read_page(description, account_email=self.rt.account_email())
        if seen.kind in _ACCEPTED:
            self._verify(page, hold, tools, evidence, row, seen.kind)
            return
        if seen.kind in ("captcha", "phone_verification", "blocked"):
            raise SignUpStop(_STOPS[seen.kind])
        self._confirm(page, tools, evidence, row)

    # -- verification ------------------------------------------------------------------------------------
    def _verify(self, page: Any, hold: Any, tools: Any, evidence: dict, row: dict, kind: str) -> None:
        host = site_host(page.url or "")
        step = "mail_link" if kind == "email_link" else "mail_code"
        sent = row.get("sent_at") or utcnow()
        if not isinstance(sent, datetime):
            sent = utcnow()
        if sent.tzinfo is None:
            sent = sent.replace(tzinfo=UTC)
        description = self._describe(page)
        senders = ",".join(read_page(description, account_email=self.rt.account_email()).senders)
        resend_left = self._has_resend(description)
        tries = 0
        type_it = False
        mail_why = ""
        next_check = ""
        while tries < 3:
            extra = {
                "offer": str(row.get("offer") or ""),
                "account": row["_id"],
                "sent_at": sent.isoformat(),
                "senders": senders,
                "resend": resend_left,
                "type_it": type_it,
                "mail_why": mail_why,
                "next_check_at": next_check,
            }
            key, reply = self._ask(page, hold, tools, host, step, extra=extra)
            reply_kind = str(reply.get("kind") or "")
            if reply_kind == "resend" and resend_left:
                self._click_resend(page)
                resend_left = False
                sent = utcnow()
                self.accounts.update(row["_id"], sent_at=sent)
                continue
            if reply_kind == "wait":
                next_check = str(reply.get("next_check_at") or "")
                continue
            if reply_kind == "error" and str(reply.get("error") or "") in _MAIL_TYPE:
                type_it, mail_why = True, _MAIL_TYPE[str(reply.get("error"))]
                continue
            if reply_kind != "box":
                raise SignUpStop("signup_unclear")
            opened = self._open_box(key, reply, host, step)
            value = str(opened.get("link") or opened.get("code") or "")
            tools.hide(value)
            tries += 1
            if step == "mail_link":
                if not value or registrable_domain(value) != registrable_domain(host) or not secure_page(value):
                    raise SignUpStop("signup_link_elsewhere")
                page.goto(value, wait_until="domcontentloaded", timeout=15000)
            else:
                self._type_code(page, value)
            value = ""
            if self._past_prompt(page):
                self._confirm(page, tools, evidence, row)
                return
        self._unclear(row)
        raise SignUpStop("signup_unclear")

    def _past_prompt(self, page: Any) -> bool:
        seen = read_page(self._describe(page), account_email=self.rt.account_email())
        return seen.kind not in ("email_code", "code_prompt", "email_link", "captcha", "phone_verification", "blocked")

    def _type_code(self, page: Any, code: str) -> None:
        description = self._describe(page)
        boxes = read_page(description, account_email=self.rt.account_email()).code_boxes
        if len(boxes) > 1:
            for box, char in zip(boxes, code, strict=False):
                page.locator(f'[{SIGNUP_MARK}="{box}"]').first.fill(char, timeout=5000)
        elif boxes:
            page.locator(f'[{SIGNUP_MARK}="{boxes[0]}"]').first.fill(code, timeout=5000)
        onward = re.compile(r"verif|confirm|continue|submit", re.I)
        buttons = [item for item in description.get("buttons") or [] if _RESEND.search(str(item.get("text") or "")) is None]
        button = next((item for item in buttons if onward.search(str(item.get("text") or ""))), None)
        if button and button.get("key"):
            page.locator(f'[{SIGNUP_MARK}="{button["key"]}"]').first.click(timeout=5000)
        page.wait_for_timeout(400)

    def _has_resend(self, description: dict) -> bool:
        return any(_RESEND.search(str(item.get("text") or "")) for item in description.get("buttons") or [] if isinstance(item, dict))

    def _click_resend(self, page: Any) -> None:
        description = self._describe(page)
        button = next((item for item in description.get("buttons") or [] if isinstance(item, dict) and _RESEND.search(str(item.get("text") or ""))), None)
        if button and button.get("key"):
            page.locator(f'[{SIGNUP_MARK}="{button["key"]}"]').first.click(timeout=5000)
            page.wait_for_timeout(400)

    def _confirm(self, page: Any, tools: Any, evidence: dict, row: dict) -> None:
        self._blank(page)
        saved = False
        try:
            saved = bool(tools.save_session())
        except Exception:
            saved = False
        self.accounts.update(row["_id"], status="confirmed", item_pending="confirmed", confirmed_at=utcnow())
        evidence.setdefault("account", {})["outcome"] = "confirmed"
        signin = evidence.setdefault("signin", {})
        signin["outcome"] = "signed_in"
        signin["session_saved"] = saved
        host = site_host(page.url or "")
        tools.say(f"The account on {host} is created." + (" The session is saved for next time." if saved else ""))

    def _unclear(self, row: dict) -> None:
        self.accounts.update(row["_id"], status="unclear", item_pending="unclear")

    def _blank(self, page: Any) -> None:
        try:
            page.evaluate("() => { document.querySelectorAll('input[type=password]').forEach((el) => { el.value = ''; }); }")
        except Exception:
            return

    def _reset(self, description: dict, host: str) -> str:
        for link in description.get("links") or []:
            if isinstance(link, dict) and _RESET.search(str(link.get("text") or "")):
                found = safe_link(link.get("href"), host)
                if found:
                    return found
        return ""

    # -- the hold ----------------------------------------------------------------------------------------
    def _ask(self, page: Any, hold: Any, tools: Any, host: str, step: str, extra: dict) -> tuple[HandoffKey, dict]:
        key = HandoffKey()
        ask = {"ask_id": new_id("ask"), "host": host, "step": step, "key": key.public, **extra}
        ask["key_sig"] = sign_public(self._secret, key.public, ask_context(ask))
        outcome = run_hold(
            minutes=hold.minutes,
            poll_seconds=hold.poll_seconds,
            sleep=lambda seconds: page.wait_for_timeout(int(seconds * 1000)),
            heartbeat=lambda: hold.heartbeat({"purpose": "account", "signin": ask}),
            continue_requested=hold.continue_requested,
            receipt_visible=lambda: False,
        )
        if outcome != "continue":
            hold.finish("timeout")
            raise SignUpStop("not_approved")
        reply = hold.reply()
        hold.finish("continued")
        if not isinstance(reply, dict):
            raise SignUpStop("signup_declined")
        reply = {**reply, "ask_id": ask["ask_id"]}
        return key, reply
