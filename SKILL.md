---
name: xuanji-mini
description: 接收 DataWorks DQC 的 TapTap Android 下载或安装链路告警，使用现有 taptap-data-analysis 指标知识库和只读 DView MCP 复核指标、下钻维度并做有限归因定位；知识库无法唯一匹配指标时输出 insufficient_definition。用于 DQC 告警分析、下载异常排查、安装异常排查和维度下钻。
---

# 璇玑 Mini

处理单份 DQC JSON 或等价告警文本。每个指标串行执行有限定位，最终只输出一个 JSON object，不添加 Markdown 围栏或解释文字。

## 1. 解析告警

- 只读取输入中实际存在的字段；保留未知字段但不猜含义，忽略 `$velocityCount)` 模板残留。
- 按以下顺序回退：项目 `dqcEntityQuality.projectName -> payload.projectName`；对象表 `rule.tableName -> dqcEntityQuality.entityName`；分区 `rule.actualExpression -> dqcEntityQuality.actualExpression`；任务 ID `rule.taskId -> dqcEntityQuality.taskId`；通过条件比较符 `rule.op -> rule.operator`，记为 `pass_operator`；阈值 `rule.expectValue`，缺失时仅把 `criticalThreshold` 或 `warningThreshold` 作为补充。DataWorks payload 的比较符描述“校验通过”，不是“触发告警”。
- 优先取规则名 `【】` 内文本作为 `metric_hint`；否则只去除明显的时间、趋势和阈值修饰语。
- 将输入 `ruleChecks` 的位置视为零基索引；后续每个调查通过 `rule_indexes` 原样指出自己覆盖的规则位置，不重排规则。
- 不把所有 `checkResult` 当成业务指标值。“连续 N 周下降”等趋势规则中的值可能只是命中标志，真实值必须查询取得。

## 2. 路由已注册告警

对象表为 `tap_dw.ads_dmg_quality_platform_download_chain_monitor_1d` 或省略项目名的同名表时，在指标匹配前必须完整读取 [DQC 告警路由表](references/dqc-alert-routing.md)，逐条匹配已注册档案。按路由表固定得到 APK/沙盒范围、链路阶段、规则类型、监控字段、监控分子/分母字段、`alert_operator`、知识库指标名和 Playbook ID；不得让调度层或模型临时推断这些映射。payload 的 `pass_operator` 与档案的 `alert_operator` 互补时表示同一条规则的正常两面，不是口径冲突。

同一对象表中未命中注册档案的规则返回 `insufficient_definition`，不得模糊匹配到最相似档案。注册档案声明知识库定义缺失时也返回 `insufficient_definition`，不得把路由表当成指标定义。其他对象表继续执行通用知识库匹配，但只有存在明确适用的 Playbook 时才可进入归因。

规则名已经唯一命中注册档案后，payload 的字段、比较符或阈值与档案存在无法解释的差异时，只记录非阻断 profile warning，并继续使用已注册范围和知识库定义执行根指标复核与归因。不得仅因这些差异返回 `insufficient_definition`，也不得丢弃已经完成的调查结果；真实值、原始比较符和阈值仍原样保留在 `alert_rules` 中。后续根指标预检若确实无法对齐当前值、方向、范围或日期，再按 Playbook 的停止条件返回对应状态。

路由完成后，按“项目 + 对象表 + 实际分区 + 知识库标准指标 + game_type”合并同一调查，保留全部原规则及其 `rule_indexes`。同一指标的绝对值、相对过去 7 日和三周趋势规则可以合并；APK 与沙盒不得合并。不同调查串行执行，一个调查受阻不得阻塞其他调查。

## 3. 通过指标定义门禁

加载 `taptap-data-analysis` Skill 并按其根目录解析知识库，不依赖固定绝对路径：先读 `knowledge-base/manifest.yaml` 路由业务域，再读目标域 `_index.yaml`，按标准名、alias 或不改变业务含义的文本归一化做精确匹配，最后读取唯一命中的 metric YAML。

只在定义唯一、语义覆盖 APK/沙盒与下载/安装、观察窗口可对齐且技术口径足以执行只读查询时继续。禁止按字段名、阈值或相似指标猜测，禁止把完成率替代失败率，禁止临时发明 alias 或 mapping。未命中、多义或口径不足时，不查询该指标，返回：

```json
{
  "status": "insufficient_definition",
  "rule_indexes": [0],
  "metric_hint": "告警规则中提取出的指标名称",
  "alert_partition": "输入中的原始分区",
  "alert_rules": [{"rule_name": "输入中的原始规则名"}],
  "reason": "当前指标知识库中未找到唯一对应的标准指标定义",
  "action": "请告警或指标维护方补充或修正知识库定义"
}
```

