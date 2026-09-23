"""Agent execution payload validation and identifier extraction.

Every payload must carry agent identity, payload metadata, correlation, and
`trace_context.trace.spans`. Those identifiers are checked before a job is
created.
"""

from __future__ import annotations

import re
from typing import Any

from evalorch.services.context_resolver import extract_spans

KNOWN_SECTIONS = (
    "agent_registry",
    "payload_metadata",
    "correlation",
    "trace_context",
)

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_OTEL_TRACE_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_OTEL_SPAN_ID_RE = re.compile(r"^[0-9a-fA-F]{16}$")


class PayloadValidationError(ValueError):
    pass


def _first_str(payload: dict[str, Any], paths: list[tuple[str, ...]]) -> str | None:
    for path in paths:
        current: Any = payload
        for token in path:
            if not isinstance(current, dict) or token not in current:
                current = None
                break
            current = current[token]
        if isinstance(current, (str, int)) and not isinstance(current, bool):
            text = str(current).strip()
            if text:
                return text
    return None


def is_uuid(value: Any) -> bool:
    return isinstance(value, str) and _UUID_RE.match(value.strip()) is not None


def is_otel_trace_id(value: Any) -> bool:
    """32 hex characters. Hyphenated UUIDs are not OpenTelemetry trace ids."""
    return isinstance(value, str) and _OTEL_TRACE_ID_RE.match(value.strip()) is not None


def is_otel_span_id(value: Any) -> bool:
    """16 hex characters. Hyphenated UUIDs are not OpenTelemetry span ids."""
    return isinstance(value, str) and _OTEL_SPAN_ID_RE.match(value.strip()) is not None


def extract_identifiers(payload: dict[str, Any]) -> dict[str, str | None]:
    """Pull the indexed identifiers out of a complete payload."""
    return {
        "external_payload_id": _first_str(
            payload,
            [("payload_metadata", "payload_id")],
        ),
        "trace_id": _first_str(
            payload,
            [
                ("correlation", "trace_id"),
                ("trace_context", "trace", "trace_id"),
            ],
        ),
        "session_id": _first_str(
            payload,
            [
                ("correlation", "session_id"),
                ("trace_context", "trace", "session_id"),
            ],
        ),
        "event_id": _first_str(payload, [("correlation", "event_id")]),
        "invocation_id": _first_str(
            payload,
            [
                ("correlation", "invocation_id"),
                ("trace_context", "trace", "invocation_id"),
            ],
        ),
        "payload_version": _first_str(
            payload,
            [
                ("payload_metadata", "payload_version"),
            ],
        ),
    }


