# xuanji-mini 最小开发方案与 Session 交接

> 文档状态：已完成方案对齐，下一 Session 按本文开始实现
>
> 更新时间：2026-08-18
>
> 当前阶段：只创建开发方案，不创建半成品 Skill，不修改现有工程

## 1. 最终目标

创建一个名为 `xuanji-mini` 的轻量 Skill，接收 DataWorks DQC 下载、安装链路告警，在当前用户权限下使用现有指标知识库和 DView MCP 进行只读查询，按已经验证过的下载/安装排查流程完成有限的维度定位。

第一阶段只要求：

```text
DataWorks DQC 告警
    -> 读取告警中实际存在的信息
    -> 在现有指标知识库中匹配指标定义
    -> 查询告警对象表或知识库标准指标
    -> 按既有下载/安装 Playbook 做维度下钻
    -> 返回简洁的结构化分析结果
```

本阶段不处理 Grafana，不接飞书，不做定时任务。先证明下面这条链路能在一次 LLM 调用内跑通：

```text
一份 DQC payload -> $xuanji-mini -> DView MCP 查询 -> 最终 JSON
```

跑通以后，才把结果接回 `grafana-lark-daily-push-poc` 的告警卡片。

## 2. 已冻结的产品原则

### 2.1 所见即所得

`xuanji-mini` 只消费 DQC 告警中实际提供的信息，不替告警系统补业务语义。

DQC webhook 已经能够提供以下关键上下文：

- 所属项目；
- 对象类型；
- 对象名称；
- 实际分区；
- 一条或多条触发规则；
- 规则名；
- 当前样本值；
- 比较符；
- 阈值；
- 规则强弱、状态、规则 ID 等辅助字段。

因此，不维护 `ruleId -> metric_id` 映射，也不要求告警维护方增加额外的分析标签。

### 2.2 知识库是指标定义的唯一来源

指标定义、标准名称、别名、分子、分母、观察窗口和标准 SQL 只能来自当前已有指标知识库。

允许的匹配包括：

- 标准名称精确命中；
- alias 精确命中；
- 仅做无业务含义变化的文本归一化后命中，例如大小写、空格、全半角、`->` 与 `→`；
- 规则名中的修饰语去除后唯一命中，例如“最近 1 天低于阈值”“连续 3 周下降”。

不允许：

- 根据字段名猜测指标定义；
- 根据规则阈值反推分子分母；
- 从 `xuanji` 或“下载完成率”工程复制一份指标定义，绕开知识库；
- 用相似指标替代当前指标；
- 为了继续分析而临时发明别名或 mapping；
- 多个候选定义存在歧义时自行选择一个。

如果知识库中找不到唯一、可信的对应指标，立即返回：

```json
{
  "status": "insufficient_definition",
  "metric_hint": "告警规则中提取出的指标名称",
  "reason": "当前指标知识库中未找到唯一对应的标准指标定义",
  "action": "请告警或指标维护方补充或修正知识库定义"
}
```

这不是 `xuanji-mini` 的失败，也不应在本 Skill 内修复。它是给告警维护方和指标知识库维护方的明确反馈。

### 2.3 Skill 负责定位，不承诺自动确认根因

第一阶段允许回答：

- 异常主要集中在哪些游戏或样本切片；
- 结构变化和分组自身表现变化分别贡献了多少；
- 剔除头部切片后，大盘异常是否明显缩小；
- 最强父切片内部还有哪个二级维度值得关注；
- 当前证据不足在哪里。

第一阶段不允许直接断言：

- 某客户端版本就是根因；
- 某 CDN、错误码或系统安装器就是根因；
- 游戏发版与指标下降存在因果关系；
- 最大候选就是已确认根因；
- 时间上同时发生就证明存在因果关系。

推荐措辞：

- “异常主要集中在……”；
- “该切片解释了较多不利变化……”；
- “剔除该切片后，异常明显缩小/仍然存在……”；
- “该现象与某事件时间接近，仅作为背景证据……”；
- “建议对应团队继续核查……”；
- “当前只能定位影响范围，尚不能确认机制根因”。

## 3. 严格范围

### 3.1 第一阶段纳入

- 来源：DataWorks DQC webhook 告警；
- 场景：TapTap Android 下载和安装链路；
- 输入：原始 DQC JSON，或内容等价的告警文本；
- 指标发现：现有 `taptap-data-analysis` 指标知识库；
- 查询：现有只读 DView MCP；
- 分析：指标复核、历史比较、一级维度定位、最多一次二级下钻；
- 输出：一段最终 JSON；
- 运行方式：用户或 Host 显式调用 `$xuanji-mini`。

### 3.2 第一阶段不纳入

- Grafana 告警；
- 飞书卡片渲染与发送；
- 最近 N 小时告警聚合；
- webhook receiver；
- 定时调度；
- 告警状态管理；
- Python 包；
- CLI；
- 虚拟环境；
- 数据库；
- 缓存；
- 运行 manifest；
- JSON Schema 文件；
- Golden Case 框架；
- Shadow Planner；
- 独立查询执行器；
- 完整 Evidence Store；
- 复杂重试、锁、队列和服务化部署。

