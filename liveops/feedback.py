"""Recommendation feedback is observation, never authorization to execute a business action."""

from __future__ import annotations

import time

from .model_budget import ModelBudget
from .store import BusinessError, Store


# Closed reason codes keep metrics countable; free text would need review before it means anything.
REASONS = {"accept": {"fits_goal"}, "modify": {"wording", "order", "too_long", "missing_info", "other"},
           "reject": {"wrong_product", "wrong_order", "factual_error", "off_topic", "unsafe_claim", "other"}}


class Feedback:
    def __init__(self, store: Store):
        self.store = store
        with store.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS feedback_events (
                    event_id TEXT PRIMARY KEY, live_id TEXT NOT NULL, command_id TEXT NOT NULL,
                    choice TEXT NOT NULL, rating INTEGER, created_at REAL NOT NULL,
                    reason TEXT, alternative_sku TEXT);
                CREATE TABLE IF NOT EXISTS feedback_latest (
                    live_id TEXT NOT NULL, command_id TEXT NOT NULL, event_id TEXT NOT NULL,
                    PRIMARY KEY(live_id,command_id));
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(feedback_events)")}
            for column in ("reason", "alternative_sku"):
                if column not in columns:
                    db.execute(f"ALTER TABLE feedback_events ADD COLUMN {column} TEXT")
            db.commit()

    def record(self, live_id: str, command_id: str, event_id: str, choice: str, rating: int | None = None,
               reason: str | None = None, alternative_sku: str | None = None) -> dict:
        command = self.store.command(command_id)
        if not command or command["live_id"] != live_id:
            raise BusinessError("COMMAND_NOT_FOUND", "该直播间没有此建议")
        response = command["response"] or {}
        if command["status"] != "ok" or response.get("action", {}).get("type") not in {"advise", "brief", "plan"}:
            raise BusinessError("FEEDBACK_NOT_ADVICE", "仅对已完成的只读建议记录反馈，反馈不代表业务审批")
        if choice not in {"accept", "reject", "modify"} or (rating is not None and (type(rating) is not int or not 1 <= rating <= 5)):
            raise BusinessError("INVALID_FEEDBACK", "反馈类型或评分无效")
        if reason is not None and reason not in REASONS[choice]:
            raise BusinessError("INVALID_FEEDBACK", "反馈原因与反馈类型不匹配")
        if alternative_sku is not None:
            if choice == "accept":
                raise BusinessError("INVALID_FEEDBACK", "采纳时不需要替代商品")
            if not any(p["sku"] == alternative_sku for p in self.store.products()):
                raise BusinessError("INVALID_FEEDBACK", "替代商品 SKU 不存在")
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM feedback_events WHERE event_id=?", (event_id,)).fetchone()
            if existing:
                if any(existing[k] != v for k, v in {"live_id": live_id, "command_id": command_id, "choice": choice, "rating": rating,
                                                     "reason": reason, "alternative_sku": alternative_sku}.items()):
                    raise BusinessError("FEEDBACK_ID_CONFLICT", "同一反馈ID不能绑定其他内容")
                return {**dict(existing), "replayed": True, "business_action_executed": False}
            now = time.time()
            db.execute("INSERT INTO feedback_events (event_id,live_id,command_id,choice,rating,created_at,reason,alternative_sku) VALUES (?,?,?,?,?,?,?,?)",
                       (event_id, live_id, command_id, choice, rating, now, reason, alternative_sku))
            db.execute("INSERT INTO feedback_latest VALUES (?,?,?) ON CONFLICT(live_id,command_id) DO UPDATE SET event_id=excluded.event_id",
                       (live_id, command_id, event_id))
            db.commit()
        return {"event_id": event_id, "command_id": command_id, "choice": choice, "rating": rating, "reason": reason,
                "alternative_sku": alternative_sku, "business_action_executed": False}

    def metrics(self, live_id: str) -> dict:
        self.store.live(live_id)
        with self.store.connection() as db:
            statuses = dict(db.execute("SELECT status,COUNT(*) FROM commands WHERE live_id=? GROUP BY status", (live_id,)).fetchall())
            rows = db.execute("SELECT e.* FROM feedback_latest l JOIN feedback_events e ON e.event_id=l.event_id WHERE l.live_id=?", (live_id,)).fetchall()
            commands = db.execute("SELECT command_id,status,created_at,updated_at FROM commands WHERE live_id=?", (live_id,)).fetchall()
            approvals = db.execute("SELECT command_id,event_type,created_at FROM audit_events WHERE live_id=? AND event_type IN ('approval_requested','approval_resolved','approval_expired') ORDER BY created_at", (live_id,)).fetchall()
        waits = {}
        starts = {}
        for event in approvals:
            cid = event["command_id"]
            if event["event_type"] == "approval_requested":
                starts.setdefault(cid, event["created_at"])
            elif cid in starts:
                waits[cid] = max(0, event["created_at"] - starts.pop(cid))
        finished = [c for c in commands if c["status"] not in {"received", "running", "pending_approval", "unknown", "manual_review"}]
        elapsed = [max(0, (c["updated_at"] - c["created_at"] - waits.get(c["command_id"], 0))*1000) for c in finished]
        ratings = [r["rating"] for r in rows if r["rating"] is not None]
        return {"scope": "all_commands_in_this_local_live_session", "command_status_counts": statuses,
                "terminal_commands": len(finished), "ok_commands": statuses.get("ok", 0),
                "ok_rate": statuses.get("ok", 0)/len(finished) if finished else None,
                "ok_rate_definition": "status_ok_over_terminal_including_block_reject_clarify_not_business_quality",
                "terminal_latency_ms_excluding_recorded_approval_wait": round(sum(elapsed)/len(elapsed), 2) if elapsed else None,
                "latency_scope": "command_wall_time_minus_recorded_approval_wait_not_provider_latency",
                "feedback_count": len(rows), "accepted_count": sum(r["choice"] == "accept" for r in rows),
                "acceptance_rate": sum(r["choice"] == "accept" for r in rows)/len(rows) if rows else None,
                "acceptance_definition": "latest_explicit_feedback_only_not_all_recommendations_not_causal_lift",
                "rating_count": len(ratings), "mean_rating": sum(ratings)/len(ratings) if ratings else None,
                "reason_counts": {choice: {r: n for r in sorted(REASONS[choice])
                                           if (n := sum(row["choice"] == choice and row["reason"] == r for row in rows))}
                                  for choice in ("modify", "reject")},
                "unreasoned_count": sum(r["choice"] != "accept" and r["reason"] is None for r in rows),
                "alternatives": sorted({r["alternative_sku"] for r in rows if r["alternative_sku"]}),
                "reason_definition": "operator_selected_codes_not_verified_root_cause_not_used_for_permissions",
                "provider": ModelBudget(self.store).summary(live_id),
                "unmeasured": ["GMV", "causal_uplift", "production_platform_success_rate"]}
