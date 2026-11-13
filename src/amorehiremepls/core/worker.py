"""Worker process: runs queued/resumed workflows, drives the sync coordinator, runs scheduled discovery.

Scheduling uses wall-clock slots in the configured time zone (AMORE_TIMEZONE) claimed atomically in MongoDB so two workers (or a
restart) never start the same slot twice. A due slot only queues a check: it is read by the same single pass that
answers "Check for new jobs", so a scheduled check and a pressed button never read the boards twice. Browser work is
sequential per authenticated site.
"""

from __future__ import annotations

import logging
import signal
import socket
import threading
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from amorehiremepls.core.accounts import active_admin_id, active_user_ids, user_scope
from amorehiremepls.core.runtime import Runtime
from amorehiremepls.core.schedule import due_slot, ignored_entries, parse_times, slot_key, times_text
from amorehiremepls.core.util import local_now, local_zone, utcnow
from amorehiremepls.jobs.discovery import SOURCES, SourceError
from amorehiremepls.web.jobs_refresh import ACTIVE, SEARCH_KINDS, is_active
from amorehiremepls.web.ui_settings import UISettingsStore

log = logging.getLogger(__name__)

# A search asked for on the Jobs page is held this long; after that it reads as stopped, not as still checking.
DISCOVERY_LEASE_SECONDS = 1800
# How many newly found job ids one search keeps, so the Jobs page can mark them.
NEW_JOB_IDS_KEPT = 200
# Where the worker says which times of day it checks at. The dashboard reads it, because the dashboard a person has
# open may run on another computer with other settings.
SCHEDULE_CONTROL = "discovery_schedule"


