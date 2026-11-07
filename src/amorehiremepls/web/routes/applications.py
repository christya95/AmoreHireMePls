"""Routes registered by create_app."""

from __future__ import annotations

from amorehiremepls.core.authorization import MATERIAL_CHANGED
from amorehiremepls.web.chrome import (
    Depends,
    Form,
    HTMLResponse,
    HTTPException,
    RedirectResponse,
    Request,
    ScopeBinding,
    activity_payload,
    answers_differ,
    application_timeline,
    continue_waiting_runs,
    field_answer_flash_is_stale,
    form_filling_now,
    shown_failure_reason,
    shown_status,
    shown_status_reason,
    timedelta,
    utcnow,
    what_to_do_line,
)
from amorehiremepls.web.shots import evidence_shots


def _can_clear(app: dict | None) -> bool:
    from amorehiremepls.apply.applications import submission_lock_reason

    return bool(app) and submission_lock_reason(app) is None


def _clear_block(app: dict | None) -> str:
    """One sentence when Start over must stay hidden because a receipt or a sent application already exists."""
    if not app:
        return ""
    evidence = app.get("evidence") or {}
    if evidence.get("receipt_text") or evidence.get("confirmation") or evidence.get("receipt_path") or evidence.get("basis") == "receipt_observed":
        return "A confirmation from the employer is already saved, so this attempt cannot be cleared."
    if app.get("status") == "submitted":
        return "This application was already sent, so it cannot be cleared."
    return ""


def _clear_application(rt, app_id: str, back_to: str | None = None):
    """Back up and clear an application that was never sent, then return to its job (or `back_to`). A receipt is refused."""
    from amorehiremepls.apply.applications import ApplicationRefused

    try:
        outcome = rt.applications().delete_unsent(app_id, rt.settings.private_dir / "application_backups")
    except ApplicationRefused as exc:
        return RedirectResponse(f"{back_to or '/applications/' + app_id}?err={exc.code}", status_code=303)
    flash = "app_withdrawn" if outcome == "withdrawn" else "app_removed"
    app = rt.store.c("applications").find_one({"_id": app_id})
    return RedirectResponse(f"{back_to or '/jobs/' + app['job_id']}?ok={flash}", status_code=303)


def approval_stale(rt, app: dict) -> bool:
    """True when a saved approval is for this job but not for the package on the page."""
    from amorehiremepls.core.runtime import RealServices

    if not app or not app.get("job_id"):
        return False
    return RealServices(rt).authorization_decision(app).reason == MATERIAL_CHANGED


def _can_cancel(app: dict, runs: list[dict], filling: bool) -> bool:
    """Cancel is offered only while a task is filling the form and the final click has not been noted yet."""
    from amorehiremepls.apply.applications import LIVE_RUN_STATES, final_click_noted

    if not filling or app.get("status") not in ("authorized", "executing"):
        return False
    live = [run for run in runs if run.get("status") in LIVE_RUN_STATES]
    return bool(live) and not any(final_click_noted(run) for run in live)


def application_view(rt, a: dict, job: dict, session: dict, runs: list[dict] | None = None) -> dict:
    """Everything the status block needs for one application: the headline status, the one or two sentences from the
    latest saved event, and the one thing the page asks of the person. The job page reuses it so the two pages agree."""
    from amorehiremepls.apply.ats import adapter_for
    from amorehiremepls.core.human_check import active_hold
    from amorehiremepls.core.runtime import RealServices
    from amorehiremepls.web.chrome import answer_gap_line, needed_now, plain_headline, reason_adds, without_costs

    app_id = a["_id"]
    action = rt.actions.by_key(f"submit:{a['job_id']}")
    site_adapter = adapter_for((a.get("review_package") or {}).get("apply_url") or job.get("apply_url") or job.get("posting_url"))
    can_retry = site_adapter is not None and a["status"] in ("handoff", "failed")
    if runs is None:
        runs = list(rt.store.c("runs").find({"params.job_id": a["job_id"]}).sort("created_at", -1))
    if session.get("role") == "member":
        a = without_costs(a)
        action = without_costs(action) if action else action
        runs = without_costs(runs)
    saved_reason = shown_failure_reason(a, action)
    if a.get("status") == "failed" and saved_reason != (a.get("reason") or ""):
        a = {**a, "reason": saved_reason}
    show_status = shown_status(a, action, runs)
    hold = active_hold(rt.store, app_id)
    field_hold = {"label": hold.get("field_label") or "", "reason": hold.get("field_reason") or ""} if hold and hold.get("purpose") == "field" else None
    filling = form_filling_now(a, runs, action) and not field_hold
    live = any(run.get("status") in ("queued", "claimed", "running", "waiting", "paused") for run in runs)
    stale = approval_stale(rt, a)
    happening = plain_headline(what_to_do_line(a, runs, saved_reason, action, field_hold, stale=stale), show_status)
    can_fill = bool(a.get("status") == "authorized" and answers_differ(a, action) and not filling and not live and RealServices(rt).has_authorization(a))
    can_clear = _can_clear(a)
    can_cancel = _can_cancel(a, runs, filling)
    show_reason = shown_status_reason(a, action, show_status)
    if not stale and show_reason == MATERIAL_CHANGED:
        show_reason = ""
    show_reason = show_reason if reason_adds(happening, show_reason, a, action) else ""
    need = needed_now(a, show_status, hold, can_fill=can_fill, can_retry=can_retry, can_clear=can_clear, working=filling, can_cancel=can_cancel, stale=stale)
    return dict(
        a=a,
        job=job,
        action=action,
        runs=runs,
        can_retry=can_retry,
        site_label=site_adapter.site_label if site_adapter else None,
        hold=hold,
        saved_reason=saved_reason,
        happening=happening,
        gap=answer_gap_line(a, action),
        activity_live=activity_payload(a, runs, saved_reason, action=action, stale=stale)["live"],
        filling=filling,
        show_status=show_status,
        show_reason=show_reason,
        note_raw=show_reason == MATERIAL_CHANGED,
        answers_changed=answers_differ(a, action),
        can_fill=can_fill,
        can_clear=can_clear,
        can_cancel=can_cancel,
        clear_block="" if can_clear else _clear_block(a),
        need=need,
    )


