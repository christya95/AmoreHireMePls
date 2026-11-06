"""Dashboard presentation settings (theme, accent, display name, avatar) and small view helpers.

These are per-user presentation preferences, not candidate facts: they live in the `controls`
collection under one key and never feed resumes, answers or messages. The avatar file lives under the
private directory and is only ever served through an authenticated route.
"""

from __future__ import annotations

import re
import struct
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from amorehiremepls.core.db import Store
from amorehiremepls.core.util import sha256_bytes, to_local, utcnow

UI_SETTINGS_KEY = "ui_settings"
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# Accent presets (name -> hex). "sage" matches the reference design; everything else derives through color-mix().
ACCENT_PRESETS: dict[str, str] = {
    "sage": "#6f8f73",
    "blue": "#3b6fd6",
    "violet": "#7c5cd6",
    "teal": "#1f8a8a",
    "amber": "#c98a1b",
    "rose": "#d6517a",
}
DEFAULT_ACCENT = ACCENT_PRESETS["sage"]
# Background tint presets. "accent" means "derive the page background from the accent" (the original behaviour);
# the others are fixed hues mixed into a neutral base, so the accent can stay green while the page does not.
TINT_PRESETS: dict[str, str | None] = {"accent": None, "neutral": "#8c8f90", "warm": "#c9a06a", "cool": "#6a8fc9"}
TINT_LABELS = {"accent": "Match accent", "neutral": "Neutral", "warm": "Warm", "cool": "Cool"}
AVATAR_STYLES = ("soft", "solid", "ring", "gradient", "mark")
AVATAR_MAX_BYTES = 1_048_576
AVATAR_MAX_DIMENSION = 4096
AVATAR_MIN_DIMENSION = 16
AVATAR_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
Theme = Literal["system", "light", "dark"]


class AvatarInfo(BaseModel):
    kind: Literal["initials", "upload"] = "initials"
    style: str = "soft"
    content_type: str | None = None
    ext: str | None = None
    sha256: str | None = None
    width: int | None = None
    height: int | None = None
    updated_at: datetime | None = None


class UISettings(BaseModel):
    theme: Theme = "system"
    accent: str = DEFAULT_ACCENT
    bg_tint: str = "accent"  # "accent" | "neutral" | "warm" | "cool" | "#rrggbb"
    display_name: str | None = None
    avatar: AvatarInfo = Field(default_factory=AvatarInfo)
    checklist_dismissed: bool = False
    collapsed_intros: list[str] = Field(default_factory=list)  # page keys whose intro strip is folded
    company_logos: bool = True  # show each employer's logo (deterministic fetch, no model use); off shows initials
    updated_at: datetime | None = None

    @property
    def accent_name(self) -> str | None:
        for name, hexv in ACCENT_PRESETS.items():
            if hexv.lower() == self.accent.lower():
                return name
        return None

    @property
    def tint_name(self) -> str | None:
        return self.bg_tint if self.bg_tint in TINT_PRESETS else None

    @property
    def tint_css(self) -> str:
        """Value for the --tint custom property: a hex colour, or "accent" meaning "follow --accent"."""
        if self.bg_tint == "accent":
            return "accent"
        return TINT_PRESETS.get(self.bg_tint) or self.bg_tint


def normalize_accent(value: str | None) -> str | None:
    """Preset name or #rrggbb -> canonical lowercase hex; None when invalid."""
    v = (value or "").strip().lower()
    if v in ACCENT_PRESETS:
        return ACCENT_PRESETS[v]
    if HEX_RE.match(v):
        return v
    return None


def normalize_tint(value: str | None) -> str | None:
    """Tint preset name or #rrggbb -> canonical stored value; None when invalid."""
    v = (value or "").strip().lower()
    if v in TINT_PRESETS:
        return v
    if HEX_RE.match(v):
        return v
    return None


def tint_css_from_cookie(value: str | None) -> str:
    v = normalize_tint(value) or "accent"
    return "accent" if v == "accent" else (TINT_PRESETS.get(v) or v)


class UISettingsStore:
    def __init__(self, store: Store) -> None:
        self.store = store

    def load(self) -> UISettings:
        doc = self.store.get_control(UI_SETTINGS_KEY)
        if not doc:
            return UISettings()
        try:
            return UISettings.model_validate(doc)
        except Exception:  # a hand-edited document never breaks the dashboard
            return UISettings()

    def save(self, ui: UISettings) -> UISettings:
        ui.updated_at = utcnow()
        self.store.set_control(UI_SETTINGS_KEY, ui.model_dump(mode="python"))
        return ui


# -- avatar files -------------------------------------------------------------------------------------


