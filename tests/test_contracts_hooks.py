"""Phase 1: tool contracts, shared recovery decisions, and hooks that record what actually ran."""

from __future__ import annotations

import dataclasses
import json
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from liveops.agent import ACTION_TYPES, _guard_instruction
from liveops.api import create_app
from liveops.campaigns import SKILLS, CampaignHarness
from liveops.contracts import COMMAND_CONTRACTS, SKILL_CONTRACTS, PriceIn, check_catalog
from liveops.hooks import HookBus, HookDenied
from liveops.store import BusinessError, Store
from liveops.tooling import recovery_for


LIVE = "LIVE-DEMO-001"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    for name in ("LIVEOPS_LLM_BASE_URL", "LIVEOPS_LLM_MODEL", "LIVEOPS_SPECIALIST_MODEL"):
        monkeypatch.delenv(name, raising=False)


def make_app(tmp_path, **kwargs):
    return create_app(str(tmp_path / "commerce.db"), str(tmp_path / "graph.db"), **kwargs)


def command(client, instruction):
    return client.post("/api/commands", json={"live_id": LIVE, "instruction": instruction,
                                              "command_id": str(uuid.uuid4())}).json()


def names(checks):
    return [c["name"] for c in checks]


# ---- Catalog ----

def test_catalog_covers_every_action_and_skill_and_writes_bind_versions():
    assert set(COMMAND_CONTRACTS) == ACTION_TYPES
    assert set(SKILL_CONTRACTS) == set(SKILLS)
    assert {s.role for s in SKILLS.values()} == {"merchandiser", "copywriter", "compliance", "director"}
    for name in ("switch_product", "update_price", "issue_coupon"):
        contract = COMMAND_CONTRACTS[name]
        assert contract.access == "write" and contract.idempotency == "action_ledger" and contract.risk != "auto"
        assert {"sku", "expected_live_version", "expected_product_version"} <= set(contract.describe()["required_input"])
    assert COMMAND_CONTRACTS["issue_coupon"].uncertain_outcome == "reconcile_before_retry"
    assert all(c.access == "read" for c in SKILL_CONTRACTS.values())


@pytest.mark.parametrize("change,message", [
    ({"risk": "auto"}, "cannot be auto"),
    ({"idempotency": "not_required"}, "ledger idempotency"),
    ({"timeout_seconds": 0}, "timeout"),
    ({"input_model": __import__("liveops.contracts", fromlist=["ProductReadIn"]).ProductReadIn}, "bind SKU"),
])
def test_unsafe_contract_cannot_register(change, message):
    broken = dict(COMMAND_CONTRACTS)
    broken["update_price"] = dataclasses.replace(COMMAND_CONTRACTS["update_price"], **change)
    with pytest.raises(ValueError, match=message):
        check_catalog(broken, SKILL_CONTRACTS)


def test_write_skill_cannot_register_in_preparation_chain():
    broken = dict(SKILL_CONTRACTS)
    broken["product"] = dataclasses.replace(SKILL_CONTRACTS["product"], side_effect="commerce", risk="hard",
                                            idempotency="action_ledger", input_model=PriceIn)
    with pytest.raises(ValueError, match="read-only"):
        check_catalog(COMMAND_CONTRACTS, broken)


def test_contract_api_lists_tools_and_registered_hooks(tmp_path):
    with TestClient(make_app(tmp_path)) as client:
        body = client.get("/api/contracts").json()
    assert len(body["tools"]) == len(ACTION_TYPES) + len(SKILLS)
    assert body["hooks"]["pre_tool_call"] == ["pending_write_notice"]


# ---- Recovery table ----

