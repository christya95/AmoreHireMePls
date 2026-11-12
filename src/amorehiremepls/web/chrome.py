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
from amorehiremepls.core.actions import REPLAY_LABEL
from amorehiremepls.core.authorization import ScopeBinding, StopConditions
from amorehiremepls.core.runtime import Runtime
from amorehiremepls.core.site_signin import stop_for
from amorehiremepls.core.util import redact_free, sha256_file, to_local, utcnow, zone_label
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
    needed_lines,
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
TEMPLATES.env.filters["needed"] = needed_lines
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
    "application.html": "This application",
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
    "regenerated_doc_only": ("A fresh resume was made. This application was left as it is because of its state.", "ok"),
    "no_base": ("Choose your approved base resume under Your profile first.", "bad"),
    "busy": ("A background task is working on this job right now. Wait for it to pause or finish.", "bad"),
    "already_submitted": ("This application was already sent. The files are kept exactly as they were sent.", "bad"),
    "app_removed": ("This attempt was cleared. The job is still here, so you can prepare it again. Nothing was sent, and the tracker history was left as it is.", "ok"),
    "app_withdrawn": (
        "This application was removed from the dashboard. The job is still here. If a GitHub tracker row exists, it is marked withdrawn and the rest of the history stays.",
        "ok",
    ),
    "job_restarted": ("The unsent application was cleared. Read the new one and approve it again. New resume files replace the old approval. Nothing was sent.", "ok"),
    "posting_closed": (
        "This posting is no longer up. The job is under Closed. Tailored resumes that were never sent were removed. Your base resume and anything already sent were kept.",
        "ok",
    ),
    "posting_open": ("This posting is still up.", "ok"),
    "posting_kept": ("An application was already sent, so this job was left open and its files were kept.", "ok"),
    "posting_unreadable": ("The posting could not be read. The job was left as it is.", "bad"),
    "postings_checked": ("Checked which jobs are still posted. Any that are no longer up are under Closed. Sent applications were left alone.", "ok"),
    "postings_fresh": ("Those jobs were checked in the last few hours. Open one job and check that posting if you want to look again.", "ok"),
    "resumes_removed": (
        "Tailored resumes that were never sent were deleted. Your base resume and any sent application were kept. An approval that pointed at a deleted file no longer applies.",
        "ok",
    ),
    "resumes_none": ("No unused tailored resumes were found. Sent applications and your base resume were left as they are.", "ok"),
    "confirm_needed": ("Type the confirmation word to continue. Nothing was changed.", "bad"),
    "tasks_cleared": ("Queued tasks that had not started were cancelled, and finished tasks were hidden from this list. Jobs and applications stay.", "ok"),
    "already_sent": ("This application was already sent, so it was left as it is.", "bad"),
    "receipt_saved": ("A confirmation from the employer is already saved, so this application was left as it is.", "bad"),
    "result_unclear": ("The result is unclear, so this application stays until you record what the site showed.", "bad"),
    "still_working": ("The app is still working on this application. Wait until it stops.", "bad"),
    "already_removed": ("That application was already removed.", "bad"),
    "missing": ("There is no application to change.", "bad"),
    "captcha_on": ("The app will now try to solve simple captchas itself during an approved application. You can turn this off here any time.", "ok"),
    "captcha_off": ("Captchas are left to you again. A human check pauses the application for you to finish.", "ok"),
    "apply_queued": ("The app will open the job site with these answers. Your approval already covers them.", "ok"),
    "retry_started": ("The app will read the form again and prepare fresh answers. It waits for your approval before applying.", "ok"),
    "retry_unsupported": ("The app has no form filler for this job site yet, so there is nothing to retry. Finish on the site yourself.", "bad"),
    "retry_busy": ("A background task is working on this job right now. Wait for it to pause or finish.", "bad"),
    "cancel_requested": ("Cancel received. The app stops before the final click and closes the page. Nothing is sent, and you can try again later.", "ok"),
    "cancel_missing": ("Nothing is filling in this form right now, so there was nothing to stop.", "bad"),
    "cancel_too_late": (
        "The final click already happened, so this attempt cannot be stopped. The app is checking the result. Record what the site shows if it stays unclear.",
        "bad",
    ),
    "continue_ok": ("Continue received. The same page will be checked again. The form is not sent a second time.", "ok"),
    "continue_missing": ("That check is no longer being held.", "bad"),
    "continue_changed": ("The answers or the resume changed, so Continue was refused.", "bad"),
    "field_ok": ("Answer received. It will be typed into the same open form. The form is not started over.", "ok"),
    "field_missing": ("That field is no longer waiting.", "bad"),
    "field_empty": ("Type an answer first. Nothing was entered on the form.", "bad"),
    "field_changed": ("The answers or the resume changed, so that answer was refused.", "bad"),
    "signin_allowed": ("1Password was approved. The saved login is being used on the sign-in page now.", "ok"),
    "signin_code_sent": ("The one-time code from 1Password is being typed now.", "ok"),
    "signin_not_approved": ("1Password was not approved in time. Press Allow 1Password again when you are ready. Nothing failed and nothing was sent.", "bad"),
    "signin_lookup_failed": ("1Password did not answer just now. Press Allow 1Password again. Nothing was sent.", "bad"),
    "signin_gave_up": ("1Password was not approved after several tries, so this attempt stopped. Nothing was sent. Retry with the app is safe.", "bad"),
    "signin_not_here": ("This dashboard cannot open 1Password. Open this page from the dashboard on the computer where 1Password is installed, then press Allow there.", "bad"),
    "signin_missing": ("That sign-in step is no longer waiting.", "bad"),
    "signin_changed": ("The saved login changed while the page was waiting. Retry with the app.", "bad"),
    "signin_no_code": ("The saved login has no one-time code in 1Password. Type the code here instead.", "bad"),
    "signin_declined": ("The app will not sign in this time. Nothing was read from 1Password and nothing was sent.", "ok"),
    "signin_resend": ("The app is asking the job site to send a new code. Type it here when it arrives.", "ok"),
    "signin_unchanged": ("The saved login in 1Password has not changed since the site refused it, so it was not sent. Nothing was sent.", "bad"),
    "signin_cycle_cap": ("The site refused the saved login too many times, so the app stopped trying for now. Nothing was sent.", "bad"),
    "signin_no_login": ("1Password was approved, but it has no saved login for this site. Nothing was sent.", "bad"),
    "signin_several": ("1Password has more than one saved login for this site, so none was used. Nothing was sent.", "bad"),
    "signin_locked": ("The site said this account is locked or needs a wait, so the app did not sign in. Nothing was sent.", "bad"),
    "signin_mismatch": ("The sign-in request could not be checked, so no login was sent to it. Retry with the app.", "bad"),
    "signin_reader_not_installed": ("The 1Password command line tool was not found on this computer. Nothing was sent.", "bad"),
    "signin_reader_not_signed_in": ("1Password is not signed in on this computer. Sign in to the 1Password app, then retry.", "bad"),
    "signin_reader_integration_off": ("The 1Password app did not answer. Turn on Integrate with 1Password CLI in its Developer settings, then retry.", "bad"),
    "signin_reader_failed": ("1Password could not be read. Nothing was sent. Retry with the app is safe.", "bad"),
    "signin_on": ("The app may now sign in with logins you saved in 1Password. It asks you on the application page each time.", "ok"),
    "signin_off": ("Sign-in pages are left to you again.", "ok"),
    "notify_sent": ("Test notification sent.", "ok"),
    "notify_failed": ("The test notification could not be sent. Check the mail server settings and try again.", "bad"),
    "notify_off": ("Nothing is set up to send a notification yet.", "bad"),
    "candidate_saved": ("The file is in your candidate folder. It is read the next time the app checks a fit or tailors a resume.", "ok"),
    "candidate_resume_chosen": ("This resume is now the one every tailored version starts from.", "ok"),
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
FIX_BUTTON = "I changed the password, try again"
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
    "ready_for_review": "Ready for you to read",
    "needs_input": "Some answers are still needed",
    "authorized": "You approved this application",
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


