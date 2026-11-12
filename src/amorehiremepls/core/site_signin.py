"""Site sign-in: the words for each stop, the record of attempts, and the dashboard's side of one ask.

The browser task (see `apply/signin.py`) finds a sign-in form and asks. The owner presses Allow on the application
page. The dashboard that received the press reads the saved login, which is when the password manager shows its
own approval window, and seals it to the task's one-time key (`core/sealed.py`). No prompt is ever triggered
without that press, and a missed prompt only means the page asks again.

`SignInMemory` keeps one row per attempt: the site, a fingerprint of the saved login, and how it ended. It holds no
secret. A password the site refused is never sent again by the app itself. The owner is shown the site's own words,
a link to reset the password and a link back to the sign-in page, and only their press tries again. When the saved
login has not changed since the refusal, the press is answered with a second question first. Refusals are counted per
site inside a time window, and a site that says the account is locked is left alone until its wait has passed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from amorehiremepls.core.credentials import (
    ApprovalNotGiven,
    CredentialSource,
    LoginLookupError,
    LookupTransient,
    choose_login,
    registrable_domain,
    same_site,
    secure_page,
    site_host,
    with_retries,
)
from amorehiremepls.core.sealed import SealError, public_is_ours, seal
from amorehiremepls.core.util import new_id, utcnow

APPROVAL_ASKS = 6  # how many times one ask may be answered with a missed approval window before the run stops
UNCLEAR_LIMIT = 2  # sign-ins that never showed a result, in a row, before the app stops offering that login
REJECT_CYCLES = 3  # refused passwords for one site inside the window before the app stops offering another try
REJECT_WINDOW_HOURS = 24
LOCK_WAIT_MINUTES = 30  # the wait after a site reports a lock or a throttle without saying how long
CODE_LABEL = "Verification code"


@dataclass(frozen=True)
class Stop:
    """One reason a sign-in ended without signing in. `{site}` is the page's host."""

    reason: str  # the timeline line and the saved reason
    title: str  # what the owner should do, one sentence
    text: str  # the second sentence
    retry_safe: bool = True  # Retry with the app may run the same sign-in again
    wall: str = "login"