## 4. 加载分析规则

指标定义唯一命中后，在执行任何根指标复核或归因查询前，必须完整读取路由结果指定的 Playbook。当前 `download-install` 路由读取 [下载与安装排查 Playbook](references/download-install-playbook.md)。先根据知识库定义确认当前指标属于下载或安装链路，再执行 Playbook 中该链路的共同规则、预检、排查步骤和停止条件。

Playbook 是基线选择、日期语义、归因数据资产、维度顺序、贡献计算、候选门槛、反事实、二级下钻和停止条件的唯一来源。本文件不重复定义这些规则。不得依赖模型记忆、既往分析经验或本文件中的执行说明替代 Playbook，也不得因为预判没有明显归因而跳过其要求的合法检查；只有 Playbook 明确允许停止或跳过时才可结束相应步骤。

指标知识库仍是指标名、方向、分子、分母、观察窗口、标准 SQL 和 caveats 的唯一来源。Playbook 不得替代或改写指标定义。

## 5. 复核根指标

先按已加载 Playbook 分别解析告警表分区日期和实际分析业务日期；两者可以相同，但不得默认相同。在日期关系明确前不得执行根指标或归因查询。

使用当前用户权限下的只读 DView MCP。需要表结构时先 `describe_table`，不得凭记忆使用列名。

调用 `describe_table` 时，`schema` 与 `table` 的表名限定方式二选一：显式传 `schema` 时，`table` 只能传不含 schema/project 前缀的裸表名；若 `table` 传 `schema.table` 完整名，则必须省略 `schema`。例如使用 `schema: tap_dw` 时传 `table: ads_example_1d`，不得同时传 `table: tap_dw.ads_example_1d`。参数校验失败时先按此规则修正后重试；该重试不属于 SQL 执行或 SQL 修正次数，也不得改变后续分析结果。

1. 若告警对象表直接含有且能唯一识别目标指标，优先用它复现当前值和历史值。
2. 否则使用 metric YAML 的标准 SQL、标准结果表及 caveats。只有对账或解释差异确有必要时才同时查询两种来源。
3. 按已加载 Playbook 执行全部根指标预检，复核当前值、基线和告警方向。不得自行简化其范围、日期、样本、口径或对齐要求。
4. 只有根指标通过 Playbook 预检后才可进入归因；不通过时使用 Playbook 规定的停止条件和状态，不得为了继续下钻而更换口径。

任何 DView SQL 查询报错后，先读取 [SQL 快速报错排查手册](references/sql-fast-triage.md)，保留原始错误码、错误类别和错误信息，按同时匹配的报错信号与 SQL 形态选择修正规则，再重试。错误类别为 `semantic_analysis` 时必须先检查 SQL 本身并执行有依据的修正重试，不得直接结束查询。不得只凭错误码套用规则；手册没有明确匹配项时，只根据原始错误做最小修正。

SQL 因明确错误最多修正两次，不得删除关键过滤或更换口径以求成功。根指标或全部已登记归因家族的分区缺失、合法空结果、权限不足或查询失败，分别按 Playbook 记 `insufficient_data`、`query_blocked` 或 `query_failed`；有根指标但完整根范围无法从归因数据源复现，或没有任何已登记维度字段可执行时记 `unsupported_drilldown`。单个维度家族出现这些问题时只淘汰当前家族、记录 `evidence_limits` 并继续后续家族，不得提前返回受阻状态。失败和空结果不得写成零。

DView 查询结果存在 1000 行返回上限。任何按维度、版本、错误码或其他业务键分桶的查询，都必须在 SQL 内按 Playbook 和已登记模板先保留合法候选桶，再把其余源桶聚合进不可候选的闭合残差桶；不得依赖 DView 截断、`LIMIT`、业务 Top 或多页拼接得到“近似完整”结果。模板声明结果预算时必须校验实际返回行数、源桶数和残差吸收桶数；返回恰好 1000 行、超过预算、缺少规定的基数审计字段，或无法证明全部源桶已经闭合时，当前家族按不完整结果失败并继续后续家族，不得从被截断结果产生候选。

## 6. 执行归因

根指标通过后，严格按已加载 Playbook 中对应链路的顺序执行归因。需要字段结构时先 `describe_table`；每个查询完整继承知识库定义和 Playbook 要求的分析范围。

