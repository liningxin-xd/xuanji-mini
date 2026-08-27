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
from runtime.root_preflight import RootPreflight
from runtime.task_assembler import writer_pack_size
from runtime.task_coordinator import RegisteredAlertCoordinator

from .config import HostServiceSettings
from .dview_client import DViewMCPClient
from .sink import FileTaskResultSink, FileValidatedResultSink

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
        started = time.monotonic()
        query_counts = {"root": 0, "attribution": 0}
        async with self._lock_for(task_id), self._dview_client.session() as session:
            loop = asyncio.get_running_loop()

            def query_for(bucket: str) -> Callable[..., Any]:
                def query(**kwargs: Any) -> Any:
                    query_counts[bucket] += 1
                    future = asyncio.run_coroutine_threadsafe(
                        session.query(**kwargs),
                        loop,
                    )
                    return future.result()

                return query

            coordinator = self._build_coordinator(
                root_query=query_for("root"),
                attribution_query=query_for("attribution"),
            )
            result = await asyncio.to_thread(operation, coordinator)
        self._log_operation(
            task_id=task_id,
            phase=phase,
            requested_investigation_id=investigation_id,
            result=result,
            query_counts=query_counts,
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        return result

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
        task_id: str,
        phase: str,
        requested_investigation_id: str | None,
        result: dict[str, Any],
        query_counts: dict[str, int],
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
            "task_id": task_id if _SAFE_ID.fullmatch(task_id) else "invalid",
            "investigation_id": (
                investigation_id
                if isinstance(investigation_id, str)
                and _SAFE_ID.fullmatch(investigation_id)
                else None
            ),
            "phase": phase,
            "duration_ms": duration_ms,
            "root_query_count": query_counts["root"],
            "attribution_query_count": query_counts["attribution"],
            "root_snapshot_reused": state_observation.get(
                "root_snapshot_reused", False
            ),
            "writer_pack_bytes": writer_bytes,
            "investigation_status": investigation_status,
            "overall_status": result.get("overall_status"),
            "exception_type": None,
        }
        _LOGGER.info(
            "xuanji_operation %s",
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
        )

    def _state_observation(
        self,
        task_id: str,
        investigation_id: Any,
    ) -> dict[str, Any]:
        if (
            _SAFE_ID.fullmatch(task_id) is None
            or not isinstance(investigation_id, str)
            or _SAFE_ID.fullmatch(investigation_id) is None
        ):
            return {}
        try:
            state = json.loads(
                (self._settings.tasks_root / task_id / "state.json").read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, json.JSONDecodeError):
            return {}
        investigations = state.get("investigations")
        if not isinstance(investigations, list):
            return {}
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
            return {}
        preflight = investigation.get("root_preflight")
        result = investigation.get("result")
        return {
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
