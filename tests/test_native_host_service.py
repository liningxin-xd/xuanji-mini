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
from host_service.sink import FileTaskResultSink, FileValidatedResultSink
from host_service.tools import create_mcp
from runtime.host_adapter import DViewExecutionError
from runtime.receipts import TrustedReceiptVerifier
from runtime.runner import AttributionRunner
from tests.runtime_result_fixtures import raw_result_for_ticket
from tests.test_registered_alert_coordinator import FixtureRootExecutor


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
        tasks_root=root / "tasks",
        results_root=root / "results",
    )


class _FakeRuntime:
    async def run_task(self, **kwargs):
        return {
            "action": "write_conclusion",
            "task_id": kwargs["task_id"],
            "investigation_id": "inv-00-fixture",
            "writer_pack": {"status": "no_dominant_slice"},
        }

    async def submit_repair(self, **kwargs):
        return {
            "action": "write_conclusion",
            "task_id": kwargs["task_id"],
            "investigation_id": kwargs["investigation_id"],
            "run_id": kwargs["run_id"],
            "writer_pack": {"status": "completed"},
        }

    async def finalize(self, **kwargs):
        return {
            "action": "task_complete",
            "task_id": kwargs["task_id"],
            "overall_status": "completed",
            "analysis_preview": {"overall_status": "completed"},
            "validation_receipt": {"status": "valid"},
            "audit_detail": "retained_by_host",
        }


