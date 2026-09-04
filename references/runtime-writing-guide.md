# Runtime 文案指南

本指南用于 Host 当前分析档案 writer pack 的一次文案生成。

Runtime 已冻结状态、候选、日期、数值、query ID 和执行证据；不要重写或补充这些字段。
结构化事实冻结后不执行独立二次润色，不改变 Runtime 已确定的状态或证据。
最终公开 analysis 使用 schema v5；候选上限只约束 Writer，Host 从冻结 state 无损装配全部公开事实。

## 输入

只读取 writer pack：

- `metric/analysis_date/game_type/root_metric`
- `steps` 的状态、候选数和 warning code
- `primary_v2` 的 `post_primary_steps` 和可选机器反事实
- 最多每家族 3 个 `candidates`
- 候选上的可选 `breadth_calibration`
- 可选 `cross_dimension_overlap_calibration`
- `evidence_limits`

不要请求 SQL、raw rows、完整 state、receipt、hash 或 Playbook。

## 输出

只返回一个 JSON object，字段严格为：

```json
{
  "summary": "...",
  "finding_texts": {"candidate_id": "..."},
  "evidence_limits": ["..."],
  "recommended_action": "..."
}
```

`finding_texts` 的 key 必须逐字复制已暴露 `candidate_id`；没有候选时返回空对象。

## 表达边界

- `summary` 写标准指标、当前相对基线方向和综合判断。
- finding 写具体 label/value、当前与基线变化及不利影响，但不要手抄数值字段。
- 机器反事实只用于校准 summary 和行动方向，不改写其数值、对象或 finding。
- `breadth_calibration.specificity_status=broad_change` 时降低单一渠道、品牌或 OS 特异性，转向共同影响范围排查。
- 四象限校准只用于区分两个既有候选的共享或独有范围，不新增候选、不相加影响，也不升级为机制根因。
- `entrant/exit` 只描述流量进入或退出的结构影响，不写成切片表现恶化。
- `evidence_limits` 只写当前真实限制，不填通用免责声明。
- `recommended_action` 必须独立可读，明确对象、环节和观察目标。
- 全部文字必须渠道无关，不得出现 CardKit、卡片、线程、分页、destination、发送或恢复状态。
- 使用“核查、复核、跟踪、验证”，不用“证明、确定根因、必然导致”。
- 不使用没有在同句绑定对象的“该游戏、该切片、该方向、对应团队”。
- 漏写 finding、渠道术语或泛化行动由 Host 确定性回退；回退不改变事实或建议身份。

## 自检

- 是否只提交四个允许字段？
- finding key 是否全部来自 writer pack？
- 是否没有新增候选、查询、状态、日期或数值？
- 是否把相关性、贡献或时间共现升级成因果？
- 是否把剔除后的算术解释力写成了机制根因？
- 行动字段脱离上文后是否仍能理解？
