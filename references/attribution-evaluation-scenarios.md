# 归因链路测评场景笔记

记录日期：2026-08-25

本文记录本轮 code review 实际检查过的历史告警、聚合数据范围和可复用测评断言。它不是生产 Playbook、指标定义或当前告警证据，运行 `xuanji-mini` 时不得主动加载本文，也不得复用本文数值回答新告警。

## 使用方式

- 历史批次只作为回归样本，测评时重新构造输入或使用隔离副本，不修改已完成 batch。
- 数值快照用于证明场景曾真实出现；数仓回刷后允许数值变化，但集合关系和路由断言不应变化。
- `expected` 表示目标行为；标记为 `known_gap` 的场景在修复前应作为预期失败，而不是误报为已通过。
- 生产中只有根范围不可复现或全部已登记家族均不可执行时，才允许合法的 `unsupported_drilldown`。

## 已检查证据

### 历史 analysis batch

| Batch ID | 告警分区 | 检查到的场景 |
|---|---|---|
| `20260824T062112Z-4d10eb1c7b59` | `dt=2026-08-23` | 沙盒下载完成率由游戏和预约自动下载定位；APK 安装属于既有异常延续 |
| `20260824T102551Z-5e7b7b17eb02` | `dt=2026-08-23` | 同一输入的稳定复跑；游戏、预约、反事实和规则合并 |
| `20260825T014122Z-3ccf0caec54e` | `dt=2026-08-24` | 沙盒失败率只跑快判即停止；沙盒完成率定位渠道和包体；APK 安装定位游戏和 OS 二级 |

### 表结构

通过 `describe_table` 检查：

- `tap_dw.ads_report_store_platform_device_game_download_chain_attribution_1d`
- `tap_dw.ads_report_store_platform_device_game_install_chain_attribution_1d`

关键语义：

- 下载 `app_major_version` 是首次非空客户端大版本；`first_download_matched` 标识首次下载上下文。
- 安装 `diagnostic_event_matched` 只表示匹配任一安装诊断事件，不是 `installStart` 的有效性标记。
- 沙盒的客户端 `installStart` 相关字段不适用。
- 安装官方指标只按 `is_metric_anchor=1` 的 `official_download_complete` 和 `official_install_complete` 汇总。

### 历史聚合查询

| Query ID | 日期范围 | 粒度 | 用途 |
|---|---|---|---|
| `f7ceea40-2781-4129-b055-715e06fdc8b7` | 2026-08-15 至 2026-08-22 | 日期 + app/sandbox | 检查安装 `D/S/C`、完成但无 start、start 但未完成和诊断事件覆盖 |
| `fa1618ea-da96-4839-8c34-8ac2ac2c4167` | 2026-08-22 至 2026-08-24 | 日期 + app/sandbox | 检查下载版本、首次下载上下文、设备维度和活跃 OS 的覆盖 |

冻结观察：

- APK 每天有 5,259 至 6,663 个 `official_install_complete=1` 但未观测到 `installStart` 的样本。
- 2026-08-20 APK：`D=1,476,380`、`S=1,339,162`、`C=1,033,718`、`C且非S=6,383`、`S且非C=311,827`。
- 沙盒连续 8 天 `S=0`，但每天约有 539,795 至 573,062 个官方安装完成样本，证明 APK 的 `D/S/C` 语义不能直接套用沙盒。
- 下载维度匹配覆盖整体很高；2026-08-22 APK 仅观察到 1 个“版本存在但首次下载上下文未匹配”样本。

## 历史回归场景

