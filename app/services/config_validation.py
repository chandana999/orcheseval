"""Validation and normalization of evaluation configurations.

The normalized form is what gets stored in `config_json` and snapshotted onto a
job, so the runner can rely on every check having an explicit evaluator,
check_type, priority, max_attempts, and missing-context policy.
"""

from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.evaluators.registry import (
    evaluator_check_type,
    evaluator_exists,
    resolve_evaluator_name,
)
from app.models.enums import CheckType, OnMissingContext


class ConfigValidationError(ValueError):
    """Raised with a list of human readable problems."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value]
    return []


def _normalize_policy(value: Any, default: str) -> str:
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {OnMissingContext.NOT_APPLICABLE.value, "na", "skip"}:
        return OnMissingContext.NOT_APPLICABLE.value
    if text in {OnMissingContext.FAIL.value, "failed", "error"}:
        return OnMissingContext.FAIL.value
    raise ConfigValidationError(
        [f"on_missing_context must be 'not_applicable' or 'fail', got {value!r}"]
    )


def _validate_input_mapping(mapping: Any, where: str, problems: list[str]) -> dict[str, Any]:
    if mapping is None:
        return {}
    if not isinstance(mapping, dict):
        problems.append(f"{where}: input_mapping must be an object")
        return {}
    normalized: dict[str, Any] = {}
    for key, spec in mapping.items():
        if not isinstance(key, str) or not key:
            problems.append(f"{where}: input_mapping keys must be non-empty strings")
            continue
        if isinstance(spec, str):
            if not spec.strip():
                problems.append(f"{where}: input_mapping['{key}'] must be a non-empty path")
                continue
            normalized[key] = spec.strip()
        elif isinstance(spec, dict):
            path = spec.get("path")
            if not isinstance(path, str) or not path.strip():
                problems.append(
                    f"{where}: input_mapping['{key}'] object requires a string 'path'"
                )
                continue
            entry: dict[str, Any] = {"path": path.strip()}
            if "required" in spec:
                entry["required"] = bool(spec["required"])
            if "nullable" in spec:
                entry["nullable"] = bool(spec["nullable"])
            if "type" in spec:
                entry["type"] = str(spec["type"])
            if "default" in spec:
                entry["default"] = spec["default"]
            normalized[key] = entry
        else:
            problems.append(
                f"{where}: input_mapping['{key}'] must be a path string or an object"
            )
    return normalized


def normalize_check(raw: Any, index: int, defaults: dict[str, Any], problems: list[str]) -> dict[str, Any] | None:
    where = f"checks[{index}]"
    if not isinstance(raw, dict):
        problems.append(f"{where}: must be an object")
        return None

    check_id = raw.get("check_id") or raw.get("id")
    if not isinstance(check_id, str) or not check_id.strip():
        problems.append(f"{where}: check_id is required")
        return None
    check_id = check_id.strip()

    evaluator = raw.get("evaluator") or raw.get("evaluator_type")
    declared_type = raw.get("check_type")

    if evaluator:
        evaluator_name = resolve_evaluator_name(str(evaluator))
        if not evaluator_exists(evaluator_name):
            problems.append(f"{where}: unknown evaluator {evaluator!r}")
            return None
    elif declared_type and str(declared_type).upper() == CheckType.LLM_JUDGE.value:
        evaluator_name = "llm_judge"
    else:
        problems.append(
            f"{where}: 'evaluator' is required for deterministic checks "
            "(e.g. required_fields, json_schema, workflow_order)"
        )
        return None

    resolved_type = evaluator_check_type(evaluator_name)
    if declared_type:
        try:
            declared = CheckType(str(declared_type).upper())
        except ValueError:
            problems.append(
                f"{where}: check_type must be DETERMINISTIC or LLM_JUDGE, got {declared_type!r}"
            )
            return None
        if declared is not resolved_type:
            problems.append(
                f"{where}: check_type {declared.value} conflicts with evaluator "
                f"'{evaluator_name}' ({resolved_type.value})"
            )
            return None

    input_mapping = _validate_input_mapping(raw.get("input_mapping"), where, problems)

    params = raw.get("params")
    if params is None:
        # Allow evaluator parameters to be inlined next to the mapping.
        reserved = {
            "check_id",
            "id",
            "check_type",
            "evaluator",
            "evaluator_type",
            "input_mapping",
            "priority",
            "max_attempts",
            "on_missing_context",
            "applies_when",
            "description",
            "params",
        }
        params = {k: v for k, v in raw.items() if k not in reserved}
    if not isinstance(params, dict):
        problems.append(f"{where}: params must be an object")
        params = {}

    try:
        priority = int(raw.get("priority", defaults.get("priority", 0)))
    except (TypeError, ValueError):
        problems.append(f"{where}: priority must be an integer")
        priority = 0

    try:
        max_attempts = int(raw.get("max_attempts", defaults.get("max_attempts", settings.runner_max_attempts)))
    except (TypeError, ValueError):
        problems.append(f"{where}: max_attempts must be an integer")
        max_attempts = settings.runner_max_attempts
    if max_attempts < 1:
        problems.append(f"{where}: max_attempts must be >= 1")
        max_attempts = 1

    try:
        policy = _normalize_policy(
            raw.get("on_missing_context"), defaults.get("on_missing_context")
        )
    except ConfigValidationError as exc:
        problems.append(f"{where}: {exc}")
        policy = defaults.get("on_missing_context")

    applies_when = raw.get("applies_when")
    if applies_when is not None and not isinstance(applies_when, dict):
        problems.append(f"{where}: applies_when must be an object")
        applies_when = None

    normalized = {
        "check_id": check_id,
        "check_type": resolved_type.value,
        "evaluator": evaluator_name,
        "description": raw.get("description"),
        "input_mapping": input_mapping,
        "params": params,
        "priority": priority,
        "max_attempts": max_attempts,
        "on_missing_context": policy,
    }
    if applies_when:
        normalized["applies_when"] = applies_when
    return normalized


def validate_and_normalize_config(config: Any) -> dict[str, Any]:
    """Validate a configuration document and return its normalized form."""
    problems: list[str] = []
    if not isinstance(config, dict):
        raise ConfigValidationError(["configuration must be a JSON object"])

    default_policy = _normalize_policy(
        config.get("on_missing_context"), settings.default_on_missing_context
    )
    raw_defaults = config.get("defaults") if isinstance(config.get("defaults"), dict) else {}
    defaults = {
        "priority": raw_defaults.get("priority", 0),
        "max_attempts": raw_defaults.get("max_attempts", settings.runner_max_attempts),
        "on_missing_context": _normalize_policy(
            raw_defaults.get("on_missing_context"), default_policy
        ),
    }
    if "timeout_seconds" in raw_defaults:
        defaults["timeout_seconds"] = raw_defaults["timeout_seconds"]

    raw_checks = config.get("checks")
    if not isinstance(raw_checks, list) or not raw_checks:
        problems.append("configuration must define a non-empty 'checks' array")
        raw_checks = []

    checks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_check in enumerate(raw_checks):
        normalized = normalize_check(raw_check, index, defaults, problems)
        if normalized is None:
            continue
        if normalized["check_id"] in seen:
            problems.append(f"duplicate check_id '{normalized['check_id']}'")
            continue
        seen.add(normalized["check_id"])
        checks.append(normalized)

    model = config.get("model")
    if model is not None and not isinstance(model, dict):
        problems.append("model must be an object")
        model = None

    sampling = config.get("sampling")
    if sampling is not None and not isinstance(sampling, dict):
        problems.append("sampling must be an object")
        sampling = None
    max_payloads = None
    if isinstance(sampling, dict) and sampling.get("max_payloads") is not None:
        try:
            max_payloads = int(sampling["max_payloads"])
            if max_payloads < 1:
                problems.append("sampling.max_payloads must be >= 1")
        except (TypeError, ValueError):
            problems.append("sampling.max_payloads must be an integer")

    if problems:
        raise ConfigValidationError(problems)

    normalized_config: dict[str, Any] = {
        "evaluation_type": config.get("evaluation_type", "agent_workflow"),
        "evaluation_version": str(config.get("evaluation_version", "1")),
        "target_agents": _as_str_list(config.get("target_agents")),
        "workflow_order": _as_str_list(config.get("workflow_order")),
        "on_missing_context": default_policy,
        "defaults": defaults,
        "checks": checks,
    }
    if model:
        normalized_config["model"] = model
    if sampling:
        normalized_config["sampling"] = {**sampling}
        if max_payloads is not None:
            normalized_config["sampling"]["max_payloads"] = max_payloads
    for passthrough in ("description", "metadata", "timeout_seconds", "retry"):
        if passthrough in config:
            normalized_config[passthrough] = config[passthrough]
    return normalized_config


def checks_for_job(config_snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    checks = config_snapshot.get("checks")
    return list(checks) if isinstance(checks, list) else []


def find_check(config_snapshot: dict[str, Any], check_id: str) -> dict[str, Any] | None:
    for check in checks_for_job(config_snapshot):
        if check.get("check_id") == check_id:
            return check
    return None
