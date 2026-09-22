"""Real PostgreSQL concurrency scenarios. Each worker opens its own transaction."""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from psycopg.errors import DeadlockDetected
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.core.database import transaction
from app.evaluators.base import EvaluatorOutput
from app.models.entities import EvaluationResult, EvaluationTicket
from app.models.enums import JobStatus, ResultStatus, TicketStatus
from app.repositories.job_repository import JobRepository
from app.repositories.result_repository import ResultRepository
from app.repositories.ticket_repository import StaleWorkerError, TicketRepository
from app.services import evaluation_service
from app.services.errors import TransientEvaluationError
from app.services.evaluation_service import execute_ticket
from app.services.recovery_service import recover_abandoned_tickets
from app.services.ticket_service import claim_tickets, heartbeat_ticket, settle_ticket
from tests.conftest import (
    API_HEADERS,
    DETERMINISTIC_CHECKS,
    make_payload,
    write_dataset,
)

_TERMINAL = {
    TicketStatus.DONE.value,
    TicketStatus.FAILED.value,
    TicketStatus.CANCELLED.value,
    TicketStatus.NOT_APPLICABLE.value,
}


def _job_id(seeded_job) -> uuid.UUID:
    return uuid.UUID(seeded_job["job"]["id"])


def _rows(sql: str, params: dict):
    with transaction() as conn:
        return list(conn.execute(text(sql), params).mappings().all())


def _defer_others(job_id: uuid.UUID, keep_id: uuid.UUID) -> None:
    with transaction() as conn:
        conn.execute(
            text(
                """
                UPDATE evaluation_tickets
                SET available_at = now() + interval '1 day'
                WHERE job_id = :job_id AND id <> :keep_id
                  AND status IN ('READY', 'RETRY')
                """
            ),
            {"job_id": job_id, "keep_id": keep_id},
        )


def _pad_ready_tickets(job_id: uuid.UUID, total: int) -> None:
    with transaction() as conn:
        _total, existing = TicketRepository(conn).list_by_job(job_id, limit=500)
        if len(existing) > total:
            raise AssertionError(f"job already has {len(existing)} tickets")
        template = existing[0]
        clones = [
            EvaluationTicket(
                id=uuid.uuid4(),
                job_id=job_id,
                payload_id=uuid.uuid4(),
                source_dataset_id=template.source_dataset_id,
                source_payload_ref=f"clone-{uuid.uuid4().hex}.json",
                source_payload_id=template.source_payload_id,
                metric_record_id=uuid.uuid4(),
                metric_id=template.metric_id,
                metric_version_number=template.metric_version_number,
                evaluation_profile_id=template.evaluation_profile_id,
                metric_snapshot_json=template.metric_snapshot_json,
                check_id=template.check_id,
                check_type=template.check_type,
                evaluator=template.evaluator,
                status=TicketStatus.READY,
                priority=template.priority,
                attempt_count=0,
                max_attempts=template.max_attempts,
            )
            for _ in range(total - len(existing))
        ]
        TicketRepository(conn).bulk_insert(clones)
        conn.execute(
            text("UPDATE evaluation_jobs SET total_tickets = :total WHERE id = :id"),
            {"total": total, "id": job_id},
        )


def _keep_only(job_id: uuid.UUID, ticket_id: uuid.UUID) -> None:
    with transaction() as conn:
        conn.execute(
            text("DELETE FROM evaluation_tickets WHERE job_id = :job_id AND id <> :id"),
            {"job_id": job_id, "id": ticket_id},
        )
        conn.execute(
            text("UPDATE evaluation_jobs SET total_tickets = 1 WHERE id = :id"),
            {"id": job_id},
        )


def _passed_output() -> EvaluatorOutput:
    return EvaluatorOutput(
        status=ResultStatus.PASSED,
        passed=True,
        score=1.0,
        explanation="ok",
        evaluator_type="required_fields",
    )


def _scripted_evaluator(run):
    class Scripted:
        name = "required_fields"

        def run(self, values, check, context):
            return run(values, check, context)

    return Scripted()


def _claim_groups(job_id: uuid.UUID):
    return _rows(
        """
        SELECT status, worker_id, COUNT(*) AS n
        FROM evaluation_tickets
        WHERE job_id = :id
        GROUP BY status, worker_id
        ORDER BY status, worker_id
        """,
        {"id": job_id},
    )


