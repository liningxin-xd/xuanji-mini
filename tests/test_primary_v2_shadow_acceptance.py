from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from runtime.contracts import RepositoryContracts, canonical_sha256
from scripts.primary_v2_shadow_acceptance import (
    ShadowAcceptanceError,
    verify_shadow,
)


ROOT = Path(__file__).resolve().parents[1]


class PrimaryV2ShadowAcceptanceTest(unittest.TestCase):
    def test_documented_shadow_verifier_scripts_are_directly_executable(self):
        for script in (
            "primary_v1_shadow_acceptance.py",
            "primary_v2_shadow_acceptance.py",
        ):
            with self.subTest(script=script):
                completed = subprocess.run(
                    [sys.executable, str(ROOT / "scripts" / script), "--help"],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(0, completed.returncode, completed.stderr)

    def test_profile_aware_fixture_passes_with_bounded_query_counts(self):
        with tempfile.TemporaryDirectory() as temp:
            root, transcript = self._fixture(Path(temp))
            result = verify_shadow(
                data_root=root,
                task_id="shadow-v2-task",
                scenario="app-download",
                transcript_path=transcript,
            )
            self.assertEqual("passed", result["status"])
            self.assertEqual("primary_v2", result["analysis_profile"])
            self.assertEqual(8, result["root_query_count"])
            self.assertEqual(7, result["primary_query_count"])
            self.assertEqual(0, result["post_primary_query_count"])
            self.assertTrue(
                result["evidence_limits"]["normal_release_repair_free"]
            )

    def test_profile_or_contract_hash_drift_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root, transcript = self._fixture(Path(temp))
            path = root / "runs" / "shadow-v2-run" / "state.json"
            state = json.loads(path.read_text(encoding="utf-8"))
            state["analysis_profile_sha256"] = "0" * 64
            self._write_state(path, state)
            with self.assertRaises(ShadowAcceptanceError):
                verify_shadow(
                    data_root=root,
                    task_id="shadow-v2-task",
                    scenario="app-download",
                    transcript_path=transcript,
                )

    def test_post_primary_step_order_or_query_cap_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root, transcript = self._fixture(Path(temp))
            path = root / "runs" / "shadow-v2-run" / "state.json"
            state = json.loads(path.read_text(encoding="utf-8"))
            background = state["post_primary"]["steps"][2]
            background.update(
                {
                    "status": "succeeded",
                    "cursor": 4,
                    "items": [
                        self._query_owner(f"background-{index}")
                        for index in range(4)
                    ],
                }
            )
            self._write_state(path, state)
            with self.assertRaises(ShadowAcceptanceError):
                verify_shadow(
                    data_root=root,
                    task_id="shadow-v2-task",
                    scenario="app-download",
                    transcript_path=transcript,
                )

    def test_unselected_enhancement_query_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root, transcript = self._fixture(Path(temp))
            path = root / "runs" / "shadow-v2-run" / "state.json"
            state = json.loads(path.read_text(encoding="utf-8"))
            state["post_primary"]["steps"][4].update(
                self._query_owner("unselected-error-code")
            )
            self._write_state(path, state)
            with self.assertRaises(ShadowAcceptanceError):
                verify_shadow(
                    data_root=root,
                    task_id="shadow-v2-task",
                    scenario="app-download",
                    transcript_path=transcript,
                )

    def test_succeeded_query_step_without_attempt_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root, transcript = self._fixture(Path(temp))
            path = root / "runs" / "shadow-v2-run" / "state.json"
            state = json.loads(path.read_text(encoding="utf-8"))
            state["post_primary"]["steps"][1].update(
                {"status": "succeeded", "reason": None}
            )
            self._write_state(path, state)
            with self.assertRaises(ShadowAcceptanceError):
                verify_shadow(
                    data_root=root,
                    task_id="shadow-v2-task",
                    scenario="app-download",
                    transcript_path=transcript,
                )

    def test_transcript_private_evidence_or_full_receipt_fails_closed(self):
        leaks = (
            '{"query_id":"private-query"}\n',
            '{"action":"write_conclusion","rows":[["private-row",123]]}\n',
            "SELECT secret FROM private_table\n",
            json.dumps(
                {
                    "action": "task_complete",
                    "task_id": "shadow-v2-task",
                    "validation_receipt": {
                        "validation_receipt_sha256": "0" * 64,
                        "execution_mode": "private",
                    },
                }
            ),
        )
        for leak in leaks:
            with self.subTest(leak=leak), tempfile.TemporaryDirectory() as temp:
                root, transcript = self._fixture(Path(temp))
                transcript.write_text(leak, encoding="utf-8")
                with self.assertRaises(ShadowAcceptanceError):
                    verify_shadow(
                        data_root=root,
                        task_id="shadow-v2-task",
                        scenario="app-download",
                        transcript_path=transcript,
                    )

    def test_transcript_permissions_must_remain_private(self):
        with tempfile.TemporaryDirectory() as temp:
            root, transcript = self._fixture(Path(temp))
            os.chmod(transcript, 0o644)
            with self.assertRaisesRegex(
                ShadowAcceptanceError, "transcript permissions"
            ):
                verify_shadow(
                    data_root=root,
                    task_id="shadow-v2-task",
                    scenario="app-download",
                    transcript_path=transcript,
                )

    def test_nested_task_complete_and_bounded_repair_are_supported(self):
        with tempfile.TemporaryDirectory() as temp:
            root, transcript = self._fixture(Path(temp))
            task_complete = transcript.read_text(encoding="utf-8").strip()
            transcript.write_text(
                json.dumps({"type": "tool_result", "content": task_complete})
                + "\n",
                encoding="utf-8",
            )
            result = verify_shadow(
                data_root=root,
                task_id="shadow-v2-task",
                scenario="app-download",
                transcript_path=transcript,
            )
            self.assertEqual("passed", result["status"])

            repair = {
                "action": "repair_required",
                "repair": {
                    "rendered_sql_sha256": "e" * 64,
                    "repaired_sql": "SELECT bounded FROM locked_fixture",
                },
            }
            transcript.write_text(
                json.dumps(repair) + "\n" + task_complete + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(ShadowAcceptanceError):
                verify_shadow(
                    data_root=root,
                    task_id="shadow-v2-task",
                    scenario="app-download",
                    transcript_path=transcript,
                )
            repaired = verify_shadow(
                data_root=root,
                task_id="shadow-v2-task",
                scenario="app-download",
                transcript_path=transcript,
                allow_repair=True,
            )
            self.assertFalse(
                repaired["evidence_limits"]["normal_release_repair_free"]
            )

    def test_run_sink_and_task_investigation_must_match(self):
        with tempfile.TemporaryDirectory() as temp:
            root, transcript = self._fixture(Path(temp))
            path = (
                root
                / "results"
                / "shadow-v2-run"
                / "validated-result.json"
            )
            sink = json.loads(path.read_text(encoding="utf-8"))
            sink["analysis"]["investigations"][0]["status"] = "completed"
            self._write_private(path, sink)
            with self.assertRaises(ShadowAcceptanceError):
                verify_shadow(
                    data_root=root,
                    task_id="shadow-v2-task",
                    scenario="app-download",
                    transcript_path=transcript,
                )

    def test_transcript_handoff_identity_tampering_fails_closed(self):
        fields = (
            "payload_sha256",
            "analysis_preview_sha256",
            "validation_receipt_sha256",
        )
        for field in fields:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp:
                root, transcript = self._fixture(Path(temp))
                value = json.loads(transcript.read_text(encoding="utf-8"))
                value["pipeline_handoff"][field] = "0" * 64
                transcript.write_text(
                    json.dumps(value, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                with self.assertRaises(ShadowAcceptanceError):
                    verify_shadow(
                        data_root=root,
                        task_id="shadow-v2-task",
                        scenario="app-download",
                        transcript_path=transcript,
                    )

    def _fixture(self, root: Path) -> tuple[Path, Path]:
        contracts = RepositoryContracts(ROOT)
        task_id = "shadow-v2-task"
        run_id = "shadow-v2-run"
        snapshot = self._snapshot()
        snapshot_path = (
            root / "tasks" / task_id / "root-snapshots" / ("a" * 64 + ".json")
        )
        self._write_private(snapshot_path, snapshot)

        plan = contracts.select_plan("download", "app", "下载完成率")
        primary_steps = []
        for index, plan_step in enumerate(plan.steps):
            owner = self._query_owner(f"primary-{index}")
            owner.update({"id": plan_step.id, "status": "succeeded"})
            primary_steps.append(owner)
        post_plan = contracts.post_primary_plan("post_primary_v1")
        enhancement_id = post_plan["enhancement_priority_plan"]
        enhancement_contract = contracts.enhancement_priority_plan(enhancement_id)
        enhancement_modules = []
        for module in enhancement_contract["modules"]:
            planned = {
                "id": module["id"],
                "priority": module["priority"],
                "query_cost": module["query_cost"],
            }
            if module["runtime_status"] == "disabled":
                planned.update(
                    {"status": "skipped_by_policy", "reason": module["reason"]}
                )
            else:
                planned.update(
                    {"status": "not_triggered", "reason": "fixture_not_triggered"}
                )
            enhancement_modules.append(planned)
        post_steps = [
            {
                "id": item["id"],
                "status": (
                    "succeeded"
                    if item["id"] == "breadth_check"
                    else "skipped_by_policy"
                ),
                "reason": (
                    None
                    if item["id"] == "breadth_check"
                    else "fixture_not_triggered"
                ),
            }
            for item in post_plan["steps"]
        ]
        run_result = {
            "status": "no_dominant_slice",
            "rule_indexes": [0],
            "metric": "下载完成率",
            "analysis_date": "2026-08-24",
        }
        run_analysis = {
            "source": "dataworks_dqc",
            "overall_status": "completed",
            "investigations": [run_result],
        }
        run_receipt = {
            "status": "valid",
            "investigation_status": "no_dominant_slice",
            "execution_mode": "trusted_host_adapter",
            "validated_step_count": len(primary_steps),
            "analysis_sha256": canonical_sha256(run_analysis),
        }
        run_receipt["validation_receipt_sha256"] = canonical_sha256(run_receipt)
        run_state = {
            "schema_version": 4,
            "run_id": run_id,
            "status": "finalized",
            "plan_id": plan.id,
            "plan_contract_sha256": plan.sha256,
            "execution_plan_sha256": contracts.execution_plan_sha256,
            "query_registry_sha256": contracts.query_registry_sha256,
            "triage_sha256": contracts.triage_sha256,
            "result_schemas_sha256": contracts.result_schemas_sha256,
            "secondary_relations_sha256": contracts.secondary_relations_sha256,
            "error_code_capabilities_sha256": (
                contracts.error_code_capabilities_sha256
            ),
            "error_code_triggers_sha256": contracts.error_code_triggers_sha256,
            "enhancement_priority_sha256": contracts.enhancement_priority_sha256,
            "analysis_profile": "primary_v2",
            "analysis_profile_sha256": contracts.analysis_profile_sha256(
                "primary_v2"
            ),
            "post_primary_plan_sha256": (
                contracts.post_primary_plan_contract_sha256("post_primary_v1")
            ),
            "chain": "download",
            "game_type": "app",
            "metric": "下载完成率",
            "cursor": len(primary_steps),
            "steps": primary_steps,
            "post_primary": {
                "profile": "primary_v2",
                "plan_id": "post_primary_v1",
                "primary_evidence_sha256": "b" * 64,
                "status": "completed",
                "enhancement_plan": {
                    "plan_id": enhancement_id,
                    "plan_contract_sha256": (
                        contracts.enhancement_priority_plan_contract_sha256(
                            enhancement_id
                        )
                    ),
                    "frozen_evidence_sha256": "b" * 64,
                    "max_query_modules": 2,
                    "query_module_count": 0,
                    "selected_modules": [],
                    "modules": enhancement_modules,
                    "evidence_limits": [],
                },
                "steps": post_steps,
            },
            "final_analysis_sha256": run_receipt["analysis_sha256"],
            "validation_receipt": run_receipt,
        }
        run_state["integrity_sha256"] = canonical_sha256(run_state)
        self._write_private(root / "runs" / run_id / "state.json", run_state)
        self._write_private(
            root / "runs" / run_id / "exports" / "writer-pack.json",
            {
                "analysis_profile": "primary_v2",
                "post_primary_steps": post_steps,
                "candidates": [],
            },
        )
        self._write_private(
            root / "results" / run_id / "validated-result.json",
            {
                "run_id": run_id,
                "analysis": run_analysis,
                "validation_receipt": run_receipt,
            },
        )

        investigation = {
            "investigation_id": "inv-00-fixture",
            "rule_indexes": [0],
            "alert_date": "2026-08-24",
            "route": {
                "chain": "download",
                "game_type": "app",
                "canonical_metric": "下载完成率",
            },
            "root_preflight": {
                "analysis_date": "2026-08-24",
                "root_snapshot_sha256": snapshot["snapshot_sha256"],
            },
            "machine_mode": "full_queue",
            "run_id": run_id,
            "status": "completed",
            "result": run_result,
            "validation_receipt": run_receipt,
        }
        task_analysis = {
            "source": "dataworks_dqc",
            "project": "tap_dw",
            "table": "tap_dw.ads_dmg_quality_platform_download_chain_monitor_1d",
            "partition": "dt=2026-08-24",
            "overall_status": "completed",
            "investigations": [run_result],
        }
        payload_sha256 = "c" * 64
        task_receipt = {
            "status": "valid",
            "task_id": task_id,
            "payload_sha256": payload_sha256,
            "definition_bundle_sha256": contracts.definition_bundle_sha256,
            "overall_status": "completed",
            "investigation_count": 1,
            "successful_investigation_count": 1,
            "rule_indexes_sha256": canonical_sha256([0]),
            "analysis_sha256": canonical_sha256(task_analysis),
            "root_snapshot_sha256s": [snapshot["snapshot_sha256"]],
            "investigation_receipts": [
                {
                    key: run_receipt[key]
                    for key in (
                        "status",
                        "investigation_status",
                        "execution_mode",
                        "validated_step_count",
                        "analysis_sha256",
                        "validation_receipt_sha256",
                    )
                }
            ],
        }
        task_receipt["validation_receipt_sha256"] = canonical_sha256(task_receipt)
        task_state = {
            "schema_version": 1,
            "task_id": task_id,
            "payload_sha256": payload_sha256,
            "definition_bundle_sha256": contracts.definition_bundle_sha256,
            "analysis_profile": "primary_v2",
            "status": "completed",
            "overall_status": "completed",
            "normalized_alert": {"rules": [{"rule_name": "fixture"}]},
            "investigations": [investigation],
            "task_analysis_sha256": task_receipt["analysis_sha256"],
            "task_validation_receipt_sha256": task_receipt[
                "validation_receipt_sha256"
            ],
        }
        task_state["integrity_sha256"] = canonical_sha256(task_state)
        self._write_private(root / "tasks" / task_id / "state.json", task_state)
        self._write_private(
            root
            / "results"
            / "tasks"
            / task_id
            / "validated-task-result.json",
            {
                "task_id": task_id,
                "analysis": task_analysis,
                "validation_receipt": task_receipt,
            },
        )

        compact_receipt = {
            key: task_receipt[key]
            for key in (
                "status",
                "overall_status",
                "investigation_count",
                "successful_investigation_count",
                "analysis_sha256",
                "validation_receipt_sha256",
            )
        }
        transcript = root / "model-transcript.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "action": "task_complete",
                    "task_id": task_id,
                    "overall_status": "completed",
                    "analysis_preview": task_analysis,
                    "validation_receipt": compact_receipt,
                    "pipeline_handoff": {
                        "schema_version": 1,
                        "provider": "xuanji-mini",
                        "task_id": task_id,
                        "payload_sha256": payload_sha256,
                        "analysis_preview_sha256": canonical_sha256(task_analysis),
                        "validation_receipt_sha256": task_receipt[
                            "validation_receipt_sha256"
                        ],
                        "signing_key_id": "fixture.pipeline-handoff-ed25519-v1",
                        "signature": "fixture-signature",
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(transcript, 0o600)
        return root, transcript

    @staticmethod
    def _query_owner(identity: str) -> dict:
        binding = {"fixture": identity}
        return {
            "binding": binding,
            "binding_sha256": canonical_sha256(binding),
            "attempts": [
                {
                    "attempt_no": 0,
                    "status": "succeeded",
                    "sql_sha256": canonical_sha256(identity),
                    "query_id": f"private-{identity}",
                }
            ],
        }

    @staticmethod
    def _snapshot() -> dict:
        unsigned = {
            "schema_version": 1,
            "scope": {"name": "app"},
            "raw_results": [{} for _ in range(8)],
            "private_queries": [
                {"query_id": f"private-root-app-{index}"} for index in range(8)
            ],
        }
        return {**unsigned, "snapshot_sha256": canonical_sha256(unsigned)}

    @classmethod
    def _write_state(cls, path: Path, state: dict) -> None:
        state.pop("integrity_sha256", None)
        state["integrity_sha256"] = canonical_sha256(state)
        cls._write_private(path, state)

    @staticmethod
    def _write_private(path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        os.chmod(path, 0o600)


if __name__ == "__main__":
    unittest.main()
