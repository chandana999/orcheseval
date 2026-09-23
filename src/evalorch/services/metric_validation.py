"""Validation and normalization of one config metric into the frozen ticket definition.

The stored keys stay check_id and check_type because those are ticket columns.
The runner relies on an explicit evaluator, priority, max_attempts, and missing-context policy.
"""

from __future__ import annotations

from typing import Any

from evalorch.core.config import settings
from evalorch.evaluators.registry import (
    evaluator_check_type,
    evaluator_exists,
    resolve_evaluator_name,
)
from evalorch.models.enums import CheckType, OnMissingContext

RESERVED_DEFINITION_KEYS = frozenset(
    {
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
)


class MetricDefinitionError(ValueError):
    """Raised with a list of human readable problems."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def _normalize_policy(value: Any, default: str) -> str:
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {OnMissingContext.NOT_APPLICABLE.value, "na", "skip"}:
        return OnMissingContext.NOT_APPLICABLE.value
    if text in {OnMissingContext.FAIL.value, "failed", "error"}:
        return OnMissingContext.FAIL.value
    raise MetricDefinitionError(
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
                problems.append(f"{where}: input_mapping['{key}'] object requires a string 'path'")
                continue
            entry: dict[str, Any] = {"path": path.strip()}
            for flag in ("required", "nullable"):
                if flag in spec:
                    entry[flag] = bool(spec[flag])
            if "type" in spec:
                entry["type"] = str(spec["type"])
            if "default" in spec:
                entry["default"] = spec["default"]
            normalized[key] = entry
        else:
            problems.append(f"{where}: input_mapping['{key}'] must be a path string or an object")
    return normalized


def normalize_metric_definition(
    raw: Any,
    *,
    where: str,
    defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize one metric definition, or raise MetricDefinitionError."""
    policy_default = (defaults or {}).get("on_missing_context", settings.default_on_missing_context)
    defaults = {
        "priority": (defaults or {}).get("priority", 0),
        "max_attempts": (defaults or {}).get("max_attempts", settings.runner_max_attempts),
        "on_missing_context": policy_default,
    }
    problems: list[str] = []

    if not isinstance(raw, dict):
        raise MetricDefinitionError([f"{where}: definition must be an object"])

    check_id = raw.get("check_id") or raw.get("id")
    if not isinstance(check_id, str) or not check_id.strip():
        raise MetricDefinitionError([f"{where}: check_id is required"])
    check_id = check_id.strip()

    evaluator = raw.get("evaluator") or raw.get("evaluator_type")
    declared_type = raw.get("check_type")

    if evaluator:
        evaluator_name = resolve_evaluator_name(str(evaluator))
        if not evaluator_exists(evaluator_name):
            raise MetricDefinitionError([f"{where}: unknown evaluator {evaluator!r}"])
    elif declared_type and str(declared_type).upper() == CheckType.LLM_JUDGE.value:
        evaluator_name = "llm_judge"
    else:
        raise MetricDefinitionError(
            [
                f"{where}: 'evaluator' is required for deterministic metrics "
                "(e.g. required_fields, workflow_order, tool_calls, span_exists)"
            ]
        )

    resolved_type = evaluator_check_type(evaluator_name)
    if declared_type:
        try:
            declared = CheckType(str(declared_type).upper())
        except ValueError as exc:
            raise MetricDefinitionError(
                [f"{where}: check_type must be DETERMINISTIC or LLM_JUDGE, got {declared_type!r}"]
            ) from exc
        if declared is not resolved_type:
            raise MetricDefinitionError(
                [
                    f"{where}: check_type {declared.value} conflicts with evaluator "
                    f"'{evaluator_name}' ({resolved_type.value})"
                ]
            )

    input_mapping = _validate_input_mapping(raw.get("input_mapping"), where, problems)

    params = raw.get("params")
    if params is None:
        # Allow evaluator parameters to be inlined next to the mapping.
        params = {k: v for k, v in raw.items() if k not in RESERVED_DEFINITION_KEYS}
    if not isinstance(params, dict):
        problems.append(f"{where}: params must be an object")
        params = {}

    try:
        priority = int(raw.get("priority", defaults["priority"]))
    except (TypeError, ValueError):
        problems.append(f"{where}: priority must be an integer")
        priority = 0

    try:
        max_attempts = int(raw.get("max_attempts", defaults["max_attempts"]))
    except (TypeError, ValueError):
        problems.append(f"{where}: max_attempts must be an integer")
        max_attempts = settings.runner_max_attempts
    if max_attempts < 1:
        problems.append(f"{where}: max_attempts must be >= 1")
        max_attempts = 1

    try:
        policy = _normalize_policy(raw.get("on_missing_context"), defaults["on_missing_context"])
    except MetricDefinitionError as exc:
        problems.append(f"{where}: {exc}")
        policy = defaults["on_missing_context"]

    applies_when = raw.get("applies_when")
    if applies_when is not None and not isinstance(applies_when, dict):
        problems.append(f"{where}: applies_when must be an object")
        applies_when = None

    if problems:
        raise MetricDefinitionError(problems)

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
