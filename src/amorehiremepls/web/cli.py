"""Command line entry points: web, worker, setup, imports, browser sessions, mail access, backup/restore."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from amorehiremepls.core.config import Settings, get_settings
from amorehiremepls.core.logging_config import configure_logging

app = typer.Typer(no_args_is_help=True, add_completion=False, help="AmoreHireMePls")
tracker_app = typer.Typer(help="Tracker (GitHub ledger) operations")
profile_app = typer.Typer(help="Private profile operations")
browser_app = typer.Typer(help="Authenticated browser sessions (Mac)")
app.add_typer(tracker_app, name="tracker")
app.add_typer(profile_app, name="profile")
app.add_typer(browser_app, name="browser")
mail_app = typer.Typer(help="Read-only mail access for verification emails (run on the computer you sit at)")
app.add_typer(mail_app, name="mail")


def _rt(settings: Settings | None = None):
    from amorehiremepls.core.runtime import Runtime

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

    from amorehiremepls.web.web import create_app

    rt = _rt()
    if len(rt.settings.app_password.get_secret_value()) < 12:
        typer.echo("WARNING: AMORE_APP_PASSWORD is shorter than 12 characters; login is disabled until it is set.", err=True)
    uvicorn.run(create_app(rt), host=host or rt.settings.bind_host, port=port or rt.settings.port, log_level="info", access_log=False)


@app.command()
def worker(once: bool = typer.Option(False, help="Run one tick and exit")):
    """Run the workflow worker (runs, sync coordinator, scheduler)."""
    from amorehiremepls.core.worker import Worker

    rt = _rt()
    w = Worker(rt)
    if once:
        _echo(w.tick())
    else:
        w.run_forever()


@app.command("worker-health")
def worker_health():
    """Exit 0 when the worker heartbeat is recent (used by the Docker health check)."""
    from amorehiremepls.core.worker import worker_healthy

    rt = _rt()
    ok = worker_healthy(rt)
    typer.echo("healthy" if ok else "stale heartbeat")
    raise typer.Exit(0 if ok else 1)


@app.command("migrate-users")
def migrate_users_cmd():
    """Create the admin account (no expiry) and stamp existing records with that person. Safe to run again.

    Prints counts only. The sign-in password is the current dashboard password."""
    from amorehiremepls.core.accounts import AccountError, migrate_users
    from amorehiremepls.web.ui_settings import UISettingsStore, resolve_display_name

    rt = _rt()
    name = resolve_display_name(UISettingsStore(rt.store).load(), rt.profile)
    try:
        report = migrate_users(rt.store, rt.settings.app_password.get_secret_value(), name)
    except AccountError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    _echo(report)


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
    from_path: Path = typer.Option(Path("candidate/profile.toml"), "--from", help="Your filled in candidate/profile.toml (copy candidate/profile.example.toml to start)."),
    dry_run: bool = typer.Option(False, help="Parse and apply in memory, print counts and fingerprints, save nothing."),
    confirmed_on: str | None = typer.Option(None, help="YYYY-MM-DD you confirmed these facts; today when omitted."),
):
    """Load candidate/profile.toml into the protected profile store (never into git). Sections present in the file replace
    the saved values; facts, answers, employment and education are matched by key. Prints counts only, never values."""
    from amorehiremepls.candidate.candidate_profile import CONTROL_KEY as TOML_CONTROL_KEY
    from amorehiremepls.candidate.candidate_profile import ProfileTomlError, apply_profile_toml, load_profile_toml, preference_fingerprint
    from amorehiremepls.core.util import sha256_file, utcnow

    rt = _rt()
    try:
        data = load_profile_toml(from_path)
    except ProfileTomlError as exc:
        typer.echo(f"profile.toml problem: {exc}", err=True)
        raise typer.Exit(1) from exc
    p = rt.profiles.load()
    report = apply_profile_toml(p, data, confirmed_on=confirmed_on or utcnow().date().isoformat(), store=None if dry_run else rt.store)
    out = {"dry_run": dry_run, "file_sha256": sha256_file(from_path), **report.as_dict(), "fingerprint": preference_fingerprint(p)}
    if not dry_run:
        rt.profiles.save(p, actor="cli:profile.toml")
        rt.store.set_control(TOML_CONTROL_KEY, {"sha256": out["file_sha256"], "loaded_at": utcnow().isoformat(), "path": str(from_path)})
    _echo(out)


@profile_app.command("export")
def profile_export(
    to_path: Path = typer.Option(Path("candidate/profile.toml"), "--to", help="Where to write; must be under the candidate folder or runtime/ (both gitignored)."),
    facts: bool = typer.Option(True, help="Include personal facts, answers, employment and education (not only preferences)."),
    force: bool = typer.Option(False, help="Overwrite an existing file."),
):
    """Write the saved profile back out in the profile.toml shape, so an existing setup becomes an editable file."""
    from amorehiremepls.candidate.candidate_profile import export_profile_toml

    rt = _rt()
    resolved = to_path.resolve()
    allowed = (rt.settings.candidate_dir.resolve(), Path("runtime").resolve())
    if not any(str(resolved).startswith(str(a)) for a in allowed):
        typer.echo("refusing to write personal data outside the candidate folder or runtime/", err=True)
        raise typer.Exit(1)
    if resolved.exists() and not force:
        typer.echo(f"{to_path} exists; pass --force to overwrite", err=True)
        raise typer.Exit(1)
    text = export_profile_toml(rt.profiles.load(), include_facts=facts)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(text, encoding="utf-8")
    _echo({"written": str(to_path), "bytes": len(text.encode("utf-8")), "facts_included": facts})


@profile_app.command("fingerprint")
def profile_fingerprint():
    """Hashes of each preference block of the saved profile. Compare two setups without printing any values."""
    from amorehiremepls.candidate.candidate_profile import preference_fingerprint

    rt = _rt()
    _echo(preference_fingerprint(rt.profiles.load()))


@profile_app.command("import-skills")
def profile_import_skills(
    facts_skill: Path | None = typer.Option(None, help="Path to a private application-facts SKILL.md (legacy format)"),
    salary_skill: Path | None = typer.Option(None, help="Path to a private desired-salary SKILL.md (legacy format)"),
    apply_skill: Path | None = typer.Option(None, help="Path to a private apply-jobs SKILL.md (employment dates, work authorization, degree, confirmed answers)"),
    overrides: Path | None = typer.Option(None, help="Private JSON under runtime/ with employment/education/answers/skills not expressible in the skills"),
    confirmed_on: str | None = typer.Option(None, help="YYYY-MM-DD the facts were confirmed; today when omitted"),
    replace: bool = False,
):
    """Legacy path: import approved facts from the original markdown skill files. New setups use `amore profile import`."""
    from amorehiremepls.candidate.profile import build_profile_from_skills, merge_employment, refresh_unknowns

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
def tracker_clone(url: str = typer.Option("", help="Git URL of your private application ledger; defaults to AMORE_TRACKER_REMOTE_URL.")):
    """Clone the private tracker into the configured private clone dir (needs git credentials on this host)."""
    from amorehiremepls.records.sync import ensure_clone

    rt = _rt()
    url = url or rt.settings.tracker_remote_url
    if not url:
        typer.echo("no tracker URL: set AMORE_TRACKER_REMOTE_URL (and AMORE_TRACKER_ENABLED=true) or pass --url", err=True)
        raise typer.Exit(1)
    ensure_clone(rt.settings.tracker_clone_dir, url)
    _echo(rt.sync.status())


@tracker_app.command("preview")
def tracker_preview(clone: Path | None = None):
    from amorehiremepls.records.tracker import TrackerClone, TrackerImporter

    rt = _rt()
    _echo(TrackerImporter(rt.store, TrackerClone(clone or rt.settings.tracker_clone_dir)).preview().as_dict())


@tracker_app.command("import")
def tracker_import(clone: Path | None = None):
    from amorehiremepls.records.tracker import TrackerClone, TrackerImporter

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


# -- mail --------------------------------------------------------------------------------------

_MAIL_PROBLEMS = {
    "client_file": "That file is not a Desktop app client file from Google Cloud. Nothing was changed.",
    "no_client": "No earlier connection was found to reuse. Pass --client-file with the client file from Google Cloud.",
    "no_browser": "A browser could not be opened on this computer. Nothing was changed.",
    "consent_timeout": "No answer came back from the browser in time. Nothing was changed. Run the command again.",
    "consent_denied": "Access was not granted. Nothing was changed.",
    "consent_scope": "Read-only mail access was left unticked on the consent page. Nothing was saved. Run the command again and allow it.",
    "not_approved": "1Password was not approved in time. Nothing was saved. Run the command again.",
    "vault_missing": "The vault for the app's own items is not set or was not found in 1Password. Check AMORE_SITE_ACCOUNT_VAULT. Nothing was saved.",
    "not_installed": "The 1Password command line tool is not on this computer. Nothing was saved.",
    "transient": "The mail service could not be reached. Nothing was changed. Run the command again.",
}


def _mail_problem(exc: Exception) -> None:
    kind = getattr(exc, "detail", "") or getattr(exc, "kind", "") or "failed"
    typer.echo(_MAIL_PROBLEMS.get(kind, f"Mail access could not be changed ({kind}). Nothing was saved."), err=True)
    raise typer.Exit(1) from exc


@mail_app.command("connect")
def mail_connect(
    client_file: Path | None = typer.Option(None, "--client-file", help="The Desktop app client file from Google Cloud. Omit it to connect again with the client saved last time."),
):
    """Open the browser to allow read-only mail access, then save the grant. Prints the address, never a token."""
    from amorehiremepls.core.credentials import LoginLookupError
    from amorehiremepls.core.mail_access import STATES, MailError, read_client_file
    from amorehiremepls.core.util import local_now

    rt = _rt()
    access = rt.mail_access()
    try:
        client = read_client_file(client_file) if client_file is not None else access.saved_client()
        if client is None:
            raise MailError("no_client")
        address = access.connect(*client, login_hint=rt.account_email(), today=local_now().date().isoformat())
    except (MailError, LoginLookupError) as exc:
        _mail_problem(exc)
    _echo(
        {
            "state": "connected",
            "meaning": STATES["connected"],
            "address": address,
            "is_account_email": address.lower() == rt.account_email().lower(),
            "kept_in": rt.settings.mail_grant_store,
        }
    )


@mail_app.command("status")
def mail_status(check: bool = typer.Option(True, help="Also ask the mail service whether the grant still works.")):
    """Whether mail access is connected and for which address. Never prints a token."""
    from amorehiremepls.core.mail_access import STATES, MailTransient

    rt = _rt()
    access = rt.mail_access()
    state = access.unlock()
    reached = True
    if state == "connected" and check:
        try:
            state = access.check()
        except MailTransient:
            reached = False
    out = {
        "state": state,
        "meaning": STATES.get(state, state),
        "address": access.address(),
        "kept_in": rt.settings.mail_grant_store,
        "reading_is_on": rt.mail_verification_enabled(),
    }
    if access.address():
        out["is_account_email"] = access.address().lower() == rt.account_email().lower()
    if not reached:
        out["note"] = "The mail service could not be reached just now, so the grant was not checked."
    _echo(out)


@mail_app.command("disconnect")
def mail_disconnect():
    """Withdraw the mail grant and delete it from where it is kept."""
    from amorehiremepls.core.credentials import LoginLookupError
    from amorehiremepls.core.mail_access import STATES, MailError

    rt = _rt()
    try:
        done = rt.mail_access().disconnect()
    except (MailError, LoginLookupError) as exc:
        _mail_problem(exc)
    _echo({"state": "not_connected", "meaning": STATES["not_connected"], **done})


# -- backup ------------------------------------------------------------------------------------


@app.command()
def backup(out: Path):
    """Back up MongoDB collections + artifacts + private dir + config examples into one archive."""
    from amorehiremepls.core.backup import backup as do_backup

    rt = _rt()
    _echo(do_backup(rt.store, rt.settings.artifact_dir, rt.settings.private_dir, out, repo_root=Path(__file__).resolve().parents[3]))


@app.command()
def restore(archive: Path, force: bool = typer.Option(False, help="Overwrite a non-empty target database")):
    """Restore an archive into the configured database and directories (refuses non-empty targets without --force)."""
    from amorehiremepls.core.backup import restore as do_restore

    rt = _rt()
    _echo(do_restore(rt.store, archive, rt.settings.artifact_dir, rt.settings.private_dir, force=force))


if __name__ == "__main__":
    app()
