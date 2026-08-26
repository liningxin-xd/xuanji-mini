import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from host_service.auth import StaticBearerTokenVerifier
from host_service.config import HostConfigurationError, HostServiceSettings
from host_service.dview_client import (
    DViewMCPResponseError,
    DViewQuerySession,
)
from host_service.runtime import XuanjiHostRuntime
from host_service.sink import FileValidatedResultSink
from host_service.tools import create_mcp
from runtime.host_adapter import DViewExecutionError
from runtime.receipts import TrustedReceiptVerifier
from runtime.runner import AttributionRunner
from tests.runtime_result_fixtures import raw_result_for_ticket


def _settings(root: Path) -> HostServiceSettings:
    return HostServiceSettings(
        public_url="http://127.0.0.1:8091",
        bind_host="127.0.0.1",
        port=8091,
        host_bearer_token="h" * 32,
        dview_mcp_url="https://dview.example.test/mcp/query",
        dview_bearer_token="d" * 32,
        dview_read_timeout_seconds=660,
        receipt_key_id="native-host-test",
        receipt_secret=b"r" * 32,
        runs_root=root / "runs",
        results_root=root / "results",
    )


class _FakeRuntime:
    async def run_investigation(self, **kwargs):
        return {
            "action": "write_conclusion",
            "run_id": kwargs["run_id"],
            "executed_query_count": 7,
            "writer_pack": {"status": "no_dominant_slice"},
        }

    async def submit_repair(self, **kwargs):
        return {
            "action": "write_conclusion",
            "run_id": kwargs["run_id"],
            "executed_query_count": 6,
            "writer_pack": {"status": "completed"},
        }

    async def finalize(self, **kwargs):
        return {
            "action": "finalized",
            "run_id": kwargs["run_id"],
            "analysis_preview": {"overall_status": "completed"},
            "validation_receipt": {"status": "valid"},
            "audit_detail": "retained_by_host",
        }


class _FixtureDViewClient:
    def __init__(self, settings: HostServiceSettings, repository_root: Path):
        self._settings = settings
        self._repository_root = repository_root
        self._query_counts: dict[str, int] = {}

    @asynccontextmanager
    async def session(self):
        yield self

    async def query(self, *, sql, database_type, limit):
        self.assert_query_contract(database_type, limit)
        signer = TrustedReceiptVerifier(
            key_id=self._settings.receipt_key_id,
            secret=self._settings.receipt_secret,
        )
        runner = AttributionRunner(
            self._repository_root,
            runs_root=self._settings.runs_root,
            trusted_receipt_verifier=signer,
        )
        for run_root in self._settings.runs_root.iterdir():
            ticket = runner.next_action(run_root.name)
            if ticket.get("rendered_sql") != sql:
                continue
            count = self._query_counts.get(run_root.name, 0) + 1
            self._query_counts[run_root.name] = count
            raw_result = raw_result_for_ticket(runner, run_root.name, ticket)
            return {
                "query_id": f"private-{run_root.name}-{count}",
                "columns": raw_result["columns"],
                "rows": raw_result["rows"],
            }
        raise AssertionError("issued SQL was not bound to an active run")

    @staticmethod
    def assert_query_contract(database_type, limit):
        if database_type != "MaxCompute" or limit != 250:
            raise AssertionError("native Host changed the DView query contract")