def continue_waiting_runs(rt: Runtime, app: dict) -> bool:
    """Saving answers or approving from the page can happen while the task is still parked on an earlier step.
    Ask that task to continue. This only sets a flag; the task does the work on its next pass. Returns whether a
    waiting task was asked to continue."""
    if app.get("status") == "needs_input":
        return False
    asked = False
    inputs = saved_form_inputs(app)
    for run in rt.store.c("runs").find({"params.job_id": app["job_id"], "status": "waiting", "interrupt.type": "needs_input"}):
        rt.store.update_run(run["_id"], requested="resume", resume_payload={"inputs": inputs})
        rt.store.run_event(run["_id"], "Continuing this application")
        asked = True
    if app.get("status") == "authorized":
        for run in rt.store.c("runs").find({"params.job_id": app["job_id"], "status": "waiting", "interrupt.type": "awaiting_authorization"}):
            rt.store.update_run(run["_id"], requested="resume", resume_payload={"authorized": True})
            rt.store.run_event(run["_id"], "Continuing this application")
            asked = True
    return asked


def saved_failure_reason(app: dict | None, action: dict | None = None) -> str:
    """The failure text already saved on the attempt. A replay label is not a reason."""
    app = app or {}
    text = str((action or {}).get("outcome_reason") or "").strip()
    if text and text != REPLAY_LABEL:
        return text
    current = str(app.get("reason") or "").strip()
    if current and current != REPLAY_LABEL:
        return current
    for entry in reversed(app.get("history") or []):
        text = str(entry.get("reason") or "").strip()
        if text and text != REPLAY_LABEL:
            return text
    return ""


def shown_failure_reason(app: dict | None, action: dict | None = None) -> str:
    """Reason to show for a failed attempt. A replay label yields the reason saved on the action."""
    if not app:
        return ""
    current = str(app.get("reason") or "").strip()
    saved = saved_failure_reason(app, action)
    if app.get("status") == "failed" and current == REPLAY_LABEL:
        return saved
    return current or saved


def shown_status(app: dict | None, action: dict | None = None, runs: list[dict] | None = None) -> str:
    """Headline status. A failed attempt is Failed. An approval can stay on file without being that headline.

    A fill that is actually in progress keeps its in-progress state. A submitted application stays submitted.
    An approval whose answers have not failed stays approved."""
    app = app or {}
    status = str(app.get("status") or "")
    if status in ("submitted", "needs_verification", "handoff", "challenge_retry"):
        return status
    if form_filling_now(app, runs or [], action):
        return status or "executing"
    if status == "failed" or _same_package_failed(app, action):
        return "failed"
    return status


