"""Build a bounded, source-aware context for intent parsing."""

from __future__ import annotations

import re
from typing import Any


MAX_PRODUCTS = 8


def recent_context(commands: list[dict[str, Any]], products: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Keep conversational references, never reusable prices or execution authority."""
    turns = []
    focus = None
    listed = []
    focus_resolved = False
    ambiguous = False
    for command in commands:
        response = command.get("response") or {}
        if not response or command.get("status") not in {"ok", "clarification", "pending_approval"}:
            continue
        action = response.get("action") or command.get("proposal") or {}
        if action.get("type") == "set_preference":
            continue
        result = response.get("result") or {}
        mentioned = [p["sku"] for p in products or [] if mentions_sku(command["instruction"], p["sku"])
                     or p["title"] in command["instruction"] or p["title"][-2:] in command["instruction"]]
        if not focus_resolved and mentioned:
            focus_resolved = True
            ambiguous = len(mentioned) > 1
            focus = mentioned[0] if not ambiguous else None
        if command.get("status") == "ok":
            if not focus_resolved and action.get("sku"):
                focus = action["sku"]
                focus_resolved = True
            if not listed and action.get("type") in {"plan", "prepare"}:
                listed = result.get("plan") or result.get("selected_skus") or []
        turns.append({"user": command["instruction"][:200], "assistant": response.get("message", "")[:400],
                      "status": command["status"], "action_type": action.get("type")})
        if len(turns) >= 4:
            break
    return {"source": "recent_command_history_not_live_facts", "turns": list(reversed(turns)),
            "focus_sku": focus, "focus_ambiguous": ambiguous, "listed_skus": listed[:5]}


def mentions_sku(text: str, sku: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9-]){re.escape(sku)}(?![A-Za-z0-9-])", text, re.IGNORECASE) is not None


# "第二款" is an item; "第一个方案" / "第一份" names a list, not an item.
ORDINAL_RE = re.compile(r"第([一二三四五12345])(?:款|件|个(?!方案|列表))")
_LIST_ORDINAL_RE = re.compile(r"第([一二三四五12345])(?:份|个方案|个列表|版)")
_NEWEST = ("新方案", "新列表", "最新", "这一份", "刚才那份", "刚出的")
_PREVIOUS = ("旧方案", "原方案", "上一份", "之前那份", "原来那份", "之前的方案", "老方案")
_KIND_WORDS = {"ranked_list": ("推荐列表", "推荐里", "推荐的", "选品列表"),
               "reviewed_briefs": ("备播计划", "备播列表", "备播里", "备播的")}


def _cn_index(token: str) -> int:
    return "一二三四五".find(token) if token in "一二三四五" else int(token) - 1


def has_reference(text: str) -> bool:
    return ORDINAL_RE.search(text) is not None


def resolve_ordinal(text: str, conversation: dict[str, Any]) -> dict[str, Any]:
    """Bind "第N款" to one concrete list (ID, source turn, plan revision) or ask; never guess between lists."""
    match = ORDINAL_RE.search(text)
    if not match:
        return {"status": "none"}
    index = _cn_index(match[1])
    lists = conversation.get("lists")
    if lists is None:
        # Pre-phase-2 snapshots only carry the flat list; keep their behavior, without a binding.
        listed = conversation.get("listed_skus", [])
        if index >= len(listed):
            return {"status": "clarify", "question": "你指的是哪个列表里的商品？请给我商品名称或 SKU。"}
        return {"status": "resolved", "sku": listed[index], "reference": None}
    target, explicit = None, False
    kind = next((k for k, words in _KIND_WORDS.items() if any(w in text for w in words)), None)
    scoped = [l for l in lists if kind is None or l["deliverable"] == kind]
    list_ordinal = _LIST_ORDINAL_RE.search(text)
    if list_ordinal:
        position = _cn_index(list_ordinal[1])
        target, explicit = (scoped[position] if position < len(scoped) else None), True
    elif any(w in text for w in _PREVIOUS):
        target, explicit = (scoped[-2] if len(scoped) >= 2 else None), True
    elif any(w in text for w in _NEWEST):
        target, explicit = (scoped[-1] if scoped else None), True
    if explicit and target is None:
        return {"status": "clarify", "question": "没有找到你说的那一份列表，请直接告诉我商品名称或 SKU。"}
    if not explicit:
        valid = [l for l in scoped if l["status"] != "superseded"]
        if not valid:
            reason = "之前的列表已因需求变化失效" if scoped else "当前阶段还没有推荐或备播列表"
            return {"status": "clarify", "question": f"{reason}，“第几款”无法对应，请直接说商品名称或 SKU。"}
        at_index = {l["skus"][index] if index < len(l["skus"]) else None for l in valid}
        if len(valid) > 1 and len(at_index) > 1:
            names = "、".join(f"{'推荐列表' if l['deliverable'] == 'ranked_list' else '备播计划'}（{'/'.join(l['skus'])}）" for l in valid)
            return {"status": "clarify", "question": f"现在有 {len(valid)} 份列表同时有效：{names}。请说明是哪一份的第几款，例如“新方案的第二款”，或直接说商品名。"}
        target = valid[-1]
    if target["status"] == "superseded":
        return {"status": "clarify", "question": "你说的那份列表已被新需求替换，原来的序号不再有效。请按当前列表或商品名称指定。"}
    plan_ref = target.get("plan_ref")
    if target.get("plan_replanned"):
        return {"status": "clarify", "question": "这份备播计划已经重规划，原来的序号可能对应了别的商品。请按商品名称或 SKU 指定。"}
    if index >= len(target["skus"]):
        return {"status": "clarify", "question": f"那份列表只有 {len(target['skus'])} 款商品，请确认是第几款或直接说商品名。"}
    return {"status": "resolved", "sku": target["skus"][index], "reference": {
        "kind": "ordinal", "index": index, "list_id": target["list_id"], "sku": target["skus"][index],
        "source_command_id": target["source_command_id"], "plan_ref": plan_ref}}


def interpretation_context(instruction: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    live = snapshot["live"]
    products = snapshot["products"]
    query = instruction.lower()
    matched = [
        product for product in products
        if mentions_sku(instruction, product["sku"])
        or product["title"].lower() in query
        or product["title"][-2:].lower() in query
    ]
    current = [product for product in products if product["sku"] == live["current_sku"]]
    previous = [product for product in products if product["sku"] == live.get("previous_sku")]
    ordered = list(dict.fromkeys(p["sku"] for p in matched + current + previous + products))
    lookup = {product["sku"]: product for product in products}
    selected = [lookup[sku] for sku in ordered[:MAX_PRODUCTS]]
    return {
        "source": "live_business_db",
        "live_id": live["live_id"],
        "live_version": live["version"],
        "current_sku": live["current_sku"],
        "previous_sku": live.get("previous_sku"),
        "goal": live["goal"][:120],
        "preference": snapshot["preferences"].get("style", "")[:120],
        "conversation": snapshot.get("_conversation", {}),
        "runtime_hint": {k: v for k, v in snapshot.get("_runtime", {}).items() if k != "conversation"},
        "products": [
            {
                "sku": p["sku"],
                "title": p["title"][:80],
                "price_cents": p["price_cents"],
                "stock": p["stock"],
                "version": p["version"],
                "features": p.get("features", "")[:200],
            }
            for p in selected
        ],
    }
