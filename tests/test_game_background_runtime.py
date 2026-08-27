from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from runtime.contracts import RepositoryContracts
from runtime.game_background_selector import GameBackgroundSelector
from runtime.game_background_validator import (
    GameBackgroundValidationError,
    GameBackgroundValidator,
)
from runtime.runner import AttributionRunner
from tests.runtime_result_fixtures import (
    raw_result_for_ticket,
    self_reported_error_event,
    self_reported_result_event,
)


ROOT = Path(__file__).resolve().parents[1]


class GameBackgroundRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    def _new_runner(self, run_id: str) -> AttributionRunner:
        runner = AttributionRunner(
            ROOT,
            runs_root=self.temp_dir.name,
            analysis_profile="primary_v2",
        )
        runner.init_run(
            run_id=run_id,
            chain="download",
            game_type="app",
            metric="下载完成率",
            alert_date="2026-08-22",
            receipt_mode="self_reported",
        )
        return runner

    def _complete(
        self,
        run_id: str,
        *,
        games: list[tuple[str, int, int, int, int]],
        background_errors: set[int] | None = None,
        empty_background: set[int] | None = None,
    ) -> tuple[AttributionRunner, int, list[str]]:
        runner = self._new_runner(run_id)
        query_count = 0
        background_ids = []
        while True:
            ticket = runner.next_action(run_id)
            if ticket["action"] == "queue_complete":
                break
            query_count += 1
            if ticket["step_id"] == "game_id":
                raw = raw_result_for_ticket(
                    runner, run_id, ticket, candidate=True
                )
                self._set_game_candidates(raw, games)
            elif ticket["step_id"] == "secondary":
                raw = raw_result_for_ticket(runner, run_id, ticket)
            elif ticket["step_id"] == "game_background":
                state = runner.load_state(run_id)
                background = state["post_primary"]["steps"][2]
                item_index = background["cursor"]
                background_ids.append(ticket["parameters"]["game_id"])
                if item_index in (background_errors or set()):
                    runner.record(
                        run_id,
                        {
                            "event": "query_error",
                            "step_id": "game_background",
                            "attempt_no": ticket["attempt_no"],
                            "receipt_type": "self_reported_receipt",
                            "submitted_sql_sha256": ticket[
                                "rendered_sql_sha256"
                            ],
                            "query_id": f"private-background-{item_index}",
                            "error_class": "execution",
                            "error_code": "ODPS-PRIVATE",
                            "error_message": (
                                "SELECT internal FROM tap_dw.private_table "
                                f"query_id=private-background-{item_index}"
                            ),
                        },
                    )
                    continue
                raw = raw_result_for_ticket(runner, run_id, ticket)
                if item_index in (empty_background or set()):
                    raw["rows"] = []
            else:
                raw = raw_result_for_ticket(runner, run_id, ticket)
                self._set_root_counts(raw)
            runner.record(
                run_id,
                self_reported_result_event(
                    ticket, raw, f"{run_id}-{ticket['step_id']}-{query_count}"
                ),
            )
        return runner, query_count, background_ids

    @staticmethod
    def _set_game_candidates(
        raw: dict,
        games: list[tuple[str, int, int, int, int]],
    ) -> None:
        template = raw["rows"][0]
        residual_template = raw["rows"][1]
        root = {
            "current_denominator": 1000,
            "current_numerator": 700,
            "baseline_denominator": 1000,
            "baseline_numerator": 800,
        }
        rows = []
        for game_id, current_den, current_num, baseline_den, baseline_num in games:
            row = copy.deepcopy(template)
            row.update(
                {
                    "dimension_value": game_id,
                    "dimension_label": f"Game {game_id}",
                    "current_denominator": current_den,
                    "current_numerator": current_num,
                    "baseline_denominator": baseline_den,
                    "baseline_numerator": baseline_num,
                }
            )
            rows.append(row)
        residual = copy.deepcopy(residual_template)
        residual.update(
            {
                "current_denominator": root["current_denominator"]
                - sum(game[1] for game in games),
                "current_numerator": root["current_numerator"]
                - sum(game[2] for game in games),
                "baseline_denominator": root["baseline_denominator"]
                - sum(game[3] for game in games),
                "baseline_numerator": root["baseline_numerator"]
                - sum(game[4] for game in games),
            }
        )
        rows.append(residual)
        for row in rows:
            row.update(
                {
                    "overall_current_denominator": root["current_denominator"],
                    "overall_current_numerator": root["current_numerator"],
                    "overall_baseline_denominator": root[
                        "baseline_denominator"
                    ],
                    "overall_baseline_numerator": root["baseline_numerator"],
                }
            )
        raw["rows"] = rows

    @staticmethod
    def _set_root_counts(raw: dict) -> None:
        rows = raw["rows"]
        rows[0].update(
            {
                "current_denominator": 1,
                "current_numerator": 0,
                "baseline_denominator": 1,
                "baseline_numerator": 0,
            }
        )
        rows[1].update(
            {
                "current_denominator": 999,
                "current_numerator": 700,
                "baseline_denominator": 999,
                "baseline_numerator": 800,
            }
        )
        for row in rows:
            row.update(
                {
                    "overall_current_denominator": 1000,
                    "overall_current_numerator": 700,
                    "overall_baseline_denominator": 1000,
                    "overall_baseline_numerator": 800,
                }
            )
            if "overall_current_dimension_matched_denominator" in row:
                row.update(
                    {
                        "overall_current_dimension_matched_denominator": 1000,
                        "overall_baseline_dimension_matched_denominator": 1000,
                        "overall_current_dimension_match_rate": 1.0,
                        "overall_baseline_dimension_match_rate": 1.0,
                    }
                )

    def test_two_games_reach_threshold_and_execute_two_queries(self):
        runner, query_count, background_ids = self._complete(
            "background-two",
            games=[
                ("12345", 200, 130, 200, 160),
                ("23456", 200, 130, 200, 160),
            ],
        )
        self.assertEqual(10, query_count)
        self.assertEqual([12345, 23456], background_ids)
        background = runner.load_state("background-two")["post_primary"][
            "steps"
        ][2]
        self.assertEqual("succeeded", background["status"])
        self.assertEqual(2, background["cursor"])

        pack = runner.build_writer_pack("background-two")
        analysis = runner.assemble_final(
            "background-two",
            {
                "summary": "已完成有界归因和游戏背景校准。",
                "finding_texts": {
                    item["candidate_id"]: f"候选 {item['label']} 达到机器门槛。"
                    for item in pack["candidates"]
                },
                "evidence_limits": [],
                "recommended_action": "复核候选游戏的下载链路变化。",
            },
            {
                "source": "dataworks_dqc",
                "project": "tap_dw",
                "table": (
                    "tap_dw.ads_dmg_quality_platform_download_chain_monitor_1d"
                ),
                "partition": "dt=2026-08-22",
                "investigation": {
                    "rule_indexes": [0],
                    "metric_hint": "下载完成率",
                    "alert_partition": "dt=2026-08-22",
                    "alert_rules": [{"rule_name": "下载完成率告警"}],
                },
            },
        )
        investigation = analysis["investigations"][0]
        self.assertNotIn("game_background_findings", investigation)
        self.assertNotIn("background_candidates", investigation)
        self.assertTrue(
            all(
                finding["attribution_level"] in {"primary", "secondary"}
                for finding in investigation["top_findings"]
            )
        )

    def test_three_games_reach_threshold_and_first_failure_is_isolated(self):
        runner, query_count, background_ids = self._complete(
            "background-three",
            games=[
                ("12345", 100, 60, 100, 80),
                ("23456", 100, 60, 100, 80),
                ("34567", 100, 60, 100, 80),
            ],
            background_errors={0},
        )
        self.assertEqual(11, query_count)
        self.assertEqual([12345, 23456, 34567], background_ids)
        state = runner.load_state("background-three")
        background = state["post_primary"]["steps"][2]
        self.assertEqual(
            ["failed", "succeeded", "succeeded"],
            [item["status"] for item in background["items"]],
        )
        pack = runner.build_writer_pack("background-three")
        self.assertEqual("completed", pack["result_status_hint"])
        encoded = json.dumps(pack, ensure_ascii=False)
        for forbidden in (
            "SELECT internal",
            "tap_dw.private_table",
            "private-background-0",
            "event_detail",
            "source_snapshot_dt",
            "receipt",
            "query_id",
            "raw_result",
        ):
            self.assertNotIn(forbidden, encoded)
        self.assertLessEqual(len(encoded.encode("utf-8")), 12 * 1024)
        self.assertIn("game_id:12345:query_failed", pack["evidence_limits"])
        self.assertEqual(2, len(pack["game_background"]))
        self.assertIn(
            "SELECT internal",
            background["items"][0]["reason"],
        )

    def test_legal_empty_result_only_adds_an_evidence_limit(self):
        runner, _, _ = self._complete(
            "background-empty",
            games=[("12345", 500, 350, 500, 400)],
            empty_background={0},
        )
        pack = runner.build_writer_pack("background-empty")
        self.assertEqual([], pack["game_background"][0]["facts"])
        self.assertIn(
            "game_id:12345:no_registered_event", pack["evidence_limits"]
        )
        self.assertEqual("completed", pack["result_status_hint"])

    def test_top_three_below_threshold_execute_no_background_queries(self):
        runner, query_count, background_ids = self._complete(
            "background-threshold-skip",
            games=[
                ("12345", 100, 75, 100, 80),
                ("23456", 100, 75, 100, 80),
                ("34567", 100, 75, 100, 80),
            ],
        )
        self.assertEqual(8, query_count)
        self.assertEqual([], background_ids)
        background = runner.load_state("background-threshold-skip")[
            "post_primary"
        ]["steps"][2]
        self.assertEqual("skipped_by_policy", background["status"])
        self.assertEqual(
            "game_candidates_do_not_reach_background_threshold",
            background["reason"],
        )

    def test_all_background_failures_do_not_change_attribution_status(self):
        runner, _, _ = self._complete(
            "background-all-failed",
            games=[("12345", 500, 350, 500, 400)],
            background_errors={0},
        )
        state = runner.load_state("background-all-failed")
        self.assertEqual("succeeded", state["steps"][0]["status"])
        self.assertEqual("succeeded", state["post_primary"]["steps"][1]["status"])
        self.assertEqual("failed", state["post_primary"]["steps"][2]["status"])
        pack = runner.build_writer_pack("background-all-failed")
        self.assertEqual("completed", pack["result_status_hint"])
        self.assertIn(
            "game_id:12345:query_failed", pack["evidence_limits"]
        )

    def test_resume_starts_from_the_next_selected_game(self):
        runner = self._new_runner("background-resume")
        while True:
            ticket = runner.next_action("background-resume")
            if ticket["step_id"] == "game_background":
                break
            if ticket["step_id"] == "game_id":
                raw = raw_result_for_ticket(
                    runner, "background-resume", ticket, candidate=True
                )
                self._set_game_candidates(
                    raw,
                    [
                        ("12345", 200, 130, 200, 160),
                        ("23456", 200, 130, 200, 160),
                    ],
                )
            elif ticket["step_id"] == "secondary":
                raw = raw_result_for_ticket(runner, "background-resume", ticket)
            else:
                raw = raw_result_for_ticket(runner, "background-resume", ticket)
                self._set_root_counts(raw)
            runner.record(
                "background-resume",
                self_reported_result_event(
                    ticket, raw, f"resume-{ticket['step_id']}"
                ),
            )
        runner.record(
            "background-resume",
            self_reported_result_event(
                ticket,
                raw_result_for_ticket(
                    runner, "background-resume", ticket
                ),
                "resume-background-first",
            ),
        )
        restarted = AttributionRunner(
            ROOT,
            runs_root=self.temp_dir.name,
            analysis_profile="primary_v2",
        )
        restarted.init_run(
            run_id="background-resume",
            chain="download",
            game_type="app",
            metric="下载完成率",
            alert_date="2026-08-22",
            receipt_mode="self_reported",
            resume=True,
        )
        next_ticket = restarted.next_action("background-resume")
        self.assertEqual(23456, next_ticket["parameters"]["game_id"])
        first = restarted.load_state("background-resume")["post_primary"][
            "steps"
        ][2]["items"][0]
        self.assertEqual("succeeded", first["status"])
        self.assertEqual(1, len(first["attempts"]))

    def test_secondary_failure_does_not_prevent_background_selection(self):
        runner = self._new_runner("background-after-secondary-failure")
        while True:
            ticket = runner.next_action("background-after-secondary-failure")
            if ticket["step_id"] == "secondary":
                runner.record(
                    "background-after-secondary-failure",
                    {
                        "event": "query_error",
                        "step_id": "secondary",
                        "attempt_no": ticket["attempt_no"],
                        "receipt_type": "self_reported_receipt",
                        "submitted_sql_sha256": ticket["rendered_sql_sha256"],
                        "query_id": "secondary-private-failure",
                        "error_class": "execution",
                        "error_code": "ODPS-PRIVATE",
                        "error_message": "private secondary failure",
                    },
                )
                break
            raw = raw_result_for_ticket(
                runner,
                "background-after-secondary-failure",
                ticket,
                candidate=ticket["step_id"] == "game_id",
            )
            runner.record(
                "background-after-secondary-failure",
                self_reported_result_event(
                    ticket, raw, f"secondary-failure-{ticket['step_id']}"
                ),
            )
        background_ticket = runner.next_action(
            "background-after-secondary-failure"
        )
        self.assertEqual("game_background", background_ticket["step_id"])

    def test_background_semantic_repair_resumes_the_same_game(self):
        runner = self._new_runner("background-repair")
        while True:
            ticket = runner.next_action("background-repair")
            if ticket["step_id"] == "game_background":
                break
            raw = raw_result_for_ticket(
                runner,
                "background-repair",
                ticket,
                candidate=ticket["step_id"] == "game_id",
            )
            runner.record(
                "background-repair",
                self_reported_result_event(
                    ticket, raw, f"repair-primary-{ticket['step_id']}"
                ),
            )
        game_id = ticket["parameters"]["game_id"]
        runner.record(
            "background-repair",
            self_reported_error_event(
                ticket,
                query_id="background-semantic-error",
                error_class="semantic_analysis",
            ),
        )
        repair = runner.next_action("background-repair")
        self.assertEqual("repair_query", repair["action"])
        repaired_sql = repair["original_sql"].replace(
            "event_candidates", "event_candidates_repaired"
        )
        runner.record(
            "background-repair",
            {
                "event": "repair_submitted",
                "step_id": "game_background",
                "repair_attempt": repair["repair_attempt"],
                "repair_reason": "修正结果 CTE 别名作用域",
                "error_evidence": "ODPS-0130071 semantic analysis",
                "repaired_sql": repaired_sql,
            },
        )
        resumed = runner.next_action("background-repair")
        self.assertEqual(game_id, resumed["parameters"]["game_id"])
        runner.record(
            "background-repair",
            self_reported_result_event(
                resumed,
                raw_result_for_ticket(
                    runner, "background-repair", resumed
                ),
                "background-repair-success",
            ),
        )
        item = runner.load_state("background-repair")["post_primary"][
            "steps"
        ][2]["items"][0]
        self.assertEqual("succeeded", item["status"])
        self.assertEqual(2, len(item["attempts"]))