def _same_package_failed(app: dict, action: dict | None) -> bool:
    """True when the saved attempt failed for the answers currently on the application."""
    if not action or action.get("status") != "failed":
        return False
    if str(app.get("status") or "") not in ("authorized", "executing"):
        return False
    action_hash = str(action.get("material_hash") or "")
    current = str(app.get("material_hash") or "")
    if action_hash and current and action_hash != current:
        return False
    return True


def shown_status_reason(app: dict | None, action: dict | None, status_shown: str) -> str:
    """The sentence beside the headline. A failed attempt uses the saved reason, never the replay label."""
    raw = str((app or {}).get("reason") or "").strip()
    if status_shown != "failed":
        return "" if raw == REPLAY_LABEL else raw
    basis = dict(app or {})
    basis["status"] = "failed"
    text = shown_failure_reason(basis, action)
    if not text or text == REPLAY_LABEL:
        return ""
    return plain_failure(text)


def event_log(runs: list[dict], limit: int = 40, saved: str = "", saved_at: Any = None) -> list[dict]:
    """Newest saved step lines: time plus the saved message. The replay label is skipped. Secrets the existing filter covers are not shown."""
    rows: list[dict] = []
    for run in runs or []:
        for event in run.get("events") or []:
            text = redact_free(" ".join(str(event.get("message") or "").split()))[:300]
            if event.get("at") and text and text != REPLAY_LABEL:
                rows.append({"at": event["at"], "text": text})
    extra = redact_free(" ".join(str(saved or "").split()))[:300]
    if saved_at and extra and extra != REPLAY_LABEL and all(row["text"] != extra for row in rows):
        rows.append({"at": saved_at, "text": extra})
    rows.sort(key=lambda row: row["at"], reverse=True)
    return rows[:limit]


def application_timeline(app: dict, runs: list[dict], saved_reason: str = "") -> list[dict]:
    """Plain updates from records already saved. No model call."""
    saved = str(saved_reason or "").strip()
    if not saved or saved == REPLAY_LABEL:
        saved = saved_failure_reason(app)
    rows: list[dict] = []
    for entry in app.get("history") or []:
        if entry.get("text"):
            text = str(entry["text"])
            replay = REPLAY_LABEL in text
            step = not replay
            if replay:
                text = "The form was not opened. Showing the result already saved."
        else:
            status = entry.get("status") or ""
            text = _HISTORY_LINE.get(status, status.replace("_", " "))
            reason = (entry.get("reason") or "").strip()
            replay = reason == REPLAY_LABEL
            step = False
            if replay:
                text = "The form was not opened. Showing the result already saved."
                reason = ""
            elif reason == REPLAY_LABEL:
                reason = saved
            if reason and reason != REPLAY_LABEL:
                text = f"{text}. {reason}"
        if entry.get("at") and text:
            rows.append({"at": entry["at"], "text": _for_people(text), "replay": replay, "step": step})
    for run in runs:
        for event in run.get("events") or []:
            text = _run_event_line(run, event)
            replay = REPLAY_LABEL in text
            if replay:
                text = "The form was not opened. Showing the result already saved."
            if event.get("at") and text:
                rows.append({"at": event["at"], "text": _for_people(text), "replay": replay, "step": not replay and text not in _BOOKKEEPING})
    rows.sort(key=lambda row: row["at"], reverse=True)
    kept: list[dict] = []
    for row in rows:
        if kept and kept[-1]["text"] == row["text"]:
            kept[-1]["count"] = kept[-1].get("count", 1) + 1
            if not row.get("replay"):
                kept[-1]["replay"] = False
            if row.get("step"):
                kept[-1]["step"] = True
            continue
        kept.append({**row, "count": 1, "step": row.get("step", False)})
    replayed = any(row.get("replay") for row in kept)
    shown = {row["text"] for row in kept if not row.get("replay")}
    display = [row for row in kept if not (row.get("replay") and row["text"] in shown)]
    if replayed:
        for row in display:
            row["replayed_failure"] = True
    return display[:20]


def _run_event_line(run: dict, event: dict) -> str:
    message = redact_free(event.get("message") or "")
    outcome = (run.get("state") or {}).get("outcome") or {}
    repeated = isinstance(outcome, dict) and outcome.get("adapter_ran") is False
    if message in ("running", "queued", "Picked up", "hidden from the task list", REPLAY_LABEL, "Task started", "Task finished", "Task queued"):
        return ""
    if message == "completed":
        return "The form was not opened. Showing the result already saved." if repeated else ""
    if repeated and message == "Applying on the job site":
        return ""
    if message == "waiting":
        return _WAIT_LINE.get((run.get("interrupt") or {}).get("type"), "Paused")
    if message == "failed":
        return f"Task stopped: {(run.get('error') or 'it stopped')[:180]}"
    if message.startswith("cancelled"):
        return "Task was replaced"
    return message


