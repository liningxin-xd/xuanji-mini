# 下载人为停止率一级归因 SQL 模板

仅在下载人为停止率告警通过根指标复核和告警新增性门禁、准备执行一级归因时读取。本模板固定使用 `is_human_stop / download_sample_flag`，不得用于其他指标。

## 使用边界

- 每次只查询一个维度家族。`game_id` 必须使用 [下载人为停止率游戏归因 QuerySpec](download-stop-rate-game-attribution.yaml)；本模板用于 `is_reserve_auto_download` 和 Playbook 后续明确选择的低基数家族。
- 禁止 `CROSS JOIN`、逗号连接、`JOIN ... ON 1 = 1`，也禁止把总体汇总 CTE 连接回分桶结果。
- 总体数值通过分桶聚合后的窗口函数获得。不要把聚合函数与窗口函数写在同一 CTE 层级。
- `business_date` 绑定已经确定的 `analysis_dt`；基线固定为此前 7 个完整业务日。
- `__DIMENSION_SOURCE_FIELD__`、`__DIMENSION_QUALITY_SOURCE_EXPR__`、`__DIMENSION_VALUE_EXPR__` 和 `__DIMENSION_LABEL_EXPR__` 必须按 [一级归因维度登记](primary-attribution-dimensions.md) 为同一维度家族成组替换。每次只选择当前家族的一个源字段，不得原样提交。
- 在 SQL 内按 Playbook 收敛高基数结果并生成 `__other_below_threshold__` 闭合残差桶，结果必须少于 250 行；禁止使用 DView 的 1000 行截断、业务 Top、`LIMIT` 或分页。

## 固定骨架

