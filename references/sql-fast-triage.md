# SQL 快速报错排查手册

只在 DView SQL 查询返回错误后读取本手册。保留原始错误码、错误类别、错误信息、query ID 或 trace ID；只有报错信号与 SQL 形态同时匹配时才应用规则。修正不得改变指标定义、日期范围、平台、APK/沙盒范围、分子分母或关键过滤，并计入当前 SQL 最多两次修正。

## 执行预算与终态

- 每条 SQL 最多执行 3 次：首次执行加最多两次有依据修正。
- 单次 DView 查询默认最多等待 5 分钟，单个维度家族默认最多 15 分钟，单份 analysis request 默认最多 45 分钟；调用基础设施提供更短的硬上限时从其限制。
- `semantic_analysis`、字段/别名/类型/聚合/连接/函数错误、超时、限流、资源不足或服务暂时不可用都不是业务阻塞。修正次数或时间预算耗尽时结束当前 analysis attempt，由外层重新调用，不得包装为 `query_blocked`、`unsupported_drilldown` 或“未发现原因”。
- `query_blocked` 只接受 MaxCompute/DView 明确返回的权限或授权错误；`unsupported_drilldown` 只接受 [告警查询登记表](queries/alert-query-registry.yaml) 中正式共享数据源不存在或不可访问。两者必须保留 provider 原始错误码、类别、信息及非空 query ID/trace ID，并由独立 reviewer 实际执行调用方生成的登记 source probe，取得不与 executor 重叠的新 query/trace ID 和同类错误。probe 成功、SQL/参数/源表/fingerprint 不匹配、错误消息未指向登记源表，或只影响局部查询时都不是 blocker，必须重试。
- 查询成功但正确分区、成熟窗口或样本确实不足属于 `insufficient_data`，必须由成功的分区/样本检查结果证明，不属于 blocked。

## 规则 1：MaxCompute 拒绝笛卡尔积

匹配条件：

- 数据源为 MaxCompute；
- 错误为 `ODPS-0130071` / `semantic_analysis`，或包含 `ODPS-0130252` 且服务端未给出更具体原因；
- SQL 使用 `CROSS JOIN`、`JOIN ... ON 1 = 1`，或等价方式把单行汇总结果连接回分桶结果。

原因：MaxCompute 默认拒绝笛卡尔积。即使被连接的汇总 CTE 预期只有一行，语义层也可能在执行前拒绝查询。

修正：在分桶聚合结果上使用窗口函数计算全局总量，并在后续 CTE 中引用这些总量，不连接单行汇总表。

错误模式：

```sql
WITH bucket AS (...),
totals AS (
  SELECT SUM(curr_num) AS total_curr_num
  FROM bucket
)
SELECT bucket.*, totals.total_curr_num
FROM bucket
CROSS JOIN totals
```

推荐模式：

```sql
WITH bucket AS (...),
with_totals AS (
  SELECT
    bucket.*,
    SUM(curr_num) OVER () AS total_curr_num
  FROM bucket
)
SELECT *
FROM with_totals
```

若 SQL 不含上述笛卡尔积形态，不得仅凭相同错误码套用本规则；继续依据原始错误信息做最小排查。

## 规则 2：`semantic_analysis` 优先检查 SQL

匹配条件：DView 返回的错误类别为 `semantic_analysis`。

判断：该错误通常表示查询已进入数据库语义检查，但 SQL 中的字段、作用域、聚合、类型、连接或函数关系不合法。优先按 SQL 编写错误处理，不得直接记为 `query_failed` 或结束当前查询。

按原始错误信息依次检查：

1. 字段是否存在，表别名、列别名和 CTE 是否在当前作用域可见；需要时重新 `describe_table`。
2. 非聚合字段是否完整出现在 `GROUP BY`，聚合函数与窗口函数是否拆到不同 CTE 层级。
3. `CASE`、`IF`、`UNION ALL`、比较和算术表达式的类型是否兼容，除数是否可能为零。
4. `JOIN` 是否缺少有效连接键、引用歧义字段或形成笛卡尔积；命中时同时应用规则 1。
5. 函数、日期表达式、类型转换和窗口语法是否符合当前数据库引擎。

根据明确问题做最小修正并重试。错误信息不具体时，从最内层 CTE 开始分段验证 SQL，再逐层恢复聚合、窗口和排序；不得通过删除分区、平台、日期、APK/沙盒或指标口径过滤来换取成功。

只有完成有依据的检查和最多两次修正后仍失败，才结束当前 SQL attempt；整个调查必须由外层重新调用，不能把 SQL 编写失败提交为业务终态。