| ID | 输入或历史现象 | 目标行为 | 当前状态 |
|---|---|---|---|
| `H-DL-01` | 2026-08-23 沙盒下载完成率绝对阈值与三周趋势同时命中 | 合并为一个调查；保留 `game_id` 和预约自动下载结果；允许反事实与重叠校准 | `passed_history` |
| `H-DL-02` | 2026-08-24 沙盒下载失败率只比七日基线上升约 1.33bp，游戏和预约均无 5bp 候选 | 继续尝试 `brand -> channel -> app version -> OS -> apk_size`；至少一个家族合法后才可 `no_dominant_slice` | `covered_by_writer` |
| `H-DL-03` | 2026-08-24 沙盒下载完成率定位 `paid_sem` 和两个包体档位 | 新顺序先尝试品牌；最终候选仍按全局不利影响排序，不能因执行顺序改变候选数值 | `replay_required` |
| `H-DL-04` | 2026-08-22 沙盒下载完成率因游戏与预约结果未同时通过而拒绝下钻 | 淘汰失败家族并继续五个低基数维度；不得返回组合门禁式 `unsupported_drilldown` | `regression_required` |
| `H-DL-05` | 2026-08-22 APK 人为停止率约 6.04%，游戏与预约结果未同时通过 | 使用停止率专用 QuerySpec/模板并继续固定低基数队列 | `regression_required` |
| `H-IN-01` | 2026-08-21 APK 安装完成率仍低于阈值，但较七日基线改善约 27bp | 判为既有异常延续并返回 `no_dominant_slice`；不为了给方向而强行下钻 | `passed_history` |
| `H-IN-02` | 2026-08-22 APK 安装完成率下降约 121bp，定位诡秘之主和 Android 16 | 游戏优先结论必须保留；阶段质量不能否定游戏或 OS 二级结果 | `passed_history` |
| `H-IN-03` | 2026-08-20 APK 安装曾因官方锚点、chain 覆盖和游戏闭合组合门禁而拒绝 | 官方游戏投影、阶段拆解和低基数家族相互独立；chain 只约束依赖 chain 的诊断 | `regression_required` |
| `H-IN-04` | APK 每日存在完成但未观测 start 的样本 | 用集合交集拆阶段；`C且非S` 单列覆盖风险，不能拒绝全部阶段结果 | `fixed_stage_v3` |
| `H-IN-05` | 沙盒安装 `S=0` 且 `C>0` | 明确跳过 APK 专属 `D/S/C`，继续游戏和官方低基数维度 | `fixed_sandbox_route` |

## 路由覆盖清单

当前注册 16 条路由。未来测评至少覆盖下列业务族；同一族不同规则类型仍要验证规则合并、根值字段和日期语义。

| 业务族 | APK | 沙盒 | 规则类型 | 归因资产 |
|---|---:|---:|---|---|
| 下载完成率 | 3 | 3 | absolute、relative_7d、trend_3w | 完成率游戏 QuerySpec + 完成率一级模板 |
| 下载失败率 | 1 | 1 | absolute | 失败率专用游戏 QuerySpec + 一级模板 |
| 下载失败次数比率 | 1 | 1 | absolute | PV 率专用游戏 QuerySpec + 一级模板；不得增加实体率上限 |
| 下载人为停止率 | 1 | 1 | absolute | 停止率专用游戏 QuerySpec + 一级模板 |
| 下载安装完成率 | 3 | 1 | APK 三类；沙盒仅 absolute | 安装游戏 QuerySpec + 阶段 QuerySpec + 安装一级模板 |

## 状态机测评矩阵

### 共同前置

| ID | 场景 | Expected |
|---|---|---|
| `C-01` | 路由未注册或知识库未唯一命中 | `insufficient_definition`，不执行归因 |
| `C-02` | 告警分区到业务日期映射失败 | `insufficient_definition` |
| `C-03` | 正确业务日期的目标或基线分区不成熟 | `insufficient_data` |
| `C-04` | 根值与告警在合法精度和口径下仍无法对齐 | `insufficient_definition` |
| `C-05` | 告警在阈值侧但前一日和七日基线同侧，且无 5bp 新增恶化 | `no_dominant_slice`，属于正确停止 |

### 下载归因

