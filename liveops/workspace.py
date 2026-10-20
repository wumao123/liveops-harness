"""Scene-scoped context projections and immutable, addressable turn artifacts."""

from __future__ import annotations

import copy
import hashlib
import json
import time

from .context import mentions_sku
from .spec import diff_specs
from .store import BusinessError, Store


SCENES = {"pre_live", "on_live", "post_live"}
MAX_LISTS = 4
WRITE_TYPES = {"switch_product", "update_price", "issue_coupon", "set_preference"}
# Listing-specific phrasing only; a generic "不要" also negates writes ("不要给面霜改价") and must not exclude a product.
EXCLUDE_WORDS = ("先不上", "不上架", "别上", "先不讲", "不讲", "别推", "不推", "不进开场", "别让它进", "撤掉", "下掉")
RESTORE_WORDS = ("可以上了", "恢复上架", "重新上", "可以讲了", "可以推了")


def exclusion_change(text: str, mentioned: list[str], proposed: dict | None = None) -> dict:
    """Keywords are the offline path; a checked model proposal (already limited to named products) is merged in."""
    proposed = proposed or {}
    change = {"exclude_skus": list(proposed.get("exclude_skus", [])), "restore_skus": list(proposed.get("restore_skus", []))}
    keyword = _keyword_exclusion(text, mentioned)
    for key in change:
        change[key] = list(dict.fromkeys(change[key] + keyword.get(key, [])))
    change["exclude_skus"] = [s for s in change["exclude_skus"] if s not in change["restore_skus"]]
    return {k: v for k, v in change.items() if v}


def _keyword_exclusion(text: str, mentioned: list[str]) -> dict:
    if not mentioned:
        return {}
    if any(w in text for w in RESTORE_WORDS):
        return {"restore_skus": mentioned}
    if any(w in text for w in EXCLUDE_WORDS):
        return {"exclude_skus": mentioned}
    return {}


def list_id_for(command_id: str, skus: list[str]) -> str:
    return "list-" + hashlib.sha256(Store._json([command_id, skus]).encode()).hexdigest()[:16]


def _add_list(topic: dict, entry: dict) -> None:
    """Each list keeps its source; a scope change supersedes, a parallel request stays an alternative."""
    lists = topic.setdefault("lists", [])
    if any(item["list_id"] == entry["list_id"] for item in lists):
        return
    prior = next((l for l in reversed(lists) if l["deliverable"] == entry["deliverable"] and l["status"] != "superseded"), None)
    if entry.get("spec") is None or prior is not None and prior.get("spec") is None:
        diff = {"change": "unspecified", "changed_fields": [], "effect": "legacy_turn_without_spec_kept_as_alternative"}
    else:
        diff = diff_specs(prior["spec"] if prior else None, entry["spec"])
    for item in lists:
        if diff["change"] == "scope_or_budget" and item["deliverable"] == entry["deliverable"] and item["status"] != "superseded":
            item.update(status="superseded", superseded_by=entry["list_id"])
        elif item["status"] == "active":
            item["status"] = "alternative"
    lists.append({**entry, "status": "active", "change": diff["change"]})
    del lists[:-MAX_LISTS]


def initial_state() -> dict:
    return {"revision": 0, "control_revision": 0, "scene": "on_live", "audience": None,
            "topics": {scene: {"focus_sku": None, "focus_ambiguous": False, "listed_skus": [], "lists": [], "excluded_skus": []}
                       for scene in sorted(SCENES)}}


def reduce_state(state: dict, event: dict) -> dict:
    """Only runtime metadata is reduced here; commerce facts remain authoritative in Store."""
    result = copy.deepcopy(state)
    kind, payload = event["kind"], event["payload"]
    if kind == "scene_changed":
        if payload["scene"] not in SCENES:
            raise BusinessError("INVALID_SCENE", "未知直播阶段")
        if payload["scene"] != result["scene"]:
            result["control_revision"] += 1
        result["scene"] = payload["scene"]
    elif kind == "audience_observed":
        if type(payload["viewers"]) is not int or not 0 <= payload["viewers"] <= 10_000_000:
            raise BusinessError("INVALID_OBSERVATION", "在线人数必须为有效非负整数")
        result["audience"] = {"viewers": payload["viewers"], "observed_at": event["at"], "source": "simulator_observation"}
    elif kind == "turn_observed":
        if payload["status"] in {"ok", "clarification", "pending_approval"} and payload["action_type"] != "set_preference":
            topic = result["topics"][payload["scene"]]
            mentioned = payload.get("mentioned_skus", [])
            if mentioned:
                topic["focus_ambiguous"] = len(mentioned) > 1
                topic["focus_sku"] = mentioned[0] if len(mentioned) == 1 else None
            elif payload["status"] == "ok" and payload.get("sku"):
                topic["focus_sku"], topic["focus_ambiguous"] = payload["sku"], False
            if payload["status"] == "ok" and payload["action_type"] in {"plan", "prepare"}:
                topic["listed_skus"] = payload.get("listed_skus", [])[:5]
                if payload.get("list"):
                    _add_list(topic, payload["list"])
            if payload.get("exclude_skus") or payload.get("restore_skus"):
                excluded = [s for s in topic.get("excluded_skus", []) if s not in payload.get("restore_skus", [])]
                topic["excluded_skus"] = list(dict.fromkeys(excluded + payload.get("exclude_skus", [])))[:5]
            topic["last_artifact_id"] = payload["artifact_id"]
    else:
        raise BusinessError("RUNTIME_EVENT_DENIED", "未注册的运行时事件")
    result["revision"] += 1
    return result