def register(app, rt, require_session, require_csrf, render) -> None:
    # -- applications / review ---------------------------------------------------------------
    @app.get("/applications/{app_id}", response_class=HTMLResponse)
    def application(request: Request, app_id: str, session: dict = Depends(require_session)):
        a = rt.store.c("applications").find_one({"_id": app_id})
        if not a:
            raise HTTPException(404)
        job = rt.jobs.get(a["job_id"])
        scopes = [s for s in rt.auth.active("application_submit") if s.binding.target_id == a["job_id"] or s.mode == "batch"]
        doc_sha = ((a.get("review_package") or {}).get("document") or {}).get("sha256")
        doc_art = rt.store.c("artifacts").find_one({"sha256": doc_sha, "owner_id": a["job_id"]}, {"content": 0}) if doc_sha else None
        ctx = application_view(rt, a, job, session)
        a = ctx["a"]
        ctx.update(scopes=scopes, doc_art=doc_art, shots=evidence_shots(rt, a), updates=application_timeline(a, ctx["runs"], ctx["saved_reason"]))
        happening = ctx["happening"]
        if request.query_params.get("ok") == "field_ok" and field_answer_flash_is_stale(happening):
            ctx["flash"] = None
            ctx["flash_tone"] = None
        ctx["intro"] = None  # the status block at the top is this page's introduction
        return render(request, "application.html", session, **ctx)

    @app.get("/applications/{app_id}/activity")
    def application_activity(request: Request, app_id: str, session: dict = Depends(require_session)):
        from fastapi.responses import JSONResponse

        a = rt.store.c("applications").find_one({"_id": app_id})
        if not a:
            raise HTTPException(404)
        runs = list(rt.store.c("runs").find({"params.job_id": a["job_id"]}).sort("created_at", -1))
        action = rt.actions.by_key(f"submit:{a['job_id']}")
        from amorehiremepls.core.human_check import active_hold

        hold = active_hold(rt.store, app_id)
        field = None
        if hold and hold.get("purpose") == "field":
            field = {"label": hold.get("field_label") or "", "reason": hold.get("field_reason") or ""}
        return JSONResponse(activity_payload(a, runs, shown_failure_reason(a, action), field, action=action, stale=approval_stale(rt, a)))

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
        from amorehiremepls.core.runtime import RealServices

        # The same approval posts again when the saved one is for an older package. A package that is already covered stays as it is.
        covered = bool(a) and RealServices(rt).has_authorization(a)
        ready = bool(a) and a.get("material_hash") == material_hash and (a.get("status") == "ready_for_review" or (a.get("status") == "authorized" and not covered))
        if not ready:
            raise HTTPException(409, "review package changed or not ready; refresh and review again")
        host = (a["review_package"].get("apply_url") or "").split("/")[2] if "://" in (a["review_package"].get("apply_url") or "") else None
        rt.auth.grant(
            "application_submit", ScopeBinding(target_id=a["job_id"], material_hash=material_hash, allowed_hosts=[host] if host else []), ttl=timedelta(hours=hours), note=note
        )
        now = utcnow()
        rt.store.c("applications").update_one(
            {"_id": app_id},
            {
                "$set": {"status": "authorized", "reason": "", "updated_at": now},
                "$push": {"history": {"at": now, "status": "authorized", "next_action": "the task applies this package"}},
            },
        )
        a = rt.store.c("applications").find_one({"_id": app_id})
        if not continue_waiting_runs(rt, a):
            from amorehiremepls.core.orchestration import queue_approved_send
            from amorehiremepls.core.runtime import RealServices

            queue_approved_send(rt.store, a, RealServices(rt).has_authorization(a))
        return RedirectResponse(f"/applications/{app_id}", status_code=303)

    @app.post("/applications/{app_id}/fill")
    def application_fill(request: Request, app_id: str, session: dict = Depends(require_session), csrf: str = Form(...)):
        """Open the form with the answers on the page. A finished attempt for different answers is not reused."""
        require_csrf(request, session, csrf)
        a = rt.store.c("applications").find_one({"_id": app_id})
        if not a or a.get("status") != "authorized":
            raise HTTPException(409, "approve these answers first")
        from amorehiremepls.core.orchestration import queue_approved_send
        from amorehiremepls.core.runtime import RealServices

        if not continue_waiting_runs(rt, a):
            queue_approved_send(rt.store, a, RealServices(rt).has_authorization(a), replace_repeat=True)
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
        previous = rt.actions.by_key(f"submit:{job['_id']}")
        if previous and previous.get("status") == "failed":
            rt.actions.retire_failed_attempt(previous)
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

    @app.post("/applications/{app_id}/field-answer")
    def application_field_answer(
        request: Request,
        app_id: str,
        session: dict = Depends(require_session),
        csrf: str = Form(...),
        material_hash: str = Form(...),
        answer: str = Form(""),
    ):
        """Type one answer into the same open form. Any signed-in dashboard can send it. The value is not written to the log here."""
        require_csrf(request, session, csrf)
        from amorehiremepls.core.human_check import submit_field_answer

        a = rt.store.c("applications").find_one({"_id": app_id})
        if not a or a.get("material_hash") != material_hash:
            raise HTTPException(409, "package changed; refresh and review again")
        result = submit_field_answer(rt.store, a, answer)
        if result == "ok":
            rt.store.audit("field_answer_submitted", app_id)
        flash = {"ok": "field_ok", "missing": "field_missing", "changed": "field_changed", "empty": "field_empty"}[result]
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

    @app.post("/applications/{app_id}/start-over")
    def application_start_over(request: Request, app_id: str, session: dict = Depends(require_session), csrf: str = Form(...)):
        """Clear an unsent attempt and return to the job. The button is the confirmation. Nothing is sent."""
        require_csrf(request, session, csrf)
        return _clear_application(rt, app_id)

    @app.post("/applications/{app_id}/remove")
    def application_remove(request: Request, app_id: str, session: dict = Depends(require_session), csrf: str = Form(...)):
        """The same clearing as Start over, from a row of the Applications list. Returns to the list. Nothing is sent."""
        require_csrf(request, session, csrf)
        if not rt.store.c("applications").find_one({"_id": app_id}, {"_id": 1}):
            raise HTTPException(404)
        return _clear_application(rt, app_id, back_to="/applications")

    @app.post("/applications/{app_id}/cancel")
    def application_cancel(request: Request, app_id: str, session: dict = Depends(require_session), csrf: str = Form(...)):
        """Stop the attempt that is filling the form now, before the final click. The task closes the page and records
        the stop itself; a cancel that arrives after the final click is refused so the receipt wait decides."""
        require_csrf(request, session, csrf)
        from amorehiremepls.apply.applications import request_cancel

        a = rt.store.c("applications").find_one({"_id": app_id})
        if not a:
            raise HTTPException(404)
        result = request_cancel(rt.store, a)
        flash = {"ok": "cancel_requested", "missing": "cancel_missing", "too_late": "cancel_too_late"}[result]
        code = "ok" if result == "ok" else "err"
        return RedirectResponse(f"/applications/{app_id}?{code}={flash}", status_code=303)

    @app.post("/applications/{app_id}/remove-resumes")
    def application_remove_resumes(request: Request, app_id: str, session: dict = Depends(require_session), csrf: str = Form(...), confirm: str = Form("")):
        """Delete tailored resumes for this unsent application. The base resume and a sent application stay."""
        require_csrf(request, session, csrf)
        if confirm.strip().lower() != "remove":
            return RedirectResponse(f"/applications/{app_id}?err=confirm_needed", status_code=303)
        a = rt.store.c("applications").find_one({"_id": app_id})
        if not a:
            raise HTTPException(404)
        from amorehiremepls.apply.applications import remove_unused_resumes

        result = remove_unused_resumes(rt.store, rt.settings.artifact_dir, a["job_id"])
        if result["refused"]:
            return RedirectResponse(f"/applications/{app_id}?err={result['refused']}", status_code=303)
        flash = "resumes_removed" if result["removed"] else "resumes_none"
        return RedirectResponse(f"/applications/{app_id}?ok={flash}", status_code=303)
