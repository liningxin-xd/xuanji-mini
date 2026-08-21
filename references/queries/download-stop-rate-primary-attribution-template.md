# 下载人为停止率一级归因 SQL 模板

仅在下载人为停止率告警通过根指标复核和告警新增性门禁、准备执行一级归因时读取。本模板固定使用 `is_human_stop / download_sample_flag`，不得用于其他指标。

## 使用边界

- 每次只查询一个维度家族。`game_id` 必须使用 [下载人为停止率游戏归因 QuerySpec](download-stop-rate-game-attribution.yaml)；本模板用于 `is_reserve_auto_download` 和 Playbook 后续明确选择的低基数家族。
- 禁止 `CROSS JOIN`、逗号连接、`JOIN ... ON 1 = 1`，也禁止把总体汇总 CTE 连接回分桶结果。
- 总体数值通过分桶聚合后的窗口函数获得。不要把聚合函数与窗口函数写在同一 CTE 层级。
- `business_date` 绑定已经确定的 `analysis_dt`；基线固定为此前 7 个完整业务日。
- `__DIMENSION_VALUE_EXPR__` 和 `__DIMENSION_LABEL_EXPR__` 必须按同一维度家族成对替换，不得原样提交。
- 高基数结果按 Playbook 生成 `__other_below_threshold__` 闭合残差桶，禁止用业务 Top 或 `LIMIT` 截断。

## 固定骨架

```sql
WITH scoped_rows AS (
  SELECT
    dt,
    game_id,
    game_name,
    is_reserve_auto_download,
    download_sample_flag,
    is_human_stop
  FROM tap_dw.ads_report_store_platform_device_game_download_chain_attribution_1d
  WHERE dt BETWEEN TO_CHAR(
      DATEADD(TO_DATE(${business_date}), -7, 'dd'), 'yyyy-mm-dd'
    ) AND ${business_date}
    AND platform = 'ANDROID'
    AND game_type = ${game_type}
), bucket_aggregates AS (
  SELECT
    __DIMENSION_VALUE_EXPR__ AS dimension_value,
    __DIMENSION_LABEL_EXPR__ AS dimension_label,
    COUNT(DISTINCT CASE WHEN dt < ${business_date} THEN dt END)
      AS bucket_baseline_day_count,
    SUM(CASE WHEN dt = ${business_date}
      THEN download_sample_flag ELSE 0 END) AS current_denominator,
    SUM(CASE WHEN dt < ${business_date}
      THEN download_sample_flag ELSE 0 END) AS baseline_denominator,
    SUM(CASE WHEN dt = ${business_date}
      THEN is_human_stop ELSE 0 END) AS current_numerator,
    SUM(CASE WHEN dt < ${business_date}
      THEN is_human_stop ELSE 0 END) AS baseline_numerator,
    SUM(CASE WHEN download_sample_flag IS NULL
          OR download_sample_flag <> 1
          OR is_human_stop IS NULL
          OR is_human_stop NOT IN (0, 1)
        THEN 1 ELSE 0 END) AS invalid_metric_row_count
  FROM scoped_rows
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

## `is_reserve_auto_download` 维度替换

```sql
__DIMENSION_VALUE_EXPR__ = CASE
  WHEN is_reserve_auto_download IN (0, 1)
    THEN CAST(is_reserve_auto_download AS STRING)
  WHEN is_reserve_auto_download IS NULL THEN '__none__'
  ELSE CONCAT('invalid_', CAST(is_reserve_auto_download AS STRING))
END
__DIMENSION_LABEL_EXPR__ = MAX(CASE
  WHEN is_reserve_auto_download = 1 THEN 'reserve_auto_download'
  WHEN is_reserve_auto_download = 0 THEN 'other_download'
  WHEN is_reserve_auto_download IS NULL THEN '__none__'
  ELSE 'invalid'
END)
```

替换后确认占位符消失、`GROUP BY` 与维度表达式一致且 SQL 不含笛卡尔积。查询结果仍须通过 Playbook 的 7 日完整性、分子子集、回勾和贡献闭合门禁。
