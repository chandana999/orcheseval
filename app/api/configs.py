"""Evaluation configuration endpoints.

Configurations are validated and normalized on write, versioned on (name, version),
and never mutated in place: a new version is a new row, so running jobs keep the
snapshot they started with.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query
from psycopg.errors import UniqueViolation

from app.dependencies import ApiKey, DbConnection
from app.models.entities import EvaluationConfig
from app.models.enums import ConfigStatus
from app.repositories.config_repository import ConfigRepository
from app.schemas import (
    EvaluationConfigCreate,
    EvaluationConfigListResponse,
    EvaluationConfigResponse,
    EvaluationConfigStatusUpdate,
)
from app.services.config_validation import (
    ConfigValidationError,
    validate_and_normalize_config,
)

router = APIRouter(prefix="/api/v1/evaluation-configs", tags=["evaluation-configs"])


@router.post("", response_model=EvaluationConfigResponse, status_code=201)
def create_config(
    body: EvaluationConfigCreate,
    conn: DbConnection,
    _api_key: ApiKey,
) -> EvaluationConfigResponse:
    try:
        normalized = validate_and_normalize_config(body.config)
    except ConfigValidationError as exc:
        raise HTTPException(status_code=422, detail={"errors": exc.problems}) from exc
    try:
        status = ConfigStatus(body.status.upper())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid status") from exc

    configs = ConfigRepository(conn)
    version = body.version or configs.next_version(body.name)
    entity = EvaluationConfig(
        id=uuid.uuid4(),
        name=body.name,
        version=version,
        description=body.description or normalized.get("description"),
        config_json=normalized,
        status=status,
        checks_count=len(normalized.get("checks", [])),
    )
    try:
        with conn.transaction():
            stored = configs.insert(entity)
    except UniqueViolation as exc:
        raise HTTPException(
            status_code=409,
            detail=f"configuration '{body.name}' version {version} already exists",
        ) from exc
    return EvaluationConfigResponse.model_validate(stored)


@router.post("/validate")
def validate_config(body: EvaluationConfigCreate, _api_key: ApiKey) -> dict:
    """Dry-run validation: normalize a configuration without storing it."""
    try:
        normalized = validate_and_normalize_config(body.config)
    except ConfigValidationError as exc:
        raise HTTPException(status_code=422, detail={"errors": exc.problems}) from exc
    return {
        "valid": True,
        "checks_count": len(normalized["checks"]),
        "check_ids": [c["check_id"] for c in normalized["checks"]],
        "normalized_config": normalized,
    }


@router.get("", response_model=EvaluationConfigListResponse)
def list_configs(
    conn: DbConnection,
    _api_key: ApiKey,
    name: str | None = None,
    status: ConfigStatus | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> EvaluationConfigListResponse:
    total, items = ConfigRepository(conn).list(
        name=name, status=status, limit=limit, offset=offset
    )
    return EvaluationConfigListResponse(
        total=total, items=[EvaluationConfigResponse.model_validate(c) for c in items]
    )


@router.get("/{config_id}", response_model=EvaluationConfigResponse)
def get_config(
    config_id: uuid.UUID, conn: DbConnection, _api_key: ApiKey
) -> EvaluationConfigResponse:
    config = ConfigRepository(conn).get(config_id)
    if config is None:
        raise HTTPException(status_code=404, detail="Evaluation config not found")
    return EvaluationConfigResponse.model_validate(config)


@router.patch("/{config_id}/status", response_model=EvaluationConfigResponse)
def update_config_status(
    config_id: uuid.UUID,
    body: EvaluationConfigStatusUpdate,
    conn: DbConnection,
    _api_key: ApiKey,
) -> EvaluationConfigResponse:
    """Change lifecycle status only. Check definitions are immutable per version."""
    try:
        status = ConfigStatus(body.status.upper())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid status") from exc
    with conn.transaction():
        updated = ConfigRepository(conn).set_status(config_id, status)
    if updated is None:
        raise HTTPException(status_code=404, detail="Evaluation config not found")
    return EvaluationConfigResponse.model_validate(updated)
