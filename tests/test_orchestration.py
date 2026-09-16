"""Ticket claiming, state machine, and recovery against real PostgreSQL."""

from __future__ import annotations

import uuid

import psycopg
import pytest
from psycopg.rows import dict_row

from app.core.config import settings
from app.core.database import transaction
from app.models.enums import IllegalTicketTransition, JobStatus, TicketStatus
from app.repositories.job_repository import JobRepository
from app.repositories.ticket_repository import TicketRepository
from app.services.recovery_service import recover_abandoned_tickets
from app.services.ticket_service import claim_tickets, settle_ticket


def _raw_connection():
    """A connection outside the pool, so two claims can overlap in time."""
    return psycopg.connect(settings.conninfo, row_factory=dict_row, autocommit=False)


def test_claim_marks_running_with_lease_and_attempt(seeded_job):
    job_id = uuid.UUID(seeded_job["job"]["id"])
    with transaction() as conn:
        claimed = claim_tickets(conn, worker_id="w-1", limit=2, lease_seconds=60)
        assert len(claimed) == 2
        for ticket in claimed:
            assert ticket.status is TicketStatus.RUNNING
            assert ticket.worker_id == "w-1"
            assert ticket.attempt_count == 1
            assert ticket.lease_expires_at is not None
        job = JobRepository(conn).get(job_id)
        assert job.status is JobStatus.RUNNING
        assert job.started_at is not None


def test_claim_respects_priority_order(seeded_job):
    with transaction() as conn:
        claimed = claim_tickets(conn, worker_id="w-1", limit=2)
    # summary_present has priority 5, the other checks default to 0.
    assert {t.check_id for t in claimed} == {"summary_present"}


def test_skip_locked_prevents_two_runners_taking_the_same_ticket(seeded_job):
    first = _raw_connection()
    second = _raw_connection()
    try:
        with first.transaction():
            batch_one = TicketRepository(first).claim(
                limit=3, worker_id="w-1", lease_seconds=60
            )
            assert len(batch_one) == 3
            # While the first transaction is still open, the second runner must
            # skip those rows rather than block on them.
            with second.transaction():
                batch_two = TicketRepository(second).claim(
                    limit=6, worker_id="w-2", lease_seconds=60
                )
        ids_one = {t.id for t in batch_one}
        ids_two = {t.id for t in batch_two}
        assert len(batch_two) == 3
        assert ids_one.isdisjoint(ids_two)
    finally:
        first.close()
        second.close()


def test_tickets_of_cancelled_jobs_are_not_claimable(seeded_job, client):
    job_id = seeded_job["job"]["id"]
    client.post(f"/api/v1/evaluation-jobs/{job_id}/cancel")
    with transaction() as conn:
        assert claim_tickets(conn, worker_id="w-1", limit=10) == []


def test_retry_backoff_delays_availability(seeded_job):
    with transaction() as conn:
        ticket = claim_tickets(conn, worker_id="w-1", limit=1)[0]
    with transaction() as conn:
        settle_ticket(
            conn,
            ticket.id,
            TicketStatus.RETRY,
            error_code="TRANSIENT_ERROR",
            error_message="provider timeout",
            delay_seconds=300,
        )
    with transaction() as conn:
        stored = TicketRepository(conn).get(ticket.id)
        assert stored.status is TicketStatus.RETRY
        assert stored.worker_id is None
        # Not runnable until the backoff elapses.
        claimed_ids = {t.id for t in claim_tickets(conn, worker_id="w-2", limit=10)}
        assert ticket.id not in claimed_ids


def test_retry_becomes_claimable_once_available(seeded_job):
    with transaction() as conn:
        ticket = claim_tickets(conn, worker_id="w-1", limit=1)[0]
    with transaction() as conn:
        settle_ticket(
            conn, ticket.id, TicketStatus.RETRY, error_code="TRANSIENT_ERROR", delay_seconds=0
        )
    with transaction() as conn:
        claimed = claim_tickets(conn, worker_id="w-2", limit=10)
        assert ticket.id in {t.id for t in claimed}
        again = next(t for t in claimed if t.id == ticket.id)
        assert again.attempt_count == 2


def test_illegal_transitions_are_rejected(seeded_job):
    with transaction() as conn:
        tickets = TicketRepository(conn)
        ticket_id = tickets.claim(limit=1, worker_id="w-1", lease_seconds=60)[0].id
        tickets.transition(ticket_id, TicketStatus.DONE)

    with transaction() as conn:
        with pytest.raises(IllegalTicketTransition):
            TicketRepository(conn).transition(ticket_id, TicketStatus.RUNNING)

    with transaction() as conn:
        with pytest.raises(IllegalTicketTransition):
            TicketRepository(conn).transition(ticket_id, TicketStatus.RETRY)