def test_ten_workers_claim_one_hundred_tickets_once(seeded_job):
    job_id = _job_id(seeded_job)
    _pad_ready_tickets(job_id, 100)
    claimed: list[uuid.UUID] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        try:
            idle = 0
            while idle < 5:
                with transaction() as conn:
                    batch = claim_tickets(
                        conn, worker_id=f"w-{index}", limit=10, lease_seconds=60, job_id=job_id
                    )
                if not batch:
                    idle += 1
                    time.sleep(0.01)
                    continue
                idle = 0
                with lock:
                    claimed.extend(ticket.id for ticket in batch)
        except BaseException as exc:  # noqa: BLE001 - surfaced to the main thread
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors, errors
    assert all(not thread.is_alive() for thread in threads)
    assert len(claimed) == 100
    assert len(set(claimed)) == 100

    groups = _claim_groups(job_id)
    assert sum(int(row["n"]) for row in groups) == 100
    workers = {f"w-{index}" for index in range(10)}
    for row in groups:
        assert row["status"] == "RUNNING", groups
        assert row["worker_id"] in workers, groups


def test_two_workers_race_one_ticket(seeded_job):
    job_id = _job_id(seeded_job)
    with transaction() as conn:
        ticket_id = TicketRepository(conn).list_by_job(job_id, limit=1)[1][0].id
    _keep_only(job_id, ticket_id)

    barrier = threading.Barrier(2)
    found: dict[str, list] = {}
    errors: list[BaseException] = []

    def worker(name: str) -> None:
        try:
            barrier.wait(timeout=10)
            with transaction() as conn:
                found[name] = claim_tickets(
                    conn, worker_id=name, limit=1, lease_seconds=60, job_id=job_id
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(name,)) for name in ("w-a", "w-b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert not errors, errors
    sizes = sorted(len(found[name]) for name in ("w-a", "w-b"))
    assert sizes == [0, 1], found
    winner = next(name for name in ("w-a", "w-b") if found[name])

    rows = _rows(
        """
        SELECT status, worker_id, attempt_count
        FROM evaluation_tickets WHERE job_id = :id
        """,
        {"id": job_id},
    )
    assert len(rows) == 1
    assert rows[0]["status"] == "RUNNING"
    assert rows[0]["worker_id"] == winner
    assert rows[0]["attempt_count"] == 1


def test_stale_worker_cannot_overwrite_after_the_new_owner_settles(seeded_job):
    job_id = _job_id(seeded_job)
    with transaction() as conn:
        ticket = claim_tickets(conn, worker_id="w-1", limit=1, lease_seconds=60, job_id=job_id)[0]
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
        assert report.recovered_to_retry >= 1
        conn.execute(
            text("UPDATE evaluation_tickets SET available_at = now() - interval '1 second' WHERE id = :id"),
            {"id": ticket.id},
        )
    with transaction() as conn:
        again = next(
            item
            for item in claim_tickets(conn, worker_id="w-2", limit=10, job_id=job_id)
            if item.id == ticket.id
        )
    result_id = uuid.uuid4()
    with transaction() as conn:
        ResultRepository(conn).upsert(
            EvaluationResult(
                id=result_id,
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
                attempt_count=again.attempt_count,
            )
        )
        settle_ticket(
            conn,
            ticket.id,
            TicketStatus.DONE,
            worker_id="w-2",
            result_id=result_id,
            job_id=ticket.job_id,
        )

    with transaction() as conn:
        before = TicketRepository(conn).get(ticket.id)
    assert before is not None
    lease_before = before.lease_expires_at

    for target in (TicketStatus.DONE, TicketStatus.FAILED):
        with transaction() as conn:
            with pytest.raises(StaleWorkerError):
                TicketRepository(conn).transition(
                    ticket.id,
                    target,
                    expected_worker_id="w-1",
                    error_code="STALE",
                    error_message="worker A must not settle",
                )
    with transaction() as conn:
        extended = heartbeat_ticket(conn, ticket.id, worker_id="w-1", lease_seconds=60)
        assert extended is None
        stored = TicketRepository(conn).get(ticket.id)
    assert stored is not None
    assert stored.lease_expires_at == lease_before
    assert stored.status is TicketStatus.DONE
    assert stored.worker_id == "w-2"
    assert stored.result_id == result_id
    with transaction() as conn:
        result = ResultRepository(conn).get_by_ticket(ticket.id)
    assert result is not None
    assert result.explanation == "worker-b"


def test_heartbeat_keeps_a_ticket_ahead_of_recovery(seeded_job):
    job_id = _job_id(seeded_job)
    with transaction() as conn:
        ticket = claim_tickets(conn, worker_id="w-1", limit=1, lease_seconds=5, job_id=job_id)[0]
    stop = threading.Event()
    errors: list[BaseException] = []

    def beat() -> None:
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and not stop.is_set():
                try:
                    with transaction() as conn:
                        owned = heartbeat_ticket(
                            conn, ticket.id, worker_id="w-1", lease_seconds=5
                        )
                    if owned is None:
                        errors.append(AssertionError("heartbeat lost ownership"))
                        return
                except OperationalError as exc:
                    if "deadlock" not in str(exc).lower():
                        raise
                time.sleep(2)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=beat)
    thread.start()
    started = time.monotonic()
    try:
        while time.monotonic() - started < 8:
            try:
                with transaction() as conn:
                    recover_abandoned_tickets(conn)
            except OperationalError as exc:
                if "deadlock" not in str(exc).lower():
                    raise
            time.sleep(1)
    finally:
        stop.set()
        thread.join(timeout=10)
    assert not errors, errors

    with transaction() as conn:
        stored = TicketRepository(conn).get(ticket.id)
    assert stored is not None
    assert stored.status is TicketStatus.RUNNING, _claim_groups(job_id)
    assert stored.worker_id == "w-1"
    with transaction() as conn:
        settled, _progress = settle_ticket(
            conn, ticket.id, TicketStatus.DONE, worker_id="w-1", job_id=job_id
        )
    assert settled.status is TicketStatus.DONE
    assert settled.worker_id == "w-1"


