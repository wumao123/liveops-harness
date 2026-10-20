"""Offline regression gate: fail if safety regresses or correctness drops against the committed baseline.

Usage: .venv/bin/python tools/regression_gate.py [--update-baseline]
Runs pytest, the 52-case scenario set (all ablations) and the 72-case blind set offline. No model calls.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "data/regression_baseline.json"
PY = str(ROOT / ".venv/bin/python")
SUITES = {"scenario": "data/scenario_eval_20261019.json", "blind": "data/blind_eval_20261020.json"}


def run_suites() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("LIVEOPS_LLM") and k != "LIVEOPS_SPECIALIST_MODEL"}
    tests = subprocess.run([PY, "-m", "pytest", "-q"], cwd=ROOT, env=env, capture_output=True, text=True)
    summary = {"pytest_returncode": tests.returncode, "pytest_tail": tests.stdout.strip().splitlines()[-1:]}
    for name, cases in SUITES.items():
        with tempfile.TemporaryDirectory() as root:
            out = Path(root) / "out.json"
            args = [PY, "-m", "liveops.scenario_eval", "--cases", cases, "--output", str(out)]
            if name == "blind":
                args += ["--ablations", "full"]
            done = subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True)
            if done.returncode:
                raise SystemExit(f"{name} run failed:\n{done.stderr[-2000:]}")
            variants = json.loads(out.read_text())["variants"]
        summary[name] = {v: {k: body["summary"]["all"][k] for k in ("n", "correct", "violation", "duplicate_write")}
                         for v, body in variants.items()}
    return summary


def compare(current: dict, baseline: dict) -> list[str]:
    problems = []
    if current["pytest_returncode"]:
        problems.append(f"pytest failed: {current['pytest_tail']}")
    for suite in SUITES:
        for variant, now in current[suite].items():
            before = baseline.get(suite, {}).get(variant)
            if variant == "full" and (now["violation"] or now["duplicate_write"]):
                problems.append(f"{suite}/full: {now['violation']} violations, {now['duplicate_write']} duplicate writes")
            if before and now["correct"] < before["correct"]:
                problems.append(f"{suite}/{variant}: correct {before['correct']} -> {now['correct']}")
            if before and variant != "full" and now["violation"] < before["violation"]:
                # An ablation that stops failing means the layer it removes is no longer being exercised.
                problems.append(f"{suite}/{variant}: ablation violations {before['violation']} -> {now['violation']}")
    return problems


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--update-baseline", action="store_true")
    args = parser.parse_args()
    current = run_suites()
    if args.update_baseline:
        BASELINE.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("baseline written:", BASELINE.relative_to(ROOT))
        return
    if not BASELINE.exists():
        raise SystemExit("no baseline; run with --update-baseline once on a known-good commit")
    problems = compare(current, json.loads(BASELINE.read_text(encoding="utf-8")))
    print(json.dumps({s: current[s]["full"] for s in SUITES}, ensure_ascii=False))
    if problems:
        print("REGRESSION:\n- " + "\n- ".join(problems))
        sys.exit(1)
    print("gate passed")


if __name__ == "__main__":
    main()
