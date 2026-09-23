"""Tool call existence and tool response existence checks."""

from __future__ import annotations

from typing import Any

from evalorch.evaluators.base import Evaluator, EvaluatorOutput, ExecutionContext
from evalorch.models.enums import CheckType

NAME_KEYS = ("name", "tool", "tool_name", "function", "function_name")
RESPONSE_KEYS = ("response", "result", "output", "tool_response", "return_value")


def _tool_name(call: dict[str, Any]) -> str | None:
    for key in NAME_KEYS:
        value = call.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, dict):
            nested = value.get("name")
            if isinstance(nested, str) and nested.strip():
                return nested
    return None


def _has_response(call: dict[str, Any]) -> bool:
    for key in RESPONSE_KEYS:
        if key not in call:
            continue
        value = call[key]
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, (list, dict)) and len(value) == 0:
            continue
        return True
    return False


class ToolCallEvaluator(Evaluator):
    """Verify expected tools were called and (optionally) responded.

    Mapping must provide `tool_calls`, e.g. `"tool_calls": "validator.tool_calls"`.

    Config params::

        {"expected_tools": ["policy_lookup"],
         "require_response": true,
         "min_calls": 1,
         "forbidden_tools": []}
    """

    name = "tool_calls"
    version = "1.0.0"
    check_type = CheckType.DETERMINISTIC

    def evaluate(
        self,
        evaluator_input: dict[str, Any],
        evaluator_config: dict[str, Any],
        execution_context: ExecutionContext,
    ) -> EvaluatorOutput:
        params = evaluator_config.get("params") or {}
        expected = [str(t) for t in (params.get("expected_tools") or [])]
        forbidden = [str(t) for t in (params.get("forbidden_tools") or [])]
        require_response = bool(params.get("require_response", True))
        min_calls = int(params.get("min_calls", 1 if not expected else 0))

        raw = self.require(evaluator_input, "tool_calls")
        if raw is None:
            calls: list[dict[str, Any]] = []
        elif isinstance(raw, dict):
            calls = [raw]
        elif isinstance(raw, list):
            calls = [c for c in raw if isinstance(c, dict)]
        else:
            return self.outcome(
                passed=False,
                score=0.0,
                explanation=f"mapped tool_calls is {type(raw).__name__}, expected a list",
                evidence={"actual_type": type(raw).__name__},
            )

        observed = [name for name in (_tool_name(c) for c in calls) if name]
        missing = [tool for tool in expected if tool not in observed]
        used_forbidden = [tool for tool in forbidden if tool in observed]

        without_response: list[str] = []
        if require_response:
            for call in calls:
                name = _tool_name(call) or "<unnamed>"
                if expected and name not in expected:
                    continue
                if not _has_response(call):
                    without_response.append(name)

        problems: list[str] = []
        if len(calls) < min_calls:
            problems.append(f"expected at least {min_calls} tool call(s), found {len(calls)}")
        if missing:
            problems.append(f"missing tool call(s): {', '.join(missing)}")
        if used_forbidden:
            problems.append(f"forbidden tool(s) called: {', '.join(used_forbidden)}")
        if without_response:
            problems.append(f"tool call(s) without a response: {', '.join(without_response)}")

        satisfied = len(expected) - len(missing)
        score = round(satisfied / len(expected), 4) if expected else (0.0 if problems else 1.0)

        return self.outcome(
            passed=not problems,
            score=1.0 if not problems else score,
            explanation="; ".join(problems) if problems else f"{len(calls)} tool call(s) verified",
            evidence={
                "expected_tools": expected,
                "observed_tools": observed,
                "missing_tools": missing,
                "forbidden_tools_called": used_forbidden,
                "calls_without_response": without_response,
                "call_count": len(calls),
                "require_response": require_response,
            },
        )
