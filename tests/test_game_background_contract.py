import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLAYBOOK_PATH = ROOT / "references" / "download-install-playbook.md"
BACKGROUND_QUERY_PATH = (
    ROOT / "references" / "queries" / "game-operation-events.yaml"
)
DOWNLOAD_GAME_QUERY_PATHS = (
    ROOT / "references" / "queries" / "download-game-attribution.yaml",
    ROOT
    / "references"
    / "queries"
    / "download-failed-rate-game-attribution.yaml",
    ROOT
    / "references"
    / "queries"
    / "download-failed-pv-rate-game-attribution.yaml",
    ROOT
    / "references"
    / "queries"
    / "download-stop-rate-game-attribution.yaml",
)
ACTIVE_SAMPLE_PREDICATES = {
    "download-game-attribution.yaml": (
        "download_sample_flag = 1",
        "is_download_complete IN (0, 1)",
    ),
    "download-failed-rate-game-attribution.yaml": (
        "download_sample_flag = 1",
        "is_explicit_failed IN (0, 1)",
    ),
    "download-failed-pv-rate-game-attribution.yaml": (
        "game_download_cnt_1d > 0",
        "game_download_failed_cnt_1d >= 0",
    ),
    "download-stop-rate-game-attribution.yaml": (
        "download_sample_flag = 1",
        "is_human_stop IN (0, 1)",
    ),
}


class GameBackgroundContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.query_spec = BACKGROUND_QUERY_PATH.read_text(encoding="utf-8")
        cls.sql = cls.query_spec.split("sql: |", 1)[1].split(
            "\noutput:", 1
        )[0]
        cls.playbook = PLAYBOOK_PATH.read_text(encoding="utf-8")

    def test_recent_lifecycle_query_uses_guarded_comparison_window(self):
        self.assertTrue(self.query_spec.startswith("version: 2\n"))
        self.assertIn("game_history AS", self.sql)
        self.assertIn("FROM tap_dw.dwt_game_detail_info_view_df", self.sql)
        self.assertRegex(
            self.sql,
            re.compile(
                r"WHERE dt BETWEEN TO_CHAR\(\s*"
                r"DATEADD\(TO_DATE\(\$\{business_date\}\), -8, 'dd'\)"
            ),
        )
        self.assertIn("守卫分区只用于给窗口首日提供状态前态", self.playbook)
        self.assertIn("LAG(dt) OVER", self.sql)
        self.assertIn("AS previous_snapshot_dt", self.sql)
        self.assertIn("AS has_contiguous_previous", self.sql)
        self.assertNotIn(
            "MAX(dt) FROM tap_dw.dwt_game_detail_info_view_df", self.sql
        )

    def test_registered_download_open_preserves_registered_event_day(self):
        self.assertIn("LAG(is_android_download_enable)", self.sql)
        self.assertIn(
            "android_download_start_date AS lifecycle_event_date", self.sql
        )
        self.assertIn("WHERE dt = android_download_start_date", self.sql)
        self.assertIn("AND is_android_download_enable = 1", self.sql)
        self.assertIn("'registered_lifecycle_date_only'", self.sql)

    def test_observed_state_transitions_use_actual_partition_day(self):
        transitions = (
            ("previous_download_enable = 0", "is_android_download_enable = 1"),
            ("previous_reserve_enable = 0", "is_android_reserve_enable = 1"),
            (
                "previous_playable_enable = 0",
                "is_android_download_enable = 1 OR is_android_triali = 1",
            ),
        )
        for previous_state, current_state in transitions:
            with self.subTest(previous_state=previous_state):
                self.assertIn(previous_state, self.sql)
                self.assertIn(current_state, self.sql)
        self.assertEqual(
            3, self.sql.count("WHERE has_contiguous_previous = 1")
        )
        self.assertIn("dt AS lifecycle_event_date", self.sql)
        self.assertIn("lifecycle_event_date AS event_date0", self.sql)
        self.assertIn("'observed_state_transition'", self.sql)
        self.assertIn("老游戏在窗口内重新开放下载", self.playbook)

    def test_playable_state_preserves_unknown_and_deduplicates_download(self):
        self.assertIn(
            "WHEN is_android_download_enable = 0 "
            "AND is_android_triali = 0 THEN 0",
            self.sql,
        )
        self.assertIn("ELSE NULL\n      END AS playable_enable", self.sql)
        self.assertIn("LAG(playable_enable) OVER", self.sql)
        self.assertIn(
            "COALESCE(is_android_download_enable, 0) <> 1", self.sql
        )
        self.assertIn(
            "android_playable_start_date = android_download_start_date",
            self.sql,
        )
        self.assertIn(
            "不得再把 `playable_open` 当作第二条独立证据",
            self.playbook,
        )

    def test_future_registered_lifecycle_dates_cannot_be_emitted(self):
        for lifecycle_date in (
            "android_download_start_date",
            "android_reserve_start_date",
            "android_playable_start_date",
        ):
            expected = (
                f"AND {lifecycle_date} BETWEEN TO_CHAR(\n"
                "        DATEADD(TO_DATE(${business_date}), -7, 'dd'), "
                "'yyyy-mm-dd'\n"
                "      ) AND ${business_date}"
            )
            self.assertIn(expected, self.sql)

    def test_reserve_auto_download_is_context_not_a_synthetic_event(self):
        self.assertIn("reserve_auto_download_enabled", self.sql)
        self.assertIn("add_map['reserve_auto_download']", self.sql)
        self.assertNotIn("'reserve_auto_download' AS event_kind", self.sql)
        self.assertIn(
            "reserve_auto_download_enabled: integer", self.query_spec
        )

    def test_operation_events_overlap_window_and_expose_snapshot(self):
        self.assertIn("event_date0 <= ${business_date}", self.sql)
        self.assertRegex(
            self.sql,
            re.compile(
                r"COALESCE\(NULLIF\(event_date1, ''\), event_date0\)"
                r" >= TO_CHAR\(\s*DATEADD\(TO_DATE\(\$\{business_date\}\),"
                r" -7, 'dd'\)"
            ),
        )
        self.assertIn("dt AS source_snapshot_dt", self.sql)
        self.assertIn("source_snapshot_dt: date", self.query_spec)

    def test_game_background_selection_and_overlap_are_required(self):
        self.assertIn(
            "若头部游戏通过剔除反事实的主导条件",
            self.playbook,
        )
        self.assertIn("名单只包含该主导游戏", self.playbook)
        self.assertIn(
            "选择累计不利影响首次达到 `abs(root_adverse_delta) * 0.50` 的最小前缀",
            self.playbook,
        )
        self.assertIn("最多 3 款", self.playbook)
        self.assertIn(
            "不得为了凑满 3 款继续查询较弱候选", self.playbook
        )
        self.assertIn(
            "对名单中的每个 `game_id` 分别绑定、分别尝试执行一次",
            self.playbook,
        )
        self.assertIn("不得把多个游戏改写成临时 `IN` 查询", self.playbook)
        self.assertIn(
            "该组合满足上述条件时，本模块是规定校准步骤",
            self.playbook,
        )

    def test_background_query_failure_does_not_override_attribution(self):
        for failure in (
            "目标背景分区缺失",
            "权限阻塞",
            "修正两次后仍失败",
            "结果量超过 `max_rows`",
        ):
            self.assertIn(failure, self.playbook)
        self.assertIn(
            "不得把已经合法形成的调查状态改写为这些受阻状态",
            self.playbook,
        )
        self.assertIn("不得阻止名单中其他游戏继续查询", self.playbook)
        self.assertIn(
            "不得删除已有 `top_findings`、反事实或重叠验证结果",
            self.playbook,
        )
        self.assertIn(
            "只有根指标或规定归因路径自身受阻时",
            self.playbook,
        )
        self.assertIn(
            "四象限桶只校准原候选的 `finding`",
            self.playbook,
        )

    def test_positive_background_facts_are_not_evidence_limits(self):
        self.assertIn(
            "不得把正向背景事实本身写进 `evidence_limits`",
            self.playbook,
        )
        self.assertIn(
            "不得把一款游戏的背景套用到其他候选", self.playbook
        )


