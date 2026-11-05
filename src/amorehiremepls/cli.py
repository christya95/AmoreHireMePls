"""Command line entry points: web, worker, setup, imports, browser sessions, backup/restore."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from .config import Settings, get_settings
from .logging_config import configure_logging

app = typer.Typer(no_args_is_help=True, add_completion=False, help="AmoreHireMePls")
tracker_app = typer.Typer(help="Tracker (GitHub ledger) operations")
profile_app = typer.Typer(help="Private profile operations")
browser_app = typer.Typer(help="Authenticated browser sessions (Mac)")
app.add_typer(tracker_app, name="tracker")
app.add_typer(profile_app, name="profile")
app.add_typer(browser_app, name="browser")


def _rt(settings: Settings | None = None):
    from .runtime import Runtime

    s = settings or get_settings()
    configure_logging(s.log_dir)
    rt = Runtime(s)
    rt.store.ensure_indexes()
    return rt


def _echo(obj) -> None:
    typer.echo(json.dumps(obj, indent=2, default=str))


@app.command()
def web(host: str | None = None, port: int | None = None):
    """Run the dashboard/API."""
    import uvicorn

    from .web import create_app

    rt = _rt()
    if len(rt.settings.app_password.get_secret_value()) < 12:
        typer.echo("WARNING: AMORE_APP_PASSWORD is shorter than 12 characters; login is disabled until it is set.", err=True)
    uvicorn.run(create_app(rt), host=host or rt.settings.bind_host, port=port or rt.settings.port, log_level="info", access_log=False)


@app.command()
def worker(once: bool = typer.Option(False, help="Run one tick and exit")):
    """Run the workflow worker (runs, sync coordinator, scheduler)."""
    from .worker import Worker

    rt = _rt()
    w = Worker(rt)
    if once:
        _echo(w.tick())
    else:
        w.run_forever()


@app.command("worker-health")
def worker_health():
    """Exit 0 when the worker heartbeat is recent (used by the Docker health check)."""
    from .worker import worker_healthy

    rt = _rt()
    ok = worker_healthy(rt)
    typer.echo("healthy" if ok else "stale heartbeat")
    raise typer.Exit(0 if ok else 1)


@app.command("setup-db")
def setup_db():
    """Create indexes and verify the checkpointer against the configured MongoDB."""
    rt = _rt()
    _echo({"ping": rt.store.ping(), "indexes": rt.store.ensure_indexes(), "checkpointer": type(rt.checkpointer()).__name__})


@app.command("provider-check")
def provider_check():
    _echo(_rt().provider_check())


@app.command()
def health():
    _echo(_rt().health())


@app.command("emergency-stop")
def emergency_stop(active: bool = True, reason: str = "cli"):
    rt = _rt()
    rt.store.set_emergency_stop(active, reason)
    typer.echo(f"emergency stop {'ACTIVE' if active else 'cleared'}")


# -- profile ---------------------------------------------------------------------------


@profile_app.command("import")
def profile_import(
    facts_skill: Path | None = typer.Option(None, help="Path to the private joss-application-facts SKILL.md"),
    salary_skill: Path | None = typer.Option(None, help="Path to the private joss-desired-salary SKILL.md"),
    apply_skill: Path | None = typer.Option(None, help="Path to the private apply-joss-jobs SKILL.md (employment dates, work authorization, degree, confirmed answers)"),
    overrides: Path | None = typer.Option(None, help="Private JSON under runtime/ with employment/education/answers/skills not expressible in the skills"),
    confirmed_on: str = "2026-10-15",
    replace: bool = False,
):
    """Import approved facts into the protected profile store (never into git). Nothing personal is defaulted in code."""
    from .profile import build_profile_from_skills, merge_employment, refresh_unknowns

    rt = _rt()
    existing = rt.profiles.load()
    if existing.facts and not replace:
        typer.echo("profile already has facts; pass --replace to rebuild from the skill files", err=True)
        raise typer.Exit(1)
    p = build_profile_from_skills(
        facts_skill.read_text(encoding="utf-8") if facts_skill else None,
        salary_skill.read_text(encoding="utf-8") if salary_skill else None,
        confirmed_on,
        apply_text=apply_skill.read_text(encoding="utf-8") if apply_skill else None,
        overrides=json.loads(overrides.read_text(encoding="utf-8")) if overrides else None,
    )
    p.base_resume = existing.base_resume
    if existing.base_resume:  # keep base-resume-derived rows (titles/internships) across re-imports
        p.employment = merge_employment(p.employment, [e for e in existing.employment if e.provenance.source.startswith("base_resume:")])
        p.unknowns = refresh_unknowns(p)
    rt.profiles.save(p, actor="cli:import")
    _echo(
        {
            "facts": len(p.facts),
            "answers": len(p.answers),
            "education": len(p.education),
            "employment": len(p.employment),
            "floor_set": bool(p.compensation.floor_base),
            "unknowns": p.unknowns,
            "base_resume": bool(p.base_resume),
        }
    )


@profile_app.command("base-resume")
def profile_base_resume(
    path: Path | None = typer.Argument(None, help="The DOCX to approve. Omitted: the resume found in the candidate folder (AMORE_CANDIDATE_DIR)."),
    approved_on: str = typer.Option(..., help="YYYY-MM-DD the user approved this base"),
    note: str = "",
):
    """Select the approved base resume DOCX (copied into the private dir, hashed, never modified)."""
    rt = _rt()
    source = "cli:user"
    if path is None:
        docs = rt.candidate_docs()
        if docs.base_resume is None:
            typer.echo(f"no .docx found in the candidate folder {docs.directory}; pass a path or drop the resume there", err=True)
            raise typer.Exit(1)
        for w in docs.warnings:
            typer.echo(w, err=True)
        path, source = docs.base_resume, "cli:candidate_folder"
    p = rt.profiles.select_base_resume(path, approved_on, source, rt.settings.private_dir, note)
    _echo(p.base_resume.model_dump())


@profile_app.command("candidate-docs")
def profile_candidate_docs():
    """What the candidate folder holds: the resume picked as base, the extra documents, their size, and any warnings."""
    rt = _rt()
    docs = rt.candidate_docs()
    out = docs.as_dict()
    if docs.base_resume is not None and rt.profile.base_resume is not None:
        out["base_resume_matches_approved"] = rt.profile.base_resume.sha256 == docs.base_resume_sha256
    _echo(out)


@profile_app.command("show")
def profile_show(redact: bool = True):
    rt = _rt()
    p = rt.profiles.load()
    data = p.model_dump()
    if redact:
        for f in data["facts"]:
            if f["category"] in ("address", "contact"):
                f["value"] = "***"
    _echo(data)


# -- tracker -----------------------------------------------------------------------------


@tracker_app.command("clone")
def tracker_clone(url: str = "https://github.com/christya95/job-application-records.git"):
    """Clone the private tracker into the configured private clone dir (needs git credentials on this host)."""
    from .sync import ensure_clone

    rt = _rt()
    ensure_clone(rt.settings.tracker_clone_dir, url)
    _echo(rt.sync.status())


@tracker_app.command("preview")
def tracker_preview(clone: Path | None = None):
    from .tracker import TrackerClone, TrackerImporter

    rt = _rt()
    _echo(TrackerImporter(rt.store, TrackerClone(clone or rt.settings.tracker_clone_dir)).preview().as_dict())


@tracker_app.command("import")
def tracker_import(clone: Path | None = None):
    from .tracker import TrackerClone, TrackerImporter

    rt = _rt()
    _echo(TrackerImporter(rt.store, TrackerClone(clone or rt.settings.tracker_clone_dir)).import_all())


@tracker_app.command("sync")
def tracker_sync(reconcile: bool = False):
    rt = _rt()
    if reconcile:
        _echo(rt.sync.reconcile_all())
    _echo(rt.sync.process_pending(owner="cli"))
    _echo(rt.sync.status())


# -- browser -------------------------------------------------------------------------------


@browser_app.command("login")
def browser_login(site: str = typer.Argument(..., help="site key, e.g. myworkdayjobs.com"), url: str = typer.Argument(...), done_url: str | None = None):
    """Open a headed browser on this Mac for you to sign in; saves the session privately for the worker."""
    rt = _rt()
    rt.browser.config.headless = False
    if site.lower() not in rt.settings.allowed_hosts and url.split("/")[2].lower() not in rt.settings.allowed_hosts:
        typer.echo("host not in AMORE_BROWSER_ALLOWED_HOSTS; add it first", err=True)
        raise typer.Exit(1)
    path = rt.browser.login_interactive(site, url, done_url)
    typer.echo(f"session saved: {path}")


@browser_app.command("status")
def browser_status(site: str):
    _echo(_rt().browser.session_status(site))


@browser_app.command("logout")
def browser_logout(site: str):
    typer.echo("removed" if _rt().browser.logout(site) else "no session")


# -- backup ------------------------------------------------------------------------------------


@app.command()
def backup(out: Path):
    """Back up MongoDB collections + artifacts + private dir + config examples into one archive."""
    from .backup import backup as do_backup

    rt = _rt()
    _echo(do_backup(rt.store, rt.settings.artifact_dir, rt.settings.private_dir, out, repo_root=Path(__file__).resolve().parents[2]))


@app.command()
def restore(archive: Path, force: bool = typer.Option(False, help="Overwrite a non-empty target database")):
    """Restore an archive into the configured database and directories (refuses non-empty targets without --force)."""
    from .backup import restore as do_restore

    rt = _rt()
    _echo(do_restore(rt.store, archive, rt.settings.artifact_dir, rt.settings.private_dir, force=force))


if __name__ == "__main__":
    app()
