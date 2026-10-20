"""Bounded real-provider development evaluation; never run from normal pytest."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import platform
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

from .agent import _llm_action, _normalize_action, _rule_action
from .campaigns import CampaignHarness
from .memory import forget_preference, memory_view
from .policy import decide
from .private_config import load_private_settings
from .store import Store


LIVE = "LIVE-DEMO-001"
ROOT = Path(__file__).resolve().parent.parent


def apply(store, action):
    snapshot = store.snapshot(LIVE)
    action = _normalize_action(action, snapshot)
    return store.apply_action(LIVE, str(uuid.uuid4()), action, str(uuid.uuid4()))


def canonical(action, snapshot):
    result = dict(action)
    if result["type"] == "status" and not result.get("sku"):
        result["sku"] = snapshot["live"]["current_sku"]
    return result


def matches(action, expected, snapshot):
    result = canonical(action, snapshot)
    return all(result.get(key) == value for key, value in expected.items())


class RecordedProvider:
    def __init__(self, budget=60):
        self.real_post = httpx.post
        self.budget = budget
        self.rows = []
        self.label = ""
        self.lock = threading.Lock()

    def post(self, url, **kwargs):
        with self.lock:
            if len(self.rows) >= self.budget:
                raise RuntimeError("experiment_request_budget_exhausted")
            row = {"index": len(self.rows) + 1, "label": self.label, "status": None}
            self.rows.append(row)
        started = time.perf_counter()
        try:
            response = self.real_post(url, **kwargs)
            row["status"] = response.status_code
            if response.status_code == 200:
                payload = response.json()
                row.update({"response_id": payload.get("id"), "model": payload.get("model"),
                            "usage": payload.get("usage"),
                            "finish_reason": payload.get("choices", [{}])[0].get("finish_reason")})
            return response
        except Exception as exc:
            row["error_type"] = type(exc).__name__
            raise
        finally:
            row["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)


def nlu_cases(store, cases, provider, checkpoint):
    apply(store, {"type": "switch_product", "sku": "SK-103"})
    apply(store, {"type": "switch_product", "sku": "SK-101"})
    snapshot = store.snapshot(LIVE)
    rows = []
    for case in cases:
        provider.label = f"intent/{case['id']}"
        baseline = _normalize_action(_rule_action(case["instruction"], snapshot), snapshot)
        started = time.perf_counter()
        result = _llm_action(case["instruction"], snapshot)
        model_action = _normalize_action(result["action"], snapshot) if result["action"] else None
        final = model_action or baseline
        row = {**case, "rule_action": baseline, "model_action": model_action,
               "final_action": final, "attempts": result["attempts"], "error": result["error"],
               "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
               "rule_joint_correct": matches(baseline, case["expected"], snapshot),
               "model_joint_correct": bool(model_action) and matches(model_action, case["expected"], snapshot),
               "final_joint_correct": matches(final, case["expected"], snapshot),
               "model_type_correct": bool(model_action) and model_action["type"] == case["expected"]["type"],
               "candidate_gate": decide(final, snapshot),
               "source": "model" if model_action else "offline_fallback"}
        rows.append(row)
        checkpoint(rows)
        print(f"Intent {case['id']}: model={row['model_joint_correct']} final={row['final_joint_correct']}", flush=True)
    return rows


def model_writers(root, provider):
    rows = []
    for workers in (1, 3):
        provider.label = f"writer/workers_{workers}"
        harness = CampaignHarness(Store(str(root / f"writers-{workers}.db")), workers=workers)
        before = harness.store.snapshot(LIVE)
        plan = harness.create(LIVE, "三商品真实模型手卡", skus=["SK-101", "SK-102", "SK-103"])
        started = time.perf_counter()
        state = harness.run(LIVE, plan["run_id"])
        final = harness.artifact(LIVE, plan["run_id"], state["output_artifact"]) if "output_artifact" in state else None
        rows.append({"workers": workers, "status": state["status"],
                     "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                     "approved_briefs": len(final["approved_briefs"]) if final else 0,
                     "final": final, "trace": state["trace"],
                     "business_state_unchanged": harness.store.snapshot(LIVE) == before})
        print(f"Writers {workers}: {state['status']}", flush=True)
    return rows


def memory_cases(root):
    rows = []
    fixtures = [
        ("statement_persists", "优先讲 SK-101", [], "none"),
        ("recent_behavior_gap", "优先讲 SK-101", ["SK-102", "SK-103", "SK-102"], "conflict"),
        ("opening_not_general_behavior", "开场先讲 SK-101", ["SK-102", "SK-103", "SK-102"], "unassessed"),
        ("negative_preference_not_gap", "不要讲 SK-101", ["SK-102", "SK-103", "SK-102"], "none"),
        ("insufficient_events", "优先讲 SK-101", ["SK-102", "SK-103"], "none"),
        ("preferred_in_recent_window", "优先讲 SK-101", ["SK-102", "SK-103", "SK-101"], "none"),
        ("no_statement_no_inference", "", ["SK-102", "SK-103", "SK-102"], "none"),
        ("forget_invalidates_active_preference", "不要夸大功效", [], "forgotten"),
    ]
    for name, statement, switches, expected in fixtures:
        store = Store(str(root / f"memory-{name}.db"))
        if statement:
            apply(store, {"type": "set_preference", "value": statement})
        for sku in switches:
            apply(store, {"type": "switch_product", "sku": sku})
        reopened = Store(store.path)
        version = reopened.live(LIVE)["version"]
        forgotten = forget_preference(reopened, LIVE) if expected == "forgotten" else None
        result = memory_view(reopened, LIVE)
        actual = "conflict" if result["conflicts"] else "unassessed" if result["unassessed"] else "none"
        passed = actual == expected
        if expected == "forgotten":
            passed = bool(forgotten["removed"] and not result["L1_statement"]["value"]
                          and result["L2_facts"]["live_version"] == version + 1)
        else:
            passed = passed and result["L1_statement"]["value"] == statement
        if expected == "conflict":
            passed = passed and len(result["conflicts"][0]["evidence_event_ids"]) == 3
        if len(switches) >= 2:
            passed = passed and result["L2_facts"]["previous_sku"] == switches[-2]
        rows.append({"id": name, "expected": expected, "passed": passed, "view": result})
    return rows


def summarize(rows):
    result = []
    for group in ("normal", "challenge", "all"):
        subset = rows if group == "all" else [r for r in rows if r["group"] == group]
        result.append({"group": group, "n": len(subset),
                       **{key: sum(r[key] for r in subset) for key in
                          ("rule_joint_correct", "model_joint_correct", "final_joint_correct", "model_type_correct")},
                       "fallbacks": sum(r["source"] == "offline_fallback" for r in subset)})
    return result


def usage_summary(requests):
    complete = [r for r in requests if isinstance(r.get("usage"), dict)]
    return {"requests": len(requests), "responses_with_usage": len(complete),
            "requests_without_usage": len(requests) - len(complete),
            "reported_prompt_tokens": sum(r["usage"].get("prompt_tokens", 0) for r in complete),
            "reported_completion_tokens": sum(r["usage"].get("completion_tokens", 0) for r in complete),
            "reported_total_tokens": sum(r["usage"].get("total_tokens", 0) for r in complete),
            "reported_cache_hit_tokens": sum(r["usage"].get("prompt_cache_hit_tokens", 0) for r in complete),
            "actual_currency_cost": None,
            "note": "Usage returned by provider, not a billing invoice; preflight call excluded."}


def main():
    load_private_settings()
    if os.environ.get("LIVEOPS_LLM_BASE_URL") != "https://api.deepseek.com":
        raise ValueError("This bounded evaluation expects the configured official endpoint")
    dataset_path = ROOT / "data/intent_eval_20261018.json"
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    provider = RecordedProvider(budget=60)
    output = ROOT / "outputs/real_model_eval_20261018.json"
    report = {"scope": "real_DeepSeek_development_set_plus_local_memory_contracts",
              "date": "2026-10-18", "started_at_utc": datetime.now(timezone.utc).isoformat(),
              "python": platform.python_version(), "model_requested": os.environ["LIVEOPS_LLM_MODEL"],
              "thinking": os.environ.get("LIVEOPS_LLM_THINKING"), "request_budget": provider.budget,
              "per_request_max_output_tokens": 500, "configured_network_timeout_seconds": 5,
              "dataset_scope": dataset["scope"], "dataset_sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
              "source_sha256": {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                                for name in ("agent.py", "campaigns.py", "memory.py", "specialists.py", "real_model_eval.py")}}

    def checkpoint(rows):
        report["intent_rows"] = copy.deepcopy(rows)
        report["intent_summary"] = summarize(rows)
        report["provider_requests"] = provider.rows
        report["usage_summary"] = usage_summary(provider.rows)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    with tempfile.TemporaryDirectory(prefix="liveops-real-eval-") as directory:
        root = Path(directory)
        try:
            with patch("httpx.post", side_effect=provider.post):
                rows = nlu_cases(Store(str(root / "intent.db")), dataset["cases"], provider, checkpoint)
                report["writer_runs"] = model_writers(root, provider)
            report["memory_contracts"] = memory_cases(root)
            report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        finally:
            checkpoint(report.get("intent_rows", []))
    print(json.dumps({"intent": report.get("intent_summary"), "usage": report.get("usage_summary"),
                      "memory_passed": sum(r["passed"] for r in report.get("memory_contracts", [])),
                      "output": str(output)}, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