不得省略 Playbook 要求的阶段、把可选步骤当成必选步骤，或在 Playbook 之外临时增加维度、组合、算法、门槛和因果判断。单个维度家族失败是家族级限制：该家族不得产生候选，但只要根指标和归因数据源的完整根范围仍合法，就必须记录限制并按 Playbook 继续后续已登记家族，不得直接结束整个调查。只有根范围或全部已登记归因数据源不可用时才返回受阻状态。某一步因数据、权限或适用条件不能执行时，按 Playbook 的边界继续或停止，不得用猜测补足证据。

已注册下载/安装告警通过根指标预检后，必须按 Playbook 生成 `attribution_execution`。进入归因时先冻结完整 `steps` 顺序，每完成一步立即写入其真实 `status`、`candidate_count` 或 `reason`，不能等到撰写结论时根据已有候选反推执行记录。单步失败仍保留在原位置并继续；不得省略失败项、重排步骤、把未执行写成成功，或因已经找到游戏候选而截断固定队列。实际执行二级归因时，另在 `secondary_steps` 中记录唯一的父维度、父切片身份、子维度、状态和候选数；未执行时省略该字段或写空数组，不得把二级候选伪装成一级候选。

### 固定归因队列运行协议

根指标预检和告警新增性门禁已经决定进入 `mode=full_queue` 后，必须在本 Skill 根目录使用本地 runner 冻结并执行完整一级队列：

1. 先执行 `python3 -m runtime.runner init --run-id <run_id> --chain <download|install> --game-type <app|sandbox> --metric <标准指标> --alert-date <alert_dt> --analysis-date <analysis_dt>`。不得在门禁决定进入 `full_queue` 前创建 run。
2. 重复执行 `python3 -m runtime.runner next --run-id <run_id>`，每次只处理票据返回的当前动作。`execute_query` 的 `rendered_sql` 必须原样提交只读 DView MCP，不得自行选择 QuerySpec、合并家族或重写 SQL。
3. 使用 `python3 -m runtime.runner record --run-id <run_id>` 从 stdin 登记 DView 原始结果或原始错误。成功和 SQL 错误必须回传票据中的 `rendered_sql_sha256`；有真实 query ID 时原样回传，没有时省略。
4. `repair_query` 表示当前游标已锁定。只依据票据内嵌的完整 triage、原始错误和失败 SQL提交 `repair_submitted`；不得执行其他步骤，不得修改源 QuerySpec。runner 最多接受两次通过保护门禁的修正。
5. 单步骤 `failed` 后继续调用 `next`；不得把家族失败升级为整个调查失败。沙盒 `install_stage` 由 runner 自动记录为 `skipped_not_applicable`。
6. 只有 `next` 返回 `queue_complete` 后才执行 `python3 -m runtime.runner export --run-id <run_id>` 生成 `attribution_execution`，并在最终返回前执行 `python3 -m runtime.runner validate-final --run-id <run_id> --analysis-json <analysis.json> --investigation-index <index>`。
7. 不得直接修改 `.runs/*/state.json`、票据、SQL、事件或 revision，不得绕过 runner 提前调用 writer。`validate-final` 未通过时不得提交最终结果。

当前为任务票据模式：runner 能确定性约束队列、资产、参数、SQL hash、状态和最终证据，但无法直接观察模型向 DView MCP 发出的请求；实际提交 SQL 与票据一致性仍依赖调用方如实回传 hash。

## 7. 停止并输出

使用 Playbook 的停止条件、状态和结论边界。不得选择未通过 Playbook 门槛的候选讲故事，也不得在满足停止条件后继续无方向扩展。

结构化事实冻结后、撰写任何用户可见字段前，必须完整读取 [告警诊断文案规范](references/diagnosis-writing-policy.md)。文案规范只调整 `summary`、各类 `finding`、`evidence_limits`、`recommended_action`、`reason` 和 `action` 的表达，不得改变 Playbook 已确定的状态、对象、数值、证据强度或输出协议，也不得执行独立的二次润色。最终输出前按文案规范完成独立可读性与措辞自检。

顶层输出固定使用精确字段名 `source: "dataworks_dqc"`、`project`、
`table`、`partition`、`overall_status` 和 `investigations`。
`project` 是从输入解析出的原始项目名，`table` 是带项目名的原始 DQC
对象表，`partition` 是原始 DQC 分区表达式；三者都必须是非空字符串，不能
改名为 `project_name`、`object_table`、`alert_partition` 或放进
`investigations`。这些字段属于返回 JSON 根节点，即后续 Host 信封的
`analysis` 对象内部顶层；`investigations[].alert_partition` 仍须按调查
单独输出，不能替代根节点 `partition`。输入确实无法解析出任一必填审计字段时
不得猜测，调用方不能把该返回包装成成功结果。`overall_status` 为：所有指标
形成合法结果时 `completed`；部分完成、部分受阻时 `partial`；没有任何指标
形成合法结果时 `failed`。