class Worker:
    def __init__(self, rt: Runtime, poll_seconds: float = 5.0) -> None:
        self.rt = rt
        self.poll = poll_seconds
        self.name = f"{socket.gethostname()}:{threading.get_ident()}"
        self.stop_event = threading.Event()
        self.runner = rt.runner()
        self._schedule_published = False

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

    def publish_schedule(self) -> None:
        """Save and log the times of day this worker checks the boards at. Done once, at its first pass."""
        value = self.rt.settings.discovery_schedule
        times = times_text(parse_times(value))
        self.rt.store.set_control(SCHEDULE_CONTROL, {"times": times, "timezone": local_zone().key, "worker": self.name, "at": utcnow()})
        self._schedule_published = True
        if ignored_entries(value):
            log.warning("discovery schedule: %d entries are not HH:MM times of day and were left out", ignored_entries(value))
        log.info("discovery schedule: %s", f"{', '.join(times)} ({local_zone().key})" if times else "none, the boards are checked only when asked")

    def tick(self) -> dict[str, Any]:
        self.heartbeat()
        if not self._schedule_published:
            self.publish_schedule()
        done: dict[str, Any] = {"runs": 0, "sync": 0, "scheduled": 0, "searches": 0, "logos": 0, "challenge_retries": 0}
        self.rt.auth.expire_stale()
        self.rt.actions.sweep_expired_leases()
        # Tracker sync is not an access check. Expired and turned-off members still flush queued ledger items.
        results = self.rt.sync.process_pending(owner=self.name)
        done["sync"] = len(results)
        self._give_unowned_runs_an_owner()
        ids = active_user_ids(self.rt.store)
        if ids is None:
            self.rt.reload_profile()
            self._tick_owned(done)
        else:
            for uid in ids:
                with user_scope(uid):
                    self.rt.reload_profile()
                    self._tick_owned(done)
        return done

    def _tick_owned(self, done: dict[str, Any]) -> None:
        self._queue_approved_sends()
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
        done["scheduled"] += self.run_schedule()  # a due time of day queues a check; the next line reads the boards for it
        done["searches"] += self.run_requested_discovery()
        done["logos"] += self.refresh_logos()
        done["challenge_retries"] += self.run_due_challenge_retries()

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

    def _give_unowned_runs_an_owner(self) -> None:
        """A queued application with no user_id is invisible to the per-person claim and stays queued forever.

        The pre-account session writes that way. Attach the run, and any job or application it points at that
        also has no owner, to the active admin. A run that already belongs to someone is left alone."""
        admin_id = active_admin_id(self.rt.store)
        if not admin_id:
            return
        missing = {"$or": [{"user_id": {"$exists": False}}, {"user_id": None}, {"user_id": ""}]}
        runs = self.rt.store.db["runs"]
        for run in runs.find({"status": "queued", "kind": "application", **missing}).sort("created_at", 1):
            runs.update_one({"_id": run["_id"], "status": "queued"}, {"$set": {"user_id": admin_id, "updated_at": utcnow()}})
            job_id = (run.get("params") or {}).get("job_id")
            if not job_id:
                continue
            self.rt.store.db["jobs"].update_one({"_id": job_id, **missing}, {"$set": {"user_id": admin_id}})
            self.rt.store.db["applications"].update_many({"job_id": job_id, **missing}, {"$set": {"user_id": admin_id}})

    def _start(self, run: dict) -> None:
        self.rt.store.run_event(run["_id"], "Picked up")
        job_id = run["params"]["job_id"]
        state = {"run_id": run["_id"], "job_id": job_id, "worker": self.name, "notes": []}
        if run["params"].get("form_fields") is not None:
            state["form_fields"] = run["params"]["form_fields"]
        if run["params"].get("phase") == "execute" and run["params"].get("application_id"):
            state["application_id"] = run["params"]["application_id"]
            state["status"] = "authorized"
        self.runner._invoke(run, state)

    def _queue_approved_sends(self) -> None:
        """An approved package whose task already finished gets one apply task. It does not rebuild the package."""
        from amorehiremepls.core.orchestration import queue_approved_send
        from amorehiremepls.core.runtime import RealServices

        services = RealServices(self.rt)
        for app in self.rt.store.c("applications").find({"status": "authorized"}).limit(5):
            try:
                allowed = services.has_authorization(app)
            except Exception:
                log.exception("could not check approval for %s", app.get("_id"))
                continue
            queue_approved_send(self.rt.store, app, allowed)

    # -- schedule ------------------------------------------------------------------------
    def due_slots(self, now: datetime | None = None) -> list[str]:
        """The one slot that is due, if any: the latest scheduled time of day that has passed, within the grace time."""
        at = due_slot(parse_times(self.rt.settings.discovery_schedule), now or local_now())
        return [slot_key(at)] if at else []

    def _active_search(self) -> dict | None:
        """A check for new jobs that is waiting to start or reading boards right now, for this person."""
        now = utcnow()
        open_runs = self.rt.store.c("runs").find({"kind": {"$in": list(SEARCH_KINDS)}, "status": {"$in": sorted(ACTIVE)}}).sort("created_at", 1)
        return next((run for run in open_runs if is_active(run, now)), None)

    def run_schedule(self, now: datetime | None = None) -> int:
        """Queue the check a due time of day asks for. Returns how many were queued (0 or 1).

        The slot is claimed first, so a restart or a second worker never queues it again. A check that is already
        waiting or running answers the slot too: nothing is queued next to it. Nothing is read here."""
        count = 0
        for slot in self.due_slots(now):
            if not (self.rt.store.get_control("discovery_sources", []) or []):
                continue  # no board is watched: nothing to check, and the slot stays free in case one is added soon
            if not self.rt.store.claim_schedule_slot(slot, self.name):
                continue
            active = self._active_search()
            if active:
                self.rt.store.c("runs").update_one({"_id": active["_id"]}, {"$set": {"params.answers_slot": slot}})
                log.info("scheduled discovery for %s: a check is already under way and answers it", slot)
                continue
            self.rt.store.create_run("discovery", {"trigger": slot, "schedule_slot": slot})
            log.info("scheduled discovery queued for %s", slot)
            count += 1
        return count

    def run_requested_discovery(self) -> int:
        """A search asked for on the Jobs page or queued by the schedule. One pass answers every request that was
        waiting, so pressing the button several times, or pressing it as a scheduled check comes due, never reads the
        boards several times."""
        asked = [{"kind": "discovery_request"}, {"kind": "discovery", "params.schedule_slot": {"$exists": True}}]  # by the button, by the schedule
        waiting = list(self.rt.store.c("runs").find({"status": "queued", "requested": {"$ne": "cancel"}, "$or": asked}).sort("created_at", 1))
        claimed = None
        for run in waiting:
            claimed = self.rt.store.reserve("runs", {"_id": run["_id"]}, self.name, ["queued"], "running", lease_seconds=DISCOVERY_LEASE_SECONDS, extra={"started_at": utcnow()})
            if claimed:
                break
        if not claimed:
            return 0
        self.rt.store.run_event(claimed["_id"], "Picked up")
        summary = self.run_discovery(trigger=(claimed.get("params") or {}).get("trigger") or "dashboard", run=claimed)
        others = [r["_id"] for r in waiting if r["_id"] != claimed["_id"]]
        if others:
            now = utcnow()
            answered = {"status": "completed", "summary": {**summary, "answered_by": claimed["_id"]}, "finished_at": now, "updated_at": now}
            self.rt.store.c("runs").update_many({"_id": {"$in": others}, "status": "queued"}, {"$set": answered})
        return 1

    def run_discovery(self, trigger: str = "manual", run: dict | None = None) -> dict[str, Any]:
        """Read every watched board once. `run` is a request already claimed from the Jobs page; without it a
        scheduled pass makes its own task. Each board's outcome is kept so the Jobs page can say what happened."""
        sources = self.rt.store.get_control("discovery_sources", []) or []
        if run is None:
            run = self.rt.store.create_run("discovery", {"trigger": trigger, "sources": len(sources)})
            self.rt.store.update_run(run["_id"], status="running", started_at=utcnow())
        disc = self.rt.discovery(run["_id"])
        summary: dict[str, Any] = {"ingested": 0, "new": 0, "qualified": 0, "errors": [], "sources": [], "new_job_ids": []}
        try:
            for index, src in enumerate(sources, 1):
                row: dict[str, Any] = {"type": src.get("type"), "key": src.get("key"), "employer": src.get("employer"), "found": 0, "new": 0, "error": None}
                self.rt.store.update_run(
                    run["_id"], progress={"index": index, "total": len(sources), "source": {k: row[k] for k in ("type", "key", "employer")}, "new": summary["new"]}
                )
                summary["sources"].append(row)
                cls = SOURCES.get(src.get("type"))
                if cls is None:
                    row["error"] = "unsupported"
                    summary["errors"].append(f"unknown source type {src.get('type')}")
                    continue
                try:
                    raws = cls().fetch(src["key"], src.get("employer"))
                except SourceError as exc:
                    row["error"] = "not_found" if getattr(getattr(exc.__cause__, "response", None), "status_code", None) == 404 else "unreachable"
                    summary["errors"].append(str(exc))
                    continue
                for raw in raws:
                    job, created = disc.ingest_raw(raw)
                    summary["ingested"] += 1
                    summary["new"] += int(created)
                    row["found"] += 1
                    row["new"] += int(created)
                    if created and len(summary["new_job_ids"]) < NEW_JOB_IDS_KEPT:
                        summary["new_job_ids"].append(job["_id"])
                    if created or job.get("status") == "new":
                        q = disc.qualify_job(job["_id"])
                        if q["status"] == "qualified":
                            summary["qualified"] += 1
                            try:
                                disc.rank_fit(job["_id"])
                            except Exception as exc:  # budget or provider problems must not stop discovery
                                summary["errors"].append(f"rank {job['_id']}: {type(exc).__name__}")
        except Exception as exc:
            # A board that answers with something unexpected must not leave the task looking busy forever.
            log.exception("discovery stopped early")
            summary["errors"].append(f"stopped early: {type(exc).__name__}")
            self.rt.store.update_run(run["_id"], status="failed", summary=summary, error=f"stopped early: {type(exc).__name__}", finished_at=utcnow(), progress=None)
            return summary
        self.rt.store.update_run(run["_id"], status="completed", summary=summary, finished_at=utcnow(), progress=None)
        return summary


def worker_healthy(rt: Runtime, max_age_seconds: int = 120) -> bool:
    hb = rt.store.get_control("worker_heartbeat")
    if not hb:
        return False
    at = hb.get("at")
    return at is not None and (utcnow() - at).total_seconds() < max_age_seconds


def published_schedule(rt: Runtime) -> tuple[list[tuple[int, int]], ZoneInfo]:
    """(times of day, their zone) the running worker checks the boards at. Read from what the worker saved at its
    first pass; before any worker has said so, from this process's own settings."""
    saved = rt.store.get_control(SCHEDULE_CONTROL)
    if isinstance(saved, dict):
        try:
            return parse_times(",".join(saved.get("times") or [])), ZoneInfo(str(saved.get("timezone") or "UTC"))
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return parse_times(rt.settings.discovery_schedule), local_zone()


def next_slot_description(rt: Runtime) -> str:
    times, zone = published_schedule(rt)
    if not times:
        return "disabled"
    return f"daily at {','.join(times_text(times))} {zone.key}"
