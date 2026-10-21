"""Bounded, resumable specialist workflows; no worker can mutate commerce state."""

from __future__ import annotations

import copy
import hashlib
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts import SKILL_CONTRACTS, BriefDraft, validate
from .hooks import HookDenied, default_hooks, required_check
from .store import BusinessError, Store
from .model_budget import ModelBudget, MAX_CAMPAIGN_LIMITS, model_scope
from .tooling import recovery_for


Role = Literal["merchandiser", "copywriter", "compliance", "director"]


class TaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    task_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    role: Role
    dependencies: list[str] = Field(default_factory=list, max_length=12)
    sku: str | None = None


class PlanSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tasks: list[TaskSpec] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def validate_dag(self):
        lookup = {t.task_id: t for t in self.tasks}
        if len(lookup) != len(self.tasks):
            raise ValueError("duplicate task ID")
        completed: set[str] = set()
        for task in self.tasks:
            if len(set(task.dependencies)) != len(task.dependencies):
                raise ValueError("duplicate dependency")
            if not set(task.dependencies) <= lookup.keys():
                raise ValueError("unknown dependency")
            if task.role in {"copywriter", "compliance"} and not task.sku:
                raise ValueError("product role requires SKU")
        while len(completed) < len(lookup):
            ready = {t.task_id for t in self.tasks if set(t.dependencies) <= completed} - completed
            if not ready:
                raise ValueError("cyclic plan")
            completed |= ready
        if sum(t.role == "merchandiser" for t in self.tasks) != 1 or sum(t.role == "director" for t in self.tasks) != 1:
            raise ValueError("one selection and one summary required")
        for task in self.tasks:
            parents = [lookup[name] for name in task.dependencies]
            if task.role == "merchandiser" and parents:
                raise ValueError("selection cannot depend on other tasks")
            if task.role == "copywriter" and (len(parents) != 1 or parents[0].role != "merchandiser"):
                raise ValueError("copywriter must depend on selection")
            if task.role == "compliance" and (len(parents) != 1 or parents[0].role != "copywriter" or parents[0].sku != task.sku):
                raise ValueError("review must depend on the same product draft")
            if task.role == "director" and (task.task_id != "summary" or not parents or any(p.role != "compliance" for p in parents)):
                raise ValueError("summary must depend on reviews")
        ancestors = {"summary"}
        for _ in self.tasks:
            ancestors |= {d for name in list(ancestors) for d in lookup[name].dependencies}
        if ancestors != lookup.keys():
            raise ValueError("orphan task")
        return self


