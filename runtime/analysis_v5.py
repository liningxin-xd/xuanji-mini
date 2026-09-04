from __future__ import annotations

import hashlib
from copy import deepcopy
from typing import Any


ANALYSIS_SCHEMA_VERSION = 5
PUBLIC_FACTS_SCHEMA_VERSION = 1
NARRATIVE_SCHEMA_VERSION = 1


# These names are the current Android domain registry. The public schema carries
# the display names so downstream presentation code never interprets field IDs.
DIMENSION_DISPLAY_NAMES = {
    "game_id": "游戏",
    "is_reserve_auto_download": "预约自动下载",
    "device_brand": "设备品牌",
    "device_model": "设备型号",
    "channel_group": "渠道",
    "app_major_version": "客户端大版本",
    "os_major_version": "系统大版本",
    "apk_size_tier": "安装包大小分层",
    "install_stage": "安装阶段",
    "storage_headroom_tier": "存储余量分层",
    "secondary": "二级归因",
}

STEP_DISPLAY_NAMES = {
    **DIMENSION_DISPLAY_NAMES,
    "root_metric": "根指标",
    "counterfactual": "反事实校准",
    "secondary": "二级归因",
    "game_background": "背景信号",
    "breadth_check": "广泛性检查",
    "error_code": "错误码校准",
    "cross_dimension_overlap": "重叠范围校准",
}

EVENT_DISPLAY_NAMES = {
    "download_open": "开放 Android 下载",
    "reservation_open": "开放预约",
    "playable_open": "开放可玩",
    "incident": "事故事件",
    "update": "更新事件",
}

TEMPORAL_RELATION_NAMES = {
    "same_day": "与异常业务日同日",
    "one_day_before": "发生在异常业务日前一日",
    "within_baseline": "发生在基线窗口内",
    "active_from_before_baseline": "在基线窗口前已开始并持续重合",
}

_CHANNEL_TERMS = ("CardKit", "主卡", "副卡", "回复卡", "本消息线程")
_VAGUE_ACTIONS = (
    "复核相关链路和候选维度",
    "关注近期客户端或服务端变更",
    "持续跟踪后续表现",
)


class AnalysisV5Error(ValueError):
    pass


def build_public_facts(
    *,
    writer_pack: dict[str, Any],
    machine_state: dict[str, Any] | None = None,
    attribution_execution: dict[str, Any],
    writer_patch: dict[str, Any],
) -> dict[str, Any]:
    metric = _required_text(writer_pack.get("metric"), "writer_pack.metric")
    polarity = writer_pack.get("metric_polarity")
    if polarity not in {"higher_is_better", "lower_is_better", "target_range"}:
        raise AnalysisV5Error("writer_pack.metric_polarity is invalid")

    root_metric = _build_root_metric(writer_pack, metric, polarity)
    candidates = _public_candidates(writer_pack, machine_state)
    findings = _build_findings(candidates, writer_patch, metric, polarity)
    background_signals = _build_background_signals(
        writer_pack, findings, machine_state=machine_state
    )
    calibration_results = _build_calibrations(
        writer_pack, metric, polarity, machine_state=machine_state
    )
    steps = _build_steps(
        writer_pack,
        attribution_execution,
        machine_state=machine_state,
        findings=findings,
        background_signals=background_signals,
        calibration_results=calibration_results,
    )
    recommendations = _build_recommendations(
        writer_pack,
        writer_patch,
        findings=findings,
        metric=metric,
    )
    narrative = _build_user_narrative(
        writer_pack,
        writer_patch,
        findings=findings,
        recommendations=recommendations,
        metric=metric,
    )
    recommendations = narrative.pop("recommendations")

    return {
        "schema_version": PUBLIC_FACTS_SCHEMA_VERSION,
        "metric": root_metric,
        "steps": steps,
        "findings": findings,
        "background_signals": background_signals,
        "calibration_results": calibration_results,
        "recommendations": recommendations,
        "audit_codes": _machine_audit_codes(writer_pack),
        "user_narrative": narrative,
    }


def public_machine_projection(value: dict[str, Any]) -> dict[str, Any]:
    projected = deepcopy(value)
    projected.pop("user_narrative", None)
    findings = projected.get("findings")
    if isinstance(findings, list):
        for finding in findings:
            if isinstance(finding, dict):
                finding.pop("narrative_text", None)
    recommendations = projected.get("recommendations")
    if isinstance(recommendations, list):
        for recommendation in recommendations:
            if isinstance(recommendation, dict):
                recommendation.pop("display_text", None)
    return projected