def authorized_now(app: dict, runs: list[dict]) -> str | None:
    if app.get("status") != "authorized":
        return None
    live = [run for run in runs if run.get("status") in ("queued", "claimed", "running", "waiting", "paused")]
    if any(run["status"] in ("claimed", "running") for run in live):
        return "The app is applying now. This page updates on its own."
    if any(run["status"] == "queued" for run in live):
        return "Approved. Applying starts within a minute. This page updates on its own."
    if any(run.get("requested") == "resume" for run in live):
        return "Approved. The task will continue within a minute. This page updates on its own."
    waiting = next((run for run in live if run["status"] == "waiting"), None)
    kind = (waiting.get("interrupt") or {}).get("type") if waiting else None
    if kind == "needs_input":
        return "Approved, but the task is still waiting on an earlier step, so nothing is being sent yet."
    if kind == "awaiting_authorization":
        return "Approved. The task will apply within a minute. This page updates on its own."
    return "Approved, and nothing is being sent right now. No task is working on this application."


# Bookkeeping written when a task changes state. What to do now skips these when a real step is already saved.
_BOOKKEEPING = {
    "Task finished",
    "Task started",
    "Task queued",
    "Paused",
    "Picked up",
    "hidden from the task list",
    "The form was not opened. Showing the result already saved.",
}


_NOT_OPENED = "The form was not opened. Showing the result already saved."
_PLACE_MISS = re.compile(r"(?P<label>[^:\n]{1,80}?):\s*no (?:list|places) appeared for (?P<value>.+)$", re.I)
_PLACE_NONE = re.compile(r"(?P<label>[^:\n]{1,80}?):\s*a list appeared for (?P<value>.+?) but none matched", re.I)
_BOX_TYPED = re.compile(r"location box typed (?P<value>.+?)\.\s*(?P<tail>No list appeared|A list appeared but none matched)", re.I)
_TYPING = re.compile(r"(?P<label>[^:\n]{1,80}?):\s*typing\s+(?P<value>\S.*?)\s*$", re.I)
_HIDDEN_WORDS = re.compile(r"\b(chromium|browser|visit|replay|packages?|worker|handoff|artifacts?|adapter|payload|scopes?)\b", re.I)


def _for_people(text: str) -> str:
    """Saved notes stay as they were written. One old sentence used a word the page no longer shows."""
    return str(text or "").replace("A new visit starts from the beginning", "Starting again begins at the top of the form")


def situation_line(app: dict, runs: list[dict], saved_reason: str = "") -> str:
    """What to do now is the latest thing already saved. A pass that did not open the form says so."""
    rows = application_timeline(app, runs, saved_reason)
    for row in rows:
        if row.get("replay") or row["text"] == _NOT_OPENED or REPLAY_LABEL in row["text"]:
            return _NOT_OPENED
        if row["text"] in _BOOKKEEPING:
            continue
        return row["text"]
    for row in rows:
        if not row.get("replay") and REPLAY_LABEL not in row["text"]:
            return row["text"]
    return ""


def form_in_progress(app: dict, runs: list[dict]) -> bool:
    """True only while this application is being worked on, which is when the form animation runs."""
    if app.get("status") not in ("authorized", "executing"):
        return False
    return any(run.get("status") in ("claimed", "running") for run in runs)


FILLING_LINE = "The form is being filled in now."
IDLE_LINE = "Nothing is being filled in right now."
OPENING_LINE = "The form opens within a minute."
_FINISHED_ACTION = {"succeeded", "failed", "needs_verification"}
_LOCATION_LABEL = re.compile(r"location\b", re.I)


def form_filling_now(app: dict, runs: list[dict], action: dict | None = None) -> bool:
    """True while the form is actually being filled. A task that only returns a saved result does not count."""
    if not form_in_progress(app, runs):
        return False
    if action and action.get("status") in _FINISHED_ACTION and action.get("material_hash") == app.get("material_hash"):
        return False
    return True


def opening_soon(app: dict, runs: list[dict], action: dict | None = None) -> bool:
    """A queued task will open the form. A queued repeat of a finished result will not."""
    if form_filling_now(app, runs, action):
        return False
    if not any(run.get("status") == "queued" for run in runs):
        return False
    if not action or action.get("status") not in _FINISHED_ACTION:
        return True
    if action.get("material_hash") == app.get("material_hash"):
        return False
    return action.get("status") == "failed"


def network_streaming(app: dict, runs: list[dict], action: dict | None = None) -> bool:
    """True while a real fill is starting or running, so the request list stays open and refreshes."""
    if app.get("status") not in ("authorized", "executing"):
        return False
    return form_filling_now(app, runs, action) or opening_soon(app, runs, action)


def _typed_place(evidence: dict) -> str:
    typed = str(evidence.get("location_typed") or "").strip()
    if typed:
        return typed
    value = str(evidence.get("value") or "").strip()
    if value and len(value) <= 120 and "\n" not in value:
        return value
    return ""


def answer_gap_line(app: dict | None, action: dict | None = None) -> str:
    """Plain sentence when the answer on the page is not the value that was typed last time."""
    if not app:
        return ""
    label, current = "", ""
    for answer in (app.get("review_package") or {}).get("answers") or []:
        name = str(answer.get("label") or "").strip()
        if answer.get("field_id") == "location" or _LOCATION_LABEL.match(name):
            label = name or "Location"
            current = str(answer.get("value") or "").strip()
            break
    if not label or not current:
        return ""
    typed = ""
    for source in (action, app):
        evidence = (source or {}).get("evidence")
        if isinstance(evidence, dict):
            typed = _typed_place(evidence)
            if typed:
                break
    if not typed or typed.casefold() == current.casefold():
        return ""
    return f"Last time, {label} was filled in as {typed}. This page now says {current}. That new answer has not been typed yet."