上述能力只有在最小 Skill 实际跑通并暴露明确需要后，才能逐项增加。

## 4. 最终目录结构

实现完成后的目录固定为：

```text
xuanji-mini/
├── SKILL.md
├── agents/
│   └── openai.yaml
└── references/
    └── download-install-playbook.md
```

约束：

- 不创建 `scripts/`；
- 不创建 `src/`、`tests/`、`config/`、`assets/`；
- 不复制现有项目源代码；
- 不复制超长 SQL；
- 不新增 `requirements.txt`、`pyproject.toml` 或 lock 文件；
- `SKILL.md` 保持简短，只放必须每次加载的执行流程；
- 表、字段、维度关系、门槛和日期语义放入单一 reference；
- 最终 Skill 不继续扩展辅助说明文档。

本文件是开发阶段的 Session 交接文档。实现完成并验证后，应把仍有执行价值的内容收敛到 `SKILL.md` 和 `references/download-install-playbook.md`，再决定是否移出最终 Skill 目录，避免把开发过程文档长期带入 Skill。

## 5. 现有材料及提取边界

### 5.1 指标知识库

指标识别必须使用当前安装的 `taptap-data-analysis` Skill 知识库，而不是在 `xuanji-mini` 中复制指标 YAML。

当前开发机器上的参考入口是：

```text
/Users/xindong/.agents/skills/taptap-data-analysis/knowledge-base/manifest.yaml
/Users/xindong/.agents/skills/taptap-data-analysis/knowledge-base/metrics/
/Users/xindong/.agents/skills/taptap-data-analysis/knowledge-base/tables/
```

但实现时不要把这个绝对路径当成可移植契约。运行时应按已加载 `taptap-data-analysis` Skill 的根目录解析其知识库；若 Host 无法提供该 Skill 或知识库，则无法合法完成指标定义匹配。

需要沿用的知识库规则：

- 先读 manifest 判断业务域；
- 再读目标域 `_index.yaml` 做名称/alias 匹配；
- 命中后读取对应 metric YAML；
- 需要字段结构时使用 `describe_table`；
- 标准 SQL 优先来自 metric YAML；
- 任何比率必须按知识库口径计算。

`xuanji-mini` 是本轮排查的主流程所有者。它使用知识库解决“指标是什么、怎么算”，再使用本文提取的 Playbook 解决“异常后先查什么、后查什么”。不要让普通问数流程把本次任务改写成另一个归因产品流程。

### 5.2 `下载完成率` 工程

来源目录：

```text
/Users/xindong/Documents/tmp_research/下载完成率
```

主要提取：

- 下载与安装的人工诊断优先级；
- 下载和安装归因宽表名称；
- APK 与沙盒日期语义；
- 游戏优先、预约自动下载并行快判；
- 游戏阶段后的终态路由思路；
- 非游戏 Top 3 和一次二级下钻；
- 游戏状态、版本和包体背景表；
- 质量桶、覆盖率和结论边界。

重点参考文件：

```text
下载完成率/android_attribution_execution_framework.md
下载完成率/android_download_attribution_dimensions.md
下载完成率/android_attribution_specialty_modules.md
下载完成率/docs/terminal_state_routing_design.md
下载完成率/android_download_metrics_definition.md
下载完成率/sql/analysis/android_attribution_game_first_screen.sql
下载完成率/sql/analysis/android_attribution_reserve_auto_download_screen.sql
下载完成率/sql/analysis/android_attribution_anomaly_fast_screen.sql
下载完成率/sql/analysis/android_attribution_terminal_state_routing.sql
下载完成率/sql/analysis/android_attribution_top_entity_counterfactual.sql
下载完成率/sql/analysis/android_attribution_lifecycle_event_timeline.sql
```

不提取：

- 每日 runner；
- OpenClaw 部署；
- 物化报告；
- 五文件产物契约；
- 完整维度 checklist；
- 进度文件；
- 1855 行批量下钻 SQL；
- 查询状态持久化；
- 调度和发布体系。

### 5.3 `xuanji` 工程

来源目录：

```text
/Users/xindong/Documents/tmp_research/xuanji
```

主要提取：

- 预检 -> 一级分解 -> 候选门禁 -> 反事实 -> 一次二级下钻 -> 停止；
- 前 7 个完整日的池化基线；
- 结构影响、表现影响和总影响的中心化对称分解；
- 样本、父范围占比、5bp 和质量桶门槛；
- 剔除头部切片的算术反事实；
- 父范围必须完整继承到二级查询；
- 不同维度家族的影响不能相加；
- 最多一次二级下钻，禁止三级组合；
- “定位影响范围，不确认根因”的报告边界。

重点参考文件：

