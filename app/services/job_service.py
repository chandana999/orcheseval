"""Job creation and cancellation.

The API creates durable work and returns; it never evaluates inside the request.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from psycopg import Connection

from app.core.config import settings
from app.core.metrics import JOBS_CREATED, TICKETS_CREATED
from app.models.entities import EvaluationJob, EvaluationTicket, JobProgress
from app.models.enums import CheckType, JobStatus, TicketStatus
from app.repositories.config_repository import ConfigRepository
from app.repositories.dataset_repository import DatasetRepository
from app.repositories.job_repository import JobRepository
from app.repositories.payload_repository import PayloadRepository
from app.repositories.ticket_repository import TicketRepository
from app.services.config_validation import checks_for_job


class JobCreationError(ValueError):
    pass


@dataclass
class JobCreationResult:
    job: EvaluationJob
    ticket_count: int
    payload_count: int
    check_ids: list[str]


def resolve_payload_ids(
    conn: Connection,
    *,
    dataset_id: uuid.UUID | None,
    payload_ids: list[uuid.UUID] | None,
    max_payloads: int | None,
) -> list[uuid.UUID]:
    """Select the payloads a job will evaluate, from PostgreSQL only."""
    payloads = PayloadRepository(conn)

    if payload_ids:
        found = set(payloads.existing_ids(payload_ids))
        missing = [str(pid) for pid in payload_ids if pid not in found]
        if missing:
            raise JobCreationError(f"unknown payload ids: {', '.join(missing)}")
        selected = list(dict.fromkeys(payload_ids))
    elif dataset_id is not None:
        if DatasetRepository(conn).get(dataset_id) is None:
            raise JobCreationError(f"dataset {dataset_id} not found")
        selected = payloads.list_ids_by_dataset(dataset_id)
        if not selected:
            raise JobCreationError(f"dataset {dataset_id} has no payloads")
    else:
        raise JobCreationError("either dataset_id or payload_ids is required")

    if max_payloads is not None:
        selected = selected[:max_payloads]
    return selected


def create_job(
    conn: Connection,
    *,
    evaluation_config_id: uuid.UUID,
    dataset_id: uuid.UUID | None = None,
    payload_ids: list[uuid.UUID] | None = None,
    name: str | None = None,
    priority: int | None = None,
    max_attempts: int | None = None,
    max_payloads: int | None = None,
) -> JobCreationResult:
    """Create a job, snapshot its configuration, and create one ticket per
    payload x check. Everything happens in the caller's transaction."""
    config = ConfigRepository(conn).get(evaluation_config_id)
    if config is None:
        raise JobCreationError(f"evaluation config {evaluation_config_id} not found")

    snapshot = dict(config.config_json)
    checks = checks_for_job(snapshot)
    if not checks:
        raise JobCreationError("evaluation configuration defines no checks")

    sampling = snapshot.get("sampling") or {}
    effective_max_payloads = max_payloads or sampling.get("max_payloads")
    selected_payloads = resolve_payload_ids(
        conn,
        dataset_id=dataset_id,
        payload_ids=payload_ids,
        max_payloads=effective_max_payloads,
    )

    # Record provenance of the snapshot so results are traceable to a config version.
    snapshot["_snapshot"] = {
        "evaluation_config_id": str(config.id),
        "config_name": config.name,
        "config_version": config.version,
    }

    job = EvaluationJob(
        id=uuid.uuid4(),
        name=name,
        dataset_id=dataset_id,
        evaluation_config_id=config.id,
        config_snapshot_json=snapshot,
        status=JobStatus.CREATED,
        payload_count=len(selected_payloads),
        total_tickets=len(selected_payloads) * len(checks),
        completed_tickets=0,
        failed_tickets=0,
        cancelled_tickets=0,
        not_applicable_tickets=0,
        created_at=None,
    )
    jobs = JobRepository(conn)
    job = jobs.insert(job)

    default_max_attempts = max_attempts or settings.runner_max_attempts
    tickets: list[EvaluationTicket] = []
    for payload_id in selected_payloads:
        for check in checks:
            tickets.append(
                EvaluationTicket(
                    id=uuid.uuid4(),
                    job_id=job.id,
                    payload_id=payload_id,
                    evaluation_config_id=config.id,
                    check_id=check["check_id"],
                    check_type=CheckType(check["check_type"]),
                    evaluator=check["evaluator"],
                    status=TicketStatus.READY,
                    priority=priority if priority is not None else int(check.get("priority", 0)),
                    attempt_count=0,
                    max_attempts=int(check.get("max_attempts", default_max_attempts)),
                )
            )

    created = TicketRepository(conn).bulk_insert(tickets)
    jobs.set_total_tickets(job.id, total=created, status=JobStatus.READY)
    job = jobs.get(job.id)
    assert job is not None

    JOBS_CREATED.inc()
    TICKETS_CREATED.inc(created)
    return JobCreationResult(
        job=job,
        ticket_count=created,
        payload_count=len(selected_payloads),
        check_ids=[c["check_id"] for c in checks],
    )


@dataclass
class CancellationResult:
    job: EvaluationJob
    cancelled_tickets: int
    running_tickets: int
    progress: JobProgress | None


def cancel_job(conn: Connection, job_id: uuid.UUID) -> CancellationResult:
    """Request cancellation and cancel every unclaimed ticket, atomically.

    Tickets already RUNNING are left alone: the runner notices the cancellation
    before persisting and settles them as CANCELLED, and the recovery sweep
    catches any whose runner died. Execution history is preserved either way.
    """
    jobs = JobRepository(conn)
    job = jobs.get_for_update(job_id)
    if job is None:
        raise JobCreationError(f"job {job_id} not found")
    if job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.PARTIAL_FAILED}:
        raise JobCreationError(f"cannot cancel a job in {job.status.value} state")

    jobs.request_cancellation(job_id)
    cancelled = TicketRepository(conn).cancel_unclaimed(job_id)
    progress = jobs.recompute_progress(job_id)
    updated = jobs.get(job_id)
    assert updated is not None
    return CancellationResult(
        job=updated,
        cancelled_tickets=cancelled,
        running_tickets=progress.running_tickets if progress else 0,
        progress=progress,
    )


def retry_failed_tickets(conn: Connection, job_id: uuid.UUID) -> tuple[EvaluationJob, int]:
    """Reset FAILED tickets to READY so the runner picks them up again."""
    jobs = JobRepository(conn)
    job = jobs.get_for_update(job_id)
    if job is None:
        raise JobCreationError(f"job {job_id} not found")
    if job.cancellation_requested_at is not None:
        raise JobCreationError("cannot retry tickets on a cancelled job")

    reset_ids = TicketRepository(conn).reset_failed(job_id)
    if not reset_ids:
        raise JobCreationError("no failed tickets to retry")
    jobs.recompute_progress(job_id)
    updated = jobs.get(job_id)
    assert updated is not None
    return updated, len(reset_ids)


def job_status_payload(conn: Connection, job: EvaluationJob) -> dict[str, Any]:
    """Job view with counters recomputed from durable ticket state."""
    counts = TicketRepository(conn).counts_by_status(job.id)
    terminal = sum(
        counts.get(status.value, 0)
        for status in (
            TicketStatus.DONE,
            TicketStatus.FAILED,
            TicketStatus.CANCELLED,
            TicketStatus.NOT_APPLICABLE,
        )
    )
    total = job.total_tickets or sum(counts.values())
    return {
        "ticket_counts": counts,
        "terminal_tickets": terminal,
        "progress_ratio": round(terminal / total, 4) if total else 0.0,
    }