def build_existing_anomaly_facts(
    *, investigation: dict[str, Any], writer_patch: dict[str, Any]
) -> dict[str, Any]:
    preflight = investigation.get("root_preflight")
    route = investigation.get("route")
    if not isinstance(preflight, dict) or not isinstance(route, dict):
        raise AnalysisV5Error("existing anomaly requires preflight and route facts")
    metric = _required_text(preflight.get("metric"), "root_preflight.metric")
    polarity = preflight.get("direction")
    if polarity not in {"higher_is_better", "lower_is_better"}:
        raise AnalysisV5Error("root_preflight.direction is invalid")
    root_metric = _root_metric(
        metric=metric,
        polarity=polarity,
        current=preflight["current_value"],
        baseline=preflight["baseline_value"],
        delta_bp=preflight["delta_bp"],
    )
    previous = _measure(
        measure_id="root.previous",
        semantic_type="metric_value",
        value=preflight["previous_value"],
        unit="ratio",
        polarity=polarity,
        direction="unchanged",
        comparable_group=f"root:{metric}:value",
        additive=False,
        display_precision=2,
    )
    threshold = _absolute_threshold(route.get("rules"))
    anomaly_context: dict[str, Any] = {
        "state": "ongoing",
        "previous_value": previous,
    }
    if threshold is not None:
        anomaly_context["threshold"] = _measure(
            measure_id="root.alert_threshold",
            semantic_type="alert_threshold",
            value=threshold,
            unit="ratio",
            polarity=polarity,
            direction="unchanged",
            comparable_group=f"root:{metric}:value",
            additive=False,
            display_precision=2,
        )
    action = _required_text(writer_patch.get("recommended_action"), "recommended_action")
    recommendation = {
        "recommendation_id": "recommendation:monitor-root",
        "bound_finding_ids": [],
        "bound_object_refs": ["root_metric"],
        "action_type": "monitor_metric",
        "scope": {"object_ref": "root_metric", "display_name": metric},
        "observation_goal": f"验证{metric}是否离开告警区间并恢复至稳定水平",
        "display_text": action,
    }
    narrative = _machine_result_narrative(
        writer_patch,
        fallback_summary=f"{metric}仍处于告警区间，本轮按新增性策略未继续归因。",
        fallback_action=f"继续跟踪{metric}，并验证后续业务日是否离开告警区间。",
    )
    recommendation["display_text"] = narrative["recommended_action"]
    return {
        "schema_version": PUBLIC_FACTS_SCHEMA_VERSION,
        "metric": root_metric,
        "anomaly_context": anomaly_context,
        "steps": [
            {
                "step_id": "root_metric",
                "display_name": STEP_DISPLAY_NAMES["root_metric"],
                "ordinal": 1,
                "status": "signal_found",
                "checked_count": 1,
                "result": "既有异常仍在告警区间，按新增性策略停止归因",
                "measure_ids": [
                    root_metric["current"]["measure_id"],
                    root_metric["baseline"]["measure_id"],
                    root_metric["change"]["measure_id"],
                    previous["measure_id"],
                ],
            },
            {
                "step_id": "attribution",
                "display_name": "归因检查",
                "ordinal": 2,
                "status": "skipped_by_policy",
                "reason": "existing_anomaly_without_new_adverse_change",
                "result": "未发现足以触发本轮归因的新不利变化",
                "measure_ids": [],
            },
        ],
        "findings": [],
        "background_signals": [],
        "calibration_results": [],
        "recommendations": [recommendation],
        "audit_codes": list(dict.fromkeys(investigation.get("profile_warnings", []))),
        "user_narrative": narrative,
    }


def build_blocked_facts(
    *, investigation: dict[str, Any], writer_patch: dict[str, Any]
) -> dict[str, Any]:
    metric = _required_text(investigation.get("metric_hint"), "metric_hint")
    status = _required_text(investigation.get("result_status"), "result_status")
    action = _required_text(writer_patch.get("recommended_action"), "recommended_action")
    recommendation = {
        "recommendation_id": "recommendation:resolve-blocker",
        "bound_finding_ids": [],
        "bound_object_refs": ["root_metric"],
        "action_type": "resolve_analysis_blocker",
        "scope": {"object_ref": "root_metric", "display_name": metric},
        "observation_goal": f"补齐{metric}分析所需条件并重新执行归因",
        "display_text": action,
    }
    reason = _required_text(
        investigation.get("machine_reason") or writer_patch.get("summary"),
        "machine_reason",
    )
    narrative = _machine_result_narrative(
        writer_patch,
        fallback_summary=reason,
        fallback_action=f"补齐{metric}分析所需条件后重新执行归因。",
    )
    recommendation["display_text"] = narrative["recommended_action"]
    return {
        "schema_version": PUBLIC_FACTS_SCHEMA_VERSION,
        "metric": {
            "metric_id": "root_metric",
            "display_name": metric,
            "polarity": "unknown",
            "baseline_label": "前 7 日加权基线",
            "baseline_window_days": 7,
        },
        "steps": [
            {
                "step_id": "root_metric",
                "display_name": STEP_DISPLAY_NAMES["root_metric"],
                "ordinal": 1,
                "status": "blocked" if status == "query_blocked" else "failed",
                "reason": status,
                "result": reason,
                "measure_ids": [],
            }
        ],
        "findings": [],
        "background_signals": [],
        "calibration_results": [],
        "recommendations": [recommendation],
        "audit_codes": list(dict.fromkeys(investigation.get("profile_warnings", []))),
        "user_narrative": narrative,
    }


def _build_root_metric(
    writer_pack: dict[str, Any], metric: str, polarity: str
) -> dict[str, Any]:
    root = writer_pack.get("root_metric")
    if not isinstance(root, dict):
        return {
            "metric_id": "root_metric",
            "display_name": metric,
            "polarity": polarity,
            "baseline_label": "前 7 日加权基线",
            "baseline_window_days": 7,
        }
    return _root_metric(
        metric=metric,
        polarity=polarity,
        current=root.get("current_value"),
        baseline=root.get("baseline_value"),
        delta_bp=root.get("delta_bp"),
    )


