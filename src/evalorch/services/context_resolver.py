"""Configuration-driven context resolution.

Evaluators never dig through raw payloads. A check declares an `input_mapping`,
this module turns the complete payload stored in PostgreSQL into a normalized
input dictionary, and the evaluator only sees that dictionary.

Supported mapping values::

    "correlation.session_id"                    # correlation path
    "payload_metadata.payload_id"               # metadata path
    "summarizer.attributes.output"              # span path, matched by span name
    "name:<name>.attributes"                    # explicit span name
    "agent_id:<id>.attributes"                  # explicit span agent id
    "agent_type:<type>.attributes"              # explicit span agent type
    "span_id:<id>.attributes"                   # explicit span id
    "spans"                                     # trace_context.trace.spans, ordered by started_at
    "spans.1.name"                              # index into that ordered array
    "spans.*.name"                              # wildcard over the span array

    {"path": "summarizer.output", "required": false, "type": "object"}

Every failure is reported with a structured code instead of a silent wrong value.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# Sections addressed directly on the payload document.
RESERVED_SECTIONS = frozenset(
    {
        "payload",
        "agent_registry",
        "payload_metadata",
        "correlation",
        "trace_context",
        "spans",
    }
)

SPAN_IDENTITY_KEYS = ("name", "agent_id", "agent_type", "span_id")

TYPE_MAP: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "object": (dict,),
    "array": (list,),
    "any": (object,),
}

_INDEX_RE = re.compile(r"\[(\d+|\*)\]")


class ErrorCode:
    MISSING_SPAN = "MISSING_SPAN"
    MISSING_PATH = "MISSING_PATH"
    NULL_VALUE = "NULL_VALUE"
    INVALID_TYPE = "INVALID_TYPE"
    UNSUPPORTED_MAPPING = "UNSUPPORTED_MAPPING"
    AMBIGUOUS_SPAN = "AMBIGUOUS_SPAN"


@dataclass
class ResolutionError:
    key: str
    path: str | None
    code: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "path": self.path, "code": self.code, "message": self.message}


@dataclass
class ResolvedInput:
    """Normalized evaluator input plus a full audit trail of how it was built."""

    values: dict[str, Any] = field(default_factory=dict)
    resolutions: dict[str, dict[str, Any]] = field(default_factory=dict)
    errors: list[ResolutionError] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing_required

    def error_codes(self) -> list[str]:
        return sorted({e.code for e in self.errors})

    def as_snapshot(self) -> dict[str, Any]:
        """Serializable record of the resolution, stored on ticket and result."""
        return {
            "values": self.values,
            "resolutions": self.resolutions,
            "errors": [e.as_dict() for e in self.errors],
            "missing_required": list(self.missing_required),
        }


class ContextResolutionError(RuntimeError):
    """Raised when required context is unavailable and the policy is to fail."""

    def __init__(self, resolved: ResolvedInput) -> None:
        codes = ", ".join(resolved.error_codes()) or "MISSING_CONTEXT"
        keys = ", ".join(resolved.missing_required)
        super().__init__(f"required context unavailable ({codes}) for: {keys}")
        self.resolved = resolved


# --------------------------------------------------------------------------- spans
def _coerce_span(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    return dict(raw)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


def scope_payload_to_spans(payload: dict[str, Any], span_names: list[Any] | None) -> dict[str, Any]:
    """Return a view of the shared payload limited to this metric's spans.

    The source document stays on disk and is not copied onto the ticket. When
    the metric names spans, only those spans are visible to mapping and to the
    stored input. A metric with no span names still sees the full trace.
    """
    wanted = {str(name).strip() for name in (span_names or []) if str(name).strip()}
    if not wanted:
        return payload
    trace_context = payload.get("trace_context")
    if not isinstance(trace_context, dict):
        return payload
    trace = trace_context.get("trace")
    if not isinstance(trace, dict):
        return payload
    raw = trace.get("spans")
    if not isinstance(raw, list):
        return payload
    kept = [
        span
        for span in raw
        if isinstance(span, dict) and any(span_has_identity(span, token) for token in wanted)
    ]
    scoped_trace = dict(trace)
    scoped_trace["spans"] = kept
    scoped_context = dict(trace_context)
    scoped_context["trace"] = scoped_trace
    scoped = dict(payload)
    scoped["trace_context"] = scoped_context
    return scoped


def extract_spans(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every span in trace_context.trace.spans, ordered by started_at.

    The array can be empty or hold any number of spans. Sibling spans and deeper
    children stay in the list; parent_span_id is left on each span. A top-level
    spans field is ignored.
    """
    trace_context = payload.get("trace_context")
    trace = trace_context.get("trace") if isinstance(trace_context, dict) else None
    raw = trace.get("spans") if isinstance(trace, dict) else None
    if not isinstance(raw, list):
        return []
    spans = [span for span in (_coerce_span(item) for item in raw) if span]
    return order_spans(spans)


