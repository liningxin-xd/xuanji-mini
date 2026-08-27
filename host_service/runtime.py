from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from host_integration import PrimaryInvestigationHost

from .config import HostServiceSettings
from .dview_client import DViewMCPClient
from .sink import FileValidatedResultSink

ROOT = Path(__file__).resolve().parents[1]


class XuanjiHostRuntime:
    def __init__(
        self,
        settings: HostServiceSettings,
        *,
        dview_client: DViewMCPClient | None = None,
        validated_result_sink: FileValidatedResultSink | None = None,
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
        self._repository_root = Path(repository_root)
        self._run_locks: dict[str, asyncio.Lock] = {}

    async def run_investigation(self, **kwargs: Any) -> dict[str, Any]:
        run_id = str(kwargs.get("run_id", ""))
        return await self._with_dview(
            run_id,
            lambda host: host.xuanji_run_investigation(**kwargs),
        )

    async def submit_repair(self, **kwargs: Any) -> dict[str, Any]:
        run_id = str(kwargs.get("run_id", ""))
        return await self._with_dview(
            run_id,
            lambda host: host.xuanji_submit_repair(**kwargs),
        )

    async def finalize(self, **kwargs: Any) -> dict[str, Any]:
        run_id = str(kwargs.get("run_id", ""))
        async with self._lock_for(run_id):
            host = self._build_host(_unexpected_query)
            return await asyncio.to_thread(host.xuanji_finalize, **kwargs)

    async def _with_dview(
        self,
        run_id: str,
        operation: Callable[[PrimaryInvestigationHost], dict[str, Any]],
    ) -> dict[str, Any]:
        async with self._lock_for(run_id), self._dview_client.session() as session:
            loop = asyncio.get_running_loop()

            def query(**kwargs: Any) -> Any:
                future = asyncio.run_coroutine_threadsafe(
                    session.query(**kwargs),
                    loop,
                )
                return future.result()

            host = self._build_host(query)
            return await asyncio.to_thread(operation, host)

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


def _unexpected_query(**_: Any) -> Any:
    raise RuntimeError("finalization attempted an unexpected DView query")
