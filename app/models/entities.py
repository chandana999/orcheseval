"""Plain dataclasses mapped by hand from SQL rows. No ORM, no metaclasses."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.models.enums import CheckType, JobStatus, ResultStatus, TicketStatus


@dataclass
class MetricRecord:
    metric_record_id: uuid.UUID
    metric_id: str
    metric_code: str
    metric_name: str
    metric_desc: str | None
    metric_type: str
    metric_version_number: int
    definition_payload: dict[str, Any]
    default_threshold_operator: str | None
    llm_model_name: str | None
    llm_model_version: str | None
    llm_deployed_id: str | None
    is_active_indicator: bool
    previous_metric_record_id: uuid.UUID | None
    change_summary: str | None
    metric_create_timestamp: datetime | None = None


@dataclass
class EvaluationProfile:
    """A named, versioned collection of metric versions.

    `evaluation_profile_id` is the business key carried in
    payload.agent_registry; `id` is the surrogate key rows are mapped to.
    """

    evaluation_profile_id: str
    name: str
    version: int = 1
    description: str | None = None
    is_active: bool = True
    metadata_json: dict[str, Any] | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class EvaluationProfileMetric:
    """Maps a profile to one exact metric record version."""

    profile_id: uuid.UUID
    metric_record_id: uuid.UUID
    metric_id: str | None = None
    execution_order: int = 0
    enabled: bool = True
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime | None = None


@dataclass
class EvaluationJob:
    id: uuid.UUID
    name: str | None
    dataset_id: str | None
    config_snapshot_json: dict[str, Any]
    status: JobStatus
    payload_count: int
    total_tickets: int
    completed_tickets: int
    failed_tickets: int
    cancelled_tickets: int
    not_applicable_tickets: int
    created_at: datetime | None = None
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
    source_dataset_id: str
    source_payload_ref: str
    source_payload_id: str | None
    metric_record_id: uuid.UUID
    metric_id: str
    metric_version_number: int
    evaluation_profile_id: str
    metric_snapshot_json: dict[str, Any]
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
    source_payload_ref: str | None
    metric_record_id: uuid.UUID | None
    metric_id: str | None
    metric_version_number: int | None
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
