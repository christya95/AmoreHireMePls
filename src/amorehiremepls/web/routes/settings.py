"""Routes registered by create_app."""

from __future__ import annotations

from amorehiremepls.web.chrome import (
    ACCENT_PRESETS,
    AVATAR_MAX_BYTES,
    AVATAR_STYLES,
    EMPLOYER_DOMAINS,
    PAGE_INTROS,
    REQUEST_TIMEOUT,
    TINT_LABELS,
    TINT_PRESETS,
    AvatarError,
    Depends,
    File,
    FileResponse,
    Form,
    HTMLResponse,
    HTTPException,
    JSONResponse,
    RedirectResponse,
    Request,
    UISettings,
    UploadFile,
    normalize_accent,
    normalize_tint,
    re,
    remove_avatar_files,
    resolve_display_name,
    store_avatar,
)


def register(app, rt, ui_store, require_session, require_csrf, set_ui_cookie, safe_next, render) -> None:
    # -- dashboard settings (theme, accent, name, avatar) ------------------------------------------
    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, session: dict = Depends(require_session)):
        ui = ui_store.load()
        return render(
            request,
            "settings.html",
            session,
            presets=ACCENT_PRESETS,
            tints=[(k, TINT_LABELS[k], v) for k, v in TINT_PRESETS.items()],
            tint_name=ui.tint_name,
            tint_hex=ui.bg_tint if ui.tint_name is None else (TINT_PRESETS.get(ui.bg_tint) or ui.accent),
            styles=AVATAR_STYLES,
            accent_name=ui.accent_name,
            max_kb=AVATAR_MAX_BYTES // 1024,
            default_name=resolve_display_name(UISettings(), rt.profile),
            intro_count=len(ui.collapsed_intros),
            captcha_autosolve=rt.captcha_autosolve_enabled(),
            captcha_attempts=rt.settings.captcha_max_attempts,
            captcha_env_default=rt.settings.captcha_autosolve,
            site_signin=rt.site_signin_enabled(),
            site_signin_env_default=rt.settings.site_signin,
            site_signin_reader=rt.credential_source().available(),
            notify_email=rt.notifier().email_ready(),
            notify_webhook=rt.notifier().webhook_ready(),
        )

    @app.post("/settings/captcha")
    def settings_captcha(request: Request, session: dict = Depends(require_session), csrf: str = Form(...), autosolve: str = Form("")):
        """Opt-in automatic captcha step for approved applications. Off by default; the choice is kept in `controls`."""
        require_csrf(request, session, csrf)
        enabled = autosolve in ("on", "1", "true")
        rt.store.set_control("captcha_autosolve", enabled)
        rt.store.audit("captcha_autosolve_changed", None, enabled=enabled)
        return RedirectResponse(f"/settings?ok={'captcha_on' if enabled else 'captcha_off'}", status_code=303)

    @app.post("/settings/signin")
    def settings_signin(request: Request, session: dict = Depends(require_session), csrf: str = Form(...), signin: str = Form("")):
        """Opt-in sign-in with saved logins. Off by default; the choice is kept in `controls`."""
        require_csrf(request, session, csrf)
        enabled = signin in ("on", "1", "true")
        rt.store.set_control("site_signin", enabled)
        rt.store.audit("site_signin_changed", None, enabled=enabled)
        return RedirectResponse(f"/settings?ok={'signin_on' if enabled else 'signin_off'}", status_code=303)

    @app.post("/settings/notify-test")
    def settings_notify_test(request: Request, session: dict = Depends(require_session), csrf: str = Form(...)):
        """Send one test note to the configured channels. The button is the authorization for that single send."""
        require_csrf(request, session, csrf)
        result = rt.notifier().send_test()
        if result.get("reason") == "not_configured":
            flash = "notify_off"
        elif result.get("sent"):
            flash = "notify_sent"
        else:
            flash = "notify_failed"
        return RedirectResponse(f"/settings?ok={flash}", status_code=303)

    @app.post("/settings/checklist")
    def settings_checklist(request: Request, session: dict = Depends(require_session), csrf: str = Form(...), dismissed: str = Form("1"), next: str = Form("/")):
        require_csrf(request, session, csrf)
        ui = ui_store.load()
        ui.checklist_dismissed = dismissed == "1"
        ui_store.save(ui)
        return RedirectResponse(safe_next(next), status_code=303)

    @app.post("/settings/intro")
    def settings_intro(
        request: Request,
        session: dict = Depends(require_session),
        csrf: str = Form(...),
        page: str = Form(""),
        collapsed: str = Form("1"),
        reset: str = Form(""),
        next: str = Form("/"),
    ):
        require_csrf(request, session, csrf)
        ui = ui_store.load()
        if reset:
            ui.collapsed_intros = []
        else:
            key = re.sub(r"[^a-z_]", "", page.lower())[:40]
            if not key or f"{key}.html" not in PAGE_INTROS:
                raise HTTPException(400, "unknown page")
            folded = set(ui.collapsed_intros)
            (folded.add if collapsed == "1" else folded.discard)(key)
            ui.collapsed_intros = sorted(folded)
        ui_store.save(ui)
        if request.headers.get("x-requested-with") == "fetch":
            return JSONResponse({"ok": True, "collapsed": ui.collapsed_intros})
        return RedirectResponse(safe_next(next, "/settings"), status_code=303)

    @app.post("/settings/theme")
    def settings_theme(request: Request, session: dict = Depends(require_session), csrf: str = Form(...), theme: str = Form(...), next: str = Form("/")):
        require_csrf(request, session, csrf)
        if theme not in ("system", "light", "dark"):
            raise HTTPException(400, "unknown theme")
        ui = ui_store.load()
        ui.theme = theme  # type: ignore[assignment]
        ui_store.save(ui)
        resp = JSONResponse({"ok": True, "theme": theme}) if request.headers.get("x-requested-with") == "fetch" else RedirectResponse(safe_next(next), status_code=303)
        set_ui_cookie(resp, ui)
        return resp

    @app.post("/settings/appearance")
    def settings_appearance(
        request: Request,
        session: dict = Depends(require_session),
        csrf: str = Form(...),
        theme: str = Form("system"),
        accent: str = Form(""),
        accent_hex: str = Form(""),
        tint: str = Form("accent"),
        tint_hex: str = Form(""),
        company_logos: str = Form(""),
    ):
        require_csrf(request, session, csrf)
        if theme not in ("system", "light", "dark"):
            raise HTTPException(400, "unknown theme")
        chosen = normalize_accent(accent_hex if accent == "custom" or not accent else accent)
        if chosen is None:
            return RedirectResponse("/settings?err=bad_accent", status_code=303)
        chosen_tint = normalize_tint(tint_hex if tint == "custom" else tint)
        if chosen_tint is None:
            return RedirectResponse("/settings?err=bad_tint", status_code=303)
        ui = ui_store.load()
        ui.theme, ui.accent, ui.bg_tint = theme, chosen, chosen_tint  # type: ignore[assignment]
        ui.company_logos = company_logos in ("on", "1", "true")
        ui_store.save(ui)
        resp = RedirectResponse("/settings?ok=saved", status_code=303)
        set_ui_cookie(resp, ui)
        return resp

    @app.post("/settings/identity")
    def settings_identity(request: Request, session: dict = Depends(require_session), csrf: str = Form(...), display_name: str = Form("")):
        require_csrf(request, session, csrf)
        name = " ".join(display_name.split())[:60]
        ui = ui_store.load()
        ui.display_name = name or None
        ui_store.save(ui)
        return RedirectResponse("/settings?ok=saved", status_code=303)

    @app.post("/settings/avatar")
    async def settings_avatar(request: Request, session: dict = Depends(require_session), csrf: str = Form(...), style: str = Form(""), file: UploadFile | None = File(None)):
        require_csrf(request, session, csrf)
        ui = ui_store.load()
        if file is not None and file.filename:
            data = await file.read(AVATAR_MAX_BYTES + 1)
            try:
                ui.avatar = store_avatar(rt.settings.private_dir, data, file.content_type)
            except AvatarError as exc:
                raise HTTPException(400, str(exc)) from exc
            ui_store.save(ui)
            rt.store.audit("avatar_uploaded", None, sha256=ui.avatar.sha256, content_type=ui.avatar.content_type)
            return RedirectResponse("/settings?ok=avatar_saved", status_code=303)
        if style in AVATAR_STYLES:
            ui.avatar.style = style
            ui_store.save(ui)
            return RedirectResponse("/settings?ok=saved", status_code=303)
        raise HTTPException(400, "choose an image file or an initials style")

    @app.post("/settings/avatar/remove")
    def settings_avatar_remove(request: Request, session: dict = Depends(require_session), csrf: str = Form(...)):
        require_csrf(request, session, csrf)
        ui = ui_store.load()
        remove_avatar_files(rt.settings.private_dir)
        ui.avatar.kind, ui.avatar.sha256, ui.avatar.content_type, ui.avatar.ext = "initials", None, None, None
        ui_store.save(ui)
        return RedirectResponse("/settings?ok=avatar_removed", status_code=303)

    @app.get("/settings/avatar/image")
    def settings_avatar_image(session: dict = Depends(require_session)):
        ui = ui_store.load()
        if ui.avatar.kind != "upload" or not ui.avatar.ext or not ui.avatar.content_type:
            raise HTTPException(404)
        path = rt.settings.private_dir / "avatar" / f"avatar.{ui.avatar.ext}"
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type=ui.avatar.content_type, headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"})

    @app.get("/logos/{employer_key}.png")
    def company_logo(employer_key: str, session: dict = Depends(require_session)):
        """Employer icon from the public cache. The content type comes from sniffed magic bytes (never from the
        remote site) and the body is served with `nosniff`. 404 lets the page fall back to initials. A first
        request may fetch once with a short budget; later requests are cache hits."""
        if not re.fullmatch(r"[a-z0-9-]{1,80}", employer_key) or not ui_store.load().company_logos:
            raise HTTPException(404)
        hit = rt.logos.lookup(employer_key)
        if hit is None and rt.logos.needs_fetch(employer_key):
            # Page render records the domain in memory. After a restart, or when the image is the first request,
            # fall back to the alias table and then the stored jobs. This runs in the web process; no worker required.
            domain = rt.logos.wanted.get(employer_key) or EMPLOYER_DOMAINS.get(employer_key)
            if not domain:
                fields = {"employer": 1, "posting_url": 1, "apply_url": 1, "url": 1, "company_website": 1, "website": 1, "homepage": 1, "job_url": 1, "source_url": 1}
                jobs = list(rt.store.c("jobs").find({}, fields).limit(400))
                domain = rt.logos.resolve_domain(employer_key, jobs)
            if domain:
                hit = rt.logos.ensure(employer_key, domain, timeout=REQUEST_TIMEOUT)
        if hit is None:
            raise HTTPException(404, headers={"Cache-Control": "private, no-store"})
        path, media = hit
        return FileResponse(path, media_type=media, headers={"Cache-Control": "private, max-age=604800, immutable", "X-Content-Type-Options": "nosniff"})