def sniff_image(data: bytes) -> tuple[str, str, int, int] | None:
    """Return (content_type, ext, width, height) from the file header, or None when it is not a PNG/JPEG/WebP."""
    if len(data) < 30:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        w, h = struct.unpack(">II", data[16:24])
        return "image/png", "png", w, h
    if data[:3] == b"\xff\xd8\xff":
        i = 2
        n = len(data)
        while i + 9 < n:
            if data[i] != 0xFF:
                return None
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seg_len = struct.unpack(">H", data[i + 2 : i + 4])[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", data[i + 5 : i + 9])
                return "image/jpeg", "jpg", w, h
            i += 2 + seg_len
        return None
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        chunk = data[12:16]
        if chunk == b"VP8 " and len(data) >= 30:
            w = struct.unpack("<H", data[26:28])[0] & 0x3FFF
            h = struct.unpack("<H", data[28:30])[0] & 0x3FFF
            return "image/webp", "webp", w, h
        if chunk == b"VP8L" and len(data) >= 25:
            bits = int.from_bytes(data[21:25], "little")
            return "image/webp", "webp", (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if chunk == b"VP8X" and len(data) >= 30:
            return "image/webp", "webp", 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
    return None


class AvatarError(ValueError):
    pass


def validate_avatar(data: bytes, declared_type: str | None) -> tuple[str, str, int, int]:
    """Size, declared type and sniffed header must all agree; anything else is refused."""
    if not data:
        raise AvatarError("empty file")
    if len(data) > AVATAR_MAX_BYTES:
        raise AvatarError(f"file is larger than {AVATAR_MAX_BYTES // 1024} KB")
    declared = (declared_type or "").split(";")[0].strip().lower()
    if declared == "image/jpg":
        declared = "image/jpeg"
    if declared not in AVATAR_TYPES:
        raise AvatarError("only PNG, JPEG or WebP images are accepted")
    sniffed = sniff_image(data)
    if sniffed is None or sniffed[0] != declared:
        raise AvatarError("file content does not match its image type")
    ctype, ext, w, h = sniffed
    if not (AVATAR_MIN_DIMENSION <= w <= AVATAR_MAX_DIMENSION and AVATAR_MIN_DIMENSION <= h <= AVATAR_MAX_DIMENSION):
        raise AvatarError(f"image must be between {AVATAR_MIN_DIMENSION} and {AVATAR_MAX_DIMENSION} pixels per side")
    return ctype, ext, w, h


def avatar_path(private_dir: Path, ext: str) -> Path:
    p = private_dir / "avatar" / f"avatar.{ext}"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def store_avatar(private_dir: Path, data: bytes, declared_type: str | None) -> AvatarInfo:
    ctype, ext, w, h = validate_avatar(data, declared_type)
    remove_avatar_files(private_dir)
    avatar_path(private_dir, ext).write_bytes(data)
    return AvatarInfo(kind="upload", content_type=ctype, ext=ext, sha256=sha256_bytes(data), width=w, height=h, updated_at=utcnow())


def remove_avatar_files(private_dir: Path) -> None:
    for ext in set(AVATAR_TYPES.values()):
        p = private_dir / "avatar" / f"avatar.{ext}"
        if p.exists():
            p.unlink()


# -- greeting, initials ---------------------------------------------------------------------------------


def resolve_display_name(ui: UISettings, profile: Any, fallback: str = "") -> str:
    """Saved dashboard name, else the profile's preferred/first name, else the caller's fallback (empty by default:
    the greeting then renders without a name rather than with somebody else's)."""
    if ui.display_name:
        return ui.display_name
    for key in ("preferred_name", "legal_first_name"):
        v = profile.fact_value(key) if profile is not None else None
        if v:
            return v.split()[0]
    v = profile.fact_value("display_name") if profile is not None else None
    if v:
        return v.split()[0]
    return fallback


def initials(name: str) -> str:
    parts = [p for p in re.split(r"[\s._-]+", name.strip()) if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def greeting(now: datetime | None = None) -> str:
    hour = to_local(now or utcnow()).hour
    if 5 <= hour < 12:
        return "Good morning"
    if 12 <= hour < 17:
        return "Good afternoon"
    if 17 <= hour < 22:
        return "Good evening"
    return "Working late"


# -- status tones -----------------------------------------------------------------------------------------

_TONES: dict[str, str] = {
    "ok": "submitted applied applied_historical synced succeeded active qualified ok completed done resolved present allowed",
    "warn": "needs_review needs_input ready_for_review authorized handoff challenge_retry waiting paused sync_pending local_only committed_local prepared researched queued claimed pending unresolved",  # noqa: E501
    "bad": "needs_verification failed conflict blocked error cancelled canceled rejected expired revoked missing down skipped",
    "info": "new running executing draft imported reserved",
}
STATUS_TONE = {s: tone for tone, words in _TONES.items() for s in words.split()}


def status_tone(status: Any) -> str:
    return STATUS_TONE.get(str(status or "").lower(), "neutral")


def humanize(value: Any) -> str:
    return str(value or "").replace("_", " ")


# -- plain-language labels ----------------------------------------------------------------------------------
# Every word the dashboard shows for an internal code lives here (see docs/ui-copy.md for the plain -> technical map).
# The raw code stays visible in a muted chip next to the label, and the Help page lists both.

STATUS_LABELS: dict[str, str] = {
    # applications
    "queued": "Queued",
    "preparing": "Preparing",
    "ready_for_review": "Waiting for your approval",
    "needs_input": "Needs your answers",
    "authorized": "Approved",
    "challenge_retry": "Trying once more later",
    "executing": "Applying now",
    "handoff": "Finish on the site yourself",
    "needs_verification": "Result unclear, check it",
    "submitted": "Applied",
    "removed": "Removed",
    "withdrawn": "Removed",
    "failed": "Failed",
    "duplicate": "Already applied",
    "blocked": "Blocked by your rules",
    "denied": "Not approved",
    # jobs
    "new": "New",
    "needs_review": "Needs your decision",
    "qualified": "Good fit",
    "applied": "Applied",
    "applied_historical": "Applied (from your tracker)",
    "rejected": "Rejected",
    "excluded": "Skipped by your rules",
    "closed": "Closed",
    # background tasks (runs)
    "claimed": "Starting",
    "running": "Running",
    "waiting": "Waiting for you",
    "paused": "Paused",
    "cancelled": "Cancelled",
    "succeeded": "Finished",
    "completed": "Finished",
    # why a task is waiting (interrupt types)
    "awaiting_authorization": "Waiting for your approval",
    "awaiting_input": "Needs your answers",
    "emergency_stop": "Stopped by the emergency stop",
    # saving to the GitHub tracker (sync outbox)
    "sync_pending": "Waiting to save to GitHub",
    "committing": "Saving to GitHub now",
    "committed_local": "Saved here, not on GitHub yet",
    "local_only": "Saved here only",
    "synced": "Saved to GitHub",
    "conflict": "GitHub save needs you",
    "not_applicable": "No GitHub save needed",
    # approvals (authorization scopes)
    "active": "Active",
    "revoked": "Withdrawn",
    "expired": "Expired",
    "exhausted": "Used up",
    "single": "One job",
    "batch": "Several jobs",
    "application_submit": "Applying to a job",
    "owner_notification": "Alert to you",
    "outreach_send": "Sending a message",
    # recruiter messages (outreach)
    "draft": "Draft",
    "sent": "Sent",
    "recorded": "Recorded as sent",
    "connection_request": "Connection note",
    "outreach_first_contact": "First message",
    "follow_up": "Follow up",
    "message": "Message",
    "email": "Email",
    "reply": "Reply",
    "linkedin": "LinkedIn",
    # people never to contact (recipient exclusions)
    "unresolved": "Not yet identified",
    "resolved": "Identified",
    # how a resume was written
    "deterministic": "Rules only, no AI",
    "model": "AI, checked against your facts",
    # where a record came from
    "local": "This app",
    "tracker_import": "Your GitHub tracker",
    "manual": "Added by you",
    "url_import": "Pasted link",
    "greenhouse": "Greenhouse board",
    "lever": "Lever board",
    # kinds of background task
    "application": "Application",
    "discovery": "Job search",
    "discovery_request": "Job search (asked for)",
    # activity log events
    "action_denied": "Sending blocked",
    "action_finished": "Sending finished",
    "action_late_outcome": "Late result recorded",
    "action_lease_expired": "Sending timed out, result unclear",
    "action_prepared": "Prepared to send",
    "action_reconciled": "Result confirmed by you",
    "application_handoff": "Handed to you to finish on the site",
    "application_retry": "Retried with the app",
    "action_handoff": "Stopped for a person before sending",
    "captcha_autosolve_changed": "Captcha switch changed",
    "field_answer_submitted": "Answer saved for the open form",
    "authorization_granted": "Approval given",
    "authorization_revoked": "Approval withdrawn",
    "avatar_uploaded": "Avatar changed",
    "batch_runs_created": "Several jobs queued",
    "document_hash_mismatch": "File changed since review, not served",
    "document_path_refused": "File outside the allowed folder, not served",
    "documents_regenerated": "Resume rebuilt",
    "inputs_supplied": "Answers saved",
    "outreach_manual_send_recorded": "Message recorded as sent",
    "profile_saved": "Profile saved",
    "rank_failed": "Fit score failed",
    "recipient_history_reconciled": "Contact history checked",
    "resume_tailored": "Resume tailored",
    "run_pause_requested": "Task pause requested",
    "run_resume_requested": "Task resume requested",
    "run_cancel_requested": "Task cancel requested",
    "run_cancelled": "Task cancelled before it started",
    "run_hidden": "Task hidden from the list",
    "runs_hidden": "Finished tasks hidden from the list",
    "tracker_synced": "Saved to GitHub",
    # salary and preference option codes
    "midpoint": "Middle of the range",
    "upper_half": "Upper half of the range",
    "minimum": "Bottom of the range",
    "leave_blank": "Leave it blank",
    "ignore": "Treat like any other employer",
}

# Submission screenshots the app keeps as evidence: label -> (short title, one-line meaning).
SCREENSHOT_LABELS: dict[str, tuple[str, str]] = {
    "summary": ("Filled review page", "The form exactly as it was filled in, captured right before the final click that sends it."),
    "receipt": ("Confirmation page", "What the employer site showed after applying. The confirmation text was checked against it."),
    "ambiguous": ("Unclear result", "What the site showed after the click when no confirmation could be found. Check it yourself and record the result."),
    "challenge": ("Human check", "The site asked for a check that needs a person (a captcha). Nothing was sent at this point."),
    "validation": ("Form complaint", "The site refused the form and pointed at a field. Nothing was sent."),
    "handoff": ("Needs you", "A sign in or similar wall. Nothing was sent."),
    "receipt_mismatch": ("Confirmation for another job?", "A confirmation page appeared but it did not mention this job. Check it yourself."),
    "failed": ("Stopped early", "The page as it looked when the app gave up before the final click."),
}

# The one obvious verb for an application in a given state.
NEXT_ACTIONS: dict[str, str] = {
    "ready_for_review": "Read and approve",
    "needs_input": "Answer questions",
    "handoff": "Finish on the site",
    "needs_verification": "Check result",
    "authorized": "Open package",
    "challenge_retry": "Nothing to do yet",
    "submitted": "Contact recruiters",
    "failed": "See what failed",
}

# The eight steps of one job: plain name -> (one-line tooltip, technical name).
STEP_HELP: dict[str, tuple[str, str]] = {
    "Import": ("The job ad is saved here from a link you pasted or a job board the app watches. Nothing is sent.", "import"),
    "Check fit": ("The app checks the job against your rules and explains how well you match. Nothing is sent.", "qualify"),
    "Tailor resume": ("The app writes a version of your resume for this job, using only facts from your approved resume.", "tailor"),
    "Read application": ("You read the resume and the form answers exactly as they would be sent.", "review"),
    "Approve": ("You give the go ahead for this exact application. Change anything and you will be asked again.", "authorize"),
    "Apply": ("The app applies on the job site, or hands it to you when the site needs a person.", "submit"),
    "Save to tracker": ("The application is written to your private GitHub tracker.", "record"),
    "Contact recruiters": ("You add recruiters at that employer and the app drafts short notes. You send them yourself.", "outreach"),
}

# "?" hints and the Help glossary: key -> (plain title, plain text, technical term).
GLOSSARY: dict[str, tuple[str, str, str]] = {
    "authorization": (
        "Approval",
        "Your go ahead for one exact application: this job, these answers, this resume file. Without it nothing is sent from this computer.",
        "authorization scope",
    ),
    "material_hash": (
        "Fingerprint",
        "A short code made from everything that would be sent: the answers, the resume file, the job and the apply link. If any of it changes, the code changes and the old approval no longer counts.",
        "material hash",
    ),
    "batch": (
        "Prepare several at once",
        "Prepares packages for your best matching jobs, up to the number you choose. Nothing is sent. Each package still waits for your approval.",
        "bounded batch",
    ),
    "batch_scope": (
        "Approval for several jobs",
        "One approval that lets the app apply to up to N ready packages that match your filters, without asking each time. It stops at the first unclear result or failure.",
        "batch scope",
    ),
    "needs_verification": (
        "Result unclear",
        "The app tried to apply but could not find a confirmation. Check the job site yourself and record what you saw. The app will not try again on its own.",
        "needs_verification",
    ),
    "handoff": (
        "Finish on the site yourself",
        "This job site needs a person (for example LinkedIn Easy Apply, Workday, a login or a human check such as a captcha). The package is ready for you. Apply yourself, then record the confirmation text here.",
        "handoff",
    ),
    "human_check": (
        "Human check",
        "A captcha or similar check. The app can hold the page so you can finish it and press Continue. If you cannot see the page, it asks you to finish on the site after the wait. Under Settings you can let the app try simple picture checks itself; some sites forbid that.",
        "captcha",
    ),
    "emergency_stop": (
        "Emergency stop",
        "Stops all sending and applying immediately. Anything already in progress ends with a recorded result or 'result unclear'. Nothing is deleted. Clear it when you are ready.",
        "emergency stop",
    ),
    "sync_states": (
        "Saving to GitHub",
        "'Waiting to save to GitHub' means the application is recorded here but not in your GitHub tracker yet. 'Saved to GitHub' means it is there. A GitHub problem never changes whether you applied.",
        "sync outbox states",
    ),
    "outreach_scope": (
        "Sending messages",
        "Recruiter notes are drafts until you act. Each send is a separate go ahead. LinkedIn notes you send yourself and record afterwards.",
        "outreach scope",
    ),
    "fit_rank": (
        "Fit score",
        "1 is a strong match, 5 a long shot. Worked out from the job ad and your profile. It orders the list and is never shown to employers.",
        "fit rank",
    ),
    "grounding": (
        "Facts check",
        "Every claim comes from your approved facts. Each line of a tailored resume must trace back to your approved base resume. Anything else is thrown away and the rules only version is used.",
        "grounding",
    ),
    "evidence": (
        "Proof of applying",
        "When the app applies for you it keeps two screenshots: the filled form right before the final click, and the confirmation page afterwards. If the result was unclear it keeps what the site showed instead.",
        "submission evidence",
    ),
    "regenerate": (
        "Rebuild the resume",
        "A new resume file is made. The old approval no longer applies. Nothing is sent.",
        "regenerate documents",
    ),
    "start_over": (
        "Start over",
        "Clears this unsent attempt, its approval, and the resume files made for it. The job stays, so you can prepare it again. Nothing is sent, and the tracker history is left as it is.",
        "clear unsent application",
    ),
    "remove_application": (
        "Remove",
        "Takes this prepared application off the list, with its approval and the resume files made for it. Nothing is sent, and the job stays, so you can prepare it again. The tracker history is left as it is.",
        "clear unsent application",
    ),
    "cancel_attempt": (
        "Cancel this attempt",
        "Stops the app before the final click and closes the page. Nothing is sent, and you can try again later. Once the final click has happened the attempt cannot be stopped; the app checks the result instead.",
        "cancel live run",
    ),
    "task": (
        "Background task",
        "Work the app does on its own, one task per application attempt. Still waiting is at the top. "
        "Finished and cancelled tasks sit below and can be hidden. Clear background tasks cancels ones that have not started "
        "and hides the finished ones. Nothing that was already sending is cancelled, and the job and the application stay.",
        "run",
    ),
    "snapshot": (
        "Copy of the job ad",
        "The text of the job ad as it was when the app saved it, so later changes on the site do not change what you reviewed.",
        "posting snapshot",
    ),
    "tracker": (
        "Your GitHub tracker",
        "Your private list of every application, kept in a GitHub repository. The app reads your past applications from it and saves new ones to it.",
        "tracker / sync",
    ),
}

# One or two plain sentences per page: what it is for, what to do here.
PAGE_INTROS: dict[str, tuple[str, str]] = {
    "index.html": (
        "Your home page. The tiles show where things stand and the list shows only what needs you.",
        "Work through 'Needs your attention' from the top. Then add or search for new jobs.",
    ),
    "jobs.html": (
        "Every job the app knows about, with how well it fits your rules.",
        "Paste a link to add a job. Open a job to tailor a resume and prepare an application.",
    ),
    "job.html": (
        "One job from start to finish. The block at the top says what is happening and what, if anything, it needs from you.",
        "The steps show where this job is. Everything else on this page is information you can read.",
    ),
    "applications.html": (
        "Applications the app has prepared: the resume, the answers and where they would go.",
        "Open one to answer questions, approve it, finish it on the site or check an unclear result.",
    ),
    "application.html": (
        "One application. The block at the top says what is happening and asks for the one thing it needs, if anything.",
        "The sections below are closed until they matter. Open any of them to read what would be sent and what happened.",
    ),
    "runs.html": (
        "The app's background tasks, one per application attempt. A task pauses whenever it needs you.",
        "You rarely need this page. Open a waiting task to jump to its package.",
    ),
    "run.html": ("One background task with its steps and cost.", "If it is waiting, the button opens the package it is waiting on."),
    "documents.html": (
        "Resumes the app tailored from your approved base resume. The base resume itself is never changed.",
        "Open a PDF to read exactly what would be uploaded, or download the DOCX.",
    ),
    "outreach.html": (
        "Short notes to recruiters at employers where you have applied.",
        "Add a person by LinkedIn link, let the app draft the note, send it yourself, then record that you sent it.",
    ),
    "history.html": (
        "Your past applications, loaded from your private GitHub tracker.",
        "Load again after you edit the tracker by hand. Loading twice is safe.",
    ),
    "sync.html": ("How new applications get saved to your GitHub tracker.", "Nothing to do unless an item says it needs you."),
    "usage.html": ("How much the AI has been used and what it cost, step by step.", "Add prices for your AI model so costs show as numbers instead of 'unknown'."),
    "profile.html": (
        "The facts about you that every resume line and form answer comes from.",
        "Keep it accurate. If you do not know a value, leave it blank and the app will ask you instead of guessing.",
    ),
    "authorizations.html": ("Every approval you have given and what it covered.", "Withdraw anything you no longer want. Approve several jobs at once only when you are sure."),
    "settings.html": ("How the dashboard looks and greets you.", "Nothing here changes resumes, answers or messages."),
    "help.html": ("How the whole thing works, in two minutes.", "Come back here whenever a word is unclear."),
}


def status_label(status: Any) -> str:
    s = str(status or "")
    return STATUS_LABELS.get(s.lower(), humanize(s))


def next_action_label(status: Any) -> str:
    return NEXT_ACTIONS.get(str(status or "").lower(), "Open")


# -- dependency-free SVG chart geometry ------------------------------------------------------------------


def _smooth_path(pts: list[tuple[float, float]]) -> str:
    """Catmull-Rom spline converted to cubic Beziers; monotone enough for small series."""
    if len(pts) < 2:
        return ""
    d = [f"M{pts[0][0]:.1f},{pts[0][1]:.1f}"]
    for i in range(len(pts) - 1):
        p0 = pts[i - 1] if i > 0 else pts[i]
        p1, p2 = pts[i], pts[i + 1]
        p3 = pts[i + 2] if i + 2 < len(pts) else p2
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
        d.append(f"C{c1[0]:.1f},{c1[1]:.1f} {c2[0]:.1f},{c2[1]:.1f} {p2[0]:.1f},{p2[1]:.1f}")
    return " ".join(d)


def line_chart(series: list[tuple[str, int | float]], width: int = 640, height: int = 220, pad: tuple[int, int, int, int] = (16, 12, 30, 36)) -> dict[str, Any]:
    """Geometry for a smooth area chart. Returns paths, point coordinates, y ticks and x labels."""
    top, right, bottom, left = pad
    n = len(series)
    values = [float(v) for _, v in series]
    vmax = max(values) if values else 0.0
    ymax = max(1.0, vmax)
    # a round-ish axis maximum (1, 2, 5 ladder)
    for step in (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000):
        if ymax <= step * 4:
            ymax = step * 4
            tick_step = step
            break
    else:
        tick_step = max(1, int(ymax // 4))
        ymax = tick_step * 4
    inner_w = max(1, width - left - right)
    inner_h = max(1, height - top - bottom)

    def x(i: int) -> float:
        return left + (inner_w * i / (n - 1) if n > 1 else inner_w / 2)

    def y(v: float) -> float:
        return top + inner_h - inner_h * (v / ymax)

    pts = [(x(i), y(v)) for i, v in enumerate(values)]
    line = _smooth_path(pts)
    area = f"{line} L{pts[-1][0]:.1f},{top + inner_h:.1f} L{pts[0][0]:.1f},{top + inner_h:.1f} Z" if line else ""
    ticks = [{"v": int(tick_step * k), "y": round(y(tick_step * k), 1)} for k in range(0, 5)]
    return {
        "width": width,
        "height": height,
        "line": line,
        "area": area,
        "points": [{"x": round(px, 1), "y": round(py, 1), "label": series[i][0], "value": series[i][1]} for i, (px, py) in enumerate(pts)],
        "ticks": ticks,
        "x0": left,
        "x1": width - right,
        "baseline": round(top + inner_h, 1),
        "max": vmax,
        "total": sum(values),
    }


# Rank 1 is a close match and 5 is a poor one (assess_fit). The anchors sit inside the colour bands
# so the ring agrees with the rank unless grounded points or blockers nudge it.
_FIT_BASE = {1: 94, 2: 78, 3: 55, 4: 32, 5: 14}
_FIT_RING_C = round(2 * 3.141592653589793 * 52, 2)  # r=52 circle used by the job-page ring


def fit_score(fit: dict | None) -> dict[str, Any] | None:
    """0–100 view of a stored fit assessment. Reads the existing rank and point lists only; no model call.

    score = clamp(base[rank] + nudge − blocker penalty, 0, 100)
    base is 94 / 78 / 55 / 32 / 14 for ranks 1–5.
    nudge is round((share of grounded matches − 0.5) × 16), so at most 8 points either way, and 0 when
    there is no breakdown. Each mandatory blocker subtracts 6, capped at 12.
    Bands: 0–39 weak (red), 40–69 partial (yellow), 70–89 strong (green), 90–100 excellent (blue).
    """
    if not isinstance(fit, dict):
        return None
    rank = fit.get("rank")
    if isinstance(rank, bool) or not isinstance(rank, int):
        return None
    rank = min(5, max(1, rank))
    matches = _fit_lines(fit.get("fit_points"))
    gaps = _fit_lines(fit.get("genuine_gaps"))
    blockers = _fit_lines(fit.get("mandatory_qualification_blockers"))
    unknown = _fit_lines(fit.get("missing_evidence"))
    parts = len(matches) + len(gaps) + len(blockers) + len(unknown)
    nudge = round((len(matches) / parts - 0.5) * 16) if parts else 0
    score = max(0, min(100, _FIT_BASE[rank] + nudge - min(12, 6 * len(blockers))))
    if score >= 90:
        band, label, tone = "excellent", "Excellent match", "info"
    elif score >= 70:
        band, label, tone = "strong", "Strong match", "ok"
    elif score >= 40:
        band, label, tone = "partial", "Partial match", "warn"
    else:
        band, label, tone = "weak", "Weak match", "bad"
    return {
        "score": score,
        "rank": rank,
        "band": band,
        "label": label,
        "tone": tone,
        "matches": matches,
        "gaps": gaps,
        "blockers": blockers,
        "unknown": unknown,
        "matches_n": len(matches),
        "gaps_n": len(gaps) + len(blockers),
        "unknown_n": len(unknown),
        "circ": _FIT_RING_C,
        "offset": round(_FIT_RING_C * (1 - score / 100), 2),
    }


# Internal evidence ids the fit model is asked to cite. They stay in the stored JSON and must not be shown.
_EVIDENCE_ID = r"(?:(?:emp|fact|exp|proj|skill|pskill|cert|b)_[A-Za-z0-9]+|resume_summary)"
_EVIDENCE_ID_RE = re.compile(rf"\b{_EVIDENCE_ID}\b")
_EVIDENCE_PAREN_RE = re.compile(rf"\s*\((?:\s*{_EVIDENCE_ID}\s*,)*\s*{_EVIDENCE_ID}\s*\)")
_EVIDENCE_PREFIX_RE = re.compile(rf"^(?:{_EVIDENCE_ID}\s*:\s*)+")


_NEEDED_SENTENCE = {
    "base_resume": "A resume still needs to be selected.",
    "approved base resume not selected": "A resume still needs to be selected.",
    "legal_first_name": "A legal first name is still needed.",
    "legal_last_name": "A legal last name is still needed.",
    "email": "Email is still needed.",
    "phone": "A phone number is still needed.",
    "city": "A city is still needed.",
    "work_authorization": "Work authorization is still needed.",
    "tailored resume failed grounding": "The prepared resume could not be checked against your facts.",
}
_SNAKE_KEY = re.compile(r"^[a-z][a-z0-9_]*$")
_SNAKE_TOKEN = re.compile(r"\b[a-z]+_[a-z0-9_]+\b")
_NEEDED_GENERIC = "Something else is still needed."


def needed_lines(items: Any) -> list[str]:
    """Plain sentences for missing answers and blockers. Skill gaps stay in their own list. Raw keys are never shown."""
    lines: list[str] = []
    seen: set[str] = set()
    for raw in items or []:
        text = " ".join(str(raw or "").split())
        if not text:
            continue
        if text in _NEEDED_SENTENCE:
            line = _NEEDED_SENTENCE[text]
        elif text.lower().startswith("answer needed:"):
            label = text.split(":", 1)[1].strip()
            if not label or _SNAKE_KEY.fullmatch(label) or "_" in label:
                line = _NEEDED_GENERIC
            elif label.endswith("?"):
                line = f"An answer is still needed: {label}"
            else:
                line = f"{label} is still needed."
        elif _SNAKE_KEY.fullmatch(text) or _SNAKE_TOKEN.search(text):
            line = _NEEDED_GENERIC
        else:
            line = text
        if line not in seen:
            seen.add(line)
            lines.append(line)
    return lines


def plain_fit_line(value: Any) -> str:
    """Fit sentences for the dashboard. Drops fact and employment ids, including a leading `emp_1:` cite."""
    text = " ".join(str(value or "").split())
    text = _EVIDENCE_PAREN_RE.sub("", text)
    text = _EVIDENCE_PREFIX_RE.sub("", text)
    text = _EVIDENCE_ID_RE.sub("", text)
    text = re.sub(r"\(\s*\)", "", text)
    text = re.sub(r"(^|\s):\s*", r"\1", text)
    text = re.sub(r"\s+([,.;:])", r"\1", text)
    text = " ".join(text.split()).strip(" -,:;")
    return text


_FAILURE_PLAIN: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^resume field not found.*", re.I | re.S), "The form had no place to attach the resume"),
    (re.compile(r"^could not confirm the resume was attached.*", re.I | re.S), "The resume did not show as attached on the form"),
    (re.compile(r"^the form is missing fields the app expected:\s*([^.]+)", re.I), r"The form did not have: \1"),
    (re.compile(r"^the form did not respond in time[^;.]*", re.I), "The job site did not respond in time"),
    (re.compile(r"^option '?(.+?)'? is not offered;?\s*the site offers\s*\[.*$", re.I | re.S), r"The job site did not list \1 among its choices"),
    (re.compile(r"^timeout before final action.*", re.I | re.S), "The job site did not respond in time"),
    (re.compile(r"^the form reported a problem:\s*\[?([^\]]*)\]?", re.I), r"The job site reported a problem: \1"),
    (re.compile(r"^form validation errors:\s*\[?([^\]]*)\]?", re.I), r"The job site reported a problem: \1"),
    (re.compile(r"^upload (path outside artifact directory|file missing)[^.]*", re.I), "The resume file was not where the app expected it"),
    (re.compile(r"^host \S+ not in allowed hosts.*", re.I), "The job site is not on the list of sites the app may open"),
]


def plain_failure(reason: Any) -> str:
    """One plain sentence for a failed attempt, ending with the fact that nothing was sent. Technical text
    (Playwright call logs, selectors) stays in the evidence, not in the sentence."""
    text = " ".join(str(reason or "").split())
    if not text:
        return ""
    text = re.sub(r"\.?\s*Nothing was (filled in or )?sent\.?$", "", text, flags=re.I).strip()
    for pat, repl in _FAILURE_PLAIN:
        if pat.search(text):
            text = pat.sub(repl, text, count=1)
            break
    text = re.sub(r"Call log:.*$", "", text, flags=re.I).strip()
    text = text.replace("'", "").strip(" .;:")
    text = text[:1].upper() + text[1:]
    return f"{text}. Nothing was sent." if text else "Nothing was sent."


def evidence_source(fact_id: Any) -> str:
    """Plain label for an answer's fact id. The id itself is not shown."""
    fid = str(fact_id or "")
    if fid == "user:input":
        return "typed on this page"
    if fid == "artifact":
        return "your resume file"
    if fid == "research:salary":
        return "salary estimate (proposed)"
    if fid == "compensation_rule":
        return "your pay rules and the posted range"
    if fid.startswith("emp_"):
        return "your work history"
    if fid:
        return "your profile"
    return ""


def _fit_lines(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str):
            text = plain_fit_line(item)
            if text:
                out.append(text)
    return out


def donut(items: list[tuple[str, int | float, str]]) -> dict[str, Any]:
    """Segments for an SVG donut (circumference normalised to 100). items: (label, value, css class)."""
    total = float(sum(v for _, v, _ in items)) or 0.0
    segs = []
    offset = 25.0  # start at 12 o'clock
    for label, value, cls in items:
        pct = (float(value) / total * 100) if total else 0.0
        segs.append({"label": label, "value": value, "pct": round(pct, 1), "dash": f"{pct:.2f} {100 - pct:.2f}", "offset": round(offset, 2), "cls": cls})
        offset -= pct
    return {"total": int(total) if total.is_integer() else total, "segments": segs}


def month_buckets(count: int = 12, now: datetime | None = None) -> list[tuple[str, str]]:
    """Last `count` months in the configured time zone as (yyyy-mm key, short label), oldest first."""
    t = to_local(now or utcnow())
    y, m = t.year, t.month
    out: list[tuple[str, str]] = []
    for _ in range(count):
        out.append((f"{y:04d}-{m:02d}", datetime(y, m, 1).strftime("%b")))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return list(reversed(out))
