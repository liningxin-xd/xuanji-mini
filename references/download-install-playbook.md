# Android 下载与安装异常排查 Playbook

## 目录

- [共同规则](#共同规则)
- [告警分区与业务日期](#告警分区与业务日期)
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
2. 在监控表的 `alert_dt` 分区按平台和 APK/沙盒行复现 DQC 当前值。该表是告警口径的事实源。
3. 只有对账确有必要时，才在标准指标表的 `analysis_dt` 分区复算；先汇总分子、分母再相除，并按监控表的 4 位小数精度比较。
4. 根指标通过后，所有基线和归因查询都以 `analysis_dt` 为目标日；基线日期也相对 `analysis_dt` 选择。

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
- 不用目标日期之后的数据解释历史异常。安装成熟窗口是指标自身观察窗口的一部分，不属于事后解释数据。
- 查询前用 `describe_table` 核实表和字段。诊断事件只定位阶段，不替代知识库正式分子、分母。

### 预检

继续前必须确认：

1. 知识库定义唯一，且指标方向明确。
2. 分区可解析为业务日期，目标和 7 个基线日数据齐全。
3. 当前与基线分子、分母均大于零，比率合法。
4. Android、APK/沙盒、下载/安装和观察窗口一致。
5. 当前值与告警方向一致，或差异可由趋势命中标志、cohort 日期、成熟窗口、四舍五入或已知合法口径差异解释。

告警新增性门禁允许继续后，每个实际执行的维度家族还必须确认维度数据覆盖完整根范围，且能闭合到大盘变化。这是一级及后续归因的家族门禁，不是判断既有异常延续的前置查询。

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

### 一级顺序

先并行快判两个独立维度家族：

```text
game_id
is_reserve_auto_download
```

判断异常是单个/少数游戏主导、多个游戏共同变化，还是预约自动下载的结构或组内表现变化。两个家族可能投影同一批样本，即使都显著也不得相加。

游戏不能充分解释或剔除后异常仍明显时，再检查：

```text
apk_size_tier
channel_group
app_major_version
os_major_version
device_brand
```

实际执行时按 `apk_size_tier -> channel_group -> app_major_version -> os_major_version -> device_brand` 的稳定顺序即可；候选最终按单项全局不利影响排序。`network_type_group`、`device_model`、存储和细地域默认只做二级，禁止首轮无方向横扫。

### 终态路由

字段存在且成本合理时，比较头部游戏或大盘的 `download_terminal_state` 分布。终态只选择后续方向：

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

```text
game_id -> apk_size_tier, channel_group, app_major_version,
           os_major_version, device_brand
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

`has_client_install_trigger`、`has_client_install_start`、安装完成和安装失败必须区分；客户端触发或开始均不能表述为系统安装器已成功拉起。可用存储字段必须同时检查覆盖率。

### 一级顺序

先查：

```text
game_id
```

游戏解释不足时检查：

```text
apk_size_tier
app_major_version
os_major_version
device_brand
storage_headroom_tier
```

人工解释顺序为 APK/沙盒、游戏贡献、游戏包版本/包大小、是否进入 `installStart`、完成/失败及失败原因、安装器类型/客户端版本、机型/OS/剩余存储。下载专属的预约自动下载、首次下载网络和地域不进入安装一级归因。

### 合法二级关系

```text
game_id -> apk_size_tier, app_major_version, os_major_version,
           device_brand, storage_headroom_tier
apk_size_tier -> game_id, device_brand, storage_headroom_tier
app_major_version -> device_brand, os_major_version
os_major_version -> device_brand, app_major_version, storage_headroom_tier
device_brand -> device_model, os_major_version, app_major_version,
                storage_headroom_tier
storage_headroom_tier -> apk_size_tier, device_brand, os_major_version
```

## 贡献分解

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
- 维度家族闭合且覆盖率足以支撑结论。

候选按单项全局不利影响排序。非游戏一级最多保留 Top 3。没有候选达到 5bp 时返回 `no_dominant_slice`，不能选择最大但不达标的桶。

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

质量桶不能成为业务候选；其占比突然扩大可以作为数据质量发现。高基数长尾需要合并时使用闭合残差桶，并保持其不可候选属性。

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

剔除后异常绝对值缩小至少 50%、恢复到 5bp 容差内或方向反转，说明该游戏是主要影响范围，可以继续一次二级或游戏背景查询。若切片就是全部范围或剔除后分母非正，停止并说明无法计算。反事实只证明算术解释力，不证明根因。

## 二级下钻

仅当一级候选达到门槛、有足够解释力或明确业务价值、关系已注册、数据覆盖可靠且本次尚未做过二级时执行。只选一个父候选和一个合法子维度。

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

二级也执行同样的池化、分解、质量桶、样本、占比、5bp 和闭合门禁。结束后立即停止维度层级扩展；禁止三级、多个父节点或临时组合。只有命中后文的明确条件时，才可进入不增加维度层级的方向增强验证。

## 游戏背景

仅当游戏候选明确主导时按需查询：

```text
tap_dw.dwt_game_detail_info_view_df
tap_dmp.ods_server_sync_apks
```

可补充游戏状态、可下载状态、当前 APK ID、版本/版本码、包体大小、创建时间和状态，检查是否有接近异常日的发版或状态变化。使用这些辅助来源前同样执行后文的辅助维度门禁；关联或时序不合法的记录不能成为背景证据。背景事件只作为时间线索，不改变贡献计算，不升级为因果结论。

## 方向增强验证

只有告警新增性门禁允许继续，现有一级已经形成合法候选，且按条件触发的剔除反事实与最多一次二级已经执行或合法跳过后，才考虑方向增强验证。先冻结已有定位结论、候选身份、贡献数值和已经实际取得的反事实结果；增强验证只判断该方向是否重复、是否具有实体特异性或是否值得优先探索，不得创造未通过既有门槛的新候选，也不得改写已经闭合的贡献。方向增强属于合法定位完成后的可选校准，不是根指标或规定归因查询。

没有命中下列触发条件时立即停止，不执行验证查询，不需要逐项记录“未验证”。每个调查最多执行两个需要新增查询的增强模块；每个模块只设计一条聚合查询同时覆盖所需日期，因明确 SQL 错误按规则修正重试不视为新模块。多个需要新增查询的模块同时触发时，按“跨维度候选重叠 -> 同日新旧版本准实验 -> 同类负对照”的顺序选择前两个；因上限跳过已触发模块时写入 `evidence_limits`。不得借增强验证新增三级、多个二级父节点或无方向组合扫描。

### 跨维度候选重叠

同时满足以下条件时执行一次：

- 至少两个不同维度家族产生合法候选；
- 较弱候选的不利影响不少于 `max(0.0005, abs(root_adverse_delta) * 0.25)`；

只选择全局不利影响最大的两个跨家族候选。分别查询当前和基线的完整四象限：

```text
BOTH
LEFT_ONLY
RIGHT_ONLY
NEITHER
```

每个象限必须提供分子、分母。四象限必须分别闭合到根指标，从交叉结果复算的两个候选边际影响必须与原一级结果在 `0.000001` 容差内对账，四象限贡献分解也必须闭合。门禁不通过时该验证无效，不得猜测重叠。

验证只允许区分影响主要位于共享人群、左侧独有人群、右侧独有人群或部分重叠；不同候选及四象限影响仍不得相加为因果份额。共享程度高时合并排查方向，独有影响均明显时保留两个方向，均不能升级为机制根因。

### 同日新旧版本准实验

同时满足以下条件时执行一次：

- 初步候选或合法二级已经收敛到具体游戏及 APK/版本方向；
- 游戏背景发现异常窗口附近存在 APK 或版本变化；
- 同一业务日的新旧版本均有样本，且在双方共同存在的合法一级维度支持层中各自满足既有样本门槛。

比较必须继承完整根范围和候选游戏。共同支持默认只使用下载和安装链路都已登记的 `device_brand + os_major_version`；其他支持字段只有出现在当前链路的一级白名单且新旧版本两侧都覆盖可靠时才可增加。不得引入新的临时维度，不得使用目标日之后的数据，不得为了得到预期方向临时挑选品牌、OS、渠道或其他过滤。共同支持不足、版本身份不唯一或发布选择偏差无法界定时停止该模块。

必须同时检查版本事件之前的合法趋势：异常在发版前已经开始时，新版本最多表述为次级放大因素，不能解释异常起点；新版本同日更差且此前没有同方向恶化时，可以提升为优先排查方向，但同日差异仍只是准实验关联，不能确认版本是根因。

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
3. 一对多来源先收敛到根逻辑粒度，关联后分子、分母和行数不得放大。
4. 分别检查当前与基线匹配率及其变化；`unmatched`、未知和歧义样本保留为质量桶，不得并入业务类别。
5. 涉及时序时，辅助事件必须发生在被解释事件之前或知识库允许的观察窗口内。

门禁失败时跳过对应游戏背景证据或增强模块，并写入 `evidence_limits`。若辅助维度只用于增强验证，不影响已经由正式归因数据形成的定位结论；若候选本身依赖该辅助维度，则门禁通过前不得将其写入 `top_findings`。

增强查询受阻或失败时，仍须遵守 SQL 快速报错排查和最多两次有依据修正规则，并保留真实失败事实。由于增强验证是定位完成后的可选校准，其受阻、失败、共同支持不足或没有新增判断不覆盖已经合法形成的调查状态；在 `evidence_limits` 记录验证边界，不将整个调查改写为 `query_blocked` 或 `query_failed`。只有增强结果通过自身全部门禁时，才可用它校准 `summary`、候选 `finding` 和 `recommended_action`。

## 停止与结论

出现任一情况停止当前指标：

1. 知识库未唯一命中：`insufficient_definition`。
2. 正确映射后的目标或基线分区、样本或成熟窗口不足：`insufficient_data`。
3. 告警日期与分析日期映射、分子、分母、方向或范围无法可靠确定：`insufficient_definition`。
4. 按正确日期、范围和精度复核后，当前值仍与告警无法对齐且无法解释。
5. 权限阻止根指标或规定归因查询：`query_blocked`。
6. 根指标或规定归因 SQL 修正两次仍失败：`query_failed`。
7. 根指标存在但无合法下钻数据源：`unsupported_drilldown`。
8. 告警新增性判为既有异常延续且没有达到 5bp 的实质性新增恶化：`no_dominant_slice`。
9. 一级没有非质量候选达到 5bp：`no_dominant_slice`。
10. 一级、按条件执行的反事实和最多一次二级均已完成或合法跳过，且没有命中方向增强触发条件。
11. 已执行两个需要新增查询的方向增强模块，或继续验证不会改变排查方向。

合法措辞：

- “异常主要集中在该游戏/切片。”
- “该切片解释了较多不利变化。”
- “剔除该切片后，异常明显缩小/仍然存在。”
- “告警属于既有异常延续，未发现实质性新增恶化。”
- “两个候选的影响主要集中在共享人群/各自独有人群。”
- “同类样本也出现同方向变化，该候选的实体特异性有限。”
- “异常早于版本变化出现，该版本最多是次级放大因素。”
- “该现象与事件时间接近，仅作为背景证据。”
- “当前只能定位影响范围，尚不能确认机制根因。”
- “建议对应团队继续核查该方向。”

禁止措辞：直接断言版本、CDN、错误码、系统安装器或游戏发版是根因；把最大候选、时间共现、跨维度重叠、同类共振、同日版本差异或反事实改善当成因果确认。

### 输出语义

- `summary` 和顶层 `finding` 描述整体指标变化、告警新增性、检查范围和综合结论，不得把整体指标伪装成维度切片。
- `top_findings` 只包含通过贡献分解、闭合、样本、占比、质量桶和候选门槛的具体切片。每项必须写 `dimension`、`label` 或 `value`、`adverse_impact_bp` 和 `finding`；缺少切片身份或只描述整体变化的项不是合法候选。方向增强结果可以校准 `finding` 的排查范围和特异性，但不得创建新切片、改变原贡献或写入辅助模块自身的整体结果。
- 至少存在一个合法切片时才返回 `completed`。告警新增性已经确认是既有异常延续，或完成规定的一级检查但没有合法候选时，返回 `no_dominant_slice` 并省略 `top_findings`；`summary` 必须分别明确“因无实质性新增恶化而停止”或已经执行的一级检查范围。若根指标或规定归因下钻因数据、权限、查询或数据源限制未完成，使用对应受阻状态，不能写成“分析完成”。
- 方向增强模块没有触发时不输出占位说明；触发后受阻、失败或证据不足时，把真实边界写入 `evidence_limits`，不覆盖已有合法定位状态。增强结果通过全部门禁时，使用现有 `summary`、`top_findings[].finding`、`evidence_limits` 和 `recommended_action` 表达，不新增接口字段。
- `counterfactual` 只记录实际完成的剔除计算，必须写目标切片的 `dimension`、`label` 或 `value`、`removal_delta_bp`、`restoration_ratio` 和 `finding`。未触发、未执行或无法计算时省略该字段；原因属于 `evidence_limits`，不是反事实结论。`no_dominant_slice` 不得携带反事实。
