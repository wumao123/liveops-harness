"""Long-dialogue context experiment: does the intent model keep list, focus, budget and negative constraints?

Six scripted 60-turn sessions run offline through the real agent (rules), so every variant sees the same history.
At fixed turns, four probe instructions are parsed by the model under three context designs. Probes are parsed
only, never submitted, so they do not change the session. This measures the intent layer, not end-to-end safety.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from .agent import _guard_instruction, _llm_action, _normalize_action
from .api import create_app
from .store import Store


LIVE = "LIVE-DEMO-001"
NAMES = {"SK-101": "洁面", "SK-102": "面霜", "SK-103": "防晒乳", "SK-104": "精华", "SK-105": "卸妆巾"}
# Budgets keep the negated product inside budget, so a remembered budget cannot satisfy the negation by accident.
# Every negated product is in the no-budget top 3 (SK-104, SK-103, SK-101), so forgetting it is visible.
GROUPS = [
    {"group": "g1", "budget_yuan": 100, "negated": "SK-103", "focus": "SK-102"},
    {"group": "g2", "budget_yuan": 130, "negated": "SK-101", "focus": "SK-105"},
    {"group": "g3", "budget_yuan": 160, "negated": "SK-104", "focus": "SK-101"},
    {"group": "g4", "budget_yuan": 100, "negated": "SK-101", "focus": "SK-104"},
    {"group": "g5", "budget_yuan": 160, "negated": "SK-104", "focus": "SK-103"},
    {"group": "g6", "budget_yuan": 130, "negated": "SK-103", "focus": "SK-105"},
]
FILLERS = ["查看状态", "当前在讲哪款", "复盘一下", "谢谢", "好的，继续", "直播间节奏怎么样"]
PROBE_AT = (15, 30, 45, 60)
PROBES = {
    "list": "给第二款写一段口播",
    "focus": "它现在库存还有多少",
    "budget": "按刚才的预算再推荐三款",
    "negative": "推荐三款开场商品",
}
VARIANTS = ("full_history", "recent_4_turns", "recent_4_plus_structured_state")


def script(group: dict) -> list[str]:
    turns = ["你好", f"今天预算{group['budget_yuan']}元以内，推荐三款",
             f"今天{NAMES[group['negated']]}先不上，别让它进开场", f"介绍{NAMES[group['focus']]}"]
    while len(turns) < max(PROBE_AT):
        turns.append(FILLERS[len(turns) % len(FILLERS)])
    return turns


def _rank(action: dict, products: list[dict]) -> list[str]:
    ranked = sorted((p for p in products if p["stock"] > 0 and p["price_cents"] <= action.get("max_price_cents", float("inf"))
                     and (not action.get("skus") or p["sku"] in action["skus"])
                     and p["sku"] not in (action.get("exclude_skus") or [])),
                    key=lambda p: (p["stock"] < 50, -(p["price_cents"] - p["cost_cents"]), -p["stock"]))
    return [p["sku"] for p in ranked[:action.get("top_k", 5)]]


def conversations(app, scene: str = "on_live") -> dict[str, dict]:
    workspace = app.state.workspace
    with app.state.store.connection() as db:
        rows = db.execute("SELECT user_text,answer_text,status,kind FROM runtime_turns WHERE live_id=? AND scene=? "
                          "AND kind!='set_preference' ORDER BY created_at", (LIVE, scene)).fetchall()
    turns = [{"user": r["user_text"][:200], "assistant": r["answer_text"][:240], "status": r["status"], "action_type": r["kind"]}
             for r in rows]
    structured = workspace.context(LIVE, scene)["conversation"]
    plain = {"source": "recent_command_history_not_live_facts"}
    return {"full_history": {**plain, "turns": turns},
            "recent_4_turns": {**plain, "turns": turns[-4:]},
            "recent_4_plus_structured_state": structured}


def grade(probe: str, action: dict | None, group: dict, truth: dict, snapshot: dict) -> str:
    """kept = right answer; asked = clarified (safe, costs a turn); lost = wrong or missing; error = no action."""
    if action is None:
        return "error"
    clean = _normalize_action(action, snapshot)
    if clean["type"] == "clarify":
        return "asked"
    if probe == "list":
        return "kept" if clean["type"] in {"brief", "advise"} and clean.get("sku") == truth["list"][1] else "lost"
    if probe == "focus":
        return "kept" if clean.get("sku") == group["focus"] else "lost"
    if probe == "budget":
        return "kept" if clean["type"] in {"plan", "prepare"} and clean.get("max_price_cents") == group["budget_yuan"] * 100 else "lost"
    if clean["type"] not in {"plan", "prepare"}:
        return "lost"
    return "kept" if group["negated"] not in _rank(clean, snapshot["products"]) else "lost"


def replay_probes(group: dict, root: Path):
    """Rebuild each probe's exact snapshot offline; history is deterministic, so no provider call is needed."""
    app = create_app(str(root / f"{group['group']}.db"), str(root / f"{group['group']}-c.db"))
    agent = app.state.agent
    try:
        for index, text in enumerate(script(group), 1):
            with patch.dict(os.environ, {"LIVEOPS_LLM_MODEL": ""}):
                agent.submit(LIVE, text, f"{group['group']}-t{index}")
            if index not in PROBE_AT:
                continue
            state = app.state.workspace.get(LIVE)["topics"]["on_live"]
            active = [l for l in state["lists"] if l["status"] == "active"]
            truth = {"list": active[-1]["skus"] if active else []}
            snapshot = app.state.store.snapshot(LIVE)
            runtime = app.state.workspace.context(LIVE)
            for variant, conversation in conversations(app).items():
                yield index, variant, truth, snapshot, {**snapshot, "_conversation": conversation,
                    "_runtime": {k: v for k, v in runtime.items() if k != "conversation"}}
    finally:
        agent.close()


