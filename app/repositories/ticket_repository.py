from __future__ import annotations

import uuid
from typing import Any

from app.models.entities import EvaluationTicket
from app.models.enums import CheckType, TicketStatus, assert_ticket_transition
from app.repositories.base import BaseRepository

TICKET_COLUMNS = """
    id, job_id, payload_id, evaluation_config_id, check_id, check_type, evaluator,
    status, priority, attempt_count, max_attempts, available_at, worker_id,
    claimed_at, started_at, completed_at, lease_expires_at, result_id,
    input_snapshot_json, error_code, error_message, created_at, updated_at
"""


class TicketRepository(BaseRepository):
    """Durable evaluation work items.

    Claiming is atomic: rows are locked with FOR UPDATE SKIP LOCKED and flipped
    to RUNNING inside the caller's transaction, so two runners can never take the
    same ticket and nothing is marked RUNNING before COMMIT.
    """

    def _to_entity(self, row: dict[str, Any]) -> EvaluationTicket:
        return EvaluationTicket(
            id=row["id"],
            job_id=row["job_id"],
            payload_id=row["payload_id"],
            evaluation_config_id=row["evaluation_config_id"],
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

    # ------------------------------------------------------------------ create
    def bulk_insert(self, tickets: list[EvaluationTicket]) -> int:
        if not tickets:
            return 0
        with self.conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO evaluation_tickets (
                    id, job_id, payload_id, evaluation_config_id, check_id,
                    check_type, evaluator, status, priority, attempt_count,
                    max_attempts, available_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                """,
                [
                    (
                        t.id,
                        t.job_id,
                        t.payload_id,
                        t.evaluation_config_id,
                        t.check_id,
                        t.check_type.value,
                        t.evaluator,
                        t.status.value,
                        t.priority,
                        t.attempt_count,
                        t.max_attempts,
                    )
                    for t in tickets
                ],
            )
        return len(tickets)

    # ------------------------------------------------------------------- claim
    def claim(
        self,
        *,
        limit: int,
        worker_id: str,
        lease_seconds: int,
        job_id: uuid.UUID | None = None,
    ) -> list[EvaluationTicket]:
        """Atomically claim up to `limit` runnable tickets.

        Must be called inside a transaction. Jobs with a cancellation request are
        excluded so cancellation stops new work immediately.
        """
        rows = self.conn.execute(
            """
            SELECT t.id
            FROM evaluation_tickets t
            JOIN evaluation_jobs j ON j.id = t.job_id
            WHERE t.status IN ('READY', 'RETRY')
              AND t.available_at <= now()
              AND j.cancellation_requested_at IS NULL
              AND j.status IN ('CREATED', 'READY', 'RUNNING')
              AND (%s::uuid IS NULL OR t.job_id = %s::uuid)
            ORDER BY t.priority DESC, t.created_at
            FOR UPDATE OF t SKIP LOCKED
            LIMIT %s
            """,  # noqa: S608 - no interpolated values, %s are bound parameters
            (job_id, job_id, limit),
        ).fetchall()
        if not rows:
            return []

        ids = [r["id"] for r in rows]
        updated = self.conn.execute(
            f"""
            UPDATE evaluation_tickets
            SET status = 'RUNNING',
                worker_id = %s,
                claimed_at = now(),
                started_at = now(),
                lease_expires_at = now() + (%s * INTERVAL '1 second'),
                attempt_count = attempt_count + 1,
                error_code = NULL,
                error_message = NULL,
                updated_at = now()
            WHERE id = ANY(%s)
            RETURNING {TICKET_COLUMNS}
            """,
            (worker_id, lease_seconds, ids),
        ).fetchall()
        return [self._to_entity(r) for r in updated]

    # -------------------------------------------------------------- transitions
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
    ) -> EvaluationTicket:
        """Validate and persist a single ticket state transition.

        The row is locked first so the legality check runs against committed
        state rather than a stale in-memory copy.
        """
        current = self.conn.execute(
            "SELECT status FROM evaluation_tickets WHERE id = %s FOR UPDATE",
            (ticket_id,),
        ).fetchone()
        if current is None:
            raise LookupError(f"ticket {ticket_id} not found")
        current_status = self._enum(TicketStatus, current["status"])
        assert_ticket_transition(current_status, target)

        message = error_message[:4000] if error_message else None

        if target is TicketStatus.DONE:
            sql = f"""
                UPDATE evaluation_tickets
                SET status = 'DONE', result_id = %s, completed_at = now(),
                    lease_expires_at = NULL, error_code = NULL, error_message = NULL,
                    input_snapshot_json = COALESCE(%s, input_snapshot_json),
                    updated_at = now()
                WHERE id = %s
                RETURNING {TICKET_COLUMNS}
            """
            params: tuple[Any, ...] = (result_id, self._json(input_snapshot), ticket_id)
        elif target is TicketStatus.RETRY:
            sql = f"""
                UPDATE evaluation_tickets
                SET status = 'RETRY',
                    available_at = now() + (%s * INTERVAL '1 second'),
                    worker_id = NULL, lease_expires_at = NULL,
                    error_code = %s, error_message = %s,
                    updated_at = now()
                WHERE id = %s
                RETURNING {TICKET_COLUMNS}
            """
            params = (float(delay_seconds or 0.0), error_code, message, ticket_id)
        elif target in (TicketStatus.FAILED, TicketStatus.NOT_APPLICABLE):
            sql = f"""
                UPDATE evaluation_tickets
                SET status = %s, completed_at = now(), lease_expires_at = NULL,
                    result_id = COALESCE(%s, result_id),
                    error_code = %s, error_message = %s,
                    input_snapshot_json = COALESCE(%s, input_snapshot_json),
                    updated_at = now()
                WHERE id = %s
                RETURNING {TICKET_COLUMNS}
            """
            params = (
                target.value,
                result_id,
                error_code,
                message,
                self._json(input_snapshot),
                ticket_id,
            )
        elif target is TicketStatus.CANCELLED:
            sql = f"""
                UPDATE evaluation_tickets
                SET status = 'CANCELLED', completed_at = now(), lease_expires_at = NULL,
                    error_code = COALESCE(%s, 'CANCELLED'), error_message = %s,
                    updated_at = now()
                WHERE id = %s
                RETURNING {TICKET_COLUMNS}
            """
            params = (error_code, message, ticket_id)
        elif target is TicketStatus.READY:
            sql = f"""
                UPDATE evaluation_tickets
                SET status = 'READY', attempt_count = 0, available_at = now(),
                    worker_id = NULL, claimed_at = NULL, started_at = NULL,
                    completed_at = NULL, lease_expires_at = NULL, result_id = NULL,
                    error_code = NULL, error_message = NULL, updated_at = now()
                WHERE id = %s
                RETURNING {TICKET_COLUMNS}
            """
            params = (ticket_id,)
        else:  # RUNNING is reserved for claim()
            raise ValueError(f"transition to {target.value} is not supported here")

        row = self.conn.execute(sql, params).fetchone()
        return self._to_entity(row)

    def cancel_unclaimed(self, job_id: uuid.UUID) -> int:
        """Cancel every ticket that has not been claimed yet."""
        rows = self.conn.execute(
            """
            UPDATE evaluation_tickets
            SET status = 'CANCELLED',
                error_code = 'CANCELLED',
                error_message = 'job cancellation requested',
                completed_at = now(),
                lease_expires_at = NULL,
                updated_at = now()
            WHERE job_id = %s AND status IN ('READY', 'RETRY')
            RETURNING id
            """,
            (job_id,),
        ).fetchall()
        return len(rows)

    def reset_failed(self, job_id: uuid.UUID) -> list[uuid.UUID]:
        """Operator retry: FAILED -> READY, clearing attempts and errors."""
        rows = self.conn.execute(
            """
            UPDATE evaluation_tickets
            SET status = 'READY', attempt_count = 0, available_at = now(),
                worker_id = NULL, claimed_at = NULL, started_at = NULL,
                completed_at = NULL, lease_expires_at = NULL, result_id = NULL,
                error_code = NULL, error_message = NULL, updated_at = now()
            WHERE job_id = %s AND status = 'FAILED'
            RETURNING id
            """,
            (job_id,),
        ).fetchall()
        return [r["id"] for r in rows]

    def recover_expired_leases(self, *, limit: int = 100) -> list[EvaluationTicket]:
        """Reclaim RUNNING tickets whose lease expired (runner crash/restart).

        Tickets on a cancelled job become CANCELLED. Otherwise tickets with
        attempts left go back to RETRY, and the rest FAIL.

        Locks the parent job first (FOR UPDATE OF j, t) so this sweep cannot
        deadlock with ticket settlement, which also locks job-then-ticket.
        """
        rows = self.conn.execute(
            """
            WITH locked AS (
                SELECT t2.id
                FROM evaluation_jobs j2
                JOIN evaluation_tickets t2 ON t2.job_id = j2.id
                WHERE t2.status = 'RUNNING' AND t2.lease_expires_at < now()
                ORDER BY t2.lease_expires_at
                FOR UPDATE OF j2, t2 SKIP LOCKED
                LIMIT %s
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
                available_at = now(),
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
            (limit,),
        ).fetchall()
        return [self._to_entity(r) for r in rows]

    def cancel_running_for_cancelled_jobs(self, *, limit: int = 100) -> list[EvaluationTicket]:
        """Sweep tickets left RUNNING on jobs that were cancelled."""
        rows = self.conn.execute(
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
                LIMIT %s
            )
            RETURNING t.*
            """,
            (limit,),
        ).fetchall()
        return [self._to_entity(r) for r in rows]

    # -------------------------------------------------------------------- read
    def get(self, ticket_id: uuid.UUID) -> EvaluationTicket | None:
        row = self.conn.execute(
            f"SELECT {TICKET_COLUMNS} FROM evaluation_tickets WHERE id = %s",
            (ticket_id,),
        ).fetchone()
        return self._to_entity(row) if row else None

    def counts_by_status(self, job_id: uuid.UUID) -> dict[str, int]:
        rows = self.conn.execute(
            """
            SELECT status, COUNT(*) AS n FROM evaluation_tickets
            WHERE job_id = %s GROUP BY status
            """,
            (job_id,),
        ).fetchall()
        return {r["status"]: int(r["n"]) for r in rows}

    def global_counts_by_status(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM evaluation_tickets GROUP BY status"
        ).fetchall()
        return {r["status"]: int(r["n"]) for r in rows}

    def list_by_job(
        self,
        job_id: uuid.UUID,
        *,
        status: TicketStatus | None = None,
        check_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[int, list[EvaluationTicket]]:
        clauses = ["job_id = %s"]
        params: list[Any] = [job_id]
        if status is not None:
            clauses.append("status = %s")
            params.append(status.value)
        if check_id is not None:
            clauses.append("check_id = %s")
            params.append(check_id)
        where = " AND ".join(clauses)
        total = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM evaluation_tickets WHERE {where}",  # noqa: S608
            params,
        ).fetchone()["n"]
        rows = self.conn.execute(
            f"""
            SELECT {TICKET_COLUMNS} FROM evaluation_tickets WHERE {where}
            ORDER BY priority DESC, created_at, check_id
            OFFSET %s LIMIT %s
            """,  # noqa: S608
            [*params, offset, limit],
        ).fetchall()
        return int(total), [self._to_entity(r) for r in rows]

    def jobs_with_active_tickets(self) -> list[uuid.UUID]:
        rows = self.conn.execute(
            """
            SELECT DISTINCT job_id FROM evaluation_tickets
            WHERE status IN ('READY', 'RUNNING', 'RETRY')
            """
        ).fetchall()
        return [r["job_id"] for r in rows]
