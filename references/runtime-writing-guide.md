# Runtime 文案指南

本指南只用于 `primary_v1` writer pack 的一次文案生成。

Runtime 已冻结状态、候选、日期、数值、query ID 和执行证据；不要重写或补充这些字段。

## 输入

只读取 writer pack：

- `metric/analysis_date/game_type/root_metric`
- `steps` 的状态、候选数和 warning code
- 最多每家族 3 个 `candidates`
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
- `entrant/exit` 只描述流量进入或退出的结构影响，不写成切片表现恶化。
- `evidence_limits` 只写当前真实限制，不填通用免责声明。
- `recommended_action` 必须独立可读，明确对象、环节和观察目标。
- 使用“核查、复核、跟踪、验证”，不用“证明、确定根因、必然导致”。
- 不使用没有在同句绑定对象的“该游戏、该切片、该方向、对应团队”。

## 自检

- 是否只提交四个允许字段？
- finding key 是否全部来自 writer pack？
- 是否没有新增候选、查询、状态、日期或数值？
- 是否把相关性、贡献或时间共现升级成因果？
- 行动字段脱离上文后是否仍能理解？
