"""JSON schema / output structure validation.

Implements the commonly used subset of JSON Schema (type, required, properties,
items, enum, const, bounds, additionalProperties) with the standard library so
the platform does not gain another dependency.
"""

from __future__ import annotations

import json
from typing import Any

from app.evaluators.base import Evaluator, EvaluatorOutput, ExecutionContext
from app.models.enums import CheckType
from app.services.errors import InvalidEvaluatorConfigError

TYPE_MAP: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "null": (type(None),),
}


def _type_matches(value: Any, expected: str) -> bool:
    types = TYPE_MAP.get(expected)
    if types is None:
        return True
    if expected in {"number", "integer"} and isinstance(value, bool):
        return False
    return isinstance(value, types)


def validate_schema(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Return a list of human readable violations (empty means valid)."""
    errors: list[str] = []
    if not isinstance(schema, dict):
        raise InvalidEvaluatorConfigError("schema must be a JSON object")

    expected_type = schema.get("type")
    if expected_type:
        candidates = expected_type if isinstance(expected_type, list) else [expected_type]
        if not any(_type_matches(value, t) for t in candidates):
            errors.append(
                f"{path}: expected type {'/'.join(candidates)}, got {type(value).__name__}"
            )
            return errors

    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: expected const {schema['const']!r}, got {value!r}")

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: value {value!r} not in enum {schema['enum']!r}")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < int(schema["minLength"]):
            errors.append(f"{path}: shorter than minLength {schema['minLength']}")
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            errors.append(f"{path}: longer than maxLength {schema['maxLength']}")
        if "pattern" in schema:
            import re

            if not re.search(schema["pattern"], value):
                errors.append(f"{path}: does not match pattern {schema['pattern']!r}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: above maximum {schema['maximum']}")

    if isinstance(value, dict):
        for key in schema.get("required", []) or []:
            if key not in value:
                errors.append(f"{path}: missing required property '{key}'")
        properties = schema.get("properties") or {}
        for key, subschema in properties.items():
            if key in value:
                errors.extend(validate_schema(value[key], subschema, f"{path}.{key}"))
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(properties))
            if extra:
                errors.append(f"{path}: unexpected properties {extra}")

    if isinstance(value, list):
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            errors.append(f"{path}: fewer than minItems {schema['minItems']}")
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            errors.append(f"{path}: more than maxItems {schema['maxItems']}")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                errors.extend(validate_schema(item, item_schema, f"{path}[{index}]"))

    return errors


class JsonSchemaEvaluator(Evaluator):
    """Validate a mapped value against a JSON schema.

    Config params::

        {"target": "summary", "schema": {...}, "parse_json_strings": true}
    """

    name = "json_schema"
    version = "1.0.0"
    check_type = CheckType.DETERMINISTIC

    def evaluate(
        self,
        evaluator_input: dict[str, Any],
        evaluator_config: dict[str, Any],
        execution_context: ExecutionContext,
    ) -> EvaluatorOutput:
        params = evaluator_config.get("params") or {}
        schema = params.get("schema")
        if not isinstance(schema, dict):
            raise InvalidEvaluatorConfigError("json_schema check requires params.schema object")

        target = params.get("target")
        if target is None:
            if len(evaluator_input) != 1:
                raise InvalidEvaluatorConfigError(
                    "json_schema check requires params.target when the mapping has "
                    "more than one key"
                )
            target = next(iter(evaluator_input))
        value = self.require(evaluator_input, str(target))

        parsed_from_string = False
        if isinstance(value, str) and params.get("parse_json_strings", True):
            try:
                value = json.loads(value)
                parsed_from_string = True
            except json.JSONDecodeError as exc:
                return self.outcome(
                    passed=False,
                    score=0.0,
                    explanation=f"target '{target}' is a string but not valid JSON: {exc}",
                    evidence={"target": target, "violations": [str(exc)]},
                )

        violations = validate_schema(value, schema)
        return self.outcome(
            passed=not violations,
            score=1.0 if not violations else 0.0,
            explanation=(
                f"'{target}' matches the schema"
                if not violations
                else f"'{target}' has {len(violations)} schema violation(s)"
            ),
            evidence={
                "target": target,
                "violations": violations,
                "parsed_from_json_string": parsed_from_string,
                "schema_keys": sorted(schema.keys()),
            },
        )