```text
xuanji/docs/implementation/phase-1-download-install-mvp.md
xuanji/configs/metric-packs/download_completion_rate.yaml
xuanji/configs/metric-packs/install_completion_rate.yaml
xuanji/src/xuanji/primitives/decomposition.py
xuanji/src/xuanji/primitives/counterfactual.py
xuanji/sql/metric_snapshot.sql
xuanji/sql/dimension_breakdown.sql
```

不提取：

- Engine；
- Contracts；
- Metric Pack 加载器；
- Query Runtime；
- Warehouse bridge；
- Golden Cases；
- Shadow Planner；
- Evidence 持久化；
- 报告 renderer；
- 审计和离线验签。

## 6. DQC 输入解释规则

### 6.1 已知 webhook JSON 结构

DQC payload 的核心结构通常是：

```json
{
  "projectName": "tap_dw",
  "dqcEntityQuality": {
    "entityName": "ads_dmg_quality_platform_download_chain_monitor_1d",
    "actualExpression": "dt=2026-08-17"
  },
  "ruleChecks": [
    {
      "ruleId": 123,
      "ruleName": "【沙盒下载完成率】最近1天_低于75%",
      "checkResult": 0.7468,
      "op": ">=",
      "expectValue": 0.75,
      "blockType": 0,
      "checkResultStatus": 2
    }
  ]
}
```

真实 payload 存在两种字段层级，读取时使用以下回退规则：

```text
项目：
  dqcEntityQuality.projectName
  -> payload.projectName

对象表：
  rule.tableName
  -> dqcEntityQuality.entityName

实际分区：
  rule.actualExpression
  -> dqcEntityQuality.actualExpression

任务 ID：
  rule.taskId
  -> dqcEntityQuality.taskId

比较符：
  rule.op
  -> rule.operator

阈值：
  rule.expectValue
  -> rule.criticalThreshold / rule.warningThreshold 只作为补充信息
```

未知字段应保留但不猜含义。缺字段时只降低可分析程度，不得填造值。

### 6.2 告警文本输入

如果输入不是 JSON，而是钉钉/邮件中渲染后的文本，也允许 LLM 从可见文本提取同等字段。例如：

```text
【所属项目】：tap_dw
【对象名称】：ads_dmg_quality_platform_download_chain_monitor_1d
【实际分区】：dt=2026-08-17
规则名：【沙盒下载完成率】最近1天_低于75%
当前样本值：0.7468
比较符：>=
阈值：0.75
```

`$velocityCount)` 是 DataWorks 模板残留，解析时忽略。

### 6.3 指标名提取

优先提取规则名 `【】` 中的内容：

```text
【apk下载完成->安装完成率】最近1天_低于73%
                         -> apk下载完成->安装完成率

【沙盒下载完成率】连续3周下降
                         -> 沙盒下载完成率
```

没有 `【】` 时，可以去除明显的规则条件后把剩余文本作为 `metric_hint`，但只有知识库唯一命中后才能继续。

### 6.4 同指标多规则合并

一个 DQC payload 可以同时包含同一基础指标的多个规则。例如：

```text
【apk下载完成->安装完成率】最近1天_低于73%
【apk下载完成->安装完成率】连续3周下降
```

两条规则必须合并为一个指标调查任务，避免重复查询和重复结论。合并键为：

```text
项目 + 对象表 + 实际分区 + 知识库标准指标
```

所有原规则仍需要保留在最终结果中。

注意：趋势规则的 `checkResult=1.0000` 很可能是规则命中标志，不是业务指标值。不得把所有 DQC `checkResult` 一律当作指标当前值。真实指标值应通过告警对象表或知识库标准 SQL 查询取得。

### 6.5 多指标处理

一个 payload 中不同指标分别执行定义门禁和分析。例如：

```text
APK 下载安装完成率 -> 知识库命中 -> 继续分析
沙盒下载完成率     -> 知识库命中 -> 继续分析
沙盒下载失败率     -> 知识库未命中 -> insufficient_definition
```

一个指标 `insufficient_definition` 不阻塞其他指标。最终结果应包含每个基础指标的独立状态。

第一版可以串行处理，避免引入并发、子 Agent 或任务编排。

## 7. 指标知识库门禁

每个 `metric_hint` 严格执行：

```text
读取知识库 manifest
    -> 路由业务域
    -> 读取业务域 _index.yaml
    -> 匹配标准名/alias
    -> 读取命中的 metric YAML
    -> 保存标准名称、口径、caveats、标准 SQL
```

允许继续的条件：

- 恰好命中一个标准指标定义；
- 定义覆盖告警中的 APK/沙盒和下载/安装语义；
- 时间窗口能够与告警规则和分区日期对齐；
- 可以确定合法的只读查询方式。

必须 `insufficient_definition` 的条件：

- 完全未命中；
- 命中多个定义且无法排除歧义；
- 只命中近似指标；
- 告警写“失败率”，知识库只有“完成率”；
- 告警写 APK，但定义只支持沙盒，或反之；
- 指标观察窗口与告警语义冲突；
- 指标文件缺少足以可靠查询的技术口径。

