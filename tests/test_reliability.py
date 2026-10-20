"""Circuit breaker, shared connection pool and rule/model agreement."""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from liveops import model_budget
from liveops.api import create_app
from liveops.model_budget import CircuitBreaker, breaker, provider_post
from liveops.store import BusinessError


LIVE = "LIVE-DEMO-001"
URL = "https://model.invalid/v1/chat/completions"


def ok(url):
    return httpx.Response(200, request=httpx.Request("POST", url), json={"choices": [{"message": {"content": "{}"}}]})


def test_breaker_opens_after_consecutive_transport_failures_and_probes_once(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(model_budget.time, "monotonic", lambda: clock[0])
    sent = []

    def down(url, **kwargs):
        sent.append(url)
        raise httpx.ConnectTimeout("down")

    monkeypatch.setattr(model_budget.httpx, "post", down)
    for _ in range(3):
        with pytest.raises(httpx.ConnectTimeout):
            provider_post(URL, phase="intent", json={})
    with pytest.raises(BusinessError) as exc:
        provider_post(URL, phase="intent", json={})
    assert exc.value.code == "MODEL_CIRCUIT_OPEN" and len(sent) == 3
    assert breaker.status()["model.invalid"]["state"] == "open"
    clock[0] += 31
    with pytest.raises(httpx.ConnectTimeout):
        provider_post(URL, phase="intent", json={})  # the single half-open probe
    assert len(sent) == 4 and breaker.status()["model.invalid"]["state"] == "open"
    clock[0] += 31
    monkeypatch.setattr(model_budget.httpx, "post", lambda url, **kwargs: ok(url))
    assert provider_post(URL, phase="intent", json={}).status_code == 200
    assert breaker.status()["model.invalid"]["state"] == "closed"


def test_client_errors_do_not_open_the_breaker(monkeypatch):
    monkeypatch.setattr(model_budget.httpx, "post",
                        lambda url, **kwargs: httpx.Response(400, request=httpx.Request("POST", url)))
    for _ in range(5):
        assert provider_post(URL, phase="intent", json={}).status_code == 400
    assert breaker.status()["model.invalid"]["state"] == "closed"
    monkeypatch.setattr(model_budget.httpx, "post",
                        lambda url, **kwargs: httpx.Response(503, request=httpx.Request("POST", url)))
    for _ in range(3):
        provider_post(URL, phase="intent", json={})
    assert breaker.status()["model.invalid"]["state"] == "open"


def test_open_circuit_falls_back_to_rules_without_waiting_and_keeps_every_write_check(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVEOPS_LLM_BASE_URL", "https://model.invalid/v1")
    monkeypatch.setenv("LIVEOPS_LLM_MODEL", "fixture")
    monkeypatch.setenv("LIVEOPS_INTENT_ROUTING", "model_only")
    monkeypatch.setattr("liveops.agent.time.sleep", lambda _: None)
    sent = []

    def down(url, **kwargs):
        sent.append(url)
        raise httpx.ConnectTimeout("down")

    monkeypatch.setattr(model_budget.httpx, "post", down)
    app = create_app(str(tmp_path / "b.db"), str(tmp_path / "c.db"))
    with TestClient(app) as client:
        say = lambda text: client.post("/api/commands", json={"live_id": LIVE, "instruction": text,
                                                              "command_id": str(uuid.uuid4())}).json()
        first, second = say("查看面霜库存"), say("查看精华库存")
        third = say("把精华价格调到149元")
        assert client.get("/health").json()["model_circuit"]["model.invalid"]["state"] == "open"
    # Two attempts on the first command, then one more trips the breaker; later commands send nothing.
    assert len(sent) == 3
    assert first["interpretation"]["source"] == "offline_fallback" == third["interpretation"]["source"]
    assert third["interpretation"]["fallback_error"]["code"] == "MODEL_CIRCUIT_OPEN"
    assert third["status"] == "pending_approval" and third["action"]["sku"] == "SK-104"


def test_breaker_rejects_out_of_range_settings():
    with pytest.raises(ValueError):
        CircuitBreaker(failures=0)
    with pytest.raises(ValueError):
        CircuitBreaker(failures=3, cooldown_seconds=10_000)


def test_real_sends_reuse_one_pooled_client(monkeypatch):
    monkeypatch.setattr(model_budget, "_client", None)
    created, posted = [], []

    class FakeClient:
        def __init__(self, **kwargs):
            created.append(kwargs)

        def post(self, url, **kwargs):
            posted.append(url)
            return ok(url)

    monkeypatch.setattr(model_budget.httpx, "Client", FakeClient)
    for _ in range(3):
        provider_post(URL, phase="intent", json={})
    assert len(created) == 1 and len(posted) == 3
    monkeypatch.setattr(model_budget, "_client", None)
