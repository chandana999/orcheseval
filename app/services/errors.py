"""Failure taxonomy shared by evaluators, the runner, and retry policy."""

from __future__ import annotations

import httpx

from app.models.enums import ErrorClass


class EvaluationError(RuntimeError):
    """Base class carrying a structured error code."""

    error_class: ErrorClass = ErrorClass.PERMANENT
    error_code = "EVALUATION_ERROR"

    def __init__(self, message: str, *, error_code: str | None = None) -> None:
        super().__init__(message)
        if error_code:
            self.error_code = error_code


class TransientEvaluationError(EvaluationError):
    """Retryable: provider timeout, rate limit, temporary connection problem."""

    error_class = ErrorClass.TRANSIENT
    error_code = "TRANSIENT_ERROR"


class PermanentEvaluationError(EvaluationError):
    """Not retryable: invalid configuration, unknown evaluator, invalid input."""

    error_class = ErrorClass.PERMANENT
    error_code = "PERMANENT_ERROR"


class MissingContextError(EvaluationError):
    """Required evaluation context could not be resolved from the payload."""

    error_class = ErrorClass.MISSING_CONTEXT
    error_code = "MISSING_CONTEXT"


class UnknownEvaluatorError(PermanentEvaluationError):
    error_code = "UNKNOWN_EVALUATOR"


class InvalidEvaluatorConfigError(PermanentEvaluationError):
    error_code = "INVALID_EVALUATOR_CONFIG"


TRANSIENT_HTTP_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
PERMANENT_HTTP_STATUS = {400, 401, 403, 404, 405, 422}


def classify_error(exc: BaseException) -> ErrorClass:
    """Map an exception onto the retry policy.

    Unknown exceptions are treated as transient so a one-off glitch does not
    permanently burn a ticket; the max-attempt ceiling still bounds retries.
    """
    if isinstance(exc, EvaluationError):
        return exc.error_class

    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in PERMANENT_HTTP_STATUS:
            return ErrorClass.PERMANENT
        if code in TRANSIENT_HTTP_STATUS:
            return ErrorClass.TRANSIENT
        return ErrorClass.TRANSIENT
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError)):
        return ErrorClass.TRANSIENT
    if isinstance(exc, TimeoutError):
        return ErrorClass.TRANSIENT
    if isinstance(exc, (ValueError, TypeError, KeyError, LookupError)):
        return ErrorClass.PERMANENT

    message = str(exc).lower()
    if "deadlock" in message or "could not serialize" in message:
        return ErrorClass.TRANSIENT
    if "timeout" in message or "temporarily" in message or "rate limit" in message:
        return ErrorClass.TRANSIENT
    return ErrorClass.TRANSIENT


def error_code_for(exc: BaseException) -> str:
    if isinstance(exc, EvaluationError):
        return exc.error_code
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP_{exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "LLM_TIMEOUT"
    if isinstance(exc, (httpx.ConnectError, httpx.NetworkError)):
        return "CONNECTION_ERROR"
    return type(exc).__name__.upper()
