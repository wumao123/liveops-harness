"""Durable coupon outbox with a separate, fault-injectable platform ledger."""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from .store import BusinessError, Store
from .tooling import business_error, error


FAULTS = {"normal", "lost_response", "delayed_visibility", "before_commit", "query_unavailable", "reject"}


class RemoteRejected(Exception):
    pass


class QueryUnavailable(Exception):
    pass


class SimulatedPlatform:
    """A simulated external system whose commit is independent of the local DB."""

    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as db:
            db.executescript(
                "CREATE TABLE IF NOT EXISTS platform_config (id INTEGER PRIMARY KEY, mode TEXT, visibility_queries INTEGER);"
                "INSERT OR IGNORE INTO platform_config VALUES (1, 'normal', 2);"
                "CREATE TABLE IF NOT EXISTS platform_coupons (operation_id TEXT PRIMARY KEY, payload_hash TEXT, receipt TEXT, hidden_queries INTEGER);"
                "CREATE TABLE IF NOT EXISTS submit_counts (operation_id TEXT PRIMARY KEY, count INTEGER);"
            )

    def set_fault(self, mode: str, visibility_queries: int = 2) -> None:
        if mode not in FAULTS or not 0 <= visibility_queries <= 10:
            raise ValueError("Unknown simulator fault")
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE platform_config SET mode=?, visibility_queries=? WHERE id=1", (mode, visibility_queries))

    def get_fault(self) -> dict:
        with sqlite3.connect(self.path) as db:
            mode, queries = db.execute("SELECT mode, visibility_queries FROM platform_config WHERE id=1").fetchone()
        return {"enabled": True, "mode": mode, "visibility_queries": queries}

    def create_coupon(self, operation_id: str, action: dict, payload_hash: str) -> dict:
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO submit_counts VALUES (?, 1) ON CONFLICT(operation_id) DO UPDATE SET count=count+1", (operation_id,))
            previous = db.execute("SELECT payload_hash, receipt FROM platform_coupons WHERE operation_id=?", (operation_id,)).fetchone()
            if previous:
                if previous[0] != payload_hash:
                    raise RemoteRejected("Operation ID is bound to different parameters")
                return json.loads(previous[1])
            mode, hidden_queries = db.execute("SELECT mode, visibility_queries FROM platform_config WHERE id=1").fetchone()
            if mode in {"before_commit", "reject"}:
                db.commit()
                if mode == "reject":
                    raise RemoteRejected("Simulated platform explicitly rejected the operation")
                raise TimeoutError("Simulated request failed before a commit")
            receipt = {
                "operation_id": operation_id, "payload_hash": payload_hash,
                "coupon_id": f"SIM-{uuid.uuid4().hex[:12]}",
                "sku": action["sku"], "amount_cents": action["amount_cents"], "quantity": action["quantity"],
            }
            db.execute("INSERT INTO platform_coupons VALUES (?, ?, ?, ?)",
                       (operation_id, payload_hash, Store._json(receipt), hidden_queries if mode == "delayed_visibility" else 0))
            db.commit()
        if mode in {"lost_response", "delayed_visibility", "query_unavailable"}:
            raise TimeoutError("Platform committed, but the response was lost")
        return receipt

    def query(self, operation_id: str) -> dict:
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute("BEGIN IMMEDIATE")
            mode = db.execute("SELECT mode FROM platform_config WHERE id=1").fetchone()[0]
            if mode == "query_unavailable":
                raise QueryUnavailable("This simulator mode does not provide status queries")
            row = db.execute("SELECT receipt, hidden_queries FROM platform_coupons WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None:
                return {"status": "not_found"}
            if row[1] > 0:
                db.execute("UPDATE platform_coupons SET hidden_queries=hidden_queries-1 WHERE operation_id=?", (operation_id,))
                return {"status": "pending"}
            return {"status": "succeeded", "receipt": json.loads(row[0])}

    def coupon_count(self) -> int:
        with sqlite3.connect(self.path) as db:
            return db.execute("SELECT COUNT(*) FROM platform_coupons").fetchone()[0]

    def submission_count(self, operation_id: str) -> int:
        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT count FROM submit_counts WHERE operation_id=?", (operation_id,)).fetchone()
        return row[0] if row else 0


class RemoteOperations:
    def __init__(self, store: Store, platform: SimulatedPlatform, max_queries: int = 3):
        self.store, self.platform, self.max_queries = store, platform, max_queries

    def submit(self, live_id: str, command_id: str, action: dict, key: str, expires_at: float) -> dict:
        previous = self.store.ledger_result(key)
        if previous:
            return {"status": "ok", **previous, "replayed": True}
        operation_id = self.store.queue_remote_coupon(live_id, command_id, action, key, expires_at)
        return self._dispatch(operation_id)

    def _dispatch(self, operation_id: str) -> dict:
        try:
            claimed = self.store.begin_remote_dispatch(operation_id)
        except BusinessError as exc:
            self.store.set_remote_state(operation_id, "failed", exc.code)
            return self._response(operation_id)
        if not claimed:
            return self.reconcile(operation_id)
        operation = self.store.remote_operation(operation_id)
        try:
            receipt = self.platform.create_coupon(operation_id, operation["action"], operation["payload_hash"])
        except TimeoutError:
            self.store.set_remote_state(operation_id, "unknown", "REMOTE_TIMEOUT")
        except RemoteRejected:
            self.store.set_remote_state(operation_id, "failed", "REMOTE_REJECTED")
        else:
            self._complete(operation_id, receipt)
        return self._response(operation_id)

    def _complete(self, operation_id: str, receipt: dict) -> None:
        operation = self.store.remote_operation(operation_id)
        action = operation["action"]
        matches = (receipt.get("operation_id") == operation_id
                   and receipt.get("payload_hash") == operation["payload_hash"]
                   and all(receipt.get(key) == action[key] for key in ("sku", "amount_cents", "quantity"))
                   and isinstance(receipt.get("coupon_id"), str) and bool(receipt["coupon_id"]))
        if not matches:
            self.store.set_remote_state(operation_id, "manual_review", "REMOTE_RECEIPT_MISMATCH")
            return
        self.store.complete_remote_coupon(operation_id, receipt)

    def reconcile(self, operation_id: str) -> dict:
        operation = self.store.remote_operation(operation_id)
        if operation["status"] == "ready":
            return self._dispatch(operation_id)
        if operation["status"] in {"succeeded", "failed"}:
            return self._response(operation_id)
        try:
            observed = self.platform.query(operation_id)
        except QueryUnavailable:
            self.store.set_remote_state(operation_id, "manual_review", "REMOTE_QUERY_UNAVAILABLE")
            return self._response(operation_id)
        queries = self.store.note_remote_query(operation_id, observed["status"])
        if observed["status"] == "succeeded":
            self._complete(operation_id, observed["receipt"])
        elif queries >= self.max_queries:
            self.store.set_remote_state(operation_id, "manual_review", "REMOTE_UNCERTAIN")
        else:
            self.store.set_remote_state(operation_id, "unknown", "REMOTE_NOT_VISIBLE")
        return self._response(operation_id)

    def _response(self, operation_id: str) -> dict:
        operation = self.store.remote_operation(operation_id)
        status = operation["status"]
        if status == "succeeded":
            return {"status": "ok", **operation["result"]}
        messages = {
            "unknown": "提交状态待确认；将查询回执，不重复发券",
            "manual_review": "状态无法自动确认，需要人工对账；未重复发券",
            "failed": "该操作已停止，未确认成功",
            "sent": "已发送，等待平台回执",
            "ready": "操作已持久化，等待发送",
        }
        message = messages[status]
        tool_error = business_error(operation["last_error"], message) if status == "failed" else error(
            operation["last_error"] or "REMOTE_PENDING", "uncertain", message,
        )
        if status == "manual_review":
            tool_error["next_step"] = "manual_review"
        return {
            "status": "blocked" if status == "failed" else status,
            "operation_id": operation_id, "remote_status": status,
            "code": operation["last_error"], "message": message, "tool_error": tool_error,
        }
