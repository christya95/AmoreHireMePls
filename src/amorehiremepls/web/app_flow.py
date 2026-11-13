"""How the application page reads as one flow: where the application is, and the one thing to do next.

`needed_now` (in `chrome.py`) decides the ask. This module finishes it for the page:

- `finish_need`: a stopped application always names a next action, and "I applied on the site" is never that action
  for an application that was not sent. It is offered apart, under More. Only after the final click, when the
  form went out and no confirmation was seen, is recording it the main step.
- `flow_steps`: the compact tracker (Prepared … Confirmed) with one step marked as current, waiting on the person,
  or stopped.
- `attempts`: a short history of earlier tries.
- `site_status`: what is known about signing in on this job's site, said before approval.
- `STATES`: the state → action table. `docs/ui-copy.md` lists the same rows and a test walks every one.

Nothing here grants an approval or queues work. It only chooses words and which existing control to show.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from amorehiremepls.core.credentials import registrable_domain, site_host

NOTHING_NEEDED = "Nothing is needed from you right now."
OPEN_SITE = "Open the job site"
MANUAL_NOTE = "If you apply there yourself, record it under More, I applied on the site."
# Kinds whose control waits on the person. `site` opens the job site; `cancel` and an empty kind do not wait on them.
KIND_LABEL = {
    "human_check": "Continue",
    "answer": "Save this answer",
    "answers": "Answer the questions",
    "resume": "Select a resume",
    "approve": "Approve this exact application",
    "fill": "Fill in the form",
    "run": "Apply with the app",
    "cancel": "Cancel this attempt",
    "retry": "Retry with the app",
    "site": OPEN_SITE,
    "verify": "Save the result",
}
WAITS_ON_YOU = frozenset({"manual", "human_check", "signin", "field", "answer", "answers", "resume", "approve", "fill", "run", "retry", "site", "verify"})

STEPS: tuple[tuple[str, str], ...] = (
    ("prepared", "Prepared"),
    ("approved", "Approved"),
    ("opening", "Opening the job site"),
    ("signin", "Signing in"),
    ("filling", "Filling the form"),
    ("code", "Verification code"),
    ("sending", "Sending"),
    ("confirmed", "Confirmed"),
)
STEP_STATE_LABEL = {"done": "done", "current": "now", "you": "waiting on you", "stopped": "stopped here", "todo": ""}


def _site_href(app: dict, job: dict | None) -> str:
    """The job's apply page, only when it is a web address. It came from a posting, so nothing else is trusted."""
    pkg = app.get("review_package") or {}
    for raw in (pkg.get("apply_url"), (job or {}).get("apply_url"), pkg.get("posting_url"), (job or {}).get("posting_url")):
        text = str(raw or "").strip()
        if urlsplit(text).scheme in ("http", "https") and urlsplit(text).netloc:
            return text
    return ""


def _sent(app: dict) -> bool:
    evidence = app.get("evidence") or {}
    if app.get("status") in ("submitted", "needs_verification") or evidence.get("final_clicked"):
        return True
    return bool(evidence.get("receipt_text") or evidence.get("confirmation") or evidence.get("receipt_path") or evidence.get("basis") == "receipt_observed")


def _site_need(title: str, text: str, href: str) -> dict:
    return {"kind": "site", "title": title, "text": text, "field": None, "href": href, "label": OPEN_SITE}


