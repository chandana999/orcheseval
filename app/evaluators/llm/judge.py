"""LLM judge evaluator.

One configuration-driven judge covers summarization accuracy/clarity/completeness,
response appropriateness, policy compliance, resolution quality, and any other
rubric: the check supplies the criteria and the prompt, and the resolver supplies
the values. The judge never reads the raw payload.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.core.config import settings
from app.core.metrics import LLM_COST_USD, LLM_TOKENS
from app.evaluators.base import Evaluator, EvaluatorOutput, ExecutionContext
from app.evaluators.llm.providers import get_llm_provider
from app.models.enums import CheckType, ResultStatus
from app.services.errors import (
    InvalidEvaluatorConfigError,
    TransientEvaluationError,
)

DEFAULT_PROMPT_TEMPLATE = """You are an impartial evaluation judge.

Evaluation criteria:
{criteria}

Inputs to evaluate (JSON):
{inputs}

Respond with ONLY a JSON object in this exact shape:
{{"passed": true or false, "score": 0.0-1.0, "explanation": "one or two sentences"}}"""


def estimate_cost_usd(prompt_tokens: int, completion_tokens: int) -> float:
    """Estimate USD cost from prompt and completion token counts."""
    prompt_tokens = max(0, int(prompt_tokens or 0))
    completion_tokens = max(0, int(completion_tokens or 0))
    cost = (prompt_tokens / 1_000_000) * settings.input_cost_per_1m + (
        completion_tokens / 1_000_000
    ) * settings.output_cost_per_1m
    return round(cost, 6)


def _render_inputs(evaluator_input: dict[str, Any], limit: int) -> str:
    rendered: dict[str, Any] = {}
    for key, value in evaluator_input.items():
        if isinstance(value, str):
            rendered[key] = value if len(value) <= limit else value[:limit] + "...<truncated>"
        else:
            text = json.dumps(value, ensure_ascii=False, default=str)
            rendered[key] = (
                json.loads(text)
                if len(text) <= limit
                else text[:limit] + "...<truncated>"
            )
    return json.dumps(rendered, indent=2, ensure_ascii=False, default=str)


def parse_judge_response(text: str) -> dict[str, Any]:
    """Extract the JSON verdict from a model response."""
    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end <= start:
        raise ValueError("judge response contained no JSON object")
    parsed = json.loads(text[start:end])
    if not isinstance(parsed, dict):
        raise ValueError("judge response JSON was not an object")
    return parsed


class LLMJudgeEvaluator(Evaluator):
    """Config params::

        {"criteria": "Does the summary faithfully reflect the conversation?",
         "prompt_template": "...{criteria}...{inputs}...",   # optional
         "provider": "openai", "model": "gpt-4o-mini",
         "temperature": 0, "max_tokens": 200,
         "pass_threshold": 0.7, "max_input_chars": 4000}
    """

    name = "llm_judge"
    version = "1.0.0"
    check_type = CheckType.LLM_JUDGE

    def evaluate(
        self,
        evaluator_input: dict[str, Any],
        evaluator_config: dict[str, Any],
        execution_context: ExecutionContext,
    ) -> EvaluatorOutput:
        params = evaluator_config.get("params") or {}
        snapshot_model = (execution_context.config_snapshot or {}).get("model") or {}

        criteria = (
            params.get("criteria")
            or evaluator_config.get("criteria")
            or evaluator_config.get("description")
        )
        if not criteria:
            raise InvalidEvaluatorConfigError(
                "llm_judge check requires params.criteria describing what to judge"
            )
        if not evaluator_input:
            raise InvalidEvaluatorConfigError(
                "llm_judge check requires a non-empty input_mapping"
            )

        provider_name = (
            params.get("provider")
            or evaluator_config.get("provider")
            or snapshot_model.get("provider")
            or settings.default_provider
        )
        model = (
            params.get("model")
            or evaluator_config.get("model")
            or snapshot_model.get("model")
            or settings.judge_model
        )
        temperature = float(
            params.get(
                "temperature",
                evaluator_config.get("temperature", snapshot_model.get("temperature", 0.0)),
            )
        )
        max_tokens = int(
            params.get(
                "max_tokens",
                evaluator_config.get("max_tokens", snapshot_model.get("max_tokens", 300)),
            )
        )
        pass_threshold = float(params.get("pass_threshold", 0.7))
        max_input_chars = int(params.get("max_input_chars", 4000))

        template = params.get("prompt_template") or DEFAULT_PROMPT_TEMPLATE
        inputs_block = _render_inputs(evaluator_input, max_input_chars)
        try:
            prompt = template.format(criteria=criteria, inputs=inputs_block)
        except (KeyError, IndexError) as exc:
            raise InvalidEvaluatorConfigError(
                f"prompt_template must only use {{criteria}} and {{inputs}}: {exc}"
            ) from exc

        provider = get_llm_provider(provider_name)
        try:
            response = provider.generate(
                prompt, model, {"temperature": temperature, "max_tokens": max_tokens}
            )
        except httpx.HTTPStatusError as exc:
            # 429/5xx are retryable; classify_error decides, but make the intent explicit.
            if exc.response.status_code in {408, 409, 425, 429, 500, 502, 503, 504}:
                raise TransientEvaluationError(
                    f"judge provider returned {exc.response.status_code}",
                    error_code=f"HTTP_{exc.response.status_code}",
                ) from exc
            raise
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            raise TransientEvaluationError(
                f"judge provider unreachable: {exc}", error_code="LLM_TIMEOUT"
            ) from exc

        prompt_tokens = response.prompt_tokens
        completion_tokens = response.completion_tokens
        if prompt_tokens:
            LLM_TOKENS.labels(direction="prompt").inc(prompt_tokens)
        if completion_tokens:
            LLM_TOKENS.labels(direction="completion").inc(completion_tokens)
        cost = estimate_cost_usd(prompt_tokens, completion_tokens)
        LLM_COST_USD.inc(cost)

        evidence: dict[str, Any] = {
            "criteria": criteria,
            "provider": provider_name,
            "model": response.model or model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "pass_threshold": pass_threshold,
            "input_keys": sorted(evaluator_input.keys()),
            "prompt": prompt,
            "raw_response": response.output,
            "latency_ms": round(response.latency_ms, 3),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "estimated_cost_usd": cost,
        }

        try:
            verdict = parse_judge_response(response.output)
        except (ValueError, json.JSONDecodeError) as exc:
            # Unparseable verdict is an evaluation error, not a silent failure.
            return EvaluatorOutput(
                status=ResultStatus.ERROR,
                passed=None,
                score=None,
                explanation=f"judge response could not be parsed: {exc}",
                evidence=evidence,
                evaluator_type=self.name,
                evaluator_version=self.version,
                error_code="JUDGE_RESPONSE_UNPARSEABLE",
                error_message=str(exc),
            )

        score = verdict.get("score")
        try:
            score = float(score) if score is not None else None
        except (TypeError, ValueError):
            score = None
        if score is not None:
            score = max(0.0, min(1.0, score))

        if "passed" in verdict:
            passed = bool(verdict["passed"])
        elif score is not None:
            passed = score >= pass_threshold
        else:
            passed = False

        evidence["judge_verdict"] = verdict
        return self.outcome(
            passed=passed,
            score=score if score is not None else (1.0 if passed else 0.0),
            explanation=str(verdict.get("explanation") or "")[:2000],
            evidence=evidence,
            output={"verdict": verdict},
        )