def test_crashed_worker_is_recovered_and_completed_by_the_next_claim(seeded_job):
    job_id = _job_id(seeded_job)
    with transaction() as conn:
        ticket = claim_tickets(conn, worker_id="w-crashed", limit=1, lease_seconds=1, job_id=job_id)[0]
    _defer_others(job_id, ticket.id)
    time.sleep(2)
    with transaction() as conn:
        report = recover_abandoned_tickets(conn)
    assert report.recovered_to_retry == 1
    rows = _rows(
        "SELECT status, worker_id, attempt_count FROM evaluation_tickets WHERE id = :id",
        {"id": ticket.id},
    )
    assert rows[0]["status"] == "RETRY"
    assert rows[0]["worker_id"] is None
    assert rows[0]["attempt_count"] == 1
    time.sleep(1.2)
    with transaction() as conn:
        claimed = claim_tickets(conn, worker_id="w-2", limit=1, lease_seconds=60, job_id=job_id)
    assert len(claimed) == 1
    assert claimed[0].id == ticket.id
    assert claimed[0].attempt_count == 2
    outcome = execute_ticket(claimed[0], worker_id="w-2")
    assert outcome.status is TicketStatus.DONE
    rows = _rows(
        "SELECT status, attempt_count, worker_id FROM evaluation_tickets WHERE id = :id",
        {"id": ticket.id},
    )
    assert rows[0]["status"] == "DONE"
    assert rows[0]["attempt_count"] == 2
    with transaction() as conn:
        result = ResultRepository(conn).get_by_ticket(ticket.id)
    assert result is not None


