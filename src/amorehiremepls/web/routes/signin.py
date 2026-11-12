"""Site sign-in controls: the owner's Allow press, the saved one-time code, a new code, and sending a refused login again.

The lookup runs here, in the dashboard that received the press, so the password manager's own approval window
opens on the computer the owner is using. The saved login leaves this process only as a sealed box for the waiting
browser task. It is never written in the clear, logged, or returned to the page.
"""

from __future__ import annotations

from amorehiremepls.core.human_check import note_missed_approval, note_unchanged_login, reply_to_signin, signin_hold
from amorehiremepls.core.site_signin import APPROVAL_ASKS, SignInMemory, answer_ask
from amorehiremepls.web.chrome import Depends, Form, HTTPException, RedirectResponse, Request


def register(app, rt, require_session, require_csrf) -> None:
    def _back(app_id: str, flash: str, ok: bool) -> RedirectResponse:
        return RedirectResponse(f"/applications/{app_id}?{'ok' if ok else 'err'}={flash}", status_code=303)

    def _open_ask(app_id: str, material_hash: str):
        a = rt.store.c("applications").find_one({"_id": app_id})
        if not a or a.get("material_hash") != material_hash:
            raise HTTPException(409, "package changed; refresh and review again")
        hold, state = signin_hold(rt.store, a)
        return a, hold, state

    @app.post("/applications/{app_id}/signin/allow")
    def signin_allow(request: Request, app_id: str, session: dict = Depends(require_session), csrf: str = Form(...), material_hash: str = Form(...), same: str = Form("")):
        """The owner allows one read of the saved login, or of its one-time code. This press is what opens 1Password's window.

        After a refused password the same press is the owner's Continue. A saved login that has not changed since the
        site refused it is not read: the page asks a second question, and only `same=yes` sends it once more."""
        require_csrf(request, session, csrf)
        a, hold, state = _open_ask(app_id, material_hash)
        if hold is None:
            return _back(app_id, "signin_missing" if state == "missing" else "field_changed", False)
        ask = hold["signin"]
        if ask.get("step") == "code" and not ask.get("offer_code"):
            return _back(app_id, "signin_no_code", False)
        email = str(rt.profile.fact_value("email") or "")
        answer = answer_ask(ask, rt.credential_source(), SignInMemory(rt.store), rt.settings.app_secret.get_secret_value(), prefer_username=email, confirm_same=same == "yes")
        rt.store.audit("site_signin_asked", app_id, host=ask.get("host"), step=ask.get("step"), result=answer.flash)
        if answer.unchanged:
            note_unchanged_login(rt.store, hold, answer.unchanged)
            return _back(app_id, answer.flash, False)
        if answer.missed and ask.get("step") != "code" and note_missed_approval(rt.store, hold) >= APPROVAL_ASKS:
            reply_to_signin(rt.store, hold, {"kind": "error", "error": "not_approved"})
            return _back(app_id, "signin_gave_up", False)
        if answer.reply is not None and not reply_to_signin(rt.store, hold, answer.reply):
            return _back(app_id, "signin_missing", False)
        return _back(app_id, answer.flash, answer.ok)

    @app.post("/applications/{app_id}/signin/decline")
    def signin_decline(request: Request, app_id: str, session: dict = Depends(require_session), csrf: str = Form(...), material_hash: str = Form(...)):
        """The owner does not want the app to sign in this time. The attempt stops and nothing is read."""
        require_csrf(request, session, csrf)
        a, hold, state = _open_ask(app_id, material_hash)
        if hold is None or hold["signin"].get("step") != "login":
            return _back(app_id, "signin_missing", False)
        reply_to_signin(rt.store, hold, {"kind": "declined"})
        rt.store.audit("site_signin_declined", app_id, host=hold["signin"].get("host"))
        return _back(app_id, "signin_declined", True)

    @app.post("/applications/{app_id}/signin/resend")
    def signin_resend(request: Request, app_id: str, session: dict = Depends(require_session), csrf: str = Form(...), material_hash: str = Form(...)):
        """The owner asks for a new code. The task presses the site's own resend control once, then asks again."""
        require_csrf(request, session, csrf)
        a, hold, state = _open_ask(app_id, material_hash)
        if hold is None or hold["signin"].get("step") != "code" or not hold["signin"].get("resend"):
            return _back(app_id, "signin_missing", False)
        reply_to_signin(rt.store, hold, {"kind": "resend"})
        rt.store.audit("site_signin_resend", app_id, host=hold["signin"].get("host"))
        return _back(app_id, "signin_resend", True)