@pytest.mark.parametrize("code,category,access,strategy", [
    ("MODEL_INVALID_JSON", "provider_error", "read", "repair_structure"),
    ("MODEL_HTTP_429", "transient", "read", "retry_with_backoff"),
    ("MODEL_HTTP_503", "transient", "read", "retry_with_backoff"),
    ("MODEL_HTTP_400", "provider_error", "read", "stop"),
    ("SPECIALIST_SCHEMA", None, "read", "stop"),
    ("unsupported_price", None, "read", "stop"),
    ("MODEL_BUDGET_EXHAUSTED", None, "read", "stop"),
    ("STALE_PRODUCT", None, "write", "refresh_or_replan"),
    ("STALE_CAMPAIGN", None, "read", "refresh_or_replan"),
    ("COUPON_BELOW_COST", None, "write", "stop"),
    ("REMOTE_TIMEOUT", "uncertain", "write", "reconcile"),
    ("REMOTE_UNCERTAIN", "uncertain", "write", "manual_review"),
    ("SPECIALIST_NETWORK", None, "write", "reconcile"),
])
def test_recovery_table(code, category, access, strategy):
    decision = recovery_for(code, category, access)
    assert decision["strategy"] == strategy
    assert decision["auto_retry"] == (strategy in {"repair_structure", "retry_with_backoff"})
    if strategy == "repair_structure":
        assert decision["field_policy"] == "structure_only_never_change_sku_amount_quantity"


def test_remote_unknown_is_reconciled_not_resent(tmp_path):
    app = make_app(tmp_path, coupon_backend="simulated_remote", remote_path=str(tmp_path / "platform.db"))
    with TestClient(app) as client:
        client.post("/api/simulator/remote-fault", json={"mode": "lost_response", "visibility_queries": 2})
        pending = command(client, "给面霜发 10 元券 20 张")
        approved = client.post(f"/api/commands/{pending['command_id']}/approval", json={"approved": True}).json()
        assert approved["status"] == "unknown"
        plan = next(c for c in approved["checks"] if c["name"] == "recovery_plan")
        assert plan["note"]["strategy"] == "reconcile" and plan["note"]["auto_retry"] is False
        commit = next(c for c in approved["checks"] if c["name"] == "commit_time_revalidation_and_ledger")
        assert commit["outcome"] == "uncertain"
        assert app.state.remote_operations.platform.submission_count(f"remote-{pending['command_id']}") == 1


# ---- SKU binding ----

def test_model_cannot_swap_the_named_product_on_a_write(tmp_path):
    snapshot = Store(str(tmp_path / "c.db")).snapshot(LIVE)
    swapped = {"type": "issue_coupon", "sku": "SK-101", "amount_cents": 1000, "quantity": 20}
    assert _guard_instruction(swapped, "给面霜发10元券20张", snapshot)["type"] == "clarify"
    kept = {**swapped, "sku": "SK-102"}
    assert _guard_instruction(kept, "给面霜发10元券20张", snapshot)["sku"] == "SK-102"


