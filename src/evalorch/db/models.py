"""SQLAlchemy models for the evaluation schema.

Alembic builds the database from this metadata. Repositories use these tables
through a Session; locking queries still run as SQL so SKIP LOCKED stays exact.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

CHECK_TYPE = ENUM(
    "DETERMINISTIC",
    "LLM_JUDGE",
    name="evaluation_check_type",
    create_type=False,
)
JOB_STATUS = ENUM(
    "CREATED",
    "READY",
    "RUNNING",
    "COMPLETED",
    "PARTIAL_FAILED",
    "FAILED",
    "CANCELLED",
    name="evaluation_job_status",
    create_type=False,
)
TICKET_STATUS = ENUM(
    "READY",
    "RUNNING",
    "RETRY",
    "DONE",
    "FAILED",
    "CANCELLED",
    "NOT_APPLICABLE",
    name="evaluation_ticket_status",
    create_type=False,
)
RESULT_STATUS = ENUM(
    "PASSED",
    "FAILED",
    "NOT_APPLICABLE",
    "ERROR",
    name="evaluation_result_status",
    create_type=False,
)


class Base(DeclarativeBase):
    pass


class EvaluationJobRow(Base):
    __tablename__ = "evaluation_jobs"
    __table_args__ = (
        CheckConstraint(
            "jsonb_typeof(config_snapshot_json) = 'object'", name="ck_jobs_json_object"
        ),
        CheckConstraint(
            "payload_count >= 0 AND total_tickets >= 0 AND completed_tickets >= 0 "
            "AND failed_tickets >= 0 AND cancelled_tickets >= 0 "
            "AND not_applicable_tickets >= 0 AND "
            "completed_tickets + failed_tickets + cancelled_tickets + not_applicable_tickets "
            "<= total_tickets",
            name="ck_jobs_counters",
        ),
        Index("ix_evaluation_jobs_status", "status", "created_at"),
        Index("ix_evaluation_jobs_dataset_id", "dataset_id"),
        Index("uq_evaluation_jobs_idempotency_key", "idempotency_key", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    name: Mapped[str | None] = mapped_column(Text)
    dataset_id: Mapped[str | None] = mapped_column(Text)
    config_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(JOB_STATUS, server_default="CREATED")
    payload_count: Mapped[int] = mapped_column(Integer, server_default="0")
    total_tickets: Mapped[int] = mapped_column(Integer, server_default="0")
    completed_tickets: Mapped[int] = mapped_column(Integer, server_default="0")
    failed_tickets: Mapped[int] = mapped_column(Integer, server_default="0")
    cancelled_tickets: Mapped[int] = mapped_column(Integer, server_default="0")
    not_applicable_tickets: Mapped[int] = mapped_column(Integer, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancellation_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    error_message: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    idempotency_request_hash: Mapped[str | None] = mapped_column(Text)


class EvaluationResultRow(Base):
    __tablename__ = "evaluation_results"
    __table_args__ = (
        Index("uq_results_ticket_id", "ticket_id", unique=True),
        Index("ix_results_job_status", "job_id", "status"),
        Index("ix_results_payload", "payload_id"),
        Index("ix_results_ticket", "ticket_id"),
        Index("ix_results_check_id", "job_id", "check_id"),
        Index("ix_results_metric", "job_id", "metric_id"),
        Index("ix_results_source_payload", "job_id", "source_payload_ref"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("evaluation_jobs.id", ondelete="CASCADE")
    )
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("evaluation_tickets.id", ondelete="CASCADE", use_alter=True),
    )
    payload_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    source_payload_ref: Mapped[str | None] = mapped_column(Text)
    metric_record_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    metric_id: Mapped[str | None] = mapped_column(Text)
    metric_version_number: Mapped[int | None] = mapped_column(Integer)
    check_id: Mapped[str] = mapped_column(Text)
    check_type: Mapped[str] = mapped_column(CHECK_TYPE)
    evaluator_type: Mapped[str] = mapped_column(Text)
    evaluator_version: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(RESULT_STATUS)
    passed: Mapped[bool | None] = mapped_column(Boolean)
    score: Mapped[float | None] = mapped_column(Double)
    explanation: Mapped[str | None] = mapped_column(Text)
    evidence_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    input_snapshot_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    output_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    execution_time_ms: Mapped[float | None] = mapped_column(Double)
    attempt_count: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EvaluationTicketRow(Base):
    __tablename__ = "evaluation_tickets"
    __table_args__ = (
        CheckConstraint("max_attempts >= 1", name="ck_tickets_max_attempts"),
        CheckConstraint(
            "attempt_count >= 0 AND attempt_count <= max_attempts + 1",
            name="ck_tickets_attempts",
        ),
        CheckConstraint(
            "jsonb_typeof(metric_snapshot_json) = 'object'", name="ck_tickets_snapshot_object"
        ),
        Index(
            "uq_tickets_job_payload_metric",
            "job_id",
            "source_payload_ref",
            "metric_record_id",
            unique=True,
        ),
        Index(
            "ix_tickets_claim",
            text("priority DESC"),
            "created_at",
            "available_at",
            postgresql_where=text("status IN ('READY', 'RETRY')"),
        ),
        Index(
            "ix_tickets_lease",
            "lease_expires_at",
            postgresql_where=text("status = 'RUNNING'"),
        ),
        Index("ix_tickets_job_status", "job_id", "status"),
        Index("ix_tickets_payload", "payload_id"),
        Index("ix_tickets_agent", "job_id", "agent_id"),
        Index("ix_tickets_metric_record", "metric_record_id"),
        Index("ix_tickets_source_payload", "job_id", "source_payload_ref"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("evaluation_jobs.id", ondelete="CASCADE")
    )
    payload_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    source_dataset_id: Mapped[str] = mapped_column(Text)
    source_payload_ref: Mapped[str] = mapped_column(Text)
    source_payload_id: Mapped[str | None] = mapped_column(Text)
    metric_record_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    metric_id: Mapped[str] = mapped_column(Text)
    metric_version_number: Mapped[int] = mapped_column(Integer)
    agent_id: Mapped[str] = mapped_column(Text)
    metric_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    check_id: Mapped[str] = mapped_column(Text)
    check_type: Mapped[str] = mapped_column(CHECK_TYPE)
    evaluator: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(TICKET_STATUS, server_default="READY")
    priority: Mapped[int] = mapped_column(Integer, server_default="0")
    attempt_count: Mapped[int] = mapped_column(Integer, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, server_default="3")
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    worker_id: Mapped[str | None] = mapped_column(Text)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("evaluation_results.id", ondelete="SET NULL", use_alter=True),
    )
    input_snapshot_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
