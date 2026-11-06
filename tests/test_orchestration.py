from datetime import UTC, datetime

import pytest

from amorehiremepls.core.orchestration import Runner, build_application_graph, make_checkpointer

pytestmark = pytest.mark.mongo


class FakeServices:
    """In-memory stand-in for qualification/preparation/execution with controllable behaviour."""

    def __init__(self, store, needs_input=True, authorized=False, exec_status="succeeded"):
        self.store = store
        self.apps = {}
        self.needs_input = needs_input
        self.authorized = authorized
        self.exec_status = exec_status
        self.exec_calls = 0
        self.recorded = []

    def qualify(self, job_id):
        return {"_id": job_id, "status": "qualified"}

    def scan_fields(self, job):
        return [{"id": "fn", "label": "First name"}]

    def prepare(self, job_id, fields):
        app = {
            "_id": f"app_{job_id}",
            "status": "needs_input" if self.needs_input else "ready_for_review",
            "material_hash": "m1",
            "review_package": {"outstanding": ["answer needed: Why us?"] if self.needs_input else [], "answers": [], "employer": "Acme", "title": "Dev"},
        }
        self.apps[app["_id"]] = app
        return app

    def get_application(self, app_id):
        return self.apps[app_id]

    def supply_inputs(self, app_id, values):
        app = self.apps[app_id]
        app["status"], app["review_package"]["outstanding"] = "ready_for_review", []
        return app

    def has_authorization(self, app):
        return self.authorized

    def execute(self, app_id, worker):
        self.exec_calls += 1
        if self.exec_status == "succeeded":
            self.apps[app_id]["status"] = "submitted"
        return {"status": self.exec_status, "reason": "fake", "evidence": {}}

    def record_manual(self, app_id, manual):
        self.apps[app_id]["status"] = "submitted"
        self.recorded.append(manual)
        return self.apps[app_id]

    def after_submission(self, app_id):
        return {"outbox": "enqueued"}


def _runner(store, svc):
    return Runner(store, build_application_graph(svc, store, make_checkpointer(store)))


def test_interrupts_for_inputs_then_authorization_then_completes_and_restart_resumes(store):
    svc = FakeServices(store, needs_input=True, authorized=False)
    runner = _runner(store, svc)
    run = runner.start_application_run("job1")
    assert run["status"] == "waiting" and run["interrupt"]["type"] == "needs_input"
    # "restart": new graph instance, same checkpointer collections -> resumes from Mongo checkpoint
    runner2 = _runner(store, svc)
    run = runner2.resume(run["_id"], {"inputs": {"why": "x"}})
    assert run["status"] == "waiting" and run["interrupt"]["type"] == "awaiting_authorization" and run["interrupt"]["material_hash"] == "m1"
    svc.authorized = True
    run = runner2.resume(run["_id"], {"authorized": True})
    assert run["status"] == "completed" and run["state"]["status"] == "submitted" and svc.exec_calls == 1
    assert store.c("lg_checkpoints").count_documents({"thread_id": run["thread_id"]}) >= 3


def test_pause_and_resume_do_not_redo_completed_nodes_and_cancel_stops(store):
    svc = FakeServices(store, needs_input=False, authorized=True)
    runner = _runner(store, svc)
    run = runner.start_application_run("job2")  # no interrupts -> completes
    assert run["status"] == "completed" and svc.exec_calls == 1
    from langgraph.types import Command

    svc2 = FakeServices(store, needs_input=True, authorized=True)
    runner2 = _runner(store, svc2)
    run2 = runner2.start_application_run("job3")
    assert run2["status"] == "waiting"
    runner2.request(run2["_id"], "pause")  # pause lands while the operator is answering
    paused = runner2._invoke(store.c("runs").find_one({"_id": run2["_id"]}), Command(resume={"inputs": {"a": "b"}}))
    assert paused["status"] == "paused" and svc2.exec_calls == 0
    # resume clears the pause; the answer given with the paused invoke was kept as a pending write, so the run completes
    done = runner2.resume(run2["_id"])
    assert done["status"] == "completed" and svc2.exec_calls == 1
    svc3 = FakeServices(store, needs_input=True, authorized=True)
    runner3 = _runner(store, svc3)
    run3 = runner3.start_application_run("job4")
    store.update_run(run3["_id"], requested="cancel")
    cancelled = runner3._invoke(store.c("runs").find_one({"_id": run3["_id"]}), Command(resume={"inputs": {"a": "b"}}))
    assert cancelled["status"] == "cancelled" and svc3.exec_calls == 0
    assert runner3.resume(run3["_id"])["status"] == "cancelled"  # cancelled runs stay cancelled


def test_a_resume_that_is_not_an_approval_leaves_the_task_waiting(store):
    svc = FakeServices(store, needs_input=False, authorized=False)
    runner = _runner(store, svc)
    run = runner.start_application_run("job-wait")
    assert run["status"] == "waiting" and run["interrupt"]["type"] == "awaiting_authorization"
    same = runner.resume(run["_id"], {})
    assert same["status"] == "waiting" and same["interrupt"]["type"] == "awaiting_authorization" and svc.exec_calls == 0
    untouched = runner.resume(run["_id"], None)
    assert untouched["status"] == "waiting" and svc.exec_calls == 0
    svc.authorized = True
    done = runner.resume(run["_id"], {"authorized": True})
    assert done["status"] == "completed" and svc.exec_calls == 1


