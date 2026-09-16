"""Configuration validation and normalization. No database required."""

from __future__ import annotations

import pytest

from app.services.config_validation import (
    ConfigValidationError,
    validate_and_normalize_config,
)


def test_normalizes_defaults_and_check_metadata():
    normalized = validate_and_normalize_config(
        {
            "checks": [
                {
                    "check_id": "summary_present",
                    "evaluator": "required_fields",
                    "input_mapping": {"summary": " summarizer.output.summary "},
                }
            ]
        }
    )
    check = normalized["checks"][0]
    assert check["check_type"] == "DETERMINISTIC"
    assert check["evaluator"] == "required_fields"
    assert check["input_mapping"]["summary"] == "summarizer.output.summary"
    assert check["max_attempts"] >= 1
    assert check["on_missing_context"] == "not_applicable"


def test_llm_judge_check_type_is_inferred():
    normalized = validate_and_normalize_config(
        {
            "checks": [
                {
                    "check_id": "faithful",
                    "check_type": "LLM_JUDGE",
                    "input_mapping": {"summary": "summarizer.output.summary"},
                    "params": {"criteria": "is it faithful?"},
                }
            ]
        }
    )
    assert normalized["checks"][0]["evaluator"] == "llm_judge"
    assert normalized["checks"][0]["check_type"] == "LLM_JUDGE"


def test_evaluator_aliases_resolve():
    normalized = validate_and_normalize_config(
        {
            "checks": [
                {
                    "check_id": "structure",
                    "evaluator": "output_structure",
                    "input_mapping": {"output": "classifier.output"},
                    "params": {"schema": {"type": "object"}},
                }
            ]
        }
    )
    assert normalized["checks"][0]["evaluator"] == "json_schema"


def test_rejects_unknown_evaluator():
    with pytest.raises(ConfigValidationError) as exc:
        validate_and_normalize_config(
            {"checks": [{"check_id": "x", "evaluator": "telepathy"}]}
        )
    assert "unknown evaluator" in str(exc.value)


def test_rejects_duplicate_check_ids():
    with pytest.raises(ConfigValidationError) as exc:
        validate_and_normalize_config(
            {
                "checks": [
                    {"check_id": "dup", "evaluator": "required_fields"},
                    {"check_id": "dup", "evaluator": "required_fields"},
                ]
            }
        )
    assert "duplicate check_id" in str(exc.value)


def test_rejects_empty_checks_and_bad_mapping():
    with pytest.raises(ConfigValidationError):
        validate_and_normalize_config({"checks": []})

    with pytest.raises(ConfigValidationError) as exc:
        validate_and_normalize_config(
            {
                "checks": [
                    {
                        "check_id": "x",
                        "evaluator": "required_fields",
                        "input_mapping": {"summary": {"required": True}},
                    }
                ]
            }
        )
    assert "path" in str(exc.value)


def test_check_type_conflict_is_rejected():
    with pytest.raises(ConfigValidationError) as exc:
        validate_and_normalize_config(
            {
                "checks": [
                    {
                        "check_id": "x",
                        "check_type": "DETERMINISTIC",
                        "evaluator": "llm_judge",
                        "params": {"criteria": "hmm"},
                    }
                ]
            }
        )
    assert "conflicts with evaluator" in str(exc.value)


def test_inline_params_are_collected():
    normalized = validate_and_normalize_config(
        {
            "checks": [
                {
                    "check_id": "spans",
                    "evaluator": "span_exists",
                    "input_mapping": {"spans": "spans"},
                    "required_spans": ["classifier"],
                }
            ]
        }
    )
    assert normalized["checks"][0]["params"]["required_spans"] == ["classifier"]
