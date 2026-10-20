"""Crash-conservative provider admission accounting, shared by every agent role."""

from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

import httpx

from .store import BusinessError, Store


_scope: ContextVar[tuple | None] = ContextVar("provider_budget_scope", default=None)
DEFAULT_LIMITS = {"max_calls": 6, "max_reserved_tokens": 100_000}
MAX_CAMPAIGN_LIMITS = {"max_calls": 20, "max_reserved_tokens": 300_000}
LIVE_LIMITS = {"max_calls": 200, "max_reserved_tokens": 2_000_000}


class ModelBudget:
    def __init__(self, store: Store):
        self.store = store
        with store.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS provider_scopes (
                    live_id TEXT NOT NULL, scope_id TEXT NOT NULL, limits_json TEXT NOT NULL,
                    PRIMARY KEY(live_id,scope_id));
                CREATE TABLE IF NOT EXISTS provider_calls (
                    call_id TEXT PRIMARY KEY, live_id TEXT NOT NULL, scope_id TEXT NOT NULL,
                    phase TEXT NOT NULL, model TEXT NOT NULL, reserved_tokens INTEGER NOT NULL,
                    charged_tokens INTEGER NOT NULL, reported_tokens INTEGER,
                    status TEXT NOT NULL, started_at REAL NOT NULL, elapsed_ms REAL);
                CREATE INDEX IF NOT EXISTS provider_live_scope ON provider_calls(live_id,scope_id);
            """)

    def reserve(self, live_id: str, scope_id: str, limits: dict, payload: dict, phase: str) -> str:
        self.store.live(live_id)
        # UTF-8 bytes deliberately overestimate common text tokenization. This is not a price invoice.
        estimate = len(Store._json(payload.get("messages", [])).encode("utf-8")) + int(payload.get("max_tokens", 0)) + 256
        if estimate <= 0:
            raise BusinessError("MODEL_BUDGET_INVALID", "请求预算无效")
        call_id = str(uuid.uuid4())
        encoded = Store._json(limits)
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT limits_json FROM provider_scopes WHERE live_id=? AND scope_id=?", (live_id, scope_id)).fetchone()
            if row and row[0] != encoded:
                raise BusinessError("MODEL_BUDGET_CONFLICT", "同一任务的模型预算不能在恢复时重置")
            db.execute("INSERT OR IGNORE INTO provider_scopes VALUES (?,?,?)", (live_id, scope_id, encoded))
            for clause, args, ceiling in (("live_id=? AND scope_id=?", (live_id, scope_id), limits),
                                           ("live_id=?", (live_id,), LIVE_LIMITS)):
                used = db.execute(f"SELECT COUNT(*),COALESCE(SUM(charged_tokens),0) FROM provider_calls WHERE {clause}", args).fetchone()
                if used[0] >= ceiling["max_calls"] or used[1] + estimate > ceiling["max_reserved_tokens"]:
                    raise BusinessError("MODEL_BUDGET_EXHAUSTED", "模型请求或预估token预算已用尽，已停止调用；可使用确定性结果或另建任务")
            db.execute("INSERT INTO provider_calls VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (call_id, live_id, scope_id, phase, str(payload.get("model", "unknown"))[:120], estimate, estimate, None,
                        "reserved_usage_unknown", time.time(), None))
            db.commit()
        return call_id

    def finish(self, call_id: str, response=None, *, elapsed_ms: float, failed: bool = False):
        reported = None
        if response is not None:
            try:
                usage = response.json().get("usage") or {}
                total, prompt, completion = usage.get("total_tokens"), usage.get("prompt_tokens"), usage.get("completion_tokens")
                valid = lambda value: type(value) is int and value >= 0
                if valid(total) and (not (valid(prompt) and valid(completion)) or total == prompt + completion):
                    reported = total
                elif total is None and valid(prompt) and valid(completion):
                    reported = prompt + completion
            except (ValueError, AttributeError, TypeError):
                pass
        status = "reported" if reported is not None else "failed_usage_unknown" if failed else "response_usage_unknown"
        with self.store.connection() as db:
            db.execute("UPDATE provider_calls SET reported_tokens=?,charged_tokens=COALESCE(?,reserved_tokens),status=?,elapsed_ms=? WHERE call_id=?",
                       (reported, reported, status, elapsed_ms, call_id))
            db.commit()

    def summary(self, live_id: str, scope_id: str | None = None) -> dict:
        self.store.live(live_id)
        with self.store.connection() as db:
            rows = db.execute("SELECT * FROM provider_calls WHERE live_id=?" + (" AND scope_id=?" if scope_id else ""),
                              (live_id, scope_id) if scope_id else (live_id,)).fetchall()
            scope = db.execute("SELECT limits_json FROM provider_scopes WHERE live_id=? AND scope_id=?", (live_id, scope_id)).fetchone() if scope_id else None
        return {"scope_id": scope_id, "calls": len(rows), "reported_tokens": sum(r["reported_tokens"] or 0 for r in rows),
                "charged_tokens": sum(r["charged_tokens"] for r in rows),
                "unknown_usage_calls": sum(r["reported_tokens"] is None for r in rows),
                "limits": json.loads(scope[0]) if scope else LIVE_LIMITS if scope_id is None else None,
                "accounting": "durable_admission_estimate_not_currency_invoice", "reset_policy": "persistent_no_automatic_refill"}


@contextmanager
def model_scope(budget: ModelBudget, live_id: str, scope_id: str, limits: dict | None = None):
    token = _scope.set((budget, live_id, scope_id, limits or DEFAULT_LIMITS))
    try:
        yield
    finally:
        _scope.reset(token)


def provider_post(url: str, *, phase: str, **kwargs):
    scope = _scope.get()
    if scope is None:
        return httpx.post(url, **kwargs)
    budget, live_id, scope_id, limits = scope
    call_id = budget.reserve(live_id, scope_id, limits, kwargs.get("json", {}), phase)
    started = time.monotonic()
    try:
        response = httpx.post(url, **kwargs)
    except Exception:
        budget.finish(call_id, elapsed_ms=round((time.monotonic()-started)*1000, 2), failed=True)
        raise
    budget.finish(call_id, response, elapsed_ms=round((time.monotonic()-started)*1000, 2), failed=response.status_code >= 400)
    return response
