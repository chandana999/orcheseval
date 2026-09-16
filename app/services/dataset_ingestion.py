"""Payload ingestion. Both input methods converge on the same PostgreSQL rows.

API insertion and optional file upload call `ingest_payloads`, so there is exactly
one persistence path and one downstream evaluation flow.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Any

from psycopg import Connection

from app.core.config import settings
from app.core.metrics import PAYLOADS_INGESTED
from app.models.entities import Dataset, EvaluationPayload
from app.models.enums import DatasetStatus
from app.repositories.dataset_repository import DatasetRepository
from app.repositories.payload_repository import PayloadRepository
from app.services.payload_validation import (
    PayloadValidationError,
    extract_identifiers,
    parse_upload,
    validate_payload,
)
from app.services.storage import storage_service


@dataclass
class IngestionResult:
    dataset: Dataset | None
    payload_ids: list[uuid.UUID]
    warnings: list[str]
    report: dict[str, Any]


def build_payload_row(
    payload_json: dict[str, Any],
    *,
    dataset_id: uuid.UUID | None,
    source_type: str,
    index: int | None = None,
    external_payload_id: str | None = None,
) -> tuple[EvaluationPayload, dict[str, Any]]:
    """Validate a payload and build the row that stores it complete."""
    report = validate_payload(payload_json, index=index)
    identifiers = extract_identifiers(payload_json)
    payload = EvaluationPayload(
        id=uuid.uuid4(),
        dataset_id=dataset_id,
        external_payload_id=external_payload_id or identifiers["external_payload_id"],
        trace_id=identifiers["trace_id"],
        session_id=identifiers["session_id"],
        workflow_id=identifiers["workflow_id"],
        payload_json=payload_json,
        payload_version=identifiers["payload_version"],
        source_type=source_type,
        validation_report=report,
    )
    return payload, report


def ingest_payloads(
    conn: Connection,
    payloads: list[dict[str, Any]],
    *,
    dataset_id: uuid.UUID | None,
    source_type: str,
) -> tuple[list[uuid.UUID], list[str]]:
    """Persist complete payloads. Caller owns the transaction."""
    if not payloads:
        raise PayloadValidationError("at least one payload is required")
    if len(payloads) > settings.max_payloads_per_dataset:
        raise PayloadValidationError(
            f"payload count {len(payloads)} exceeds MAX_PAYLOADS_PER_DATASET "
            f"({settings.max_payloads_per_dataset})"
        )

    rows: list[EvaluationPayload] = []
    warnings: list[str] = []
    seen_external: set[str] = set()
    for index, raw in enumerate(payloads):
        payload, report = build_payload_row(
            raw, dataset_id=dataset_id, source_type=source_type, index=index
        )
        external = payload.external_payload_id
        if external:
            if external in seen_external:
                # Duplicate ids inside one request would violate the unique index.
                warnings.append(
                    f"payload #{index}: duplicate external payload id '{external}' "
                    "stored without an external id"
                )
                payload.external_payload_id = None
            else:
                seen_external.add(external)
        for warning in report.get("warnings", []):
            warnings.append(f"payload #{index}: {warning}")
        rows.append(payload)

    PayloadRepository(conn).bulk_insert(rows)
    PAYLOADS_INGESTED.labels(source_type=source_type).inc(len(rows))
    return [row.id for row in rows], warnings


def create_dataset(
    conn: Connection,
    *,
    name: str,
    description: str | None = None,
    source_type: str = "api",
    metadata: dict[str, Any] | None = None,
    status: DatasetStatus = DatasetStatus.CREATED,
) -> Dataset:
    dataset = Dataset(
        id=uuid.uuid4(),
        name=name,
        description=description,
        source_type=source_type,
        source_path=None,
        record_count=0,
        status=status,
        checksum_sha256=None,
        validation_report=None,
        metadata_json=metadata,
        created_at=None,  # set by the database default
    )
    # created_at is NOT NULL with a default; the dataclass value is unused on insert.
    dataset.created_at = None
    return DatasetRepository(conn).insert(dataset)


def ingest_dataset_from_upload(
    conn: Connection,
    *,
    name: str,
    description: str | None,
    filename: str | None,
    content: bytes,
    archive_file: bool = True,
) -> IngestionResult:
    """Optional local ingestion: validate, parse, store in PostgreSQL, return ids.

    The uploaded bytes are optionally archived for provenance only. Evaluation
    never reads the file again, so deleting or moving it afterwards is safe.
    """
    if len(content) > settings.max_upload_bytes:
        raise PayloadValidationError(
            f"file exceeds MAX_UPLOAD_BYTES ({settings.max_upload_bytes} bytes)"
        )
    payloads, source_type = parse_upload(filename, content)

    dataset = create_dataset(
        conn,
        name=name,
        description=description,
        source_type=source_type,
        status=DatasetStatus.INGESTING,
    )
    payload_ids, warnings = ingest_payloads(
        conn, payloads, dataset_id=dataset.id, source_type=source_type
    )

    source_path: str | None = None
    if archive_file:
        try:
            extension = "jsonl" if source_type == "jsonl" else "json"
            source_path = storage_service.upload_bytes(
                f"uploads/{dataset.id}/original.{extension}", content
            )
        except (OSError, ValueError) as exc:
            # Archiving is best effort: PostgreSQL already holds the payloads.
            warnings.append(f"file archive skipped: {exc}")

    checksum = hashlib.sha256(content).hexdigest()
    report = {
        "payload_count": len(payload_ids),
        "source_type": source_type,
        "filename": filename,
        "file_size_bytes": len(content),
        "warnings": warnings,
        "authoritative_store": "postgresql",
    }
    dataset = DatasetRepository(conn).finalize_ingestion(
        dataset.id,
        status=DatasetStatus.READY,
        record_count=len(payload_ids),
        checksum_sha256=checksum,
        source_path=source_path,
        validation_report=report,
    )
    return IngestionResult(
        dataset=dataset, payload_ids=payload_ids, warnings=warnings, report=report
    )


def add_payloads_to_dataset(
    conn: Connection,
    dataset: Dataset,
    payloads: list[dict[str, Any]],
    *,
    source_type: str = "api",
) -> IngestionResult:
    """Database/API input path: insert payloads into an existing dataset."""
    payload_ids, warnings = ingest_payloads(
        conn, payloads, dataset_id=dataset.id, source_type=source_type
    )
    record_count = PayloadRepository(conn).count_by_dataset(dataset.id)
    updated = DatasetRepository(conn).finalize_ingestion(
        dataset.id,
        status=DatasetStatus.READY,
        record_count=record_count,
    )
    return IngestionResult(
        dataset=updated,
        payload_ids=payload_ids,
        warnings=warnings,
        report={"payload_count": len(payload_ids), "dataset_record_count": record_count},
    )
