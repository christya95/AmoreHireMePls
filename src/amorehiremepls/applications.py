"""Application preparation and execution.

prepare(job)  -> tailored artifact + answers from confirmed facts -> review package (+ material hash)
execute(app)  -> idempotent action, authorization check, site adapter plan, verified receipt or
                 needs_verification / failed / handoff.  Never marks `submitted` without evidence.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .actions import ActionStore, Outcome
from .authorization import material_hash
from .browser import HandoffRequired, Plan, normalize_screenshots
from .db import Store
from .jobs import JobStore
from .profile import BandEvidence, Profile, compute_experience, desired_salary
from .tailoring import Tailor
from .util import new_id, sha256_file, to_toronto, utcnow

log = logging.getLogger(__name__)

SENSITIVE = re.compile(r"(gender|race|ethnic|disab|veteran|sexual orientation|religion|pronoun|date of birth|age\b|marital|citizenship status|criminal)", re.I)
FIELD_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bfirst\s*name|given name", re.I), "legal_first_name"),
    (re.compile(r"\blast\s*name|surname|family name", re.I), "legal_last_name"),
    (re.compile(r"preferred name|nickname", re.I), "preferred_name"),
    (re.compile(r"\be-?mail", re.I), "email"),
    (re.compile(r"\bphone|mobile|telephone", re.I), "phone"),
    (re.compile(r"address line ?1|street|^address$", re.I), "address_line1"),
    (re.compile(r"\bcity\b|town", re.I), "city"),
    (re.compile(r"province|state|region", re.I), "province"),
    (re.compile(r"postal|zip", re.I), "postal_code"),
    (re.compile(r"\bcountry\b", re.I), "country"),
    (re.compile(r"linkedin", re.I), "linkedin_url"),
    (re.compile(r"authoriz|eligible to work|legally (able|entitled) to work|work permit", re.I), "work_authorization"),
    (re.compile(r"sponsor", re.I), "requires_sponsorship"),
    (re.compile(r"salary|compensation|pay expectation|expected pay", re.I), "__salary__"),
    (re.compile(r"resume|cv\b|curriculum", re.I), "__resume__"),
    (re.compile(r"degree|education level|highest level", re.I), "degree_level"),
    (re.compile(r"school|university|college|institution", re.I), "school"),
    (re.compile(r"years? of (professional |software |backend )?experience", re.I), "__years_full_time__"),
    (re.compile(r"total years|years of experience", re.I), "__years_total__"),
    (re.compile(r"employment type|job type|looking for", re.I), "employment_preference"),
    (re.compile(r"ai (tools|workflow)|copilot", re.I), "ai_workflow"),
]
YES_WORDS = ("yes", "authorized", "authorised", "eligible", "i am", "no sponsorship", "do not require")
NO_WORDS = ("no", "not ", "require sponsorship", "need sponsorship")


@dataclass
class FormField:
    id: str
    label: str
    type: str = "text"  # text | select | checkbox | file | textarea | radio
    options: list[str] = field(default_factory=list)
    selector: str = ""
    required: bool = False


@dataclass
class Answer:
    field_id: str
    label: str
    value: str | None
    fact_ids: list[str]
    needs_input: bool
    reason: str = ""
    sensitive: bool = False

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def _pick_option(options: list[str], want_yes: bool) -> str | None:
    for o in options:
        low = o.lower()
        if want_yes and any(w in low for w in YES_WORDS) and not any(w in low for w in ("not ", "need", "require sponsorship")):
            return o
        if not want_yes and (low.strip() == "no" or any(w in low for w in ("not ", "do not"))):
            return o
    return None


def map_answers(
    fields: list[FormField], profile: Profile, job: dict, resume_path: str | None, band: BandEvidence | None = None, fit: str = "fair", as_of: date | None = None
) -> list[Answer]:
    answers: list[Answer] = []
    exp = compute_experience(profile.employment, as_of)
    for f in fields:
        label = f.label or f.id
        if SENSITIVE.search(label):
            answers.append(Answer(f.id, label, None, [], needs_input=f.required, reason="sensitive disclosure: left blank unless Joss supplies a preference", sensitive=True))
            continue
        key = next((k for pat, k in FIELD_RULES if pat.search(label)), None)
        if key is None:
            answers.append(Answer(f.id, label, None, [], needs_input=True, reason="no confirmed fact maps to this question"))
            continue
        if key == "__resume__":
            answers.append(Answer(f.id, label, resume_path, ["artifact"], needs_input=resume_path is None, reason="" if resume_path else "no tailored resume yet"))
            continue
        if key == "__salary__":
            d = desired_salary(profile.compensation, band, fit)  # type: ignore[arg-type]
            if d.outcome == "answer":
                answers.append(Answer(f.id, label, str(d.amount), ["compensation_rule"], needs_input=False, reason=d.reason))
            else:
                answers.append(Answer(f.id, label, None, [], needs_input=True, reason=f"salary rule: {d.reason}"))
            continue
        if key in ("__years_full_time__", "__years_total__"):
            years = exp.full_time_years_completed if key == "__years_full_time__" else exp.total_years_completed
            answers.append(
                Answer(
                    f.id,
                    label,
                    str(years),
                    [e.id for e in profile.employment],
                    needs_input=bool(exp.unknown_ranges and key == "__years_total__"),
                    reason="completed years computed from dates" + ("; some ranges unknown" if exp.unknown_ranges else ""),
                )
            )
            continue
        fact = profile.fact(key)
        value = fact.value if fact else (profile.preferences.work_authorization if key == "work_authorization" else None)
        if key in ("work_authorization", "requires_sponsorship") and f.options:
            want_yes = key == "work_authorization"
            opt = _pick_option(f.options, want_yes)
            answers.append(Answer(f.id, label, opt, [fact.id] if fact else [], needs_input=opt is None, reason="" if opt else "no matching option"))
            continue
        if f.options and value and value not in f.options:
            match = next((o for o in f.options if o.lower() == value.lower() or value.lower() in o.lower()), None)
            if match is None:
                answers.append(Answer(f.id, label, None, [fact.id] if fact else [], needs_input=True, reason=f"option list lacks '{value}'"))
                continue
            value = match
        answers.append(Answer(f.id, label, value, [fact.id] if fact else [], needs_input=value is None, reason="" if value else f"fact '{key}' unknown"))
    return answers


PlanBuilder = Callable[[dict, dict, list[dict]], Plan]


class ApplicationService:
    def __init__(self, store: Store, profile: Profile, tailor: Tailor, actions: ActionStore, plan_builders: dict[str, PlanBuilder] | None = None) -> None:
        self.store = store
        self.profile = profile
        self.tailor = tailor
        self.actions = actions
        self.jobs = JobStore(store)
        self.apps = store.c("applications")
        self.plan_builders = plan_builders or {}

    def get_or_create(self, job_id: str) -> dict:
        app = self.apps.find_one({"job_id": job_id, "provenance.origin": "local"})
        if app:
            return app
        app = {
            "_id": new_id("app"),
            "job_id": job_id,
            "status": "queued",
            "provenance": {"origin": "local", "created_at": utcnow()},
            "sync": {"state": "not_applicable"},
            "created_at": utcnow(),
            "updated_at": utcnow(),
            "history": [],
        }
        self.apps.insert_one(app)
        return app

    def _set(self, app_id: str, status: str | None = None, **fields: Any) -> dict:
        upd: dict[str, Any] = {"updated_at": utcnow(), **fields}
        if status:
            upd["status"] = status
        self.apps.update_one(
            {"_id": app_id}, {"$set": upd, "$push": {"history": {"at": utcnow(), "status": status, **{k: v for k, v in fields.items() if k in ("reason", "next_action")}}}}
        )
        return self.apps.find_one({"_id": app_id})

    def prepare(self, job_id: str, fields: list[FormField], base_resume_path: Path | None = None) -> dict:
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        app = self.get_or_create(job_id)
        prior = self.apps.find_one({"job_id": job_id, "status": "submitted"})
        if prior:
            return self._set(app["_id"], "duplicate", reason=f"already submitted ({prior.get('tracker_record_id') or prior['_id']})")
        if job.get("status") == "excluded":
            return self._set(app["_id"], "blocked", reason="job excluded by filters", next_action="review qualification")
        base = self.profile.base_resume
        base_path = base_resume_path or (Path(base.path) if base else None)
        unknowns: list[str] = []
        artifact = None
        if base_path is None or not base_path.exists():
            unknowns.append("approved base resume not selected")
        else:
            snapshot = job.get("latest_snapshot") or {}
            artifact = self.store.c("artifacts").find_one(
                {
                    "owner_id": job_id,
                    "kind": "resume_tailored",
                    "fact_version": self.profile.version,
                    "posting_hash": snapshot.get("posting_hash"),
                    "base_sha256": base.sha256 if base else None,
                    "grounding.ok": True,  # a cached ungrounded artifact is re-tailored, not reused
                    "superseded": {"$ne": True},  # regenerated documents replace older ones for new packages
                },
                sort=[("created_at", -1)],
            )
            if artifact is None:
                display = self.profile.fact_value("display_name") or "Resume"
                artifact = self.tailor.tailor_for_job(job, base_path, base.sha256 if base else "", self.profile.version, display_name=display)
            if not artifact.get("grounding", {}).get("ok"):
                unknowns.append("tailored resume failed grounding")
        ext = job.get("extracted") or (job.get("latest_snapshot") or {}).get("extracted") or {}
        band = None
        if ext.get("salary_min") and ext.get("salary_max"):
            band = BandEvidence(
                min_base=ext["salary_min"],
                max_base=ext["salary_max"],
                currency=ext.get("salary_currency"),
                period=ext.get("salary_period") or "annual",
                title_matches=True,
                location_matches=True,
                source_url=job.get("posting_url"),
                is_total_comp_or_ote=bool(ext.get("salary_is_total_comp")),
            )
        fit_rank = (job.get("fit") or {}).get("rank")
        fit = "strong" if fit_rank == 1 else "stretch" if (fit_rank or 3) >= 4 else "fair"
        resume_pdf = artifact.get("pdf_path") if artifact and artifact.get("page_limit_verified") else (artifact.get("path") if artifact else None)
        answers = map_answers(fields, self.profile, job, resume_pdf, band, fit)
        material_unknowns = [a.label for a in answers if a.needs_input and not a.sensitive]
        unknowns += [f"answer needed: {u}" for u in material_unknowns]
        unknowns += self.profile.missing_essentials()
        unknowns = sorted(set(unknowns))
        package = {
            "employer": job["employer"],
            "title": job["title"],
            "posting_url": job.get("posting_url"),
            "requisition_id": job.get("requisition_id"),
            "linkedin_job_id": job.get("linkedin_job_id"),
            "apply_url": job.get("apply_url") or job.get("posting_url"),
            "apply_route": job.get("apply_route"),
            "document": {
                "filename": artifact.get("filename"),
                "sha256": artifact.get("sha256"),
                "pdf_sha256": artifact.get("pdf_sha256"),
                "pages": artifact.get("pages"),
                "page_limit_verified": artifact.get("page_limit_verified"),
                "path": resume_pdf,
                "render_note": artifact.get("render_note"),
            }
            if artifact
            else None,
            "answers": [a.as_dict() for a in answers],
            "gaps": (job.get("fit") or {}).get("genuine_gaps", []),
            "qualification": job.get("qualification"),
            "outstanding": unknowns,
            "prepared_at": utcnow(),
            "unsupported_requirements_note": artifact.get("unsupported_requirements_note") if artifact else "",
        }
        mhash = material_hash(
            answers=[(a.field_id, a.value) for a in answers if not a.needs_input],
            document_hashes=[h for h in [artifact.get("sha256") if artifact else None, artifact.get("pdf_sha256") if artifact else None] if h],
            extra={"job_id": job_id, "requisition": job.get("requisition_id"), "apply_url": package["apply_url"]},
        )
        status = "needs_input" if unknowns else "ready_for_review"
        return self._set(
            app["_id"],
            status,
            review_package=package,
            material_hash=mhash,
            artifact_id=artifact["_id"] if artifact else None,
            next_action="supply missing inputs" if unknowns else "review and authorize",
        )

    def supply_inputs(self, app_id: str, values: dict[str, str]) -> dict:
        app = self.apps.find_one({"_id": app_id})
        pkg = app["review_package"]
        for a in pkg["answers"]:
            if a["field_id"] in values:
                a["value"], a["needs_input"], a["fact_ids"], a["reason"] = values[a["field_id"]], False, ["user:input"], "supplied by Joss"
        pkg["outstanding"] = [u for u in pkg["outstanding"] if not any(u == f"answer needed: {a['label']}" for a in pkg["answers"] if not a["needs_input"])]
        doc_hashes = [h for h in [(pkg.get("document") or {}).get("sha256"), (pkg.get("document") or {}).get("pdf_sha256")] if h]
        mhash = material_hash(
            answers=[(a["field_id"], a["value"]) for a in pkg["answers"] if not a["needs_input"]],
            document_hashes=doc_hashes,
            extra={"job_id": app["job_id"], "requisition": pkg.get("requisition_id"), "apply_url": pkg["apply_url"]},
        )
        status = "needs_input" if pkg["outstanding"] else "ready_for_review"
        self.store.audit("inputs_supplied", app_id, fields=list(values))
        return self._set(app_id, status, review_package=pkg, material_hash=mhash)

    def execute(self, app_id: str, worker: str, run_adapter: Callable[[Plan], dict] | None = None) -> Outcome:
        app = self.apps.find_one({"_id": app_id})
        if app is None:
            return Outcome("not_found", "unknown application")
        if app["status"] not in ("ready_for_review", "authorized", "executing", "needs_verification", "submitted", "failed"):
            return Outcome("denied", f"application is {app['status']}; not executable")
        job = self.jobs.get(app["job_id"])
        pkg = app["review_package"]
        prior = self.actions.prior_terminal_for_target("application_submit", job["_id"])
        if prior is not None and prior.get("material_hash") != app["material_hash"]:
            return Outcome(prior["status"], "an earlier submission for this job already exists; reconcile first", action_id=prior["_id"])
        host = (pkg.get("apply_url") or "").split("/")[2].lower() if pkg.get("apply_url") and "://" in pkg["apply_url"] else None
        builder = self._builder_for(host, pkg.get("apply_route"))
        if builder is None or run_adapter is None:
            self._set(
                app_id,
                "handoff",
                reason=f"no supported site adapter for {host or pkg.get('apply_route')}",
                next_action="complete manually from the review package, then record the confirmation",
            )
            self.store.audit("application_handoff", app_id, host=host)
            return Outcome("handoff", "manual completion required", action_id=None)
        if job["status"] == "executing" and job.get("reserved_by") == worker:
            reserved = job  # re-entry by the same worker after an interrupt/resume
        else:
            # never treat an `executing` job held by someone else as free; only its expired lease can be taken
            reserved = self.store.reserve("jobs", {"_id": job["_id"]}, worker, [s for s in [job["status"]] if s != "executing"], "executing")
        if reserved is None:
            return Outcome("in_progress", "job reserved by another worker")
        action = self.actions.prepare(
            "application_submit", job["_id"], f"submit:{job['_id']}", {"application_id": app_id, "apply_url": pkg["apply_url"]}, app["material_hash"], host=host
        )
        if action.get("material_hash") != app["material_hash"] and action["status"] in ("succeeded", "needs_verification"):
            self.store.release("jobs", {"_id": job["_id"]}, worker, job["status"] if job["status"] != "executing" else "qualified")
            return Outcome(action["status"], "an earlier submission for this job already exists; reconcile first", action_id=action["_id"])

        def adapter(action_doc: dict) -> dict:
            plan = builder(job, pkg, pkg["answers"])
            if not plan.job_id:
                plan.job_id = job["_id"]  # screenshots are named after and stored with the job's artifacts
            try:
                return run_adapter(plan)
            except HandoffRequired as exc:
                raise _Handoff(str(exc), exc.evidence) from exc

        try:
            outcome = self.actions.execute(action["_id"], worker, adapter, job=job, has_material_unknowns=bool(pkg.get("outstanding")))
        except _Handoff as exc:
            self.store.release("jobs", {"_id": job["_id"]}, worker, job["status"])
            self.actions.coll.update_one({"_id": action["_id"]}, {"$set": {"status": "ready", "handoff": exc.evidence}, "$unset": {"reserved_by": "", "reserved_until": ""}})
            self._set(app_id, "handoff", reason=str(exc), next_action="refresh the site session from the Mac (amore browser login) or complete manually")
            return Outcome("handoff", str(exc), exc.evidence, action["_id"])
        self._apply_outcome(app, job, outcome, worker)
        return outcome

    def _apply_outcome(self, app: dict, job: dict, outcome: Outcome, worker: str) -> None:
        app_id = app["_id"]
        if outcome.status == "succeeded":
            ev = outcome.evidence
            shots = self.register_screenshots(job["_id"], app_id, ev.get("screenshots"))
            self._set(
                app_id,
                "submitted",
                submitted_at=utcnow(),
                application_date=to_toronto(utcnow()).date().isoformat(),
                evidence={
                    "basis": "receipt_observed",
                    "receipt_text": (ev.get("receipt_text") or "")[:1000],
                    "final_url": ev.get("final_url"),
                    "screenshots": shots,
                    # only identifiers the adapter actually observed in the receipt; never assumed from the job
                    "verified_identifiers": list(ev.get("matched_identifiers") or []),
                    "expected_identifiers": [x for x in [job.get("requisition_id")] if x],
                    "action_id": outcome.action_id,
                },
                evidence_basis="receipt_screenshot_reviewed" if any(s["label"] == "receipt" for s in shots) else "employer_status_reviewed",
                date_basis="employer receipt observed in America/Toronto",
                sync={"state": "sync_pending"},
                next_action="tracker sync + recruiter outreach prep",
            )
            self.store.release("jobs", {"_id": job["_id"]}, worker, "applied")
            from .sync import SyncOutbox

            SyncOutbox(self.store).enqueue(app_id, "record_submission")
        elif outcome.status == "needs_verification":
            ev = self._with_screenshots(job["_id"], app_id, outcome.evidence)
            self._set(app_id, "needs_verification", reason=outcome.reason, evidence=ev, next_action="inspect the employer account/email, then reconcile the action")
            self.store.release("jobs", {"_id": job["_id"]}, worker, "needs_verification")
        elif outcome.status == "failed":
            nxt = "retry is allowed: the action returned to ready before any irreversible step" if outcome.retryable else "fix the reported problem and prepare again"
            ev = self._with_screenshots(job["_id"], app_id, outcome.evidence)
            self._set(app_id, "failed", reason=outcome.reason, evidence=ev, retryable=outcome.retryable, next_action=nxt)
            self.store.release("jobs", {"_id": job["_id"]}, worker, "qualified")
        elif outcome.status in ("denied", "blocked"):
            self._set(
                app_id,
                "ready_for_review" if outcome.status == "denied" else "blocked",
                reason=outcome.reason,
                next_action="grant authorization" if outcome.status == "denied" else "emergency stop active",
            )
            self.store.release("jobs", {"_id": job["_id"]}, worker, job["status"] if job["status"] != "executing" else "qualified")
        else:
            self.store.release("jobs", {"_id": job["_id"]}, worker, job["status"] if job["status"] != "executing" else "qualified")

    def _with_screenshots(self, job_id: str, app_id: str, evidence: Any) -> Any:
        if not isinstance(evidence, dict) or not evidence.get("screenshots"):
            return evidence
        return {**evidence, "screenshots": self.register_screenshots(job_id, app_id, evidence.get("screenshots"))}

    def register_screenshots(self, job_id: str, app_id: str, raw: Any) -> list[dict]:
        """Normalise adapter screenshot evidence and register each capture in `artifacts` (kind `screenshot`).

        The artifact mirrors the tailored-document shape (`owner_id`, `path`, `filename`, `sha256`, `created_at`) so the
        hash-verified document download route can serve it by id; `job_id`, `application_id` and `label` let the
        application page list `summary` / `receipt` / `ambiguous` captures. Re-registering the same file is a no-op."""
        shots = normalize_screenshots(raw)
        arts = self.store.c("artifacts")
        for shot in shots:
            path = Path(shot["path"])
            if shot.get("sha256") is None and path.is_file():
                shot["sha256"] = sha256_file(path)
            if not shot.get("sha256"):
                continue  # file never landed; keep the evidence entry but there is nothing to serve
            existing = arts.find_one({"kind": "screenshot", "application_id": app_id, "sha256": shot["sha256"], "label": shot["label"]}, {"_id": 1})
            if existing:
                shot["artifact_id"] = existing["_id"]
                continue
            art = {
                "_id": new_id("art"),
                "owner_id": job_id,
                "job_id": job_id,
                "application_id": app_id,
                "kind": "screenshot",
                "label": shot["label"],
                "path": str(path),
                "filename": path.name,
                "sha256": shot["sha256"],
                "media_type": "image/png",
                "captured_at": shot.get("captured_at"),
                "url": shot.get("url"),
                "created_at": utcnow(),
            }
            arts.insert_one(art)
            shot["artifact_id"] = art["_id"]
        return shots

    def reconcile(self, app_id: str, resolved: str, evidence: dict, by: str = "user") -> dict:
        app = self.apps.find_one({"_id": app_id})
        action = self.actions.by_key(f"submit:{app['job_id']}")
        if action and action["status"] == "needs_verification":
            self.actions.reconcile(action["_id"], resolved, evidence, by)
        job = self.jobs.get(app["job_id"])
        if resolved == "succeeded":
            ev = {"basis": "explicit_user_confirmation", **evidence}
            self.jobs.set_status(job["_id"], "applied")
            from .sync import SyncOutbox

            SyncOutbox(self.store).enqueue(app_id, "record_submission")
            return self._set(
                app_id,
                "submitted",
                submitted_at=utcnow(),
                application_date=evidence.get("application_date") or to_toronto(utcnow()).date().isoformat(),
                evidence=ev,
                evidence_basis="explicit_user_confirmation",
                date_basis=evidence.get("date_basis", "user confirmed after reconciliation"),
                sync={"state": "sync_pending"},
            )
        self.jobs.set_status(job["_id"], "qualified")
        return self._set(app_id, "failed", reason="reconciled as not submitted", evidence=evidence)

    def record_manual_submission(self, app_id: str, confirmation: str, application_date: str, resume_sha256: str | None, receipt_path: str | None = None) -> dict:
        """Joss completed a handoff; record it with explicit_user_confirmation evidence."""
        app = self.apps.find_one({"_id": app_id})
        job = self.jobs.get(app["job_id"])
        ev = {"basis": "explicit_user_confirmation", "confirmation": confirmation, "resume_sha256": resume_sha256, "receipt_path": receipt_path}
        self.jobs.set_status(job["_id"], "applied")
        from .sync import SyncOutbox

        SyncOutbox(self.store).enqueue(app_id, "record_submission")
        return self._set(
            app_id,
            "submitted",
            submitted_at=utcnow(),
            application_date=application_date,
            evidence=ev,
            evidence_basis="explicit_user_confirmation",
            date_basis="user-reported date (America/Toronto)",
            sync={"state": "sync_pending"},
        )

    def _builder_for(self, host: str | None, route: str | None) -> PlanBuilder | None:
        if route and route in self.plan_builders:
            return self.plan_builders[route]
        if host:
            for key, b in self.plan_builders.items():
                if key == host or host.endswith("." + key):
                    return b
        return None


class _Handoff(Exception):
    def __init__(self, msg: str, evidence: dict) -> None:
        super().__init__(msg)
        self.evidence = evidence
