import copy
import json
import tempfile
import unittest
from pathlib import Path

from runtime.evidence_pack import EvidencePackBuilder
from runtime.final_assembler import FinalAssembler, FinalAssemblyError
from runtime.final_validator import FinalEvidenceValidator, FinalValidationError
from runtime.runner import AttributionRunner
from tests.runtime_result_fixtures import raw_result_for_ticket, self_reported_result_event


ROOT = Path(__file__).resolve().parents[1]


class WriterPackRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.runner = AttributionRunner(ROOT, runs_root=self.temp_dir.name)
        self.runner.init_run(
            run_id="writer-run",
            chain="download",
            game_type="app",
            metric="下载完成率",
            alert_date="2026-08-22",
            receipt_mode="self_reported",
        )
        while True:
            ticket = self.runner.next_action("writer-run")
            if ticket["action"] == "queue_complete":
                break
            raw_result = raw_result_for_ticket(
                self.runner,
                "writer-run",
                ticket,
                candidate=ticket["step_id"] == "game_id",
            )
            self.runner.record(
                "writer-run",
                self_reported_result_event(
                    ticket, raw_result, f"writer-{ticket['step_id']}"
                ),
            )

    def _context(self):
        return {
            "source": "dataworks_dqc",
            "project": "tap_dw",
            "table": "tap_dw.ads_dmg_quality_platform_download_chain_monitor_1d",
            "partition": "dt=2026-08-22",
            "investigation": {
                "rule_indexes": [0],
                "metric_hint": "下载完成率",
                "alert_partition": "dt=2026-08-22",
                "alert_rules": [{"rule_name": "【APK下载完成率】最近1天低于阈值"}],
            },
        }

    def _patch(self, candidate_id):
        return {
            "summary": "下载完成率已复现相对基线下降。",
            "finding_texts": {candidate_id: "Slice A 的变化达到候选门槛。"},
            "evidence_limits": ["当前证据只能定位切片，不能确认机制根因。"],
            "recommended_action": "核查 Slice A 的下载链路与版本变化。",
        }

    def test_writer_pack_exposes_only_compact_machine_selected_candidates(self):
        pack = self.runner.build_writer_pack("writer-run")
        self.assertEqual("primary_v1", pack["analysis_profile"])
        self.assertEqual("completed", pack["result_status_hint"])
        self.assertEqual("game_id:12345", pack["candidates"][0]["candidate_id"])
        state_candidate = self.runner.load_state("writer-run")["steps"][0][
            "candidates"
        ][0]
        self.assertEqual(
            state_candidate["adverse_impact_bp"],
            pack["candidates"][0]["adverse_impact_bp"],
        )
        self.assertEqual(0.79, pack["root_metric"]["current_value"])
        self.assertEqual(0.8, pack["root_metric"]["baseline_value"])
        encoded = json.dumps(pack, ensure_ascii=False)
        self.assertNotIn("query_id", encoded)
        self.assertNotIn("raw_result", encoded)

    def test_each_family_is_capped_at_three_writer_candidates(self):
        state = self.runner.load_state("writer-run")
        game_step = state["steps"][0]
        base = game_step["candidates"][0]
        game_step["candidates"] = [
            {**copy.deepcopy(base), "value": f"slice-{index}"}
            for index in range(4)
        ]
        game_step["candidate_count"] = 4
        pack = EvidencePackBuilder().build(state)
        game_candidates = [
            item for item in pack["candidates"] if item["dimension"] == "game_id"
        ]
        self.assertEqual(3, len(game_candidates))

    def test_assembler_owns_machine_fields_and_final_validator_accepts_them(self):
        pack = self.runner.build_writer_pack("writer-run")
        candidate = pack["candidates"][0]
        analysis = self.runner.assemble_final(
            "writer-run", self._patch(candidate["candidate_id"]), self._context()
        )
        investigation = analysis["investigations"][0]
        finding = investigation["top_findings"][0]
        self.assertEqual(candidate["value"], finding["value"])
        self.assertEqual(candidate["adverse_impact_bp"], finding["adverse_impact_bp"])
        self.assertEqual(
            "self_reported_development",
            investigation["attribution_execution"]["execution_mode"],
        )
        state = self.runner.load_state("writer-run")
        self.assertEqual(
            "valid", FinalEvidenceValidator().validate(state, analysis, 0)["status"]
        )

        mutated = copy.deepcopy(analysis)
        mutated["investigations"][0]["current_value"] = 0.5
        with self.assertRaisesRegex(FinalValidationError, "frozen root facts"):
            FinalEvidenceValidator().validate(state, mutated, 0)

    def test_writer_patch_cannot_submit_machine_fields_or_unknown_candidates(self):
        pack = self.runner.build_writer_pack("writer-run")
        patch = self._patch(pack["candidates"][0]["candidate_id"])
        patch["delta_bp"] = -100
        with self.assertRaisesRegex(FinalAssemblyError, "must contain only"):
            self.runner.assemble_final("writer-run", patch, self._context())

        patch = self._patch("game_id:not-exposed")
        with self.assertRaisesRegex(FinalAssemblyError, "unknown candidates"):
            self.runner.assemble_final("writer-run", patch, self._context())

    def test_assembler_preserves_machine_evidence_limits_and_deduplicates(self):
        pack = self.runner.build_writer_pack("writer-run")
        pack["evidence_limits"] = [
            "device_brand:result_incomplete",
            "shared-limit",
        ]
        patch = self._patch(pack["candidates"][0]["candidate_id"])
        patch["evidence_limits"] = ["shared-limit", "writer-limit"]
        analysis = FinalAssembler().assemble(
            writer_pack=pack,
            attribution_execution=self.runner.export("writer-run"),
            writer_patch=patch,
            analysis_context=self._context(),
        )
        self.assertEqual(
            [
                "device_brand:result_incomplete",
                "shared-limit",
                "writer-limit",
            ],
            analysis["investigations"][0]["evidence_limits"],
        )


if __name__ == "__main__":
    unittest.main()
