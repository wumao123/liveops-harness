"""Opt-in real-model conversational check after bounded history externalization."""

import hashlib
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from .api import create_app
from .private_config import load_private_settings
from .real_model_eval import RecordedProvider


def main():
    load_private_settings()
    live = "LIVE-DEMO-001"
    provider = RecordedProvider(budget=8)
    output = Path("outputs/article_context_real_20261019.json")
    report = {"scope": "two_real_questions_separated_by_eight_offline_filler_turns_not_independent_benchmark",
              "provider_calls": provider.rows,
              "source_sha256": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path("liveops").glob("*.py"))}}
    with tempfile.TemporaryDirectory(prefix="liveops-context-") as root:
        app = create_app(str(Path(root)/"business.db"), str(Path(root)/"checkpoint.db"))
        agent, workspace = app.state.agent, app.state.workspace
        try:
            workspace.observe(live, "stage", "scene_changed", {"scene": "pre_live"}, 0)
            before = app.state.store.snapshot(live)
            with patch("liveops.agent.httpx.post", provider.post):
                provider.label = "budget_plan"
                first = agent.submit(live, "预算100元以内，推荐三款开场商品并解释顺序", "plan")
                report["first_response"] = first
                listed = first.get("result", {}).get("plan", [])
                with patch.dict(os.environ, {"LIVEOPS_LLM_MODEL": ""}):
                    for i in range(8):
                        agent.submit(live, "查看状态", f"offline-filler-{i}")
                pack = workspace.context(live)
                report["context_before_followup"] = pack
                provider.label = "ordinal_after_eight_turns"
                followup = agent.submit(live, "给第二款写一段简短口播，带一句互动提问", "followup")
                report["followup_response"] = followup
            report["checks"] = {
                "initial_model_understanding": first.get("interpretation", {}).get("source") == "model",
                "initial_model_generation": first.get("generation", {}).get("source") == "model",
                "budget_scope_respected": len(listed) == 3 and all(app.state.store.product(sku)["price_cents"] <= 10000 for sku in listed),
                "plan_survives_outside_recent_four_turns": pack["conversation"]["listed_skus"] == listed and all(t["action_type"] != "plan" for t in pack["conversation"]["turns"]),
                "followup_resolves_displayed_second_product": len(listed) >= 2 and followup["action"].get("sku") == listed[1],
                "followup_model_generation": followup.get("generation", {}).get("source") == "model",
                "no_cross_scene_history": workspace.context(live, "on_live")["conversation"]["listed_skus"] == [],
                "no_commerce_writes": app.state.store.snapshot(live) == before,
            }
            report["ledger"] = agent.model_budget.summary(live)
        finally:
            agent.close()
            report["all_checks_passed"] = bool(report.get("checks")) and all(report["checks"].values())
            output.parent.mkdir(exist_ok=True)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"checks": report["checks"], "provider_calls": len(provider.rows), "tokens": report["ledger"]["reported_tokens"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
