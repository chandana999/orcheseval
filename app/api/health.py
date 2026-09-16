from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response

from app import __version__
from app.core.database import healthcheck
from app.core.metrics import metrics_response
from app.evaluators.registry import describe_evaluators
from app.schemas import HealthResponse
from app.services.storage import storage_service

router = APIRouter(tags=["health"])


def _probe_storage() -> str:
    try:
        return "ok" if storage_service.writable() else "error: storage not writable"
    except Exception as exc:  # pragma: no cover - filesystem dependent
        return f"error: {exc}"


@router.get("/health", response_model=HealthResponse)
def health_check() -> HealthResponse:
    postgres_ok, postgres = healthcheck()
    services = {"api": "ok", "postgres": postgres, "storage": _probe_storage()}
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
    postgres_ok, postgres = healthcheck()
    storage = _probe_storage()
    if postgres_ok and storage == "ok":
        return JSONResponse({"status": "ready", "postgres": postgres, "storage": storage})
    return JSONResponse(
        {"status": "not_ready", "postgres": postgres, "storage": storage},
        status_code=503,
    )


@router.get("/metrics")
def prometheus_metrics() -> Response:
    return Response(content=metrics_response(), media_type="text/plain; version=0.0.4")


@router.get("/api/v1/evaluators")
def list_evaluators() -> dict:
    """Evaluators available to configurations."""
    return {"evaluators": describe_evaluators()}
