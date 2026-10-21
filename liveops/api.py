"""HTTP API and local operator console."""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .agent import LiveAgent
from .campaigns import CampaignHarness
from .memory import Scene, forget_preference, memory_view
from .metrics import campaign_metrics
from .store import BusinessError, Store
from .remote import RemoteOperations, SimulatedPlatform
from .tooling import business_error as describe_business_error
from .workspace import Workspace
from .worker import CampaignWorker
from .feedback import Feedback


DEMO_LIVE_ID = "LIVE-DEMO-001"
WEB_DIR = Path(__file__).resolve().parent / "web"


class CommandIn(BaseModel):
    live_id: str = DEMO_LIVE_ID
    instruction: str = Field(min_length=1, max_length=500)
    command_id: str | None = None


class ApprovalIn(BaseModel):
    approved: bool


class CampaignIn(BaseModel):
    live_id: str = DEMO_LIVE_ID
    instruction: str = Field(min_length=1, max_length=500)
    run_id: str | None = None
    skus: list[str] | None = Field(default=None, min_length=1, max_length=5)
    top_k: int = Field(default=3, ge=1, le=5)
    max_model_calls: int = Field(default=20, ge=0, le=20)
    max_reserved_tokens: int = Field(default=300000, ge=0, le=300000)


class ReplanIn(BaseModel):
    replacements: dict[str, str] = Field(default_factory=dict, max_length=5)
    expected_revision: int = Field(ge=1)


class RuntimeEventIn(BaseModel):
    event_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=0)
    kind: Literal["scene_changed", "audience_observed"]
    scene: Scene | None = None
    viewers: int | None = Field(default=None, ge=0, le=10000000, strict=True)


class FeedbackIn(BaseModel):
    event_id: str = Field(min_length=1, max_length=100)
    choice: Literal["accept", "reject", "modify"]
    rating: int | None = Field(default=None, ge=1, le=5, strict=True)
    reason: str | None = Field(default=None, max_length=32)
    alternative_sku: str | None = Field(default=None, max_length=32)


class AdvanceIn(BaseModel):
    max_waves: int | None = Field(default=None, ge=1, le=12)
    retry_failed: bool = False


class StockChangeIn(BaseModel):
    live_id: str = DEMO_LIVE_ID
    sku: str
    stock: int = Field(ge=0, le=100000)


class RemoteFaultIn(BaseModel):
    mode: Literal["normal", "lost_response", "delayed_visibility", "before_commit", "query_unavailable", "reject"]
    visibility_queries: int = Field(default=2, ge=0, le=10)


