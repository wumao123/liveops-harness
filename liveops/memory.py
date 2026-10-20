"""Evidence-backed memory views, not another source of live price or inventory."""

from __future__ import annotations

import re
import time
from collections import Counter
from typing import Literal

from .store import BusinessError, Store


Scene = Literal["pre_live", "on_live", "post_live"]


def memory_view(store: Store, live_id: str, scene: Scene = "on_live") -> dict:
    snapshot = store.snapshot(live_id)
    statement = snapshot["preferences"].get("style", "")[:120]
    events = [event for event in store.audit(live_id, limit=100) if event["event_type"] == "action_committed"]
    counts = Counter(event["payload"]["action"]["type"] for event in events)
    switches = [event for event in reversed(events) if event["payload"]["action"]["type"] == "switch_product"]
    preferred = re.findall(r"(?:优先讲|首选)\s*(SK-\d+)(?![A-Za-z0-9-])", statement)
    negative = any(word in statement for word in ("不要", "不能", "禁止", "不讲"))
    unassessed = []
    if "开场" in statement and not negative:
        unassessed.append({"reason": "opening_event_scope_unavailable",
                           "next_step": "collect_explicit_opening_events_not_general_switches"})
    if negative or "开场" in statement:
        preferred = []
    preferred = [sku for sku in preferred if any(p["sku"] == sku for p in snapshot["products"])]
    conflicts = []
    # A transparent narrow detector: explicit SKU preference versus three committed switches.
    if len(preferred) == 1 and len(switches) >= 3:
        recent = switches[-3:]
        observed = [event["payload"]["action"]["sku"] for event in recent]
        if all(sku != preferred[0] for sku in observed):
            conflicts.append({"kind": "statement_behavior_gap", "declared_sku": preferred[0],
                              "observed_skus": observed, "evidence_event_ids": [e["event_id"] for e in recent],
                              "next_step": "ask_user_not_overwrite", "scope": "three_events_not_three_live_sessions"})
    return {
        "scene": scene, "live_id": live_id,
        "L1_statement": {"value": statement, "source": "explicit_user_preference", "can_override_policy": False},
        "L2_facts": {"source": "live_business_db", "live_version": snapshot["live"]["version"],
                     "current_sku": snapshot["live"]["current_sku"], "previous_sku": snapshot["live"].get("previous_sku"),
                     "rule": "requery_live_price_and_stock_not_summary"},
        "L3_behavior": {"source": "committed_audit_events", "window_limit": 100,
                        "sample_size": len(events), "action_counts": dict(counts),
                        "evidence_event_ids": [e["event_id"] for e in events[:20]], "causal_lift_available": False},
        "conflicts": conflicts,
        "unassessed": unassessed,
    }


def forget_preference(store: Store, live_id: str) -> dict:
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM live_sessions WHERE live_id=?", (live_id,)).fetchone() is None:
            raise BusinessError("LIVE_NOT_FOUND", "直播间不存在")
        removed = db.execute("DELETE FROM preferences WHERE live_id=? AND key='style'", (live_id,)).rowcount
        if removed:
            db.execute("UPDATE live_sessions SET version=version+1 WHERE live_id=?", (live_id,))
            db.execute("INSERT INTO audit_events (live_id,event_type,payload,created_at) VALUES (?,?,?,?)",
                       (live_id, "preference_forgotten", '{"key":"style","source":"explicit_user_request"}', time.time()))
        db.commit()
    return {"removed": bool(removed), "scope": "active_preference_only_audit_and_checkpoints_retained"}
