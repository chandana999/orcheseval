"""Evaluator behaviour. No database required."""

from __future__ import annotations

import pytest

from evalorch.evaluators.llm.providers.base import LLMResponse
from evalorch.evaluators.registry import get_evaluator
from evalorch.models.enums import ResultStatus
from evalorch.services.errors import InvalidEvaluatorConfigError, UnknownEvaluatorError
from evalorch.services.evaluation_service import evaluate_payload_check
from tests.conftest import make_payload


def run_check(payload, check):
    resolved, output = evaluate_payload_check(payload, check)
    return resolved, output


def test_required_fields_passes_and_fails():
    payload = make_payload()
    check = {
        "check_id": "c1",
        "evaluator": "required_fields",
        "input_mapping": {"summary": "summarizer.attributes.output.summary"},
        "params": {"fields": ["summary"]},
    }
    _resolved, output = run_check(payload, check)
    assert output.status is ResultStatus.PASSED

    empty = make_payload(summary="   ")
    _resolved, output = run_check(empty, check)
    assert output.status is ResultStatus.FAILED
    assert output.evidence["missing"][0]["reason"] == "empty"


def test_workflow_order_strict_and_subsequence():
    payload = make_payload()
    base = {
        "check_id": "c3",
        "evaluator": "workflow_order",
        "input_mapping": {"spans": "spans"},
    }
    expected = ["classifier", "validator", "summarizer"]
    strict = {**base, "params": {"expected_order": expected, "mode": "strict"}}
    _resolved, output = run_check(payload, strict)
    assert output.passed is True
    assert output.evidence["mode"] == "strict"

    reordered = make_payload()
    reordered["trace_context"]["trace"]["spans"][0]["started_at"] = "2026-01-05T10:00:09Z"
    _resolved, output = run_check(reordered, strict)
    assert output.passed is False
    assert output.evidence["actual_order"][0] != "classifier"

    with_extra = make_payload()
    with_extra["trace_context"]["trace"]["spans"].append(
        {
            "span_id": "8888888888888888",
            "trace_id": with_extra["correlation"]["trace_id"],
            "parent_span_id": None,
            "name": "retriever",
            "kind": "internal",
            "started_at": "2026-01-05T10:00:01Z",
            "ended_at": "2026-01-05T10:00:01Z",
            "duration_ms": 10,
            "status": "ok",
            "error_message": None,
            "attributes": {},
            "events": [],
            "links": [],
        }
    )
    named = {**base, "span_names": [*expected, "retriever"]}
    subsequence = {**named, "params": {"expected_order": expected, "mode": "subsequence"}}
    strict_with_extra = {**named, "params": {"expected_order": expected, "mode": "strict"}}
    _resolved, output = run_check(with_extra, subsequence)
    assert output.passed is True
    assert output.evidence["mode"] == "subsequence"
    assert output.evidence["actual_order"] == ["classifier", "retriever", "validator", "summarizer"]

    _resolved, output = run_check(with_extra, strict_with_extra)
    assert output.passed is False

    _resolved, output = run_check(reordered, subsequence)
    assert output.passed is False
    assert "out of order" in output.explanation


def test_ticket_mapping_does_not_receive_unnamed_spans():
    payload = make_payload()
    payload["trace_context"]["trace"]["spans"].append(
        {
            "span_id": "8888888888888888",
            "trace_id": payload["correlation"]["trace_id"],
            "parent_span_id": None,
            "name": "retriever",
            "kind": "internal",
            "started_at": "2026-01-05T10:00:04Z",
            "ended_at": "2026-01-05T10:00:04Z",
            "duration_ms": 10,
            "status": "ok",
            "error_message": None,
            "attributes": {"output": {"blob": "large"}},
            "events": [],
            "links": [],
        }
    )
    check = {
        "check_id": "summary_only",
        "evaluator": "required_fields",
        "span_names": ["summarizer"],
        "input_mapping": {"summary": "summarizer.attributes.output.summary", "spans": "spans"},
        "params": {"fields": ["summary"]},
    }
    resolved, output = run_check(payload, check)
    assert output.passed is True
    assert [span["name"] for span in resolved.values["spans"]] == ["summarizer"]


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
        "input_mapping": {"tool_calls": "validator.attributes.tool_calls"},
        "params": {"expected_tools": ["policy_lookup"], "require_response": True},
    }
    _resolved, output = run_check(make_payload(), check)
    assert output.passed is True

    _resolved, output = run_check(make_payload(tool_response=False), check)
    assert output.passed is False
    assert output.evidence["calls_without_response"] == ["policy_lookup"]


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
        "evalorch.evaluators.llm.judge.get_llm_provider", lambda name=None: FakeProvider()
    )
    check = {
        "check_id": "judge",
        "check_type": "LLM_JUDGE",
        "evaluator": "llm_judge",
        "input_mapping": {
            "summary": "summarizer.attributes.output.summary",
            "conversation": "classifier.attributes.conversation_history",
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
        "evalorch.evaluators.llm.judge.get_llm_provider", lambda name=None: BadProvider()
    )
    check = {
        "check_id": "judge",
        "check_type": "LLM_JUDGE",
        "evaluator": "llm_judge",
        "input_mapping": {"summary": "summarizer.attributes.output.summary"},
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
                "evaluator": "tool_calls",
                "input_mapping": {"spans": "spans"},
                "params": {"expected_tools": ["policy_lookup"]},
            },
        )
