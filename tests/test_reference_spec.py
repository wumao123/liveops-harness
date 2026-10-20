"""Phase 2: ordinals bind to one source list, request specs gate templates, and changes say what they invalidate."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from liveops.agent import _guard_instruction, _normalize_action
from liveops.api import create_app
from liveops.context import resolve_ordinal
from liveops.spec import build_request_spec, diff_specs, select_template
from liveops.store import BusinessError, Store


LIVE = "LIVE-DEMO-001"
# Offline ranking (stock >= 50 first, then margin): no budget -> 104,103,101,105,102; <=100元 -> 103,101,105.
FULL = ["SK-104", "SK-103", "SK-101", "SK-105", "SK-102"]
BUDGET = ["SK-103", "SK-101", "SK-105"]
PREPARED = ["SK-104", "SK-103", "SK-101"]


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    for name in ("LIVEOPS_LLM_BASE_URL", "LIVEOPS_LLM_MODEL", "LIVEOPS_SPECIALIST_MODEL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def app(tmp_path):
    return create_app(str(tmp_path / "commerce.db"), str(tmp_path / "graph.db"))


def say(client, instruction, command_id=None):
    return client.post("/api/commands", json={"live_id": LIVE, "instruction": instruction,
                                              "command_id": command_id or str(uuid.uuid4())}).json()


def lists(app):
    return app.state.workspace.get(LIVE)["topics"]["on_live"]["lists"]


# ---- Reference binding ----

def test_ordinal_survives_many_turns_and_records_its_source(app):
    with TestClient(app) as client:
        plan = say(client, "帮我做选品计划", "plan-a")
        assert plan["result"]["plan"] == FULL
        for _ in range(8):
            say(client, "查看状态")
        brief = say(client, "介绍第二款")
    assert brief["status"] == "ok" and brief["action"]["sku"] == "SK-103"
    reference = brief["action"]["reference"]
    assert reference["source_command_id"] == "plan-a" and reference["index"] == 1
    assert reference["list_id"] == lists(app)[0]["list_id"] and reference["plan_ref"] is None


def test_two_valid_lists_that_disagree_require_a_selector(app):
    with TestClient(app) as client:
        say(client, "预算100元以内推荐")
        say(client, "准备三款备播手卡")
        assert [l["status"] for l in lists(app)] == ["alternative", "active"]
        ambiguous = say(client, "介绍第二款")
        assert ambiguous["status"] == "clarification" and "2 份列表" in ambiguous["message"]
        assert say(client, "介绍推荐列表的第二款")["action"]["sku"] == BUDGET[1]
        assert say(client, "介绍备播计划的第二款")["action"]["sku"] == PREPARED[1]
        assert say(client, "介绍新方案的第一款")["action"]["sku"] == PREPARED[0]


def test_lists_that_agree_at_the_position_do_not_force_a_question():
    conversation = {"lists": [
        {"list_id": "a", "deliverable": "ranked_list", "skus": ["SK-104", "SK-103"], "status": "alternative", "source_command_id": "c1"},
        {"list_id": "b", "deliverable": "reviewed_briefs", "skus": ["SK-104", "SK-101"], "status": "active", "source_command_id": "c2"}]}
    assert resolve_ordinal("介绍第一款", conversation)["sku"] == "SK-104"
    assert resolve_ordinal("介绍第二款", conversation)["status"] == "clarify"


def test_budget_change_supersedes_old_list_and_old_ordinals(app):
    with TestClient(app) as client:
        say(client, "帮我做选品计划")
        changed = say(client, "预算100元以内重新推荐")
        assert changed["result"]["spec_change"]["change"] == "scope_or_budget"
        assert changed["result"]["spec_change"]["changed_fields"] == ["max_price_cents"]
        assert "已失效" in changed["message"]
        old, new = lists(app)
        assert old["status"] == "superseded" and old["superseded_by"] == new["list_id"]
        assert say(client, "介绍第二款")["action"]["sku"] == BUDGET[1]
        stale = say(client, "介绍旧方案的第二款")
        assert stale["status"] == "clarification" and "替换" in stale["message"]


def test_approval_on_old_list_cannot_transfer_after_scope_change(app):
    with TestClient(app) as client:
        say(client, "帮我做选品计划")
        pending = say(client, "把第二款改成99元", "price-old-list")
        assert pending["status"] == "pending_approval" and pending["action"]["sku"] == "SK-103"
        assert pending["action"]["reference"]["list_id"] == lists(app)[0]["list_id"]
        say(client, "预算100元以内重新推荐")
        result = client.post("/api/commands/price-old-list/approval", json={"approved": True}).json()
    assert result["status"] == "blocked" and result["result"]["code"] == "REFERENCE_STALE"
    assert result["result"]["tool_error"]["category"] == "state_conflict"
    assert app.state.store.product("SK-103")["price_cents"] == 9900
    with app.state.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM action_ledger").fetchone()[0] == 0


def test_approval_on_still_valid_list_commits(app):
    with TestClient(app) as client:
        say(client, "帮我做选品计划")
        say(client, "把第二款改成99元", "price-ok")
        say(client, "查看状态")
        result = client.post("/api/commands/price-ok/approval", json={"approved": True}).json()
    assert result["status"] == "ok" and app.state.store.product("SK-103")["price_cents"] == 9900


def test_replanned_campaign_ordinals_and_pending_approval_go_stale(app):
    with TestClient(app) as client:
        prepared = say(client, "准备三款备播手卡")
        run_id = prepared["result"]["run_id"]
        assert lists(app)[0]["plan_ref"] == {"run_id": run_id, "revision": 1, "replan_count": 0}
        pending = say(client, "把第二款改成99元", "price-plan")
        assert pending["action"]["reference"]["plan_ref"]["run_id"] == run_id
        app.state.campaigns.replan(LIVE, run_id, {"SK-104": "SK-105"}, 1)
        asked = say(client, "介绍第一款")
        assert asked["status"] == "clarification" and "重规划" in asked["message"]
        result = client.post("/api/commands/price-plan/approval", json={"approved": True}).json()
    assert result["result"]["code"] == "REFERENCE_STALE"
    assert app.state.store.product("SK-103")["price_cents"] == 9900


def test_style_only_change_keeps_selection_as_alternative(app):
    with TestClient(app) as client:
        say(client, "准备三款备播手卡")
        styled = say(client, "准备三款备播手卡，风格活泼")
        assert styled["result"]["spec_change"]["change"] == "style_only"
        assert styled["result"]["spec_change"]["effect"] == "keep_selection_rewrite_briefs"
        assert [l["status"] for l in lists(app)] == ["alternative", "active"]
        assert say(client, "介绍第二款")["action"]["sku"] == PREPARED[1]


def test_bindings_survive_restart(tmp_path):
    first = create_app(str(tmp_path / "b.db"), str(tmp_path / "c.db"))
    with TestClient(first) as client:
        say(client, "帮我做选品计划", "plan-r")
    second = create_app(str(tmp_path / "b.db"), str(tmp_path / "c.db"))
    with TestClient(second) as client:
        brief = say(client, "介绍第三款")
    assert brief["action"]["sku"] == FULL[2] and brief["action"]["reference"]["source_command_id"] == "plan-r"


def test_model_cannot_supply_or_forge_a_reference(tmp_path):
    snapshot = Store(str(tmp_path / "b.db")).snapshot(LIVE)
    forged = {"kind": "ordinal", "index": 0, "list_id": "list-forged", "sku": "SK-102", "source_command_id": "x"}
    guarded = _guard_instruction({"type": "switch_product", "sku": "SK-102", "reference": forged}, "切到面霜", snapshot)
    assert "reference" not in guarded
    mismatched = _normalize_action({"type": "brief", "sku": "SK-101", "reference": forged}, snapshot)
    assert "reference" not in mismatched


def test_ordinal_without_any_list_asks_instead_of_using_catalog_order(app):
    with TestClient(app) as client:
        asked = say(client, "介绍第二款")
    assert asked["status"] == "clarification" and "SKU" in asked["message"]


def test_store_rejects_a_reference_it_cannot_verify(tmp_path):
    store = Store(str(tmp_path / "plain.db"))
    snapshot = store.snapshot(LIVE)
    action = {"type": "switch_product", "sku": "SK-103", "expected_live_version": snapshot["live"]["version"],
              "expected_product_version": 1, "reference": {"kind": "ordinal", "index": 1, "list_id": "list-x",
                                                           "sku": "SK-103", "source_command_id": "c"}}
    with pytest.raises(BusinessError) as exc:
        store.apply_action(LIVE, "c-1", action, "key-1")
    assert exc.value.code == "REFERENCE_STALE"


# ---- Request spec ----

def test_request_spec_is_validated_and_selects_a_fixed_template():
    spec = build_request_spec({"type": "plan", "top_k": 3, "max_price_cents": 10000}, "安排选品，开播后再改价和发券")
    assert spec.deliverable == "ranked_list" and spec.permission == "read_only"
    assert spec.excluded_operations == ["update_price", "issue_coupon"]
    assert select_template(spec) == "rank_by_stock_and_margin_v1"
    assert select_template(build_request_spec({"type": "prepare", "top_k": 3}, "")) == "bounded_campaign_dag_v1"
    for bad in ({"type": "plan", "top_k": 6}, {"type": "plan", "top_k": 3, "max_price_cents": 0},
                {"type": "prepare", "top_k": 2, "skus": []}, {"type": "advise"}):
        with pytest.raises(BusinessError):
            build_request_spec(bad)


def test_spec_diff_classifies_changes_and_never_carries_write_slots():
    base = build_request_spec({"type": "prepare", "top_k": 3}, "").model_dump()
    assert diff_specs(None, base)["change"] == "new"
    assert diff_specs(base, base)["change"] == "none"
    assert diff_specs(base, build_request_spec({"type": "prepare", "top_k": 2}, "").model_dump())["changed_fields"] == ["top_k"]
    assert diff_specs(base, build_request_spec({"type": "plan", "top_k": 3}, "").model_dump())["change"] == "different_deliverable"
    assert "never" in diff_specs(base, base)["rule"]


def test_plan_reply_states_excluded_writes_and_runs_none(app):
    before = app.state.store.snapshot(LIVE)
    with TestClient(app) as client:
        reply = say(client, "安排选品计划，开播后再切品")
    assert reply["result"]["request_spec"]["excluded_operations"] == ["switch_product"]
    assert reply["result"]["template"] == "rank_by_stock_and_margin_v1"
    assert "单独发起并审批" in reply["message"]
    assert app.state.store.snapshot(LIVE) == before


def test_lists_stay_inside_the_context_budget(app):
    with TestClient(app) as client:
        for budget in (200, 150, 120, 100, 90):
            say(client, f"预算{budget}元以内推荐")
    pack = app.state.workspace.context(LIVE, char_budget=1600)
    assert len(Store._json(pack)) <= 1600
    kept = pack["conversation"]["lists"]
    assert kept and kept[-1]["status"] == "active" and len(lists(app)) == 4


# ---- Feedback reasons (observation only) ----

def test_feedback_reasons_are_closed_codes_and_counted(app):
    with TestClient(app) as client:
        first = say(client, "帮我做选品计划")["command_id"]
        second = say(client, "预算100元以内推荐")["command_id"]
        url = f"/api/feedback/{LIVE}"
        ok = client.post(f"{url}/{first}", json={"event_id": "f1", "choice": "reject", "reason": "wrong_order",
                                                  "alternative_sku": "SK-102"}).json()
        assert ok["business_action_executed"] is False and ok["reason"] == "wrong_order"
        client.post(f"{url}/{second}", json={"event_id": "f2", "choice": "modify"})
        assert client.post(f"{url}/{second}", json={"event_id": "f3", "choice": "accept", "reason": "wrong_order"}).status_code == 409
        assert client.post(f"{url}/{second}", json={"event_id": "f4", "choice": "accept", "alternative_sku": "SK-101"}).status_code == 409
        assert client.post(f"{url}/{second}", json={"event_id": "f5", "choice": "reject", "alternative_sku": "SK-999"}).status_code == 409
        assert client.post(f"{url}/{first}", json={"event_id": "f1", "choice": "reject", "reason": "other"}).status_code == 409
        metrics = client.get(f"/api/metrics/{LIVE}").json()
    assert metrics["reason_counts"] == {"modify": {}, "reject": {"wrong_order": 1}}
    assert metrics["unreasoned_count"] == 1 and metrics["alternatives"] == ["SK-102"]
    assert app.state.store.product("SK-102")["price_cents"] == 12900


def test_feedback_table_migrates_without_losing_rows(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    Store(str(path))
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE feedback_events (event_id TEXT PRIMARY KEY, live_id TEXT NOT NULL, command_id TEXT NOT NULL, "
                   "choice TEXT NOT NULL, rating INTEGER, created_at REAL NOT NULL)")
        db.execute("INSERT INTO feedback_events VALUES ('old','LIVE-DEMO-001','c','accept',5,1.0)")
    from liveops.feedback import Feedback
    Feedback(Store(str(path)))
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT choice,reason,alternative_sku FROM feedback_events").fetchall() == [("accept", None, None)]


# ---- Scenario runner (phase 3/4); found the deferred-write prepare bug below ----

def test_prepare_with_deferred_coupon_is_preparation_not_a_coupon(app):
    with TestClient(app) as client:
        reply = say(client, "准备三款备播手卡，开播后再发券")
        # An immediate coupon command still parses as a coupon and needs its slots.
        immediate = say(client, "给三款都发券")
    assert reply["status"] == "ok" and reply["action"]["type"] == "prepare"
    assert reply["result"]["request_spec"]["excluded_operations"] == ["issue_coupon"]
    assert immediate["status"] == "clarification"
    with app.state.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM action_ledger").fetchone()[0] == 0


def test_scenario_runner_grades_and_ablation_stays_in_process():
    import json as _json
    from pathlib import Path
    from liveops.scenario_eval import ABLATIONS, grade, run_case
    cases = {c["id"]: c for c in _json.loads(Path("data/scenario_eval_20261019.json").read_text(encoding="utf-8"))["cases"]}
    assert grade(cases["s02"], run_case(cases["s02"]))["verdict"] == "correct_complete"
    assert grade(cases["ch04"], run_case(cases["ch04"]))["first_block"] == "commit_time_revalidation_and_ledger:REFERENCE_STALE"
    ablated = grade(cases["ch04"], run_case(cases["ch04"], "no_runtime_revalidation"))
    assert ablated["verdict"] == "violation"
    # The patch is gone after the case: the real store check is back.
    from liveops.store import Store
    assert Store._validate_runtime_write.__name__ == "_validate_runtime_write"
    assert set(ABLATIONS) >= {"full", "no_instruction_guard", "no_risk_policy", "no_runtime_revalidation"}
    groups = {}
    for c in cases.values():
        groups.setdefault(c["group"], set()).add(c["split"])
    assert all(len(s) == 1 for s in groups.values())


def test_unique_code_binding_overrides_a_model_confirmation_question_for_reads_only(tmp_path):
    # Found in the first real DeepSeek scenario run (dev case ch03): the model asked to confirm a unique binding.
    snapshot = Store(str(tmp_path / "b.db")).snapshot(LIVE)
    snapshot["_conversation"] = {"lists": [
        {"list_id": "old", "deliverable": "ranked_list", "skus": FULL, "status": "superseded", "source_command_id": "c1"},
        {"list_id": "new", "deliverable": "ranked_list", "skus": BUDGET, "status": "active", "source_command_id": "c2"}]}
    model_clarify = {"type": "clarify", "question": "您说的第二款是 SK-101 吗？"}
    read = _guard_instruction(model_clarify, "介绍第二款", snapshot)
    assert read["type"] == "brief" and read["sku"] == BUDGET[1] and read["reference"]["list_id"] == "new"
    # A write stays a question when the model asks; ambiguity still asks.
    assert _guard_instruction(model_clarify, "把第二款改成99元", snapshot)["type"] == "clarify"
    snapshot["_conversation"]["lists"][0]["status"] = "alternative"
    assert _guard_instruction(model_clarify, "介绍第二款", snapshot)["type"] == "clarify"


def test_long_dialogue_grading_and_scripts_are_well_formed(tmp_path):
    from liveops import long_dialogue_eval as L
    snapshot = Store(str(tmp_path / "b.db")).snapshot(LIVE)
    group = L.GROUPS[0]
    truth = {"list": BUDGET}
    assert len(L.script(group)) == 60 and all(len(L.script(g)) == 60 for g in L.GROUPS)
    assert L.grade("list", {"type": "brief", "sku": BUDGET[1]}, group, truth, snapshot) == "kept"
    assert L.grade("list", {"type": "brief", "sku": "SK-104"}, group, truth, snapshot) == "lost"
    assert L.grade("focus", {"type": "clarify", "question": "哪款？"}, group, truth, snapshot) == "asked"
    assert L.grade("budget", {"type": "plan", "top_k": 3, "max_price_cents": 10000}, group, truth, snapshot) == "kept"
    assert L.grade("budget", {"type": "plan", "top_k": 3}, group, truth, snapshot) == "lost"
    # Negation: the negated SKU must not appear in what the action would actually rank.
    assert L.grade("negative", {"type": "plan", "top_k": 3}, group, truth, snapshot) == "lost"
    assert L.grade("negative", {"type": "plan", "top_k": 3, "skus": ["SK-101", "SK-105", "SK-102"]}, group, truth, snapshot) == "kept"
    assert L.grade("negative", None, group, truth, snapshot) == "error"
    # Each negated product is in the no-budget top three, so forgetting it is observable.
    assert all(g["negated"] in FULL[:3] for g in L.GROUPS)


# ---- Scene exclusion constraint (found by the 60-turn real-model experiment) ----

def test_stated_exclusion_survives_history_and_applies_to_plans(app):
    with TestClient(app) as client:
        say(client, "今天防晒乳先不上，别让它进开场")
        for _ in range(12):
            say(client, "查看状态")
        assert app.state.workspace.context(LIVE)["conversation"]["excluded_skus"] == ["SK-103"]
        plan = say(client, "推荐三款开场商品")
        prepared = say(client, "准备三款备播手卡")
        restored = say(client, "防晒乳可以上了")
        again = say(client, "推荐三款开场商品")
    assert "SK-103" not in plan["result"]["plan"] and len(plan["result"]["plan"]) == 3
    assert plan["result"]["request_spec"]["scope"]["exclude_skus"] == ["SK-103"] and "已按本场约束排除" in plan["message"]
    assert "SK-103" not in prepared["result"]["selected_skus"]
    assert restored["status"] == "ok" and "SK-103" in again["result"]["plan"]


def test_exclusion_is_not_triggered_by_negated_writes_and_naming_overrides_it(app):
    with TestClient(app) as client:
        say(client, "不要给防晒乳改价到99元")
        assert app.state.workspace.context(LIVE)["conversation"]["excluded_skus"] == []
        say(client, "防晒乳先不上")
        explicit = say(client, "推荐三款，包括防晒乳")
    assert "SK-103" in explicit["result"]["plan"]


def test_exclusion_change_invalidates_the_previous_list(app):
    with TestClient(app) as client:
        say(client, "帮我做选品计划")
        say(client, "防晒乳先不上")
        changed = say(client, "帮我做选品计划")
    assert changed["result"]["spec_change"]["change"] == "scope_or_budget"
    assert changed["result"]["spec_change"]["changed_fields"] == ["exclude_skus"]
    assert [l["status"] for l in lists(app)] == ["superseded", "active"]


def test_model_scene_constraint_is_checked_against_named_products_and_write_talk(tmp_path, monkeypatch):
    from liveops.agent import _scene_constraint
    snapshot = Store(str(tmp_path / "b.db")).snapshot(LIVE)
    assert _scene_constraint({"exclude_skus": ["SK-104"]}, "精华这次就不排了", snapshot) == {"exclude_skus": ["SK-104"]}
    # A model may not exclude a product the user never named, and price/coupon talk is never an exclusion.
    assert _scene_constraint({"exclude_skus": ["SK-102"]}, "精华这次就不排了", snapshot) is None
    assert _scene_constraint({"exclude_skus": ["SK-104"]}, "精华先别降价", snapshot) is None
    assert _scene_constraint({"restore_skus": ["SK-104"]}, "精华又能讲了", snapshot) == {"restore_skus": ["SK-104"]}
    assert _scene_constraint("SK-104", "精华这次就不排了", snapshot) is None

    app = create_app(str(tmp_path / "a.db"), str(tmp_path / "c.db"))
    actions = iter([{"type": "advise", "sku": "SK-104", "scene_constraint": {"exclude_skus": ["SK-104"]}},
                    {"type": "plan", "top_k": 3}])
    monkeypatch.setattr("liveops.agent._llm_action", lambda *_: {"action": next(actions), "attempts": 1, "error": None})
    with TestClient(app) as client:
        first = say(client, "精华这次就不排了")
        plan = say(client, "推荐三款开场商品")
    assert first["interpretation"]["scene_constraint"]["exclude_skus"] == ["SK-104"]
    assert "SK-104" not in plan["result"]["plan"] and len(plan["result"]["plan"]) == 3


@pytest.mark.parametrize("instruction,kind,sku", [
    ("把卸妆巾挂上来", "switch_product", "SK-105"),
    ("精华换上来讲", "switch_product", "SK-104"),
    ("面霜来一波券，面额12元，发25张", "issue_coupon", "SK-102"),
])
def test_write_vocabulary_is_shared_by_parser_and_guard(tmp_path, instruction, kind, sku):
    # Found by the blind set: the guard rejected phrasings the model had parsed correctly.
    from liveops.agent import _rule_action
    snapshot = Store(str(tmp_path / "b.db")).snapshot(LIVE)
    model = {"type": kind, "sku": sku, **({"amount_cents": 1200, "quantity": 25} if kind == "issue_coupon" else {})}
    assert _guard_instruction(model, instruction, snapshot)["type"] == kind
    assert _rule_action(instruction, snapshot)["type"] == kind


def test_return_to_previous_switches_but_topic_talk_does_not(tmp_path):
    from liveops.agent import _rule_action
    snapshot = Store(str(tmp_path / "b.db")).snapshot(LIVE)
    snapshot["live"]["previous_sku"] = "SK-105"
    assert _rule_action("回到上一款", snapshot) == {"type": "switch_product", "sku": "SK-105"}
    assert _guard_instruction({"type": "switch_product", "sku": "SK-102"}, "回到面霜的话题", snapshot)["type"] == "clarify"
    assert _guard_instruction({"type": "update_price", "sku": "SK-102", "amount_cents": 10900},
                              "面霜降到109元，顺便来张5元券20张", snapshot)["type"] == "clarify"


def test_rule_fast_path_skips_the_model_only_for_unambiguous_parses(tmp_path, monkeypatch):
    from liveops.agent import _rule_fast_path
    snapshot = Store(str(tmp_path / "b.db")).snapshot(LIVE)
    assert _rule_fast_path("把面霜价格改成119元", snapshot)["type"] == "update_price"
    assert _rule_fast_path("给面霜发10元券20张", snapshot)["type"] == "issue_coupon"
    assert _rule_fast_path("面霜现在还剩多少货", snapshot)["type"] == "status"
    # Hedged, pronoun, comparison and open-ended turns go to the model.
    for text in ("如果明天给面霜发10元券20张呢", "它改成119元", "比较面霜和精华", "帮我做选品计划", "给第二款写口播"):
        assert _rule_fast_path(text, snapshot) is None

    app = create_app(str(tmp_path / "a.db"), str(tmp_path / "c.db"))
    monkeypatch.setenv("LIVEOPS_LLM_BASE_URL", "https://model.invalid/v1")
    monkeypatch.setenv("LIVEOPS_LLM_MODEL", "fixture")
    calls = []
    monkeypatch.setattr("liveops.agent._llm_action", lambda *a: calls.append(a) or {"action": None, "attempts": 0, "error": None})
    with TestClient(app) as client:
        fast = say(client, "把面霜价格改成119元")
        slow = say(client, "面霜适合干皮吗")
    assert fast["status"] == "pending_approval" and fast["interpretation"]["source"] == "rule_fast_path"
    assert len(calls) == 1 and slow["interpretation"]["source"] != "rule_fast_path"
    monkeypatch.setenv("LIVEOPS_INTENT_ROUTING", "nonsense")
    with pytest.raises(ValueError):
        from liveops.agent import intent_routing
        intent_routing()


def test_model_cannot_choose_an_item_count_the_user_never_gave(tmp_path):
    snapshot = Store(str(tmp_path / "b.db")).snapshot(LIVE)
    assert "top_k" not in _guard_instruction({"type": "plan", "top_k": 3}, "今天主推哪几款，帮我排个序", snapshot)
    assert _guard_instruction({"type": "plan", "top_k": 3}, "推荐三款开场商品", snapshot)["top_k"] == 3
    assert "top_k" not in _guard_instruction({"type": "plan", "top_k": 2}, "推荐第二款相关的", snapshot)


def test_fast_path_never_turns_a_write_sentence_into_a_read(tmp_path):
    # Found in the hybrid A/B on the (no longer blind) set, case s02: "调到" was missing from the rule price branch.
    from liveops.agent import _rule_action, _rule_fast_path
    snapshot = Store(str(tmp_path / "b.db")).snapshot(LIVE)
    assert _rule_action("精华价格调到149元", snapshot) == {"type": "update_price", "sku": "SK-104", "amount_cents": 14900}
    assert _rule_fast_path("精华价格调到149元", snapshot)["type"] == "update_price"
    assert _rule_fast_path("精华价格调一下", snapshot) is None
