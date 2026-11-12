"""Hold one browser page while a human check is on screen.

The worker has a single browser, so a hold pauses other browser work until Continue is pressed or the
time runs out. Continue does not click submit again: it only asks the same page to be checked for a
confirmation. On timeout the browser is closed and the caller records a handoff. A fresh session is
never started from here, so a submit that already happened cannot be repeated.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import timedelta

from amorehiremepls.core.actions import ActionStore
from amorehiremepls.core.config import Settings
from amorehiremepls.core.db import Store
from amorehiremepls.core.util import new_id, utcnow

HOLD_FRESH_SECONDS = 20


def run_hold(
    *,
    minutes: float,
    poll_seconds: float,
    sleep: Callable[[float], None],
    heartbeat: Callable[[], None],
    continue_requested: Callable[[], bool],
    receipt_visible: Callable[[], bool],
    monotonic: Callable[[], float] = time.monotonic,
) -> str:
    """Wait until a receipt is visible, Continue is pressed, or the deadline passes.

    Returns `receipt`, `continue`, or `timeout`. Does not click anything.
    """
    if minutes <= 0:
        return "timeout"
    deadline = monotonic() + minutes * 60
    while True:
        heartbeat()
        if receipt_visible():
            return "receipt"
        if continue_requested():
            return "continue"
        if monotonic() >= deadline:
            return "timeout"
        sleep(min(poll_seconds, max(deadline - monotonic(), 0)))


class HumanHold:
    """Mongo record for one held page, plus the lease extension that keeps the same action executing."""

    def __init__(self, store: Store, actions: ActionStore, settings: Settings, application_id: str, job_id: str, worker: str, poll_seconds: float = 1.0) -> None:
        self.store = store
        self.actions = actions
        self.settings = settings
        self.application_id = application_id
        self.job_id = job_id
        self.worker = worker
        self.minutes = float(settings.human_check_hold_minutes)
        self.poll_seconds = poll_seconds
        self.hold_id: str | None = None
        self._closed = False

    def heartbeat(self, evidence: dict | None = None) -> None:
        evidence = evidence or {}
        if self.hold_id is None or self._closed:
            # A finished hold is over. The next wait in the same attempt (a second approval, a code) is its own record.
            self._begin(evidence)
        now = utcnow()
        until = now + timedelta(seconds=180)
        action = self.actions.by_key(f"submit:{self.job_id}")
        if action and action.get("status") == "executing":
            self.actions.extend_lease(action["_id"], action.get("reserved_by") or self.worker, seconds=180)
        self.store.c("jobs").update_one({"_id": self.job_id, "status": "executing"}, {"$set": {"reserved_until": until, "updated_at": now}})
        self.store.set_control("worker_heartbeat", {"at": now, "name": self.worker})
        fields = {"heartbeat_at": now, **_field_hold_fields(evidence)}
        self.store.c("human_holds").update_one({"_id": self.hold_id}, {"$set": fields})

    def supplied_answer(self) -> str:
        """The answer typed on the dashboard, if this hold is waiting on a field. Empty when none was sent."""
        if not self.hold_id:
            return ""
        doc = self.store.c("human_holds").find_one({"_id": self.hold_id}, {"answer": 1, "continue_requested": 1})
        if not doc or not doc.get("continue_requested"):
            return ""
        return " ".join(str(doc.get("answer") or "").split())[:200]

    def continue_requested(self) -> bool:
        if not self.hold_id:
            return False
        doc = self.store.c("human_holds").find_one({"_id": self.hold_id}, {"continue_requested": 1})
        return bool(doc and doc.get("continue_requested"))

    def reply(self) -> dict | None:
        """The dashboard's answer to a sign-in ask, taken out of the database as it is read. None when there is none."""
        if not self.hold_id:
            return None
        doc = self.store.c("human_holds").find_one_and_update({"_id": self.hold_id, "signin_reply": {"$exists": True}}, {"$unset": {"signin_reply": ""}})
        got = (doc or {}).get("signin_reply")
        return got if isinstance(got, dict) else None

    def forget_answer(self) -> None:
        """Remove a typed verification code from the record once the page has it."""
        if self.hold_id:
            self.store.c("human_holds").update_one({"_id": self.hold_id}, {"$unset": {"answer": ""}})

    def finish(self, status: str) -> None:
        if not self.hold_id:
            return
        self._closed = True
        self.store.c("human_holds").update_one({"_id": self.hold_id, "status": "holding"}, {"$set": {"status": status, "finished_at": utcnow()}, "$unset": {"signin_reply": ""}})

    def _begin(self, evidence: dict) -> None:
        action = self.actions.by_key(f"submit:{self.job_id}") or {}
        now = utcnow()
        # A crashed hold must not block a new one. Only one page is held at a time.
        self.store.c("human_holds").update_many({"application_id": self.application_id, "status": "holding"}, {"$set": {"status": "released", "finished_at": now}})
        doc = {
            "_id": new_id("hold"),
            "application_id": self.application_id,
            "job_id": self.job_id,
            "action_id": action.get("_id"),
            "idempotency_key": action.get("idempotency_key") or f"submit:{self.job_id}",
            "material_hash": action.get("material_hash"),
            "status": "holding",
            "continue_requested": False,
            "visible": not self.settings.browser_headless,
            "cdp_host": "127.0.0.1",
            "cdp_port": evidence.get("cdp_port") or None,
            "worker": self.worker,
            "started_at": now,
            "heartbeat_at": now,
            "expires_at": now + timedelta(minutes=self.minutes),
            **_field_hold_fields(evidence),
        }
        self.store.c("human_holds").insert_one(doc)
        self.hold_id = doc["_id"]
        self._closed = False


