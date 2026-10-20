"""Small opt-in real-model smoke run, not an independent accuracy benchmark."""

import json
import hashlib
import statistics
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from .api import create_app
from .private_config import load_private_settings
from .real_model_eval import RecordedProvider


LIVE = "LIVE-DEMO-001"
CASES = [
    ("greeting", "你好，你能帮我什么？"),
    ("budget_plan", "预算100元以内，推荐三款开场商品并解释顺序"),
    ("ordinal_brief", "给第二款写一段自然的直播口播，带一个互动提问"),
    ("sensitive_skin", "它适合敏感肌吗？"),
    ("ordinal_after_followup", "再给第三款写一段简短口播"),
    ("missing_quantity", "给面霜发10元券"),
    ("conditional_coupon", "如果明天给它发10元券20张呢？"),
    ("explicit_memory", "记住：口播简短，不夸大功效"),
    ("multi_preparation", "准备三款商品手卡并合规审核"),
    ("complete_coupon", "给面霜发10元券20张"),
]


def main():
    load_private_settings()
    provider = RecordedProvider(budget=32)
    output = Path("outputs/conversation_upgrade_verified_20261019.json")
    output.parent.mkdir(exist_ok=True)
    report = {"scope": "ten_handwritten_development_cases_not_independent_accuracy_or_production_benchmark",
              "generated_at_utc": datetime.now(timezone.utc).isoformat(),
              "source_sha256": {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in (
                  "liveops/agent.py", "liveops/advisor.py", "liveops/context.py", "liveops/api.py", "liveops/campaigns.py")},
              "cases": [], "provider_calls": provider.rows}
    with tempfile.TemporaryDirectory(prefix="liveops-conversation-") as directory:
        app = create_app(str(Path(directory) / "business.db"), str(Path(directory) / "checkpoints.db"))
        with TestClient(app) as client, patch("liveops.agent.httpx.post", provider.post):
            for name, question in CASES:
                provider.label = name
                start = time.monotonic()
                response = client.post("/api/commands", json={"instruction": question, "command_id": name})
                response.raise_for_status()
                row = {"name": name, "question": question, "seconds": round(time.monotonic() - start, 3),
                       "response": response.json()}
                report["cases"].append(row)
                output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps({"name": name, "seconds": row["seconds"], "status": row["response"]["status"],
                                  "action": row["response"]["action"], "generation": row["response"].get("generation", {}).get("source"),
                                  "error_code": row["response"].get("generation", {}).get("error_code"),
                                  "message": row["response"]["message"][:180]}, ensure_ascii=False), flush=True)
            indexed = {row["name"]: row["response"] for row in report["cases"]}
            plan = indexed["budget_plan"].get("result", {}).get("plan", [])
            report["checks"] = {
                "budget_scope": bool(plan) and all(app.state.store.product(s)["price_cents"] <= 10000 for s in plan),
                "ordinal_target": len(plan) >= 2 and indexed["ordinal_brief"]["action"].get("sku") == plan[1],
                "list_survives_intervening_followups": len(plan) >= 3 and indexed["ordinal_after_followup"]["action"].get("sku") == plan[2],
                "missing_quantity_clarified": indexed["missing_quantity"]["status"] == "clarification",
                "conditional_did_not_request_execution": indexed["conditional_coupon"]["action"].get("type") in {"advise", "clarify"},
                "explicit_preference_preserved": app.state.store.preferences(LIVE).get("style") == "口播简短，不夸大功效",
                "complete_coupon_requires_approval": indexed["complete_coupon"]["status"] == "pending_approval",
                "no_coupons_committed_without_approval": app.state.store.snapshot(LIVE)["coupons"] == [],
            }
            run_id = indexed["multi_preparation"].get("result", {}).get("run_id")
            if run_id:
                provider.label = "preparation_workers"
                done = client.post(f"/api/campaigns/{LIVE}/{run_id}/advance", json={}).json()
                report["campaign"] = done
                report["campaign_metrics"] = client.get(f"/api/campaigns/{LIVE}/{run_id}/metrics").json()
                report["checks"]["preparation_completed"] = done["status"] == "succeeded" and len(done["tasks"]) == 8
            else:
                report["checks"]["preparation_completed"] = False
    report["summary"] = {
        "cases": len(report["cases"]), "checks_passed": sum(report["checks"].values()), "checks": len(report["checks"]),
        "median_command_seconds": round(statistics.median(r["seconds"] for r in report["cases"]), 3),
        "provider_requests": len(provider.rows),
        "reported_total_tokens": sum(t for r in provider.rows if type(t := (r.get("usage") or {}).get("total_tokens")) is int),
        "requests_without_reported_tokens": sum(type((r.get("usage") or {}).get("total_tokens")) is not int for r in provider.rows),
    }
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"checks": report["checks"], "summary": report["summary"], "output": str(output)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
