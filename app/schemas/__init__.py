"""Pydantic request/response models for the HTTP API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ------------------------------------------------------------------- health
class HealthResponse(BaseModel):
    status: str
    version: str
    services: dict[str, str]


# ------------------------------------------------------------------ datasets
class DatasetCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    metadata: dict[str, Any] | None = None
    payloads: list[dict[str, Any]] | None = Field(
        default=None,
        description="Optional payloads to ingest with the dataset (database/API input).",
    )


class DatasetResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    source_type: str
    source_path: str | None
    record_count: int
    status: str
    checksum_sha256: str | None
    validation_report: dict[str, Any] | None
    metadata_json: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime | None = None


class DatasetListResponse(BaseModel):
    total: int
    items: list[DatasetResponse]


class IngestionResponse(BaseModel):
    dataset_id: uuid.UUID | None
    payload_ids: list[uuid.UUID]
    payload_count: int
    warnings: list[str] = Field(default_factory=list)
    storage: str = Field(
        default="postgresql",
        description="Authoritative store. Uploaded files are never read during evaluation.",
    )
    dataset: DatasetResponse | None = None


# ------------------------------------------------------------------ payloads
class PayloadCreate(BaseModel):
    payload: dict[str, Any] = Field(..., description="Complete agent execution payload")
    dataset_id: uuid.UUID | None = None
    external_payload_id: str | None = None


class PayloadBulkCreate(BaseModel):
    payloads: list[dict[str, Any]] = Field(..., min_length=1)
    dataset_id: uuid.UUID | None = None


class PayloadResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    dataset_id: uuid.UUID | None
    external_payload_id: str | None
    trace_id: str | None
    session_id: str | None
    workflow_id: str | None
    payload_version: str | None
    source_type: str
    validation_report: dict[str, Any] | None = None
    created_at: datetime | None = None


class PayloadDetailResponse(PayloadResponse):
    payload_json: dict[str, Any]


class PayloadListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[PayloadResponse]


# ------------------------------------------------------- evaluation configs
class EvaluationConfigCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    version: int | None = Field(default=None, ge=1)
    status: str = "ACTIVE"
    config: dict[str, Any] = Field(..., description="Evaluation definition with checks")


class EvaluationConfigResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    version: int
    description: str | None
    config_json: dict[str, Any]
    status: str
    checks_count: int
    created_at: datetime | None = None
    updated_at: datetime | None = None


class EvaluationConfigListResponse(BaseModel):
    total: int
    items: list[EvaluationConfigResponse]


class EvaluationConfigStatusUpdate(BaseModel):
    status: str


# ------------------------------------------------------------------ jobs
class EvaluationJobCreate(BaseModel):
    evaluation_config_id: uuid.UUID
    dataset_id: uuid.UUID | None = None
    payload_ids: list[uuid.UUID] | None = None
    name: str | None = Field(default=None, max_length=255)
    priority: int | None = None
    max_attempts: int | None = Field(default=None, ge=1)
    max_payloads: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _require_target(self) -> EvaluationJobCreate:
        if self.dataset_id is None and not self.payload_ids:
            raise ValueError("provide dataset_id or payload_ids")
        return self


class EvaluationJobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str | None
    dataset_id: uuid.UUID | None
    evaluation_config_id: uuid.UUID
    status: str
    payload_count: int
    total_tickets: int
    completed_tickets: int
    failed_tickets: int
    cancelled_tickets: int
    not_applicable_tickets: int
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    cancellation_requested_at: datetime | None = None
    error_message: str | None = None


class EvaluationJobDetailResponse(EvaluationJobResponse):
    config_snapshot_json: dict[str, Any]
    ticket_counts: dict[str, int] = Field(default_factory=dict)
    terminal_tickets: int = 0
    progress_ratio: float = 0.0


class EvaluationJobCreateResponse(BaseModel):
    job: EvaluationJobResponse
    ticket_count: int
    payload_count: int
    check_ids: list[str]


class EvaluationJobListResponse(BaseModel):
    total: int
    items: list[EvaluationJobResponse]


class JobCancellationResponse(BaseModel):
    job: EvaluationJobResponse
    cancelled_tickets: int
    running_tickets: int
    detail: str


class RetryFailedResponse(BaseModel):
    job: EvaluationJobResponse
    reset_count: int


class RecoveryResponse(BaseModel):
    recovered_to_retry: int
    recovered_to_failed: int
    cancelled_running: int
    jobs_updated: list[str]
    total: int


# ------------------------------------------------------------------ tickets
class EvaluationTicketResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    job_id: uuid.UUID
    payload_id: uuid.UUID
    evaluation_config_id: uuid.UUID
    check_id: str
    check_type: str
    evaluator: str
    status: str
    priority: int
    attempt_count: int
    max_attempts: int
    available_at: datetime | None = None
    worker_id: str | None = None
    claimed_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    lease_expires_at: datetime | None = None
    result_id: uuid.UUID | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class EvaluationTicketDetailResponse(EvaluationTicketResponse):
    input_snapshot_json: dict[str, Any] | None = None


class TicketListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[EvaluationTicketResponse]


class TicketSummaryResponse(BaseModel):
    job_id: uuid.UUID
    counts: dict[str, int]


# ------------------------------------------------------------------ results
class EvaluationResultResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    job_id: uuid.UUID
    ticket_id: uuid.UUID
    payload_id: uuid.UUID
    check_id: str
    check_type: str
    evaluator_type: str
    evaluator_version: str | None
    status: str
    passed: bool | None
    score: float | None
    explanation: str | None
    evidence_json: dict[str, Any] | None = None
    input_snapshot_json: dict[str, Any] | None = None
    output_json: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    execution_time_ms: float | None = None
    attempt_count: int | None = None
    created_at: datetime | None = None
    completed_at: datetime | None = None


class EvaluationResultDetailResponse(EvaluationResultResponse):
    pass


class ResultListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[EvaluationResultResponse]