def _root_metric(
    *, metric: str, polarity: str, current: Any, baseline: Any, delta_bp: Any
) -> dict[str, Any]:
    direction = _direction(float(current) - float(baseline))
    return {
        "metric_id": "root_metric",
        "display_name": metric,
        "polarity": polarity,
        "baseline_label": "前 7 日加权基线",
        "baseline_window_days": 7,
        "current": _measure(
            measure_id="root.current",
            semantic_type="metric_value",
            value=current,
            unit="ratio",
            polarity=polarity,
            direction="unchanged",
            comparable_group=f"root:{metric}:value",
            additive=False,
            display_precision=2,
        ),
        "baseline": _measure(
            measure_id="root.baseline",
            semantic_type="baseline_value",
            value=baseline,
            unit="ratio",
            polarity=polarity,
            direction="unchanged",
            comparable_group=f"root:{metric}:value",
            additive=False,
            display_precision=2,
        ),
        "change": _measure(
            measure_id="root.change",
            semantic_type="absolute_change",
            value=delta_bp,
            unit="bp",
            polarity=polarity,
            direction=direction,
            comparable_group=f"root:{metric}:change",
            additive=False,
            display_precision=2,
        ),
    }


def _public_candidates(
    writer_pack: dict[str, Any], machine_state: dict[str, Any] | None
) -> list[dict[str, Any]]:
    if machine_state is None:
        candidates = writer_pack.get("candidates")
        if not isinstance(candidates, list):
            raise AnalysisV5Error("writer_pack.candidates is invalid")
        return deepcopy(candidates)

    result: list[dict[str, Any]] = []
    for step in machine_state.get("steps", []):
        if (
            not isinstance(step, dict)
            or step.get("status") != "succeeded"
            or step.get("produces_candidates") is not True
        ):
            continue
        dimension = _required_text(step.get("id"), "state.step.id")
        candidates = step.get("candidates")
        if not isinstance(candidates, list) or len(candidates) != step.get(
            "candidate_count"
        ):
            raise AnalysisV5Error("state primary candidates are incomplete")
        for candidate in candidates:
            result.append(
                _public_candidate(candidate, dimension=dimension, level="primary")
            )

    secondary = _post_primary_steps(machine_state).get("secondary")
    if isinstance(secondary, dict) and secondary.get("status") == "succeeded":
        candidates = secondary.get("candidates")
        if not isinstance(candidates, list) or len(candidates) != secondary.get(
            "candidate_count"
        ):
            raise AnalysisV5Error("state secondary candidates are incomplete")
        for candidate in candidates:
            result.append(
                _public_candidate(
                    candidate,
                    dimension=_required_text(
                        secondary.get("child_dimension"), "secondary.child_dimension"
                    ),
                    level="secondary",
                    parent_dimension=_required_text(
                        secondary.get("parent_dimension"),
                        "secondary.parent_dimension",
                    ),
                    parent_value=_required_text(
                        secondary.get("parent_value"), "secondary.parent_value"
                    ),
                    parent_label=_required_text(
                        secondary.get("parent_label"), "secondary.parent_label"
                    ),
                )
            )
    ids = [item["candidate_id"] for item in result]
    if len(ids) != len(set(ids)):
        raise AnalysisV5Error("public candidate IDs are not unique")
    return result


def _public_candidate(
    candidate: Any,
    *,
    dimension: str,
    level: str,
    parent_dimension: str | None = None,
    parent_value: str | None = None,
    parent_label: str | None = None,
) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        raise AnalysisV5Error("state candidate must be an object")
    value = _required_text(candidate.get("value"), "candidate.value")
    item = {
        "candidate_id": (
            f"{dimension}:{value}"
            if level == "primary"
            else f"secondary:{parent_dimension}:{parent_value}:{dimension}:{value}"
        ),
        "attribution_level": level,
        "dimension": dimension,
        "value": value,
        "label": _required_text(
            candidate.get("label") or value, "candidate.label"
        ),
        "current_rate": candidate.get("current_rate"),
        "baseline_rate": candidate.get("baseline_rate"),
        "adverse_impact_bp": candidate.get("adverse_impact_bp"),
        "lifecycle": candidate.get("lifecycle", "common"),
    }
    if level == "secondary":
        item.update(
            {
                "parent_dimension": parent_dimension,
                "parent_value": parent_value,
                "parent_label": parent_label,
            }
        )
    return item


def _primary_steps(
    writer_pack: dict[str, Any], machine_state: dict[str, Any] | None
) -> list[dict[str, Any]]:
    if machine_state is not None:
        steps = machine_state.get("steps")
    else:
        steps = writer_pack.get("steps")
    if not isinstance(steps, list):
        raise AnalysisV5Error("primary steps are invalid")
    return steps


