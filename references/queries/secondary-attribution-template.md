# 二级归因 SQL 模板

仅在 Playbook 的二级下钻条件已经命中、父候选已经通过一级贡献与质量门禁时读取。本模板一次只展开一个父候选和一个已登记子维度，并把父范围外全部样本收敛为不可候选的 `outside_parent`，使子桶贡献仍能闭合到完整根指标。

## 共同绑定

- `${business_date}` 是已经确定的 `analysis_dt`，基线为此前 7 个完整业务日。
- `${game_type}` 继承根调查的 APK/沙盒范围。
- `${parent_value}` 是一级候选已经冻结的规范化字符串值，不得从结果中重新挑选。
- 父、子字段、质量匹配表达式和标准化表达式只从 [归因维度登记](primary-attribution-dimensions.md) 取得，父子关系只从 Playbook 取得。
- `outside_parent`、`quality` 和 `residual` 均不可成为候选。只有 `bucket_kind=child` 的子桶可以进入贡献门禁。
- 禁止业务 Top、`LIMIT`、笛卡尔积以及把父范围分母冒充根分母。低样本或低占比子桶统一收敛为 `__other_below_threshold__`。

## 下载指标绑定

下载骨架的三个指标占位符必须按当前知识库标准指标整组替换：

| 标准指标 | `__DENOMINATOR_SOURCE_FIELD__` | `__NUMERATOR_SOURCE_FIELD__` | `__INVALID_METRIC_PREDICATE__` |
|---|---|---|---|
| `下载完成率` | `download_sample_flag` | `is_download_complete` | `metric_denominator IS NULL OR metric_denominator <> 1 OR metric_numerator IS NULL OR metric_numerator NOT IN (0, 1)` |
| `下载失败率` | `download_sample_flag` | `is_explicit_failed` | `metric_denominator IS NULL OR metric_denominator <> 1 OR metric_numerator IS NULL OR metric_numerator NOT IN (0, 1)` |
| `下载失败次数比率` | `game_download_cnt_1d` | `game_download_failed_cnt_1d` | `metric_denominator IS NULL OR metric_denominator <= 0 OR metric_numerator IS NULL OR metric_numerator < 0` |
| `下载人为停止率` | `download_sample_flag` | `is_human_stop` | `metric_denominator IS NULL OR metric_denominator <> 1 OR metric_numerator IS NULL OR metric_numerator NOT IN (0, 1)` |

标准指标不在表中时停止二级，不得改绑最相似指标。下载失败次数比率不是实体子集率，不增加 `numerator <= denominator` 条件。

## 下载骨架

