from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
STATUS_LABELS = {
    "completed": "分析完成",
    "no_dominant_slice": "未发现主导切片",
    "insufficient_definition": "指标定义不足",
    "insufficient_data": "数据不足",
    "query_blocked": "查询受阻",
    "query_failed": "查询失败",
    "unsupported_drilldown": "暂不支持下钻",
}


def load_analysis(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite number {value!r} is not allowed")
            ),
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read analysis file {path}: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError("analysis must be a JSON object")
    if payload.get("source") != "dataworks_dqc":
        raise ValueError("analysis.source must be 'dataworks_dqc'")
    if payload.get("overall_status") not in {"completed", "partial", "failed"}:
        raise ValueError("analysis.overall_status is invalid")
    investigations = payload.get("investigations")
    if not isinstance(investigations, list):
        raise ValueError("analysis.investigations must be an array")
    for index, investigation in enumerate(investigations):
        if not isinstance(investigation, dict):
            raise ValueError(f"analysis.investigations[{index}] must be an object")
        if investigation.get("status") not in STATUS_LABELS:
            raise ValueError(f"analysis.investigations[{index}].status is invalid")
        rule_indexes = investigation.get("rule_indexes")
        if (
            not isinstance(rule_indexes, list)
            or not rule_indexes
            or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in rule_indexes)
            or rule_indexes != sorted(set(rule_indexes))
        ):
            raise ValueError(
                f"analysis.investigations[{index}].rule_indexes must be sorted unique "
                "non-negative integers"
            )
    return payload


def build_card(analysis: dict[str, Any]) -> dict[str, Any]:
    project = _text(analysis.get("project"), "未知项目")
    table = _text(analysis.get("table"), "未知表")
    partition = _text(analysis.get("partition"), "未知分区")
    investigations = analysis["investigations"]

    elements: list[dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": f"**对象** {project} · {table}\n**分区** {partition}",
        }
    ]
    if not investigations:
        elements.append({"tag": "markdown", "content": "暂无可展示的调查结果"})
    for investigation in investigations:
        elements.extend(_investigation_elements(investigation))

    status = analysis["overall_status"]
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": "璇玑 Mini · DQC 分析"},
            "subtitle": {
                "tag": "plain_text",
                "content": f"{len(investigations)} 项调查 · {status}",
            },
            "template": {"completed": "green", "partial": "orange", "failed": "red"}[status],
        },
        "body": {"elements": elements},
    }


def render(analysis_file: Path, output: Path) -> dict[str, Any]:
    analysis = load_analysis(analysis_file)
    card = build_card(analysis)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(card, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return card


def send_to_mock_lark(base_url: str, chat_id: str, card: dict[str, Any]) -> str:
    parsed = urlsplit(base_url)
    if parsed.scheme != "http" or parsed.hostname not in LOCAL_HOSTS:
        raise ValueError("mock send only accepts an http localhost base URL")
    root = base_url.rstrip("/")
    token = _post_json(
        f"{root}/open-apis/auth/v3/tenant_access_token/internal",
        {"app_id": "mock_app", "app_secret": "mock_secret"},
    ).get("tenant_access_token")
    if not token:
        raise RuntimeError("Mock Lark did not return a tenant token")
    query = urlencode({"receive_id_type": "chat_id"})
    response = _post_json(
        f"{root}/open-apis/im/v1/messages?{query}",
        {
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
            "uuid": "xuanji-mini-demo",
        },
        authorization=f"Bearer {token}",
    )
    message_id = response.get("data", {}).get("message_id")
    if response.get("code") != 0 or not message_id:
        raise RuntimeError(f"Mock Lark rejected the card: {response}")
    return str(message_id)


def _investigation_elements(investigation: dict[str, Any]) -> list[dict[str, Any]]:
    status = investigation["status"]
    metric = _text(investigation.get("metric") or investigation.get("metric_hint"), "未知指标")
    lines = [f"**{metric}** · {STATUS_LABELS[status]}"]

    if status in {"completed", "no_dominant_slice"}:
        values = _metric_values(investigation)
        if values:
            lines.append(values)
        if _has_text(investigation.get("summary")):
            lines.extend(("**分析摘要**", _text(investigation["summary"])))
        for finding in investigation.get("top_findings", []):
            if not isinstance(finding, dict):
                continue
            label = _text(finding.get("label") or finding.get("value"), "未知切片")
            impact = _number(finding.get("adverse_impact_bp"))
            detail = _text(finding.get("finding"), "")
            impact_text = f" · {impact:g}bp" if impact is not None else ""
            line = f"- {label}{impact_text}"
            lines.append(f"{line}：{detail}" if detail else line)
        counterfactual = investigation.get("counterfactual")
        if isinstance(counterfactual, dict) and _has_text(counterfactual.get("finding")):
            lines.extend(("**反事实**", _text(counterfactual["finding"])))
        if _has_text(investigation.get("finding")):
            lines.extend(("**分析结论**", _text(investigation["finding"])))
    else:
        reason = investigation.get("reason") or investigation.get("summary")
        if _has_text(reason):
            lines.extend(("**当前诊断**", _text(reason)))

    directions = _texts(investigation.get("evidence_limits"))
    action = investigation.get("recommended_action") or investigation.get("action")
    if _has_text(action):
        directions.append(_text(action))
    if directions:
        lines.append("**优先排查方向**")
        lines.extend(directions)
    return [{"tag": "hr"}, {"tag": "markdown", "content": "\n".join(lines)}]


def _metric_values(investigation: dict[str, Any]) -> str:
    current = _number(investigation.get("current_value"))
    baseline = _number(investigation.get("baseline_value"))
    delta = _number(investigation.get("delta_bp"))
    parts: list[str] = []
    if current is not None:
        parts.append(f"当前 {_rate(current)}")
    if baseline is not None:
        parts.append(f"基线 {_rate(baseline)}")
    if delta is not None:
        parts.append(f"变化 {delta:+g}bp")
    return " · ".join(parts)


def _rate(value: float) -> str:
    return f"{value * 100:.2f}%"


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _text(value: object, default: str = "") -> str:
    if not isinstance(value, str) or not value.strip():
        return default
    return value


def _has_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _texts(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_text(item) for item in value if _has_text(item)]


def _post_json(url: str, payload: dict[str, Any], authorization: str | None = None) -> dict[str, Any]:
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if authorization:
        headers["Authorization"] = authorization
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urlopen(request, timeout=5) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict):
        raise RuntimeError("Mock Lark returned a non-object response")
    return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Render xuanji-mini analysis as a Lark Card 2.0 demo")
    subcommands = result.add_subparsers(dest="command", required=True)
    for name in ("render", "send"):
        command = subcommands.add_parser(name)
        command.add_argument("--analysis-file", type=Path, required=True)
        command.add_argument("--output", type=Path, default=Path("artifacts/card.json"))
        if name == "send":
            command.add_argument("--base-url", default="http://127.0.0.1:18080")
            command.add_argument("--chat-id", default="mock-chat")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        card = render(args.analysis_file, args.output)
        result: dict[str, Any] = {"status": "rendered", "card": str(args.output)}
        if args.command == "send":
            result.update(
                status="sent",
                message_id=send_to_mock_lark(args.base_url, args.chat_id, card),
            )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
