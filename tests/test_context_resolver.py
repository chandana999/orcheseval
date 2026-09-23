"""Context resolver behaviour. No database required."""

from __future__ import annotations

from evalorch.services.context_resolver import (
    ErrorCode,
    extract_spans,
    find_span,
    resolve_input_mapping,
    resolve_path,
    scope_payload_to_spans,
)
from evalorch.services.evaluation_service import _metric_span_names
from tests.conftest import make_payload


def test_resolves_session_trace_and_span_paths():
    payload = make_payload(category="billing")
    resolved = resolve_input_mapping(
        payload,
        {
            "history": "classifier.attributes.conversation_history",
            "trace": "correlation.trace_id",
            "category": "classifier.attributes.output.category",
            "summary": "summarizer.attributes.output.summary",
            "first_tool": "validator.attributes.tool_calls[0].name",
        },
    )
    assert resolved.ok
    assert len(resolved.values["trace"]) == 32
    assert resolved.values["category"] == "billing"
    assert resolved.values["first_tool"] == "policy_lookup"
    assert len(resolved.values["history"]) == 2
    assert resolved.resolutions["category"]["span_selector"] == "name"


def test_explicit_span_selectors_and_order():
    payload = make_payload()
    resolved = resolve_input_mapping(
        payload,
        {
            "by_name": "summarizer.attributes.output.category",
            "by_span_id": {"path": "spans.1.name"},
            "second_span": "spans.1.name",
        },
    )
    assert resolved.ok, resolved.errors
    assert resolved.values["second_span"] == "validator"
    assert resolved.values["by_span_id"] == "validator"


def test_missing_span_is_reported_not_guessed():
    payload = make_payload(include_summarizer=False)
    resolved = resolve_input_mapping(payload, {"summary": "summarizer.attributes.output.summary"})
    assert not resolved.ok
    assert resolved.missing_required == ["summary"]
    assert resolved.errors[0].code == ErrorCode.MISSING_SPAN
    assert "summarizer" not in (resolved.resolutions["summary"]["available_spans"] or [])


def test_optional_mapping_and_default_do_not_block_evaluation():
    payload = make_payload(include_summarizer=False)
    resolved = resolve_input_mapping(
        payload,
        {
            "summary": {"path": "summarizer.attributes.output.summary", "required": False},
            "fallback": {"path": "summarizer.attributes.output.summary", "default": "n/a"},
        },
    )
    assert resolved.ok
    assert resolved.values["fallback"] == "n/a"
    assert "summary" not in resolved.values


def test_type_validation_rejects_wrong_shape():
    payload = make_payload()
    resolved = resolve_input_mapping(
        payload, {"category": {"path": "classifier.attributes.output.category", "type": "object"}}
    )
    assert not resolved.ok
    assert resolved.errors[0].code == ErrorCode.INVALID_TYPE


def test_wildcard_and_list_key_traversal():
    payload = make_payload()
    resolved = resolve_input_mapping(
        payload,
        {
            "names": "spans.*.name",
            "roles": "classifier.attributes.conversation_history.role",
        },
    )
    assert resolved.values["names"] == ["classifier", "validator", "summarizer"]
    assert resolved.values["roles"] == ["user", "assistant"]


def test_spans_are_ordered_by_declared_order():
    payload = make_payload(spans_out_of_order=True)
    spans = extract_spans(payload)
    assert [s["name"] for s in spans] == ["classifier", "validator", "summarizer"]


def test_top_level_spans_are_ignored():
    payload = make_payload()
    payload["spans"] = [{"name": "other", "span_id": "aaaaaaaaaaaaaaaa"}]
    spans = extract_spans(payload)
    assert [span["name"] for span in spans] == ["classifier", "validator", "summarizer"]
    value, code, _detail = resolve_path(payload, "other.attributes")
    assert value is None
    assert code == ErrorCode.MISSING_SPAN


def test_whole_payload_and_spans_list_access():
    payload = make_payload()
    resolved = resolve_input_mapping(payload, {"spans": "spans", "meta": "payload_metadata"})
    assert len(resolved.values["spans"]) == 3
    assert resolved.values["meta"]["payload_version"] == "1.0"


