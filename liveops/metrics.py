"""Summarize committed campaign traces without treating unknown usage as free."""

from __future__ import annotations

from typing import Any


def campaign_metrics(state: dict[str, Any]) -> dict[str, Any]:
    trace = state.get("trace", [])
    model_trace = [item for item in trace if item.get("backend") == "model"]
    calls = sum(item.get("model_calls", 0) for item in model_trace)
    reported = sum(item.get("usage_reported_calls", 0) for item in model_trace)
    breakdown = sum(item.get("token_breakdown_calls", 0) for item in model_trace)
    inconsistent = sum(item.get("usage_inconsistent_calls", 0) for item in model_trace)
    unknown = max(0, calls - reported)
    legacy = any("usage_reported_calls" not in item for item in model_trace)
    status = ("no_model_calls_recorded" if not calls else "partial_or_unknown" if unknown or legacy or inconsistent else "reported_totals_available")
    return {
        "run_id": state["run_id"], "status": state["status"],
        "scope": "committed_trace_only_not_provider_invoice_or_crash_safe_usage_ledger",
        "task_attempts": state.get("steps", 0),
        "completed_tasks": sum(item["status"] == "succeeded" for item in state["tasks"].values()),
        "failed_tasks": sum(item["status"] == "failed" for item in state["tasks"].values()),
        "model_calls_recorded": calls,
        "extra_calls_within_task_attempts": sum(max(0, item.get("model_calls", 0) - 1) for item in model_trace),
        "failed_task_model_calls": sum(item.get("model_calls", 0) for item in model_trace if item["status"] == "failed"),
        "usage_status": status, "calls_with_reported_totals": reported,
        "calls_without_reported_totals": unknown,
        "reported_total_tokens": sum(item.get("reported_total_tokens", 0) for item in model_trace),
        "reported_prompt_tokens": sum(item.get("reported_prompt_tokens", 0) for item in model_trace),
        "reported_completion_tokens": sum(item.get("reported_completion_tokens", 0) for item in model_trace),
        "calls_with_token_breakdown": breakdown,
        "calls_with_inconsistent_usage": inconsistent,
        "token_breakdown_complete": bool(calls) and breakdown == calls and not legacy,
        "actual_currency_cost": None,
        "cost_note": "No price/cached-token/tool-billing adapter; missing usage is not zero cost.",
    }
