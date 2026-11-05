"""Recruiter identities, duplicate checks, drafting rules, and channels.

Identity: a recipient has aliases (normalized LinkedIn profile URL, lowercase email). Names alone
never establish identity. First contact requires a reconciled history: prior conversations, pending
actions, imported records and excluded identities are all checked; unknown history blocks sending.

Drafts follow the profile's saved outreach rules ([outreach] in candidate/profile.toml: character limit, dash rule,
the kind of roles to ask about, the candidate's one line pitch); the validator is code, not a prompt. Sending happens
only through a configured supported channel (SMTP) under its own scope; LinkedIn messages/Connect notes are manual
handoffs with the exact text.
"""

from __future__ import annotations

import logging
import re
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Any

from amorehiremepls.candidate.profile import Profile
from amorehiremepls.core.actions import ActionFailed, ActionStore
from amorehiremepls.core.ai import AIRunner, ProviderUnavailable
from amorehiremepls.core.authorization import material_hash
from amorehiremepls.core.db import Store
from amorehiremepls.core.skills import OutreachInput
from amorehiremepls.core.util import new_id, normalize_employer, normalize_url, utcnow

log = logging.getLogger(__name__)

DASH_RE = re.compile(r"[-\u2010-\u2015\u2212]")
LAYOFF_RE = re.compile(r"(laid off|lay-?off|let go|downsiz|redundan|terminated|job loss)", re.I)
PLACEHOLDER_RE = re.compile(r"[\[{<][^\]}>]{1,40}[\]}>]|TODO|XXX|lorem", re.I)
# internal notes must never leak into a message: salary figures, filter/exclusion vocabulary, research notes
INTERNAL_RE = re.compile(
    r"(salary floor|\b[A-Z]{3}\s?\d{2,3}(,?\d{3})?\b|\b(C\$|\$|€|£)\s?\d{2,3}(,?\d{3})?\b|exclu(de|sion)|excluded employer|filter|research note|citation)",
    re.I,
)


def _ask_phrase_pattern(role_phrase: str) -> re.Pattern[str] | None:
    """Every meaningful word of the configured role phrase ("permanent backend roles") must appear in the note, in
    any order; "back end" and "backend" count as the same word."""
    filler = ("the", "and", "for", "any", "other", "roles", "role", "jobs", "positions")
    words = [w for w in re.sub(r"[^a-z0-9 ]", " ", (role_phrase or "").lower()).split() if len(w) > 2 and w not in filler]
    if not words:
        return None
    return re.compile("".join(rf"(?=.*{re.escape(w)})" for w in words), re.I | re.S)


def normalize_alias(value: str) -> str | None:
    v = (value or "").strip()
    if not v:
        return None
    if "@" in v and "/" not in v:
        return "email:" + v.lower()
    if "linkedin.com/in/" in v.lower():
        url = normalize_url(v)
        return "li:" + url.split("linkedin.com/in/", 1)[1].strip("/").lower()
    if v.startswith("li:") or v.startswith("email:"):
        return v.lower()
    return None


@dataclass
class ValidationResult:
    ok: bool
    problems: list[str]
    length: int


def validate_connect_note(text: str, applied_role: str | None, rules: dict[str, Any]) -> ValidationResult:
    problems = []
    max_chars = int(rules.get("connect_note_max_chars", 300))
    if len(text) >= max_chars:
        problems.append(f"{len(text)} characters; must be under {max_chars}")
    if rules.get("no_dashes", True) and DASH_RE.search(text):
        problems.append("contains a hyphen or dash")
    if rules.get("no_layoff_mention", True) and LAYOFF_RE.search(text):
        problems.append("mentions layoff")
    if PLACEHOLDER_RE.search(text):
        problems.append("contains a placeholder")
    if INTERNAL_RE.search(text):
        problems.append("leaks internal filters or notes")
    if rules.get("mention_applied_role", True):
        if applied_role:
            key = re.sub(r"[^a-z0-9 ]", " ", applied_role.lower()).split()
            if not any(k in text.lower() for k in key if len(k) > 3):
                problems.append("does not mention the role already applied to")
        elif re.search(r"\bapplied\b|my application", text, re.I):
            problems.append("claims an application that was not confirmed")
    ask = rules.get("ask_other_roles", rules.get("ask_other_permanent_backend_roles", True))
    if ask:
        phrase = (rules.get("role_phrase") or "").strip() or "other roles"
        pattern = _ask_phrase_pattern(phrase)
        squashed = re.sub(r"\bback end\b", "backend", text, flags=re.I)
        if "?" not in text or (pattern and not pattern.search(squashed)):
            problems.append(f"must ask whether {phrase} may fit")
    return ValidationResult(not problems, problems, len(text))