| ID | 场景 | Expected |
|---|---|---|
| `D-01` | 游戏和预约均合法，游戏已充分解释 | 保留两个快判家族；按条件做反事实、一次二级和增强，不强制横扫 |
| `D-02` | 任一快判家族不合法 | 继续完整五维队列 |
| `D-03` | 两个快判合法但都无候选 | 继续完整五维队列 |
| `D-04` | 五维中任一家族查询、字段、闭合或覆盖失败 | 当前家族 `failed`，后续家族继续 |
| `D-05` | 至少一个家族合法，但所有业务桶均不足 5bp | `no_dominant_slice`，摘要列出实际完成范围 |
| `D-06` | 全部已登记家族均因同类数据、权限或查询问题不可执行 | 按真实原因使用 `insufficient_data/query_blocked/query_failed/unsupported_drilldown` |
| `D-07` | 失败次数比率分子大于分母 | 只按非负计数比检查，不套用 `numerator <= denominator` |
| `D-08` | 不同维度同时命中同一批样本 | 各自保留但不得相加；满足门槛时做四象限校准 |

### 安装归因

| ID | 场景 | Expected |
|---|---|---|
| `I-01` | APK 新增恶化 | `game_id` 第一，随后执行 APK 阶段拆解 |
| `I-02` | 游戏不合法、无候选、解释不足或剔除后仍异常 | 阶段之后依次尝试 `brand -> storage -> OS -> apk_size` |
| `I-03` | 阶段查询失败或质量字段异常 | 保留游戏及官方维度结果；不得升级为整条不支持 |
| `I-04` | APK `C且非S>0` | 按集合交集计算两个损耗阶段，并单列覆盖风险 |
| `I-05` | APK `S=0,C=0` | 可报告开始前未观测损耗；`C/S` 保持未定义 |
| `I-06` | APK `S->C` 同向恶化达到 5bp | 条件执行安装事件版本诊断；版本结果不进入官方 `C/D` 候选 |
| `I-07` | 沙盒安装 | 跳过 APK 专属 `installStart` 阶段，继续官方归因队列 |
| `I-08` | chain_id、install_round_id 或诊断事件覆盖不足 | 只限制依赖这些字段的阶段/增强模块，不否定官方投影 |

## 未来 runner 验收字段

为防止只靠文案约束，建议每个归因家族产生一条内部执行记录：

```text
family
status = succeeded | failed | skipped_with_reason
query_id（真实存在时）
candidate_count
warning_codes
```

writer 或测评器至少断言：

1. 触发固定队列时，预期家族恰好覆盖且顺序正确。
2. 单家族失败后仍存在下一家族结果。
3. `no_dominant_slice` 至少有一个合法家族，并已覆盖全部触发家族。
4. `unsupported_drilldown` 只能在完整根范围不可复现或全部家族不可执行时出现。
5. APK 阶段拆解的两个损耗桶闭合到 `D-C`，覆盖风险不混入损耗。
6. 沙盒安装不执行 APK `installStart` 阶段。
7. 维度执行顺序不改变最终按不利影响排序的候选数值。

## 本轮修复状态

- `covered_by_writer`：`attribution_execution.steps` 固化下载与安装的完整顺序；daily-push companion branch `codex/attribution-execution-validation` 在新写入注册告警时校验覆盖、顺序、合法跳过和最终状态。
- `fixed_stage_v3`：APK 阶段 QuerySpec v3 使用集合交集拆出开始前未完成、开始后未完成和已开始且完成；两类损耗闭合到 `D-C`，`C且非S` 只作为覆盖风险。
- `fixed_sandbox_route`：沙盒 `install_stage` 固定记录为 `skipped_not_applicable`，不执行 APK QuerySpec，后续官方维度队列仍必须完整执行。
- 安装二级模板的观察窗口 min/max 已限定到 `official_download_complete=1` 的官方分母样本。