```sql
WITH raw_scoped_rows AS (
  SELECT
    dt,
    device_id,
    game_id,
    __PARENT_SOURCE_FIELD__ AS parent_source,
    __PARENT_QUALITY_SOURCE_EXPR__ AS parent_quality_matched,
    __CHILD_SOURCE_FIELD__ AS child_source,
    __CHILD_QUALITY_SOURCE_EXPR__ AS child_quality_matched,
    __DENOMINATOR_SOURCE_FIELD__ AS metric_denominator,
    __NUMERATOR_SOURCE_FIELD__ AS metric_numerator
  FROM tap_dw.ads_report_store_platform_device_game_download_chain_attribution_1d
  WHERE dt BETWEEN TO_CHAR(
      DATEADD(TO_DATE(${business_date}), -7, 'dd'), 'yyyy-mm-dd'
    ) AND ${business_date}
    AND platform = 'ANDROID'
    AND game_type = ${game_type}
), scoped_rows AS (
  SELECT
    raw_scoped_rows.*,
    COUNT(*) OVER (PARTITION BY dt, device_id, game_id) AS grain_row_count
  FROM raw_scoped_rows
), normalized_rows AS (
  SELECT
    dt,
    __PARENT_VALUE_EXPR__ AS parent_value,
    __CHILD_VALUE_EXPR__ AS child_value,
    metric_denominator,
    metric_numerator,
    grain_row_count,
    CASE WHEN __INVALID_METRIC_PREDICATE__ THEN 1 ELSE 0 END
      AS invalid_metric_row
  FROM scoped_rows
), classified_rows AS (
  SELECT
    dt,
    CASE
      WHEN parent_value <> ${parent_value} THEN 'outside_parent'
      WHEN child_value IN (
        'unknown', 'invalid', 'not_applicable', 'unmatched',
        '__none__', '__other__', '__other_below_threshold__'
      ) OR child_value LIKE 'invalid_%'
        OR child_value LIKE 'ambiguous_%' THEN 'quality'
      ELSE 'child'
    END AS initial_bucket_kind,
    CASE WHEN parent_value = ${parent_value}
      THEN child_value ELSE 'outside_parent' END AS dimension_value,
    metric_denominator,
    metric_numerator,
    grain_row_count,
    invalid_metric_row
  FROM normalized_rows
), bucket_aggregates AS (
  SELECT
    initial_bucket_kind,
    dimension_value,
    COUNT(DISTINCT CASE WHEN dt < ${business_date} THEN dt END)
      AS bucket_baseline_day_count,
    SUM(CASE WHEN dt = ${business_date}
      THEN metric_denominator ELSE 0 END) AS current_denominator,
    SUM(CASE WHEN dt < ${business_date}
      THEN metric_denominator ELSE 0 END) AS baseline_denominator,
    SUM(CASE WHEN dt = ${business_date}
      THEN metric_numerator ELSE 0 END) AS current_numerator,
    SUM(CASE WHEN dt < ${business_date}
      THEN metric_numerator ELSE 0 END) AS baseline_numerator,
    SUM(CASE WHEN dt = ${business_date} THEN 1 ELSE 0 END)
      AS current_row_count,
    SUM(CASE WHEN dt < ${business_date} THEN 1 ELSE 0 END)
      AS baseline_row_count,
    SUM(CASE WHEN grain_row_count > 1 THEN 1 ELSE 0 END)
      AS duplicate_row_count,
    SUM(invalid_metric_row) AS invalid_metric_row_count
  FROM classified_rows
  GROUP BY initial_bucket_kind, dimension_value
), buckets_with_totals AS (
  SELECT
    bucket_aggregates.*,
    MAX(bucket_baseline_day_count) OVER () AS baseline_day_count,
    SUM(current_denominator) OVER () AS overall_current_denominator,
    SUM(baseline_denominator) OVER () AS overall_baseline_denominator,
    SUM(current_numerator) OVER () AS overall_current_numerator,
    SUM(baseline_numerator) OVER () AS overall_baseline_numerator,
    SUM(current_row_count) OVER () AS overall_current_row_count,
    SUM(baseline_row_count) OVER () AS overall_baseline_row_count,
    SUM(duplicate_row_count) OVER () AS overall_duplicate_row_count,
    SUM(invalid_metric_row_count) OVER () AS overall_invalid_metric_row_count
  FROM bucket_aggregates
), eligible_buckets AS (
  SELECT
    CASE
      WHEN initial_bucket_kind <> 'child' THEN initial_bucket_kind
      WHEN (
        current_denominator >= 100
        OR baseline_denominator * 1.0 / NULLIF(baseline_day_count, 0) >= 100
      ) AND (
        current_denominator * 1.0
          / NULLIF(overall_current_denominator, 0) >= 0.01
        OR baseline_denominator * 1.0
          / NULLIF(overall_baseline_denominator, 0) >= 0.01
      ) THEN 'child'
      ELSE 'residual'
    END AS bucket_kind,
    CASE
      WHEN initial_bucket_kind = 'child' AND NOT (
        (
          current_denominator >= 100
          OR baseline_denominator * 1.0
            / NULLIF(baseline_day_count, 0) >= 100
        ) AND (
          current_denominator * 1.0
            / NULLIF(overall_current_denominator, 0) >= 0.01
          OR baseline_denominator * 1.0
            / NULLIF(overall_baseline_denominator, 0) >= 0.01
        )
      ) THEN '__other_below_threshold__'
      ELSE dimension_value
    END AS output_dimension_value,
    buckets_with_totals.*
  FROM buckets_with_totals
), collapsed_buckets AS (
  SELECT
    bucket_kind,
    output_dimension_value AS dimension_value,
    MAX(output_dimension_value) AS dimension_label,
    MAX(baseline_day_count) AS baseline_day_count,
    SUM(current_denominator) AS current_denominator,
    SUM(baseline_denominator) AS baseline_denominator,
    SUM(current_numerator) AS current_numerator,
    SUM(baseline_numerator) AS baseline_numerator,
    SUM(current_row_count) AS current_row_count,
    SUM(baseline_row_count) AS baseline_row_count,
    SUM(duplicate_row_count) AS duplicate_row_count,
    SUM(invalid_metric_row_count) AS invalid_metric_row_count,
    MAX(overall_current_denominator) AS overall_current_denominator,
    MAX(overall_baseline_denominator) AS overall_baseline_denominator,
    MAX(overall_current_numerator) AS overall_current_numerator,
    MAX(overall_baseline_numerator) AS overall_baseline_numerator,
    MAX(overall_current_row_count) AS overall_current_row_count,
    MAX(overall_baseline_row_count) AS overall_baseline_row_count,
    MAX(overall_duplicate_row_count) AS overall_duplicate_row_count,
    MAX(overall_invalid_metric_row_count) AS overall_invalid_metric_row_count
  FROM eligible_buckets
  GROUP BY bucket_kind, output_dimension_value
)
SELECT
  ${business_date} AS analysis_date,
  ${game_type} AS game_type,
  ${parent_value} AS parent_value,
  bucket_kind,
  dimension_value,
  dimension_label,
  baseline_day_count,
  current_denominator,
  baseline_denominator,
  current_numerator,
  baseline_numerator,
  current_row_count,
  baseline_row_count,
  duplicate_row_count,
  invalid_metric_row_count,
  overall_current_denominator,
  overall_baseline_denominator,
  overall_current_numerator,
  overall_baseline_numerator,
  overall_current_row_count,
  overall_baseline_row_count,
  overall_duplicate_row_count,
  overall_invalid_metric_row_count
FROM collapsed_buckets
ORDER BY
  CASE bucket_kind
    WHEN 'child' THEN 1 WHEN 'residual' THEN 2
    WHEN 'quality' THEN 3 ELSE 4 END,
  current_denominator DESC,
  dimension_value ASC
```