每个调查必须先写 `rule_indexes`，并包含非空 `metric_hint`、原始非空 `alert_partition` 和 `alert_rules`。`alert_rules` 与 `rule_indexes` 一一对应，每项至少包含输入中的非空 `rule_name`；只有原始规则实际提供时才保留 `check_result`、`operator` 和 `threshold`。`rule_indexes` 是非空、升序、无重复的零基整数数组；每个下标必须落在本次输入的 `ruleChecks` 内，不同调查不得重复使用下标，全部调查合起来必须恰好覆盖每条输入规则。合并同一标准指标时，一个调查可以包含多个下标。不要伪造缺失字段或 query ID。

已注册告警中，除知识库或路由尚未建立的 `insufficient_definition` 外，调查必须包含 Playbook 定义的 `attribution_execution`。`completed` 和完成归因后的 `no_dominant_slice` 使用 `mode=full_queue`；既有异常延续使用 `mode=existing_anomaly_stop`；根指标或归因根范围发生数据、权限或查询失败时使用 `mode=root_precheck_failed`；完整归因根范围无法复现时才可使用 `mode=root_scope_failed`。进入家族查询后，任何受阻状态都必须保留完整队列。该字段是 writer 的提交门禁，缺失、顺序不完整或状态矛盾时不得把结果包装为成功。

`completed` 和 `no_dominant_slice` 调查必须使用卡片的规范字段名，并包含 `YYYY-MM-DD` 的 `analysis_date`、标准指标 `metric`、有限数值 `current_value`、`baseline_value`、`delta_bp`、非空 `summary`、非空字符串数组 `evidence_limits` 和非空 `recommended_action`。其他受阻状态必须包含非空 `reason` 和 `action`；只有指标定义和日期对齐已经完成时才可附带 `analysis_date`，`insufficient_definition` 不得猜测。不得改写为嵌套 `root_metric`、`findings` 或 `counterfactual.interpretation`，否则展示层会拒绝结果。

`top_findings` 只保存实际完成贡献计算、通过闭合与质量检查并达到 Playbook 候选门槛的具体切片，不能保存整体指标变化或待执行事项。每项必须包含非空 `dimension`、非空 `label` 或 `value`、`attribution_level=primary|secondary`、有限数值 `adverse_impact_bp` 和非空 `finding`。一级 finding 的 `dimension` 必须回勾 `steps` 中候选数为正的同名家族；二级 finding 还必须包含与 `secondary_steps` 完全一致的 `parent_dimension` 及非空 `parent_label` 或 `parent_value`。每个家族写出的 finding 数不得超过对应 `candidate_count`。整体变化只写入 `summary` 或顶层 `finding`。存在至少一个合法切片且执行清单中至少一个一级或二级 `candidate_count` 为正时才可返回 `completed`；完成队列后的 `no_dominant_slice` 要求全部候选数均为零并省略 `top_findings`。单个家族失败不算“规定下钻未完成”。只有根范围或全部已登记家族受阻时才按实际原因返回 Playbook 的受阻状态，不得返回 `completed`。

`counterfactual` 只在实际执行剔除计算后输出，且必须包含非空 `dimension`、非空 `label` 或 `value`、有限数值 `removal_delta_bp`、有限数值 `restoration_ratio` 和非空 `finding`。未执行、未触发或无法计算时省略整个字段，把证据边界写入 `evidence_limits`；不得用 `counterfactual.finding` 描述“尚未执行”。`no_dominant_slice` 不得包含 `counterfactual`。

结论的证据边界严格遵守 Playbook，用户可见措辞严格遵守文案规范；不得把定位结果升级为未经证实的因果结论。

DView 返回真实 query ID 时，可用 `queries: [{"purpose": "...", "query_id": "..."}]` 保留；没有返回时省略 `queries`，不得伪造。

## 安全边界

- 只通过现有 DView MCP 执行只读查询；禁止 DDL、DML 和任何数仓修改。
- 禁止输出、保存或转发凭据；禁止用 shell、curl 或自建连接绕过 MCP。
- 分析时间范围必须遵守已加载 Playbook 的日期边界和知识库定义的观察窗口，不得自行扩大或引入额外解释数据。
- 不把查询失败、空结果或缺失值当成零或“未发现异常”。
- 不将定义不足降级成猜测，不复制或改写知识库中的指标定义。
