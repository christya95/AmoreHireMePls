"""Dashboard + API (FastAPI, server-rendered). Password login, signed session cookie, CSRF token on
every state-changing form. No raw browser-control endpoints exist; state changes go through services."""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer

from .authorization import ScopeBinding, StopConditions
from .discovery import SOURCES
from .profile import ExcludedEmployer, Provenance, RecipientExclusion
from .runtime import Runtime
from .tracker import TrackerClone, TrackerImporter
from .ui_settings import (
    ACCENT_PRESETS,
    AVATAR_MAX_BYTES,
    AVATAR_STYLES,
    GLOSSARY,
    PAGE_INTROS,
    TINT_LABELS,
    TINT_PRESETS,
    AvatarError,
    UISettings,
    UISettingsStore,
    donut,
    greeting,
    humanize,
    initials,
    line_chart,
    month_buckets,
    next_action_label,
    normalize_accent,
    normalize_tint,
    remove_avatar_files,
    resolve_display_name,
    status_label,
    status_tone,
    store_avatar,
    tint_css_from_cookie,
)
from .util import sha256_file, to_toronto, utcnow
from .worker import next_slot_description, worker_healthy

DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
TEMPLATES.env.filters["toronto"] = lambda dt: to_toronto(dt).strftime("%Y-%m-%d %H:%M") if dt else ""
TEMPLATES.env.filters["toronto_day"] = lambda dt: to_toronto(dt).strftime("%b %d") if dt else ""
TEMPLATES.env.filters["json"] = lambda v: json.dumps(v, indent=1, default=str)
TEMPLATES.env.filters["tone"] = status_tone
TEMPLATES.env.filters["human"] = humanize
TEMPLATES.env.filters["label"] = status_label
TEMPLATES.env.filters["verb"] = next_action_label
TEMPLATES.env.filters["initials"] = initials
STATIC_DIR = Path(__file__).parent / "static"
BRAND_DIR = STATIC_DIR / "brand"


def _brand_paths() -> str:
    """Inner markup of static/brand/mark.svg (the paths only), the single source for every rendering of the logo."""
    svg = (BRAND_DIR / "mark.svg").read_text(encoding="utf-8")
    inner = re.sub(r"^.*?<svg[^>]*>", "", svg, count=1, flags=re.S)
    inner = re.sub(r"</svg>\s*$", "", inner, flags=re.S)
    return re.sub(r"<!--.*?-->", "", inner, flags=re.S).strip()


TEMPLATES.env.globals["brand_paths"] = _brand_paths()
# the same mark without the smallest heart (<g class="detail">), for renderings that cannot apply CSS (favicons)
TEMPLATES.env.globals["brand_paths_small"] = re.sub(r'<g class="detail">.*?</g>', "", _brand_paths(), flags=re.S).strip()


@lru_cache(maxsize=64)
def favicon_svg(accent: str) -> str:
    """The tile mark with the saved accent substituted, drawn without the smallest heart because favicons are 16 to 32 px;
    cached per accent (the accent is already a validated hex)."""
    return TEMPLATES.env.from_string('{% from "_brand.html" import mark %}{{ mark(standalone=True, color=color, small=True) }}').render(color=accent)


SESSION_COOKIE = "amore_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 14
UI_COOKIE = "amore_ui"  # theme|accent|tint mirror of the saved setting, so the first paint (and the login page) match
UI_COOKIE_MAX_AGE = 60 * 60 * 24 * 365

PAGE_TITLES = {
    "index.html": "Overview",
    "jobs.html": "Jobs",
    "job.html": "Job",
    "applications.html": "Applications",
    "application.html": "Review package",
    "runs.html": "Applications · Runs",
    "run.html": "Run",
    "documents.html": "Documents",
    "outreach.html": "Outreach",
    "history.html": "History",
    "sync.html": "Sync",
    "usage.html": "Usage",
    "profile.html": "Candidate profile",
    "authorizations.html": "Authorizations",
    "settings.html": "Settings",
    "help.html": "Help",
}
NAV = [
    ("Work", None, None),
    ("/", "Overview", "overview"),
    ("/jobs", "Jobs", "jobs"),
    ("/applications", "Applications", "applications"),
    ("/documents", "Documents", "documents"),
    ("/outreach", "Outreach", "outreach"),
    ("Records", None, None),
    ("/history", "History", "history"),
    ("/sync", "Sync", "sync"),
    ("/usage", "Usage", "usage"),
    ("Account", None, None),
    ("/authorizations", "Authorizations", "auth"),
    ("/profile", "Candidate profile", "profile"),
    ("/settings", "Settings", "settings"),
    ("/help", "Help", "help"),
]
NAV_ALIASES = {"/runs": "/applications"}  # Runs lives as a tab under Applications; the route itself is unchanged
ATTENTION_STATUSES = ("ready_for_review", "needs_input", "handoff", "needs_verification")
FLASHES = {
    "saved": ("Saved.", "ok"),
    "avatar_saved": ("Avatar updated.", "ok"),
    "avatar_removed": ("Avatar removed; showing initials.", "ok"),
    "bad_accent": ("Accent must be a preset or a #rrggbb colour.", "bad"),
    "bad_tint": ("Background tint must be a preset or a #rrggbb colour.", "bad"),
    "bad_avatar": ("That file was not accepted.", "bad"),
    "provider_ok": ("Model provider check passed.", "ok"),
    "provider_failed": ("Model provider check failed; see the System card.", "bad"),
    "regenerated": ("Documents regenerated. Hashes changed, so any earlier approval for this job was withdrawn.", "ok"),
    "regenerated_doc_only": ("A fresh document was generated. The existing package was left as it is because of its state.", "ok"),
    "no_base": ("Select the approved base resume under Candidate profile first.", "bad"),
    "busy": ("A run is working on this job right now; wait for it to pause or finish.", "bad"),
    "already_submitted": ("This application was submitted; documents are kept as they were sent.", "bad"),
}
HOSTS_ENV_EXAMPLE = "AMORE_BROWSER_ALLOWED_HOSTS=boards.greenhouse.io,jobs.lever.co"


