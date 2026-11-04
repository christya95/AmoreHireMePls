"""External action lifecycle with persisted intent, idempotency keys and replay safety.

    prepare -> (authorization check) -> reserve (atomic) -> record intent -> adapter -> record outcome

Re-running `execute` for the same idempotency key never calls the adapter again: it returns the
recorded outcome, or reports that another worker holds the lease, or that the earlier attempt ended
ambiguously (`needs_verification`) and must be reconciled first.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from .authorization import AuthorizationStore, Decision, ScopeKind
from .db import Store
from .util import new_id, utcnow

log = logging.getLogger(__name__)

ACTION_SCOPE: dict[str, ScopeKind] = {
    "application_submit": "application_submit",
    "outreach_first_contact": "outreach_first_contact",
    "connection_request": "connection_request",
    "follow_up": "follow_up",
    "email_send": "email_send",
}


class AmbiguousOutcome(Exception):
    """Adapter could not tell whether the side effect happened (timeout after final click, etc.)."""

    def __init__(self, message: str, evidence: dict | None = None) -> None:
        super().__init__(message)
        self.evidence = evidence or {}


class ActionFailed(Exception):
    def __init__(self, message: str, evidence: dict | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.evidence = evidence or {}
        self.retryable = retryable


@dataclass
class Outcome:
    status: str  # succeeded | failed | needs_verification | blocked | denied | in_progress | not_found
    reason: str = ""
    evidence: dict = field(default_factory=dict)
    action_id: str | None = None
    adapter_ran: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "succeeded"


class ActionStore:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.coll = store.c("actions")
        self.auth = AuthorizationStore(store)

    def prepare(self, kind: str, target_id: str, idempotency_key: str, payload: dict, material_hash: str, host: str | None = None, run_id: str | None = None) -> dict:
        """Persist intent. Idempotent on `idempotency_key`."""
        existing = self.coll.find_one({"idempotency_key": idempotency_key})
        if existing:
            if existing.get("material_hash") != material_hash and existing["status"] in ("ready", "pending"):
                self.coll.update_one({"_id": existing["_id"]}, {"$set": {"material_hash": material_hash, "payload": payload, "updated_at": utcnow()}})
                existing = self.coll.find_one({"_id": existing["_id"]})
            return existing
        doc = {
            "_id": new_id("act"),
            "kind": kind,
            "target_id": target_id,
            "idempotency_key": idempotency_key,
            "payload": payload,
            "material_hash": material_hash,
            "host": host,
            "status": "ready",
            "run_id": run_id,
            "attempts": [],
            "created_at": utcnow(),
            "updated_at": utcnow(),
        }
        if not self.store.insert_unique("actions", doc):
            return self.coll.find_one({"idempotency_key": idempotency_key})
        self.store.audit("action_prepared", doc["_id"], kind=kind, target=target_id)
        return doc

    def get(self, action_id: str) -> dict | None:
        return self.coll.find_one({"_id": action_id})

    def by_key(self, idempotency_key: str) -> dict | None:
        return self.coll.find_one({"idempotency_key": idempotency_key})

    def execute(
        self, action_id: str, owner: str, adapter: Callable[[dict], dict], *, job: dict | None = None, has_material_unknowns: bool = False, lease_seconds: int = 900
    ) -> Outcome:
        action = self.get(action_id)
        if action is None:
            return Outcome("not_found", "unknown action")
        terminal = {"succeeded", "failed", "needs_verification"}
        if action["status"] in terminal:
            return Outcome(action["status"], "recorded outcome (replay)", action.get("evidence", {}), action_id)
        if self.store.emergency_stop_active():
            self.coll.update_one({"_id": action_id}, {"$set": {"status": "blocked", "blocked_reason": "emergency stop", "updated_at": utcnow()}})
            return Outcome("blocked", "emergency stop active", action_id=action_id)
        decision: Decision = self.auth.check(
            ACTION_SCOPE[action["kind"]], action["target_id"], action["material_hash"], host=action.get("host"), job=job, has_material_unknowns=has_material_unknowns
        )
        if not decision.allowed:
            self.store.audit("action_denied", action_id, reason=decision.reason)
            return Outcome("denied", decision.reason, action_id=action_id)
        reserved = self.store.reserve("actions", {"_id": action_id}, owner, ["ready", "blocked"], "executing", lease_seconds, extra={"scope_id": decision.scope_id})
        if reserved is None:
            current = self.get(action_id) or {}
            if current.get("status") in terminal:
                return Outcome(current["status"], "recorded outcome (replay)", current.get("evidence", {}), action_id)
            return Outcome("in_progress", f"held by {current.get('reserved_by')}", action_id=action_id)
        attempt = {"owner": owner, "started_at": utcnow()}
        self.coll.update_one({"_id": action_id}, {"$push": {"attempts": attempt}})
        try:
            evidence = adapter(reserved)
            if not isinstance(evidence, dict) or not evidence.get("verified"):
                raise AmbiguousOutcome("adapter returned no verified evidence", evidence if isinstance(evidence, dict) else {})
            status, reason = "succeeded", "verified evidence recorded"
        except AmbiguousOutcome as exc:
            evidence, status, reason = exc.evidence, "needs_verification", str(exc)
        except ActionFailed as exc:
            evidence, status, reason = exc.evidence, "failed", str(exc)
        except Exception as exc:  # unknown exception after the adapter may have acted: never assume it did not
            log.exception("adapter raised; recording needs_verification")
            evidence, status, reason = {"exception": type(exc).__name__}, "needs_verification", f"unexpected error: {exc}"
        self.store.release("actions", {"_id": action_id}, owner, status, extra={"evidence": evidence, "outcome_reason": reason, "finished_at": utcnow()})
        self.auth.consume(decision.scope_id, status)
        self.store.audit("action_finished", action_id, status=status, reason=reason)
        return Outcome(status, reason, evidence, action_id, adapter_ran=True)

    def reconcile(self, action_id: str, resolved_status: str, evidence: dict, by: str = "user") -> None:
        """Resolve a needs_verification action after inspecting the external state."""
        if resolved_status not in ("succeeded", "failed"):
            raise ValueError("resolved_status must be succeeded or failed")
        self.coll.update_one(
            {"_id": action_id, "status": "needs_verification"}, {"$set": {"status": resolved_status, "evidence": evidence, "reconciled_by": by, "reconciled_at": utcnow()}}
        )
        self.store.audit("action_reconciled", action_id, status=resolved_status, by=by)

    def pending_verification(self) -> list[dict]:
        return list(self.coll.find({"status": "needs_verification"}))
