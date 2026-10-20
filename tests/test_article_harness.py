import copy
import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from liveops.api import create_app
from liveops.campaigns import CampaignHarness
from liveops.feedback import Feedback
from liveops.model_budget import ModelBudget, model_scope, provider_post
from liveops.specialists import SpecialistModel
from liveops.store import BusinessError, Store
from liveops.workspace import Workspace, initial_state, reduce_state


LIVE = "LIVE-DEMO-001"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.delenv("LIVEOPS_LLM_MODEL", raising=False)
    monkeypatch.delenv("LIVEOPS_SPECIALIST_MODEL", raising=False)


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "business.db"))


def observe(workspace, kind="scene_changed", **payload):
    state = workspace.get(LIVE)
    return workspace.observe(LIVE, f"e-{state['revision']}", kind, payload, state["revision"])


def record(workspace, cid, *, kind="advise", sku="SK-101", message="建议", instruction="介绍SK-101", result=None):
    store = workspace.store
    store.save_command(cid, LIVE, instruction)
    response = {"status": "ok", "action": {"type": kind, "sku": sku}, "message": message, "result": result or {}}
    store.update_command(cid, "ok", response=response)
    return workspace.record(store.command(cid), response)


def test_reducer_is_pure_and_cannot_mutate_commerce_truth():
    state = initial_state()
    original = copy.deepcopy(state)
    result = reduce_state(state, {"kind": "scene_changed", "payload": {"scene": "pre_live"}})
    assert state == original and result["scene"] == "pre_live" and result["control_revision"] == 1
    with pytest.raises(BusinessError):
        reduce_state(state, {"kind": "price_changed", "payload": {"price_cents": 1}})


def test_runtime_idempotency_and_revision_conflict(store):
    w = Workspace(store)
    before = store.snapshot(LIVE)
    first = w.observe(LIVE, "event", "scene_changed", {"scene": "pre_live"}, 0)
    assert w.observe(LIVE, "event", "scene_changed", {"scene": "pre_live"}, 0) == first
    assert w.get(LIVE)["revision"] == 1 and store.snapshot(LIVE) == before
    with pytest.raises(BusinessError) as exc:
        w.observe(LIVE, "event", "scene_changed", {"scene": "post_live"}, 1)
    assert exc.value.code == "EVENT_ID_CONFLICT"
    with pytest.raises(BusinessError) as exc:
        w.observe(LIVE, "other", "scene_changed", {"scene": "post_live"}, 0)
    assert exc.value.code == "RUNTIME_VERSION_CONFLICT"


def test_scene_focus_survives_long_history_without_cross_scene_bleed(store):
    w = Workspace(store)
    record(w, "plan", kind="plan", result={"plan": ["SK-102", "SK-101"]})
    for i in range(12):
        record(w, f"turn-{i}", sku=None, instruction="谢谢", message="不客气")
    pack = w.context(LIVE)
    assert len(pack["conversation"]["turns"]) == 4
    assert pack["conversation"]["listed_skus"] == ["SK-102", "SK-101"]
    observe(w, scene="pre_live")
    assert w.context(LIVE)["conversation"]["listed_skus"] == []
    record(w, "pre", sku="SK-104", instruction="介绍SK-104")
    observe(w, scene="on_live")
    assert w.context(LIVE)["conversation"]["focus_sku"] == "SK-101"
    assert w.context(LIVE, "pre_live")["conversation"]["focus_sku"] == "SK-104"


def test_context_has_hard_char_bound_and_lossless_artifact_lookup(store):
    w = Workspace(store)
    for i in range(8):
        artifact = record(w, f"long-{i}", message="完整材料" * 1500, instruction="介绍SK-101" + "背景" * 150)
    pack = w.context(LIVE, char_budget=1600)
    assert len(Store._json(pack)) <= 1600
    assert pack["compression"]["injected_chars"] == len(Store._json(pack))
    assert pack["compression"]["recent_turn_count"] == len(pack["conversation"]["turns"])
    assert w.artifact(LIVE, artifact)["message"] == "完整材料" * 1500
    with pytest.raises(BusinessError):
        w.artifact("OTHER", artifact)


