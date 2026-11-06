"""Wiring of real components. One Runtime per process; services implement the orchestration protocol."""

from __future__ import annotations

import logging
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from amorehiremepls.apply.applications import ApplicationService, FormField, SalaryResearcher
from amorehiremepls.apply.browser import BrowserAdapter, BrowserConfig, Plan, Step
from amorehiremepls.apply.network_log import NetworkLog
from amorehiremepls.apply.tailoring import Tailor
from amorehiremepls.candidate.candidate_docs import CandidateDocs, load_candidate_docs
from amorehiremepls.candidate.profile import Profile, ProfileStore
from amorehiremepls.core.actions import ActionStore
from amorehiremepls.core.ai import AIRunner, Budget, Pricing, Provider, build_provider
from amorehiremepls.core.authorization import AuthorizationStore
from amorehiremepls.core.config import Settings
from amorehiremepls.core.db import Store
from amorehiremepls.core.orchestration import Runner, build_application_graph, make_checkpointer
from amorehiremepls.core.skills import catalog
from amorehiremepls.core.util import utcnow
from amorehiremepls.jobs.discovery import Discovery
from amorehiremepls.jobs.jobs import JobStore
from amorehiremepls.jobs.logos import LogoCache
from amorehiremepls.outreach.outreach import OutreachService
from amorehiremepls.records.sync import SyncCoordinator

log = logging.getLogger(__name__)


def generic_plan_builder(job: dict, pkg: dict, answers: list[dict]) -> Plan:
    """Builds a plan from a scanned form (`pkg['form_plan']`): fields with selectors + a submit selector.
    Site-specific adapters can register under their hostname instead."""
    fp = job.get("form_plan") or {}
    if not fp.get("submit_selector"):
        raise ValueError("no form plan for this job")
    by_id = {a["field_id"]: a for a in answers}
    steps: list[Step] = []
    for f in fp.get("fields", []):
        a = by_id.get(f["id"])
        if not a or a.get("value") is None:
            continue
        t = f.get("type", "text")
        if t == "file":
            steps.append(Step("upload", f["selector"], a["value"]))  # path is constrained to the artifact dir by the adapter
        elif t == "select":
            steps.append(Step("select", f["selector"], a["value"], sensitive=bool(a.get("sensitive"))))
        elif t in ("checkbox", "radio"):
            steps.append(Step("check", f["selector"]))
        else:
            steps.append(Step("fill", f["selector"], a["value"], sensitive=bool(a.get("sensitive"))))
    for sel in fp.get("next_selectors", []):
        steps.append(Step("click", sel))
    steps.append(Step("click", fp["submit_selector"], final=True, timeout_ms=int(fp.get("receipt_timeout_ms", 20000))))
    return Plan(
        start_url=pkg["apply_url"],
        steps=steps,
        receipt_text=fp.get("receipt_text", "thank you"),
        # a known requisition id must appear in the receipt unless the plan explicitly says the site never shows it
        receipt_must_include=[x for x in [job.get("requisition_id")] if x and fp.get("receipt_includes_requisition", True)],
        job_id=job.get("_id"),
    )