`insufficient_definition` 不继续查询指标，也不进入维度下钻。

## 8. 查询来源选择

知识库命中后，按最少查询、最高口径一致性选择来源。

### 8.1 告警对象表优先复现告警

当告警对象表直接包含该指标，并能通过 `describe_table` 唯一识别对应字段时，优先查询告警对象表：

```text
tap_dw.ads_dmg_quality_platform_download_chain_monitor_1d
```

它适合用于：

- 复现告警当前值；
- 获取同口径历史值；
- 检查 APK/沙盒行；
- 检查 DQC 告警表是否已经产出目标分区。

不得只凭记忆使用列名。若列结构不确定，先 `describe_table`。

### 8.2 知识库标准 SQL

出现以下情况时，使用 metric YAML 中的标准 SQL：

- 告警对象表没有对应指标；
- 告警对象表字段无法可靠映射；
- 需要标准分子、分母做复算；
- 需要知识库指定的标准结果表；
- 需要知识库 caveats 规定的时间窗口或过滤条件。

不要把查询告警表和查询标准指标机械地都执行一遍。只有需要对账或解释差异时才执行两者。

### 8.3 查询失败处理

允许根据明确的 SQL 错误修正并重试，最多两次。禁止通过改换相似指标、删掉关键过滤条件或换一个口径来“跑通”。

区分以下状态：

- 分区不存在或未就绪：`insufficient_data`；
- 查询返回合法空结果：先检查分区，仍为空后 `insufficient_data`；
- 权限不足：`query_blocked`；
- SQL 修正两次仍失败：`query_failed`；
- 有指标值但无法找到合法维度数据源：`unsupported_drilldown`。

这些状态不能伪装成“没有发现异常”。

## 9. 最小分析流程

### 9.1 总体状态机

第一版使用下面这条线性流程，不实现 Engine：

```text
PARSE_DQC
  -> RESOLVE_METRIC_DEFINITION
  -> PRECHECK
  -> ROOT_METRIC_AND_TREND
  -> GAME_FIRST_SCREEN
  -> TOP_SLICE_COUNTERFACTUAL（满足门槛时）
  -> TERMINAL_STATE_ROUTING（数据支持时）
  -> NON_GAME_L1（游戏解释不足时）
  -> ONE_L2（最强父候选值得继续时）
  -> OPTIONAL_GAME_CONTEXT（游戏明确主导时）
  -> CONCLUDE
```

这只是 Skill 中的执行顺序，不创建状态对象、状态表或持久化事件。

### 9.2 PRECHECK

必须检查：

1. 指标定义已经唯一命中；
2. 实际分区可以解析为业务日期；
3. 当前指标数据存在；
4. 基线需要的历史数据存在；
5. 分子、分母大于零且比率合法；
6. APK/沙盒、Android 和观察窗口与定义一致；
7. 安装指标的 cohort 日期语义已正确处理；
8. 维度查询不会跨越告警范围。

基线默认使用目标日前 7 个完整、可比业务日，并采用：

```text
baseline_rate = SUM(baseline_numerator) / SUM(baseline_denominator)
```

禁止使用 7 个逐日日率的简单平均，除非指标知识库明确规定另一种聚合方法。

如果规则包含“连续 3 周下降”等更长窗口，可用一次趋势查询覆盖最长必要窗口并把结果作为背景，但维度贡献的默认比较仍使用前 7 个完整日。不得把趋势规则中的布尔命中值当成指标值。

### 9.3 根指标和趋势

至少得到：

- 目标业务日期；
- 当前分子、分母、指标值；
- 基线分子、分母、指标值；
- 当前值相对基线的变化；
- 对完成率使用 `delta_bp = (current_rate - baseline_rate) * 10000`；
- 是否与 DQC 告警方向一致；
- 如存在长周期规则，保留相应趋势上下文。

如果查询结果与告警样本值不一致，先检查：

- DQC `checkResult` 是否只是趋势规则命中标志；
- 告警分区和指标 cohort 日期是否不同；
- APK 安装未来 3 天窗口是否已经成熟；
- 告警表与知识库标准表是否使用不同但合法的口径。

无法解释差异时，不进入业务归因，输出数据/口径缺口。

## 10. 下载链路 Playbook

下载归因宽表：

```text
tap_dw.ads_report_store_platform_device_game_download_chain_attribution_1d
```

正式粒度：

```text
dt + platform + game_type + device_id + game_id
```

### 10.1 第一轮：游戏与预约自动下载

下载指标优先并行检查：

```text
game_id
is_reserve_auto_download
```

目标是先回答：

- 是否由一个或少数游戏主导；
- 是否是大量游戏共同变化；
- 是否存在预约自动下载流量结构变化；
- 是否是预约组内部表现变化，而不是单纯流量占比变化；
- 游戏和预约是否可能覆盖同一批样本。

