"""Payload endpoints: insert complete agent execution payloads and inspect them."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query

from app.core.metrics import PAYLOADS_INGESTED
from app.dependencies import ApiKey, DbConnection
from app.repositories.dataset_repository import DatasetRepository
from app.repositories.payload_repository import PayloadRepository
from app.schemas import (
    IngestionResponse,
    PayloadBulkCreate,
    PayloadCreate,
    PayloadDetailResponse,
    PayloadListResponse,
    PayloadResponse,
)
from app.services.dataset_ingestion import build_payload_row, ingest_payloads
from app.services.payload_validation import PayloadValidationError

router = APIRouter(prefix="/api/v1/payloads", tags=["payloads"])


@router.post("", response_model=PayloadDetailResponse, status_code=201)
def create_payload(
    body: PayloadCreate,
    conn: DbConnection,
    _api_key: ApiKey,
) -> PayloadDetailResponse:
    """Store one complete payload. PostgreSQL keeps the document verbatim."""
    if body.dataset_id is not None and DatasetRepository(conn).get(body.dataset_id) is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    try:
        payload, _report = build_payload_row(
            body.payload,
            dataset_id=body.dataset_id,
            source_type="api",
            external_payload_id=body.external_payload_id,
        )
        with conn.transaction():
            stored = PayloadRepository(conn).insert(payload)
            if body.dataset_id is not None:
                DatasetRepository(conn).refresh_record_count(body.dataset_id)
    except PayloadValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    PAYLOADS_INGESTED.labels(source_type="api").inc()
    return PayloadDetailResponse.model_validate(stored)


@router.post("/bulk", response_model=IngestionResponse, status_code=201)
def create_payloads_bulk(
    body: PayloadBulkCreate,
    conn: DbConnection,
    _api_key: ApiKey,
) -> IngestionResponse:
    if body.dataset_id is not None and DatasetRepository(conn).get(body.dataset_id) is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    try:
        with conn.transaction():
            payload_ids, warnings = ingest_payloads(
                conn, body.payloads, dataset_id=body.dataset_id, source_type="api"
            )
            if body.dataset_id is not None:
                DatasetRepository(conn).refresh_record_count(body.dataset_id)
    except PayloadValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return IngestionResponse(
        dataset_id=body.dataset_id,
        payload_ids=payload_ids,
        payload_count=len(payload_ids),
        warnings=warnings,
    )


@router.get("", response_model=PayloadListResponse)
def list_payloads(
    conn: DbConnection,
    _api_key: ApiKey,
    dataset_id: uuid.UUID | None = None,
    trace_id: str | None = None,
    session_id: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> PayloadListResponse:
    total, items = PayloadRepository(conn).list(
        dataset_id=dataset_id,
        trace_id=trace_id,
        session_id=session_id,
        limit=limit,
        offset=offset,
    )
    return PayloadListResponse(
        total=total,
        limit=limit,
        offset=offset,
        items=[PayloadResponse.model_validate(p) for p in items],
    )


@router.get("/{payload_id}", response_model=PayloadDetailResponse)
def get_payload(
    payload_id: uuid.UUID, conn: DbConnection, _api_key: ApiKey
) -> PayloadDetailResponse:
    payload = PayloadRepository(conn).get(payload_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Payload not found")
    return PayloadDetailResponse.model_validate(payload)
