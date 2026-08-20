# DQC 告警路由表

本文只负责将已知 DQC 告警映射为固定分析范围和 Playbook，不定义指标分子、分母、观察窗口或归因规则。

## 适用对象

仅对以下对象表使用本注册表：

```text
tap_dw.ads_dmg_quality_platform_download_chain_monitor_1d
ads_dmg_quality_platform_download_chain_monitor_1d
```

所有当前档案的 `playbook_id` 均为 `download-install`。

## 匹配顺序

1. 从规则名 `【】` 中提取并仅做大小写、空格、全半角和 `->`/`→` 归一化的 `metric_hint`。
2. `metric_hint` 以 `apk` 开头时固定 `game_type=app`；以 `沙盒` 开头时固定 `game_type=sandbox`。不得根据监控字段区分，因为两种范围复用字段名。
3. 按规则名后缀分类：先匹配“连续 3 周下降”为 `trend_3w`，再匹配“对比过去 7 天均值”为 `relative_7d`，最后匹配“最近 1 天”为 `absolute_1d`。
4. 使用“对象表 + metric_hint + rule_kind”命中下表。payload 含字段级范围时，它必须与 `monitor_field` 一致；不一致立即停止。
5. `observed_rule_id` 和阈值只用于识别当前已知配置，不作为指标定义，也不得覆盖 payload 中的真实值。规则重建导致 ID 变化时，只要其他注册字段一致仍可路由。
6. 未命中档案、比较符不一致或字段不一致时返回 `insufficient_definition`，不得选择相似档案。

## 已注册档案

| 范围 | stage | metric_hint | rule_kind | monitor_field | 已知条件 | observed_rule_id | 知识库指标 |
|---|---|---|---|---|---|---:|---|
| app | download | `apk下载完成率` | `absolute_1d` | `game_download_complete_rate_1d` | `< 0.80` | 28723123 | `下载完成率` |
| app | download | `apk下载完成率` | `relative_7d` | `wave_game_download_complete_rate_prev_7d` | `< 0.98` | 28723124 | `下载完成率` |
| app | download | `apk下载完成率` | `trend_3w` | `is_game_download_complete_rate_7d_trend_down` | `!= 0` | 28723125 | `下载完成率` |
| app | download | `apk下载失败率` | `absolute_1d` | `game_download_failed_rate_1d` | `> 0.03` | 28723126 | 缺失，停止 |
| app | download | `apk下载失败次数比率` | `absolute_1d` | `game_download_failed_pv_rate_1d` | `> 0.10` | 28723127 | 缺失，停止 |
| app | download | `apk人为停止率` | `absolute_1d` | `game_download_stop_rate_1d` | `> 0.06` | 28723128 | 缺失，停止 |
| app | install | `apk下载完成->安装完成率` | `absolute_1d` | `game_download_complete_and_install_complete_prev_2d_rate_p3d` | `< 0.73` | 28723129 | `下载安装完成率` |
| app | install | `apk下载完成->安装完成率` | `relative_7d` | `wave_game_download_complete_and_install_complete_prev_2d_rate_p3d_prev_7d` | `< 0.96` | 28723130 | `下载安装完成率` |
| app | install | `apk下载完成->安装完成率` | `trend_3w` | `is_game_download_complete_and_install_complete_prev_2d_rate_p3d_7d_trend_down` | `!= 0` | 28723131 | `下载安装完成率` |
| sandbox | download | `沙盒下载完成率` | `absolute_1d` | `game_download_complete_rate_1d` | `< 0.75` | 28723132 | `下载完成率` |
| sandbox | download | `沙盒下载完成率` | `relative_7d` | `wave_game_download_complete_rate_prev_7d` | `< 0.94` | 28723133 | `下载完成率` |
| sandbox | download | `沙盒下载完成率` | `trend_3w` | `is_game_download_complete_rate_7d_trend_down` | `!= 0` | 28723134 | `下载完成率` |
| sandbox | download | `沙盒下载失败率` | `absolute_1d` | `game_download_failed_rate_1d` | `> 0.01` | 28723135 | 缺失，停止 |
| sandbox | download | `沙盒下载失败次数比率` | `absolute_1d` | `game_download_failed_pv_rate_1d` | `> 0.03` | 28723136 | 缺失，停止 |
| sandbox | download | `沙盒人为停止率` | `absolute_1d` | `game_download_stop_rate_1d` | `> 0.018` | 28723137 | 缺失，停止 |
| sandbox | install | `沙盒下载完成->安装完成率` | `absolute_1d` | `game_download_complete_and_install_complete_prev_2d_rate_p3d` | `< 0.99` | 28723138 | `下载安装完成率` |

不要补造当前未注册的沙盒安装相对 7 日或三周趋势档案；只有看到真实规则配置后才能增加。

## checkResult 语义

- `absolute_1d`：`checkResult` 是告警当前值，但仍需按 Playbook 从监控表复现。
- `relative_7d`：`checkResult` 是当前值相对过去 7 日基线的比率，不是根指标当前值；必须另查当前指标及基线。
- `trend_3w`：`checkResult` 是 `0/1` 命中标志，不是业务指标；必须另查当前指标及各周值。

同一标准指标、同一 `game_type`、同一告警分区下的不同 `rule_kind` 合并为一次调查。不同 `game_type` 永不合并。
