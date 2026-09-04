from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from host_integration import PrimaryInvestigationHost
from runtime.host_adapter import ProductionDViewExecutor
from runtime.query_observation import current_query_observation
from runtime.root_preflight import RootPreflight
from runtime.task_assembler import writer_pack_size
from runtime.task_coordinator import RegisteredAlertCoordinator

from .config import HostServiceSettings
from .dview_client import DViewMCPClient
from .pipeline_handoff import PipelineHandoffError, PipelineHandoffSigner
from .sink import FileTaskResultSink, FileValidatedResultSink
from .telemetry import (
    OperationTrace,
    bind_trace,
    current_trace,
    emit_event,
    exception_diagnostic,
    exception_type_name,
)

ROOT = Path(__file__).resolve().parents[1]
_LOGGER = logging.getLogger(__name__)
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class XuanjiHostRuntime:
    def __init__(
        self,
        settings: HostServiceSettings,
        *,
        dview_client: DViewMCPClient | None = None,
        validated_result_sink: FileValidatedResultSink | None = None,
        task_result_sink: FileTaskResultSink | None = None,
        repository_root: Path | str = ROOT,
    ):
        self._settings = settings
        self._dview_client = dview_client or DViewMCPClient(
            url=settings.dview_mcp_url,
            bearer_token=settings.dview_bearer_token,
            read_timeout_seconds=settings.dview_read_timeout_seconds,
        )
        self._sink = validated_result_sink or FileValidatedResultSink(
            settings.results_root
        )
        self._task_sink = task_result_sink or FileTaskResultSink(
            settings.results_root / "tasks"
        )
        self._pipeline_handoff_signer = PipelineHandoffSigner(
            receipt_key_id=settings.receipt_key_id,
            receipt_secret=settings.receipt_secret,
        )
        self._repository_root = Path(repository_root)
        self._run_locks: dict[str, asyncio.Lock] = {}

    async def run_task(self, **kwargs: Any) -> dict[str, Any]:
        task_id = str(kwargs.get("task_id", ""))
        return await self._with_dview(
            task_id,
            phase="run_task",
            investigation_id=None,
            operation=lambda coordinator: coordinator.run_task(**kwargs),
        )

    async def submit_repair(self, **kwargs: Any) -> dict[str, Any]:
        task_id = str(kwargs.get("task_id", ""))
        return await self._with_dview(
            task_id,
            phase="submit_repair",
            investigation_id=str(kwargs.get("investigation_id", "")),
            operation=lambda coordinator: coordinator.submit_repair(**kwargs),
        )

    async def finalize(self, **kwargs: Any) -> dict[str, Any]:
        task_id = str(kwargs.get("task_id", ""))
        return await self._with_dview(
            task_id,
            phase="finalize",
            investigation_id=str(kwargs.get("investigation_id", "")),
            operation=lambda coordinator: coordinator.finalize(**kwargs),
        )

    async def _with_dview(
        self,
        task_id: str,
        *,
        phase: str,
        investigation_id: str | None,
        operation: Callable[[RegisteredAlertCoordinator], dict[str, Any]],
    ) -> dict[str, Any]:
        trace = current_trace()
        if trace is None:
            trace = OperationTrace.create(
                task_id=task_id,
                phase=phase,
                boundary_owned=False,
                analysis_profile=self._settings.analysis_profile,
            )
            with bind_trace(trace):
                emit_event(_LOGGER, logging.INFO, "operation_started", trace=trace)
                return await self._execute_with_dview(
                    trace=trace,
                    task_id=task_id,
                    phase=phase,
                    investigation_id=investigation_id,
                    operation=operation,
                )
        trace.analysis_profile = self._settings.analysis_profile
        return await self._execute_with_dview(
            trace=trace,
            task_id=task_id,
            phase=phase,
            investigation_id=investigation_id,
            operation=operation,
        )

    async def _execute_with_dview(
        self,
        *,
        trace: OperationTrace,
        task_id: str,
        phase: str,
        investigation_id: str | None,
        operation: Callable[[RegisteredAlertCoordinator], dict[str, Any]],
    ) -> dict[str, Any]:
        started = time.monotonic()
        trace.set_stage("task_lock_wait")
        try:
            async with self._lock_for(task_id):
                trace.set_stage("dview_session_open")
                async with self._dview_client.session() as session:
                    loop = asyncio.get_running_loop()

                    def query_for(bucket: str) -> Callable[..., Any]:
                        def query(**kwargs: Any) -> Any:
                            observation = current_query_observation()
                            ordinal = trace.start_query(
                                bucket=bucket,
                                stage=(observation.stage if observation is not None else None),
                                step_id=(
                                    observation.step_id if observation is not None else None
                                ),
                                attempt_no=(
                                    observation.attempt_no
                                    if observation is not None
                                    else None
                                ),
                                run_id=(
                                    observation.run_id
                                    if observation is not None
                                    else None
                                ),
                            )
                            trace.capture_query_request(dict(kwargs))
                            query_started = time.monotonic()
                            emit_event(
                                _LOGGER,
                                logging.INFO,
                                "query_started",
                                trace=trace,
                                query_bucket=bucket,
                                query_ordinal=ordinal,
                                query_stage=trace.last_query_stage,
                                query_step=trace.last_query_step,
                                query_attempt=trace.last_query_attempt,
                            )
                            future = asyncio.run_coroutine_threadsafe(
                                session.query(**kwargs),
                                loop,
                            )
                            try:
                                response = future.result()
                            except Exception as exc:
                                trace.set_stage("dview_query_response")
                                emit_event(
                                    _LOGGER,
                                    logging.ERROR,
                                    "query_failed",
                                    trace=trace,
                                    query_bucket=bucket,
                                    query_ordinal=ordinal,
                                    query_stage=trace.last_query_stage,
                                    query_step=trace.last_query_step,
                                    query_attempt=trace.last_query_attempt,
                                    duration_ms=round(
                                        (time.monotonic() - query_started) * 1000
                                    ),
                                    **exception_diagnostic(exc),
                                )
                                raise
                            trace.capture_query_response(response)
                            trace.set_stage("query_result_processing")
                            emit_event(
                                _LOGGER,
                                logging.INFO,
                                "query_succeeded",
                                trace=trace,
                                query_bucket=bucket,
                                query_ordinal=ordinal,
                                query_stage=trace.last_query_stage,
                                query_step=trace.last_query_step,
                                query_attempt=trace.last_query_attempt,
                                duration_ms=round(
                                    (time.monotonic() - query_started) * 1000
                                ),
                            )
                            return response

                        return query

                    trace.set_stage("coordinator_build")
                    coordinator = self._build_coordinator(
                        root_query=query_for("root"),
                        attribution_query=query_for("attribution"),
                    )
                    trace.set_stage("coordinator_execute")
                    try:
                        result = await asyncio.to_thread(operation, coordinator)
                        if result.get("action") == "task_complete":
                            trace.set_stage("pipeline_handoff")
                            result = self._attach_pipeline_handoff(task_id, result)
                    except Exception as exc:
                        trace.capture_failure(exc)
                        raise
        except Exception as exc:
            trace.capture_failure(exc)
            self._enrich_trace_from_state(trace, task_id, investigation_id)
            if not trace.boundary_owned:
                self._log_operation_failure(
                    trace=trace,
                    task_id=task_id,
                    requested_investigation_id=investigation_id,
                    duration_ms=round((time.monotonic() - started) * 1000),
                    outer_exception=exc,
                )
            raise
        trace.set_stage("operation_complete")
        self._log_operation(
            trace=trace,
            task_id=task_id,
            phase=phase,
            requested_investigation_id=investigation_id,
            result=result,
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        return result

    def _enrich_trace_from_state(
        self,
        trace: OperationTrace,
        task_id: str,
        investigation_id: str | None,
    ) -> None:
        observation = self._state_observation(task_id, investigation_id)
        trace.investigation_id = observation.get("investigation_id")
        trace.task_status = observation.get("task_status")
        trace.current_investigation_index = observation.get(
            "current_investigation_index"
        )
        trace.investigation_count = observation.get("investigation_count")
        trace.investigation_status = observation.get("investigation_status")
        trace.root_snapshot_reused = observation.get("root_snapshot_reused")
        trace.root_snapshot_count = observation.get("root_snapshot_count")

    def _attach_pipeline_handoff(
        self,
        task_id: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        artifact = self._task_sink.load(task_id)
        preview, handoff = self._pipeline_handoff_signer.build(
            task_id=task_id,
            artifact=artifact,
        )
        receipt = result.get("validation_receipt")
        if result.get("analysis_preview") != preview or not isinstance(receipt, dict):
            raise PipelineHandoffError(
                "task response no longer matches the authoritative task sink"
            )
        if receipt.get("validation_receipt_sha256") != handoff.get(
            "validation_receipt_sha256"
        ):
            raise PipelineHandoffError(
                "task response receipt no longer matches the task sink"
            )
        return {**result, "pipeline_handoff": handoff}

    def _build_coordinator(
        self,
        *,
        root_query: Callable[..., Any],
        attribution_query: Callable[..., Any],
    ) -> RegisteredAlertCoordinator:
        executor = ProductionDViewExecutor(root_query)
        return RegisteredAlertCoordinator(
            investigation_host=self._build_host(attribution_query),
            root_preflight=RootPreflight(
                executor=executor,
                repository_root=self._repository_root,
            ),
            run_result_store=self._sink,
            task_result_sink=self._task_sink,
            task_result_store=self._task_sink,
            tasks_root=self._settings.tasks_root,
            repository_root=self._repository_root,
            analysis_profile=self._settings.analysis_profile,
        )

    def _build_host(self, dview_query: Callable[..., Any]) -> PrimaryInvestigationHost:
        return PrimaryInvestigationHost(
            dview_query=dview_query,
            receipt_key_id=self._settings.receipt_key_id,
            receipt_secret=self._settings.receipt_secret,
            runs_root=self._settings.runs_root,
            validated_result_sink=self._sink,
            repository_root=self._repository_root,
            analysis_profile=self._settings.analysis_profile,
        )

    def _lock_for(self, run_id: str) -> asyncio.Lock:
        return self._run_locks.setdefault(run_id, asyncio.Lock())

    def _log_operation(
        self,
        *,
        trace: OperationTrace,
        task_id: str,
        phase: str,
        requested_investigation_id: str | None,
        result: dict[str, Any],
        duration_ms: int,
    ) -> None:
        investigation_id = result.get("investigation_id") or requested_investigation_id
        state_observation = self._state_observation(task_id, investigation_id)
        writer_pack = result.get("writer_pack")
        writer_bytes = (
            writer_pack_size(writer_pack) if isinstance(writer_pack, dict) else 0
        )
        investigation_status = state_observation.get("investigation_status")
        if investigation_status is None and isinstance(writer_pack, dict):
            investigation_status = writer_pack.get("result_status_hint")
        payload = {
            "schema_version": 3,
            "operation_id": trace.operation_id,
            "error_id": None,
            "task_id": task_id if _SAFE_ID.fullmatch(task_id) else "invalid",
            "investigation_id": (
                investigation_id
                if isinstance(investigation_id, str)
                and _SAFE_ID.fullmatch(investigation_id)
                else None
            ),
            "phase": phase,
            "analysis_profile": self._settings.analysis_profile,
            "stage": trace.stage,
            "failure_stage": None,
            "duration_ms": duration_ms,
            "root_query_count": trace.query_counts["root"],
            "attribution_query_count": trace.query_counts["attribution"],
            "root_snapshot_reused": state_observation.get(
                "root_snapshot_reused", False
            ),
            "root_snapshot_count": state_observation.get("root_snapshot_count"),
            "writer_pack_bytes": writer_bytes,
            "investigation_status": investigation_status,
            "overall_status": result.get("overall_status"),
            "exception_type": None,
            "exception_wrapper_type": None,
            "exception_types": [],
            "exception_leaf_types": [],
            "exception_group_depth": 0,
            "last_query_bucket": trace.last_query_bucket,
            "last_query_ordinal": trace.last_query_ordinal,
            "last_query_stage": trace.last_query_stage,
            "last_query_step": trace.last_query_step,
            "last_query_attempt": trace.last_query_attempt,
            "last_query_run_id": trace.last_query_run_id,
            "task_status": state_observation.get("task_status"),
            "current_investigation_index": state_observation.get(
                "current_investigation_index"
            ),
            "investigation_count": state_observation.get("investigation_count"),
        }
        _LOGGER.info(
            "xuanji_operation %s",
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
        )

    def _log_operation_failure(
        self,
        *,
        trace: OperationTrace,
        task_id: str,
        requested_investigation_id: str | None,
        duration_ms: int,
        outer_exception: BaseException,
    ) -> None:
        state_observation = self._state_observation(
            task_id, requested_investigation_id
        )
        payload = {
            "schema_version": 3,
            **trace.fields(),
            "duration_ms": duration_ms,
            "investigation_id": state_observation.get("investigation_id"),
            "root_snapshot_reused": state_observation.get(
                "root_snapshot_reused", False
            ),
            "root_snapshot_count": state_observation.get("root_snapshot_count"),
            "writer_pack_bytes": 0,
            "investigation_status": state_observation.get("investigation_status"),
            "overall_status": None,
            "task_status": state_observation.get("task_status"),
            "current_investigation_index": state_observation.get(
                "current_investigation_index"
            ),
            "investigation_count": state_observation.get("investigation_count"),
            "exception_wrapper_type": exception_type_name(outer_exception),
        }
        _LOGGER.error(
            "xuanji_operation %s",
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
        )

    def _state_observation(
        self,
        task_id: str,
        investigation_id: Any,
    ) -> dict[str, Any]:
        if _SAFE_ID.fullmatch(task_id) is None:
            return {}
        try:
            state = json.loads(
                (self._settings.tasks_root / task_id / "state.json").read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, json.JSONDecodeError):
            return {}
        snapshot_root = self._settings.tasks_root / task_id / "root-snapshots"
        try:
            root_snapshot_count = sum(
                1
                for path in snapshot_root.iterdir()
                if path.suffix == ".json" and path.is_file()
            )
        except OSError:
            root_snapshot_count = 0
        investigations = state.get("investigations")
        if not isinstance(investigations, list):
            return {}
        if not isinstance(investigation_id, str) or _SAFE_ID.fullmatch(
            investigation_id
        ) is None:
            index = state.get("current_investigation_index")
            investigation = (
                investigations[index]
                if isinstance(index, int) and 0 <= index < len(investigations)
                else None
            )
        else:
            investigation = next(
                (
                    item
                    for item in investigations
                    if isinstance(item, dict)
                    and item.get("investigation_id") == investigation_id
                ),
                None,
            )
        if not isinstance(investigation, dict):
            return {
                "task_status": state.get("status"),
                "current_investigation_index": state.get(
                    "current_investigation_index"
                ),
                "investigation_count": len(investigations),
                "root_snapshot_count": root_snapshot_count,
            }
        preflight = investigation.get("root_preflight")
        result = investigation.get("result")
        return {
            "task_status": state.get("status"),
            "current_investigation_index": state.get("current_investigation_index"),
            "investigation_count": len(investigations),
            "investigation_id": investigation.get("investigation_id"),
            "root_snapshot_count": root_snapshot_count,
            "root_snapshot_reused": (
                preflight.get("root_snapshot_reused", False)
                if isinstance(preflight, dict)
                else False
            ),
            "investigation_status": (
                result.get("status")
                if isinstance(result, dict)
                else investigation.get("result_status")
            ),
        }