如果二者同时显著，不得直接相加。第一版可以明确标注“可能重叠”，不强制新增候选重叠查询。

### 10.2 游戏候选剔除反事实

当头部游戏满足以下任一条件时，执行一次剔除反事实：

- 占该维度家族不利变化至少 50%；
- 单项不利影响不小于大盘净不利变化。

反事实比较：

```text
原始大盘当前值 vs 基线
剔除头部游戏后的当前值 vs 基线
```

如果剔除后异常绝对值缩小至少 50%、恢复到 5bp 容差内或方向反转，可以认为该游戏是主要影响范围，允许继续查询游戏内部或游戏背景。

反事实只说明解释力，不证明根因。

### 10.3 终态路由

在数据字段和查询成本允许时，比较头部游戏或大盘的 `download_terminal_state` 分布：

```text
explicit_failed       -> 优先错误码、CDN、客户端版本方向
human_stop            -> 优先包体、设备、等待体验方向
unobserved_residual   -> 优先覆盖率、客户端进程或数据链路方向
末态分布稳定           -> 优先结构变化/within-between 分解
多项同时小幅变化       -> mixed，按标准维度顺序继续
```

终态路由只决定后续优先级，不改变指标定义和贡献算法。

### 10.4 非游戏一级维度

当游戏不能充分解释，或剔除后异常仍明显存在时，检查：

```text
apk_size_tier
channel_group
app_major_version
os_major_version
device_brand
```

`network_type_group`、`device_model`、存储和更细地域优先作为二级维度，不在第一轮进行无方向全量横扫。

只保留达到门槛的 Top 3 非游戏候选。

### 10.5 下载二级关系

最多选择一个父候选，允许的常用关系：

```text
game_id
  -> apk_size_tier, channel_group, app_major_version,
     os_major_version, device_brand

apk_size_tier
  -> game_id, channel_group, device_brand, os_major_version

channel_group
  -> game_id, app_major_version, device_brand

app_major_version
  -> device_brand, os_major_version

os_major_version
  -> device_brand, app_major_version

device_brand
  -> device_model, os_major_version, app_major_version,
     network_type_group

is_reserve_auto_download
  -> game_id, channel_group, apk_size_tier
```

二级查询必须完整继承父维度和值。

## 11. 安装链路 Playbook

安装归因宽表：

```text
tap_dw.ads_report_store_platform_device_game_install_chain_attribution_1d
```

APK 逻辑粒度：

```text
dt + platform + game_type + device_id + game_id + chain_id
```

沙盒逻辑粒度：

```text
dt + platform + game_type + device_id + game_id + install_round_id
```

安装表的物理分区 `dt` 表示下载完成 cohort 日期，不是安装结果事件发生日期。

### 11.1 第一轮：游戏

安装指标先检查：

```text
game_id
```

下载阶段专属的预约自动下载、首次下载网络和地域不进入安装一级归因。

### 11.2 安装非游戏一级维度

游戏解释不足时检查：

```text
apk_size_tier
app_major_version
os_major_version
device_brand
storage_headroom_tier
```

人工解释优先级为：

```text
APK / 沙盒
  -> 游戏贡献
  -> 游戏包版本 / 包大小
  -> 是否进入 installStart
  -> 安装完成 / 失败 / 失败原因
  -> 安装器类型 / 客户端版本
  -> 机型 / OS / 剩余存储
```

诊断事件只用于定位链路阶段，不替代正式安装完成率分子和分母。

### 11.3 安装二级关系

最多选择一个父候选，允许的常用关系：

```text
game_id
  -> apk_size_tier, app_major_version, os_major_version,
     device_brand, storage_headroom_tier

apk_size_tier
  -> game_id, device_brand, storage_headroom_tier

app_major_version
  -> device_brand, os_major_version

os_major_version
  -> device_brand, app_major_version, storage_headroom_tier

device_brand
  -> device_model, os_major_version, app_major_version,
     storage_headroom_tier

storage_headroom_tier
  -> apk_size_tier, device_brand, os_major_version
```

## 12. 贡献分解与候选门槛

### 12.1 中心化对称分解

对于每个维度桶，定义：

```text
current_share   = 当前桶分母 / 当前大盘分母
baseline_share  = 基线桶分母 / 基线大盘分母
current_rate    = 当前桶分子 / 当前桶分母
baseline_rate   = 基线桶分子 / 基线桶分母
overall_mid     = (当前大盘率 + 基线大盘率) / 2
```

计算：

```text
composition_impact
  = (current_share - baseline_share)
    * ((current_rate + baseline_rate) / 2 - overall_mid)

performance_impact
  = (current_rate - baseline_rate)
    * (current_share + baseline_share) / 2

total_impact
  = composition_impact + performance_impact
```

对“越高越好”的完成率指标：

```text
adverse_impact = MAX(-total_impact, 0)
```