class _FixtureDViewClient:
    def __init__(self, settings: HostServiceSettings, repository_root: Path):
        self._settings = settings
        self._repository_root = repository_root
        self._query_counts: dict[str, int] = {}
        self._root_executor = FixtureRootExecutor(
            current_rate=0.79,
            historical_rate=0.80,
        )

    @asynccontextmanager
    async def session(self):
        yield self

    async def query(self, *, sql, database_type, limit):
        self.assert_query_contract(database_type, limit)
        if "ads_dmg_quality_platform_download_chain_monitor_1d" in sql:
            response = self._root_executor.execute_read_only(sql)
            return {
                "query_id": response.query_id,
                "columns": response.raw_result["columns"],
                "rows": response.raw_result["rows"],
            }
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
            "XUANJI_TASKS_ROOT": "/var/lib/xuanji/tasks",
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
                "xuanji_run_task",
                "xuanji_submit_repair",
                "xuanji_finalize",
            },
            {tool.name for tool in tools},
        )
        self.assertTrue(all(tool.annotations.idempotentHint for tool in tools))
        self.assertTrue(all(not tool.annotations.destructiveHint for tool in tools))

    async def test_normal_tool_results_exclude_private_evidence(self):
        run_tool = self.mcp._tool_manager._tools["xuanji_run_task"].fn
        result = await run_tool(
            task_id="download-shadow",
            dqc_payload={"ruleChecks": [{"ruleName": "registered"}]},
        )
        finalize_tool = self.mcp._tool_manager._tools["xuanji_finalize"].fn
        finalized = await finalize_tool(
            task_id="download-shadow",
            investigation_id="inv-00-fixture",
            writer_patch={"summary": "summary"},
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
        runtime.run_task = AsyncMock(
            side_effect=RuntimeError(
                "SELECT secret FROM table query_id=private raw_result=rows"
            )
        )
        mcp = create_mcp(
            _settings(Path(self.temp_dir.name)),
            runtime=runtime,
        )
        tool = mcp._tool_manager._tools["xuanji_run_task"].fn
        with self.assertLogs("host_service.tools", level="ERROR") as logs:
            with self.assertRaisesRegex(Exception, "xuanji Host request failed") as captured:
                await tool(
                    task_id="failed-shadow",
                    dqc_payload={"ruleChecks": [{"ruleName": "registered"}]},
                )
        self.assertNotIn("SELECT secret", str(captured.exception))
        self.assertNotIn("private", str(captured.exception))
        self.assertNotIn("RuntimeError", str(captured.exception))
        rendered_logs = "\n".join(logs.output)
        self.assertIn('"task_id":"failed-shadow"', rendered_logs)
        self.assertIn('"phase":"run_task"', rendered_logs)
        self.assertIn('"exception_type":"RuntimeError"', rendered_logs)
        self.assertNotIn("SELECT secret", rendered_logs)
        self.assertNotIn("query_id=private", rendered_logs)


class NativeHostRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_registered_task_completes_through_async_bridge(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(Path(temp_dir))
            client = _FixtureDViewClient(settings, Path(__file__).parents[1])
            runtime = XuanjiHostRuntime(settings, dview_client=client)
            payload = {
                "projectName": "tap_dw",
                "dqcEntityQuality": {
                    "entityName": "ads_dmg_quality_platform_download_chain_monitor_1d",
                    "actualExpression": "dt=2026-08-24",
                },
                "ruleChecks": [
                    {
                        "ruleName": "【apk下载完成率】最近1天_低于80%",
                        "tableName": "ads_dmg_quality_platform_download_chain_monitor_1d",
                        "actualExpression": "dt=2026-08-24",
                        "op": ">=",
                        "expectValue": 0.8,
                    }
                ],
            }
            with self.assertLogs("host_service.runtime", level="INFO") as logs:
                result = await runtime.run_task(
                    task_id="native-download", dqc_payload=payload
                )
            self.assertEqual("write_conclusion", result["action"])
            completed = await runtime.finalize(
                task_id="native-download",
                investigation_id=result["investigation_id"],
                writer_patch={
                    "summary": "固定队列已完成，未发现达到候选门槛的切片。",
                    "finding_texts": {},
                    "evidence_limits": [],
                    "recommended_action": "继续跟踪下载链路及恢复情况。",
                },
            )
            self.assertEqual("task_complete", completed["action"])
            encoded = json.dumps([result, completed], ensure_ascii=False)
            for marker in (
                "private-native",
                "query_id",
                "raw_result",
                "rendered_sql",
                "SELECT ",
                "WITH ",
            ):
                self.assertNotIn(marker, encoded)
            rendered_logs = "\n".join(logs.output)
            for field in (
                '"task_id":"native-download"',
                '"phase":"run_task"',
                '"root_query_count":8',
                '"attribution_query_count":',
                '"root_snapshot_reused":false',
                '"writer_pack_bytes":',
                '"exception_type":null',
            ):
                self.assertIn(field, rendered_logs)
            for marker in ("SELECT ", "query_id", "raw_result", "private-native"):
                self.assertNotIn(marker, rendered_logs)


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

    def test_task_sink_is_idempotent_and_conflict_rejecting(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sink = FileTaskResultSink(temp_dir)
            analysis = {
                "overall_status": "completed",
                "investigations": [],
                "new_flow_metadata": {"version": 2},
            }
            receipt = {"status": "valid", "new_flow_metadata": {"opaque": True}}
            sink("task-1", analysis, receipt)
            sink("task-1", analysis, receipt)
            artifact = sink.load("task-1")
            self.assertEqual(1, artifact["schema_version"])
            self.assertEqual(
                {"schema_version", "task_id", "analysis", "validation_receipt"},
                set(artifact),
            )
            self.assertEqual(analysis, artifact["analysis"])
            with self.assertRaisesRegex(RuntimeError, "conflicting content"):
                sink("task-1", {"overall_status": "failed"}, receipt)

    def test_task_sink_rejects_unversioned_or_unknown_schema(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            task_root = Path(temp_dir) / "task-1"
            task_root.mkdir()
            target = task_root / "validated-task-result.json"
            sink = FileTaskResultSink(temp_dir)
            for payload in (
                {
                    "task_id": "task-1",
                    "analysis": {},
                    "validation_receipt": {},
                },
                {
                    "schema_version": 2,
                    "task_id": "task-1",
                    "analysis": {},
                    "validation_receipt": {},
                },
                {
                    "schema_version": True,
                    "task_id": "task-1",
                    "analysis": {},
                    "validation_receipt": {},
                },
            ):
                target.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "artifact is invalid"):
                    sink.load("task-1")


if __name__ == "__main__":
    unittest.main()
