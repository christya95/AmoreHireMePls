"""Routes registered by create_app."""

from __future__ import annotations

from amorehiremepls.web.chrome import (
    SOURCES,
    STEP_HELP,
    Any,
    Depends,
    Form,
    HTMLResponse,
    HTTPException,
    Path,
    RedirectResponse,
    Request,
    fit_score,
    json,
    plain_failure,
    status_label,
    utcnow,
)
from amorehiremepls.web.shots import split_screenshots


def register(app, rt, require_session, require_csrf, render) -> None:
    # -- jobs --------------------------------------------------------------------------------------
    @app.get("/jobs", response_class=HTMLResponse)
    def jobs(request: Request, session: dict = Depends(require_session), status_filter: str = ""):
        q = {"status": status_filter} if status_filter else {"status": {"$ne": "closed"}}
        jobs = list(rt.store.c("jobs").find(q).sort("updated_at", -1).limit(200))
        apps = {a["job_id"]: a for a in rt.store.c("applications").find({"provenance.origin": "local"})}
        return render(request, "jobs.html", session, jobs=jobs, apps=apps, sources=rt.store.get_control("discovery_sources", []), source_types=sorted(SOURCES))

    @app.post("/jobs/import")
    def jobs_import(
        request: Request,
        session: dict = Depends(require_session),
        csrf: str = Form(...),
        url: str = Form(...),
        employer: str = Form(""),
        title: str = Form(""),
        requisition_id: str = Form(""),
        location: str = Form(""),
        posting_text: str = Form(""),
    ):
        require_csrf(request, session, csrf)
        job = rt.discovery().import_url(url, employer or None, title or None, posting_text or None, requisition_id or None, location or None)
        rt.discovery().qualify_job(job["_id"])
        return RedirectResponse(f"/jobs/{job['_id']}", status_code=303)

    @app.post("/jobs/sources")
    def jobs_sources(request: Request, session: dict = Depends(require_session), csrf: str = Form(...), type: str = Form(...), key: str = Form(...), employer: str = Form("")):
        require_csrf(request, session, csrf)
        if type not in SOURCES:
            raise HTTPException(400, "unknown source type")
        sources = rt.store.get_control("discovery_sources", []) or []
        sources.append({"type": type, "key": key.strip(), "employer": employer.strip() or None})
        rt.store.set_control("discovery_sources", sources)
        return RedirectResponse("/jobs", status_code=303)

    @app.post("/jobs/discover")
    def jobs_discover(request: Request, session: dict = Depends(require_session), csrf: str = Form(...)):
        require_csrf(request, session, csrf)
        rt.store.create_run("discovery_request", {"trigger": "dashboard"})
        rt.store.set_control("discovery_requested", {"at": utcnow()})
        return RedirectResponse("/jobs", status_code=303)

    @app.post("/jobs/check-postings")
    def jobs_check_postings(request: Request, session: dict = Depends(require_session), csrf: str = Form(...)):
        """Re-read open jobs one at a time. A posting that is gone is closed and taken off this list."""
        require_csrf(request, session, csrf)
        import httpx

        with httpx.Client(timeout=20.0, follow_redirects=True, headers={"User-Agent": "AmoreHireMePls/0.1 (posting reader)"}) as client:
            summary = rt.discovery().check_open_postings(client=client, artifact_dir=rt.settings.artifact_dir)
        flash = "postings_fresh" if summary["checked"] == 0 else "postings_checked"
        return RedirectResponse(f"/jobs?ok={flash}", status_code=303)

    @app.post("/jobs/remove-unused-resumes")
    def jobs_remove_unused_resumes(request: Request, session: dict = Depends(require_session), csrf: str = Form(...), confirm: str = Form("")):
        """Delete tailored resumes for every application that was never sent. The base resume stays."""
        require_csrf(request, session, csrf)
        if confirm.strip().lower() != "remove":
            return RedirectResponse("/jobs?err=confirm_needed", status_code=303)
        from amorehiremepls.apply.applications import remove_unused_resumes

        removed = 0
        for job in rt.store.c("jobs").find({}, {"_id": 1}):
            removed += remove_unused_resumes(rt.store, rt.settings.artifact_dir, job["_id"])["removed"]
        flash = "resumes_removed" if removed else "resumes_none"
        return RedirectResponse(f"/jobs?ok={flash}", status_code=303)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_detail(request: Request, job_id: str, session: dict = Depends(require_session)):
        job = rt.jobs.get(job_id)
        if not job:
            raise HTTPException(404)
        apps = list(rt.store.c("applications").find({"job_id": job_id}))
        arts = list(rt.store.c("artifacts").find({"owner_id": job_id}, {"content": 0}).sort("created_at", -1))
        runs = list(rt.store.c("runs").find({"params.job_id": job_id}).sort("created_at", -1))
        local = next((a for a in apps if (a.get("provenance") or {}).get("origin") == "local"), None)
        scopes = [s for s in rt.auth.active("application_submit") if s.binding.target_id == job_id]
        active_run = next((r for r in runs if r["status"] in ("queued", "claimed", "running", "waiting", "paused")), None)
        outreach_n = rt.store.c("outreach").count_documents({"employer_norm": job.get("employer_norm")}) if job.get("employer_norm") else 0
        steps, next_step = job_steps(job, local, arts, bool(scopes), active_run, outreach_n)
        docs, shots = split_screenshots(arts)
        from amorehiremepls.apply.applications import submission_lock_reason

        ctx = {
            "job": job,
            "fit": fit_score(job.get("fit")),
            "apps": apps,
            "artifacts": docs,
            "screenshots": shots,
            "runs": runs,
            "local_app": local,
            "scopes": scopes,
            "can_clear": local is not None and submission_lock_reason(local) is None,
        }
        ctx.update(steps=steps, next_step=next_step, active_run=active_run)
        return render(request, "job.html", session, **ctx)

    def job_steps(job: dict, app_doc: dict | None, arts: list, scope_active: bool, active_run: dict | None, outreach_n: int) -> tuple[list[dict], dict]:
        """Linear workflow for one job with the current step and the single next action under it."""
        st = (app_doc or {}).get("status")
        if job.get("status") == "closed":
            names = {tech: (name, tip) for name, (tip, tech) in STEP_HELP.items()}
            flags = ["import", "qualify", "tailor", "review", "authorize", "submit", "record", "outreach"]
            steps = [{"key": k, "name": names[k][0], "tip": names[k][1], "done": False, "current": False} for k in flags]
            reason = job.get("closed_reason") or "This posting is no longer up"
            return steps, {"label": reason, "href": None, "form": None, "note": "The job stays under Closed. Nothing was erased from your tracker.", "verb": ""}
        pkg = (app_doc or {}).get("review_package")
        after_approval = st in ("authorized", "executing", "handoff", "needs_verification", "submitted", "failed")
        submitted = st == "submitted" or job.get("status") in ("applied", "applied_historical")
        sync_state = ((app_doc or {}).get("sync") or {}).get("state")
        recorded = submitted and (bool((app_doc or {}).get("tracker_record_id")) or sync_state in ("synced", "not_applicable") or job.get("status") == "applied_historical")
        flags = [
            ("import", True),
            ("qualify", bool(job.get("qualification"))),
            ("tailor", bool(arts)),
            ("review", bool(pkg) and st not in ("needs_input", "queued", "preparing")),
            ("authorize", after_approval or scope_active),
            ("submit", submitted),
            ("record", recorded),
            ("outreach", outreach_n > 0),
        ]
        names = {tech: (name, tip) for name, (tip, tech) in STEP_HELP.items()}
        current = next((key for key, done in flags if not done), None)
        steps = [{"key": k, "name": names[k][0], "tip": names[k][1], "done": d, "current": k == current} for k, d in flags]
        app_href = f"/applications/{app_doc['_id']}" if app_doc else None
        run_href = f"/runs/{active_run['_id']}" if active_run else None
        nxt: dict[str, Any] = {"label": "All done for this job", "href": None, "form": None, "note": ""}
        if job.get("status") == "excluded":
            nxt = {"label": "Skipped by your rules", "href": "/profile", "note": "Change your preferences under Your profile if this is wrong.", "verb": "Open preferences"}
        elif current == "qualify":
            note = "Your rules first, then a fit score. Nothing is sent."
            nxt = {"label": "Check how well this job fits", "form": f"/jobs/{job['_id']}/qualify", "verb": "Check fit", "note": note}
        elif current == "tailor":
            if active_run:
                nxt = {"label": "The app is tailoring the resume", "href": run_href, "verb": "Watch", "note": ""}
            elif job.get("status") == "needs_review":
                note = "Your rules could not decide on this job. Read the reasons below first."
                nxt = {"label": "Decide whether this job is worth preparing", "form": f"/jobs/{job['_id']}/run", "verb": "Prepare it anyway", "note": note}
            else:
                note = "Nothing is sent. The app stops and waits for your approval."
                nxt = {"label": "Tailor the resume and prepare the application", "form": f"/jobs/{job['_id']}/run", "verb": "Tailor resume", "note": note}
        elif current == "review":
            if st == "needs_input":
                nxt = {"label": "The form asks questions your profile cannot answer", "href": app_href, "verb": "Answer questions", "note": ""}
            elif st in ("duplicate", "blocked", "denied"):
                nxt = {"label": status_label(st), "href": app_href, "verb": "See why", "note": (app_doc or {}).get("reason") or ""}
            elif active_run:
                nxt = {"label": "The app is preparing the application package", "href": run_href, "verb": "Watch", "note": ""}
            else:
                nxt = {"label": "Prepare the application package", "form": f"/jobs/{job['_id']}/run", "verb": "Tailor resume", "note": ""}
        elif current == "authorize":
            note = "Your approval covers these answers and these exact files only."
            nxt = {"label": "Read the package and approve this exact application", "href": f"{app_href}#authorize", "verb": "Read and approve", "note": note}
        elif current == "submit":
            if st == "handoff":
                note = "The app cannot fill in this job site for you."
                nxt = {"label": "Apply on the job site yourself, then record the confirmation", "href": app_href, "verb": "Finish on the site", "note": note}
            elif st == "needs_verification":
                nxt = {"label": "The result is unclear. Check the job site", "href": app_href, "verb": "Check result", "note": ""}
            elif st == "failed":
                nxt = {"label": "Applying stopped early. Nothing was sent", "href": app_href, "verb": "See what failed", "note": plain_failure((app_doc or {}).get("reason"))}
            elif st == "executing":
                nxt = {"label": "Applying now", "href": run_href or app_href, "verb": "Watch", "note": ""}
            elif active_run and active_run.get("status") in ("queued", "claimed", "running"):
                nxt = {"label": "The app is applying now", "href": run_href or app_href, "verb": "Watch", "note": ""}
            elif active_run and active_run.get("requested") == "resume":
                nxt = {"label": "Approved. The task will continue within a minute", "href": run_href or app_href, "verb": "Watch", "note": ""}
            else:
                nxt = {"label": "Approved, and nothing is being sent right now", "href": app_href, "verb": "See updates", "note": "No task is working on this application."}
        elif current == "record":
            note = "A GitHub problem never changes whether you applied."
            nxt = {"label": "Applied. Not saved to your GitHub tracker yet", "href": "/sync", "verb": "Open GitHub tracker", "note": note}
        elif current == "outreach":
            note = "Drafts only. You send them yourself."
            nxt = {"label": "Applied. Next: short notes to recruiters at this employer", "href": "/outreach", "verb": "Contact recruiters", "note": note}
        return steps, nxt

    @app.post("/jobs/{job_id}/regenerate")
    def job_regenerate(request: Request, job_id: str, session: dict = Depends(require_session), csrf: str = Form(...)):
        """Re-tailor the documents for this job now (for example to get a PDF once LibreOffice exists). New files mean
        new hashes: single approvals for this job are revoked and a live review package is rebuilt."""
        require_csrf(request, session, csrf)
        job = rt.jobs.get(job_id)
        if not job:
            raise HTTPException(404)
        p = rt.reload_profile()
        base = p.base_resume
        if base is None or not Path(base.path).is_file():
            return RedirectResponse(f"/jobs/{job_id}?err=no_base", status_code=303)
        local = rt.store.c("applications").find_one({"job_id": job_id, "provenance.origin": "local"})
        st = (local or {}).get("status")
        if st in ("submitted", "executing"):
            return RedirectResponse(f"/jobs/{job_id}?err=already_submitted", status_code=303)
        if rt.store.c("runs").find_one({"params.job_id": job_id, "status": {"$in": ["queued", "claimed", "running"]}}):
            return RedirectResponse(f"/jobs/{job_id}?err=busy", status_code=303)
        rt.store.c("artifacts").update_many({"owner_id": job_id, "kind": "resume_tailored"}, {"$set": {"superseded": True}})
        art = rt.tailor().tailor_for_job(job, Path(base.path), base.sha256, p.version, display_name=p.fact_value("display_name") or "Resume")
        for s in rt.auth.active("application_submit"):
            if s.binding.target_id == job_id:
                rt.auth.revoke(s.id, "documents regenerated from the dashboard")
        rt.store.audit("documents_regenerated", job_id, artifact_id=art["_id"], sha256=art["sha256"], pdf=bool(art.get("pdf_path")))
        if local and st in ("needs_input", "ready_for_review", "authorized"):
            from amorehiremepls.apply.applications import FormField

            fields = [
                FormField(f.get("id", ""), f.get("label", ""), f.get("type", "text"), f.get("options", []), f.get("selector", ""), bool(f.get("required")))
                for f in (job.get("form_plan") or {}).get("fields", [])
            ]
            rt.applications().prepare(job_id, fields)
            return RedirectResponse(f"/jobs/{job_id}?ok=regenerated", status_code=303)
        return RedirectResponse(f"/jobs/{job_id}?ok={'regenerated_doc_only' if local else 'regenerated'}", status_code=303)

    @app.post("/jobs/{job_id}/check-posting")
    def job_check_posting(request: Request, job_id: str, session: dict = Depends(require_session), csrf: str = Form(...)):
        """Re-read this one posting even if it was checked recently. Does not submit."""
        require_csrf(request, session, csrf)
        if not rt.jobs.get(job_id):
            raise HTTPException(404)
        import httpx

        with httpx.Client(timeout=20.0, follow_redirects=True, headers={"User-Agent": "AmoreHireMePls/0.1 (posting reader)"}) as client:
            result = rt.discovery().check_posting(job_id, force=True, client=client, artifact_dir=rt.settings.artifact_dir)
        flash = {"closed": "posting_closed", "open": "posting_open", "kept_sent": "posting_kept", "unreadable": "posting_unreadable", "skipped": "posting_open"}[result["outcome"]]
        code = "ok" if result["outcome"] != "unreadable" else "err"
        return RedirectResponse(f"/jobs/{job_id}?{code}={flash}", status_code=303)

    @app.post("/jobs/{job_id}/delete-application")
    def job_delete_application(request: Request, job_id: str, session: dict = Depends(require_session), csrf: str = Form(...), confirm: str = Form("")):
        """Remove the unsent application for this job. The job itself stays."""
        require_csrf(request, session, csrf)
        if confirm.strip().lower() != "delete":
            return RedirectResponse(f"/jobs/{job_id}?err=confirm_needed", status_code=303)
        local = rt.store.c("applications").find_one({"job_id": job_id, "provenance.origin": "local"})
        if local is None:
            return RedirectResponse(f"/jobs/{job_id}?err=missing", status_code=303)
        from amorehiremepls.web.routes.applications import _clear_application

        return _clear_application(rt, local["_id"], "delete")

    @app.post("/jobs/{job_id}/restart")
    def job_restart(request: Request, job_id: str, session: dict = Depends(require_session), csrf: str = Form(...), confirm: str = Form("")):
        """Clear the unsent application workflow so the package can be read and approved again. Does not submit."""
        require_csrf(request, session, csrf)
        if confirm.strip().lower() != "restart":
            return RedirectResponse(f"/jobs/{job_id}?err=confirm_needed", status_code=303)
        local = rt.store.c("applications").find_one({"job_id": job_id, "provenance.origin": "local"})
        if local is None:
            return RedirectResponse(f"/jobs/{job_id}?err=missing", status_code=303)
        from amorehiremepls.web.routes.applications import _clear_application

        return _clear_application(rt, local["_id"], "restart")

    @app.post("/jobs/{job_id}/qualify")
    def job_qualify(request: Request, job_id: str, session: dict = Depends(require_session), csrf: str = Form(...)):
        require_csrf(request, session, csrf)
        d = rt.discovery()
        d.qualify_job(job_id)
        try:
            d.rank_fit(job_id)
        except Exception as exc:
            rt.store.audit("rank_failed", job_id, error=type(exc).__name__)
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/jobs/{job_id}/run")
    def job_run(request: Request, job_id: str, session: dict = Depends(require_session), csrf: str = Form(...)):
        require_csrf(request, session, csrf)
        if rt.store.c("runs").find_one({"params.job_id": job_id, "status": {"$in": ["queued", "claimed", "running", "waiting", "paused"]}}):
            raise HTTPException(409, "a run for this job is already active")
        run = rt.store.create_run("application", {"job_id": job_id})
        return RedirectResponse(f"/runs/{run['_id']}", status_code=303)

    @app.post("/jobs/{job_id}/form-plan")
    def job_form_plan(request: Request, job_id: str, session: dict = Depends(require_session), csrf: str = Form(...), plan_json: str = Form(...)):
        require_csrf(request, session, csrf)
        plan = json.loads(plan_json)
        job = rt.jobs.get(job_id)
        rt.jobs.set_status(job_id, job["status"], form_plan=plan, apply_route="generic_form")
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/runs/batch")
    def batch_run(request: Request, session: dict = Depends(require_session), csrf: str = Form(...), max_count: int = Form(3), status_in: str = Form("qualified")):
        require_csrf(request, session, csrf)
        created = 0
        for job in rt.store.c("jobs").find({"status": status_in}).sort([("fit.rank", 1), ("updated_at", -1)]).limit(max_count):
            if not rt.store.c("runs").find_one({"params.job_id": job["_id"], "status": {"$in": ["queued", "claimed", "running", "waiting", "paused"]}}):
                rt.store.create_run("application", {"job_id": job["_id"], "batch": True})
                created += 1
        rt.store.audit("batch_runs_created", None, count=created)
        return RedirectResponse("/runs", status_code=303)
