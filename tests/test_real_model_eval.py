import httpx
import pytest

from liveops.real_model_eval import LIVE, RecordedProvider, canonical, matches, memory_cases


def test_scoring_normalizes_implicit_current_product():
    snapshot = {"live": {"current_sku": "SK-101"}}
    assert matches({"type": "status", "sku": None}, {"type": "status", "sku": "SK-101"}, snapshot)
    assert not matches({"type": "status", "sku": "SK-102"}, {"type": "status", "sku": "SK-101"}, snapshot)
    assert canonical({"type": "invalid"}, snapshot) == {"type": "invalid"}


def test_provider_budget_never_performs_extra_request(monkeypatch):
    calls = []

    def post(url, **kwargs):
        calls.append(url)
        return httpx.Response(200, json={"model": "fixture", "choices": [{"finish_reason": "stop"}],
                                         "usage": {"total_tokens": 2}})

    monkeypatch.setattr("httpx.post", post)
    provider = RecordedProvider(budget=1)
    provider.post("https://model.invalid")
    with pytest.raises(RuntimeError, match="budget"):
        provider.post("https://model.invalid")
    assert len(calls) == 1
    assert provider.rows[0]["usage"]["total_tokens"] == 2


def test_memory_experiment_contracts_run_without_models(tmp_path):
    rows = memory_cases(tmp_path)
    assert len(rows) == 8
    assert all(row["passed"] for row in rows)
    assert all(row["view"]["live_id"] == LIVE for row in rows)