def _parse_ui_cookie(raw: str | None) -> tuple[str, str, str]:
    """theme, accent hex, tint css value ("accent" or hex) from the mirror cookie; anything odd falls back to defaults."""
    theme, accent, tint = "system", ACCENT_PRESETS["sage"], "accent"
    if raw and "|" in raw:
        parts = raw.split("|")
        if parts[0] in ("system", "light", "dark"):
            theme = parts[0]
        accent = normalize_accent(parts[1]) or accent
        if len(parts) > 2:
            tint = tint_css_from_cookie(parts[2])
    return theme, accent, tint


def _nav(path: str, attention: int) -> list[dict]:
    items: list[dict] = []
    best = None
    for alias, target in NAV_ALIASES.items():
        if path == alias or path.startswith(alias + "/"):
            path = target
    for href, _, icon in NAV:
        if icon and (path == href or (href != "/" and path.startswith(href + "/"))) and (best is None or len(href) > len(best)):
            best = href
    for href, label, icon in NAV:
        if icon is None:
            items.append({"label": href, "label_only": True})
            continue
        items.append({"href": href, "label": label, "icon": icon, "active": href == best, "count": attention if href == "/applications" else 0})
    return items


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
            "nav": _nav(request.url.path, attention),
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
        }
        resp = TEMPLATES.TemplateResponse(request, template, {**base, **ctx})
        if request.cookies.get(UI_COOKIE) != ui_cookie_value(ui):
            set_ui_cookie(resp, ui)
        return resp

    def open_questions() -> list[str]:
        q = []
        if rt.profile.base_resume is None:
            q.append("Approved base resume DOCX not selected. Where is the current approved base DOCX?")
        for x in rt.profile.preferences.recipient_exclusions:
            if x.resolution == "unresolved":
                q.append(f"Recipient exclusion '{x.label}' is unresolved: bind it to a LinkedIn profile URL/employer.")
        if not rt.settings.provider_configured:
            q.append("No model provider configured (AMORE_MODEL + key). Deterministic paths only.")
        if not (rt.settings.tracker_clone_dir / ".git").exists():
            q.append("Tracker clone missing; run `amore tracker clone` on this host before enabling sync.")
        return q

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
        token = serializer.dumps({"user": "joss", "csrf": secrets.token_urlsafe(24), "at": utcnow().isoformat()})
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="strict", secure=rt.settings.secure_cookies, max_age=SESSION_MAX_AGE)
        rt.store.audit("login", "joss")
        return resp

    @app.post("/logout")
    def logout(request: Request, session: dict = Depends(require_session), csrf: str = Form(...)):
        require_csrf(request, session, csrf)
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    # -- overview --------------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, session: dict = Depends(require_session)):
        rt.reload_profile()
        s = rt.store
        counts = {
            "jobs": {r["_id"]: r["n"] for r in s.c("jobs").aggregate([{"$group": {"_id": "$status", "n": {"$sum": 1}}}])},
            "applications": {r["_id"]: r["n"] for r in s.c("applications").aggregate([{"$group": {"_id": "$status", "n": {"$sum": 1}}}])},
            "sync": rt.sync.outbox.summary(),
            "needs_verification": s.c("actions").count_documents({"status": "needs_verification"}),
        }
        runs = list(s.c("runs").find().sort("created_at", -1).limit(8))
        month_start = to_toronto(utcnow()).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        usage_pipeline = [
            {
                "$group": {
                    "_id": None,
                    "calls": {"$sum": 1},
                    "in": {"$sum": "$input_tokens"},
                    "out": {"$sum": "$output_tokens"},
                    "cost": {"$sum": {"$ifNull": ["$cost_usd", 0]}},
                    "unknown": {"$sum": {"$cond": [{"$eq": ["$cost_known", False]}, 1, 0]}},
                }
            }
        ]
        usage = next(iter(s.c("usage").aggregate(usage_pipeline)), {})
        usage_month = next(iter(s.c("usage").aggregate([{"$match": {"created_at": {"$gte": month_start}}}, *usage_pipeline])), {})
        return render(
            request,
            "index.html",
            session,
            counts=counts,
            runs=runs,
            usage=usage,
            usage_month=usage_month,
            kpis=overview_kpis(counts, usage_month),
            chart=applications_chart(),
            pipeline=jobs_donut(counts["jobs"]),
            attention=attention_items(),
            activity=list(s.c("audit").find({"event": {"$nin": ["login", "login_failed"]}}).sort("created_at", -1).limit(10)),
            recent_jobs=list(s.c("jobs").find().sort("updated_at", -1).limit(6)),
            schedule=next_slot_description(rt),
            provider=rt.settings.effective_model or "not configured (default provider: openai)",
            provider_last=s.get_control("provider_last_check") or {},
            missing=rt.profile.missing_essentials(),
            checklist=None if ui_store.load().checklist_dismissed else getting_started(),
        )

    def getting_started() -> dict:
        """Setup checklist computed from real state. Each item links to the one place where it can be completed."""
        s, p, st = rt.store, rt.profile, rt.settings
        last = s.get_control("provider_last_check") or {}
        key_env = "OPENAI_API_KEY" if st.model_provider == "openai" else "ANTHROPIC_API_KEY"
        key_present = bool(os.environ.get(key_env))
        model = st.effective_model
        priced = bool(model) and rt.pricing.cost(model, 1000, 1000) is not None
        missing = p.missing_essentials()
        local_apps = {"provenance.origin": "local"}
        first_review = s.c("applications").find_one({**local_apps, "status": {"$in": ["ready_for_review", "needs_input"]}}, {"_id": 1})
        if not (model and key_present):
            provider_hint = "Set AMORE_MODEL and the API key in your environment, then run the check."
        elif last and not last.get("ok"):
            provider_hint = "Last check failed: " + str(last.get("error") or "")
        else:
            provider_hint = "Not checked yet."

        def item(key: str, label: str, done: bool, href: str, action: str, hint: str = "", code: str = "", method: str = "get") -> dict:
            return {"key": key, "label": label, "done": bool(done), "href": href, "action": action, "hint": hint, "code": code, "method": method}

        items = [
            item("profile", "Confirm your profile facts", bool(p.facts) and not missing, "/profile", "Open profile", ("Missing: " + ", ".join(missing)) if missing else ""),
            item("base_resume", "Approve the base resume", p.base_resume is not None, "/profile", "Select the DOCX", "Paste the path of the approved base resume on this machine."),
            item("history", "Import your application history", s.c("applications").count_documents({"provenance.origin": "tracker_import"}) > 0, "/history", "Import history"),
            item(
                "provider",
                "Connect the model provider",
                bool(model) and key_present and bool(last.get("ok")),
                "/provider/check",
                "Check provider now",
                provider_hint,
                "" if model else "AMORE_MODEL=gpt-5.4-mini",
                "post",
            ),
            item(
                "hosts",
                "Allow the sites the browser may visit",
                bool(st.allowed_hosts),
                "/help#hosts",
                "How to set it",
                "Environment only: add this line to .env (comma separated hosts) and restart the web and worker processes.",
                HOSTS_ENV_EXAMPLE,
            ),
            item(
                "pricing",
                "Fill in model pricing",
                priced,
                "/usage",
                "Open usage",
                "Add the configured model to config/pricing.json so costs are numbers, not 'unknown'." if model else "Set a model first.",
            ),
            item("job", "Import your first job", s.c("jobs").estimated_document_count() > 0, "/jobs", "Import a job"),
            item(
                "review",
                "Open your first review package",
                s.c("applications").count_documents({**local_apps, "review_package": {"$ne": None}}) > 0,
                "/applications",
                "Open applications",
                "Start a run from a job; the worker prepares the package.",
            ),
            item(
                "authorization",
                "Grant your first approval",
                s.c("authorizations").estimated_document_count() > 0,
                f"/applications/{first_review['_id']}" if first_review else "/applications",
                "Review and approve" if first_review else "Open applications",
                "Approval is per exact package; nothing is submitted before it.",
            ),
        ]
        done = sum(1 for i in items if i["done"])
        return {"items": items, "done": done, "total": len(items), "complete": done == len(items)}

    def overview_kpis(counts: dict, usage_month: dict) -> list[dict]:
        jobs, apps, sync = counts["jobs"], counts["applications"], counts["sync"]
        prepared = sum(apps.get(k, 0) for k in ("ready_for_review", "needs_input", "authorized", "handoff"))
        submitted = apps.get("submitted", 0)
        pending = sum(sync.get(k, 0) for k in ("sync_pending", "local_only", "committed_local"))
        needs_ver = counts["needs_verification"] + apps.get("needs_verification", 0)
        calls = usage_month.get("calls", 0) or 0
        unknown = usage_month.get("unknown", 0) or 0
        if not calls:
            spend, spend_note = "$0.00", "no model calls this month"
        elif unknown:
            spend, spend_note = f"${usage_month.get('cost', 0):.2f}+", f"{unknown} of {calls} calls without known pricing"
        else:
            spend, spend_note = f"${usage_month.get('cost', 0):.2f}", f"{calls} model calls this month"
        conflicts = sync.get("conflict", 0)

        def kpi(label: str, value: Any, note: str, icon: str, tone: str, href: str, pill: str, pill_tone: str) -> dict:
            return {"label": label, "value": value, "note": note, "icon": icon, "tone": tone, "href": href, "pill": pill, "pill_tone": pill_tone}

        return [
            kpi(
                "Jobs to look at",
                jobs.get("new", 0) + jobs.get("needs_review", 0),
                f"{jobs.get('new', 0)} new · {jobs.get('needs_review', 0)} need review",
                "jobs",
                "",
                "/jobs",
                f"{jobs.get('qualified', 0)} qualified",
                "ok",
            ),
            kpi(
                "Applications submitted",
                submitted,
                f"{prepared} prepared and waiting on you",
                "applications",
                "ok",
                "/applications",
                f"{prepared} prepared",
                "warn" if prepared else "neutral",
            ),
            kpi(
                "Needs verification",
                needs_ver,
                "unknown outcomes to reconcile",
                "alert",
                "bad" if needs_ver else "",
                "/applications?status_filter=needs_verification",
                "action required" if needs_ver else "all clear",
                "bad" if needs_ver else "ok",
            ),
            kpi(
                "Sync pending",
                pending,
                f"{conflicts} conflicts · {sync.get('synced', 0)} synced",
                "sync",
                "warn" if pending else "",
                "/sync",
                "conflict" if conflicts else ("queued" if pending else "up to date"),
                "bad" if conflicts else ("warn" if pending else "ok"),
            ),
            kpi("Spend this month", spend, spend_note, "dollar", "info", "/usage", "unknown pricing" if unknown else "measured", "warn" if unknown else "neutral"),
        ]

    def applications_chart() -> dict:
        buckets = month_buckets(12)
        keys = {k: 0 for k, _ in buckets}
        for a in rt.store.c("applications").find({"status": "submitted"}, {"application_date": 1, "record.application_date": 1, "updated_at": 1}):
            day = a.get("application_date") or (a.get("record") or {}).get("application_date")
            key = str(day)[:7] if day else to_toronto(a.get("updated_at")).strftime("%Y-%m") if a.get("updated_at") else None
            if key in keys:
                keys[key] += 1
        return line_chart([(label, keys[k]) for k, label in buckets])

    def jobs_donut(by_status: dict) -> dict:
        order = [("new", "c1"), ("needs_review", "c4"), ("qualified", "c8"), ("applied", "c2"), ("applied_historical", "c3"), ("rejected", "c5")]
        items = [(k, by_status.get(k, 0), cls) for k, cls in order if by_status.get(k)]
        other = sum(v for k, v in by_status.items() if k not in dict(order))
        if other:
            items.append(("other", other, "c6"))
        return donut(items)

    def attention_items() -> list[dict]:
        s = rt.store
        items: list[dict] = []
        titles = {
            "ready_for_review": ("Awaiting your authorization", "Review the package and authorize this exact submission.", "warn", "key"),
            "needs_input": ("Needs your answers", "The form asks questions the profile cannot answer.", "warn", "help"),
            "handoff": ("Manual handoff", "Finish on the employer site, then record the confirmation here.", "info", "hand"),
            "needs_verification": ("Outcome unknown", "Check the employer site and reconcile what you observed.", "bad", "alert"),
        }

        def item(title: str, detail: str, hint: str, tone: str, icon: str, href: str, action: str) -> dict:
            return {"title": title, "detail": detail, "hint": hint, "tone": tone, "icon": icon, "href": href, "action": action}

        apps = list(s.c("applications").find({"status": {"$in": list(ATTENTION_STATUSES)}, "provenance.origin": "local"}).sort("updated_at", -1).limit(12))
        jobs = {j["_id"]: j for j in s.c("jobs").find({"_id": {"$in": [a["job_id"] for a in apps]}}, {"employer": 1, "title": 1})}
        for a in apps:
            t, d, tone, icon = titles[a["status"]]
            j = jobs.get(a["job_id"], {})
            items.append(item(t, f"{j.get('employer', '?')} · {j.get('title', '')}", d, tone, icon, f"/applications/{a['_id']}", next_action_label(a["status"])))
        for r in s.c("runs").find({"status": {"$in": ["waiting", "paused", "failed"]}}).sort("updated_at", -1).limit(8):
            it = r.get("interrupt") or {}
            if it.get("application_id") and any(x["href"].endswith(it["application_id"]) for x in items):
                continue
            label = {"waiting": f"Run waiting: {humanize(it.get('type') or 'interrupt')}", "paused": "Run paused", "failed": "Run failed"}[r["status"]]
            tone = "bad" if r["status"] == "failed" else "warn"
            verb = {"waiting": "Open run", "paused": "Resume run", "failed": "See what failed"}[r["status"]]
            items.append(item(label, f"{r['kind']} {r.get('params', {}).get('job_id', '')}", r.get("error") or "", tone, "runs", f"/runs/{r['_id']}", verb))
        review = s.c("jobs").count_documents({"status": "needs_review"})
        if review:
            detail = f"{review} job{'s' if review != 1 else ''} outside the automatic rules"
            items.append(item("Jobs need your review", detail, "Decide whether each one is worth a run.", "info", "jobs", "/jobs?status_filter=needs_review", "Review jobs"))
        held = s.c("recipients").count_documents({"history_reconciled_at": {"$exists": False}})
        if held:
            detail = f"{held} recipient{'s' if held != 1 else ''} held before first contact"
            hint = "Confirm you have not messaged them before."
            items.append(item("Recipients waiting for a contact-history check", detail, hint, "info", "outreach", "/outreach", "Check history"))
        return items

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
            ("Private profile, base resume copy, avatar, browser sessions", str(s.private_dir.resolve())),
            ("Tracker clone (GitHub ledger)", str(s.tracker_clone_dir.resolve())),
            ("Logs and PID files", str(s.log_dir.resolve())),
            ("Model pricing", str(s.pricing_file.resolve())),
            ("Runtime state", f"MongoDB database '{s.mongodb_database}'"),
        ]
        order = ["authorization", "material_hash", "batch", "needs_verification", "handoff", "emergency_stop", "sync_states", "outreach_scope", "fit_rank", "grounding"]
        order.append("regenerate")
        statuses = {
            "Applications": ["queued", "ready_for_review", "needs_input", "authorized", "executing", "handoff", "needs_verification", "submitted", "failed", "duplicate"],
            "Jobs": ["new", "needs_review", "qualified", "applied", "applied_historical", "excluded"],
            "Runs": ["queued", "running", "waiting", "paused", "failed", "succeeded"],
            "Sync": ["sync_pending", "committed_local", "local_only", "synced", "conflict"],
        }
        concepts = [(k, *GLOSSARY[k]) for k in order]
        return render(request, "help.html", session, places=places, concepts=concepts, statuses=statuses, hosts_example=HOSTS_ENV_EXAMPLE, allowed_hosts=sorted(s.allowed_hosts))

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_detail(request: Request, run_id: str, session: dict = Depends(require_session)):
        run = rt.store.c("runs").find_one({"_id": run_id})
        if not run:
            raise HTTPException(404)
        return render(request, "run.html", session, run=run)

    # -- profile ------------------------------------------------------------------------------
    @app.get("/profile", response_class=HTMLResponse)
    def profile(request: Request, session: dict = Depends(require_session)):
        p = rt.reload_profile()
        from .profile import compute_experience

        return render(request, "profile.html", session, p=p, exp=compute_experience(p.employment).as_dict(), provider_check=None)

    @app.post("/profile/preferences")
    def profile_prefs(
        request: Request,
        session: dict = Depends(require_session),
        csrf: str = Form(...),
        role_families: str = Form(""),
        excluded_role_families: str = Form(""),
        locations: str = Form(""),
        employment_types: str = Form("permanent"),
        floor_base: int = Form(0),
        target_base: str = Form(""),
        band_rule: str = Form("midpoint"),
        unknown_rule: str = Form("needs_input"),
        small_startup_rule: str = Form("needs_review"),
        excluded_employers: str = Form(""),
    ):
        require_csrf(request, session, csrf)
        p = rt.reload_profile()
        split = lambda s: [x.strip() for x in s.split(",") if x.strip()]  # noqa: E731
        p.preferences.role_families = [x.lower() for x in split(role_families)]
        p.preferences.excluded_role_families = [x.lower() for x in split(excluded_role_families)]
        p.preferences.locations = split(locations)
        p.preferences.employment_types = split(employment_types)
        p.preferences.small_startup_rule = small_startup_rule  # type: ignore[assignment]
        p.compensation.floor_base = floor_base
        p.compensation.target_base = int(target_base) if target_base.strip() else None
        p.compensation.band_rule = band_rule  # type: ignore[assignment]
        p.compensation.unknown_rule = unknown_rule  # type: ignore[assignment]
        p.compensation.provenance = Provenance(source="user:dashboard", confirmed_on=utcnow().date().isoformat())
        if excluded_employers.strip():
            p.preferences.excluded_employers = [
                ExcludedEmployer(name=line.split("|")[0].strip(), aliases=split(line.split("|")[1]) if "|" in line else [], reason="dashboard")
                for line in excluded_employers.splitlines()
                if line.strip()
            ]
        rt.profiles.save(p, actor="user")
        return RedirectResponse("/profile", status_code=303)

    @app.post("/profile/facts")
    def profile_fact(request: Request, session: dict = Depends(require_session), csrf: str = Form(...), key: str = Form(...), value: str = Form(""), category: str = Form("other")):
        require_csrf(request, session, csrf)
        from .profile import Fact

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

    # -- history -------------------------------------------------------------------------------
    @app.get("/history", response_class=HTMLResponse)
    def history(request: Request, session: dict = Depends(require_session)):
        apps = list(rt.store.c("applications").find({"provenance.origin": "tracker_import"}).sort("record.application_date", -1))
        imports = list(rt.store.c("imports").find().sort("created_at", -1).limit(5))
        preview = None
        if (rt.settings.tracker_clone_dir / ".git").exists():
            try:
                preview = TrackerImporter(rt.store, TrackerClone(rt.settings.tracker_clone_dir)).preview().as_dict()
            except Exception as exc:
                preview = {"error": f"{type(exc).__name__}: {exc}"}
        return render(request, "history.html", session, apps=apps, imports=imports, preview=preview, sync=rt.sync.status())

    @app.post("/history/import")
    def history_import(request: Request, session: dict = Depends(require_session), csrf: str = Form(...)):
        require_csrf(request, session, csrf)
        if not (rt.settings.tracker_clone_dir / ".git").exists():
            raise HTTPException(400, "tracker clone missing")
        TrackerImporter(rt.store, TrackerClone(rt.settings.tracker_clone_dir)).import_all()
        return RedirectResponse("/history", status_code=303)

    # -- jobs --------------------------------------------------------------------------------------
    @app.get("/jobs", response_class=HTMLResponse)
    def jobs(request: Request, session: dict = Depends(require_session), status_filter: str = ""):
        q = {"status": status_filter} if status_filter else {}
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
        ctx = {"job": job, "apps": apps, "artifacts": arts, "runs": runs, "local_app": local, "scopes": scopes, "steps": steps, "next_step": next_step, "active_run": active_run}
        return render(request, "job.html", session, **ctx)

    def job_steps(job: dict, app_doc: dict | None, arts: list, scope_active: bool, active_run: dict | None, outreach_n: int) -> tuple[list[dict], dict]:
        """Linear workflow for one job with the current step and the single next action under it."""
        st = (app_doc or {}).get("status")
        pkg = (app_doc or {}).get("review_package")
        after_approval = st in ("authorized", "executing", "handoff", "needs_verification", "submitted", "failed")
        submitted = st == "submitted" or job.get("status") in ("applied", "applied_historical")
        sync_state = ((app_doc or {}).get("sync") or {}).get("state")
        recorded = submitted and (bool((app_doc or {}).get("tracker_record_id")) or sync_state in ("synced", "not_applicable") or job.get("status") == "applied_historical")
        flags = [
            ("Import", True),
            ("Qualify", bool(job.get("qualification"))),
            ("Tailor", bool(arts)),
            ("Review", bool(pkg) and st not in ("needs_input", "queued", "preparing")),
            ("Authorize", after_approval or scope_active),
            ("Submit", submitted),
            ("Record", recorded),
            ("Outreach", outreach_n > 0),
        ]
        current = next((name for name, done in flags if not done), None)
        steps = [{"name": n, "done": d, "current": n == current} for n, d in flags]
        app_href = f"/applications/{app_doc['_id']}" if app_doc else None
        run_href = f"/runs/{active_run['_id']}" if active_run else None
        nxt: dict[str, Any] = {"label": "All done for this job", "href": None, "form": None, "note": ""}
        if job.get("status") == "excluded":
            nxt = {"label": "Excluded by your rules", "href": "/profile", "note": "Change the preferences under Candidate profile if this is wrong.", "verb": "Open preferences"}
        elif current == "Qualify":
            nxt = {"label": "Qualify this job", "form": f"/jobs/{job['_id']}/qualify", "verb": "Qualify and rank", "note": "Deterministic rules first, then the fit rank."}
        elif current == "Tailor":
            if active_run:
                nxt = {"label": "The worker is tailoring the resume", "href": run_href, "verb": "Watch the run", "note": ""}
            elif job.get("status") == "needs_review":
                note = "Qualification landed outside the automatic rules; read the reasons below first."
                nxt = {"label": "Decide whether this job is worth a run", "form": f"/jobs/{job['_id']}/run", "verb": "Start a run anyway", "note": note}
            else:
                note = "Nothing is submitted; the run pauses for your approval."
                nxt = {"label": "Tailor the resume and prepare the package", "form": f"/jobs/{job['_id']}/run", "verb": "Tailor and prepare", "note": note}
        elif current == "Review":
            if st == "needs_input":
                nxt = {"label": "The form asks questions the profile cannot answer", "href": app_href, "verb": "Answer questions", "note": ""}
            elif st in ("duplicate", "blocked", "denied"):
                nxt = {"label": status_label(st), "href": app_href, "verb": "See why", "note": (app_doc or {}).get("reason") or ""}
            elif active_run:
                nxt = {"label": "The worker is preparing the review package", "href": run_href, "verb": "Watch the run", "note": ""}
            else:
                nxt = {"label": "Prepare the review package", "form": f"/jobs/{job['_id']}/run", "verb": "Tailor and prepare", "note": ""}
        elif current == "Authorize":
            note = "Approval covers these answers and these document hashes only."
            nxt = {"label": "Read the package and approve this exact submission", "href": f"{app_href}#authorize", "verb": "Approve submission", "note": note}
        elif current == "Submit":
            if st == "handoff":
                nxt = {"label": "Apply on the employer site yourself, then record the confirmation", "href": app_href, "verb": "Finish on site", "note": "No supported adapter."}
            elif st == "needs_verification":
                nxt = {"label": "The outcome is unknown; check the employer site", "href": app_href, "verb": "Check outcome", "note": ""}
            elif st == "failed":
                nxt = {"label": "The submission failed", "href": app_href, "verb": "See what failed", "note": ""}
            elif st == "executing":
                nxt = {"label": "Submitting now", "href": run_href or app_href, "verb": "Watch", "note": ""}
            else:
                nxt = {"label": "Approved; the worker submits at the next tick", "href": run_href or app_href, "verb": "Watch the run", "note": ""}
        elif current == "Record":
            nxt = {"label": "Submitted; the tracker record is not on GitHub yet", "href": "/sync", "verb": "Open sync", "note": "A sync problem never changes the outcome."}
        elif current == "Outreach":
            nxt = {"label": "Submitted. Next: recruiter notes for this employer", "href": "/outreach", "verb": "Draft outreach", "note": "Drafts only; you send them yourself."}
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
            from .applications import FormField

            fields = [
                FormField(f.get("id", ""), f.get("label", ""), f.get("type", "text"), f.get("options", []), f.get("selector", ""), bool(f.get("required")))
                for f in (job.get("form_plan") or {}).get("fields", [])
            ]
            rt.applications().prepare(job_id, fields)
            return RedirectResponse(f"/jobs/{job_id}?ok=regenerated", status_code=303)
        return RedirectResponse(f"/jobs/{job_id}?ok={'regenerated_doc_only' if local else 'regenerated'}", status_code=303)

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

    # -- applications / review ---------------------------------------------------------------
    @app.get("/applications/{app_id}", response_class=HTMLResponse)
    def application(request: Request, app_id: str, session: dict = Depends(require_session)):
        a = rt.store.c("applications").find_one({"_id": app_id})
        if not a:
            raise HTTPException(404)
        job = rt.jobs.get(a["job_id"])
        scopes = [s for s in rt.auth.active("application_submit") if s.binding.target_id == a["job_id"] or s.mode == "batch"]
        action = rt.actions.by_key(f"submit:{a['job_id']}")
        doc_sha = ((a.get("review_package") or {}).get("document") or {}).get("sha256")
        doc_art = rt.store.c("artifacts").find_one({"sha256": doc_sha, "owner_id": a["job_id"]}, {"content": 0}) if doc_sha else None
        return render(request, "application.html", session, a=a, job=job, scopes=scopes, action=action, doc_art=doc_art)

    @app.post("/applications/{app_id}/inputs")
    async def application_inputs(request: Request, app_id: str, session: dict = Depends(require_session)):
        form = await request.form()
        require_csrf(request, session, form.get("csrf"))
        values = {k[6:]: v for k, v in form.items() if k.startswith("input_") and str(v).strip()}
        rt.applications().supply_inputs(app_id, values)
        return RedirectResponse(f"/applications/{app_id}", status_code=303)

    @app.post("/applications/{app_id}/authorize")
    def application_authorize(
        request: Request, app_id: str, session: dict = Depends(require_session), csrf: str = Form(...), material_hash: str = Form(...), hours: int = Form(24), note: str = Form("")
    ):
        require_csrf(request, session, csrf)
        a = rt.store.c("applications").find_one({"_id": app_id})
        if a["status"] != "ready_for_review" or a["material_hash"] != material_hash:
            raise HTTPException(409, "review package changed or not ready; refresh and review again")
        host = (a["review_package"].get("apply_url") or "").split("/")[2] if "://" in (a["review_package"].get("apply_url") or "") else None
        rt.auth.grant(
            "application_submit", ScopeBinding(target_id=a["job_id"], material_hash=material_hash, allowed_hosts=[host] if host else []), ttl=timedelta(hours=hours), note=note
        )
        rt.store.c("applications").update_one({"_id": app_id}, {"$set": {"status": "authorized", "updated_at": utcnow()}})
        for run in rt.store.c("runs").find({"params.job_id": a["job_id"], "status": "waiting", "interrupt.type": "awaiting_authorization"}):
            rt.store.update_run(run["_id"], requested="resume", resume_payload={"authorized": True})
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

    # -- authorizations --------------------------------------------------------------------------
    @app.get("/authorizations", response_class=HTMLResponse)
    def authorizations(request: Request, session: dict = Depends(require_session)):
        return render(request, "authorizations.html", session, scopes=list(rt.store.c("authorizations").find().sort("created_at", -1).limit(100)))

    @app.post("/authorizations/batch")
    def batch_scope(
        request: Request,
        session: dict = Depends(require_session),
        csrf: str = Form(...),
        employers: str = Form(""),
        sites: str = Form(""),
        max_fit_rank: int = Form(2),
        max_count: int = Form(3),
        hours: int = Form(48),
        note: str = Form(""),
    ):
        require_csrf(request, session, csrf)
        kind = "application_submit"  # the only kind that may be batch-authorized; outreach/email are per action
        filters = {"max_fit_rank": max_fit_rank}
        if employers.strip():
            filters["employers"] = [e.strip() for e in employers.split(",") if e.strip()]
        if sites.strip():
            filters["sites"] = [s.strip() for s in sites.split(",") if s.strip()]
        rt.auth.grant(
            kind,
            ScopeBinding(job_filters=filters, allowed_hosts=filters.get("sites", [])),
            mode="batch",
            max_count=max_count,
            ttl=timedelta(hours=hours),
            note=note,
            stop=StopConditions(stop_on_needs_verification=True, max_failures=1, stop_on_material_unknowns=True),
        )
        return RedirectResponse("/authorizations", status_code=303)

    @app.post("/authorizations/{scope_id}/revoke")
    def revoke(request: Request, scope_id: str, session: dict = Depends(require_session), csrf: str = Form(...)):
        require_csrf(request, session, csrf)
        rt.auth.revoke(scope_id, "dashboard")
        return RedirectResponse("/authorizations", status_code=303)

    # -- outreach -------------------------------------------------------------------------------
    @app.get("/outreach", response_class=HTMLResponse)
    def outreach(request: Request, session: dict = Depends(require_session)):
        o = rt.outreach()
        recs = list(rt.store.c("recipients").find().sort("updated_at", -1).limit(200))
        for r in recs:
            r["history"] = o.recipients.history_check(r)
        msgs = list(rt.store.c("outreach").find().sort("created_at", -1).limit(200))
        submitted = list(rt.store.c("applications").find({"status": "submitted"}).sort("updated_at", -1).limit(50))
        jobs = {j["_id"]: j for j in rt.store.c("jobs").find({"_id": {"$in": [a["job_id"] for a in submitted]}})}
        return render(request, "outreach.html", session, recipients=recs, messages=msgs, submitted=submitted, jobs=jobs, smtp=bool(rt.smtp))

    @app.post("/outreach/recipients")
    def add_recipient(
        request: Request,
        session: dict = Depends(require_session),
        csrf: str = Form(...),
        name: str = Form(...),
        employer: str = Form(...),
        profile_url: str = Form(""),
        email: str = Form(""),
        remit: str = Form(""),
    ):
        require_csrf(request, session, csrf)
        rt.outreach().recipients.upsert(name=name, employer=employer, profile_url=profile_url or None, email=email or None, remit=remit, provenance="dashboard")
        return RedirectResponse("/outreach", status_code=303)

    @app.post("/outreach/recipients/{rid}/reconcile")
    def reconcile_recipient(request: Request, rid: str, session: dict = Depends(require_session), csrf: str = Form(...), note: str = Form("")):
        require_csrf(request, session, csrf)
        rt.outreach().recipients.mark_reconciled(rid, note)
        return RedirectResponse("/outreach", status_code=303)

    @app.post("/outreach/recipients/{rid}/history")
    def import_recipient_history(
        request: Request, rid: str, session: dict = Depends(require_session), csrf: str = Form(...), kind: str = Form("message"), when: str = Form(""), note: str = Form("")
    ):
        require_csrf(request, session, csrf)
        rec = rt.store.c("recipients").find_one({"_id": rid})
        alias = next(iter(rec.get("aliases", [])), None)
        if alias:
            rt.outreach().recipients.import_history(alias=alias, kind=kind, when=when or None, note=note, source="dashboard")
        return RedirectResponse("/outreach", status_code=303)

    @app.post("/outreach/recipients/{rid}/draft")
    def draft(request: Request, rid: str, session: dict = Depends(require_session), csrf: str = Form(...), kind: str = Form("connection_request"), applied_role: str = Form("")):
        require_csrf(request, session, csrf)
        rec = rt.store.c("recipients").find_one({"_id": rid})
        if applied_role:
            # only accept roles with a confirmed submission at this employer
            confirmed = rt.store.c("applications").find_one({"status": "submitted"})
            jobs = [j for j in rt.store.c("jobs").find({"employer_norm": rec["employer_norm"], "status": {"$in": ["applied", "applied_historical"]}})]
            if not confirmed or not any(j["title"].lower() == applied_role.lower() for j in jobs):
                raise HTTPException(400, "applied_role must match a confirmed submission at this employer")
        rt.outreach().draft(rid, kind, applied_role or None)
        return RedirectResponse("/outreach", status_code=303)

    @app.post("/outreach/messages/{mid}/send")
    def send(request: Request, mid: str, session: dict = Depends(require_session), csrf: str = Form(...), channel: str = Form("linkedin")):
        require_csrf(request, session, csrf)
        res = rt.outreach().send(mid, "dashboard", channel)
        rt.store.c("outreach").update_one({"_id": mid}, {"$set": {"last_send_result": res}})
        return RedirectResponse("/outreach", status_code=303)

    @app.post("/outreach/messages/{mid}/record-send")
    def record_send(request: Request, mid: str, session: dict = Depends(require_session), csrf: str = Form(...), confirmation: str = Form(...)):
        require_csrf(request, session, csrf)
        rt.outreach().record_manual_send(mid, confirmation)
        return RedirectResponse("/outreach", status_code=303)

    # -- sync / usage ------------------------------------------------------------------------
    @app.get("/sync", response_class=HTMLResponse)
    def sync(request: Request, session: dict = Depends(require_session)):
        return render(request, "sync.html", session, status=rt.sync.status(), items=list(rt.store.c("sync_outbox").find().sort("created_at", -1).limit(100)))

    @app.post("/sync/{what}")
    def sync_action(request: Request, what: str, session: dict = Depends(require_session), csrf: str = Form(...)):
        require_csrf(request, session, csrf)
        if what == "process":
            rt.store.c("sync_outbox").update_many({"status": {"$in": ["sync_pending", "local_only"]}}, {"$set": {"next_attempt_at": utcnow()}})
        elif what == "reconcile":
            rt.sync.reconcile_all()
        else:
            raise HTTPException(404)
        return RedirectResponse("/sync", status_code=303)

    @app.get("/usage", response_class=HTMLResponse)
    def usage(request: Request, session: dict = Depends(require_session)):
        rows = list(rt.store.c("usage").find().sort("created_at", -1).limit(200))
        by_skill = list(
            rt.store.c("usage").aggregate(
                [
                    {
                        "$group": {
                            "_id": "$skill",
                            "calls": {"$sum": 1},
                            "in": {"$sum": "$input_tokens"},
                            "out": {"$sum": "$output_tokens"},
                            "cost": {"$sum": {"$ifNull": ["$cost_usd", 0]}},
                            "unknown": {"$sum": {"$cond": [{"$eq": ["$cost_known", False]}, 1, 0]}},
                            "cached": {"$sum": {"$cond": ["$cached_result", 1, 0]}},
                        }
                    }
                ]
            )
        )
        submitted = rt.store.c("applications").count_documents({"status": "submitted", "provenance.origin": "local"})
        return render(
            request,
            "usage.html",
            session,
            rows=rows,
            by_skill=by_skill,
            submitted=submitted,
            pricing=rt.pricing.loaded_from,
            catalog=rt.ai(None).catalog.describe() if rt.ai(None) else [],
        )

    # -- applications list / documents ---------------------------------------------------------
    @app.get("/applications", response_class=HTMLResponse)
    def applications_list(request: Request, session: dict = Depends(require_session), status_filter: str = ""):
        q: dict[str, Any] = {"provenance.origin": "local"}
        if status_filter:
            q["status"] = status_filter
        apps = list(rt.store.c("applications").find(q).sort("updated_at", -1).limit(200))
        jobs = {j["_id"]: j for j in rt.store.c("jobs").find({"_id": {"$in": [a["job_id"] for a in apps]}}, {"employer": 1, "title": 1, "location": 1})}
        counts = {r["_id"]: r["n"] for r in rt.store.c("applications").aggregate([{"$match": {"provenance.origin": "local"}}, {"$group": {"_id": "$status", "n": {"$sum": 1}}}])}
        return render(request, "applications.html", session, apps=apps, jobs=jobs, counts=counts, status_filter=status_filter, tab="packages")

    @app.get("/documents", response_class=HTMLResponse)
    def documents(request: Request, session: dict = Depends(require_session)):
        arts = list(rt.store.c("artifacts").find({}, {"content": 0}).sort("created_at", -1).limit(200))
        jobs = {j["_id"]: j for j in rt.store.c("jobs").find({"_id": {"$in": [a.get("owner_id") for a in arts]}}, {"employer": 1, "title": 1})}
        return render(request, "documents.html", session, artifacts=arts, jobs=jobs)

    @app.get("/documents/{artifact_id}/download")
    def document_download(artifact_id: str, session: dict = Depends(require_session), kind: str = "docx", inline: int = 0):
        """Serve a tailored document by id only. The path comes from the artifact record, must resolve under the
        artifact directory, and its hash must still match the reviewed hash; otherwise nothing is served."""
        art = rt.store.c("artifacts").find_one({"_id": artifact_id})
        if not art:
            raise HTTPException(404)
        if kind == "pdf":
            raw, expected, media, name = art.get("pdf_path"), art.get("pdf_sha256"), "application/pdf", None
        elif kind == "docx":
            raw, expected, media, name = art.get("path"), art.get("sha256"), DOCX_MEDIA_TYPE, art.get("filename")
        else:
            raise HTTPException(400, "kind must be docx or pdf")
        if not raw or not expected:
            raise HTTPException(404, f"no {kind} recorded for this document")
        path = Path(raw).resolve()
        root = rt.settings.artifact_dir.resolve()
        if not path.is_relative_to(root):
            rt.store.audit("document_path_refused", artifact_id, kind=kind)
            raise HTTPException(403, "document path is outside the artifact directory")
        if not path.is_file():
            raise HTTPException(404, "file missing on this host")
        if sha256_file(path) != expected:
            rt.store.audit("document_hash_mismatch", artifact_id, kind=kind)
            raise HTTPException(409, "file on disk no longer matches the reviewed hash; not serving it")
        filename = re.sub(r"[^A-Za-z0-9._-]+", "_", name or path.name) or f"document.{kind}"
        disposition = "inline" if inline and kind == "pdf" else "attachment"
        headers = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
        return FileResponse(path, media_type=media, filename=filename, content_disposition_type=disposition, headers=headers)

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
        )

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

    return app
