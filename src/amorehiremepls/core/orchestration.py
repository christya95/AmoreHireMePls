"""Durable workflows with LangGraph + MongoDBSaver.

Application graph:  qualify -> prepare -> review -> execute -> record
  * review interrupts for missing inputs and for authorization (dashboard grants a scope, then resumes)
  * execute interrupts for handoffs (login challenge, unsupported site) and resumes with a manual record
  * every node checks pause/cancel requests; a pause raises at a node boundary and the checkpoint
    already holds the completed steps, so `resume()` continues without redoing them
  * the side effect itself is guarded by the idempotent action store, so replaying `execute`
    after a crash returns the recorded outcome instead of acting twice
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt

from amorehiremepls.core.actions import REPLAY_LABEL
from amorehiremepls.core.db import CHECKPOINT_COLLECTION, CHECKPOINT_WRITES_COLLECTION, Store
from amorehiremepls.core.util import redact_free, utcnow

log = logging.getLogger(__name__)


class Paused(Exception):
    pass


class Cancelled(Exception):
    pass


class AppState(TypedDict, total=False):
    run_id: str
    job_id: str
    application_id: str
    worker: str
    form_fields: list[dict]
    status: str
    outcome: dict
    notes: list[str]


class Services(Protocol):
    def qualify(self, job_id: str) -> dict: ...
    def scan_fields(self, job: dict) -> list[dict]: ...
    def prepare(self, job_id: str, fields: list[dict]) -> dict: ...
    def supply_inputs(self, application_id: str, values: dict) -> dict: ...
    def has_authorization(self, application: dict) -> bool: ...
    def execute(self, application_id: str, worker: str) -> dict: ...
    def record_manual(self, application_id: str, manual: dict) -> dict: ...
    def after_submission(self, application_id: str) -> dict: ...
    def get_application(self, application_id: str) -> dict: ...


def make_checkpointer(store: Store | None) -> BaseCheckpointSaver:
    if store is None:
        return InMemorySaver()
    from langgraph.checkpoint.mongodb import MongoDBSaver

    return MongoDBSaver(store.client, db_name=store.db.name, checkpoint_collection_name=CHECKPOINT_COLLECTION, writes_collection_name=CHECKPOINT_WRITES_COLLECTION)


def build_application_graph(services: Services, store: Store | None, checkpointer: BaseCheckpointSaver):
    def guard(state: AppState) -> None:
        if store is None:
            return
        req = store.run_requested_state(state["run_id"])
        if req == "cancel":
            raise Cancelled()
        if req == "pause":
            raise Paused()

    def step(state: AppState, message: str) -> None:
        if store is not None and state.get("run_id"):
            store.run_event(state["run_id"], message)

    def qualify(state: AppState) -> dict:
        guard(state)
        # An approval that arrived after the previous task finished starts here. The package is already
        # approved, so this pass applies it and does not read the form or rebuild the resume.
        if state.get("application_id") and state.get("status") == "authorized":
            step(state, "Applying the approved package")
            return {"status": "authorized", "application_id": state["application_id"]}
        step(state, "Checking the job")
        job = services.qualify(state["job_id"])
        return {"status": job.get("status"), "notes": [f"qualified: {job.get('status')}"]}

    def route_after_qualify(state: AppState) -> str:
        if state.get("application_id") and state.get("status") == "authorized":
            return "execute"
        return "prepare" if state.get("status") in ("qualified", "needs_review") else END

    def prepare(state: AppState) -> dict:
        guard(state)
        step(state, "Preparing the application")
        fields = state.get("form_fields") or services.scan_fields(services.qualify_cached(state["job_id"]) if hasattr(services, "qualify_cached") else {"_id": state["job_id"]})
        app = services.prepare(state["job_id"], fields)
        return {"application_id": app["_id"], "status": app["status"], "form_fields": fields}

    def review(state: AppState) -> dict:
        guard(state)
        step(state, "Checking what still needs you")
        app = services.get_application(state["application_id"])
        while app["status"] == "needs_input":
            answer = interrupt({"type": "needs_input", "application_id": app["_id"], "outstanding": app["review_package"]["outstanding"]})
            if answer and answer.get("inputs"):
                app = services.supply_inputs(app["_id"], answer["inputs"])
            else:
                return {"status": app["status"], "notes": ["review: inputs still missing"]}
        if app["status"] in ("duplicate", "blocked"):
            return {"status": app["status"]}
        if not services.has_authorization(app):
            ask = {"type": "awaiting_authorization", "application_id": app["_id"], "material_hash": app["material_hash"], "package": _compact_package(app)}
            decision = interrupt(ask)
            if not decision or not decision.get("authorized"):
                # A resume that is not an approval must leave the task parked. Ending here is what left an
                # approved package with no task to send it.
                interrupt(ask)
                return {"status": "ready_for_review", "notes": ["review: authorization not granted"]}
        return {"status": "authorized"}

    def route_after_review(state: AppState) -> str:
        return "execute" if state.get("status") == "authorized" else END

    def _note_saved_failure(state: AppState, outcome: dict) -> None:
        """A later pass that only replays a finished attempt still records the earlier stop line."""
        if outcome.get("status") not in ("failed", "needs_verification"):
            return
        reason = str(outcome.get("reason") or "").strip()
        if reason != REPLAY_LABEL:
            return
        line = ""
        if store is not None and outcome.get("action_id"):
            action = store.c("actions").find_one({"_id": outcome["action_id"]}, {"outcome_reason": 1, "last_failure": 1}) or {}
            stored = str(action.get("outcome_reason") or action.get("last_failure") or "").strip()
            if stored and stored != REPLAY_LABEL:
                line = _plain_log(stored)
        if not line:
            line = evidence_failure_line(reason, outcome.get("evidence"))
        if line:
            step(state, line)

    def execute(state: AppState) -> dict:
        guard(state)
        step(state, "Applying on the job site")
        outcome = services.execute(state["application_id"], state.get("worker", "worker"))
        _note_saved_failure(state, outcome)
        if outcome["status"] == "handoff":
            manual = interrupt({"type": "handoff", "application_id": state["application_id"], "reason": outcome.get("reason"), "evidence": outcome.get("evidence", {})})
            if manual and manual.get("manual"):
                services.record_manual(state["application_id"], manual["manual"])
                return {"status": "submitted", "outcome": {"status": "succeeded", "basis": "explicit_user_confirmation"}}
            return {"status": "handoff", "outcome": outcome}
        status = {
            "succeeded": "submitted",
            "needs_verification": "needs_verification",
            "failed": "failed",
            "denied": "ready_for_review",
            "blocked": "blocked",
            "retry_scheduled": "challenge_retry",
        }.get(outcome["status"], outcome["status"])
        return {"status": status, "outcome": outcome}

    def route_after_execute(state: AppState) -> str:
        return "record" if state.get("status") == "submitted" else END

    def record(state: AppState) -> dict:
        guard(state)
        step(state, "Saving the result")
        info = services.after_submission(state["application_id"])
        return {"notes": [f"recorded: {info}"]}

    g = StateGraph(AppState)
    g.add_node("qualify", qualify)
    g.add_node("prepare", prepare)
    g.add_node("review", review)
    g.add_node("execute", execute)
    g.add_node("record", record)
    g.set_entry_point("qualify")
    g.add_conditional_edges("qualify", route_after_qualify, {"prepare": "prepare", "execute": "execute", END: END})
    g.add_edge("prepare", "review")
    g.add_conditional_edges("review", route_after_review, {"execute": "execute", END: END})
    g.add_conditional_edges("execute", route_after_execute, {"record": "record", END: END})
    g.add_edge("record", END)
    return g.compile(checkpointer=checkpointer)


def _compact_package(app: dict) -> dict:
    pkg = app.get("review_package") or {}
    return {
        "employer": pkg.get("employer"),
        "title": pkg.get("title"),
        "requisition_id": pkg.get("requisition_id"),
        "document": pkg.get("document"),
        # self-identification answers stay in the review package only; the run record never carries them
        "answers": [{"label": a["label"], "value": a["value"]} for a in pkg.get("answers", []) if a.get("value") and not a.get("sensitive")],
        "gaps": pkg.get("gaps"),
        "outstanding": pkg.get("outstanding"),
    }


class Runner:
    """Starts and resumes runs; records interrupts, pauses, cancellations and completion on the run doc."""

    def __init__(self, store: Store | None, graph) -> None:
        self.store = store
        self.graph = graph

    def _config(self, thread_id: str) -> dict:
        return {"configurable": {"thread_id": thread_id}}

    def start_application_run(self, job_id: str, worker: str = "worker", form_fields: list[dict] | None = None) -> dict:
        run = self.store.create_run("application", {"job_id": job_id}) if self.store else {"_id": "run_local", "thread_id": f"t_{job_id}"}
        state: AppState = {"run_id": run["_id"], "job_id": job_id, "worker": worker, "notes": []}
        if form_fields is not None:
            state["form_fields"] = form_fields
        return self._invoke(run, state)

    def resume(self, run_id: str, payload: Any = None) -> dict:
        run = self._run(run_id)
        if run["status"] in ("completed", "cancelled"):
            return run
        if run["status"] == "waiting" and not payload:
            return run
        if self.store:
            self.store.update_run(run_id, requested=None)
        value = Command(resume=payload) if payload is not None and run["status"] == "waiting" else None
        return self._invoke(run, value)

    def request(self, run_id: str, what: str) -> None:
        assert what in ("pause", "cancel")
        if self.store:
            self.store.update_run(run_id, requested=what)
            self.store.run_event(run_id, f"{what} requested")

    def _run(self, run_id: str) -> dict:
        if self.store:
            return self.store.c("runs").find_one({"_id": run_id})
        return {"_id": run_id, "thread_id": run_id, "status": "queued"}

    def _invoke(self, run: dict, value: Any) -> dict:
        cfg = self._config(run["thread_id"])
        self._set(run["_id"], "running", started_at=utcnow())
        try:
            result = self.graph.invoke(value, cfg)
        except Paused:
            return self._set(run["_id"], "paused", note="paused at node boundary; resume() continues from the checkpoint")
        except Cancelled:
            return self._set(run["_id"], "cancelled", note="cancelled before the next node")
        except Exception as exc:  # keep the checkpoint; surface the error
            log.exception("run %s failed", run["_id"])
            return self._set(run["_id"], "failed", error=f"{type(exc).__name__}: {exc}"[:400])
        interrupts = result.get("__interrupt__") if isinstance(result, dict) else None
        if interrupts:
            payload = interrupts[0].value if hasattr(interrupts[0], "value") else interrupts[0]
            return self._set(run["_id"], "waiting", interrupt=payload, state=_public_state(result))
        return self._set(run["_id"], "completed", state=_public_state(result), interrupt=None, finished_at=utcnow())

    def _set(self, run_id: str, status: str, **fields: Any) -> dict:
        if self.store:
            self.store.update_run(run_id, status=status, **fields)
            self.store.run_event(run_id, status)
            return self.store.c("runs").find_one({"_id": run_id})
        return {"_id": run_id, "status": status, **fields}

    def state(self, run_id: str) -> dict:
        run = self._run(run_id)
        snap = self.graph.get_state(self._config(run["thread_id"]))
        return {"values": _public_state(snap.values), "next": list(snap.next), "tasks": [getattr(t, "name", str(t)) for t in snap.tasks]}


def _plain_log(text: str) -> str:
    cleaned = " ".join(str(text or "").split())
    cut = cleaned.lower().find("call log:")
    if cut >= 0:
        cleaned = cleaned[:cut].strip()
    return redact_free(cleaned)[:300]


def evidence_failure_line(reason: str, evidence: Any) -> str:
    """The earlier stop line kept when a later pass only has the replay label. Typed values stay out."""
    if str(reason or "").strip() != REPLAY_LABEL:
        return ""
    ev = evidence if isinstance(evidence, dict) else {}
    err = _plain_log(str(ev.get("error") or ""))
    if err:
        return err
    missing: list[str] = []
    pre = ev.get("preflight") if isinstance(ev.get("preflight"), dict) else {}
    for item in pre.get("missing") or []:
        if isinstance(item, dict) and item.get("label"):
            missing.append(str(item["label"]))
    if missing:
        return _plain_log("the form is missing fields the app expected: " + ", ".join(missing))
    errs = [str(item) for item in (ev.get("validation_errors") or [])][:3]
    if errs:
        return _plain_log("the form reported a problem: " + "; ".join(errs))
    return ""


def _public_state(values: dict) -> dict:
    return {k: v for k, v in (values or {}).items() if k != "__interrupt__"}


LIVE_RUN = ("queued", "claimed", "running", "waiting", "paused")
_FINISHED = ("succeeded", "failed", "needs_verification")


def _same_finished_answers(action: dict | None, app: dict) -> bool:
    """True when queueing would only hand back a result already saved for these answers."""
    if not action or action.get("status") not in _FINISHED:
        return False
    if action.get("material_hash") == app.get("material_hash"):
        return True
    return action.get("status") in ("succeeded", "needs_verification")


def queue_approved_send(store: Store, app: dict, allowed: bool, *, replace_repeat: bool = False) -> bool:
    """Queue one apply task for an approved application that has no task working on it.

    Does not read the form again and does not rebuild the resume. The same answers are not opened again
    after a finished attempt. A different set of answers, once approved, is a new attempt. `replace_repeat`
    is the explicit button after a finished failure: it queues that new attempt even when an earlier
    approval already queued a task that only returned the saved result."""
    if not allowed or app.get("status") != "authorized" or not app.get("_id") or not app.get("job_id"):
        return False
    if store.c("runs").find_one({"params.job_id": app["job_id"], "status": {"$in": list(LIVE_RUN)}}):
        return False
    action = store.c("actions").find_one({"idempotency_key": f"submit:{app['job_id']}"})
    if _same_finished_answers(action, app):
        return False
    now = utcnow()
    params = {"job_id": app["job_id"], "application_id": app["_id"], "phase": "execute", "material_hash": app.get("material_hash")}
    if replace_repeat:
        taken = store.c("applications").update_one(
            {"_id": app["_id"], "status": "authorized", "fill_for": {"$ne": app.get("material_hash")}},
            {"$set": {"send_queued_at": now, "fill_for": app.get("material_hash"), "updated_at": now}},
        )
        if taken.modified_count != 1:
            return False
        store.create_run("application", params)
        return True
    approved_at = max((h.get("at") for h in (app.get("history") or []) if h.get("status") == "authorized" and h.get("at")), default=None)
    queued_at = app.get("send_queued_at")
    if queued_at and (approved_at is None or queued_at >= approved_at):
        return False
    fresh = [{"send_queued_at": None}, {"send_queued_at": {"$exists": False}}]
    if approved_at is not None:
        fresh.append({"send_queued_at": {"$lt": approved_at}})
    taken = store.c("applications").update_one(
        {"_id": app["_id"], "status": "authorized", "$or": fresh},
        {"$set": {"send_queued_at": now, "fill_for": app.get("material_hash"), "updated_at": now}},
    )
    if taken.modified_count != 1:
        return False
    store.create_run("application", params)
    return True