_ASK_TEXT = ("ask_id", "host", "step", "key", "key_sig", "fingerprint")
_ASK_FLAGS = ("offer_code", "resend", "fix")


def _signin_ask(evidence: dict) -> dict:
    """The sign-in ask shown on the application page: the site, the step, and the task's one-time public key. No secret."""
    ask = evidence.get("signin")
    if not isinstance(ask, dict):
        return {}
    kept = {name: str(ask.get(name) or "")[:200] for name in _ASK_TEXT}
    kept.update({name: bool(ask.get(name)) for name in _ASK_FLAGS})
    return {"signin": kept}


def _field_hold_fields(evidence: dict) -> dict:
    """Label and difficulty for a field wait. The typed answer is not stored here."""
    if evidence.get("purpose") == "signin":
        return {"purpose": "signin", **_signin_ask(evidence)}
    if evidence.get("purpose") != "field":
        return {}
    return {
        "purpose": "field",
        "field_label": str(evidence.get("field_label") or "")[:120],
        "field_reason": str(evidence.get("field_reason") or "")[:300],
        "sensitive": bool(evidence.get("sensitive")),
        **_signin_ask(evidence),
    }


def active_hold(store: Store, application_id: str, max_age_seconds: int = HOLD_FRESH_SECONDS) -> dict | None:
    doc = store.c("human_holds").find_one({"application_id": application_id, "status": "holding"}, sort=[("started_at", -1)])
    if not doc:
        return None
    hb = doc.get("heartbeat_at")
    if hb is None or (utcnow() - hb).total_seconds() > max_age_seconds:
        return None
    return doc


def request_continue(store: Store, application: dict) -> str:
    """Ask the held page to be checked again. Same action, same approval, no new submit.

    Returns `ok`, `missing`, or `changed`.
    """
    hold = active_hold(store, application["_id"])
    if hold is None or hold.get("purpose") in ("field", "signin"):
        return "missing"
    if hold.get("material_hash") != application.get("material_hash"):
        return "changed"
    action = store.c("actions").find_one({"idempotency_key": hold.get("idempotency_key")})
    if action is None or action.get("material_hash") != application.get("material_hash"):
        return "changed"
    if action.get("_id") != hold.get("action_id"):
        return "changed"
    store.c("human_holds").update_one({"_id": hold["_id"], "status": "holding"}, {"$set": {"continue_requested": True, "continue_at": utcnow()}})
    return "ok"


def submit_field_answer(store: Store, application: dict, answer: str) -> str:
    """Save one dashboard answer on the open field hold. The same page types it. Returns ok, empty, missing, or changed.

    The answer stays on the hold so the browser can type it. It is not written to a run event here.
    """
    hold = active_hold(store, application["_id"])
    if hold is None or hold.get("purpose") != "field":
        return "missing"
    if hold.get("material_hash") != application.get("material_hash"):
        return "changed"
    action = store.c("actions").find_one({"idempotency_key": hold.get("idempotency_key")})
    if action is None or action.get("material_hash") != application.get("material_hash") or action.get("_id") != hold.get("action_id"):
        return "changed"
    text = " ".join(str(answer or "").split())[:200]
    if not text:
        return "empty"
    store.c("human_holds").update_one(
        {"_id": hold["_id"], "status": "holding"},
        {"$set": {"answer": text, "continue_requested": True, "continue_at": utcnow()}},
    )
    return "ok"


def signin_hold(store: Store, application: dict) -> tuple[dict | None, str]:
    """The open hold that carries a sign-in ask for this application, checked like every other answer. `ok`, `missing` or `changed`."""
    hold = active_hold(store, application["_id"])
    if hold is None or not isinstance(hold.get("signin"), dict) or not hold["signin"].get("ask_id"):
        return None, "missing"
    if hold.get("material_hash") != application.get("material_hash"):
        return None, "changed"
    action = store.c("actions").find_one({"idempotency_key": hold.get("idempotency_key")})
    if action is None or action.get("material_hash") != application.get("material_hash") or action.get("_id") != hold.get("action_id"):
        return None, "changed"
    return hold, "ok"


def reply_to_signin(store: Store, hold: dict, reply: dict) -> bool:
    """Hand one answer to the waiting task. Only the first answer to an ask is kept."""
    done = store.c("human_holds").update_one(
        {"_id": hold["_id"], "status": "holding", "continue_requested": {"$ne": True}, "signin.ask_id": hold["signin"]["ask_id"]},
        {"$set": {"signin_reply": reply, "continue_requested": True, "continue_at": utcnow()}},
    )
    return done.modified_count == 1


def note_unchanged_login(store: Store, hold: dict, why: str) -> None:
    """The saved login is the one the site refused. The page asks the owner a second question before it is sent again."""
    store.c("human_holds").update_one({"_id": hold["_id"], "status": "holding"}, {"$set": {"signin_unchanged": str(why)[:40]}})


def note_missed_approval(store: Store, hold: dict) -> int:
    """Count one missed approval window on this ask. Returns how many there have been."""
    doc = store.c("human_holds").find_one_and_update({"_id": hold["_id"], "status": "holding"}, {"$inc": {"signin_missed": 1}}, return_document=True)
    return int((doc or {}).get("signin_missed") or 0)


def launch_args(cdp_port: int) -> list[str]:
    """Chromium args. The debug port, when set, listens on localhost only."""
    args = ["--disable-dev-shm-usage"]
    if cdp_port:
        args.append(f"--remote-debugging-port={cdp_port}")
        args.append("--remote-debugging-address=127.0.0.1")
    return args
