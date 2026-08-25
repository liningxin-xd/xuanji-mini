# 安装开始后版本诊断 SQL 模板

仅在安装告警已经完成 `game_id` 优先归因、独立 `D/S/C` 阶段门禁有效，且 `S -> C` 同向不利变化达到 5bp 时读取。本模板只诊断已观测安装开始后的版本差异，不定义或改写官方 `C / D` 指标。

## 使用边界

- 范围固定为 `is_metric_anchor=1`、`official_download_complete=1`、`has_client_install_start=1`、`diagnostic_event_matched=1`。
- 分母是已观测安装开始 `S`，分子是其中的 `official_install_complete`；不得把结果称为官方安装完成率，也不得与官方 `C / D` 的一级贡献相加或排序。
- 唯一允许的版本字段是 `install_event_app_major_version`。空版本进入不可候选质量桶 `__missing_install_event_version__`，不得写成某个未知版本导致异常。
- 当前和基线都必须回勾已经通过阶段门禁的 `S`、`C`，并保持 7 个完整基线业务日。任一回勾或版本覆盖门禁失败时跳过本诊断，继续其他安装家族。
- 禁止业务 Top、`LIMIT`、分页、笛卡尔积和临时替换其他版本字段。模板必须在 SQL 内收敛长尾版本，结果少于 250 行，不得依赖 DView 的 1000 行截断。

## 固定骨架