对其他方向的指标，必须使用知识库定义的好坏方向；方向无法确认时停止，不猜测。

同一维度家族内，所有桶的 `total_impact` 之和应接近大盘变化。闭合误差明显时，不从该维度产生正式候选。

### 12.2 候选门槛

沿用现有验证过的最低门槛：

- 当前样本或基线日均样本不少于 100；
- 当前或基线父范围占比不少于 1%；
- 对大盘不利影响不少于 5bp，即 `0.0005`；
- 非质量桶；
- 维度家族贡献闭合；
- 覆盖率足以支撑结论。

以下值默认只作质量线索：

```text
unknown
invalid
invalid_*
not_applicable
unmatched
ambiguous_*
__none__
__other__
__other_below_threshold__
```

质量桶不能成为业务根因候选，但质量桶自身突然扩大可以形成数据质量发现。

### 12.3 维度之间不可相加

`game_id`、品牌、版本、包体等维度覆盖的是同一批样本的不同投影。各维度家族的不利影响不能相加，也不能声称 Top 3 跨维度合计解释了多少变化。

候选只按单项全局不利影响排序。

## 13. 二级下钻约束

只有满足以下条件才执行二级：

- 一级存在达到门槛的候选；
- 该候选有足够解释力或明确业务价值；
- 二级关系在 Playbook 中已注册；
- 查询能完整继承根范围和父范围；
- 本次调查尚未做过二级下钻。

二级必须继承：

- 标准指标定义；
- 目标日期和基线日期；
- 平台；
- APK/沙盒；
- 下载/安装阶段；
- 根过滤条件；
- 一级父维度名；
- 一级父维度值。

二级完成后立即停止自动扩展。禁止：

- 三级下钻；
- 临时创造新的维度组合；
- 同时对多个父节点展开；
- 为了找到“答案”不断扫描所有字段。

## 14. 可选游戏背景查询

只有游戏候选明确主导时才查询：

```text
tap_dw.dwt_game_detail_info_view_df
tap_dmp.ods_server_sync_apks
```

用于获取：

- 游戏状态；
- 可下载状态；
- 当前 APK ID；
- APK 版本和版本码；
- 包体大小；
- 创建时间和状态；
- 是否存在与异常日期接近的发版或状态变化。

运营内容表只在确有必要时作为补充，不进入 V0 必查项。

背景事件与异常时间接近只能写成待核查线索，不能升级为因果根因。

## 15. 停止条件

出现任一情况立即停止当前指标调查：

1. 知识库未唯一命中：`insufficient_definition`；
2. 目标或基线分区不足：`insufficient_data`；
3. 分子、分母或方向无法可靠确定；
4. 当前值与告警无法对齐且无法解释；
5. 查询被权限阻止：`query_blocked`；
6. 查询修正两次后仍失败：`query_failed`；
7. 找不到合法下钻数据源：`unsupported_drilldown`；
8. 所有一级候选均低于 5bp：`no_dominant_slice`；
9. 头部候选反事实解释力有限，且没有更强一级方向；
10. 已完成一次二级下钻；
11. 继续查询只会增加相关性线索，不能提高定位价值；
12. 当前证据只能定位影响范围，无法确认机制。

停止不是失败。最终结果必须区分“没有主导切片”“数据不足”“定义不足”“查询失败”和“已经完成有限定位”。

## 16. V0 输出契约

第一版不创建独立 JSON Schema 文件，但要求最终回答只输出一个 JSON object，不输出 Markdown 前后缀。

### 16.1 顶层结构

```json
{
  "source": "dataworks_dqc",
  "project": "tap_dw",
  "table": "tap_dw.ads_dmg_quality_platform_download_chain_monitor_1d",
  "partition": "dt=2026-08-17",
  "overall_status": "partial",
  "investigations": []
}
```

`overall_status`：

```text
completed  所有指标均形成合法结果
partial    部分指标完成，部分指标定义/数据/查询受阻
failed     没有任何指标形成合法结果
```

### 16.2 单指标结果

完成或有限定位：

```json
{
  "status": "completed",
  "metric_hint": "apk下载完成->安装完成率",
  "metric": "APK 下载安装完成率",
  "alert_rules": [
    {
      "rule_name": "【apk下载完成->安装完成率】最近1天_低于73%",
      "check_result": 0.7021,
      "operator": ">=",
      "threshold": 0.73
    },
    {
      "rule_name": "【apk下载完成->安装完成率】连续3周下降",
      "check_result": 1.0,
      "operator": "=",
      "threshold": 0.0
    }
  ],
  "alert_partition": "2026-08-17",
  "analysis_date": "2026-08-15",
  "current_value": 0.7021,
  "baseline_value": 0.735,
  "delta_bp": -329,
  "top_findings": [
    {
      "level": 1,
      "dimension": "game_id",
      "value": "example",
      "label": "example game",
      "adverse_impact_bp": 120,
      "finding": "异常主要集中在该游戏样本"
    }
  ],
  "counterfactual": null,
  "summary": "异常主要集中在……",
  "evidence_limits": [
    "当前结果只定位影响范围，不能确认机制根因"
  ],
  "recommended_action": "建议继续核查……",
  "queries": [
    {
      "purpose": "root_metric",
      "query_id": "provider query id if available"
    }
  ]
}
```