def validate_message(text: str, applied_role: str | None, rules: dict[str, Any]) -> ValidationResult:
    problems = []
    words = len(text.split())
    lo, hi = rules.get("message_words", [45, 75])
    if words > hi + 15:
        problems.append(f"{words} words; keep it about {lo} to {hi}")
    if DASH_RE.search(text):
        problems.append("contains a hyphen or dash")
    if LAYOFF_RE.search(text):
        problems.append("mentions layoff")
    if PLACEHOLDER_RE.search(text) or INTERNAL_RE.search(text):
        problems.append("contains placeholder or internal notes")
    if "?" not in text:
        problems.append("needs one easy question")
    if not applied_role and re.search(r"\bapplied\b|my application", text, re.I):
        problems.append("claims an application that was not confirmed")
    return ValidationResult(not problems, problems, len(text))


def template_connect_note(first_name: str, employer: str, applied_role: str | None, *, one_liner: str = "", role_phrase: str = "", sign_off_name: str = "") -> str:
    """Deterministic Connect note from the profile's outreach rules. ``one_liner`` is the candidate's own sentence about
    what they do ("I build backend services in Java and Python"); ``role_phrase`` names the roles to ask about
    ("permanent backend roles"); ``sign_off_name`` is the candidate's first name."""
    pitch = (one_liner or "").strip().rstrip(".")
    roles = (role_phrase or "").strip() or "other roles"
    thanks = f" Thanks, {sign_off_name.strip()}" if sign_off_name.strip() else " Thanks"
    if applied_role:
        about = f" {pitch}." if pitch else ""
        return f"Hi {first_name}, I recently applied for the {applied_role} role at {employer}.{about} Do you know of other {roles} at {employer} that may fit?{thanks}"
    lead = f"{pitch} and noticed" if pitch else "I noticed"
    return f"Hi {first_name}, {lead} you recruit for {employer}. Would you be open to a quick note about {roles} that may fit?{thanks}"


