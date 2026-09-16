"""Agent execution payload validation and identifier extraction.

Payloads are stored complete and unmodified. Validation is descriptive, not
restrictive: multi-agent payloads differ between workflows, so the report records
which known sections were found instead of rejecting unfamiliar shapes.
"""

from __future__ import annotations

import json
from typing import Any

from app.services.context_resolver import extract_spans

KNOWN_SECTIONS = (
    "payload_metadata",
    "trace_data",
    "session_context",
    "spans",
    "resolved_configuration",
)


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


def extract_identifiers(payload: dict[str, Any]) -> dict[str, str | None]:
    """Pull the indexed identifiers out of a complete payload."""
    return {
        "external_payload_id": _first_str(
            payload,
            [
                ("payload_metadata", "payload_id"),
                ("payload_id",),
                ("id",),
                ("payload_metadata", "id"),
            ],
        ),
        "trace_id": _first_str(
            payload, [("trace_data", "trace_id"), ("trace_id",), ("trace", "trace_id")]
        ),
        "session_id": _first_str(
            payload,
            [
                ("trace_data", "session_id"),
                ("session_context", "session_id"),
                ("session_id",),
            ],
        ),
        "workflow_id": _first_str(
            payload, [("trace_data", "workflow_id"), ("workflow_id",)]
        ),
        "payload_version": _first_str(
            payload,
            [("payload_metadata", "version"), ("version",), ("payload_version",)],
        ),
    }


def validate_payload(payload: Any, *, index: int | None = None) -> dict[str, Any]:
    """Validate one payload and return a descriptive report."""
    label = f"payload #{index}" if index is not None else "payload"
    if not isinstance(payload, dict):
        raise PayloadValidationError(f"{label} must be a JSON object, got {type(payload).__name__}")
    if not payload:
        raise PayloadValidationError(f"{label} must not be empty")

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

    agents = []
    for span in spans:
        name = span.get("agent_id") or span.get("agent_type") or span.get("span_id")
        if name:
            agents.append(str(name))

    return {
        "valid": True,
        "sections_present": sections_present,
        "span_count": len(spans),
        "agents": agents,
        "identifiers": identifiers,
        "warnings": warnings,
        "validation_engine": "eval-platform-payload-v1",
    }


def parse_json_bytes(content: bytes) -> list[dict[str, Any]]:
    """Parse a .json upload: a single payload, a list, or {"payloads": [...]}"""
    try:
        document = json.loads(content.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise PayloadValidationError(f"file is not valid UTF-8: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise PayloadValidationError(f"invalid JSON: {exc}") from exc

    if isinstance(document, list):
        payloads = document
    elif isinstance(document, dict):
        for key in ("payloads", "records", "items", "data"):
            if isinstance(document.get(key), list):
                payloads = document[key]
                break
        else:
            payloads = [document]
    else:
        raise PayloadValidationError("JSON file must contain an object or an array")

    if not payloads:
        raise PayloadValidationError("file must contain at least one payload")
    return payloads


def parse_jsonl_bytes(content: bytes) -> list[dict[str, Any]]:
    """Parse a .jsonl upload: one payload per non-blank line."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PayloadValidationError(f"file is not valid UTF-8: {exc}") from exc

    payloads: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise PayloadValidationError(f"invalid JSON on line {line_number}: {exc}") from exc
        if not isinstance(record, dict):
            raise PayloadValidationError(f"line {line_number} must be a JSON object")
        payloads.append(record)

    if not payloads:
        raise PayloadValidationError("file must contain at least one payload")
    return payloads


def parse_upload(filename: str | None, content: bytes) -> tuple[list[dict[str, Any]], str]:
    """Dispatch on file extension. Returns (payloads, source_type)."""
    name = (filename or "").lower()
    if name.endswith(".jsonl") or name.endswith(".ndjson"):
        return parse_jsonl_bytes(content), "jsonl"
    if name.endswith(".json"):
        return parse_json_bytes(content), "json"
    raise PayloadValidationError("file must have a .json, .jsonl, or .ndjson extension")
