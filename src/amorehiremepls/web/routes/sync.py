"""Routes registered by create_app."""

from __future__ import annotations

from amorehiremepls.web.chrome import (
    Depends,
    Form,
    HTMLResponse,
    HTTPException,
    RedirectResponse,
    Request,
    utcnow,
)


def register(app, rt, require_session, require_csrf, render) -> None:
    # -- sync / usage ------------------------------------------------------------------------
    @app.get("/sync", response_class=HTMLResponse)
    def sync(request: Request, session: dict = Depends(require_session)):
        return render(request, "sync.html", session, status=rt.sync.status(), items=list(rt.store.c("sync_outbox").find().sort("created_at", -1).limit(100)))

    @app.post("/sync/{what}")
    def sync_action(request: Request, what: str, session: dict = Depends(require_session), csrf: str = Form(...)):
        require_csrf(request, session, csrf)
        if what == "process":
            rt.store.c("sync_outbox").update_many({"status": {"$in": ["sync_pending", "local_only"]}}, {"$set": {"next_attempt_at": utcnow()}})
        elif what == "reconcile":
            rt.sync.reconcile_all()
        else:
            raise HTTPException(404)
        return RedirectResponse("/sync", status_code=303)

    @app.get("/usage", response_class=HTMLResponse)
    def usage(request: Request, session: dict = Depends(require_session)):
        if session.get("role") == "member":
            return RedirectResponse("/", status_code=303)
        rows = list(rt.store.c("usage").find().sort("created_at", -1).limit(200))
        by_skill = list(
            rt.store.c("usage").aggregate(
                [
                    {
                        "$group": {
                            "_id": "$skill",
                            "calls": {"$sum": 1},
                            "in": {"$sum": "$input_tokens"},
                            "out": {"$sum": "$output_tokens"},
                            "cost": {"$sum": {"$ifNull": ["$cost_usd", 0]}},
                            "unknown": {"$sum": {"$cond": [{"$eq": ["$cost_known", False]}, 1, 0]}},
                            "cached": {"$sum": {"$cond": ["$cached_result", 1, 0]}},
                        }
                    }
                ]
            )
        )
        submitted = rt.store.c("applications").count_documents({"status": "submitted", "provenance.origin": "local"})
        return render(
            request,
            "usage.html",
            session,
            rows=rows,
            by_skill=by_skill,
            submitted=submitted,
            pricing=rt.pricing.loaded_from,
            catalog=rt.ai(None).catalog.describe() if rt.ai(None) else [],
        )
