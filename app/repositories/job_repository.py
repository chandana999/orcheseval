from __future__ import annotations

import uuid
from typing import Any

from app.models.entities import EvaluationJob, JobProgress
from app.models.enums import JobStatus, TicketStatus
from app.repositories.base import BaseRepository


class JobRepository(BaseRepository):
    def _to_entity(self, row: dict[str, Any]) -> EvaluationJob:
        return EvaluationJob(
            id=row["id"],
            name=row["name"],
            dataset_id=row["dataset_id"],
            evaluation_config_id=row["evaluation_config_id"],
            config_snapshot_json=row["config_snapshot_json"],
            status=self._enum(JobStatus, row["status"]),
            payload_count=row["payload_count"],
            total_tickets=row["total_tickets"],
            completed_tickets=row["completed_tickets"],
            failed_tickets=row["failed_tickets"],
            cancelled_tickets=row["cancelled_tickets"],
            not_applicable_tickets=row["not_applicable_tickets"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            cancellation_requested_at=row["cancellation_requested_at"],
            updated_at=row.get("updated_at"),
            error_message=row.get("error_message"),
        )

    def insert(self, job: EvaluationJob) -> EvaluationJob:
        row = self.conn.execute(
            """
            INSERT INTO evaluation_jobs (
                id, name, dataset_id, evaluation_config_id, config_snapshot_json,
                status, payload_count, total_tickets
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                job.id,
                job.name,
                job.dataset_id,
                job.evaluation_config_id,
                self._json(job.config_snapshot_json),
                job.status.value,
                job.payload_count,
                job.total_tickets,
            ),
        ).fetchone()
        return self._to_entity(row)

    def get(self, job_id: uuid.UUID) -> EvaluationJob | None:
        row = self.conn.execute(
            "SELECT * FROM evaluation_jobs WHERE id = %s", (job_id,)
        ).fetchone()
        return self._to_entity(row) if row else None

    def get_for_update(self, job_id: uuid.UUID) -> EvaluationJob | None:
        """Lock the job row so concurrent runners serialize progress updates."""
        row = self.conn.execute(
            "SELECT * FROM evaluation_jobs WHERE id = %s FOR UPDATE", (job_id,)
        ).fetchone()
        return self._to_entity(row) if row else None

    def get_cancellation_state(self, job_id: uuid.UUID) -> tuple[JobStatus, bool] | None:
        """Cheap check used by the runner before persisting a result."""
        row = self.conn.execute(
            """
            SELECT status, cancellation_requested_at
            FROM evaluation_jobs WHERE id = %s
            """,
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        return self._enum(JobStatus, row["status"]), row["cancellation_requested_at"] is not None

    def list(
        self,
        *,
        status: JobStatus | None = None,
        dataset_id: uuid.UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[int, list[EvaluationJob]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = %s")
            params.append(status.value)
        if dataset_id is not None:
            clauses.append("dataset_id = %s")
            params.append(dataset_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        total = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM evaluation_jobs {where}",  # noqa: S608
            params,
        ).fetchone()["n"]
        rows = self.conn.execute(
            f"""
            SELECT * FROM evaluation_jobs {where}
            ORDER BY created_at DESC OFFSET %s LIMIT %s
            """,  # noqa: S608
            [*params, offset, limit],
        ).fetchall()
        return int(total), [self._to_entity(r) for r in rows]

    def set_total_tickets(self, job_id: uuid.UUID, *, total: int, status: JobStatus) -> None:
        self.conn.execute(
            """
            UPDATE evaluation_jobs
            SET total_tickets = %s, status = %s, updated_at = now()
            WHERE id = %s
            """,
            (total, status.value, job_id),
        )

    def mark_running_if_pending(self, job_ids: list[uuid.UUID]) -> int:
        """Flip jobs to RUNNING when their first ticket is claimed.

        Uses SKIP LOCKED so a runner claiming tickets never waits on another
        runner that is flipping the same job.
        """
        if not job_ids:
            return 0
        locked = self.conn.execute(
            """
            SELECT id FROM evaluation_jobs
            WHERE id = ANY(%s) AND status IN ('CREATED', 'READY')
            FOR UPDATE SKIP LOCKED
            """,
            (job_ids,),
        ).fetchall()
        if not locked:
            return 0
        ids = [row["id"] for row in locked]
        self.conn.execute(
            """
            UPDATE evaluation_jobs
            SET status = 'RUNNING',
                started_at = COALESCE(started_at, now()),
                updated_at = now()
            WHERE id = ANY(%s)
            """,
            (ids,),
        )
        return len(ids)

    def request_cancellation(self, job_id: uuid.UUID) -> bool:
        """Flag the job so no further tickets can be claimed. Idempotent."""
        row = self.conn.execute(
            """
            UPDATE evaluation_jobs
            SET cancellation_requested_at = COALESCE(cancellation_requested_at, now()),
                updated_at = now()
            WHERE id = %s
            RETURNING cancellation_requested_at
            """,
            (job_id,),
        ).fetchone()
        return row is not None

    def fail(self, job_id: uuid.UUID, message: str) -> None:
        self.conn.execute(
            """
            UPDATE evaluation_jobs
            SET status = 'FAILED',
                error_message = %s,
                completed_at = COALESCE(completed_at, now()),
                updated_at = now()
            WHERE id = %s
            """,
            (message[:4000], job_id),
        )

    def ticket_counts(self, job_id: uuid.UUID) -> dict[str, int]:
        rows = self.conn.execute(
            """
            SELECT status, COUNT(*) AS n
            FROM evaluation_tickets WHERE job_id = %s GROUP BY status
            """,
            (job_id,),
        ).fetchall()
        return {row["status"]: int(row["n"]) for row in rows}

    def recompute_progress(self, job_id: uuid.UUID, *, lock: bool = True) -> JobProgress | None:
        """Derive job counters and status from durable ticket rows.

        This is the only place job progress is written: counters are never
        incremented from runner memory, so a crashed runner cannot corrupt them.

        Callers that already hold FOR UPDATE on the job row must pass lock=False
        so lock order stays job-then-ticket (avoids FK deadlocks).
        """
        job = self.get_for_update(job_id) if lock else self.get(job_id)
        if job is None:
            return None

        counts = self.ticket_counts(job_id)
        done = counts.get(TicketStatus.DONE.value, 0)
        failed = counts.get(TicketStatus.FAILED.value, 0)
        cancelled = counts.get(TicketStatus.CANCELLED.value, 0)
        not_applicable = counts.get(TicketStatus.NOT_APPLICABLE.value, 0)
        running = counts.get(TicketStatus.RUNNING.value, 0)
        ready = counts.get(TicketStatus.READY.value, 0)
        retry = counts.get(TicketStatus.RETRY.value, 0)

        total = sum(counts.values())
        terminal = done + failed + cancelled + not_applicable
        pending = ready + retry
        cancellation_requested = job.cancellation_requested_at is not None

        if total == 0:
            status = job.status
        elif terminal < total:
            status = JobStatus.RUNNING if (running or terminal) else JobStatus.READY
        elif cancelled > 0 and (cancellation_requested or done + failed == 0):
            status = JobStatus.CANCELLED
        elif failed == 0:
            status = JobStatus.COMPLETED
        elif done + not_applicable > 0:
            status = JobStatus.PARTIAL_FAILED
        else:
            status = JobStatus.FAILED

        is_terminal = total > 0 and terminal >= total
        started = status != JobStatus.READY and status != JobStatus.CREATED

        row = self.conn.execute(
            """
            UPDATE evaluation_jobs
            SET status = %s,
                total_tickets = GREATEST(total_tickets, %s),
                completed_tickets = %s,
                failed_tickets = %s,
                cancelled_tickets = %s,
                not_applicable_tickets = %s,
                started_at = CASE WHEN %s THEN COALESCE(started_at, now()) ELSE started_at END,
                completed_at = CASE WHEN %s THEN COALESCE(completed_at, now()) ELSE NULL END,
                updated_at = now()
            WHERE id = %s
            RETURNING *
            """,
            (
                status.value,
                total,
                done,
                failed,
                cancelled,
                not_applicable,
                started,
                is_terminal,
                job_id,
            ),
        ).fetchone()

        return JobProgress(
            job_id=job_id,
            status=self._enum(JobStatus, row["status"]),
            total_tickets=row["total_tickets"],
            completed_tickets=done,
            failed_tickets=failed,
            cancelled_tickets=cancelled,
            not_applicable_tickets=not_applicable,
            running_tickets=running,
            pending_tickets=pending,
            counts_by_status=counts,
        )