def test_expired_lease_is_recovered_to_retry_then_failed(seeded_job):
    with transaction() as conn:
        ticket = claim_tickets(conn, worker_id="w-crashed", limit=1, lease_seconds=60)[0]
        # Simulate a runner that died: lease in the past, still RUNNING.
        conn.execute(
            "UPDATE evaluation_tickets SET lease_expires_at = now() - interval '1 minute' "
            "WHERE id = %s",
            (ticket.id,),
        )

    with transaction() as conn:
        report = recover_abandoned_tickets(conn)
        assert report.recovered_to_retry == 1
        recovered = TicketRepository(conn).get(ticket.id)
        assert recovered.status is TicketStatus.RETRY
        assert recovered.error_code == "LEASE_EXPIRED"
        assert recovered.worker_id is None

    # Exhaust the attempts, then the same sweep must fail the ticket.
    with transaction() as conn:
        conn.execute(
            "UPDATE evaluation_tickets SET status = 'RUNNING', attempt_count = max_attempts, "
            "lease_expires_at = now() - interval '1 minute' WHERE id = %s",
            (ticket.id,),
        )
    with transaction() as conn:
        report = recover_abandoned_tickets(conn)
        assert report.recovered_to_failed == 1
        assert TicketRepository(conn).get(ticket.id).status is TicketStatus.FAILED


def test_running_tickets_on_cancelled_jobs_are_swept(seeded_job, client):
    job_id = seeded_job["job"]["id"]
    with transaction() as conn:
        ticket = claim_tickets(conn, worker_id="w-1", limit=1, lease_seconds=60)[0]
    client.post(f"/api/v1/evaluation-jobs/{job_id}/cancel")
    with transaction() as conn:
        conn.execute(
            "UPDATE evaluation_tickets SET lease_expires_at = now() - interval '1 second' "
            "WHERE id = %s",
            (ticket.id,),
        )
    with transaction() as conn:
        report = recover_abandoned_tickets(conn)
        assert report.cancelled_running == 1
        assert TicketRepository(conn).get(ticket.id).status is TicketStatus.CANCELLED
        assert JobRepository(conn).get(uuid.UUID(job_id)).status is JobStatus.CANCELLED


def test_job_counters_are_derived_from_ticket_rows(seeded_job):
    job_id = uuid.UUID(seeded_job["job"]["id"])
    with transaction() as conn:
        tickets = claim_tickets(conn, worker_id="w-1", limit=6)
    assert len(tickets) == 6

    with transaction() as conn:
        for index, ticket in enumerate(tickets):
            if index < 4:
                settle_ticket(conn, ticket.id, TicketStatus.DONE)
            elif index == 4:
                settle_ticket(conn, ticket.id, TicketStatus.FAILED, error_code="BOOM")
            else:
                settle_ticket(conn, ticket.id, TicketStatus.NOT_APPLICABLE)

    with transaction() as conn:
        job = JobRepository(conn).get(job_id)
        assert job.completed_tickets == 4
        assert job.failed_tickets == 1
        assert job.not_applicable_tickets == 1
        # Some checks passed and one failed: the job is partially failed.
        assert job.status is JobStatus.PARTIAL_FAILED
        assert job.completed_at is not None


def test_all_done_marks_job_completed(seeded_job):
    job_id = uuid.UUID(seeded_job["job"]["id"])
    with transaction() as conn:
        tickets = claim_tickets(conn, worker_id="w-1", limit=6)
    with transaction() as conn:
        for ticket in tickets:
            settle_ticket(conn, ticket.id, TicketStatus.DONE)
    with transaction() as conn:
        job = JobRepository(conn).get(job_id)
        assert job.status is JobStatus.COMPLETED
        assert job.completed_tickets == 6


def test_failed_ticket_can_be_reset_to_ready(seeded_job, client):
    job_id = seeded_job["job"]["id"]
    with transaction() as conn:
        tickets = claim_tickets(conn, worker_id="w-1", limit=6)
    with transaction() as conn:
        for ticket in tickets:
            settle_ticket(conn, ticket.id, TicketStatus.FAILED, error_code="BOOM")
    with transaction() as conn:
        assert JobRepository(conn).get(uuid.UUID(job_id)).status is JobStatus.FAILED

    response = client.post(f"/api/v1/evaluation-jobs/{job_id}/retry-failed")
    assert response.status_code == 200
    assert response.json()["reset_count"] == 6

    with transaction() as conn:
        counts = TicketRepository(conn).counts_by_status(uuid.UUID(job_id))
        assert counts == {"READY": 6}
        reset = TicketRepository(conn).get(tickets[0].id)
        assert reset.attempt_count == 0
        assert reset.error_code is None
