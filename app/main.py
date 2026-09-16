"""FastAPI application.

The API only creates and reads durable state in PostgreSQL. Evaluation happens in
the separate runner process (`python -m runner.main`).
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app import __version__
from app.api import configs, datasets, health, jobs, payloads
from app.core.config import settings
from app.core.database import close_pool, get_pool
from app.core.logging import get_logger, setup_logging

setup_logging()
logger = get_logger(__name__)

limiter = Limiter(key_func=get_remote_address, default_limits=[settings.rate_limit])


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_pool()
    logger.info(
        "api_started",
        env=settings.app_env,
        database=settings.pgdatabase,
        schema=settings.pgschema,
    )
    yield
    close_pool()
    logger.info("api_stopped")


app = FastAPI(
    title="eval-platform",
    version=__version__,
    description=(
        "Database-first agent evaluation platform. PostgreSQL is the durable "
        "source of truth for payloads, configurations, jobs, tickets, and results."
    ),
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(datasets.router)
app.include_router(payloads.router)
app.include_router(configs.router)
app.include_router(jobs.router)
app.include_router(jobs.tickets_router)
app.include_router(jobs.results_router)
app.include_router(jobs.ops_router)


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    logger.warning("value_error", path=str(request.url.path), error=str(exc))
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.get("/")
def root() -> dict:
    return {
        "name": "eval-platform",
        "version": __version__,
        "docs": "/docs",
        "health": "/health",
        "durable_store": "postgresql",
    }
