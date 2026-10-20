"""Offline paired harness experiments, not LLM-quality or production benchmarks."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import platform
import statistics
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from .campaigns import CampaignHarness, PlanSpec, SKILLS, TaskSpec
from .store import BusinessError, Store


LIVE = "LIVE-DEMO-001"
SKUS = [f"SK-{i}" for i in range(101, 106)]
FAULTS = ("claim", "price", "stock", "source", "empty", "duplicate", "title", "sku", "version")


def corrupt(body, fault):
    body = copy.deepcopy(body)
    if fault == "claim":
        body["claims"].append("保证治愈皮肤疾病")
    elif fault == "price":
        body["price_cents"] += 1
    elif fault == "stock":
        body["stock"] += 1
    elif fault == "source":
        body["source"] = "invented:source"
    elif fault == "empty":
        body["claims"] = []
    elif fault == "duplicate":
        body["claims"].append(body["claims"][0])
    elif fault == "title":
        body["title"] += "-wrong"
    elif fault == "sku":
        body["sku"] = "SK-WRONG"
    elif fault == "version":
        body["product_version"] += 1
    elif fault is not None:
        raise ValueError(fault)
    return body


class FixtureHarness(CampaignHarness):
    def __init__(self, *args, fault=None, review=True, fail_task=None,
                 permanent=False, latency=0, capture=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.model = None
        self.fault, self.review = fault, review
        self.fail_task, self.permanent = fail_task, permanent
        self.latency, self.capture = latency, capture
        self.failures = 0
        self.context_records = []

    def context(self, state, task):
        scoped = super().context(state, task)
        if self.capture:
            # Counterfactual full-sharing packet uses only data present at this stage.
            artifacts = {
                name: self.artifact(LIVE, state["run_id"], item["artifact_id"])
                for name, item in state["tasks"].items() if "artifact_id" in item
            }
            full = {"state": state, "snapshot": self.store.snapshot(LIVE), "artifacts": artifacts}
            self.context_records.append({
                "task_id": task.task_id, "role": task.role,
                "scoped_chars": len(self._json(scoped)), "full_chars": len(self._json(full)),
                "scoped_has_cost": "cost_cents" in self._json(scoped),
                "full_has_cost": "cost_cents" in self._json(full),
                "scoped_skus": [p["sku"] for p in scoped["products"]],
                "expected_sku": task.sku,
                "dependencies": sorted(scoped["dependencies"]),
                "expected_dependencies": sorted(task.dependencies),
            })
        return scoped

    def _perform(self, task, context):
        if task.task_id == self.fail_task and (self.permanent or self.failures == 0):
            self.failures += 1
            raise RuntimeError("controlled experiment failure")
        if task.role == "copywriter" and self.latency:
            time.sleep(self.latency)
        body = super()._perform(task, context)
        if task.role == "copywriter":
            return corrupt(body, self.fault)
        if task.role == "compliance" and not self.review:
            draft = next(iter(context["dependencies"].values()))
            return {"sku": task.sku, "approved": True, "brief": draft,
                    "reason": "experiment_only_review_disabled"}
        return body


def fresh(root, name, **kwargs):
    return FixtureHarness(Store(str(root / f"{name}.db")), **kwargs)


def finish(harness, plan):
    state = harness.run(LIVE, plan["run_id"])
    assert state["status"] in {"succeeded", "review_required"}
    artifact = harness.artifact(LIVE, state["run_id"], state["output_artifact"])
    return state, artifact


def latency_stats(values):
    ordered = sorted(values)
    return {"n": len(values), "median_ms": round(statistics.median(values), 3),
            "p95_ms": round(ordered[max(0, math.ceil(.95 * len(ordered)) - 1)], 3)}


def parallel_experiment(root, repeats, delay):
    rows = []
    for injected in sorted({0, delay}):
        for count in (1, 3, 5):
            for repeat in range(repeats):
                pair = {"products": count, "repeat": repeat, "injected_writer_latency_ms": injected * 1000}
                outputs = {}
                # Alternate execution order to reduce a fixed warm-up/order advantage.
                for workers in ((1, 3) if repeat % 2 == 0 else (3, 1)):
                    h = fresh(root, f"parallel-{injected}-{count}-{repeat}-{workers}", workers=workers, latency=injected)
                    before = h.store.snapshot(LIVE)
                    plan = h.create(LIVE, "并发受控实验", skus=SKUS[:count])
                    start = time.perf_counter()
                    state, artifact = finish(h, plan)
                    pair[f"workers_{workers}_ms"] = (time.perf_counter() - start) * 1000
                    outputs[workers] = artifact
                    assert state["steps"] == 2 * count + 2
                    assert h.store.snapshot(LIVE) == before
                pair["same_output"] = outputs[1] == outputs[3]
                assert pair["same_output"]
                rows.append(pair)
    summary = []
    for injected in sorted({0, delay}):
        for count in (1, 3, 5):
            subset = [r for r in rows if r["products"] == count and r["injected_writer_latency_ms"] == injected * 1000]
            serial = latency_stats([r["workers_1_ms"] for r in subset])
            parallel = latency_stats([r["workers_3_ms"] for r in subset])
            summary.append({"products": count, "injected_writer_latency_ms": injected * 1000,
                            "serial": serial, "parallel": parallel,
                            "median_speedup": round(serial["median_ms"] / parallel["median_ms"], 3),
                            "same_output_pairs": sum(r["same_output"] for r in subset)})
    return {"summary": summary, "raw_pairs": rows}


def context_experiment(root):
    rows = []
    for count in range(1, 6):
        h = fresh(root, f"context-{count}", capture=True)
        finish(h, h.create(LIVE, "上下文交接实验", skus=SKUS[:count]))
        for record in h.context_records:
            record["products_in_plan"] = count
            rows.append(record)
    scoped = sum(r["scoped_chars"] for r in rows)
    full = sum(r["full_chars"] for r in rows)
    product_roles = [r for r in rows if r["expected_sku"]]
    return {"task_packets": len(rows), "scoped_total_chars": scoped, "full_total_chars": full,
            "chars_reduction_pct": round((1 - scoped / full) * 100, 3),
            "scoped_cost_exposure_packets": sum(r["scoped_has_cost"] for r in rows),
            "full_cost_exposure_packets": sum(r["full_has_cost"] for r in rows),
            "product_role_packets": len(product_roles),
            "product_scope_correct": sum(r["scoped_skus"] == [r["expected_sku"]] for r in product_roles),
            "dependency_scope_correct": sum(r["dependencies"] == r["expected_dependencies"] for r in rows),
            "unit": "JSON Unicode characters, not tokenizer tokens or measured API cost", "raw": rows}


def review_experiment(root):
    rows = []
    for sku in SKUS:
        for fault in (None, *FAULTS):
            row = {"sku": sku, "fault": fault, "bad": fault is not None}
            for enabled in (False, True):
                h = fresh(root, f"review-{sku}-{fault}-{enabled}", fault=fault, review=enabled)
                before = h.store.snapshot(LIVE)
                state, artifact = finish(h, h.create(LIVE, "审核准入实验", skus=[sku]))
                row["review_on_admitted" if enabled else "review_off_admitted"] = bool(artifact["approved_briefs"])
                assert h.store.snapshot(LIVE) == before
                assert state["status"] == ("succeeded" if artifact["approved_briefs"] else "review_required")
            rows.append(row)
    bad = [r for r in rows if r["bad"]]
    good = [r for r in rows if not r["bad"]]
    return {"distinct_fixtures": len(rows), "bad_fixtures": len(bad), "good_fixtures": len(good),
            "fault_types": list(FAULTS),
            "review_off_bad_admitted": sum(r["review_off_admitted"] for r in bad),
            "review_on_bad_admitted": sum(r["review_on_admitted"] for r in bad),
            "review_on_good_admitted": sum(r["review_on_admitted"] for r in good), "raw": rows}


def recovery_experiment(root, repeats):
    rows = []
    for repeat in range(repeats):
        for mode in ("partial_failure", "pause_after_reviews"):
            kwargs = {"fail_task": f"brief_{repeat % 3}"} if mode == "partial_failure" else {}
            h = fresh(root, f"recover-{mode}-{repeat}", **kwargs)
            before = h.store.snapshot(LIVE)
            plan = h.create(LIVE, "恢复实验", skus=SKUS[:3])
            initial = h.run(LIVE, plan["run_id"], **({"max_waves": 3} if not kwargs else {}))
            completed = {name for name, item in initial["tasks"].items() if item["status"] == "succeeded"}
            initial_attempts = {name: item["attempts"] for name, item in initial["tasks"].items()}
            restarted = FixtureHarness(Store(h.store.path))
            resumed = restarted.run(LIVE, plan["run_id"], retry_failed=bool(kwargs))
            full = fresh(root, f"full-restart-{mode}-{repeat}")
            full_state, full_output = finish(full, full.create(LIVE, "恢复实验", skus=SKUS[:3]))
            output = restarted.artifact(LIVE, plan["run_id"], resumed["output_artifact"])
            row = {"repeat": repeat, "mode": mode, "before_status": initial["status"],
                   "after_status": resumed["status"], "same_output": output == full_output,
                   "initial_steps": initial["steps"], "checkpoint_total_attempts": resumed["steps"],
                   "full_restart_total_attempts": initial["steps"] + full_state["steps"],
                   "completed_tasks_reexecuted": sum(resumed["tasks"][t]["attempts"] - initial_attempts[t] for t in completed),
                   "snapshot_unchanged": h.store.snapshot(LIVE) == before}
            assert row["after_status"] == "succeeded" and row["same_output"] and row["snapshot_unchanged"]
            rows.append(row)
    summary = []
    for mode in ("partial_failure", "pause_after_reviews"):
        subset = [r for r in rows if r["mode"] == mode]
        checkpoint = sum(r["checkpoint_total_attempts"] for r in subset)
        restart = sum(r["full_restart_total_attempts"] for r in subset)
        summary.append({"mode": mode, "n": len(subset),
                        "checkpoint_total_attempts": checkpoint, "full_restart_total_attempts": restart,
                        "attempt_reduction_pct": round((1 - checkpoint / restart) * 100, 3),
                        "completed_tasks_reexecuted": sum(r["completed_tasks_reexecuted"] for r in subset),
                        "recovered": sum(r["after_status"] == "succeeded" for r in subset)})
    return {"summary": summary, "raw": rows}


def boundary_experiment(root):
    h = fresh(root, "permissions")
    rows = []
    for role in ("merchandiser", "copywriter", "compliance", "director"):
        task = TaskSpec(task_id="probe", role=role, sku="SK-101")
        for tool in (*SKILLS, "issue_coupon", "update_price"):
            try:
                h.pre_tool_call(task, tool, {"role": role, "task_id": "probe"})
                allowed, code = True, None
            except BusinessError as exc:
                allowed, code = False, exc.code
            expected = tool in SKILLS and SKILLS[tool].role == role
            rows.append({"role": role, "tool": tool, "allowed": allowed, "expected": expected, "code": code})
    forever = fresh(root, "retry-budget", fail_task="brief_0", permanent=True)
    plan = forever.create(LIVE, "持续失败实验", skus=SKUS[:3])
    state = forever.run(LIVE, plan["run_id"])
    for _ in range(20):
        state = forever.run(LIVE, plan["run_id"], retry_failed=True)
    bounded = fresh(root, "step-budget", max_steps=2)
    budget_plan = bounded.create(LIVE, "预算实验", skus=SKUS[:3])
    for _ in range(5):
        budget_state = bounded.run(LIVE, budget_plan["run_id"], retry_failed=True)
    return {"capability_checks": rows,
            "unauthorized_probes": sum(not r["expected"] for r in rows),
            "unauthorized_allowed": sum(r["allowed"] and not r["expected"] for r in rows),
            "authorized_probes": sum(r["expected"] for r in rows),
            "authorized_allowed": sum(r["allowed"] and r["expected"] for r in rows),
            "permanent_failure": {"extra_advance_requests": 20, "failed_task_attempts": state["tasks"]["brief_0"]["attempts"],
                                  "max_attempts": forever.max_attempts, "final_status": state["status"],
                                  "healthy_brief_attempts": [state["tasks"][f"brief_{i}"]["attempts"] for i in (1, 2)]},
            "step_budget": {"advance_requests": 5, "steps": budget_state["steps"],
                            "max_steps": bounded.max_steps, "reason": budget_state["budget_stop"]}}


def plan_experiment(root):
    h = fresh(root, "contracts")
    original = h.create(LIVE, "计划契约实验", skus=SKUS[:1])["plan"]
    rows = []
    for mutation in ("cycle", "missing", "duplicate", "wrong_product", "forbidden_role", "extra_field"):
        bad = copy.deepcopy(original)
        if mutation == "cycle":
            bad["tasks"][0]["dependencies"] = ["summary"]
        elif mutation == "missing":
            bad["tasks"][1]["dependencies"] = ["absent"]
        elif mutation == "duplicate":
            bad["tasks"][1]["task_id"] = "selection"
        elif mutation == "wrong_product":
            bad["tasks"][2]["sku"] = "SK-999"
        elif mutation == "forbidden_role":
            bad["tasks"][1]["role"] = "cashier"
        else:
            bad["tasks"][1]["shell"] = "forbidden"
        try:
            PlanSpec.model_validate(bad)
            rejected = False
        except ValidationError:
            rejected = True
        rows.append({"mutation": mutation, "rejected": rejected})
    return {"invalid_plans": len(rows), "rejected": sum(r["rejected"] for r in rows), "raw": rows}


def run_experiments(repeats=10, delay=.04):
    if not 1 <= repeats <= 100 or not 0 <= delay <= .2:
        raise ValueError("invalid experiment budget")
    # Prevent accidentally using credentials or calling a configured online model.
    with patch("liveops.specialists.SpecialistModel.configured", return_value=None):
        with tempfile.TemporaryDirectory(prefix="liveops-multiagent-eval-") as directory:
            root = Path(directory)
            result = {"mode": "offline_deterministic_fault_injection_not_real_llm_or_taobao",
                      "created_at_utc": datetime.now(timezone.utc).isoformat(),
                      "python": platform.python_version(), "platform": platform.platform(),
                      "repeats": repeats, "llm_calls": 0,
                      "source_sha256": {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                                        for name in ("campaigns.py", "store.py", "multiagent_eval.py")},
                      "parallel": parallel_experiment(root, repeats, delay),
                      "context": context_experiment(root),
                      "review": review_experiment(root),
                      "recovery": recovery_experiment(root, repeats),
                      "boundaries": boundary_experiment(root),
                      "plans": plan_experiment(root)}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=10)
    parser.add_argument("--latency-ms", type=float, default=40)
    parser.add_argument("--output", type=Path, default=Path("outputs/multiagent_eval_20261017.json"))
    args = parser.parse_args()
    result = run_experiments(args.repeats, args.latency_ms / 1000)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    brief = {name: value.get("summary", {k: v for k, v in value.items() if k not in {"raw", "raw_pairs", "capability_checks"}})
             for name, value in result.items() if isinstance(value, dict) and name != "source_sha256"}
    print(json.dumps(brief, ensure_ascii=False, indent=2))
    print(f"Saved: {args.output.resolve()}")


if __name__ == "__main__":
    main()