class Runtime:
    def __init__(self, settings: Settings, store: Store | None = None, provider: Provider | None = None) -> None:
        self.settings = settings
        self.store = store or Store.from_settings(settings)
        self.profiles = ProfileStore(self.store)
        self.profile: Profile = self.profiles.load()
        self.provider = provider or build_provider(settings.effective_model)
        self.pricing = Pricing(settings.pricing_file)
        self.actions = ActionStore(self.store)
        self.auth = AuthorizationStore(self.store)
        self.jobs = JobStore(self.store)
        self.browser = BrowserAdapter(
            BrowserConfig(
                allowed_hosts=settings.allowed_hosts,
                headless=settings.browser_headless,
                session_dir=settings.browser_session_dir,
                screenshot_dir=settings.artifact_dir / "screenshots",  # fallback only; job runs use artifact_dir/jobs/<job_id>/screenshots
                upload_root=settings.artifact_dir,
                artifact_dir=settings.artifact_dir,
                resource_hosts=settings.resource_hosts,
                captcha_max_attempts=settings.captcha_max_attempts,
                cdp_port=settings.human_check_cdp_port,
            ),
            network=NetworkLog(self.store),
        )
        self.sync = SyncCoordinator(self.store, settings.tracker_clone_dir, settings.tracker_remote, settings.tracker_branch, settings.tracker_push_enabled)
        smtp = (
            {
                "host": settings.smtp_host,
                "port": settings.smtp_port,
                "username": settings.smtp_login,
                "password": settings.smtp_password.get_secret_value(),
                "from": settings.smtp_from,
            }
            if settings.smtp_host
            else None
        )
        self.smtp = smtp
        self.logos = LogoCache(settings.artifact_dir)  # public research cache (favicons), no candidate data, no model use
        self._checkpointer = None

    def reload_profile(self) -> Profile:
        self.profile = self.profiles.load()
        return self.profile

    def budget(self) -> Budget:
        s = self.settings
        return Budget(s.budget_max_tokens, s.budget_max_usd, s.budget_max_tool_steps, s.budget_max_seconds)

    def ai(self, run_id: str | None = None) -> AIRunner | None:
        if not self.settings.provider_configured and getattr(self.provider, "name", "none") == "none":
            return None
        return AIRunner(self.store, self.provider, catalog, self.pricing, self.budget(), run_id=run_id)

    def captcha_autosolve_enabled(self) -> bool:
        """Settings toggle (control `captcha_autosolve`) wins over the environment default; both default to off."""
        value = self.store.get_control("captcha_autosolve")
        return bool(self.settings.captcha_autosolve if value is None else value)

    def captcha_solver(self, run_id: str | None = None):
        """The opt-in solver for the application-submit path, or None (human check handoff)."""
        if not self.captcha_autosolve_enabled():
            return None
        ai = self.ai(run_id)
        if ai is None:
            return None
        from amorehiremepls.apply.captcha import ModelCaptchaSolver

        return ModelCaptchaSolver(ai)

    def candidate_docs(self) -> CandidateDocs:
        """Fresh read of the drop folder (base DOCX + extra notes). Cheap, so every tailor/rank sees the current files."""
        return load_candidate_docs(self.settings.candidate_dir, self.settings.candidate_supplemental_max_chars)

    def discovery(self, run_id: str | None = None) -> Discovery:
        return Discovery(self.store, self.profile, self.ai(run_id), supplemental_text=self.candidate_docs().supplemental_text)

    def tailor(self, run_id: str | None = None) -> Tailor:
        docs = self.candidate_docs()
        return Tailor(
            self.store,
            self.settings.artifact_dir,
            self.ai(run_id),
            supplemental_text=docs.supplemental_text,
            supplemental_sha256=docs.supplemental_sha256,
            preferences=self.profile.preferences,
            profession=self.profile.profession(),
        )

    def notifier(self):
        from amorehiremepls.core.notify import Notifier

        return Notifier(self.store, self.settings, self.actions)

    def applications(self, run_id: str | None = None, builders: dict | None = None) -> ApplicationService:
        return ApplicationService(
            self.store,
            self.profile,
            self.tailor(run_id),
            self.actions,
            builders or {"generic_form": generic_plan_builder},
            notifier=self.notifier(),
            challenge_retry_minutes=self.settings.challenge_retry_minutes,
            salary_research=SalaryResearcher(self.ai(run_id), self.profile),  # one bounded estimate when a form asks for pay and no range is posted
        )

    def outreach(self, run_id: str | None = None) -> OutreachService:
        return OutreachService(self.store, self.profile, self.ai(run_id), self.smtp)

    def checkpointer(self):
        if self._checkpointer is None:
            self._checkpointer = make_checkpointer(self.store)
        return self._checkpointer

    def runner(self) -> Runner:
        return Runner(self.store, build_application_graph(RealServices(self), self.store, self.checkpointer()))

    def provider_check(self) -> dict[str, Any]:
        info = self.provider.check()
        info["configured_model"] = self.settings.effective_model or None
        info["default_provider"] = self.settings.model_provider
        info["api_key_present"] = bool(os.environ.get("OPENAI_API_KEY" if self.settings.model_provider == "openai" else "ANTHROPIC_API_KEY"))
        info["pricing_loaded"] = bool(self.pricing.prices)
        return info

    def health(self) -> dict[str, Any]:
        try:
            db = self.store.ping()
        except Exception as exc:
            db = {"ok": False, "error": type(exc).__name__}
        return {
            "ok": bool(db.get("ok")),
            "db": db,
            "emergency_stop": self.store.emergency_stop_active() if db.get("ok") else None,
            "provider_configured": self.settings.provider_configured,
            "base_resume_selected": self.profile.base_resume is not None,
        }