def regrade_with_guard(rows: list[dict]) -> list[dict]:
    """What the system would do: the recorded model action passes through the same code guard as production."""
    lookup = {(r["group"], r["turn"], r["variant"], r["probe"], r["repeat"]): r for r in rows}
    with tempfile.TemporaryDirectory(prefix="liveops-long-regrade-") as root:
        for group in GROUPS:
            for index, variant, truth, snapshot, probe_snapshot in replay_probes(group, Path(root)):
                for probe, instruction in PROBES.items():
                    for key in [k for k in lookup if k[:4] == (group["group"], index, variant, probe)]:
                        row = lookup[key]
                        if row["truth"]["list"] != truth["list"]:
                            raise RuntimeError("offline replay diverged from the recorded run")
                        guarded = None if row["action"] is None else _guard_instruction(row["action"], instruction, probe_snapshot)
                        row["system_action"] = guarded
                        row["system_outcome"] = grade(probe, guarded, group, truth, snapshot)
    return rows


def run_group(group: dict, root: Path, provider_label=lambda label: None, repeats: int = 1) -> list[dict]:
    app = create_app(str(root / f"{group['group']}.db"), str(root / f"{group['group']}-c.db"))
    agent, rows = app.state.agent, []
    try:
        turns = script(group)
        for index, text in enumerate(turns, 1):
            # History is produced offline so every variant sees identical turns.
            with patch.dict(os.environ, {"LIVEOPS_LLM_MODEL": ""}):
                agent.submit(LIVE, text, f"{group['group']}-t{index}")
            if index not in PROBE_AT:
                continue
            state = app.state.workspace.get(LIVE)["topics"]["on_live"]
            active = [l for l in state["lists"] if l["status"] == "active"]
            truth = {"list": active[-1]["skus"] if active else [], "workspace_focus": state["focus_sku"]}
            snapshot = app.state.store.snapshot(LIVE)
            runtime = app.state.workspace.context(LIVE)
            for variant, conversation in conversations(app).items():
                probe_snapshot = {**snapshot, "_runtime": {k: v for k, v in runtime.items() if k != "conversation"},
                                  "_conversation": conversation}
                for probe, instruction in PROBES.items():
                    for repeat in range(repeats):
                        provider_label(f"{group['group']}/t{index}/{variant}/{probe}/r{repeat + 1}")
                        started = time.perf_counter()
                        attempt = _llm_action(instruction, probe_snapshot)
                        rows.append({"group": group["group"], "turn": index, "variant": variant, "probe": probe,
                                     "repeat": repeat + 1, "outcome": grade(probe, attempt["action"], group, truth, snapshot),
                                     "action": attempt["action"], "attempts": attempt["attempts"],
                                     "error": (attempt["error"] or {}).get("code"),
                                     "conversation_chars": len(Store._json(conversation)),
                                     "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                                     "truth": {"list": truth["list"], "focus": group["focus"],
                                               "budget_cents": group["budget_yuan"] * 100, "negated": group["negated"],
                                               "workspace_focus": truth["workspace_focus"]}})
    finally:
        agent.close()
    return rows


