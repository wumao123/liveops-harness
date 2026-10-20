"""Crash-conservative provider admission accounting, shared by every agent role."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit

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


class CircuitBreaker:
    """Per provider host: after N consecutive transport failures, skip the provider for a cooldown.

    Only transport-level failures count (network/timeout, 429, 5xx); a 400 or bad JSON means the provider is up.
    Open state raises MODEL_CIRCUIT_OPEN before any budget is reserved, so callers fall back at once.
    Half-open lets exactly one probe through; its result closes or reopens the circuit.
    """

    def __init__(self, failures: int | None = None, cooldown_seconds: float | None = None):
        # `is None`, not `or`: an explicit 0 must be rejected, not silently replaced by the default.
        self.failures = int(os.getenv("LIVEOPS_BREAKER_FAILURES", "3")) if failures is None else failures
        self.cooldown = (float(os.getenv("LIVEOPS_BREAKER_COOLDOWN_SECONDS", "30"))
                         if cooldown_seconds is None else cooldown_seconds)
        if not 1 <= self.failures <= 20 or not 1 <= self.cooldown <= 600:
            raise ValueError("breaker limits out of range")
        self._lock = threading.Lock()
        self._hosts: dict[str, dict] = {}

    def _state(self, host: str) -> dict:
        return self._hosts.setdefault(host, {"failures": 0, "opened_at": None, "probing": False, "opened_count": 0})

    def before(self, host: str) -> None:
        with self._lock:
            state = self._state(host)
            if state["opened_at"] is None:
                return
            if time.monotonic() - state["opened_at"] < self.cooldown or state["probing"]:
                raise BusinessError("MODEL_CIRCUIT_OPEN", "模型服务连续失败，已暂时改用规则和模板；稍后自动重试")
            state["probing"] = True

    def record(self, host: str, ok: bool) -> None:
        with self._lock:
            state = self._state(host)
            state["probing"] = False
            if ok:
                state.update(failures=0, opened_at=None)
                return
            state["failures"] += 1
            if state["opened_at"] is not None or state["failures"] >= self.failures:
                state["opened_at"] = time.monotonic()
                state["opened_count"] += 1

    def release(self, host: str) -> None:
        """A call that ended for a non-provider reason (budget, bug) neither proves nor disproves health."""
        with self._lock:
            self._state(host)["probing"] = False

    def status(self) -> dict:
        with self._lock:
            now = time.monotonic()
            return {host: {"state": "closed" if s["opened_at"] is None
                           else "half_open" if now - s["opened_at"] >= self.cooldown else "open",
                           "consecutive_failures": s["failures"], "opened_count": s["opened_count"]}
                    for host, s in self._hosts.items()}

    def reset(self) -> None:
        with self._lock:
            self._hosts.clear()


breaker = CircuitBreaker()
_ORIGINAL_POST = httpx.post
_client: httpx.Client | None = None
_client_lock = threading.Lock()


def _send(url: str, **kwargs):
    """Reuse one keep-alive pool. Tests and recorded experiments replace httpx.post; that seam is honored."""
    global _client
    if httpx.post is not _ORIGINAL_POST:
        return httpx.post(url, **kwargs)
    with _client_lock:
        if _client is None:
            _client = httpx.Client(limits=httpx.Limits(max_connections=20, max_keepalive_connections=10))
    return _client.post(url, **kwargs)


def provider_post(url: str, *, phase: str, **kwargs):
    host = urlsplit(url).netloc or url
    breaker.before(host)
    scope = _scope.get()
    call_id = None
    if scope is not None:
        budget, live_id, scope_id, limits = scope
        try:
            call_id = budget.reserve(live_id, scope_id, limits, kwargs.get("json", {}), phase)
        except BusinessError:
            breaker.release(host)
            raise
    started = time.monotonic()
    try:
        response = _send(url, **kwargs)
    except httpx.RequestError:
        breaker.record(host, ok=False)
        if call_id:
            budget.finish(call_id, elapsed_ms=round((time.monotonic()-started)*1000, 2), failed=True)
        raise
    except Exception:
        breaker.release(host)
        if call_id:
            budget.finish(call_id, elapsed_ms=round((time.monotonic()-started)*1000, 2), failed=True)
        raise
    breaker.record(host, ok=not (response.status_code == 429 or response.status_code >= 500))
    if call_id:
        budget.finish(call_id, response, elapsed_ms=round((time.monotonic()-started)*1000, 2), failed=response.status_code >= 400)
    return response