class RecipientStore:
    def __init__(self, store: Store, profile: Profile) -> None:
        self.store = store
        self.profile = profile
        self.coll = store.c("recipients")

    def upsert(
        self,
        *,
        name: str,
        employer: str,
        profile_url: str | None = None,
        email: str | None = None,
        remit: str = "",
        source_url: str | None = None,
        channel: str = "linkedin",
        provenance: str = "manual",
    ) -> dict:
        aliases = [a for a in (normalize_alias(profile_url or ""), normalize_alias(email or "")) if a]
        existing = self.coll.find_one({"aliases": {"$in": aliases}}) if aliases else None
        now = utcnow()
        if existing:
            merged = sorted(set(existing.get("aliases", [])) | set(aliases))
            self.coll.update_one(
                {"_id": existing["_id"]},
                {
                    "$set": {"aliases": merged, "last_checked_at": now, "remit": remit or existing.get("remit", ""), "updated_at": now},
                    "$addToSet": {"names": name, "sources": source_url or provenance},
                },
            )
            return self.coll.find_one({"_id": existing["_id"]})
        doc = {
            "_id": new_id("rcp"),
            "names": [name],
            "display_name": name,
            "employer": employer,
            "employer_norm": normalize_employer(employer),
            "aliases": aliases,
            "remit": remit,
            "channel": channel,
            "sources": [source_url or provenance],
            "identity_confidence": "verified" if aliases else "name_only",
            "status": "active",
            "created_at": now,
            "updated_at": now,
            "last_checked_at": now,
        }
        self.coll.insert_one(doc)
        return doc

    def import_history(self, *, alias: str, kind: str, when: str | None, note: str = "", source: str = "import") -> dict | None:
        """Record a prior conversation/connection/send from imported evidence (e.g. exported LinkedIn messages)."""
        a = normalize_alias(alias)
        if not a:
            return None
        rec = self.coll.find_one({"aliases": a})
        if rec is None:
            rec = {
                "_id": new_id("rcp"),
                "names": [],
                "display_name": alias,
                "employer": "",
                "employer_norm": "",
                "aliases": [a],
                "remit": "",
                "channel": "linkedin",
                "sources": [source],
                "identity_confidence": "verified",
                "status": "active",
                "created_at": utcnow(),
                "updated_at": utcnow(),
            }
            self.coll.insert_one(rec)
        self.store.c("outreach").insert_one(
            {"_id": new_id("msg"), "recipient_id": rec["_id"], "kind": kind, "status": "imported", "sent_at": when, "note": note, "source": source, "created_at": utcnow()}
        )
        return rec

    def exclusion_for(self, rec: dict) -> str | None:
        ex = self.profile.is_excluded_employer(rec.get("employer", ""))
        if ex and "outreach" in ex.applies_to:
            return f"employer excluded: {ex.name}"
        for rx in self.profile.preferences.recipient_exclusions:
            if rx.resolution != "resolved":
                continue  # unresolved identity exclusions never block by name
            targets = {a for a in (normalize_alias(rx.profile_url or ""), normalize_alias(rx.email or "")) if a}
            if targets & set(rec.get("aliases", [])):
                return f"recipient excluded: {rx.label}"
        if rec.get("status") in ("opted_out", "bounced", "excluded"):
            return f"recipient status {rec['status']}"
        return None

    def history_check(self, rec: dict) -> dict[str, Any]:
        """Reconcile prior contact across channels, imports and pending actions. Unknown history blocks first contact."""
        prior = list(self.store.c("outreach").find({"recipient_id": rec["_id"]}))
        pending = list(self.store.c("actions").find({"target_id": rec["_id"], "status": {"$in": ["ready", "executing", "needs_verification"]}}))
        reasons = []
        if rec.get("identity_confidence") == "name_only":
            reasons.append("identity not verified (no profile URL or email); names alone do not establish identity")
        if any(p.get("status") in ("sent", "imported", "replied") for p in prior):
            reasons.append("prior conversation exists")
        if pending:
            reasons.append("a pending action already targets this recipient")
        if any(p.get("kind") == "connection_request" and p.get("status") in ("sent", "imported", "pending") for p in prior):
            reasons.append("connection request already pending or sent")
        unresolved = [
            rx.label
            for rx in self.profile.preferences.recipient_exclusions
            if rx.resolution == "unresolved" and any(rx.label.lower() == n.split()[0].lower() for n in rec.get("names", []) if n)
        ]
        if unresolved:
            reasons.append(f"unresolved identity exclusion '{unresolved[0]}' shares this first name; reconcile before contacting")
        ex = self.exclusion_for(rec)
        if ex:
            reasons.append(ex)
        if not rec.get("history_reconciled_at") and not prior:
            reasons.append("contact history not yet reconciled (mark reconciled after checking LinkedIn/email)")
        return {"first_contact_allowed": not reasons, "reasons": reasons, "prior_count": len(prior), "pending_count": len(pending)}

    def mark_reconciled(self, recipient_id: str, note: str = "") -> None:
        self.coll.update_one({"_id": recipient_id}, {"$set": {"history_reconciled_at": utcnow(), "history_note": note}})
        self.store.audit("recipient_history_reconciled", recipient_id, note=note)