所有示例值均为结构示意，运行时不得照抄。

### 16.3 定义不足

```json
{
  "status": "insufficient_definition",
  "metric_hint": "沙盒下载失败率",
  "alert_rules": [],
  "reason": "当前指标知识库中未找到唯一对应的标准指标定义",
  "action": "请告警或指标维护方补充或修正知识库定义"
}
```

### 16.4 无主导切片

```json
{
  "status": "no_dominant_slice",
  "metric_hint": "沙盒下载完成率",
  "metric": "沙盒下载完成率",
  "current_value": 0.7468,
  "baseline_value": 0.752,
  "delta_bp": -52,
  "summary": "已完成一级维度检查，没有非质量切片达到 5bp 门槛",
  "evidence_limits": [
    "不能选择最大但低于门槛的切片作为原因"
  ]
}
```

## 17. `SKILL.md` 实现要求

### 17.1 Frontmatter

只包含：

```yaml
---
name: xuanji-mini
description: ...
---
```

description 必须同时说明：

- 接收 DataWorks DQC 下载/安装链路告警；
- 使用现有 TapTap 指标知识库和只读 DView MCP；
- 做指标复核、维度下钻和有限归因定位；
- 找不到指标定义时输出 `insufficient_definition`；
- 触发词包括 DQC 告警分析、下载/安装异常排查、维度下钻。

不要在 frontmatter 增加其他字段。

### 17.2 Body

`SKILL.md` 只保留每次运行都需要的流程：

1. 解析 DQC；
2. 合并同指标规则；
3. 指标知识库门禁；
4. 查询根指标和趋势；
5. 读取 playbook；
6. 一级、反事实、一次二级；
7. 停止和输出；
8. 安全与结论边界。

详细表、维度关系、公式和门槛链接到 `references/download-install-playbook.md`。

### 17.3 安全边界

必须写入：

- 只读查询；
- 禁止 DDL/DML；
- 禁止修改数仓；
- 禁止输出或保存凭据；
- 禁止通过 shell/curl 绕过 MCP；
- 不查询目标日期之后的数据作为历史解释；
- 不把失败或空结果写成零；
- 不将定义不足降级成猜测。

## 18. `download-install-playbook.md` 实现要求

reference 从本文提炼以下内容：

- 下载和安装宽表；
- 日期与 cohort 语义；
- 下载/安装一级维度；
- 合法二级关系；
- 游戏优先和预约快判；
- 终态路由；
- 候选门槛；
- 中心化对称分解；
- 剔除反事实；
- 游戏背景表；
- 质量桶；
- 停止条件；
- 结论措辞边界。

reference 不复制指标定义。指标定义仍由知识库在运行时提供。

reference 不复制现有完整 SQL。LLM 根据知识库定义、`describe_table` 结果和简洁查询模式生成本次只读 SQL。

## 19. `agents/openai.yaml` 实现要求

使用 `skill-creator` 的生成脚本创建，不手写不必要字段。只提供：

```text
display_name
short_description
default_prompt
```

建议语义：

```text
display_name: 璇玑 Mini
short_description: 分析 DataWorks DQC 下载与安装链路告警
default_prompt: 使用 $xuanji-mini 分析这份 DataWorks DQC 告警并进行维度下钻。
```

## 20. 逐步开发计划

### Step 1：重新检查工作区

下一 Session 开始时执行：

```text
1. 确认 cwd=/Users/xindong/Documents/tmp_research
2. 查看 xuanji-mini/ 当前文件
3. 查看相关仓库 git status
4. 不覆盖用户在本文件或其他工程中的新改动
```

### Step 2：读取 Skill 创建规范

完整读取：

```text
/Users/xindong/.codex/skills/.system/skill-creator/SKILL.md
```

按规范使用 `init_skill.py` 初始化，不手工造不完整目录。

初始化目标是当前工作区的 `xuanji-mini/`。由于目录已经存在本开发文档，先确认初始化脚本对已有目录的行为；不要用破坏性命令删除本文。若脚本拒绝已有目录，可先在安全临时目录初始化，再用 `apply_patch` 将所需文件加入当前目录。

### Step 3：提炼 Playbook

重新读取第 5 节列出的现有工程源文件，创建：

```text
xuanji-mini/references/download-install-playbook.md
```

只提炼稳定规则，不复制完整项目说明和超长 SQL。

验收：

- 下载、安装两条路径清晰；
- 表名正确；
- 一级和二级关系完整；
- 日期语义明确；
- 门槛和公式明确；
- 不含指标定义副本；
- 不引入工程运行依赖。

### Step 4：编写 `SKILL.md`

