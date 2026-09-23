"""Recovery of abandoned work.

A runner that crashes leaves tickets RUNNING with an expired lease. PostgreSQL is
the source of truth, so recovery is a query: reclaim expired leases, settle
tickets belonging to cancelled jobs, and recompute the affected jobs.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from evalorch.core.config import settings
from evalorch.core.logging import get_logger
from evalorch.core.metrics import TICKET_TRANSITIONS, TICKETS_RECOVERED
from evalorch.models.enums import TicketStatus
from evalorch.repositories.job_repository import JobRepository
from evalorch.repositories.ticket_repository import TicketRepository

logger = get_logger(__name__)


@dataclass
class RecoveryReport:
    recovered_to_retry: int = 0
    recovered_to_failed: int = 0
    cancelled_running: int = 0
    jobs_updated: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.recovered_to_retry + self.recovered_to_failed + self.cancelled_running

    def as_dict(self) -> dict[str, object]:
        return {
            "recovered_to_retry": self.recovered_to_retry,
            "recovered_to_failed": self.recovered_to_failed,
            "cancelled_running": self.cancelled_running,
            "jobs_updated": self.jobs_updated,
            "total": self.total,
        }


def _record_recovery(report: RecoveryReport, status: TicketStatus) -> None:
    if status is TicketStatus.CANCELLED:
        report.cancelled_running += 1
        outcome, to_status = "cancelled", "CANCELLED"
    elif status is TicketStatus.FAILED:
        report.recovered_to_failed += 1
        outcome, to_status = "failed", "FAILED"
    else:
        report.recovered_to_retry += 1
        outcome, to_status = "retry", "RETRY"
    TICKETS_RECOVERED.labels(outcome=outcome).inc()
    TICKET_TRANSITIONS.labels(from_status="RUNNING", to_status=to_status).inc()


def recover_abandoned_tickets(session: Session, *, limit: int | None = None) -> RecoveryReport:
    """Reclaim expired leases and settle running tickets on cancelled jobs."""
    batch_limit = settings.runner_recovery_batch_limit if limit is None else limit
    report = RecoveryReport()
    tickets = TicketRepository(session)
    affected: set[uuid.UUID] = set()

    for ticket in tickets.recover_expired_leases(limit=batch_limit):
        affected.add(ticket.job_id)
        _record_recovery(report, ticket.status)

    for ticket in tickets.cancel_running_for_cancelled_jobs(limit=batch_limit):
        affected.add(ticket.job_id)
        _record_recovery(report, ticket.status)

    jobs = JobRepository(session)
    for job_id in affected:
        jobs.recompute_progress(job_id)
        report.jobs_updated.append(str(job_id))

    if report.total:
        logger.info("tickets_recovered", **report.as_dict())
    return report


def reconcile_job(session: Session, job_id: uuid.UUID) -> None:
    """Force a job's counters and status to match its ticket rows."""
    JobRepository(session).recompute_progress(job_id)


def reconcile_active_jobs(session: Session) -> int:
    """Recompute every job that still has non-terminal tickets (startup sweep)."""
    jobs = JobRepository(session)
    job_ids = TicketRepository(session).jobs_with_active_tickets()
    for job_id in job_ids:
        jobs.recompute_progress(job_id)
    return len(job_ids)