def create_app(db_path: str | None = None, checkpoint_path: str | None = None,
               coupon_backend: str | None = None, remote_path: str | None = None,
               background_worker: bool | None = None) -> FastAPI:
    store = Store(db_path or os.getenv("LIVEOPS_DB_PATH", "data/liveops.db"))
    backend = coupon_backend or os.getenv("LIVEOPS_COUPON_BACKEND", "local")
    if backend not in {"local", "simulated_remote"}:
        raise ValueError("LIVEOPS_COUPON_BACKEND must be local or simulated_remote")
    remote_operations = None
    if backend == "simulated_remote":
        platform = SimulatedPlatform(remote_path or os.getenv("LIVEOPS_PLATFORM_PATH", str(Path(store.path).with_name("simulated_platform.db"))))
        remote_operations = RemoteOperations(store, platform)
    workspace = Workspace(store)
    feedback = Feedback(store)
    campaigns = CampaignHarness(store)
    agent = LiveAgent(store, checkpoint_path or os.getenv("LIVEOPS_CHECKPOINT_PATH", "data/checkpoints.db"), remote_operations, campaigns, workspace)
    if background_worker is None:
        background_worker = os.getenv("LIVEOPS_CAMPAIGN_WORKER", "1") != "0"
    worker = CampaignWorker(campaigns)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if background_worker:
            # Jobs left queued by a previous process are picked up here once their lease has expired.
            worker.start()
        try:
            yield
        finally:
            worker.stop()
            agent.close()

    app = FastAPI(title="LiveOps Harness", version="0.1.0", lifespan=lifespan)
    app.state.store = store
    app.state.agent = agent
    app.state.remote_operations = remote_operations
    app.state.campaigns = campaigns
    app.state.workspace = workspace
    app.state.worker = worker

    @app.exception_handler(BusinessError)
    async def business_error(_, exc: BusinessError):
        return JSONResponse(status_code=409, content={
            "code": exc.code,
            "detail": exc.message,
            "tool_error": describe_business_error(exc.code, exc.message),
        })

    @app.get("/")
    def index():
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/app.css")
    def css():
        return FileResponse(WEB_DIR / "app.css", media_type="text/css")

    @app.get("/app.js")
    def js():
        return FileResponse(WEB_DIR / "app.js", media_type="application/javascript")

    @app.get("/harness.css")
    def harness_css():
        return FileResponse(WEB_DIR / "harness.css", media_type="text/css")

    @app.get("/health")
    def health():
        return {"status": "ok", "mode": "model" if os.getenv("LIVEOPS_LLM_MODEL") else "offline", "coupon_backend": backend,
                "intent_model": os.getenv("LIVEOPS_LLM_MODEL") or None,
                "writer_model": os.getenv("LIVEOPS_SPECIALIST_MODEL") or None,
                "advice_generation": bool(os.getenv("LIVEOPS_LLM_MODEL") and os.getenv("LIVEOPS_LLM_BASE_URL")),
                "model_circuit": __import__("liveops.model_budget", fromlist=["breaker"]).breaker.status()}

    @app.get("/api/state/{live_id}")
    def state(live_id: str):
        return store.snapshot(live_id)

    @app.get("/api/commands/{live_id}")
    def commands(live_id: str):
        store.live(live_id)
        return store.commands(live_id)

    @app.get("/api/audit/{live_id}")
    def audit(live_id: str):
        store.live(live_id)
        return store.audit(live_id)

    @app.post("/api/commands")
    def submit(body: CommandIn):
        store.live(body.live_id)
        return agent.submit(body.live_id, body.instruction.strip(), body.command_id)

    @app.post("/api/commands/{command_id}/approval")
    def approve(command_id: str, body: ApprovalIn):
        return agent.resolve(command_id, body.approved)

    @app.post("/api/simulator/stock")
    def simulate_stock(body: StockChangeIn):
        store.live(body.live_id)
        store.simulate_stock_change(body.live_id, body.sku, body.stock)
        return store.snapshot(body.live_id)

    @app.get("/api/simulator/remote-fault")
    def get_remote_fault():
        return remote_operations.platform.get_fault() if remote_operations else {"enabled": False, "mode": "normal"}

    @app.post("/api/simulator/remote-fault")
    def remote_fault(body: RemoteFaultIn):
        if remote_operations is None:
            raise BusinessError("REMOTE_DISABLED", "请启用 simulated_remote 发券后端")
        remote_operations.platform.set_fault(body.mode, body.visibility_queries)
        return {"mode": body.mode, "visibility_queries": body.visibility_queries}

    @app.get("/api/operations/{live_id}")
    def operations(live_id: str):
        store.live(live_id)
        return store.remote_operations(live_id)

    @app.post("/api/commands/{command_id}/reconcile")
    def reconcile(command_id: str):
        return agent.reconcile(command_id)

    @app.get("/api/skills")
    def skills():
        from .campaigns import SKILLS
        return [spec.model_dump() for spec in SKILLS.values()]

    @app.get("/api/contracts")
    def contracts():
        from .contracts import catalog
        return {"tools": catalog(), "hooks": agent.hooks.names(),
                "rule": "optional_hooks_can_only_note_or_deny_required_checks_cannot_be_unregistered"}

    @app.get("/api/campaigns/{live_id}")
    def list_campaigns(live_id: str):
        store.live(live_id)
        with store.connection() as db:
            rows = db.execute("SELECT state FROM campaign_runs WHERE live_id=? ORDER BY rowid DESC LIMIT 20", (live_id,)).fetchall()
        return [{k: value for k, value in json.loads(row["state"]).items()
                 if k in {"run_id", "status", "instruction", "selected_skus", "created_at"}} for row in rows]

    @app.post("/api/campaigns")
    def create_campaign(body: CampaignIn):
        return campaigns.create(body.live_id, body.instruction, run_id=body.run_id, skus=body.skus, top_k=body.top_k,
                                model_limits={"max_calls": body.max_model_calls, "max_reserved_tokens": body.max_reserved_tokens})

    @app.get("/api/campaigns/{live_id}/{run_id}")
    def get_campaign(live_id: str, run_id: str):
        result = campaigns.get(live_id, run_id)
        return {**result, "freshness": campaigns.freshness(result), "job": campaigns.job(run_id)}

    @app.post("/api/campaigns/{live_id}/{run_id}/start", status_code=202)
    def start_campaign(live_id: str, run_id: str):
        # Returns at once; progress comes from /events or GET, and closing the client does not stop the run.
        return campaigns.start(live_id, run_id)

    @app.post("/api/campaigns/{live_id}/{run_id}/cancel")
    def cancel_campaign(live_id: str, run_id: str):
        return campaigns.cancel(live_id, run_id)

    @app.get("/api/campaigns/{live_id}/{run_id}/events")
    async def campaign_events(live_id: str, run_id: str, after: int | None = Query(default=None, ge=0),
                              last_event_id: str | None = Header(default=None)):
        # EventSource resends the last seen id as Last-Event-ID on reconnect; ?after= serves fetch-based clients.
        cursor = after if after is not None else int(last_event_id) if (last_event_id or "").isdigit() else 0
        await run_in_threadpool(campaigns.get, live_id, run_id)  # 409 PLAN_NOT_FOUND before the stream opens

        async def stream():
            position, idle_since, opened = cursor, time.monotonic(), time.monotonic()
            yield "retry: 2000\n\n"
            while True:
                rows, active = await run_in_threadpool(campaigns.events, live_id, run_id, position)
                for row in rows:
                    position = row["seq"]
                    yield f"id: {row['seq']}\nevent: {row['kind']}\ndata: {json.dumps(row['body'], ensure_ascii=False)}\n\n"
                if rows:
                    idle_since = time.monotonic()
                elif not active:
                    yield "event: end\ndata: {}\n\n"
                    return
                elif time.monotonic() - idle_since > 15:
                    yield ": keep-alive\n\n"
                    idle_since = time.monotonic()
                if time.monotonic() - opened > 300:
                    # Bounded connection; the client reconnects with Last-Event-ID and loses nothing.
                    return
                await asyncio.sleep(0.25)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/campaigns/{live_id}/{run_id}/metrics")
    def get_campaign_metrics(live_id: str, run_id: str):
        result = campaign_metrics(campaigns.get(live_id, run_id))
        result["provider_ledger"] = agent.model_budget.summary(live_id, f"campaign:{run_id}")
        return result

    @app.post("/api/campaigns/{live_id}/{run_id}/replan")
    def replan_campaign(live_id: str, run_id: str, body: ReplanIn):
        return campaigns.replan(live_id, run_id, body.replacements, body.expected_revision)

    @app.post("/api/campaigns/{live_id}/{run_id}/advance")
    def advance_campaign(live_id: str, run_id: str, body: AdvanceIn):
        return campaigns.run(live_id, run_id, max_waves=body.max_waves, retry_failed=body.retry_failed)

    @app.get("/api/campaigns/{live_id}/{run_id}/artifacts/{artifact_id}")
    def get_campaign_artifact(live_id: str, run_id: str, artifact_id: str):
        return campaigns.artifact(live_id, run_id, artifact_id)

    @app.get("/api/memory/{live_id}")
    def memory(live_id: str, scene: Scene = "on_live"):
        result = memory_view(store, live_id, scene)
        result["runtime_context"] = workspace.context(live_id, scene)
        result["conversation"] = result["runtime_context"]["conversation"]
        return result

    @app.get("/api/runtime/{live_id}")
    def runtime(live_id: str):
        return {"state": workspace.get(live_id), "context": workspace.context(live_id)}

    @app.post("/api/runtime/{live_id}/events")
    def observe(live_id: str, body: RuntimeEventIn):
        if body.kind == "scene_changed" and body.scene is None or body.kind == "audience_observed" and body.viewers is None:
            raise BusinessError("INVALID_OBSERVATION", "缺少阶段或在线人数")
        payload = {"scene": body.scene} if body.kind == "scene_changed" else {"viewers": body.viewers}
        return workspace.observe(live_id, body.event_id, body.kind, payload, body.expected_revision)

    @app.get("/api/runtime/{live_id}/artifacts/{artifact_id}")
    def turn_artifact(live_id: str, artifact_id: str):
        return workspace.artifact(live_id, artifact_id)

    @app.post("/api/feedback/{live_id}/{command_id}")
    def record_feedback(live_id: str, command_id: str, body: FeedbackIn):
        return feedback.record(live_id, command_id, body.event_id, body.choice, body.rating, body.reason, body.alternative_sku)

    @app.get("/api/metrics/{live_id}")
    def metrics(live_id: str):
        return feedback.metrics(live_id)

    @app.delete("/api/memory/{live_id}/preference")
    def forget(live_id: str):
        return forget_preference(store, live_id)

    return app


app = create_app()
