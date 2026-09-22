"""Job creation and cancellation.

The API creates durable work and returns; it never evaluates inside the request.
Paylods are read from EVALUATION_TEMP_ROOT; PostgreSQL stores jobs, tickets, and
results. Each ticket freezes the exact metric record version used for execution.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.core.metrics import JOBS_CREATED, TICKETS_CREATED
from app.models.entities import EvaluationJob, EvaluationTicket, JobProgress
from app.models.enums import CheckType, JobStatus, TicketStatus
from app.repositories.job_repository import JobRepository
from app.repositories.ticket_repository import TicketRepository
from app.services.dataset_folder import (
    DatasetFolderError,
    PayloadFile,
    read_dataset_folder,
)
from app.services.payload_validation import PayloadValidationError, validate_payload
from app.services.profile_resolution import (
    ProfileResolutionError,
    checks_for_agent,
    extract_agent_id,
)

logger = get_logger(__name__)


class JobCreationError(ValueError):
    status_code = 400


@dataclass
class JobCreationResult:
    job: EvaluationJob
    ticket_count: int
    payload_count: int
    dataset_id: str
    metric_ids: list[str]
    profiles: list[str]
    warnings: list[str]
    tickets_summary: dict[str, Any]
    replayed: bool = False


def _wrap(exc: BaseException) -> JobCreationError:
    error = JobCreationError(str(exc))
    error.status_code = int(getattr(exc, "status_code", 400))
    return error


def _request_hash(
    *,
    dataset_id: str,
    name: str | None,
    priority: int | None,
    max_attempts: int | None,
) -> str:
    body = json.dumps(
        {
            "dataset_id": dataset_id,
            "name": name,
            "priority": priority,
            "max_attempts": max_attempts,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode()).hexdigest()


def _result_for_existing_job(
    session: Session, job: EvaluationJob, stored_hash: str | None, request_hash: str
) -> JobCreationResult:
    if stored_hash != request_hash:
        error = JobCreationError("Idempotency-Key was already used for a different request")
        error.status_code = 409
        raise error
    snapshot = job.config_snapshot_json or {}
    agents = snapshot.get("agents") or {}
    profiles = list(agents)
    metric_ids: list[str] = []
    for agent_doc in agents.values():
        for metric in agent_doc.get("metrics") or []:
            metric_id = metric.get("metric_id")
            if metric_id and metric_id not in metric_ids:
                metric_ids.append(metric_id)
    by_agent: dict[str, int] = defaultdict(int)
    for payload in snapshot.get("payloads") or []:
        agent_id = payload.get("agent_id")
        metrics = (agents.get(agent_id) or {}).get("metrics") or []
        if agent_id:
            by_agent[agent_id] += len(metrics)
    counts = TicketRepository(session).counts_by_status(job.id)
    return JobCreationResult(
        job=job,
        ticket_count=job.total_tickets,
        payload_count=job.payload_count,
        dataset_id=job.dataset_id or "",
        metric_ids=metric_ids,
        profiles=profiles,
        warnings=[],
        tickets_summary={"by_agent": dict(by_agent), "by_status": counts},
        replayed=True,
    )


def create_job(
    session: Session,
    *,
    dataset_id: str,
    name: str | None = None,
    priority: int | None = None,
    max_attempts: int | None = None,
    idempotency_key: str | None = None,
) -> JobCreationResult:
    """Create a job and one ticket per payload file x mapped metric.

    Everything happens in the caller's transaction. A failure before COMMIT
    leaves no job or ticket rows.
    """
    key = idempotency_key.strip() if idempotency_key else None
    if idempotency_key is not None and not key:
        raise JobCreationError("Idempotency-Key is empty")
    if key is not None and len(key) > 200:
        raise JobCreationError("Idempotency-Key is too long")
    request_hash = (
        _request_hash(
            dataset_id=dataset_id, name=name, priority=priority, max_attempts=max_attempts
        )
        if key
        else None
    )
    jobs = JobRepository(session)
    if key:
        existing = jobs.get_by_idempotency_key(key)
        if existing is not None:
            return _result_for_existing_job(session, existing[0], existing[1], request_hash or "")

    try:
        folder = read_dataset_folder(dataset_id)
    except (DatasetFolderError, PayloadValidationError) as exc:
        raise _wrap(exc) from exc

    default_max_attempts = max_attempts or settings.runner_max_attempts
    agent_cache: dict[str, list[dict[str, Any]]] = {}
    prepared: list[tuple[PayloadFile, uuid.UUID, str, list[dict[str, Any]]]] = []
    warnings = list(folder.warnings)
    metric_ids: list[str] = []
    agents_used: list[str] = []

    for payload_file in folder.payloads:
        source = payload_file.filename
        try:
            report = validate_payload(payload_file.payload)
            warnings.extend(f"{source}: {w}" for w in report.get("warnings") or [])
            agent_id = extract_agent_id(payload_file.payload, source=source)
            if agent_id not in agent_cache:
                agent_cache[agent_id] = checks_for_agent(folder.config, agent_id, source=source)
                agents_used.append(agent_id)
            snapshots = agent_cache[agent_id]
        except (PayloadValidationError, ProfileResolutionError) as exc:
            raise _wrap(exc) from exc

        payload_uuid = uuid.uuid4()
        prepared.append((payload_file, payload_uuid, agent_id, snapshots))

    tickets: list[EvaluationTicket] = []
    snapshot_doc: dict[str, Any] = {
        "dataset_id": dataset_id,
        "payloads": [],
        "agents": {},
    }

    for payload_file, payload_uuid, agent_id, snapshots in prepared:
        snapshot_doc["payloads"].append(
            {
                "file": payload_file.filename,
                "payload_id": payload_file.payload_id,
                "agent_id": agent_id,
                "synthetic_payload_id": str(payload_uuid),
            }
        )
        if agent_id not in snapshot_doc["agents"]:
            snapshot_doc["agents"][agent_id] = {
                "metrics": [
                    {
                        "metric_record_id": s["metric_record_id"],
                        "metric_id": s["metric_id"],
                        "metric_code": s["metric_code"],
                        "metric_version_number": s["metric_version_number"],
                        "check_id": s["definition_payload"]["check_id"],
                    }
                    for s in snapshots
                ]
            }
        for frozen in snapshots:
            check = frozen["definition_payload"]
            metric_ids.append(frozen["metric_id"])
            tickets.append(
                EvaluationTicket(
                    id=uuid.uuid4(),
                    job_id=uuid.uuid4(),  # replaced after job insert
                    payload_id=payload_uuid,
                    source_dataset_id=dataset_id,
                    source_payload_ref=payload_file.filename,
                    source_payload_id=payload_file.payload_id,
                    metric_record_id=uuid.UUID(frozen["metric_record_id"]),
                    metric_id=frozen["metric_id"],
                    metric_version_number=int(frozen["metric_version_number"]),
                    evaluation_profile_id=agent_id,
                    metric_snapshot_json=frozen,
                    check_id=check["check_id"],
                    check_type=CheckType(check["check_type"]),
                    evaluator=check["evaluator"],
                    status=TicketStatus.READY,
                    priority=priority if priority is not None else int(check.get("priority", 0)),
                    attempt_count=0,
                    max_attempts=int(check.get("max_attempts", default_max_attempts)),
                )
            )

    if not tickets:
        raise JobCreationError("no evaluation tickets could be created from the dataset")

    job = EvaluationJob(
        id=uuid.uuid4(),
        name=name,
        dataset_id=dataset_id,
        config_snapshot_json=snapshot_doc,
        status=JobStatus.CREATED,
        payload_count=len(prepared),
        total_tickets=len(tickets),
        completed_tickets=0,
        failed_tickets=0,
        cancelled_tickets=0,
        not_applicable_tickets=0,
        created_at=None,
    )
    job = jobs.insert(
        job, idempotency_key=key, idempotency_request_hash=request_hash
    )
    for ticket in tickets:
        ticket.job_id = job.id

    created = TicketRepository(session).bulk_insert(tickets)
    jobs.set_total_tickets(job.id, total=created, status=JobStatus.READY)
    job = jobs.get(job.id)
    assert job is not None

    unique_metrics = list(dict.fromkeys(metric_ids))
    by_agent: dict[str, int] = defaultdict(int)
    for ticket in tickets:
        by_agent[ticket.evaluation_profile_id] += 1

    JOBS_CREATED.inc()
    TICKETS_CREATED.inc(created)
    logger.info(
        "evaluation_job_created",
        job_id=str(job.id),
        dataset_id=dataset_id,
        payload_count=len(prepared),
        ticket_count=created,
        profiles=agents_used,
    )
    return JobCreationResult(
        job=job,
        ticket_count=created,
        payload_count=len(prepared),
        dataset_id=dataset_id,
        metric_ids=unique_metrics,
        profiles=agents_used,
        warnings=warnings,
        tickets_summary={
            "by_agent": dict(by_agent),
            "by_status": {"READY": created},
        },
    )


@dataclass
class CancellationResult:
    job: EvaluationJob
    cancelled_tickets: int
    running_tickets: int
    progress: JobProgress | None


def cancel_job(session: Session, job_id: uuid.UUID) -> CancellationResult:
    """Request cancellation and cancel every unclaimed ticket, atomically.

    Tickets already RUNNING are left alone: the runner notices the cancellation
    before persisting and settles them as CANCELLED, and the recovery sweep
    catches any whose runner died. Execution history is preserved either way.
    """
    jobs = JobRepository(session)
    job = jobs.get_for_update(job_id)
    if job is None:
        error = JobCreationError(f"job {job_id} not found")
        error.status_code = 404
        raise error
    if job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.PARTIAL_FAILED}:
        error = JobCreationError(f"cannot cancel a job in {job.status.value} state")
        error.status_code = 409
        raise error

    jobs.request_cancellation(job_id)
    cancelled = TicketRepository(session).cancel_unclaimed(job_id)
    progress = jobs.recompute_progress(job_id)
    updated = jobs.get(job_id)
    assert updated is not None
    return CancellationResult(
        job=updated,
        cancelled_tickets=cancelled,
        running_tickets=progress.running_tickets if progress else 0,
        progress=progress,
    )


def retry_failed_tickets(session: Session, job_id: uuid.UUID) -> tuple[EvaluationJob, int]:
    """Reset FAILED tickets to READY so the runner picks them up again.

    Retries keep the same job, ticket, payload reference, metric record, metric
    version, and frozen configuration snapshot. The active metric configuration
    is not re-resolved.
    """
    jobs = JobRepository(session)
    job = jobs.get_for_update(job_id)
    if job is None:
        error = JobCreationError(f"job {job_id} not found")
        error.status_code = 404
        raise error
    if job.cancellation_requested_at is not None:
        error = JobCreationError("cannot retry tickets on a cancelled job")
        error.status_code = 409
        raise error

    reset_ids = TicketRepository(session).reset_failed(job_id)
    if not reset_ids:
        error = JobCreationError("no failed tickets to retry")
        error.status_code = 409
        raise error
    jobs.recompute_progress(job_id)
    updated = jobs.get(job_id)
    assert updated is not None
    return updated, len(reset_ids)


def job_status_payload(session: Session, job: EvaluationJob) -> dict[str, Any]:
    """Job view with counters recomputed from durable ticket state."""
    counts = TicketRepository(session).counts_by_status(job.id)
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