_NOTHING_SENT = "Nothing was sent."
STOPS: dict[str, Stop] = {
    "off": Stop(
        "{site} asked you to sign in.",
        "Sign in on the job site first.",
        f"{_NOTHING_SENT} Signing in with saved logins is turned off under Settings. Turn it on, or sign in to the saved session yourself, then retry.",
    ),
    "no_wait": Stop(
        "{site} asked you to sign in.",
        "Sign in on the job site first.",
        f"{_NOTHING_SENT} The app could not wait for you on that page, because the wait for a held page is set to 0 minutes.",
    ),
    "no_form": Stop(
        "{site} asked you to sign in, and no sign-in form the app can fill was found.",
        "Sign in on the job site yourself.",
        f"{_NOTHING_SENT} The page did not show a username, a password and a button to press. After you are signed in, retry.",
    ),
    "no_login": Stop(
        "No saved login for {site} was found in 1Password.",
        "Save a login for this site in 1Password, then retry.",
        f"{_NOTHING_SENT} Set the website of the saved login to this site. If you have no account there, create one on the site yourself. The app never creates accounts.",
    ),
    "several": Stop(
        "1Password has more than one saved login for {site}.",
        "Keep one saved login for this site, then retry.",
        f"{_NOTHING_SENT} The app does not guess between them. A login whose username is your profile email is preferred when only one has it.",
    ),
    "rejected": Stop(
        "{site} did not accept the saved password.",
        "Change the password on the job site, then continue.",
        f"{_NOTHING_SENT} The app will not send that password again by itself. Save the new password in the same 1Password item, then press I changed the password, try again.",
        retry_safe=False,
    ),
    "remembered": Stop(
        "{site} did not accept this saved password last time, so it was not sent again.",
        "Change the password on the job site, then continue.",
        f"{_NOTHING_SENT} The saved login has not changed since the site refused it.",
        retry_safe=False,
    ),
    "locked": Stop(
        "{site} says the account is locked or has had too many attempts.",
        "Wait, or unlock the account on the job site yourself.",
        f"{_NOTHING_SENT} The app does not sign in there again until the wait has passed.",
        retry_safe=False,
    ),
    "cycle_cap": Stop(
        "{site} refused the saved login several times, so the app stopped trying for now.",
        "Wait before trying again.",
        f"{_NOTHING_SENT} More tries could lock the account.",
        retry_safe=False,
    ),
    "unclear_limit": Stop(
        "The last sign-ins on {site} never showed a result, so the app stopped trying.",
        "Sign in on the job site yourself once to check the account.",
        f"{_NOTHING_SENT} When it works, retry. The app asks you to confirm before it sends the same login again.",
        retry_safe=False,
    ),
    "captcha": Stop(
        "{site} asked for a human check on its sign-in page.",
        "Finish the human check and sign in on the job site yourself.",
        f"{_NOTHING_SENT} The app does not get around a check on a sign-in page.",
        retry_safe=False,
    ),
    "passkey": Stop(
        "{site} only offers a passkey or a security key.",
        "Sign in on the job site yourself.",
        f"{_NOTHING_SENT} The app cannot use a passkey.",
        retry_safe=False,
    ),
    "account_needed": Stop(
        "{site} needs an account, and the page only offers sign up.",
        "Create the account on the job site yourself.",
        f"{_NOTHING_SENT} The app never creates accounts. Save the new login in 1Password, then retry.",
        retry_safe=False,
    ),
    "not_approved": Stop(
        "1Password was not approved in time.",
        "Retry when you are at your computer.",
        f"{_NOTHING_SENT} The app asks again and waits for you to allow 1Password. Retry with the app is safe.",
    ),
    "declined": Stop(
        "You chose not to sign in to {site} with 1Password.",
        "Sign in on the job site yourself, or retry.",
        _NOTHING_SENT,
    ),
    "reader_not_installed": Stop(
        "1Password could not be read on the computer that was asked.",
        "Open this page on the computer where 1Password is installed, then retry.",
        f"{_NOTHING_SENT} The 1Password command line tool was not found there.",
    ),
    "reader_not_signed_in": Stop(
        "1Password is not signed in on the computer that was asked.",
        "Sign in to the 1Password app, then retry.",
        _NOTHING_SENT,
    ),
    "reader_integration_off": Stop(
        "The 1Password app did not answer the command line tool.",
        "Open 1Password, turn on Settings, Developer, Integrate with 1Password CLI, then retry.",
        _NOTHING_SENT,
    ),
    "reader_failed": Stop(
        "1Password could not be read.",
        "Check that 1Password opens on your computer, then retry.",
        f"{_NOTHING_SENT} Retry with the app is safe.",
    ),
    "mismatch": Stop(
        "The sign-in request could not be checked, so no login was sent to it.",
        "Retry with the app.",
        f"{_NOTHING_SENT} The dashboard and the background task must be started with the same app secret.",
    ),
    "code_timeout": Stop(
        "{site} asked for a verification code and none was typed in time.",
        "Retry when you can read the code.",
        f"{_NOTHING_SENT} The password was accepted. Retry with the app is safe and asks for a new code.",
        wall="code",
    ),
    "code_rejected": Stop(
        "{site} did not accept the verification codes that were typed.",
        "Retry with a fresh code.",
        f"{_NOTHING_SENT} The password was accepted. Retry with the app is safe and asks for a new code.",
        wall="code",
    ),
    "unclear": Stop(
        "{site} did not finish signing in before the wait ended.",
        "Retry with the app.",
        f"{_NOTHING_SENT} The page never said whether the sign-in worked. Retry with the app is safe once more.",
    ),
    "not_secure": Stop(
        "The sign-in page on {site} is not a secure page, so the saved password was not typed.",
        "Sign in on the job site yourself.",
        _NOTHING_SENT,
        retry_safe=False,
    ),
    "moved": Stop(
        "The sign-in page changed to another site before the password was typed, so it was not typed.",
        "Retry with the app.",
        _NOTHING_SENT,
    ),
    "again": Stop(
        "{site} asked to sign in a second time in the same attempt.",
        "Retry with the app.",
        f"{_NOTHING_SENT} The app signs in once per attempt.",
    ),
    "too_long": Stop(
        "Signing in to {site} took longer than the time allowed.",
        "Retry with the app.",
        f"{_NOTHING_SENT} Retry with the app is safe.",
    ),
}
# Walls that are not a sign-in form. The same sentences are used with the switch on or off.
WALLS: dict[str, Stop] = {
    "login": STOPS["off"],
    "code": Stop(
        "{site} asked for a verification code.",
        "Retry with the app when you can read the code.",
        f"{_NOTHING_SENT} The app asks you for the code on this page and types it for you.",
        wall="code",
    ),
    "checkpoint": Stop(
        "{site} asked you to verify it is you, with no code box to fill.",
        "The job site asked you to verify it is you.",
        f"Finish that check yourself. {_NOTHING_SENT}",
        retry_safe=False,
        wall="checkpoint",
    ),
    "blocked": Stop(
        "{site} blocked the app from opening the page.",
        "The job site blocked this browser.",
        f"{_NOTHING_SENT} Signing in again will not change that.",
        retry_safe=False,
        wall="blocked",
    ),
}


def stop_for(code: str) -> Stop:
    return STOPS.get(code) or WALLS.get(code) or STOPS["unclear"]


