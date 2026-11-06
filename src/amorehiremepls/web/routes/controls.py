"""Routes registered by create_app."""

from __future__ import annotations

from amorehiremepls.core.util import utcnow
from amorehiremepls.web.chrome import (
    GLOSSARY,
    HOSTS_ENV_EXAMPLE,
    STEP_HELP,
    Depends,
    Form,
    HTMLResponse,
    HTTPException,
    RedirectResponse,
    Request,
    json,
)
from amorehiremepls.web.task_list import (
    SEND_KINDS,
    can_cancel,
    can_hide,
    clear_plan,
    next_application,
    next_name,
    offer_cancel,
    owner_state_for,
    partition,
    queue_summary,
    queued_reason,
    side_effect_started,
    task_title,
)


def register(app, rt, require_session, require_csrf, render) -> None:
    # -- controls ---------------------------------------------------------------------------
    @app.post("/controls/emergency-stop")
    def emergency(request: Request, session: dict = Depends(require_session), csrf: str = Form(...), active: str = Form("on"), reason: str = Form("")):
        require_csrf(request, session, csrf)
        rt.store.set_emergency_stop(active == "on", reason or "dashboard")
        return RedirectResponse("/", status_code=303)

    def _runs_redirect(run_id: str, nxt: str) -> RedirectResponse:
        dest = "/runs" if nxt == "/runs" else f"/runs/{run_id}"
        return RedirectResponse(dest, status_code=303)

    @app.post("/runs/hide-finished")
    def hide_finished(request: Request, session: dict = Depends(require_session), csrf: str = Form(...)):
        """Hide every finished, cancelled, and failed task from this list. Documents stay."""
        require_csrf(request, session, csrf)
        n = 0
        for run in rt.store.c("runs").find({"dismissed": {"$ne": True}}):
            if can_hide(run):
                rt.store.update_run(run["_id"], dismissed=True)
                rt.store.run_event(run["_id"], "hidden from the task list")
                n += 1
        if n:
            rt.store.audit("runs_hidden", None, count=n)
        return RedirectResponse("/runs", status_code=303)

    @app.post("/runs/clear")
    def clear_tasks(request: Request, session: dict = Depends(require_session), csrf: str = Form(...), confirm: str = Form("")):
        """Cancel queued tasks that have not started, and hide finished ones. A task that is already sending stays."""
        require_csrf(request, session, csrf)
        if confirm.strip().lower() != "clear":
            return RedirectResponse("/runs?err=confirm_needed", status_code=303)
        runs = list(rt.store.c("runs").find({"dismissed": {"$ne": True}}))
        busy_jobs = {a.get("job_id") for a in rt.store.c("applications").find({"status": "executing"}, {"job_id": 1})}
        busy_runs = {a.get("run_id") for a in rt.store.c("actions").find({"status": "executing", "kind": {"$in": sorted(SEND_KINDS)}}, {"run_id": 1})}
        cancel_ids, hide_ids, left = clear_plan(runs, busy_runs, busy_jobs)
        for run_id in cancel_ids:
            settled = rt.store.c("runs").update_one(
                {"_id": run_id, "status": "queued"},
                {"$set": {"status": "cancelled", "requested": None, "interrupt": None, "finished_at": utcnow(), "note": "cancelled before it started", "updated_at": utcnow()}},
            )
            if settled.modified_count != 1:
                left += 1
                hide_ids = [item for item in hide_ids if item != run_id]
                continue
            rt.store.run_event(run_id, "cancelled before it started")
        for run_id in hide_ids:
            rt.store.update_run(run_id, dismissed=True)
            rt.store.run_event(run_id, "hidden from the task list")
        if cancel_ids or hide_ids:
            rt.store.audit("runs_cleared", None, cancelled=len(cancel_ids), hidden=len(hide_ids), left=left)
        return RedirectResponse(f"/runs?ok=tasks_cleared&left={left}", status_code=303)

    @app.post("/runs/{run_id}/{what}")
    def run_control(request: Request, run_id: str, what: str, session: dict = Depends(require_session), csrf: str = Form(...), payload: str = Form(""), next: str = Form("")):
        require_csrf(request, session, csrf)
        run = rt.store.c("runs").find_one({"_id": run_id})
        if run is None:
            raise HTTPException(404)
        if what == "hide":
            if not can_hide(run):
                raise HTTPException(409, "only a finished task can be hidden")
            rt.store.update_run(run_id, dismissed=True)
            rt.store.run_event(run_id, "hidden from the task list")
            rt.store.audit("run_hidden", run_id)
            return _runs_redirect(run_id, next or "/runs")
        if what == "cancel":
            if side_effect_started(rt.store, run):
                raise HTTPException(409, "this task has already started sending")
            if can_cancel(run):
                settled = rt.store.c("runs").update_one(
                    {"_id": run_id, "status": "queued"},
                    {"$set": {"status": "cancelled", "requested": None, "interrupt": None, "finished_at": utcnow(), "note": "cancelled before it started", "updated_at": utcnow()}},
                )
                if settled.modified_count == 1:
                    rt.store.run_event(run_id, "cancelled before it started")
                    rt.store.audit("run_cancelled", run_id)
                    return _runs_redirect(run_id, next or "/runs")
                run = rt.store.c("runs").find_one({"_id": run_id}) or run
            if run.get("status") in ("completed", "succeeded", "cancelled", "failed"):
                raise HTTPException(409, "this task has already finished")
            rt.store.update_run(run_id, requested="cancel")
            rt.store.audit("run_cancel_requested", run_id)
            return _runs_redirect(run_id, next)
        if what == "pause":
            rt.store.update_run(run_id, requested=what)
        elif what == "resume":
            data = json.loads(payload) if payload else None
            rt.store.update_run(run_id, requested="resume", resume_payload=data)
        else:
            raise HTTPException(404)
        rt.store.audit(f"run_{what}_requested", run_id)
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.get("/runs", response_class=HTMLResponse)
    def runs(request: Request, session: dict = Depends(require_session)):
        found = list(rt.store.c("runs").find({"dismissed": {"$ne": True}}).sort("created_at", -1).limit(100))
        job_ids = [r.get("params", {}).get("job_id") for r in found if r.get("params", {}).get("job_id")]
        jobs = {j["_id"]: j for j in rt.store.c("jobs").find({"_id": {"$in": job_ids}}, {"employer": 1, "title": 1})}
        waiting, earlier = partition(found)
        nxt = next_application(waiting)
        summary = queue_summary(waiting, next_name(nxt, jobs))
        busy_jobs = {a.get("job_id") for a in rt.store.c("applications").find({"status": "executing"}, {"job_id": 1})}
        busy_runs = {a.get("run_id") for a in rt.store.c("actions").find({"status": "executing", "kind": {"$in": sorted(SEND_KINDS)}}, {"run_id": 1})}
        raw_left = request.query_params.get("left") or ""
        left_running = int(raw_left) if request.query_params.get("ok") == "tasks_cleared" and raw_left.isdigit() else 0
        return render(
            request,
            "runs.html",
            session,
            waiting=waiting,
            earlier=earlier,
            jobs=jobs,
            summary=summary,
            busy_jobs=busy_jobs,
            busy_runs=busy_runs,
            left_running=left_running,
            tab="runs",
        )

    @app.get("/help", response_class=HTMLResponse)
    def help_page(request: Request, session: dict = Depends(require_session)):
        s = rt.settings
        places = [
            ("Tailored resumes and screenshots", str(s.artifact_dir.resolve())),
            ("Your profile, a copy of the base resume, avatar, browser sign-ins", str(s.private_dir.resolve())),
            ("Your GitHub tracker, downloaded", str(s.tracker_clone_dir.resolve())),
            ("Logs and process ID files", str(s.log_dir.resolve())),
            ("AI model prices", str(s.pricing_file.resolve())),
            ("Everything else the app remembers", f"MongoDB database '{s.mongodb_database}'"),
        ]
        order = ["authorization", "material_hash", "batch", "batch_scope", "needs_verification", "handoff", "emergency_stop", "sync_states", "outreach_scope", "fit_rank"]
        order += ["grounding", "regenerate", "evidence", "task", "snapshot", "tracker"]
        statuses = {
            "Applications": ["queued", "ready_for_review", "needs_input", "authorized", "executing", "handoff", "needs_verification", "submitted", "failed", "duplicate"],
            "Jobs": ["new", "needs_review", "qualified", "applied", "applied_historical", "excluded"],
            "Background tasks": ["queued", "running", "waiting", "paused", "failed", "succeeded"],
            "Saving to GitHub": ["sync_pending", "committed_local", "local_only", "synced", "conflict"],
        }
        concepts = [(k, *GLOSSARY[k]) for k in order]
        steps = [(name, tip, tech) for name, (tip, tech) in STEP_HELP.items()]
        ctx = {"places": places, "concepts": concepts, "steps": steps, "statuses": statuses, "hosts_example": HOSTS_ENV_EXAMPLE, "allowed_hosts": sorted(s.allowed_hosts)}
        return render(request, "help.html", session, **ctx)

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_detail(request: Request, run_id: str, session: dict = Depends(require_session)):
        run = rt.store.c("runs").find_one({"_id": run_id})
        if not run and session.get("role") != "member":
            run = rt.store.db["runs"].find_one({"_id": run_id})
        if not run:
            raise HTTPException(404)
        if session.get("role") == "member":
            from amorehiremepls.web.chrome import without_costs

            run = {**run, "summary": without_costs(run.get("summary")), "events": without_costs(run.get("events") or []), "usage": {}}
        accounts = rt.store.db["users"].count_documents({}) > 0
        owner = rt.store.db["users"].find_one({"_id": run.get("user_id")}, {"status": 1, "role": 1, "access_until": 1}) if run.get("user_id") else None
        heartbeat = rt.store.get_control("worker_heartbeat") or {}
        reason = queued_reason(
            run,
            now=utcnow(),
            worker_at=heartbeat.get("at"),
            owner_state=owner_state_for(run, owner, accounts),
            emergency=rt.store.emergency_stop_active(),
        )
        job_id = (run.get("params") or {}).get("job_id")
        job = rt.store.c("jobs").find_one({"_id": job_id}, {"employer": 1, "title": 1}) if job_id else None
        if job is None and job_id and session.get("role") != "member":
            job = rt.store.db["jobs"].find_one({"_id": job_id}, {"employer": 1, "title": 1})
        return render(
            request,
            "run.html",
            session,
            run=run,
            run_title=task_title(run, job),
            can_cancel_detail=offer_cancel(run, side_effect_started(rt.store, run)),
            queue_reason=reason,
        )