class NativeHostConfigurationTest(unittest.IsolatedAsyncioTestCase):
    def test_environment_is_fail_closed_and_secrets_are_not_represented(self):
        with self.assertRaisesRegex(
            HostConfigurationError,
            "XUANJI_HOST_PUBLIC_URL is required",
        ):
            HostServiceSettings.from_env({})

        values = {
            "XUANJI_HOST_PUBLIC_URL": "http://127.0.0.1:8091",
            "XUANJI_HOST_BEARER_TOKEN": "host-secret-" + "h" * 32,
            "XUANJI_DVIEW_MCP_URL": "https://dview.example.test/mcp/query",
            "XUANJI_DVIEW_BEARER_TOKEN": "dview-secret-" + "d" * 16,
            "XUANJI_RECEIPT_KEY_ID": "receipt-v1",
            "XUANJI_RECEIPT_SECRET": "receipt-secret-" + "r" * 32,
            "XUANJI_RUNS_ROOT": "/var/lib/xuanji/runs",
            "XUANJI_RESULTS_ROOT": "/var/lib/xuanji/results",
        }
        settings = HostServiceSettings.from_env(values)
        rendered = repr(settings)
        for marker in ("host-secret", "dview-secret", "receipt-secret"):
            self.assertNotIn(marker, rendered)

    async def test_static_bearer_token_verifier_uses_constant_time_identity(self):
        verifier = StaticBearerTokenVerifier("t" * 32)
        self.assertIsNone(await verifier.verify_token("wrong"))
        access = await verifier.verify_token("t" * 32)
        self.assertIsNotNone(access)
        self.assertEqual(["xuanji"], access.scopes)


class DViewMCPBridgeTest(unittest.IsolatedAsyncioTestCase):
    async def test_structured_query_result_stays_structured(self):
        result = SimpleNamespace(
            isError=False,
            content=[SimpleNamespace(type="text", text="model-visible fallback")],
            structuredContent={
                "result": "| value |\n| --- |\n| 1 |\n\n"
                "*查询ID `private-query-id`, 共 1 行, 耗时 0.1s*"
            },
        )
        client_session = SimpleNamespace(call_tool=AsyncMock(return_value=result))
        query = DViewQuerySession(client_session)
        response = await query.query(
            sql="SELECT 1",
            database_type="MaxCompute",
            limit=250,
        )
        self.assertEqual(
            {"structuredContent": result.structuredContent},
            response,
        )
        client_session.call_tool.assert_awaited_once_with(
            "query",
            arguments={
                "sql": "SELECT 1",
                "database_type": "MaxCompute",
                "limit": 250,
            },
        )

    async def test_query_failure_preserves_private_id_as_typed_evidence(self):
        text = (
            "**查询失败**: MaxCompute数据库错误: 查询失败\n"
            "错误码: ODPS-0130071\n错误类别: semantic_analysis\n\n"
            "*查询ID: `private-query-id`*"
        )
        result = SimpleNamespace(
            isError=False,
            content=[SimpleNamespace(type="text", text=text)],
            structuredContent=None,
        )
        query = DViewQuerySession(
            SimpleNamespace(call_tool=AsyncMock(return_value=result))
        )
        with self.assertRaises(DViewExecutionError) as captured:
            await query.query(
                sql="SELECT broken",
                database_type="MaxCompute",
                limit=250,
            )
        self.assertEqual("private-query-id", captured.exception.query_id)
        self.assertEqual("semantic_analysis", captured.exception.error_class)
        self.assertEqual("ODPS-0130071", captured.exception.error_code)
        self.assertNotIn(
            "private-query-id",
            captured.exception.error_message,
        )

    async def test_unverifiable_tool_error_does_not_invent_query_id(self):
        result = SimpleNamespace(
            isError=True,
            content=[SimpleNamespace(type="text", text="permission denied")],
            structuredContent=None,
        )
        query = DViewQuerySession(
            SimpleNamespace(call_tool=AsyncMock(return_value=result))
        )
        with self.assertRaisesRegex(
            DViewMCPResponseError,
            "without a verifiable query ID",
        ):
            await query.query(
                sql="SELECT 1",
                database_type="MaxCompute",
                limit=250,
            )


class NativeHostToolSurfaceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup_temp_dir)
        self.mcp = create_mcp(
            _settings(Path(self.temp_dir.name)),
            runtime=_FakeRuntime(),
        )

    async def _cleanup_temp_dir(self):
        self.temp_dir.cleanup()

    async def test_endpoint_registers_exactly_three_tools(self):
        tools = self.mcp._tool_manager.list_tools()
        self.assertEqual(
            {
                "xuanji_run_investigation",
                "xuanji_submit_repair",
                "xuanji_finalize",
            },
            {tool.name for tool in tools},
        )
        self.assertTrue(all(tool.annotations.idempotentHint for tool in tools))
        self.assertTrue(all(not tool.annotations.destructiveHint for tool in tools))

    async def test_normal_tool_results_exclude_private_evidence(self):
        run_tool = self.mcp._tool_manager._tools["xuanji_run_investigation"].fn
        result = await run_tool(
            run_id="download-shadow",
            chain="download",
            game_type="app",
            metric="下载完成率",
            alert_date="2026-08-24",
        )
        finalize_tool = self.mcp._tool_manager._tools["xuanji_finalize"].fn
        finalized = await finalize_tool(
            run_id="download-shadow",
            writer_patch={"summary": "summary"},
            analysis_context={"source": "dataworks_dqc"},
        )
        encoded = json.dumps([result, finalized], ensure_ascii=False)
        for marker in (
            "SELECT ",
            "WITH ",
            "| --- |",
            "query_id",
            "raw_result",
            "receipt_signature",
            "rendered_sql",
            "private-query-id",
        ):
            self.assertNotIn(marker, encoded)

    async def test_internal_exception_text_is_not_returned(self):
        runtime = _FakeRuntime()
        runtime.run_investigation = AsyncMock(
            side_effect=RuntimeError(
                "SELECT secret FROM table query_id=private raw_result=rows"
            )
        )
        mcp = create_mcp(
            _settings(Path(self.temp_dir.name)),
            runtime=runtime,
        )
        tool = mcp._tool_manager._tools["xuanji_run_investigation"].fn
        with self.assertRaisesRegex(Exception, "RuntimeError") as captured:
            await tool(
                run_id="failed-shadow",
                chain="download",
                game_type="app",
                metric="下载完成率",
                alert_date="2026-08-24",
            )
        self.assertNotIn("SELECT secret", str(captured.exception))
        self.assertNotIn("private", str(captured.exception))


class NativeHostRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_download_and_apk_queues_complete_through_async_bridge(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(Path(temp_dir))
            client = _FixtureDViewClient(settings, Path(__file__).parents[1])
            runtime = XuanjiHostRuntime(settings, dview_client=client)
            scenarios = (
                ("native-download", "download", "下载完成率", 7),
                ("native-install", "install", "下载安装完成率", 6),
            )
            for run_id, chain, metric, expected_count in scenarios:
                result = await runtime.run_investigation(
                    run_id=run_id,
                    chain=chain,
                    game_type="app",
                    metric=metric,
                    alert_date="2026-08-24",
                )
                self.assertEqual("write_conclusion", result["action"])
                self.assertEqual(expected_count, result["executed_query_count"])
                encoded = json.dumps(result, ensure_ascii=False)
                for marker in (
                    "private-native",
                    "query_id",
                    "raw_result",
                    "rendered_sql",
                    "SELECT ",
                    "WITH ",
                ):
                    self.assertNotIn(marker, encoded)


class ValidatedResultSinkTest(unittest.TestCase):
    def test_sink_is_private_idempotent_and_conflict_rejecting(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sink = FileValidatedResultSink(temp_dir)
            analysis = {"queries": [{"query_id": "private-query-id"}]}
            receipt = {"status": "valid", "receipt_signature": "private"}
            sink("run-1", analysis, receipt)
            sink("run-1", analysis, receipt)

            path = Path(temp_dir) / "run-1" / "validated-result.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                "private-query-id", payload["analysis"]["queries"][0]["query_id"]
            )

            with self.assertRaisesRegex(RuntimeError, "conflicting content"):
                sink("run-1", {"changed": True}, receipt)


if __name__ == "__main__":
    unittest.main()
