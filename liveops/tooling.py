"""Small, serializable error contract shared by tool and model paths."""

from __future__ import annotations

from typing import Literal, TypedDict


ErrorCategory = Literal["invalid_input", "business_rule", "state_conflict", "transient", "provider_error", "uncertain"]
NextStep = Literal["stop", "correct_input", "refresh_and_resubmit", "retry_later", "query_status", "manual_review"]


class ToolError(TypedDict):
    code: str
    category: ErrorCategory
    retryable: bool
    next_step: NextStep
    message: str


def error(code: str, category: ErrorCategory, message: str) -> ToolError:
    next_steps = {
        "invalid_input": "correct_input",
        "business_rule": "stop",
        "state_conflict": "refresh_and_resubmit",
        "transient": "retry_later",
        "provider_error": "stop",
        "uncertain": "query_status",
    }
    return {
        "code": code,
        "category": category,
        "retryable": category == "transient",
        "next_step": next_steps[category],
        "message": message,
    }


def business_error(code: str, message: str) -> ToolError:
    if code in {"STALE_STATE", "STALE_PRODUCT", "SCENE_WRITE_DENIED", "RUNTIME_VERSION_CONFLICT", "PLAN_REVISION_CONFLICT", "REFERENCE_STALE"}:
        return error(code, "state_conflict", message)
    if code in {"COMMAND_ID_CONFLICT", "SKU_NOT_FOUND", "UNSUPPORTED_ACTION"}:
        return error(code, "invalid_input", message)
    return error(code, "business_rule", message)


RecoveryStrategy = Literal["repair_structure", "retry_with_backoff", "refresh_or_replan",
                           "reconcile", "manual_review", "correct_input", "stop"]

# One decision table; each path keeps its own loop but asks this table whether to loop.
_CODE_STRATEGY: dict[str, RecoveryStrategy] = {
    # Structure-only repair: the guard re-checks SKU/amount/quantity against the user's words afterwards.
    "MODEL_INVALID_JSON": "repair_structure",
    "ValidationError": "repair_structure", "JSONDecodeError": "repair_structure",
    "unknown_reference": "repair_structure", "uncited_recommendation": "repair_structure",
    "citation_mismatch": "repair_structure", "plan_scope_mismatch": "repair_structure",
    # Content checks are not retried until they pass.
    "unsafe_claim": "stop", "unsupported_price": "stop", "unknown_sku_in_text": "stop",
    "SPECIALIST_SCHEMA": "stop", "MODEL_BUDGET_EXHAUSTED": "stop", "STEP_LIMIT": "stop", "REPLAN_LIMIT": "stop",
    "CAPABILITY_DENIED": "stop", "CONTEXT_SCOPE": "stop", "CONTEXT_BUDGET": "stop",
    "OUTPUT_CONTRACT": "stop", "SKILL_CONTRACT": "stop", "ACTION_CONTRACT": "stop", "HOOK_FAILED": "stop",
    "WORKER_FAILED": "retry_with_backoff", "SPECIALIST_NETWORK": "retry_with_backoff",
    "SPECIALIST_TRANSIENT": "retry_with_backoff", "MODEL_NETWORK": "retry_with_backoff",
    "STALE_CAMPAIGN": "refresh_or_replan", "APPROVAL_EXPIRED": "refresh_or_replan",
    "REMOTE_TIMEOUT": "reconcile", "REMOTE_NOT_VISIBLE": "reconcile", "REMOTE_PENDING": "reconcile",
    "REMOTE_UNCERTAIN": "manual_review", "REMOTE_QUERY_UNAVAILABLE": "manual_review",
    "REMOTE_RECEIPT_MISMATCH": "manual_review",
}
_CATEGORY_STRATEGY: dict[str, RecoveryStrategy] = {
    "invalid_input": "correct_input", "business_rule": "stop", "state_conflict": "refresh_or_replan",
    "transient": "retry_with_backoff", "provider_error": "stop", "uncertain": "reconcile",
}


def recovery_for(code: str, category: ErrorCategory | None = None, access: Literal["read", "write"] = "read") -> dict:
    if code in _CODE_STRATEGY:
        strategy = _CODE_STRATEGY[code]
    elif code.startswith("MODEL_HTTP_"):
        status = int(code.rsplit("_", 1)[-1]) if code.rsplit("_", 1)[-1].isdigit() else 0
        strategy = "retry_with_backoff" if status == 429 or status >= 500 else "stop"
    else:
        strategy = _CATEGORY_STRATEGY[category or business_error(code, "")["category"]]
    if access == "write" and strategy == "retry_with_backoff":
        # A write that may have reached the platform is never resent by a generic retry.
        strategy = "reconcile"
    return {
        "code": code, "strategy": strategy,
        "auto_retry": strategy in {"repair_structure", "retry_with_backoff"},
        "field_policy": "structure_only_never_change_sku_amount_quantity" if strategy == "repair_structure" else "unchanged",
        "budget": "shared_persistent_provider_ledger" if strategy in {"repair_structure", "retry_with_backoff"} else "none",
    }
