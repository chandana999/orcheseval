"""Required field / required output checks."""

from __future__ import annotations

from typing import Any

from app.evaluators.base import Evaluator, EvaluatorOutput, ExecutionContext
from app.models.enums import CheckType


def _dig(value: Any, path: str) -> tuple[bool, Any]:
    current = value
    for token in [t for t in path.split(".") if t]:
        if isinstance(current, dict):
            if token not in current:
                return False, None
            current = current[token]
        elif isinstance(current, list) and token.isdigit():
            index = int(token)
            if index >= len(current):
                return False, None
            current = current[index]
        else:
            return False, None
    return True, current


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict, tuple, set)):
        return len(value) == 0
    return False


class RequiredFieldsEvaluator(Evaluator):
    """Verify mapped values exist and are non-empty.

    Config params::

        {"fields": ["summary", "validator_result.is_valid"],
         "allow_empty": false}

    With no `fields`, every mapped key is required. Field names may address
    nested paths inside a mapped value using dots.
    """

    name = "required_fields"
    version = "1.0.0"
    check_type = CheckType.DETERMINISTIC

    def evaluate(
        self,
        evaluator_input: dict[str, Any],
        evaluator_config: dict[str, Any],
        execution_context: ExecutionContext,
    ) -> EvaluatorOutput:
        params = evaluator_config.get("params") or {}
        allow_empty = bool(params.get("allow_empty", False))
        fields = params.get("fields") or list(evaluator_input.keys())

        present: list[str] = []
        missing: list[dict[str, Any]] = []
        for field in fields:
            root, _, sub = str(field).partition(".")
            if root not in evaluator_input:
                missing.append({"field": field, "reason": "unmapped_or_unresolved"})
                continue
            found, value = _dig(evaluator_input[root], sub) if sub else (True, evaluator_input[root])
            if not found:
                missing.append({"field": field, "reason": "path_absent"})
            elif not allow_empty and _is_empty(value):
                missing.append({"field": field, "reason": "empty"})
            else:
                present.append(str(field))

        total = len(fields) or 1
        return self.outcome(
            passed=not missing,
            score=round(len(present) / total, 4),
            explanation=(
                f"all {len(present)} required field(s) present"
                if not missing
                else f"{len(missing)} required field(s) missing or empty"
            ),
            evidence={
                "checked_fields": [str(f) for f in fields],
                "present": present,
                "missing": missing,
                "allow_empty": allow_empty,
            },
        )
