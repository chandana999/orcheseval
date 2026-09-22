"""Ticket claiming, state machine, and recovery against real PostgreSQL."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.core.database import open_session, transaction
from app.models.entities import EvaluationResult
from app.models.enums import IllegalTicketTransition, JobStatus, ResultStatus, TicketStatus
from app.repositories.job_repository import JobRepository
from app.repositories.result_repository import ResultRepository
from app.repositories.ticket_repository import StaleWorkerError, TicketRepository
from app.services.evaluation_service import execute_ticket
from app.services.recovery_service import recover_abandoned_tickets
from app.services.ticket_service import claim_tickets, settle_ticket


def _raw_connection():
    """A session on its own connection, so two claims can overlap in time."""
    return open_session()


def test_ready_running_done(seeded_job):
    with transaction() as conn:
        ticket = claim_tickets(conn, worker_id="w-1", limit=1, lease_seconds=60)[0]
        assert ticket.status is TicketStatus.RUNNING
        assert ticket.worker_id == "w-1"
        assert ticket.attempt_count == 1
    with transaction() as conn:
        settled, _ = settle_ticket(conn, ticket.id, TicketStatus.DONE, worker_id="w-1")
        assert settled.status is TicketStatus.DONE
        assert settled.worker_id == "w-1"


def test_settlement_without_worker_id_is_rejected(seeded_job):
    with transaction() as conn:
        ticket = claim_tickets(conn, worker_id="w-1", limit=1)[0]
    with transaction() as conn:
        with pytest.raises(ValueError, match="worker_id is required"):
            settle_ticket(conn, ticket.id, TicketStatus.DONE)
    with transaction() as conn:
        with pytest.raises(ValueError, match="worker_id is required"):
            TicketRepository(conn).transition(ticket.id, TicketStatus.DONE)
    with transaction() as conn:
        stored = TicketRepository(conn).get(ticket.id)
        assert stored.status is TicketStatus.RUNNING
        assert stored.worker_id == "w-1"


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
        with first.begin():
            batch_one = TicketRepository(first).claim(limit=3, worker_id="w-1", lease_seconds=60)
            assert len(batch_one) == 3
            # While the first transaction is still open, the second runner must
            # skip those rows rather than block on them.
            with second.begin():
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
    client.post(f"/v1/evaluation-jobs/{job_id}/cancel")
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
            worker_id="w-1",
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
            conn,
            ticket.id,
            TicketStatus.RETRY,
            error_code="TRANSIENT_ERROR",
            delay_seconds=0,
            worker_id="w-1",
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
        tickets.transition(ticket_id, TicketStatus.DONE, expected_worker_id="w-1")

    with transaction() as conn:
        with pytest.raises(IllegalTicketTransition):
            TicketRepository(conn).transition(
                ticket_id, TicketStatus.RUNNING, expected_worker_id="w-1"
            )

    with transaction() as conn:
        with pytest.raises(IllegalTicketTransition):
            TicketRepository(conn).transition(
                ticket_id, TicketStatus.RETRY, expected_worker_id="w-1"
            )


def test_expired_lease_is_recovered_to_retry_then_failed(seeded_job):
    with transaction() as conn:
        ticket = claim_tickets(conn, worker_id="w-crashed", limit=1, lease_seconds=60)[0]
        # Simulate a runner that died: lease in the past, still RUNNING.
        conn.execute(
            text(
                "UPDATE evaluation_tickets SET lease_expires_at = now() - interval '1 minute' "
                "WHERE id = :id"
            ),
            {"id": ticket.id},
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
            text(
                "UPDATE evaluation_tickets SET status = 'RUNNING', attempt_count = max_attempts, "
                "lease_expires_at = now() - interval '1 minute' WHERE id = :id"
            ),
            {"id": ticket.id},
        )
    with transaction() as conn:
        report = recover_abandoned_tickets(conn)
        assert report.recovered_to_failed == 1
        assert TicketRepository(conn).get(ticket.id).status is TicketStatus.FAILED


def test_running_tickets_on_cancelled_jobs_are_swept(seeded_job, client):
    job_id = seeded_job["job"]["id"]
    with transaction() as conn:
        ticket = claim_tickets(conn, worker_id="w-1", limit=1, lease_seconds=60)[0]
    client.post(f"/v1/evaluation-jobs/{job_id}/cancel")
    with transaction() as conn:
        conn.execute(
            text(
                "UPDATE evaluation_tickets SET lease_expires_at = now() - interval '1 second' "
                "WHERE id = :id"
            ),
            {"id": ticket.id},
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
                settle_ticket(conn, ticket.id, TicketStatus.DONE, worker_id="w-1")
            elif index == 4:
                settle_ticket(conn, ticket.id, TicketStatus.FAILED, error_code="BOOM", worker_id="w-1")
            else:
                settle_ticket(conn, ticket.id, TicketStatus.NOT_APPLICABLE, worker_id="w-1")

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
            settle_ticket(conn, ticket.id, TicketStatus.DONE, worker_id="w-1")
    with transaction() as conn:
        job = JobRepository(conn).get(job_id)
        assert job.status is JobStatus.COMPLETED
        assert job.completed_tickets == 6


def test_stale_worker_cannot_settle_after_reclaim(seeded_job):
    with transaction() as conn:
        ticket = claim_tickets(conn, worker_id="w-1", limit=1, lease_seconds=60)[0]

    with transaction() as conn:
        conn.execute(
            text(
                "UPDATE evaluation_tickets SET lease_expires_at = now() - interval '1 minute' "
                "WHERE id = :id"
            ),
            {"id": ticket.id},
        )
    with transaction() as conn:
        report = recover_abandoned_tickets(conn)
        assert report.recovered_to_retry == 1
        # Recovery adds a short jitter; make this ticket claimable immediately.
        conn.execute(
            text(
                "UPDATE evaluation_tickets SET available_at = now() - interval '1 second' "
                "WHERE id = :id"
            ),
            {"id": ticket.id},
        )

    with transaction() as conn:
        claimed = claim_tickets(conn, worker_id="w-2", limit=10)
        again = next(item for item in claimed if item.id == ticket.id)
        assert again.status is TicketStatus.RUNNING
        assert again.worker_id == "w-2"
        assert again.attempt_count == 2

    with transaction() as conn:
        with pytest.raises(StaleWorkerError):
            settle_ticket(conn, ticket.id, TicketStatus.DONE, worker_id="w-1")
        with pytest.raises(StaleWorkerError):
            settle_ticket(
                conn, ticket.id, TicketStatus.FAILED, error_code="BOOM", worker_id="w-1"
            )

    with transaction() as conn:
        ResultRepository(conn).upsert(
            EvaluationResult(
                id=uuid.uuid4(),
                job_id=ticket.job_id,
                ticket_id=ticket.id,
                payload_id=ticket.payload_id,
                source_payload_ref=ticket.source_payload_ref,
                metric_record_id=ticket.metric_record_id,
                metric_id=ticket.metric_id,
                metric_version_number=ticket.metric_version_number,
                check_id=ticket.check_id,
                check_type=ticket.check_type,
                evaluator_type=ticket.evaluator,
                evaluator_version="test",
                status=ResultStatus.PASSED,
                passed=True,
                score=1.0,
                explanation="worker-b",
                evidence_json=None,
                input_snapshot_json=None,
                output_json=None,
                error_code=None,
                error_message=None,
                execution_time_ms=1,
                attempt_count=2,
            )
        )

    outcome = execute_ticket(ticket, worker_id="w-1")
    assert outcome.status is TicketStatus.RUNNING
    assert outcome.error_code == "STALE_WORKER"

    with transaction() as conn:
        stored = TicketRepository(conn).get(ticket.id)
        assert stored.status is TicketStatus.RUNNING
        assert stored.worker_id == "w-2"
        assert stored.attempt_count == 2
        result = ResultRepository(conn).get_by_ticket(ticket.id)
        assert result is not None
        assert result.explanation == "worker-b"
        assert result.attempt_count == 2


def test_heartbeat_extends_only_the_owning_workers_lease(seeded_job):
    with transaction() as conn:
        ticket = claim_tickets(conn, worker_id="w-1", limit=1, lease_seconds=30)[0]
        owned_until = ticket.lease_expires_at
    with transaction() as conn:
        rejected = TicketRepository(conn).extend_lease(
            ticket.id, worker_id="w-2", lease_seconds=300
        )
        assert rejected is None
        unchanged = TicketRepository(conn).get(ticket.id)
        assert unchanged.worker_id == "w-1"
        assert unchanged.lease_expires_at == owned_until
        extended = TicketRepository(conn).extend_lease(
            ticket.id, worker_id="w-1", lease_seconds=300
        )
        assert extended is not None
        assert extended.status is TicketStatus.RUNNING
        assert extended.worker_id == "w-1"
        assert extended.lease_expires_at > owned_until


def test_transient_attempts_retry_until_max_then_fail(seeded_job, monkeypatch):
    from app.services.errors import TransientEvaluationError

    class Boom:
        name = "required_fields"

        def run(self, values, check, context):
            raise TransientEvaluationError("provider timeout")

    monkeypatch.setattr("app.services.evaluation_service.get_evaluator", lambda name: Boom())
    ticket_id = None
    steps = ((1, TicketStatus.RETRY), (2, TicketStatus.RETRY), (3, TicketStatus.FAILED))
    for attempt, expected in steps:
        with transaction() as conn:
            claimed = claim_tickets(conn, worker_id=f"w-{attempt}", limit=1)
            ticket = claimed[0]
            if ticket_id is None:
                ticket_id = ticket.id
            assert ticket.id == ticket_id
            assert ticket.attempt_count == attempt
        outcome = execute_ticket(ticket, worker_id=ticket.worker_id)
        assert outcome.status is expected
        if expected is TicketStatus.RETRY:
            with transaction() as conn:
                conn.execute(
                    text(
                        "UPDATE evaluation_tickets SET available_at = now() + interval '1 day' "
                        "WHERE job_id = :job_id AND id <> :id AND status IN ('READY', 'RETRY')"
                    ),
                    {"job_id": ticket.job_id, "id": ticket.id},
                )
    with transaction() as conn:
        stored = TicketRepository(conn).get(ticket_id)
        assert stored.status is TicketStatus.FAILED
        assert stored.attempt_count == 3
        assert stored.error_code == "TRANSIENT_ATTEMPTS_EXHAUSTED"


def test_failed_ticket_can_be_reset_to_ready(seeded_job, client):
    job_id = seeded_job["job"]["id"]
    with transaction() as conn:
        tickets = claim_tickets(conn, worker_id="w-1", limit=6)
    with transaction() as conn:
        for ticket in tickets:
            settle_ticket(conn, ticket.id, TicketStatus.FAILED, error_code="BOOM", worker_id="w-1")
    with transaction() as conn:
        assert JobRepository(conn).get(uuid.UUID(job_id)).status is JobStatus.FAILED

    response = client.post(f"/v1/evaluation-jobs/{job_id}/retry-failed")
    assert response.status_code == 200
    assert response.json()["reset_count"] == 6

    with transaction() as conn:
        counts = TicketRepository(conn).counts_by_status(uuid.UUID(job_id))
        assert counts == {"READY": 6}
        reset = TicketRepository(conn).get(tickets[0].id)
        assert reset.attempt_count == 0
        assert reset.error_code is None
