"""Evaluator registry: check configuration -> evaluator implementation."""

from __future__ import annotations

from app.evaluators.base import Evaluator
from app.evaluators.deterministic import (
    CrossSpanConsistencyEvaluator,
    FieldComparisonEvaluator,
    JsonSchemaEvaluator,
    RequiredFieldsEvaluator,
    SpanExistsEvaluator,
    ToolCallEvaluator,
    WorkflowOrderEvaluator,
)
from app.evaluators.llm import LLMJudgeEvaluator
from app.models.enums import CheckType
from app.services.errors import UnknownEvaluatorError

_EVALUATOR_CLASSES: list[type[Evaluator]] = [
    RequiredFieldsEvaluator,
    JsonSchemaEvaluator,
    WorkflowOrderEvaluator,
    SpanExistsEvaluator,
    ToolCallEvaluator,
    CrossSpanConsistencyEvaluator,
    FieldComparisonEvaluator,
    LLMJudgeEvaluator,
]

EVALUATORS: dict[str, type[Evaluator]] = {cls.name: cls for cls in _EVALUATOR_CLASSES}

# Aliases keep configuration readable and tolerate the obvious spellings.
ALIASES: dict[str, str] = {
    "json_schema_validation": "json_schema",
    "schema": "json_schema",
    "output_structure": "json_schema",
    "required_field": "required_fields",
    "required_output": "required_fields",
    "workflow": "workflow_order",
    "agent_span_exists": "span_exists",
    "span_existence": "span_exists",
    "tool_call": "tool_calls",
    "tool_call_exists": "tool_calls",
    "tool_response_exists": "tool_calls",
    "consistency": "cross_span_consistency",
    "field_compare": "field_comparison",
    "llm": "llm_judge",
    "judge": "llm_judge",
}


def resolve_evaluator_name(name: str) -> str:
    key = str(name or "").strip().lower()
    return ALIASES.get(key, key)


def get_evaluator(name: str) -> Evaluator:
    resolved = resolve_evaluator_name(name)
    evaluator_class = EVALUATORS.get(resolved)
    if evaluator_class is None:
        raise UnknownEvaluatorError(
            f"unknown evaluator '{name}'; available: {sorted(EVALUATORS)}"
        )
    return evaluator_class()


def evaluator_exists(name: str) -> bool:
    return resolve_evaluator_name(name) in EVALUATORS


def default_evaluator_for(check_type: CheckType) -> str:
    return "llm_judge" if check_type is CheckType.LLM_JUDGE else "required_fields"


def evaluator_check_type(name: str) -> CheckType:
    resolved = resolve_evaluator_name(name)
    evaluator_class = EVALUATORS.get(resolved)
    if evaluator_class is None:
        raise UnknownEvaluatorError(f"unknown evaluator '{name}'")
    return evaluator_class.check_type


def describe_evaluators() -> list[dict[str, str]]:
    return [
        {
            "evaluator": cls.name,
            "version": cls.version,
            "check_type": cls.check_type.value,
            "description": (cls.__doc__ or "").strip().split("\n")[0],
        }
        for cls in _EVALUATOR_CLASSES
    ]