def answers_differ(app: dict | None, action: dict | None) -> bool:
    """The saved attempt failed for a different set of answers than the ones on the page."""
    if not app or not action or action.get("status") != "failed":
        return False
    return action.get("material_hash") != app.get("material_hash")


def _clean_place(value: str) -> str:
    text = " ".join(str(value or "").split()).strip()
    return text[:-1].strip() if text.endswith(".") else text


def _saved_typed_place(app: dict | None, action: dict | None) -> str:
    for source in (action, app):
        evidence = (source or {}).get("evidence")
        if isinstance(evidence, dict):
            typed = _typed_place(evidence)
            if typed:
                return typed
    return ""


def _place_label(app: dict | None, text: str) -> str:
    for answer in ((app or {}).get("review_package") or {}).get("answers") or []:
        name = str(answer.get("label") or "").strip()
        if answer.get("field_id") == "location" or _LOCATION_LABEL.match(name):  # a question that merely mentions a location is not the place box
            return name or "Location"
    if re.search(r"location", text, re.I):
        return "Location"
    return ""


def _stall_sentence(text: str, app: dict | None = None, action: dict | None = None) -> str:
    """One or two sentences from a saved place note. The field and the typed value come from that note."""
    raw = " ".join(str(text or "").split())
    if not raw or raw == _NOT_OPENED:
        return ""
    # A chosen row is a finished step. The same words otherwise mean the form is still waiting.
    if re.search(r"\bchose\b", raw, re.I):
        return ""
    label, value, kind = "", "", ""
    missed = _PLACE_MISS.search(raw)
    unmatched = _PLACE_NONE.search(raw)
    box = _BOX_TYPED.search(raw)
    if missed:
        label, value, kind = missed.group("label").strip(), _clean_place(missed.group("value")), "none"
    elif unmatched:
        label, value, kind = unmatched.group("label").strip(), _clean_place(unmatched.group("value")), "mismatch"
    elif box:
        value = _clean_place(box.group("value"))
        kind = "mismatch" if "none matched" in box.group("tail").lower() else "none"
    elif re.search(r"no (?:list|places) appeared", raw, re.I):
        kind = "none"
    elif re.search(r"none matched|a list appeared", raw, re.I):
        kind = "mismatch"
    if not kind:
        return ""
    if not value:
        value = _saved_typed_place(app, action)
    if not label:
        label = _place_label(app, raw)
    if kind == "mismatch":
        if label and value:
            return f"{label} was typed as {value}. A list of places appeared, but none matched, so the form is waiting."
        return "A list of places appeared, but none matched, so the form is waiting."
    if label and value:
        return f"{label} was typed as {value}. No list of places appeared, so the form is waiting."
    if value:
        return f"A place was typed as {value}. No list of places appeared, so the form is waiting."
    return "No list of places appeared, so the form is waiting."


def _typed_sentence(text: str) -> str:
    found = _TYPING.search(" ".join(str(text or "").split()))
    if not found:
        return ""
    label = found.group("label").strip()
    value = _clean_place(found.group("value"))
    if not label or not value or _HIDDEN_WORDS.search(label) or _HIDDEN_WORDS.search(value):
        return ""
    return f"{label} was typed as {value}."


def _showable(text: str) -> str:
    cleaned = " ".join(_for_people(text).split())
    if not cleaned or cleaned in _BOOKKEEPING or cleaned == _NOT_OPENED:
        return ""
    if _HIDDEN_WORDS.search(cleaned):
        return ""
    return cleaned


def _field_reason(field: dict | None) -> str:
    if not isinstance(field, dict):
        return ""
    return str(field.get("reason") or field.get("field_reason") or "").strip()


def field_answer_flash_is_stale(line: str) -> bool:
    """The green note says an answer will be typed. It is stale once a saved event says it already was."""
    text = line or ""
    return "was typed as" in text or "No list of places appeared" in text or "so the form is waiting" in text


def tips_hidden_for(line: str) -> bool:
    """A live status replaces the long page tip, so the top of the page is the status."""
    return line in (FILLING_LINE, OPENING_LINE, _NOT_OPENED) or "form is waiting" in (line or "")


STALE_APPROVAL_LINE = "Your earlier approval does not cover this version. Approve it again."


def what_to_do_line(app: dict, runs: list[dict], saved_reason: str = "", action: dict | None = None, field: dict | None = None, stale: bool = False) -> str:
    """One or two plain sentences for what is happening now, from the latest saved event. No model call."""
    reason = _field_reason(field)
    held = bool(reason) or (isinstance(field, dict) and (field.get("label") or field.get("field_label")))
    if reason:
        stalled = _stall_sentence(reason, app, action)
        if stalled:
            return stalled
    if form_filling_now(app, runs, action) and not held:
        return FILLING_LINE
    if stale and not held:
        return STALE_APPROVAL_LINE
    latest = situation_line(app, runs, saved_reason)
    if not held:
        gap = answer_gap_line(app, action)
        if gap:
            return gap
    stalled = _stall_sentence(latest, app, action)
    if stalled:
        return stalled
    if opening_soon(app, runs, action) and not held:
        return OPENING_LINE
    if latest == _NOT_OPENED:
        return _NOT_OPENED
    typed = _typed_sentence(latest)
    if typed:
        return typed
    if held and reason:
        shown_reason = _showable(reason)
        if shown_reason:
            return shown_reason
    shown = _showable(latest)
    if shown:
        return shown
    return IDLE_LINE


