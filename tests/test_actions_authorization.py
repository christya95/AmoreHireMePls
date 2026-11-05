import threading
from datetime import timedelta

import pytest

from amorehiremepls.core.actions import ActionFailed, ActionStore, AmbiguousOutcome
from amorehiremepls.core.authorization import AuthorizationStore, ScopeBinding, material_hash

pytestmark = pytest.mark.mongo


def _grant(store, target, mat, **kw):
    return AuthorizationStore(store).grant("application_submit", ScopeBinding(target_id=target, material_hash=mat), **kw)


def test_no_action_without_matching_authorization(store):
    acts = ActionStore(store)
    mat = material_hash(answers={"q": "a"}, document_hashes=["abc"])
    a = acts.prepare("application_submit", "job1", "submit:job1", {"x": 1}, mat)
    calls = []
    out = acts.execute(a["_id"], "w1", lambda d: calls.append(1) or {"verified": True})
    assert out.status == "denied" and not calls


def test_wrong_target_expired_and_changed_material_fail_closed(store):
    acts = ActionStore(store)
    mat = material_hash(answers={"q": "a"}, document_hashes=["abc"])
    # scope for another job
    _grant(store, "job_other", mat)
    a = acts.prepare("application_submit", "job1", "submit:job1", {}, mat)
    assert acts.execute(a["_id"], "w", lambda d: {"verified": True}).status == "denied"
    # expired scope for the right job
    _grant(store, "job1", mat, ttl=timedelta(seconds=-1))
    assert acts.execute(a["_id"], "w", lambda d: {"verified": True}).status == "denied"
    # valid scope but material changed afterwards (document re-tailored)
    _grant(store, "job1", mat)
    changed = material_hash(answers={"q": "a"}, document_hashes=["def"])
    a2 = acts.prepare("application_submit", "job1", "submit:job1", {}, changed)
    out = acts.execute(a2["_id"], "w", lambda d: {"verified": True})
    assert out.status == "denied" and "material content changed" in out.reason


def test_success_consumes_scope_and_replay_does_not_rerun_adapter(store):
    acts = ActionStore(store)
    mat = material_hash(answers={"q": "a"})
    scope = _grant(store, "job1", mat)
    a = acts.prepare("application_submit", "job1", "submit:job1", {}, mat)
    calls = []

    def adapter(doc):
        calls.append(doc["_id"])
        return {"verified": True, "receipt": "Thank you"}

    first = acts.execute(a["_id"], "w1", adapter)
    assert first.status == "succeeded" and first.adapter_ran
    second = acts.execute(a["_id"], "w2", adapter)
    assert second.status == "succeeded" and not second.adapter_ran and len(calls) == 1
    assert AuthorizationStore(store).get(scope.id).status == "consumed"
    # a brand-new action for the same job after the scope was consumed is denied
    a3 = acts.prepare("application_submit", "job1", "submit:job1:v2", {}, mat)
    assert acts.execute(a3["_id"], "w1", adapter).status == "denied"


def test_timeout_after_click_becomes_needs_verification_and_blocks_retry(store):
    acts = ActionStore(store)
    mat = material_hash(message="hi")
    _grant(store, "job1", mat)
    a = acts.prepare("application_submit", "job1", "submit:job1", {}, mat)

    def flaky(doc):
        raise AmbiguousOutcome("timeout after final click", {"last_url": "https://ats.example/submit"})

    out = acts.execute(a["_id"], "w1", flaky)
    assert out.status == "needs_verification"
    calls = []
    again = acts.execute(a["_id"], "w1", lambda d: calls.append(1) or {"verified": True})
    assert again.status == "needs_verification" and not calls
    acts.reconcile(a["_id"], "succeeded", {"verified": True, "basis": "employer status page showed Submitted"})
    assert acts.get(a["_id"])["status"] == "succeeded"


def test_concurrent_workers_execute_once(store):
    acts = ActionStore(store)
    mat = material_hash(answers={"a": 1})
    _grant(store, "job1", mat)
    a = acts.prepare("application_submit", "job1", "submit:job1", {}, mat)
    ran = []
    barrier = threading.Barrier(6)

    def adapter(doc):
        ran.append(1)
        return {"verified": True}

    def worker(i):
        barrier.wait()
        acts.execute(a["_id"], f"w{i}", adapter)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(ran) == 1


def test_emergency_stop_blocks_and_failure_stops_scope(store):
    acts = ActionStore(store)
    mat = material_hash(answers={"a": 1})
    _grant(store, "job1", mat, max_count=3, mode="single")
    a = acts.prepare("application_submit", "job1", "submit:job1", {}, mat)
    store.set_emergency_stop(True, "test")
    assert acts.execute(a["_id"], "w", lambda d: {"verified": True}).status == "blocked"
    store.set_emergency_stop(False)

    def fail(doc):
        raise ActionFailed("validation error: phone required", {"field": "phone"})

    out = acts.execute(a["_id"], "w", fail)
    assert out.status == "failed"
    assert AuthorizationStore(store).active("application_submit") == []  # max_failures=1 stopped the scope