def finish_need(
    need: dict,
    app: dict,
    job: dict | None = None,
    *,
    show_status: str = "",
    working: bool = False,
    live: bool = False,
    allowed: bool = True,
    can_clear: bool = False,
    hold: dict | None = None,
) -> dict:
    """The ask the page shows. Same controls as before; two rules are added.

    1. A stopped application never reads "nothing is needed". It says what stopped and what to do.
    2. Recording "I applied on the site" marks an application as sent, so it is never the main button for one that
       was not sent. The main button opens the job site; the record form sits under More."""
    status = str(app.get("status") or "")
    wall = str(app.get("handoff_kind") or "")
    href = _site_href(app, job)
    out = dict(need)
    if out.get("kind") == "manual" and _sent(app):
        # The final click happened, so recording what the site showed is the real next step here.
        out.update(
            title="The form was sent, but the app saw no confirmation.",
            text="Check the job site or your email. If the application is there, record what you saw. If it is not, apply on the job site yourself.",
            label="Record the confirmation",
        )
    elif out.get("kind") == "manual":
        if status == "handoff" and wall == "checkpoint":
            text = "Nothing was sent. The app does not go through that check. Finish it on the job site yourself and apply there."
            out = _site_need("The job site asked you to verify it is you.", text, href)
        elif status == "handoff" and wall == "blocked":
            out = _site_need("The job site blocked this browser.", "Nothing was sent. Signing in again will not change that. You can apply on the job site yourself.", href)
        elif status == "handoff" and wall == "unsupported":
            out = _site_need("Finish the remaining step on the job site.", "The app filled what it could and stopped before sending. Nothing was sent.", href)
        else:
            out = _site_need("Apply on the job site yourself.", "The app cannot fill in this job site for you. Use the resume below. Nothing was sent.", href)
    elif out.get("title") == NOTHING_NEEDED and not out.get("kind") and not working and not hold:
        if status == "authorized" and not allowed and not live:
            text = "The approval for this application ended or was used up. Approve it again and the app applies. Nothing is sent until you do."
            out = {"kind": "approve", "title": "This application needs your approval again.", "text": text, "field": None}
        elif status == "handoff":
            out = _site_need("Apply on the job site yourself.", "The app stopped and cannot continue on this site. Nothing was sent.", href)
        elif show_status == "failed" and not live:
            again = " To prepare it again, use Start over under More." if can_clear else ""
            out = _site_need("This attempt stopped.", f"Nothing was sent. The app cannot try this site again as it is. You can apply on the job site yourself.{again}", href)
    out["manual_offer"] = status == "handoff" and out.get("kind") != "manual"
    if out.get("kind") == "site" and out["manual_offer"]:
        out["text"] = f"{out['text']} {MANUAL_NOTE}"
    if not out.get("label"):
        out["label"] = ("Use this code" if out.get("code") else "Use this answer") if out.get("kind") == "field" else KIND_LABEL.get(str(out.get("kind") or ""), "")
    out["waits_on_you"] = out.get("kind") in WAITS_ON_YOU
    return out


def hold_limit(hold: dict | None, local) -> str:
    """How long the app keeps waiting, as one sentence. `local` formats a time for the person's clock."""
    until = (hold or {}).get("expires_at")
    if not until:
        return ""
    return f"The app waits for you until {local(until, '%H:%M')}. After that this attempt stops with nothing sent, and you can start it again here."


# -- the tracker -----------------------------------------------------------------------------------------
def _signin_seen(app: dict, hold: dict | None) -> tuple[bool, bool]:
    """Whether this attempt met a sign-in page, and whether the site asked for a code while signing in."""
    record = (app.get("evidence") or {}).get("signin")
    record = record if isinstance(record, dict) else {}
    wall = str(app.get("handoff_kind") or "") if app.get("status") == "handoff" else ""
    ask = (hold or {}).get("signin") if isinstance((hold or {}).get("signin"), dict) else None
    signin = bool(record) or wall in ("login", "code") or bool(ask)
    code = bool(record.get("codes_asked")) or wall == "code" or bool(ask and (hold or {}).get("purpose") == "field")
    return signin, code