NOTHING_NEEDED = "Nothing is needed from you right now."
CANCEL_OFFER = "If you want, stop this attempt. Nothing is sent, and you can try again later."
_RESUME_MISSING = {"base_resume", "approved base resume not selected"}


_FAILED_HEAD = f"{_HISTORY_LINE['failed']}. "


def plain_headline(happening: str, show_status: str) -> str:
    """The bold line of a failed attempt reads as one plain sentence; the raw reason stays in the timeline and the details."""
    line = str(happening or "")
    if show_status != "failed" or not line.startswith(_FAILED_HEAD):
        return line
    return f"{_FAILED_HEAD}{plain_failure(line[len(_FAILED_HEAD) :])}"


def reason_adds(happening: str, reason: str, app: dict | None = None, action: dict | None = None) -> bool:
    """True when the saved reason says something the status line does not already say."""
    text = " ".join(str(reason or "").split())
    line = " ".join(str(happening or "").split())
    if not text or text == line or text in line or text == REPLAY_LABEL:
        return False
    if _stall_sentence(text, app, action) == line:
        return False
    return not _HIDDEN_WORDS.search(text)


def _approve_ask() -> dict:
    return {
        "kind": "approve",
        "title": "Read the resume and the answers, then approve this exact application.",
        "text": "Your approval covers this job, these answers and these exact files only. If anything changes, you will be asked again.",
        "field": None,
    }


def _fix_view(site: str, fix: dict | None) -> dict:
    """What a refused sign-in adds to the status block: the site's own words, the two links, and which saved login to edit.

    The words and the links came from a web page. They are plain text and checked addresses here; the template escapes them."""
    if not fix:
        return {}
    links = []
    if fix.get("reset_url"):
        links.append({"label": f"Reset the password on {site}", "href": fix["reset_url"], "note": "Opens the job site in a new tab. The app never changes a password."})
    else:
        links.append({"label": "The reset link could not be found on the sign-in page. Use the site's own forgot password option.", "href": "", "note": ""})
    if fix.get("signin_url"):
        links.append({"label": "Open the sign-in page for this job", "href": fix["signin_url"], "note": "The same address the app opened."})
    title = f', named "{fix["item_title"]}",' if fix.get("item_title") else ""
    return {"quote": str(fix.get("said") or ""), "links": links, "aside": f"Save the new password in the same 1Password item{title} so the app can read it."}


