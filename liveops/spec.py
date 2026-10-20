"""Structured request specs for read-only planning; code validates them before any template runs."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .contracts import validate
from .store import BusinessError


Deliverable = Literal["ranked_list", "reviewed_briefs"]
WriteOp = Literal["switch_product", "update_price", "issue_coupon"]

# Deterministic keyword hints; not a style model. Ranking ignores style, briefs record it.
STYLE_WORDS = ("活泼", "简短", "专业", "亲切", "幽默", "正式", "口语", "温柔", "热情")
_WRITE_WORDS = {"switch_product": ("切品", "切到", "上架"), "update_price": ("改价", "降价", "调价"),
                "issue_coupon": ("发券", "优惠券")}


class RequestScope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    top_k: int = Field(ge=1, le=5)
    max_price_cents: int | None = Field(default=None, gt=0)
    skus: list[str] | None = Field(default=None, min_length=1, max_length=5)
    # Scene constraints the operator stated earlier ("防晒乳先不上"); listed so the reply can say what was applied.
    exclude_skus: list[str] | None = Field(default=None, min_length=1, max_length=5)


class RequestSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    version: Literal["1"] = "1"
    deliverable: Deliverable
    scope: RequestScope
    style: list[str] = Field(default_factory=list, max_length=len(STYLE_WORDS))
    # Writes mentioned inside a planning request are listed, never performed by the plan.
    excluded_operations: list[WriteOp] = Field(default_factory=list, max_length=3)
    permission: Literal["read_only"] = "read_only"


TEMPLATES: dict[str, str] = {"ranked_list": "rank_by_stock_and_margin_v1", "reviewed_briefs": "bounded_campaign_dag_v1"}


def build_request_spec(action: dict[str, Any], instruction: str = "") -> RequestSpec:
    if action.get("type") not in {"plan", "prepare"}:
        raise BusinessError("SPEC_NOT_APPLICABLE", "只有选品和备播请求生成任务规格")
    scope = {"top_k": action.get("top_k", 3 if action["type"] == "prepare" else 5)}
    for field in ("max_price_cents", "skus", "exclude_skus"):
        if action.get(field) is not None:
            scope[field] = action[field]
    body = {"deliverable": "ranked_list" if action["type"] == "plan" else "reviewed_briefs", "scope": scope,
            "style": [w for w in STYLE_WORDS if w in instruction],
            "excluded_operations": [op for op, words in _WRITE_WORDS.items() if any(w in instruction for w in words)]}
    return validate(RequestSpec, body, "REQUEST_SPEC")


def select_template(spec: RequestSpec) -> str:
    template = TEMPLATES.get(spec.deliverable)
    if template is None:
        # Reaching here means a new deliverable was added without a template; do not improvise a plan.
        raise BusinessError("NO_TEMPLATE", "没有可用的固定模板处理该请求")
    return template


def diff_specs(old: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
    """Classify a requirement change. Pure, so the runtime reducer and the agent share one rule."""
    rule = "amount_sku_quantity_of_writes_are_never_carried_over_or_auto_modified"
    if old is None:
        return {"change": "new", "changed_fields": [], "effect": "first_list_in_scene", "rule": rule}
    if old.get("deliverable") != new.get("deliverable"):
        return {"change": "different_deliverable", "changed_fields": ["deliverable"],
                "effect": "both_lists_stay_valid_ordinals_need_a_selector_when_they_differ", "rule": rule}
    old_scope, new_scope = old.get("scope", {}), new.get("scope", {})
    scope_fields = sorted(k for k in {"top_k", "max_price_cents", "skus", "exclude_skus"} if old_scope.get(k) != new_scope.get(k))
    if scope_fields:
        return {"change": "scope_or_budget", "changed_fields": scope_fields,
                "effect": "old_list_superseded_reselect_products", "rule": rule}
    if sorted(old.get("style", [])) != sorted(new.get("style", [])):
        effect = "keep_selection_rewrite_briefs" if new.get("deliverable") == "reviewed_briefs" else "ranking_unchanged_style_not_used"
        return {"change": "style_only", "changed_fields": ["style"], "effect": effect, "rule": rule}
    return {"change": "none", "changed_fields": [], "effect": "same_request_kept_as_alternative", "rule": rule}


def change_message(diff: dict[str, Any]) -> str:
    return {
        "scope_or_budget": "需求范围或预算变了：上一份同类列表已失效，之前的“第几款”不再指向它。",
        "style_only": "只改了风格：沿用上一份的商品，只重写手卡。" if diff["effect"] == "keep_selection_rewrite_briefs"
                      else "只改了风格：排序规则不受风格影响，列表与上一份一致。",
        "different_deliverable": "推荐列表和备播列表同时有效；说“第几款”时如两份不同，我会先问是哪一份。",
    }.get(diff["change"], "")