def flow_steps(app: dict, show_status: str, need: dict, hold: dict | None = None, working: bool = False, final_noted: bool = False, live: bool = False) -> list[dict]:
    """The tracker rows: `{key, label, state, note}` with state done, current, you, stopped or todo.

    Steps that did not come up are left out: Signing in appears only when the site asked for it, Verification code
    only when a code was asked for."""
    status = str(app.get("status") or "")
    evidence = app.get("evidence") or {}
    wall = str(app.get("handoff_kind") or "") if status == "handoff" else ""
    purpose = str((hold or {}).get("purpose") or "") if hold else ""
    signin, signin_code = _signin_seen(app, hold)
    email_code = bool(hold and purpose == "field" and not isinstance(hold.get("signin"), dict) and "code" in str(hold.get("field_label") or "").lower())
    clicked = bool(final_noted or evidence.get("final_clicked"))
    at, state = "prepared", "current"
    if status == "submitted":
        at, state = "confirmed", "done"
    elif status == "needs_verification":
        at, state = "confirmed", "you"
    elif hold:
        if purpose == "signin":
            at = "signin"
        elif purpose == "field" and (signin_code or email_code):
            at = "code"
        elif purpose == "field":
            at = "filling"
        else:
            at = "sending" if clicked else "filling"
        state = "you"
    elif working:
        at, state = ("sending" if clicked else "filling"), "current"
    elif status == "challenge_retry":
        at, state = ("sending" if clicked else "filling"), "current"
    elif status == "handoff":
        at = {"login": "signin", "code": "code", "checkpoint": "opening", "blocked": "opening", "human_check": "sending" if clicked else "filling", "unsupported": "filling"}.get(
            wall, "opening"
        )
        state = "stopped"
    elif show_status == "failed":
        started = bool(evidence.get("steps_done")) or bool((evidence.get("preflight") or {}).get("checked")) or bool(evidence.get("cancelled"))
        at, state = ("filling" if started else "opening"), "stopped"
    elif status == "needs_input":
        at, state = "prepared", "you"
    elif status == "ready_for_review" or need.get("kind") == "approve":
        at, state = "approved", "you"
    elif status in ("authorized", "executing"):
        at, state = ("opening", "current") if live else ("approved", "done")
        if not live and need.get("kind") in ("run", "fill"):
            at, state = "opening", "you"
    rows = []
    order = [key for key, _ in STEPS]
    if signin_code and not email_code:  # a code asked for while signing in comes before the form
        order.remove("code")
        order.insert(order.index("signin") + 1, "code")
    labels = dict(STEPS)
    reached = order.index(at)
    for index, key in enumerate(order):
        if key == "signin" and not signin and at != "signin":
            continue
        if key == "code" and not (signin_code or email_code) and at != "code":
            continue
        if index < reached:
            mark = "done"
        elif index == reached:
            mark = state
        else:
            mark = "todo"
        rows.append({"key": key, "label": labels[key], "state": mark, "note": STEP_STATE_LABEL[mark]})
    return rows


def flow_key(steps: list[dict], need: dict) -> str:
    """Changes whenever the tracker or the ask changes, so the open page knows to show the new one."""
    return "|".join(f"{row['key']}:{row['state']}" for row in steps) + f"#{need.get('kind') or ''}"


# -- earlier tries ---------------------------------------------------------------------------------------
def attempts(runs: list[dict], line_for, limit: int = 6) -> list[dict]:
    """Newest first: `{n, started, how, live}`. `line_for(run)` gives the last plain sentence saved for that run."""
    ordered = sorted([r for r in runs or [] if r.get("created_at")], key=lambda r: r["created_at"])
    rows = []
    for number, run in enumerate(ordered, start=1):
        live = run.get("status") in ("queued", "claimed", "running", "waiting", "paused")
        rows.append(
            {"n": number, "started": run.get("started_at") or run.get("created_at"), "how": line_for(run) or ("Still going." if live else "No step was saved."), "live": live}
        )
    rows.reverse()
    return rows[:limit]


def attempt_line(runs: list[dict], local) -> str:
    """The newest try as "Attempt 3 · started <time>", so a new try is not mistaken for the last one's stop."""
    ordered = sorted([r for r in runs or [] if r.get("created_at")], key=lambda r: r["created_at"])
    if not ordered:
        return ""
    newest = ordered[-1]
    return f"Attempt {len(ordered)} · started {local(newest.get('started_at') or newest.get('created_at'), '%Y-%m-%d %H:%M')}"


# -- signing in on this job's site -----------------------------------------------------------------------
ACCOUNT_STOPS = ("account_needed", "no_login")  # sign-in outcomes that mean: this site needs an account the app does not have


