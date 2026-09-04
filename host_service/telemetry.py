from __future__ import annotations

import json
import logging
import re
import secrets
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator


_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SAFE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}")
_CURRENT_TRACE: ContextVar[OperationTrace | None] = ContextVar(
    "xuanji_operation_trace", default=None
)


@dataclass
class OperationTrace:
    operation_id: str
    task_id: str
    phase: str
    boundary_owned: bool
    analysis_profile: str | None = None
    stage: str = "tool_dispatch"
    query_counts: dict[str, int] = field(
        default_factory=lambda: {"root": 0, "attribution": 0}
    )
    last_query_bucket: str | None = None
    last_query_ordinal: int | None = None
    last_query_stage: str | None = None
    last_query_step: str | None = None
    last_query_attempt: int | None = None
    last_query_run_id: str | None = None
    investigation_id: str | None = None
    task_status: str | None = None
    current_investigation_index: int | None = None
    investigation_count: int | None = None
    investigation_status: str | None = None
    root_snapshot_reused: bool | None = None
    root_snapshot_count: int | None = None
    error_id: str | None = None
    failure_stage: str | None = None
    exception_type: str | None = None
    exception_types: list[str] = field(default_factory=list)
    exception_leaf_types: list[str] = field(default_factory=list)
    exception_group_depth: int = 0
    diagnostic_bundle_status: str | None = None
    diagnostic_bundle_file: str | None = None
    diagnostic_bundle_error_type: str | None = None
    private_tool_input: Any = field(default=None, repr=False)
    private_last_query_request: Any = field(default=None, repr=False)
    private_last_query_transport_response: Any = field(default=None, repr=False)
    private_last_query_transport_response_received: bool = field(
        default=False, repr=False
    )
    private_last_query_response: Any = field(default=None, repr=False)
    private_last_query_response_received: bool = field(default=False, repr=False)

    @classmethod
    def create(
        cls,
        *,
        task_id: str,
        phase: str,
        boundary_owned: bool,
        analysis_profile: str | None = None,
    ) -> OperationTrace:
        return cls(
            operation_id=f"op-{secrets.token_hex(8)}",
            task_id=_safe_id(task_id),
            phase=_safe_name(phase),
            boundary_owned=boundary_owned,
            analysis_profile=(
                _safe_name(analysis_profile) if analysis_profile is not None else None
            ),
        )

    def set_stage(self, stage: str) -> None:
        self.stage = _safe_name(stage)

    def start_query(
        self,
        *,
        bucket: str,
        stage: str | None,
        step_id: str | None,
        attempt_no: int | None,
        run_id: str | None,
    ) -> int:
        safe_bucket = _safe_name(bucket)
        self.query_counts[safe_bucket] = self.query_counts.get(safe_bucket, 0) + 1
        self.last_query_bucket = safe_bucket
        self.last_query_ordinal = self.query_counts[safe_bucket]
        self.last_query_stage = _optional_safe_name(stage)
        self.last_query_step = _optional_safe_name(step_id)
        self.last_query_attempt = attempt_no if isinstance(attempt_no, int) else None
        self.last_query_run_id = _optional_safe_id(run_id)
        self.private_last_query_request = None
        self.private_last_query_transport_response = None
        self.private_last_query_transport_response_received = False
        self.private_last_query_response = None
        self.private_last_query_response_received = False
        self.set_stage("dview_query")
        return self.last_query_ordinal

    def capture_tool_input(self, value: Any) -> None:
        self.private_tool_input = value

    def capture_query_request(self, value: Any) -> None:
        self.private_last_query_request = value

    def capture_query_transport_response(self, value: Any) -> None:
        self.private_last_query_transport_response = value
        self.private_last_query_transport_response_received = True

    def capture_query_response(self, value: Any) -> None:
        self.private_last_query_response = value
        self.private_last_query_response_received = True

    def capture_failure(self, exc: BaseException) -> None:
        if self.error_id is not None:
            return
        diagnostic = exception_diagnostic(exc)
        self.error_id = f"err-{secrets.token_hex(8)}"
        self.failure_stage = self.stage
        self.exception_type = diagnostic["exception_type"]
        self.exception_types = diagnostic["exception_types"]
        self.exception_leaf_types = diagnostic["exception_leaf_types"]
        self.exception_group_depth = diagnostic["exception_group_depth"]

    def capture_named_failure(self, exception_type: str) -> None:
        if self.error_id is not None:
            return
        safe_type = _safe_name(exception_type)
        self.error_id = f"err-{secrets.token_hex(8)}"
        self.failure_stage = self.stage
        self.exception_type = safe_type
        self.exception_types = [safe_type]
        self.exception_leaf_types = [safe_type]

    def fields(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "error_id": self.error_id,
            "task_id": self.task_id,
            "phase": self.phase,
            "analysis_profile": self.analysis_profile,
            "stage": self.stage,
            "failure_stage": self.failure_stage,
            "root_query_count": self.query_counts.get("root", 0),
            "attribution_query_count": self.query_counts.get("attribution", 0),
            "last_query_bucket": self.last_query_bucket,
            "last_query_ordinal": self.last_query_ordinal,
            "last_query_stage": self.last_query_stage,
            "last_query_step": self.last_query_step,
            "last_query_attempt": self.last_query_attempt,
            "last_query_run_id": self.last_query_run_id,
            "investigation_id": self.investigation_id,
            "task_status": self.task_status,
            "current_investigation_index": self.current_investigation_index,
            "investigation_count": self.investigation_count,
            "investigation_status": self.investigation_status,
            "root_snapshot_reused": self.root_snapshot_reused,
            "root_snapshot_count": self.root_snapshot_count,
            "exception_type": self.exception_type,
            "exception_types": list(self.exception_types),
            "exception_leaf_types": list(self.exception_leaf_types),
            "exception_group_depth": self.exception_group_depth,
            "diagnostic_bundle_status": self.diagnostic_bundle_status,
            "diagnostic_bundle_file": self.diagnostic_bundle_file,
            "diagnostic_bundle_error_type": self.diagnostic_bundle_error_type,
        }