## 安装骨架

```sql
WITH raw_scoped_rows AS (
  SELECT
    dt,
    device_id,
    game_id,
    __PARENT_SOURCE_FIELD__ AS parent_source,
    __PARENT_QUALITY_SOURCE_EXPR__ AS parent_quality_matched,
    __CHILD_SOURCE_FIELD__ AS child_source,
    __CHILD_QUALITY_SOURCE_EXPR__ AS child_quality_matched,
    official_download_complete AS metric_denominator,
    official_install_complete AS metric_numerator,
    official_observation_days
  FROM tap_dw.ads_report_store_platform_device_game_install_chain_attribution_1d
  WHERE dt BETWEEN TO_CHAR(
      DATEADD(TO_DATE(${business_date}), -7, 'dd'), 'yyyy-mm-dd'
    ) AND ${business_date}
    AND platform = 'ANDROID'
    AND game_type = ${game_type}
    AND is_metric_anchor = 1
), scoped_rows AS (
  SELECT
    raw_scoped_rows.*,
    COUNT(*) OVER (PARTITION BY dt, device_id, game_id) AS grain_row_count
  FROM raw_scoped_rows
), normalized_rows AS (
  SELECT
    dt,
    __PARENT_VALUE_EXPR__ AS parent_value,
    __CHILD_VALUE_EXPR__ AS child_value,
    metric_denominator,
    metric_numerator,
    official_observation_days,
    grain_row_count,
    CASE WHEN metric_denominator IS NULL
          OR metric_denominator NOT IN (0, 1)
          OR metric_numerator IS NULL
          OR metric_numerator NOT IN (0, 1)
          OR metric_numerator > metric_denominator
      THEN 1 ELSE 0 END AS invalid_metric_row
  FROM scoped_rows
), classified_rows AS (
  SELECT
    dt,
    CASE
      WHEN parent_value <> ${parent_value} THEN 'outside_parent'
      WHEN child_value IN (
        'unknown', 'invalid', 'not_applicable', 'unmatched',
        '__none__', '__other__', '__other_below_threshold__'
      ) OR child_value LIKE 'invalid_%'
        OR child_value LIKE 'ambiguous_%' THEN 'quality'
      ELSE 'child'
    END AS initial_bucket_kind,
    CASE WHEN parent_value = ${parent_value}
      THEN child_value ELSE 'outside_parent' END AS dimension_value,
    metric_denominator,
    metric_numerator,
    official_observation_days,
    grain_row_count,
    invalid_metric_row
  FROM normalized_rows
), bucket_aggregates AS (
  SELECT
    initial_bucket_kind,
    dimension_value,
    COUNT(DISTINCT CASE WHEN dt < ${business_date} THEN dt END)
      AS bucket_baseline_day_count,
    SUM(CASE WHEN dt = ${business_date}
      THEN metric_denominator ELSE 0 END) AS current_denominator,
    SUM(CASE WHEN dt < ${business_date}
      THEN metric_denominator ELSE 0 END) AS baseline_denominator,
    SUM(CASE WHEN dt = ${business_date}
      THEN metric_numerator ELSE 0 END) AS current_numerator,
    SUM(CASE WHEN dt < ${business_date}
      THEN metric_numerator ELSE 0 END) AS baseline_numerator,
    SUM(CASE WHEN dt = ${business_date} THEN 1 ELSE 0 END)
      AS current_row_count,
    SUM(CASE WHEN dt < ${business_date} THEN 1 ELSE 0 END)
      AS baseline_row_count,
    MIN(CASE WHEN dt = ${business_date}
      THEN official_observation_days ELSE NULL END)
      AS current_observation_days_min,
    MAX(CASE WHEN dt = ${business_date}
      THEN official_observation_days ELSE NULL END)
      AS current_observation_days_max,
    MIN(CASE WHEN dt < ${business_date}
      THEN official_observation_days ELSE NULL END)
      AS baseline_observation_days_min,
    MAX(CASE WHEN dt < ${business_date}
      THEN official_observation_days ELSE NULL END)
      AS baseline_observation_days_max,
    SUM(CASE WHEN grain_row_count > 1 THEN 1 ELSE 0 END)
      AS duplicate_row_count,
    SUM(invalid_metric_row) AS invalid_metric_row_count
  FROM classified_rows
  GROUP BY initial_bucket_kind, dimension_value
), buckets_with_totals AS (
  SELECT
    bucket_aggregates.*,
    MAX(bucket_baseline_day_count) OVER () AS baseline_day_count,
    SUM(current_denominator) OVER () AS overall_current_denominator,
    SUM(baseline_denominator) OVER () AS overall_baseline_denominator,
    SUM(current_numerator) OVER () AS overall_current_numerator,
    SUM(baseline_numerator) OVER () AS overall_baseline_numerator,
    SUM(current_row_count) OVER () AS overall_current_row_count,
    SUM(baseline_row_count) OVER () AS overall_baseline_row_count,
    MIN(current_observation_days_min) OVER ()
      AS overall_current_observation_days_min,
    MAX(current_observation_days_max) OVER ()
      AS overall_current_observation_days_max,
    MIN(baseline_observation_days_min) OVER ()
      AS overall_baseline_observation_days_min,
    MAX(baseline_observation_days_max) OVER ()
      AS overall_baseline_observation_days_max,
    SUM(duplicate_row_count) OVER () AS overall_duplicate_row_count,
    SUM(invalid_metric_row_count) OVER () AS overall_invalid_metric_row_count
  FROM bucket_aggregates
), eligible_buckets AS (
  SELECT
    CASE
      WHEN initial_bucket_kind <> 'child' THEN initial_bucket_kind
      WHEN (
        current_denominator >= 100
        OR baseline_denominator * 1.0 / NULLIF(baseline_day_count, 0) >= 100
      ) AND (
        current_denominator * 1.0
          / NULLIF(overall_current_denominator, 0) >= 0.01
        OR baseline_denominator * 1.0
          / NULLIF(overall_baseline_denominator, 0) >= 0.01
      ) THEN 'child'
      ELSE 'residual'
    END AS bucket_kind,
    CASE
      WHEN initial_bucket_kind = 'child' AND NOT (
        (
          current_denominator >= 100
          OR baseline_denominator * 1.0
            / NULLIF(baseline_day_count, 0) >= 100
        ) AND (
          current_denominator * 1.0
            / NULLIF(overall_current_denominator, 0) >= 0.01
          OR baseline_denominator * 1.0
            / NULLIF(overall_baseline_denominator, 0) >= 0.01
        )
      ) THEN '__other_below_threshold__'
      ELSE dimension_value
    END AS output_dimension_value,
    buckets_with_totals.*
  FROM buckets_with_totals
), collapsed_buckets AS (
  SELECT
    bucket_kind,
    output_dimension_value AS dimension_value,
    MAX(output_dimension_value) AS dimension_label,
    MAX(baseline_day_count) AS baseline_day_count,
    SUM(current_denominator) AS current_denominator,
    SUM(baseline_denominator) AS baseline_denominator,
    SUM(current_numerator) AS current_numerator,
    SUM(baseline_numerator) AS baseline_numerator,
    SUM(current_row_count) AS current_row_count,
    SUM(baseline_row_count) AS baseline_row_count,
    MIN(current_observation_days_min) AS current_observation_days_min,
    MAX(current_observation_days_max) AS current_observation_days_max,
    MIN(baseline_observation_days_min) AS baseline_observation_days_min,
    MAX(baseline_observation_days_max) AS baseline_observation_days_max,
    SUM(duplicate_row_count) AS duplicate_row_count,
    SUM(invalid_metric_row_count) AS invalid_metric_row_count,
    MAX(overall_current_denominator) AS overall_current_denominator,
    MAX(overall_baseline_denominator) AS overall_baseline_denominator,
    MAX(overall_current_numerator) AS overall_current_numerator,
    MAX(overall_baseline_numerator) AS overall_baseline_numerator,
    MAX(overall_current_row_count) AS overall_current_row_count,
    MAX(overall_baseline_row_count) AS overall_baseline_row_count,
    MIN(overall_current_observation_days_min)
      AS overall_current_observation_days_min,
    MAX(overall_current_observation_days_max)
      AS overall_current_observation_days_max,
    MIN(overall_baseline_observation_days_min)
      AS overall_baseline_observation_days_min,
    MAX(overall_baseline_observation_days_max)
      AS overall_baseline_observation_days_max,
    MAX(overall_duplicate_row_count) AS overall_duplicate_row_count,
    MAX(overall_invalid_metric_row_count) AS overall_invalid_metric_row_count
  FROM eligible_buckets
  GROUP BY bucket_kind, output_dimension_value
)
SELECT
  ${business_date} AS analysis_date,
  ${game_type} AS game_type,
  ${parent_value} AS parent_value,
  bucket_kind,
  dimension_value,
  dimension_label,
  baseline_day_count,
  current_denominator,
  baseline_denominator,
  current_numerator,
  baseline_numerator,
  current_row_count,
  baseline_row_count,
  current_observation_days_min,
  current_observation_days_max,
  baseline_observation_days_min,
  baseline_observation_days_max,
  duplicate_row_count,
  invalid_metric_row_count,
  overall_current_denominator,
  overall_baseline_denominator,
  overall_current_numerator,
  overall_baseline_numerator,
  overall_current_row_count,
  overall_baseline_row_count,
  overall_current_observation_days_min,
  overall_current_observation_days_max,
  overall_baseline_observation_days_min,
  overall_baseline_observation_days_max,
  overall_duplicate_row_count,
  overall_invalid_metric_row_count
FROM collapsed_buckets
ORDER BY
  CASE bucket_kind
    WHEN 'child' THEN 1 WHEN 'residual' THEN 2
    WHEN 'quality' THEN 3 ELSE 4 END,
  current_denominator DESC,
  dimension_value ASC
```

安装骨架不得加入 `chain_id`、`install_round_id` 或阶段字段作为官方二级投影门禁。当前与基线存在侧的观察窗口必须分别满足 APK=3、沙盒=1；不满足时本家族无效，但不影响其他一级候选或后续合法家族。

## 执行后门禁

1. 所有占位符必须消失，父子关系和字段映射必须在登记中存在。
2. `baseline_day_count = 7`，根分母为正，行级指标非法数、正式粒度重复数均为 0。
3. 所有输出桶的当前与基线分子、分母、行数分别合计回勾根总量；`outside_parent` 必须存在，除非父范围经一级结果证明就是完整根范围。
4. 贡献计算必须包含 `child + residual + quality + outside_parent` 全部桶，并闭合到根变化；只有 `child` 桶允许成为候选。
5. 子桶的占比、样本和 5bp 均使用根范围尺度。不得在父范围内部重新计算局部门槛。
6. 任一门禁失败只淘汰本次父子家族并记录限制，不得删除已经合法形成的一级候选，也不得继续三级下钻。