class DownloadGameActiveBaselineContractTest(unittest.TestCase):
    def test_each_download_game_query_preserves_active_baseline_days(self):
        for path in DOWNLOAD_GAME_QUERY_PATHS:
            with self.subTest(path=path.name):
                query_spec = path.read_text(encoding="utf-8")
                sql = query_spec.split("sql: |", 1)[1].split(
                    "\noutput:", 1
                )[0]

                self.assertTrue(query_spec.startswith("version: 2\n"))
                self.assertIn(
                    "MAX(bucket_baseline_active_day_count) "
                    "AS bucket_baseline_active_day_count",
                    sql,
                )
                for predicate in ACTIVE_SAMPLE_PREDICATES[path.name]:
                    self.assertIn(predicate, sql)
                self.assertIn("bucket_baseline_active_day_count,", sql)
                self.assertIn(
                    "bucket_baseline_active_day_count: integer", query_spec
                )
                self.assertIn(
                    "bucket_baseline_active_day_count: "
                    "{min: 0, max: 7, allow_null: false}",
                    query_spec,
                )
                self.assertIn(
                    "baseline_denominator * 1.0\n"
                    "            / NULLIF(baseline_day_count, 0) >= 100",
                    sql,
                )
                self.assertNotIn(
                    "/ NULLIF(bucket_baseline_active_day_count, 0)", sql
                )

    def test_playbook_limits_active_day_interpretation_to_game_buckets(self):
        playbook = PLAYBOOK_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "且只可解释 `bucket_kind=game` 的未合并游戏桶", playbook
        )
        self.assertIn(
            "根基线完整性、样本门槛、池化率和贡献计算继续使用固定 7 日比较窗口",
            playbook,
        )
        self.assertIn(
            "对每个写入 `top_findings` 的游戏候选", playbook
        )
        self.assertIn(
            "包括多游戏背景名单中的第二、第三款", playbook
        )


if __name__ == "__main__":
    unittest.main()
