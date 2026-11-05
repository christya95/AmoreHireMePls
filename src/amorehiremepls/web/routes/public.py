"""Routes registered by create_app."""

from __future__ import annotations

from amorehiremepls.web.chrome import (
    BRAND_DIR,
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    TEMPLATES,
    UI_COOKIE,
    Depends,
    FileResponse,
    Form,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Request,
    _parse_ui_cookie,
    favicon_svg,
    hmac,
    secrets,
    utcnow,
    worker_healthy,
)


def register(app, rt, serializer, require_session, require_csrf) -> None:
    # -- public -----------------------------------------------------------------------------
    @app.get("/health")
    def health():
        # Unauthenticated liveness only: no versions, names or operational state are disclosed here.
        ok = bool(rt.health().get("ok"))
        return JSONResponse({"ok": ok}, status_code=200 if ok else 503)

    @app.get("/status")
    def status_detail(session: dict = Depends(require_session)):
        h = rt.health()
        h["worker_healthy"] = worker_healthy(rt) if h["ok"] else None
        h["emergency_stop"] = rt.store.emergency_stop_active()
        return JSONResponse(h)

    def render_login(request: Request, error: str | None, status_code: int = 200) -> HTMLResponse:
        theme, accent, tint = _parse_ui_cookie(request.cookies.get(UI_COOKIE))
        ctx = {"request": request, "error": error, "theme": theme, "accent": accent, "tint": tint, "nonce": secrets.token_urlsafe(12)}
        return TEMPLATES.TemplateResponse(request, "login.html", ctx, status_code=status_code)

    @app.get("/brand.svg")
    def brand_svg(request: Request):
        """The favicon: the same tile mark as the sidebar, in the saved accent colour (from the mirror cookie, so it works
        before sign-in too). Rendered once per accent; /static/brand/favicon.svg is the fixed sage fallback."""
        _, accent, _ = _parse_ui_cookie(request.cookies.get(UI_COOKIE))
        return HTMLResponse(favicon_svg(accent), media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=3600", "X-Content-Type-Options": "nosniff"})

    @app.get("/apple-touch-icon.png")
    @app.get("/apple-touch-icon-precomposed.png")
    def apple_touch_icon():
        """180 px PNG of the sage tile for home-screen bookmarks (rendered by tools/brand/render.sh, kept in static/brand)."""
        headers = {"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"}
        return FileResponse(BRAND_DIR / "favicon-180.png", media_type="image/png", headers=headers)

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        return render_login(request, None)

    @app.post("/login")
    def login(request: Request, password: str = Form(...)):
        expected = rt.settings.app_password.get_secret_value()
        if len(expected) < 12 or not hmac.compare_digest(expected, password):
            rt.store.audit("login_failed", None, ip=request.client.host if request.client else None)
            return render_login(request, "Invalid password", status_code=401)
        token = serializer.dumps({"user": "owner", "csrf": secrets.token_urlsafe(24), "at": utcnow().isoformat()})
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="strict", secure=rt.settings.secure_cookies, max_age=SESSION_MAX_AGE)
        rt.store.audit("login", "owner")
        return resp

    @app.post("/logout")
    def logout(request: Request, session: dict = Depends(require_session), csrf: str = Form(...)):
        require_csrf(request, session, csrf)
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(SESSION_COOKIE)
        return resp