class SkillSpec(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    role: Role
    read_only: Literal[True] = True
    version: str = "1"


# Derived from the contract catalog so permissions and schemas have one source.
SKILLS = {name: SkillSpec(name=name, role=c.roles[0], version=c.version) for name, c in SKILL_CONTRACTS.items()}
ROLE_SKILL = {spec.role: name for name, spec in SKILLS.items()}
# A run in one of these states never advances again; "blocked" stays retryable by an explicit advance.
FINISHED = {"succeeded", "review_required", "cancelled"}
JOB_STATES = ("idle", "queued", "cancel_requested")


class CampaignHarness:
    """SQLite checkpoints + leases fence concurrent runners, not business transactions."""

    def __init__(self, store: Store, *, workers: int = 3, max_attempts: int = 2,
                 max_steps: int = 24, deadline_seconds: float = 30):
        if not 1 <= workers <= 5 or not 1 <= max_attempts <= 3 or not 1 <= max_steps <= 100 or not 0 < deadline_seconds <= 30:
            raise ValueError("invalid harness limits")
        self.store = store
        self.model_budget = ModelBudget(store)
        self.workers = workers
        self.max_attempts = max_attempts
        self.max_steps = max_steps
        self.deadline_seconds = deadline_seconds
        from .specialists import SpecialistModel
        self.model = SpecialistModel.configured()
        self.hooks = default_hooks(store)
        with store.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS campaign_runs (
                    run_id TEXT PRIMARY KEY,
                    live_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    state TEXT NOT NULL,
                    lease_owner TEXT,
                    lease_until REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS campaign_artifacts (
                    run_id TEXT NOT NULL,
                    artifact_id TEXT NOT NULL,
                    body TEXT NOT NULL,
                    PRIMARY KEY (run_id, artifact_id)
                );
                -- Written in the same transaction as the state it describes, so a replay never shows a state that was not saved.
                CREATE TABLE IF NOT EXISTS campaign_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    body TEXT NOT NULL,
                    at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS campaign_events_run ON campaign_events (run_id, seq);
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(campaign_runs)")}
            # Databases created before background jobs existed get the columns added in place.
            for name, ddl in (("job_state", "TEXT NOT NULL DEFAULT 'idle'"), ("job_updated", "REAL NOT NULL DEFAULT 0"),
                              ("job_error", "TEXT")):
                if name not in columns:
                    db.execute(f"ALTER TABLE campaign_runs ADD COLUMN {name} {ddl}")
            db.execute("CREATE INDEX IF NOT EXISTS campaign_runs_job ON campaign_runs (job_state, job_updated)")
            db.commit()

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _summary(state: dict[str, Any]) -> dict[str, Any]:
        # Events carry progress only; artifacts and the trace stay behind their own endpoints.
        return {"status": state["status"], "revision": state["revision"], "steps": state["steps"],
                "tasks": {name: item["status"] for name, item in state["tasks"].items()},
                "budget_stop": state.get("budget_stop"), "output_artifact": state.get("output_artifact")}

    def _event(self, db, run_id: str, kind: str, body: dict[str, Any]) -> None:
        db.execute("INSERT INTO campaign_events (run_id,kind,body,at) VALUES (?,?,?,?)",
                   (run_id, kind, self._json(body), time.time()))

    def create(self, live_id: str, instruction: str, *, run_id: str | None = None,
               skus: list[str] | None = None, top_k: int = 3, model_limits: dict | None = None) -> dict[str, Any]:
        if not instruction.strip() or len(instruction) > 500 or not 1 <= top_k <= 5:
            raise BusinessError("INVALID_PLAN", "请输入准备目标，商品数量为 1 到 5")
        run_id = run_id or str(uuid.uuid4())
        limits = dict(MAX_CAMPAIGN_LIMITS if model_limits is None else model_limits)
        if set(limits) != set(MAX_CAMPAIGN_LIMITS) or any(type(limits[k]) is not int or not 0 <= limits[k] <= bound for k, bound in MAX_CAMPAIGN_LIMITS.items()):
            raise BusinessError("INVALID_MODEL_BUDGET", "模型预算必须处于平台额度范围内")
        request_hash = hashlib.sha256(self._json([live_id, instruction, skus, top_k, limits]).encode()).hexdigest()
        # Replaying an existing request must not depend on today's catalog availability.
        with self.store.connection() as db:
            existing = db.execute("SELECT request_hash,state FROM campaign_runs WHERE run_id=?", (run_id,)).fetchone()
        if existing:
            legacy_hash = hashlib.sha256(self._json([live_id, instruction, skus, top_k]).encode()).hexdigest()
            legacy_default = limits == MAX_CAMPAIGN_LIMITS and "model_limits" not in json.loads(existing["state"])
            if existing["request_hash"] != request_hash and not (legacy_default and existing["request_hash"] == legacy_hash):
                raise BusinessError("PLAN_ID_CONFLICT", "计划 ID 已绑定其他目标或范围")
            return json.loads(existing["state"])
        snapshot = self.store.snapshot(live_id)
        lookup = {p["sku"]: p for p in snapshot["products"]}
        if skus is not None and (not skus or len(skus) > 5 or len(set(skus)) != len(skus) or not set(skus) <= lookup.keys()):
            raise BusinessError("INVALID_PLAN", "商品范围必须是 1 到 5 个不重复的现有 SKU")
        ranked = sorted((p for p in snapshot["products"] if p["stock"] > 0),
                        key=lambda p: (p["stock"] < 50, -(p["price_cents"] - p["cost_cents"]), p["sku"]))
        selected = skus if skus is not None else [p["sku"] for p in ranked[:top_k]]
        if not selected:
            raise BusinessError("OUT_OF_STOCK", "没有可准备的商品")
        tasks = [TaskSpec(task_id="selection", role="merchandiser")]
        for i, sku in enumerate(selected):
            tasks.extend([
                TaskSpec(task_id=f"brief_{i}", role="copywriter", dependencies=["selection"], sku=sku),
                TaskSpec(task_id=f"review_{i}", role="compliance", dependencies=[f"brief_{i}"], sku=sku),
            ])
        tasks.append(TaskSpec(task_id="summary", role="director", dependencies=[f"review_{i}" for i in range(len(selected))]))
        plan = PlanSpec(tasks=tasks)
        state = {
            "run_id": run_id, "live_id": live_id, "instruction": instruction, "status": "pending",
            "planner_mode": "bounded_template", "selected_skus": selected,
            "snapshot_version": snapshot["live"]["version"],
            "product_versions": {sku: lookup[sku]["version"] for sku in selected},
            "preference_snapshot": snapshot["preferences"], "model_limits": limits, "replan_count": 0,
            "plan": plan.model_dump(), "revision": 1, "steps": 0,
            "tasks": {t.task_id: {"status": "pending", "attempts": 0} for t in tasks},
            "trace": [], "created_at": time.time(),
        }
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM campaign_runs WHERE run_id=?", (run_id,)).fetchone()
            if row:
                if row["request_hash"] != request_hash:
                    raise BusinessError("PLAN_ID_CONFLICT", "计划 ID 已绑定其他目标或范围")
                return json.loads(row["state"])
            db.execute("INSERT INTO campaign_runs (run_id,live_id,request_hash,state) VALUES (?,?,?,?)",
                       (run_id, live_id, request_hash, self._json(state)))
            self._event(db, run_id, "created", self._summary(state))
            db.commit()
        return state

    def replan(self, live_id: str, run_id: str, replacements: dict[str, str], expected_revision: int) -> dict[str, Any]:
        """Explicitly rebase a bounded DAG, preserving only still-valid successful branches."""
        owner = str(uuid.uuid4())
        state = self._claim(live_id, run_id, owner)
        try:
            if state["status"] == "cancelled":
                raise BusinessError("PLAN_CANCELLED", "计划已取消，请创建新计划")
            if state["revision"] != expected_revision:
                raise BusinessError("PLAN_REVISION_CONFLICT", "计划版本已变化，请刷新后重规划")
            if state.get("replan_count", 0) >= 2:
                raise BusinessError("REPLAN_LIMIT", "同一计划最多重规划两次，请重新确认目标")
            if state["steps"] >= self.max_steps:
                raise BusinessError("STEP_LIMIT", "重规划不能重置任务总步数上限")
            old = state["selected_skus"]
            if not set(replacements) <= set(old):
                raise BusinessError("INVALID_REPLACEMENT", "只能替换当前计划中的商品")
            selected = [replacements.get(sku, sku) for sku in old]
            snapshot = self.store.snapshot(live_id)
            lookup = {p["sku"]: p for p in snapshot["products"]}
            if len(set(selected)) != len(selected) or not set(selected) <= lookup.keys():
                raise BusinessError("INVALID_REPLACEMENT", "替换后必须为不重复的已有商品")
            if any(lookup[sku]["stock"] <= 0 for sku in selected):
                raise BusinessError("OUT_OF_STOCK", "重规划商品必须有库存")
            archive = copy.deepcopy(state)
            archive_id = "plan-" + hashlib.sha256(self._json(archive).encode()).hexdigest()[:24]
            spec = PlanSpec.model_validate(state["plan"])
            changed = {i for i, sku in enumerate(selected) if sku != old[i] or
                       lookup[sku]["version"] != state["product_versions"].get(sku) or
                       state.get("preference_snapshot") != snapshot["preferences"]}
            invalidated = {"selection", "summary"} | {f"{role}_{i}" for i in changed for role in ("brief", "review")}
            preserved = [task_id for task_id, item in state["tasks"].items()
                         if task_id not in invalidated and item["status"] == "succeeded"]
            new_tasks = []
            for task in spec.tasks:
                if task.sku:
                    task = task.model_copy(update={"sku": selected[old.index(task.sku)]})
                new_tasks.append(task)
                if task.task_id in invalidated:
                    state["tasks"][task.task_id] = {"status": "pending", "attempts": 0}
            state.update(selected_skus=selected, product_versions={sku: lookup[sku]["version"] for sku in selected},
                         snapshot_version=snapshot["live"]["version"], preference_snapshot=snapshot["preferences"],
                         plan=PlanSpec(tasks=new_tasks).model_dump(), revision=state["revision"]+1,
                         replan_count=state.get("replan_count", 0)+1, status="pending")
            state.pop("output_artifact", None)
            state.pop("budget_stop", None)
            state.setdefault("plan_revisions", []).append({"revision": state["revision"], "archive_id": archive_id,
                "preserved_tasks": preserved, "invalidated_tasks": sorted(invalidated), "replacements": replacements,
                "reason": "explicit_rebase_against_current_product_and_preference_versions", "at": time.time()})
            self._save(state, owner, [(archive_id, archive)])
            return state
        finally:
            with self.store.connection() as db:
                db.execute("UPDATE campaign_runs SET lease_owner=NULL,lease_until=0 WHERE run_id=? AND lease_owner=?", (run_id, owner))
                db.commit()

    def get(self, live_id: str, run_id: str) -> dict[str, Any]:
        with self.store.connection() as db:
            row = db.execute("SELECT state FROM campaign_runs WHERE live_id=? AND run_id=?", (live_id, run_id)).fetchone()
        if not row:
            raise BusinessError("PLAN_NOT_FOUND", "该直播间下没有此计划")
        return json.loads(row["state"])

    def _claim(self, live_id: str, run_id: str, owner: str) -> dict[str, Any]:
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM campaign_runs WHERE live_id=? AND run_id=?", (live_id, run_id)).fetchone()
            if row is None:
                raise BusinessError("PLAN_NOT_FOUND", "计划不存在")
            if row["lease_owner"] and row["lease_until"] > time.time():
                raise BusinessError("PLAN_BUSY", "计划正在由另一执行器推进")
            db.execute("UPDATE campaign_runs SET lease_owner=?,lease_until=? WHERE run_id=?",
                       (owner, time.time() + 60, run_id))
            db.commit()
        return json.loads(row["state"])

    def _save(self, state: dict[str, Any], owner: str, artifacts: list[tuple[str, dict]] | None = None,
              *, event: str = "state", job_state: str | None = None) -> None:
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            # The lease check fences a runner whose lease expired while it was paused or partitioned.
            changed = db.execute("UPDATE campaign_runs SET state=?,job_state=COALESCE(?,job_state),"
                                 "job_updated=CASE WHEN ? IS NULL THEN job_updated ELSE ? END "
                                 "WHERE run_id=? AND lease_owner=? AND lease_until>?",
                                 (self._json(state), job_state, job_state, time.time(), state["run_id"], owner, time.time())).rowcount
            if changed != 1:
                raise BusinessError("PLAN_LEASE_LOST", "计划执行租约失效，请读取已保存状态")
            for artifact_id, body in artifacts or []:
                db.execute("INSERT OR REPLACE INTO campaign_artifacts VALUES (?,?,?)",
                           (state["run_id"], artifact_id, self._json(body)))
            self._event(db, state["run_id"], event, self._summary(state))
            db.commit()

    def _cancel_requested(self, run_id: str) -> bool:
        with self.store.connection() as db:
            row = db.execute("SELECT job_state FROM campaign_runs WHERE run_id=?", (run_id,)).fetchone()
        return row is not None and row["job_state"] == "cancel_requested"

    def _finish_cancel(self, state: dict[str, Any], owner: str) -> dict[str, Any]:
        """Called only between waves, so no task of this runner is mid-flight."""
        if state["status"] in FINISHED:
            self._save(state, owner, event="cancel_ignored", job_state="idle")
            return state
        for item in state["tasks"].values():
            if item["status"] == "running":
                # Left by a dead runner; workers are read-only, so an unfinished attempt has no side effect to undo.
                item["status"] = "pending"
        state["status"] = "cancelled"
        state["cancel"] = {"stopped_at": time.time(), "boundary": "between_waves",
                           "not_started": sorted(n for n, i in state["tasks"].items() if i["status"] == "pending"),
                           "side_effects": "none_workers_are_read_only"}
        self._save(state, owner, event="cancelled", job_state="idle")
        return state

    def start(self, live_id: str, run_id: str) -> dict[str, Any]:
        """Queue a run for the background worker. Repeating the call queues it once."""
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state,job_state FROM campaign_runs WHERE live_id=? AND run_id=?",
                             (live_id, run_id)).fetchone()
            if row is None:
                raise BusinessError("PLAN_NOT_FOUND", "该直播间下没有此计划")
            state = json.loads(row["state"])
            if state["status"] == "cancelled":
                raise BusinessError("PLAN_CANCELLED", "计划已取消，请创建新计划")
            job_state = row["job_state"]
            if job_state == "idle" and state["status"] not in FINISHED:
                db.execute("UPDATE campaign_runs SET job_state='queued',job_updated=?,job_error=NULL WHERE run_id=?",
                           (time.time(), run_id))
                self._event(db, run_id, "job_queued", {"status": state["status"]})
                job_state = "queued"
            db.commit()
        return {"run_id": run_id, "status": state["status"], "job_state": job_state,
                "events": f"/api/campaigns/{live_id}/{run_id}/events"}

    def cancel(self, live_id: str, run_id: str) -> dict[str, Any]:
        """Record the request durably first; stop now if no runner holds the lease, otherwise at its next wave boundary."""
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state,job_state FROM campaign_runs WHERE live_id=? AND run_id=?",
                             (live_id, run_id)).fetchone()
            if row is None:
                raise BusinessError("PLAN_NOT_FOUND", "该直播间下没有此计划")
            state = json.loads(row["state"])
            if state["status"] in FINISHED:
                db.rollback()
                return {"cancel": "stopped" if state["status"] == "cancelled" else "already_finished", **self._summary(state)}
            if row["job_state"] != "cancel_requested":
                db.execute("UPDATE campaign_runs SET job_state='cancel_requested',job_updated=? WHERE run_id=?",
                           (time.time(), run_id))
                self._event(db, run_id, "cancel_requested", {"status": state["status"]})
            db.commit()
        try:
            state = self.run(live_id, run_id, max_waves=1)
        except BusinessError as exc:
            if exc.code != "PLAN_BUSY":
                raise
            # The runner holding the lease sees the flag before its next wave.
            return {"cancel": "requested", **self._summary(self.get(live_id, run_id))}
        return {"cancel": "stopped" if state["status"] == "cancelled" else "already_finished", **self._summary(state)}

    def job(self, run_id: str) -> dict[str, Any]:
        with self.store.connection() as db:
            row = db.execute("SELECT job_state,job_error,lease_owner,lease_until FROM campaign_runs WHERE run_id=?",
                             (run_id,)).fetchone()
        return {"state": row["job_state"], "error": row["job_error"],
                "runner_active": bool(row["lease_owner"]) and row["lease_until"] > time.time()}

    def finish_job(self, run_id: str, error: str | None = None) -> None:
        """Worker hand-off back to idle; a cancel recorded meanwhile is left for the next pass."""
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE campaign_runs SET job_state='idle',job_updated=?,job_error=? "
                                 "WHERE run_id=? AND job_state='queued'", (time.time(), error, run_id)).rowcount
            if changed:
                self._event(db, run_id, "job_finished", {"error": error})
            db.commit()

    def next_job(self) -> tuple[str, str] | None:
        """A hint only: the lease taken inside run() is what makes one runner win."""
        with self.store.connection() as db:
            row = db.execute("SELECT live_id,run_id FROM campaign_runs WHERE job_state IN ('queued','cancel_requested') "
                             "AND (lease_owner IS NULL OR lease_until<=?) ORDER BY job_updated LIMIT 1",
                             (time.time(),)).fetchone()
        return (row["live_id"], row["run_id"]) if row else None

    def events(self, live_id: str, run_id: str, after: int, limit: int = 200) -> tuple[list[dict[str, Any]], bool]:
        """Events after a sequence number plus whether more can still arrive, read from one snapshot."""
        with self.store.connection() as db:
            db.execute("BEGIN")
            run = db.execute("SELECT job_state,lease_owner,lease_until FROM campaign_runs WHERE live_id=? AND run_id=?",
                             (live_id, run_id)).fetchone()
            if run is None:
                db.rollback()
                raise BusinessError("PLAN_NOT_FOUND", "该直播间下没有此计划")
            rows = db.execute("SELECT seq,kind,body FROM campaign_events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?",
                              (run_id, after, limit)).fetchall()
            db.rollback()
        active = run["job_state"] != "idle" or (bool(run["lease_owner"]) and run["lease_until"] > time.time())
        return [{"seq": r["seq"], "kind": r["kind"], "body": json.loads(r["body"])} for r in rows], active or len(rows) == limit

    def artifact(self, live_id: str, run_id: str, artifact_id: str) -> dict[str, Any]:
        self.get(live_id, run_id)
        with self.store.connection() as db:
            row = db.execute("SELECT body FROM campaign_artifacts WHERE run_id=? AND artifact_id=?",
                             (run_id, artifact_id)).fetchone()
        if row is None:
            raise BusinessError("ARTIFACT_NOT_FOUND", "材料不存在")
        return json.loads(row["body"])

    def context(self, state: dict[str, Any], task: TaskSpec) -> dict[str, Any]:
        """Only direct dependency artifacts and the role's public product fields are handed off."""
        snapshot = self.store.snapshot(state["live_id"])
        if snapshot["live"]["version"] != state["snapshot_version"] or any(
            p["version"] != state["product_versions"][p["sku"]]
            for p in snapshot["products"] if p["sku"] in state["product_versions"]
        ):
            raise BusinessError("STALE_CAMPAIGN", "准备期间商品或直播状态已变化，请显式重规划或创建新计划")
        product_lookup = {p["sku"]: p for p in snapshot["products"]}
        products = [product_lookup[sku] for sku in state["selected_skus"]]
        if task.sku:
            products = [p for p in products if p["sku"] == task.sku]
        public = [{k: p[k] for k in ("sku", "title", "category", "price_cents", "stock", "features", "version")} for p in products]
        dependencies = {name: self.artifact(state["live_id"], state["run_id"], state["tasks"][name]["artifact_id"])
                        for name in task.dependencies}
        result = {
            "role": task.role, "live_id": state["live_id"], "task_id": task.task_id,
            "source": "live_business_db", "snapshot_version": state["snapshot_version"],
            "products": public, "dependencies": dependencies,
            "preference": {"value": snapshot["preferences"].get("style", "")[:120], "source": "explicit_user_preference"},
        }
        if task.role in {"merchandiser", "director"}:
            from .memory import memory_view
            memory = memory_view(self.store, state["live_id"], "pre_live")
            result["memory"] = {"behavior": memory["L3_behavior"], "conflicts": memory["conflicts"],
                                "unassessed": memory["unassessed"]}
        return result

    def freshness(self, state: dict[str, Any]) -> dict:
        snapshot = self.store.snapshot(state["live_id"])
        lookup = {p["sku"]: p for p in snapshot["products"]}
        changed = [sku for sku, version in state["product_versions"].items() if sku not in lookup or lookup[sku]["version"] != version]
        preference_changed = state.get("preference_snapshot") != snapshot["preferences"]
        return {"changed_skus": changed, "preference_changed": preference_changed,
                "requires_rebase": bool(changed) or preference_changed or snapshot["live"]["version"] != state["snapshot_version"],
                "rule": "historical_artifacts_remain_auditable_not_revalidated_live_claims"}

    def pre_tool_call(self, task: TaskSpec, skill_name: str, context: dict[str, Any]) -> None:
        spec = SKILLS.get(skill_name)
        if spec is None or spec.role != task.role:
            raise BusinessError("CAPABILITY_DENIED", "子智能体无权使用该技能")
        if context.get("role") != task.role or context.get("task_id") != task.task_id:
            raise BusinessError("CONTEXT_SCOPE", "上下文不属于当前角色或任务")
        if len(self._json(context)) > 16000:
            raise BusinessError("CONTEXT_BUDGET", "子任务上下文超过字符预算，请缩小商品范围")

    def _perform(self, task: TaskSpec, context: dict[str, Any]) -> dict[str, Any]:
        if task.role == "merchandiser":
            return {"source": context["source"], "skus": [p["sku"] for p in context["products"]],
                    "memory_conflicts": context.get("memory", {}).get("conflicts", []),
                    "unassessed_preferences": context.get("memory", {}).get("unassessed", []),
                    "rationale": "已有库存与毛利规则选品；非成交效果预测"}
        if task.role == "copywriter":
            p = context["products"][0]
            return BriefDraft(sku=p["sku"], title=p["title"], claims=p["features"].split("；"),
                              price_cents=p["price_cents"], stock=p["stock"], product_version=p["version"],
                              source=f"product:{p['sku']}@{p['version']}").model_dump()
        if task.role == "compliance":
            draft = BriefDraft.model_validate(next(iter(context["dependencies"].values())))
            product = context["products"][0]
            allowed = product["features"].split("；")
            supported = (bool(draft.claims) and len(set(draft.claims)) == len(draft.claims)
                         and draft.sku == product["sku"] and draft.title == product["title"]
                         and draft.price_cents == product["price_cents"] and draft.stock == product["stock"]
                         and draft.product_version == product["version"]
                         and draft.source == f"product:{product['sku']}@{product['version']}"
                         and all(c in allowed for c in draft.claims))
            red_flags = [c for c in draft.claims if any(word in c for word in ("根治", "包治", "保证治愈", "全网最低"))]
            return {"sku": draft.sku, "approved": supported and not red_flags,
                    "reason": "field_grounded" if supported and not red_flags else "unsupported_or_prohibited_claim",
                    "evidence_source": draft.source,
                    "brief": draft.model_dump() if supported and not red_flags else None}
        reviews = list(context["dependencies"].values())
        return {"status": "ready" if all(r.get("approved") is True for r in reviews) else "needs_revision",
                "approved_briefs": [r["brief"] for r in reviews if r.get("approved") is True],
                "rejected_skus": [r["sku"] for r in reviews if r.get("approved") is not True],
                "memory_conflicts": context.get("memory", {}).get("conflicts", []),
                "unassessed_preferences": context.get("memory", {}).get("unassessed", []),
                "execution_boundary": "材料准备完成不等于已切品、改价或发券；写操作须单独审批",
                "scope": "field_grounding_and_keyword_checks_not_legal_certification"}

    def _worker(self, state: dict[str, Any], task: TaskSpec) -> dict[str, Any]:
        start = time.monotonic()
        skill = ROLE_SKILL[task.role]
        event = {"surface": "skill", "tool": skill, "access": "read", "live_id": state["live_id"],
                 "subject_id": f"{state['run_id']}/{task.task_id}", "role": task.role}
        checks: list[dict] = []
        try:
            with required_check(checks, "load_scoped_fresh_context", "pre_tool_call"):
                context = self.context(state, task)
            with required_check(checks, "capability_scope_and_budget", "pre_tool_call"):
                self.pre_tool_call(task, skill, context)
            with required_check(checks, "skill_input_contract", "pre_tool_call"):
                validate(SKILL_CONTRACTS[skill].input_model, context, "SKILL_CONTRACT")
            checks += self.hooks.run("pre_tool_call", {**event, "input": context})
            usage = {"backend": "deterministic", "model_calls": 0, "reported_total_tokens": 0}
            if task.role == "copywriter" and self.model is not None:
                # Thread-pool workers do not inherit the caller's ContextVar automatically.
                with model_scope(self.model_budget, state["live_id"], f"campaign:{state['run_id']}", state.get("model_limits", MAX_CAMPAIGN_LIMITS)):
                    body, usage = self.model.draft(context)
            else:
                body = self._perform(task, context)
            with required_check(checks, "skill_output_contract", "post_tool_call"):
                body = self.post_tool_call(task, body)
            checks += self.hooks.run("post_tool_call", {**event, "output": body, "usage": usage})
        except Exception as exc:
            if isinstance(exc, HookDenied):
                checks += [c for c in exc.checks if c not in checks]
            code = exc.code if isinstance(exc, BusinessError) else "WORKER_FAILED"
            try:
                checks += self.hooks.run("on_tool_error", {**event, "error": {"code": code}})
            except Exception:
                pass
            exc.checks = checks
            raise
        return {"body": body, "context_chars": len(self._json(context)), "elapsed_ms": round((time.monotonic()-start)*1000, 2),
                "checks": checks, "skill": skill, **usage}

    def post_tool_call(self, task: TaskSpec, body: Any) -> dict[str, Any]:
        if not isinstance(body, dict) or len(self._json(body)) > 32000:
            raise BusinessError("OUTPUT_CONTRACT", "技能输出格式错误或超过大小上限")
        return validate(SKILL_CONTRACTS[ROLE_SKILL[task.role]].output_model, body, "OUTPUT_CONTRACT").model_dump()

    def run(self, live_id: str, run_id: str, *, max_waves: int | None = None,
            retry_failed: bool = False) -> dict[str, Any]:
        if max_waves is not None and not 1 <= max_waves <= 12:
            raise BusinessError("INVALID_PLAN", "推进轮次必须在 1 到 12 之间")
        owner = str(uuid.uuid4())
        state = self._claim(live_id, run_id, owner)
        started = time.monotonic()
        try:
            if self._cancel_requested(run_id):
                return self._finish_cancel(state, owner)
            if state["status"] in FINISHED:
                return state
            if retry_failed:
                for item in state["tasks"].values():
                    if item["status"] == "failed" and item.get("error", {}).get("retryable") and item["attempts"] < self.max_attempts:
                        item["status"] = "pending"
                        item.pop("error", None)
                state["revision"] += 1
            # A dead runner may leave running read-only tasks. Stable task IDs make replay safe.
            for item in state["tasks"].values():
                if item["status"] == "running":
                    item["status"] = "pending" if item["attempts"] < self.max_attempts else "failed"
            spec = PlanSpec.model_validate(state["plan"])
            state["status"] = "running"
            self._save(state, owner)
            waves = 0
            while True:
                ready = [t for t in spec.tasks if state["tasks"][t.task_id]["status"] == "pending"
                         and all(state["tasks"][d]["status"] == "succeeded" for d in t.dependencies)]
                if not ready:
                    break
                remaining = self.max_steps - state["steps"]
                if remaining <= 0 or time.monotonic()-started >= self.deadline_seconds:
                    state["budget_stop"] = "step_limit" if remaining <= 0 else "deadline"
                    break
                if waves and self._cancel_requested(run_id):
                    return self._finish_cancel(state, owner)
                wave = ready[:min(self.workers, remaining)]
                for task in wave:
                    state["tasks"][task.task_id].update(status="running", attempts=state["tasks"][task.task_id]["attempts"]+1)
                    state["steps"] += 1
                self._save(state, owner)
                worker_state = copy.deepcopy(state)
                with ThreadPoolExecutor(max_workers=self.workers) as pool:
                    futures = {pool.submit(self._worker, worker_state, t): t for t in wave}
                    for future in as_completed(futures):
                        task = futures[future]
                        item = state["tasks"][task.task_id]
                        trace = {"trace_id": f"{run_id}/r{state['revision']}/{task.task_id}/{item['attempts']}", "revision": state["revision"],
                                 "task_id": task.task_id, "role": task.role, "attempt": item["attempts"]}
                        try:
                            result = future.result()
                        except Exception as exc:
                            code = exc.code if isinstance(exc, BusinessError) else "WORKER_FAILED"
                            recovery = recovery_for(code)
                            item.update(status="failed", error={"code": code, "retryable": recovery["strategy"] == "retry_with_backoff"})
                            trace.update(status="failed", code=code, recovery=recovery["strategy"],
                                         checks=getattr(exc, "checks", []))
                            if hasattr(exc, "model_usage"):
                                trace.update(exc.model_usage)
                            state["trace"].append(trace)
                            self._save(state, owner)
                        else:
                            body = result.pop("body")
                            digest = hashlib.sha256(self._json(body).encode()).hexdigest()
                            artifact_id = f"{task.task_id}-{digest[:16]}"
                            item.update(status="succeeded", artifact_id=artifact_id,
                                        preview=self._json(body)[:180], **result)
                            trace.update(status="succeeded", artifact_id=artifact_id, **result)
                            state["trace"].append(trace)
                            self._save(state, owner, [(artifact_id, body)])
                waves += 1
                if max_waves is not None and waves >= max_waves:
                    break
            states = [t["status"] for t in state["tasks"].values()]
            state["status"] = "succeeded" if all(s == "succeeded" for s in states) else "blocked" if "failed" in states else "paused"
            if state["status"] == "succeeded":
                state["output_artifact"] = state["tasks"]["summary"]["artifact_id"]
                final = self.artifact(live_id, run_id, state["output_artifact"])
                if final["status"] != "ready":
                    state["status"] = "review_required"
            self._save(state, owner)
            return state
        finally:
            with self.store.connection() as db:
                db.execute("UPDATE campaign_runs SET lease_owner=NULL,lease_until=0 WHERE run_id=? AND lease_owner=?", (run_id, owner))
                db.commit()
