"""Start a real offline HTTP server in a temporary sandbox and check the main flows.

Run: .venv/bin/python tools/http_smoke.py
No existing business databases are read or changed; no model credentials are loaded.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
LIVE = "LIVE-DEMO-001"


def main():
    checks = []

    def check(name, value):
        if not value:
            raise AssertionError(name)
        checks.append(name)

    with tempfile.TemporaryDirectory(prefix="liveops-http-smoke-") as directory:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = {k: v for k, v in os.environ.items() if not k.startswith("LIVEOPS_")}
        env.update(PYTHONPATH=str(ROOT), LIVEOPS_COUPON_BACKEND="simulated_remote")
        args = [sys.executable, "-m", "uvicorn", "liveops.api:app", "--host", "127.0.0.1", "--port", str(port)]
        log_path = Path(directory) / "server.log"
        process = None

        def start():
            nonlocal process
            with log_path.open("ab") as log:
                process = subprocess.Popen(args, cwd=directory, env=env, stdout=log, stderr=log)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(log_path.read_text())
                try:
                    if client.get("/health").status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            raise TimeoutError("server startup: " + log_path.read_text())

        def stop():
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10) as client:
            def get(url):
                response = client.get(url)
                response.raise_for_status()
                return response.json()

            def post(url, body):
                response = client.post(url, json=body)
                response.raise_for_status()
                return response.json()

            try:
                start()
                for url in ("/", "/app.js", "/app.css", "/harness.css", "/docs", "/openapi.json"):
                    check("asset:" + url, client.get(url).status_code == 200)
                check("five_products", len(get(f"/api/state/{LIVE}")["products"]) == 5)
                missing = post("/api/commands", {"instruction": "给面霜发10元券"})
                check("missing_quantity_clarified", missing["status"] == "clarification")
                command = post("/api/commands", {"command_id": "release-coupon", "instruction": "给面霜发10元券20张"})
                check("coupon_waits_for_approval", command["status"] == "pending_approval")
                check("no_coupon_before_approval", get(f"/api/state/{LIVE}")["coupons"] == [])
                stop()
                start()
                pending = get(f"/api/commands/{LIVE}")
                check("approval_survives_process_restart", any(c["command_id"] == "release-coupon" and c["status"] == "pending_approval" for c in pending))
                post("/api/simulator/remote-fault", {"mode": "lost_response"})
                approved = post("/api/commands/release-coupon/approval", {"approved": True})
                check("lost_remote_response_is_unknown", approved["status"] == "unknown")
                result = post("/api/commands/release-coupon/reconcile", {})
                check("remote_receipt_reconciled", result["status"] == "ok")
                replay = post("/api/commands", {"command_id": "release-coupon", "instruction": "给面霜发10元券20张"})
                check("command_replay_is_idempotent", replay["status"] == "ok" and len(get(f"/api/state/{LIVE}")["coupons"]) == 1)
                plan = post("/api/campaigns", {"live_id": LIVE, "instruction": "准备三款商品手卡并合规审核", "top_k": 3})
                run_id = plan["run_id"]
                prefix = f"/api/campaigns/{LIVE}/{run_id}"
                post(prefix + "/start", {})
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    state = get(prefix)
                    if state["status"] == "succeeded":
                        break
                    time.sleep(0.1)
                check("background_campaign_finishes", state["status"] == "succeeded")
                check("eight_campaign_tasks", len(state["tasks"]) == 8)
                material = get(prefix + "/artifacts/" + state["output_artifact"])
                check("three_approved_briefs", len(material["approved_briefs"]) == 3)
                events = client.get(prefix + "/events")
                events.raise_for_status()
                check("sse_progress_ends", "text/event-stream" in events.headers["content-type"] and "event: end" in events.text)
                get(prefix + "/metrics")
                get(f"/api/memory/{LIVE}")
                get(f"/api/runtime/{LIVE}")
                get("/api/contracts")
                check("metrics_memory_runtime_contracts_available", True)
            finally:
                stop()
    print(json.dumps({"scope": "offline_http_with_temporary_simulated_remote_and_real_process_restart", "passed": len(checks), "checks": checks}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