class GameBackgroundSelectionTest(unittest.TestCase):
    def setUp(self):
        self.selector = GameBackgroundSelector(RepositoryContracts(ROOT))

    @staticmethod
    def _state(
        adverse_impacts: list[float],
        *,
        metric: str = "下载完成率",
        delta: float = -0.10,
    ) -> dict:
        return {
            "metric": metric,
            "canonical_root_metric": {
                "current_value": 0.70,
                "baseline_value": 0.80,
                "delta": delta,
            },
            "steps": [
                {
                    "id": "game_id",
                    "status": "succeeded",
                    "candidates": [
                        {
                            "value": str(10000 + index),
                            "label": f"Game {index}",
                            "adverse_impact_bp": impact,
                        }
                        for index, impact in enumerate(adverse_impacts, start=1)
                    ],
                }
            ],
        }

    @staticmethod
    def _post_primary(dominant: bool = False) -> dict:
        counterfactual = {
            "id": "counterfactual",
            "status": "skipped_by_policy",
            "reason": "counterfactual_trigger_not_met",
        }
        if dominant:
            counterfactual = {
                "id": "counterfactual",
                "status": "succeeded",
                "result": {
                    "candidate_id": "game_id:10001",
                    "value": "10001",
                    "label": "Game 1",
                    "dominant": True,
                },
            }
        return {"steps": [counterfactual]}

    def test_dominant_counterfactual_selects_only_one_game(self):
        selected = self.selector.select(
            self._state([600, 500, 400]), self._post_primary(dominant=True)
        )
        self.assertEqual(1, len(selected["selected_games"]))
        self.assertEqual(
            "dominant_counterfactual",
            selected["selected_games"][0]["selection_reason"],
        )

    def test_three_games_are_the_minimum_prefix_to_reach_half(self):
        selected = self.selector.select(
            self._state([200, 180, 150]), self._post_primary()
        )
        self.assertEqual(3, len(selected["selected_games"]))

    def test_top_three_below_half_skips_all_background_queries(self):
        selected = self.selector.select(
            self._state([150, 140, 130, 120]), self._post_primary()
        )
        self.assertEqual("skipped_by_policy", selected["status"])
        self.assertEqual(
            "game_candidates_do_not_reach_background_threshold",
            selected["reason"],
        )

    def test_metric_direction_is_normalized_before_game_selection(self):
        higher = self.selector.select(
            self._state([500], metric="下载完成率", delta=-0.10),
            self._post_primary(),
        )
        lower = self.selector.select(
            self._state([500], metric="下载失败率", delta=0.10),
            self._post_primary(),
        )
        self.assertEqual("planned", higher["status"])
        self.assertEqual("planned", lower["status"])
        self.assertEqual(
            higher["selected_games"], lower["selected_games"]
        )

    def test_root_decimal_delta_is_compared_with_candidate_bp(self):
        selected = self.selector.select(
            self._state([4, 1], delta=-0.001), self._post_primary()
        )
        self.assertEqual(2, len(selected["selected_games"]))

    def test_only_ascii_positive_integer_game_ids_are_selectable(self):
        state = self._state([500])
        state["steps"][0]["candidates"] = [
            {"value": "abc", "label": "Text", "adverse_impact_bp": 900},
            {"value": "00123", "label": "Leading zero", "adverse_impact_bp": 800},
            {"value": "１２３", "label": "Unicode digits", "adverse_impact_bp": 700},
            {"value": "12345", "label": "Valid", "adverse_impact_bp": 500},
        ]
        selected = self.selector.select(state, self._post_primary())
        self.assertEqual(
            ["12345"],
            [item["game_id"] for item in selected["selected_games"]],
        )