```sql
WITH scoped_rows AS (
  SELECT
    dt,
    __DIMENSION_SOURCE_FIELD__ AS dimension_source,
    __DIMENSION_QUALITY_SOURCE_EXPR__ AS dimension_quality_matched,
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
    SUM(CASE WHEN dt = ${business_date}
          AND COALESCE(dimension_quality_matched, 0) = 1
      THEN download_sample_flag ELSE 0 END) AS current_dimension_matched_denominator,
    SUM(CASE WHEN dt < ${business_date}
          AND COALESCE(dimension_quality_matched, 0) = 1
      THEN download_sample_flag ELSE 0 END) AS baseline_dimension_matched_denominator,
    SUM(CASE WHEN dt = ${business_date}
          AND COALESCE(dimension_quality_matched, 0) <> 1
      THEN download_sample_flag ELSE 0 END) AS current_dimension_unmatched_denominator,
    SUM(CASE WHEN dt < ${business_date}
          AND COALESCE(dimension_quality_matched, 0) <> 1
      THEN download_sample_flag ELSE 0 END) AS baseline_dimension_unmatched_denominator,
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
    SUM(baseline_numerator) OVER () AS overall_baseline_numerator,
    SUM(current_dimension_matched_denominator) OVER ()
      AS overall_current_dimension_matched_denominator,
    SUM(baseline_dimension_matched_denominator) OVER ()
      AS overall_baseline_dimension_matched_denominator,
    SUM(current_dimension_unmatched_denominator) OVER ()
      AS overall_current_dimension_unmatched_denominator,
    SUM(baseline_dimension_unmatched_denominator) OVER ()
      AS overall_baseline_dimension_unmatched_denominator,
    COUNT(*) OVER () AS source_bucket_count
  FROM bucket_aggregates
), bucket_flags AS (
  SELECT
    CASE WHEN dimension_value IN (
        'unknown', 'invalid', 'not_applicable', 'unmatched',
        '__none__', '__other__', '__other_below_threshold__'
      ) OR dimension_value LIKE 'invalid_%'
        OR dimension_value LIKE 'ambiguous_%'
      THEN 1 ELSE 0 END AS is_quality_bucket,
    CASE WHEN (
        current_denominator >= 100
        OR baseline_denominator * 1.0
          / NULLIF(baseline_day_count, 0) >= 100
      ) AND (
        current_denominator * 1.0
          / NULLIF(overall_current_denominator, 0) >= 0.01
        OR baseline_denominator * 1.0
          / NULLIF(overall_baseline_denominator, 0) >= 0.01
      ) THEN 1 ELSE 0 END AS is_eligible_bucket,
    buckets_with_totals.*
  FROM buckets_with_totals
), classified_buckets AS (
  SELECT
    CASE
      WHEN is_quality_bucket = 1 THEN 'quality'
      WHEN is_eligible_bucket = 1 THEN 'dimension'
      ELSE 'residual'
    END AS bucket_kind,
    CASE
      WHEN dimension_value = 'unmatched' THEN 'unmatched'
      WHEN dimension_value = '__none__' THEN '__none__'
      WHEN is_quality_bucket = 1 THEN '__quality__'
      WHEN is_eligible_bucket = 1 THEN dimension_value
      ELSE '__other_below_threshold__'
    END AS output_dimension_value,
    bucket_flags.*
  FROM bucket_flags
), collapsed_buckets AS (
  SELECT
    bucket_kind,
    output_dimension_value AS dimension_value,
    CASE WHEN bucket_kind = 'dimension' THEN MAX(dimension_label)
      ELSE MAX(output_dimension_value) END AS dimension_label,
    COUNT(*) AS collapsed_source_bucket_count,
    MAX(source_bucket_count) AS source_bucket_count,
    MAX(baseline_day_count) AS baseline_day_count,
    SUM(current_denominator) AS current_denominator,
    SUM(baseline_denominator) AS baseline_denominator,
    SUM(current_numerator) AS current_numerator,
    SUM(baseline_numerator) AS baseline_numerator,
    MAX(overall_current_denominator) AS overall_current_denominator,
    MAX(overall_baseline_denominator) AS overall_baseline_denominator,
    MAX(overall_current_numerator) AS overall_current_numerator,
    MAX(overall_baseline_numerator) AS overall_baseline_numerator,
    MAX(overall_current_dimension_matched_denominator)
      AS overall_current_dimension_matched_denominator,
    MAX(overall_baseline_dimension_matched_denominator)
      AS overall_baseline_dimension_matched_denominator,
    MAX(overall_current_dimension_unmatched_denominator)
      AS overall_current_dimension_unmatched_denominator,
    MAX(overall_baseline_dimension_unmatched_denominator)
      AS overall_baseline_dimension_unmatched_denominator,
    SUM(invalid_metric_row_count) AS invalid_metric_row_count
  FROM classified_buckets
  GROUP BY bucket_kind, output_dimension_value
)
SELECT
  ${business_date} AS analysis_date,
  ${game_type} AS game_type,
  bucket_kind,
  dimension_value,
  dimension_label,
  collapsed_source_bucket_count,
  source_bucket_count,
  baseline_day_count,
  current_denominator,
  baseline_denominator,
  current_numerator,
  baseline_numerator,
  overall_current_denominator,
  overall_baseline_denominator,
  overall_current_numerator,
  overall_baseline_numerator,
  overall_current_dimension_matched_denominator,
  overall_baseline_dimension_matched_denominator,
  overall_current_dimension_unmatched_denominator,
  overall_baseline_dimension_unmatched_denominator,
  overall_current_dimension_matched_denominator * 1.0
    / NULLIF(overall_current_denominator, 0) AS overall_current_dimension_match_rate,
  overall_baseline_dimension_matched_denominator * 1.0
    / NULLIF(overall_baseline_denominator, 0) AS overall_baseline_dimension_match_rate,
  invalid_metric_row_count
FROM collapsed_buckets
ORDER BY
  CASE bucket_kind WHEN 'dimension' THEN 1 WHEN 'residual' THEN 2 ELSE 3 END,
  current_denominator DESC,
  dimension_value ASC
```

## `is_reserve_auto_download` 维度替换

本家族使用以下专用表达式；其余五个低基数家族使用 [一级归因维度登记](primary-attribution-dimensions.md) 的通用表达式和固定顺序。

```sql
__DIMENSION_VALUE_EXPR__ = CASE
  WHEN COALESCE(dimension_quality_matched, 0) <> 1 THEN 'unmatched'
  WHEN dimension_source IN (0, 1)
    THEN CAST(dimension_source AS STRING)
  WHEN dimension_source IS NULL THEN '__none__'
  ELSE CONCAT('invalid_', CAST(dimension_source AS STRING))
END
__DIMENSION_LABEL_EXPR__ = MAX(CASE
  WHEN COALESCE(dimension_quality_matched, 0) <> 1 THEN 'unmatched'
  WHEN dimension_source = 1 THEN 'reserve_auto_download'
  WHEN dimension_source = 0 THEN 'other_download'
  WHEN dimension_source IS NULL THEN '__none__'
  ELSE 'invalid'
END)
```

替换后确认占位符消失、`GROUP BY` 与维度表达式一致且 SQL 不含笛卡尔积。查询结果还必须少于 250 行，`collapsed_source_bucket_count` 合计等于 `source_bucket_count`，未单列的非质量业务源桶进入残差，并通过 Playbook 的 7 日完整性、分子子集、回勾和贡献闭合门禁。只有 `bucket_kind=dimension` 可以产生候选；匹配率和质量桶只形成风险说明，不阻止后续维度。
