"""The request list of a stop record: a panel the application page loads, and the same list as its own page."""

from __future__ import annotations

from amorehiremepls.web.chrome import Depends, HTMLResponse, HTTPException, Request
from amorehiremepls.web.stop_view import NOTICE, WHERE, stop_network_view, stop_record_view


def register(app, rt, require_session, render) -> None:
    @app.get("/applications/{app_id}/stop-record", response_class=HTMLResponse)
    def stop_record(request: Request, app_id: str, session: dict = Depends(require_session), part: int = 0):
        a = rt.store.c("applications").find_one({"_id": app_id})
        if not a:
            raise HTTPException(404)
        job = rt.jobs.get(a["job_id"]) or {}
        ctx = {"a": a, "job": job, "stop": stop_record_view(rt, a), "net": stop_network_view(rt, a), "notice": NOTICE, "where": WHERE, "part": bool(part)}
        response = render(request, "_stop_network.html" if part else "stop_record.html", session, **ctx)
        response.headers["Cache-Control"] = "private, no-store"
        return response
