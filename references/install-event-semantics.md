# 安装事件语义登记

## 路由与登记状态

本文件只在 Playbook 的“安装严格漏斗与恢复”模块已经命中触发条件后读取。未触发时不得预加载，也不得为了判断是否触发而扫描下列事件源。

当前登记状态：`candidate_incomplete`。

- 已记录 `android-daily-report` 仓库中可复用的普通 APK 候选定义。
- 本文件不重新定义安装完成率；根指标、下载源日期和成熟窗口仍以指标知识库为唯一来源。
- 当前普通 APK 登记尚不能执行严格漏斗。只有“待确认门禁”全部补齐，且当前与基线均通过覆盖、时序和守恒检查后才可执行。
- 沙盒 action、稳定 `install_round_id` 和去重规则均未登记；沙盒调查必须停止该模块。

候选定义来源于 `taptap/android-daily-report` 的以下文件，复核快照为提交 `6f96807136e6e3594fff276e21c3b51942603ac8`（2026-08-20）：

- `.agents/skills/taptap-download-install-troubleshooting/references/sql_templates.md`
- `.agents/skills/taptap-download-install-troubleshooting/SKILL.md`
- `sql/observations/taptap_android_gray_release/download_install_compare.sql`
- `.agents/references/taptap-download-install-metrics.md`

这些内容是待确认的分析模板，不是当前数仓 schema 或埋点语义的权威证明。两份模板不一致时保留差异，不选择看起来更合理的一侧。

## 普通 APK 候选来源

| 用途 | 候选来源 | 已知字段与过滤 | 当前边界 |
|---|---|---|---|
| 根指标与成熟 cohort | 指标知识库登记的 DQC 主表或设备×游戏基础表 | APK 安装完成率使用下载完成 cohort；P3D 按下载源日期回填 | 只负责根指标、分母和成熟日期，不提供事件严格时序 |
| 严格漏斗候选 | `tap_dw.dwd_str_game_core_behavior_di` | `dt`、`device_id`、`game_id`、`action`、候选时间 `client_action_tms`；`platform='ANDROID'`、`game_type='app'`、`NVL(is_risk_device, 0)=0` | `chain_id` 实际表达式、时间单位和 action 覆盖待确认 |
| 灰度交叉检查 | `tap_dw.ads_dview_tfc_user_event_di` | `action_args.chain_id`、`event_trigger_ts`、`action`、`subtype='apk'` | 这是 ClickHouse 灰度模板，不得自动替代 DWD 来源或与其拼接 |

只有通过 `describe_table` 和口径方确认后，才能把候选来源升级为可执行来源。不得因一个来源缺字段而静默切换到另一来源。

## 普通 APK 候选事件映射

| 漏斗位置 | 候选 action | 当前解释 | 登记状态 |
|---|---|---|---|
| 下载完成 cohort 锚点 | `appDownloadComplete` | DWD 严格漏斗模板使用的下载完成事件 | 待确认；灰度模板使用 `appDownloadNewComplete`，两者不得混用 |
| 安装触发 | `installRequest` | 候选的安装请求事件；安装来源字段也候选取自该事件 | 待确认是否等价于 Playbook 的“安装触发” |
| 安装初始化 | `InstallNew` | 请求与开始之间的中间阶段，用于检查完整时序 | 已记录候选语义，不得冒充 `installRequest` |
| 安装开始 | `installStart` | 已进入安装器；安装器和 ROM 标签候选取自该事件 | 已记录候选语义，字段覆盖仍待确认 |
| 安装完成信号 | `installerInstallComplete` | 安装完成回调 | 已记录候选语义，回调覆盖仍待确认 |
| 安装失败信号 | `installerInstallFail` | 安装失败回调；出现过不等于最终失败 | 已记录候选语义，reason/message 字段未登记 |

若 `installRequest` 在目标或基线覆盖不足，只能把请求层标为不可观测，从首个已确认且覆盖稳定的下游阶段报告观察漏斗；不得用 `InstallNew` 伪造请求层，也不得把缺失请求事件全部计为损耗。

## 时间与观察窗口

- DWD 模板候选事件时间为 `client_action_tms`；数据类型、单位、时区、迟到和同时间戳比例尚未登记。
- 灰度模板使用 `event_trigger_ts`；它只证明另一来源存在该字段，不能证明与 `client_action_tms` 等价。
- 普通 APK 候选窗口是下载源日期 `dt` 到 `dt + 2` 的日历 P3D，包括首尾日期，不是从下载完成时刻滚动 72 小时。
- 下载完成 cohort 只取下载源日期上的锚点；后续事件必须逐 chain 限制在自身锚点之后和观察窗口之内。
- 当前与基线 cohort 都必须在数据截止时已经成熟。窗口未结束、分区迟到或知识库使用不同观察窗口时停止模块。

