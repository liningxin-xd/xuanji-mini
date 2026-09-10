# 部署准备：已知测试豁免

状态：2026-09-10，按用户确认暂不将现有 Skill 上下文预算超限视为本次发布阻断。
本记录随三仓协调 release manifest 引用；尚未创建 release tag，不代表 ECS 运行已验收。

## XUANJI-CONTEXT-001

| 项目 | 证据与范围 |
| --- | --- |
| 测试 | `tests/test_context_budget_contract.py::ContextBudgetContractTest.test_primary_profile_documents_stay_within_the_runtime_budget` |
| 回归 | 320 项，319 通过，1 项失败；首个断言是 4698 bytes > 4096 bytes |
| 同一预算的其他检查 | Skill 71 行，超过 50–70 行范围；首个断言失败使该行数断言未执行，静态测量发现 |
| Skill SHA-256 | `d97ca3e4068c822f352ebb31db20e19180ad1afc1b1108b4720d3273a4ea42d2`；本次只将残留 policy v5.5 改为 v5.6，字节数和行数不变 |
| Runtime 文案指南 | 60 行，在 30–60 行范围内 |
| 适用范围 | 本次三仓协调发布和 `search-calc-new` 测试 destination 部署准备；ECS Agent Host 产品/版本待确定 |
| 当前决策 | 暂时接受文档长度偏离；仅纠正 Skill 的 policy 引用，保留全部约束、测试阈值和运行逻辑，不 skip、不伪报全绿 |
| 复核时点 | Step 6/9 在目标 Host 验证完整加载；Skill/Host 版本改变或生产 Go/No-Go 前重新复核 |

这是文档长度契约失败，不是 pipeline 运行抛错；本机无人值守正常不等于 ECS 已验证完整加载。
目标 Host 必须完整加载 Skill 末尾约束和所需 references。若发生截断、身份门禁丢失或新功能失败，
本豁免不能覆盖，需先修复再继续对应验收。

后续压缩任务：在保留 Host 路由边界、结构化 continuation、原始 task_complete 文本采集和发送边界的
前提下，将按需细节移入 references，恢复字节与行数预算。该任务不包含未来多二级归因实现。
用户暂缓填写负责人和行政到期日期；本记录以明确复核步骤跟踪，不额外阻断 Step 1。
