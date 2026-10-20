"""Measure the local offline workflow without claiming production throughput."""

from __future__ import annotations

import argparse
import json
import math
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .agent import LiveAgent
from .store import Store


LIVE_ID = "LIVE-DEMO-001"


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * percentile) - 1)]


def run_once(requests: int, workers: int) -> dict[str, float | int]:
    with tempfile.TemporaryDirectory(prefix="liveops-bench-") as directory:
        root = Path(directory)
        agent = LiveAgent(Store(str(root / "commerce.db")), str(root / "checkpoints.db"))

        def one_request(_index: int) -> tuple[float, bool]:
            started = time.perf_counter()
            try:
                response = agent.submit(LIVE_ID, "介绍当前商品", str(uuid.uuid4()))
                success = response["status"] == "ok"
            except Exception:
                success = False
            return (time.perf_counter() - started) * 1000, success

        started = time.perf_counter()
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(one_request, range(requests)))
        finally:
            agent.close()
        elapsed = time.perf_counter() - started
    latencies = [latency for latency, _ in results]
    return {
        "workers": workers,
        "requests": requests,
        "success": sum(success for _, success in results),
        "failed": sum(not success for _, success in results),
        "throughput_rps": round(requests / elapsed, 2),
        "p50_ms": round(_percentile(latencies, 0.50), 2),
        "p95_ms": round(_percentile(latencies, 0.95), 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Local offline read-command concurrency baseline")
    parser.add_argument("--requests", type=int, default=50)
    parser.add_argument("--workers", type=int, nargs="+", default=[1, 4, 8])
    args = parser.parse_args()
    if args.requests < 1 or any(workers < 1 for workers in args.workers):
        parser.error("requests and workers must be positive")
    report = {
        "scope": "local_offline_simulation_not_production",
        "workload": "read-only brief command with SQLite command/audit/checkpoint writes",
        "runs": [run_once(args.requests, workers) for workers in args.workers],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
