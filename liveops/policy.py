"""Deterministic risk gate for simulated live operations."""

from __future__ import annotations

from typing import Any


def decide(action: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, str]:
    kind = action.get("type", "invalid")
    if kind == "invalid":
        return {"level": "block", "code": "INVALID_ACTION", "reason": action.get("error", "无法理解指令")}
    if kind in {"status", "plan", "brief", "recap", "advise", "prepare", "clarify"}:
        return {"level": "auto", "reason": "只读操作"}
    if kind == "set_preference":
        return {"level": "auto", "reason": "主播明确要求记录偏好"}
    if snapshot.get("_runtime", {}).get("scene", "on_live") != "on_live":
        return {"level": "block", "code": "SCENE_WRITE_DENIED", "reason": "当前处于播前或播后，只能准备和复盘；切品、改价、发券需切回播中重新发起"}

    product = next((p for p in snapshot["products"] if p["sku"] == action.get("sku")), None)
    if product is None:
        return {"level": "block", "code": "SKU_NOT_FOUND", "reason": "没有找到目标商品"}
    if kind == "switch_product":
        if product["stock"] <= 0:
            return {"level": "block", "code": "OUT_OF_STOCK", "reason": "商品已售罄"}
        return {"level": "soft", "reason": "切品将立即改变直播间状态"}
    if kind == "update_price":
        amount = action.get("amount_cents", 0)
        if amount < product["cost_cents"]:
            return {"level": "block", "code": "PRICE_BELOW_COST", "reason": "价格低于成本价"}
        if amount <= 0:
            return {"level": "block", "code": "INVALID_PRICE", "reason": "价格必须大于 0"}
        return {"level": "hard", "reason": "改价需要主播确认"}
    if kind == "issue_coupon":
        amount = action.get("amount_cents", 0)
        quantity = action.get("quantity", 0)
        if quantity < 1 or quantity > 100:
            return {"level": "block", "code": "COUPON_LIMIT", "reason": "单次发券数量必须在 1 到 100 张之间"}
        if amount <= 0 or product["price_cents"] - amount < product["cost_cents"]:
            return {"level": "block", "code": "COUPON_BELOW_COST", "reason": "券后价不能低于成本价"}
        return {"level": "hard", "reason": "发券需要主播确认"}
    return {"level": "block", "code": "UNSUPPORTED_ACTION", "reason": "未授权的工具动作"}
