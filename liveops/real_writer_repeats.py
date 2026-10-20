"""Four additional paired real-provider runs within the original 60-call budget."""

from __future__ import annotations

import hashlib
import json
import os
import statistics
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from .campaigns import CampaignHarness
from .private_config import load_private_settings
from .real_model_eval import LIVE, ROOT, RecordedProvider, usage_summary
from .store import Store


def main():
    load_private_settings()
    original = json.loads((ROOT / "outputs/real_model_eval_20261018.json").read_text())
    provider = RecordedProvider(budget=60 - len(original["provider_requests"]))
    output = ROOT / "outputs/real_writer_repeats_20261018.json"
    if output.exists():
        raise SystemExit("Additional runs already exist; refusing accidental duplicate spending")
    report = {"scope": "four_additional_pairs_plus_one_original_pair_not_large_sample_benchmark",
              "started_at_utc": datetime.now(timezone.utc).isoformat(),
              "model_requested": os.environ["LIVEOPS_LLM_MODEL"],
              "additional_request_budget": provider.budget,
              "source_sha256": {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                                for name in ("agent.py", "campaigns.py", "specialists.py", "real_writer_repeats.py")},
              "additional_runs": [], "provider_requests": provider.rows}
    with tempfile.TemporaryDirectory(prefix="liveops-writer-repeats-") as directory:
        root = Path(directory)
        with patch("httpx.post", side_effect=provider.post):
            for repeat in range(1, 5):
                for workers in ((3, 1) if repeat % 2 else (1, 3)):
                    provider.label = f"writer_repeat/{repeat}/workers_{workers}"
                    h = CampaignHarness(Store(str(root / f"{repeat}-{workers}.db")), workers=workers)
                    before = h.store.snapshot(LIVE)
                    plan = h.create(LIVE, "三商品真实模型手卡", skus=["SK-101", "SK-102", "SK-103"])
                    start = time.perf_counter()
                    state = h.run(LIVE, plan["run_id"])
                    artifact = h.artifact(LIVE, plan["run_id"], state["output_artifact"]) if "output_artifact" in state else None
                    report["additional_runs"].append({"repeat": repeat, "workers": workers,
                        "elapsed_ms": round((time.perf_counter() - start) * 1000, 3), "status": state["status"],
                        "approved_briefs": len(artifact["approved_briefs"]) if artifact else 0,
                        "final": artifact, "trace": state["trace"],
                        "business_state_unchanged": h.store.snapshot(LIVE) == before})
                    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    print(f"Repeat {repeat}, workers {workers}: {state['status']}", flush=True)
    all_runs = [{"repeat": 0, **row} for row in original["writer_runs"]] + report["additional_runs"]
    report["summary"] = [{"workers": workers, "runs": 5,
        "elapsed_ms_values": [r["elapsed_ms"] for r in all_runs if r["workers"] == workers],
        "median_elapsed_ms": statistics.median(r["elapsed_ms"] for r in all_runs if r["workers"] == workers),
        "succeeded": sum(r["status"] == "succeeded" for r in all_runs if r["workers"] == workers),
        "approved_briefs": sum(r["approved_briefs"] for r in all_runs if r["workers"] == workers)} for workers in (1, 3)]
    report["additional_usage"] = usage_summary(provider.rows)
    report["combined_usage"] = usage_summary(original["provider_requests"] + provider.rows)
    report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"summary": report["summary"], "combined_usage": report["combined_usage"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
