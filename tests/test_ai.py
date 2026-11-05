import json
import threading

import pytest

from amorehiremepls.core.ai import AIRunner, Budget, BudgetExhausted, FakeProvider, Pricing, invalidate_cache
from amorehiremepls.core.skills import FitInput, catalog


def fit_response(user: str) -> dict:
    return {"rank": 2, "fit_points": ["emp_1 supports Java backend"], "genuine_gaps": ["Kubernetes"], "missing_evidence": [], "role_family": "backend"}


def _inputs(text="Java backend role", employer="Acme"):
    return FitInput(
        job_title="Backend Developer",
        employer=employer,
        posting_text=text,
        candidate_facts=["emp_1: RBC Software Developer"],
        experience_summary={"full_time_years_completed": 3},
        role_rules={},
    )


def test_pricing_unknown_model_is_unknown_not_zero(tmp_path):
    p = tmp_path / "pricing.json"
    p.write_text(json.dumps({"prices": [{"model": "gpt-test", "effective_date": "2026-11-01", "input_per_1m": 1.0, "output_per_1m": 4.0}]}))
    pricing = Pricing(p)
    assert pricing.cost("openai:gpt-test", 1_000_000, 250_000) == 2.0
    assert pricing.cost("openai:other", 10, 10) is None
    assert pricing.cost("openai:gpt-test", None, 10) is None


@pytest.mark.mongo
def test_cache_hit_and_invalidation_on_fact_or_posting_or_prompt_change(store, tmp_path):
    provider = FakeProvider({"FitOutput": fit_response})
    runner = AIRunner(store, provider, catalog, Pricing(None), Budget(10_000, 1.0, 10, 60), run_id=None)
    r1 = runner.run("assess_fit", _inputs(), fact_version=1, posting_hash="p1", untrusted_fields=("posting_text",))
    r2 = runner.run("assess_fit", _inputs(), fact_version=1, posting_hash="p1", untrusted_fields=("posting_text",))
    assert len(provider.calls) == 1 and r2.usage.cached_result and r1.cache_key == r2.cache_key
    assert not r1.usage.cost_known  # fake model has no pricing -> unknown
    runner.run("assess_fit", _inputs(), fact_version=2, posting_hash="p1")  # facts changed
    runner.run("assess_fit", _inputs(), fact_version=2, posting_hash="p2")  # posting changed
    assert len(provider.calls) == 3
    runner.provider = FakeProvider({"FitOutput": fit_response})
    runner.provider.model = "fake:other-model"
    runner.run("assess_fit", _inputs(), fact_version=2, posting_hash="p2")  # model changed
    assert len(runner.provider.calls) == 1
    assert invalidate_cache(store, skill="assess_fit") == 4
    assert "<untrusted" in provider.calls[0]["user"] and "untrusted data" in provider.calls[0]["user"]


@pytest.mark.mongo
def test_budget_exhaustion_stops_before_call(store):
    provider = FakeProvider({"FitOutput": fit_response}, usage=(5000, 1000))
    runner = AIRunner(store, provider, catalog, Pricing(None), Budget(max_tokens=6500, max_usd=1, max_tool_steps=5, max_seconds=60))
    runner.run("assess_fit", _inputs("a"), posting_hash="1")
    with pytest.raises(BudgetExhausted):
        runner.run("assess_fit", _inputs("b"), posting_hash="2")
    assert len(provider.calls) == 1
    assert runner.budget.summary()["cost_known"] is False


@pytest.mark.mongo
def test_inflight_coalescing(store):
    gate = threading.Event()

    def slow(user):
        gate.wait(2)
        return fit_response(user)

    provider = FakeProvider({"FitOutput": slow})
    runner = AIRunner(store, provider, catalog, Pricing(None))
    results = []

    def go():
        results.append(runner.run("assess_fit", _inputs(), posting_hash="same"))

    ts = [threading.Thread(target=go) for _ in range(3)]
    [t.start() for t in ts]
    import time

    time.sleep(0.2)
    gate.set()
    [t.join() for t in ts]
    assert len(provider.calls) == 1 and len(results) == 3


def test_catalog_describes_versions():
    names = {s["name"]: s["version"] for s in catalog.describe()}
    assert "assess_fit" in names and "tailor_resume" in names and "draft_outreach" in names