def test_audience_observation_is_expiring_and_not_an_approval_version(store, monkeypatch):
    w = Workspace(store)
    state = observe(w, "audience_observed", viewers=123)
    assert state["control_revision"] == 0
    seen = state["audience"]["observed_at"]
    monkeypatch.setattr("liveops.workspace.time.time", lambda: seen + 61)
    audience = w.context(LIVE)["audience"]
    assert audience["stale"] is True and "viewers" not in audience


@pytest.mark.parametrize("viewers", [-1, True, 10000001, "100"])
def test_bad_observation_rejected(store, viewers):
    with pytest.raises(BusinessError):
        observe(Workspace(store), "audience_observed", viewers=viewers)


@pytest.mark.parametrize("backend", ["local", "simulated_remote"])
def test_old_approval_cannot_revive_after_scene_round_trip(tmp_path, backend):
    app = create_app(str(tmp_path/"b.db"), str(tmp_path/"c.db"), backend, str(tmp_path/"p.db"))
    with TestClient(app) as client:
        pending = client.post("/api/commands", json={"instruction": "给面霜发10元券20张"}).json()
        assert pending["status"] == "pending_approval"
        w = app.state.workspace
        observe(w, scene="post_live")
        observe(w, scene="on_live")
        result = client.post(f"/api/commands/{pending['command_id']}/approval", json={"approved": True}).json()
        assert result["status"] == "blocked" and result["result"]["code"] == "SCENE_WRITE_DENIED"
        assert app.state.store.snapshot(LIVE)["coupons"] == []


def test_stage_gate_and_observation_not_invalidate_legitimate_approval(tmp_path):
    app = create_app(str(tmp_path/"b.db"), str(tmp_path/"c.db"))
    with TestClient(app) as client:
        observe(app.state.workspace, scene="pre_live")
        result = client.post("/api/commands", json={"instruction": "把面霜价格改成119元"}).json()
        assert result["status"] == "blocked"
        observe(app.state.workspace, scene="on_live")
        pending = client.post("/api/commands", json={"instruction": "把面霜价格改成119元"}).json()
        observe(app.state.workspace, "audience_observed", viewers=1000)
        result = client.post(f"/api/commands/{pending['command_id']}/approval", json={"approved": True}).json()
        assert result["status"] == "ok"


def test_model_budget_atomic_admission_under_concurrency(store):
    budget = ModelBudget(store)
    limits = {"max_calls": 2, "max_reserved_tokens": 10000}
    def reserve(_):
        try:
            return budget.reserve(LIVE, "shared", limits, {"model": "fixture", "max_tokens": 100}, "copywriter")
        except BusinessError:
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(reserve, range(20)))
    assert sum(v is not None for v in values) == 2
    assert budget.summary(LIVE, "shared")["unknown_usage_calls"] == 2


def test_failed_network_holds_budget_and_survives_restart(store, monkeypatch):
    budget = ModelBudget(store)
    monkeypatch.setattr("liveops.model_budget.httpx.post", lambda *a, **k: (_ for _ in ()).throw(httpx.ReadTimeout("fixture")))
    limits = {"max_calls": 1, "max_reserved_tokens": 10000}
    with model_scope(budget, LIVE, "command:failed", limits):
        with pytest.raises(httpx.ReadTimeout):
            provider_post("https://fixture.invalid", phase="intent", json={"max_tokens": 100})
    restarted = ModelBudget(Store(store.path))
    summary = restarted.summary(LIVE, "command:failed")
    assert summary["calls"] == 1 and summary["charged_tokens"] > 0 and summary["unknown_usage_calls"] == 1
    with model_scope(restarted, LIVE, "command:failed", limits):
        with pytest.raises(BusinessError) as exc:
            provider_post("https://fixture.invalid", phase="intent", json={})
    assert exc.value.code == "MODEL_BUDGET_EXHAUSTED"


