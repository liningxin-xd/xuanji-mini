---
name: xuanji-mini
description: 处理已注册的 TapTap Android 下载/安装 DQC 告警；通过只读 DView Host 和确定性 Runtime 完成一级归因，并输出经校验的紧凑诊断结果。
---

# 璇玑 Mini

每次只处理一份 DQC payload。默认分析档案为 `primary_v1`，正常路径只进行注册路由、定义门禁、根指标预检、新增性门禁、固定一级队列和一次文案生成。

## 1. 输入与身份

- 只读取 payload 实际存在的字段，不猜测缺失值，不把趋势规则的 `checkResult` 当业务指标值。
- 项目取 `dqcEntityQuality.projectName -> payload.projectName`；表、分区和规则字段按 payload 原值保留。
- `ruleChecks` 使用零基 `rule_indexes`，不得重排、遗漏、重叠或按规则名重新关联。
- 顶层输出固定为 `source/project/table/partition/overall_status/investigations`。
- 身份字段无法解析时不得包装为成功结果。

## 2. 注册路由与定义门禁

- 对 `tap_dw.ads_dmg_quality_platform_download_chain_monitor_1d` 完整读取 [DQC 告警路由表](references/dqc-alert-routing.md)。
- 只接受 16 条注册规则的精确规范化匹配；未知规则返回 `insufficient_definition`，不得模糊匹配。
- 通过 `taptap-data-analysis` 的 manifest、域索引和唯一 metric YAML 完成指标定义门禁。
- 完成率、失败率、失败次数比率和人为停止率不可互换；知识库未唯一命中时不查询。
- 同一 metric/chain/game_type 的规则可合并调查，APK 与沙盒不得合并。

## 3. `primary_v1` 前置门禁

- 使用注册根查询和知识库定义复核当前值、基线、方向、日期与范围。
- 下载分析日等于告警分区日；安装分析日为告警分区日减 2 天。
- 根指标或新增性门禁允许停止时，输出对应受阻/既有异常状态，不创建 full-queue run。
- 只有门禁明确进入 `mode=full_queue` 后才初始化 Runner。
- `primary_v1` 正常路径不得读取完整 [下载与安装排查 Playbook](references/download-install-playbook.md)。
- 队列、SQL、公式、质量门槛和候选门槛以 `contracts/` 与锁定 query assets 为唯一运行真源。

完整 Playbook 仅供人类维护和延期能力设计；二级下钻、背景、错误码、严格漏斗、四象限、准实验与负对照均不进入 `primary_v1` 上下文。

## 4. 生产 Host 执行

- 只通过现有只读 DView MCP；禁止自建连接、shell、curl、DDL 或 DML。
- 平台层按 [Primary Host Boundary Integration](references/host-boundary-integration.md)
  注册 `xuanji_run_investigation`、`xuanji_submit_repair` 和 `xuanji_finalize`；
  只把这三个窄接口暴露给模型。
- Host 在模型进程外持有至少 32 字节 receipt secret，并用同一 `TrustedReceiptVerifier` 创建 Runner 与 `HostDViewAdapter`。
- `ProductionDViewExecutor` 接收当前 DView MCP 的真实响应，保留真实 query ID，并转换为有序 `columns + rows`。
- Host 调用 `HostDViewAdapter.execute_until_blocked(run_id)`；普通成功和家族级失败都自动继续固定队列。
- 正常 Host 返回不得包含 `rendered_sql` 或 `raw_result`。
- 只有 `semantic_analysis` 进入 `repair_query` 时暂停；修正仍受两次上限和 attempt-0 语义门禁约束。
- 沙盒 `install_stage` 由 Runner 自动标记 `skipped_not_applicable`。
- 单个家族失败不得截断或重排队列，也不得升级为整个调查失败。

生产 Host 不向模型逐条发送 SQL、rows、query ID、receipt 或内部 hash。完整已校验 analysis
通过 Host 内部 sink 交给上层 writer；模型只接收脱敏后的 final copy 和 validation receipt 摘要。
调用方只能提交真实的 `query_returned` / `query_error`，步骤终态、候选和 warning 全部由 Runtime 生成。

## 5. 开发 CLI

仅隔离测试可使用 self-reported 模式：

```text
python3 -m runtime.runner init
python3 -m runtime.runner next
python3 -m runtime.runner record
python3 -m runtime.runner export
python3 -m runtime.runner writer-pack
python3 -m runtime.runner assemble-final
python3 -m runtime.runner validate-final
```

`self_reported` 不具有生产 SQL 不可篡改保证，不得用于生产调查。不得直接修改 `.runs/*/state.json`、SQL、ticket、event、export 或 receipt。

## 6. 一次文案生成

- 队列完成后调用 `runner.build_writer_pack(run_id)`；pack 必须小于等于 12 KB。
- 每个一级家族最多暴露 3 个候选；pack 不含 SQL、raw rows、HMAC、diff 或完整 state。
- 写作前只读取 [Runtime 文案指南](references/runtime-writing-guide.md)，不加载完整 Playbook。
- LLM 只能返回 `summary/finding_texts/evidence_limits/recommended_action`。
- `finding_texts` 只按 pack 中的 `candidate_id` 关联，不抄写 dimension、value、数值或 query ID。
- 结构化事实冻结后只写文字，不执行独立二次润色，不改变 Runtime 已确定的状态或证据。

Runner 使用可信 DQC context、writer patch 和冻结 state 自动组装最终 JSON；`metric/analysis_date/current_value/baseline_value/delta_bp/top_findings/attribution_execution` 均由机器填写。

## 7. 最终校验

- `completed` 必须至少有一个已暴露候选的 finding；`no_dominant_slice` 不得有 finding。
- FinalValidator 对账 metric、日期、根值、状态、逐步骤 query ID、warnings、候选身份和不利影响。
- 只有 `assemble-final` 的产物通过 `validate-final` 并生成 validation receipt 后才能提交给上层 writer。
- `assemble-final` 固定写入 `.runs/<run-id>/final/assembled-analysis.json`，将该文件交给 `validate-final`。
- 不得把切片贡献、相关性或算术剔除解释为已确认机制根因。
- 查询、权限或数据问题必须保留类型化限制；不得把失败、NULL 或空结果写成零。

## 8. 延期范围

`primary_v1` 不执行二级归因、游戏背景、错误码扩展、严格漏斗增强、四象限、版本准实验、负对照、通用未知指标推理、查询并行或缓存。不要为这些延期能力扩大正常上下文。

## 安全边界

- 不输出、保存或转发 token、secret、receipt secret 或其他凭据。
- 不伪造 query ID、candidate、状态、日期、数值或 DQC 身份。
- 不使用历史 run、旧 batch 或模型记忆补足当前证据。
- 生产结果必须保留 `execution_mode=trusted_host_adapter`；开发回放必须标记 `self_reported_development`。
