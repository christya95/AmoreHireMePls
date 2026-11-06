"""Dashboard + API (FastAPI, server-rendered). Password login, signed session cookie, CSRF token on
every state-changing form. No raw browser-control endpoints exist; state changes go through services."""

from __future__ import annotations

from typing import Any

from amorehiremepls.core.accounts import access_summary, account_is_active, bound_user_id, user_scope
from amorehiremepls.web.chrome import (
    ATTENTION_STATUSES,
    BRAND_DIR,
    DOCX_MEDIA_TYPE,
    FLASHES,
    GLOSSARY,
    PAGE_INTROS,
    PAGE_TITLES,
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    STATIC_DIR,
    TEMPLATES,
    UI_COOKIE,
    UI_COOKIE_MAX_AGE,
    BadSignature,
    FastAPI,
    HTMLResponse,
    HTTPException,
    Request,
    Runtime,
    StaticFiles,
    UISettings,
    UISettingsStore,
    URLSafeTimedSerializer,
    _nav,
    favicon_svg,
    fit_score,
    greeting,
    hmac,
    initials,
    resolve_display_name,
    saved_form_inputs,
    secrets,
    show_usage_for,
    status,
    urlsplit,
    utcnow,
    worker_healthy,
)
from amorehiremepls.web.routes.admin import register as register_admin
from amorehiremepls.web.routes.applications import register as register_applications
from amorehiremepls.web.routes.authorizations import register as register_authorizations
from amorehiremepls.web.routes.controls import register as register_controls
from amorehiremepls.web.routes.documents import register as register_documents
from amorehiremepls.web.routes.history import register as register_history
from amorehiremepls.web.routes.jobs import register as register_jobs
from amorehiremepls.web.routes.outreach import register as register_outreach
from amorehiremepls.web.routes.overview import register as register_overview
from amorehiremepls.web.routes.profile import register as register_profile
from amorehiremepls.web.routes.public import register as register_public
from amorehiremepls.web.routes.settings import register as register_settings
from amorehiremepls.web.routes.sync import register as register_sync

__all__ = ["BRAND_DIR", "DOCX_MEDIA_TYPE", "create_app", "favicon_svg", "saved_form_inputs"]