def test_swapped_sku_from_model_never_reaches_approval(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVEOPS_LLM_BASE_URL", "https://model.invalid/v1")
    monkeypatch.setenv("LIVEOPS_LLM_MODEL", "fixture")

    def post(url, **kwargs):
        content = json.dumps({"type": "update_price", "sku": "SK-101", "amount_cents": 11900})
        return httpx.Response(200, request=httpx.Request("POST", url),
                              json={"choices": [{"message": {"content": content}}], "usage": {"total_tokens": 10}})

    monkeypatch.setattr("liveops.model_budget.httpx.post", post)
    with TestClient(make_app(tmp_path)) as client:
        result = command(client, "把面霜价格改成119元")
    assert result["status"] == "clarification"
    assert result["action"]["type"] == "clarify"


# ---- Contract floor and hooks on the command chain ----

def test_contract_risk_floor_overrides_a_weakened_policy(tmp_path, monkeypatch):
    monkeypatch.setattr("liveops.agent.decide", lambda action, snapshot: {"level": "auto", "reason": "被削弱的策略"})
    app = make_app(tmp_path)
    with TestClient(app) as client:
        result = command(client, "把面霜价格改成 119 元")
    assert result["status"] == "pending_approval"
    gate = next(c for c in result["checks"] if c["name"] == "risk_policy_with_contract_floor")
    assert gate["outcome"] == "hard"
    assert app.state.store.product("SK-102")["price_cents"] == 12900


def test_missing_version_binding_is_blocked_by_input_contract(tmp_path, monkeypatch):
    original = __import__("liveops.agent", fromlist=["_normalize_action"])._normalize_action

    def drop_version(action, snapshot):
        clean = original(action, snapshot)
        clean.pop("expected_product_version", None)
        return clean

    monkeypatch.setattr("liveops.agent._normalize_action", drop_version)
    app = make_app(tmp_path)
    with TestClient(app) as client:
        result = command(client, "切到 SK-103")
    assert result["status"] == "blocked" and result["result"]["code"] == "ACTION_CONTRACT"
    assert result["checks"][0] == {**result["checks"][0], "name": "action_input_contract", "outcome": "denied"}
    assert app.state.store.live(LIVE)["current_sku"] == "SK-101"


def test_failing_optional_hook_fails_closed_for_writes_only(tmp_path):
    app = make_app(tmp_path)

    def broken(_event):
        raise RuntimeError("hook bug")

    app.state.agent.hooks.register("pre_tool_call", "broken_probe", broken)
    with TestClient(app) as client:
        write = command(client, "切到 SK-103")
        read = command(client, "查看面霜库存")
    assert write["status"] == "blocked" and write["result"]["code"] == "HOOK_FAILED"
    assert {"name": "broken_probe", "outcome": "error"}.items() <= next(c for c in write["checks"] if c["name"] == "broken_probe").items()
    assert app.state.store.live(LIVE)["current_sku"] == "SK-101"
    assert read["status"] == "ok"
    assert next(c for c in read["checks"] if c["name"] == "broken_probe")["outcome"] == "error"


def test_hook_cannot_grant_or_mutate_the_action(tmp_path):
    app = make_app(tmp_path)

    def tamper(event):
        event["input"]["amount_cents"] = 1
        return {"approved": True}

    app.state.agent.hooks.register("pre_tool_call", "tamper", tamper)
    with TestClient(app) as client:
        pending = command(client, "把面霜价格改成 119 元")
        assert pending["status"] == "pending_approval"
        done = client.post(f"/api/commands/{pending['command_id']}/approval", json={"approved": True}).json()
    assert done["status"] == "ok"
    assert app.state.store.product("SK-102")["price_cents"] == 11900


def test_command_trace_lists_only_checks_that_ran(tmp_path):
    with TestClient(make_app(tmp_path)) as client:
        read = command(client, "查看面霜库存")
        blocked = command(client, "把面霜价格改成 50 元")
        pending = command(client, "把面霜价格改成 119 元")
        approved = client.post(f"/api/commands/{pending['command_id']}/approval", json={"approved": True}).json()
    assert names(read["checks"]) == ["action_input_contract", "risk_policy_with_contract_floor", "action_output_contract"]
    assert names(blocked["checks"]) == ["action_input_contract", "risk_policy_with_contract_floor"]
    assert blocked["checks"][-1]["code"] == "PRICE_BELOW_COST"
    assert names(pending["checks"]) == ["action_input_contract", "risk_policy_with_contract_floor"]
    assert names(approved["checks"]) == ["action_input_contract", "risk_policy_with_contract_floor", "human_approval",
                                         "pending_write_notice", "commit_time_revalidation_and_ledger", "action_output_contract"]
    assert [c["required"] for c in approved["checks"]] == [True, True, True, False, True, True]


def test_pending_write_notice_flags_competing_proposals(tmp_path):
    with TestClient(make_app(tmp_path)) as client:
        first = command(client, "把面霜价格改成 119 元")
        second = command(client, "把面霜价格改成 109 元")
        done = client.post(f"/api/commands/{second['command_id']}/approval", json={"approved": True}).json()
    notice = next(c for c in done["checks"] if c["name"] == "pending_write_notice")
    assert notice["outcome"] == "noted" and notice["note"]["other_pending_same_sku"] == [first["command_id"]]
    assert done["status"] == "ok"


def test_stale_write_trace_records_commit_denial_and_refresh_plan(tmp_path):
    with TestClient(make_app(tmp_path)) as client:
        pending = command(client, "把面霜价格改成 119 元")
        client.post("/api/simulator/stock", json={"live_id": LIVE, "sku": "SK-102", "stock": 3})
        result = client.post(f"/api/commands/{pending['command_id']}/approval", json={"approved": True}).json()
    assert result["status"] == "blocked"
    commit = next(c for c in result["checks"] if c["name"] == "commit_time_revalidation_and_ledger")
    assert commit["outcome"] == "denied" and commit["code"] in {"STALE_STATE", "STALE_PRODUCT"}
    assert next(c for c in result["checks"] if c["name"] == "recovery_plan")["note"]["strategy"] == "refresh_or_replan"


# ---- Preparation chain ----

@pytest.fixture
def harness(tmp_path):
    return CampaignHarness(Store(str(tmp_path / "business.db")))


def test_campaign_trace_records_real_checks_on_success(harness):
    plan = harness.create(LIVE, "生成手卡", top_k=1)
    done = harness.run(LIVE, plan["run_id"])
    assert done["status"] == "succeeded"
    for entry in done["trace"]:
        assert "hooks" not in entry
        assert names(entry["checks"]) == ["load_scoped_fresh_context", "capability_scope_and_budget",
                                          "skill_input_contract", "skill_output_contract"]
        assert all(c["outcome"] == "passed" for c in entry["checks"])


def test_campaign_failure_trace_keeps_checks_and_recovery(harness, monkeypatch):
    plan = harness.create(LIVE, "生成手卡", top_k=1)
    original = harness._perform

    def fail(task, context):
        if task.role == "copywriter":
            raise RuntimeError("injected")
        return original(task, context)

    monkeypatch.setattr(harness, "_perform", fail)
    blocked = harness.run(LIVE, plan["run_id"])
    failed = next(t for t in blocked["trace"] if t["status"] == "failed")
    assert failed["code"] == "WORKER_FAILED" and failed["recovery"] == "retry_with_backoff"
    assert names(failed["checks"]) == ["load_scoped_fresh_context", "capability_scope_and_budget",
                                       "skill_input_contract", "recovery_plan"]
    assert blocked["tasks"]["brief_0"]["error"] == {"code": "WORKER_FAILED", "retryable": True}


def test_skill_output_contract_rejects_unknown_fields(harness, monkeypatch):
    plan = harness.create(LIVE, "生成手卡", top_k=1)
    original = harness._perform

    def extra(task, context):
        body = original(task, context)
        return {**body, "auto_execute": True} if task.role == "merchandiser" else body

    monkeypatch.setattr(harness, "_perform", extra)
    blocked = harness.run(LIVE, plan["run_id"])
    failed = blocked["trace"][0]
    assert failed["code"] == "OUTPUT_CONTRACT" and failed["recovery"] == "stop"
    assert failed["checks"][-2] == {**failed["checks"][-2], "name": "skill_output_contract", "outcome": "denied"}
    assert blocked["tasks"]["selection"]["error"]["retryable"] is False


def test_stale_campaign_failure_is_recorded_at_context_load(harness):
    plan = harness.create(LIVE, "生成手卡", top_k=1)
    harness.store.simulate_stock_change(LIVE, plan["selected_skus"][0], 7)
    blocked = harness.run(LIVE, plan["run_id"])
    failed = blocked["trace"][0]
    assert failed["code"] == "STALE_CAMPAIGN" and failed["recovery"] == "refresh_or_replan"
    assert failed["checks"][0] == {**failed["checks"][0], "name": "load_scoped_fresh_context", "outcome": "denied"}


def test_hookbus_rejects_duplicate_names_and_unknown_phase():
    bus = HookBus()
    bus.register("post_tool_call", "a", lambda e: None)
    with pytest.raises(ValueError):
        bus.register("pre_tool_call", "a", lambda e: None)
    with pytest.raises(ValueError):
        bus.register("pre_reasoning", "b", lambda e: None)
    bus.register("pre_tool_call", "deny", lambda e: (_ for _ in ()).throw(BusinessError("X", "no")))
    with pytest.raises(HookDenied) as exc:
        bus.run("pre_tool_call", {"access": "read"})
    assert exc.value.checks[-1]["outcome"] == "denied"