def test_an_already_approved_package_is_applied_without_preparing_again(store):
    from amorehiremepls.core.orchestration import queue_approved_send

    svc = FakeServices(store, needs_input=False, authorized=True)
    svc.apps["app_ready"] = {"_id": "app_ready", "status": "authorized", "material_hash": "m1", "review_package": {"outstanding": [], "answers": []}}

    def prepare(job_id, fields):
        raise AssertionError("the approved package must not be rebuilt")

    svc.prepare = prepare
    app = {
        "_id": "app_ready",
        "job_id": "job-ready",
        "status": "authorized",
        "material_hash": "m1",
        "history": [{"at": datetime(2026, 11, 5, tzinfo=UTC), "status": "authorized"}],
        "review_package": {},
        "provenance": {"origin": "local"},
    }
    store.c("applications").insert_one(app)
    assert queue_approved_send(store, app, True) is True
    assert queue_approved_send(store, store.c("applications").find_one({"_id": "app_ready"}), True) is False
    run = store.c("runs").find_one({"params.application_id": "app_ready"})
    assert run["params"]["phase"] == "execute" and run["status"] == "queued"
    done = _runner(store, svc)._invoke(run, {"run_id": run["_id"], "job_id": "job-ready", "application_id": "app_ready", "status": "authorized", "worker": "worker", "notes": []})
    assert done["status"] == "completed" and svc.exec_calls == 1


def test_the_same_answers_are_not_queued_and_changed_answers_are(store):
    from amorehiremepls.core.orchestration import queue_approved_send

    when = datetime(2026, 11, 6, tzinfo=UTC)
    app = {
        "_id": "app_gap",
        "job_id": "job_gap",
        "status": "authorized",
        "material_hash": "m1",
        "history": [{"at": when, "status": "authorized"}],
        "review_package": {},
        "provenance": {"origin": "local"},
    }
    store.c("applications").insert_one(app)
    store.c("actions").insert_one(
        {
            "_id": "act_gap",
            "idempotency_key": "submit:job_gap",
            "kind": "application_submit",
            "target_id": "job_gap",
            "status": "failed",
            "material_hash": "m1",
            "outcome_reason": "saved",
        }
    )
    assert queue_approved_send(store, app, True) is False
    assert store.c("runs").count_documents({"params.application_id": "app_gap"}) == 0
    store.c("applications").update_one({"_id": "app_gap"}, {"$set": {"material_hash": "m2"}})
    changed = store.c("applications").find_one({"_id": "app_gap"})
    assert queue_approved_send(store, changed, True) is True
    run = store.c("runs").find_one({"params.application_id": "app_gap", "status": "queued"})
    assert run["params"]["phase"] == "execute" and run["params"]["material_hash"] == "m2"
    assert queue_approved_send(store, changed, True) is False
    store.c("runs").update_one({"_id": run["_id"]}, {"$set": {"status": "completed"}})
    store.c("applications").update_one({"_id": "app_gap"}, {"$unset": {"fill_for": ""}, "$set": {"send_queued_at": datetime(2026, 11, 6, 2, tzinfo=UTC)}})
    changed = store.c("applications").find_one({"_id": "app_gap"})
    assert queue_approved_send(store, changed, True) is False
    assert queue_approved_send(store, changed, True, replace_repeat=True) is True
    assert store.c("runs").count_documents({"params.application_id": "app_gap", "params.material_hash": "m2"}) == 2


def test_handoff_interrupt_then_manual_record(store):
    svc = FakeServices(store, needs_input=False, authorized=True, exec_status="handoff")
    runner = _runner(store, svc)
    run = runner.start_application_run("job5")
    assert run["status"] == "waiting" and run["interrupt"]["type"] == "handoff"
    run = runner.resume(run["_id"], {"manual": {"confirmation": "Thank you page seen", "application_date": "2026-11-04"}})
    assert run["status"] == "completed" and svc.recorded and run["state"]["status"] == "submitted"


def test_execute_replay_after_crash_does_not_rerun_side_effect(store):
    """Crash after execute's side effect but before the checkpoint write: the node re-runs on resume, but
    the services layer (ActionStore) returns the recorded outcome. Here we simulate with a counter."""

    class CrashOnce(FakeServices):
        def __init__(self, store):
            super().__init__(store, needs_input=False, authorized=True)
            self.crashed = False

        def execute(self, app_id, worker):
            out = super().execute(app_id, worker)
            if not self.crashed:
                self.crashed = True
                raise RuntimeError("process died after the click")
            return out

    svc = CrashOnce(store)
    runner = _runner(store, svc)
    run = runner.start_application_run("job6")
    assert run["status"] == "failed" and svc.exec_calls == 1
    run = runner.resume(run["_id"])
    assert run["status"] == "completed"
    # In production ActionStore.execute returns the recorded outcome for the same idempotency key (tested in
    # test_actions_authorization); here the fake confirms the node re-entered exactly once more.
    assert svc.exec_calls == 2
