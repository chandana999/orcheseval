"""Metric definition validation and normalization. No database required."""

from __future__ import annotations

import pytest

from app.services.metric_validation import (
    MetricDefinitionError,
    normalize_metric_definition,
)


def normalize(definition: dict, **kwargs) -> dict:
    return normalize_metric_definition(definition, where="metric test", **kwargs)


def test_normalizes_defaults_and_metric_metadata():
    check = normalize(
        {
            "check_id": "summary_present",
            "evaluator": "required_fields",
            "input_mapping": {"summary": " summarizer.output.summary "},
        }
    )
    assert check["check_type"] == "DETERMINISTIC"
    assert check["evaluator"] == "required_fields"
    assert check["input_mapping"]["summary"] == "summarizer.output.summary"
    assert check["max_attempts"] >= 1
    assert check["on_missing_context"] == "not_applicable"


def test_llm_judge_check_type_is_inferred():
    check = normalize(
        {
            "check_id": "faithful",
            "check_type": "LLM_JUDGE",
            "input_mapping": {"summary": "summarizer.output.summary"},
            "params": {"criteria": "is it faithful?"},
        }
    )
    assert check["evaluator"] == "llm_judge"
    assert check["check_type"] == "LLM_JUDGE"


def test_evaluator_aliases_resolve():
    check = normalize(
        {
            "check_id": "structure",
            "evaluator": "output_structure",
            "input_mapping": {"output": "classifier.output"},
            "params": {"schema": {"type": "object"}},
        }
    )
    assert check["evaluator"] == "json_schema"


def test_rejects_unknown_evaluator():
    with pytest.raises(MetricDefinitionError) as exc:
        normalize({"check_id": "x", "evaluator": "telepathy"})
    assert "unknown evaluator" in str(exc.value)


def test_rejects_missing_check_id_and_evaluator():
    with pytest.raises(MetricDefinitionError) as exc:
        normalize({"evaluator": "required_fields"})
    assert "check_id is required" in str(exc.value)

    with pytest.raises(MetricDefinitionError) as exc:
        normalize({"check_id": "x"})
    assert "'evaluator' is required" in str(exc.value)


def test_rejects_bad_input_mapping():
    with pytest.raises(MetricDefinitionError) as exc:
        normalize(
            {
                "check_id": "x",
                "evaluator": "required_fields",
                "input_mapping": {"summary": {"required": True}},
            }
        )
    assert "path" in str(exc.value)


def test_check_type_conflict_is_rejected():
    with pytest.raises(MetricDefinitionError) as exc:
        normalize(
            {
                "check_id": "x",
                "check_type": "DETERMINISTIC",
                "evaluator": "llm_judge",
                "params": {"criteria": "hmm"},
            }
        )
    assert "conflicts with evaluator" in str(exc.value)


def test_inline_params_are_collected():
    check = normalize(
        {
            "check_id": "spans",
            "evaluator": "span_exists",
            "input_mapping": {"spans": "spans"},
            "required_spans": ["classifier"],
        }
    )
    assert check["params"]["required_spans"] == ["classifier"]


def test_profile_defaults_are_applied():
    check = normalize(
        {"check_id": "x", "evaluator": "required_fields"},
        defaults={"priority": 7, "max_attempts": 5, "on_missing_context": "fail"},
    )
    assert check["priority"] == 7
    assert check["max_attempts"] == 5
    assert check["on_missing_context"] == "fail"
