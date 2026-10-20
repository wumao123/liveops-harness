"""Opt-in live-provider replan experiment on the five-product local simulator."""

import argparse
import hashlib
import json
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from .campaigns import CampaignHarness
from .private_config import load_private_settings
from .real_model_eval import RecordedProvider
from .store import Store


LIVE = "LIVE-DEMO-001"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--real-provider", action="store_true")
    parser.add_argument("--output", default="outputs/article_harness_real_20261019.json")
    args = parser.parse_args()
    if not args.real_provider:
        parser.error("Pass --real-provider to explicitly authorize the bounded API experiment")
    load_private_settings()
    provider = RecordedProvider(budget=20)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"scope": "one_paired_development_scenario_five_simulated_products_not_production_benchmark",
              "generated_at_utc": datetime.now(timezone.utc).isoformat(),
              "scenario": "prepare_three_products_then_one_sells_out_replace_it_without_commerce_writes",
              "provider_calls": provider.rows, "variants": [],
              "source_sha256": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path("liveops").glob("*.py"))}}
    def save():
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        with tempfile.TemporaryDirectory(prefix="liveops-article-") as root, patch("liveops.specialists.httpx.post", provider.post):
            for variant in ("full_rebuild", "incremental_replan"):
                h = CampaignHarness(Store(str(Path(root)/f"{variant}.db")))
                if h.model is None:
                    raise RuntimeError("Real specialist model must be configured")
                provider.label = f"{variant}/initial"
                run = h.create(LIVE, "为三款商品准备有来源的讲解手卡", skus=["SK-101", "SK-102", "SK-103"])
                initial = h.run(LIVE, run["run_id"])
                initial_count = len(provider.rows)
                h.store.simulate_stock_change(LIVE, "SK-101", 0)
                business_before = h.store.snapshot(LIVE)
                provider.label = f"{variant}/recovery"
                start = time.monotonic()
                if variant == "full_rebuild":
                    target = h.create(LIVE, "售罄后重新准备三款商品手卡", skus=["SK-104", "SK-102", "SK-103"])
                else:
                    target = h.replan(LIVE, run["run_id"], {"SK-101": "SK-104"}, initial["revision"])
                final = h.run(LIVE, target["run_id"])
                elapsed = round((time.monotonic()-start)*1000, 2)
                artifact = h.artifact(LIVE, final["run_id"], final["output_artifact"]) if final.get("output_artifact") else None
                checks = {"initial_succeeded": initial["status"] == "succeeded", "recovered": final["status"] == "succeeded",
                          "three_valid_materials": artifact is not None and len(artifact["approved_briefs"]) == 3,
                          "sold_out_product_removed": artifact is not None and {b["sku"] for b in artifact["approved_briefs"]} == {"SK-104", "SK-102", "SK-103"},
                          "no_commerce_writes": h.store.snapshot(LIVE) == business_before}
                if variant == "incremental_replan":
                    checks["unaffected_materials_reused"] = all(final["tasks"][name].get("artifact_id") == initial["tasks"][name].get("artifact_id")
                                                                for name in ("brief_1", "brief_2", "review_1", "review_2"))
                    checks["budget_scope_not_reset"] = h.model_budget.summary(LIVE, f"campaign:{final['run_id']}")["calls"] == len([r for r in provider.rows if r["label"].startswith(variant)])
                row = {"variant": variant, "checks": checks, "recovery_ms": elapsed,
                       "recovery_provider_calls": len(provider.rows)-initial_count,
                       "recovery_task_executions": final["steps"] - (initial["steps"] if variant == "incremental_replan" else 0),
                       "preserved_tasks": final.get("plan_revisions", [{}])[-1].get("preserved_tasks", []),
                       "ledger": h.model_budget.summary(LIVE), "initial_plan": initial, "final_plan": final, "final_materials": artifact}
                report["variants"].append(row)
                save()
                print(f"{variant}: recovery_calls={row['recovery_provider_calls']}, tasks={row['recovery_task_executions']}, checks={sum(checks.values())}/{len(checks)}", flush=True)
    finally:
        report["provider_total_calls"] = len(provider.rows)
        report["reported_total_tokens"] = sum((row.get("usage") or {}).get("total_tokens", 0) for row in provider.rows)
        report["unknown_usage_calls"] = sum(type((row.get("usage") or {}).get("total_tokens")) is not int for row in provider.rows)
        report["all_checks_passed"] = len(report["variants"]) == 2 and all(all(v["checks"].values()) for v in report["variants"])
        save()
    print(f"Report: {output}", flush=True)


if __name__ == "__main__":
    main()
