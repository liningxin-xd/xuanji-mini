import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = ROOT / "SKILL.md"
PLAYBOOK_PATH = ROOT / "references" / "download-install-playbook.md"
WRITING_POLICY_PATH = ROOT / "references" / "diagnosis-writing-policy.md"
RUNTIME_WRITING_GUIDE_PATH = ROOT / "references" / "runtime-writing-guide.md"


class WritingPolicyContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.skill = SKILL_PATH.read_text(encoding="utf-8")
        cls.playbook = PLAYBOOK_PATH.read_text(encoding="utf-8")
        cls.policy = WRITING_POLICY_PATH.read_text(encoding="utf-8")
        cls.runtime_guide = RUNTIME_WRITING_GUIDE_PATH.read_text(encoding="utf-8")

    def test_skill_routes_user_visible_copy_to_the_writing_policy(self):
        self.assertIn(
            "[Runtime 文案指南](references/runtime-writing-guide.md)",
            self.skill,
        )
        self.assertIn("结构化事实冻结后", self.skill)
        self.assertIn("不执行独立二次润色", self.skill)
        self.assertIn("不改变 Runtime 已确定的状态", self.skill)
        self.assertIn("只返回一个 JSON object", self.runtime_guide)

    def test_playbook_keeps_semantic_boundaries_without_copy_templates(self):
        self.assertIn("结论语义边界", self.playbook)
        self.assertIn(
            "[告警诊断文案规范](diagnosis-writing-policy.md)",
            self.playbook,
        )
        self.assertNotIn("合法措辞：", self.playbook)
        self.assertNotIn("建议对应团队继续核查该方向", self.playbook)
        self.assertIn("不能把待核查方向写成已确认机制", self.playbook)

    def test_policy_covers_every_user_visible_output_field(self):
        for field in (
            "summary",
            "top_findings[].finding",
            "counterfactual.finding",
            "evidence_limits[]",
            "recommended_action",
            "reason",
            "action",
        ):
            with self.subTest(field=field):
                self.assertIn(field, self.policy)

    def test_prominent_actions_must_be_standalone_and_name_the_target(self):
        self.assertIn("必须脱离 `summary` 和 `top_findings` 也能读懂", self.policy)
        self.assertIn("直接写出它的 `label` 或 `value`", self.policy)
        for ambiguous_reference in (
            "该游戏",
            "该切片",
            "该指标",
            "该方向",
            "上述对象",
            "对应团队",
        ):
            with self.subTest(ambiguous_reference=ambiguous_reference):
                self.assertIn(ambiguous_reference, self.policy)

        self.assertIn(
            "推荐：优先核查诡秘之主的下载链路、版本与终态分布。",
            self.policy,
        )
        self.assertIn(
            "推荐：继续跟踪下载安装完成率，并复核连续窗口和恢复条件。",
            self.policy,
        )

    def test_copy_self_check_preserves_analysis_evidence(self):
        self.assertIn("只改文字，不回写分析事实", self.policy)
        self.assertIn("没有新增查询、候选、字段或分析事实", self.policy)
        self.assertIn("相关性、反事实或时间共现写成了根因", self.policy)


if __name__ == "__main__":
    unittest.main()
