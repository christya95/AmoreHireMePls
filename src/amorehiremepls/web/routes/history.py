"""Routes registered by create_app."""

from __future__ import annotations

from amorehiremepls.web.chrome import (
    Depends,
    Form,
    HTMLResponse,
    HTTPException,
    RedirectResponse,
    Request,
    TrackerClone,
    TrackerImporter,
)


def register(app, rt, require_session, require_csrf, render) -> None:
    # -- history -------------------------------------------------------------------------------
    @app.get("/history", response_class=HTMLResponse)
    def history(request: Request, session: dict = Depends(require_session)):
        apps = list(rt.store.c("applications").find({"provenance.origin": "tracker_import"}).sort("record.application_date", -1))
        imports = list(rt.store.c("imports").find().sort("created_at", -1).limit(5))
        preview = None
        if (rt.settings.tracker_clone_dir / ".git").exists():
            try:
                preview = TrackerImporter(rt.store, TrackerClone(rt.settings.tracker_clone_dir)).preview().as_dict()
            except Exception as exc:
                preview = {"error": f"{type(exc).__name__}: {exc}"}
        jobs = {j["_id"]: j for j in rt.store.c("jobs").find({"_id": {"$in": [a.get("job_id") for a in apps if a.get("job_id")]}})}
        return render(request, "history.html", session, apps=apps, imports=imports, preview=preview, sync=rt.sync.status(), jobs=jobs)

    @app.post("/history/import")
    def history_import(request: Request, session: dict = Depends(require_session), csrf: str = Form(...)):
        require_csrf(request, session, csrf)
        if not (rt.settings.tracker_clone_dir / ".git").exists():
            raise HTTPException(400, "tracker clone missing")
        TrackerImporter(rt.store, TrackerClone(rt.settings.tracker_clone_dir)).import_all()
        return RedirectResponse("/history", status_code=303)
