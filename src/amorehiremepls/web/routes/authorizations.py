"""Routes registered by create_app."""

from __future__ import annotations

from amorehiremepls.web.chrome import (
    Depends,
    Form,
    HTMLResponse,
    RedirectResponse,
    Request,
    ScopeBinding,
    StopConditions,
    timedelta,
)


def register(app, rt, require_session, require_csrf, render) -> None:
    # -- authorizations --------------------------------------------------------------------------
    @app.get("/authorizations", response_class=HTMLResponse)
    def authorizations(request: Request, session: dict = Depends(require_session)):
        return render(request, "authorizations.html", session, scopes=list(rt.store.c("authorizations").find().sort("created_at", -1).limit(100)))

    @app.post("/authorizations/batch")
    def batch_scope(
        request: Request,
        session: dict = Depends(require_session),
        csrf: str = Form(...),
        employers: str = Form(""),
        sites: str = Form(""),
        max_fit_rank: int = Form(2),
        max_count: int = Form(3),
        hours: int = Form(48),
        note: str = Form(""),
    ):
        require_csrf(request, session, csrf)
        kind = "application_submit"  # the only kind that may be batch-authorized; outreach/email are per action
        filters = {"max_fit_rank": max_fit_rank}
        if employers.strip():
            filters["employers"] = [e.strip() for e in employers.split(",") if e.strip()]
        if sites.strip():
            filters["sites"] = [s.strip() for s in sites.split(",") if s.strip()]
        rt.auth.grant(
            kind,
            ScopeBinding(job_filters=filters, allowed_hosts=filters.get("sites", [])),
            mode="batch",
            max_count=max_count,
            ttl=timedelta(hours=hours),
            note=note,
            stop=StopConditions(stop_on_needs_verification=True, max_failures=1, stop_on_material_unknowns=True),
        )
        return RedirectResponse("/authorizations", status_code=303)

    @app.post("/authorizations/{scope_id}/revoke")
    def revoke(request: Request, scope_id: str, session: dict = Depends(require_session), csrf: str = Form(...)):
        require_csrf(request, session, csrf)
        rt.auth.revoke(scope_id, "dashboard")
        return RedirectResponse("/authorizations", status_code=303)
