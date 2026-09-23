"""One public error envelope for every API failure.

Callers see a code, a safe message, and the request id. Stack traces, SQL,
filesystem paths, and exception text stay in the logs.
"""

from __future__ import annotations

import re
import uuid

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from evalorch.core.logging import get_logger
from evalorch.core.redaction import redact_secrets
from evalorch.services.dataset_folder import DatasetFolderError
from evalorch.services.job_service import JobCreationError
from evalorch.services.metric_validation import MetricDefinitionError
from evalorch.services.payload_validation import PayloadValidationError
from evalorch.services.profile_resolution import ProfileResolutionError

__all__ = ["redact_secrets"]

logger = get_logger(__name__)

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

_STATUS_CODES = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    404: "NOT_FOUND",
    409: "CONFLICT",
    422: "VALIDATION_ERROR",
    429: "RATE_LIMITED",
    500: "INTERNAL_SERVER_ERROR",
    503: "SERVICE_UNAVAILABLE",
}

_DOMAIN_ERRORS = (
    JobCreationError,
    DatasetFolderError,
    PayloadValidationError,
    ProfileResolutionError,
    MetricDefinitionError,
)


def resolve_request_id(header: str | None) -> str:
    """Use a safe caller-supplied id, or a new UUID."""
    if header:
        candidate = header.strip()
        if _REQUEST_ID_RE.match(candidate):
            return candidate
    return str(uuid.uuid4())


def request_id_from(request: Request) -> str:
    existing = getattr(request.state, "request_id", None)
    if isinstance(existing, str) and existing:
        return existing
    generated = str(uuid.uuid4())
    request.state.request_id = generated
    return generated


def error_body(request: Request, code: str, message: str) -> dict[str, dict[str, str]]:
    return {
        "error": {
            "code": code,
            "message": message,
            "request_id": request_id_from(request),
        }
    }


def error_response(request: Request, status_code: int, code: str, message: str) -> JSONResponse:
    response = JSONResponse(status_code=status_code, content=error_body(request, code, message))
    response.headers["X-Request-ID"] = request_id_from(request)
    return response


def _code_for(status_code: int) -> str:
    return _STATUS_CODES.get(status_code, "HTTP_ERROR")


def _safe_http_message(status_code: int, detail: object) -> str:
    if status_code >= 500 or not isinstance(detail, str) or not detail.strip():
        if status_code == 503:
            return "The service is temporarily unavailable."
        if status_code >= 500:
            return "An unexpected internal error occurred."
        return "The request could not be processed."
    return detail


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    status_code = exc.status_code
    return error_response(
        request,
        status_code,
        _code_for(status_code),
        _safe_http_message(status_code, exc.detail),
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    fields = [".".join(str(part) for part in error.get("loc", ())) for error in exc.errors()]
    logger.warning("request_validation_failed", fields=fields, error_code="VALIDATION_ERROR")
    return error_response(
        request, 422, "VALIDATION_ERROR", "Request validation failed."
    )


async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    status_code = int(getattr(exc, "status_code", 400) or 400)
    if isinstance(exc, _DOMAIN_ERRORS):
        message = str(exc)
        logger.warning(
            "domain_error",
            error_code=_code_for(status_code),
            status_code=status_code,
            error_type=type(exc).__name__,
        )
    else:
        message = "The request could not be processed."
        logger.warning(
            "value_error",
            error_code=_code_for(status_code),
            error_type=type(exc).__name__,
        )
    return error_response(request, status_code, _code_for(status_code), message)


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception(
        "unhandled_exception",
        path=request.url.path,
        error_type=type(exc).__name__,
        error_code="INTERNAL_SERVER_ERROR",
    )
    return error_response(
        request,
        500,
        "INTERNAL_SERVER_ERROR",
        "An unexpected internal error occurred.",
    )