def order_spans(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order spans by started_at, then original position. Parent links stay on each span."""

    def sort_key(item: tuple[int, dict[str, Any]]):
        index, span = item
        start = _parse_time(span.get("started_at"))
        return (0 if start else 1, start or datetime.min, index)

    return [span for _, span in sorted(enumerate(spans), key=sort_key)]


def _span_label(span: dict[str, Any]) -> str:
    return str(span.get("name") or span.get("span_id") or "")


def split_span_selector(token: str) -> tuple[str | None, str]:
    """Split name:, agent_id:, agent_type:, or span_id: from a path token."""
    for key in SPAN_IDENTITY_KEYS:
        prefix = f"{key}:"
        if token.startswith(prefix):
            return key, token[len(prefix) :]
    return None, token


def span_has_identity(span: dict[str, Any], wanted: str, *, key: str | None = None) -> bool:
    """True when wanted equals one span identity field, or the named field."""
    if not wanted:
        return False
    keys = (key,) if key else SPAN_IDENTITY_KEYS
    return any(str(span.get(item) or "") == wanted for item in keys)


def find_span(
    spans: list[dict[str, Any]], token: str
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Resolve one span from a trace of any length.

    Returns (span, selector, error_code). A selector that matches more than
    one span is ambiguous. Identity prefixes select that field only.
    """
    key, wanted = split_span_selector(token)
    if key is not None:
        matches = [span for span in spans if span_has_identity(span, wanted, key=key)]
        if len(matches) > 1:
            return None, key, ErrorCode.AMBIGUOUS_SPAN
        if len(matches) == 1:
            return matches[0], key, None
        return None, key, None
    if token.startswith("order:"):
        raw = token[len("order:") :]
        try:
            position = int(raw)
        except ValueError:
            return None, "order", None
        if 1 <= position <= len(spans):
            return spans[position - 1], "order", None
        return None, "order", None

    name_matches = [span for span in spans if span_has_identity(span, token, key="name")]
    if len(name_matches) > 1:
        return None, "name", ErrorCode.AMBIGUOUS_SPAN
    if len(name_matches) == 1:
        return name_matches[0], "name", None
    id_matches = [span for span in spans if span_has_identity(span, token, key="span_id")]
    if len(id_matches) == 1:
        return id_matches[0], "span_id", None
    return None, None, None


# --------------------------------------------------------------------------- paths
def parse_path(path: str) -> list[str]:
    """Split a mapping path into tokens, expanding [i] / [*] into own tokens."""
    tokens: list[str] = []
    for raw_segment in path.split("."):
        segment = raw_segment.strip()
        if not segment:
            continue
        indices = _INDEX_RE.findall(segment)
        base = _INDEX_RE.sub("", segment)
        if base:
            tokens.append(base)
        tokens.extend(indices)
    return tokens


def _traverse(value: Any, tokens: list[str], walked: list[str]) -> tuple[Any, str | None, str]:
    """Walk tokens through dicts/lists. Returns (value, error_code, location)."""
    current = value
    index = 0
    while index < len(tokens):
        token = tokens[index]
        location = ".".join(walked) or "<root>"
        if token == "*":
            if not isinstance(current, list):
                return None, ErrorCode.INVALID_TYPE, location
            remaining = tokens[index + 1 :]
            walked.append("*")
            if not remaining:
                return current, None, ".".join(walked)
            mapped: list[Any] = []
            for item in current:
                value, code, _location = _traverse(item, remaining, list(walked))
                if code is None:
                    mapped.append(value)
            if not mapped and current:
                return None, ErrorCode.MISSING_PATH, ".".join(walked)
            return mapped, None, ".".join(walked)
        if isinstance(current, list):
            if token.isdigit():
                list_index = int(token)
                if list_index >= len(current):
                    return None, ErrorCode.MISSING_PATH, f"{location}[{token}]"
                current = current[list_index]
                walked.append(f"[{token}]")
                index += 1
                continue
            # Allow key traversal across a list of objects by mapping over it.
            mapped = [item.get(token) for item in current if isinstance(item, dict)]
            if not mapped:
                return None, ErrorCode.MISSING_PATH, f"{location}.{token}"
            current = mapped
            walked.append(token)
            index += 1
            continue
        if isinstance(current, dict):
            if token not in current:
                return None, ErrorCode.MISSING_PATH, f"{location}.{token}"
            current = current[token]
            walked.append(token)
            index += 1
            continue
        return None, ErrorCode.MISSING_PATH, f"{location}.{token}"
    return current, None, ".".join(walked) or "<root>"


def resolve_path(payload: dict[str, Any], path: str) -> tuple[Any, str | None, dict[str, Any]]:
    """Resolve one mapping path against a complete payload.

    Returns (value, error_code, detail) where detail records how the value was
    found (root kind, span selector, span id) for the audit trail.
    """
    tokens = parse_path(path)
    detail: dict[str, Any] = {"path": path}
    if not tokens:
        return None, ErrorCode.UNSUPPORTED_MAPPING, detail

    head, *rest = tokens
    spans = extract_spans(payload)

    if head == "payload":
        detail["root"] = "payload"
        value, code, location = _traverse(payload, rest, ["payload"])
        detail["resolved_from"] = location
        return value, code, detail

    if head == "spans":
        detail["root"] = "spans"
        if not rest:
            detail["resolved_from"] = "spans"
            detail["span_count"] = len(spans)
            return spans, None, detail
        first, *tail = rest
        if first == "*" or first.isdigit():
            value, code, location = _traverse(spans, rest, ["spans"])
            detail["resolved_from"] = location
            return value, code, detail
        span, selector, error = find_span(spans, first)
        detail["available_spans"] = [_span_label(span_item) for span_item in spans]
        if error == ErrorCode.AMBIGUOUS_SPAN:
            detail["span_selector"] = selector
            return None, ErrorCode.AMBIGUOUS_SPAN, detail
        if span is None:
            detail["span_selector"] = selector or "auto"
            return None, ErrorCode.MISSING_SPAN, detail
        detail["span_selector"] = selector
        detail["span_id"] = span.get("span_id")
        detail["name"] = span.get("name")
        value, code, location = _traverse(span, tail, [f"spans.{first}"])
        detail["resolved_from"] = location
        return value, code, detail

    if head in RESERVED_SECTIONS:
        detail["root"] = head
        section = payload.get(head)
        if section is None:
            detail["resolved_from"] = head
            return None, ErrorCode.MISSING_PATH, detail
        value, code, location = _traverse(section, rest, [head])
        detail["resolved_from"] = location
        return value, code, detail

    # Span-level path: bare span name, span id, or span_id: selector.
    detail["root"] = "span"
    span, selector, error = find_span(spans, head)
    detail["available_spans"] = [_span_label(span_item) for span_item in spans]
    if error == ErrorCode.AMBIGUOUS_SPAN:
        detail["span_selector"] = selector
        return None, ErrorCode.AMBIGUOUS_SPAN, detail
    if span is None:
        detail["span_selector"] = selector or "auto"
        return None, ErrorCode.MISSING_SPAN, detail
    detail["span_selector"] = selector
    detail["span_id"] = span.get("span_id")
    detail["name"] = span.get("name")
    value, code, location = _traverse(span, rest, [head])
    detail["resolved_from"] = location
    return value, code, detail


# ------------------------------------------------------------------------ mapping
def _check_type(value: Any, expected: str) -> bool:
    types = TYPE_MAP.get(expected.lower())
    if types is None:
        return True
    if expected.lower() == "number" and isinstance(value, bool):
        return False
    if expected.lower() == "integer" and isinstance(value, bool):
        return False
    return isinstance(value, types)


def resolve_input_mapping(
    payload: dict[str, Any],
    input_mapping: dict[str, Any] | None,
    *,
    allow_null: bool = False,
) -> ResolvedInput:
    """Build normalized evaluator input from a check's input_mapping."""
    resolved = ResolvedInput()
    if not input_mapping:
        return resolved
    if not isinstance(input_mapping, dict):
        resolved.errors.append(
            ResolutionError(
                key="input_mapping",
                path=None,
                code=ErrorCode.UNSUPPORTED_MAPPING,
                message="input_mapping must be a JSON object",
            )
        )
        resolved.missing_required.append("input_mapping")
        return resolved

    for key, spec in input_mapping.items():
        required = True
        expected_type: str | None = None
        default: Any = None
        has_default = False
        nullable = allow_null

        if isinstance(spec, str):
            path = spec
        elif isinstance(spec, dict):
            path = spec.get("path")
            required = bool(spec.get("required", True))
            expected_type = spec.get("type")
            nullable = bool(spec.get("nullable", allow_null))
            if "default" in spec:
                has_default = True
                default = spec["default"]
            if not isinstance(path, str) or not path:
                resolved.errors.append(
                    ResolutionError(
                        key=key,
                        path=None,
                        code=ErrorCode.UNSUPPORTED_MAPPING,
                        message="mapping object must contain a non-empty string 'path'",
                    )
                )
                if required:
                    resolved.missing_required.append(key)
                continue
        else:
            resolved.errors.append(
                ResolutionError(
                    key=key,
                    path=None,
                    code=ErrorCode.UNSUPPORTED_MAPPING,
                    message=f"unsupported mapping value of type {type(spec).__name__}",
                )
            )
            if required:
                resolved.missing_required.append(key)
            continue

        value, code, detail = resolve_path(payload, path)
        detail["required"] = required

        if code is None and value is None and not nullable:
            code = ErrorCode.NULL_VALUE
        if code is None and expected_type and not _check_type(value, expected_type):
            code = ErrorCode.INVALID_TYPE
            detail["expected_type"] = expected_type
            detail["actual_type"] = type(value).__name__

        if code is None:
            resolved.values[key] = value
            detail["status"] = "resolved"
            resolved.resolutions[key] = detail
            continue

        message = {
            ErrorCode.MISSING_SPAN: f"no span matched '{path.split('.')[0]}'",
            ErrorCode.AMBIGUOUS_SPAN: f"more than one span is named '{path.split('.')[0]}'",
            ErrorCode.MISSING_PATH: f"path '{path}' not present in payload",
            ErrorCode.NULL_VALUE: f"path '{path}' resolved to null",
            ErrorCode.INVALID_TYPE: (
                f"path '{path}' expected {expected_type}, got {type(value).__name__}"
            ),
            ErrorCode.UNSUPPORTED_MAPPING: f"mapping for '{key}' is not supported",
        }.get(code, f"could not resolve '{path}'")

        resolved.errors.append(
            ResolutionError(key=key, path=path, code=code, message=message)
        )
        detail["status"] = code

        if has_default:
            resolved.values[key] = default
            detail["status"] = "default"
            detail["used_default"] = True
        elif required:
            resolved.missing_required.append(key)

        resolved.resolutions[key] = detail

    return resolved