创建简洁的主流程并链接 reference。

验收：

- 明确 `insufficient_definition`；
- 明确同指标多规则合并；
- 明确趋势规则 `checkResult` 不一定是指标值；
- 明确根指标查询来源选择；
- 明确一级、反事实、一次二级；
- 明确只读和结论边界；
- 最终只输出 JSON；
- 不包含依赖安装和脚本运行步骤。

### Step 5：生成 `agents/openai.yaml`

使用 skill-creator 自带脚本生成 UI 元数据。

验收：

- `display_name`、`short_description`、`default_prompt` 与 Skill 一致；
- 不增加图标、品牌色或其他未要求字段。

### Step 6：基础校验

运行：

```text
quick_validate.py xuanji-mini
```

修复所有 frontmatter、命名和目录问题。

### Step 7：使用脱敏 DQC 样例前向测试

使用本节样例：

```text
数据质量(DQC)校验告警
【所属项目】：tap_dw
【对象类型】：MaxCompute 表
【对象名称】：ads_dmg_quality_platform_download_chain_monitor_1d
【实际分区】：dt=2026-08-17

【apk下载完成->安装完成率】最近1天_低于73%
当前样本值: 0.7021 | 比较符 >= | 阈值 0.73

【apk下载完成->安装完成率】连续3周下降
当前样本值: 1.0000 | 比较符 = | 阈值 0.0

【沙盒下载完成率】最近1天_低于75%
当前样本值: 0.7468 | 比较符 >= | 阈值 0.75

【沙盒下载失败率】最近1天_高于1%
当前样本值: 0.0106 | 比较符 <= | 阈值 0.01
```

预期行为：

1. APK 安装两条规则合并为一个调查；
2. `1.0000` 不被解释成安装完成率；
3. 知识库命中的指标进入查询；
4. 当前知识库未命中的指标返回 `insufficient_definition`；
5. 一个定义不足不阻塞其他指标；
6. 查询只读；
7. 最终只有一个 JSON object；
8. 不出现无证据根因断言。

前向测试会访问真实 DView MCP。执行前需确认使用当前用户权限，且不会发送飞书或修改生产数据。

### Step 8：最小迭代

只针对真实测试暴露的问题修改：

- 规则名合并失败；
- 知识库命中不稳定；
- 日期语义错误；
- 查询顺序偏离 Playbook；
- 父范围未继承；
- 超过一次二级；
- 结果不是合法 JSON；
- 将线索写成根因。

不要在此阶段顺带增加 POC 接口、脚本、飞书、调度或 JSON Schema。

## 21. V0 完成标准

满足以下全部条件，才认为 `xuanji-mini` V0 完成：

1. Skill 基础校验通过；
2. 可以从 DQC JSON 或告警文本提取表、分区和规则；
3. 同指标多规则只调查一次；
4. 指标定义只来自现有知识库；
5. 未命中稳定返回 `insufficient_definition`；
6. 命中指标可以通过 DView MCP 查询当前值和基线；
7. 可以按下载或安装 Playbook 做游戏优先的一级定位；
8. 只有满足门槛时才做反事实或一次二级下钻；
9. 数据不足、权限阻塞、查询失败和无主导切片被正确区分；
10. 不同维度家族的影响不相加；
11. 不把规则布尔结果当成指标值；
12. 不把相关性或最大切片宣称为根因；
13. 最终输出单一 JSON object；
14. 没有 Python、CLI、虚拟环境和其他工程依赖；
15. 没有修改 `xuanji`、`下载完成率` 或 `grafana-lark-daily-push-poc`。

## 22. V0 之后的后续顺序

V0 完成后，按以下顺序评估，不能提前混入：

1. 将手工输入替换为 POC 已规范化的 DQC 告警条目；
2. 冻结一个很薄的请求 JSON 契约；
3. 冻结一个很薄的结果 JSON 契约；
4. 在 POC 告警卡片中增加分析摘要区域；
5. 分析失败时仍发送原始 DQC 告警卡片；
6. 再串联过去 N 小时告警聚合；
7. 最后才讨论无人值守 `codex exec` 和定时调度。

不应在 V0 之前拆分或重构 `grafana-lark-daily-push-poc` 的接收、窗口聚合和飞书发送代码。

## 23. 下一 Session 快速入口

下一 Session 可以直接使用以下任务描述：

```text
继续开发 /Users/xindong/Documents/tmp_research/xuanji-mini。
先完整阅读 DEVELOPMENT_PLAN.md 和 skill-creator/SKILL.md，然后按文档 Step 1-7
创建最小 xuanji-mini Skill。只创建 SKILL.md、agents/openai.yaml 和一个
references/download-install-playbook.md；不要写 Python、脚本、CLI、JSON Schema，
不要修改 xuanji、下载完成率或 grafana-lark-daily-push-poc。完成后校验 Skill，
再用文档中的脱敏 DQC 样例做一次只读前向测试。
```
