import tempfile
import unittest
from pathlib import Path

from runtime.host_adapter import HostDViewAdapter, HostQueryResponse
from runtime.receipts import TrustedReceiptVerifier
from runtime.runner import AttributionRunner, RunnerError
from tests.runtime_result_fixtures import (
    raw_result_for_ticket,
    self_reported_result_event,
)


ROOT = Path(__file__).resolve().parents[1]


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


if __name__ == "__main__":
    unittest.main()
