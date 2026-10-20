import json

import httpx
import pytest
from fastapi.testclient import TestClient

from liveops.advisor import generate_advice
from liveops.agent import _guard_instruction, _normalize_action, _rule_action
from liveops.api import create_app
from liveops.context import recent_context
from liveops.store import Store


LIVE = "LIVE-DEMO-001"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    for name in ("LIVEOPS_LLM_BASE_URL", "LIVEOPS_LLM_MODEL", "LIVEOPS_SPECIALIST_MODEL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def snapshot(tmp_path):
    return Store(str(tmp_path / "business.db")).snapshot(LIVE)


@pytest.mark.parametrize("instruction", ["如果明天给面霜发10元券20张呢", "不要给面霜发10元券20张", "给面霜发10元券20张合适吗"])
def test_discussion_never_authorizes_coupon(snapshot, instruction):
    action = {"type": "issue_coupon", "sku": "SK-102", "amount_cents": 1000, "quantity": 20}
    assert _guard_instruction(action, instruction, snapshot)["type"] == "advise"


@pytest.mark.parametrize("instruction,action", [
    ("给面霜发10元券", {"type": "issue_coupon", "sku": "SK-102", "amount_cents": 1000, "quantity": 1}),
    ("给面霜发10元券20张", {"type": "issue_coupon", "sku": "SK-102", "amount_cents": 1000, "quantity": 10}),
    ("把面霜改成119元", {"type": "update_price", "sku": "SK-102", "amount_cents": 10900}),
    ("发10元券20张", {"type": "issue_coupon", "sku": "SK-102", "amount_cents": 1000, "quantity": 20}),
    ("介绍面霜", {"type": "switch_product", "sku": "SK-102"}),
    ("把面霜改成119元再发10元券20张", {"type": "update_price", "sku": "SK-102", "amount_cents": 11900}),
])
def test_guessed_slots_and_accidental_execution_are_clarified(snapshot, instruction, action):
    assert _guard_instruction(action, instruction, snapshot)["type"] == "clarify"


def test_pronoun_uses_recent_focus_not_arbitrary_model_sku(snapshot):
    snapshot["_conversation"] = {"focus_sku": "SK-102", "listed_skus": ["SK-103", "SK-104"]}
    assert _rule_action("介绍它", snapshot)["sku"] == "SK-102"
    action = _guard_instruction({"type": "brief", "sku": "SK-101"}, "介绍第二款", snapshot)
    assert action["sku"] == "SK-104"
    assert _rule_action("介绍第五款", snapshot)["type"] == "clarify"


def test_explicit_preference_uses_original_words(snapshot):
    action = _guard_instruction({"type": "set_preference", "value": "凭空生成"}, "记住：口播简短，不夸大功效", snapshot)
    assert action["value"] == "口播简短，不夸大功效"
    assert _guard_instruction({"type": "set_preference", "value": "长口播"}, "查看偏好", snapshot)["type"] == "clarify"


def test_recent_memory_is_bounded_and_pending_is_not_a_commit():
    commands = [{"instruction": "x" * 1000, "status": "pending_approval", "proposal": {"type": "issue_coupon", "sku": "SK-105"},
                 "response": {"message": "待审批"}}]
    commands += [{"instruction": "查商品", "status": "ok", "response": {"action": {"type": "brief", "sku": "SK-102"},
                  "message": "y" * 1000, "result": {}}} for _ in range(10)]
    memory = recent_context(commands)
    assert memory["focus_sku"] == "SK-102"
    assert len(memory["turns"]) == 4
    assert all(len(t["user"]) <= 200 and len(t["assistant"]) <= 400 for t in memory["turns"])


@pytest.mark.parametrize("action", [{"type": "prepare", "top_k": 99}, {"type": "plan", "max_price_cents": -100},
                                    {"type": "prepare", "skus": ["NOT-A-SKU"]}])
def test_preparation_contract_rejects_invalid_scope(snapshot, action):
    assert _normalize_action(action, snapshot)["type"] == "clarify"


def test_budget_filter_and_chat_preparation_route(tmp_path):
    app = create_app(str(tmp_path / "business.db"), str(tmp_path / "checkpoints.db"))
    with TestClient(app) as client:
        before = app.state.store.snapshot(LIVE)
        result = client.post("/api/commands", json={"instruction": "推荐100元以内的开场商品"}).json()
        assert result["action"]["max_price_cents"] == 10000
        assert all(app.state.store.product(sku)["price_cents"] <= 10000 for sku in result["result"]["plan"])
        result = client.post("/api/commands", json={"instruction": "准备三款商品手卡并合规审核"}).json()
        run_id = result["result"]["run_id"]
        runs = client.get(f"/api/campaigns/{LIVE}").json()
        assert runs[0]["run_id"] == run_id and runs[0]["status"] == "pending"
        assert app.state.store.snapshot(LIVE) == before
        done = client.post(f"/api/campaigns/{LIVE}/{run_id}/advance", json={}).json()
        assert done["status"] == "succeeded" and len(done["tasks"]) == 8


def test_model_missing_quantity_is_not_a_pending_approval(tmp_path, monkeypatch):
    app = create_app(str(tmp_path / "business.db"), str(tmp_path / "checkpoints.db"))
    monkeypatch.setattr("liveops.agent._llm_action", lambda *_: {"action": {"type": "issue_coupon", "sku": "SK-102", "amount_cents": 1000, "quantity": 1}, "attempts": 1, "error": None})
    with TestClient(app) as client:
        response = client.post("/api/commands", json={"instruction": "给面霜发10元券"}).json()
        assert response["status"] == "clarification" and "张" in response["message"]
        assert not any(c["status"] == "pending_approval" for c in app.state.store.commands(LIVE))
        assert app.state.store.snapshot(LIVE)["coupons"] == []


def advice_response(body):
    return httpx.Response(200, request=httpx.Request("POST", "https://fixture.invalid/chat/completions"), json={
        "choices": [{"message": {"content": json.dumps(body, ensure_ascii=False)}}], "usage": {"total_tokens": 123}})


@pytest.mark.parametrize("change", [
    {"evidence_ids": ["P999"]}, {"recommended_skus": ["SK-999"]},
    {"answer": "售价1元[P1]"}, {"answer": "全网最低[P1]"}, {"answer": "已经发券[P1]"},
    {"answer": "推荐洁面，没有引用"}, {"evidence_ids": [], "recommended_skus": ["SK-101"]},
])
def test_generated_advice_rejects_unknown_references_or_unsafe_claims(snapshot, monkeypatch, change):
    monkeypatch.setenv("LIVEOPS_LLM_BASE_URL", "https://fixture.invalid")
    monkeypatch.setenv("LIVEOPS_LLM_MODEL", "fixture")
    draft = {"answer": "洁面售价79元[P1]", "evidence_ids": ["P1"], "recommended_skus": ["SK-101"], **change}
    monkeypatch.setattr("liveops.advisor.httpx.post", lambda *a, **k: advice_response(draft))
    result = generate_advice("帮我选品", snapshot, {}, "advise")
    assert result["source"] == "offline_fallback" and "answer" not in result
    assert result["reported_total_tokens"] == 123 * result["model_calls"]
    assert result["model_calls"] <= 2


def test_advice_gets_fresh_public_facts_and_never_costs(snapshot, monkeypatch):
    monkeypatch.setenv("LIVEOPS_LLM_BASE_URL", "https://fixture.invalid")
    monkeypatch.setenv("LIVEOPS_LLM_MODEL", "fixture")
    sent = []

    def post(*args, **kwargs):
        sent.append(kwargs["json"])
        return advice_response({"answer": "洁面可作为开场候选，售价79元[P1]。", "evidence_ids": ["P1"], "recommended_skus": ["SK-101"]})

    monkeypatch.setattr("liveops.advisor.httpx.post", post)
    result = generate_advice("开场怎么选", snapshot, {}, "advise")
    assert result["source"] == "model" and result["elapsed_ms"] >= 0
    context = json.loads(sent[0]["messages"][1]["content"])
    assert "cost_cents" not in json.dumps(context)
    assert context["evidence"]["P1"]["stock"] == 180


def test_stock_change_during_generation_discards_answer(tmp_path, monkeypatch):
    app = create_app(str(tmp_path / "business.db"), str(tmp_path / "checkpoints.db"))
    monkeypatch.setattr("liveops.agent._llm_action", lambda *_: {"action": {"type": "brief", "sku": "SK-101"}, "attempts": 1, "error": None})

    def generate(*_):
        app.state.store.simulate_stock_change(LIVE, "SK-101", 0)
        return {"source": "model", "answer": "剩余180件", "recommended_skus": ["SK-101"]}

    monkeypatch.setattr("liveops.advisor.generate_advice", generate)
    with TestClient(app) as client:
        result = client.post("/api/commands", json={"instruction": "介绍当前商品"}).json()
        assert result["status"] == "clarification"
        assert "180" not in result["message"] and "answer" not in result["generation"]
        assert result["generation"]["error"] == "FACTS_CHANGED"


def test_clarification_focus_is_not_an_execution_receipt(snapshot):
    commands = [{"instruction": "给面霜发10元券", "status": "clarification", "response": {
        "action": {"type": "clarify"}, "message": "请补充数量"}}]
    memory = recent_context(commands, snapshot["products"])
    assert memory["focus_sku"] == "SK-102"
    assert memory["turns"][0]["status"] == "clarification"


def test_multi_product_discussion_does_not_guess_pronoun(snapshot):
    snapshot["_conversation"] = recent_context([{"instruction": "比较面霜和精华", "status": "ok", "response": {
        "action": {"type": "advise"}, "message": "比较结果"}}], snapshot["products"])
    action = _guard_instruction({"type": "advise", "sku": "SK-101"}, "它适合谁", snapshot)
    assert action["type"] == "clarify"


def test_deleted_preferences_are_not_reintroduced_as_dialogue_context():
    commands = [{"instruction": "记住错误偏好", "status": "ok", "response": {
        "action": {"type": "set_preference", "value": "错误偏好"}, "message": "已记住错误偏好"}}]
    assert recent_context(commands)["turns"] == []


def test_model_reordering_and_followup_share_the_same_list(tmp_path, monkeypatch):
    app = create_app(str(tmp_path / "business.db"), str(tmp_path / "checkpoints.db"))
    proposed = ["SK-105", "SK-101", "SK-103"]
    actions = iter([{"type": "plan", "top_k": 3, "max_price_cents": 10000}, {"type": "brief", "sku": "SK-103"}])
    monkeypatch.setattr("liveops.agent._llm_action", lambda *_: {"action": next(actions), "attempts": 1, "error": None})
    monkeypatch.setattr("liveops.advisor.generate_advice", lambda *args: {"source": "model", "answer": "建议低价开场", "recommended_skus": proposed} if args[-1] == "plan" else {"source": "offline"})
    with TestClient(app) as client:
        plan = client.post("/api/commands", json={"instruction": "推荐100元内三款商品"}).json()
        assert plan["result"]["plan"] == proposed
        assert "1. 棉柔卸妆巾（SK-105）" in plan["message"]
        brief = client.post("/api/commands", json={"instruction": "介绍第一款"}).json()
        assert brief["action"]["sku"] == "SK-105"


def test_parser_repairs_wrapped_action_once(snapshot, monkeypatch):
    from liveops.agent import _llm_action
    monkeypatch.setenv("LIVEOPS_LLM_BASE_URL", "https://fixture.invalid")
    monkeypatch.setenv("LIVEOPS_LLM_MODEL", "fixture")
    calls = []

    def post(*args, **kwargs):
        calls.append(kwargs)
        content = '{"type":"json_object","action":{"type":"prepare"}}' if len(calls) == 1 else '{"type":"prepare","top_k":3}'
        return httpx.Response(200, request=httpx.Request("POST", "https://fixture.invalid"), json={"choices": [{"message": {"content": content}}]})

    monkeypatch.setattr("liveops.agent.httpx.post", post)
    result = _llm_action("准备三款手卡", snapshot)
    assert result["action"]["type"] == "prepare" and result["attempts"] == 2


def test_model_cannot_drop_candidates_or_expand_budget(snapshot, monkeypatch):
    action = _guard_instruction({"type": "plan", "max_price_cents": 50000}, "推荐100元以内商品", snapshot)
    assert action["max_price_cents"] == 10000
    monkeypatch.setenv("LIVEOPS_LLM_BASE_URL", "https://fixture.invalid")
    monkeypatch.setenv("LIVEOPS_LLM_MODEL", "fixture")
    monkeypatch.setattr("liveops.advisor.httpx.post", lambda *a, **k: advice_response({
        "answer": "仅推荐洁面[P1]", "evidence_ids": ["P1"], "recommended_skus": ["SK-101"]}))
    result = generate_advice("推荐商品", snapshot, {"plan": ["SK-101", "SK-103"]}, "plan")
    assert result["source"] == "offline_fallback" and result["error_code"] == "plan_scope_mismatch"


def test_advice_repairs_citation_once_and_keeps_failed_usage(snapshot, monkeypatch):
    monkeypatch.setenv("LIVEOPS_LLM_BASE_URL", "https://fixture.invalid")
    monkeypatch.setenv("LIVEOPS_LLM_MODEL", "fixture")
    calls = []

    def post(*args, **kwargs):
        calls.append(kwargs)
        return advice_response({"answer": "现有资料不足，无法确认。", "evidence_ids": [],
                                "recommended_skus": ["SK-101"] if len(calls) == 1 else []})

    monkeypatch.setattr("liveops.advisor.httpx.post", post)
    result = generate_advice("它适合敏感肌吗", snapshot, {}, "advise")
    assert result["source"] == "model" and result["model_calls"] == 2
    assert result["reported_total_tokens"] == 246
    assert [a["status"] for a in result["provider_attempts"]] == ["rejected", "accepted"]


def test_budget_digits_are_not_evidence_for_a_different_price(snapshot, monkeypatch):
    monkeypatch.setenv("LIVEOPS_LLM_BASE_URL", "https://fixture.invalid")
    monkeypatch.setenv("LIVEOPS_LLM_MODEL", "fixture")
    monkeypatch.setattr("liveops.advisor.httpx.post", lambda *a, **k: advice_response({
        "answer": "这款只需1元[P1]", "evidence_ids": ["P1"], "recommended_skus": ["SK-101"]}))
    result = generate_advice("预算100元", snapshot, {}, "advise")
    assert result["error_code"] == "unsupported_price" and result["source"] == "offline_fallback"


def test_fault_mode_is_read_back_instead_of_resetting_on_reload(tmp_path):
    app = create_app(str(tmp_path / "business.db"), str(tmp_path / "checkpoints.db"), coupon_backend="simulated_remote", remote_path=str(tmp_path / "platform.db"))
    with TestClient(app) as client:
        assert client.get("/api/simulator/remote-fault").json()["mode"] == "normal"
        client.post("/api/simulator/remote-fault", json={"mode": "lost_response"})
        assert client.get("/api/simulator/remote-fault").json()["mode"] == "lost_response"


def test_brief_does_not_replace_the_full_recommendation_list():
    commands = [{"instruction": "介绍第二款", "status": "ok", "response": {
        "action": {"type": "brief", "sku": "SK-101"}, "message": "洁面手卡", "result": {"recommended_skus": ["SK-101"]}}},
        {"instruction": "推荐三款", "status": "ok", "response": {
            "action": {"type": "plan"}, "message": "三款列表", "result": {"plan": ["SK-105", "SK-101", "SK-103"]}}}]
    assert recent_context(commands)["listed_skus"] == ["SK-105", "SK-101", "SK-103"]
