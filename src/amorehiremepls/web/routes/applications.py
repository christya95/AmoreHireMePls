"""Routes registered by create_app."""

from __future__ import annotations

from amorehiremepls.web.chrome import (
    Depends,
    Form,
    HTMLResponse,
    HTTPException,
    RedirectResponse,
    Request,
    ScopeBinding,
    application_timeline,
    authorized_now,
    continue_waiting_runs,
    timedelta,
    utcnow,
)
from amorehiremepls.web.shots import evidence_shots


def _can_clear(app: dict | None) -> bool:
    from amorehiremepls.apply.applications import submission_lock_reason

    return bool(app) and submission_lock_reason(app) is None


def _clear_application(rt, app_id: str, what: str):
    from amorehiremepls.apply.applications import ApplicationRefused

    try:
        if what == "delete":
            outcome = rt.applications().delete_unsent(app_id, rt.settings.private_dir / "application_backups")
            flash = "app_withdrawn" if outcome == "withdrawn" else "app_removed"
            app = rt.store.c("applications").find_one({"_id": app_id})
            return RedirectResponse(f"/jobs/{app['job_id']}?ok={flash}", status_code=303)
        rt.applications().restart_unsent(app_id)
    except ApplicationRefused as exc:
        return RedirectResponse(f"/applications/{app_id}?err={exc.code}", status_code=303)
    return RedirectResponse(f"/applications/{app_id}?ok=job_restarted", status_code=303)


