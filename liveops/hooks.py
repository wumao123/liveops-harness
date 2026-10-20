"""Small lifecycle hook bus. Optional hooks may add notes or deny; they can never grant or skip required checks."""

from __future__ import annotations

import copy
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Literal

from .store import BusinessError, Store
from .tooling import recovery_for


Phase = Literal["pre_tool_call", "post_tool_call", "on_tool_error"]
PHASES = ("pre_tool_call", "post_tool_call", "on_tool_error")


class HookDenied(BusinessError):
    def __init__(self, code: str, message: str, checks: list[dict]):
        super().__init__(code, message)
        self.checks = checks


@contextmanager
def required_check(checks: list[dict], name: str, phase: str) -> Iterator[None]:
    """Record a mandatory check that actually ran; failures still propagate unchanged."""
    started = time.monotonic()
    record = {"name": name, "phase": phase, "required": True}
    try:
        yield
    except BusinessError as exc:
        checks.append({**record, "outcome": "denied", "code": exc.code, "elapsed_ms": _ms(started)})
        raise
    except Exception as exc:
        checks.append({**record, "outcome": "error", "code": type(exc).__name__, "elapsed_ms": _ms(started)})
        raise
    checks.append({**record, "outcome": "passed", "elapsed_ms": _ms(started)})


def _ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000, 3)


@dataclass(frozen=True)
class Hook:
    name: str
    phase: Phase
    handler: Callable[[dict], dict | None]
    applies: Callable[[dict], bool]


class HookBus:
    def __init__(self):
        self._hooks: dict[str, list[Hook]] = {phase: [] for phase in PHASES}

    def register(self, phase: Phase, name: str, handler: Callable[[dict], dict | None], *,
                 applies: Callable[[dict], bool] = lambda _event: True) -> None:
        if phase not in PHASES:
            raise ValueError("unknown hook phase")
        if any(h.name == name for hooks in self._hooks.values() for h in hooks):
            raise ValueError("hook name already registered")
        self._hooks[phase].append(Hook(name, phase, handler, applies))

    def names(self) -> dict[str, list[str]]:
        return {phase: [h.name for h in hooks] for phase, hooks in self._hooks.items()}

    def run(self, phase: Phase, event: dict) -> list[dict]:
        """Run applicable hooks on a copy of the event; returns records of hooks that actually ran.

        pre_tool_call: a BusinessError denies; any other exception fails closed for writes.
        post_tool_call: reads may reject output; after a committed write hooks only observe.
        on_tool_error: observe only, never replaces the original error.
        """
        records: list[dict] = []
        write = event.get("access") == "write"
        for hook in self._hooks[phase]:
            view = copy.deepcopy(event)
            try:
                if not hook.applies(view):
                    continue
            except Exception:
                continue
            started = time.monotonic()
            record = {"name": hook.name, "phase": phase, "required": False}
            try:
                note = hook.handler(view)
            except BusinessError as exc:
                if phase == "pre_tool_call" or (phase == "post_tool_call" and not write):
                    records.append({**record, "outcome": "denied", "code": exc.code, "elapsed_ms": _ms(started)})
                    raise HookDenied(exc.code, exc.message, records) from exc
                records.append({**record, "outcome": "denied_after_commit" if phase == "post_tool_call" else "error",
                                "code": exc.code, "elapsed_ms": _ms(started)})
            except Exception as exc:
                records.append({**record, "outcome": "error", "code": type(exc).__name__, "elapsed_ms": _ms(started)})
                if phase == "pre_tool_call" and write:
                    raise HookDenied("HOOK_FAILED", "写操作前置检查异常，已按失败关闭处理，未执行", records) from exc
            else:
                records.append({**record, "outcome": "noted" if note else "passed", **({"note": note} if note else {}),
                                "elapsed_ms": _ms(started)})
        return records


def default_hooks(store: Store) -> HookBus:
    bus = HookBus()

    def pending_write_notice(event: dict) -> dict | None:
        action = event["input"]
        others = [c["command_id"] for c in store.commands(event["live_id"], limit=30)
                  if c["status"] == "pending_approval" and c["command_id"] != event["subject_id"]
                  and (c.get("proposal") or {}).get("type") == action["type"]
                  and (c.get("proposal") or {}).get("sku") == action.get("sku")]
        return {"other_pending_same_sku": others[:5]} if others else None

    def usage_completeness(event: dict) -> dict | None:
        usage = event["usage"]
        if usage.get("usage_reported_calls", 0) < usage.get("model_calls", 0):
            return {"usage_complete": False, "unknown_usage_calls": usage["model_calls"] - usage.get("usage_reported_calls", 0)}
        return None

    def recovery_plan(event: dict) -> dict:
        error = event["error"]
        return recovery_for(error["code"], error.get("category"), event.get("access", "read"))

    bus.register("pre_tool_call", "pending_write_notice", pending_write_notice,
                 applies=lambda e: e["surface"] == "command" and e["access"] == "write" and e["input"].get("sku") is not None)
    bus.register("post_tool_call", "usage_completeness", usage_completeness,
                 applies=lambda e: (e.get("usage") or {}).get("backend") == "model")
    bus.register("on_tool_error", "recovery_plan", recovery_plan)
    return bus
