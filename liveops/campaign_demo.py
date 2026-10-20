"""Reproducible local multi-agent harness demonstrations without platform writes."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from .campaigns import CampaignHarness
from .store import Store


class InjectedHarness(CampaignHarness):
    def __init__(self, *args, fault="hallucination", **kwargs):
        super().__init__(*args, **kwargs)
        self.fault = fault
        self.injected = False
        self.model = None

    def _perform(self, task, context):
        if self.fault == "one_failure" and task.task_id == "brief_0" and not self.injected:
            self.injected = True
            raise RuntimeError("injected read failure")
        body = super()._perform(task, context)
        if self.fault == "hallucination" and task.role == "copywriter":
            body["claims"].append("保证治愈皮肤疾病")
        return body


def main():
    live = "LIVE-DEMO-001"
    with tempfile.TemporaryDirectory(prefix="liveops-harness-demo-") as directory:
        store = Store(str(Path(directory)/"demo.db"))
        harness = CampaignHarness(store)
        harness.model = None
        before = store.snapshot(live)
        plan = harness.create(live, "准备三款商品手卡并合规复核", top_k=3)
        paused = harness.run(live, plan["run_id"], max_waves=1)
        restarted = CampaignHarness(Store(store.path))
        restarted.model = None
        done = restarted.run(live, plan["run_id"])
        final = restarted.artifact(live, done["run_id"], done["output_artifact"])
        bad_harness = InjectedHarness(store)
        bad_plan = bad_harness.create(live, "对抗测试：拦截夸大功效", top_k=2)
        blocked_materials = bad_harness.run(live, bad_plan["run_id"])
        rejected = bad_harness.artifact(live, bad_plan["run_id"], blocked_materials["output_artifact"])
        flaky = InjectedHarness(store, fault="one_failure")
        retry_plan = flaky.create(live, "故障测试：只恢复失败商品", top_k=2)
        failed = flaky.run(live, retry_plan["run_id"])
        recovered = flaky.run(live, retry_plan["run_id"], retry_failed=True)
        report = {
            "mode": "offline_deterministic_and_injected_fixtures_not_real_llm_or_taobao",
            "resume": {"before": paused["status"], "after": done["status"], "tasks": len(done["tasks"]),
                       "selection_attempts": done["tasks"]["selection"]["attempts"], "approved_briefs": len(final["approved_briefs"])},
            "grounding": {"status": blocked_materials["status"], "injected_bad_drafts": 2,
                          "admitted_bad_drafts": len(rejected["approved_briefs"])},
            "incremental_recovery": {"before": failed["status"], "after": recovered["status"],
                                     "failed_product_attempts": recovered["tasks"]["brief_0"]["attempts"],
                                     "unaffected_product_attempts": recovered["tasks"]["brief_1"]["attempts"]},
            "business_state_unchanged": store.snapshot(live) == before,
            "trace": done["trace"],
        }
    output = Path("outputs/harness_v2_demo.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n")
    print(json.dumps({k:v for k,v in report.items() if k != "trace"}, ensure_ascii=False, indent=2))
    print(f"Saved: {output.resolve()}")


if __name__ == "__main__":
    main()