def needed_now(
    app: dict,
    show_status: str,
    hold: dict | None = None,
    can_fill: bool = False,
    can_retry: bool = False,
    can_clear: bool = False,
    working: bool = False,
    can_cancel: bool = False,
    stale: bool = False,
    can_run: bool = False,
    can_retry_same: bool = False,
    signin_fix: dict | None = None,
) -> dict:
    """The one thing this application asks of the person, with the control it needs.

    `kind` names the control the page renders; an empty kind means nothing is needed. `title` is the ask in one
    sentence, `text` an optional second sentence. `field` is the single missing answer when exactly one is missing."""
    pkg = app.get("review_package") or {}
    status = str(app.get("status") or "")
    none = {"kind": "", "title": NOTHING_NEEDED, "text": "", "field": None}
    if hold and hold.get("purpose") not in ("field", "signin"):
        where = f" on this computer at 127.0.0.1:{hold['cdp_port']}" if hold.get("cdp_port") else ""
        return {
            "kind": "human_check",
            "title": "Human check needed",
            "text": f"The page is being held{where}. If you can open it, finish the check, then press Continue. Continue uses the same approval and does not send the form again. "
            "If you cannot open the page, wait. When the time is up you will be asked to finish on the site yourself.",
            "field": None,
        }
    if hold and hold.get("purpose") == "signin":
        ask = hold.get("signin") or {}
        site = str(ask.get("host") or "The job site")
        fix = signin_fix if signin_fix and signin_fix.get("status") == "rejected" else None
        base = {"kind": "signin", "field": None, "site": site, "label": "Allow 1Password", **_fix_view(site, fix)}
        if hold.get("signin_unchanged"):
            text = f"{site} already refused this password, and the login saved in 1Password is still the same. Sending it again can lock the account. Change the password first."
            if hold.get("signin_unchanged") == "unclear_limit":
                text = f"The last sign-ins on {site} never showed a result, and the login saved in 1Password is still the same. Check the account on the job site first."
            return {**base, "title": "The saved login has not changed.", "text": f"{text} Nothing was sent.", "label": FIX_BUTTON, "confirm_same": True}
        if ask.get("fix") or fix:
            text = "Nothing was sent. Change the password on the job site and save it in the same 1Password item. Then press the button, and 1Password asks you to approve again."
            return {**base, "title": f"{site} did not accept the saved login.", "text": text, "label": FIX_BUTTON if ask.get("fix") else "Allow 1Password"}
        text = (
            "The app can sign in with the login you saved in 1Password. Press Allow 1Password. A 1Password window then asks for Touch ID or your "
            "account password on the computer where this dashboard runs. The app never sees that password."
        )
        if hold.get("signin_missed"):
            text = "1Password was not approved in time. Nothing failed. Press Allow 1Password again when you are ready, and watch for the 1Password window."
        return {**base, "title": f"{site} asks you to sign in.", "text": text}
    if hold and hold.get("purpose") == "field" and hold.get("field_label"):
        label = str(hold["field_label"])
        text = "The same page is still open. Type the answer here and it is entered on that form. The form is not started over."
        ask = hold.get("signin") if isinstance(hold.get("signin"), dict) else None
        if ask:
            text = str(hold.get("field_reason") or "") or "Type the code here and it is entered on the sign-in page."
            return {"kind": "field", "title": label, "text": text, "field": None, "code": True, "offer_code": bool(ask.get("offer_code")), "resend": bool(ask.get("resend"))}
        if re.search(r"\bcode\b", label, re.I) and re.search(r"email", str(hold.get("field_reason") or ""), re.I):
            text = "The job site emailed you a code after the final click. Open that email, type the code here, and the same page finishes sending. Nothing is clicked twice."
        return {"kind": "field", "title": label, "text": text, "field": None}
    if working and can_cancel:
        return {**none, "kind": "cancel", "text": CANCEL_OFFER}
    if working:
        return none
    outstanding = [str(item or "").strip() for item in pkg.get("outstanding") or []]
    if status in ("needs_input", "ready_for_review", "queued", "preparing") and any(item in _RESUME_MISSING for item in outstanding):
        return {
            "kind": "resume",
            "title": "A resume still needs to be selected.",
            "text": "Choose your approved base resume under Your profile. The application is then prepared from it.",
            "field": None,
        }
    if status == "needs_input":
        missing = [a for a in pkg.get("answers") or [] if a.get("needs_input") and not a.get("sensitive") and a.get("fact_ids") != ["artifact"]]
        if len(missing) == 1:
            label = str(missing[0].get("label") or "").strip() or "One answer"
            return {"kind": "answer", "title": f"{label} is still needed.", "text": "Type it here and save. The application is then ready for your approval.", "field": missing[0]}
        return {
            "kind": "answers",
            "title": "Some answers are still needed.",
            "text": "Fill in the answers marked needed, then save. The application is then ready for your approval.",
            "field": None,
        }
    if status == "ready_for_review" or (status == "authorized" and stale):
        return _approve_ask()
    if can_fill:
        text = "The job site is opened with these answers. Nothing else changes."
        return {"kind": "fill", "title": "Fill in the form with the answers on this page.", "text": text, "field": None}
    if can_run:
        return {
            "kind": "run",
            "title": "This application is approved.",
            "text": "The app opens the job site with these answers. Your approval already covers them.",
            "field": None,
        }
    wall = str(app.get("handoff_kind") or "")
    if status == "handoff" and wall in ("login", "code"):
        signin = (app.get("evidence") or {}).get("signin") if isinstance((app.get("evidence") or {}).get("signin"), dict) else {}
        outcome = str(signin.get("outcome") or "")
        stop = stop_for(outcome) if outcome else None
        site = str(signin.get("host") or "The job site")
        # A refusal that is still open is the thing to fix, also when the last attempt ended on a missed approval.
        fix = signin_fix if outcome in ("rejected", "remembered", "locked", "cycle_cap", "not_approved", "declined") else None
        if fix:
            view = _fix_view(site, fix)
            if fix.get("wait_until"):
                how = "as the site said" if fix.get("wait_said") else "because the site did not say how long"
                text = f"Nothing was sent. The app waits until {_local(fix['wait_until'], '%H:%M')}, {how}, before it offers another try. You can unlock the account on the site."
                return {"kind": "", "title": f"{site} says the account is locked or has had too many attempts.", "text": text, "field": None, **view}
            if fix.get("capped"):
                text = f"Nothing was sent. More tries could lock the account. Another try is offered after {_local(fix.get('cap_until'), '%Y-%m-%d %H:%M')}. The links stay here."
                return {"kind": "", "title": f"{site} refused the saved login {fix.get('count')} times, so the app stopped for now.", "text": text, "field": None, **view}
            if can_retry and fix.get("status") == "locked":
                text = "Nothing was sent. The wait has passed. If the account is unlocked, the app can try once more."
                return {"kind": "retry", "title": f"{site} said the account was locked.", "text": text, "field": None, "label": "The account is unlocked, try again", **view}
            if can_retry:
                text = (
                    "Nothing was sent. The app will not send that password again by itself. Change it on the job site, save it in the same 1Password item, then press the button."
                )
                return {"kind": "retry", "title": f"{site} did not accept the saved login.", "text": text, "field": None, "label": FIX_BUTTON, **view}
        if can_retry and stop is not None:
            return {"kind": "retry", "title": stop.title, "text": stop.text, "field": None}
        if can_retry:
            text = "Nothing was sent. Retry with the app opens the site again. If it asks you to sign in and saved logins are on under Settings, this page asks you first."
            return {"kind": "retry", "title": "The job site asked you to sign in.", "text": text, "field": None}
    if status == "handoff" and wall == "checkpoint":
        return {
            "kind": "manual",
            "title": "The job site asked you to verify it is you.",
            "text": "Finish that check yourself. Nothing was sent. Then record the confirmation here.",
            "field": None,
        }
    if status == "handoff" and wall == "blocked":
        return {
            "kind": "manual",
            "title": "The job site blocked this browser.",
            "text": "Nothing was sent. Signing in again will not change that.",
            "field": None,
        }
    if can_retry_same and status == "handoff" and wall not in ("login", "checkpoint", "human_check", "blocked"):
        return {
            "kind": "retry",
            "title": "The app can try again.",
            "text": "Nothing was sent. It opens the job site with these same answers. Your approval already covers them.",
            "field": None,
        }
    if status == "handoff" and wall == "unsupported":
        return {
            "kind": "manual",
            "title": "Finish the remaining step on the job site, then record the confirmation here.",
            "text": "The app filled what it could and stopped before sending.",
            "field": None,
        }
    if can_retry and status in ("failed", "handoff"):
        if app.get("handoff_kind") == "human_check":
            text = "The app can try again. It reads the form afresh and asks for your approval first. If the check comes back, you finish it yourself."
            return {"kind": "retry", "title": "The site asked for a human check last time.", "text": text, "field": None}
        text = (
            "It reads the form again and prepares the answers afresh. If your approval still covers these exact answers and files, "
            "it applies right away; if anything changed, you are asked again before anything is sent."
        )
        return {"kind": "retry", "title": "The app can try again.", "text": text, "field": None}
    if status == "handoff":
        return {
            "kind": "manual",
            "title": "Apply on the job site yourself, then record the confirmation here.",
            "text": "Use the resume below. The app cannot fill in this job site for you.",
            "field": None,
        }
    if status == "needs_verification":
        return {
            "kind": "verify",
            "title": "Check the job site, then record whether it went through.",
            "text": "The app could not find a confirmation. It will not try again on its own.",
            "field": None,
        }
    if status == "submitted":
        return {**none, "text": "Applied and recorded. Next: short notes to recruiters at this employer."}
    if status == "challenge_retry":
        return {**none, "text": "A check appeared. The app tries once more later, then tells you if it still needs you."}
    if show_status == "failed" and can_clear:
        return {**none, "text": "Nothing was sent. To try again, use Start over."}
    return none


