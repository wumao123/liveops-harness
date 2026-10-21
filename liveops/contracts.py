"""Declarative tool contracts. Business limits stay in policy/store; this layer declares shape and handling."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .store import BusinessError


SideEffect = Literal["none", "preference", "commerce"]
Risk = Literal["auto", "soft", "hard"]
Idempotency = Literal["not_required", "content_addressed_artifact", "action_ledger"]
UncertainOutcome = Literal["not_applicable", "reconcile_before_retry"]
RISK_ORDER = {"auto": 0, "soft": 1, "hard": 2}


# ---- Command inputs: the normalized action produced by agent._normalize_action ----

class _Action(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_live_version: int | None = None
    expected_scene_revision: int | None = None
    context_scene: str | None = None


class PlanRef(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    run_id: str
    revision: int
    replan_count: int


class Reference(BaseModel):
    """Where an ordinal such as "第二款" came from; checked again inside the write transaction."""
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["ordinal"]
    index: int = Field(ge=0, le=4)
    list_id: str
    sku: str
    source_command_id: str
    plan_ref: PlanRef | None = None


class ProductReadIn(_Action):
    type: Literal["status", "brief", "advise"]
    sku: str | None = None
    expected_product_version: int | None = None
    reference: Reference | None = None


class RankingIn(_Action):
    type: Literal["plan", "prepare"]
    top_k: int
    max_price_cents: int | None = None
    skus: list[str] | None = None
    exclude_skus: list[str] | None = None


class RecapIn(_Action):
    type: Literal["recap"]


class ClarifyIn(_Action):
    type: Literal["clarify"]
    question: str = Field(min_length=1, max_length=200)


class InvalidIn(_Action):
    type: Literal["invalid"]
    error: str = Field(max_length=160)


class SetPreferenceIn(_Action):
    type: Literal["set_preference"]
    value: str = Field(min_length=1, max_length=120)
    expected_live_version: int


class _CommerceWrite(_Action):
    # Version binding is what makes an approval refer to one concrete state.
    sku: str
    expected_live_version: int
    expected_product_version: int
    reference: Reference | None = None


class SwitchIn(_CommerceWrite):
    type: Literal["switch_product"]


class PriceIn(_CommerceWrite):
    type: Literal["update_price"]
    amount_cents: int


class CouponIn(_CommerceWrite):
    type: Literal["issue_coupon"]
    amount_cents: int
    quantity: int


class CommandResult(BaseModel):
    model_config = ConfigDict(extra="allow")
    status: Literal["ok", "blocked", "rejected", "clarification", "expired", "unknown", "manual_review", "sent", "ready"]


# ---- Skill inputs/outputs for the read-only preparation chain ----

class SkillContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str
    live_id: str
    task_id: str
    source: Literal["live_business_db"]
    snapshot_version: int
    products: list[dict[str, Any]] = Field(min_length=1, max_length=5)
    dependencies: dict[str, Any]
    preference: dict[str, Any]
    memory: dict[str, Any] | None = None
    # Copywriter only: the reviewer's issue codes for the previous draft of this task.
    revision_feedback: dict[str, Any] | None = None


class SelectionOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    skus: list[str] = Field(min_length=1, max_length=5)
    memory_conflicts: list[Any]
    unassessed_preferences: list[Any]
    rationale: str


class BriefDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    sku: str
    title: str
    claims: list[str] = Field(max_length=10)
    price_cents: int
    stock: int
    product_version: int
    source: str


class ReviewOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sku: str
    approved: bool
    reason: str
    evidence_source: str | None = None
    brief: dict[str, Any] | None
    issues: list[str] = Field(default_factory=list, max_length=20)


class SummaryOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ready", "needs_revision"]
    approved_briefs: list[dict[str, Any]]
    rejected_skus: list[str]
    memory_conflicts: list[Any]
    unassessed_preferences: list[Any]
    execution_boundary: str
    scope: str


@dataclass(frozen=True)
class ToolContract:
    name: str
    surface: Literal["command", "skill"]
    roles: tuple[str, ...]
    side_effect: SideEffect
    risk: Risk
    timeout_seconds: float
    idempotency: Idempotency
    uncertain_outcome: UncertainOutcome
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    version: str = "1"

    @property
    def access(self) -> Literal["read", "write"]:
        return "read" if self.side_effect == "none" else "write"

    def describe(self) -> dict[str, Any]:
        required = sorted(n for n, f in self.input_model.model_fields.items() if f.is_required())
        return {"name": self.name, "surface": self.surface, "roles": list(self.roles), "access": self.access,
                "side_effect": self.side_effect, "risk_floor": self.risk, "timeout_seconds": self.timeout_seconds,
                "idempotency": self.idempotency, "uncertain_outcome": self.uncertain_outcome,
                "input": self.input_model.__name__, "required_input": required,
                "output": self.output_model.__name__, "version": self.version}


# Local SQLite tools declare the store's busy_timeout; only the product skill makes an external call.
_DB_TIMEOUT = 10.0
_OPERATOR = ("operator",)


def _command(name, model, *, side_effect="none", risk="auto", idempotency="not_required", uncertain="not_applicable"):
    return ToolContract(name, "command", _OPERATOR, side_effect, risk, _DB_TIMEOUT, idempotency, uncertain, model, CommandResult)


COMMAND_CONTRACTS: dict[str, ToolContract] = {c.name: c for c in (
    _command("status", ProductReadIn),
    _command("brief", ProductReadIn),
    _command("advise", ProductReadIn),
    _command("plan", RankingIn),
    _command("prepare", RankingIn),
    _command("recap", RecapIn),
    _command("clarify", ClarifyIn),
    _command("invalid", InvalidIn),
    _command("set_preference", SetPreferenceIn, side_effect="preference", idempotency="action_ledger"),
    _command("switch_product", SwitchIn, side_effect="commerce", risk="soft", idempotency="action_ledger"),
    _command("update_price", PriceIn, side_effect="commerce", risk="hard", idempotency="action_ledger"),
    # The coupon tool may run against a remote platform; a timeout is unknown, never a resend.
    _command("issue_coupon", CouponIn, side_effect="commerce", risk="hard", idempotency="action_ledger",
             uncertain="reconcile_before_retry"),
)}

SKILL_CONTRACTS: dict[str, ToolContract] = {c.name: c for c in (
    ToolContract("catalog", "skill", ("merchandiser",), "none", "auto", _DB_TIMEOUT, "content_addressed_artifact", "not_applicable", SkillContext, SelectionOut),
    ToolContract("product", "skill", ("copywriter",), "none", "auto", 5.0, "content_addressed_artifact", "not_applicable", SkillContext, BriefDraft),
    ToolContract("review", "skill", ("compliance",), "none", "auto", _DB_TIMEOUT, "content_addressed_artifact", "not_applicable", SkillContext, ReviewOut),
    ToolContract("assemble", "skill", ("director",), "none", "auto", _DB_TIMEOUT, "content_addressed_artifact", "not_applicable", SkillContext, SummaryOut),
)}


def check_catalog(commands: dict[str, ToolContract], skills: dict[str, ToolContract]) -> None:
    """Registration-time invariants; a contract that violates them must not be loadable."""
    problems = []
    for contract in [*commands.values(), *skills.values()]:
        fields = contract.input_model.model_fields
        required = {n for n, f in fields.items() if f.is_required()}
        if not 0 < contract.timeout_seconds <= 30:
            problems.append(f"{contract.name}: timeout out of range")
        if contract.side_effect == "commerce":
            if contract.risk == "auto":
                problems.append(f"{contract.name}: commerce write cannot be auto")
            if not {"sku", "expected_live_version", "expected_product_version"} <= required:
                problems.append(f"{contract.name}: commerce write must bind SKU and versions")
        if contract.access == "write" and contract.idempotency != "action_ledger":
            problems.append(f"{contract.name}: write requires ledger idempotency")
        if contract.access == "read" and (contract.risk != "auto" or contract.idempotency == "action_ledger"):
            problems.append(f"{contract.name}: read tool declares write handling")
        if contract.uncertain_outcome == "reconcile_before_retry" and contract.side_effect != "commerce":
            problems.append(f"{contract.name}: reconciliation is only for commerce writes")
    for name, contract in commands.items():
        literal = contract.input_model.model_fields["type"].annotation
        if contract.surface != "command" or name not in getattr(literal, "__args__", ()):
            problems.append(f"{name}: input type literal mismatch")
    for contract in skills.values():
        # The preparation DAG is read-only by construction.
        if contract.surface != "skill" or contract.access != "read" or len(contract.roles) != 1:
            problems.append(f"{contract.name}: skill must be read-only and single-role")
    if problems:
        raise ValueError("; ".join(problems))


check_catalog(COMMAND_CONTRACTS, SKILL_CONTRACTS)


def validate(model: type[BaseModel], value: Any, code: str) -> BaseModel:
    try:
        return model.model_validate(value)
    except ValidationError as exc:
        fields = ",".join(sorted({".".join(map(str, e["loc"])) or "root" for e in exc.errors()}))[:160]
        raise BusinessError(code, f"工具契约校验失败：{fields}") from exc


def catalog() -> list[dict[str, Any]]:
    return [c.describe() for c in [*COMMAND_CONTRACTS.values(), *SKILL_CONTRACTS.values()]]
