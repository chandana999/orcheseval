"""Ticket claiming and settlement.

Every settlement validates the transition, persists it, and recomputes job
progress from durable ticket rows inside the same transaction.
"""

from __future__ import annotations

import os
import socket
import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.core.metrics import TICKET_TRANSITIONS, TICKETS_CLAIMED
from app.models.entities import EvaluationTicket, JobProgress
from app.models.enums import TicketStatus
from app.repositories.job_repository import JobRepository
from app.repositories.ticket_repository import TicketRepository


logger = get_logger(__name__)


def build_worker_id(prefix: str = "runner") -> str:
    return f"{prefix}:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def claim_tickets(
    session: Session,
    *,
    worker_id: str,
    limit: int | None = None,
    lease_seconds: int | None = None,
    job_id: uuid.UUID | None = None,
) -> list[EvaluationTicket]:
    """Atomically claim runnable tickets (FOR UPDATE SKIP LOCKED)."""
    tickets = TicketRepository(session).claim(
        limit=limit or settings.runner_claim_batch_size,
        worker_id=worker_id,
        lease_seconds=lease_seconds or settings.runner_lease_seconds,
        job_id=job_id,
    )
    if tickets:
        TICKETS_CLAIMED.inc(len(tickets))
        TICKET_TRANSITIONS.labels(from_status="READY", to_status="RUNNING").inc(len(tickets))
        # Reflect that the job has started, without blocking other claimers.
        JobRepository(session).mark_running_if_pending(list({t.job_id for t in tickets}))
    return tickets


def settle_ticket(
    session: Session,
    ticket_id: uuid.UUID,
    target: TicketStatus,
    *,
    from_status: TicketStatus | None = None,
    result_id: uuid.UUID | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    delay_seconds: float | None = None,
    input_snapshot: dict[str, Any] | None = None,
    job_id: uuid.UUID | None = None,
    worker_id: str | None = None,
) -> tuple[EvaluationTicket, JobProgress | None]:
    """Transition a ticket and refresh its job's progress.

    Lock order is always job row first, then ticket row. Updating a ticket
    takes a SHARE lock on its parent job (FK); taking FOR UPDATE on the job
    afterwards deadlocks when two tickets of the same job settle together.
    """
    if not worker_id:
        raise ValueError("worker_id is required to settle a ticket")
    tickets = TicketRepository(session)
    jobs = JobRepository(session)
    resolved_job_id = job_id
    if resolved_job_id is None:
        current = tickets.get(ticket_id)
        if current is None:
            raise LookupError(f"ticket {ticket_id} not found")
        resolved_job_id = current.job_id
    jobs.get_for_update(resolved_job_id)
    ticket = tickets.transition(
        ticket_id,
        target,
        result_id=result_id,
        error_code=error_code,
        error_message=error_message,
        delay_seconds=delay_seconds,
        input_snapshot=input_snapshot,
        expected_worker_id=worker_id,
    )
    TICKET_TRANSITIONS.labels(
        from_status=(from_status or TicketStatus.RUNNING).value, to_status=target.value
    ).inc()
    progress = jobs.recompute_progress(resolved_job_id, lock=False)
    return ticket, progress


def heartbeat_ticket(
    session: Session,
    ticket_id: uuid.UUID,
    *,
    worker_id: str,
    lease_seconds: int,
) -> EvaluationTicket | None:
    """Extend the lease for a ticket this worker still owns. No-op when it does not."""
    extended = TicketRepository(session).extend_lease(
        ticket_id, worker_id=worker_id, lease_seconds=lease_seconds
    )
    if extended is not None:
        logger.info(
            "lease_extended",
            ticket_id=str(extended.id),
            job_id=str(extended.job_id),
            worker_id=worker_id,
        )
    return extended


def refresh_ticket_gauges(session: Session) -> dict[str, int]:
    counts = TicketRepository(session).global_counts_by_status()
    from app.core.metrics import TICKETS_BY_STATUS

    for status in TicketStatus:
        TICKETS_BY_STATUS.labels(status=status.value).set(counts.get(status.value, 0))
    return counts
