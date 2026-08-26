import json
import tempfile
import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from runtime.host_adapter import (
    DViewExecutionError,
    HostDViewAdapter,
    HostQueryResponse,
    ProductionDViewExecutor,
)
from runtime.receipts import TrustedReceiptVerifier
from runtime.runner import AttributionRunner, RunnerError
from tests.runtime_result_fixtures import (
    raw_result_for_ticket,
    self_reported_result_event,
)


ROOT = Path(__file__).resolve().parents[1]
DVIEW_RESPONSE_PATH = ROOT / "tests/fixtures/runtime/dview-query-response.json"


class ResultValidatorRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.runner = AttributionRunner(ROOT, runs_root=self.temp_dir.name)
        self.runner.init_run(
            run_id="result-run",
            chain="download",
            game_type="app",
            metric="下载完成率",
            alert_date="2026-08-22",
            receipt_mode="self_reported",
        )
        self.ticket = self.runner.next_action("result-run")

    def _record(self, raw_result, query_id="result-query"):
        return self.runner.record(
            "result-run",
            self_reported_result_event(self.ticket, raw_result, query_id),
        )

    def test_candidate_count_and_details_are_derived_from_raw_rows(self):
        raw_result = raw_result_for_ticket(
            self.runner, "result-run", self.ticket, candidate=True
        )
        self._record(raw_result)
        state = self.runner.load_state("result-run")
        step = state["steps"][0]
        self.assertEqual("succeeded", step["status"])
        self.assertEqual(1, step["candidate_count"])
        self.assertEqual("slice-a", step["candidates"][0]["value"])
        self.assertGreaterEqual(step["candidates"][0]["adverse_impact_bp"], 5)

    def test_caller_cannot_submit_terminal_classification_fields(self):
        raw_result = raw_result_for_ticket(self.runner, "result-run", self.ticket)
        for field, value in (
            ("candidate_count", 0),
            ("warning_codes", []),
            ("reason", "skip"),
        ):
            event = self_reported_result_event(self.ticket, raw_result, "query-x")
            event[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(
                RunnerError, "unknown"
            ):
                self.runner.record("result-run", event)

        with self.assertRaisesRegex(RunnerError, "unsupported"):
            self.runner.record(
                "result-run",
                {
                    "event": "step_validation_failed",
                    "step_id": self.ticket["step_id"],
                },
            )

    def test_malformed_result_becomes_a_runner_classified_family_failure(self):
        raw_result = raw_result_for_ticket(self.runner, "result-run", self.ticket)
        raw_result["rows"][0]["overall_current_numerator"] += 1
        result = self._record(raw_result)
        self.assertEqual(1, result["cursor"])
        state = self.runner.load_state("result-run")
        self.assertEqual("failed", state["steps"][0]["status"])
        self.assertEqual("result_incomplete", state["steps"][0]["failure_code"])

    def test_raw_result_hash_is_mandatory_and_verified(self):
        raw_result = raw_result_for_ticket(self.runner, "result-run", self.ticket)
        event = self_reported_result_event(self.ticket, raw_result, "query-hash")
        event["raw_result_sha256"] = "0" * 64
        with self.assertRaisesRegex(RunnerError, "does not match"):
            self.runner.record("result-run", event)

    def test_query_id_cannot_be_reused_by_the_next_step(self):
        raw_result = raw_result_for_ticket(self.runner, "result-run", self.ticket)
        self._record(raw_result, "shared-query")
        next_ticket = self.runner.next_action("result-run")
        next_result = raw_result_for_ticket(
            self.runner, "result-run", next_ticket
        )
        with self.assertRaisesRegex(RunnerError, "already bound"):
            self.runner.record(
                "result-run",
                self_reported_result_event(next_ticket, next_result, "shared-query"),
            )

    def test_source_bucket_and_column_contract_failures_are_typed(self):
        first_result = raw_result_for_ticket(self.runner, "result-run", self.ticket)
        self._record(first_result)
        second = self.runner.next_action("result-run")
        broken = raw_result_for_ticket(
            self.runner,
            "result-run",
            second,
            break_source_closure=True,
        )
        self.runner.record(
            "result-run",
            self_reported_result_event(second, broken, "source-closure-query"),
        )
        state = self.runner.load_state("result-run")
        self.assertEqual("result_incomplete", state["steps"][1]["failure_code"])

    def test_cross_family_root_mismatch_fails_only_the_current_family(self):
        ticket = self.ticket
        while ticket["action"] != "queue_complete":
            raw_result = raw_result_for_ticket(
                self.runner, "result-run", ticket, candidate=True
            )
            if ticket["step_id"] == "device_brand":
                for row in raw_result["rows"]:
                    row["overall_current_numerator"] = 780
                raw_result["rows"][-1]["current_numerator"] -= 10
            self.runner.record(
                "result-run",
                self_reported_result_event(
                    ticket, raw_result, f"root-{ticket['step_id']}"
                ),
            )
            ticket = self.runner.next_action("result-run")

        state = self.runner.load_state("result-run")
        canonical = state["canonical_root_metric"]
        self.assertAlmostEqual(0.79, canonical["current_value"])
        self.assertAlmostEqual(0.8, canonical["baseline_value"])
        self.assertAlmostEqual(-0.01, canonical["delta"])
        device = state["steps"][2]
        self.assertEqual("failed", device["status"])
        self.assertEqual("result_incomplete", device["failure_code"])
        self.assertEqual(
            "result_incomplete: root metric does not rehook the canonical "
            "investigation root",
            device["reason"],
        )
        self.assertTrue(
            all(step["status"] == "succeeded" for step in state["steps"][3:])
        )

    def test_registered_quality_bucket_is_accepted_and_cannot_be_a_business_bucket(self):
        self.assertTrue(self.runner.result_validator._is_quality_value("__quality__"))
        first_result = raw_result_for_ticket(self.runner, "result-run", self.ticket)
        self._record(first_result)
        second = self.runner.next_action("result-run")
        raw_result = raw_result_for_ticket(self.runner, "result-run", second)
        row = raw_result["rows"][0]
        row["bucket_kind"] = "quality"
        row["dimension_value"] = "__quality__"
        row["dimension_label"] = "__quality__"
        self.runner.record(
            "result-run",
            self_reported_result_event(second, raw_result, "quality-query"),
        )
        state = self.runner.load_state("result-run")
        self.assertEqual("succeeded", state["steps"][1]["status"])
        self.assertEqual(
            ["quality_bucket_present"], state["steps"][1]["warning_codes"]
        )

    def test_dimension_quality_and_source_audits_reject_spoofed_rows(self):
        first_result = raw_result_for_ticket(self.runner, "result-run", self.ticket)
        self._record(first_result)
        second = self.runner.next_action("result-run")
        mutations = []

        quality_as_business = raw_result_for_ticket(
            self.runner, "result-run", second
        )
        quality_as_business["rows"][0]["dimension_value"] = "__quality__"
        mutations.append(quality_as_business)

        collapsed_business = raw_result_for_ticket(
            self.runner, "result-run", second
        )
        collapsed_business["rows"][0]["collapsed_source_bucket_count"] = 2
        collapsed_business["rows"][0]["source_bucket_count"] = 2
        mutations.append(collapsed_business)

        broken_match_rate = raw_result_for_ticket(
            self.runner, "result-run", second
        )
        broken_match_rate["rows"][0]["overall_current_dimension_match_rate"] = 0.5
        mutations.append(broken_match_rate)

        for index, raw_result in enumerate(mutations):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as temp_dir:
                runner = AttributionRunner(ROOT, runs_root=temp_dir)
                runner.init_run(
                    run_id="audit-run",
                    chain="download",
                    game_type="app",
                    metric="下载完成率",
                    alert_date="2026-08-22",
                    receipt_mode="self_reported",
                )
                first = runner.next_action("audit-run")
                runner.record(
                    "audit-run",
                    self_reported_result_event(
                        first,
                        raw_result_for_ticket(runner, "audit-run", first),
                        f"audit-first-{index}",
                    ),
                )
                ticket = runner.next_action("audit-run")
                runner.record(
                    "audit-run",
                    self_reported_result_event(
                        ticket, raw_result, f"audit-second-{index}"
                    ),
                )
                state = runner.load_state("audit-run")
                self.assertEqual("failed", state["steps"][1]["status"])

    def test_registered_date_mapping_is_derived_and_enforced(self):
        install = AttributionRunner(ROOT, runs_root=Path(self.temp_dir.name) / "install")
        initialized = install.init_run(
            run_id="install-date",
            chain="install",
            game_type="app",
            metric="下载安装完成率",
            alert_date="2026-08-24",
            receipt_mode="self_reported",
        )
        self.assertEqual("install-date", initialized["run_id"])
        self.assertEqual("2026-08-22", install.load_state("install-date")["analysis_date"])
        with self.assertRaisesRegex(RunnerError, "expected 2026-08-22"):
            install.init_run(
                run_id="wrong-date",
                chain="install",
                game_type="app",
                metric="下载安装完成率",
                alert_date="2026-08-24",
                analysis_date="2026-08-24",
                receipt_mode="self_reported",
            )


class _FakeExecutor:
    def __init__(self, response: HostQueryResponse):
        self.response = response
        self.sql = None

    def execute_read_only(self, sql: str) -> HostQueryResponse:
        self.sql = sql
        return self.response


class TrustedHostAdapterTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.signer = TrustedReceiptVerifier(key_id="host-test", secret=b"x" * 32)

    def test_production_mode_requires_a_host_receipt_authority(self):
        runner = AttributionRunner(ROOT, runs_root=self.temp_dir.name)
        with self.assertRaisesRegex(RunnerError, "Host adapter"):
            runner.init_run(
                run_id="no-host",
                chain="download",
                game_type="app",
                metric="下载完成率",
                alert_date="2026-08-22",
            )

    def test_host_adapter_executes_the_issued_sql_and_signs_receipt(self):
        runner = AttributionRunner(
            ROOT,
            runs_root=self.temp_dir.name,
            trusted_receipt_verifier=self.signer,
        )
        runner.init_run(
            run_id="host-run",
            chain="download",
            game_type="app",
            metric="下载完成率",
            alert_date="2026-08-22",
        )
        ticket = runner.next_action("host-run")
        raw_result = raw_result_for_ticket(runner, "host-run", ticket)
        executor = _FakeExecutor(
            HostQueryResponse(
                query_id="host-query-1",
                receipt_id="receipt-1",
                raw_result=raw_result,
            )
        )
        adapter = HostDViewAdapter(
            runner=runner, executor=executor, receipt_signer=self.signer
        )
        result = adapter.execute_current("host-run")
        self.assertEqual(1, result["cursor"])
        self.assertEqual(ticket["rendered_sql"], executor.sql)

    def test_production_executor_parses_the_current_dview_mcp_shape(self):
        fixture = json.loads(DVIEW_RESPONSE_PATH.read_text(encoding="utf-8"))
        calls = []

        def query(**kwargs):
            calls.append(kwargs)
            return fixture

        response = ProductionDViewExecutor(query).execute_read_only("SELECT 1")
        self.assertEqual("00000000-0000-4000-8000-000000000001", response.query_id)
        self.assertEqual(
            {
                "columns": [
                    "sample_null",
                    "sample_integer",
                    "sample_decimal",
                    "sample_date",
                ],
                "rows": [[None, 1, 1.5, "2026-08-26"]],
            },
            response.raw_result,
        )
        self.assertEqual("MaxCompute", calls[0]["database_type"])
        self.assertEqual(250, calls[0]["limit"])

    def test_production_executor_normalizes_structured_transport_types(self):
        response = ProductionDViewExecutor(
            lambda **_: {
                "query_id": "structured-query",
                "columns": [
                    {"name": "whole"},
                    {"name": "fraction"},
                    {"name": "day"},
                    {"name": "day_time"},
                    {"name": "missing"},
                ],
                "rows": [
                    {
                        "whole": Decimal("2"),
                        "fraction": Decimal("2.5"),
                        "day": date(2026, 8, 26),
                        "day_time": datetime(2026, 8, 26, 12, 30),
                        "missing": None,
                    }
                ],
            }
        ).execute_read_only("SELECT typed_values")
        self.assertEqual(
            [[2, 2.5, "2026-08-26", "2026-08-26", None]],
            response.raw_result["rows"],
        )

    def test_production_executor_preserves_numeric_dimension_values_as_text(self):
        response = ProductionDViewExecutor(
            lambda **_: {
                "result": (
                    "| analysis_date | game_type | bucket_kind | dimension_value | "
                    "dimension_label | current_denominator |\n"
                    "| --- | --- | --- | --- | --- | --- |\n"
                    "| 2026-08-26 | app | game | 12345 | 12345 | 100 |\n\n"
                    "*查询ID `numeric-dimension`, 共 1 行, 耗时 1.00s*"
                )
            }
        ).execute_read_only("SELECT numeric_dimension")
        self.assertEqual("12345", response.raw_result["rows"][0][3])
        self.assertEqual("12345", response.raw_result["rows"][0][4])
        self.assertEqual(100, response.raw_result["rows"][0][5])

    def test_execute_until_blocked_runs_four_primary_profiles_without_leaks(self):
        scenarios = (
            ("host-download-candidate", "download", "app", "下载完成率", {"game_id"}, 7),
            ("host-download-flat", "download", "sandbox", "下载失败率", set(), 7),
            ("host-install-app", "install", "app", "下载安装完成率", set(), 6),
            ("host-install-sandbox", "install", "sandbox", "下载安装完成率", set(), 5),
        )
        for run_id, chain, game_type, metric, candidate_steps, expected_queries in scenarios:
            with self.subTest(run_id=run_id):
                runner = AttributionRunner(
                    ROOT,
                    runs_root=self.temp_dir.name,
                    trusted_receipt_verifier=self.signer,
                )
                runner.init_run(
                    run_id=run_id,
                    chain=chain,
                    game_type=game_type,
                    metric=metric,
                    alert_date="2026-08-24",
                )
                client = _RunnerBackedMCPClient(runner, run_id, candidate_steps)
                adapter = HostDViewAdapter(
                    runner=runner,
                    executor=ProductionDViewExecutor(client),
                    receipt_signer=self.signer,
                )
                result = adapter.execute_until_blocked(run_id)
                self.assertEqual("queue_complete", result["action"])
                self.assertEqual(expected_queries, result["executed_query_count"])
                serialized = json.dumps(result)
                self.assertNotIn("rendered_sql", serialized)
                self.assertNotIn("raw_result", serialized)
                expected_status = "completed" if candidate_steps else "no_dominant_slice"
                self.assertEqual(
                    expected_status,
                    runner.build_writer_pack(run_id)["result_status_hint"],
                )

    def test_execute_until_blocked_pauses_only_for_semantic_repair(self):
        runner = AttributionRunner(
            ROOT,
            runs_root=self.temp_dir.name,
            trusted_receipt_verifier=self.signer,
        )
        runner.init_run(
            run_id="host-semantic",
            chain="download",
            game_type="app",
            metric="下载完成率",
            alert_date="2026-08-22",
        )

        def query(**_):
            raise DViewExecutionError(
                query_id="semantic-query",
                error_class="semantic_analysis",
                error_code="ODPS-0130071",
                error_message="column is not in GROUP BY",
            )

        adapter = HostDViewAdapter(
            runner=runner,
            executor=ProductionDViewExecutor(query),
            receipt_signer=self.signer,
        )
        result = adapter.execute_until_blocked("host-semantic")
        self.assertEqual("repair_query", result["action"])
        self.assertEqual(1, result["executed_query_count"])

    def test_execute_until_blocked_continues_after_a_family_query_error(self):
        runner = AttributionRunner(
            ROOT,
            runs_root=self.temp_dir.name,
            trusted_receipt_verifier=self.signer,
        )
        runner.init_run(
            run_id="host-family-error",
            chain="download",
            game_type="app",
            metric="下载完成率",
            alert_date="2026-08-22",
        )
        delegate = _RunnerBackedMCPClient(runner, "host-family-error", set())

        def query(**kwargs):
            ticket = runner.next_action("host-family-error")
            if ticket["step_id"] == "game_id":
                raise DViewExecutionError(
                    query_id="blocked-game-query",
                    error_class="permission",
                    error_code="ACCESS_DENIED",
                    error_message="permission denied",
                )
            return delegate(**kwargs)

        adapter = HostDViewAdapter(
            runner=runner,
            executor=ProductionDViewExecutor(query),
            receipt_signer=self.signer,
        )
        result = adapter.execute_until_blocked("host-family-error")
        self.assertEqual("queue_complete", result["action"])
        self.assertEqual(7, result["executed_query_count"])
        state = runner.load_state("host-family-error")
        self.assertEqual("failed", state["steps"][0]["status"])
        self.assertTrue(
            all(step["status"] == "succeeded" for step in state["steps"][1:])
        )

    def test_execute_until_blocked_rejects_250_rows_and_continues(self):
        runner = AttributionRunner(
            ROOT,
            runs_root=self.temp_dir.name,
            trusted_receipt_verifier=self.signer,
        )
        runner.init_run(
            run_id="host-row-limit",
            chain="download",
            game_type="app",
            metric="下载完成率",
            alert_date="2026-08-22",
        )
        delegate = _RunnerBackedMCPClient(runner, "host-row-limit", set())

        def query(**kwargs):
            ticket = runner.next_action("host-row-limit")
            if ticket["step_id"] != "game_id":
                return delegate(**kwargs)
            if kwargs["limit"] != 250:
                raise AssertionError("Host did not request the row-limit sentinel")
            raw_result = raw_result_for_ticket(runner, "host-row-limit", ticket)
            raw_result["rows"] = [dict(raw_result["rows"][0]) for _ in range(250)]
            return {"result": _markdown_result(raw_result, "row-limit-game")}

        adapter = HostDViewAdapter(
            runner=runner,
            executor=ProductionDViewExecutor(query),
            receipt_signer=self.signer,
        )
        result = adapter.execute_until_blocked("host-row-limit")
        self.assertEqual("queue_complete", result["action"])
        state = runner.load_state("host-row-limit")
        self.assertEqual("failed", state["steps"][0]["status"])
        self.assertEqual("result_incomplete", state["steps"][0]["failure_code"])
        self.assertTrue(
            all(step["status"] == "succeeded" for step in state["steps"][1:])
        )


class _RunnerBackedMCPClient:
    def __init__(self, runner, run_id, candidate_steps):
        self.runner = runner
        self.run_id = run_id
        self.candidate_steps = candidate_steps
        self.query_count = 0

    def __call__(self, *, sql, database_type, limit):
        ticket = self.runner.next_action(self.run_id)
        if sql != ticket["rendered_sql"]:
            raise AssertionError("Host did not execute the current issued SQL")
        if database_type != "MaxCompute" or limit != 250:
            raise AssertionError("Host changed the registered DView query transport")
        raw_result = raw_result_for_ticket(
            self.runner,
            self.run_id,
            ticket,
            candidate=ticket["step_id"] in self.candidate_steps,
        )
        self.query_count += 1
        return {
            "result": _markdown_result(
                raw_result,
                f"{self.run_id}-{self.query_count}",
            )
        }


def _markdown_result(raw_result, query_id):
    def cell(value):
        if value is None:
            return "NULL"
        return str(value).replace("\\", "\\\\").replace("|", "\\|")

    columns = raw_result["columns"]
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in raw_result["rows"]:
        values = [row[name] for name in columns] if isinstance(row, dict) else row
        lines.append("| " + " | ".join(cell(value) for value in values) + " |")
    lines.extend(
        [
            "",
            f"*查询ID `{query_id}`, 共 {len(raw_result['rows'])} 行, 耗时 1.00s*",
        ]
    )
    return "\n".join(lines)


if __name__ == "__main__":
    unittest.main()