```sql
WITH scoped_started_rows AS (
  SELECT
    dt,
    install_event_app_major_version,
    official_install_complete
  FROM tap_dw.ads_report_store_platform_device_game_install_chain_attribution_1d
  WHERE dt BETWEEN TO_CHAR(
      DATEADD(TO_DATE(${business_date}), -7, 'dd'), 'yyyy-mm-dd'
    ) AND ${business_date}
    AND platform = 'ANDROID'
    AND game_type = ${game_type}
    AND is_metric_anchor = 1
    AND official_download_complete = 1
    AND has_client_install_start = 1
    AND diagnostic_event_matched = 1
), version_aggregates AS (
  SELECT
    CASE
      WHEN install_event_app_major_version IS NULL
        OR TRIM(CAST(install_event_app_major_version AS STRING)) = ''
        THEN '__missing_install_event_version__'
      ELSE CAST(install_event_app_major_version AS STRING)
    END AS dimension_value,
    COUNT(DISTINCT CASE WHEN dt < ${business_date} THEN dt END)
      AS bucket_baseline_day_count,
    SUM(CASE WHEN dt = ${business_date} THEN 1 ELSE 0 END)
      AS current_start_denominator,
    SUM(CASE WHEN dt < ${business_date} THEN 1 ELSE 0 END)
      AS baseline_start_denominator,
    SUM(CASE WHEN dt = ${business_date}
      THEN official_install_complete ELSE 0 END) AS current_complete_numerator,
    SUM(CASE WHEN dt < ${business_date}
      THEN official_install_complete ELSE 0 END) AS baseline_complete_numerator,
    SUM(CASE
      WHEN official_install_complete IS NULL
        OR official_install_complete NOT IN (0, 1)
      THEN 1 ELSE 0 END) AS invalid_metric_row_count
  FROM scoped_started_rows
  GROUP BY CASE
    WHEN install_event_app_major_version IS NULL
      OR TRIM(CAST(install_event_app_major_version AS STRING)) = ''
      THEN '__missing_install_event_version__'
    ELSE CAST(install_event_app_major_version AS STRING)
  END
), versions_with_totals AS (
  SELECT
    version_aggregates.*,
    MAX(bucket_baseline_day_count) OVER () AS baseline_day_count,
    SUM(current_start_denominator) OVER () AS overall_current_start_denominator,
    SUM(baseline_start_denominator) OVER () AS overall_baseline_start_denominator,
    SUM(current_complete_numerator) OVER () AS overall_current_complete_numerator,
    SUM(baseline_complete_numerator) OVER () AS overall_baseline_complete_numerator,
    SUM(CASE WHEN dimension_value = '__missing_install_event_version__'
      THEN current_start_denominator ELSE 0 END) OVER ()
      AS current_missing_version_count,
    SUM(CASE WHEN dimension_value = '__missing_install_event_version__'
      THEN baseline_start_denominator ELSE 0 END) OVER ()
      AS baseline_missing_version_count,
    COUNT(*) OVER () AS source_bucket_count
  FROM version_aggregates
), version_flags AS (
  SELECT
    CASE WHEN dimension_value = '__missing_install_event_version__'
      THEN 1 ELSE 0 END AS is_quality_bucket,
    CASE WHEN (
        current_start_denominator >= 100
        OR baseline_start_denominator * 1.0
          / NULLIF(baseline_day_count, 0) >= 100
      ) AND (
        current_start_denominator * 1.0
          / NULLIF(overall_current_start_denominator, 0) >= 0.01
        OR baseline_start_denominator * 1.0
          / NULLIF(overall_baseline_start_denominator, 0) >= 0.01
      ) THEN 1 ELSE 0 END AS is_eligible_bucket,
    versions_with_totals.*
  FROM versions_with_totals
), classified_versions AS (
  SELECT
    CASE
      WHEN is_quality_bucket = 1 THEN 'quality'
      WHEN is_eligible_bucket = 1 THEN 'version'
      ELSE 'residual'
    END AS bucket_kind,
    CASE
      WHEN is_quality_bucket = 1 THEN '__missing_install_event_version__'
      WHEN is_eligible_bucket = 1 THEN dimension_value
      ELSE '__other_below_threshold__'
    END AS output_dimension_value,
    version_flags.*
  FROM version_flags
), collapsed_versions AS (
  SELECT
    bucket_kind,
    output_dimension_value AS dimension_value,
    COUNT(*) AS collapsed_source_bucket_count,
    MAX(source_bucket_count) AS source_bucket_count,
    MAX(baseline_day_count) AS baseline_day_count,
    SUM(current_start_denominator) AS current_start_denominator,
    SUM(baseline_start_denominator) AS baseline_start_denominator,
    SUM(current_complete_numerator) AS current_complete_numerator,
    SUM(baseline_complete_numerator) AS baseline_complete_numerator,
    MAX(overall_current_start_denominator)
      AS overall_current_start_denominator,
    MAX(overall_baseline_start_denominator)
      AS overall_baseline_start_denominator,
    MAX(overall_current_complete_numerator)
      AS overall_current_complete_numerator,
    MAX(overall_baseline_complete_numerator)
      AS overall_baseline_complete_numerator,
    MAX(current_missing_version_count) AS current_missing_version_count,
    MAX(baseline_missing_version_count) AS baseline_missing_version_count,
    SUM(invalid_metric_row_count) AS invalid_metric_row_count
  FROM classified_versions
  GROUP BY bucket_kind, output_dimension_value
)
SELECT
  ${business_date} AS analysis_date,
  ${game_type} AS game_type,
  bucket_kind,
  dimension_value,
  collapsed_source_bucket_count,
  source_bucket_count,
  baseline_day_count,
  current_start_denominator,
  baseline_start_denominator,
  current_complete_numerator,
  baseline_complete_numerator,
  overall_current_start_denominator,
  overall_baseline_start_denominator,
  overall_current_complete_numerator,
  overall_baseline_complete_numerator,
  current_missing_version_count,
  baseline_missing_version_count,
  invalid_metric_row_count
FROM collapsed_versions
ORDER BY
  CASE bucket_kind WHEN 'version' THEN 1 WHEN 'residual' THEN 2 ELSE 3 END,
  current_start_denominator DESC,
  dimension_value ASC
```

执行后必须确认：结果少于 250 行；`collapsed_source_bucket_count` 合计等于 `source_bucket_count`；未单列的非质量版本源桶进入残差；所有版本桶合计严格回勾阶段 QuerySpec 的当前/基线 `S` 和 `C`；`baseline_day_count=7`；无非法正式分子；缺失版本桶分别保留当前与基线占比，并按 Playbook 既有的完整性、跨期稳定性和质量桶门禁判断覆盖是否足够。只有覆盖门禁通过、且 `bucket_kind=version` 的非质量版本桶达到 Playbook 的既有样本、占比和 5bp 方向门槛时，才可用来校准 `summary` 与 `recommended_action`。本诊断不得产生 `top_findings` 或 `counterfactual`。
