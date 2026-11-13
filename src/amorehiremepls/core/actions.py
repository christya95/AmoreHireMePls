"""External action lifecycle with persisted intent, idempotency keys and replay safety.

    prepare -> (authorization check) -> reserve (atomic) -> record intent -> adapter -> record outcome

Re-running `execute` for the same idempotency key never calls the adapter again: it returns the
outcome already saved (`outcome_reason` when one was recorded), or reports that another worker holds
the lease, or that the earlier attempt ended ambiguously (`needs_verification`) and must be reconciled
first. The replay label is only a fallback when nothing was saved.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta

from amorehiremepls.core.authorization import AuthorizationStore, Decision, ScopeKind
from amorehiremepls.core.db import Store
from amorehiremepls.core.util import new_id, utcnow

log = logging.getLogger(__name__)

# Used only when a finished action has no saved reason. Callers must not treat it as the failure.
REPLAY_LABEL = "recorded outcome (replay)"

ACTION_SCOPE: dict[str, ScopeKind] = {
    "application_submit": "application_submit",
    "outreach_first_contact": "outreach_first_contact",
    "connection_request": "connection_request",
    "follow_up": "follow_up",
    "email_send": "email_send",
    "owner_notification": "owner_notification",
    "site_account_create": "site_account_create",
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


class ActionHandoff(Exception):
    """The adapter stopped before anything irreversible happened and a person must continue (login wall, human
    check that gates the submit). The action returns to `ready`; the caller records the handoff and may resume later."""

    def __init__(self, reason: str, evidence: dict | None = None) -> None:
        super().__init__(reason)
        self.evidence = evidence or {}


@dataclass
class Outcome:
    status: str  # succeeded | failed | needs_verification | blocked | denied | in_progress | not_found
    reason: str = ""
    evidence: dict = field(default_factory=dict)
    action_id: str | None = None
    adapter_ran: bool = False
    retryable: bool = False  # failed before any irreversible step; the action is back in `ready`

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

    def retire_failed_attempt(self, action: dict) -> bool:
        """Keep a finished failure and free its key so a different set of answers can be tried.

        The old document stays. A later prepare with the original key inserts a new attempt."""
        if not action or action.get("status") != "failed" or not action.get("_id") or not action.get("idempotency_key"):
            return False
        prior = f"{action['idempotency_key']}:prior:{action['_id']}"
        result = self.coll.update_one(
            {"_id": action["_id"], "status": "failed"},
            {"$set": {"idempotency_key": prior, "superseded_at": utcnow(), "updated_at": utcnow()}},
        )
        return result.modified_count == 1

    def execute(
        self, action_id: str, owner: str, adapter: Callable[[dict], dict], *, job: dict | None = None, has_material_unknowns: bool = False, lease_seconds: int = 900
    ) -> Outcome:
        action = self.get(action_id)
        if action is None:
            return Outcome("not_found", "unknown action")
        terminal = {"succeeded", "failed", "needs_verification"}
        if action["status"] in terminal:
            return _recorded_outcome(action, action_id)
        if self.store.emergency_stop_active():
            self.coll.update_one({"_id": action_id}, {"$set": {"status": "blocked", "blocked_reason": "emergency stop", "updated_at": utcnow()}})
            return Outcome("blocked", "emergency stop active", action_id=action_id)
        decision: Decision = self.auth.check(
            ACTION_SCOPE[action["kind"]], action["target_id"], action["material_hash"], host=action.get("host"), job=job, has_material_unknowns=has_material_unknowns
        )
        if not decision.allowed:
            self.store.audit("action_denied", action_id, reason=decision.reason)
            return Outcome("denied", decision.reason, action_id=action_id)
        reserved = self.store.reserve(
            "actions", {"_id": action_id}, owner, ["ready", "blocked"], "executing", lease_seconds, extra={"scope_id": decision.scope_id}, allow_takeover=False
        )
        if reserved is None:
            current = self.get(action_id) or {}
            if current.get("status") in terminal:
                return _recorded_outcome(current, action_id)
            if current.get("status") == "executing" and current.get("reserved_until") and current["reserved_until"] < utcnow():
                # Another worker's lease expired mid-execution. The side effect may or may not have happened:
                # never take it over and run the adapter again.
                swept = self._expire_lease(current)
                if swept:
                    return Outcome("needs_verification", swept["outcome_reason"], swept.get("evidence", {}), action_id)
                current = self.get(action_id) or {}
                return _recorded_outcome(current, action_id)
            return Outcome("in_progress", f"held by {current.get('reserved_by')}", action_id=action_id)
        attempt = {"owner": owner, "started_at": utcnow()}
        self.coll.update_one({"_id": action_id}, {"$push": {"attempts": attempt}})
        retryable = False
        try:
            evidence = adapter(reserved)
            if not isinstance(evidence, dict) or not evidence.get("verified"):
                raise AmbiguousOutcome("adapter returned no verified evidence", evidence if isinstance(evidence, dict) else {})
            status, reason = "succeeded", "verified evidence recorded"
        except AmbiguousOutcome as exc:
            evidence, status, reason = exc.evidence, "needs_verification", str(exc)
        except ActionHandoff as exc:
            # nothing was sent: hand the action back as `ready` with the evidence, and let the caller interrupt for a person
            self.store.release("actions", {"_id": action_id}, owner, "ready", extra={"handoff": exc.evidence, "outcome_reason": str(exc), "finished_at": utcnow()})
            self.store.audit("action_handoff", action_id, reason=str(exc))
            raise
        except ActionFailed as exc:
            evidence, status, reason, retryable = exc.evidence, "failed", str(exc), exc.retryable
        except Exception as exc:  # unknown exception after the adapter may have acted: never assume it did not
            log.exception("adapter raised; recording needs_verification")
            evidence, status, reason = {"exception": type(exc).__name__}, "needs_verification", f"unexpected error: {exc}"
        # A retryable failure happened before any irreversible step: the action returns to `ready` keeping
        # its attempts, so a later execute() can run the adapter again under a fresh authorization check.
        stored_status = "ready" if retryable else status
        released = self.store.release(
            "actions",
            {"_id": action_id},
            owner,
            stored_status,
            extra={"evidence": evidence, "outcome_reason": reason, "finished_at": utcnow(), "last_failure": reason if retryable else None},
        )
        if released is None:
            # Our lease expired and was swept to needs_verification while the adapter ran. Keep that state
            # (a human must reconcile) but attach what we observed.
            self.coll.update_one({"_id": action_id}, {"$set": {"late_outcome": {"owner": owner, "status": status, "reason": reason, "evidence": evidence, "at": utcnow()}}})
            self.store.audit("action_late_outcome", action_id, status=status, owner=owner)
            return Outcome("needs_verification", "lease expired during execution; late result recorded for reconciliation", evidence, action_id, adapter_ran=True)
        self.auth.consume(decision.scope_id, "failed_retryable" if retryable else status)
        self.store.audit("action_finished", action_id, status=status, reason=reason, retryable=retryable)
        return Outcome(status, reason, evidence, action_id, adapter_ran=True, retryable=retryable)

    def _expire_lease(self, action: dict) -> dict | None:
        """Move one `executing` action whose lease expired to needs_verification (atomic on the lease)."""
        from pymongo import ReturnDocument

        reason = f"worker lease expired while executing (held by {action.get('reserved_by')}); outcome unknown"
        swept = self.coll.find_one_and_update(
            {"_id": action["_id"], "status": "executing", "reserved_until": {"$lt": utcnow()}},
            {
                "$set": {"status": "needs_verification", "outcome_reason": reason, "lease_expired": True, "updated_at": utcnow()},
                "$unset": {"reserved_by": "", "reserved_until": ""},
            },
            return_document=ReturnDocument.AFTER,
        )
        if swept:
            self.store.audit("action_lease_expired", action["_id"], owner=action.get("reserved_by"))
        return swept

    def extend_lease(self, action_id: str, owner: str, seconds: int = 180) -> None:
        """Keep an in-progress action from being swept while a human-check hold is still watching the same page."""
        now = utcnow()
        self.coll.update_one(
            {"_id": action_id, "status": "executing", "reserved_by": owner},
            {"$set": {"reserved_until": now + timedelta(seconds=seconds), "updated_at": now}},
        )

    def sweep_expired_leases(self) -> int:
        """Worker housekeeping: expired `executing` leases become needs_verification, never re-runs."""
        n = 0
        for doc in self.coll.find({"status": "executing", "reserved_until": {"$lt": utcnow()}}):
            if self._expire_lease(doc):
                n += 1
        return n

    def prior_terminal_for_target(self, kind: str, target_id: str) -> dict | None:
        """Any earlier action of this kind for the target that succeeded or is unresolved blocks new attempts."""
        return self.coll.find_one({"kind": kind, "target_id": target_id, "status": {"$in": ["succeeded", "needs_verification"]}})

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


def _recorded_outcome(action: dict, action_id: str) -> Outcome:
    """The attempt already finished. Keep the reason that was saved with it."""
    saved = str(action.get("outcome_reason") or "").strip()
    evidence = action.get("evidence") or {}
    return Outcome(action.get("status") or "in_progress", saved or REPLAY_LABEL, evidence, action_id)