def _post_primary_steps(
    machine_state: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    if machine_state is None:
        return {}
    post_primary = machine_state.get("post_primary")
    if post_primary is None:
        return {}
    if not isinstance(post_primary, dict) or not isinstance(
        post_primary.get("steps"), list
    ):
        raise AnalysisV5Error("state post-primary steps are invalid")
    return {
        _required_text(item.get("id"), "post_primary.id"): item
        for item in post_primary["steps"]
        if isinstance(item, dict)
    }


def _ordered_post_primary_steps(
    writer_pack: dict[str, Any], machine_state: dict[str, Any] | None
) -> list[dict[str, Any]]:
    if machine_state is None:
        steps = writer_pack.get("post_primary_steps", [])
    else:
        post_primary = machine_state.get("post_primary")
        steps = post_primary.get("steps", []) if isinstance(post_primary, dict) else []
    if not isinstance(steps, list):
        raise AnalysisV5Error("post-primary steps are invalid")
    return [
        item
        for item in steps
        if isinstance(item, dict) and item.get("reason") != "profile_step_disabled"
    ]


def _background_groups(
    writer_pack: dict[str, Any], machine_state: dict[str, Any] | None
) -> Any:
    step = _post_primary_steps(machine_state).get("game_background")
    if isinstance(step, dict) and step.get("status") in {"succeeded", "failed"}:
        items = step.get("items")
        if not isinstance(items, list):
            raise AnalysisV5Error("state game background items are invalid")
        return [
            {
                "candidate_id": item.get("candidate_id"),
                "facts": deepcopy(item.get("facts", [])),
            }
            for item in items
            if isinstance(item, dict) and item.get("status") == "succeeded"
        ]
    return writer_pack.get("game_background", [])


def _bound_finding_ids(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    ids: list[str] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        candidate_id = item.get("candidate_id")
        if not isinstance(candidate_id, str) and item.get("scope") == "focus_game":
            game_id = item.get("focus_game_id")
            if isinstance(game_id, int) and not isinstance(game_id, bool) and game_id > 0:
                candidate_id = f"game_id:{game_id}"
        if isinstance(candidate_id, str) and candidate_id:
            ids.append(f"finding:{candidate_id}")
    return list(dict.fromkeys(ids))


def _build_findings(
    candidates: list[dict[str, Any]],
    writer_patch: dict[str, Any],
    metric: str,
    polarity: str,
) -> list[dict[str, Any]]:
    finding_texts = writer_patch.get("finding_texts")
    if not isinstance(finding_texts, dict):
        raise AnalysisV5Error("writer_patch.finding_texts is invalid")
    result: list[dict[str, Any]] = []
    for order, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            raise AnalysisV5Error("writer candidate must be an object")
        candidate_id = _required_text(candidate.get("candidate_id"), "candidate_id")
        narrative_text = finding_texts.get(candidate_id)
        if not isinstance(narrative_text, str) or not narrative_text.strip():
            narrative_text = ""
        level = candidate.get("attribution_level", "primary")
        if level not in {"primary", "secondary"}:
            raise AnalysisV5Error("candidate attribution_level is invalid")
        finding_id = f"finding:{candidate_id}"
        dimension_id = _required_text(candidate.get("dimension"), "candidate.dimension")
        object_value = _required_text(candidate.get("value"), "candidate.value")
        object_label = _required_text(
            candidate.get("label") or candidate.get("value"), "candidate.label"
        )
        item = {
            "finding_id": finding_id,
            "candidate_id": candidate_id,
            "level": level,
            "host_order": order,
            "dimension": {
                "id": dimension_id,
                "display_name": DIMENSION_DISPLAY_NAMES.get(
                    dimension_id, dimension_id
                ),
            },
            "object": {
                "object_ref": f"candidate:{candidate_id}",
                "value": object_value,
                "display_name": object_label,
            },
            "lifecycle": candidate.get("lifecycle", "common"),
            "current": _measure(
                measure_id=f"{finding_id}:current",
                semantic_type="metric_value",
                value=candidate.get("current_rate"),
                unit="ratio",
                polarity=polarity,
                direction="unchanged",
                comparable_group=f"finding:{candidate_id}:value",
                additive=False,
                display_precision=2,
            ),
            "baseline": _measure(
                measure_id=f"{finding_id}:baseline",
                semantic_type="baseline_value",
                value=candidate.get("baseline_rate"),
                unit="ratio",
                polarity=polarity,
                direction="unchanged",
                comparable_group=f"finding:{candidate_id}:value",
                additive=False,
                display_precision=2,
            ),
            "change": _measure(
                measure_id=f"{finding_id}:change",
                semantic_type="absolute_change",
                value=(
                    float(candidate.get("current_rate"))
                    - float(candidate.get("baseline_rate"))
                )
                * 10000,
                unit="bp",
                polarity=polarity,
                direction=_direction(
                    float(candidate.get("current_rate"))
                    - float(candidate.get("baseline_rate"))
                ),
                comparable_group=f"finding:{candidate_id}:change",
                additive=False,
                display_precision=2,
            ),
            "adverse_impact": _measure(
                measure_id=f"{finding_id}:adverse-impact",
                semantic_type="adverse_impact",
                value=candidate.get("adverse_impact_bp"),
                unit="bp",
                polarity=polarity,
                direction="adverse",
                comparable_group=f"root:{metric}:adverse-impact",
                additive=False,
                display_precision=2,
            ),
            "narrative_text": narrative_text.strip(),
        }
        if level == "secondary":
            parent_candidate_id = (
                f"{candidate.get('parent_dimension')}:{candidate.get('parent_value')}"
            )
            item["parent_finding_id"] = f"finding:{parent_candidate_id}"
            item["parent_object_ref"] = f"candidate:{parent_candidate_id}"
        result.append(item)
    known_finding_ids = {item["finding_id"] for item in result}
    for item in result:
        parent_id = item.get("parent_finding_id")
        if parent_id is not None and parent_id not in known_finding_ids:
            raise AnalysisV5Error("secondary finding lacks its primary parent")
    return result


def _build_background_signals(
    writer_pack: dict[str, Any],
    findings: list[dict[str, Any]],
    *,
    machine_state: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    candidate_to_finding = {
        item["candidate_id"]: item for item in findings
    }
    groups = _background_groups(writer_pack, machine_state)
    if not isinstance(groups, list):
        raise AnalysisV5Error("writer_pack.game_background is invalid")
    signals: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, dict):
            raise AnalysisV5Error("game background group must be an object")
        candidate_id = _required_text(group.get("candidate_id"), "background.candidate_id")
        finding = candidate_to_finding.get(candidate_id)
        if finding is None:
            raise AnalysisV5Error("background signal lacks its frozen finding")
        facts = group.get("facts")
        if not isinstance(facts, list):
            raise AnalysisV5Error("background facts must be an array")
        for fact in facts:
            if not isinstance(fact, dict):
                raise AnalysisV5Error("background fact must be an object")
            event_type = _required_text(fact.get("event_kind"), "event_kind")
            event_date = _required_text(fact.get("event_date"), "event_date")
            temporal = _required_text(
                fact.get("temporal_relation"), "temporal_relation"
            )
            transition = fact.get("transition_evidence", "operation_event")
            evidence_level = (
                "direct_observation"
                if transition in {"operation_event", "observed_state_transition"}
                else "registered_date"
            )
            source_type = (
                "operation_event"
                if transition == "operation_event"
                else "lifecycle_registry"
            )
            stable = f"{candidate_id}|{event_type}|{event_date}|{transition}"
            signals.append(
                {
                    "signal_id": "background:" + hashlib.sha256(
                        stable.encode("utf-8")
                    ).hexdigest()[:16],
                    "bound_finding_id": finding["finding_id"],
                    "object": deepcopy(finding["object"]),
                    "event_type": {
                        "code": event_type,
                        "display_name": EVENT_DISPLAY_NAMES.get(event_type, event_type),
                    },
                    "event_at": event_date,
                    "temporal_relation": {
                        "code": temporal,
                        "display_name": TEMPORAL_RELATION_NAMES.get(temporal, temporal),
                    },
                    "evidence_level": evidence_level,
                    "source_type": source_type,
                    "evidence_priority": len(signals) + 1,
                }
            )
    return signals


def _build_calibrations(
    writer_pack: dict[str, Any],
    metric: str,
    polarity: str,
    *,
    machine_state: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    post_steps = _post_primary_steps(machine_state)
    counterfactual_step = post_steps.get("counterfactual")
    counterfactual = (
        counterfactual_step.get("result")
        if isinstance(counterfactual_step, dict)
        and counterfactual_step.get("status") == "succeeded"
        else writer_pack.get("counterfactual")
    )
    if isinstance(counterfactual, dict):
        candidate_id = _required_text(
            counterfactual.get("candidate_id")
            or f"{counterfactual.get('dimension')}:{counterfactual.get('value')}",
            "counterfactual.candidate_id",
        )
        removal_delta = float(counterfactual["removal_delta_bp"])
        restoration_ratio = float(counterfactual["restoration_ratio"])
        if restoration_ratio > 0:
            direction = "reduced"
        elif restoration_ratio < 0:
            direction = "expanded"
        else:
            direction = "unchanged"
        result.append(
            {
                "calibration_id": "counterfactual:" + candidate_id,
                "calibration_type": "counterfactual_removal",
                "bound_finding_ids": [f"finding:{candidate_id}"],
                "object_ref": f"candidate:{candidate_id}",
                "operation": "remove_object",
                "direction": direction,
                "measures": [
                    _measure(
                        measure_id=f"counterfactual:{candidate_id}:remaining-change",
                        semantic_type="remaining_root_change",
                        value=removal_delta,
                        unit="bp",
                        polarity=polarity,
                        direction=_direction(removal_delta),
                        comparable_group=f"root:{metric}:change",
                        additive=False,
                        display_precision=2,
                    ),
                    _measure(
                        measure_id=f"counterfactual:{candidate_id}:restoration",
                        semantic_type="restoration_ratio",
                        value=restoration_ratio,
                        unit="ratio",
                        polarity="higher_is_better",
                        direction=direction,
                        denominator="absolute_root_anomaly",
                        comparable_group=f"root:{metric}:restoration",
                        additive=False,
                        display_precision=1,
                    ),
                ],
                "evidence_boundary": "只表示剔除后的算术解释力，不代表已确认机制根因",
            }
        )
    breadth = post_steps.get("breadth_check")
    if isinstance(breadth, dict) and breadth.get("status") == "succeeded":
        for item in breadth.get("calibrations", []):
            candidate_id = _required_text(item.get("candidate_id"), "breadth.candidate_id")
            result.append(
                {
                    "calibration_id": "breadth:" + candidate_id,
                    "calibration_type": "breadth_check",
                    "bound_finding_ids": [f"finding:{candidate_id}"],
                    "object_ref": f"candidate:{candidate_id}",
                    "operation": "compare_peer_buckets",
                    "direction": _required_text(
                        item.get("specificity_status"), "breadth.specificity_status"
                    ),
                    "measures": [
                        _measure(
                            measure_id=f"breadth:{candidate_id}:focus-change",
                            semantic_type="adverse_rate_change",
                            value=item.get("focus_adverse_rate_change_bp"),
                            unit="bp",
                            polarity=polarity,
                            direction="adverse",
                            comparable_group=f"finding:{candidate_id}:change",
                            additive=False,
                            display_precision=2,
                        ),
                        _measure(
                            measure_id=f"breadth:{candidate_id}:supporting-count",
                            semantic_type="supporting_bucket_count",
                            value=item.get("supporting_bucket_count"),
                            unit="count",
                            polarity="unknown",
                            direction="unchanged",
                            comparable_group=f"breadth:{candidate_id}:count",
                            additive=False,
                            display_precision=0,
                        ),
                    ],
                    "details": {
                        "supporting_buckets": deepcopy(item.get("supporting_buckets", [])),
                        "minimum_relative_rate_change": item.get(
                            "minimum_relative_rate_change"
                        ),
                    },
                    "evidence_boundary": "用于判断异常是否集中于单一对象，不证明共同机制根因",
                }
            )
    error_code = post_steps.get("error_code")
    if isinstance(error_code, dict) and error_code.get("status") == "succeeded":
        result.append(
            {
                "calibration_id": "error-code",
                "calibration_type": "error_code",
                "bound_finding_ids": _bound_finding_ids(
                    error_code.get("frozen_scopes", [])
                ),
                "operation": "compare_error_codes",
                "direction": "observed",
                "measures": [],
                "details": {"facts": deepcopy(error_code.get("facts", []))},
                "evidence_boundary": "错误码分布用于定位异常范围，不单独确认机制根因",
            }
        )
    overlap = post_steps.get("cross_dimension_overlap")
    if isinstance(overlap, dict) and overlap.get("status") == "succeeded":
        frozen = overlap.get("frozen_candidates", [])
        result.append(
            {
                "calibration_id": "cross-dimension-overlap",
                "calibration_type": "cross_dimension_overlap",
                "bound_finding_ids": _bound_finding_ids(frozen),
                "operation": "compare_overlap_quadrants",
                "direction": "observed",
                "measures": [],
                "details": {"quadrants": deepcopy(overlap.get("facts", []))},
                "evidence_boundary": "重叠范围只描述样本交集及影响，不代表因果关系",
            }
        )
    return result


def _build_steps(
    writer_pack: dict[str, Any],
    execution: dict[str, Any],
    *,
    machine_state: dict[str, Any] | None,
    findings: list[dict[str, Any]],
    background_signals: list[dict[str, Any]],
    calibration_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    root = writer_pack.get("root_metric")
    result: list[dict[str, Any]] = []
    if isinstance(root, dict):
        result.append(
            {
                "step_id": "root_metric",
                "display_name": STEP_DISPLAY_NAMES["root_metric"],
                "ordinal": 1,
                "status": "signal_found",
                "checked_count": 1,
                "result": "根指标已通过定义、样本与告警复现检查",
                "measure_ids": ["root.current", "root.baseline", "root.change"],
            }
        )
    finding_ids_by_dimension: dict[str, list[str]] = {}
    for finding in findings:
        finding_ids_by_dimension.setdefault(
            str(finding["dimension"]["id"]), []
        ).append(str(finding["finding_id"]))
    execution_steps = {
        item.get("step"): item
        for item in execution.get("steps", [])
        if isinstance(item, dict)
    }
    writer_steps = _primary_steps(writer_pack, machine_state)
    if not isinstance(writer_steps, list):
        raise AnalysisV5Error("writer_pack.steps is invalid")
    for writer_step in writer_steps:
        if not isinstance(writer_step, dict):
            raise AnalysisV5Error("writer step must be an object")
        step_id = _required_text(
            writer_step.get("id") or writer_step.get("step"), "writer_step.step"
        )
        execution_step = execution_steps.get(step_id, {})
        status = _step_status(
            writer_step.get("status"), writer_step.get("candidate_count")
        )
        item: dict[str, Any] = {
            "step_id": step_id,
            "display_name": STEP_DISPLAY_NAMES.get(step_id, step_id),
            "ordinal": len(result) + 1,
            "status": status,
            "result": _step_result(status, writer_step.get("candidate_count")),
            "finding_ids": finding_ids_by_dimension.get(step_id, []),
            "measure_ids": [],
        }
        candidate_count = writer_step.get("candidate_count")
        if isinstance(candidate_count, int) and not isinstance(candidate_count, bool):
            item["candidate_count"] = candidate_count
        buckets = writer_step.get("breadth_buckets")
        if isinstance(buckets, list):
            item["checked_count"] = len(buckets)
        checked_count = writer_step.get("checked_count")
        if isinstance(checked_count, int) and not isinstance(checked_count, bool):
            item["checked_count"] = checked_count
        reason = writer_step.get("failure_code") or execution_step.get("reason")
        if isinstance(reason, str) and reason:
            item["reason"] = reason
        result.append(item)

    post_steps = _ordered_post_primary_steps(writer_pack, machine_state)
    if not isinstance(post_steps, list):
        raise AnalysisV5Error("writer_pack.post_primary_steps is invalid")
    for post_step in post_steps:
        if not isinstance(post_step, dict):
            raise AnalysisV5Error("post-primary step must be an object")
        step_id = _required_text(
            post_step.get("id") or post_step.get("step"), "post_primary.step"
        )
        status = _step_status(
            post_step.get("status"),
            post_step.get("candidate_count"),
            has_signal=_post_step_has_signal(
                step_id,
                background_signals=background_signals,
                calibrations=calibration_results,
            ),
        )
        calibration_types = {
            "counterfactual": {"counterfactual_removal"},
            "breadth_check": {"breadth_check"},
            "error_code": {"error_code"},
            "cross_dimension_overlap": {"cross_dimension_overlap"},
        }.get(step_id, set())
        item = {
            "step_id": step_id,
            "display_name": STEP_DISPLAY_NAMES.get(step_id, step_id),
            "ordinal": len(result) + 1,
            "status": status,
            "result": _post_step_result(
                step_id,
                status,
                post_step,
                background_signals=background_signals,
                calibrations=calibration_results,
            ),
            "finding_ids": (
                [item["finding_id"] for item in findings if item["level"] == "secondary"]
                if step_id == "secondary"
                else []
            ),
            "measure_ids": (
                [
                    measure["measure_id"]
                    for calibration in calibration_results
                    if calibration["calibration_type"] in calibration_types
                    for measure in calibration["measures"]
                ]
            ),
        }
        candidate_count = post_step.get("candidate_count")
        if isinstance(candidate_count, int) and not isinstance(candidate_count, bool):
            item["candidate_count"] = candidate_count
        if step_id == "game_background":
            item["signal_ids"] = [
                signal["signal_id"] for signal in background_signals
            ]
            item["checked_count"] = len(post_step.get("items", []))
        reason = post_step.get("reason") or post_step.get("failure_code") or post_step.get("limit_code")
        if isinstance(reason, str) and reason:
            item["reason"] = reason
        result.append(item)
    return result


def _build_recommendations(
    writer_pack: dict[str, Any],
    writer_patch: dict[str, Any],
    *,
    findings: list[dict[str, Any]],
    metric: str,
) -> list[dict[str, Any]]:
    action = _required_text(writer_patch.get("recommended_action"), "recommended_action")
    primary = [item for item in findings if item["level"] == "primary"]
    refs = [str(item["object"]["object_ref"]) for item in primary]
    ids = [str(item["finding_id"]) for item in primary]
    scope_names = [str(item["object"]["display_name"]) for item in primary]
    scope_display = "、".join(scope_names) if scope_names else metric
    return [
        {
            "recommendation_id": "recommendation:primary-investigation",
            "bound_finding_ids": ids,
            "bound_object_refs": refs or ["root_metric"],
            "action_type": "inspect_metric_path" if primary else "monitor_metric",
            "scope": {
                "object_ref": refs[0] if len(refs) == 1 else "investigation_scope",
                "display_name": scope_display,
            },
            "observation_goal": f"核查{scope_display}的{metric}变化并验证后续业务日是否恢复",
            "display_text": action,
        }
    ]


def _build_user_narrative(
    writer_pack: dict[str, Any],
    writer_patch: dict[str, Any],
    *,
    findings: list[dict[str, Any]],
    recommendations: list[dict[str, Any]],
    metric: str,
) -> dict[str, Any]:
    invalid_reason = _narrative_invalid_reason(writer_patch)
    supplied = writer_patch.get("finding_texts", {})
    missing_ids = [
        item["candidate_id"]
        for item in findings
        if not isinstance(supplied.get(item["candidate_id"]), str)
        or not supplied[item["candidate_id"]].strip()
    ]
    if invalid_reason is None:
        fallback_findings = {
            item["candidate_id"]: _fallback_finding(item, metric)
            for item in findings
            if item["candidate_id"] in missing_ids
        }
        finding_texts = {
            item["candidate_id"]: (
                fallback_findings[item["candidate_id"]]
                if item["candidate_id"] in fallback_findings
                else supplied[item["candidate_id"]].strip()
            )
            for item in findings
        }
        for finding in findings:
            finding["narrative_text"] = finding_texts[finding["candidate_id"]]
        narrative = _base_narrative(
            writer_patch,
            fallback_status="partial" if missing_ids else "not_used",
            finding_texts=finding_texts,
        )
        if missing_ids:
            narrative["fallback_reason"] = "missing_finding_texts"
            narrative["fallback_candidate_ids"] = missing_ids
        narrative["recommendations"] = recommendations
        return narrative

    fallback_summary = _fallback_summary(writer_pack, metric, findings)
    fallback_findings = {
        item["candidate_id"]: _fallback_finding(item, metric) for item in findings
    }
    fallback_action = _fallback_action(findings, metric)
    fallback_recommendations = deepcopy(recommendations)
    for item in fallback_recommendations:
        item["display_text"] = fallback_action
    for finding in findings:
        finding["narrative_text"] = fallback_findings[finding["candidate_id"]]
    return {
        "schema_version": NARRATIVE_SCHEMA_VERSION,
        "summary": fallback_summary,
        "finding_texts": fallback_findings,
        "evidence_limits": _channel_neutral_evidence_limits(writer_patch),
        "recommended_action": fallback_action,
        "fallback_status": "used",
        "fallback_reason": invalid_reason,
        "recommendations": fallback_recommendations,
    }


def _base_narrative(
    writer_patch: dict[str, Any],
    *,
    fallback_status: str,
    finding_texts: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": NARRATIVE_SCHEMA_VERSION,
        "summary": _required_text(writer_patch.get("summary"), "summary"),
        "finding_texts": deepcopy(
            writer_patch.get("finding_texts", {})
            if finding_texts is None
            else finding_texts
        ),
        "evidence_limits": _channel_neutral_evidence_limits(writer_patch),
        "recommended_action": _required_text(
            writer_patch.get("recommended_action"), "recommended_action"
        ),
        "fallback_status": fallback_status,
    }


def _machine_result_narrative(
    writer_patch: dict[str, Any], *, fallback_summary: str, fallback_action: str
) -> dict[str, Any]:
    invalid_reason = _narrative_invalid_reason(writer_patch)
    if invalid_reason is None:
        return _base_narrative(writer_patch, fallback_status="not_used")
    return {
        "schema_version": NARRATIVE_SCHEMA_VERSION,
        "summary": fallback_summary,
        "finding_texts": {},
        "evidence_limits": _channel_neutral_evidence_limits(writer_patch),
        "recommended_action": fallback_action,
        "fallback_status": "used",
        "fallback_reason": invalid_reason,
    }


def _narrative_invalid_reason(writer_patch: dict[str, Any]) -> str | None:
    texts: list[str] = []
    for field in ("summary", "recommended_action"):
        value = writer_patch.get(field)
        if isinstance(value, str):
            texts.append(value)
    finding_texts = writer_patch.get("finding_texts")
    if isinstance(finding_texts, dict):
        texts.extend(value for value in finding_texts.values() if isinstance(value, str))
    evidence_limits = writer_patch.get("evidence_limits")
    if isinstance(evidence_limits, list):
        texts.extend(value for value in evidence_limits if isinstance(value, str))
    combined = "\n".join(texts)
    if any(term in combined for term in _CHANNEL_TERMS):
        return "channel_specific_language"
    if any(term in combined for term in _VAGUE_ACTIONS):
        return "generic_action_without_scope"
    return None


def _channel_neutral_evidence_limits(writer_patch: dict[str, Any]) -> list[str]:
    values = writer_patch.get("evidence_limits", [])
    if not isinstance(values, list):
        return []
    return [
        value
        for value in values
        if isinstance(value, str)
        and value.strip()
        and not any(term in value for term in _CHANNEL_TERMS)
    ]


def _fallback_summary(
    writer_pack: dict[str, Any], metric: str, findings: list[dict[str, Any]]
) -> str:
    if findings:
        names = "、".join(str(item["object"]["display_name"]) for item in findings[:3])
        return f"{metric}相对前 7 日加权基线发生不利变化，达到门槛的范围包括{names}。"
    return f"{metric}已完成规定检查，当前没有达到候选门槛的子维度。"


def _fallback_finding(finding: dict[str, Any], metric: str) -> str:
    label = finding["object"]["display_name"]
    dimension = finding["dimension"]["display_name"]
    return f"{dimension}「{label}」的{metric}变化达到候选门槛，建议按结构化影响值确定排查优先级。"


def _fallback_action(findings: list[dict[str, Any]], metric: str) -> str:
    if findings:
        names = "、".join(str(item["object"]["display_name"]) for item in findings[:3])
        return f"优先核查{names}的{metric}链路，并验证后续业务日是否恢复。"
    return f"继续跟踪{metric}，并验证后续业务日是否离开告警区间。"


def _machine_audit_codes(writer_pack: dict[str, Any]) -> list[str]:
    values = writer_pack.get("evidence_limits", [])
    if not isinstance(values, list):
        raise AnalysisV5Error("writer_pack.evidence_limits is invalid")
    return list(
        dict.fromkeys(
            item.strip()
            for item in values
            if isinstance(item, str) and item.strip()
        )
    )


def _step_status(
    status: Any, candidate_count: Any, *, has_signal: bool = False
) -> str:
    if status == "succeeded":
        return (
            "signal_found"
            if has_signal or isinstance(candidate_count, int) and candidate_count > 0
            else "no_signal"
        )
    if status in {"skipped_by_policy", "skipped_not_applicable"}:
        return "skipped_by_policy"
    if status == "failed":
        return "failed"
    if status == "query_blocked":
        return "blocked"
    raise AnalysisV5Error(f"unknown public step status: {status}")


def _step_result(status: str, candidate_count: Any) -> str:
    if status == "signal_found":
        return f"发现 {candidate_count} 个达到候选门槛的信号"
    if status == "no_signal":
        return "已检查但未发现达到候选门槛的信号"
    if status == "skipped_by_policy":
        return "按策略跳过"
    if status == "blocked":
        return "执行受阻"
    return "执行失败"


def _post_step_result(
    step_id: str,
    status: str,
    step: dict[str, Any],
    *,
    background_signals: list[dict[str, Any]],
    calibrations: list[dict[str, Any]],
) -> str:
    if step_id == "game_background" and background_signals:
        return f"保留 {len(background_signals)} 条通过校验的背景信号"
    if step_id == "counterfactual" and any(
        item["calibration_type"] == "counterfactual_removal"
        for item in calibrations
    ):
        return "已完成反事实校准"
    calibration_type = {
        "breadth_check": "breadth_check",
        "error_code": "error_code",
        "cross_dimension_overlap": "cross_dimension_overlap",
    }.get(step_id)
    if calibration_type is not None:
        count = sum(
            item["calibration_type"] == calibration_type for item in calibrations
        )
        if count:
            return f"保留 {count} 组通过校验的校准结果"
    return _step_result(status, step.get("candidate_count"))


def _post_step_has_signal(
    step_id: str,
    *,
    background_signals: list[dict[str, Any]],
    calibrations: list[dict[str, Any]],
) -> bool:
    if step_id == "game_background":
        return bool(background_signals)
    calibration_type = {
        "counterfactual": "counterfactual_removal",
        "breadth_check": "breadth_check",
        "error_code": "error_code",
        "cross_dimension_overlap": "cross_dimension_overlap",
    }.get(step_id)
    return calibration_type is not None and any(
        item["calibration_type"] == calibration_type for item in calibrations
    )


def _measure(
    *,
    measure_id: str,
    semantic_type: str,
    value: Any,
    unit: str,
    polarity: str,
    direction: str,
    comparable_group: str,
    additive: bool,
    display_precision: int,
    denominator: str | None = None,
) -> dict[str, Any]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AnalysisV5Error(f"{measure_id} value must be numeric")
    result: dict[str, Any] = {
        "measure_id": measure_id,
        "semantic_type": semantic_type,
        "value": float(value),
        "unit": unit,
        "polarity": polarity,
        "direction": direction,
        "comparable_group": comparable_group,
        "additive": additive,
        "display_precision": display_precision,
    }
    if denominator is not None:
        result["denominator"] = denominator
    return result


def _direction(delta: float) -> str:
    if delta > 0:
        return "increase"
    if delta < 0:
        return "decrease"
    return "unchanged"


def _absolute_threshold(rules: Any) -> float | None:
    if not isinstance(rules, list):
        return None
    values = [
        item.get("alert_threshold")
        for item in rules
        if isinstance(item, dict) and item.get("rule_kind") == "absolute_1d"
    ]
    if len(values) != 1 or isinstance(values[0], bool) or not isinstance(
        values[0], (int, float)
    ):
        return None
    return float(values[0])


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AnalysisV5Error(f"{field} must be a non-empty string")
    return value.strip()
