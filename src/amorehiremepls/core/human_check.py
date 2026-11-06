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

    def heartbeat(self, evidence: dict | None = None) -> None:
        evidence = evidence or {}
        if self.hold_id is None:
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

    def finish(self, status: str) -> None:
        if not self.hold_id:
            return
        self.store.c("human_holds").update_one({"_id": self.hold_id, "status": "holding"}, {"$set": {"status": status, "finished_at": utcnow()}})

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


def _field_hold_fields(evidence: dict) -> dict:
    """Label and difficulty for a field wait. The typed answer is not stored here."""
    if evidence.get("purpose") != "field":
        return {}
    return {
        "purpose": "field",
        "field_label": str(evidence.get("field_label") or "")[:120],
        "field_reason": str(evidence.get("field_reason") or "")[:300],
        "sensitive": bool(evidence.get("sensitive")),
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
    if hold is None or hold.get("purpose") == "field":
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


def launch_args(cdp_port: int) -> list[str]:
    """Chromium args. The debug port, when set, listens on localhost only."""
    args = ["--disable-dev-shm-usage"]
    if cdp_port:
        args.append(f"--remote-debugging-port={cdp_port}")
        args.append("--remote-debugging-address=127.0.0.1")
    return args
