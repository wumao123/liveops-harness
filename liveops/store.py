"""SQLite commerce simulator, action ledger, and audit trail."""

from __future__ import annotations

import json
import hashlib
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class BusinessError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


PRODUCTS = [
    ("SK-101", "氨基酸洁面", "护肤", 7900, 4200, 180, "温和清洁；120ml；适合日常使用"),
    ("SK-102", "保湿修护面霜", "护肤", 12900, 7300, 46, "保湿修护；50g；无香精配方"),
    ("SK-103", "轻薄防晒乳", "护肤", 9900, 5400, 122, "SPF50+；50ml；清爽肤感"),
    ("SK-104", "玻尿酸精华", "护肤", 15900, 9500, 68, "补水精华；30ml；透明质酸配方"),
    ("SK-105", "棉柔卸妆巾", "护肤", 3900, 1700, 240, "一次性卸妆巾；30片；便携包装"),
]


class Store:
    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS live_sessions (
                    live_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    current_sku TEXT,
                    previous_sku TEXT,
                    goal TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 0,
                    started_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS products (
                    sku TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    category TEXT NOT NULL,
                    price_cents INTEGER NOT NULL,
                    cost_cents INTEGER NOT NULL,
                    stock INTEGER NOT NULL,
                    features TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS coupons (
                    coupon_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    live_id TEXT NOT NULL,
                    sku TEXT NOT NULL,
                    amount_cents INTEGER NOT NULL,
                    quantity INTEGER NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS commands (
                    command_id TEXT PRIMARY KEY,
                    live_id TEXT NOT NULL,
                    instruction TEXT NOT NULL,
                    status TEXT NOT NULL,
                    proposal TEXT,
                    response TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS action_ledger (
                    idempotency_key TEXT PRIMARY KEY,
                    command_id TEXT NOT NULL,
                    live_id TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    result TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    live_id TEXT NOT NULL,
                    command_id TEXT,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS preferences (
                    live_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (live_id, key)
                );
                CREATE TABLE IF NOT EXISTS remote_operations (
                    operation_id TEXT PRIMARY KEY,
                    command_id TEXT NOT NULL UNIQUE,
                    live_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    query_attempts INTEGER NOT NULL DEFAULT 0,
                    receipt TEXT,
                    result TEXT,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )
            columns = {row["name"] for row in db.execute("PRAGMA table_info(live_sessions)")}
            if "previous_sku" not in columns:
                db.execute("ALTER TABLE live_sessions ADD COLUMN previous_sku TEXT")
            if db.execute("SELECT COUNT(*) FROM live_sessions").fetchone()[0] == 0:
                db.execute(
                    "INSERT INTO live_sessions (live_id, status, current_sku, goal, version, started_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ("LIVE-DEMO-001", "live", "SK-101", "护肤专场：控制优惠力度，避免售罄", 0, time.time()),
                )
            if db.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0:
                db.executemany(
                    "INSERT INTO products (sku, title, category, price_cents, cost_cents, stock, features) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    PRODUCTS,
                )
            db.commit()

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    def live(self, live_id: str) -> dict[str, Any]:
        with self.connection() as db:
            row = db.execute("SELECT * FROM live_sessions WHERE live_id=?", (live_id,)).fetchone()
        if row is None:
            raise BusinessError("LIVE_NOT_FOUND", "直播间不存在")
        return dict(row)

    def products(self) -> list[dict[str, Any]]:
        with self.connection() as db:
            rows = db.execute("SELECT * FROM products ORDER BY sku").fetchall()
        return [dict(row) for row in rows]

    def product(self, sku: str) -> dict[str, Any]:
        with self.connection() as db:
            row = db.execute("SELECT * FROM products WHERE sku=?", (sku,)).fetchone()
        if row is None:
            raise BusinessError("SKU_NOT_FOUND", f"商品 {sku} 不存在")
        return dict(row)

    def preferences(self, live_id: str) -> dict[str, str]:
        with self.connection() as db:
            rows = db.execute("SELECT key, value FROM preferences WHERE live_id=?", (live_id,)).fetchall()
        return {row["key"]: row["value"] for row in rows}

    def snapshot(self, live_id: str) -> dict[str, Any]:
        with self.connection() as db:
            db.execute("BEGIN")
            live = db.execute("SELECT * FROM live_sessions WHERE live_id=?", (live_id,)).fetchone()
            if live is None:
                raise BusinessError("LIVE_NOT_FOUND", "直播间不存在")
            products = db.execute("SELECT * FROM products ORDER BY sku").fetchall()
            coupons = db.execute(
                "SELECT coupon_id, sku, amount_cents, quantity, created_at FROM coupons "
                "WHERE live_id=? ORDER BY coupon_id DESC LIMIT 8", (live_id,)
            ).fetchall()
            preferences = db.execute(
                "SELECT key, value FROM preferences WHERE live_id=?", (live_id,)
            ).fetchall()
            db.commit()
        return {
            "live": dict(live),
            "products": [dict(row) for row in products],
            "coupons": [dict(row) for row in coupons],
            "preferences": {row["key"]: row["value"] for row in preferences},
        }

    def save_command(self, command_id: str, live_id: str, instruction: str) -> bool:
        now = time.time()
        with self.connection() as db:
            try:
                db.execute(
                    "INSERT INTO commands VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (command_id, live_id, instruction, "running", None, None, now, now),
                )
                db.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def command(self, command_id: str) -> dict[str, Any] | None:
        with self.connection() as db:
            row = db.execute("SELECT * FROM commands WHERE command_id=?", (command_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        for field in ("proposal", "response"):
            result[field] = json.loads(result[field]) if result[field] else None
        return result

    def update_command(self, command_id: str, status: str, proposal: Any = None, response: Any = None) -> None:
        with self.connection() as db:
            db.execute(
                "UPDATE commands SET status=?, proposal=COALESCE(?, proposal), "
                "response=COALESCE(?, response), updated_at=? WHERE command_id=?",
                (
                    status,
                    self._json(proposal) if proposal is not None else None,
                    self._json(response) if response is not None else None,
                    time.time(),
                    command_id,
                ),
            )
            db.commit()

    def commands(self, live_id: str, limit: int = 30) -> list[dict[str, Any]]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT * FROM commands WHERE live_id=? ORDER BY created_at DESC LIMIT ?", (live_id, limit)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["proposal"] = json.loads(item["proposal"]) if item["proposal"] else None
            item["response"] = json.loads(item["response"]) if item["response"] else None
            result.append(item)
        return result

    def audit(self, live_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT * FROM audit_events WHERE live_id=? ORDER BY event_id DESC LIMIT ?", (live_id, limit)
            ).fetchall()
        return [dict(row) | {"payload": json.loads(row["payload"])} for row in rows]

    def record_event(self, live_id: str, command_id: str | None, kind: str, payload: Any) -> None:
        with self.connection() as db:
            db.execute(
                "INSERT INTO audit_events (live_id, command_id, event_type, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (live_id, command_id, kind, self._json(payload), time.time()),
            )
            db.commit()

    def apply_action(self, live_id: str, command_id: str, action: dict[str, Any], key: str) -> dict[str, Any]:
        """Commit a side effect and its audit event in one transaction."""
        kind = action["type"]
        now = time.time()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT result FROM action_ledger WHERE idempotency_key=?", (key,)
            ).fetchone()
            if previous:
                db.rollback()
                return {**json.loads(previous["result"]), "replayed": True}

            if kind in {"switch_product", "update_price", "issue_coupon"}:
                self._validate_runtime_write(db, live_id, action)
            live = db.execute("SELECT * FROM live_sessions WHERE live_id=?", (live_id,)).fetchone()
            if live is None or live["status"] != "live":
                raise BusinessError("LIVE_INACTIVE", "直播间未开播，操作已停止")
            if live["version"] != action.get("expected_live_version"):
                raise BusinessError("STALE_STATE", "直播状态已变化，请重新发起指令")

            sku = action.get("sku")
            product = db.execute("SELECT * FROM products WHERE sku=?", (sku,)).fetchone() if sku else None
            if kind in {"switch_product", "update_price", "issue_coupon"} and product is None:
                raise BusinessError("SKU_NOT_FOUND", "商品不存在，操作已停止")
            if product is not None and product["version"] != action.get("expected_product_version"):
                raise BusinessError("STALE_PRODUCT", "商品价格或库存已变化，请重新发起指令")

            if kind == "switch_product":
                if product["stock"] <= 0:
                    raise BusinessError("OUT_OF_STOCK", "商品已售罄，不能切品")
                db.execute(
                    "UPDATE live_sessions SET previous_sku=current_sku, current_sku=?, version=version+1 WHERE live_id=?",
                    (sku, live_id),
                )
                result = {"message": f"已切换讲解商品为 {product['title']}", "sku": sku}
            elif kind == "update_price":
                amount = action["amount_cents"]
                if amount < product["cost_cents"] or amount <= 0:
                    raise BusinessError("PRICE_BELOW_COST", "价格低于成本价，操作被拦截")
                db.execute("UPDATE products SET price_cents=?, version=version+1 WHERE sku=?", (amount, sku))
                db.execute("UPDATE live_sessions SET version=version+1 WHERE live_id=?", (live_id,))
                result = {"message": f"{product['title']} 价格已改为 {amount / 100:.2f} 元", "sku": sku, "price_cents": amount}
            elif kind == "issue_coupon":
                amount = action["amount_cents"]
                quantity = action["quantity"]
                if amount <= 0 or quantity < 1 or quantity > 100:
                    raise BusinessError("COUPON_LIMIT", "优惠券金额或数量超出范围")
                if product["price_cents"] - amount < product["cost_cents"]:
                    raise BusinessError("COUPON_BELOW_COST", "券后价低于成本价，操作被拦截")
                cursor = db.execute(
                    "INSERT INTO coupons (live_id, sku, amount_cents, quantity, created_at) VALUES (?, ?, ?, ?, ?)",
                    (live_id, sku, amount, quantity, now),
                )
                db.execute("UPDATE live_sessions SET version=version+1 WHERE live_id=?", (live_id,))
                result = {
                    "message": f"已为 {product['title']} 创建 {quantity} 张 {amount / 100:.2f} 元券",
                    "coupon_id": cursor.lastrowid,
                }
            elif kind == "set_preference":
                value = action["value"]
                db.execute(
                    "INSERT INTO preferences (live_id, key, value, updated_at) VALUES (?, 'style', ?, ?) "
                    "ON CONFLICT(live_id, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    (live_id, value, now),
                )
                db.execute("UPDATE live_sessions SET version=version+1 WHERE live_id=?", (live_id,))
                result = {"message": f"已记住主播偏好：{value}"}
            else:
                raise BusinessError("UNSUPPORTED_ACTION", "不支持的操作")

            db.execute(
                "INSERT INTO action_ledger VALUES (?, ?, ?, ?, ?, ?)",
                (key, command_id, live_id, kind, self._json(result), now),
            )
            db.execute(
                "INSERT INTO audit_events (live_id, command_id, event_type, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (live_id, command_id, "action_committed", self._json({"action": action, "result": result, "key": key}), now),
            )
            db.commit()
            return {**result, "replayed": False}

    def simulate_stock_change(self, live_id: str, sku: str, stock: int) -> None:
        if stock < 0:
            raise BusinessError("INVALID_STOCK", "库存不能为负数")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE products SET stock=?, version=version+1 WHERE sku=?", (stock, sku))
            if changed.rowcount != 1:
                raise BusinessError("SKU_NOT_FOUND", "商品不存在")
            db.execute("UPDATE live_sessions SET version=version+1 WHERE live_id=?", (live_id,))
            db.execute(
                "INSERT INTO audit_events (live_id, command_id, event_type, payload, created_at) VALUES (?, NULL, ?, ?, ?)",
                (live_id, "stock_changed", self._json({"sku": sku, "stock": stock}), time.time()),
            )
            db.commit()

    @staticmethod
    def action_hash(action: dict[str, Any]) -> str:
        return hashlib.sha256(Store._json(action).encode()).hexdigest()

    def ledger_result(self, key: str) -> dict[str, Any] | None:
        with self.connection() as db:
            row = db.execute("SELECT result FROM action_ledger WHERE idempotency_key=?", (key,)).fetchone()
        return json.loads(row["result"]) if row else None

    @staticmethod
    def _validate_runtime_write(db, live_id: str, action: dict[str, Any]) -> None:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_states'").fetchone():
            row = None
        else:
            row = db.execute("SELECT state FROM runtime_states WHERE live_id=?", (live_id,)).fetchone()
        if row is None:
            if action.get("reference"):
                raise BusinessError("REFERENCE_STALE", "无法确认“第几款”的来源列表，请按商品名称重新发起")
            return
        runtime = json.loads(row[0])
        if runtime["scene"] != "on_live" or runtime["control_revision"] != action.get("expected_scene_revision", 0):
            raise BusinessError("SCENE_WRITE_DENIED", "直播阶段已变化，原审批不再有效，请在播中重新发起操作")
        reference = action.get("reference")
        if reference:
            # An approval is bound to the list the ordinal came from; a superseded list does not carry it over.
            lists = runtime["topics"].get(action.get("context_scene") or runtime["scene"], {}).get("lists", [])
            bound = next((l for l in lists if l["list_id"] == reference["list_id"]), None)
            if (bound is None or bound["status"] == "superseded" or reference["index"] >= len(bound["skus"])
                    or bound["skus"][reference["index"]] != action.get("sku")):
                raise BusinessError("REFERENCE_STALE", "“第几款”对应的列表已失效或被替换，原审批不再有效，请按商品名称重新发起")
            plan_ref = reference.get("plan_ref")
            if plan_ref and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='campaign_runs'").fetchone():
                run = db.execute("SELECT state FROM campaign_runs WHERE run_id=?", (plan_ref["run_id"],)).fetchone()
                if run is None or json.loads(run[0]).get("replan_count", 0) != plan_ref["replan_count"]:
                    raise BusinessError("REFERENCE_STALE", "引用的备播计划已重规划，原审批不再有效，请按商品名称重新发起")

    @staticmethod
    def _validate_remote_coupon(db, live_id: str, action: dict[str, Any]) -> None:
        Store._validate_runtime_write(db, live_id, action)
        live = db.execute("SELECT * FROM live_sessions WHERE live_id=?", (live_id,)).fetchone()
        product = db.execute("SELECT * FROM products WHERE sku=?", (action.get("sku"),)).fetchone()
        if live is None or live["status"] != "live":
            raise BusinessError("LIVE_INACTIVE", "直播间未开播")
        if live["version"] != action.get("expected_live_version"):
            raise BusinessError("STALE_STATE", "直播状态已变化，请重新发起指令")
        if product is None:
            raise BusinessError("SKU_NOT_FOUND", "商品不存在")
        if product["version"] != action.get("expected_product_version"):
            raise BusinessError("STALE_PRODUCT", "商品价格或库存已变化")
        if product["stock"] <= 0:
            raise BusinessError("OUT_OF_STOCK", "商品已售罄")
        amount, quantity = action["amount_cents"], action["quantity"]
        if action["type"] != "issue_coupon" or amount <= 0 or not 1 <= quantity <= 100:
            raise BusinessError("COUPON_LIMIT", "优惠券参数超出范围")
        if product["price_cents"] - amount < product["cost_cents"]:
            raise BusinessError("COUPON_BELOW_COST", "券后价低于成本价")

    def queue_remote_coupon(self, live_id, command_id, action, key, expires_at) -> str:
        operation_id = f"remote-{command_id}"
        payload_hash = self.action_hash(action)
        now = time.time()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM remote_operations WHERE command_id=?", (command_id,)).fetchone()
            if existing:
                if existing["payload_hash"] != payload_hash or existing["live_id"] != live_id:
                    raise BusinessError("COMMAND_ID_CONFLICT", "操作 ID 已绑定不同内容")
                return existing["operation_id"]
            if now >= expires_at:
                raise BusinessError("APPROVAL_EXPIRED", "审批已过期，请重新发起指令")
            self._validate_remote_coupon(db, live_id, action)
            db.execute(
                "INSERT INTO remote_operations (operation_id, command_id, live_id, action, payload_hash, "
                "idempotency_key, status, expires_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'ready', ?, ?, ?)",
                (operation_id, command_id, live_id, self._json(action), payload_hash, key, expires_at, now, now),
            )
            db.execute(
                "INSERT INTO audit_events (live_id, command_id, event_type, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (live_id, command_id, "remote_queued", self._json({"operation_id": operation_id, "payload_hash": payload_hash}), now),
            )
            db.commit()
        return operation_id

    def remote_operation(self, operation_id: str) -> dict[str, Any]:
        with self.connection() as db:
            row = db.execute("SELECT * FROM remote_operations WHERE operation_id=?", (operation_id,)).fetchone()
        if row is None:
            raise BusinessError("OPERATION_NOT_FOUND", "远端操作不存在")
        result = dict(row)
        for key in ("action", "receipt", "result"):
            result[key] = json.loads(result[key]) if result[key] else None
        return result

    def remote_operations(self, live_id: str) -> list[dict[str, Any]]:
        with self.connection() as db:
            ids = db.execute("SELECT operation_id FROM remote_operations WHERE live_id=? ORDER BY created_at DESC LIMIT 50", (live_id,)).fetchall()
        return [self.remote_operation(row["operation_id"]) for row in ids]

    def begin_remote_dispatch(self, operation_id: str) -> bool:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM remote_operations WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None or row["status"] != "ready":
                return False
            if time.time() >= row["expires_at"]:
                raise BusinessError("APPROVAL_EXPIRED", "审批已过期，未发送的操作已停止")
            self._validate_remote_coupon(db, row["live_id"], json.loads(row["action"]))
            db.execute("UPDATE remote_operations SET status='sent', attempts=attempts+1, updated_at=? WHERE operation_id=?", (time.time(), operation_id))
            db.commit()
        return True

    def set_remote_state(self, operation_id: str, status: str, code: str) -> None:
        if status not in {"unknown", "manual_review", "failed"}:
            raise ValueError("Invalid remote transition")
        with self.connection() as db:
            permitted = ("sent", "unknown") if status == "unknown" else ("ready", "sent", "unknown", "manual_review")
            db.execute(
                "UPDATE remote_operations SET status=?, last_error=?, updated_at=? "
                f"WHERE operation_id=? AND status IN ({','.join('?' for _ in permitted)})",
                (status, code, time.time(), operation_id, *permitted),
            )
            db.commit()

    def note_remote_query(self, operation_id: str, observed: str) -> int:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE remote_operations SET query_attempts=query_attempts+1, updated_at=? WHERE operation_id=?", (time.time(), operation_id))
            row = db.execute("SELECT * FROM remote_operations WHERE operation_id=?", (operation_id,)).fetchone()
            db.execute(
                "INSERT INTO audit_events (live_id, command_id, event_type, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (row["live_id"], row["command_id"], "remote_query", self._json({"operation_id": operation_id, "observed": observed}), time.time()),
            )
            db.commit()
        return row["query_attempts"]

    def complete_remote_coupon(self, operation_id: str, receipt: dict[str, Any]) -> dict[str, Any]:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM remote_operations WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None:
                raise BusinessError("OPERATION_NOT_FOUND", "远端操作不存在")
            if row["status"] == "succeeded":
                return json.loads(row["result"])
            if row["status"] == "failed":
                raise BusinessError("OPERATION_FAILED", "该操作已经被明确拒绝")
            action = json.loads(row["action"])
            if receipt.get("operation_id") != operation_id or receipt.get("payload_hash") != row["payload_hash"]:
                raise BusinessError("REMOTE_RECEIPT_MISMATCH", "回执与批准的操作不一致")
            previous = db.execute("SELECT result FROM action_ledger WHERE idempotency_key=?", (row["idempotency_key"],)).fetchone()
            if previous:
                result = json.loads(previous["result"])
            else:
                cursor = db.execute(
                    "INSERT INTO coupons (live_id, sku, amount_cents, quantity, created_at) VALUES (?, ?, ?, ?, ?)",
                    (row["live_id"], action["sku"], action["amount_cents"], action["quantity"], time.time()),
                )
                result = {
                    "message": f"已确认仿真平台创建 {action['quantity']} 张 {action['amount_cents']/100:.2f} 元券",
                    "coupon_id": cursor.lastrowid, "remote_coupon_id": receipt["coupon_id"],
                    "operation_id": operation_id, "remote_status": "succeeded", "replayed": False,
                    "idempotency_key": row["idempotency_key"],
                }
                db.execute("UPDATE live_sessions SET version=version+1 WHERE live_id=?", (row["live_id"],))
                db.execute("INSERT INTO action_ledger VALUES (?, ?, ?, ?, ?, ?)",
                           (row["idempotency_key"], row["command_id"], row["live_id"], "issue_coupon", self._json(result), time.time()))
                db.execute(
                    "INSERT INTO audit_events (live_id, command_id, event_type, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                    (row["live_id"], row["command_id"], "action_committed", self._json({"action": action, "result": result}), time.time()),
                )
            db.execute("UPDATE remote_operations SET status='succeeded', receipt=?, result=?, last_error=NULL, updated_at=? WHERE operation_id=?",
                       (self._json(receipt), self._json(result), time.time(), operation_id))
            db.commit()
        return result