## 关联键与去重

候选安装实体为：

```text
device_id + game_id + chain_id
```

执行前必须满足：

- `chain_id` 的实际字段或 JSON 表达式已经登记，且目标与基线使用同一语义；当前 DWD 表达式仍缺失。
- `chain_id` 非空率、跨日期复用率以及跨设备或跨游戏冲突率已经报告。冲突 chain 排除，不得只按 `chain_id` 关联。
- 稳定事件 ID 字段当前未登记。来源 Skill 建议有事件 ID 时优先按事件 ID 去重；没有时按 `(chain_id, action, event_tms, info_hash)` 去重，但 `info_hash` 的合法来源和表达式也尚未登记。因此当前没有可执行的事件去重键。
- 完成事件去重后，每个实体的各阶段取最早合法事件时间。相邻阶段时间相同而无法判序时单列质量桶，不能计入严格漏斗。
- scene、安装来源、安装器和 ROM 等辅助标签必须与 chain 同期。同一实体内标签冲突时单列并排除，不得用 `MAX` 随机归类。

## 候选严格时序与终态

只有关联和去重门禁通过后，才按以下候选顺序验证：

```text
下载完成 -> installRequest -> InstallNew -> installStart
         -> 最终完成 / 最终失败 / unknown_loss
```

- `observed_*`：对应事件在下载完成后独立出现，不要求所有上游事件齐全，只用于诊断覆盖和乱序。
- `reached_trigger`：`installRequest` 不早于下载完成。
- `reached_start`：`installRequest <= InstallNew <= installStart`，且均不早于下载完成。
- 最终完成：严格到达安装开始，并在成熟窗口内观察到开始后的 `installerInstallComplete`。
- 最终失败：严格到达安装开始，成熟窗口内没有安装完成，并观察到开始后的 `installerInstallFail`。
- 失败后恢复：同一实体先出现安装失败信号，之后在成熟窗口内出现安装完成；计入最终完成，不计入最终失败。
- `unknown_loss`：严格到达安装开始，但成熟窗口内既不满足最终完成，也不满足最终失败。
- `start_only`：严格到达安装开始，但没有观察到完成或失败回调；它是 `unknown_loss` 的诊断子集，不得再次相加。来源模板当前按“独立观察到 `installStart`”计算，可能包含上游缺失样本；正式登记必须收紧为严格开始口径或证明两者等价。
- `no_install_diagnostic_event`：下载完成后没有观察到 request、new、start、complete 或 fail；只表示没有安装诊断事件，不得解释为用户未安装。
- `terminal_event_out_of_order`：存在完成或失败信号，但上游缺失、顺序错误或时间相同而无法判序；进入质量桶，不并入合法终态。

终态至少满足：

```text
安装开始 = 最终完成 + 最终失败 + unknown_loss
```

`observed_install_fail`、`start_only`、`no_install_diagnostic_event`、失败后恢复和乱序桶是诊断切片，可能相互重叠或与严格状态重叠，不构成可直接相加的末态家族。

## 执行与停止门禁

普通 APK 严格漏斗只有以下项目全部确认后才可执行：

1. 确认权威事件源，以及 DWD 与 ClickHouse 来源是否只是同源映射。
2. 确认下载完成锚点使用 `appDownloadComplete` 还是 `appDownloadNewComplete`，以及各自适用范围。
3. 确认 `installRequest` 是否为 Playbook 的安装触发，并确认 `InstallNew` 的准确语义。
4. 登记 DWD 的 `chain_id` 表达式、稳定事件 ID；若无事件 ID，则登记可执行的 fallback 去重键。
5. 确认事件时间字段的数据类型、单位、时区和迟到语义。
6. 确认 APK P3D 与当前知识库指标观察窗口完全一致。
7. 通过 action 覆盖、chain 覆盖、冲突、重复、乱序、回调覆盖和终态守恒检查。

任一项缺失时，停止严格漏斗模块并在 `evidence_limits` 写明具体缺口；不得按字段名、action 外观或灰度模板自行补齐。合法跳过该增强模块不影响已经通过 Playbook 形成的定位结论。

## 沙盒登记

当前没有登记沙盒的事件源、下载完成锚点、安装触发/开始/完成/失败 action、事件时间、稳定 `install_round_id`、去重键和 1d 观察窗口细节。沙盒调查不得复用普通 APK 的 action 或 `chain_id`，必须停止严格漏斗模块，等待后续文档补充。