@pytest.mark.parametrize("usage,expected", [
    ({"total_tokens": 12, "prompt_tokens": 10, "completion_tokens": 2}, 12),
    ({"prompt_tokens": 10, "completion_tokens": 2}, 12),
    ({"total_tokens": True}, None),
    ({"total_tokens": 12, "prompt_tokens": 11, "completion_tokens": 9}, None),
    ({}, None),
])
def test_usage_reconciliation_is_conservative(store, usage, expected):
    budget = ModelBudget(store)
    call = budget.reserve(LIVE, "x", {"max_calls": 2, "max_reserved_tokens": 10000}, {}, "intent")
    budget.finish(call, httpx.Response(200, json={"usage": usage}), elapsed_ms=1)
    summary = budget.summary(LIVE, "x")
    assert summary["reported_tokens"] == (expected or 0)
    assert summary["unknown_usage_calls"] == (expected is None)
    if expected is not None:
        assert summary["charged_tokens"] == expected


def test_budget_scope_limits_cannot_be_reset_and_context_does_not_leak(store):
    budget = ModelBudget(store)
    limits = {"max_calls": 1, "max_reserved_tokens": 10000}
    budget.reserve(LIVE, "fixed", limits, {}, "intent")
    with pytest.raises(BusinessError) as exc:
        budget.reserve(LIVE, "fixed", {**limits, "max_calls": 2}, {}, "intent")
    assert exc.value.code == "MODEL_BUDGET_CONFLICT"
    from liveops.model_budget import _scope
    assert _scope.get() is None
    with model_scope(budget, LIVE, "outer"):
        with model_scope(budget, LIVE, "inner"):
            assert _scope.get()[2] == "inner"
        assert _scope.get()[2] == "outer"
    assert _scope.get() is None


def test_invalid_schema_responses_are_in_durable_ledger(store, monkeypatch):
    h = CampaignHarness(store)
    h.model = SpecialistModel("https://fixture.invalid", "fixture", "empty")
    monkeypatch.setattr("liveops.specialists.httpx.post", lambda *a, **k: httpx.Response(200, request=httpx.Request("POST", "https://fixture.invalid"), json={
        "usage": {"total_tokens": 100}, "choices": [{"message": {"content": "not-json"}}]}))
    run = h.create(LIVE, "prepare", top_k=1)
    assert h.run(LIVE, run["run_id"])["status"] == "blocked"
    assert h.model_budget.summary(LIVE, f"campaign:{run['run_id']}")["reported_tokens"] == 200


def test_zero_budget_stops_before_provider_and_does_not_auto_retry(store, monkeypatch):
    h = CampaignHarness(store)
    h.model = SpecialistModel("https://fixture.invalid", "fixture", "empty")
    monkeypatch.setattr("liveops.specialists.httpx.post", lambda *a, **k: pytest.fail("must not call provider"))
    run = h.create(LIVE, "prepare", top_k=2, model_limits={"max_calls": 0, "max_reserved_tokens": 0})
    done = h.run(LIVE, run["run_id"])
    assert done["status"] == "blocked"
    assert done["tasks"]["brief_0"]["error"] == {"code": "MODEL_BUDGET_EXHAUSTED", "retryable": False}
    assert h.model_budget.summary(LIVE)["calls"] == 0


def test_incremental_replan_reuses_unaffected_branches_and_archives_plan(store):
    h = CampaignHarness(store)
    run = h.create(LIVE, "prepare", skus=["SK-101", "SK-102", "SK-103"])
    done = h.run(LIVE, run["run_id"])
    before_business = store.snapshot(LIVE)
    revised = h.replan(LIVE, run["run_id"], {"SK-101": "SK-104"}, done["revision"])
    change = revised["plan_revisions"][-1]
    assert set(change["preserved_tasks"]) == {"brief_1", "brief_2", "review_1", "review_2"}
    assert len(change["invalidated_tasks"]) == 4
    assert h.artifact(LIVE, run["run_id"], change["archive_id"]) == done
    resumed = h.run(LIVE, run["run_id"])
    assert resumed["status"] == "succeeded" and resumed["steps"] == 12
    assert resumed["tasks"]["brief_1"]["artifact_id"] == done["tasks"]["brief_1"]["artifact_id"]
    assert len({t["trace_id"] for t in resumed["trace"]}) == len(resumed["trace"])
    assert store.snapshot(LIVE) == before_business