def test_transient_failure_then_success_uses_two_attempts(seeded_job, monkeypatch):
    job_id = _job_id(seeded_job)
    with transaction() as conn:
        ticket_id = TicketRepository(conn).list_by_job(job_id, limit=1)[1][0].id
    _defer_others(job_id, ticket_id)
    calls = {"n": 0}

    def run(_values, _check, _context):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TransientEvaluationError("provider timeout")
        return _passed_output()

    monkeypatch.setattr(
        "app.services.evaluation_service.get_evaluator", lambda _name: _scripted_evaluator(run)
    )
    monkeypatch.setattr(
        evaluation_service.settings.__class__,
        "backoff_for_attempt",
        lambda _self, _attempt: 30.0,
    )

    with transaction() as conn:
        ticket = claim_tickets(conn, worker_id="w-1", limit=1, lease_seconds=60, job_id=job_id)[0]
    assert ticket.id == ticket_id
    outcome = execute_ticket(ticket, worker_id="w-1")
    assert outcome.status is TicketStatus.RETRY
    assert outcome.retry_in_seconds == 30.0
    future = _rows(
        "SELECT available_at > now() AS later, status FROM evaluation_tickets WHERE id = :id",
        {"id": ticket.id},
    )
    assert future[0]["status"] == "RETRY"
    assert future[0]["later"] is True

    with transaction() as conn:
        conn.execute(
            text("UPDATE evaluation_tickets SET available_at = now() WHERE id = :id"),
            {"id": ticket.id},
        )
        again = claim_tickets(conn, worker_id="w-2", limit=1, lease_seconds=60, job_id=job_id)[0]
    assert again.id == ticket.id
    assert again.attempt_count == 2
    outcome = execute_ticket(again, worker_id="w-2")
    assert outcome.status is TicketStatus.DONE
    rows = _rows(
        "SELECT status, attempt_count FROM evaluation_tickets WHERE id = :id",
        {"id": ticket.id},
    )
    assert rows[0]["status"] == "DONE"
    assert rows[0]["attempt_count"] == 2
    with transaction() as conn:
        result = ResultRepository(conn).get_by_ticket(ticket.id)
    assert result is not None
    assert result.status is ResultStatus.PASSED


def test_attempts_stop_at_max_and_a_deadlock_retry_is_not_an_attempt(seeded_job, monkeypatch):
    job_id = _job_id(seeded_job)
    with transaction() as conn:
        ticket_id = TicketRepository(conn).list_by_job(job_id, limit=1)[1][0].id
        conn.execute(
            text("UPDATE evaluation_tickets SET max_attempts = 2 WHERE id = :id"),
            {"id": ticket_id},
        )
    _defer_others(job_id, ticket_id)

    def run(_values, _check, _context):
        raise TransientEvaluationError("provider timeout")

    monkeypatch.setattr(
        "app.services.evaluation_service.get_evaluator", lambda _name: _scripted_evaluator(run)
    )
    monkeypatch.setattr(evaluation_service.time, "sleep", lambda _seconds: None)
    real_persist = evaluation_service._persist_once
    persist_calls = {"n": 0}

    def flaky(*args, **kwargs):
        persist_calls["n"] += 1
        if persist_calls["n"] == 1:
            raise OperationalError(
                "UPDATE evaluation_tickets", {}, DeadlockDetected("deadlock detected")
            )
        return real_persist(*args, **kwargs)

    monkeypatch.setattr(evaluation_service, "_persist_once", flaky)

    ticket = None
    outcome = None
    for _attempt in range(2):
        with transaction() as conn:
            if ticket is not None:
                conn.execute(
                    text("UPDATE evaluation_tickets SET available_at = now() WHERE id = :id"),
                    {"id": ticket.id},
                )
            claimed = claim_tickets(conn, worker_id="w-loop", limit=1, lease_seconds=60, job_id=job_id)
        assert len(claimed) == 1
        assert claimed[0].id == ticket_id
        ticket = claimed[0]
        outcome = execute_ticket(ticket, worker_id="w-loop")
    assert outcome is not None
    assert outcome.status is TicketStatus.FAILED
    assert persist_calls["n"] == 2
    rows = _rows(
        "SELECT status, attempt_count, error_code FROM evaluation_tickets WHERE id = :id",
        {"id": ticket_id},
    )
    assert rows[0]["status"] == "FAILED", rows
    assert rows[0]["attempt_count"] == 2, rows
    assert rows[0]["error_code"] == "TRANSIENT_ATTEMPTS_EXHAUSTED", rows


