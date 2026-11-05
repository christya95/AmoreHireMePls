"""Shared templates, static files and page helpers for the dashboard.

Route modules and create_app import these names. Session, CSRF and rendering stay in create_app."""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer

from amorehiremepls.apply.browser import normalize_screenshots
from amorehiremepls.candidate.candidate_profile import CONTROL_KEY as TOML_CONTROL_KEY
from amorehiremepls.candidate.candidate_profile import PROFILE_TOML_NAME, ProfileTomlError, apply_profile_toml, load_profile_toml, toml_status
from amorehiremepls.candidate.profile import ExcludedEmployer, Provenance, RecipientExclusion
from amorehiremepls.core.authorization import ScopeBinding, StopConditions
from amorehiremepls.core.runtime import Runtime
from amorehiremepls.core.util import sha256_file, to_local, utcnow, zone_label
from amorehiremepls.core.worker import next_slot_description, worker_healthy
from amorehiremepls.jobs.discovery import SOURCES
from amorehiremepls.jobs.logos import EMPLOYER_DOMAINS, REQUEST_TIMEOUT
from amorehiremepls.records.tracker import TrackerClone, TrackerImporter
from amorehiremepls.web.ui_settings import (
    ACCENT_PRESETS,
    AVATAR_MAX_BYTES,
    AVATAR_STYLES,
    GLOSSARY,
    PAGE_INTROS,
    SCREENSHOT_LABELS,
    STEP_HELP,
    TINT_LABELS,
    TINT_PRESETS,
    AvatarError,
    UISettings,
    UISettingsStore,
    donut,
    evidence_source,
    fit_score,
    greeting,
    humanize,
    initials,
    line_chart,
    month_buckets,
    next_action_label,
    normalize_accent,
    normalize_tint,
    plain_failure,
    plain_fit_line,
    remove_avatar_files,
    resolve_display_name,
    status_label,
    status_tone,
    store_avatar,
    tint_css_from_cookie,
)

DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _local(dt: Any, fmt: str) -> str:
    """Wall time in the configured zone for a datetime or an ISO-8601 string (screenshot evidence stores strings); blank when unparseable."""
    if not dt:
        return ""
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except ValueError:
            return dt
    return to_local(dt).strftime(fmt)


