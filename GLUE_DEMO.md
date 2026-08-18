# 告警卡片胶水层 Demo

当前阶段只验证：

```text
demo analysis.json -> Lark Card JSON 2.0 -> 本地 Mock Lark
```

不读取 DQC webhook，不调用 xuanji-mini、DView 或真实飞书，也不包含调度。

## 生成 Card JSON

```bash
python3 scripts/alert_card_glue.py render \
  --analysis-file fixtures/demo-analysis.json
```

输出位于 `artifacts/card.json`。

## 发送到 Mock Lark

终端一：

```bash
python3 scripts/mock_lark.py
```

终端二：

```bash
python3 scripts/alert_card_glue.py send \
  --analysis-file fixtures/demo-analysis.json
```

`send` 只接受 localhost HTTP 地址，不能发送到真实飞书。

## 测试

```bash
python3 -m unittest discover -s tests
```
