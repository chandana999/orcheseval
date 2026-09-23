"""Payload and config.json contract: UUIDs, OpenTelemetry ids, and span trees."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from evalorch.services.context_resolver import ErrorCode, extract_spans, resolve_input_mapping
from evalorch.services.payload_validation import PayloadValidationError, validate_payload
from evalorch.services.profile_resolution import (
    ProfileNotFoundError,
    ProfileResolutionError,
    metrics_for_agent,
)
from tests.conftest import write_dataset

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "dataset-folder-001"
AGENT_ID = "3f1a8c2e-6b44-4d7a-9e10-2c8b5a7d6e01"


def _payload() -> dict:
    return json.loads((EXAMPLES / "payload_001.json").read_text(encoding="utf-8"))


def _config() -> dict:
    return json.loads((EXAMPLES / "config.json").read_text(encoding="utf-8"))


def test_example_payload_and_config_share_one_agent_uuid():
    payload = _payload()
    config = _config()
    report = validate_payload(payload)
    assert report["valid"] is True
    assert payload["agent_registry"]["agent_id"] == AGENT_ID
    assert payload["correlation"]["agent_id"] == AGENT_ID
    assert config["agentId"] == AGENT_ID
    snapshots = metrics_for_agent(config, AGENT_ID, source="config.json")
    assert [item["metric_id"] for item in snapshots] == [
        "summary_present",
        "workflow_sequence",
        "policy_tool_called",
        "all_agents_ran",
    ]
    judge = snapshots[0]["definition_payload"]
    assert judge["span_names"] == ["summarizer"]
    assert judge["thresholds"]["pass"] == 1.0


def test_identifier_uuids_are_required_and_user_id_is_not():
    payload = _payload()
    validate_payload(payload)
    payload["correlation"]["user_id"] = "cus-8812"
    payload["trace_context"]["trace"]["user_id"] = "cus-8812"
    validate_payload(payload)

    for path in (
        ("agent_registry", "agent_id"),
        ("correlation", "agent_id"),
        ("payload_metadata", "payload_id"),
        ("correlation", "event_id"),
        ("correlation", "session_id"),
        ("correlation", "invocation_id"),
    ):
        broken = _payload()
        cursor = broken
        for key in path[:-1]:
            cursor = cursor[key]
        cursor[path[-1]] = "not-a-uuid"
        with pytest.raises(PayloadValidationError, match="UUID"):
            validate_payload(broken)


def test_agent_uuid_must_match_across_payload_and_config():
    payload = _payload()
    payload["correlation"]["agent_id"] = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    with pytest.raises(PayloadValidationError, match="agent_id"):
        validate_payload(payload)

    with pytest.raises(ProfileResolutionError, match="agentId must be a UUID"):
        metrics_for_agent({"agentId": "support-triage-agent", "metrics": []}, AGENT_ID, source="config.json")

    with pytest.raises(ProfileNotFoundError):
        metrics_for_agent(_config(), "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", source="config.json")


def test_otel_ids_are_accepted_and_uuids_are_not_used_for_them():
    payload = _payload()
    validate_payload(payload)
    trace = payload["trace_context"]["trace"]
    assert len(trace["trace_id"]) == 32
    assert "-" not in trace["trace_id"]
    assert trace["spans"][0]["parent_span_id"] is None
    assert len(trace["spans"][0]["span_id"]) == 16

    broken = _payload()
    broken["correlation"]["trace_id"] = AGENT_ID
    broken["trace_context"]["trace"]["trace_id"] = AGENT_ID
    with pytest.raises(PayloadValidationError, match="OpenTelemetry trace id"):
        validate_payload(broken)

    broken = _payload()
    broken["trace_context"]["trace"]["spans"][0]["span_id"] = AGENT_ID
    with pytest.raises(PayloadValidationError, match="OpenTelemetry span id"):
        validate_payload(broken)


def test_spans_stay_an_array_and_keep_parent_links():
    payload = _payload()
    spans = payload["trace_context"]["trace"]["spans"]
    assert isinstance(spans, list)
    assert len(spans) == 3
    extracted = extract_spans(payload)
    assert [span["name"] for span in extracted] == ["classifier", "validator", "summarizer"]
    by_name = {span["name"]: span for span in extracted}
    assert by_name["classifier"]["parent_span_id"] is None
    assert by_name["validator"]["parent_span_id"] == by_name["classifier"]["span_id"]
    assert by_name["summarizer"]["parent_span_id"] == by_name["validator"]["span_id"]

    broken = _payload()
    broken["trace_context"]["trace"]["spans"] = {"classifier": spans[0]}
    with pytest.raises(PayloadValidationError, match="spans must be an array"):
        validate_payload(broken)

    broken = _payload()
    broken["trace_context"]["trace"]["spans"][1]["parent_span_id"] = "ffffffffffffaaaa"
    with pytest.raises(PayloadValidationError, match="does not match a span"):
        validate_payload(broken)


def _span_copy(template: dict, *, span_id: str, parent: str | None, name: str, started_at: str) -> dict:
    span = copy.deepcopy(template)
    span["span_id"] = span_id
    span["parent_span_id"] = parent
    span["name"] = name
    span["started_at"] = started_at
    span["ended_at"] = started_at
    return span


def test_trace_accepts_any_number_of_spans():
    payload = _payload()
    template = payload["trace_context"]["trace"]["spans"][0]
    root_id = template["span_id"]
    trace_id = template["trace_id"]

    payload["trace_context"]["trace"]["spans"] = []
    validate_payload(payload)
    assert extract_spans(payload) == []

    only_root = _span_copy(template, span_id=root_id, parent=None, name="research_agent", started_at="2026-01-05T10:00:00Z")
    only_root["trace_id"] = trace_id
    payload["trace_context"]["trace"]["spans"] = [only_root]
    validate_payload(payload)
    assert [span["name"] for span in extract_spans(payload)] == ["research_agent"]

    children = [
        only_root,
        _span_copy(template, span_id="1111111111111111", parent=root_id, name="lookup", started_at="2026-01-05T10:00:01Z"),
        _span_copy(template, span_id="2222222222222222", parent=root_id, name="draft", started_at="2026-01-05T10:00:02Z"),
        _span_copy(template, span_id="3333333333333333", parent="2222222222222222", name="cite", started_at="2026-01-05T10:00:03Z"),
        _span_copy(template, span_id="4444444444444444", parent=root_id, name="lookup", started_at="2026-01-05T10:00:04Z"),
    ]
    for child in children:
        child["trace_id"] = trace_id
    payload["trace_context"]["trace"]["spans"] = children
    validate_payload(payload)
    extracted = extract_spans(payload)
    assert [span["name"] for span in extracted] == ["research_agent", "lookup", "draft", "cite", "lookup"]
    assert sum(span["parent_span_id"] == root_id for span in extracted) == 3

    resolved = resolve_input_mapping(payload, {"all_names": "spans.*.name", "one": "span_id:3333333333333333.name"})
    assert resolved.ok
    assert resolved.values["all_names"] == ["research_agent", "lookup", "draft", "cite", "lookup"]
    assert resolved.values["one"] == "cite"

    ambiguous = resolve_input_mapping(payload, {"lookup": "lookup.name"})
    assert not ambiguous.ok
    assert ambiguous.errors[0].code == ErrorCode.AMBIGUOUS_SPAN


def test_disabled_metric_is_not_frozen_and_missing_fields_are_rejected():
    config = _config()
    config["metrics"][0]["enabled"] = False
    snapshots = metrics_for_agent(config, AGENT_ID, source="config.json")
    assert "summary_present" not in [item["metric_id"] for item in snapshots]

    config = _config()
    del config["metrics"][0]["inputMapping"]
    with pytest.raises(ProfileResolutionError, match="inputMapping"):
        metrics_for_agent(config, AGENT_ID, source="config.json")


def test_llm_judge_metric_keeps_plugin_id_and_thresholds():
    config = {
        "agentId": AGENT_ID,
        "metrics": [
            {
                "metricId": "response-quality",
                "metricName": "Response Quality",
                "metricVersion": "1.0",
                "metricType": "llm_judge",
                "evaluatorPluginId": "deepeval",
                "enabled": True,
                "spanNames": ["research_agent", "summarizer"],
                "thresholds": {"pass": 0.8, "fail": 0.5},
                "weight": 20,
                "inputMapping": {
                    "input": "conversation_history",
                    "actualOutput": "agent_response",
                },
                "params": {"model": "gpt-5", "temperature": 0},
            }
        ],
    }
    frozen = metrics_for_agent(config, AGENT_ID, source="config.json")[0]
    definition = frozen["definition_payload"]
    assert definition["evaluator"] == "llm_judge"
    assert definition["check_type"] == "LLM_JUDGE"
    assert definition["evaluator_plugin_id"] == "deepeval"
    assert definition["thresholds"] == {"pass": 0.8, "fail": 0.5}
    assert definition["weight"] == 20
    assert definition["params"]["pass_threshold"] == 0.8
    assert definition["params"]["model"] == "gpt-5"
    assert frozen["metric_version_number"] == 1
    assert definition["input_mapping"]["actualOutput"] == "agent_response"


def test_contract_dataset_creates_one_ticket_per_enabled_metric(client, temp_root):
    payload = _payload()
    config = _config()
    write_dataset(temp_root, "contract-ds", [payload], config=config)
    response = client.post(
        "/v1/evaluation-jobs",
        json={"dataset_id": "contract-ds", "name": "contract"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["job"]["total_tickets"] == 4

    payload["payload_metadata"]["payload_id"] = "pl-not-a-uuid"
    write_dataset(temp_root, "bad-payload", [payload], config=config)
    rejected = client.post(
        "/v1/evaluation-jobs",
        json={"dataset_id": "bad-payload", "name": "bad"},
    )
    assert rejected.status_code == 400
    assert "UUID" in rejected.json()["error"]["message"]


def test_old_config_and_top_level_spans_are_rejected():
    with pytest.raises(ProfileResolutionError, match="not supported"):
        metrics_for_agent({"agents": [], "checks": []}, AGENT_ID, source="config.json")

    with pytest.raises(PayloadValidationError, match="agent_registry"):
        validate_payload({"spans": []})


def test_nested_span_relationships_are_preserved_when_a_child_is_reordered():
    payload = _payload()
    spans = payload["trace_context"]["trace"]["spans"]
    spans[0]["started_at"] = "2026-01-05T10:00:05Z"
    spans[2]["started_at"] = "2026-01-05T10:00:00Z"
    extracted = extract_spans(payload)
    by_name = {span["name"]: span for span in extracted}
    assert by_name["summarizer"]["parent_span_id"] == by_name["validator"]["span_id"]
    assert by_name["validator"]["parent_span_id"] == by_name["classifier"]["span_id"]
    assert copy.deepcopy(spans[1]["parent_span_id"]) == by_name["classifier"]["span_id"]
