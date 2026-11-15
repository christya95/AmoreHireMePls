"""The owner's presses for creating a job-site account.

The press is the authorization. 1Password and the mailbox are touched only here, on the dashboard the owner is
using. A password, a code or a link leaves this process only inside a sealed box for the waiting task.
"""

from __future__ import annotations

from datetime import UTC, datetime

from amorehiremepls.core.human_check import note_missed_approval, reply_to_signin, signin_hold
from amorehiremepls.core.mail_codes import GmailReader, answer_mail_ask, next_check_at
from amorehiremepls.core.sealed import seal
from amorehiremepls.core.site_accounts import AccountStore, apply_item_mark, approve, resolve_unclear
from amorehiremepls.core.site_signin import ask_context
from amorehiremepls.web.chrome import Depends, Form, HTTPException, RedirectResponse, Request


def register(app, rt, require_session, require_csrf) -> None:
    def _back(app_id: str, flash: str, ok: bool) -> RedirectResponse:
        return RedirectResponse(f"/applications/{app_id}?{'ok' if ok else 'err'}={flash}", status_code=303)

    def _open(app_id: str, material_hash: str):
        found = rt.store.c("applications").find_one({"_id": app_id})
        if not found or found.get("material_hash") != material_hash:
            raise HTTPException(409, "package changed; refresh and review again")
        hold, state = signin_hold(rt.store, found)
        if hold is None or hold.get("purpose") != "account":
            return found, None, state
        return found, hold, state

    @app.post("/applications/{app_id}/account/allow")
    async def account_allow(request: Request, app_id: str, session: dict = Depends(require_session), csrf: str = Form(...), material_hash: str = Form(...), offer: str = Form("")):
        """Create the login, or save the answers on the card. This press is what opens 1Password when a login is saved."""
        require_csrf(request, session, csrf)
        found, hold, state = _open(app_id, material_hash)
        if hold is None:
            return _back(app_id, "account_missing", False)
        ask = hold["signin"]
        form = await request.form()
        row = AccountStore(rt.store).get(str(ask.get("account") or ""))
        if row is None:
            return _back(app_id, "account_missing", False)
        if ask.get("step") == "account_ask":
            answers = {str(key)[2:]: str(value) for key, value in form.items() if str(key).startswith("q_") and str(value).strip()}
            if not answers:
                return _back(app_id, "account_answers_needed", False)
            AccountStore(rt.store).remember_answers(row["_id"], answers)
            if not reply_to_signin(rt.store, hold, {"kind": "answers"}):
                return _back(app_id, "account_missing", False)
            return _back(app_id, "account_answers_saved", True)
        job = rt.jobs.get(found.get("job_id")) if found.get("job_id") else None
        press = approve(rt, ask, row, job, offer)
        rt.store.audit("site_account_asked", app_id, host=ask.get("host"), step=ask.get("step"), result=press.flash)
        if press.missed:
            note_missed_approval(rt.store, hold)
            return _back(app_id, press.flash, False)
        if press.reply is not None and not reply_to_signin(rt.store, hold, press.reply):
            return _back(app_id, "account_missing", False)
        return _back(app_id, press.flash, press.ok)

    @app.post("/applications/{app_id}/account/decline")
    def account_decline(request: Request, app_id: str, session: dict = Depends(require_session), csrf: str = Form(...), material_hash: str = Form(...)):
        require_csrf(request, session, csrf)
        _found, hold, _state = _open(app_id, material_hash)
        if hold is None or hold["signin"].get("step") not in ("account", "account_ask", "password"):
            return _back(app_id, "account_missing", False)
        reply_to_signin(rt.store, hold, {"kind": "declined"})
        return _back(app_id, "account_declined", True)

    @app.post("/applications/{app_id}/account/code")
    def account_code(request: Request, app_id: str, session: dict = Depends(require_session), csrf: str = Form(...), material_hash: str = Form(...), value: str = Form("")):
        """The owner types a code or pastes a link. It is sealed to the waiting task and not kept on the page."""
        require_csrf(request, session, csrf)
        _found, hold, _state = _open(app_id, material_hash)
        if hold is None or hold["signin"].get("step") not in ("mail_code", "mail_link"):
            return _back(app_id, "account_missing", False)
        text = " ".join(str(value or "").split())
        if not text or len(text) > 2000:
            return _back(app_id, "account_code_needed", False)
        ask = hold["signin"]
        field = "link" if ask.get("step") == "mail_link" else "code"
        try:
            box = seal(str(ask.get("key") or ""), {"host": ask.get("host"), field: text}, ask_context(ask))
        except Exception:
            return _back(app_id, "account_mismatch", False)
        text = ""
        if not reply_to_signin(rt.store, hold, {"kind": "box", "box": box}):
            return _back(app_id, "account_missing", False)
        return _back(app_id, "account_code_sent", True)

    @app.post("/applications/{app_id}/account/mail")
    def account_mail(request: Request, app_id: str, session: dict = Depends(require_session), csrf: str = Form(...), material_hash: str = Form(...)):
        """Read the verification mail once. This press is what may open 1Password for the mail grant."""
        require_csrf(request, session, csrf)
        _found, hold, _state = _open(app_id, material_hash)
        if hold is None or hold["signin"].get("step") not in ("mail_code", "mail_link"):
            return _back(app_id, "account_missing", False)
        ask = dict(hold["signin"])
        if not rt.mail_verification_enabled():
            reply_to_signin(rt.store, hold, {"kind": "error", "error": "mail_off"})
            return _back(app_id, "account_mail_off", False)
        access = rt.mail_access()
        state = access.unlock() if access.state() == "locked" else access.state()
        if state == "needs_reconnect":
            reply_to_signin(rt.store, hold, {"kind": "error", "error": "mail_reconnect"})
            return _back(app_id, "account_mail_reconnect", False)
        if state != "connected":
            reply_to_signin(rt.store, hold, {"kind": "error", "error": "mail_locked" if state == "locked" else "mail_off"})
            return _back(app_id, "account_mail_off", False)
        senders = [part for part in str(ask.get("senders") or "").split(",") if part]
        ask["senders"] = senders
        answer = answer_mail_ask(ask, GmailReader(access), rt.settings.app_secret.get_secret_value(), rt.account_email())
        rt.store.audit("site_account_mail", app_id, host=ask.get("host"), result=answer.flash)
        if answer.reply is not None:
            if not reply_to_signin(rt.store, hold, answer.reply):
                return _back(app_id, "account_missing", False)
            return _back(app_id, answer.flash, answer.ok)
        try:
            sent = datetime.fromisoformat(str(ask.get("sent_at") or ""))
            sent = sent if sent.tzinfo else sent.replace(tzinfo=UTC)
            when = next_check_at(sent, int((answer.finding.looks if answer.finding else 0) or 0), int(rt.settings.mail_wait_seconds))
        except (ValueError, TypeError):
            when = None
        reply_to_signin(rt.store, hold, {"kind": "wait", "next_check_at": when.isoformat() if when else ""})
        return _back(app_id, answer.flash or "mail_none", False)

    @app.post("/applications/{app_id}/account/resend")
    def account_resend(request: Request, app_id: str, session: dict = Depends(require_session), csrf: str = Form(...), material_hash: str = Form(...)):
        require_csrf(request, session, csrf)
        _found, hold, _state = _open(app_id, material_hash)
        if hold is None or not hold["signin"].get("resend"):
            return _back(app_id, "account_missing", False)
        reply_to_signin(rt.store, hold, {"kind": "resend"})
        return _back(app_id, "account_resend", True)

    @app.post("/applications/{app_id}/account/resolve")
    def account_resolve(request: Request, app_id: str, session: dict = Depends(require_session), csrf: str = Form(...), exists: str = Form("")):
        """The owner looked at the site after an unclear sign-up and says whether the account is there."""
        require_csrf(request, session, csrf)
        found = rt.store.c("applications").find_one({"_id": app_id})
        if not found:
            raise HTTPException(404, "unknown application")
        row = AccountStore(rt.store).for_site(str(((found.get("evidence") or {}).get("signin") or {}).get("host") or ""))
        if row is None or row.get("status") != "unclear":
            return _back(app_id, "account_resolve_missing", False)
        flash = resolve_unclear(rt, row, exists == "yes")
        if flash == "account_resolved_exists":
            apply_item_mark(rt, AccountStore(rt.store).get(row["_id"]) or row)
        elif flash == "account_resolved_none":
            apply_item_mark(rt, AccountStore(rt.store).get(row["_id"]) or row)
        return _back(app_id, flash, flash.startswith("account_resolved"))

    @app.post("/applications/{app_id}/account/mark")
    def account_mark(request: Request, app_id: str, session: dict = Depends(require_session), csrf: str = Form(...)):
        """Bring the saved login in line with what the run learned. This press is what opens 1Password."""
        require_csrf(request, session, csrf)
        row = AccountStore(rt.store).pending_for(app_id)
        if row is None:
            return _back(app_id, "account_item_nothing", False)
        return _back(app_id, apply_item_mark(rt, row), True)
