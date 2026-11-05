"""Routes registered by create_app."""

from __future__ import annotations

from amorehiremepls.web.chrome import (
    PROFILE_TOML_NAME,
    TOML_CONTROL_KEY,
    Depends,
    ExcludedEmployer,
    File,
    Form,
    HTMLResponse,
    HTTPException,
    JSONResponse,
    Path,
    ProfileTomlError,
    Provenance,
    RecipientExclusion,
    RedirectResponse,
    Request,
    UploadFile,
    apply_profile_toml,
    load_profile_toml,
    sha256_file,
    toml_status,
    utcnow,
    zone_label,
)


def register(app, rt, require_session, require_csrf, safe_next, render) -> None:
    # -- profile ------------------------------------------------------------------------------
    @app.get("/profile", response_class=HTMLResponse)
    def profile(request: Request, session: dict = Depends(require_session)):
        p = rt.reload_profile()
        from amorehiremepls.candidate.profile import compute_experience

        # The drop folder (candidate/): offer its resume for adoption; never swap the approved base on its own.
        cand = rt.candidate_docs()
        cand_state = None
        if cand.base_resume is not None:
            cand_state = "new" if p.base_resume is None else ("same" if p.base_resume.sha256 == cand.base_resume_sha256 else "differs")
        toml = toml_status(rt.settings.candidate_dir, rt.store)
        return render(
            request,
            "profile.html",
            session,
            p=p,
            exp=compute_experience(p.employment).as_dict(),
            provider_check=None,
            cand=cand,
            cand_state=cand_state,
            cand_path=str(cand.base_resume.resolve()) if cand.base_resume else "",
            toml=toml,
            toml_report=rt.store.get_control("candidate_profile_toml_last_report") or None,
            zone=zone_label(),
        )

    @app.post("/profile/load-toml")
    def profile_load_toml(request: Request, session: dict = Depends(require_session), csrf: str = Form(...)):
        """Apply candidate/profile.toml to the saved profile. Explicit click only; the file is never reloaded on its own."""
        require_csrf(request, session, csrf)
        path = rt.settings.candidate_dir / PROFILE_TOML_NAME
        try:
            data = load_profile_toml(path)
            p = rt.reload_profile()
            report = apply_profile_toml(p, data, confirmed_on=utcnow().date().isoformat(), store=rt.store)
        except ProfileTomlError as exc:
            rt.store.set_control("candidate_profile_toml_last_report", {"ok": False, "error": str(exc), "at": utcnow().isoformat()})
            return RedirectResponse("/profile?err=toml_invalid", status_code=303)
        rt.profiles.save(p, actor="user")
        rt.reload_profile()
        rt.store.set_control(TOML_CONTROL_KEY, {"sha256": sha256_file(path), "loaded_at": utcnow().isoformat()})
        rt.store.set_control("candidate_profile_toml_last_report", {"ok": True, "at": utcnow().isoformat(), **report.as_dict()})
        rt.store.audit("candidate_profile_toml_loaded", None, sha256=sha256_file(path), **report.as_dict())
        return RedirectResponse("/profile?ok=toml_loaded", status_code=303)

    @app.post("/profile/preferences")
    def profile_prefs(
        request: Request,
        session: dict = Depends(require_session),
        csrf: str = Form(...),
        profession: str = Form(""),
        role_families: str = Form(""),
        excluded_role_families: str = Form(""),
        excluded_role_family_exceptions: str = Form(""),
        locations: str = Form(""),
        remote_eligible_terms: str = Form(""),
        region_label: str = Form(""),
        employment_types: str = Form("permanent"),
        currency: str = Form(""),
        floor_base: int = Form(0),
        target_base: str = Form(""),
        band_rule: str = Form("midpoint"),
        unknown_rule: str = Form("needs_input"),
        small_startup_rule: str = Form("needs_review"),
        startup_signals_extra: str = Form(""),
        excluded_employers: str = Form(""),
        outreach_role_phrase: str = Form(""),
        outreach_one_liner: str = Form(""),
        outreach_max_chars: int = Form(300),
        outreach_ask_other_roles: str = Form(""),
        outreach_no_dashes: str = Form(""),
        outreach_no_layoff_mention: str = Form(""),
        resume_max_pages: int = Form(2),
        resume_max_bullets: int = Form(4),
        resume_allowed_terms: str = Form(""),
        style_no_dashes: str = Form(""),
    ):
        require_csrf(request, session, csrf)
        p = rt.reload_profile()
        split = lambda s: [x.strip() for x in s.split(",") if x.strip()]  # noqa: E731
        p.candidate.profession = profession.strip()
        p.preferences.role_families = [x.lower() for x in split(role_families)]
        p.preferences.excluded_role_families = [x.lower() for x in split(excluded_role_families)]
        p.preferences.excluded_role_family_exceptions = [x.lower() for x in split(excluded_role_family_exceptions)]
        p.preferences.locations = split(locations)
        p.preferences.remote_eligible_terms = [x.lower() for x in split(remote_eligible_terms)]
        p.preferences.region_label = region_label.strip()
        p.preferences.employment_types = split(employment_types)
        p.preferences.small_startup_rule = small_startup_rule  # type: ignore[assignment]
        p.preferences.startup_signals_extra = [x.lower() for x in split(startup_signals_extra)]
        p.compensation.currency = currency.strip().upper()[:3]
        p.compensation.floor_base = floor_base
        p.compensation.target_base = int(target_base) if target_base.strip() else None
        p.compensation.band_rule = band_rule  # type: ignore[assignment]
        p.compensation.unknown_rule = unknown_rule  # type: ignore[assignment]
        p.compensation.provenance = Provenance(source="user:dashboard", confirmed_on=utcnow().date().isoformat())
        p.preferences.excluded_employers = [
            ExcludedEmployer(name=line.split("|")[0].strip(), aliases=split(line.split("|")[1]) if "|" in line else [], reason="dashboard")
            for line in excluded_employers.splitlines()
            if line.strip()
        ]
        p.preferences.outreach_rules = {
            **p.preferences.outreach_rules,
            "role_phrase": outreach_role_phrase.strip(),
            "one_liner": outreach_one_liner.strip(),
            "connect_note_max_chars": max(60, min(int(outreach_max_chars), 2000)),
            "ask_other_roles": bool(outreach_ask_other_roles),
            "no_dashes": bool(outreach_no_dashes),
            "no_layoff_mention": bool(outreach_no_layoff_mention),
        }
        p.preferences.resume.max_pages = max(1, min(int(resume_max_pages), 6))
        p.preferences.resume.max_bullets_per_role = max(1, min(int(resume_max_bullets), 12))
        p.preferences.resume.allowed_terms = split(resume_allowed_terms)
        p.preferences.style.no_dashes = bool(style_no_dashes)
        rt.profiles.save(p, actor="user")
        return RedirectResponse("/profile?ok=preferences_saved", status_code=303)

    @app.post("/profile/facts")
    def profile_fact(request: Request, session: dict = Depends(require_session), csrf: str = Form(...), key: str = Form(...), value: str = Form(""), category: str = Form("other")):
        require_csrf(request, session, csrf)
        from amorehiremepls.candidate.profile import Fact

        p = rt.reload_profile()
        f = p.fact(key)
        prov = Provenance(source="user:dashboard", confirmed_on=utcnow().date().isoformat())
        if f:
            f.value, f.provenance = (value or None), prov
        else:
            p.facts.append(Fact(category=category, key=key, value=value or None, provenance=prov))
        rt.profiles.save(p, actor="user")
        return RedirectResponse("/profile", status_code=303)

    @app.post("/profile/base-resume")
    def base_resume(request: Request, session: dict = Depends(require_session), csrf: str = Form(...), path: str = Form(...), note: str = Form("")):
        require_csrf(request, session, csrf)
        p = Path(path).expanduser()
        if not p.is_file():
            raise HTTPException(400, "file not found on the server host")
        rt.profiles.select_base_resume(p, utcnow().date().isoformat(), "user:dashboard", rt.settings.private_dir, note)
        rt.reload_profile()
        return RedirectResponse("/profile", status_code=303)

    @app.post("/profile/candidate-upload")
    async def candidate_upload(request: Request, session: dict = Depends(require_session), csrf: str = Form(...), file: UploadFile = File(...)):
        """Put one resume or notes file into the candidate drop folder. Adopting it as the base resume stays a separate click."""
        require_csrf(request, session, csrf)
        from amorehiremepls.candidate.candidate_docs import UPLOAD_MAX_BYTES, CandidateUploadError, save_upload

        data = await file.read(UPLOAD_MAX_BYTES + 1)
        try:
            saved = save_upload(rt.settings.candidate_dir, file.filename or "", data)
        except CandidateUploadError as exc:
            return RedirectResponse(f"/profile?err=candidate_{exc.code}", status_code=303)
        except OSError:
            return RedirectResponse("/profile?err=candidate_not_writable", status_code=303)
        rt.store.audit("candidate_file_uploaded", None, name=saved.name, bytes=len(data), sha256=sha256_file(saved))
        return RedirectResponse("/profile?ok=candidate_saved", status_code=303)

    @app.post("/profile/exclusions/resolve")
    def resolve_exclusion(
        request: Request,
        session: dict = Depends(require_session),
        csrf: str = Form(...),
        exclusion_id: str = Form(...),
        profile_url: str = Form(""),
        employer: str = Form(""),
        email: str = Form(""),
    ):
        require_csrf(request, session, csrf)
        p = rt.reload_profile()
        for rx in p.preferences.recipient_exclusions:
            if rx.id == exclusion_id:
                if not (profile_url or email):
                    raise HTTPException(400, "identity requires a profile URL or email")
                rx.profile_url, rx.employer, rx.email, rx.resolution = (profile_url or None), (employer or None), (email or None), "resolved"
                rx.provenance = Provenance(source="user:dashboard", confirmed_on=utcnow().date().isoformat())
        rt.profiles.save(p, actor="user")
        return RedirectResponse("/profile", status_code=303)

    @app.post("/profile/exclusions/add")
    def add_exclusion(
        request: Request,
        session: dict = Depends(require_session),
        csrf: str = Form(...),
        label: str = Form(...),
        profile_url: str = Form(""),
        employer: str = Form(""),
        reason: str = Form(""),
    ):
        require_csrf(request, session, csrf)
        p = rt.reload_profile()
        p.preferences.recipient_exclusions.append(
            RecipientExclusion(
                label=label,
                resolution="resolved" if profile_url else "unresolved",
                profile_url=profile_url or None,
                employer=employer or None,
                reason=reason,
                provenance=Provenance(source="user:dashboard", confirmed_on=utcnow().date().isoformat()),
            )
        )
        rt.profiles.save(p, actor="user")
        return RedirectResponse("/profile", status_code=303)

    def run_provider_check() -> dict:
        try:
            info = rt.provider_check()
        except Exception as exc:  # the check itself must never take the dashboard down
            info = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
        rt.store.set_control(
            "provider_last_check",
            {"ok": bool(info.get("ok")), "error": info.get("error") or (None if info.get("ok") else info.get("detail")), "model": info.get("configured_model"), "at": utcnow()},
        )
        return info

    @app.get("/provider/check")
    def provider_check(session: dict = Depends(require_session)):
        return JSONResponse(run_provider_check())

    @app.post("/provider/check")
    def provider_check_post(request: Request, session: dict = Depends(require_session), csrf: str = Form(...), next: str = Form("/")):
        require_csrf(request, session, csrf)
        info = run_provider_check()
        target = safe_next(next)
        sep = "&" if "?" in target else "?"
        return RedirectResponse(f"{target}{sep}{'ok=provider_ok' if info.get('ok') else 'err=provider_failed'}", status_code=303)