def current_trace() -> OperationTrace | None:
    return _CURRENT_TRACE.get()


@contextmanager
def bind_trace(trace: OperationTrace) -> Iterator[OperationTrace]:
    token = _CURRENT_TRACE.set(trace)
    try:
        yield trace
    finally:
        _CURRENT_TRACE.reset(token)


def emit_event(
    logger: logging.Logger,
    level: int,
    event: str,
    *,
    trace: OperationTrace,
    **fields: Any,
) -> None:
    payload = {
        "schema_version": 1,
        "event": _safe_name(event),
        **trace.fields(),
        **fields,
    }
    logger.log(
        level,
        "xuanji_event %s",
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
    )


def emit_service_event(
    logger: logging.Logger,
    level: int,
    event: str,
    *,
    analysis_profile: str | None = None,
    exc: BaseException | None = None,
) -> str | None:
    error_id = f"err-{secrets.token_hex(8)}" if exc is not None else None
    payload = {
        "schema_version": 1,
        "event": _safe_name(event),
        "error_id": error_id,
        "analysis_profile": _optional_safe_name(analysis_profile),
        "exception_type": None,
        "exception_types": [],
        "exception_leaf_types": [],
        "exception_group_depth": 0,
    }
    if exc is not None:
        payload.update(exception_diagnostic(exc))
    logger.log(
        level,
        "xuanji_service %s",
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
    )
    return error_id


def exception_diagnostic(exc: BaseException) -> dict[str, Any]:
    types: list[str] = []
    leaves: list[str] = []
    seen: set[int] = set()
    group_depth = 0

    def visit(current: BaseException, depth: int) -> None:
        nonlocal group_depth
        if id(current) in seen or len(types) >= 24 or depth > 8:
            return
        seen.add(id(current))
        name = _safe_name(type(current).__name__)
        if name not in types:
            types.append(name)
        children = getattr(current, "exceptions", None)
        if isinstance(children, tuple) and children:
            group_depth = max(group_depth, depth + 1)
            for child in children[:16]:
                if isinstance(child, BaseException):
                    visit(child, depth + 1)
            return
        if name not in leaves:
            leaves.append(name)
        chained = current.__cause__
        if chained is None and not current.__suppress_context__:
            chained = current.__context__
        if isinstance(chained, BaseException):
            visit(chained, depth + 1)

    visit(exc, 0)
    root_type = _safe_name(type(exc).__name__)
    return {
        "exception_type": root_type,
        "exception_types": types or [root_type],
        "exception_leaf_types": leaves or [root_type],
        "exception_group_depth": group_depth,
    }


def exception_type_name(exc: BaseException) -> str:
    """Return only a bounded class name, never exception text or provenance."""
    return _safe_name(type(exc).__name__)


def _safe_id(value: Any) -> str:
    return value if isinstance(value, str) and _SAFE_ID.fullmatch(value) else "invalid"


def _safe_name(value: Any) -> str:
    return value if isinstance(value, str) and _SAFE_NAME.fullmatch(value) else "invalid"


def _optional_safe_name(value: Any) -> str | None:
    if value is None:
        return None
    return _safe_name(value)


def _optional_safe_id(value: Any) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) and _SAFE_ID.fullmatch(value) else "invalid"