def test_replan_refresh_invalidates_changed_product_not_siblings(store):
    h = CampaignHarness(store)
    run = h.run(LIVE, h.create(LIVE, "prepare", skus=["SK-101", "SK-102"])["run_id"])
    store.simulate_stock_change(LIVE, "SK-101", 4)
    revised = h.replan(LIVE, run["run_id"], {}, run["revision"])
    assert revised["tasks"]["brief_0"]["status"] == "pending"
    assert revised["tasks"]["brief_1"]["status"] == "succeeded"
    done = h.run(LIVE, run["run_id"])
    final = h.artifact(LIVE, run["run_id"], done["output_artifact"])
    assert final["approved_briefs"][0]["stock"] == 4


def test_preference_change_replans_all_affected_copywriters(store):
    h = CampaignHarness(store)
    run = h.run(LIVE, h.create(LIVE, "prepare", top_k=2)["run_id"])
    with store.connection() as db:
        db.execute("INSERT INTO preferences(live_id,key,value,updated_at) VALUES (?,?,?,?)", (LIVE, "style", "concise", 0))
        db.commit()
    revised = h.replan(LIVE, run["run_id"], {}, run["revision"])
    assert revised["plan_revisions"][-1]["preserved_tasks"] == []


@pytest.mark.parametrize("replacement", [{"SK-101": "SK-102"}, {"SK-101": "FAKE"}, {"SK-105": "SK-104"}])
def test_replan_rejects_invalid_bindings(store, replacement):
    h = CampaignHarness(store)
    run = h.create(LIVE, "prepare", skus=["SK-101", "SK-102"])
    with pytest.raises(BusinessError):
        h.replan(LIVE, run["run_id"], replacement, run["revision"])
    assert h.get(LIVE, run["run_id"]) == run


def test_replan_revision_limit_and_total_steps_never_reset(store):
    h = CampaignHarness(store)
    run = h.run(LIVE, h.create(LIVE, "prepare", top_k=1)["run_id"])
    with pytest.raises(BusinessError) as exc:
        h.replan(LIVE, run["run_id"], {}, 999)
    assert exc.value.code == "PLAN_REVISION_CONFLICT"
    for _ in range(2):
        run = h.replan(LIVE, run["run_id"], {}, run["revision"])
        run = h.run(LIVE, run["run_id"])
    assert run["steps"] == 8
    with pytest.raises(BusinessError) as exc:
        h.replan(LIVE, run["run_id"], {}, run["revision"])
    assert exc.value.code == "REPLAN_LIMIT"


def test_replan_does_not_refill_model_call_budget(store, monkeypatch):
    h = CampaignHarness(store)
    h.model = SpecialistModel("https://fixture.invalid", "fixture", "empty")
    def post(*args, **kwargs):
        p = json.loads(kwargs["json"]["messages"][1]["content"])["products"][0]
        draft = {"sku": p["sku"], "title": p["title"], "claims": p["features"].split("；"),
                 "price_cents": p["price_cents"], "stock": p["stock"], "product_version": p["version"],
                 "source": f"product:{p['sku']}@{p['version']}"}
        return httpx.Response(200, request=httpx.Request("POST", "https://fixture.invalid"), json={
            "usage": {"total_tokens": 100}, "choices": [{"message": {"content": json.dumps(draft)}}]})
    monkeypatch.setattr("liveops.specialists.httpx.post", post)
    run = h.run(LIVE, h.create(LIVE, "prepare", skus=["SK-101", "SK-102"],
                              model_limits={"max_calls": 2, "max_reserved_tokens": 100000})["run_id"])
    assert run["status"] == "succeeded"
    h.replan(LIVE, run["run_id"], {"SK-101": "SK-104"}, run["revision"])
    done = h.run(LIVE, run["run_id"])
    assert done["status"] == "blocked" and done["tasks"]["brief_0"]["error"]["code"] == "MODEL_BUDGET_EXHAUSTED"
    assert done["tasks"]["review_1"]["status"] == "succeeded"
    assert h.model_budget.summary(LIVE, f"campaign:{run['run_id']}")["calls"] == 2


