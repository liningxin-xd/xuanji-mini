---
name: xuanji-mini
description: 接收 DataWorks DQC 的 TapTap Android 下载或安装链路告警，使用现有 taptap-data-analysis 指标知识库和只读 DView MCP 复核指标、下钻维度并做有限归因定位；知识库无法唯一匹配指标时输出 insufficient_definition。用于 DQC 告警分析、下载异常排查、安装异常排查和维度下钻。
---

# 璇玑 Mini

处理单份 DQC JSON 或等价告警文本。每个指标串行执行有限定位，最终只输出一个 JSON object，不添加 Markdown 围栏或解释文字。

## 1. 解析告警

- 只读取输入中实际存在的字段；保留未知字段但不猜含义，忽略 `$velocityCount)` 模板残留。
- 按以下顺序回退：项目 `dqcEntityQuality.projectName -> payload.projectName`；对象表 `rule.tableName -> dqcEntityQuality.entityName`；分区 `rule.actualExpression -> dqcEntityQuality.actualExpression`；任务 ID `rule.taskId -> dqcEntityQuality.taskId`；比较符 `rule.op -> rule.operator`；阈值 `rule.expectValue`，缺失时仅把 `criticalThreshold` 或 `warningThreshold` 作为补充。
- 优先取规则名 `【】` 内文本作为 `metric_hint`；否则只去除明显的时间、趋势和阈值修饰语。
- 同一标准指标按“项目 + 对象表 + 实际分区 + 知识库标准指标”合并，保留全部原规则。不同指标分别分析；一个指标受阻不得阻塞其他指标。
- 不把所有 `checkResult` 当成业务指标值。“连续 N 周下降”等趋势规则中的值可能只是命中标志，真实值必须查询取得。

## 2. 通过指标定义门禁

加载 `taptap-data-analysis` Skill 并按其根目录解析知识库，不依赖固定绝对路径：先读 `knowledge-base/manifest.yaml` 路由业务域，再读目标域 `_index.yaml`，按标准名、alias 或不改变业务含义的文本归一化做精确匹配，最后读取唯一命中的 metric YAML。

只在定义唯一、语义覆盖 APK/沙盒与下载/安装、观察窗口可对齐且技术口径足以执行只读查询时继续。禁止按字段名、阈值或相似指标猜测，禁止把完成率替代失败率，禁止临时发明 alias 或 mapping。未命中、多义或口径不足时，不查询该指标，返回：

```json
{
  "status": "insufficient_definition",
  "metric_hint": "告警规则中提取出的指标名称",
  "alert_rules": [],
  "reason": "当前指标知识库中未找到唯一对应的标准指标定义",
  "action": "请告警或指标维护方补充或修正知识库定义"
}
```

## 3. 复核根指标

使用当前用户权限下的只读 DView MCP。需要表结构时先 `describe_table`，不得凭记忆使用列名。

1. 若告警对象表直接含有且能唯一识别目标指标，优先用它复现当前值和历史值。
2. 否则使用 metric YAML 的标准 SQL、标准结果表及 caveats。只有对账或解释差异确有必要时才同时查询两种来源。
3. 检查目标业务日期、目标和基线分区、分子分母、指标方向、Android 与 APK/沙盒范围、观察窗口及安装 cohort 日期语义。
4. 默认基线为目标日前 7 个完整可比业务日，计算 `SUM(baseline_numerator) / SUM(baseline_denominator)`，不得简单平均逐日率。长趋势规则可扩大一次趋势查询窗口，但贡献比较仍用默认基线。
5. 得到当前和基线分子、分母、率、`delta_bp = (current_rate - baseline_rate) * 10000`，并核对告警方向。无法解释告警值差异时停止业务下钻。

SQL 因明确错误最多修正两次，不得删除关键过滤或更换口径以求成功。分区缺失或合法空结果经分区检查后记 `insufficient_data`；权限不足记 `query_blocked`；两次修正后仍失败记 `query_failed`；有根指标但无合法维度数据源记 `unsupported_drilldown`。失败和空结果不得写成零。

## 4. 执行有限下钻

根指标通过后必须读取 [下载与安装排查 Playbook](references/download-install-playbook.md)，按指标链路执行：

1. 先做游戏一级检查；下载同时快判预约自动下载。需要维度字段时先 `describe_table`。
2. 用完整维度家族做中心化对称分解和闭合检查，只保留达到 playbook 门槛的非质量桶候选。跨维度家族的影响不得相加。
3. 游戏解释不足时才检查规定的非游戏一级维度，仅保留全局不利影响最大的 Top 3 合法候选。
4. 头部游戏达到反事实触发条件时，最多执行一次剔除反事实。它只证明影响范围的解释力，不证明根因。
5. 数据支持时用下载终态分布决定后续优先方向；它不改变指标定义或贡献算法。
6. 只有一级候选有足够解释力、关系已在 playbook 注册且父范围可完整继承时，选择一个父候选做一次二级下钻。继承指标、日期、平台、APK/沙盒、链路阶段、根过滤和父维度值；完成后立即停止，禁止三级或多父展开。
7. 只有游戏明确主导时才可查游戏状态、APK 版本和包体背景；时间接近仅作为待核查线索。

## 5. 停止并输出

使用 playbook 的停止条件和 V0 状态。完成全部合法检查但没有候选达到 5bp 时返回 `no_dominant_slice`，不得选择低于门槛的最大桶讲故事。

顶层输出固定包含 `source: "dataworks_dqc"`、项目、带项目名的对象表、原始分区、`overall_status` 和 `investigations`。`overall_status` 为：所有指标形成合法结果时 `completed`；部分完成、部分受阻时 `partial`；没有任何指标形成合法结果时 `failed`。

每个调查保留 `metric_hint`、标准指标名（若命中）、全部 `alert_rules`、告警日期与实际分析日期、根指标值、合法发现、证据限制、建议动作和可用的 DView `query_id`。不要伪造缺失字段或 query ID。

结论只描述影响范围和证据边界：使用“异常主要集中在”“剔除后异常明显缩小/仍然存在”“建议继续核查”。不得把最大候选、反事实改善、终态路由、发版或时间共现写成已确认根因。

## 安全边界

- 只通过现有 DView MCP 执行只读查询；禁止 DDL、DML 和任何数仓修改。
- 禁止输出、保存或转发凭据；禁止用 shell、curl 或自建连接绕过 MCP。
- 不查询目标日期之后的数据来解释历史异常；安装指标仅按知识库规定的成熟窗口使用数据。
- 不把查询失败、空结果或缺失值当成零或“未发现异常”。
- 不将定义不足降级成猜测，不复制或改写知识库中的指标定义。