def stop_reason(code: str, site: str) -> str:
    return stop_for(code).reason.format(site=site or "The job site")


def ask_context(ask: dict) -> str:
    """What one sealed box is bound to: the ask, the site and the step."""
    return f"{ask.get('ask_id') or ''}|{ask.get('host') or ''}|{ask.get('step') or ''}"


def safe_link(url: Any, *sites: str) -> str:
    """A link the owner may be shown: a secure page on one of these sites. Anything else is left out."""
    text = str(url or "").strip()
    if not text or len(text) > 2000 or not secure_page(text):
        return ""
    return text if any(same_site(site_host(text), site) for site in sites if site) else ""


class SignInMemory:
    """Attempt rows in `site_signins`. No username, no password, no code.

    A refused or locked row also keeps what the owner needs to fix it: the site's own words, the reset link and the
    sign-in link found on the page, and the name of the saved login."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def _rows(self):
        return self.store.c("site_signins")

    def _recent(self, host: str, limit: int = 40) -> list[dict]:
        return list(self._rows().find({"domain": registrable_domain(host)}).sort("started_at", -1).limit(limit))

    def begin(self, host: str, login_fingerprint: str, job_id: str = "") -> str:
        """Saved before the password is sent, so a restart knows an attempt was under way."""
        now = utcnow()
        doc = {
            "_id": new_id("signin"),
            "host": site_host(host),
            "domain": registrable_domain(host),
            "fingerprint": login_fingerprint,
            "job_id": job_id,
            "status": "started",
            "started_at": now,
            "updated_at": now,
        }
        self._rows().insert_one(doc)
        return doc["_id"]

    def finish(self, attempt_id: str | None, status: str, facts: dict | None = None, wait_minutes: int = 0) -> None:
        """How the attempt ended. `facts` are the fix-it details of a refusal; `wait_minutes` is a lock's wait."""
        if not attempt_id:
            return
        now = utcnow()
        fields: dict[str, Any] = {"status": status, "finished_at": now, "updated_at": now}
        for name, cap in (("said", 300), ("reset_url", 2000), ("signin_url", 2000), ("item_title", 120)):
            if facts and facts.get(name):
                fields[name] = str(facts[name])[:cap]
        if status == "locked":
            fields["wait_said"] = bool(wait_minutes)
            fields["retry_at"] = now + timedelta(minutes=wait_minutes or LOCK_WAIT_MINUTES)
        self._rows().update_one({"_id": attempt_id}, {"$set": fields})

    def blocked(self, host: str, login_fingerprint: str, now: datetime | None = None) -> str | None:
        """Why this saved login must not be sent to this site right now, or None.

        `locked`: the site reported a lock and its wait has not passed (any saved login). `remembered`: the site
        refused this exact saved login. `unclear_limit`: the last tries with it never showed a result."""
        now = now or utcnow()
        unclear = 0
        for row in self._recent(host):
            status = row.get("status")
            if status == "signed_in":
                break
            if status == "locked" and row.get("retry_at") and row["retry_at"] > now:
                return "locked"
            if row.get("cleared_at") or row.get("fingerprint") != login_fingerprint:
                continue
            if status == "rejected":
                return "remembered"
            if status in ("started", "unclear"):
                unclear += 1
        return "unclear_limit" if unclear >= UNCLEAR_LIMIT else None

    def _refusals(self, host: str, now: datetime) -> list[dict]:
        """Refused passwords for this site inside the window, since the last sign-in that worked. Newest first."""
        since = now - timedelta(hours=REJECT_WINDOW_HOURS)
        found = []
        for row in self._recent(host):
            if row.get("status") == "signed_in" or row.get("started_at") is None or row["started_at"] < since:
                break
            if row.get("status") == "rejected":
                found.append(row)
        return found

    def rejections(self, host: str, now: datetime | None = None) -> int:
        return len(self._refusals(host, now or utcnow()))

    def clear(self, host: str) -> int:
        """The owner confirmed the same saved login may be sent once more. Earlier refusals stop blocking; they still count toward the ceiling."""
        done = self._rows().update_many(
            {"domain": registrable_domain(host), "status": {"$in": ["rejected", "started", "unclear"]}, "cleared_at": None}, {"$set": {"cleared_at": utcnow()}}
        )
        return int(done.modified_count)

    def last_success(self, host: str) -> Any:
        row = self._rows().find_one({"domain": registrable_domain(host), "status": "signed_in"}, sort=[("finished_at", -1)])
        return (row or {}).get("finished_at")

    def fix_facts(self, host: str, now: datetime | None = None) -> dict | None:
        """What the application page shows after a refusal or a lock: the site's words, the links, and when a try is allowed.

        None when the last thing that happened on this site was a sign-in that worked, or nothing was refused."""
        now = now or utcnow()
        latest = None
        for row in self._recent(host):
            if row.get("status") == "signed_in":
                break
            if row.get("status") in ("rejected", "locked"):
                latest = row
                break
        if latest is None:
            return None
        refused = self._refusals(host, now)
        capped = len(refused) >= REJECT_CYCLES
        wait_until = latest.get("retry_at") if latest.get("status") == "locked" and latest.get("retry_at") and latest["retry_at"] > now else None
        # The sign-in link is the address the attempt itself opened. The reset link came from the page, so it must be the same site.
        signin_url = safe_link(latest.get("signin_url"), site_host(str(latest.get("signin_url") or "")))
        return {
            "status": latest.get("status"),
            "site": latest.get("host") or site_host(host),
            "said": str(latest.get("said") or "")[:300],
            "reset_url": safe_link(latest.get("reset_url"), latest.get("host") or host, host, site_host(signin_url)),
            "signin_url": signin_url,
            "item_title": str(latest.get("item_title") or "")[:120],
            "count": len(refused),
            "capped": capped,
            "cap_until": (refused[-1]["started_at"] + timedelta(hours=REJECT_WINDOW_HOURS)) if capped else None,
            "wait_until": wait_until,
            "wait_said": bool(latest.get("wait_said")),
        }