def site_status(memory: Any, app: dict, job: dict | None, signin_on: bool, local, known_stop: str = "") -> dict | None:
    """What is known about signing in where this job's form lives. None when there is nothing to say.

    Read from the sign-in history only. Whether a saved session still works is not known until the site is opened,
    and the page says so instead of guessing. `known_stop` is the outcome of an earlier stop on this site that means
    an account is missing; it is said before approval, so the person can fix it first."""
    pkg = app.get("review_package") or {}
    href = _site_href(app, job)
    host = site_host(href) if href else ""
    if not host:
        return None
    posted = site_host(str(pkg.get("posting_url") or (job or {}).get("posting_url") or ""))
    elsewhere = bool(posted) and registrable_domain(posted) != registrable_domain(host)
    last = memory.last_success(host) if memory is not None else None
    asked = bool(memory.asked(host)) if memory is not None else False
    wall = str(app.get("handoff_kind") or "") in ("login", "code") or isinstance((app.get("evidence") or {}).get("signin"), dict)
    missing = known_stop if known_stop in ACCOUNT_STOPS and not last else ""
    if not (elsewhere or last or asked or wall or missing):
        return None
    lines = []
    if elsewhere:
        lines.append(f"The form for this job is on {host}, not on the site where the job is posted.")
    if last:
        lines.append(f"The app last signed in to {host} on {local(last, '%Y-%m-%d %H:%M')}. It tries that saved session first.")
    elif missing == "account_needed":
        lines.append(f"Last time {host} only offered sign up. The app does not create accounts. Create one on the job site yourself and save its login in 1Password first.")
    elif missing == "no_login":
        lines.append(f"Last time {host} asked to sign in and no saved login for it was found in 1Password. Save one there first, or the application stops at the sign-in page.")
    elif asked or wall:
        lines.append(f"{host} has asked the app to sign in before, and no sign-in there has worked yet.")
    if last or asked or wall or missing:
        if signin_on:
            lines.append("If the site asks to sign in, this page asks you to allow 1Password first.")
        else:
            lines.append("Signing in with saved logins is off under Settings, so a sign-in page stops the application for you.")
    return {"host": host, "elsewhere": elsewhere, "last": last, "asked": asked or wall, "on": signin_on, "warn": bool(missing), "lines": lines, "text": " ".join(lines)}


# -- the state → action table ----------------------------------------------------------------------------
@dataclass(frozen=True)
class State:
    """One row of the table: a state the page can be in, the control it shows, and the label on that control."""

    name: str
    kind: str  # the control `application.html` renders; "" means the app is busy or waiting and shows no button
    label: str
    app: dict = field(default_factory=dict)
    flags: dict = field(default_factory=dict)  # arguments of `needed_now` and `finish_need`
    hold: dict | None = None
    fix: dict | None = None
    waits: bool = True  # the person has to act; False when the app is working or a wait has to pass


_LATER = datetime(2999, 1, 1, tzinfo=UTC)
_PKG = {"apply_url": "https://jobs.example.test/apply/1", "posting_url": "https://jobs.example.test/1", "answers": [], "outstanding": []}
_ASK = {"ask_id": "ask_1", "host": "jobs.example.test", "step": "login"}


def _app(status: str, **extra: Any) -> dict:
    return {"_id": "app_1", "job_id": "job_1", "status": status, "material_hash": "h", "review_package": dict(_PKG), **extra}


def _stopped(wall: str, **extra: Any) -> dict:
    return _app("handoff", handoff_kind=wall, **extra)


def _signin(outcome: str) -> dict:
    return {"signin": {"host": "jobs.example.test", "outcome": outcome}, "final_clicked": False}


