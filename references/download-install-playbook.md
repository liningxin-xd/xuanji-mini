# Android 下载与安装异常排查 Playbook

## 目录

- [共同规则](#共同规则)
- [告警分区与业务日期](#告警分区与业务日期)
- [MaxCompute SQL 门禁](#maxcompute-sql-门禁)
- [数据完整性](#数据完整性)
- [告警新增性](#告警新增性)
- [下载链路](#下载链路)
- [安装链路](#安装链路)
- [贡献分解](#贡献分解)
- [候选门槛与质量](#候选门槛与质量)
- [剔除反事实](#剔除反事实)
- [二级下钻](#二级下钻)
- [游戏背景](#游戏背景)
- [方向增强验证](#方向增强验证)
- [停止与结论](#停止与结论)

本文只定义排查顺序、数据资产和分析边界。指标名、方向、分子、分母、观察窗口、标准 SQL 与 caveats 必须在运行时读取现有指标知识库，不得从本文推断或替代。

## 共同规则

### 告警分区与业务日期

执行任何根指标或归因查询前，必须分别确定并保留两个日期，不得用一个 `dt` 变量混用：

```text
alert_dt     = DQC actualExpression 指向的告警对象表分区日期
analysis_dt  = 标准指标复算、基线比较和归因使用的源业务日期或 cohort 日期
```

对于告警对象表：

```text
tap_dw.ads_dmg_quality_platform_download_chain_monitor_1d
```

使用以下已注册映射：

- 下载类指标：`analysis_dt = alert_dt`。
- 下载完成到安装完成类指标：`analysis_dt = alert_dt - 2 天`。该映射同时适用于监控表中的 `app` 和 `sandbox` 行；两者各自的 p3d/1d 观察窗口仍以指标知识库为准。
- 安装类告警值必须在 `alert_dt` 分区读取路由档案指定的监控字段，并同时读取监控表的 `game_download_complete_prev_2d_device_num_1d` 和 `game_download_complete_and_install_complete_prev_2d_device_num_p3d`；标准指标复算和安装归因则使用 `analysis_dt`。

DQC 规则名中的“最近 1 天”表示当前选择的最新告警分区，不会取消安装指标的日期偏移。监控表的 `prev_2d` 安装字段已经选择相对 `alert_dt` 成熟的源 cohort；不能仅因 `alert_dt` 接近当前日期就判定 p3d 指标未成熟。

根指标按以下顺序复核：

1. 先解析 `alert_dt` 和 `analysis_dt`，日期关系未确定时不查询。
2. 对已注册监控表，完整读取 [注册监控根值 QuerySpec](queries/registered-monitor-root.yaml)，绑定 `business_date=alert_dt` 和路由档案的 `game_type`，按固定 `platform=ANDROID` 与 APK/沙盒行复现 DQC 当前值。正常加载本 Playbook 时不得预读该 QuerySpec，其他对象表不得路由到它。QuerySpec 固定返回全部已注册指标的当前率或命中信号及其监控分子、分母字段；每条规则仍只使用路由档案声明的 `monitor_field`、`monitor_numerator_field` 和 `monitor_denominator_field`，不得按非空值或相似名称改选字段。该表是告警口径的事实源。
3. 只有对账确有必要时，才在标准指标表的 `analysis_dt` 分区复算；先汇总分子、分母再相除，并按监控表的 4 位小数精度比较。
4. 根指标通过后，所有基线和归因查询都以 `analysis_dt` 为目标日；基线日期也相对 `analysis_dt` 选择。

注册监控根值 QuerySpec 必须恰好返回一行；空结果、重复行、平台或 `game_type` 不一致均停止根值复现。当前率必须用同一行中已登记的分子和分母重新计算并与物化率按 4 位小数对账；wave 和趋势字段只复现对应 DQC 规则，不能代替知识库标准指标的当前值、分子或分母。QuerySpec 的字段范围不构成新的指标定义，知识库门禁仍须先通过。

例如告警分区为 `dt=2026-08-19`，规则为 APK 下载完成到安装完成率，则：

```text
alert_dt    = 2026-08-19
analysis_dt = 2026-08-17
```

应在监控表 `2026-08-19` 分区复现告警值，在标准指标表和安装归因表使用 `2026-08-17`。禁止拿标准指标表 `2026-08-19` 的回刷中临时值与告警值比较后返回 `insufficient_data`。

上述日期偏移只适用于已注册的告警对象表和指标字段，不得根据其他表或字段名中的 `prev_2d` 自行类推。无法可靠确定 `alert_dt -> analysis_dt` 映射时返回 `insufficient_definition`；只有正确映射后的目标或基线分区、样本或成熟窗口不足时才返回 `insufficient_data`。

### 范围与日期

- 仅处理 TapTap Android APK（通常为 `app`）或沙盒（`sandbox`）下载/安装链路。
- 根范围包含知识库定义、目标业务日期、平台、APK/沙盒、告警对象表过滤及知识库 caveats；每个后续查询完整继承。
- 默认基线取目标日前 7 个完整、可比业务日。基线率始终为 `SUM(分子) / SUM(分母)`；样本门槛按当前日样本或基线日均样本判断。告警新增性判断还必须保留紧邻目标日的前一个完整、可比业务日的独立分子、分母和比率，不得用 7 日池化率冒充单日连续性证据。
- 不用目标日期之后、且位于知识库注册观察窗口之外的数据解释历史异常。为判定目标 cohort 终态、成熟度或窗口内恢复而读取的后续事件属于指标观察窗口的一部分，下载与安装均不视为事后解释；窗口结束后的业务事件、反馈或变更不得回填历史结论。
- 查询前用 `describe_table` 核实表和字段。诊断事件只定位阶段，不替代知识库正式分子、分母。

### 预检

继续前必须确认：

1. 知识库定义唯一，且指标方向明确。
2. 分区可解析为业务日期，目标和 7 个基线日数据齐全。
3. 当前与基线分子为非负有限数、分母为正的有限数，比率合法。
4. Android、APK/沙盒、下载/安装和观察窗口一致。
5. 当前值与告警方向一致，或差异可由趋势命中标志、cohort 日期、成熟窗口、四舍五入或已知合法口径差异解释。

### MaxCompute SQL 门禁

任何 MaxCompute 查询在首次执行前必须检查 SQL 形态。禁止使用 `CROSS JOIN`、逗号连接、`JOIN ... ON 1 = 1` 或其他笛卡尔积写法，包括把预期单行的总体汇总 CTE 连接回分桶结果。需要在分桶行携带总体分子、分母时，先完成分桶聚合，再在下一层 CTE 使用 `SUM(...) OVER ()`；聚合函数和窗口函数不得写在同一表达式层级。

不得为了减少查询次数临时把总体、多个维度家族、质量检查或贡献计算拼成一条复杂 SQL。优先一次只执行一个已经登记的维度家族，使每条查询都能独立验证分区、范围、粒度、分子分母和闭合；不同家族的结果不得横向连接或相加。若已存在 QuerySpec 或 SQL 模板，必须从模板组装，不得重新手写等价组合 SQL。只有模板不能覆盖当前已登记口径时才可做最小扩展，并在首次执行前重新检查本门禁。

### DView 结果基数门禁

DView 最多返回 1000 行，这个上限是完整性边界，不是可接受的采样方式。任何按业务键分桶的查询在提交前都必须有静态可证明低于 1000 行的结果预算；不得先执行原始高基数聚合，再使用已截断的前 1000 行做闭合、候选选择或结论。也不得使用 `LIMIT`、业务 Top、`OFFSET`/多页拼接或按结果顺序分批查询绕过，因为它们不能证明跨页快照、排序和长尾总量一致。

一级低基数模板、`game_id` QuerySpec、二级模板和安装开始后版本模板统一使用 SQL 侧收敛：先在完整根范围聚合全部原始桶并计算总体，再只保留同时通过既有样本和占比门槛的业务桶；默认质量值归并到有界质量桶，其余全部聚合为不可候选的 `__other_below_threshold__` 闭合残差。一级、二级和安装开始后版本模板返回完整源桶数 `source_bucket_count` 与当前结果桶吸收的 `collapsed_source_bucket_count`；已有 `game_id` QuerySpec 保持固定输出 schema，以其中已登记的残差收敛结构和 `max_rows` 验收。收敛后结果必须少于 250 行，因此即使 `device_brand`、`game_id`、客户端/OS 大版本、安装事件版本或二级 `device_model` 的原始桶超过 1000，也必须在 DView 返回前完成聚合。

250 行预算来自现有门槛的静态上界，不是经验值：当前日占比至少 1% 的互斥业务桶最多 100 个，基线占比至少 1% 最多再引入 100 个；一级再加至多 3 个质量桶和 1 个残差桶，最多 204 行；二级再加 1 个 `outside_parent`，最多 205 行。样本门槛只会继续减少这些桶。安装开始后版本模板只有 1 个版本缺失质量桶，结果上界更低。不得降低占比门槛或新增质量输出类别后继续沿用 250 行预算；任何门槛或分类变化必须重新证明上界并更新模板测试。

执行后逐项检查：实际结果行数小于 250；带基数审计字段的模板要求每行 `source_bucket_count` 相同且不小于结果行数、所有 `collapsed_source_bucket_count` 为正且合计等于 `source_bucket_count`；固定 `game_id` QuerySpec 则检查声明的 `max_rows` 和残差结构；所有结果分子分母回勾总体并通过贡献闭合。存在未单列的非质量业务源桶时必须有残差桶。返回恰好 1000 行、达到或超过 250 行、缺少当前模板规定的基数审计、合并计数不闭合，或业务源桶被淘汰但没有残差桶，都说明模板未正确执行或结果不完整，当前家族失败并继续固定队列。错误码或其他没有已登记安全收敛模板的高基数增强查询不得临时发明 Top；无法给出同等闭合预算时跳过对应增强模块并记录限制。

### 数据完整性

根指标查询必须在同一次聚合中完成以下检查，不因这些检查新增维度查询：

1. 使用完整根键后，目标与基线在知识库声明的正式粒度上唯一；不得用 `MAX`、`DISTINCT` 或任意保留一行掩盖未解释的重复。
2. 对知识库定义为“分子是分母子集”的实体率，当前和基线均必须满足 `0 <= numerator <= denominator` 且比率在 `[0, 1]`。失败 PV 率等非子集计数比只按知识库定义检查，不得套用实体率上限。
3. 目标与基线使用相同的字段定义、过滤、风险样本规则和观察窗口；分区迟到、全零、NULL 或样本突变不得解释为业务变化。

根粒度或分子/分母语义无法可靠确定时使用 `insufficient_definition`；已确定口径但正确分区、覆盖或成熟样本不足时使用 `insufficient_data`。不新增数据质量状态。

告警新增性门禁允许继续后，每个实际执行的维度家族还必须确认：

- 归因数据在下载或安装正式逻辑粒度上唯一；只有存在已定义的去重键和确定性保留规则时才可去重。
- 目标与基线的字段语义、分桶边界和标签生成时点一致，历史缓存或当前快照不得回填历史。
- 维度数据覆盖完整根范围，分子、分母和行数能回勾根调查，该家族能闭合到大盘变化。
- 安装家族的官方分子分母投影只以 `is_metric_anchor=1`、`official_download_complete` 和 `official_install_complete` 检查唯一性与闭合。APK 的 `chain_id` 非空率、唯一性和跨设备/游戏冲突率，以及沙盒 `install_round_id` 的覆盖与歧义，只门禁依赖链路键的阶段或增强诊断；链路键与阶段质量不得作为官方投影的前置门禁。

低基数一级模板返回的维度匹配率、`unmatched` 桶和安装观察窗口是随业务结果输出的风险证据，不新增通过率阈值，也不构成拒绝当前结果或停止后续维度的硬门禁。匹配失败样本必须留在完整根范围内，不能过滤或回填旧标签；`unmatched` 只作为不可候选质量桶，其占比或变化写入 `evidence_limits`。安装观察窗口偏离知识库期望时保留已经查询到的分桶结果并说明成熟度风险，继续固定队列；不得仅凭这些辅助字段返回 `unsupported_drilldown`。

家族门禁不通过时不得从该家族产生候选，并把家族名和真实限制写入 `evidence_limits`。只要根范围的正式分子分母仍合法，单个家族的缺字段、空桶、重复、覆盖不足、回勾失败或贡献不闭合都不能证明整个归因数据源不可用；必须继续下一个已登记维度家族。只有完整根范围本身无法从归因数据源复现，或按对应链路的固定顺序尝试后没有任何已登记家族可执行时，才按真实原因返回 `insufficient_data`、`query_blocked`、`query_failed` 或 `unsupported_drilldown`。这些是一级及后续归因的家族门禁，不是判断既有异常延续的前置查询。

### 告警新增性

根指标预检通过后、执行任何一级归因前，必须复用当前值和 7 日池化基线判断本次告警是否包含实质性新增恶化。先按知识库方向计算：

```text
root_adverse_delta =
  越高越好: baseline_rate - current_rate
  越低越好: current_rate - baseline_rate
```

对于绝对阈值规则，只有同时满足以下条件时才判为既有高位或既有异常延续，不执行一级、反事实、二级和方向增强查询。同一调查合并多条已命中的绝对阈值规则时，每条规则都必须通过以下延续性判定；任一规则是当日新跨过的阈值时继续归因：

- 当前值命中该规则的绝对阈值；
- 紧邻目标日的前一个完整、可比业务日也命中同一规则阈值；
- 7 日池化基线率也在同一告警侧；
- `root_adverse_delta < 0.0005`。

其中：

- `root_adverse_delta >= 0` 时，说明仍在告警侧但相对基线没有达到 5bp 的实质性新增恶化。
- `root_adverse_delta < 0` 时，说明仍在告警侧但相对基线正在恢复。

此时返回 `no_dominant_slice`，省略 `top_findings` 和 `counterfactual`；`summary` 必须明确告警值已经复现、属于既有异常延续且未发现实质性新增恶化，`recommended_action` 指向继续跟踪既有问题及复核告警阈值或恢复条件。该判断只证明指标告警状态延续，不证明历史机制或根因保持不变。不得为给出新方向而继续下钻，也不得把当日分子占比较高的稳定存量写成新增根因。

上述任一条件不满足时继续执行归因。同一调查合并了相对基线或趋势规则时，只要其中任一规则按其合法窗口复核出实质性新增恶化，就不得仅凭绝对阈值的延续性提前停止。

## 下载链路

### 数据与粒度

归因宽表：

```text
tap_dw.ads_report_store_platform_device_game_download_chain_attribution_1d
```

正式粒度：

```text
dt + platform + game_type + device_id + game_id
```

`dt` 是下载业务日。首次下载上下文只取同一业务日、同一设备、游戏和游戏类型内的首条下载事件。非互斥过程标记不得相加成总样本。

先用路由后经知识库确认的标准指标严格选择一组专用查询资产：

| 标准指标 | `game_id` QuerySpec | 低基数维度模板 |
|---|---|---|
| `下载完成率` | [下载完成率游戏归因 QuerySpec](queries/download-game-attribution.yaml) | [下载完成率一级归因模板](queries/download-primary-attribution-template.md) |
| `下载失败率` | [下载失败率游戏归因 QuerySpec](queries/download-failed-rate-game-attribution.yaml) | [下载失败率一级归因模板](queries/download-failed-rate-primary-attribution-template.md) |
| `下载失败次数比率` | [下载失败次数比率游戏归因 QuerySpec](queries/download-failed-pv-rate-game-attribution.yaml) | [下载失败次数比率一级归因模板](queries/download-failed-pv-rate-primary-attribution-template.md) |
| `下载人为停止率` | [下载人为停止率游戏归因 QuerySpec](queries/download-stop-rate-game-attribution.yaml) | [下载人为停止率一级归因模板](queries/download-stop-rate-primary-attribution-template.md) |

执行下载 `game_id` 一级归因时只完整读取当前标准指标绑定的 QuerySpec，绑定 `business_date=analysis_dt` 和当前 `game_type`，不得读取其他指标的 QuerySpec、重新手写或改组 SQL。执行 `is_reserve_auto_download` 或后续低基数家族前，同样只读取当前指标绑定的模板，并按模板指向的 [一级维度登记](queries/primary-attribution-dimensions.md) 选择一个 Playbook 已登记的维度家族；每次只能替换一个维度表达式。标准指标不在上表时停止，不得改绑最相似指标。每组专用资产固定正式宽表、日期、平台、APK/沙盒范围、分子分母和“分桶聚合后再用窗口总量”的 CTE 结构，但不替代知识库指标定义、数据完整性门禁、候选门槛或贡献计算。

### 一级顺序

先并行快判两个独立维度家族：

```text
game_id
is_reserve_auto_download
```

判断异常是单个/少数游戏主导、多个游戏共同变化，还是预约自动下载的结构或组内表现变化。两个家族可能投影同一批样本，即使都显著也不得相加。

两个规定家族的合法结果必须分别保留到候选选择结束，不能因为 `game_id` 已经达到反事实主导条件而省略或丢弃 `is_reserve_auto_download` 家族。两者都形成合法候选且达到后文跨维度候选重叠门槛时，必须执行一次完整四象限验证；该验证只校准共享范围，不创建交叉候选，也不改变两个原始贡献。

完成两个快判家族后，无论是否已经形成游戏候选，都继续执行后续低基数一级。游戏候选仍保持最高业务优先级，其他家族用于补充风险范围和交叉校准，不能覆盖或降级已经合法的游戏结论。随后检查：

```text
device_brand
channel_group
app_major_version
os_major_version
apk_size_tier
```

实际执行固定按 `device_brand -> channel_group -> app_major_version -> os_major_version -> apk_size_tier` 的稳定顺序，逐个尝试完上述五个家族；单个家族查询、完整性或闭合失败只淘汰该家族并继续下一个，不能提前返回 `unsupported_drilldown`。候选最终按单项全局不利影响排序，同等证据下优先呈现游戏候选。`network_type_group`、`device_model`、存储和细地域默认只做二级，禁止首轮无方向横扫。

### 终态路由

字段存在且成本合理时，比较头部游戏或大盘的 `download_terminal_state` 分布。使用前必须根据知识库或已登记的归因表定义，确认当前与基线 cohort 都已经完成各自观察窗口，且终态互斥、完备并回勾下载分母。

若存在 `same_day_completed`、`cross_day_completed` 和 `immature_censored`，前两者都属于成功，`immature_censored` 单列且不参与原因贡献。只有完整观察窗口结束后仍无其他已定义终态的样本才可进入 `unobserved_residual`。根指标仍严格使用知识库观察窗口，不得为拆终态自行选择 P99 或 T+N 重新定义完成率。成熟度、互斥性或回勾不通过时，跳过终态路由并写入 `evidence_limits`，不覆盖已合法形成的一级定位。

终态只选择后续方向：

| 主要变化 | 优先方向 |
|---|---|
| `explicit_failed` 上升 | 错误码、URL/host/CDN、客户端版本 |
| `system_interrupt` 上升（若可观测） | 客户端版本、OS、品牌、进程管理 |
| `continued_unfinished` 上升（若可观测） | 包体、网络、断点续传、任务调度 |
| `human_stop` 上升 | 包体、设备、等待体验；若细分状态不可用则按 mixed 处理 |
| `unobserved_residual` 上升 | 埋点覆盖、链路匹配、客户端进程或数据链路 |
| 末态稳定 | 优先结构/组内表现分解 |
| 多项小幅变化 | mixed，按标准非游戏顺序继续 |

不得把路由方向写成已确认机制。

### 合法二级关系

下列列表只登记允许的父子关系，不是另一套执行顺序；一级固定队列仍以上文顺序为准。需要从 `game_id` 选择二级子维度时，列表按一级业务优先级排列。

```text
game_id -> device_brand, channel_group, app_major_version,
           os_major_version, apk_size_tier
apk_size_tier -> game_id, channel_group, device_brand, os_major_version
channel_group -> game_id, app_major_version, device_brand
app_major_version -> device_brand, os_major_version
os_major_version -> device_brand, app_major_version
device_brand -> device_model, os_major_version, app_major_version,
                network_type_group
is_reserve_auto_download -> game_id, channel_group, apk_size_tier
```

## 安装链路

### 数据、粒度与 cohort

归因宽表：

```text
tap_dw.ads_report_store_platform_device_game_install_chain_attribution_1d
```

逻辑粒度：

```text
APK:  dt + platform + game_type + device_id + game_id + chain_id
沙盒: dt + platform + game_type + device_id + game_id + install_round_id
```

物理分区 `dt` 表示下载完成 cohort 日，不是安装结果发生日。APK 与沙盒观察窗口可能不同，严格使用知识库定义。APK 只用可靠 `chain_id` 关联；缺失链路进入质量桶，禁止用同日或邻近事件兜底。沙盒多轮歧义进入质量桶。

`has_client_install_trigger`、`has_client_install_start`、安装完成和安装失败必须区分；客户端触发或开始均不能表述为系统安装器已成功拉起。可用存储字段必须同时检查覆盖率。`install_event_app_major_version` 是安装事件侧版本，只覆盖已经匹配到安装事件的样本；它不得直接拆分从下载完成开始的官方完整分母，也不得把没有安装事件版本的样本合并成普通“未知版本”后归因。

### 一级顺序

第一优先级固定完整读取 [安装游戏归因 QuerySpec](queries/install-game-attribution.yaml) 查：

```text
game_id
```

无论 `game_id` 是否形成候选，游戏家族完成后第二步都必须执行后文独立的 `D/S/C` 安装阶段拆解。阶段拆解不得移动到游戏归因之前，也不得因为游戏候选显著而省略；它只回答损耗主要发生在安装开始前还是开始后，不覆盖游戏优先结论。

阶段拆解之后，无论 `game_id` 是否已经形成候选，都完整读取 [安装低基数一级归因模板](queries/install-primary-attribution-template.md)，再检查官方完整分母可用的维度。游戏候选仍保持最高业务优先级，其他维度用于补充风险范围和交叉校准：

```text
device_brand
storage_headroom_tier
os_major_version
apk_size_tier
```

固定按 `device_brand -> storage_headroom_tier -> os_major_version -> apk_size_tier` 的顺序逐个尝试完上述四个家族。单个家族缺字段、查询失败、完整性不足或贡献不闭合时，记录该家族限制并继续下一个；只要官方根投影合法，就不得把单个家族失败升级为整个安装下钻不支持。阶段拆解显示 `S -> C` 同向不利变化达到 5bp 时，另按后文规则检查安装事件版本；不得把该版本检查插到 `game_id` 之前，也不得让它替代上述官方投影家族。

人工解释顺序为 APK/沙盒、游戏贡献、游戏包版本/包大小、是否进入 `installStart`、完成/失败及失败原因、安装器类型/客户端版本、机型/OS/剩余存储。下载专属的预约自动下载、首次下载网络和地域不进入安装一级归因。

### 安装阶段路由

告警新增性允许继续且调查属于安装链路时，先完成 `game_id` 一级归因，再完整读取独立的 [安装阶段损耗拆解 QuerySpec](queries/install-stage-loss-decomposition.yaml)，绑定 `business_date=analysis_dt` 和当前 `game_type`。正常加载本 Playbook、告警新增性停止或游戏家族完成前不得预读、执行该 QuerySpec。它是游戏之后的固定第二步，不计入方向增强查询上限；不得另写事件明细查询替代。

阶段 QuerySpec 只使用 `is_metric_anchor=1` 的官方锚点行。下载完成分母固定为 `D = official_download_complete`，已观测安装开始固定为分母实体中 `has_client_install_start=1` 的 `S`，正式安装完成固定为 `C = official_install_complete`；诊断字段不得替代正式分子。它返回目标日和此前 7 个完整业务日的池化基线计数、损耗率、观察窗口与质量计数，不按游戏或其他维度分桶。

该阶段只适用于 `game_type=app`。沙盒没有适用的客户端 `installStart` 语义，必须把阶段步骤记为 `skipped_not_applicable`，不得执行本 QuerySpec，也不得因此停止后续官方维度队列。

APK 阶段结果只在以下门禁全部通过时有效：`baseline_day_count=7`；当前与基线官方下载分母 `D` 均为正；官方锚点无重复；正式分子、分母和下载分母内的开始标记均非空且为二值；满足 `C <= D`、`S <= D`；当前与基线观察窗口均严格为 3 天。`S=0` 不是质量失败：该侧仍可报告开始前未完成损耗，但开始后安装完成率保持未定义且不得填零。`C` 中未观测到 `S`、下载分母外观测到 `S`、或 `S` 与诊断事件匹配标记不一致，只作为覆盖/时序风险单列，不使合法集合拆解整体失效。任何真正的门禁失败都省略阶段率，在 `evidence_limits` 精确记录对应质量事实；不得改写为整个安装归因不支持，也不得否定已经合法的游戏或其他官方投影家族。

APK 链路键缺失与跨实体冲突、沙盒轮次缺失与歧义，以及 `T/S/C/E` 覆盖和子集关系只约束阶段或增强诊断，不否定已经闭合的官方 `game_id` 或低基数家族分子分母投影。任一阶段门禁失败时省略对应阶段计算、继续合法的维度贡献分解，并在 `evidence_limits` 记录具体限制；不能改用未登记事件源，也不能输出“官方安装锚点、链路键覆盖与 game_id 必须同时完成”的合并门禁。

先按知识库正式口径确定下载完成分母 `D` 和最终安装完成 `C`，再使用已确认字段聚合安装开始 `S`。阶段损耗使用集合交集，不要求所有官方完成样本都观测到开始事件：

```text
C_started = D ∩ S ∩ C
开始前未完成 = D ∩ non-S ∩ non-C
开始后未完成 = D ∩ S ∩ non-C
开始前未完成率 = count(D ∩ non-S ∩ non-C) / count(D)
开始后未完成占下载分母比例 = count(D ∩ S ∩ non-C) / count(D)
开始后安装完成率 = count(C_started) / count(S)
```

两个未完成集合必须闭合到官方损耗 `D - C`。`C ∩ non-S` 单列为“官方完成但未观测到 installStart”的覆盖风险，不混入任一损耗桶，也不进入 `C_started / S`。用户可见文案必须称开始前桶为“未观测到 installStart 且最终未完成”的样本，不能直接称“没有进入安装”或“用户没有开始安装”，因为事件未上报与真实未发生无法仅凭该字段区分。任一下游状态在缺少上游状态时出现，必须单列为时序/覆盖质量问题，不得为了得到正数损耗而强行设为零或重排事件。

若需要继续拆安装触发 `T` 或失败信号 `E`，只能在阶段结果已经合法且后文方向增强条件触发时使用已登记事件语义；不得把它们加入固定阶段 QuerySpec 的有效性前置门禁。`E` 只是曾出现失败信号的样本，不默认为最终失败。

分别对当前和 7 日池化基线先汇总计数再计算阶段率。某段的同向不利变化达到 5bp 时，用它选择后续方向：损耗主要增加在 `D -> S` 时优先检查触发链路、APK 包体、OS/品牌及事件覆盖；主要增加在 `S -> C` 时优先检查安装事件版本、安装器、OS/品牌、存储、失败信号与回调覆盖。阶段路由只描述损耗位置，不产生 `top_findings` 候选，不确认技术原因。

只有阶段门禁有效且 `S -> C` 同向不利变化达到 5bp 时，才完整读取 [安装开始后版本诊断模板](queries/install-post-start-version-template.md)。该模板只在 `official_download_complete=1 AND has_client_install_start=1 AND diagnostic_event_matched=1` 的已观测开始人群中，以 `S` 为分母、`C` 为分子拆 `install_event_app_major_version`；结果用于校准后续方向，不参与官方 `C / D` 的一级贡献排序，也不产生 `top_findings`。版本缺失、覆盖跨期不稳定或无法回勾 `S/C` 时跳过该诊断并继续其他已登记家族，不得建立“未知版本导致安装低下”的结论。

字段不存在、覆盖跨期不稳定或子集关系无法对齐时，省略对应阶段计算、继续合法的维度定位，并在 `evidence_limits` 说明安装阶段不可靠；不得因阶段路由不可用而扫描未登记事件表。独立阶段路由是游戏之后的规定诊断查询，但不是候选归因家族；其失败不得把已合法形成的定位改写为 `query_failed` 或 `unsupported_drilldown`。

### 合法二级关系

下列列表只登记允许的父子关系，不是另一套执行顺序；一级固定队列仍以上文顺序为准。需要从 `game_id` 选择二级子维度时，列表按一级业务优先级排列。

```text
game_id -> device_brand, storage_headroom_tier, os_major_version,
           apk_size_tier
apk_size_tier -> game_id, device_brand, storage_headroom_tier
os_major_version -> device_brand, storage_headroom_tier
device_brand -> device_model, os_major_version, storage_headroom_tier
storage_headroom_tier -> apk_size_tier, device_brand, os_major_version
```

## 归因执行清单

注册告警通过根指标预检后，每个调查都必须在 `attribution_execution` 中留下可机读的执行清单。该字段用于 writer 和测评器验证真实覆盖，不是用户可见结论，不能只在 `summary` 或 `evidence_limits` 中用自然语言代替。

告警新增性判为既有异常延续时使用：

```json
{
  "mode": "existing_anomaly_stop",
  "chain": "download",
  "game_type": "app",
  "reason": "前一日和七日基线均在同一告警侧，且没有达到 5bp 的新增恶化",
  "steps": []
}
```

进入归因时使用 `mode=full_queue`，并严格按以下清单执行和记录：

`attribution_execution.execution_mode` 必须保留 runner 冻结的 `trusted_host_adapter` 或 `self_reported_development`；生产调查只允许前者，不得省略或改写以隐藏执行信任等级。

```text
下载 app/sandbox:
game_id -> is_reserve_auto_download -> device_brand -> channel_group
-> app_major_version -> os_major_version -> apk_size_tier

APK 安装:
game_id -> install_stage -> device_brand -> storage_headroom_tier
-> os_major_version -> apk_size_tier

沙盒安装:
game_id -> install_stage(skipped_not_applicable) -> device_brand
-> storage_headroom_tier -> os_major_version -> apk_size_tier
```

每个 `steps[]` 项固定包含 `step` 和 `status`：

- 候选家族成功时使用 `status=succeeded`，并写非负整数 `candidate_count`；没有候选也写 `candidate_count=0`。
- 查询、字段、完整性或闭合失败时使用 `status=failed` 和非空 `reason`，随后继续下一步。
- 只有沙盒的 `install_stage` 可以使用 `status=skipped_not_applicable`，并写非空 `reason`。
- runner 固定队列的每次查询必须写当次执行唯一的非空 `query_id`；不得伪造、跨步骤或 attempt 复用。质量风险写为由 runner 生成、去重后的非空 `warning_codes` 数组。

实际执行二级归因时，`attribution_execution` 另写最多一个 `secondary_steps[]` 项：

```json
{
  "parent_dimension": "game_id",
  "parent_value": "12345",
  "step": "device_brand",
  "status": "succeeded",
  "candidate_count": 1
}
```

父切片身份使用非空 `parent_value` 或 `parent_label`，父维度必须是一级 `steps` 中候选数为正的家族，`step` 必须命中当前链路登记的父子关系。成功时写非负 `candidate_count`；失败时写非空 `reason`。真实 query ID 和质量风险沿用一级步骤字段规则。未执行二级时省略 `secondary_steps` 或写空数组。

`completed` 必须使用完整队列，且一级与二级 `candidate_count` 合计为正；完整队列的 `no_dominant_slice` 要求至少一个一级家族成功且所有一级、二级候选数均为零。每个 `top_findings[]` 必须写 `attribution_level=primary|secondary` 并回勾候选数为正的对应步骤；二级 finding 还要回勾相同父维度和父切片身份，写出数量不得超过对应 `candidate_count`。`no_dominant_slice` 的另一合法形态是 `existing_anomaly_stop`。单个步骤失败不能缩短数组。`insufficient_data`、`query_blocked` 和 `query_failed` 若发生在根指标或归因根范围预检，使用 `mode=root_precheck_failed`、非空 `reason` 和空 `steps`；若发生在归因家族，则必须使用完整队列且所有候选家族均为 `failed`。`unsupported_drilldown` 只允许两种执行证据：完整根范围无法从归因源复现时使用 `mode=root_scope_failed`、非空 `reason` 和空 `steps`；或使用完整队列且所有候选家族均为 `failed`。writer 将拒绝缺少步骤、顺序不符、非法跳过、候选与结论不闭合或 finding 无对应执行证据的新结果。

## 贡献分解

计算前先按分母存在性标记每个分组：

```text
common   = 当前和基线均有分母
entrant  = 当前有分母、基线无分母
exit     = 当前无分母、基线有分母
```

只有 `common` 可以解释组内表现变化。`entrant` 和 `exit` 使用存在侧 rate carry-across 仅为保持数学闭合，其 `performance_impact = 0`，只允许表述新增/退出流量的结构影响，不得声称该切片自身完成率恶化。如果单边缺失来自分区、身份键、分桶边界或标签口径变化，必须归入质量问题，不得标记为 `entrant/exit` 业务生命周期。

对一个完整维度家族的每个桶定义：

```text
s1 = current_denominator / overall_current_denominator
s0 = baseline_denominator / overall_baseline_denominator
r1 = current_numerator / current_denominator
r0 = baseline_numerator / baseline_denominator
Rmid = (overall_current_rate + overall_baseline_rate) / 2

composition_impact = (s1 - s0) * ((r1 + r0) / 2 - Rmid)
performance_impact = (r1 - r0) * (s1 + s0) / 2
total_impact = composition_impact + performance_impact
```

对于知识库定义为越高越好的完成率：

```text
adverse_impact = max(-total_impact, 0)
```

其他方向按知识库定义转换；方向不明确立即停止。若一个桶只在一侧存在，使用存在侧的 rate carry-across 到缺失侧以保持闭合，不把缺失侧率填零。每个维度家族必须满足：

```text
abs(sum(total_impact) - (overall_current_rate - overall_baseline_rate))
  <= 0.000001
```

闭合不通过时不得从该家族产生正式候选。不同维度家族是同一总体的不同投影，严禁相加或声称跨家族 Top 3 合计解释多少。

## 候选门槛与质量

候选同时满足：

- 当前日样本或基线日均样本不少于 100；
- 当前或基线父范围占比不少于 1%；
- 对大盘不利影响不少于 `0.0005`，即 5bp；
- 非质量桶；
- 维度家族闭合，候选来自非质量业务桶。

候选按单项全局不利影响排序。非游戏一级最多保留 Top 3。没有候选达到 5bp 时返回 `no_dominant_slice`，不能选择最大但不达标的桶。

下载 `game_id` QuerySpec 同时返回完整根比较窗口的 `baseline_day_count` 和每个游戏实际存在有效指标样本的 `bucket_baseline_active_day_count`。根基线完整性、样本门槛、池化率和贡献计算继续使用固定 7 日比较窗口，不因单个游戏晚进入而改换基线；桶级活跃天数只用于解释稳定性，且只可解释 `bucket_kind=game` 的未合并游戏桶。对每个写入 `top_findings` 的游戏候选，包括多游戏背景名单中的第二、第三款，都必须独立检查桶级活跃天数：少于 7 日时，`finding` 必须明确当前率实际与多少个有效样本基线日比较；少于 3 日时还必须在 `evidence_limits` 说明该游戏的组内表现基线较短，不能写成稳定七日游戏基线。缺失日不得填零，也不得把生命周期内尚无流量的日期改写成数据缺失。

默认质量值包括：

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

质量桶不能成为业务候选；其占比突然扩大可以作为数据质量发现。维度匹配率和安装观察窗口没有新增的硬阈值，风险只限制措辞强度，不阻止其他已闭合业务桶按现有门槛形成候选，也不阻止继续下一个维度。高基数长尾必须在 SQL 内使用闭合残差桶收敛到 DView 结果预算以内，并保持其不可候选属性；不得先接收 1000 行截断结果再在 Host 侧合并。

## 剔除反事实

当头部游戏满足任一条件时最多执行一次：

- 占该维度家族不利变化至少 50%；
- 单项不利影响不小于大盘净不利变化。

同时从当前和基线分子、分母中剔除该切片：

```text
current_without = (overall_current_numerator - slice_current_numerator)
                  / (overall_current_denominator - slice_current_denominator)
baseline_without = (overall_baseline_numerator - slice_baseline_numerator)
                   / (overall_baseline_denominator - slice_baseline_denominator)
removal_delta = current_without - baseline_without
restoration_ratio = 1 - abs(removal_delta) / abs(original_delta)
```

剔除后异常绝对值缩小至少 50%、恢复到 5bp 容差内或方向反转，说明该游戏是主要影响范围，后文游戏背景选择固定只查询该主导游戏，并可按二级门禁继续一次二级。若切片就是全部范围或剔除后分母非正，停止并说明无法计算。反事实只证明算术解释力，不证明根因。头部游戏未通过该主导条件时，不阻止后文按多游戏累计覆盖门槛选择背景候选。

## 二级下钻

仅当一级候选达到门槛、有足够解释力或明确业务价值、关系已注册、数据覆盖可靠且本次尚未做过二级时执行。只选一个父候选和一个合法子维度。

命中条件后完整读取 [二级归因 SQL 模板](queries/secondary-attribution-template.md)，并按模板指向的 [归因维度登记](queries/primary-attribution-dimensions.md) 绑定一个父维度、父值和一个合法子维度。下载链路还必须按当前标准指标选择模板登记的唯一分子、分母和行级合法性表达式；安装链路固定使用官方锚点投影。不得临时手写另一套父子聚合、只查父范围内部，或省略 `outside_parent` 后继续归因。

查询必须继承：

```text
标准指标定义
目标和基线日期
平台
APK/沙盒
下载/安装阶段
全部根过滤
一级父维度名和值
```

二级查询必须在同一聚合中保留根范围总分子、总分母。一级父维度名和值只限定需要展开的子维度明细，不得把父范围分母重新称为大盘分母。二级完整家族由“父范围内的子维度桶 + 父范围外的 `outside_parent` 闭合残差桶”组成；`outside_parent` 只用于闭合，不能成为候选。

二级沿用根范围尺度上的池化、分解、质量桶、样本、占比、5bp 和闭合门禁。子桶的 `adverse_impact_bp` 必须按根指标分母计算并达到全局 5bp，不能用父范围内的局部 5bp 替代。结束后立即停止维度层级扩展；禁止三级、多个父节点或临时组合。只有命中后文的明确条件时，才可进入不增加维度层级的方向增强验证。

## 游戏背景

先只使用已经通过样本、占比、5bp、质量和闭合门禁的 `game_id` 候选冻结游戏背景名单，不得为了触发背景查询降低候选门槛。按以下互斥规则选择：

1. 若头部游戏通过剔除反事实的主导条件，名单只包含该主导游戏，不再为其余游戏补足数量。
2. 若没有单一主导游戏且 `root_adverse_delta > 0`，将合法游戏候选按 `adverse_impact` 从高到低排序，选择累计不利影响首次达到 `abs(root_adverse_delta) * 0.50` 的最小前缀，最多 3 款。前 3 款仍未达到该门槛时不执行游戏背景查询；达到后立即停止选择，不得为了凑满 3 款继续查询较弱候选。

游戏桶属于同一已闭合维度家族且互斥，因此累计不利影响只可用于本节的有限查询选择；不得在输出中将该累计值写成原因解释率、因果份额，或与其他维度家族贡献相加。名单非空时才完整读取 [游戏运营事件 QuerySpec](queries/game-operation-events.yaml)，并对名单中的每个 `game_id` 分别绑定、分别尝试执行一次；不得把多个游戏改写成临时 `IN` 查询。正常加载本 Playbook 时不得预读该 QuerySpec，也不得在触发前扫描其中登记的数据源。背景查询属于合法归因完成后的校准，不是决定调查状态的规定归因路径，也不计入方向增强模块上限。

任一游戏返回合法空结果、目标背景分区缺失、权限阻塞、按快速排查规则修正两次后仍失败、结果量超过 `max_rows` 或其他背景质量门禁失败，都只省略该游戏的背景证据并在 `evidence_limits` 记录游戏身份及真实的 `insufficient_data`、`query_blocked`、`query_failed` 或质量限制；不得阻止名单中其他游戏继续查询，不得把已经合法形成的调查状态改写为这些受阻状态，不得删除已有 `top_findings`、反事实或重叠验证结果，也不得把失败或空结果写成零或“没有发生业务变化”。只有根指标或规定归因路径自身受阻时，才按对应受阻状态结束调查。

QuerySpec 每次只绑定名单中的一个 `game_id`，并将本调查已经确定的 `analysis_dt` 绑定为 QuerySpec 的 `business_date`；查询结果仍以 `analysis_date` 返回该日期。运营事件表的 `dt` 使用最新快照分区，事件业务时间使用 `event_date0/event_date1`，事件区间必须与目标日前 7 日基线到目标日组成的比较窗口相交；返回的 `source_snapshot_dt` 用于审计本次采用的修订快照，不得当作事件日期。游戏详情表读取同一比较窗口及其前一日守卫分区；守卫分区只用于给窗口首日提供状态前态，不得作为事件输出。登记生命周期事实以登记日期及该日状态识别，实际状态切换则以窗口内相邻两个自然日快照中观察到的关闭到开启识别，二者不得混写。前态分区缺失、日期不连续或状态为空时不得把未知当成关闭，也不得输出 `observed_state_transition`；登记日期仍可作为较弱事实时保留，否则省略对应背景并说明证据限制。不得把预先登记但尚未到达、对应状态也未开启的未来日期当作已发生事件。不得用无分区约束的 `MAX(dt)` 查询游戏详情表，也不得把运营事件表的快照分区改写为 `analysis_dt`。执行前仍须通过 `describe_table` 核实字段与类型，除有明确 SQL 报错并按快速排查手册修正外，不得改写 QuerySpec 的数据源、过滤、事件类型、状态变化或修订去重语义。

查询保留该游戏与完整比较窗口相交的全部合法运营和生命周期事件。`transition_evidence=observed_state_transition` 表示在有界且连续的日快照中实际观察到关闭到开启，其 `event_date0/event_date1` 必须使用状态发生切换的分区日，即使它与登记的首次日期不同；这包括老游戏在窗口内重新开放下载、预约或可玩状态。`registered_lifecycle_date_only` 表示登记日期与该日开启状态一致，但未在该日直接观察到关闭到开启，其 `event_date0/event_date1` 使用登记生命周期日期，证据更弱。两类事实在同一日期重合时只保留 `observed_state_transition`，不得重复输出。可玩状态只在下载或试玩任一明确开启时为开启、两者均明确关闭时为关闭，其余为未知；若同一日的可玩切换完全由已经输出的下载开关切换派生，只保留 `download_open`，不得再把 `playable_open` 当作第二条独立证据。`reserve_auto_download_enabled` 只表示对应事件日登记的游戏配置，不能单独证明存在实际预约自动下载样本，也不能伪造成带启用日期的事件。实际预约自动下载流量只能来自已完成并通过门禁的 `is_reserve_auto_download` 归因家族。

只去除同一运营事件的重复修订，不得增加日报展示使用的每游戏 `game_rank = 1`、跨游戏 Top、下载量排序或 `LIMIT`。QuerySpec 的 `max_rows` 只用于识别异常结果量，超过时停止使用这批背景结果，不得截断后继续。合法空结果只表示在登记来源与比较窗口内未找到事件，不得写成没有发生业务变化；将这一证据边界写入 `evidence_limits`。

QuerySpec 登记以下运营事件和生命周期数据源：

```text
tap_bi.dwd_app_operation_events_df
tap_dw.dwt_game_detail_info_view_df
```

仅在已取得的事件或候选方向需要补充当前 APK、版本码、包体和状态时，才继续按需查询：

```text
tap_dmp.ods_server_sync_apks
```

解释下载指标时，背景证据按“近期开放下载或首次可玩 -> 已由归因家族证实的预约自动下载样本 -> APK、版本和包体 -> 下载终态 -> 更新 -> 事故或公告”的相关性顺序校准现有候选；安装指标按安装阶段与事件机制重新判断，不得照搬下载顺序。开放预约本身不等于预约自动下载，包体预热也不等于用户侧提前下载。事故或公告的标题若只描述游戏运行、卡顿或黑屏而未登记下载机制，只能作为较弱背景，不能盖过已验证的下载生命周期事实。

运营事件可补充开放下载、开放预约、首次可玩、事故、更新及其事件有效期和登记版本；QuerySpec 已随生命周期事件携带事件日可用的游戏状态、下载/预约开关、预约自动下载配置、包体预热、APK ID、版本和包体。只有需要 APK 创建时间、状态或更完整修订历史时才继续查询 APK 辅助表。使用这些辅助来源前同样执行后文的辅助维度门禁；关联或时序不合法的记录不能成为背景证据。背景事件只作为时间线索，不改变贡献计算，不升级为因果结论。

取得合法背景事实时，应优先校准对应游戏的现有 `top_findings[].finding`，并按需使用 `summary` 和 `recommended_action` 表达整体排查范围；不得把一款游戏的背景套用到其他候选，也不得把正向背景事实本身写进 `evidence_limits`。只有空结果、状态变化未被直接观察、快照可能随修订变化、切片基线活跃日不足、关联歧义或因果限制等真实边界才写入 `evidence_limits`。

## 方向增强验证

只有告警新增性门禁允许继续，现有一级已经形成合法候选，且按条件触发的剔除反事实与最多一次二级已经执行或合法跳过后，才考虑方向增强验证。先冻结已有定位结论、候选身份、贡献数值和已经实际取得的反事实结果；增强验证只判断该方向是否重复、是否具有实体特异性或是否值得优先探索，不得创造未通过既有门槛的新候选，也不得改写已经闭合的贡献。方向增强属于合法定位完成后的可选校准，不是根指标或规定归因查询。

没有命中下列触发条件时立即停止，不执行验证查询，不需要逐项记录“未验证”。所有触发信号必须来自已经完成的根指标、终态路由、安装阶段路由、一级或二级聚合、剔除反事实、游戏背景，或已按优先级执行的前序增强模块；不得先扫描当前模块自身的数据源来判断其是否触发。

每个调查最多执行两个需要新增查询的增强模块；每个模块只设计一条聚合查询同时覆盖所需日期，因明确 SQL 错误按规则修正重试不视为新模块。多个需要新增查询的模块同时触发时，按“安装严格漏斗 -> 错误码与恢复 -> 跨维度候选重叠 -> 同日新旧版本准实验 -> 同类负对照”的顺序选择前两个。非安装调查跳过安装严格漏斗；没有已完成查询提供的合法失败信号时跳过错误码与恢复。若安装严格漏斗与错误码使用同一已登记事件源，且一条聚合查询能同时满足两者全部门禁，则合并为一个链路专项模块、只占一个名额；不得为节省名额而省略任一模块的字段、守恒或字典门禁。因上限跳过已触发模块时写入 `evidence_limits`。不得借增强验证新增三级、多个二级父节点或无方向组合扫描。

### 安装严格漏斗与恢复

只有安装调查已形成合法一级候选，且满足以下任一信号时才执行一次：

- 安装阶段路由中 `D -> S` 或 `S -> C` 的同向不利变化达到 5bp；
- 阶段路由或已登记归因宽表中的失败信号、`start_only` 或已定义未收口终态占比相对基线上升至少 5bp；
- 已完成的开始后版本诊断显示某个合法版本桶的 `S -> C` 损耗相对基线上升至少 5bp。

只有上述信号实际触发后，才完整读取 [安装事件语义登记](install-event-semantics.md)。该文件必须登记事件数据源、action 语义、事件时间、去重键、关联键和观察窗口；APK 能使用稳定 `chain_id`，沙盒能使用稳定 `install_round_id`，且当前与基线 cohort 已按知识库观察窗口成熟时才执行。文件为空、登记不完整或当前 APK/沙盒不适用时停止该模块，不得按字段或 action 名称猜测事件。

在每个 chain/安装轮次内按事件时间验证：

```text
下载完成 -> 安装触发 -> 安装开始 -> 最终完成 / 最终失败 / unknown_loss
```

该模块的聚合查询必须同时产出当前与基线的下载完成、触发、开始、最终完成、最终失败、`unknown_loss`、`start_only` 和失败后恢复计数及比率，不新增最终输出字段。“出现过失败信号”与“最终失败”分开：失败后在成熟窗口内完成的样本计为恢复，不计入最终失败。`start_only` 只说明已观测安装开始但没有完成/失败回调，是 `unknown_loss` 的诊断子集，不得再与 `unknown_loss` 相加；`unknown_loss` 只是严格漏斗中没有已知最终状态的剩余。两者都不能直接写成用户取消、安装执行失败、系统拦截或 ROM 问题。

守恒至少满足 `安装开始 = 最终完成 + 最终失败 + unknown_loss`。若存在下游事件缺上游、相同时间戳无法判序、回调覆盖突变或守恒不通过，严格漏斗验证无效。安装阶段结果只校准 `summary`、现有候选的 `finding` 和 `recommended_action`，不创建新候选，不改写原贡献。

### 错误码与恢复

只有已形成合法一级候选，且满足以下任一信号时才执行一次：

- 下载互斥终态中 `explicit_failed` 占比相对基线上升至少 5bp；
- 安装阶段路由或严格漏斗中，失败信号或最终失败占比相对基线上升至少 5bp；
- 根指标本身或已完成的归因聚合已经提供失败 PV 率，且该率相对基线上升至少 5bp、同口径受影响实体不少于 100。

错误码来源只允许使用归因宽表中已有正式语义的字段，或已登记且通过辅助维度门禁的事件源；安装事件源需要时从 [安装事件语义登记](install-event-semantics.md) 取得。没有合法错误码来源时停止该模块。

查询继承完整根范围，并优先在同一聚合中同时保留大盘和已定位焦点切片的对照。聚合结果对每个错误码至少产出当前与基线的错误事件数、受影响设备×游戏/chain 数、同口径业务分母、错误实体率、每个受影响实体的重复次数，以及可观测时的最终失败与后续恢复数，最终只用于校准既有输出字段。Retry 次数是查询后的解释指标，不得反向作为本次查询的触发条件；其相对基线增加至少 50%而错误实体率稳定时，优先表述为重试放大，不得表述为影响面同比例扩大。

只有错误码查询通过门禁并冻结需要解释的编码后，才完整读取 [下载与安装错误码字典](download-install-error-code-dictionary.md)，根据事件日期、来源和适用版本补充含义；模块未触发时不得预先加载。字典为空、版本不适用或一码多义时保留原编码，将含义标记为未确认并写入 `evidence_limits`。不得按历史经验、编码外观或未登记的本地常量猜测含义；原始 `info`、message 和 URL 只能使用脱敏后的类别，不得输出可回溯明细。

错误码可以重叠，只是机制方向证据，不得把各码的 PV、受影响实体或估算影响相加为原因贡献。该模块只校准现有候选的 `finding`、`summary` 和 `recommended_action`，不创建 `top_findings` 候选。

### 跨维度候选重叠

同时满足以下条件时执行一次：

- 至少两个不同维度家族产生合法候选；
- 较弱候选的不利影响不少于 `max(0.0005, abs(root_adverse_delta) * 0.25)`；

下载链路的 `game_id` 与 `is_reserve_auto_download` 是规定并行快判家族；该组合满足上述条件时，本模块是规定校准步骤，不能以单个游戏已经充分解释大盘或已经完成游戏背景查询为由跳过，并占用一个方向增强模块名额。

只选择全局不利影响最大的两个跨家族候选。分别查询当前和基线的完整四象限：

```text
BOTH
LEFT_ONLY
RIGHT_ONLY
NEITHER
```

每个象限必须提供分子、分母，四个象限合计必须闭合到根指标。从交叉结果复算的两个候选边际影响必须与原一级结果在 `0.000001` 容差内对账，四象限贡献分解也必须闭合。门禁不通过时该验证无效，不得猜测重叠。

验证只允许区分影响主要位于共享人群、左侧独有人群、右侧独有人群或部分重叠；不同候选及四象限影响仍不得相加为因果份额。共享程度高时合并排查方向，独有影响均明显时保留两个方向，均不能升级为机制根因。四象限桶只校准原候选的 `finding`，不得作为第三个 `top_findings` 候选，也不得覆盖原候选的贡献数值。

### 同日新旧版本准实验

同时满足以下条件时执行一次：

- 初步候选或合法二级已经收敛到具体游戏及 APK/版本方向；
- 游戏背景发现异常窗口附近存在 APK 或版本变化；
- 同一业务日的新旧版本均有样本，且在双方共同存在的合法一级维度支持层中各自满足既有样本门槛。

比较必须继承完整根范围和候选游戏。共同支持默认只使用下载和安装链路都已登记的 `device_brand + os_major_version`；其他支持字段只有出现在当前链路的一级白名单且新旧版本两侧都覆盖可靠时才可增加。不得引入新的临时维度，不得使用目标日之后的数据，不得为了得到预期方向临时挑选品牌、OS、渠道或其他过滤。共同支持不足、版本身份不唯一或发布选择偏差无法界定时停止该模块。

必须同时检查版本事件之前的合法趋势：异常在发版前已经开始时，新版本最多表述为次级放大因素，不能解释异常起点；新版本同日更差且此前没有同方向恶化时，可以提升为优先排查方向，但同日差异仍只是准实验关联，不能确认版本是根因。

若游戏背景能提供发布、灰度或配置变更的精确权威时刻，且目标业务日同时包含变更前后样本，则在该模块的同一聚合查询中补充小时级断点对照。阶段边界必须直接取自变更记录并在查看结果前冻结，不得根据率值最低的小时反向移动断点。下载按首次下载开始小时归 cohort，安装按知识库观察窗口已成熟的下载源 cohort 归属；临近数据截止且尚未成熟的小时只报告开始量或错误信号，不与已成熟小时比较完成率。异常早于变更或新版本发布用于修复已发生问题时，不得用该版本解释异常起点。没有精确权威变更记录时不执行小时级断点，不搜索或猜测替代时刻，只保留已经满足门禁的同日共同样本比较和事前趋势检查。

### 同类负对照与广泛性检查

只有初步结论准备指向具体 `game_id` 或 `apk_size_tier`，且满足以下任一条件时执行一次：

- 头部游戏剔除后异常缩小至少 50%、恢复到 5bp 容差内或方向反转；
- `root_adverse_delta > 0`，且头部游戏或包体候选的不利影响不少于 `root_adverse_delta` 的 50%。

游戏候选优先选择相同 `game_type`、相同包体档位的其他游戏作为 peer。包体候选若同时有已通过门槛的焦点游戏，选择同一 `game_type`、同包体档位且不含焦点游戏的样本作为 peer；没有具体焦点游戏时，选择同一 `game_type` 的其他包体档位作为负对照，此时只校准包体档位的特异性。焦点当前日或基线日均样本必须不少于 100，peer 按同样口径必须不少于 500；范围、日期和聚合方法必须与根调查一致。

peer 与焦点同方向变化且绝对变化达到焦点的 50% 时，降低实体特异性，优先转向共同影响范围；peer 与焦点同方向但绝对变化不足焦点的 25% 时，增强焦点切片特异性；介于两者之间或方向不一致时只写证据不足。负对照只能校准排查范围，不能证明焦点实体或共同特征是根因。

广泛性检查不新增查询：只有初步结论准备指向单一 `channel_group`、`device_brand` 或 `os_major_version` 桶时，才复用已完成且已闭合的对应一级家族结果。若至少两个其他非质量桶各自通过既有样本与占比门槛，并出现与焦点同方向、绝对变化不少于焦点 50% 的恶化，则降低“单一渠道/品牌/OS 特异”的置信度，转向共同影响范围排查。该检查只校准现有候选的特异性，不产生新候选，不计入两个新增查询模块的上限。

### 辅助维度门禁

辅助维度不做固定扫描。只有前述游戏背景查询或已触发的增强模块需要归因宽表之外的数据时，才在使用前校验：

1. 通过 `describe_table` 确认来源字段、关联键和时间字段。
2. 关联键确实表示目标业务日或事件时点上的关系；快照字段不得冒充历史关系。
3. 事件来源先按稳定事件 ID 去重；没有事件 ID 时只可使用已确认的 `关联键 + action + event_time + 脱敏内容摘要` 复合键，并把同时间戳无法判序的比例保留为质量边界。
4. 只有 `chain_id` 或 `install_round_id` 时不得默认全局唯一；先检查其是否跨日期、设备或游戏复用，再使用已登记的复合业务键关联。冲突范围无法界定时停止对应模块。
5. 一对多来源先收敛到根逻辑粒度，关联后分子、分母和行数不得放大。
6. 分别检查当前与基线匹配率及其变化；`unmatched`、未知和歧义样本保留为质量桶，不得并入业务类别。
7. 涉及时序时，辅助事件必须发生在被解释事件之前或知识库允许的观察窗口内。

门禁失败时跳过对应游戏背景证据或增强模块，并写入 `evidence_limits`。错误码权威字典只用于补充编码含义时是例外：字典关联门禁失败只省略含义，不使已通过事件源、关联键、时序和闭合门禁的错误码统计失效。若辅助维度只用于增强验证，不影响已经由正式归因数据形成的定位结论；若候选本身依赖该辅助维度，则门禁通过前不得将其写入 `top_findings`。

增强查询受阻或失败时，仍须遵守 SQL 快速报错排查和最多两次有依据修正规则，并保留真实失败事实。由于增强验证是定位完成后的可选校准，其受阻、失败、共同支持不足或没有新增判断不覆盖已经合法形成的调查状态；在 `evidence_limits` 记录验证边界，不将整个调查改写为 `query_blocked` 或 `query_failed`。只有增强结果通过自身全部门禁时，才可用它校准 `summary`、候选 `finding` 和 `recommended_action`。

## 停止与结论

出现任一情况停止当前指标：

1. 知识库未唯一命中：`insufficient_definition`。
2. 正确映射后的目标或基线分区、样本或成熟窗口不足：`insufficient_data`。
3. 告警日期与分析日期映射、分子、分母、方向或范围无法可靠确定：`insufficient_definition`。
4. 按正确日期、范围和精度复核后，当前值仍与告警无法对齐且无法解释：`insufficient_definition`。
5. 权限阻止根指标，或阻止全部已登记归因家族：`query_blocked`；只阻止一个家族时继续其他家族。
6. 根指标 SQL 修正两次仍失败，或全部已登记归因家族都查询失败：`query_failed`；单个家族失败时继续其他家族。
7. 根指标存在，但归因数据源无法复现完整根范围，或没有任何已登记维度字段可执行：`unsupported_drilldown`；单个维度家族不完整或不闭合不满足此条件。
8. 告警新增性判为既有异常延续且没有达到 5bp 的实质性新增恶化：`no_dominant_slice`。
9. 至少一个一级家族形成合法结果，并且按链路规定完成全部已触发的后续一级后，没有非质量候选达到 5bp：`no_dominant_slice`。
10. 一级、按条件执行的反事实和最多一次二级均已完成或合法跳过，且没有命中方向增强触发条件。
11. 已执行两个需要新增查询的方向增强模块，或继续验证不会改变排查方向。

结论语义边界：

- 可以说明异常集中范围、切片对总体的不利影响和剔除后的算术变化，但必须保留具体对象身份。
- 可以说明告警属于既有异常延续、候选之间的重叠或独有范围、同类样本的共同变化，以及异常与背景事件的先后关系。
- 可以根据证据推荐后续核查方向，但不能把待核查方向写成已确认机制或修复结论。
- 不能直接断言版本、CDN、错误码、系统安装器或游戏发版是根因；不能把最大候选、时间共现、跨维度重叠、同类共振、同日版本差异或反事实改善当成因果确认。

具体用户措辞不在 Playbook 中定义。结构化事实冻结后，按 [告警诊断文案规范](diagnosis-writing-policy.md) 生成 `summary`、各类 `finding`、`evidence_limits`、`recommended_action`、`reason` 和 `action`。

### 输出语义

- `summary` 和顶层 `finding` 描述整体指标变化、告警新增性、检查范围和综合结论，不得把整体指标伪装成维度切片。
- `top_findings` 只包含通过贡献分解、闭合、样本、占比、质量桶和候选门槛的具体切片。每项必须写 `dimension`、`label` 或 `value`、`adverse_impact_bp` 和 `finding`；缺少切片身份或只描述整体变化的项不是合法候选。`entrant/exit` 候选的 `finding` 必须明确是新增或退出流量的结构影响，不得表述为该切片自身完成率恶化。方向增强结果可以校准 `finding` 的排查范围和特异性，但不得创建新切片、改变原贡献或写入辅助模块自身的整体结果。
- 至少存在一个合法切片时才返回 `completed`。告警新增性已经确认是既有异常延续，或完成规定的一级检查但没有合法候选时，返回 `no_dominant_slice` 并省略 `top_findings`；`summary` 必须分别明确“因无实质性新增恶化而停止”或已经执行的一级检查范围。只有根指标受阻、完整根范围无法复现，或全部已登记归因家族因数据、权限或查询限制均不可执行时，才使用对应受阻状态；单个家族或独立阶段诊断失败不能把调查改写成“下钻未完成”。
- 方向增强模块没有触发时不输出占位说明；触发后受阻、失败或证据不足时，把真实边界写入 `evidence_limits`，不覆盖已有合法定位状态。增强结果通过全部门禁时，使用现有 `summary`、`top_findings[].finding`、`evidence_limits` 和 `recommended_action` 表达，不新增接口字段。
- `counterfactual` 只记录实际完成的剔除计算，必须写目标切片的 `dimension`、`label` 或 `value`、`removal_delta_bp`、`restoration_ratio` 和 `finding`。未触发、未执行或无法计算时省略该字段；原因属于 `evidence_limits`，不是反事实结论。`no_dominant_slice` 不得携带反事实。
