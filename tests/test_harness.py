from __future__ import annotations

import uuid
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import httpx

from fastapi.testclient import TestClient

from liveops.agent import _llm_action, _normalize_action
from liveops.api import create_app
from liveops.context import interpretation_context
from liveops.store import Store


LIVE = "LIVE-DEMO-001"


def make_app(tmp_path):
    return create_app(str(tmp_path / "commerce.db"), str(tmp_path / "graph.db"))


def command(client, instruction, command_id=None):
    return client.post("/api/commands", json={
        "live_id": LIVE,
        "instruction": instruction,
        "command_id": command_id or str(uuid.uuid4()),
    }).json()


def test_price_requires_approval_and_retries_are_idempotent(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        command_id = str(uuid.uuid4())
        first = command(client, "把面霜价格改成 119 元", command_id)
        assert first["status"] == "pending_approval"
        assert app.state.store.product("SK-102")["price_cents"] == 12900

        repeated = command(client, "把面霜价格改成 119 元", command_id)
        assert repeated == first

        approved = client.post(f"/api/commands/{command_id}/approval", json={"approved": True}).json()
        assert approved["status"] == "ok"
        assert app.state.store.product("SK-102")["price_cents"] == 11900
        assert command(client, "把面霜价格改成 119 元", command_id) == approved

        key = approved["result"]["idempotency_key"]
        replay = app.state.store.apply_action(LIVE, command_id, approved["action"], key)
        assert replay["replayed"] is True
        with app.state.store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM action_ledger WHERE command_id=?", (command_id,)).fetchone()[0] == 1


def test_rejection_and_below_cost_are_blocked(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        rejected_id = str(uuid.uuid4())
        assert command(client, "把面霜价格改成 119 元", rejected_id)["status"] == "pending_approval"
        rejected = client.post(f"/api/commands/{rejected_id}/approval", json={"approved": False}).json()
        assert rejected["status"] == "rejected"
        assert app.state.store.product("SK-102")["price_cents"] == 12900

        blocked = command(client, "把面霜价格改成 50 元")
        assert blocked["status"] == "blocked"
        assert app.state.store.product("SK-102")["price_cents"] == 12900


def test_pending_approval_recovers_after_restart_and_checks_fresh_state(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        command_id = str(uuid.uuid4())
        assert command(client, "给面霜发 10 元券 20 张", command_id)["status"] == "pending_approval"

    restarted = make_app(tmp_path)
    with TestClient(restarted) as client:
        pending = client.get(f"/api/commands/{LIVE}").json()
        assert pending[0]["status"] == "pending_approval"
        client.post("/api/simulator/stock", json={"live_id": LIVE, "sku": "SK-102", "stock": 0})
        result = client.post(f"/api/commands/{command_id}/approval", json={"approved": True}).json()
        assert result["status"] == "blocked"
        assert result["result"]["code"] == "STALE_STATE"
        assert restarted.state.store.snapshot(LIVE)["coupons"] == []


def test_switch_updates_state_and_recap_uses_audit(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        result = command(client, "切到 SK-103")
        assert result["status"] == "ok"
        assert app.state.store.live(LIVE)["current_sku"] == "SK-103"
        recap = command(client, "复盘本场操作")
        assert "轻薄防晒乳" in recap["message"]
        assert client.get("/").status_code == 200
        assert client.get("/app.js").status_code == 200


def test_preference_is_persisted_and_applied_to_brief(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        assert command(client, "记住开场先讲百元内商品")["status"] == "ok"
        brief = command(client, "介绍当前商品")
        assert "开场先讲百元内商品" in brief["message"]


def test_approval_rechecks_competing_price_change(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        first_id = str(uuid.uuid4())
        second_id = str(uuid.uuid4())
        assert command(client, "把面霜价格改成 119 元", first_id)["status"] == "pending_approval"
        assert command(client, "把面霜价格改成 109 元", second_id)["status"] == "pending_approval"
        assert client.post(f"/api/commands/{first_id}/approval", json={"approved": True}).json()["status"] == "ok"
        second = client.post(f"/api/commands/{second_id}/approval", json={"approved": True}).json()
        assert second["status"] == "blocked"
        assert second["result"]["code"] == "STALE_STATE"
        assert app.state.store.product("SK-102")["price_cents"] == 11900
        with app.state.store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM action_ledger").fetchone()[0] == 1


def test_coupon_limits_and_command_identity(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        assert command(client, "给面霜发 60 元券 20 张")["status"] == "blocked"
        assert command(client, "给面霜发 10 元券 101 张")["status"] == "blocked"
        assert app.state.store.snapshot(LIVE)["coupons"] == []

        command_id = str(uuid.uuid4())
        assert command(client, "介绍当前商品", command_id)["status"] == "ok"
        conflict = client.post("/api/commands", json={
            "live_id": LIVE,
            "instruction": "复盘本场操作",
            "command_id": command_id,
        })
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "COMMAND_ID_CONFLICT"


def test_model_proposals_stay_inside_tool_allowlist(tmp_path):
    snapshot = Store(str(tmp_path / "commerce.db")).snapshot(LIVE)
    assert _normalize_action({"type": "refund", "amount_cents": 10000}, snapshot)["type"] == "invalid"
    assert _normalize_action({"type": "issue_coupon", "sku": "INVENTED", "amount_cents": 100, "quantity": 1}, snapshot)["type"] == "invalid"
    assert _normalize_action({"type": "update_price", "sku": "SK-102", "amount_cents": 119.5}, snapshot)["type"] == "invalid"
    assert _normalize_action({"type": "issue_coupon", "sku": "SK-102", "amount_cents": 100, "quantity": True}, snapshot)["type"] == "invalid"


def test_model_429_retries_once_then_uses_model(tmp_path, monkeypatch):
    snapshot = Store(str(tmp_path / "commerce.db")).snapshot(LIVE)
    monkeypatch.setenv("LIVEOPS_LLM_BASE_URL", "https://model.example/v1")
    monkeypatch.setenv("LIVEOPS_LLM_MODEL", "test-model")
    monkeypatch.setattr("liveops.agent.time.sleep", lambda _: None)
    calls = []

    def fake_post(url, **kwargs):
        calls.append(kwargs)
        request = httpx.Request("POST", url)
        if len(calls) == 1:
            return httpx.Response(429, request=request)
        return httpx.Response(200, request=request, json={
            "choices": [{"message": {"content": '{"type":"plan"}'}}],
        })

    monkeypatch.setattr("liveops.agent.httpx.post", fake_post)
    outcome = _llm_action("做选品计划", snapshot)
    assert outcome == {"action": {"type": "plan"}, "attempts": 2, "error": None}
    assert len(calls) == 2


def test_model_bad_json_gets_one_correction_then_falls_back(tmp_path, monkeypatch):
    snapshot = Store(str(tmp_path / "commerce.db")).snapshot(LIVE)
    monkeypatch.setenv("LIVEOPS_LLM_BASE_URL", "https://model.example/v1")
    monkeypatch.setenv("LIVEOPS_LLM_MODEL", "test-model")
    monkeypatch.setattr("liveops.agent.time.sleep", lambda _: None)
    calls = []

    def fake_post(url, **kwargs):
        calls.append(kwargs)
        return httpx.Response(200, request=httpx.Request("POST", url), json={
            "choices": [{"message": {"content": "not json"}}],
        })

    monkeypatch.setattr("liveops.agent.httpx.post", fake_post)
    outcome = _llm_action("做选品计划", snapshot)
    assert outcome["action"] is None
    assert outcome["attempts"] == 2
    assert outcome["error"]["code"] == "MODEL_INVALID_JSON"
    assert "invalid" in calls[1]["json"]["messages"][-1]["content"]


def test_model_provider_400_does_not_retry_and_records_fallback(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    monkeypatch.setenv("LIVEOPS_LLM_BASE_URL", "https://model.example/v1")
    monkeypatch.setenv("LIVEOPS_LLM_MODEL", "test-model")
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        return httpx.Response(400, request=httpx.Request("POST", url))

    monkeypatch.setattr("liveops.agent.httpx.post", fake_post)
    with TestClient(app) as client:
        result = command(client, "给我做一个选品计划")
        assert result["status"] == "ok"
        assert result["interpretation"]["source"] == "offline_fallback"
        assert result["interpretation"]["fallback_error"]["code"] == "MODEL_HTTP_400"
        assert len(calls) == 1
        assert any(event["event_type"] == "model_fallback" for event in app.state.store.audit(LIVE))


def test_model_network_timeout_retries_only_twice(tmp_path, monkeypatch):
    snapshot = Store(str(tmp_path / "commerce.db")).snapshot(LIVE)
    monkeypatch.setenv("LIVEOPS_LLM_BASE_URL", "https://model.example/v1")
    monkeypatch.setenv("LIVEOPS_LLM_MODEL", "test-model")
    monkeypatch.setattr("liveops.agent.time.sleep", lambda _: None)
    calls = []

    def fake_post(url, **_kwargs):
        calls.append(url)
        raise httpx.ReadTimeout("timeout", request=httpx.Request("POST", url))

    monkeypatch.setattr("liveops.agent.httpx.post", fake_post)
    outcome = _llm_action("介绍当前商品", snapshot)
    assert outcome["action"] is None
    assert outcome["attempts"] == 2
    assert outcome["error"]["code"] == "MODEL_NETWORK"
    assert outcome["error"]["retryable"] is True
    assert len(calls) == 2


def test_different_commands_can_parse_concurrently(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    barrier = Barrier(2, timeout=3)

    def parse_together(_text, _snapshot):
        barrier.wait()
        return {"action": None, "attempts": 0, "error": None}

    monkeypatch.setattr("liveops.agent._llm_action", parse_together)
    with TestClient(app):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(app.state.agent.submit, LIVE, "介绍当前商品", str(uuid.uuid4())) for _ in range(2)]
            assert [future.result()["status"] for future in futures] == ["ok", "ok"]


def test_same_command_in_parallel_only_writes_once(tmp_path):
    app = make_app(tmp_path)
    command_id = str(uuid.uuid4())
    with TestClient(app):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(app.state.agent.submit, LIVE, "切到 SK-103", command_id) for _ in range(2)]
            results = [future.result() for future in futures]
        assert results[0] == results[1]
        with app.state.store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM action_ledger WHERE command_id=?", (command_id,)).fetchone()[0] == 1


def test_interpretation_context_is_bounded_and_prioritizes_target(tmp_path):
    snapshot = Store(str(tmp_path / "commerce.db")).snapshot(LIVE)
    snapshot["products"] += [
        {"sku": f"EX-{i}", "title": f"测试商品{i}", "price_cents": 10000, "stock": 10, "version": 0}
        for i in range(30)
    ]
    snapshot["preferences"]["style"] = "简短" * 100
    context = interpretation_context("查看 EX-29 库存", snapshot)
    assert context["source"] == "live_business_db"
    assert len(context["products"]) == 8
    assert context["products"][0]["sku"] == "EX-29"
    assert len(context["preference"]) == 120


def test_read_only_result_refreshes_business_truth_after_interpretation(tmp_path, monkeypatch):
    app = make_app(tmp_path)

    def change_stock_during_parse(_text, _snapshot):
        app.state.store.simulate_stock_change(LIVE, "SK-101", 7)
        return {"action": None, "attempts": 0, "error": None}

    monkeypatch.setattr("liveops.agent._llm_action", change_stock_during_parse)
    with TestClient(app) as client:
        result = command(client, "查看当前库存")
        assert result["status"] == "ok"
        assert "剩余 7 件" in result["message"]


def test_approval_expires_without_writing(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        command_id = str(uuid.uuid4())
        assert command(client, "把面霜价格改成 119 元", command_id)["status"] == "pending_approval"
        monkeypatch.setenv("LIVEOPS_APPROVAL_TTL_SECONDS", "0")
        expired = client.post(f"/api/commands/{command_id}/approval", json={"approved": True}).json()
        assert expired["status"] == "expired"
        assert expired["result"]["tool_error"]["code"] == "APPROVAL_EXPIRED"
        assert app.state.store.product("SK-102")["price_cents"] == 12900
        assert app.state.store.command(command_id)["status"] == "expired"


def test_previous_product_survives_restart(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        assert command(client, "切到 SK-102")["status"] == "ok"
        assert app.state.store.live(LIVE)["previous_sku"] == "SK-101"
    restarted = make_app(tmp_path)
    with TestClient(restarted) as client:
        result = command(client, "切回上一款")
        assert result["status"] == "ok"
        assert restarted.state.store.live(LIVE)["current_sku"] == "SK-101"
        assert restarted.state.store.live(LIVE)["previous_sku"] == "SK-102"


def test_existing_demo_database_gets_previous_sku_migration(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE live_sessions (live_id TEXT PRIMARY KEY, status TEXT, current_sku TEXT, "
            "goal TEXT, version INTEGER, started_at REAL)"
        )
        db.execute(
            "INSERT INTO live_sessions VALUES (?, ?, ?, ?, ?, ?)",
            (LIVE, "live", "SK-101", "旧直播间", 0, 1.0),
        )
    store = Store(str(path))
    assert store.live(LIVE)["previous_sku"] is None
    assert store.live(LIVE)["goal"] == "旧直播间"
