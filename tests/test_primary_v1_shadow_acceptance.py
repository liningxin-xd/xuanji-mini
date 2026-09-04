from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from runtime.contracts import canonical_sha256
from scripts.primary_v1_shadow_acceptance import (
    ShadowAcceptanceError,
    verify_shadow,
)


class PrimaryV1ShadowAcceptanceTest(unittest.TestCase):
    def test_all_three_shadow_shapes_pass_with_expected_root_counts(self):
        scenarios = (
            ("same-metric", 8),
            ("same-scope", 8),
            ("mixed-scope", 16),
        )
        for scenario, expected_queries in scenarios:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temp:
                root, transcript = self._fixture(Path(temp), scenario)
                result = verify_shadow(
                    data_root=root,
                    task_id="shadow-task",
                    scenario=scenario,
                    transcript_path=transcript,
                )
                self.assertEqual("passed", result["status"])
                self.assertEqual(expected_queries, result["root_query_count"])
                self.assertEqual(
                    "shadow-task", result["idempotency"]["task_id"]
                )

    def test_transcript_sql_or_private_keys_fail_closed(self):
        leaks = (
            '{"query_id":"private"}',
            "SELECT secret FROM private_table",
            "/var/lib/xuanji/tasks/private/state.json",
            "private-root-snap-app-0",
        )
        for leaked in leaks:
            with self.subTest(leaked=leaked), tempfile.TemporaryDirectory() as temp:
                root, transcript = self._fixture(Path(temp), "same-metric")
                transcript.write_text(leaked, encoding="utf-8")
                with self.assertRaises(ShadowAcceptanceError):
                    verify_shadow(
                        data_root=root,
                        task_id="shadow-task",
                        scenario="same-metric",
                        transcript_path=transcript,
                    )

    def test_task_receipt_tampering_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root, transcript = self._fixture(Path(temp), "same-scope")
            sink_path = (
                root
                / "results"
                / "tasks"
                / "shadow-task"
                / "validated-task-result.json"
            )
            sink = json.loads(sink_path.read_text(encoding="utf-8"))
            sink["analysis"]["overall_status"] = "failed"
            self._write_private(sink_path, sink)
            with self.assertRaises(ShadowAcceptanceError):
                verify_shadow(
                    data_root=root,
                    task_id="shadow-task",
                    scenario="same-scope",
                    transcript_path=transcript,
                )

    def _fixture(self, root: Path, scenario: str) -> tuple[Path, Path]:
        definitions = {
            "same-metric": [
                ("download", "app", "下载完成率", [0, 1, 2], "snap-app"),
            ],
            "same-scope": [
                ("download", "app", "下载完成率", [0], "snap-app"),
                ("download", "app", "下载失败率", [1], "snap-app"),
                ("download", "app", "下载人为停止率", [2], "snap-app"),
            ],
            "mixed-scope": [
                ("download", "app", "下载完成率", [0], "snap-app"),
                ("install", "app", "下载安装完成率", [1], "snap-app"),
                ("download", "sandbox", "下载完成率", [2], "snap-sandbox"),
            ],
        }[scenario]
        task_id = "shadow-task"
        alert_date = "2026-08-24"
        snapshots = {}
        investigations = []
        analysis_investigations = []
        for index, definition in enumerate(definitions):
            chain, game_type, metric, rule_indexes, snapshot_key = definition
            snapshot = snapshots.setdefault(snapshot_key, self._snapshot(snapshot_key))
            run_id = f"shadow-task-run-{index}"
            analysis_date = (
                date.fromisoformat(alert_date)
                - timedelta(days=2 if chain == "install" else 0)
            ).isoformat()
            result = {
                "status": "no_dominant_slice",
                "rule_indexes": rule_indexes,
                "metric": metric,
                "analysis_date": analysis_date,
                "attribution_execution": {
                    "mode": "full_queue",
                    "chain": chain,
                    "game_type": game_type,
                    "steps": [],
                },
            }
            investigations.append(
                {
                    "investigation_id": f"inv-{index:02d}-fixture",
                    "rule_indexes": rule_indexes,
                    "alert_date": alert_date,
                    "route": {
                        "chain": chain,
                        "game_type": game_type,
                        "canonical_metric": metric,
                    },
                    "root_preflight": {
                        "analysis_date": analysis_date,
                        "root_snapshot_sha256": snapshot["snapshot_sha256"],
                    },
                    "run_id": run_id,
                    "status": "completed",
                    "result": result,
                }
            )
            analysis_investigations.append(result)
            run_root = root / "runs" / run_id
            step_ids = (
                [
                    "game_id",
                    "install_stage",
                    "device_brand",
                    "storage_headroom_tier",
                    "os_major_version",
                    "apk_size_tier",
                ]
                if chain == "install"
                else [
                    "game_id",
                    "is_reserve_auto_download",
                    "device_brand",
                    "channel_group",
                    "app_major_version",
                    "os_major_version",
                    "apk_size_tier",
                ]
            )
            run_state = {
                "schema_version": 4,
                "run_id": run_id,
                "status": "finalized",
                "cursor": len(step_ids),
                "steps": [
                    {
                        "id": step_id,
                        "status": "succeeded",
                        "attempts": [
                            {"query_id": f"private-{index}-{step_index}"}
                        ],
                    }
                    for step_index, step_id in enumerate(step_ids)
                ],
            }
            run_state["integrity_sha256"] = canonical_sha256(run_state)
            self._write_private(
                run_root / "state.json",
                run_state,
            )
            self._write_private(
                run_root / "exports" / "writer-pack.json",
                {"analysis_profile": "primary_v1", "candidates": []},
            )

        snapshot_root = root / "tasks" / task_id / "root-snapshots"
        for index, snapshot in enumerate(snapshots.values()):
            self._write_private(snapshot_root / f"{index:064x}.json", snapshot)
        analysis = {
            "source": "dataworks_dqc",
            "overall_status": "completed",
            "investigations": analysis_investigations,
        }
        receipt = {
            "status": "valid",
            "task_id": task_id,
            "analysis_sha256": canonical_sha256(analysis),
            "root_snapshot_sha256s": [
                value["snapshot_sha256"] for value in snapshots.values()
            ],
        }
        receipt["validation_receipt_sha256"] = canonical_sha256(receipt)
        state = {
            "schema_version": 1,
            "task_id": task_id,
            "status": "completed",
            "investigations": investigations,
        }
        state["integrity_sha256"] = canonical_sha256(state)
        self._write_private(root / "tasks" / task_id / "state.json", state)
        self._write_private(
            root
            / "results"
            / "tasks"
            / task_id
            / "validated-task-result.json",
            {
                "schema_version": 1,
                "task_id": task_id,
                "analysis": analysis,
                "validation_receipt": receipt,
            },
        )
        transcript = root / "model-transcript.jsonl"
        transcript.write_text(
            '{"action":"task_complete","task_id":"shadow-task"}\n',
            encoding="utf-8",
        )
        return root, transcript

    @staticmethod
    def _snapshot(name: str) -> dict:
        unsigned = {
            "schema_version": 1,
            "scope": {"name": name},
            "raw_results": [{} for _ in range(8)],
            "private_queries": [
                {"query_id": f"private-root-{name}-{index}"} for index in range(8)
            ],
        }
        return {**unsigned, "snapshot_sha256": canonical_sha256(unsigned)}

    @staticmethod
    def _write_private(path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        os.chmod(path, 0o600)


if __name__ == "__main__":
    unittest.main()