TEMPLATES.env.filters["local"] = lambda dt: _local(dt, "%Y-%m-%d %H:%M")
TEMPLATES.env.filters["local_day"] = lambda dt: _local(dt, "%b %d")
# Older templates and tests used the original operator's zone as the filter name; the aliases keep them working.
TEMPLATES.env.filters["toronto"] = TEMPLATES.env.filters["local"]
TEMPLATES.env.filters["toronto_day"] = TEMPLATES.env.filters["local_day"]
TEMPLATES.env.globals["tz_label"] = zone_label
TEMPLATES.env.filters["json"] = lambda v: json.dumps(v, indent=1, default=str)
TEMPLATES.env.filters["tone"] = status_tone
TEMPLATES.env.filters["human"] = humanize
TEMPLATES.env.filters["label"] = status_label
TEMPLATES.env.filters["verb"] = next_action_label
TEMPLATES.env.filters["initials"] = initials
TEMPLATES.env.filters["plain"] = plain_fit_line
TEMPLATES.env.filters["failure"] = plain_failure
TEMPLATES.env.filters["evidence"] = evidence_source
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
    "application.html": "Application package",
    "runs.html": "Applications · Background tasks",
    "run.html": "Background task",
    "documents.html": "Resumes",
    "outreach.html": "Recruiters",
    "history.html": "Past applications",
    "sync.html": "GitHub tracker",
    "usage.html": "AI usage and cost",
    "people.html": "People",
    "person.html": "Person",
    "access_ended.html": "Access ended",
    "profile.html": "Your profile",
    "authorizations.html": "Approvals",
    "settings.html": "Settings",
    "help.html": "Help",
}
NAV = [
    ("Work", None, None),
    ("/", "Overview", "overview"),
    ("/jobs", "Jobs", "jobs"),
    ("/applications", "Applications", "applications"),
    ("/documents", "Resumes", "documents"),
    ("/outreach", "Recruiters", "outreach"),
    ("Records", None, None),
    ("/history", "Past applications", "history"),
    ("/sync", "GitHub tracker", "sync"),
    ("/usage", "AI usage", "usage"),
    ("Account", None, None),
    ("/admin", "People", "profile"),
    ("/authorizations", "Approvals", "auth"),
    ("/profile", "Your profile", "profile"),
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
    "provider_ok": ("The AI connection works.", "ok"),
    "provider_failed": ("The AI connection failed. See the System card for the reason.", "bad"),
    "regenerated": ("The resume was rebuilt. The files changed, so any earlier approval for this job was withdrawn.", "ok"),
    "regenerated_doc_only": ("A fresh resume was made. The existing package was left as it is because of its state.", "ok"),
    "no_base": ("Choose your approved base resume under Your profile first.", "bad"),
    "busy": ("A background task is working on this job right now. Wait for it to pause or finish.", "bad"),
    "already_submitted": ("This application was already sent. The files are kept exactly as they were sent.", "bad"),
    "app_removed": ("This application was removed. The job is still here. Nothing was sent, and the GitHub tracker history was left as it is.", "ok"),
    "app_withdrawn": (
        "This application was removed from the dashboard. The job is still here. If a GitHub tracker row exists, it is marked withdrawn and the rest of the history stays.",
        "ok",
    ),
    "job_restarted": ("The unsent application was cleared. Read the new package and approve it again. New resume files replace the old approval. Nothing was sent.", "ok"),
    "confirm_needed": ("Type the confirmation word to continue. Nothing was changed.", "bad"),
    "already_sent": ("This application was already sent, so it was left as it is.", "bad"),
    "receipt_saved": ("A confirmation from the employer is already saved, so this application was left as it is.", "bad"),
    "result_unclear": ("The result is unclear, so this application stays until you record what the site showed.", "bad"),
    "still_working": ("The app is still working on this application. Wait until it stops.", "bad"),
    "already_removed": ("That application was already removed.", "bad"),
    "missing": ("There is no application to change.", "bad"),
    "captcha_on": ("The app will now try to solve simple captchas itself during an approved application. You can turn this off here any time.", "ok"),
    "captcha_off": ("Captchas are left to you again. A human check pauses the application for you to finish.", "ok"),
    "retry_started": ("The app will read the form again and prepare a fresh package. It waits for your approval before applying.", "ok"),
    "retry_unsupported": ("The app has no form filler for this job site yet, so there is nothing to retry. Finish on the site yourself.", "bad"),
    "retry_busy": ("A background task is working on this job right now. Wait for it to pause or finish.", "bad"),
    "continue_ok": ("Continue received. The same page will be checked again. The form is not sent a second time.", "ok"),
    "continue_missing": ("That check is no longer being held.", "bad"),
    "continue_changed": ("The package changed, so Continue was refused.", "bad"),
    "notify_sent": ("Test notification sent.", "ok"),
    "notify_failed": ("The test notification could not be sent. Check the mail server settings and try again.", "bad"),
    "notify_off": ("Nothing is set up to send a notification yet.", "bad"),
    "candidate_saved": ("The file is in your candidate folder. It is read the next time the app checks a fit or tailors a resume.", "ok"),
    "candidate_bad_type": ("Only Word (.docx), Markdown (.md) or plain text (.txt) files are accepted.", "bad"),
    "candidate_too_big": ("That file is too large. The limit is 5 MB.", "bad"),
    "candidate_bad_name": ("That file name was not accepted. Give the file a short plain name and try again.", "bad"),
    "candidate_not_writable": ("The app cannot write to the candidate folder on this computer. Check the folder permissions.", "bad"),
    "toml_loaded": ("Your profile file was loaded. The counts are on the profile file card.", "ok"),
    "toml_invalid": ("The profile file could not be read. Nothing was changed; the reason is on the profile file card.", "bad"),
    "preferences_saved": ("Your rules were saved.", "ok"),
    "person_added": ("That person can sign in. They have 365 days of access.", "ok"),
    "access_extended": ("Access was extended.", "ok"),
    "password_set": ("The password was changed.", "ok"),
    "person_off": ("That person is turned off. Their queued tracker items still save.", "ok"),
    "person_on": ("That person is turned back on.", "ok"),
    "person_deleted": ("That person was deleted. A backup was saved first.", "ok"),
    "person_refused": ("That change was not made.", "bad"),
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


def _nav(path: str, attention: int, show_usage: bool = True, show_admin: bool = True) -> list[dict]:
    items: list[dict] = []
    best = None
    for alias, target in NAV_ALIASES.items():
        if path == alias or path.startswith(alias + "/"):
            path = target
    for href, _, icon in NAV:
        if icon and (path == href or (href != "/" and path.startswith(href + "/"))) and (best is None or len(href) > len(best)):
            best = href
    hidden = set()
    if not show_usage:
        hidden.add("/usage")
    if not show_admin:
        hidden.add("/admin")
    for href, label, icon in NAV:
        if href in hidden:
            continue
        if icon is None:
            items.append({"label": href, "label_only": True})
            continue
        items.append({"href": href, "label": label, "icon": icon, "active": href == best, "count": attention if href == "/applications" else 0})
    return items


_HIDDEN_COST_KEYS = {"cost_usd", "cost_known", "input_tokens", "output_tokens", "cached_tokens", "usage"}


def without_costs(value: Any) -> Any:
    """Drop token counts and prices from a value shown to a member. The stored record is not changed."""
    if isinstance(value, dict):
        return {k: without_costs(v) for k, v in value.items() if k not in _HIDDEN_COST_KEYS}
    if isinstance(value, list):
        return [without_costs(item) for item in value]
    return value


def show_usage_for(session: dict | None) -> bool:
    """Members never see AI usage, token counts or cost. A missing role is the original single operator, who does."""
    return (session or {}).get("role") != "member"


_WAIT_LINE = {
    "needs_input": "Waiting for your answers",
    "awaiting_authorization": "Waiting for your approval",
    "handoff": "Waiting for you to finish on the site or record the result",
}

_HISTORY_LINE = {
    "ready_for_review": "Package ready for you to read",
    "needs_input": "Some answers are still needed",
    "authorized": "You approved this package",
    "handoff": "Stopped before anything was sent",
    "submitted": "Recorded as applied",
    "failed": "Applying failed",
    "needs_verification": "The result is unclear",
    "executing": "Applying now",
    "challenge_retry": "Will try the human check once more later",
}


def saved_form_inputs(app: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for answer in (app.get("review_package") or {}).get("answers") or []:
        if not answer.get("field_id") or "artifact" in (answer.get("fact_ids") or []) or answer.get("sensitive"):
            continue  # self-identification answers are not replayed into the run record; the package already holds them
        out[answer["field_id"]] = "" if answer.get("value") is None else str(answer["value"])
    return out


def continue_waiting_runs(rt: Runtime, app: dict) -> None:
    """Saving answers or approving from the page can happen while the task is still parked on an earlier step.
    Ask that task to continue. This only sets a flag; the task does the work on its next pass."""
    if app.get("status") == "needs_input":
        return
    inputs = saved_form_inputs(app)
    for run in rt.store.c("runs").find({"params.job_id": app["job_id"], "status": "waiting", "interrupt.type": "needs_input"}):
        rt.store.update_run(run["_id"], requested="resume", resume_payload={"inputs": inputs})
    if app.get("status") == "authorized":
        for run in rt.store.c("runs").find({"params.job_id": app["job_id"], "status": "waiting", "interrupt.type": "awaiting_authorization"}):
            rt.store.update_run(run["_id"], requested="resume", resume_payload={"authorized": True})


def application_timeline(app: dict, runs: list[dict]) -> list[dict]:
    """Plain updates from records already saved. No model call."""
    rows: list[dict] = []
    for entry in app.get("history") or []:
        status = entry.get("status") or ""
        text = _HISTORY_LINE.get(status, status.replace("_", " "))
        reason = (entry.get("reason") or "").strip()
        if reason:
            text = f"{text}. {reason}"
        if entry.get("at"):
            rows.append({"at": entry["at"], "text": text})
    for run in runs:
        for event in run.get("events") or []:
            text = _run_event_line(run, event)
            if event.get("at") and text:
                rows.append({"at": event["at"], "text": text})
    rows.sort(key=lambda row: row["at"], reverse=True)
    kept: list[dict] = []
    for row in rows:
        if kept and kept[-1]["text"] == row["text"]:
            continue
        kept.append(row)
    return kept[:20]


def _run_event_line(run: dict, event: dict) -> str:
    message = event.get("message") or ""
    if message == "waiting":
        return _WAIT_LINE.get((run.get("interrupt") or {}).get("type"), "Paused")
    if message == "running":
        return "Task started"
    if message == "completed":
        return "Task finished"
    if message == "failed":
        return f"Task stopped: {(run.get('error') or 'it stopped')[:180]}"
    if message.startswith("cancelled"):
        return "Task was replaced"
    if message == "queued":
        return "Task queued"
    return message


def authorized_now(app: dict, runs: list[dict]) -> str | None:
    if app.get("status") != "authorized":
        return None
    live = [run for run in runs if run.get("status") in ("queued", "claimed", "running", "waiting", "paused")]
    if any(run["status"] in ("queued", "claimed", "running") for run in live):
        return "The app is applying now. Refresh this page to see what happened."
    if any(run.get("requested") == "resume" for run in live):
        return "Approved. The task will continue within a minute. Refresh to see the result."
    waiting = next((run for run in live if run["status"] == "waiting"), None)
    kind = (waiting.get("interrupt") or {}).get("type") if waiting else None
    if kind == "needs_input":
        return "Approved, but the task is still waiting on an earlier step, so nothing is being sent yet."
    if kind == "awaiting_authorization":
        return "Approved. The task will apply within a minute."
    return "Approved, and nothing is being sent right now. No task is working on this application."


__all__ = [
    "ACCENT_PRESETS",
    "ATTENTION_STATUSES",
    "AVATAR_MAX_BYTES",
    "AVATAR_STYLES",
    "Any",
    "AvatarError",
    "BRAND_DIR",
    "BadSignature",
    "DOCX_MEDIA_TYPE",
    "Depends",
    "EMPLOYER_DOMAINS",
    "ExcludedEmployer",
    "FLASHES",
    "FastAPI",
    "File",
    "FileResponse",
    "Form",
    "GLOSSARY",
    "HOSTS_ENV_EXAMPLE",
    "HTMLResponse",
    "HTTPException",
    "JSONResponse",
    "Jinja2Templates",
    "NAV",
    "NAV_ALIASES",
    "PAGE_INTROS",
    "PAGE_TITLES",
    "PROFILE_TOML_NAME",
    "Path",
    "ProfileTomlError",
    "Provenance",
    "REQUEST_TIMEOUT",
    "RecipientExclusion",
    "RedirectResponse",
    "Request",
    "Runtime",
    "SCREENSHOT_LABELS",
    "SESSION_COOKIE",
    "SESSION_MAX_AGE",
    "SOURCES",
    "STATIC_DIR",
    "STEP_HELP",
    "ScopeBinding",
    "StaticFiles",
    "StopConditions",
    "TEMPLATES",
    "TINT_LABELS",
    "TINT_PRESETS",
    "TOML_CONTROL_KEY",
    "TrackerClone",
    "TrackerImporter",
    "UISettings",
    "UISettingsStore",
    "UI_COOKIE",
    "UI_COOKIE_MAX_AGE",
    "URLSafeTimedSerializer",
    "UploadFile",
    "_HISTORY_LINE",
    "_WAIT_LINE",
    "_brand_paths",
    "_local",
    "_nav",
    "_parse_ui_cookie",
    "_run_event_line",
    "annotations",
    "application_timeline",
    "apply_profile_toml",
    "authorized_now",
    "continue_waiting_runs",
    "datetime",
    "donut",
    "evidence_source",
    "favicon_svg",
    "fit_score",
    "greeting",
    "hmac",
    "humanize",
    "initials",
    "json",
    "line_chart",
    "load_profile_toml",
    "lru_cache",
    "month_buckets",
    "next_action_label",
    "next_slot_description",
    "normalize_accent",
    "normalize_screenshots",
    "normalize_tint",
    "os",
    "plain_failure",
    "plain_fit_line",
    "re",
    "remove_avatar_files",
    "resolve_display_name",
    "saved_form_inputs",
    "secrets",
    "sha256_file",
    "status",
    "status_label",
    "status_tone",
    "store_avatar",
    "timedelta",
    "tint_css_from_cookie",
    "to_local",
    "toml_status",
    "urlsplit",
    "utcnow",
    "worker_healthy",
    "zone_label",
]