def _require_mapping(payload: dict[str, Any], key: str, label: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise PayloadValidationError(f"{label}: {key} must be an object")
    return value


def _require_text(container: dict[str, Any], key: str, label: str) -> str:
    value = container.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PayloadValidationError(f"{label}: {key} is required and must be a non-empty string")
    return value.strip()


def _require_uuid(container: dict[str, Any], key: str, label: str) -> str:
    value = _require_text(container, key, label)
    if not is_uuid(value):
        raise PayloadValidationError(f"{label}: {key} must be a UUID, got {value!r}")
    return value


def _require_same(left: str, right: str, label: str, field: str) -> None:
    if left != right:
        raise PayloadValidationError(
            f"{label}: {field} must be the same value everywhere, got {left!r} and {right!r}"
        )


def _validate_span_tree(spans: list[dict[str, Any]], trace_id: str, label: str) -> None:
    """Accept any number of spans, including siblings and deeper children."""
    by_id: dict[str, dict[str, Any]] = {}
    for index, span in enumerate(spans):
        where = f"{label}: trace_context.trace.spans[{index}]"
        if not isinstance(span, dict):
            raise PayloadValidationError(f"{where} must be an object")
        span_id = _require_text(span, "span_id", where)
        if not is_otel_span_id(span_id):
            raise PayloadValidationError(f"{where}: span_id must be a 16-character OpenTelemetry span id")
        if span_id in by_id:
            raise PayloadValidationError(f"{label}: duplicate span_id {span_id!r}")
        span_trace = _require_text(span, "trace_id", where)
        if not is_otel_trace_id(span_trace):
            raise PayloadValidationError(
                f"{where}: trace_id must be a 32-character OpenTelemetry trace id"
            )
        _require_same(span_trace, trace_id, where, "trace_id")
        if "parent_span_id" not in span:
            raise PayloadValidationError(f"{where}: parent_span_id is required (use null for the root)")
        parent = span.get("parent_span_id")
        if parent is not None and (not isinstance(parent, str) or not is_otel_span_id(parent)):
            raise PayloadValidationError(
                f"{where}: parent_span_id must be null or a 16-character OpenTelemetry span id"
            )
        _require_text(span, "name", where)
        for field in ("kind", "started_at", "ended_at", "status"):
            _require_text(span, field, where)
        if "duration_ms" not in span or isinstance(span.get("duration_ms"), bool) or not isinstance(
            span.get("duration_ms"), (int, float)
        ):
            raise PayloadValidationError(f"{where}: duration_ms must be a number")
        if "error_message" not in span:
            raise PayloadValidationError(f"{where}: error_message is required")
        if not isinstance(span.get("attributes"), dict):
            raise PayloadValidationError(f"{where}: attributes must be an object")
        if not isinstance(span.get("events"), list):
            raise PayloadValidationError(f"{where}: events must be an array")
        if not isinstance(span.get("links"), list):
            raise PayloadValidationError(f"{where}: links must be an array")
        by_id[span_id] = span

    if spans and not any(span.get("parent_span_id") is None for span in spans):
        raise PayloadValidationError(f"{label}: trace_context.trace.spans requires a root span")

    for span in spans:
        seen: set[str] = set()
        current = span
        while current.get("parent_span_id") is not None:
            parent_id = str(current["parent_span_id"])
            if parent_id in seen or parent_id == current.get("span_id"):
                raise PayloadValidationError(
                    f"{label}: span {current.get('span_id')!r} has a parent cycle"
                )
            seen.add(parent_id)
            parent = by_id.get(parent_id)
            if parent is None:
                raise PayloadValidationError(
                    f"{label}: span {current.get('span_id')!r} parent_span_id {parent_id!r} "
                    "does not match a span in this trace"
                )
            current = parent


def enforce_payload_contract(payload: dict[str, Any], *, label: str = "payload") -> str:
    """Validate the execution payload contract. Returns the agent UUID."""
    registry = _require_mapping(payload, "agent_registry", label)
    metadata = _require_mapping(payload, "payload_metadata", label)
    correlation = _require_mapping(payload, "correlation", label)
    trace_context = _require_mapping(payload, "trace_context", label)
    trace = trace_context.get("trace")
    if not isinstance(trace, dict):
        raise PayloadValidationError(f"{label}: trace_context.trace must be an object")

    agent_id = _require_uuid(registry, "agent_id", f"{label}: agent_registry")
    correlation_agent = _require_uuid(correlation, "agent_id", f"{label}: correlation")
    _require_same(agent_id, correlation_agent, label, "agent_id")

    _require_uuid(metadata, "payload_id", f"{label}: payload_metadata")
    for field in ("payload_version", "payload_type", "created_at", "evaluation_mode"):
        _require_text(metadata, field, f"{label}: payload_metadata")

    _require_uuid(correlation, "event_id", f"{label}: correlation")
    trace_id = _require_text(correlation, "trace_id", f"{label}: correlation")
    if not is_otel_trace_id(trace_id):
        raise PayloadValidationError(
            f"{label}: correlation.trace_id must be a 32-character OpenTelemetry trace id"
        )
    session_id = _require_uuid(correlation, "session_id", f"{label}: correlation")
    invocation_id = _require_uuid(correlation, "invocation_id", f"{label}: correlation")
    user_id = _require_text(correlation, "user_id", f"{label}: correlation")
    app_name = _require_text(correlation, "app_name", f"{label}: correlation")
    _require_text(correlation, "service_name", f"{label}: correlation")

    trace_trace_id = _require_text(trace, "trace_id", f"{label}: trace_context.trace")
    if not is_otel_trace_id(trace_trace_id):
        raise PayloadValidationError(
            f"{label}: trace_context.trace.trace_id must be a 32-character OpenTelemetry trace id"
        )
    _require_same(trace_id, trace_trace_id, label, "trace_id")
    for field in ("started_at_raw", "ended_at_raw", "service_name", "root_span_name_raw", "status"):
        _require_text(trace, field, f"{label}: trace_context.trace")
    if "duration_ms" not in trace or isinstance(trace.get("duration_ms"), bool) or not isinstance(
        trace.get("duration_ms"), (int, float)
    ):
        raise PayloadValidationError(f"{label}: trace_context.trace.duration_ms must be a number")
    if "error_message" not in trace:
        raise PayloadValidationError(f"{label}: trace_context.trace.error_message is required")
    _require_same(
        session_id,
        _require_uuid(trace, "session_id", f"{label}: trace_context.trace"),
        label,
        "session_id",
    )
    _require_same(
        invocation_id,
        _require_uuid(trace, "invocation_id", f"{label}: trace_context.trace"),
        label,
        "invocation_id",
    )
    _require_same(
        user_id,
        _require_text(trace, "user_id", f"{label}: trace_context.trace"),
        label,
        "user_id",
    )
    _require_same(
        app_name,
        _require_text(trace, "app_name", f"{label}: trace_context.trace"),
        label,
        "app_name",
    )

    spans = trace.get("spans")
    if not isinstance(spans, list):
        raise PayloadValidationError(f"{label}: trace_context.trace.spans must be an array")
    _validate_span_tree(spans, trace_id, label)
    return agent_id


def validate_payload(payload: Any, *, index: int | None = None) -> dict[str, Any]:
    """Validate one payload and return a descriptive report."""
    label = f"payload #{index}" if index is not None else "payload"
    if not isinstance(payload, dict):
        raise PayloadValidationError(f"{label} must be a JSON object, got {type(payload).__name__}")
    if not payload:
        raise PayloadValidationError(f"{label} must not be empty")

    enforce_payload_contract(payload, label=label)

    sections_present = [name for name in KNOWN_SECTIONS if payload.get(name) is not None]
    spans = extract_spans(payload)
    identifiers = extract_identifiers(payload)

    warnings: list[str] = []
    if not spans:
        warnings.append("no agent spans found; span-level checks will be NOT_APPLICABLE")
    if not identifiers["trace_id"]:
        warnings.append("no trace_id found")
    if not sections_present:
        warnings.append("no known payload sections found (payload stored verbatim)")

    span_names = []
    for span in spans:
        name = span.get("name") or span.get("span_id")
        if name:
            span_names.append(str(name))

    return {
        "valid": True,
        "sections_present": sections_present,
        "span_count": len(spans),
        "span_names": span_names,
        "identifiers": identifiers,
        "warnings": warnings,
        "validation_engine": "eval-platform-payload-v1",
    }
