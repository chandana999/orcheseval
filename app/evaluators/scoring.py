"""Text comparison primitives reused from eval-orchestrator.

`normalize`, `score_exact`, `score_fuzzy`, `score_semantic`, and the hash/OpenAI
embedding backend are ported from the source project so comparison behaviour
stays identical; only the imports and the return type changed.
"""

from __future__ import annotations

import hashlib
import math
import struct
from dataclasses import dataclass, field
from typing import Any

import httpx
from rapidfuzz import fuzz

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class ScoreResult:
    passed: bool
    score: float
    method: str
    details: dict[str, Any] = field(default_factory=dict)


def normalize(text: str) -> str:
    return " ".join(str(text).lower().strip().split())


def score_exact(expected: str, actual: str) -> ScoreResult:
    expected_norm = normalize(expected)
    actual_norm = normalize(actual)
    passed = expected_norm == actual_norm
    return ScoreResult(
        passed=passed,
        score=1.0 if passed else 0.0,
        method="exact",
        details={"expected_normalized": expected_norm, "actual_normalized": actual_norm},
    )


def score_fuzzy(expected: str, actual: str, threshold: float = 85.0) -> ScoreResult:
    expected_norm = normalize(expected)
    actual_norm = normalize(actual)
    ratio = fuzz.token_sort_ratio(expected_norm, actual_norm)
    partial = fuzz.partial_ratio(expected_norm, actual_norm)
    best = max(ratio, partial)
    return ScoreResult(
        passed=best >= threshold,
        score=round(best / 100, 4),
        method="fuzzy",
        details={"token_sort_ratio": ratio, "partial_ratio": partial, "threshold": threshold},
    )


def score_contains(expected: str, actual: str) -> ScoreResult:
    expected_norm = normalize(expected)
    actual_norm = normalize(actual)
    passed = expected_norm in actual_norm
    return ScoreResult(
        passed=passed,
        score=1.0 if passed else 0.0,
        method="contains",
        details={"needle": expected_norm},
    )


# ----------------------------------------------------------------- embeddings
def _hash_embedding(text: str, dims: int = 256) -> list[float]:
    values = [0.0] * dims
    tokens = text.lower().split() or [""]
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        for i in range(0, len(digest), 4):
            (n,) = struct.unpack("!I", digest[i : i + 4])
            index = n % dims
            sign = 1.0 if digest[i] % 2 == 0 else -1.0
            values[index] += sign
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return [v / norm for v in values]


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def _openai_embed(texts: list[str]) -> list[list[float]]:
    url = f"{settings.openai_base_url.rstrip('/')}/embeddings"
    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=settings.llm_timeout_seconds) as client:
        response = client.post(
            url, headers=headers, json={"model": settings.embedding_model, "input": texts}
        )
        response.raise_for_status()
        data = response.json()
    items = sorted(data["data"], key=lambda item: item["index"])
    return [item["embedding"] for item in items]


def embed_texts(texts: list[str]) -> tuple[list[list[float]], str]:
    if settings.embedding_provider == "openai" and settings.openai_api_key:
        try:
            return _openai_embed(texts), "openai"
        except Exception:
            logger.warning("embedding_openai_failed_fallback_hash")
    return [_hash_embedding(t) for t in texts], "hash"


def semantic_similarity(expected: str, actual: str) -> tuple[float, str]:
    vectors, backend = embed_texts([expected, actual])
    score = max(0.0, min(1.0, _cosine(vectors[0], vectors[1])))
    return round(score, 4), backend


def score_semantic(expected: str, actual: str, threshold: float | None = None) -> ScoreResult:
    threshold = settings.semantic_threshold if threshold is None else threshold
    similarity, backend = semantic_similarity(expected, actual)
    return ScoreResult(
        passed=similarity >= threshold,
        score=similarity,
        method="semantic",
        details={
            "similarity": similarity,
            "threshold": threshold,
            "embedding_backend": backend,
        },
    )


COMPARISON_METHODS = {
    "exact": score_exact,
    "fuzzy": score_fuzzy,
    "semantic": score_semantic,
    "contains": score_contains,
}


def compare(
    method: str,
    expected: Any,
    actual: Any,
    *,
    threshold: float | None = None,
) -> ScoreResult:
    from app.services.errors import InvalidEvaluatorConfigError

    fn = COMPARISON_METHODS.get(method)
    if fn is None:
        raise InvalidEvaluatorConfigError(
            f"unknown comparison method '{method}'; expected one of {sorted(COMPARISON_METHODS)}"
        )
    expected_text = expected if isinstance(expected, str) else _stringify(expected)
    actual_text = actual if isinstance(actual, str) else _stringify(actual)
    if method == "fuzzy" and threshold is not None:
        return score_fuzzy(expected_text, actual_text, threshold=threshold)
    if method == "semantic" and threshold is not None:
        return score_semantic(expected_text, actual_text, threshold=threshold)
    return fn(expected_text, actual_text)


def _stringify(value: Any) -> str:
    import json

    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    return str(value)
