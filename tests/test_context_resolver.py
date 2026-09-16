"""Context resolver behaviour. No database required."""

from __future__ import annotations

from app.services.context_resolver import (
    ErrorCode,
    extract_spans,
    resolve_input_mapping,
    resolve_path,
)
from tests.conftest import make_payload


def test_resolves_session_trace_and_span_paths():
    payload = make_payload(category="billing")
    resolved = resolve_input_mapping(
        payload,
        {
            "history": "session_context.conversation_history",
            "workflow": "trace_data.workflow_id",
            "category": "classifier.output.category",
            "summary": "summarizer.output.summary",
            "first_tool": "validator.tool_calls[0].name",
        },
    )
    assert resolved.ok
    assert resolved.values["workflow"] == "support_triage"
    assert resolved.values["category"] == "billing"
    assert resolved.values["first_tool"] == "policy_lookup"
    assert len(resolved.values["history"]) == 2
    assert resolved.resolutions["category"]["span_selector"] == "agent_id"


def test_explicit_span_selectors_and_order():
    payload = make_payload()
    resolved = resolve_input_mapping(
        payload,
        {
            "by_type": "agent_type:summarizer.output.category",
            "by_span_id": {"path": "spans.1.agent_id"},
            "second_agent": "order:2.agent_id",
        },
    )
    assert resolved.ok, resolved.errors
    assert resolved.values["second_agent"] == "validator"
    assert resolved.values["by_span_id"] == "validator"


def test_missing_span_is_reported_not_guessed():
    payload = make_payload(include_summarizer=False)
    resolved = resolve_input_mapping(payload, {"summary": "summarizer.output.summary"})
    assert not resolved.ok
    assert resolved.missing_required == ["summary"]
    assert resolved.errors[0].code == ErrorCode.MISSING_SPAN
    assert "summarizer" not in (resolved.resolutions["summary"]["available_spans"] or [])


def test_optional_mapping_and_default_do_not_block_evaluation():
    payload = make_payload(include_summarizer=False)
    resolved = resolve_input_mapping(
        payload,
        {
            "summary": {"path": "summarizer.output.summary", "required": False},
            "fallback": {"path": "summarizer.output.summary", "default": "n/a"},
        },
    )
    assert resolved.ok
    assert resolved.values["fallback"] == "n/a"
    assert "summary" not in resolved.values


def test_type_validation_rejects_wrong_shape():
    payload = make_payload()
    resolved = resolve_input_mapping(
        payload, {"category": {"path": "classifier.output.category", "type": "object"}}
    )
    assert not resolved.ok
    assert resolved.errors[0].code == ErrorCode.INVALID_TYPE


def test_wildcard_and_list_key_traversal():
    payload = make_payload()
    resolved = resolve_input_mapping(
        payload, {"agents": "spans.*.agent_id", "roles": "session_context.conversation_history.role"}
    )
    assert resolved.values["agents"] == ["classifier", "validator", "summarizer"]
    assert resolved.values["roles"] == ["user", "assistant"]


def test_spans_are_ordered_by_declared_order():
    payload = make_payload(spans_out_of_order=True)
    spans = extract_spans(payload)
    assert [s["agent_id"] for s in spans] == ["classifier", "validator", "summarizer"]


def test_agent_keyed_span_object_is_normalized():
    payload = {
        "trace_data": {"trace_id": "t1"},
        "spans": {
            "classifier": {"output": {"category": "billing"}, "order": 1},
            "summarizer": {"output": {"summary": "ok"}, "order": 2},
        },
    }
    value, code, detail = resolve_path(payload, "summarizer.output.summary")
    assert code is None
    assert value == "ok"
    assert detail["agent_id"] == "summarizer"


def test_whole_payload_and_spans_list_access():
    payload = make_payload()
    resolved = resolve_input_mapping(payload, {"spans": "spans", "meta": "payload_metadata"})
    assert len(resolved.values["spans"]) == 3
    assert resolved.values["meta"]["version"] == "2026-01"


def test_unsupported_mapping_value_is_rejected():
    resolved = resolve_input_mapping(make_payload(), {"bad": 42})
    assert not resolved.ok
    assert resolved.errors[0].code == ErrorCode.UNSUPPORTED_MAPPING


def test_null_value_is_distinguished_from_missing_path():
    payload = make_payload()
    payload["spans"][0]["output"]["category"] = None
    resolved = resolve_input_mapping(payload, {"category": "classifier.output.category"})
    assert resolved.errors[0].code == ErrorCode.NULL_VALUE

    resolved_nullable = resolve_input_mapping(
        payload, {"category": {"path": "classifier.output.category", "nullable": True}}
    )
    assert resolved_nullable.ok
    assert resolved_nullable.values["category"] is None


def test_snapshot_records_full_audit_trail():
    resolved = resolve_input_mapping(make_payload(), {"summary": "summarizer.output.summary"})
    snapshot = resolved.as_snapshot()
    assert snapshot["values"]["summary"]
    assert snapshot["resolutions"]["summary"]["status"] == "resolved"
    assert snapshot["errors"] == []
