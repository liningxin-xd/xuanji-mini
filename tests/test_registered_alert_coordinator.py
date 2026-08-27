from __future__ import annotations

import json
import re
import stat
import tempfile
import unittest
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path

import yaml

from runtime.alert_normalizer import AlertNormalizer
from runtime.contracts import RepositoryContracts
from runtime.host_adapter import HostQueryResponse
from runtime.root_preflight import RootPreflight, RootPreflightError, RootSnapshotError
from runtime.route_resolver import DqcRouteRegistry, RouteResolver
from runtime.task_coordinator import RegisteredAlertCoordinator, TaskCoordinatorError


ROOT = Path(__file__).resolve().parents[1]
ROUTES = yaml.safe_load(
    (ROOT / "contracts" / "dqc-routes.yaml").read_text(encoding="utf-8")
)["routes"]
ROOT_SPEC = yaml.safe_load(
    (ROOT / "references" / "queries" / "registered-monitor-root.yaml").read_text(
        encoding="utf-8"
    )
)


def rule_for(route: dict, *, partition: str = "dt=2026-08-24") -> dict:
    return {
        "ruleName": route["normalized_rule_name"],
        "tableName": "ads_dmg_quality_platform_download_chain_monitor_1d",
        "actualExpression": partition,
        "property": route["monitor_field"],
        "op": {
            "<": ">=",
            "<=": ">",
            ">": "<=",
            ">=": "<",
            "==": "!=",
            "!=": "==",
        }[route["alert_operator"]],
        "expectValue": route["alert_threshold"],
    }


def payload_for(*routes: dict) -> dict:
    return {
        "projectName": "tap_dw",
        "dqcEntityQuality": {
            "entityName": "ads_dmg_quality_platform_download_chain_monitor_1d",
            "actualExpression": "dt=2026-08-24",
        },
        "ruleChecks": [rule_for(route) for route in routes],
    }


class FixtureRootExecutor:
    def __init__(
        self,
        *,
        current_rate: float = 0.74,
        historical_rate: float = 0.75,
        fail_first: bool = False,
        fail_on_call: int | None = None,
        denominator: int = 1000,
        materialized_offset: float = 0.0,
    ):
        self.current_rate = current_rate
        self.historical_rate = historical_rate
        self.fail_first = fail_first
        self.fail_on_call = fail_on_call
        self.denominator = denominator
        self.materialized_offset = materialized_offset
        self.calls: list[tuple[str, str]] = []

    def execute_read_only(self, sql: str) -> HostQueryResponse:
        match = re.search(r"SELECT\s+'(\d{4}-\d{2}-\d{2})'\s+AS alert_date", sql)
        game = re.search(r"game_type\s*=\s*'(app|sandbox)'", sql)
        if match is None or game is None:
            raise AssertionError("root preflight did not render the locked QuerySpec")
        partition_date = match.group(1)
        game_type = game.group(1)
        self.calls.append((partition_date, game_type))
        if self.fail_first or len(self.calls) == self.fail_on_call:
            self.fail_first = False
            return HostQueryResponse(
                query_id="private-root-blocked",
                receipt_id="private-root-blocked",
                error_class="permission_denied",
                error_code="403",
                error_message="private permission detail",
            )
        is_current = len(self.calls) % 8 == 1
        rate = self.current_rate if is_current else self.historical_rate
        denominator = self.denominator
        numerator = round(rate * denominator)
        materialized_rate = numerator / denominator
        if is_current:
            materialized_rate += self.materialized_offset
        columns = ROOT_SPEC["output"]["columns"]
        row = {}
        for name, value_type in columns.items():
            if value_type == "string":
                row[name] = "value"
            elif value_type == "date":
                row[name] = partition_date
            else:
                row[name] = 0
        row.update(
            {
                "platform": "ANDROID",
                "game_type": game_type,
                "game_download_device_num_1d": denominator,
                "game_download_cnt_1d": denominator,
                "game_download_complete_device_num_1d": numerator,
                "game_download_failed_device_num_1d": numerator,
                "game_download_failed_cnt_1d": numerator,
                "game_download_stop_device_num_1d": numerator,
                "game_download_complete_rate_1d": materialized_rate,
                "game_download_failed_rate_1d": numerator / denominator,
                "game_download_failed_pv_rate_1d": numerator / denominator,
                "game_download_stop_rate_1d": numerator / denominator,
                "game_download_complete_prev_2d_device_num_1d": denominator,
                "game_download_complete_and_install_complete_prev_2d_device_num_p3d": numerator,
                "game_download_complete_and_install_complete_prev_2d_rate_p3d": materialized_rate,
            }
        )
        return HostQueryResponse(
            query_id=f"private-root-{len(self.calls)}",
            receipt_id=f"private-root-{len(self.calls)}",
            raw_result={
                "columns": list(columns),
                "rows": [[row[name] for name in columns]],
            },
        )