class OutreachService:
    def __init__(self, store: Store, profile: Profile, ai: AIRunner | None = None, smtp: dict | None = None) -> None:
        self.store = store
        self.profile = profile
        self.recipients = RecipientStore(store, profile)
        self.actions = ActionStore(store)
        self.ai = ai
        self.smtp = smtp or {}

    def _candidate_points(self) -> list[str]:
        """Grounded talking points derived from the private profile at runtime (never hardcoded)."""
        from amorehiremepls.candidate.profile import compute_experience

        points: list[str] = []
        exp = compute_experience(self.profile.employment)
        if exp.full_time_years_completed >= 1:
            fam = (self.profile.preferences.role_families[:1] or [self.profile.profession()])[0]
            points.append(f"{exp.full_time_years_completed} completed years of full time {fam} work")
        for e in self.profile.employment:
            if e.kind == "full_time":
                points.append(f"{e.title} at {e.employer}")
        skills = [s.name for s in self.profile.skills][:4]
        if skills:
            points.append("hands on with " + ", ".join(skills))
        return points[:4]

    def candidates_for_employer(self, employer: str) -> list[dict]:
        return list(self.recipients.coll.find({"employer_norm": normalize_employer(employer), "status": "active"}))

    def draft(self, recipient_id: str, kind: str, applied_role: str | None, employer: str | None = None) -> dict:
        rec = self.recipients.coll.find_one({"_id": recipient_id})
        rules = self.profile.preferences.outreach_rules
        first = (rec.get("display_name") or "there").split()[0]
        employer = employer or rec.get("employer", "")
        text, method = None, "template"
        if self.ai is not None:
            try:
                res = self.ai.run(
                    "draft_outreach",
                    OutreachInput(
                        recipient_first_name=first,
                        recipient_remit=rec.get("remit", ""),
                        employer=employer,
                        applied_role_title=applied_role,
                        channel="connect_note" if kind == "connection_request" else "message",
                        candidate_points=self._candidate_points(),
                        rules=rules,
                        candidate_first_name=self.profile.first_name(),
                        candidate_profession=self.profile.profession(),
                        candidate_pitch=str(rules.get("one_liner") or ""),
                    ),
                    fact_version=self.profile.version,
                    untrusted_fields=("recipient_remit",),
                )
                text, method = res.output["text"], "model"
            except ProviderUnavailable:
                pass
        validator = validate_connect_note if kind == "connection_request" else validate_message
        if text is not None:
            v = validator(text, applied_role, rules)
            if not v.ok:
                log.warning("model draft rejected: %s", v.problems)
                text, method = None, "template_after_rejection"
        if text is None:
            text = template_connect_note(
                first,
                employer,
                applied_role,
                one_liner=str(rules.get("one_liner") or ""),
                role_phrase=str(rules.get("role_phrase") or ""),
                sign_off_name=self.profile.first_name(),
            )
            v = validator(text, applied_role, rules)
        history = self.recipients.history_check(rec)
        doc = {
            "_id": new_id("msg"),
            "recipient_id": recipient_id,
            "kind": kind,
            "status": "draft",
            "text": text,
            "method": method,
            "applied_role": applied_role,
            "validation": {"ok": v.ok, "problems": v.problems, "length": v.length},
            "history": history,
            "created_at": utcnow(),
            "material_hash": material_hash(message=text, recipient=recipient_id, extra=kind),
        }
        self.store.c("outreach").insert_one(doc)
        return doc

    def send(self, message_id: str, worker: str, channel: str) -> dict:
        """Send through a configured supported channel under its own scope; otherwise a manual handoff."""
        msg = self.store.c("outreach").find_one({"_id": message_id})
        rec = self.recipients.coll.find_one({"_id": msg["recipient_id"]})
        history = self.recipients.history_check(rec)
        if msg["kind"] in ("outreach_first_contact", "connection_request") and not history["first_contact_allowed"]:
            return {"status": "blocked", "reasons": history["reasons"]}
        if not msg["validation"]["ok"]:
            return {"status": "blocked", "reasons": msg["validation"]["problems"]}
        if channel == "email":
            email_alias = next((a for a in rec.get("aliases", []) if a.startswith("email:")), None)
            if not email_alias:
                return {"status": "blocked", "reasons": ["no verified email address for this recipient; never invent one"]}
            if not self.smtp.get("host"):
                return {"status": "handoff", "reasons": ["SMTP not configured; copy the text and send manually"], "text": msg["text"]}
            action = self.actions.prepare("email_send", rec["_id"], f"email:{message_id}", {"message_id": message_id}, msg["material_hash"], host=self.smtp.get("host"))

            def adapter(_doc: dict) -> dict:
                first_name = self.profile.first_name()
                return self._smtp_send(email_alias.split(":", 1)[1], msg.get("subject") or (f"Hello from {first_name}" if first_name else "Hello"), msg["text"])

            out = self.actions.execute(action["_id"], worker, adapter)
            new_status = {"succeeded": "sent", "needs_verification": "needs_verification", "failed": "failed"}.get(out.status, msg["status"])
            self.store.c("outreach").update_one(
                {"_id": message_id},
                {"$set": {"status": new_status, "channel": "email", "sent_at": utcnow() if out.ok else None, "evidence": out.evidence, "action_id": out.action_id}},
            )
            return {"status": out.status, "reason": out.reason, "evidence": out.evidence}
        # LinkedIn messages / Connect notes have no supported API: manual handoff with exact text
        self.store.c("outreach").update_one({"_id": message_id}, {"$set": {"status": "handoff", "channel": channel}})
        return {
            "status": "handoff",
            "text": msg["text"],
            "recipient": rec.get("display_name"),
            "reasons": ["LinkedIn has no supported sending integration; paste this text manually and then record the send"],
        }

    def record_manual_send(self, message_id: str, confirmation: str) -> dict:
        self.store.c("outreach").update_one(
            {"_id": message_id}, {"$set": {"status": "sent", "sent_at": utcnow(), "evidence": {"basis": "explicit_user_confirmation", "confirmation": confirmation}}}
        )
        self.store.audit("outreach_manual_send_recorded", message_id)
        return self.store.c("outreach").find_one({"_id": message_id})

    def _smtp_send(self, to_addr: str, subject: str, body: str) -> dict:
        em = EmailMessage()
        em["From"], em["To"], em["Subject"] = self.smtp["from"], to_addr, subject
        em["Message-ID"] = make_msgid(domain=(self.smtp["from"].split("@")[-1] if "@" in self.smtp["from"] else None))
        em.set_content(body)
        try:
            with smtplib.SMTP(self.smtp["host"], int(self.smtp.get("port", 587)), timeout=30) as s:
                s.starttls()
                if self.smtp.get("username"):
                    s.login(self.smtp["username"], self.smtp.get("password", ""))
                refused = s.send_message(em)
        except smtplib.SMTPRecipientsRefused as exc:
            raise ActionFailed("recipient refused", {"refused": list(exc.recipients)}) from exc
        except (smtplib.SMTPException, OSError) as exc:
            # connection dropped after DATA could mean delivered: ambiguous
            from amorehiremepls.core.actions import AmbiguousOutcome

            raise AmbiguousOutcome(f"SMTP error: {type(exc).__name__}", {"to": to_addr}) from exc
        if refused:
            raise ActionFailed("some recipients refused", {"refused": list(refused)})
        return {"verified": True, "to": to_addr, "message_id": em["Message-ID"], "sent_at": utcnow().isoformat()}

    def company_list_after_submission(self, application_id: str) -> dict:
        """Per the saved rule: after a confirmed application, keep a recruiter list for that company."""
        app = self.store.c("applications").find_one({"_id": application_id})
        if not app or app.get("status") != "submitted":
            return {"error": "application not confirmed submitted"}
        job = self.store.c("jobs").find_one({"_id": app["job_id"]})
        if self.profile.is_excluded_employer(job["employer"]):
            return {"employer": job["employer"], "skipped": "employer excluded for outreach"}
        recs = self.candidates_for_employer(job["employer"])
        return {
            "employer": job["employer"],
            "applied_role": job["title"],
            "recipients": [{"id": r["_id"], "name": r["display_name"], "remit": r.get("remit"), "history": self.recipients.history_check(r)} for r in recs],
            "note": "recruiter discovery on LinkedIn is manual: add in-house recruiters/talent acquisition profiles here, then draft",
        }
