import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = ROOT / "SKILL.md"
PLAYBOOK_PATH = ROOT / "references" / "download-install-playbook.md"
QUERY_ROOT = ROOT / "references" / "queries"
DOWNLOAD_PRIMARY_TEMPLATES = (
    QUERY_ROOT / "download-primary-attribution-template.md",
    QUERY_ROOT / "download-failed-rate-primary-attribution-template.md",
    QUERY_ROOT / "download-failed-pv-rate-primary-attribution-template.md",
    QUERY_ROOT / "download-stop-rate-primary-attribution-template.md",
)
INSTALL_PRIMARY_TEMPLATE = QUERY_ROOT / "install-primary-attribution-template.md"
INSTALL_GAME_QUERY = QUERY_ROOT / "install-game-attribution.yaml"
INSTALL_STAGE_QUERY = QUERY_ROOT / "install-stage-loss-decomposition.yaml"
INSTALL_POST_START_VERSION_TEMPLATE = (
    QUERY_ROOT / "install-post-start-version-template.md"
)
SECONDARY_ATTRIBUTION_TEMPLATE = QUERY_ROOT / "secondary-attribution-template.md"
DIMENSION_REGISTRY = QUERY_ROOT / "primary-attribution-dimensions.md"
SCENARIOS_PATH = ROOT / "references" / "attribution-evaluation-scenarios.md"


class AttributionFallbackContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.skill = SKILL_PATH.read_text(encoding="utf-8")
        cls.playbook = PLAYBOOK_PATH.read_text(encoding="utf-8")

    def test_family_local_failure_does_not_end_the_investigation(self):
        self.assertIn("单个维度家族失败是家族级限制", self.skill)
        self.assertIn("不得直接结束整个调查", self.skill)
        self.assertIn("单个家族失败不算“规定下钻未完成”", self.skill)
        self.assertIn("不得提前返回受阻状态", self.skill)
        self.assertIn("继续下一个已登记维度家族", self.playbook)
        self.assertNotIn(
            "该家族若是本轮规定归因必须完成的路径，按无合法下钻数据源处理",
            self.playbook,
        )

    def test_download_full_queue_order_is_mandatory_after_fast_families(self):
        expected_order = (
            "`device_brand -> channel_group -> app_major_version -> "
            "os_major_version -> apk_size_tier`"
        )
        self.assertIn(expected_order, self.playbook)
        self.assertIn("无论是否已经形成游戏候选", self.playbook)
        self.assertIn("逐个尝试完上述五个家族", self.playbook)

    def test_registered_attribution_emits_machine_checkable_full_queue(self):
        self.assertIn("`attribution_execution`", self.skill)
        self.assertIn("不能等到撰写结论时根据已有候选反推执行记录", self.skill)
        self.assertIn("## 归因执行清单", self.playbook)
        self.assertIn("writer 将拒绝缺少步骤、顺序不符", self.playbook)
        self.assertIn(
            "game_id -> is_reserve_auto_download -> device_brand -> channel_group\n"
            "-> app_major_version -> os_major_version -> apk_size_tier",
            self.playbook,
        )
        self.assertIn(
            "game_id -> install_stage -> device_brand -> storage_headroom_tier\n"
            "-> os_major_version -> apk_size_tier",
            self.playbook,
        )
        self.assertIn("已经找到游戏候选而截断固定队列", self.skill)
        self.assertIn("`secondary_steps`", self.skill)
        self.assertIn("`attribution_level=primary|secondary`", self.skill)
        self.assertIn("一级与二级 `candidate_count` 合计为正", self.playbook)
        self.assertIn("所有一级、二级候选数均为零", self.playbook)
        self.assertIn("finding 无对应执行证据", self.playbook)

    def test_download_keeps_game_and_reserve_as_first_priority(self):
        fast_families = self.playbook.index("先并行快判两个独立维度家族")
        game_id = self.playbook.index("game_id", fast_families)
        reserve = self.playbook.index("is_reserve_auto_download", game_id)
        fallback = self.playbook.index("device_brand", reserve)
        self.assertLess(game_id, fallback)
        self.assertLess(reserve, fallback)
        self.assertIn("两个规定家族的合法结果必须分别保留", self.playbook)

    def test_install_stage_quality_does_not_gate_official_attribution(self):
        self.assertIn("链路键与阶段质量不得作为官方投影的前置门禁", self.playbook)
        self.assertIn("逐个尝试完上述四个家族", self.playbook)
        self.assertIn("install-primary-attribution-template.md", self.playbook)

    def test_install_fallback_order_prioritizes_brand_and_storage(self):
        expected_order = (
            "`device_brand -> storage_headroom_tier -> "
            "os_major_version -> apk_size_tier`"
        )
        self.assertIn(expected_order, self.playbook)

    def test_secondary_relationships_do_not_override_primary_order(self):
        self.assertEqual(
            2,
            self.playbook.count("下列列表只登记允许的父子关系，不是另一套执行顺序"),
        )
        self.assertIn(
            "game_id -> device_brand, channel_group, app_major_version,\n"
            "           os_major_version, apk_size_tier",
            self.playbook,
        )
        self.assertIn(
            "game_id -> device_brand, storage_headroom_tier, os_major_version,\n"
            "           apk_size_tier",
            self.playbook,
        )

    def test_install_keeps_game_first_and_stage_second(self):
        game_step = self.playbook.index("第一优先级固定完整读取")
        stage_step = self.playbook.index("游戏家族完成后第二步")
        fallback_step = self.playbook.index("阶段拆解之后，无论 `game_id` 是否已经形成候选")
        self.assertLess(game_step, stage_step)
        self.assertLess(stage_step, fallback_step)
        self.assertIn("无论 `game_id` 是否形成候选", self.playbook)
        self.assertIn("阶段拆解不得移动到游戏归因之前", self.playbook)

        game_query = INSTALL_GAME_QUERY.read_text(encoding="utf-8")
        self.assertIn("official_download_complete", game_query)
        self.assertIn("official_install_complete", game_query)
        self.assertNotIn("has_client_install_start", game_query)
        self.assertNotIn("diagnostic_event_matched", game_query)

    def test_download_templates_register_every_fallback_dimension(self):
        registry = DIMENSION_REGISTRY.read_text(encoding="utf-8")
        for dimension in (
            "apk_size_tier",
            "channel_group",
            "app_major_version",
            "os_major_version",
            "device_brand",
        ):
            self.assertIn(dimension, registry)

        for path in DOWNLOAD_PRIMARY_TEMPLATES:
            content = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertIn("primary-attribution-dimensions.md", content)
                self.assertIn(
                    "__DIMENSION_SOURCE_FIELD__ AS dimension_source", content
                )
                self.assertIn(
                    "__DIMENSION_QUALITY_SOURCE_EXPR__ AS "
                    "dimension_quality_matched",
                    content,
                )
                for field in (
                    "overall_current_dimension_matched_denominator",
                    "overall_baseline_dimension_matched_denominator",
                    "overall_current_dimension_unmatched_denominator",
                    "overall_baseline_dimension_unmatched_denominator",
                    "overall_current_dimension_match_rate",
                    "overall_baseline_dimension_match_rate",
                ):
                    self.assertIn(field, content)
                self.assertNotIn("official_observation_days", content)
                self.assertNotIn("grain_row_count", content)
                self.assertIn("每次只选择当前家族的一个源字段", content)

        expected_registry_order = (
            "is_reserve_auto_download\n"
            "device_brand\n"
            "channel_group\n"
            "app_major_version\n"
            "os_major_version\n"
            "apk_size_tier"
        )
        self.assertIn(expected_registry_order, registry)
        for mapping in (
            "| `device_brand` | `device_dimension_matched` |",
            "| `os_major_version` | `active_os_matched` |",
            "| `channel_group` | `1` |",
            "| `app_major_version` | `1` |",
            "| `apk_size_tier` | `1` |",
        ):
            self.assertIn(mapping, registry)

    def test_install_template_registers_every_fallback_dimension(self):
        content = INSTALL_PRIMARY_TEMPLATE.read_text(encoding="utf-8")
        registry = DIMENSION_REGISTRY.read_text(encoding="utf-8")
        for dimension in (
            "apk_size_tier",
            "os_major_version",
            "device_brand",
            "storage_headroom_tier",
        ):
            with self.subTest(dimension=dimension):
                self.assertIn(dimension, registry)
        self.assertIn("primary-attribution-dimensions.md", content)
        self.assertIn("__DIMENSION_SOURCE_FIELD__ AS dimension_source", content)
        self.assertIn(
            "__DIMENSION_QUALITY_SOURCE_EXPR__ AS dimension_quality_matched",
            content,
        )
        self.assertIn("official_download_complete", content)
        self.assertIn("official_install_complete", content)
        self.assertIn("official_observation_days", content)
        self.assertIn("is_metric_anchor = 1", content)
        self.assertIn("禁止选择 `install_event_app_major_version`", content)
        self.assertIn("无论 `game_id` 是否形成合法结果", content)
        self.assertIn("都不得省略本模板或缩短队列", content)
        self.assertNotIn("`game_id` 未形成合法结果", content)
        for field in (
            "overall_current_dimension_match_rate",
            "overall_baseline_dimension_match_rate",
            "overall_current_observation_days_min",
            "overall_current_observation_days_max",
            "overall_baseline_observation_days_min",
            "overall_baseline_observation_days_max",
        ):
            self.assertIn(field, content)
        self.assertNotIn("grain_row_count", content)

        expected_registry_order = (
            "device_brand           -> device_brand\n"
            "storage_headroom_tier  -> storage_headroom_tier\n"
            "os_major_version       -> os_major_version\n"
            "apk_size_tier          -> apk_size_tier"
        )
        self.assertIn(expected_registry_order, registry)

    def test_evaluation_scenarios_require_the_same_unconditional_queues(self):
        scenarios = SCENARIOS_PATH.read_text(encoding="utf-8")

        self.assertIn("仍完成固定五维队列", scenarios)
        self.assertIn("无论是否已形成合法候选", scenarios)
        self.assertNotIn("不强制横扫", scenarios)
        self.assertNotIn("游戏不合法、无候选、解释不足", scenarios)
        self.assertIn("当前执行清单验收字段", scenarios)

    def test_primary_risk_evidence_never_stops_the_dimension_queue(self):
        self.assertIn("不构成拒绝当前结果或停止后续维度的硬门禁", self.playbook)
        self.assertIn("不得仅凭这些辅助字段返回 `unsupported_drilldown`", self.playbook)
        self.assertIn("风险只限制措辞强度", self.playbook)

    def test_install_stage_query_has_fixed_d_s_c_decomposition(self):
        content = INSTALL_STAGE_QUERY.read_text(encoding="utf-8")
        for required in (
            "official_download_complete",
            "has_client_install_start",
            "official_install_complete",
            "current_no_observed_start_count",
            "current_pre_start_unfinished_count",
            "current_pre_start_unfinished_rate",
            "current_started_not_complete_count",
            "current_started_complete_count",
            "current_post_start_completion_rate",
            "current_official_loss_closure_gap",
            "current_complete_without_start_count",
            "current_start_without_download_count",
            "current_anchor_duplicate_excess",
            "baseline_day_count",
            "official_observation_days",
        ):
            with self.subTest(required=required):
                self.assertIn(required, content)
        self.assertIn("is_metric_anchor = 1", content)
        self.assertIn("install-stage-loss-decomposition.yaml", self.playbook)
        self.assertIn("未观测到 installStart 且最终未完成", self.playbook)
        self.assertIn("`S=0` 不是质量失败", self.playbook)
        self.assertIn("开始后安装完成率保持未定义且不得填零", self.playbook)
        self.assertIn("C_started = D ∩ S ∩ C", self.playbook)
        self.assertIn("两个未完成集合必须闭合到官方损耗 `D - C`", self.playbook)
        self.assertIn(
            "current_started_complete_count * 1.0 / NULLIF(current_start_count, 0)",
            content,
        )
        self.assertNotIn(
            "current_complete_count * 1.0 / NULLIF(current_start_count, 0)",
            content,
        )

    def test_sandbox_skips_apk_install_start_stage(self):
        content = INSTALL_STAGE_QUERY.read_text(encoding="utf-8")
        self.assertIn("allowed_values: [app]", content)
        self.assertIn("该阶段只适用于 `game_type=app`", self.playbook)
        self.assertIn("`skipped_not_applicable`", self.playbook)
        self.assertIn("不得执行本 QuerySpec", self.playbook)

    def test_historical_apk_stage_sets_close_to_official_loss(self):
        download_count = 1_476_380
        start_count = 1_339_162
        complete_count = 1_033_718
        complete_without_start_count = 6_383
        started_complete_count = complete_count - complete_without_start_count
        started_not_complete_count = start_count - started_complete_count
        pre_start_unfinished_count = (
            download_count - complete_count - started_not_complete_count
        )

        self.assertEqual(311_827, started_not_complete_count)
        self.assertEqual(130_835, pre_start_unfinished_count)
        self.assertEqual(
            download_count - complete_count,
            pre_start_unfinished_count + started_not_complete_count,
        )
        self.assertEqual(
            download_count - start_count,
            pre_start_unfinished_count + complete_without_start_count,
        )

    def test_install_secondary_observation_window_uses_official_denominator(self):
        content = SECONDARY_ATTRIBUTION_TEMPLATE.read_text(encoding="utf-8")
        install_content = content.split("## 安装骨架", 1)[1]
        for date_predicate in (
            "dt = ${business_date}",
            "dt < ${business_date}",
        ):
            for aggregate in ("MIN", "MAX"):
                expected = (
                    f"{aggregate}(CASE WHEN {date_predicate}\n"
                    "          AND metric_denominator = 1\n"
                    "      THEN official_observation_days ELSE NULL END)"
                )
                with self.subTest(aggregate=aggregate, date_predicate=date_predicate):
                    self.assertIn(expected, install_content)
        self.assertIn(
            "只在 `metric_denominator = official_download_complete = 1`",
            install_content,
        )

    def test_install_event_version_is_post_start_only(self):
        registry = DIMENSION_REGISTRY.read_text(encoding="utf-8")
        install_registry = registry.split("## 安装白名单", 1)[1].split(
            "## 二级下钻替换", 1
        )[0]
        install_whitelist = install_registry.split("```text", 1)[1].split("```", 1)[0]
        self.assertNotIn("install_event_app_major_version", install_whitelist)

        content = INSTALL_POST_START_VERSION_TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("official_download_complete = 1", content)
        self.assertIn("has_client_install_start = 1", content)
        self.assertIn("diagnostic_event_matched = 1", content)
        self.assertIn("install_event_app_major_version", content)
        self.assertIn("不得产生 `top_findings`", content)


if __name__ == "__main__":
    unittest.main()
