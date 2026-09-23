"""Match a payload to checks in the dataset config file.

The dataset folder holds payload JSON files plus config.json. The config
document identifies one agent with agentId and lists metrics. A payload is
matched when payload.agent_registry.agent_id equals that agentId. The matched
metrics are frozen onto each ticket through the existing check definition.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from evalorch.core.config import settings
from evalorch.evaluators.registry import evaluator_exists, resolve_evaluator_name
from evalorch.models.enums import CheckType
from evalorch.services.metric_validation import MetricDefinitionError, normalize_metric_definition
from evalorch.services.payload_validation import is_uuid


class ProfileResolutionError(ValueError):
    status_code = 400


class ProfileNotFoundError(ProfileResolutionError):
    status_code = 404


def extract_agent_id(payload: dict[str, Any], *, source: str) -> str:
    """Read payload['agent_registry']['agent_id']."""
    registry = payload.get("agent_registry")
    if registry is None:
        raise ProfileResolutionError(
            f"{source}: missing agent_registry; expected payload.agent_registry.agent_id"
        )
    if not isinstance(registry, dict):
        raise ProfileResolutionError(
            f"{source}: agent_registry must be an object, got {type(registry).__name__}"
        )
    agent_id = registry.get("agent_id")
    if agent_id is None or (isinstance(agent_id, str) and not agent_id.strip()):
        raise ProfileResolutionError(
            f"{source}: agent_registry.agent_id is required and must be non-empty"
        )
    if not isinstance(agent_id, str):
        raise ProfileResolutionError(f"{source}: agent_registry.agent_id must be a string")
    return agent_id.strip()


def freeze_metric(check: dict[str, Any], *, agent_id: str) -> dict[str, Any]:
    """Normalize one config check and freeze it for a ticket."""
    defaults = {
        "priority": 0,
        "max_attempts": settings.runner_max_attempts,
        "on_missing_context": settings.default_on_missing_context,
    }
    check_id = str(check.get("check_id") or check.get("id") or "").strip() or "check"
    try:
        normalized = normalize_metric_definition(
            dict(check),
            where=f"config.json agent {agent_id} check {check_id}",
            defaults=defaults,
        )
    except MetricDefinitionError as exc:
        raise ProfileResolutionError(
            f"config.json agent {agent_id!r} check {check_id!r} is invalid: {exc}"
        ) from exc

    metric_record_id = uuid.uuid5(
        uuid.NAMESPACE_URL, f"eval-platform:{agent_id}:{normalized['check_id']}"
    )
    snapshot = {
        "metric_record_id": str(metric_record_id),
        "metric_id": normalized["check_id"],
        "metric_code": normalized["check_id"],
        "metric_name": normalized["check_id"],
        "metric_desc": normalized.get("description"),
        "metric_type": normalized["check_type"],
        # Placeholder until the caller stores the normalized metricVersion.
        "metric_version_number": 1,
        "definition_payload": normalized,
        "agent_id": agent_id,
        "frozen_at": datetime.now(timezone.utc).isoformat(),
    }
    if normalized["check_type"] == CheckType.LLM_JUDGE.value:
        snapshot["model"] = {
            "model": settings.judge_model,
            "provider": settings.default_provider,
        }
    return snapshot


def _version_number(value: Any, *, source: str, metric_id: str) -> int:
    text = str(value).strip()
    head = text.split(".", 1)[0]
    try:
        number = int(head)
    except ValueError as exc:
        raise ProfileResolutionError(
            f"{source}: metric {metric_id!r} metricVersion must start with an integer, got {value!r}"
        ) from exc
    if number < 1:
        raise ProfileResolutionError(
            f"{source}: metric {metric_id!r} metricVersion must be >= 1"
        )
    return number


def _number(value: Any, *, source: str, metric_id: str, field: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProfileResolutionError(
            f"{source}: metric {metric_id!r} {field} must be a number"
        )
    return value


def _metric_definition(metric: dict[str, Any], *, source: str) -> dict[str, Any]:
    metric_id = str(metric.get("metricId") or "").strip()
    where = f"{source}: metric {metric_id or '<missing>'}"
    for field in (
        "metricId",
        "metricName",
        "metricVersion",
        "metricType",
        "evaluatorPluginId",
    ):
        value = metric.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ProfileResolutionError(f"{where}: {field} is required and must be a non-empty string")
    if not isinstance(metric.get("enabled"), bool):
        raise ProfileResolutionError(f"{where}: enabled must be true or false")
    span_names = metric.get("spanNames")
    if not isinstance(span_names, list) or any(
        not isinstance(name, str) or not name.strip() for name in span_names
    ):
        raise ProfileResolutionError(f"{where}: spanNames must be an array of non-empty strings")
    thresholds = metric.get("thresholds")
    if not isinstance(thresholds, dict) or "pass" not in thresholds or "fail" not in thresholds:
        raise ProfileResolutionError(f"{where}: thresholds must include pass and fail")
    _number(thresholds["pass"], source=source, metric_id=metric_id, field="thresholds.pass")
    _number(thresholds["fail"], source=source, metric_id=metric_id, field="thresholds.fail")
    weight = _number(metric.get("weight"), source=source, metric_id=metric_id, field="weight")
    mapping = metric.get("inputMapping")
    if not isinstance(mapping, dict):
        raise ProfileResolutionError(f"{where}: inputMapping must be an object")
    params = metric.get("params")
    if not isinstance(params, dict):
        raise ProfileResolutionError(f"{where}: params must be an object")

    metric_type = str(metric["metricType"]).strip().lower()
    plugin = resolve_evaluator_name(str(metric["evaluatorPluginId"]))
    if metric_type in {"llm_judge", "llm"}:
        evaluator = "llm_judge"
        check_type = CheckType.LLM_JUDGE.value
    elif metric_type == "deterministic":
        if not evaluator_exists(plugin):
            raise ProfileResolutionError(
                f"{where}: evaluatorPluginId {metric['evaluatorPluginId']!r} is not a known evaluator"
            )
        evaluator = plugin
        check_type = None
    elif evaluator_exists(resolve_evaluator_name(metric_type)):
        evaluator = resolve_evaluator_name(metric_type)
        check_type = None
    else:
        raise ProfileResolutionError(
            f"{where}: metricType must be llm_judge or deterministic, got {metric['metricType']!r}"
        )

    translated_params = dict(params)
    if "pass_threshold" not in translated_params:
        translated_params["pass_threshold"] = thresholds["pass"]
    check: dict[str, Any] = {
        "check_id": metric_id,
        "evaluator": evaluator,
        "description": str(metric["metricName"]).strip(),
        "input_mapping": mapping,
        "params": translated_params,
        "priority": int(weight),
    }
    if check_type is not None:
        check["check_type"] = check_type
    if span_names:
        check["applies_when"] = {"required_spans": [name.strip() for name in span_names]}
    if "on_missing_context" in metric:
        check["on_missing_context"] = metric["on_missing_context"]
    if "max_attempts" in metric:
        check["max_attempts"] = metric["max_attempts"]
    return check


def _metrics_from_config(
    config: dict[str, Any], agent_id: str, *, source: str
) -> list[dict[str, Any]]:
    raw_agent_id = config.get("agentId")
    if not isinstance(raw_agent_id, str) or not is_uuid(raw_agent_id):
        raise ProfileResolutionError(f"{source}: config.json agentId must be a UUID")
    configured = raw_agent_id.strip()
    if not is_uuid(agent_id):
        raise ProfileResolutionError(f"{source}: agent_id must be a UUID, got {agent_id!r}")
    if configured != agent_id.strip():
        raise ProfileNotFoundError(
            f"{source}: agent_id {agent_id!r} was not found in config.json"
        )

    metrics = config.get("metrics")
    if not isinstance(metrics, list) or not metrics:
        raise ProfileResolutionError(f"{source}: config.json must contain a non-empty metrics list")

    snapshots: list[dict[str, Any]] = []
    seen_checks: set[str] = set()
    for raw in metrics:
        if not isinstance(raw, dict):
            raise ProfileResolutionError(f"{source}: each config.json metrics entry must be an object")
        if raw.get("enabled") is False:
            continue
        check = _metric_definition(raw, source=source)
        frozen = freeze_metric(check, agent_id=configured)
        check_id = frozen["definition_payload"]["check_id"]
        if check_id in seen_checks:
            raise ProfileResolutionError(
                f"{source}: duplicate metricId {check_id!r} for agent {configured!r}"
            )
        seen_checks.add(check_id)
        version = _version_number(raw["metricVersion"], source=source, metric_id=check_id)
        frozen["metric_name"] = str(raw["metricName"]).strip()
        frozen["metric_desc"] = str(raw["metricName"]).strip()
        frozen["metric_version_number"] = version
        definition = frozen["definition_payload"]
        definition["evaluator_plugin_id"] = str(raw["evaluatorPluginId"]).strip()
        definition["span_names"] = [name.strip() for name in raw["spanNames"]]
        definition["thresholds"] = {
            "pass": raw["thresholds"]["pass"],
            "fail": raw["thresholds"]["fail"],
        }
        definition["weight"] = raw["weight"]
        definition["metric_version"] = str(raw["metricVersion"]).strip()
        snapshots.append(frozen)

    if not snapshots:
        raise ProfileResolutionError(f"{source}: config.json agent {configured!r} has no enabled metrics")
    return snapshots


def metrics_for_agent(
    config: dict[str, Any], agent_id: str, *, source: str
) -> list[dict[str, Any]]:
    """Return frozen metrics for the config.json agentId that matches this payload."""
    if "agents" in config or "checks" in config:
        raise ProfileResolutionError(
            f"{source}: config.json agents/checks is not supported; use agentId and metrics"
        )
    return _metrics_from_config(config, agent_id, source=source)


def metric_from_snapshot(metric_snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """Return the frozen check definition stored on a ticket."""
    if not metric_snapshot:
        return None
    definition = metric_snapshot.get("definition_payload")
    if isinstance(definition, dict) and definition.get("check_id"):
        return definition
    if metric_snapshot.get("check_id"):
        return metric_snapshot
    return None
