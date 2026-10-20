"""Local capacity baseline plus explicitly mocked usage-accounting regressions."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import statistics
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

from .benchmark import run_once
from .campaigns import CampaignHarness
from .metrics import campaign_metrics
from .specialists import SpecialistModel
from .store import Store


LIVE = "LIVE-DEMO-001"


def accounting_case(root: Path, mode: str):
    harness = CampaignHarness(Store(str(root / f"{mode}.db")))
    if mode != "offline":
        harness.model = SpecialistModel("https://model.invalid/v1", "fixture-model-not-real", "empty")

    def post(url, **kwargs):
        if mode == "transient_failure":
            return httpx.Response(500, request=httpx.Request("POST", url))
        if mode == "schema_failure":
            content = "not JSON"
            usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
        else:
            context = json.loads(kwargs["json"]["messages"][1]["content"])
            p = context["products"][0]
            content = json.dumps({"sku": p["sku"], "title": p["title"], "claims": p["features"].split("；"),
                                  "price_cents": p["price_cents"], "stock": p["stock"],
                                  "product_version": p["version"], "source": f"product:{p['sku']}@{p['version']}"})
            usage = {"total_tokens": 123}
        return httpx.Response(200, request=httpx.Request("POST", url), json={
            "choices": [{"message": {"content": content}}], "usage": usage})

    before = harness.store.snapshot(LIVE)
    with patch("liveops.specialists.httpx.post", side_effect=post):
        plan = harness.create(LIVE, "用量核对夹具", top_k=3 if mode == "transient_failure" else 1)
        state = harness.run(LIVE, plan["run_id"])
        if mode == "transient_failure":
            state = harness.run(LIVE, plan["run_id"], retry_failed=True)
    return {"metrics": campaign_metrics(state), "business_state_unchanged": harness.store.snapshot(LIVE) == before}


def main():
    with patch.dict(os.environ, {"LIVEOPS_LLM_MODEL": "", "LIVEOPS_SPECIALIST_MODEL": ""}):
        with patch("liveops.specialists.SpecialistModel.configured", return_value=None):
            with tempfile.TemporaryDirectory(prefix="liveops-operations-eval-") as directory:
                capacity = []
                for round_id, order in enumerate(((1, 4, 8), (4, 8, 1), (8, 1, 4))):
                    for workers in order:
                        capacity.append({"round": round_id + 1, **run_once(100, workers)})
                summary = []
                for workers in (1, 4, 8):
                    rows = [row for row in capacity if row["workers"] == workers]
                    summary.append({"workers": workers, "rounds": len(rows),
                                    "requests": sum(row["requests"] for row in rows),
                                    "success": sum(row["success"] for row in rows),
                                    "median_run_throughput_rps": statistics.median(row["throughput_rps"] for row in rows),
                                    "median_run_p50_ms": statistics.median(row["p50_ms"] for row in rows),
                                    "median_run_p95_ms": statistics.median(row["p95_ms"] for row in rows)})
                report = {
                    "created_at_utc": datetime.now(timezone.utc).isoformat(),
                    "python": platform.python_version(), "platform": platform.platform(),
                    "real_provider_calls": 0,
                    "source_sha256": {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                                      for name in ("specialists.py", "campaigns.py", "metrics.py", "operations_eval.py")},
                    "capacity_scope": "single_process_offline_agent_calls_not_HTTP_not_open_loop_not_production",
                    "latency_scope": "starts_inside_worker_excludes_executor_queue_wait",
                    "capacity": capacity, "capacity_summary": summary,
                    "accounting_scope": "scripted_provider_responses_not_real_token_or_cost_measurements",
                    "accounting": {mode: accounting_case(Path(directory), mode) for mode in
                                   ("schema_failure", "transient_failure", "total_only", "offline")},
                }
    output = Path("outputs/operations_eval_20261017.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"capacity_summary": report["capacity_summary"], "accounting": report["accounting"]}, ensure_ascii=False, indent=2))
    print(f"Saved: {output.resolve()}")


if __name__ == "__main__":
    main()
