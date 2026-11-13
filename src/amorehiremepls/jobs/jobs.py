"""Job identity, dedup keys and the jobs collection.

A job is one employer requisition. Dedup keys (all stored on the document, unique multikey index):
  li:<linkedin_job_id>            LinkedIn job ID
  req:<employer_norm>:<req_id>    employer + ATS/requisition ID
  url:<normalized_url>            canonical posting URL
Conservative fallback: fp:<employer_norm>:<title_norm> is recorded as a *soft* key used only to flag
possible duplicates for review, never to merge automatically.
"""

from __future__ import annotations

from typing import Any

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from amorehiremepls.core.db import Store
from amorehiremepls.core.util import extract_linkedin_job_id, new_id, normalize_employer, normalize_text, normalize_url, utcnow


def dedup_keys(employer: str, posting_url: str | None, requisition_id: str | None, linkedin_job_id: str | None) -> list[str]:
    keys: list[str] = []
    emp = normalize_employer(employer or "")
    li = (linkedin_job_id or "").strip() or (extract_linkedin_job_id(posting_url or "") if posting_url else None)
    if li:
        keys.append(f"li:{li}")
    if requisition_id and emp:
        keys.append(f"req:{emp}:{requisition_id.strip().lower()}")
    if posting_url:
        url = normalize_url(posting_url)
        if url and "linkedin.com" not in url:  # LinkedIn URLs are covered by the li: key
            keys.append(f"url:{url}")
        elif url and not li:
            keys.append(f"url:{url}")
    return sorted(set(keys))


def posting_changed(old: dict | None, new: dict) -> bool:
    """True when a posting read again says something else than the stored one: other text, or other details from the
    source. The time it was read does not count."""
    if not old:
        return True
    return (old.get("posting_hash"), old.get("extracted") or {}) != (new.get("posting_hash"), new.get("extracted") or {})


def soft_key(employer: str, title: str) -> str:
    return f"fp:{normalize_employer(employer)}:{normalize_text(title)}"


class JobStore:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.coll = store.c("jobs")

    def find_by_keys(self, keys: list[str]) -> dict | None:
        if not keys:
            return None
        return self.coll.find_one({"dedup_keys": {"$in": keys}})

    def upsert(
        self,
        *,
        employer: str,
        title: str,
        posting_url: str | None,
        requisition_id: str | None = None,
        linkedin_job_id: str | None = None,
        location: str | None = None,
        source: str = "manual",
        snapshot: dict | None = None,
        extra: dict | None = None,
    ) -> tuple[dict, bool]:
        """Create or merge a job. Returns (job, created). Key collisions merge into the existing job."""
        keys = dedup_keys(employer, posting_url, requisition_id, linkedin_job_id)
        existing = self.find_by_keys(keys)
        now = utcnow()
        if existing:
            merged_keys = sorted(set(existing.get("dedup_keys", [])) | set(keys))
            # Another job may already own one of the new keys; never merge two distinct jobs silently.
            clash = self.coll.find_one({"_id": {"$ne": existing["_id"]}, "dedup_keys": {"$in": merged_keys}})
            # `update` holds what makes this a different job than the one stored: a new identity, a first value for an
            # empty field, a changed posting. Only that moves the job up the list (updated_at).
            update: dict[str, Any] = {}
            if not clash:
                if set(merged_keys) != set(existing.get("dedup_keys", [])):
                    update["dedup_keys"] = merged_keys
            else:
                flag = {"reason": "dedup key clash", "other_job_id": clash["_id"]}
                if existing.get("needs_reconciliation") != flag:
                    update["needs_reconciliation"] = flag
            for k, v in (("requisition_id", requisition_id), ("linkedin_job_id", linkedin_job_id), ("location", location)):
                if v and not existing.get(k):
                    update[k] = v
            if snapshot and posting_changed(existing.get("latest_snapshot"), snapshot):
                update["latest_snapshot"] = snapshot
            if update:
                update["updated_at"] = now
            # Seen again with the same posting: when it was seen and the source's own details are kept, and the job
            # stays where it was in the list.
            quiet = {k: v for k, v in (extra or {}).items() if existing.get(k) != v}
            quiet["last_seen_at"] = now
            doc = self.coll.find_one_and_update({"_id": existing["_id"]}, {"$set": {**quiet, **update}, "$addToSet": {"sources": source}}, return_document=ReturnDocument.AFTER)
            return doc, False
        doc = {
            "_id": new_id("job"),
            "employer": employer,
            "employer_norm": normalize_employer(employer),
            "title": title,
            "title_norm": normalize_text(title),
            "posting_url": posting_url,
            "requisition_id": requisition_id or None,
            "linkedin_job_id": linkedin_job_id or extract_linkedin_job_id(posting_url or "") if posting_url else linkedin_job_id,
            "location": location,
            "dedup_keys": keys,
            "soft_key": soft_key(employer, title),
            "sources": [source],
            "status": "new",
            "created_at": now,
            "updated_at": now,
            "latest_snapshot": snapshot,
            **(extra or {}),
        }
        try:
            self.coll.insert_one(doc)
            return doc, True
        except DuplicateKeyError:
            # Lost a race with a concurrent insert: fall back to merge.
            return self.upsert(
                employer=employer,
                title=title,
                posting_url=posting_url,
                requisition_id=requisition_id,
                linkedin_job_id=linkedin_job_id,
                location=location,
                source=source,
                snapshot=snapshot,
                extra=extra,
            )

    def possible_duplicates(self, job: dict) -> list[dict]:
        return list(self.coll.find({"_id": {"$ne": job["_id"]}, "soft_key": job.get("soft_key")}))

    def get(self, job_id: str) -> dict | None:
        return self.coll.find_one({"_id": job_id})

    def set_status(self, job_id: str, status: str, **fields: Any) -> None:
        self.coll.update_one({"_id": job_id}, {"$set": {"status": status, "updated_at": utcnow(), **fields}})
