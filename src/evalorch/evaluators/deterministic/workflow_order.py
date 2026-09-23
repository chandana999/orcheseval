"""Workflow order and agent span existence checks."""

from __future__ import annotations

from typing import Any

from evalorch.evaluators.base import Evaluator, EvaluatorOutput, ExecutionContext
from evalorch.models.enums import CheckType
from evalorch.services.context_resolver import SPAN_IDENTITY_KEYS, span_has_identity
from evalorch.services.errors import InvalidEvaluatorConfigError


def span_identities(spans: list[Any]) -> list[str]:
    identities: list[str] = []
    for span in spans:
        if not isinstance(span, dict):
            continue
        for key in SPAN_IDENTITY_KEYS:
            if span.get(key):
                identities.append(str(span[key]))
                break
    return identities


def _coerce_spans(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [s for s in value if isinstance(s, dict)]
    if isinstance(value, dict):
        spans = []
        for name, span in value.items():
            if isinstance(span, dict):
                merged = dict(span)
                merged.setdefault("agent_id", str(name))
                spans.append(merged)
        return spans
    raise InvalidEvaluatorConfigError(
        "workflow checks expect the mapped 'spans' value to be a list or object of spans"
    )


class WorkflowOrderEvaluator(Evaluator):
    """Verify agents executed in the configured order.

    Mapping must provide `spans` (usually `"spans": "spans"`).

    Config params::

        {"expected_order": ["classifier", "validator", "summarizer"],
         "mode": "subsequence",      # or "strict"
         "ignore_missing": false}

    `subsequence` requires the expected agents to appear in relative order while
    tolerating extra spans; `strict` requires an exact positional match.
    """

    name = "workflow_order"
    version = "1.0.0"
    check_type = CheckType.DETERMINISTIC

    def evaluate(
        self,
        evaluator_input: dict[str, Any],
        evaluator_config: dict[str, Any],
        execution_context: ExecutionContext,
    ) -> EvaluatorOutput:
        params = evaluator_config.get("params") or {}
        expected = params.get("expected_order")
        if not expected:
            snapshot = execution_context.config_snapshot or {}
            expected = snapshot.get("workflow_order")
        if not expected or not isinstance(expected, list):
            raise InvalidEvaluatorConfigError(
                "workflow_order check requires params.expected_order (or a "
                "workflow_order list on the configuration)"
            )

        mode = str(params.get("mode", "subsequence")).lower()
        if mode not in {"subsequence", "strict"}:
            raise InvalidEvaluatorConfigError("workflow_order mode must be subsequence or strict")
        ignore_missing = bool(params.get("ignore_missing", False))

        spans = _coerce_spans(self.require(evaluator_input, "spans"))
        actual = span_identities(spans)

        expected_names = [str(name) for name in expected]
        present = [name for name in expected_names if any(span_has_identity(s, name) for s in spans)]
        missing = [name for name in expected_names if name not in present]

        comparable = present if ignore_missing else expected_names
        if mode == "strict":
            order_ok = actual[: len(comparable)] == comparable and not missing
            detail = "strict positional match"
        else:
            cursor = 0
            order_ok = True
            for name in comparable:
                while cursor < len(actual) and actual[cursor] != name:
                    cursor += 1
                if cursor == len(actual):
                    order_ok = False
                    break
                cursor += 1
            detail = "relative order match"

        passed = order_ok and (ignore_missing or not missing)
        score = round(len(present) / max(len(expected_names), 1), 4) if not passed else 1.0

        if missing and not ignore_missing:
            explanation = f"missing agent span(s): {', '.join(missing)}"
        elif not order_ok:
            explanation = f"agents ran out of order ({detail} failed)"
        else:
            explanation = f"workflow order satisfied ({detail})"

        return self.outcome(
            passed=passed,
            score=score,
            explanation=explanation,
            evidence={
                "expected_order": expected_names,
                "actual_order": actual,
                "missing_agents": missing,
                "mode": mode,
                "ignore_missing": ignore_missing,
            },
        )


class SpanExistsEvaluator(Evaluator):
    """Verify required agent spans exist in the payload.

    Mapping must provide `spans`. Config params::

        {"required_spans": ["classifier", "summarizer"]}
    """

    name = "span_exists"
    version = "1.0.0"
    check_type = CheckType.DETERMINISTIC

    def evaluate(
        self,
        evaluator_input: dict[str, Any],
        evaluator_config: dict[str, Any],
        execution_context: ExecutionContext,
    ) -> EvaluatorOutput:
        params = evaluator_config.get("params") or {}
        required = params.get("required_spans") or params.get("required_agents")
        if not required:
            snapshot = execution_context.config_snapshot or {}
            required = snapshot.get("target_agents")
        if not required or not isinstance(required, list):
            raise InvalidEvaluatorConfigError(
                "span_exists check requires params.required_spans (or target_agents "
                "on the configuration)"
            )

        spans = _coerce_spans(self.require(evaluator_input, "spans"))
        wanted = [str(name) for name in required]
        found = [name for name in wanted if any(span_has_identity(s, name) for s in spans)]
        missing = [name for name in wanted if name not in found]

        return self.outcome(
            passed=not missing,
            score=round(len(found) / max(len(wanted), 1), 4),
            explanation=(
                f"all {len(found)} required span(s) present"
                if not missing
                else f"missing span(s): {', '.join(missing)}"
            ),
            evidence={
                "required_spans": wanted,
                "found_spans": found,
                "missing_spans": missing,
                "available_spans": span_identities(spans),
            },
        )