def test_metric_sees_only_its_named_spans():
    payload = make_payload()
    payload["trace_context"]["trace"]["spans"].append(
        {
            "span_id": "9999999999999999",
            "trace_id": payload["correlation"]["trace_id"],
            "parent_span_id": None,
            "name": "retriever",
            "kind": "internal",
            "started_at": "2026-01-05T10:00:04Z",
            "ended_at": "2026-01-05T10:00:04Z",
            "duration_ms": 10,
            "status": "ok",
            "error_message": None,
            "attributes": {"output": {"blob": "x" * 1000}},
            "events": [],
            "links": [],
        }
    )
    scoped = scope_payload_to_spans(payload, ["summarizer"])
    assert [span["name"] for span in extract_spans(payload)] == [
        "classifier",
        "validator",
        "retriever",
        "summarizer",
    ]
    assert [span["name"] for span in extract_spans(scoped)] == ["summarizer"]
    resolved = resolve_input_mapping(scoped, {"spans": "spans", "summary": "summarizer.attributes.output.summary"})
    assert resolved.ok
    assert [span["name"] for span in resolved.values["spans"]] == ["summarizer"]
    assert "blob" not in str(resolved.values)

    untouched = scope_payload_to_spans(payload, [])
    assert len(extract_spans(untouched)) == 4


def test_span_identity_scopes_and_selects_every_field():
    payload = make_payload()
    spans = payload["trace_context"]["trace"]["spans"]
    spans[0]["agent_id"] = "agent-classifier"
    spans[0]["agent_type"] = "router"
    spans[1]["agent_id"] = "agent-validator"
    spans[1]["agent_type"] = "tool"
    spans[2]["agent_id"] = "agent-summarizer"
    spans[2]["agent_type"] = "writer"
    classifier_id = spans[0]["span_id"]

    by_agent = scope_payload_to_spans(payload, ["agent-validator"])
    by_type = scope_payload_to_spans(payload, ["writer"])
    assert [span["name"] for span in extract_spans(by_agent)] == ["validator"]
    assert [span["name"] for span in extract_spans(by_type)] == ["summarizer"]

    ordered = extract_spans(payload)
    selected = {
        "agent_id:agent-classifier": "classifier",
        "agent_type:tool": "validator",
        "name:summarizer": "summarizer",
        f"span_id:{classifier_id}": "classifier",
    }
    for token, expected in selected.items():
        span, selector, error = find_span(ordered, token)
        assert error is None
        assert span is not None
        assert span["name"] == expected
        assert selector == token.split(":", 1)[0]


def test_equals_path_names_the_span_it_addresses():
    names = _metric_span_names(
        {
            "span_names": ["summarizer"],
            "applies_when": {
                "required_spans": ["validator"],
                "equals": {"spans.research_agent.status": "ok"},
            },
        }
    )
    assert names[0] == "summarizer"
    assert "research_agent" in names
    assert "validator" not in names

    from_required = _metric_span_names(
        {"applies_when": {"required_spans": ["validator"]}}
    )
    assert from_required == ["validator"]


def test_unsupported_mapping_value_is_rejected():
    resolved = resolve_input_mapping(make_payload(), {"bad": 42})
    assert not resolved.ok
    assert resolved.errors[0].code == ErrorCode.UNSUPPORTED_MAPPING


def test_null_value_is_distinguished_from_missing_path():
    payload = make_payload()
    payload["trace_context"]["trace"]["spans"][0]["attributes"]["output"]["category"] = None
    resolved = resolve_input_mapping(payload, {"category": "classifier.attributes.output.category"})
    assert resolved.errors[0].code == ErrorCode.NULL_VALUE

    resolved_nullable = resolve_input_mapping(
        payload, {"category": {"path": "classifier.attributes.output.category", "nullable": True}}
    )
    assert resolved_nullable.ok
    assert resolved_nullable.values["category"] is None


def test_snapshot_records_full_audit_trail():
    resolved = resolve_input_mapping(make_payload(), {"summary": "summarizer.attributes.output.summary"})
    snapshot = resolved.as_snapshot()
    assert snapshot["values"]["summary"]
    assert snapshot["resolutions"]["summary"]["status"] == "resolved"
    assert snapshot["errors"] == []
