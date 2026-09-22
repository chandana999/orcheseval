from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text

from app.models.entities import EvaluationTicket
from app.models.enums import (
    ALLOWED_TICKET_TRANSITIONS,
    CheckType,
    TicketStatus,
    assert_ticket_transition,
)
from app.repositories.base import BaseRepository, _json


class StaleWorkerError(RuntimeError):
    """The worker no longer owns this RUNNING ticket."""

    def __init__(self, ticket_id: uuid.UUID) -> None:
        super().__init__(f"stale worker cannot settle ticket {ticket_id}")
        self.ticket_id = ticket_id

TICKET_COLUMNS = """
    id, job_id, payload_id, source_dataset_id, source_payload_ref, source_payload_id,
    metric_record_id, metric_id, metric_version_number, evaluation_profile_id,
    metric_snapshot_json, check_id, check_type, evaluator,
    status, priority, attempt_count, max_attempts, available_at, worker_id,
    claimed_at, started_at, completed_at, lease_expires_at, result_id,
    input_snapshot_json, error_code, error_message, created_at, updated_at
"""


class TicketRepository(BaseRepository):
    """Durable evaluation work items. Claim uses FOR UPDATE SKIP LOCKED."""

    def _to_entity(self, row: dict[str, Any]) -> EvaluationTicket:
        return EvaluationTicket(
            id=row["id"],
            job_id=row["job_id"],
            payload_id=row["payload_id"],
            source_dataset_id=row["source_dataset_id"],
            source_payload_ref=row["source_payload_ref"],
            source_payload_id=row["source_payload_id"],
            metric_record_id=row["metric_record_id"],
            metric_id=row["metric_id"],
            metric_version_number=row["metric_version_number"],
            evaluation_profile_id=row["evaluation_profile_id"],
            metric_snapshot_json=row["metric_snapshot_json"] or {},
            check_id=row["check_id"],
            check_type=self._enum(CheckType, row["check_type"]),
            evaluator=row["evaluator"],
            status=self._enum(TicketStatus, row["status"]),
            priority=row["priority"],
            attempt_count=row["attempt_count"],
            max_attempts=row["max_attempts"],
            available_at=row["available_at"],
            worker_id=row["worker_id"],
            claimed_at=row["claimed_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            lease_expires_at=row["lease_expires_at"],
            result_id=row["result_id"],
            input_snapshot_json=row["input_snapshot_json"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )

    def bulk_insert(self, tickets: list[EvaluationTicket]) -> int:
        if not tickets:
            return 0
        self.session.execute(
            text(
                """
                INSERT INTO evaluation_tickets (
                    id, job_id, payload_id, source_dataset_id, source_payload_ref,
                    source_payload_id, metric_record_id, metric_id,
                    metric_version_number, evaluation_profile_id, metric_snapshot_json,
                    check_id, check_type, evaluator, status, priority, attempt_count,
                    max_attempts, available_at
                )
                VALUES (
                    :id, :job_id, :payload_id, :source_dataset_id, :source_payload_ref,
                    :source_payload_id, :metric_record_id, :metric_id,
                    :metric_version_number, :evaluation_profile_id,
                    CAST(:metric_snapshot_json AS jsonb),
                    :check_id, :check_type, :evaluator, :status, :priority,
                    :attempt_count, :max_attempts, now()
                )
                """
            ),
            [
                {
                    "id": ticket.id,
                    "job_id": ticket.job_id,
                    "payload_id": ticket.payload_id,
                    "source_dataset_id": ticket.source_dataset_id,
                    "source_payload_ref": ticket.source_payload_ref,
                    "source_payload_id": ticket.source_payload_id,
                    "metric_record_id": ticket.metric_record_id,
                    "metric_id": ticket.metric_id,
                    "metric_version_number": ticket.metric_version_number,
                    "evaluation_profile_id": ticket.evaluation_profile_id,
                    "metric_snapshot_json": _json(ticket.metric_snapshot_json),
                    "check_id": ticket.check_id,
                    "check_type": ticket.check_type.value,
                    "evaluator": ticket.evaluator,
                    "status": ticket.status.value,
                    "priority": ticket.priority,
                    "attempt_count": ticket.attempt_count,
                    "max_attempts": ticket.max_attempts,
                }
                for ticket in tickets
            ],
        )
        return len(tickets)

    def claim(
        self,
        *,
        limit: int,
        worker_id: str,
        lease_seconds: int,
        job_id: uuid.UUID | None = None,
    ) -> list[EvaluationTicket]:
        """Claim READY or RETRY tickets whose available_at has passed.

        Uses FOR UPDATE SKIP LOCKED so two workers cannot take the same row.
        Each claimed ticket becomes RUNNING, records worker_id and a lease, and
        increments attempt_count. Returns the claimed rows, which may be empty.
        """
        rows = self._all(
            """
            SELECT t.id
            FROM evaluation_tickets t
            JOIN evaluation_jobs j ON j.id = t.job_id
            WHERE t.status IN ('READY', 'RETRY')
              AND t.available_at <= now()
              AND j.cancellation_requested_at IS NULL
              AND j.status IN ('CREATED', 'READY', 'RUNNING')
              AND (CAST(:job_id AS uuid) IS NULL OR t.job_id = CAST(:job_id AS uuid))
            ORDER BY t.priority DESC, t.created_at
            FOR UPDATE OF t SKIP LOCKED
            LIMIT :limit
            """,
            {"job_id": job_id, "limit": limit},
        )
        if not rows:
            return []
        updated = self._all_in(
            f"""
            UPDATE evaluation_tickets
            SET status = 'RUNNING',
                worker_id = :worker_id,
                claimed_at = now(),
                started_at = now(),
                lease_expires_at = now() + (:lease_seconds * INTERVAL '1 second'),
                attempt_count = attempt_count + 1,
                error_code = NULL,
                error_message = NULL,
                updated_at = now()
            WHERE id IN :ids
            RETURNING {TICKET_COLUMNS}
            """,
            key="ids",
            values=[row["id"] for row in rows],
            extra={"worker_id": worker_id, "lease_seconds": lease_seconds},
        )
        return [self._to_entity(row) for row in updated]

    def transition(
        self,
        ticket_id: uuid.UUID,
        target: TicketStatus,
        *,
        result_id: uuid.UUID | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        delay_seconds: float | None = None,
        input_snapshot: dict[str, Any] | None = None,
        expected_worker_id: str | None = None,
    ) -> EvaluationTicket:
        """Settle one ticket inside the caller's transaction.

        worker_id is required. The row is locked, then updated only when it is
        still RUNNING and owned by that worker. A different worker on the row
        is a stale-worker race and raises StaleWorkerError before transition
        legality is checked. The same worker attempting an illegal transition
        still raises IllegalTicketTransition.
        """
        current = self._one(
            "SELECT status, worker_id FROM evaluation_tickets WHERE id = :id FOR UPDATE",
            {"id": ticket_id},
        )
        if current is None:
            raise LookupError(f"ticket {ticket_id} not found")
        current_status = self._enum(TicketStatus, current["status"])
        if not expected_worker_id:
            raise ValueError("worker_id is required to settle a ticket")
        owned = (
            current_status is TicketStatus.RUNNING
            and current["worker_id"] == expected_worker_id
        )
        if not owned:
            current_worker_id = current["worker_id"]
            different_owner = (
                current_worker_id is not None and current_worker_id != expected_worker_id
            )
            if not different_owner and target not in ALLOWED_TICKET_TRANSITIONS.get(
                current_status, frozenset()
            ):
                assert_ticket_transition(current_status, target)
            raise StaleWorkerError(ticket_id)
        assert_ticket_transition(current_status, target)
        message = error_message[:4000] if error_message else None
        params: dict[str, Any] = {
            "id": ticket_id,
            "result_id": result_id,
            "error_code": error_code,
            "error_message": message,
            "delay_seconds": float(delay_seconds or 0.0),
            "input_snapshot": _json(input_snapshot),
            "status": target.value,
            "expected_worker_id": expected_worker_id,
        }
        where = "WHERE id = :id AND status = 'RUNNING' AND worker_id = :expected_worker_id"
        if target is TicketStatus.DONE:
            sql = f"""
                UPDATE evaluation_tickets
                SET status = 'DONE', result_id = :result_id, completed_at = now(),
                    lease_expires_at = NULL, error_code = NULL, error_message = NULL,
                    input_snapshot_json = COALESCE(CAST(:input_snapshot AS jsonb), input_snapshot_json),
                    updated_at = now()
                {where}
                RETURNING {TICKET_COLUMNS}
            """
        elif target is TicketStatus.RETRY:
            sql = f"""
                UPDATE evaluation_tickets
                SET status = 'RETRY',
                    available_at = now() + (:delay_seconds * INTERVAL '1 second'),
                    worker_id = NULL, lease_expires_at = NULL,
                    error_code = :error_code, error_message = :error_message,
                    updated_at = now()
                {where}
                RETURNING {TICKET_COLUMNS}
            """
        elif target in (TicketStatus.FAILED, TicketStatus.NOT_APPLICABLE):
            sql = f"""
                UPDATE evaluation_tickets
                SET status = :status, completed_at = now(), lease_expires_at = NULL,
                    result_id = COALESCE(:result_id, result_id),
                    error_code = :error_code, error_message = :error_message,
                    input_snapshot_json = COALESCE(CAST(:input_snapshot AS jsonb), input_snapshot_json),
                    updated_at = now()
                {where}
                RETURNING {TICKET_COLUMNS}
            """
        elif target is TicketStatus.CANCELLED:
            sql = f"""
                UPDATE evaluation_tickets
                SET status = 'CANCELLED', completed_at = now(), lease_expires_at = NULL,
                    error_code = COALESCE(:error_code, 'CANCELLED'),
                    error_message = :error_message,
                    updated_at = now()
                {where}
                RETURNING {TICKET_COLUMNS}
            """
        elif target is TicketStatus.READY:
            sql = f"""
                UPDATE evaluation_tickets
                SET status = 'READY', attempt_count = 0, available_at = now(),
                    worker_id = NULL, claimed_at = NULL, started_at = NULL,
                    completed_at = NULL, lease_expires_at = NULL, result_id = NULL,
                    error_code = NULL, error_message = NULL, updated_at = now()
                {where}
                RETURNING {TICKET_COLUMNS}
            """
        else:
            raise ValueError(f"transition to {target.value} is not supported here")
        row = self._one(sql, params)
        if row is None:
            raise StaleWorkerError(ticket_id)
        return self._to_entity(row)

    def extend_lease(
        self,
        ticket_id: uuid.UUID,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> EvaluationTicket | None:
        """Extend a RUNNING lease only for the worker that currently owns it."""
        if not worker_id:
            raise ValueError("worker_id is required to extend a lease")
        row = self._one(
            f"""
            UPDATE evaluation_tickets
            SET lease_expires_at = now() + (:lease_seconds * INTERVAL '1 second'),
                updated_at = now()
            WHERE id = :id
              AND status = 'RUNNING'
              AND worker_id = :worker_id
            RETURNING {TICKET_COLUMNS}
            """,
            {"id": ticket_id, "worker_id": worker_id, "lease_seconds": lease_seconds},
        )
        return self._to_entity(row) if row else None

    def cancel_unclaimed(self, job_id: uuid.UUID) -> int:
        rows = self._all(
            """
            UPDATE evaluation_tickets
            SET status = 'CANCELLED',
                error_code = 'CANCELLED',
                error_message = 'job cancellation requested',
                completed_at = now(),
                lease_expires_at = NULL,
                updated_at = now()
            WHERE job_id = :job_id AND status IN ('READY', 'RETRY')
            RETURNING id
            """,
            {"job_id": job_id},
        )
        return len(rows)

    def reset_failed(self, job_id: uuid.UUID) -> list[uuid.UUID]:
        rows = self._all(
            """
            UPDATE evaluation_tickets
            SET status = 'READY', attempt_count = 0, available_at = now(),
                worker_id = NULL, claimed_at = NULL, started_at = NULL,
                completed_at = NULL, lease_expires_at = NULL, result_id = NULL,
                error_code = NULL, error_message = NULL, updated_at = now()
            WHERE job_id = :job_id AND status = 'FAILED'
            RETURNING id
            """,
            {"job_id": job_id},
        )
        return [row["id"] for row in rows]

    def recover_expired_leases(self, *, limit: int = 100) -> list[EvaluationTicket]:
        """Reclaim RUNNING tickets whose lease has expired.

        Locks candidate rows with FOR UPDATE SKIP LOCKED. A cancelled job becomes
        CANCELLED, an exhausted attempt count becomes FAILED, and every other
        ticket returns to RETRY with worker_id cleared. Retry availability is
        spread by up to one second so recovered tickets are not claimed together.
        """
        rows = self._all(
            """
            WITH locked AS (
                SELECT t2.id
                FROM evaluation_jobs j2
                JOIN evaluation_tickets t2 ON t2.job_id = j2.id
                WHERE t2.status = 'RUNNING' AND t2.lease_expires_at < now()
                ORDER BY t2.lease_expires_at
                FOR UPDATE OF j2, t2 SKIP LOCKED
                LIMIT :limit
            )
            UPDATE evaluation_tickets t
            SET status = CASE
                    WHEN j.cancellation_requested_at IS NOT NULL
                        THEN 'CANCELLED'::evaluation_ticket_status
                    WHEN t.attempt_count >= t.max_attempts
                        THEN 'FAILED'::evaluation_ticket_status
                    ELSE 'RETRY'::evaluation_ticket_status
                END,
                error_code = CASE
                    WHEN j.cancellation_requested_at IS NOT NULL THEN 'CANCELLED'
                    ELSE 'LEASE_EXPIRED'
                END,
                error_message = CASE
                    WHEN j.cancellation_requested_at IS NOT NULL
                        THEN 'job cancelled while ticket was running'
                    ELSE 'runner lease expired; ticket recovered'
                END,
                available_at = CASE
                    WHEN j.cancellation_requested_at IS NULL
                         AND t.attempt_count < t.max_attempts
                        THEN now() + (random() * INTERVAL '1 second')
                    ELSE now()
                END,
                worker_id = NULL,
                lease_expires_at = NULL,
                completed_at = CASE
                    WHEN j.cancellation_requested_at IS NOT NULL
                         OR t.attempt_count >= t.max_attempts
                        THEN now()
                    ELSE t.completed_at
                END,
                updated_at = now()
            FROM evaluation_jobs j, locked
            WHERE t.job_id = j.id AND t.id = locked.id
            RETURNING t.*
            """,
            {"limit": limit},
        )
        return [self._to_entity(row) for row in rows]

    def cancel_running_for_cancelled_jobs(self, *, limit: int = 100) -> list[EvaluationTicket]:
        rows = self._all(
            """
            UPDATE evaluation_tickets t
            SET status = 'CANCELLED',
                error_code = 'CANCELLED',
                error_message = 'job cancelled while ticket was running',
                completed_at = now(),
                lease_expires_at = NULL,
                updated_at = now()
            WHERE t.id IN (
                SELECT t2.id
                FROM evaluation_jobs j
                JOIN evaluation_tickets t2 ON t2.job_id = j.id
                WHERE t2.status = 'RUNNING'
                  AND j.cancellation_requested_at IS NOT NULL
                  AND t2.lease_expires_at < now()
                ORDER BY t2.lease_expires_at
                FOR UPDATE OF j, t2 SKIP LOCKED
                LIMIT :limit
            )
            RETURNING t.*
            """,
            {"limit": limit},
        )
        return [self._to_entity(row) for row in rows]

    def get(self, ticket_id: uuid.UUID) -> EvaluationTicket | None:
        row = self._one(
            f"SELECT {TICKET_COLUMNS} FROM evaluation_tickets WHERE id = :id",
            {"id": ticket_id},
        )
        return self._to_entity(row) if row else None

    def counts_by_status(self, job_id: uuid.UUID) -> dict[str, int]:
        rows = self._all(
            """
            SELECT status, COUNT(*) AS n FROM evaluation_tickets
            WHERE job_id = :job_id GROUP BY status
            """,
            {"job_id": job_id},
        )
        return {row["status"]: int(row["n"]) for row in rows}

    def global_counts_by_status(self) -> dict[str, int]:
        rows = self._all("SELECT status, COUNT(*) AS n FROM evaluation_tickets GROUP BY status")
        return {row["status"]: int(row["n"]) for row in rows}

    def list_by_job(
        self,
        job_id: uuid.UUID,
        *,
        status: TicketStatus | None = None,
        check_id: str | None = None,
        metric_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[int, list[EvaluationTicket]]:
        clauses = ["job_id = :job_id"]
        params: dict[str, Any] = {"job_id": job_id, "limit": limit, "offset": offset}
        if status is not None:
            clauses.append("status = :status")
            params["status"] = status.value
        if check_id is not None:
            clauses.append("check_id = :check_id")
            params["check_id"] = check_id
        if metric_id is not None:
            clauses.append("metric_id = :metric_id")
            params["metric_id"] = metric_id
        where = " AND ".join(clauses)
        filters = {key: value for key, value in params.items() if key not in {"limit", "offset"}}
        total = self._one(
            f"SELECT COUNT(*) AS n FROM evaluation_tickets WHERE {where}", filters
        )["n"]
        rows = self._all(
            f"""
            SELECT {TICKET_COLUMNS} FROM evaluation_tickets WHERE {where}
            ORDER BY priority DESC, created_at, check_id
            OFFSET :offset LIMIT :limit
            """,
            params,
        )
        return int(total), [self._to_entity(row) for row in rows]

    def jobs_with_active_tickets(self) -> list[uuid.UUID]:
        rows = self._all(
            """
            SELECT DISTINCT job_id FROM evaluation_tickets
            WHERE status IN ('READY', 'RUNNING', 'RETRY')
            """
        )
        return [row["job_id"] for row in rows]