def test_stale_completed_materials_are_flagged_in_read_api(tmp_path):
    app = create_app(str(tmp_path/"b.db"), str(tmp_path/"c.db"))
    with TestClient(app) as client:
        h = app.state.campaigns
        run = h.run(LIVE, h.create(LIVE, "prepare", skus=["SK-101"])["run_id"])
        app.state.store.simulate_stock_change(LIVE, "SK-101", 0)
        response = client.get(f"/api/campaigns/{LIVE}/{run['run_id']}").json()
        assert response["freshness"]["requires_rebase"] is True
        assert response["freshness"]["changed_skus"] == ["SK-101"]
        assert h.get(LIVE, run["run_id"]) == run


def test_turn_write_replay_and_restart_dont_duplicate_projection(store):
    w = Workspace(store)
    artifact = record(w, "fixed")
    revision = w.get(LIVE)["revision"]
    w.record(store.command("fixed"), store.command("fixed")["response"])
    restarted = Workspace(Store(store.path))
    assert restarted.get(LIVE)["revision"] == revision
    assert restarted.context(LIVE)["compression"]["historical_turn_count"] == 1
    assert restarted.artifact(LIVE, artifact)["message"] == "建议"


def test_feedback_is_idempotent_not_execution_and_denominator_is_explicit(store):
    w = Workspace(store)
    record(w, "advice")
    before = store.snapshot(LIVE)
    feedback = Feedback(store)
    first = feedback.record(LIVE, "advice", "f1", "accept", 4)
    assert first["business_action_executed"] is False
    assert feedback.record(LIVE, "advice", "f1", "accept", 4)["replayed"] is True
    metrics = feedback.metrics(LIVE)
    assert metrics["feedback_count"] == 1 and metrics["acceptance_rate"] == 1
    assert metrics["mean_rating"] == 4 and store.snapshot(LIVE) == before
    feedback.record(LIVE, "advice", "f2", "modify")
    assert feedback.metrics(LIVE)["feedback_count"] == 1
    assert feedback.metrics(LIVE)["acceptance_rate"] == 0
    with pytest.raises(BusinessError):
        feedback.record(LIVE, "advice", "f1", "reject")
    with pytest.raises(BusinessError):
        feedback.record("OTHER", "advice", "f3", "accept")


def test_feedback_cannot_approve_writes_and_empty_metrics_are_not_zero_quality(store):
    w = Workspace(store)
    record(w, "write", kind="update_price")
    f = Feedback(store)
    assert f.metrics(LIVE)["acceptance_rate"] is None
    assert f.metrics(LIVE)["mean_rating"] is None
    with pytest.raises(BusinessError):
        f.record(LIVE, "write", "f1", "accept")


def test_runtime_and_replan_api_contract(tmp_path):
    app = create_app(str(tmp_path/"b.db"), str(tmp_path/"c.db"))
    with TestClient(app) as client:
        runtime = client.get(f"/api/runtime/{LIVE}").json()
        assert runtime["context"]["scene"] == "on_live"
        assert client.post(f"/api/runtime/{LIVE}/events", json={"event_id": "x", "expected_revision": 0, "kind": "scene_changed"}).status_code == 409
        assert client.post(f"/api/runtime/{LIVE}/events", json={"event_id": "x", "expected_revision": 0, "kind": "audience_observed", "viewers": True}).status_code == 422
        run = client.post("/api/campaigns", json={"instruction": "prepare", "skus": ["SK-101", "SK-102"]}).json()
        revised = client.post(f"/api/campaigns/{LIVE}/{run['run_id']}/replan", json={"expected_revision": run["revision"], "replacements": {"SK-101": "SK-104"}}).json()
        assert revised["selected_skus"] == ["SK-104", "SK-102"]
        assert client.get(f"/api/campaigns/{LIVE}/{run['run_id']}/metrics").json()["provider_ledger"]["calls"] == 0
        assert client.get("/harness.css").status_code == 200