STATES: tuple[State, ...] = (
    State("A resume is not selected yet", "resume", "Select a resume", _app("needs_input", review_package={**_PKG, "outstanding": ["approved base resume not selected"]})),
    State(
        "One answer is missing",
        "answer",
        "Save this answer",
        _app("needs_input", review_package={**_PKG, "answers": [{"field_id": "f1", "label": "Notice period", "needs_input": True}]}),
    ),
    State("Several answers are missing", "answers", "Answer the questions", _app("needs_input")),
    State("Being prepared", "", "", _app("preparing"), waits=False),
    State("Ready to read and approve", "approve", "Approve this exact application", _app("ready_for_review")),
    State("Answers changed after approval", "approve", "Approve this exact application", _app("authorized"), {"stale": True}),
    State("The approval ended or was used up", "approve", "Approve this exact application", _app("authorized"), {"allowed": False}),
    State("Approved, different answers on the page", "fill", "Fill in the form", _app("authorized"), {"can_fill": True}),
    State("Approved, nothing running", "run", "Apply with the app", _app("authorized"), {"can_run": True}),
    State("Queued or opening the job site", "", "", _app("authorized"), {"live": True}, waits=False),
    State("Filling the form", "cancel", "Cancel this attempt", _app("executing"), {"working": True, "can_cancel": True, "live": True}, waits=False),
    State("Sending, after the final click", "", "", _app("executing"), {"working": True, "live": True, "final_noted": True}, waits=False),
    State("Sign-in page, waiting for 1Password", "signin", "Allow 1Password", _app("executing"), {"live": True}, {"purpose": "signin", "signin": _ASK}),
    State("1Password prompt was missed", "signin", "Allow 1Password", _app("executing"), {"live": True}, {"purpose": "signin", "signin": _ASK, "signin_missed": 1}),
    State(
        "Saved password refused, attempt still open",
        "signin",
        "I changed the password, try again",
        _app("executing"),
        {"live": True},
        {"purpose": "signin", "signin": {**_ASK, "fix": True}},
    ),
    State(
        "Saved login unchanged since the refusal",
        "signin",
        "I changed the password, try again",
        _app("executing"),
        {"live": True},
        {"purpose": "signin", "signin": _ASK, "signin_unchanged": "remembered"},
    ),
    State(
        "Verification code while signing in",
        "field",
        "Use this code",
        _app("executing"),
        {"live": True},
        {"purpose": "field", "field_label": "Verification code", "signin": {**_ASK, "step": "code"}},
    ),
    State("The form asks for one more answer", "field", "Use this answer", _app("executing"), {"live": True}, {"purpose": "field", "field_label": "Start date"}),
    State("Human check on the page", "human_check", "Continue", _app("executing"), {"live": True}, {"purpose": "check"}),
    State("A later try is scheduled after a human check", "", "", _app("challenge_retry"), waits=False),
    State("Stopped: sign-in page, saved logins off", "retry", "Retry with the app", _stopped("login", evidence=_signin("off")), {"can_retry": True}),
    State("Stopped: no saved login for the site", "retry", "Retry with the app", _stopped("login", evidence=_signin("no_login")), {"can_retry": True}),
    State("Stopped: 1Password not approved in time", "retry", "Retry with the app", _stopped("login", evidence=_signin("not_approved")), {"can_retry": True}),
    State(
        "Stopped: saved password refused",
        "retry",
        "I changed the password, try again",
        _stopped("login", evidence=_signin("rejected")),
        {"can_retry": True},
        fix={"status": "rejected", "site": "jobs.example.test", "count": 1},
    ),
    State(
        "Stopped: account locked, wait running",
        "",
        "",
        _stopped("login", evidence=_signin("locked")),
        {"can_retry": True},
        fix={"status": "locked", "site": "jobs.example.test", "wait_until": _LATER},
        waits=False,
    ),
    State(
        "Stopped: refused too many times today",
        "",
        "",
        _stopped("login", evidence=_signin("cycle_cap")),
        {"can_retry": True},
        fix={"status": "rejected", "site": "jobs.example.test", "capped": True, "count": 3, "cap_until": _LATER},
        waits=False,
    ),
    State("Stopped: verification code not typed or refused", "retry", "Retry with the app", _stopped("code", evidence=_signin("code_timeout")), {"can_retry": True}),
    State("Stopped: verify it is you, no code box", "site", OPEN_SITE, _stopped("checkpoint"), {"can_retry": True}),
    State("Stopped: the site blocked the browser", "site", OPEN_SITE, _stopped("blocked"), {"can_retry": True}),
    State("Stopped: human check was not finished", "retry", "Retry with the app", _stopped("human_check"), {"can_retry": True}),
    State("Stopped: a step the app cannot do", "site", OPEN_SITE, _stopped("unsupported"), {"can_retry": True}),
    State("Stopped before sending, same approval still covers it", "retry", "Retry with the app", _stopped(""), {"can_retry": True, "can_retry_same": True}),
    State("Stopped on a site the app cannot fill", "site", OPEN_SITE, _stopped("")),
    State("Failed before anything was sent", "retry", "Retry with the app", _app("failed"), {"can_retry": True}),
    State("Failed on a site the app cannot fill", "site", OPEN_SITE, _app("failed"), {"can_clear": True}),
    State("Stopped after the final click, no confirmation seen", "manual", "Record the confirmation", _stopped("human_check", evidence={"final_clicked": True})),
    State("Unclear after the final click", "verify", "Save the result", _app("needs_verification")),
    State("Sent and confirmed", "", "", _app("submitted"), waits=False),
)