class Workspace:
    def __init__(self, store: Store):
        self.store = store
        with store.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runtime_states (live_id TEXT PRIMARY KEY, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runtime_events (
                    live_id TEXT NOT NULL, event_id TEXT NOT NULL, request_hash TEXT NOT NULL,
                    event TEXT NOT NULL, state_after TEXT NOT NULL, PRIMARY KEY(live_id,event_id));
                CREATE TABLE IF NOT EXISTS runtime_artifacts (
                    live_id TEXT NOT NULL, artifact_id TEXT NOT NULL, body TEXT NOT NULL,
                    PRIMARY KEY(live_id,artifact_id));
                CREATE TABLE IF NOT EXISTS runtime_turns (
                    command_id TEXT PRIMARY KEY, live_id TEXT NOT NULL, scene TEXT NOT NULL,
                    kind TEXT NOT NULL, status TEXT NOT NULL, user_text TEXT NOT NULL,
                    answer_text TEXT NOT NULL, artifact_id TEXT NOT NULL, created_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS runtime_scene_turns ON runtime_turns(live_id,scene,created_at);
            """)
            lives = [row[0] for row in db.execute("SELECT live_id FROM live_sessions")]
        for live in lives:
            self.get(live)
            with store.connection() as db:
                imported = {row[0] for row in db.execute("SELECT command_id FROM runtime_turns WHERE live_id=?", (live,))}
            for command in reversed(store.commands(live, limit=50)):
                if command["command_id"] not in imported and command.get("response"):
                    self.record(command, command["response"])

    def get(self, live_id: str) -> dict:
        self.store.live(live_id)
        with self.store.connection() as db:
            db.execute("INSERT OR IGNORE INTO runtime_states VALUES (?,?)", (live_id, Store._json(initial_state())))
            state = db.execute("SELECT state FROM runtime_states WHERE live_id=?", (live_id,)).fetchone()[0]
            db.commit()
        return json.loads(state)

    @staticmethod
    def _apply(db, live_id: str, event_id: str, event: dict, expected_revision: int | None = None) -> dict:
        request_hash = hashlib.sha256(Store._json({k: event[k] for k in ("kind", "payload")}).encode()).hexdigest()
        prior = db.execute("SELECT request_hash,state_after FROM runtime_events WHERE live_id=? AND event_id=?", (live_id, event_id)).fetchone()
        if prior:
            if prior["request_hash"] != request_hash:
                raise BusinessError("EVENT_ID_CONFLICT", "同一事件ID不能绑定不同内容")
            return json.loads(prior["state_after"])
        state = json.loads(db.execute("SELECT state FROM runtime_states WHERE live_id=?", (live_id,)).fetchone()[0])
        if expected_revision is not None and expected_revision != state["revision"]:
            raise BusinessError("RUNTIME_VERSION_CONFLICT", "上下文状态已变化，请刷新后重试")
        result = reduce_state(state, event)
        db.execute("UPDATE runtime_states SET state=? WHERE live_id=?", (Store._json(result), live_id))
        db.execute("INSERT INTO runtime_events VALUES (?,?,?,?,?)", (live_id, event_id, request_hash, Store._json(event), Store._json(result)))
        return result

    def observe(self, live_id: str, event_id: str, kind: str, payload: dict, expected_revision: int) -> dict:
        if kind not in {"scene_changed", "audience_observed"}:
            raise BusinessError("RUNTIME_EVENT_DENIED", "外部只能提交阶段或仿真观测事件")
        self.get(live_id)
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            result = self._apply(db, live_id, event_id, {"kind": kind, "payload": payload, "at": time.time()}, expected_revision)
            db.commit()
        return result

    def record(self, command: dict, response: dict) -> str:
        live_id = command["live_id"]
        self.get(live_id)
        raw = Store._json(response)
        artifact = "turn-" + hashlib.sha256(raw.encode()).hexdigest()[:24]
        action, result = response.get("action") or {}, response.get("result") or {}
        mentioned = [p["sku"] for p in self.store.products() if mentions_sku(command["instruction"], p["sku"])
                     or p["title"] in command["instruction"] or p["title"][-2:] in command["instruction"]]
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            state = json.loads(db.execute("SELECT state FROM runtime_states WHERE live_id=?", (live_id,)).fetchone()[0])
            previous = db.execute("SELECT scene FROM runtime_turns WHERE command_id=?", (command["command_id"],)).fetchone()
            scene = previous["scene"] if previous else action.get("context_scene", state["scene"])
            db.execute("INSERT OR IGNORE INTO runtime_artifacts VALUES (?,?,?)", (live_id, artifact, raw))
            db.execute("INSERT INTO runtime_turns VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(command_id) DO UPDATE SET status=excluded.status,answer_text=excluded.answer_text,artifact_id=excluded.artifact_id",
                       (command["command_id"], live_id, scene, action.get("type", "unknown"), response["status"],
                        command["instruction"], response.get("message", ""), artifact, command["created_at"]))
            listed = result.get("plan") or result.get("selected_skus") or []
            entry = None
            if response["status"] == "ok" and action.get("type") in {"plan", "prepare"} and listed:
                entry = {"list_id": list_id_for(command["command_id"], listed[:5]), "source_command_id": command["command_id"],
                         "deliverable": "ranked_list" if action["type"] == "plan" else "reviewed_briefs",
                         "skus": listed[:5], "spec": result.get("request_spec"),
                         "plan_ref": {"run_id": result["run_id"], "revision": result.get("plan_revision", 1),
                                      "replan_count": result.get("replan_count", 0)} if result.get("run_id") else None}
            event = {"kind": "turn_observed", "at": time.time(), "payload": {
                "scene": scene, "status": response["status"], "action_type": action.get("type", "unknown"),
                "mentioned_skus": mentioned, "sku": action.get("sku"),
                "listed_skus": listed, "artifact_id": artifact, **({"list": entry} if entry else {}),
                **(exclusion_change(command["instruction"], mentioned, (response.get("interpretation") or {}).get("scene_constraint"))
                   if action.get("type") not in WRITE_TYPES else {})}}
            self._apply(db, live_id, f"{command['command_id']}:{artifact}", event)
            db.commit()
        return artifact

    def context(self, live_id: str, scene: str | None = None, *, char_budget: int = 4000) -> dict:
        self.get(live_id)
        if scene is not None and scene not in SCENES or not 1600 <= char_budget <= 8000:
            raise BusinessError("INVALID_CONTEXT", "场景或上下文字符预算无效")
        with self.store.connection() as db:
            db.execute("BEGIN")
            state = json.loads(db.execute("SELECT state FROM runtime_states WHERE live_id=?", (live_id,)).fetchone()[0])
            scene = scene or state["scene"]
            rows = db.execute("SELECT * FROM runtime_turns WHERE live_id=? AND scene=? AND kind!='set_preference' ORDER BY created_at DESC LIMIT 4", (live_id, scene)).fetchall()
            counts = db.execute("SELECT kind,COUNT(*) FROM runtime_turns WHERE live_id=? AND scene=? GROUP BY kind", (live_id, scene)).fetchall()
            topic = state["topics"][scene]
            has_runs = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='campaign_runs'").fetchone()
            for item in topic.get("lists", []):
                if item.get("plan_ref") and has_runs:
                    row = db.execute("SELECT state FROM campaign_runs WHERE run_id=?", (item["plan_ref"]["run_id"],)).fetchone()
                    current = json.loads(row[0]).get("replan_count", 0) if row else None
                    item["plan_replanned"] = current != item["plan_ref"]["replan_count"]
        summary = {"turns_by_kind": {row[0]: row[1] for row in counts}, "rule": "结构化统计与引用，不是生成的事实结论"}
        turns = [{"user": row["user_text"][:200], "assistant": row["answer_text"][:240], "status": row["status"],
                  "action_type": row["kind"], "artifact_id": row["artifact_id"]} for row in reversed(rows)]
        audience = state["audience"]
        if audience and time.time() - audience["observed_at"] > 60:
            audience = {"source": audience["source"], "stale": True, "rule": "在线人数观测已过期，不当实时事实使用"}
        pack = {"scene": scene, "runtime_revision": state["revision"], "control_revision": state["control_revision"],
                "audience": audience, "summary": summary,
                "conversation": {**topic, "source": "scene_projection_not_live_price_or_inventory", "turns": turns},
                "compression": {"mode": "structured_scene_summary_and_artifact_offload", "char_budget": char_budget,
                                "recent_turn_count": len(turns), "historical_turn_count": sum(row[1] for row in counts)}}
        pack["compression"]["injected_chars"] = 0
        while True:
            pack["compression"]["recent_turn_count"] = len(turns)
            for _ in range(3):
                pack["compression"]["injected_chars"] = len(Store._json(pack))
            if len(Store._json(pack)) <= char_budget:
                break
            if turns:
                turns.pop(0)
                continue
            # Specs are reproducible from the source turn artifact; superseded lists go before live ones.
            lists = pack["conversation"].get("lists", [])
            slim = next((l for l in lists if "spec" in l), None)
            if slim is not None:
                slim.pop("spec")
                continue
            dropped = next((l for l in lists if l["status"] == "superseded"), None)
            if dropped is None:
                break
            lists.remove(dropped)
        return pack

    def artifact(self, live_id: str, artifact_id: str) -> dict:
        self.store.live(live_id)
        with self.store.connection() as db:
            row = db.execute("SELECT body FROM runtime_artifacts WHERE live_id=? AND artifact_id=?", (live_id, artifact_id)).fetchone()
        if row is None:
            raise BusinessError("ARTIFACT_NOT_FOUND", "当前直播间没有该上下文材料")
        return json.loads(row[0])