def register(app, rt, require_session, require_csrf, render) -> None:
    # -- applications / review ---------------------------------------------------------------
    @app.get("/applications/{app_id}", response_class=HTMLResponse)
    def application(request: Request, app_id: str, session: dict = Depends(require_session)):
        a = rt.store.c("applications").find_one({"_id": app_id})
        if not a:
            raise HTTPException(404)
        job = rt.jobs.get(a["job_id"])
        scopes = [s for s in rt.auth.active("application_submit") if s.binding.target_id == a["job_id"] or s.mode == "batch"]
        action = rt.actions.by_key(f"submit:{a['job_id']}")
        doc_sha = ((a.get("review_package") or {}).get("document") or {}).get("sha256")
        doc_art = rt.store.c("artifacts").find_one({"sha256": doc_sha, "owner_id": a["job_id"]}, {"content": 0}) if doc_sha else None
        from amorehiremepls.apply.ats import adapter_for

        site_adapter = adapter_for((a.get("review_package") or {}).get("apply_url") or job.get("apply_url") or job.get("posting_url"))
        can_retry = site_adapter is not None and a["status"] in ("handoff", "failed")
        from amorehiremepls.core.human_check import active_hold

        runs = list(rt.store.c("runs").find({"params.job_id": a["job_id"]}).sort("created_at", -1))
        if session.get("role") == "member":
            from amorehiremepls.web.chrome import without_costs

            a = without_costs(a)
            action = without_costs(action) if action else action
            runs = without_costs(runs)
        ctx = dict(
            a=a,
            job=job,
            scopes=scopes,
            action=action,
            doc_art=doc_art,
            shots=evidence_shots(rt, a),
            can_retry=can_retry,
            site_label=site_adapter.site_label if site_adapter else None,
            hold=active_hold(rt.store, app_id),
            updates=application_timeline(a, runs),
            now_line=authorized_now(a, runs),
            can_clear=_can_clear(a),
        )
        return render(request, "application.html", session, **ctx)

    @app.post("/applications/{app_id}/inputs")
    async def application_inputs(request: Request, app_id: str, session: dict = Depends(require_session)):
        form = await request.form()
        require_csrf(request, session, form.get("csrf"))
        values = {k[6:]: str(v) for k, v in form.items() if isinstance(k, str) and k.startswith("input_")}
        updated = rt.applications().supply_inputs(app_id, values)
        continue_waiting_runs(rt, updated)
        return RedirectResponse(f"/applications/{app_id}", status_code=303)

    @app.post("/applications/{app_id}/authorize")
    def application_authorize(
        request: Request, app_id: str, session: dict = Depends(require_session), csrf: str = Form(...), material_hash: str = Form(...), hours: int = Form(24), note: str = Form("")
    ):
        require_csrf(request, session, csrf)
        a = rt.store.c("applications").find_one({"_id": app_id})
        if a["status"] != "ready_for_review" or a["material_hash"] != material_hash:
            raise HTTPException(409, "review package changed or not ready; refresh and review again")
        host = (a["review_package"].get("apply_url") or "").split("/")[2] if "://" in (a["review_package"].get("apply_url") or "") else None
        rt.auth.grant(
            "application_submit", ScopeBinding(target_id=a["job_id"], material_hash=material_hash, allowed_hosts=[host] if host else []), ttl=timedelta(hours=hours), note=note
        )
        now = utcnow()
        rt.store.c("applications").update_one(
            {"_id": app_id},
            {"$set": {"status": "authorized", "updated_at": now}, "$push": {"history": {"at": now, "status": "authorized", "next_action": "the task applies this package"}}},
        )
        a["status"] = "authorized"
        continue_waiting_runs(rt, a)
        return RedirectResponse(f"/applications/{app_id}", status_code=303)

    @app.post("/applications/{app_id}/manual")
    def application_manual(
        request: Request,
        app_id: str,
        session: dict = Depends(require_session),
        csrf: str = Form(...),
        confirmation: str = Form(...),
        application_date: str = Form(...),
        resume_sha256: str = Form(""),
    ):
        require_csrf(request, session, csrf)
        a = rt.store.c("applications").find_one({"_id": app_id})
        waiting = rt.store.c("runs").find_one({"params.job_id": a["job_id"], "status": "waiting", "interrupt.type": "handoff"})
        if waiting:
            rt.store.update_run(
                waiting["_id"],
                requested="resume",
                resume_payload={"manual": {"confirmation": confirmation, "application_date": application_date, "resume_sha256": resume_sha256 or None}},
            )
        else:
            rt.applications().record_manual_submission(app_id, confirmation, application_date, resume_sha256 or None)
        return RedirectResponse(f"/applications/{app_id}", status_code=303)

    @app.post("/applications/{app_id}/retry")
    def application_retry(request: Request, app_id: str, session: dict = Depends(require_session), csrf: str = Form(...)):
        """After a handoff or failure: read the form again through the site adapter, rebuild the package (new fingerprint,
        so it waits for approval again) and start a fresh background task. The old waiting task is closed."""
        require_csrf(request, session, csrf)
        a = rt.store.c("applications").find_one({"_id": app_id})
        if not a or a["status"] not in ("handoff", "failed", "needs_input", "ready_for_review"):
            raise HTTPException(409, "nothing to retry in this state")
        job = rt.jobs.get(a["job_id"])
        from amorehiremepls.apply.ats import adapter_for

        if adapter_for(job.get("apply_url") or job.get("posting_url")) is None:
            return RedirectResponse(f"/applications/{app_id}?err=retry_unsupported", status_code=303)
        if rt.store.c("runs").find_one({"params.job_id": job["_id"], "status": {"$in": ["queued", "claimed", "running"]}}):
            return RedirectResponse(f"/applications/{app_id}?err=retry_busy", status_code=303)
        for run in rt.store.c("runs").find({"params.job_id": job["_id"], "status": {"$in": ["waiting", "paused"]}}):
            rt.store.update_run(run["_id"], status="cancelled", requested=None, interrupt=None, finished_at=utcnow(), note="replaced by a retry with the app")
            rt.store.run_event(run["_id"], "cancelled: replaced by a retry with the app")
        fp = {k: v for k, v in (job.get("form_plan") or {}).items() if k not in ("fields", "source", "external_job_id", "read_at")}
        status = "qualified" if job.get("status") in ("executing", "needs_verification") else job["status"]
        rt.jobs.set_status(job["_id"], status, form_plan=fp, apply_route=None)  # the form is read again through the adapter
        rt.store.c("applications").update_one(
            {"_id": app_id}, {"$set": {"status": "queued", "reason": None, "next_action": "the app is reading the form again", "updated_at": utcnow()}}
        )
        run = rt.store.create_run("application", {"job_id": job["_id"], "retry_of": app_id})
        rt.store.audit("application_retry", app_id, run_id=run["_id"])
        return RedirectResponse(f"/applications/{app_id}?ok=retry_started", status_code=303)

    @app.post("/applications/{app_id}/continue")
    def application_continue(request: Request, app_id: str, session: dict = Depends(require_session), csrf: str = Form(...), material_hash: str = Form(...)):
        """Resume a held human check on the same page and the same approval. Does not submit again."""
        require_csrf(request, session, csrf)
        from amorehiremepls.core.human_check import request_continue

        a = rt.store.c("applications").find_one({"_id": app_id})
        if not a or a.get("material_hash") != material_hash:
            raise HTTPException(409, "package changed; refresh and review again")
        result = request_continue(rt.store, a)
        flash = {"ok": "continue_ok", "missing": "continue_missing", "changed": "continue_changed"}[result]
        code = "ok" if result == "ok" else "err"
        return RedirectResponse(f"/applications/{app_id}?{code}={flash}", status_code=303)

    @app.post("/applications/{app_id}/reconcile")
    def application_reconcile(
        request: Request,
        app_id: str,
        session: dict = Depends(require_session),
        csrf: str = Form(...),
        resolved: str = Form(...),
        confirmation: str = Form(""),
        application_date: str = Form(""),
    ):
        require_csrf(request, session, csrf)
        rt.applications().reconcile(app_id, resolved, {"confirmation": confirmation, "application_date": application_date or None})
        return RedirectResponse(f"/applications/{app_id}", status_code=303)

    @app.post("/applications/{app_id}/delete")
    def application_delete(request: Request, app_id: str, session: dict = Depends(require_session), csrf: str = Form(...), confirm: str = Form("")):
        """Back up and remove an application that was never sent. The job stays. A receipt is refused."""
        require_csrf(request, session, csrf)
        if confirm.strip().lower() != "delete":
            return RedirectResponse(f"/applications/{app_id}?err=confirm_needed", status_code=303)
        return _clear_application(rt, app_id, "delete")

    @app.post("/applications/{app_id}/restart")
    def application_restart(request: Request, app_id: str, session: dict = Depends(require_session), csrf: str = Form(...), confirm: str = Form("")):
        """Clear the unsent package and approval, then prepare a new package. Does not submit."""
        require_csrf(request, session, csrf)
        if confirm.strip().lower() != "restart":
            return RedirectResponse(f"/applications/{app_id}?err=confirm_needed", status_code=303)
        return _clear_application(rt, app_id, "restart")
