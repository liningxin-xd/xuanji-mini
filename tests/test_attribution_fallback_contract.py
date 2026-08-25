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
DIMENSION_REGISTRY = QUERY_ROOT / "primary-attribution-dimensions.md"


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

    def test_download_fallback_order_is_mandatory_after_fast_family_failure(self):
        expected_order = (
            "`apk_size_tier -> channel_group -> app_major_version -> "
            "os_major_version -> device_brand`"
        )
        self.assertIn(expected_order, self.playbook)
        self.assertIn("任一快判家族未形成合法结果", self.playbook)
        self.assertIn("逐个尝试完上述五个家族", self.playbook)

    def test_download_keeps_game_and_reserve_as_first_priority(self):
        fast_families = self.playbook.index("先并行快判两个独立维度家族")
        game_id = self.playbook.index("game_id", fast_families)
        reserve = self.playbook.index("is_reserve_auto_download", game_id)
        fallback = self.playbook.index("apk_size_tier", reserve)
        self.assertLess(game_id, fallback)
        self.assertLess(reserve, fallback)
        self.assertIn("两个规定家族的合法结果必须分别保留", self.playbook)

    def test_install_stage_quality_does_not_gate_official_attribution(self):
        self.assertIn("链路键与阶段质量不得作为官方投影的前置门禁", self.playbook)
        self.assertIn("逐个尝试完上述四个家族", self.playbook)
        self.assertIn("install-primary-attribution-template.md", self.playbook)

    def test_install_keeps_game_first_and_stage_second(self):
        game_step = self.playbook.index("第一优先级固定完整读取")
        stage_step = self.playbook.index("游戏家族完成后第二步")
        fallback_step = self.playbook.index(
            "阶段拆解之后完整读取 [安装低基数一级归因模板]"
        )
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
                self.assertIn("每次只选择当前家族的一个源字段", content)

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
        self.assertIn("official_download_complete", content)
        self.assertIn("official_install_complete", content)
        self.assertIn("is_metric_anchor = 1", content)
        self.assertIn("禁止选择 `install_event_app_major_version`", content)

    def test_install_stage_query_has_fixed_d_s_c_decomposition(self):
        content = INSTALL_STAGE_QUERY.read_text(encoding="utf-8")
        for required in (
            "official_download_complete",
            "has_client_install_start",
            "official_install_complete",
            "current_no_observed_start_count",
            "current_started_not_complete_count",
            "current_post_start_completion_rate",
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
        self.assertIn("未观测到进入 installStart", self.playbook)
        self.assertIn("`S=0` 不是质量失败", self.playbook)
        self.assertIn("`C/S` 保持未定义且不得填零", self.playbook)

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
