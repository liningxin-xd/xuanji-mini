from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class StepStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    REPAIR_REQUIRED = "repair_required"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED_NOT_APPLICABLE = "skipped_not_applicable"


class RunStatus(str, Enum):
    ACTIVE = "active"
    QUEUE_COMPLETE = "queue_complete"
    FINALIZED = "finalized"


TERMINAL_STEP_STATUSES = {
    StepStatus.SUCCEEDED.value,
    StepStatus.FAILED.value,
    StepStatus.SKIPPED_NOT_APPLICABLE.value,
}


@dataclass(frozen=True)
class PlanStep:
    id: str
    kind: str
    produces_candidates: bool
    failure_scope: str
    automatic_status: str | None = None
    automatic_reason: str | None = None


@dataclass(frozen=True)
class ExecutionPlan:
    id: str
    chain: str
    allowed_game_types: tuple[str, ...]
    allowed_metrics: tuple[str, ...]
    steps: tuple[PlanStep, ...]
    sha256: str


@dataclass(frozen=True)
class QueryBinding:
    asset_path: str
    asset_sha256: str
    asset_kind: str
    data_sources: tuple[str, ...]
    protected_tokens: tuple[str, ...]
    required_predicates: tuple[str, ...]
    result_schema_id: str
    dimension: str | None = None
    dimension_config: dict[str, Any] | None = None


@dataclass(frozen=True)
class BuiltQuery:
    sql: str
    sha256: str
    parameters: dict[str, Any]
    binding: QueryBinding