def network_public(rows: list[dict] | None) -> list[dict]:
    """Shape one job's saved requests for the page. No model call. Times are local. Secrets are already removed."""
    public: list[dict] = []
    for row in rows or []:
        status = row.get("status")
        public.append(
            {
                "id": str(row.get("id") or ""),
                "at": _local(row.get("at"), "%Y-%m-%d %H:%M:%S"),
                "method": str(row.get("method") or ""),
                "url": str(row.get("url") or ""),
                "type": str(row.get("type") or ""),
                "status": status if isinstance(status, int) else None,
                "timing": str(row.get("timing") or ""),
                "body": str(row.get("body") or ""),
            }
        )
    return public


def hold_key(hold: dict | None) -> str:
    """What the page is waiting on right now, as a short key. The page reloads its one control when it changes."""
    if not hold:
        return ""
    ask = hold.get("signin") if isinstance(hold.get("signin"), dict) else {}
    return f"{hold.get('purpose') or 'check'}:{ask.get('ask_id') or hold.get('_id') or ''}:{int(hold.get('signin_missed') or 0)}:{hold.get('signin_unchanged') or ''}"


def activity_payload(
    app: dict,
    runs: list[dict],
    saved_reason: str = "",
    field: dict | None = None,
    network: list[dict] | None = None,
    action: dict | None = None,
    stale: bool = False,
    hold: dict | None = None,
) -> dict:
    """Saved steps plus this job's network rows, for the page to refresh. No model call."""
    rows = application_timeline(app, runs, saved_reason)
    lines = [{"at": _local(row["at"], "%Y-%m-%d %H:%M"), "text": row["text"], "count": row["count"]} for row in rows]
    live = app.get("status") in ("authorized", "executing") or any(run.get("status") in ("queued", "claimed", "running", "waiting", "paused") for run in runs)
    held = bool(field and (field.get("label") or field.get("reason")))
    filling = form_filling_now(app, runs, action) and not held
    line = what_to_do_line(app, runs, saved_reason, action, field, stale=stale)
    status = shown_status(app, action, runs)
    payload = {
        "now": "",
        "state": line,
        "status": status,
        "status_label": status_label(status),
        "tone": status_tone(status),
        "live": live,
        "working": filling,
        "network_open": network_streaming(app, runs, action),
        "lines": lines,
        "network": network_public(network),
        "hold": hold_key(hold),
    }
    if field:
        raw_reason = str(field.get("reason") or "")
        payload["field"] = {"label": str(field.get("label") or ""), "reason": _stall_sentence(raw_reason, app, action) or raw_reason}
    return payload


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
    "event_log",
    "needed_now",
    "STALE_APPROVAL_LINE",
    "network_public",
    "reason_adds",
    "NOTHING_NEEDED",
    "apply_profile_toml",
    "authorized_now",
    "saved_failure_reason",
    "shown_failure_reason",
    "shown_status",
    "shown_status_reason",
    "situation_line",
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
