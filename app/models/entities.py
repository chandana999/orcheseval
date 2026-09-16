"""Plain dataclasses mapped by hand from SQL rows. No ORM, no metaclasses."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.models.enums import (
    CheckType,
    ConfigStatus,
    DatasetStatus,
    JobStatus,
    ResultStatus,
    TicketStatus,
)


@dataclass
class Dataset:
    id: uuid.UUID
    name: str
    description: str | None
    source_type: str
    source_path: str | None
    record_count: int
    status: DatasetStatus
    checksum_sha256: str | None
    validation_report: dict[str, Any] | None
    metadata_json: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime | None = None


@dataclass
class EvaluationPayload:
    id: uuid.UUID
    dataset_id: uuid.UUID | None
    external_payload_id: str | None
    trace_id: str | None
    session_id: str | None
    workflow_id: str | None
    payload_json: dict[str, Any]
    payload_version: str | None
    source_type: str
    validation_report: dict[str, Any] | None
    created_at: datetime | None = None


@dataclass
class EvaluationConfig:
    id: uuid.UUID
    name: str
    version: int
    description: str | None
    config_json: dict[str, Any]
    status: ConfigStatus
    checks_count: int
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class EvaluationJob:
    id: uuid.UUID
    name: str | None
    dataset_id: uuid.UUID | None
    evaluation_config_id: uuid.UUID
    config_snapshot_json: dict[str, Any]
    status: JobStatus
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
    updated_at: datetime | None = None
    error_message: str | None = None


@dataclass
class EvaluationTicket:
    id: uuid.UUID
    job_id: uuid.UUID
    payload_id: uuid.UUID
    evaluation_config_id: uuid.UUID
    check_id: str
    check_type: CheckType
    evaluator: str
    status: TicketStatus
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
    input_snapshot_json: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class EvaluationResult:
    id: uuid.UUID
    job_id: uuid.UUID
    ticket_id: uuid.UUID
    payload_id: uuid.UUID
    check_id: str
    check_type: CheckType
    evaluator_type: str
    evaluator_version: str | None
    status: ResultStatus
    passed: bool | None
    score: float | None
    explanation: str | None
    evidence_json: dict[str, Any] | None
    input_snapshot_json: dict[str, Any] | None
    output_json: dict[str, Any] | None
    error_code: str | None
    error_message: str | None
    execution_time_ms: float | None
    attempt_count: int | None
    created_at: datetime | None = None
    completed_at: datetime | None = None


@dataclass
class JobProgress:
    """Job counters derived from durable ticket rows."""

    job_id: uuid.UUID
    status: JobStatus
    total_tickets: int
    completed_tickets: int
    failed_tickets: int
    cancelled_tickets: int
    not_applicable_tickets: int
    running_tickets: int
    pending_tickets: int
    counts_by_status: dict[str, int] = field(default_factory=dict)

    @property
    def terminal_tickets(self) -> int:
        return (
            self.completed_tickets
            + self.failed_tickets
            + self.cancelled_tickets
            + self.not_applicable_tickets
        )

    @property
    def is_complete(self) -> bool:
        return self.total_tickets > 0 and self.terminal_tickets >= self.total_tickets