def test_persist_failure_does_not_leave_a_partial_result(seeded_job, monkeypatch):
    job_id = _job_id(seeded_job)
    with transaction() as conn:
        ticket = claim_tickets(conn, worker_id="w-1", limit=1, lease_seconds=60, job_id=job_id)[0]

    def explode(*_args, **_kwargs):
        raise RuntimeError("persist failed before commit")

    monkeypatch.setattr(evaluation_service, "_persist_once", explode)
    execute_ticket(ticket, worker_id="w-1")

    with transaction() as conn:
        stored = TicketRepository(conn).get(ticket.id)
        result = ResultRepository(conn).get_by_ticket(ticket.id)
    assert stored is not None
    assert not (stored.status is TicketStatus.DONE and result is None)
    assert not (result is not None and stored.status is TicketStatus.RUNNING)

    if stored.status is TicketStatus.DONE:
        assert result is not None
    elif stored.status is TicketStatus.RETRY:
        assert result is None
        assert stored.worker_id is None
        assert stored.lease_expires_at is None
    else:
        assert stored.status is TicketStatus.RUNNING
        assert result is None


def test_concurrent_idempotent_job_creation_inserts_one_job(seeded_job, temp_root):
    del temp_root
    from fastapi.testclient import TestClient

    from app.main import app

    barrier = threading.Barrier(2)
    responses: list[tuple[int, str]] = []
    errors: list[BaseException] = []
    lock = threading.Lock()
    body = {
        "dataset_id": seeded_job["dataset_id"],
        "name": "idempotent-race",
        "priority": 5,
    }
    headers = {**API_HEADERS, "Idempotency-Key": "concurrency-same-key"}

    def post(client: TestClient) -> None:
        try:
            barrier.wait(timeout=10)
            response = client.post("/v1/evaluation-jobs", json=body, headers=headers)
            with lock:
                responses.append((response.status_code, response.text))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    clients = [TestClient(app) for _ in range(2)]
    for client in clients:
        client.__enter__()
        client.headers.update(API_HEADERS)
    threads = [threading.Thread(target=post, args=(client,)) for client in clients]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
    finally:
        for client in clients:
            client.__exit__(None, None, None)

    assert not errors, errors
    assert len(responses) == 2, responses
    parsed = []
    for status_code, raw in responses:
        import json

        payload = json.loads(raw)
        parsed.append((status_code, payload["job"]["id"], payload["ticket_count"]))
    statuses = sorted(status for status, _job, _count in parsed)
    assert statuses == [200, 201], parsed
    assert parsed[0][1] == parsed[1][1]
    job_id = uuid.UUID(parsed[0][1])
    expected_tickets = parsed[0][2]
    rows = _rows(
        "SELECT COUNT(*) AS n FROM evaluation_jobs WHERE idempotency_key = :key",
        {"key": "concurrency-same-key"},
    )
    assert rows[0]["n"] == 1
    tickets = _rows(
        "SELECT COUNT(*) AS n FROM evaluation_tickets WHERE job_id = :id",
        {"id": job_id},
    )
    assert tickets[0]["n"] == expected_tickets
    assert tickets[0]["n"] == 6


def _make_job(client, temp_root, count: int, name: str) -> uuid.UUID:
    dataset_id = f"mix-{name}-{uuid.uuid4().hex[:8]}"
    write_dataset(
        temp_root,
        dataset_id,
        [make_payload() for _ in range(count)],
        checks=[DETERMINISTIC_CHECKS[0]],
    )
    response = client.post(
        "/v1/evaluation-jobs", json={"dataset_id": dataset_id, "name": name, "max_attempts": 3}
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["job"]["id"])


