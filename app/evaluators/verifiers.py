"""Custom verifiers ported from evalforge-local (regex, contains_all, json_keys,
length_bounds). Reused by the field comparison evaluator."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


@dataclass
class VerifierResult:
    passed: bool
    score: float
    details: dict[str, Any]


def _regex_verifier(expected: str, actual: str, config: dict[str, Any]) -> VerifierResult:
    pattern = config.get("pattern") or expected
    flags = re.IGNORECASE if config.get("ignore_case", True) else 0
    match = re.search(pattern, actual or "", flags)
    ok = match is not None
    return VerifierResult(ok, 1.0 if ok else 0.0, {"pattern": pattern, "matched": ok})


def _contains_all_verifier(_expected: str, actual: str, config: dict[str, Any]) -> VerifierResult:
    needles = config.get("contains") or []
    if isinstance(needles, str):
        needles = [needles]
    missing = [n for n in needles if n.lower() not in (actual or "").lower()]
    ok = not missing
    score = 1.0 - (len(missing) / max(len(needles), 1))
    return VerifierResult(ok, round(score, 4), {"missing": missing, "required": needles})


def _json_keys_verifier(_expected: str, actual: str, config: dict[str, Any]) -> VerifierResult:
    required_keys = config.get("required_keys") or []
    try:
        parsed = json.loads(actual or "")
    except json.JSONDecodeError as exc:
        return VerifierResult(False, 0.0, {"error": str(exc)})
    if not isinstance(parsed, dict):
        return VerifierResult(False, 0.0, {"error": "value is not a JSON object"})
    missing = [k for k in required_keys if k not in parsed]
    ok = not missing
    score = 1.0 - (len(missing) / max(len(required_keys), 1))
    return VerifierResult(ok, round(score, 4), {"missing_keys": missing})


def _length_bounds_verifier(_expected: str, actual: str, config: dict[str, Any]) -> VerifierResult:
    text = actual or ""
    min_len = int(config.get("min_length", 0))
    max_len = int(config.get("max_length", 10_000))
    length = len(text)
    ok = min_len <= length <= max_len
    return VerifierResult(
        ok,
        1.0 if ok else 0.0,
        {"length": length, "min_length": min_len, "max_length": max_len},
    )


VERIFIERS: dict[str, Any] = {
    "regex": _regex_verifier,
    "contains_all": _contains_all_verifier,
    "json_keys": _json_keys_verifier,
    "length_bounds": _length_bounds_verifier,
}


def run_custom_verifier(
    *,
    expected: str,
    actual: str,
    verifier_config: dict[str, Any],
) -> VerifierResult:
    from app.services.errors import InvalidEvaluatorConfigError

    verifier_type = verifier_config.get("type", "regex")
    fn = VERIFIERS.get(verifier_type)
    if fn is None:
        raise InvalidEvaluatorConfigError(
            f"unknown verifier type '{verifier_type}'; expected one of {sorted(VERIFIERS)}"
        )
    return fn(expected, actual, verifier_config)
