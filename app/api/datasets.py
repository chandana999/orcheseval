"""Dataset endpoints.

Dataset creation and payload insertion work without any file (primary path).
Local JSON/JSONL upload is an optional convenience that ends in the same rows.
"""

from __future__ import annotations

import io
import json
import uuid

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse

from app.dependencies import ApiKey, DbConnection
from app.models.enums import DatasetStatus
from app.repositories.dataset_repository import DatasetRepository
from app.repositories.payload_repository import PayloadRepository
from app.schemas import (
    DatasetCreate,
    DatasetListResponse,
    DatasetResponse,
    IngestionResponse,
    PayloadBulkCreate,
)
from app.services.dataset_ingestion import (
    add_payloads_to_dataset,
    create_dataset,
    ingest_dataset_from_upload,
)
from app.services.payload_validation import PayloadValidationError

router = APIRouter(prefix="/api/v1/datasets", tags=["datasets"])


@router.post("", response_model=IngestionResponse, status_code=201)
def create_dataset_endpoint(
    body: DatasetCreate,
    conn: DbConnection,
    _api_key: ApiKey,
) -> IngestionResponse:
    """Create a dataset, optionally with payloads, entirely through the API."""
    try:
        with conn.transaction():
            dataset = create_dataset(
                conn,
                name=body.name,
                description=body.description,
                source_type="api",
                metadata=body.metadata,
                status=DatasetStatus.READY if not body.payloads else DatasetStatus.INGESTING,
            )
            payload_ids: list[uuid.UUID] = []
            warnings: list[str] = []
            if body.payloads:
                result = add_payloads_to_dataset(conn, dataset, body.payloads)
                dataset = result.dataset
                payload_ids = result.payload_ids
                warnings = result.warnings
    except PayloadValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return IngestionResponse(
        dataset_id=dataset.id,
        payload_ids=payload_ids,
        payload_count=len(payload_ids),
        warnings=warnings,
        dataset=DatasetResponse.model_validate(dataset),
    )


@router.post("/upload", response_model=IngestionResponse, status_code=201)
def upload_dataset(
    conn: DbConnection,
    _api_key: ApiKey,
    name: str = Form(...),
    description: str | None = Form(None),
    file: UploadFile = File(...),
) -> IngestionResponse:
    """Optional local ingestion of a .json / .jsonl file.

    The file is validated, parsed, and stored in PostgreSQL. Afterwards it may be
    deleted or moved: evaluation reads payloads from the database only.
    """
    content = file.file.read()
    try:
        with conn.transaction():
            result = ingest_dataset_from_upload(
                conn,
                name=name,
                description=description,
                filename=file.filename,
                content=content,
            )
    except PayloadValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    assert result.dataset is not None
    return IngestionResponse(
        dataset_id=result.dataset.id,
        payload_ids=result.payload_ids,
        payload_count=len(result.payload_ids),
        warnings=result.warnings,
        dataset=DatasetResponse.model_validate(result.dataset),
    )


@router.get("", response_model=DatasetListResponse)
def list_datasets(
    conn: DbConnection,
    _api_key: ApiKey,
    status: DatasetStatus | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> DatasetListResponse:
    total, items = DatasetRepository(conn).list(status=status, limit=limit, offset=offset)
    return DatasetListResponse(
        total=total, items=[DatasetResponse.model_validate(d) for d in items]
    )


@router.get("/{dataset_id}", response_model=DatasetResponse)
def get_dataset(dataset_id: uuid.UUID, conn: DbConnection, _api_key: ApiKey) -> DatasetResponse:
    dataset = DatasetRepository(conn).get(dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return DatasetResponse.model_validate(dataset)


@router.post("/{dataset_id}/payloads", response_model=IngestionResponse, status_code=201)
def add_payloads(
    dataset_id: uuid.UUID,
    body: PayloadBulkCreate,
    conn: DbConnection,
    _api_key: ApiKey,
) -> IngestionResponse:
    """Insert payloads into an existing dataset (no file involved)."""
    dataset = DatasetRepository(conn).get(dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    try:
        with conn.transaction():
            result = add_payloads_to_dataset(conn, dataset, body.payloads)
    except PayloadValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    assert result.dataset is not None
    return IngestionResponse(
        dataset_id=dataset_id,
        payload_ids=result.payload_ids,
        payload_count=len(result.payload_ids),
        warnings=result.warnings,
        dataset=DatasetResponse.model_validate(result.dataset),
    )


@router.get("/{dataset_id}/export")
def export_dataset(
    dataset_id: uuid.UUID,
    conn: DbConnection,
    _api_key: ApiKey,
):
    """Export the dataset's payloads as JSONL, streamed from PostgreSQL."""
    dataset = DatasetRepository(conn).get(dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")

    payloads = PayloadRepository(conn)
    lines = [
        json.dumps(payload, ensure_ascii=False)
        for payload in payloads.iter_payload_json_by_dataset(dataset_id)
    ]
    body = "\n".join(lines) + ("\n" if lines else "")
    return StreamingResponse(
        io.StringIO(body),
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": f"attachment; filename=dataset_{dataset_id}.jsonl"
        },
    )
