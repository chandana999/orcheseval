"""Cross-span consistency and configured field comparison."""

from __future__ import annotations

from typing import Any

from app.evaluators import scoring
from app.evaluators.base import Evaluator, EvaluatorOutput, ExecutionContext
from app.evaluators.verifiers import run_custom_verifier
from app.models.enums import CheckType
from app.services.errors import InvalidEvaluatorConfigError


def _dig(value: Any, path: str | None) -> tuple[bool, Any]:
    if not path:
        return True, value
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


class CrossSpanConsistencyEvaluator(Evaluator):
    """Compare values that came from different spans.

    Config params::

        {"comparisons": [
            {"left": "classifier_category", "right": "summary_category",
             "left_path": "category", "right_path": "category",
             "method": "exact", "threshold": 0.85, "label": "category agreement"}
         ],
         "require_all": true}

    `left`/`right` are mapping keys; `left_path`/`right_path` optionally address a
    field inside those mapped values.
    """

    name = "cross_span_consistency"
    version = "1.0.0"
    check_type = CheckType.DETERMINISTIC

    def evaluate(
        self,
        evaluator_input: dict[str, Any],
        evaluator_config: dict[str, Any],
        execution_context: ExecutionContext,
    ) -> EvaluatorOutput:
        params = evaluator_config.get("params") or {}
        comparisons = params.get("comparisons")
        if not comparisons or not isinstance(comparisons, list):
            raise InvalidEvaluatorConfigError(
                "cross_span_consistency check requires a params.comparisons list"
            )
        require_all = bool(params.get("require_all", True))

        results: list[dict[str, Any]] = []
        for index, spec in enumerate(comparisons):
            if not isinstance(spec, dict):
                raise InvalidEvaluatorConfigError(
                    f"comparison #{index} must be an object"
                )
            left_key = spec.get("left")
            right_key = spec.get("right")
            if not left_key or not right_key:
                raise InvalidEvaluatorConfigError(
                    f"comparison #{index} requires 'left' and 'right' mapping keys"
                )
            method = str(spec.get("method", "exact"))
            threshold = spec.get("threshold")

            left_found, left_value = _dig(
                self.require(evaluator_input, str(left_key)), spec.get("left_path")
            )
            right_found, right_value = _dig(
                self.require(evaluator_input, str(right_key)), spec.get("right_path")
            )

            entry: dict[str, Any] = {
                "label": spec.get("label") or f"{left_key} vs {right_key}",
                "left": left_key,
                "right": right_key,
                "left_path": spec.get("left_path"),
                "right_path": spec.get("right_path"),
                "method": method,
            }

            if not left_found or not right_found:
                entry.update(
                    passed=False,
                    score=0.0,
                    reason="path_absent",
                    left_present=left_found,
                    right_present=right_found,
                )
                results.append(entry)
                continue

            score_result = scoring.compare(
                method,
                right_value if method == "contains" else left_value,
                left_value if method == "contains" else right_value,
                threshold=float(threshold) if threshold is not None else None,
            )
            entry.update(
                passed=score_result.passed,
                score=score_result.score,
                left_value=_truncate(left_value),
                right_value=_truncate(right_value),
                details=score_result.details,
            )
            results.append(entry)

        passed_count = sum(1 for r in results if r["passed"])
        passed = passed_count == len(results) if require_all else passed_count > 0
        avg_score = round(sum(r["score"] for r in results) / max(len(results), 1), 4)
        failures = [r["label"] for r in results if not r["passed"]]

        return self.outcome(
            passed=passed,
            score=avg_score,
            explanation=(
                f"all {len(results)} cross-span comparison(s) consistent"
                if passed
                else f"inconsistent: {', '.join(failures)}"
            ),
            evidence={"comparisons": results, "require_all": require_all},
        )


class FieldComparisonEvaluator(Evaluator):
    """Compare a mapped actual value against a mapped or literal expected value.

    Reuses the exact/fuzzy/semantic scoring and the custom verifiers ported from
    evalforge-local.

    Config params::

        {"actual": "final_response", "expected": "expected_answer",
         "expected_value": "literal alternative to a mapping key",
         "method": "fuzzy", "threshold": 0.85,
         "verifier": {"type": "contains_all", "contains": ["refund"]}}
    """

    name = "field_comparison"
    version = "1.0.0"
    check_type = CheckType.DETERMINISTIC

    def evaluate(
        self,
        evaluator_input: dict[str, Any],
        evaluator_config: dict[str, Any],
        execution_context: ExecutionContext,
    ) -> EvaluatorOutput:
        params = evaluator_config.get("params") or {}
        actual_key = str(params.get("actual", "actual"))
        actual_value = self.require(evaluator_input, actual_key)

        verifier_config = params.get("verifier")
        if verifier_config:
            expected_text = params.get("expected_value")
            if expected_text is None and params.get("expected"):
                expected_text = evaluator_input.get(str(params["expected"]))
            result = run_custom_verifier(
                expected=scoring._stringify(expected_text),
                actual=scoring._stringify(actual_value),
                verifier_config=verifier_config,
            )
            return self.outcome(
                passed=result.passed,
                score=result.score,
                explanation=(
                    f"verifier '{verifier_config.get('type', 'regex')}' "
                    f"{'passed' if result.passed else 'failed'}"
                ),
                evidence={
                    "verifier": verifier_config.get("type", "regex"),
                    "actual_key": actual_key,
                    **result.details,
                },
            )

        if "expected_value" in params:
            expected_value = params["expected_value"]
            expected_source = "literal"
        else:
            expected_key = str(params.get("expected", "expected"))
            expected_value = self.require(evaluator_input, expected_key)
            expected_source = expected_key

        method = str(params.get("method", "exact"))
        threshold = params.get("threshold")
        score_result = scoring.compare(
            method,
            expected_value,
            actual_value,
            threshold=float(threshold) if threshold is not None else None,
        )

        return self.outcome(
            passed=score_result.passed,
            score=score_result.score,
            explanation=(
                f"{method} comparison {'passed' if score_result.passed else 'failed'} "
                f"(score {score_result.score})"
            ),
            evidence={
                "method": method,
                "actual_key": actual_key,
                "expected_source": expected_source,
                "actual_value": _truncate(actual_value),
                "expected_value": _truncate(expected_value),
                **score_result.details,
            },
        )


def _truncate(value: Any, limit: int = 2000) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "...<truncated>"
    return value