@dataclass(frozen=True)
class AskAnswer:
    """What the dashboard does with one press of Allow. `reply` goes to the waiting task; None keeps it waiting."""

    flash: str
    ok: bool
    reply: dict | None = None
    missed: bool = False  # the approval window was missed; the same ask stays open
    unchanged: str = ""  # the saved login is the one the site refused; the owner is asked a second question first


def answer_ask(
    ask: dict, source: CredentialSource, memory: SignInMemory | None, app_secret: str, prefer_username: str = "", sleep: Any = None, confirm_same: bool = False
) -> AskAnswer:
    """Read the saved login for one ask and seal it to the asking task. Called only from the owner's button press.

    The password manager shows its own approval window during this call. A missed window returns `missed` and leaves
    the ask open. A reply is an error code or a sealed box; the secret is never returned in the clear.

    A saved login the site already refused is not read at all unless `confirm_same` says the owner asked for exactly
    that. A lock that has not run out, or too many refusals inside the window, ends the attempt without a read.
    """
    context = ask_context(ask)
    host = site_host(str(ask.get("host") or ""))
    step = str(ask.get("step") or "login")
    if not host or not public_is_ours(app_secret, str(ask.get("key") or ""), context, str(ask.get("key_sig") or "")):
        return AskAnswer("signin_mismatch", False, {"kind": "error", "error": "mismatch"})
    if not source.available():
        return AskAnswer("signin_not_here", False)
    kwargs = {"sleep": sleep} if sleep is not None else {}
    try:
        login = choose_login(with_retries(source.logins, **kwargs), host, prefer_username)
        if step == "code":
            if ask.get("fingerprint") and ask.get("fingerprint") != login.fingerprint:
                return AskAnswer("signin_changed", False)
            payload = {"host": host, "code": with_retries(lambda: source.one_time_code(login), **kwargs)}
        else:
            barred = memory.blocked(host, login.fingerprint) if memory is not None else None
            if barred == "locked":
                return AskAnswer("signin_locked", False, {"kind": "error", "error": "locked"})
            if memory is not None and memory.rejections(host) >= REJECT_CYCLES:
                return AskAnswer("signin_cycle_cap", False, {"kind": "error", "error": "cycle_cap"})
            if barred and not confirm_same:
                return AskAnswer("signin_unchanged", False, unchanged=barred)
            if barred and memory is not None:
                memory.clear(host)
            found = with_retries(lambda: source.secret(login), **kwargs)
            payload = {
                "host": host,
                "username": found.username,
                "password": found.password,
                "has_code": found.has_one_time_code,
                "fingerprint": login.fingerprint,
                "title": login.title,
            }
    except ApprovalNotGiven:
        return AskAnswer("signin_not_approved", False, missed=True)
    except LookupTransient:
        return AskAnswer("signin_lookup_failed", False, missed=True)
    except LoginLookupError as exc:
        if step == "code":
            return AskAnswer("signin_no_code", False)
        code = exc.kind if exc.kind in ("no_login", "several") else f"reader_{exc.kind}"
        return AskAnswer(f"signin_{code}", False, {"kind": "error", "error": code})
    try:
        box = seal(str(ask.get("key") or ""), payload, context)
    except SealError:
        return AskAnswer("signin_mismatch", False, {"kind": "error", "error": "mismatch"})
    return AskAnswer("signin_code_sent" if step == "code" else "signin_allowed", True, {"kind": "box", "box": box})