class UnexpectedRootExecutor:
    def execute_read_only(self, sql: str) -> HostQueryResponse:
        raise KeyError("private internal contract detail")


class MemoryArtifactStore:
    def __init__(self):
        self.values: dict[str, dict] = {}

    def load(self, artifact_id: str) -> dict:
        return deepcopy(self.values[artifact_id])


class MemoryTaskSink(MemoryArtifactStore):
    def __call__(self, task_id: str, analysis: dict, validation_receipt: dict) -> None:
        payload = {
            "task_id": task_id,
            "analysis": deepcopy(analysis),
            "validation_receipt": deepcopy(validation_receipt),
        }
        existing = self.values.get(task_id)
        if existing is not None and existing != payload:
            raise RuntimeError("conflicting task result")
        self.values[task_id] = payload


class FakeInvestigationHost:
    def __init__(self, run_store: MemoryArtifactStore):
        self.run_store = run_store
        self.runs: dict[str, dict] = {}

    def xuanji_run_investigation(self, **kwargs):
        self.runs[kwargs["run_id"]] = deepcopy(kwargs)
        root = kwargs["canonical_root_metric"]
        return {
            "action": "write_conclusion",
            "run_id": kwargs["run_id"],
            "writer_pack": {
                "analysis_profile": "primary_v1",
                "run_id": kwargs["run_id"],
                "metric": kwargs["metric"],
                "analysis_date": kwargs["alert_date"],
                "game_type": kwargs["game_type"],
                "execution_mode": "trusted_host_adapter",
                "result_status_hint": "no_dominant_slice",
                "steps": [],
                "candidates": [],
                "evidence_limits": [],
                "root_metric": {
                    "current_value": root["current_value"],
                    "baseline_value": root["baseline_value"],
                    "delta_bp": root["delta"] * 10000,
                },
            },
        }

    def xuanji_submit_repair(self, **kwargs):
        raise AssertionError("repair is not expected in this fixture")

    def xuanji_finalize(self, **kwargs):
        run = self.runs[kwargs["run_id"]]
        context = kwargs["analysis_context"]
        patch = kwargs["writer_patch"]
        root = run["canonical_root_metric"]
        investigation = {
            **deepcopy(context["investigation"]),
            "status": "no_dominant_slice",
            "metric": run["metric"],
            "analysis_date": (
                date.fromisoformat(run["alert_date"])
                - timedelta(days=2 if run["chain"] == "install" else 0)
            ).isoformat(),
            "current_value": root["current_value"],
            "baseline_value": root["baseline_value"],
            "delta_bp": root["delta"] * 10000,
            "summary": patch["summary"],
            "evidence_limits": patch["evidence_limits"],
            "recommended_action": patch["recommended_action"],
            "attribution_execution": {
                "mode": "full_queue",
                "chain": run["chain"],
                "game_type": run["game_type"],
                "execution_mode": "trusted_host_adapter",
                "steps": [],
            },
        }
        analysis = {
            "source": context["source"],
            "project": context["project"],
            "table": context["table"],
            "partition": context["partition"],
            "overall_status": "completed",
            "investigations": [investigation],
        }
        receipt = {
            "status": "valid",
            "investigation_status": "no_dominant_slice",
            "execution_mode": "trusted_host_adapter",
            "validated_step_count": 0,
            "analysis_sha256": "a" * 64,
            "validation_receipt_sha256": "b" * 64,
        }
        self.run_store.values[kwargs["run_id"]] = {
            "run_id": kwargs["run_id"],
            "analysis": analysis,
            "validation_receipt": receipt,
        }
        return {"action": "finalized"}