def summarize(rows: list[dict], field: str = "outcome") -> dict:
    out = {}
    for variant in VARIANTS:
        subset = [r for r in rows if r["variant"] == variant]
        block = {"n": len(subset)}
        for probe in PROBES:
            probe_rows = [r for r in subset if r["probe"] == probe]
            block[probe] = {o: sum(r[field] == o for r in probe_rows) for o in ("kept", "asked", "lost", "error")}
        block["by_turn_kept"] = {t: sum(r[field] == "kept" for r in subset if r["turn"] == t) for t in PROBE_AT}
        block["by_turn_n"] = {t: sum(r["turn"] == t for r in subset) for t in PROBE_AT}
        chars = sorted(r["conversation_chars"] for r in subset)
        block["conversation_chars_median"] = chars[len(chars) // 2] if chars else None
        block["conversation_chars_max"] = chars[-1] if chars else None
        latencies = sorted(r["elapsed_ms"] for r in subset)
        block["parse_ms_p50"] = latencies[len(latencies) // 2] if latencies else None
        out[variant] = block
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--real-provider", action="store_true")
    parser.add_argument("--repeats", type=int, default=1, choices=range(1, 4))
    parser.add_argument("--max-requests", type=int, default=330)
    parser.add_argument("--output", default="outputs/long_dialogue_real_20261020.json")
    parser.add_argument("--regrade", help="add code-guard outcomes to a recorded run; makes no provider calls")
    args = parser.parse_args()
    if args.regrade:
        path = Path(args.regrade)
        report = json.loads(path.read_text(encoding="utf-8"))
        report["rows"] = regrade_with_guard(report["rows"])
        report["summary_after_code_guard"] = summarize(report["rows"], "system_outcome")
        report["regrade_note"] = "model actions replayed through agent._guard_instruction offline; no new provider calls"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({v: {p: s[p] for p in PROBES} for v, s in report["summary_after_code_guard"].items()}, ensure_ascii=False, indent=1))
        return
    if not args.real_provider:
        parser.error("Pass --real-provider to explicitly authorize the bounded API experiment")
    from .private_config import load_private_settings
    from .real_model_eval import RecordedProvider
    load_private_settings()
    provider = RecordedProvider(budget=args.max_requests)
    report = {"generated_at_utc": datetime.now(timezone.utc).isoformat(), "model": os.getenv("LIVEOPS_LLM_MODEL"),
              "scope": "six_scripted_60_turn_sessions_offline_history_real_model_parses_probes_only_not_end_to_end",
              "design": {"groups": GROUPS, "probe_at_turn": PROBE_AT, "probes": PROBES, "variants": VARIANTS,
                         "fillers": FILLERS, "history_parser": "offline_rules"},
              "provider_calls": provider.rows,
              "source_sha256": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path("liveops").glob("*.py"))}}
    rows = []
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="liveops-long-") as root, patch("liveops.model_budget.httpx.post", provider.post):
            for group in GROUPS:
                rows += run_group(group, Path(root), lambda label: setattr(provider, "label", label), args.repeats)
    finally:
        report["rows"] = rows
        report["summary"] = summarize(rows)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({v: {p: s[p] for p in PROBES} | {"chars_median": s["conversation_chars_median"]}
                      for v, s in report["summary"].items()}, ensure_ascii=False, indent=1))
    print("provider_calls", len(provider.rows))


if __name__ == "__main__":
    main()
