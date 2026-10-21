"""Review-then-revise experiment: injected bad first drafts, revised by the real model from the reviewer's issue codes.

The first draft of every task is a preset faulty fixture (the same nine fault types as multiagent_eval), because
real first drafts passed review 30/30 on 2026-10-18 and would give the loop nothing to fix. Only revision
rounds call the model. Control arm: the same fixtures with revisions disabled.

Usage: .venv/bin/python -m liveops.revision_eval [--offline]
--offline replaces the model with the deterministic writer (checks the plumbing, says nothing about the model).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from .campaigns import CampaignHarness, TaskSpec
from .multiagent_eval import FAULTS, SKUS, corrupt
from .store import Store


LIVE = "LIVE-DEMO-001"
ROOT = Path(__file__).resolve().parents[1]
REQUEST_BUDGET = 150


class FirstDraftFixture:
    """Stands in for the writer model: round 0 returns the faulty fixture, later rounds go to the real writer."""

    def __init__(self, harness: CampaignHarness, fault: str, writer):
        self.harness, self.fault, self.writer = harness, fault, writer

    def draft(self, context):
        task = TaskSpec(task_id=context["task_id"], role="copywriter", dependencies=["selection"],
                        sku=context["products"][0]["sku"])
        if not context.get("revision_feedback"):
            body = corrupt(self.harness._perform(task, context), self.fault)
            return body, {"backend": "fixture", "model_calls": 0, "reported_total_tokens": 0}
        if self.writer is None:
            return self.harness._perform(task, context), {
                "backend": "deterministic", "model_calls": 0, "reported_total_tokens": 0}
        return self.writer.draft(context)


def one_run(root: Path, sku: str, fault: str, revisions: int, writer) -> dict:
    harness = CampaignHarness(Store(str(root / f"{sku}-{fault}-{revisions}.db")), max_revisions=revisions)
    harness.model = FirstDraftFixture(harness, fault, writer)
    before = harness.store.snapshot(LIVE)
    plan = harness.create(LIVE, "审核打回修订实验", skus=[sku])
    started = time.perf_counter()
    state = harness.run(LIVE, plan["run_id"])
    output = harness.artifact(LIVE, plan["run_id"], state["output_artifact"]) if state.get("output_artifact") else None
    briefs = [t for t in state["trace"] if t["task_id"] == "brief_0"]
    reviews = [t for t in state["trace"] if t["task_id"] == "review_0"]
    return {
        "sku": sku, "fault": fault, "max_revisions": revisions, "status": state["status"],
        "admitted": bool(output and output["approved_briefs"]),
        "rounds_used": state["tasks"]["brief_0"].get("revisions", 0),
        "review_issues": [harness.artifact(LIVE, plan["run_id"], t["artifact_id"])["issues"]
                          for t in reviews if t["status"] == "succeeded"],
        "writer_backends": [t.get("backend") for t in briefs],
        "model_calls": sum(t.get("model_calls", 0) for t in briefs),
        "reported_total_tokens": sum(t.get("reported_total_tokens", 0) for t in briefs),
        "failed_tasks": [t["task_id"] for t in state["trace"] if t["status"] == "failed"],
        "steps": state["steps"], "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        "business_state_unchanged": harness.store.snapshot(LIVE) == before,
    }


def summarize(rows: list[dict]) -> dict:
    def arm(revisions):
        part = [r for r in rows if r["max_revisions"] == revisions]
        return {"runs": len(part), "admitted": sum(r["admitted"] for r in part),
                "review_required": sum(r["status"] == "review_required" for r in part),
                "other_status": sum(r["status"] not in {"succeeded", "review_required"} for r in part),
                "admitted_by_round": {n: sum(r["admitted"] and r["rounds_used"] == n for r in part) for n in (0, 1, 2)},
                "model_calls": sum(r["model_calls"] for r in part),
                "reported_total_tokens": sum(r["reported_total_tokens"] for r in part),
                "steps": sum(r["steps"] for r in part),
                "business_state_unchanged": all(r["business_state_unchanged"] for r in part)}
    loop = [r for r in rows if r["max_revisions"] == 2]
    return {"control_no_revision": arm(0), "revise_up_to_2": arm(2),
            "by_fault": {f: {"admitted": sum(r["admitted"] for r in loop if r["fault"] == f),
                             "runs": sum(r["fault"] == f for r in loop)} for f in FAULTS}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    name = "revision_eval_offline_20261021.json" if args.offline else "revision_eval_20261021.json"
    output = ROOT / "outputs" / name
    if output.exists() and not args.offline:
        raise SystemExit("Real-model results already exist; refusing accidental duplicate spending")
    writer, provider = None, None
    if not args.offline:
        from .private_config import load_private_settings
        from .real_model_eval import RecordedProvider, usage_summary
        from .specialists import SpecialistModel
        load_private_settings()
        model = os.getenv("LIVEOPS_SPECIALIST_MODEL") or os.environ["LIVEOPS_LLM_MODEL"]
        writer = SpecialistModel(os.environ["LIVEOPS_LLM_BASE_URL"], model, os.environ["LIVEOPS_LLM_API_KEY"])
        provider = RecordedProvider(budget=REQUEST_BUDGET)
    report = {"scope": "injected_faulty_first_drafts_real_model_revisions_not_natural_error_rate",
              "started_at_utc": datetime.now(timezone.utc).isoformat(), "python": platform.python_version(),
              "writer": "deterministic_offline" if args.offline else writer.model,
              "thinking": os.environ.get("LIVEOPS_LLM_THINKING"),
              "request_budget": None if args.offline else REQUEST_BUDGET,
              "faults": list(FAULTS), "skus": SKUS,
              "source_sha256": {n: hashlib.sha256((ROOT / "liveops" / n).read_bytes()).hexdigest()
                                for n in ("campaigns.py", "specialists.py", "contracts.py", "revision_eval.py")},
              "rows": []}

    def save():
        report["summary"] = summarize(report["rows"])
        if provider:
            report["provider_requests"] = provider.rows
            report["usage_summary"] = usage_summary(provider.rows)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    with tempfile.TemporaryDirectory(prefix="liveops-revision-") as directory:
        root = Path(directory)
        context = patch("httpx.post", side_effect=provider.post) if provider else patch.dict(os.environ, {})
        try:
            with context:
                for sku in SKUS:
                    for fault in FAULTS:
                        for revisions in (0, 2):
                            if provider:
                                provider.label = f"revision/{sku}/{fault}/max{revisions}"
                            report["rows"].append(one_run(root, sku, fault, revisions, writer))
                    save()
                    print(f"{sku} done", flush=True)
            report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        finally:
            save()
    print(json.dumps({"summary": report["summary"], "usage": report.get("usage_summary"), "output": str(output)},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
