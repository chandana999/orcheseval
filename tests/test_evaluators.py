"""Evaluator behaviour. No database required."""

from __future__ import annotations

import pytest

from app.evaluators.llm.providers.base import LLMResponse
from app.evaluators.registry import get_evaluator
from app.models.enums import ResultStatus
from app.services.errors import InvalidEvaluatorConfigError, UnknownEvaluatorError
from app.services.evaluation_service import evaluate_payload_check
from tests.conftest import make_payload


def run_check(payload, check):
    resolved, output = evaluate_payload_check(payload, check)
    return resolved, output


def test_required_fields_passes_and_fails():
    payload = make_payload()
    check = {
        "check_id": "c1",
        "evaluator": "required_fields",
        "input_mapping": {"summary": "summarizer.output.summary"},
        "params": {"fields": ["summary"]},
    }
    _resolved, output = run_check(payload, check)
    assert output.status is ResultStatus.PASSED

    empty = make_payload(summary="   ")
    _resolved, output = run_check(empty, check)
    assert output.status is ResultStatus.FAILED
    assert output.evidence["missing"][0]["reason"] == "empty"


def test_json_schema_validation_reports_violations():
    payload = make_payload()
    check = {
        "check_id": "c2",
        "evaluator": "json_schema",
        "input_mapping": {"output": "classifier.output"},
        "params": {
            "target": "output",
            "schema": {
                "type": "object",
                "required": ["category", "confidence"],
                "properties": {
                    "category": {"type": "string", "enum": ["billing", "technical"]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
            },
        },
    }
    _resolved, output = run_check(payload, check)
    assert output.passed is True

    payload["spans"][0]["output"]["confidence"] = 4.2
    _resolved, output = run_check(payload, check)
    assert output.passed is False
    assert any("above maximum" in v for v in output.evidence["violations"])


def test_workflow_order_strict_and_subsequence():
    payload = make_payload()
    base = {
        "check_id": "c3",
        "evaluator": "workflow_order",
        "input_mapping": {"spans": "spans"},
    }
    strict = {**base, "params": {"expected_order": ["classifier", "validator", "summarizer"], "mode": "strict"}}
    _resolved, output = run_check(payload, strict)
    assert output.passed is True

    reordered = make_payload()
    reordered["spans"][0]["order"] = 9  # classifier now runs last
    _resolved, output = run_check(reordered, strict)
    assert output.passed is False
    assert output.evidence["actual_order"][0] != "classifier"


def test_span_exists_flags_missing_agent():
    payload = make_payload(include_summarizer=False)
    check = {
        "check_id": "c4",
        "evaluator": "span_exists",
        "input_mapping": {"spans": "spans"},
        "params": {"required_spans": ["classifier", "summarizer"]},
    }
    _resolved, output = run_check(payload, check)
    assert output.passed is False
    assert output.evidence["missing_spans"] == ["summarizer"]
    assert output.score == 0.5


def test_tool_calls_requires_response():
    check = {
        "check_id": "c5",
        "evaluator": "tool_calls",
        "input_mapping": {"tool_calls": "validator.tool_calls"},
        "params": {"expected_tools": ["policy_lookup"], "require_response": True},
    }
    _resolved, output = run_check(make_payload(), check)
    assert output.passed is True

    _resolved, output = run_check(make_payload(tool_response=False), check)
    assert output.passed is False
    assert output.evidence["calls_without_response"] == ["policy_lookup"]


def test_cross_span_consistency_detects_disagreement():
    payload = make_payload()
    check = {
        "check_id": "c6",
        "evaluator": "cross_span_consistency",
        "input_mapping": {
            "classified": "classifier.output.category",
            "summarized": "summarizer.output.category",
        },
        "params": {
            "comparisons": [
                {"left": "classified", "right": "summarized", "method": "exact", "label": "category"}
            ]
        },
    }
    _resolved, output = run_check(payload, check)
    assert output.passed is True

    payload["spans"][2]["output"]["category"] = "technical"
    _resolved, output = run_check(payload, check)
    assert output.passed is False
    assert output.evidence["comparisons"][0]["label"] == "category"


def test_field_comparison_methods_and_verifier():
    payload = make_payload()
    fuzzy = {
        "check_id": "c7",
        "evaluator": "field_comparison",
        "input_mapping": {"actual": "session_context.final_response"},
        "params": {
            "actual": "actual",
            "expected_value": "the duplicate charge was refunded",
            "method": "fuzzy",
            "threshold": 60.0,
        },
    }
    _resolved, output = run_check(payload, fuzzy)
    assert output.passed is True

    verifier = {
        "check_id": "c8",
        "evaluator": "field_comparison",
        "input_mapping": {"actual": "session_context.final_response"},
        "params": {
            "actual": "actual",
            "verifier": {"type": "contains_all", "contains": ["refunded", "duplicate"]},
        },
    }
    _resolved, output = run_check(payload, verifier)
    assert output.passed is True

    semantic = {**fuzzy, "params": {**fuzzy["params"], "method": "semantic", "threshold": 0.99}}
    _resolved, output = run_check(payload, semantic)
    assert output.evidence["embedding_backend"] == "hash"


def test_llm_judge_parses_verdict(monkeypatch):
    class FakeProvider:
        def generate(self, prompt, model, config=None):
            assert "conversation" in prompt.lower() or "summary" in prompt.lower()
            return LLMResponse(
                output='Here you go: {"passed": true, "score": 0.88, "explanation": "faithful"}',
                latency_ms=12.0,
                prompt_tokens=100,
                completion_tokens=20,
                model=model,
            )

    monkeypatch.setattr(
        "app.evaluators.llm.judge.get_llm_provider", lambda name=None: FakeProvider()
    )
    check = {
        "check_id": "judge",
        "check_type": "LLM_JUDGE",
        "evaluator": "llm_judge",
        "input_mapping": {
            "summary": "summarizer.output.summary",
            "conversation": "session_context.conversation_history",
        },
        "params": {"criteria": "Is the summary faithful?", "model": "test-model"},
    }
    _resolved, output = run_check(make_payload(), check)
    assert output.status is ResultStatus.PASSED
    assert output.score == 0.88
    assert output.evidence["estimated_cost_usd"] > 0
    assert output.evidence["prompt_tokens"] == 100


def test_llm_judge_unparseable_response_is_an_error(monkeypatch):
    class BadProvider:
        def generate(self, prompt, model, config=None):
            return LLMResponse(output="I cannot comply", latency_ms=5.0, model=model)

    monkeypatch.setattr(
        "app.evaluators.llm.judge.get_llm_provider", lambda name=None: BadProvider()
    )
    check = {
        "check_id": "judge",
        "check_type": "LLM_JUDGE",
        "evaluator": "llm_judge",
        "input_mapping": {"summary": "summarizer.output.summary"},
        "params": {"criteria": "Is the summary faithful?"},
    }
    _resolved, output = run_check(make_payload(), check)
    assert output.status is ResultStatus.ERROR
    assert output.error_code == "JUDGE_RESPONSE_UNPARSEABLE"


def test_unknown_evaluator_and_bad_config_are_permanent_errors():
    with pytest.raises(UnknownEvaluatorError):
        get_evaluator("does_not_exist")

    with pytest.raises(InvalidEvaluatorConfigError):
        run_check(
            make_payload(),
            {
                "check_id": "bad",
                "evaluator": "json_schema",
                "input_mapping": {"output": "classifier.output"},
                "params": {},
            },
        )