def test_mixed_jobs_finish_under_workers_retries_and_one_crash(seeded_job, client, temp_root, monkeypatch):
    del seeded_job
    job_ids = [
        _make_job(client, temp_root, 5, "a"),
        _make_job(client, temp_root, 8, "b"),
        _make_job(client, temp_root, 6, "c"),
    ]
    by_job: dict[uuid.UUID, list[uuid.UUID]] = {}
    with transaction() as conn:
        tickets = TicketRepository(conn)
        for job_id in job_ids:
            by_job[job_id] = [item.id for item in tickets.list_by_job(job_id, limit=20)[1]]
    permanent = {ids[0] for ids in by_job.values()}
    once = {ids[1] for ids in by_job.values()}
    crash_id = by_job[job_ids[0]][2]
    seen: dict[uuid.UUID, int] = {}
    seen_lock = threading.Lock()

    def run(_values, _check, context):
        ticket_id = context.ticket_id
        with seen_lock:
            seen[ticket_id] = seen.get(ticket_id, 0) + 1
            attempt = seen[ticket_id]
        if ticket_id in permanent or (ticket_id in once and attempt == 1):
            raise TransientEvaluationError("scripted failure")
        return _passed_output()

    monkeypatch.setattr(
        "app.services.evaluation_service.get_evaluator", lambda _name: _scripted_evaluator(run)
    )

    with transaction() as conn:
        conn.execute(
            text(
                """
                UPDATE evaluation_tickets
                SET available_at = now() + interval '1 day'
                WHERE id <> :id AND status = 'READY'
                """
            ),
            {"id": crash_id},
        )
    with transaction() as conn:
        crashed = claim_tickets(conn, worker_id="w-crashed", limit=1, lease_seconds=1)
    assert len(crashed) == 1 and crashed[0].id == crash_id
    with transaction() as conn:
        conn.execute(
            text(
                """
                UPDATE evaluation_tickets
                SET available_at = now()
                WHERE id <> :id AND status = 'READY'
                """
            ),
            {"id": crash_id},
        )

    stop = threading.Event()
    errors: list[BaseException] = []

    def recover() -> None:
        while not stop.is_set():
            try:
                with transaction() as conn:
                    recover_abandoned_tickets(conn)
            except OperationalError as exc:
                if "deadlock" not in str(exc).lower():
                    errors.append(exc)
                    return
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
                return
            time.sleep(0.5)

    def worker(index: int) -> None:
        try:
            while not stop.is_set():
                with transaction() as conn:
                    batch = claim_tickets(
                        conn, worker_id=f"pool-{index}", limit=2, lease_seconds=30
                    )
                if not batch:
                    time.sleep(0.05)
                    continue
                for ticket in batch:
                    execute_ticket(ticket, worker_id=f"pool-{index}")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    recovery = threading.Thread(target=recover)
    recovery.start()
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(worker, index) for index in range(4)]
        deadline = time.monotonic() + 30
        finished = False
        while time.monotonic() < deadline:
            rows = _rows(
                """
                SELECT status FROM evaluation_tickets
                WHERE job_id IN (:a, :b, :c)
                """,
                {"a": job_ids[0], "b": job_ids[1], "c": job_ids[2]},
            )
            if rows and all(row["status"] in _TERMINAL for row in rows):
                finished = True
                break
            time.sleep(0.2)
        stop.set()
        for future in futures:
            future.result(timeout=15)
    recovery.join(timeout=5)
    assert not errors, errors
    assert finished, _rows(
        """
        SELECT job_id, status, worker_id, COUNT(*) AS n
        FROM evaluation_tickets
        WHERE job_id IN (:a, :b, :c)
        GROUP BY job_id, status, worker_id
        """,
        {"a": job_ids[0], "b": job_ids[1], "c": job_ids[2]},
    )

    for job_id in job_ids:
        with transaction() as conn:
            progress = JobRepository(conn).recompute_progress(job_id)
            stored = JobRepository(conn).get(job_id)
            ticket_rows = conn.execute(
                text(
                    """
                    SELECT status, worker_id FROM evaluation_tickets WHERE job_id = :id
                    """
                ),
                {"id": job_id},
            ).mappings().all()
        assert progress is not None and stored is not None
        done = sum(1 for row in ticket_rows if row["status"] == "DONE")
        failed = sum(1 for row in ticket_rows if row["status"] == "FAILED")
        cancelled = sum(1 for row in ticket_rows if row["status"] == "CANCELLED")
        not_applicable = sum(1 for row in ticket_rows if row["status"] == "NOT_APPLICABLE")
        assert done + failed + cancelled + not_applicable == stored.total_tickets, ticket_rows
        if failed == 0 and cancelled == 0:
            expected = JobStatus.COMPLETED
        elif done + not_applicable > 0 and failed > 0:
            expected = JobStatus.PARTIAL_FAILED
        elif failed > 0:
            expected = JobStatus.FAILED
        else:
            expected = JobStatus.CANCELLED
        assert stored.status is expected, (stored.status, ticket_rows)
        assert all(row["status"] != "RUNNING" for row in ticket_rows)
