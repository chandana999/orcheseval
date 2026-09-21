"""Pydantic request/response models for the HTTP API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ------------------------------------------------------------------- health
class HealthResponse(BaseModel):
    status: str
    version: str
    services: dict[str, str]


# ------------------------------------------------------------------ jobs
class EvaluationJobCreate(BaseModel):
    dataset_id: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Folder name under EVALUATION_TEMP_ROOT. Not a database id.",
    )
    name: str | None = Field(default=None, max_length=255)
    priority: int | None = None
    max_attempts: int | None = Field(default=None, ge=1)


class EvaluationJobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str | None
    dataset_id: str | None
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
    dataset_id: str
    metric_ids: list[str] = Field(default_factory=list)
    profiles: list[str] = Field(default_factory=list)
    tickets_summary: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


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
    source_dataset_id: str
    source_payload_ref: str
    source_payload_id: str | None = None
    metric_record_id: uuid.UUID
    metric_id: str
    metric_version_number: int
    evaluation_profile_id: str
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
    metric_snapshot_json: dict[str, Any] | None = None
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
    source_payload_ref: str | None = None
    metric_record_id: uuid.UUID | None = None
    metric_id: str | None = None
    metric_version_number: int | None = None
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