class RealServices:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt

    def qualify(self, job_id: str) -> dict:
        job = self.rt.jobs.get(job_id)
        if job and job.get("status") in ("applied", "applied_historical", "executing"):
            return job
        disc = self.rt.discovery()
        job = disc.qualify_job(job_id)
        if job["status"] in ("qualified", "needs_review"):
            job = disc.rank_fit(job_id)
        return job

    def scan_fields(self, job: dict) -> list[dict]:
        job = self.rt.jobs.get(job["_id"]) or job
        if job.get("form_plan", {}).get("fields"):
            return job["form_plan"]["fields"]
        url = job.get("apply_url") or job.get("posting_url")
        if not url or job.get("apply_route") == "linkedin_or_employer_site_handoff":
            return []
        from amorehiremepls.apply.ats import adapter_for

        adapter = adapter_for(url, job.get("apply_route"))
        if adapter is not None:
            try:  # the vendor's public question list needs no browser and names every control
                qs = adapter.fetch_questions(job)
                fields = [asdict(f) for f in qs.fields]
                plan = {**job.get("form_plan", {}), "fields": fields, "source": qs.source, "external_job_id": qs.external_job_id, "read_at": utcnow()}
                self.rt.jobs.set_status(job["_id"], job["status"], form_plan=plan, apply_route=adapter.name)
                return fields
            except Exception as exc:
                log.warning("%s question list failed for %s: %s; falling back to a page scan", adapter.name, url, type(exc).__name__)
        try:
            fields = self.rt.browser.scan_form(url, site_key=url.split("/")[2])
        except Exception as exc:  # scanning is best-effort; failures mean a handoff later
            log.warning("form scan failed for %s: %s", url, type(exc).__name__)
            return []
        self.rt.jobs.set_status(job["_id"], job["status"], form_plan={**job.get("form_plan", {}), "fields": fields})
        return fields

    def prepare(self, job_id: str, fields: list[dict]) -> dict:
        ff = [
            FormField(f.get("id", ""), f.get("label", ""), f.get("type", "text"), f.get("options", []), f.get("selector", ""), bool(f.get("required")), f.get("meta") or {})
            for f in fields
        ]
        return self.rt.applications().prepare(job_id, ff)

    def supply_inputs(self, application_id: str, values: dict) -> dict:
        return self.rt.applications().supply_inputs(application_id, values)

    def has_authorization(self, app: dict) -> bool:
        job = self.rt.jobs.get(app["job_id"])
        host = (app.get("review_package") or {}).get("apply_url", "")
        host = host.split("/")[2].lower() if "://" in host else None
        return self.rt.auth.check(
            "application_submit", app["job_id"], app["material_hash"], host=host, job=job, has_material_unknowns=bool((app.get("review_package") or {}).get("outstanding"))
        ).allowed

    def execute(self, application_id: str, worker: str) -> dict:
        svc = self.rt.applications()
        solver = self.rt.captcha_solver()  # None unless the owner switched the automatic captcha step on
        app = self.rt.store.c("applications").find_one({"_id": application_id}) or {}
        hold = None
        if self.rt.settings.human_check_hold_minutes > 0 and app.get("job_id"):
            from amorehiremepls.core.human_check import HumanHold

            hold = HumanHold(self.rt.store, self.rt.actions, self.rt.settings, application_id, app["job_id"], worker)

        running = self.rt.store.c("runs").find_one({"kind": "application", "params.job_id": app.get("job_id"), "status": "running"}, sort=[("updated_at", -1)])
        run_id = (running or {}).get("_id")

        def note(message: str) -> None:
            if run_id and message.strip():
                self.rt.store.run_event(run_id, message.strip()[:300])

        def run(plan):
            # The hold, when a check appears, keeps this same browser page. It does not start a second submit.
            # Each step is saved as it happens so the application page can show it.
            return self.rt.browser.run_plan(plan, site_key=plan.start_url.split("/")[2], captcha_solver=solver, hold=hold, progress=note)

        out = svc.execute(application_id, worker, run_adapter=run)
        return {"status": out.status, "reason": out.reason, "evidence": out.evidence, "action_id": out.action_id, "adapter_ran": out.adapter_ran}

    def record_manual(self, application_id: str, manual: dict) -> dict:
        return self.rt.applications().record_manual_submission(
            application_id, manual.get("confirmation", ""), manual.get("application_date", ""), manual.get("resume_sha256"), manual.get("receipt_path")
        )

    def after_submission(self, application_id: str) -> dict:
        return self.rt.outreach().company_list_after_submission(application_id)

    def get_application(self, application_id: str) -> dict:
        return self.rt.store.c("applications").find_one({"_id": application_id})


def private_path(settings: Settings, *parts: str) -> Path:
    p = settings.private_dir.joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p
