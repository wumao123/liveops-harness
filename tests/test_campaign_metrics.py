import json

import httpx
import pytest
from fastapi.testclient import TestClient

from liveops.api import create_app
from liveops.campaigns import CampaignHarness
from liveops.metrics import campaign_metrics
from liveops.specialists import SpecialistModel
from liveops.store import Store


LIVE = "LIVE-DEMO-001"


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.delenv("LIVEOPS_SPECIALIST_MODEL", raising=False)
    h = CampaignHarness(Store(str(tmp_path / "business.db")))
    h.model = SpecialistModel("https://model.invalid/v1", "fixture", "empty")
    return h


def response(body, usage=None, status=200):
    payload = {"choices": [{"message": {"content": body}}]}
    if usage is not None:
        payload["usage"] = usage
    return httpx.Response(status, request=httpx.Request("POST", "https://model.invalid/v1"), json=payload)


def valid_body(context):
    p = context["products"][0]
    return json.dumps({"sku": p["sku"], "title": p["title"], "claims": p["features"].split("；"),
                       "price_cents": p["price_cents"], "stock": p["stock"],
                       "product_version": p["version"], "source": f"product:{p['sku']}@{p['version']}"})


def test_failed_json_retains_billed_usage(harness, monkeypatch):
    monkeypatch.setattr("liveops.specialists.httpx.post", lambda *a, **k: response("bad JSON", {
        "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}))
    plan = harness.create(LIVE, "成本测试", top_k=1)
    done = harness.run(LIVE, plan["run_id"])
    m = campaign_metrics(done)
    assert m["model_calls_recorded"] == m["failed_task_model_calls"] == 2
    assert m["reported_total_tokens"] == 240
    assert m["reported_prompt_tokens"] == 200 and m["reported_completion_tokens"] == 40
    assert m["token_breakdown_complete"] is True
    assert m["actual_currency_cost"] is None
    attempts = done["trace"][-1]["provider_attempts"]
    assert all(x["outcome"] == "schema_error" and x["elapsed_ms"] >= 0 for x in attempts)


def test_repair_success_counts_both_calls(harness, monkeypatch):
    calls = []

    def post(*args, **kwargs):
        calls.append(kwargs)
        context = json.loads(kwargs["json"]["messages"][1]["content"])
        return response("bad JSON" if len(calls) == 1 else valid_body(context), {
            "prompt_tokens": 100, "completion_tokens": 20})

    monkeypatch.setattr("liveops.specialists.httpx.post", post)
    plan = harness.create(LIVE, "修复测试", top_k=1)
    done = harness.run(LIVE, plan["run_id"])
    m = campaign_metrics(done)
    assert done["status"] == "succeeded"
    assert m["model_calls_recorded"] == 2 and m["reported_total_tokens"] == 240
    assert m["extra_calls_within_task_attempts"] == 1


@pytest.mark.parametrize("fault", ["http", "network"])
def test_missing_usage_is_unknown_not_free(harness, monkeypatch, fault):
    def post(*args, **kwargs):
        if fault == "network":
            raise httpx.ReadTimeout("fixture timeout")
        return response("error", status=429)

    monkeypatch.setattr("liveops.specialists.httpx.post", post)
    plan = harness.create(LIVE, "无回执测试", top_k=1)
    done = harness.run(LIVE, plan["run_id"])
    m = campaign_metrics(done)
    assert m["model_calls_recorded"] == 2
    assert m["calls_without_reported_totals"] == 2
    assert m["usage_status"] == "partial_or_unknown"
    assert m["token_breakdown_complete"] is False and m["actual_currency_cost"] is None


def test_nested_retries_are_visible(harness, monkeypatch):
    # Counts every nested retry, so the circuit breaker must not short-circuit here (it is tested separately).
    monkeypatch.setattr("liveops.model_budget.breaker.failures", 20)
    monkeypatch.setattr("liveops.specialists.httpx.post", lambda *a, **k: response("error", status=500))
    plan = harness.create(LIVE, "叠加重试测试", top_k=3)
    harness.run(LIVE, plan["run_id"])
    done = harness.run(LIVE, plan["run_id"], retry_failed=True)
    m = campaign_metrics(done)
    assert m["model_calls_recorded"] == 12
    assert m["calls_without_reported_totals"] == 12
    assert all(done["tasks"][f"brief_{i}"]["attempts"] == 2 for i in range(3))


def test_total_only_usage_does_not_invent_input_output(harness, monkeypatch):
    def post(*args, **kwargs):
        context = json.loads(kwargs["json"]["messages"][1]["content"])
        return response(valid_body(context), {"total_tokens": 123})

    monkeypatch.setattr("liveops.specialists.httpx.post", post)
    plan = harness.create(LIVE, "只有总数测试", top_k=1)
    m = campaign_metrics(harness.run(LIVE, plan["run_id"]))
    assert m["reported_total_tokens"] == 123
    assert m["usage_status"] == "reported_totals_available"
    assert m["token_breakdown_complete"] is False
    assert m["actual_currency_cost"] is None


def test_metrics_endpoint_is_read_only(tmp_path, monkeypatch):
    monkeypatch.delenv("LIVEOPS_SPECIALIST_MODEL", raising=False)
    monkeypatch.delenv("LIVEOPS_LLM_MODEL", raising=False)
    with TestClient(create_app(str(tmp_path / "business.db"), str(tmp_path / "checkpoints.db"))) as client:
        before = client.get(f"/api/state/{LIVE}").json()
        plan = client.post("/api/campaigns", json={"instruction": "手卡", "top_k": 1}).json()
        client.post(f"/api/campaigns/{LIVE}/{plan['run_id']}/advance", json={})
        m = client.get(f"/api/campaigns/{LIVE}/{plan['run_id']}/metrics").json()
        assert m["usage_status"] == "no_model_calls_recorded"
        assert m["completed_tasks"] == 4 and m["actual_currency_cost"] is None
        assert client.get(f"/api/state/{LIVE}").json() == before
        assert client.get(f"/api/campaigns/OTHER/{plan['run_id']}/metrics").status_code == 409


@pytest.mark.parametrize("usage,known_prompt,inconsistent", [
    ({"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 10}, 100, 1),
    ({"prompt_tokens": 100, "completion_tokens": -1, "total_tokens": True}, 100, 0),
    ({"total_tokens": "123"}, 0, 0),
])
def test_incomplete_or_inconsistent_provider_usage_is_not_complete(harness, monkeypatch, usage, known_prompt, inconsistent):
    def post(*args, **kwargs):
        context = json.loads(kwargs["json"]["messages"][1]["content"])
        return response(valid_body(context), usage)

    monkeypatch.setattr("liveops.specialists.httpx.post", post)
    plan = harness.create(LIVE, "异常用量测试", top_k=1)
    m = campaign_metrics(harness.run(LIVE, plan["run_id"]))
    assert m["token_breakdown_complete"] is False
    assert m["usage_status"] == "partial_or_unknown"
    assert m["reported_prompt_tokens"] == known_prompt
    assert m["calls_with_inconsistent_usage"] == inconsistent
