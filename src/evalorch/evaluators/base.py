"""The evaluator contract.

Every evaluator receives normalized input from the context resolver and returns
normalized output. Evaluators do not read raw payloads and do not touch the
database; persistence is the evaluation service's job.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from evalorch.models.enums import CheckType, ResultStatus


@dataclass
class ExecutionContext:
    """Identifiers and settings an evaluator may need, but no raw payload."""

    job_id: uuid.UUID
    ticket_id: uuid.UUID
    payload_id: uuid.UUID
    check_id: str
    check_type: CheckType
    attempt: int
    max_attempts: int
    worker_id: str | None = None
    config_snapshot: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvaluatorOutput:
    """Normalized evaluator result."""

    status: ResultStatus
    passed: bool | None = None
    score: float | None = None
    explanation: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    evaluator_type: str = ""
    evaluator_version: str = ""
    execution_time_ms: float = 0.0
    error_code: str | None = None
    error_message: str | None = None
    output: dict[str, Any] | None = None

    def as_output_json(self) -> dict[str, Any]:
        payload = {
            "status": self.status.value,
            "passed": self.passed,
            "score": self.score,
            "explanation": self.explanation,
            "evaluator_type": self.evaluator_type,
            "evaluator_version": self.evaluator_version,
            "execution_time_ms": round(self.execution_time_ms, 3),
        }
        if self.error_code:
            payload["error_code"] = self.error_code
        if self.error_message:
            payload["error_message"] = self.error_message
        if self.output:
            payload["details"] = self.output
        return payload


class Evaluator:
    """Base class. Subclasses implement `evaluate`."""

    name: str = "evaluator"
    version: str = "1.0.0"
    check_type: CheckType = CheckType.DETERMINISTIC

    def evaluate(
        self,
        evaluator_input: dict[str, Any],
        evaluator_config: dict[str, Any],
        execution_context: ExecutionContext,
    ) -> EvaluatorOutput:
        raise NotImplementedError

    def run(
        self,
        evaluator_input: dict[str, Any],
        evaluator_config: dict[str, Any],
        execution_context: ExecutionContext,
    ) -> EvaluatorOutput:
        """Time the evaluation and stamp identity fields onto the output."""
        started = time.perf_counter()
        output = self.evaluate(evaluator_input, evaluator_config, execution_context)
        output.execution_time_ms = (time.perf_counter() - started) * 1000
        output.evaluator_type = output.evaluator_type or self.name
        output.evaluator_version = output.evaluator_version or self.version
        return output

    # ------------------------------------------------------------- helpers
    def outcome(
        self,
        *,
        passed: bool,
        score: float | None = None,
        explanation: str = "",
        evidence: dict[str, Any] | None = None,
        output: dict[str, Any] | None = None,
    ) -> EvaluatorOutput:
        return EvaluatorOutput(
            status=ResultStatus.PASSED if passed else ResultStatus.FAILED,
            passed=passed,
            score=score if score is not None else (1.0 if passed else 0.0),
            explanation=explanation,
            evidence=evidence or {},
            evaluator_type=self.name,
            evaluator_version=self.version,
            output=output,
        )

    def not_applicable(
        self,
        *,
        explanation: str,
        evidence: dict[str, Any] | None = None,
        error_code: str = "NOT_APPLICABLE",
    ) -> EvaluatorOutput:
        return EvaluatorOutput(
            status=ResultStatus.NOT_APPLICABLE,
            passed=None,
            score=None,
            explanation=explanation,
            evidence=evidence or {},
            evaluator_type=self.name,
            evaluator_version=self.version,
            error_code=error_code,
        )

    @staticmethod
    def require(evaluator_input: dict[str, Any], key: str) -> Any:
        """Fetch a mapped value, failing loudly if the mapping omitted it."""
        from evalorch.services.errors import InvalidEvaluatorConfigError

        if key not in evaluator_input:
            raise InvalidEvaluatorConfigError(
                f"input_mapping is missing required key '{key}'"
            )
        return evaluator_input[key]
