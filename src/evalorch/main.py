"""FastAPI application.

The API only creates and reads durable state in PostgreSQL. Evaluation happens in
the separate runner process (`python -m runner.main`).
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.exceptions import HTTPException as StarletteHTTPException

from evalorch import __version__
from evalorch.api import health, jobs
from evalorch.api.errors import (
    error_response,
    http_exception_handler,
    request_id_from,
    resolve_request_id,
    unhandled_exception_handler,
    validation_exception_handler,
    value_error_handler,
)
from evalorch.core.config import settings
from evalorch.core.database import close_pool, get_engine
from evalorch.core.logging import get_logger, setup_logging

setup_logging()
logger = get_logger(__name__)

limiter = Limiter(key_func=get_remote_address, default_limits=[settings.rate_limit])


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_engine()
    if (
        not settings.api_key
        and settings.is_development
        and settings.allow_unauthenticated_dev
    ):
        logger.warning(
            "api_auth_bypass_active",
            app_env=settings.app_env,
        )
    logger.info(
        "api_started",
        env=settings.app_env,
        database=settings.pgdatabase,
        schema=settings.pgschema,
    )
    yield
    close_pool()
    logger.info("api_stopped")


class RequestIdMiddleware:
    """Attach X-Request-ID without using BaseHTTPMiddleware.

    BaseHTTPMiddleware re-raises endpoint exceptions after the error handler
    has already built the response.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])}
        request_id = resolve_request_id(headers.get("x-request-id"))
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        structlog.contextvars.bind_contextvars(request_id=request_id)

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                raw_headers = list(message.get("headers", []))
                if not any(key.lower() == b"x-request-id" for key, _value in raw_headers):
                    raw_headers.append((b"x-request-id", request_id.encode("latin-1")))
                message["headers"] = raw_headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            structlog.contextvars.clear_contextvars()


app = FastAPI(
    title="eval-platform",
    version=__version__,
    lifespan=lifespan,
)

app.state.limiter = limiter
_cors_origins = [origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()]
# A wildcard origin cannot be combined with credentialed responses.
_cors_credentials = bool(_cors_origins) and "*" not in _cors_origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins or [],
    allow_credentials=_cors_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)

app.include_router(health.router)
app.include_router(jobs.router, prefix="/v1")
app.include_router(jobs.tickets_router, prefix="/v1")
app.include_router(jobs.results_router, prefix="/v1")
app.include_router(jobs.ops_router, prefix="/v1")
app.add_middleware(RequestIdMiddleware)


async def _rate_limit_handler(request: Request, _exc: RateLimitExceeded):
    request_id_from(request)
    return error_response(request, 429, "RATE_LIMITED", "Rate limit exceeded.")


app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)
app.add_exception_handler(StarletteHTTPException, http_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(ValueError, value_error_handler)
app.add_exception_handler(Exception, unhandled_exception_handler)


@app.get("/")
def root() -> dict:
    return {
        "name": "eval-platform",
        "version": __version__,
        "docs": "/docs",
        "health": "/health",
        "durable_store": "postgresql",
        "evaluation_jobs": "/v1/evaluation-jobs",
    }
