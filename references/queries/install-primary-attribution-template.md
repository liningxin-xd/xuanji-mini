# 安装低基数一级归因 SQL 模板

仅在安装告警通过根指标复核和告警新增性门禁，已经先完成 `game_id` 与独立 `D/S/C` 阶段拆解，且 `game_id` 未形成合法结果、没有合法候选或解释不足时读取。模板使用与安装游戏 QuerySpec 相同的官方锚点投影，不定义指标。

## 使用边界

- 每次只查询一个维度家族。允许的字段、顺序和替换表达式只从 [一级归因维度登记](primary-attribution-dimensions.md) 的安装白名单取得。
- `business_date` 绑定已经确定的 `analysis_dt`；基线固定为此前 7 个完整业务日。
- 分母只使用 `official_download_complete`，分子只使用 `official_install_complete`，范围只使用 `is_metric_anchor = 1`。
- 本模板不读取 `chain_id`、`install_round_id` 或 `T/S/C/E` 阶段字段；这些字段的质量不能否定已经闭合的官方投影。
- 禁止选择 `install_event_app_major_version`。该字段只允许在阶段门禁通过后，使用独立的开始后版本模板拆 `S -> C`，不得将没有安装事件版本的官方分母行归入普通未知桶。
- 禁止笛卡尔积、业务 Top 和 `LIMIT`。需要收敛长尾时使用不可候选的 `__other_below_threshold__` 残差桶并保持闭合。

## 固定骨架

```sql
WITH scoped_anchor_rows AS (
  SELECT
    dt,
    __DIMENSION_SOURCE_FIELD__ AS dimension_source,
    official_download_complete,
    official_install_complete
  FROM tap_dw.ads_report_store_platform_device_game_install_chain_attribution_1d
  WHERE dt BETWEEN TO_CHAR(
      DATEADD(TO_DATE(${business_date}), -7, 'dd'), 'yyyy-mm-dd'
    ) AND ${business_date}
    AND platform = 'ANDROID'
    AND game_type = ${game_type}
    AND is_metric_anchor = 1
), bucket_aggregates AS (
  SELECT
    __DIMENSION_VALUE_EXPR__ AS dimension_value,
    __DIMENSION_LABEL_EXPR__ AS dimension_label,
    COUNT(DISTINCT CASE WHEN dt < ${business_date} THEN dt END)
      AS bucket_baseline_day_count,
    SUM(CASE WHEN dt = ${business_date}
      THEN official_download_complete ELSE 0 END) AS current_denominator,
    SUM(CASE WHEN dt < ${business_date}
      THEN official_download_complete ELSE 0 END) AS baseline_denominator,
    SUM(CASE WHEN dt = ${business_date}
      THEN official_install_complete ELSE 0 END) AS current_numerator,
    SUM(CASE WHEN dt < ${business_date}
      THEN official_install_complete ELSE 0 END) AS baseline_numerator,
    SUM(CASE WHEN official_download_complete IS NULL
          OR official_download_complete NOT IN (0, 1)
          OR official_install_complete IS NULL
          OR official_install_complete NOT IN (0, 1)
          OR official_install_complete > official_download_complete
        THEN 1 ELSE 0 END) AS invalid_metric_row_count
  FROM scoped_anchor_rows
  GROUP BY __DIMENSION_VALUE_EXPR__
), buckets_with_totals AS (
  SELECT
    bucket_aggregates.*,
    MAX(bucket_baseline_day_count) OVER () AS baseline_day_count,
    SUM(current_denominator) OVER () AS overall_current_denominator,
    SUM(baseline_denominator) OVER () AS overall_baseline_denominator,
    SUM(current_numerator) OVER () AS overall_current_numerator,
    SUM(baseline_numerator) OVER () AS overall_baseline_numerator
  FROM bucket_aggregates
)
SELECT
  ${business_date} AS analysis_date,
  ${game_type} AS game_type,
  dimension_value,
  dimension_label,
  baseline_day_count,
  current_denominator,
  baseline_denominator,
  current_numerator,
  baseline_numerator,
  overall_current_denominator,
  overall_baseline_denominator,
  overall_current_numerator,
  overall_baseline_numerator,
  invalid_metric_row_count
FROM buckets_with_totals
ORDER BY current_denominator DESC, dimension_value ASC
```

替换后确认三个占位符全部消失、`GROUP BY` 与维度表达式一致、SQL 不含笛卡尔积。每次查询的 `scoped_anchor_rows` 只能选择当前家族的一个源字段，不能为了后续家族预选其他维度。查询结果仍须通过 Playbook 的 7 日完整性、官方分子子集、回勾和贡献闭合门禁；模板本身不能证明门禁通过。