class GameBackgroundValidatorTest(unittest.TestCase):
    def setUp(self):
        self.contracts = RepositoryContracts(ROOT)
        self.binding = self.contracts.game_background_binding(game_id=12345)
        self.validator = GameBackgroundValidator(self.contracts)
        columns, _ = self.contracts.query_spec_result_contract(self.binding)
        self.columns = list(columns)
        self.row = {name: 0 for name in self.columns}
        self.row.update(
            {
                "analysis_date": "2026-08-22",
                "app_id": 12345,
                "app_title": "Game 12345",
                "event_type": 1,
                "event_priority": 1,
                "event_kind": "download_open",
                "event_title": "Android download opened",
                "event_detail": "private detail",
                "event_date0": "2026-08-22",
                "event_date1": "2026-08-22",
                "days_before_analysis": 0,
                "temporal_relation": "same_day",
                "transition_evidence": "observed_state_transition",
                "game_status": "ONLINE",
                "game_package_app_version": "1.0.0",
                "apk_app_version_name": "1.0.0",
                "source": "game_detail_lifecycle",
                "source_snapshot_dt": "2026-08-22",
                "impact_score1": 0,
            }
        )

    def _validate(self, rows: list[dict]):
        return self.validator.validate(
            raw_result={"columns": self.columns, "rows": rows},
            binding=self.binding,
            analysis_date="2026-08-22",
            game_id=12345,
        )

    def test_wrong_app_and_future_event_are_rejected(self):
        cases = []
        wrong_app = copy.deepcopy(self.row)
        wrong_app["app_id"] = 99999
        cases.append((wrong_app, "identity_mismatch"))
        future = copy.deepcopy(self.row)
        future.update(
            {
                "event_date0": "2026-08-23",
                "event_date1": "2026-08-23",
                "days_before_analysis": -1,
            }
        )
        cases.append((future, "temporal_evidence_invalid"))
        for row, code in cases:
            with self.subTest(code=code), self.assertRaisesRegex(
                GameBackgroundValidationError, ".+"
            ) as raised:
                self._validate([row])
            self.assertEqual(code, raised.exception.code)

    def test_duplicate_event_revisions_are_folded_before_fact_capping(self):
        incident = copy.deepcopy(self.row)
        incident.update(
            {
                "event_type": 5,
                "event_priority": 3,
                "event_kind": "incident",
                "event_title": "Service incident",
                "transition_evidence": "operation_event",
                "source": "operation_events",
            }
        )
        revision = copy.deepcopy(incident)
        revision["source"] = "operation_events_revision"
        outcome = self._validate([incident, revision])
        self.assertEqual(1, len(outcome.facts))

    def test_observed_and_registered_lifecycle_are_distinguished(self):
        observed = self._validate([self.row])
        self.assertEqual(
            "observed_state_transition",
            observed.facts[0]["transition_evidence"],
        )
        registered_row = copy.deepcopy(self.row)
        registered_row["transition_evidence"] = (
            "registered_lifecycle_date_only"
        )
        registered = self._validate([registered_row])
        self.assertEqual(
            "registered_lifecycle_date_only",
            registered.facts[0]["transition_evidence"],
        )
        self.assertEqual(
            ("state_transition_not_directly_observed",),
            registered.limit_codes,
        )

        preferred = self._validate([registered_row, self.row])
        self.assertEqual(1, len(preferred.facts))
        self.assertEqual(
            "observed_state_transition",
            preferred.facts[0]["transition_evidence"],
        )
        self.assertEqual((), preferred.limit_codes)

    def test_playable_transition_derived_from_download_is_not_duplicated(self):
        playable = copy.deepcopy(self.row)
        playable.update(
            {
                "event_type": 2,
                "event_priority": 2,
                "event_kind": "playable_open",
                "event_title": "Android playable opened",
                "is_android_download_enable": 1,
            }
        )
        outcome = self._validate([self.row, playable])
        self.assertEqual(
            ["download_open"],
            [fact["event_kind"] for fact in outcome.facts],
        )

    def test_writer_facts_are_capped_at_four(self):
        rows = []
        for offset in range(5):
            row = copy.deepcopy(self.row)
            event_date = f"2026-08-{22 - offset:02d}"
            row.update(
                {
                    "event_type": 5,
                    "event_priority": 3,
                    "event_kind": "incident",
                    "event_title": f"Incident {offset}",
                    "event_date0": event_date,
                    "event_date1": event_date,
                    "days_before_analysis": offset,
                    "temporal_relation": (
                        "same_day"
                        if offset == 0
                        else "one_day_before"
                        if offset == 1
                        else "within_baseline"
                    ),
                    "transition_evidence": "operation_event",
                    "source": "operation_events",
                }
            )
            rows.append(row)
        outcome = self._validate(rows)
        self.assertEqual(4, len(outcome.facts))


if __name__ == "__main__":
    unittest.main()
