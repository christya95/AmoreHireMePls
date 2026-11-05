"""Authorization scopes for external actions. Fail closed.

A scope binds an action kind to a target (job or recipient) or a bounded batch filter, plus the
material content hash (answers + document hashes + message). Changing material content invalidates
the match. Scopes expire, have maximum counts, and stop conditions. Distinct kinds exist for
application submission, first contact, connection requests, follow-ups and email sends.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

from amorehiremepls.core.db import Store
from amorehiremepls.core.util import new_id, sha256_json, utcnow

ScopeKind = Literal["application_submit", "outreach_first_contact", "connection_request", "follow_up", "email_send", "owner_notification"]


class ScopeBinding(BaseModel):
    target_id: str | None = None  # exact job_id or recipient_id (single mode)
    material_hash: str | None = None  # required in single mode
    allowed_hosts: list[str] = Field(default_factory=list)
    allowed_actions: list[str] = Field(default_factory=list)
    job_filters: dict[str, Any] = Field(default_factory=dict)  # batch mode: e.g. {"employers": [...], "max_fit_rank": 3}


class StopConditions(BaseModel):
    stop_on_needs_verification: bool = True
    max_failures: int = 1
    stop_on_material_unknowns: bool = True


class Scope(BaseModel):
    id: str = Field(default_factory=lambda: new_id("scope"))
    kind: ScopeKind
    mode: Literal["single", "batch"] = "single"
    binding: ScopeBinding
    max_count: int = 1
    used_count: int = 0
    failures: int = 0
    status: Literal["active", "consumed", "revoked", "expired", "stopped"] = "active"
    expires_at: datetime
    stop: StopConditions = Field(default_factory=StopConditions)
    created_by: str = "user"
    created_at: datetime = Field(default_factory=utcnow)
    note: str = ""


class Decision(BaseModel):
    allowed: bool
    reason: str
    scope_id: str | None = None


def material_hash(answers: Any = None, document_hashes: list[str] | None = None, message: str | None = None, recipient: str | None = None, extra: Any = None) -> str:
    return sha256_json({"answers": answers, "documents": sorted(document_hashes or []), "message": message, "recipient": recipient, "extra": extra})


class AuthorizationStore:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.coll = store.c("authorizations")

    def grant(
        self,
        kind: ScopeKind,
        binding: ScopeBinding,
        *,
        mode: str = "single",
        max_count: int = 1,
        ttl: timedelta = timedelta(hours=24),
        created_by: str = "user",
        note: str = "",
        stop: StopConditions | None = None,
    ) -> Scope:
        if mode == "single" and (not binding.target_id or not binding.material_hash):
            raise ValueError("single-action scope requires target_id and material_hash")
        if mode == "batch":
            if kind != "application_submit":
                raise ValueError("batch scopes exist only for application_submit; outreach, email and connection requests need per-action approval")
            if not binding.job_filters:
                raise ValueError("batch scope requires job filters (employers, sites or max_fit_rank)")
        scope = Scope(kind=kind, mode=mode, binding=binding, max_count=max_count, expires_at=utcnow() + ttl, created_by=created_by, note=note, stop=stop or StopConditions())
        self.coll.insert_one({"_id": scope.id, **scope.model_dump(exclude={"id"})})
        self.store.audit("authorization_granted", scope.id, kind=kind, mode=mode, target=binding.target_id, max_count=max_count)
        return scope

    def get(self, scope_id: str) -> Scope | None:
        doc = self.coll.find_one({"_id": scope_id})
        if not doc:
            return None
        doc["id"] = doc.pop("_id")
        return Scope.model_validate(doc)

    def revoke(self, scope_id: str, reason: str = "") -> None:
        self.coll.update_one({"_id": scope_id}, {"$set": {"status": "revoked", "revoked_reason": reason}})
        self.store.audit("authorization_revoked", scope_id, reason=reason)

    def active(self, kind: ScopeKind | None = None) -> list[Scope]:
        q: dict[str, Any] = {"status": "active", "expires_at": {"$gt": utcnow()}}
        if kind:
            q["kind"] = kind
        out = []
        for doc in self.coll.find(q):
            doc["id"] = doc.pop("_id")
            out.append(Scope.model_validate(doc))
        return out

    def check(self, kind: ScopeKind, target_id: str, material: str, host: str | None = None, job: dict | None = None, has_material_unknowns: bool = False) -> Decision:
        """Find one active scope that authorizes exactly this action. Any mismatch denies."""
        # Alerts go only to the address configured for this install (the owner), never to an employer or recruiter.
        # Configuring that address is the authorization; there is no per-job approval to consume.
        if kind == "owner_notification":
            return Decision(allowed=True, reason="owner alert", scope_id=None)
        now = utcnow()
        for scope in self.active(kind):
            if scope.expires_at <= now:
                continue
            if scope.used_count >= scope.max_count:
                continue
            b = scope.binding
            if host and b.allowed_hosts and host.lower() not in {h.lower() for h in b.allowed_hosts}:
                continue
            if scope.mode == "single":
                if b.target_id != target_id:
                    continue
                if b.material_hash != material:
                    return Decision(allowed=False, reason="material content changed since authorization; renew it", scope_id=scope.id)
                return Decision(allowed=True, reason="exact single-action authorization", scope_id=scope.id)
            # batch: only application submissions can be batch-authorized, and only against a concrete job
            # that matches the scope's filters. Without a job there is nothing to match, so deny.
            if kind != "application_submit" or job is None:
                continue
            if has_material_unknowns and scope.stop.stop_on_material_unknowns:
                continue
            if not _job_matches(job, b.job_filters):
                continue
            if b.target_id and b.target_id != target_id:
                continue
            return Decision(allowed=True, reason="bounded batch authorization", scope_id=scope.id)
        return Decision(allowed=False, reason="no matching active authorization")

    def consume(self, scope_id: str, outcome: str) -> None:
        if outcome == "failed_retryable":
            # nothing irreversible happened and the action is back in `ready`; the scope stays usable, but
            # repeated transient failures stop it so a retry loop cannot run unattended.
            doc = self.coll.find_one_and_update({"_id": scope_id}, {"$inc": {"retryable_failures": 1}}, return_document=True)
            if doc and doc.get("retryable_failures", 0) >= 3:
                self.coll.update_one({"_id": scope_id}, {"$set": {"status": "stopped", "stopped_reason": "repeated retryable failures"}})
            return
        inc = {"used_count": 1}
        if outcome in ("failed",):
            inc["failures"] = 1
        doc = self.coll.find_one_and_update({"_id": scope_id}, {"$inc": inc}, return_document=True)
        if not doc:
            return
        new_status = None
        if doc["used_count"] >= doc["max_count"]:
            new_status = "consumed"
        if outcome == "needs_verification" and doc["stop"]["stop_on_needs_verification"]:
            new_status = "stopped"
        if doc.get("failures", 0) >= doc["stop"]["max_failures"] and outcome == "failed":
            new_status = "stopped"
        if new_status:
            self.coll.update_one({"_id": scope_id}, {"$set": {"status": new_status, "stopped_reason": outcome}})

    def expire_stale(self) -> int:
        return self.coll.update_many({"status": "active", "expires_at": {"$lte": utcnow()}}, {"$set": {"status": "expired"}}).modified_count


def _job_matches(job: dict, filters: dict[str, Any]) -> bool:
    if not filters:
        return False  # a batch scope must say what it covers
    employers = filters.get("employers")
    if employers and job.get("employer_norm") not in {e.lower() for e in employers} and job.get("employer") not in employers:
        return False
    max_rank = filters.get("max_fit_rank")
    if max_rank is not None and (job.get("fit", {}).get("rank") is None or job["fit"]["rank"] > max_rank):
        return False
    hosts = filters.get("sites")
    if hosts:
        from urllib.parse import urlsplit

        host = urlsplit(job.get("apply_url") or job.get("posting_url") or "").netloc.lower()
        if host not in {h.lower() for h in hosts}:
            return False
    job_ids = filters.get("job_ids")
    if job_ids and job.get("_id") not in job_ids:
        return False
    return True
