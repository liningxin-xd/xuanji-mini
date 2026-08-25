# 归因维度登记

本文登记下载与安装低基数一级模板允许使用的维度字段和替换表达式。使用前必须通过 `describe_table` 确认字段存在及类型；字段不存在只淘汰当前维度家族，不得改用相似字段。

## 通用替换

每次从下方白名单选择一个逻辑维度及其源字段 `FIELD`，先替换模板的源字段占位符，再逐字替换两个表达式占位符：

```sql
__DIMENSION_SOURCE_FIELD__ = FIELD
__DIMENSION_VALUE_EXPR__ = CASE
  WHEN dimension_source IS NULL
    OR TRIM(CAST(dimension_source AS STRING)) = '' THEN '__none__'
  ELSE CAST(dimension_source AS STRING)
END
__DIMENSION_LABEL_EXPR__ = MAX(CASE
  WHEN dimension_source IS NULL
    OR TRIM(CAST(dimension_source AS STRING)) = '' THEN '__none__'
  ELSE CAST(dimension_source AS STRING)
END)
```

`GROUP BY` 必须使用与 `__DIMENSION_VALUE_EXPR__` 逐字相同的非聚合表达式。不得同时选择两个字段，不得拼接组合维度。

## 下载白名单

固定按以下顺序使用；逻辑维度与源字段同名：

```text
is_reserve_auto_download
apk_size_tier
channel_group
app_major_version
os_major_version
device_brand
```

`is_reserve_auto_download` 使用专用表达式：

```sql
__DIMENSION_VALUE_EXPR__ = CASE
  WHEN dimension_source IN (0, 1)
    THEN CAST(dimension_source AS STRING)
  WHEN dimension_source IS NULL THEN '__none__'
  ELSE CONCAT('invalid_', CAST(dimension_source AS STRING))
END
__DIMENSION_LABEL_EXPR__ = MAX(CASE
  WHEN dimension_source = 1 THEN 'reserve_auto_download'
  WHEN dimension_source = 0 THEN 'other_download'
  WHEN dimension_source IS NULL THEN '__none__'
  ELSE 'invalid'
END)
```

## 安装白名单

固定按以下顺序使用。左侧为 Playbook 逻辑维度，右侧为已通过 `describe_table` 核实的安装宽表源字段：

```text
apk_size_tier          -> apk_size_tier
os_major_version       -> os_major_version
device_brand           -> device_brand
storage_headroom_tier  -> storage_headroom_tier
```

安装维度全部使用通用替换。模板只使用官方锚点的正式分子分母；链路键和阶段字段不参与这些维度家族的候选门禁。`install_event_app_major_version` 不在安装官方分母白名单中，因为没有安装事件的下载完成样本天然取不到该字段；它只能按 Playbook 使用独立的开始后版本模板拆 `S -> C`。

## 二级下钻替换

二级下钻只允许使用 Playbook 已登记的父子关系。父维度和值必须来自已经通过一级门禁的候选；子维度必须使用与一级相同的源字段、标签时点和质量规则，不得改用相似字段。

模板中的父、子源字段和质量匹配表达式按下表替换。常量 `1` 表示该维度没有额外匹配标记；质量表达式为 0 或 NULL 的行必须归入 `unmatched`，不得落入看似合法的业务桶。

### 下载二级字段

| 逻辑维度 | 源字段 | 质量匹配表达式 |
|---|---|---|
| `game_id` | `game_id` | `1` |
| `is_reserve_auto_download` | `is_reserve_auto_download` | `1` |
| `apk_size_tier` | `apk_size_tier` | `1` |
| `channel_group` | `channel_group` | `1` |
| `app_major_version` | `app_major_version` | `1` |
| `os_major_version` | `os_major_version` | `active_os_matched` |
| `device_brand` | `device_brand` | `device_dimension_matched` |
| `device_model` | `device_model` | `device_dimension_matched` |
| `network_type_group` | `network_type_group` | `first_download_matched` |

### 安装二级字段

| 逻辑维度 | 源字段 | 质量匹配表达式 |
|---|---|---|
| `game_id` | `game_id` | `1` |
| `apk_size_tier` | `apk_size_tier` | `1` |
| `os_major_version` | `os_major_version` | `active_os_matched` |
| `device_brand` | `device_brand` | `device_dimension_matched` |
| `device_model` | `device_model` | `device_dimension_matched` |
| `storage_headroom_tier` | `storage_headroom_tier` | `device_dimension_matched` |

将父、子字段分别替换为：

```sql
__PARENT_SOURCE_FIELD__ = 父维度源字段
__PARENT_QUALITY_SOURCE_EXPR__ = 父维度质量匹配表达式
__CHILD_SOURCE_FIELD__ = 子维度源字段
__CHILD_QUALITY_SOURCE_EXPR__ = 子维度质量匹配表达式
```

除下载 `is_reserve_auto_download` 外，父子标准化表达式固定为：

```sql
__PARENT_VALUE_EXPR__ = CASE
  WHEN COALESCE(parent_quality_matched, 0) <> 1 THEN 'unmatched'
  WHEN parent_source IS NULL
    OR TRIM(CAST(parent_source AS STRING)) = '' THEN '__none__'
  ELSE CAST(parent_source AS STRING)
END
__CHILD_VALUE_EXPR__ = CASE
  WHEN COALESCE(child_quality_matched, 0) <> 1 THEN 'unmatched'
  WHEN child_source IS NULL
    OR TRIM(CAST(child_source AS STRING)) = '' THEN '__none__'
  ELSE CAST(child_source AS STRING)
END
```

下载 `is_reserve_auto_download` 作为父维度时，使用：

```sql
__PARENT_VALUE_EXPR__ = CASE
  WHEN parent_source IN (0, 1) THEN CAST(parent_source AS STRING)
  WHEN parent_source IS NULL THEN '__none__'
  ELSE CONCAT('invalid_', CAST(parent_source AS STRING))
END
```

`${parent_value}` 只绑定一级候选已经冻结的规范化字符串值。模板执行前必须确认父、子不是同一逻辑维度，关系存在于 Playbook 白名单中，且所有占位符均已消失。
