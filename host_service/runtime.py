from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from host_integration import PrimaryInvestigationHost
from runtime.host_adapter import ProductionDViewExecutor
from runtime.root_preflight import RootPreflight
from runtime.task_coordinator import RegisteredAlertCoordinator

from .config import HostServiceSettings
from .dview_client import DViewMCPClient
from .sink import FileTaskResultSink, FileValidatedResultSink

ROOT = Path(__file__).resolve().parents[1]


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
            lambda coordinator: coordinator.run_task(**kwargs),
        )

    async def submit_repair(self, **kwargs: Any) -> dict[str, Any]:
        task_id = str(kwargs.get("task_id", ""))
        return await self._with_dview(
            task_id,
            lambda coordinator: coordinator.submit_repair(**kwargs),
        )

    async def finalize(self, **kwargs: Any) -> dict[str, Any]:
        task_id = str(kwargs.get("task_id", ""))
        return await self._with_dview(
            task_id,
            lambda coordinator: coordinator.finalize(**kwargs),
        )

    async def _with_dview(
        self,
        task_id: str,
        operation: Callable[[RegisteredAlertCoordinator], dict[str, Any]],
    ) -> dict[str, Any]:
        async with self._lock_for(task_id), self._dview_client.session() as session:
            loop = asyncio.get_running_loop()

            def query(**kwargs: Any) -> Any:
                future = asyncio.run_coroutine_threadsafe(
                    session.query(**kwargs),
                    loop,
                )
                return future.result()

            coordinator = self._build_coordinator(query)
            return await asyncio.to_thread(operation, coordinator)

    def _build_coordinator(
        self, dview_query: Callable[..., Any]
    ) -> RegisteredAlertCoordinator:
        executor = ProductionDViewExecutor(dview_query)
        return RegisteredAlertCoordinator(
            investigation_host=self._build_host(dview_query),
            root_preflight=RootPreflight(
                executor=executor,
                repository_root=self._repository_root,
            ),
            run_result_store=self._sink,
            task_result_sink=self._task_sink,
            task_result_store=self._task_sink,
            tasks_root=self._settings.tasks_root,
            repository_root=self._repository_root,
        )

    def _build_host(self, dview_query: Callable[..., Any]) -> PrimaryInvestigationHost:
        return PrimaryInvestigationHost(
            dview_query=dview_query,
            receipt_key_id=self._settings.receipt_key_id,
            receipt_secret=self._settings.receipt_secret,
            runs_root=self._settings.runs_root,
            validated_result_sink=self._sink,
            repository_root=self._repository_root,
        )

    def _lock_for(self, run_id: str) -> asyncio.Lock:
        return self._run_locks.setdefault(run_id, asyncio.Lock())
