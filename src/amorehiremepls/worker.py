"""Worker process: runs queued/resumed workflows, drives the sync coordinator, runs scheduled discovery.

Scheduling uses America/Toronto wall-clock slots claimed atomically in MongoDB so two workers (or a
restart) never start the same slot twice. Browser work is sequential per authenticated site.
"""

from __future__ import annotations

import logging
import signal
import socket
import threading
from datetime import datetime, timedelta
from typing import Any

from .discovery import SOURCES, SourceError
from .runtime import Runtime
from .ui_settings import UISettingsStore
from .util import TORONTO, toronto_now, utcnow

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, rt: Runtime, poll_seconds: float = 5.0) -> None:
        self.rt = rt
        self.poll = poll_seconds
        self.name = f"{socket.gethostname()}:{threading.get_ident()}"
        self.stop_event = threading.Event()
        self.runner = rt.runner()

    # -- lifecycle --------------------------------------------------------------------
    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: self.stop_event.set())
        signal.signal(signal.SIGINT, lambda *_: self.stop_event.set())
        self.rt.store.ensure_indexes()
        try:
            self.recover_interrupted_runs()
        except Exception:
            log.exception("run recovery at startup failed")
        try:
            self.rt.sync.reconcile_all()
        except Exception:
            log.exception("sync reconciliation at startup failed")
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("worker tick failed")
            self.stop_event.wait(self.poll)

    def heartbeat(self) -> None:
        self.rt.store.set_control("worker_heartbeat", {"at": utcnow(), "name": self.name})
        self.rt.store.set_control(f"worker_heartbeat:{self.name}", {"at": utcnow()})

    def recover_interrupted_runs(self, stale_after_seconds: int = 180) -> int:
        """A hard crash leaves runs in `running`/`claimed` with no process behind them. At startup, any such
        run whose owner is this worker name, whose lease expired, or whose owner heartbeat is stale becomes
        failed("interrupted") so resume() can continue it from the last checkpoint. External actions are
        protected separately (expired action leases become needs_verification, never re-run)."""
        now = utcnow()
        n = 0
        for run in self.rt.store.c("runs").find({"status": {"$in": ["running", "claimed"]}}):
            owner = run.get("reserved_by") or (run.get("state") or {}).get("worker") or run.get("params", {}).get("worker")
            lease_expired = bool(run.get("reserved_until")) and run["reserved_until"] < now
            hb = self.rt.store.get_control(f"worker_heartbeat:{owner}") if owner else None
            stale_owner = owner is None or hb is None or (now - hb["at"]).total_seconds() > stale_after_seconds
            if owner == self.name or lease_expired or stale_owner:
                self.rt.store.c("runs").update_one(
                    {"_id": run["_id"], "status": run["status"]},
                    {
                        "$set": {"status": "failed", "error": "interrupted: worker stopped while the run was active; resume to continue from the checkpoint", "updated_at": now},
                        "$unset": {"reserved_by": "", "reserved_until": ""},
                    },
                )
                self.rt.store.run_event(run["_id"], "interrupted (recovered at worker startup)")
                n += 1
        swept = self.rt.actions.sweep_expired_leases()
        if n or swept:
            log.warning("recovered %d interrupted runs; %d expired action leases marked needs_verification", n, swept)
        return n

    def tick(self) -> dict[str, Any]:
        self.heartbeat()
        self.rt.reload_profile()
        done: dict[str, Any] = {"runs": 0, "sync": 0, "scheduled": 0}
        self.rt.auth.expire_stale()
        self.rt.actions.sweep_expired_leases()
        for run in self.rt.store.c("runs").find({"status": "queued", "kind": "application"}).sort("created_at", 1).limit(self.rt.settings.worker_concurrency):
            if self.rt.store.emergency_stop_active():
                break
            claimed = self.rt.store.reserve("runs", {"_id": run["_id"]}, self.name, ["queued"], "claimed", lease_seconds=3600)
            if claimed:
                self._start(run)
                done["runs"] += 1
        # a cancel request on an idle (waiting/paused) run has no node boundary to observe it; settle it here
        for run in self.rt.store.c("runs").find({"requested": "cancel", "status": {"$in": ["waiting", "paused", "queued"]}}):
            self.rt.store.update_run(run["_id"], status="cancelled", requested=None, interrupt=None, finished_at=utcnow(), note="cancelled while idle")
            self.rt.store.run_event(run["_id"], "cancelled while idle")
        for run in self.rt.store.c("runs").find({"requested": "resume", "status": {"$in": ["waiting", "paused", "failed"]}}).limit(self.rt.settings.worker_concurrency):
            payload = run.get("resume_payload")
            self.rt.store.update_run(run["_id"], requested=None, resume_payload=None)
            self.runner.resume(run["_id"], payload)
            done["runs"] += 1
        results = self.rt.sync.process_pending(owner=self.name)
        done["sync"] = len(results)
        done["scheduled"] = self.run_schedule()
        done["logos"] = self.refresh_logos()
        done["challenge_retries"] = self.run_due_challenge_retries()
        return done

    def refresh_logos(self, limit: int = 3) -> int:
        """Cheap periodic step: when the "Show company logos" option is on, fetch a few missing employer icons so
        page views rarely have to. Deterministic favicon fetch; no model usage; failures are remembered for a week."""
        try:
            if not UISettingsStore(self.rt.store).load().company_logos:
                return 0
            fields = {"employer": 1, "posting_url": 1, "apply_url": 1, "url": 1, "company_website": 1, "website": 1}
            jobs = list(self.rt.store.c("jobs").find({}, fields).sort("updated_at", -1).limit(200))
            return self.rt.logos.refresh(jobs, limit=limit)
        except Exception:
            log.exception("logo refresh failed")
            return 0

    def run_due_challenge_retries(self) -> int:
        """One later try per application, when it is due. A fresh browser session. No second click if the first try already clicked."""
        self._close_expired_challenge_retries()
        now = utcnow()
        n = 0
        due = self.rt.store.c("challenge_retries").find({"status": "scheduled", "due_at": {"$lte": now}}).sort("due_at", 1).limit(1)
        for doc in due:
            claimed = self.rt.store.reserve("challenge_retries", {"_id": doc["_id"]}, self.name, ["scheduled"], "running", 900, allow_takeover=False)
            if not claimed:
                continue
            try:
                self._run_challenge_retry(claimed)
            finally:
                self.rt.store.c("challenge_retries").update_one({"_id": doc["_id"], "status": "running"}, {"$set": {"status": "done", "finished_at": utcnow()}})
            n += 1
        return n

    def _close_expired_challenge_retries(self) -> None:
        """A crashed later try is not started again. The owner is told once, and submit is not clicked again."""
        now = utcnow()
        for doc in self.rt.store.c("challenge_retries").find({"status": "running", "reserved_until": {"$lt": now}}):
            closed = self.rt.store.c("challenge_retries").update_one(
                {"_id": doc["_id"], "status": "running"}, {"$set": {"status": "done", "result": "lease_expired", "finished_at": now}}
            )
            if closed.modified_count != 1:
                continue
            app = self.rt.store.c("applications").find_one({"_id": doc.get("application_id")})
            if not app or app.get("status") in ("submitted", "handoff", "needs_verification") or not app.get("challenge_retry_active"):
                continue
            svc = self.rt.applications()
            svc._set(
                app["_id"],
                "handoff",
                reason="The later try stopped before a confirmation. Finish on the site yourself.",
                next_action="Apply on the job site yourself, then record the confirmation text here.",
                handoff_kind="human_check",
            )
            svc._notify(svc.apps.find_one({"_id": app["_id"]}), "human_check")

    def _run_challenge_retry(self, doc: dict) -> None:
        app_id = doc["application_id"]
        updated = self.rt.store.c("applications").update_one(
            {"_id": app_id, "status": "challenge_retry"},
            {"$set": {"status": "authorized", "challenge_retry_active": True, "updated_at": utcnow()}},
        )
        if updated.modified_count != 1:
            return
        svc = self.rt.applications()
        confirm_only = bool(doc.get("final_clicked"))
        solver = None if confirm_only else self.rt.captcha_solver()

        def run(plan):
            host = plan.start_url.split("/")[2]
            return self.rt.browser.run_plan(plan, site_key=host, captcha_solver=solver, hold=None, confirm_only=confirm_only)

        svc.execute(app_id, self.name, run_adapter=run)

    def _start(self, run: dict) -> None:
        job_id = run["params"]["job_id"]
        state = {"run_id": run["_id"], "job_id": job_id, "worker": self.name, "notes": []}
        if run["params"].get("form_fields") is not None:
            state["form_fields"] = run["params"]["form_fields"]
        self.runner._invoke(run, state)

    # -- schedule ------------------------------------------------------------------------
    def due_slots(self, now: datetime | None = None) -> list[str]:
        sched = self.rt.settings.discovery_schedule.strip()
        if not sched:
            return []
        now = now or toronto_now()
        slots = []
        for hhmm in [s.strip() for s in sched.split(",") if s.strip()]:
            try:
                h, m = (int(x) for x in hhmm.split(":"))
            except ValueError:
                continue
            slot_time = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if slot_time <= now < slot_time + timedelta(hours=6):
                slots.append(f"discovery:{slot_time.strftime('%Y-%m-%dT%H:%M')}")
        return slots

    def run_schedule(self) -> int:
        count = 0
        for slot in self.due_slots():
            if self.rt.store.claim_schedule_slot(slot, self.name):
                log.info("running scheduled discovery for %s", slot)
                self.run_discovery(trigger=slot)
                count += 1
        return count

    def run_discovery(self, trigger: str = "manual") -> dict[str, Any]:
        sources = self.rt.store.get_control("discovery_sources", []) or []
        run = self.rt.store.create_run("discovery", {"trigger": trigger, "sources": len(sources)})
        disc = self.rt.discovery(run["_id"])
        summary: dict[str, Any] = {"ingested": 0, "new": 0, "qualified": 0, "errors": []}
        for src in sources:
            cls = SOURCES.get(src.get("type"))
            if cls is None:
                summary["errors"].append(f"unknown source type {src.get('type')}")
                continue
            try:
                raws = cls().fetch(src["key"], src.get("employer"))
            except SourceError as exc:
                summary["errors"].append(str(exc))
                continue
            for raw in raws:
                job, created = disc.ingest_raw(raw)
                summary["ingested"] += 1
                summary["new"] += int(created)
                if created or job.get("status") == "new":
                    q = disc.qualify_job(job["_id"])
                    if q["status"] == "qualified":
                        summary["qualified"] += 1
                        try:
                            disc.rank_fit(job["_id"])
                        except Exception as exc:  # budget or provider problems must not stop discovery
                            summary["errors"].append(f"rank {job['_id']}: {type(exc).__name__}")
        self.rt.store.update_run(run["_id"], status="completed", summary=summary, finished_at=utcnow())
        return summary


def worker_healthy(rt: Runtime, max_age_seconds: int = 120) -> bool:
    hb = rt.store.get_control("worker_heartbeat")
    if not hb:
        return False
    at = hb.get("at")
    return at is not None and (utcnow() - at).total_seconds() < max_age_seconds


def next_slot_description(rt: Runtime) -> str:
    sched = rt.settings.discovery_schedule.strip()
    if not sched:
        return "disabled"
    return f"daily at {sched} {TORONTO.key}"
