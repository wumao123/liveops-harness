"""Restart the existing local console without replacing its interactive databases."""

import argparse
import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import time
import urllib.request
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATABASES = {"business": "interactive_20261019.db", "checkpoints": "interactive_checkpoints_20261019.db", "platform": "interactive_platform_20261019.db"}


def fetch(path):
    with urllib.request.urlopen("http://127.0.0.1:8765" + path, timeout=3) as response:
        return json.load(response)


def signature(path):
    with sqlite3.connect(path) as db:
        result = {table: db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                  for table in ("live_sessions", "products", "coupons", "commands", "action_ledger", "audit_events", "preferences", "remote_operations")}
    return hashlib.sha256(json.dumps(result, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def database_signature(path):
    digest = hashlib.sha256()
    with sqlite3.connect(path) as db:
        for statement in db.iterdump():
            digest.update(statement.encode())
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, required=True)
    args = parser.parse_args()
    command = subprocess.check_output(["ps", "-p", str(args.pid), "-o", "command="], text=True)
    if "uvicorn liveops.api:app" not in command or "--port 8765" not in command:
        raise SystemExit("Refusing to stop an unexpected process")
    for filename in DATABASES.values():
        if not (ROOT/"data"/filename).is_file():
            raise SystemExit("Expected interactive database missing; refusing a data-path change")
    previous = fetch("/health")
    os.kill(args.pid, signal.SIGTERM)
    for _ in range(120):
        status = subprocess.run(["ps", "-p", str(args.pid), "-o", "stat="], capture_output=True, text=True).stdout.strip()
        if not status or status.startswith("Z"):
            break
        time.sleep(0.5)
    else:
        raise SystemExit("Server did not stop gracefully; no new process started")
    backup = ROOT/"data"/"backups"/("article_upgrade_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    backup.mkdir(parents=True)
    for filename in DATABASES.values():
        with sqlite3.connect(ROOT/"data"/filename) as source, sqlite3.connect(backup/filename) as target:
            source.backup(target)
        os.chmod(backup/filename, 0o600)
    before = signature(ROOT/"data"/DATABASES["business"])
    extra_before = {name: database_signature(ROOT/"data"/filename) for name, filename in DATABASES.items() if name != "business"}
    env = {**os.environ, "LIVEOPS_DB_PATH": str(ROOT/"data"/DATABASES["business"]),
           "LIVEOPS_CHECKPOINT_PATH": str(ROOT/"data"/DATABASES["checkpoints"]),
           "LIVEOPS_PLATFORM_PATH": str(ROOT/"data"/DATABASES["platform"]), "LIVEOPS_COUPON_BACKEND": "simulated_remote"}
    with (ROOT/"data"/"interactive_server_20261019.log").open("ab") as log:
        process = subprocess.Popen([str(ROOT/".venv"/"bin"/"python"), "-m", "liveops.private_config", "-m", "uvicorn", "liveops.api:app", "--host", "127.0.0.1", "--port", "8765"],
                                   cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(90):
        if process.poll() is not None:
            raise SystemExit("Server exited; inspect the local log. Backups retained.")
        try:
            after_health = fetch("/health")
            runtime = fetch("/api/runtime/LIVE-DEMO-001")
            break
        except (OSError, ValueError):
            time.sleep(0.5)
    else:
        raise SystemExit("Server health timeout; backups retained.")
    after = signature(ROOT/"data"/DATABASES["business"])
    extra_after = {name: database_signature(ROOT/"data"/filename) for name, filename in DATABASES.items() if name != "business"}
    report = {"url": "http://127.0.0.1:8765/", "pid": process.pid, "backup": str(backup),
              "before_business_sha256": before, "after_business_sha256": after,
              "all_existing_business_rows_unchanged": before == after,
              "checkpoint_and_platform_unchanged": extra_before == extra_after,
              "before_health": previous, "after_health": after_health,
              "runtime_scene": runtime["state"]["scene"], "runtime_revision": runtime["state"]["revision"]}
    (ROOT/"outputs"/"article_harness_rollout_20261019.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if before != after or extra_before != extra_after:
        raise SystemExit("Existing business records changed; inspect before proceeding")


if __name__ == "__main__":
    main()