def create_app(rt: Runtime) -> FastAPI:
    app = FastAPI(title="AmoreHireMePls", docs_url=None, redoc_url=None, openapi_url=None)
    secret = rt.settings.app_secret.get_secret_value()
    if len(secret) < 32:
        # Never run with a weak signing key; a random one means sessions reset on restart (dev only).
        secret = secrets.token_urlsafe(48)
        app.state.ephemeral_secret = True
    serializer = URLSafeTimedSerializer(secret, salt="amore-session")
    app.state.rt = rt
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    ui_store = UISettingsStore(rt.store)

    @app.middleware("http")
    async def bind_user(request: Request, call_next):
        session = read_session(request)
        uid = bound_user_id(rt.store, (session or {}).get("user"))
        with user_scope(uid):
            if uid:
                rt.reload_profile()
            return await call_next(request)

    # -- auth helpers ---------------------------------------------------------------------
    def read_session(request: Request) -> dict | None:
        raw = request.cookies.get(SESSION_COOKIE)
        if not raw:
            return None
        try:
            return serializer.loads(raw, max_age=SESSION_MAX_AGE)
        except BadSignature:
            return None

    def require_session(request: Request) -> dict:
        s = read_session(request)
        if not s:
            raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
        user_id = str(s.get("user") or "")
        if user_id.startswith("user_"):
            user = rt.store.db["users"].find_one({"_id": user_id})
            if user is None or user.get("status") == "deleted":
                raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
            allowed = request.url.path in ("/access-ended", "/logout")
            if not account_is_active(user) and not allowed:
                raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/access-ended"})
        return s

    def require_admin(request: Request) -> dict:
        s = require_session(request)
        if not show_usage_for(s):
            raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/"})
        return s

    def require_csrf(request: Request, session: dict, token: str | None) -> None:
        if not token or not hmac.compare_digest(session.get("csrf", ""), token):
            raise HTTPException(status_code=403, detail="CSRF token mismatch")
        origin = request.headers.get("origin") or request.headers.get("referer")
        if origin and request.headers.get("host") and urlsplit(origin).netloc.lower() != request.headers["host"].lower():
            raise HTTPException(status_code=403, detail="cross-site request refused")

    def ui_cookie_value(ui: UISettings) -> str:
        return f"{ui.theme}|{ui.accent}|{ui.bg_tint}"

    def set_ui_cookie(resp: Any, ui: UISettings) -> None:
        resp.set_cookie(UI_COOKIE, ui_cookie_value(ui), httponly=True, samesite="strict", secure=rt.settings.secure_cookies, max_age=UI_COOKIE_MAX_AGE)

    def avatar_url_for(ui: UISettings) -> str | None:
        return f"/settings/avatar/image?v={ui.avatar.sha256[:16]}" if ui.avatar.kind == "upload" and ui.avatar.sha256 else None

    def safe_next(value: str | None, default: str = "/") -> str:
        v = (value or "").strip()
        return v if v.startswith("/") and not v.startswith("//") and "\\" not in v else default

    def render(request: Request, template: str, session: dict | None = None, **ctx: Any) -> HTMLResponse:
        ui = ui_store.load()
        name = resolve_display_name(ui, rt.profile)
        health = rt.health()
        role = (session or {}).get("role") or "admin"
        show_usage = show_usage_for(session)
        user_doc = None
        user_id = str((session or {}).get("user") or "")
        if user_id.startswith("user_"):
            user_doc = rt.store.db["users"].find_one({"_id": user_id})
        summary = access_summary(user_doc) if user_doc else None
        if not name and user_doc:
            name = (user_doc.get("display_name") or "").strip()
        attention = rt.store.c("applications").count_documents({"status": {"$in": list(ATTENTION_STATUSES)}, "provenance.origin": "local"}) if health.get("ok") else 0
        flash_key = request.query_params.get("ok") or request.query_params.get("err")
        flash, flash_tone = FLASHES.get(flash_key or "", (None, None))
        intro_key = template.rsplit(".", 1)[0]
        base = {
            "request": request,
            "csrf": (session or {}).get("csrf", ""),
            "emergency": rt.store.emergency_stop_active(),
            "now": utcnow(),
            "base_resume": rt.profile.base_resume,
            "open_questions": open_questions(),
            "title": PAGE_TITLES.get(template, "AmoreHireMePls"),
            "nav": _nav(request.url.path, attention, show_usage=show_usage, show_admin=role != "member"),
            "show_usage": show_usage,
            "access_badge": summary["badge"] if summary and role == "member" else None,
            "health": health,
            "worker_ok": worker_healthy(rt) if health.get("ok") else False,
            "ui": ui,
            "display_name": name,
            "initials": initials(name),
            "greeting": greeting(),
            "avatar_url": avatar_url_for(ui),
            "nonce": secrets.token_urlsafe(12),
            "flash": flash,
            "flash_tone": flash_tone,
            "intro": PAGE_INTROS.get(template),
            "intro_key": intro_key,
            "intro_collapsed": intro_key in ui.collapsed_intros,
            "glossary": GLOSSARY,
            "fit_score": fit_score,
            # logo_src(job, employer=None) -> app-served image URL or None (initials). Deterministic, no model use.
            "logo_src": rt.logos.src_for if ui.company_logos else (lambda job=None, employer=None: None),
        }
        resp = TEMPLATES.TemplateResponse(request, template, {**base, **ctx})
        if request.cookies.get(UI_COOKIE) != ui_cookie_value(ui):
            set_ui_cookie(resp, ui)
        return resp

    def open_questions() -> list[str]:
        q = []
        if rt.profile.base_resume is None:
            q.append("Your approved base resume (DOCX) is not chosen yet. Drop it in the candidate folder or paste where it is on this computer.")
        for x in rt.profile.preferences.recipient_exclusions:
            if x.resolution == "unresolved":
                q.append(f"'{x.label}' is on your do not contact list but has no LinkedIn link or employer yet. Add one so the app can tell them apart from namesakes.")
        if not rt.settings.provider_configured:
            q.append("No AI model is connected (AMORE_MODEL and its key). Only the rules based steps work until it is.")
        if rt.settings.tracker_enabled and not (rt.settings.tracker_clone_dir / ".git").exists():
            q.append("Your GitHub tracker is not downloaded on this computer yet. Run `amore tracker clone` here before turning on saving to GitHub.")
        return q

    register_public(app=app, rt=rt, serializer=serializer, require_session=require_session, require_csrf=require_csrf)
    register_admin(app=app, rt=rt, require_admin=require_admin, require_csrf=require_csrf, render=render)
    register_overview(app=app, rt=rt, ui_store=ui_store, require_session=require_session, render=render)
    register_controls(app=app, rt=rt, require_session=require_session, require_csrf=require_csrf, render=render)
    register_profile(app=app, rt=rt, require_session=require_session, require_csrf=require_csrf, safe_next=safe_next, render=render)
    register_history(app=app, rt=rt, require_session=require_session, require_csrf=require_csrf, render=render)
    register_jobs(app=app, rt=rt, require_session=require_session, require_csrf=require_csrf, render=render)
    register_applications(app=app, rt=rt, require_session=require_session, require_csrf=require_csrf, render=render)
    register_authorizations(app=app, rt=rt, require_session=require_session, require_csrf=require_csrf, render=render)
    register_outreach(app=app, rt=rt, require_session=require_session, require_csrf=require_csrf, render=render)
    register_sync(app=app, rt=rt, require_session=require_session, require_csrf=require_csrf, render=render)
    register_documents(app=app, rt=rt, require_session=require_session, render=render)
    register_settings(
        app=app,
        rt=rt,
        ui_store=ui_store,
        require_session=require_session,
        require_csrf=require_csrf,
        set_ui_cookie=set_ui_cookie,
        safe_next=safe_next,
        render=render,
    )

    return app
