---
name: xuanji-mini
description: 处理已注册的 TapTap Android 下载/安装 DQC 告警；通过三个任务级 Host 工具完成确定性归因与有界后置校准。
---

# 璇玑 Mini

每次只处理一份完整 DQC payload。路由、指标定义、根预检、新增性门禁、固定队列、后置校准、校验和任务组装全部由 Host 执行。

## 分析档案

部署方通过 Host 私有的 `XUANJI_ANALYSIS_PROFILE` 选择档案，不得成为模型参数。`primary_v1` 保持固定一级队列；`primary_v2` 随后执行零查询剔除反事实、一次 `game_id` 单父二级归因、最多三款游戏背景查询和一次下载错误码校准。错误码只允许 `download / app / 下载失败率` 在已有一级候选、根不利变化至少 5bp 且当前受影响实体至少 100 时触发；其他指标、安装和沙盒按策略跳过，且不推导最终失败或恢复。

## 工具协议

模型只使用三个任务级工具：

- `xuanji_run_task(task_id, dqc_payload)`
- `xuanji_submit_repair(task_id, investigation_id, run_id, repair fields...)`
- `xuanji_finalize(task_id, investigation_id, writer_patch)`

不得调用调查级 Host 工具，不得在模型可见终端中查询 DView。每次只处理 Host 返回的当前 action。

## 启动任务

1. 上游 request 已提供 `task_id` 时必须逐字使用；独立调用没有上游身份时才生成稳定且唯一的 `task_id`。
2. 将未改写的完整 DQC payload 作为 `dqc_payload` 交给 `xuanji_run_task`。
3. 相同 `task_id` 只能与相同 payload 重试或恢复；不得补齐、重排或拆分 `ruleChecks`。

正常路径禁止模型读取 `contracts/dqc-routes.yaml`、路由 Markdown、数据分析知识库 manifest/metric YAML、锁定 SQL 资产、Runtime state 或完整 Playbook。Host 是路由、定义和调查选择的唯一控制者。

## `write_conclusion`

只读当前响应的 `writer_pack` 和 [Runtime 文案指南](references/runtime-writing-guide.md)。不得加载 Playbook 或为其他 investigation 预先生成文案。

调用 `xuanji_finalize` 时，`writer_patch` 必须且只能包含：

- `summary`
- `finding_texts`
- `evidence_limits`
- `recommended_action`

`finding_texts` 只按 writer pack 已暴露的 `candidate_id` 关联。不得提交 `analysis_context`、身份、数值、日期、状态、候选、查询证据或内部 hash。

## `repair_required`

只处理当前有界 repair packet，并把 packet 要求的修正字段原样交给 `xuanji_submit_repair`。不得扩大到其他步骤或 investigation，不得绕过两次修正上限。

## `task_complete`

以 Host 返回的 `analysis_preview`、`pipeline_handoff`、`overall_status` 和紧凑 validation receipt 摘要作为任务结果。不得重算状态、重组 investigations 或用旧 run/batch 补齐证据。完整交接协议见 [Pipeline Handoff](references/pipeline-handoff.md)。

完整已校验 analysis 保留在 Host 内部 task sink。模型可见 preview 单独出现时不是权威产物；只有当前同一 `task_complete` 返回的 preview 与 `pipeline_handoff` 原样配对，并由上层 writer 验签、核对 task/payload 身份后，才是允许提交的公开派生产物。

## 失败与安全

- 查询、权限或数据问题以 Host 返回的类型化状态为准；不把失败、NULL 或空结果写成零。
- Host ToolError 表示运行异常；保留相同 task 供重试，不伪造 `query_failed` 调查。
- 不输出或索取 SQL、raw rows、query ID、完整 receipt、私有 result hash、state 路径、token 或 secret；不得拆改或伪造 `pipeline_handoff` 中的公开校验字段。
- 不把相关性、贡献或算术剔除写成已确认机制根因。
- `primary_v1` 不执行后置模块；当前 `primary_v2` 不执行安装或沙盒错误码、下载恢复、三级归因、多父节点、严格漏斗、四象限、准实验、负对照、查询并行或通用缓存。
