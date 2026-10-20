"""Run response-loss recovery through the HTTP API with a persistent simulator."""

import argparse
import json
import os
import tempfile
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from .api import create_app


def demonstrate() -> dict:
    os.environ.pop("LIVEOPS_LLM_BASE_URL", None)
    os.environ.pop("LIVEOPS_LLM_MODEL", None)
    live_id = "LIVE-DEMO-001"
    command_id = str(uuid.uuid4())
    events = []
    with tempfile.TemporaryDirectory(prefix="liveops-remote-demo-") as directory:
        root = Path(directory)

        def app():
            return create_app(str(root / "commerce.db"), str(root / "graph.db"),
                              coupon_backend="simulated_remote", remote_path=str(root / "platform.db"))

        first = app()
        with TestClient(first) as client:
            client.post("/api/simulator/remote-fault", json={"mode": "delayed_visibility", "visibility_queries": 2}).raise_for_status()
            pending = client.post("/api/commands", json={"command_id": command_id, "instruction": "给面霜发 10 元券 20 张"}).json()
            events.append({"phase": "proposal", "status": pending["status"]})
            submitted = client.post(f"/api/commands/{command_id}/approval", json={"approved": True}).json()
            events.append({"phase": "response_lost", "status": submitted["status"],
                           "platform_batches": first.state.remote_operations.platform.coupon_count(),
                           "local_batches": len(first.state.store.snapshot(live_id)["coupons"])})
            assert submitted["status"] == "unknown"

        restarted = app()
        with TestClient(restarted) as client:
            for index in range(3):
                recovered = client.post(f"/api/commands/{command_id}/reconcile").json()
                events.append({"phase": f"query_after_restart_{index+1}", "status": recovered["status"]})
            assert recovered["status"] == "ok"
            platform = restarted.state.remote_operations.platform
            submissions = platform.submission_count(f"remote-{command_id}")
            local_batches = len(restarted.state.store.snapshot(live_id)["coupons"])
            with restarted.state.store.connection() as db:
                ledger_rows = db.execute("SELECT COUNT(*) FROM action_ledger WHERE command_id=?", (command_id,)).fetchone()[0]
            assert submissions == local_batches == ledger_rows == platform.coupon_count() == 1
            return {
                "scope": "local_simulated_platform_no_real_taobao", "command_id": command_id,
                "events": events, "platform_submit_calls": submissions,
                "platform_batches": platform.coupon_count(), "local_batches": local_batches, "ledger_rows": ledger_rows,
            }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="outputs/remote_recovery_demo.json")
    args = parser.parse_args()
    report = demonstrate()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