def writer_patch(summary: str = "机器证据已完成当前调查。") -> dict:
    return {
        "summary": summary,
        "finding_texts": {},
        "evidence_limits": [],
        "recommended_action": "继续跟踪已登记指标并复核对应链路。",
    }


class AlertRouteAndPreflightTest(unittest.TestCase):
    def test_all_sixteen_rules_resolve_exactly_and_metrics_do_not_swap(self):
        registry = DqcRouteRegistry(ROOT)
        self.assertEqual(16, len(registry.routes))
        for route in ROUTES:
            with self.subTest(rule=route["normalized_rule_name"]):
                resolved = registry.resolve(
                    route["object_table"], route["normalized_rule_name"]
                )
                self.assertIsNotNone(resolved)
                self.assertEqual(route["canonical_metric"], resolved.canonical_metric)
                self.assertEqual(route["game_type"], resolved.game_type)
        self.assertIsNone(
            registry.resolve(
                ROUTES[0]["object_table"],
                ROUTES[0]["normalized_rule_name"].replace("80%", "81%"),
            )
        )

    def test_normalizer_preserves_unknown_fields_and_groups_each_index_once(self):
        payload = payload_for(*ROUTES[:3], ROUTES[9])
        payload.pop("projectName")
        payload["dqcEntityQuality"]["projectName"] = "tap_dw"
        payload["payloadExtension"] = {"kept": True}
        payload["ruleChecks"][0]["ruleExtension"] = "kept"
        normalized = AlertNormalizer().normalize(payload)
        self.assertEqual("tap_dw", normalized["project"])
        self.assertEqual({"kept": True}, normalized["unknown_fields"]["payloadExtension"])
        self.assertEqual(
            "kept", normalized["rules"][0]["unknown_fields"]["ruleExtension"]
        )
        investigations = RouteResolver().resolve(normalized)
        self.assertEqual([[0, 1, 2], [3]], [item["rule_indexes"] for item in investigations])
        self.assertEqual(["app", "sandbox"], [item["route"]["game_type"] for item in investigations])
        self.assertEqual([0, 1, 2, 3], sorted(index for item in investigations for index in item["rule_indexes"]))

    def test_unknown_rule_becomes_explicit_insufficient_definition(self):
        payload = payload_for(ROUTES[0])
        payload["ruleChecks"][0]["ruleName"] = "【apk未知指标】最近1天_低于80%"
        investigation = RouteResolver().resolve(AlertNormalizer().normalize(payload))[0]
        self.assertEqual("insufficient_definition", investigation["status"])
        self.assertEqual([0], investigation["rule_indexes"])

    def test_root_preflight_uses_current_plus_seven_days_and_freezes_root(self):
        investigation = RouteResolver().resolve(
            AlertNormalizer().normalize(payload_for(ROUTES[0]))
        )[0]
        executor = FixtureRootExecutor(current_rate=0.74, historical_rate=0.75)
        result = RootPreflight(executor=executor, repository_root=ROOT).run(investigation)
        self.assertEqual("full_queue", result["mode"])
        self.assertEqual("2026-08-24", result["analysis_date"])
        self.assertEqual(8, len(executor.calls))
        self.assertEqual(8, result["root_query_count"])
        self.assertFalse(result["root_snapshot_reused"])
        self.assertEqual(0.74, result["canonical_root_metric"]["current_value"])
        self.assertEqual(0.75, result["canonical_root_metric"]["baseline_value"])

    def test_install_date_lags_two_days_and_existing_anomaly_stops(self):
        investigation = RouteResolver().resolve(
            AlertNormalizer().normalize(payload_for(ROUTES[6]))
        )[0]
        executor = FixtureRootExecutor(current_rate=0.72, historical_rate=0.719)
        result = RootPreflight(executor=executor, repository_root=ROOT).run(investigation)
        self.assertEqual("existing_anomaly_stop", result["mode"])
        self.assertEqual("2026-08-22", result["analysis_date"])

    def test_materialized_root_rate_must_reconcile_at_four_decimals(self):
        investigation = RouteResolver().resolve(
            AlertNormalizer().normalize(payload_for(ROUTES[0]))
        )[0]
        executor = FixtureRootExecutor(materialized_offset=0.001)
        with self.assertRaises(RootPreflightError) as captured:
            RootPreflight(executor=executor, repository_root=ROOT).run(investigation)
        self.assertEqual("insufficient_definition", captured.exception.status)

    def test_exactly_five_bp_of_new_adverse_change_runs_the_full_queue(self):
        investigation = RouteResolver().resolve(
            AlertNormalizer().normalize(payload_for(ROUTES[0]))
        )[0]
        executor = FixtureRootExecutor(
            current_rate=0.7495,
            historical_rate=0.75,
            denominator=10000,
        )
        result = RootPreflight(executor=executor, repository_root=ROOT).run(
            investigation
        )
        self.assertEqual("full_queue", result["mode"])
        self.assertAlmostEqual(5.0, result["root_adverse_delta_bp"])

    def test_incomplete_eight_day_result_never_writes_a_snapshot(self):
        investigation = RouteResolver().resolve(
            AlertNormalizer().normalize(payload_for(ROUTES[0]))
        )[0]
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot_root = Path(temp_dir) / "root-snapshots"
            with self.assertRaises(RootPreflightError):
                RootPreflight(
                    executor=FixtureRootExecutor(fail_on_call=4),
                    repository_root=ROOT,
                ).run(investigation, snapshot_root=snapshot_root)
            self.assertEqual(
                [],
                list(snapshot_root.glob("*.json")) if snapshot_root.exists() else [],
            )


class TaskCoordinatorTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.run_store = MemoryArtifactStore()
        self.task_sink = MemoryTaskSink()
        self.host = FakeInvestigationHost(self.run_store)

    def coordinator(self, executor: FixtureRootExecutor) -> RegisteredAlertCoordinator:
        return RegisteredAlertCoordinator(
            investigation_host=self.host,
            root_preflight=RootPreflight(executor=executor, repository_root=ROOT),
            run_result_store=self.run_store,
            task_result_sink=self.task_sink,
            task_result_store=self.task_sink,
            tasks_root=Path(self.temp_dir.name) / ".tasks",
            repository_root=ROOT,
        )

    def test_unknown_and_successful_investigations_form_stable_partial_task(self):
        payload = payload_for(ROUTES[0])
        unknown = deepcopy(payload["ruleChecks"][0])
        unknown["ruleName"] = "【apk未知指标】最近1天_低于80%"
        payload["ruleChecks"] = [unknown, payload["ruleChecks"][0]]
        coordinator = self.coordinator(
            FixtureRootExecutor(current_rate=0.74, historical_rate=0.75)
        )

        first = coordinator.run_task(task_id="task-partial", dqc_payload=payload)
        self.assertEqual("write_conclusion", first["action"])
        self.assertNotIn("run_id", first)
        second = coordinator.finalize(
            task_id="task-partial",
            investigation_id=first["investigation_id"],
            writer_patch=writer_patch("规则未命中注册定义。"),
        )
        self.assertEqual("write_conclusion", second["action"])
        self.assertIn("run_id", second)
        completed = coordinator.finalize(
            task_id="task-partial",
            investigation_id=second["investigation_id"],
            writer_patch=writer_patch(),
        )
        self.assertEqual("task_complete", completed["action"])
        self.assertEqual("partial", completed["overall_status"])
        self.assertEqual(
            ["insufficient_definition", "no_dominant_slice"],
            [
                item["status"]
                for item in completed["analysis_preview"]["investigations"]
            ],
        )
        encoded = json.dumps([first, second, completed], ensure_ascii=False)
        for marker in ("SELECT ", "query_id", "raw_result", "private-root"):
            self.assertNotIn(marker, encoded)

        resumed = coordinator.run_task(task_id="task-partial", dqc_payload=payload)
        self.assertEqual(completed, resumed)
        authoritative = self.task_sink.load("task-partial")
        self.assertEqual(
            completed["validation_receipt"]["analysis_sha256"],
            authoritative["validation_receipt"]["analysis_sha256"],
        )
        bundle_sha256 = RepositoryContracts(ROOT).definition_bundle_sha256
        self.assertEqual(
            bundle_sha256,
            authoritative["validation_receipt"]["definition_bundle_sha256"],
        )
        state = json.loads(
            (
                Path(self.temp_dir.name)
                / ".tasks"
                / "task-partial"
                / "state.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(bundle_sha256, state["definition_bundle_sha256"])
        self.assertNotIn(
            "definition_bundle_sha256",
            completed["validation_receipt"],
        )

    def test_same_scope_metrics_reuse_one_complete_root_snapshot(self):
        executor = FixtureRootExecutor(current_rate=0.74, historical_rate=0.75)
        coordinator = self.coordinator(executor)
        first = coordinator.run_task(
            task_id="task-shared-root",
            dqc_payload=payload_for(ROUTES[0], ROUTES[3], ROUTES[4]),
        )
        self.assertEqual(8, len(executor.calls))
        second = coordinator.finalize(
            task_id="task-shared-root",
            investigation_id=first["investigation_id"],
            writer_patch=writer_patch(),
        )
        self.assertEqual(8, len(executor.calls))
        third = coordinator.finalize(
            task_id="task-shared-root",
            investigation_id=second["investigation_id"],
            writer_patch=writer_patch(),
        )
        self.assertEqual(8, len(executor.calls))
        completed = coordinator.finalize(
            task_id="task-shared-root",
            investigation_id=third["investigation_id"],
            writer_patch=writer_patch(),
        )
        snapshots = list(
            (
                Path(self.temp_dir.name)
                / ".tasks"
                / "task-shared-root"
                / "root-snapshots"
            ).glob("*.json")
        )
        self.assertEqual(1, len(snapshots))
        self.assertEqual(0o600, stat.S_IMODE(snapshots[0].stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE(snapshots[0].parent.stat().st_mode))
        authoritative = self.task_sink.load("task-shared-root")
        self.assertEqual(
            1,
            len(
                authoritative["validation_receipt"][
                    "root_snapshot_sha256s"
                ]
            ),
        )
        self.assertNotIn("snapshot", json.dumps(completed, ensure_ascii=False))

    def test_app_and_sandbox_use_separate_root_snapshots(self):
        executor = FixtureRootExecutor(current_rate=0.74, historical_rate=0.75)
        coordinator = self.coordinator(executor)
        first = coordinator.run_task(
            task_id="task-two-root-scopes",
            dqc_payload=payload_for(ROUTES[0], ROUTES[9]),
        )
        second = coordinator.finalize(
            task_id="task-two-root-scopes",
            investigation_id=first["investigation_id"],
            writer_patch=writer_patch(),
        )
        self.assertEqual("write_conclusion", second["action"])
        self.assertEqual(16, len(executor.calls))
        self.assertEqual({"app", "sandbox"}, {item[1] for item in executor.calls})

    def test_completed_root_snapshot_is_reused_after_coordinator_restart(self):
        payload = payload_for(ROUTES[0], ROUTES[3])
        first_executor = FixtureRootExecutor(
            current_rate=0.74, historical_rate=0.75
        )
        first_coordinator = self.coordinator(first_executor)
        first = first_coordinator.run_task(
            task_id="task-root-restart",
            dqc_payload=payload,
        )
        self.assertEqual(8, len(first_executor.calls))

        resumed_executor = FixtureRootExecutor(
            current_rate=0.10, historical_rate=0.20
        )
        resumed = self.coordinator(resumed_executor).finalize(
            task_id="task-root-restart",
            investigation_id=first["investigation_id"],
            writer_patch=writer_patch(),
        )
        self.assertEqual("write_conclusion", resumed["action"])
        self.assertEqual([], resumed_executor.calls)
        state = json.loads(
            (
                Path(self.temp_dir.name)
                / ".tasks"
                / "task-root-restart"
                / "state.json"
            ).read_text(encoding="utf-8")
        )
        self.assertTrue(state["investigations"][1]["root_preflight"]["root_snapshot_reused"])
        self.assertEqual(0, state["investigations"][1]["root_preflight"]["root_query_count"])

    def test_blocked_investigation_does_not_prevent_the_next_writer_pack(self):
        payload = payload_for(ROUTES[0], ROUTES[3])
        coordinator = self.coordinator(FixtureRootExecutor(fail_first=True))
        first = coordinator.run_task(task_id="task-blocked", dqc_payload=payload)
        self.assertEqual("query_blocked", first["writer_pack"]["result_status_hint"])
        second = coordinator.finalize(
            task_id="task-blocked",
            investigation_id=first["investigation_id"],
            writer_patch=writer_patch("根指标查询受权限阻断。"),
        )
        self.assertEqual("write_conclusion", second["action"])
        self.assertNotEqual(first["investigation_id"], second["investigation_id"])

    def test_task_writer_pack_enforces_twelve_kibibyte_budget(self):
        coordinator = self.coordinator(
            FixtureRootExecutor(current_rate=0.74, historical_rate=0.75)
        )
        original = self.host.xuanji_run_investigation

        def oversized_run(**kwargs):
            result = original(**kwargs)
            result["writer_pack"]["evidence_limits"] = ["x" * (12 * 1024)]
            return result

        self.host.xuanji_run_investigation = oversized_run
        with self.assertRaisesRegex(TaskCoordinatorError, "12 KB context budget"):
            coordinator.run_task(
                task_id="task-oversized",
                dqc_payload=payload_for(ROUTES[0]),
            )

    def test_long_task_ids_produce_distinct_internal_run_ids(self):
        payload = payload_for(ROUTES[0])
        prefix = "task-" + "x" * 90
        first = self.coordinator(
            FixtureRootExecutor(current_rate=0.74, historical_rate=0.75)
        ).run_task(task_id=prefix + "-a", dqc_payload=payload)
        second = self.coordinator(
            FixtureRootExecutor(current_rate=0.74, historical_rate=0.75)
        ).run_task(task_id=prefix + "-b", dqc_payload=payload)
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertLessEqual(len(first["run_id"]), 128)

    def test_unexpected_root_exception_keeps_task_retryable(self):
        coordinator = self.coordinator(UnexpectedRootExecutor())
        with self.assertRaises(KeyError):
            coordinator.run_task(
                task_id="task-root-operational",
                dqc_payload=payload_for(ROUTES[0]),
            )
        state = json.loads(
            (
                Path(self.temp_dir.name)
                / ".tasks"
                / "task-root-operational"
                / "state.json"
            ).read_text(encoding="utf-8")
        )
        investigation = state["investigations"][0]
        self.assertEqual("pending", investigation["status"])
        self.assertIsNone(investigation["result"])
        self.assertNotEqual("query_failed", investigation.get("result_status"))

    def test_corrupt_root_snapshot_is_operational_not_query_failed(self):
        payload = payload_for(ROUTES[0], ROUTES[3])
        coordinator = self.coordinator(
            FixtureRootExecutor(current_rate=0.74, historical_rate=0.75)
        )
        first = coordinator.run_task(
            task_id="task-corrupt-root",
            dqc_payload=payload,
        )
        snapshot = next(
            (
                Path(self.temp_dir.name)
                / ".tasks"
                / "task-corrupt-root"
                / "root-snapshots"
            ).glob("*.json")
        )
        document = json.loads(snapshot.read_text(encoding="utf-8"))
        document["raw_results"][0]["rows"][0][0] = "changed"
        snapshot.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(RootSnapshotError):
            coordinator.finalize(
                task_id="task-corrupt-root",
                investigation_id=first["investigation_id"],
                writer_patch=writer_patch(),
            )
        state = json.loads(
            (
                Path(self.temp_dir.name)
                / ".tasks"
                / "task-corrupt-root"
                / "state.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual("pending", state["investigations"][1]["status"])
        self.assertIsNone(state["investigations"][1]["result"])

    def test_unexpected_full_queue_exception_keeps_task_retryable(self):
        executor = FixtureRootExecutor(current_rate=0.74, historical_rate=0.75)
        coordinator = self.coordinator(executor)
        original = self.host.xuanji_run_investigation

        def fail_operationally(**kwargs):
            raise RuntimeError("private run state is unavailable")

        self.host.xuanji_run_investigation = fail_operationally
        with self.assertRaises(RuntimeError):
            coordinator.run_task(
                task_id="task-queue-operational",
                dqc_payload=payload_for(ROUTES[0]),
            )
        self.assertEqual(8, len(executor.calls))
        state = json.loads(
            (
                Path(self.temp_dir.name)
                / ".tasks"
                / "task-queue-operational"
                / "state.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual("pending", state["investigations"][0]["status"])
        self.assertIsNone(state["investigations"][0]["result"])

        self.host.xuanji_run_investigation = original
        resumed = coordinator.run_task(
            task_id="task-queue-operational",
            dqc_payload=payload_for(ROUTES[0]),
        )
        self.assertEqual("write_conclusion", resumed["action"])
        self.assertEqual(8, len(executor.calls))


if __name__ == "__main__":
    unittest.main()
