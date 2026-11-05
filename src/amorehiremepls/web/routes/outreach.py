"""Routes registered by create_app."""

from __future__ import annotations

from amorehiremepls.web.chrome import (
    Depends,
    Form,
    HTMLResponse,
    HTTPException,
    RedirectResponse,
    Request,
)


def register(app, rt, require_session, require_csrf, render) -> None:
    # -- outreach -------------------------------------------------------------------------------
    @app.get("/outreach", response_class=HTMLResponse)
    def outreach(request: Request, session: dict = Depends(require_session)):
        o = rt.outreach()
        recs = list(rt.store.c("recipients").find().sort("updated_at", -1).limit(200))
        for r in recs:
            r["history"] = o.recipients.history_check(r)
        msgs = list(rt.store.c("outreach").find().sort("created_at", -1).limit(200))
        submitted = list(rt.store.c("applications").find({"status": "submitted"}).sort("updated_at", -1).limit(50))
        jobs = {j["_id"]: j for j in rt.store.c("jobs").find({"_id": {"$in": [a["job_id"] for a in submitted]}})}
        return render(request, "outreach.html", session, recipients=recs, messages=msgs, submitted=submitted, jobs=jobs, smtp=bool(rt.smtp))

    @app.post("/outreach/recipients")
    def add_recipient(
        request: Request,
        session: dict = Depends(require_session),
        csrf: str = Form(...),
        name: str = Form(...),
        employer: str = Form(...),
        profile_url: str = Form(""),
        email: str = Form(""),
        remit: str = Form(""),
    ):
        require_csrf(request, session, csrf)
        rt.outreach().recipients.upsert(name=name, employer=employer, profile_url=profile_url or None, email=email or None, remit=remit, provenance="dashboard")
        return RedirectResponse("/outreach", status_code=303)

    @app.post("/outreach/recipients/{rid}/reconcile")
    def reconcile_recipient(request: Request, rid: str, session: dict = Depends(require_session), csrf: str = Form(...), note: str = Form("")):
        require_csrf(request, session, csrf)
        rt.outreach().recipients.mark_reconciled(rid, note)
        return RedirectResponse("/outreach", status_code=303)

    @app.post("/outreach/recipients/{rid}/history")
    def import_recipient_history(
        request: Request, rid: str, session: dict = Depends(require_session), csrf: str = Form(...), kind: str = Form("message"), when: str = Form(""), note: str = Form("")
    ):
        require_csrf(request, session, csrf)
        rec = rt.store.c("recipients").find_one({"_id": rid})
        alias = next(iter(rec.get("aliases", [])), None)
        if alias:
            rt.outreach().recipients.import_history(alias=alias, kind=kind, when=when or None, note=note, source="dashboard")
        return RedirectResponse("/outreach", status_code=303)

    @app.post("/outreach/recipients/{rid}/draft")
    def draft(request: Request, rid: str, session: dict = Depends(require_session), csrf: str = Form(...), kind: str = Form("connection_request"), applied_role: str = Form("")):
        require_csrf(request, session, csrf)
        rec = rt.store.c("recipients").find_one({"_id": rid})
        if applied_role:
            # only accept roles with a confirmed submission at this employer
            confirmed = rt.store.c("applications").find_one({"status": "submitted"})
            jobs = [j for j in rt.store.c("jobs").find({"employer_norm": rec["employer_norm"], "status": {"$in": ["applied", "applied_historical"]}})]
            if not confirmed or not any(j["title"].lower() == applied_role.lower() for j in jobs):
                raise HTTPException(400, "applied_role must match a confirmed submission at this employer")
        rt.outreach().draft(rid, kind, applied_role or None)
        return RedirectResponse("/outreach", status_code=303)

    @app.post("/outreach/messages/{mid}/send")
    def send(request: Request, mid: str, session: dict = Depends(require_session), csrf: str = Form(...), channel: str = Form("linkedin")):
        require_csrf(request, session, csrf)
        res = rt.outreach().send(mid, "dashboard", channel)
        rt.store.c("outreach").update_one({"_id": mid}, {"$set": {"last_send_result": res}})
        return RedirectResponse("/outreach", status_code=303)

    @app.post("/outreach/messages/{mid}/record-send")
    def record_send(request: Request, mid: str, session: dict = Depends(require_session), csrf: str = Form(...), confirmation: str = Form(...)):
        require_csrf(request, session, csrf)
        rt.outreach().record_manual_send(mid, confirmation)
        return RedirectResponse("/outreach", status_code=303)
