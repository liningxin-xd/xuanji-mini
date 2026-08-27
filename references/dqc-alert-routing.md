# DQC 告警路由表

本文只负责将已知 DQC 告警映射为固定分析范围、监控表对账字段和 Playbook，不定义指标业务分子、分母、观察窗口或归因规则。

## 适用对象

仅对以下对象表使用本注册表：

```text
tap_dw.ads_dmg_quality_platform_download_chain_monitor_1d
ads_dmg_quality_platform_download_chain_monitor_1d
```

所有当前档案的 `playbook_id` 均为 `download-install`。

## 匹配顺序

1. 先使用“对象表 + 已登记完整规则名”命中当前观测档案；这是确定性查表，不调用 LLM。
2. 完整标题未命中时，从规则名 `【】` 中提取并仅做大小写、空格、全半角和 `->`/`→` 归一化的 `metric_hint`。
3. `metric_hint` 以 `apk` 开头时固定 `game_type=app`；以 `沙盒` 开头时固定 `game_type=sandbox`。不得根据监控字段区分，因为两种范围复用字段名。
4. 按规则名后缀分类：先匹配“连续 3 周下降”为 `trend_3w`，再匹配“对比过去 7 天均值”为 `relative_7d`，最后匹配“最近 1 天”为 `absolute_1d`。
5. 使用“对象表 + metric_hint + rule_kind”唯一命中 Contract entry。表内条件是触发告警的 `alert_operator`；DataWorks payload 的 `rule.op -> rule.operator` 是校验通过的 `pass_operator`，两者语义相反。
6. `pass_operator` 与 `alert_operator` 符合下表互补关系时是正常配置；完整标题、字段、比较符或阈值存在其他差异时记录非阻断 profile warning，继续使用已注册范围和知识库定义分析，不得仅因此返回 `insufficient_definition` 或丢弃调查结果。
7. DataWorks payload 的 `taskId` 是一次 DQC 任务实例身份，不是稳定规则 ID，不得作为路由键。未来只有明确由 DQC 配置提供的稳定 rule ID 才可增加为优先查表身份。
8. 只有对象表、`metric_hint` 或 `rule_kind` 无法唯一命中 entry，或者 entry 绑定的知识库定义/执行计划缺失时，才在路由阶段返回 `insufficient_definition`；不得选择文本最相似的其他指标。
9. `monitor_numerator_field` 和 `monitor_denominator_field` 只登记监控表中用于复现物化率的字段；必须与知识库正式技术口径一致，不得反向充当指标定义。

## 比较符语义

| 告警条件 (`alert_operator`) | DataWorks 通过条件 (`pass_operator`) |
|---|---|
| `<` | `>=` |
| `<=` | `>` |
| `>` | `<=` |
| `>=` | `<` |
| `==` | `!=` |
| `!=` | `==` |

例如完成率告警条件 `< 0.75` 对应 payload 通过条件 `>= 0.75`；失败率告警条件 `> 0.01` 对应 payload 通过条件 `<= 0.01`。两组分别描述告警侧和通过侧，不得直接用字符串相等判断是否匹配。

## 已注册档案

机器路由只读取 `contracts/dqc-routes.yaml`。该 Contract 当前登记 16 条 route entry，覆盖 APK/沙盒的下载完成率、下载失败率、下载失败次数比率、下载人为停止率和下载安装完成率。每个 entry 显式包含稳定 `route_id`、`metric_hint`、`rule_kind`、已观测标题、监控字段、阈值、范围和 `canonical_metric`。

`canonical_metric` 是 `contracts/metric-definitions.lock.json` 中的知识库指标键。Runtime 启动时必须验证它能唯一解析到已编译的 data-analysis 定义，并且当前 chain/game type/metric 存在执行计划；标题只用于 DQC 配置审计，不是指标定义。

线上路由不调用 LLM、不使用向量召回或编辑距离，也不从相似指标推断。新增业务必须先增加 route entry、知识库指标 lock 和对应 execution plan，再进入 shadow。

修改路由时必须先更新 Contract 并通过契约测试。不要补造当前未注册的沙盒安装相对 7 日或三周趋势档案；只有看到真实规则配置后才能增加。

## checkResult 语义

- `absolute_1d`：`checkResult` 是告警当前值，但仍需按 Playbook 从监控表复现。
- `relative_7d`：`checkResult` 是当前值相对过去 7 日基线的比率，不是根指标当前值；必须另查当前指标及基线。
- `trend_3w`：`checkResult` 是 `0/1` 命中标志，不是业务指标；必须另查当前指标及各周值。

同一标准指标、同一 `game_type`、同一告警分区下的不同 `rule_kind` 合并为一次调查。不同 `game_type` 永不合并。
