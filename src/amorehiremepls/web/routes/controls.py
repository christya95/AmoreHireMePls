"""Routes registered by create_app."""

from __future__ import annotations

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


def register(app, rt, require_session, require_csrf, render) -> None:
    # -- controls ---------------------------------------------------------------------------
    @app.post("/controls/emergency-stop")
    def emergency(request: Request, session: dict = Depends(require_session), csrf: str = Form(...), active: str = Form("on"), reason: str = Form("")):
        require_csrf(request, session, csrf)
        rt.store.set_emergency_stop(active == "on", reason or "dashboard")
        return RedirectResponse("/", status_code=303)

    @app.post("/runs/{run_id}/{what}")
    def run_control(request: Request, run_id: str, what: str, session: dict = Depends(require_session), csrf: str = Form(...), payload: str = Form("")):
        require_csrf(request, session, csrf)
        if what in ("pause", "cancel"):
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
        runs = list(rt.store.c("runs").find().sort("created_at", -1).limit(100))
        jobs = {j["_id"]: j for j in rt.store.c("jobs").find({"_id": {"$in": [r.get("params", {}).get("job_id") for r in runs]}}, {"employer": 1, "title": 1})}
        return render(request, "runs.html", session, runs=runs, jobs=jobs, tab="runs")

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
        if not run:
            raise HTTPException(404)
        if session.get("role") == "member":
            from amorehiremepls.web.chrome import without_costs

            run = {**run, "summary": without_costs(run.get("summary")), "events": without_costs(run.get("events") or []), "usage": {}}
        return render(request, "run.html", session, run=run)
