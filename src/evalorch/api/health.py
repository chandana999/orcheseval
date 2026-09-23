from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response

from evalorch import __version__
from evalorch.core.database import healthcheck
from evalorch.core.metrics import metrics_response
from evalorch.evaluators.registry import describe_evaluators
from evalorch.schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health_check() -> HealthResponse:
    postgres_ok, _postgres = healthcheck()
    services = {"api": "ok", "postgres": "ok" if postgres_ok else "unavailable"}
    return HealthResponse(
        status="healthy" if postgres_ok else "degraded",
        version=__version__,
        services=services,
    )


@router.get("/health/live")
def liveness() -> dict:
    return {"status": "alive"}


@router.get("/health/ready")
def readiness() -> JSONResponse:
    """Ready only when PostgreSQL answers. The body never includes the driver error."""
    postgres_ok, _postgres = healthcheck()
    if postgres_ok:
        return JSONResponse({"status": "ready", "postgres": "ok"})
    return JSONResponse({"status": "not_ready", "postgres": "unavailable"}, status_code=503)


@router.get("/metrics")
def prometheus_metrics() -> Response:
    return Response(content=metrics_response(), media_type="text/plain; version=0.0.4")


@router.get("/v1/evaluators")
def list_evaluators() -> dict:
    """Evaluators available to metric definitions."""
    return {"evaluators": describe_evaluators()}
