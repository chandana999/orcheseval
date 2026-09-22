"""Persistence retries SQLAlchemy-wrapped PostgreSQL deadlocks."""

from __future__ import annotations

from psycopg.errors import DeadlockDetected, QueryCanceled
from sqlalchemy.exc import OperationalError

from app.core.database import transaction
from app.evaluators.base import EvaluatorOutput
from app.models.enums import ResultStatus, TicketStatus
from app.repositories.job_repository import JobRepository
from app.repositories.result_repository import ResultRepository
from app.repositories.ticket_repository import TicketRepository
from app.services import evaluation_service
from app.services.evaluation_service import execute_ticket, is_postgres_deadlock
from app.services.ticket_service import claim_tickets


def _wrapped_deadlock() -> OperationalError:
    return OperationalError("UPDATE evaluation_tickets", {}, DeadlockDetected("deadlock detected"))


def _passed_output() -> EvaluatorOutput:
    return EvaluatorOutput(
        status=ResultStatus.PASSED,
        passed=True,
        score=1.0,
        explanation="ok",
        evaluator_type="required_fields",
    )


def test_sqlstate_40p01_is_a_deadlock_and_other_errors_are_not():
    class SqlStateOnly(Exception):
        sqlstate = "40P01"

    class Serialization(Exception):
        sqlstate = "40001"

    assert is_postgres_deadlock(_wrapped_deadlock())
    assert is_postgres_deadlock(OperationalError("UPDATE", {}, SqlStateOnly("deadlock detected")))
    assert not is_postgres_deadlock(
        OperationalError("UPDATE", {}, Serialization("could not serialize access"))
    )
    assert not is_postgres_deadlock(
        OperationalError("SELECT", {}, QueryCanceled("canceling statement due to statement timeout"))
    )


def test_wrapped_deadlock_retries_persistence_without_an_evaluation_retry(
    seeded_job, monkeypatch
):
    with transaction() as conn:
        ticket = claim_tickets(conn, worker_id="w-1", limit=1, lease_seconds=60)[0]
    assert ticket.attempt_count == 1

    calls = {"n": 0}
    real_persist = evaluation_service._persist_once

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _wrapped_deadlock()
        return real_persist(*args, **kwargs)

    monkeypatch.setattr(evaluation_service, "_persist_once", flaky)
    monkeypatch.setattr(evaluation_service.time, "sleep", lambda _seconds: None)

    outcome = evaluation_service._persist(
        ticket,
        _passed_output(),
        None,
        target=TicketStatus.DONE,
        worker_id="w-1",
    )

    assert calls["n"] == 2
    assert outcome.status is TicketStatus.DONE
    assert outcome.result_id is not None

    with transaction() as conn:
        stored = TicketRepository(conn).get(ticket.id)
        result = ResultRepository(conn).get_by_ticket(ticket.id)
        job = JobRepository(conn).get(ticket.job_id)

    assert stored is not None
    assert stored.status is TicketStatus.DONE
    assert stored.attempt_count == 1
    assert result is not None
    assert result.status is ResultStatus.PASSED
    assert job is not None
    assert job.completed_tickets == 1
    assert job.total_tickets == seeded_job["job"]["total_tickets"]
    assert job.failed_tickets == 0


def test_non_deadlock_operational_error_is_not_retried(seeded_job, monkeypatch):
    with transaction() as conn:
        ticket = claim_tickets(conn, worker_id="w-1", limit=1, lease_seconds=60)[0]

    calls = {"n": 0}

    def not_a_deadlock(*_args, **_kwargs):
        calls["n"] += 1
        raise OperationalError(
            "SELECT 1",
            {},
            QueryCanceled("canceling statement due to statement timeout"),
        )

    monkeypatch.setattr(evaluation_service, "_persist_once", not_a_deadlock)
    monkeypatch.setattr(evaluation_service.time, "sleep", lambda _seconds: None)

    try:
        evaluation_service._persist(
            ticket,
            _passed_output(),
            None,
            target=TicketStatus.DONE,
            worker_id="w-1",
        )
    except OperationalError as exc:
        assert not is_postgres_deadlock(exc)
    else:
        raise AssertionError("non-deadlock OperationalError was swallowed")

    assert calls["n"] == 1
    with transaction() as conn:
        stored = TicketRepository(conn).get(ticket.id)
    assert stored is not None
    assert stored.status is TicketStatus.RUNNING
    assert stored.attempt_count == 1


def test_exhausted_deadlock_retries_use_evaluation_failure_handling(seeded_job, monkeypatch):
    with transaction() as conn:
        ticket = claim_tickets(conn, worker_id="w-1", limit=1, lease_seconds=60)[0]
    assert ticket.attempt_count == 1

    calls = {"n": 0}

    def always_deadlock(*_args, **_kwargs):
        calls["n"] += 1
        raise _wrapped_deadlock()

    monkeypatch.setattr(evaluation_service, "_persist_once", always_deadlock)
    monkeypatch.setattr(evaluation_service.time, "sleep", lambda _seconds: None)

    outcome = execute_ticket(ticket, worker_id="w-1")

    assert calls["n"] == 5
    assert outcome.status is TicketStatus.RETRY
    with transaction() as conn:
        stored = TicketRepository(conn).get(ticket.id)
        job = JobRepository(conn).get(ticket.job_id)
    assert stored is not None
    assert stored.status is TicketStatus.RETRY
    assert stored.attempt_count == 1
    assert job is not None
    assert job.completed_tickets == 0
