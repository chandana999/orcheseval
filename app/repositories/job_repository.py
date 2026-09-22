from __future__ import annotations

import uuid
from typing import Any

from app.models.entities import EvaluationJob, JobProgress
from app.models.enums import JobStatus, TicketStatus
from app.repositories.base import BaseRepository, _json


class JobRepository(BaseRepository):
    def _to_entity(self, row: dict[str, Any]) -> EvaluationJob:
        return EvaluationJob(
            id=row["id"],
            name=row["name"],
            dataset_id=row["dataset_id"],
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

    def insert(
        self,
        job: EvaluationJob,
        *,
        idempotency_key: str | None = None,
        idempotency_request_hash: str | None = None,
    ) -> EvaluationJob:
        row = self._one(
            """
            INSERT INTO evaluation_jobs (
                id, name, dataset_id, config_snapshot_json,
                status, payload_count, total_tickets,
                idempotency_key, idempotency_request_hash
            )
            VALUES (
                :id, :name, :dataset_id, CAST(:config_snapshot_json AS jsonb),
                :status, :payload_count, :total_tickets,
                :idempotency_key, :idempotency_request_hash
            )
            RETURNING *
            """,
            {
                "id": job.id,
                "name": job.name,
                "dataset_id": job.dataset_id,
                "config_snapshot_json": _json(job.config_snapshot_json),
                "status": job.status.value,
                "payload_count": job.payload_count,
                "total_tickets": job.total_tickets,
                "idempotency_key": idempotency_key,
                "idempotency_request_hash": idempotency_request_hash,
            },
        )
        return self._to_entity(row)

    def get_by_idempotency_key(self, key: str) -> tuple[EvaluationJob, str | None] | None:
        row = self._one(
            "SELECT * FROM evaluation_jobs WHERE idempotency_key = :key",
            {"key": key},
        )
        if row is None:
            return None
        return self._to_entity(row), row.get("idempotency_request_hash")

    def get(self, job_id: uuid.UUID) -> EvaluationJob | None:
        row = self._one("SELECT * FROM evaluation_jobs WHERE id = :id", {"id": job_id})
        return self._to_entity(row) if row else None

    def get_for_update(self, job_id: uuid.UUID) -> EvaluationJob | None:
        row = self._one(
            "SELECT * FROM evaluation_jobs WHERE id = :id FOR UPDATE", {"id": job_id}
        )
        return self._to_entity(row) if row else None

    def get_cancellation_state(self, job_id: uuid.UUID) -> tuple[JobStatus, bool] | None:
        row = self._one(
            """
            SELECT status, cancellation_requested_at
            FROM evaluation_jobs WHERE id = :id
            """,
            {"id": job_id},
        )
        if row is None:
            return None
        return self._enum(JobStatus, row["status"]), row["cancellation_requested_at"] is not None

    def list(
        self,
        *,
        status: JobStatus | None = None,
        dataset_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[int, list[EvaluationJob]]:
        clauses: list[str] = []
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if status is not None:
            clauses.append("status = :status")
            params["status"] = status.value
        if dataset_id is not None:
            clauses.append("dataset_id = :dataset_id")
            params["dataset_id"] = dataset_id
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        filters = {key: value for key, value in params.items() if key not in {"limit", "offset"}}
        total = self._one(f"SELECT COUNT(*) AS n FROM evaluation_jobs {where}", filters)["n"]
        rows = self._all(
            f"""
            SELECT * FROM evaluation_jobs {where}
            ORDER BY created_at DESC OFFSET :offset LIMIT :limit
            """,
            params,
        )
        return int(total), [self._to_entity(row) for row in rows]

    def set_total_tickets(self, job_id: uuid.UUID, *, total: int, status: JobStatus) -> None:
        self._run(
            """
            UPDATE evaluation_jobs
            SET total_tickets = :total, status = :status, updated_at = now()
            WHERE id = :id
            """,
            {"total": total, "status": status.value, "id": job_id},
        )

    def mark_running_if_pending(self, job_ids: list[uuid.UUID]) -> int:
        if not job_ids:
            return 0
        locked = self._all_in(
            """
            SELECT id FROM evaluation_jobs
            WHERE id IN :ids AND status IN ('CREATED', 'READY')
            FOR UPDATE SKIP LOCKED
            """,
            key="ids",
            values=job_ids,
        )
        if not locked:
            return 0
        ids = [row["id"] for row in locked]
        self._run_in(
            """
            UPDATE evaluation_jobs
            SET status = 'RUNNING',
                started_at = COALESCE(started_at, now()),
                updated_at = now()
            WHERE id IN :ids
            """,
            key="ids",
            values=ids,
        )
        return len(ids)

    def request_cancellation(self, job_id: uuid.UUID) -> bool:
        row = self._one(
            """
            UPDATE evaluation_jobs
            SET cancellation_requested_at = COALESCE(cancellation_requested_at, now()),
                updated_at = now()
            WHERE id = :id
            RETURNING cancellation_requested_at
            """,
            {"id": job_id},
        )
        return row is not None

    def fail(self, job_id: uuid.UUID, message: str) -> None:
        self._run(
            """
            UPDATE evaluation_jobs
            SET status = 'FAILED',
                error_message = :message,
                completed_at = COALESCE(completed_at, now()),
                updated_at = now()
            WHERE id = :id
            """,
            {"message": message[:4000], "id": job_id},
        )

    def ticket_counts(self, job_id: uuid.UUID) -> dict[str, int]:
        rows = self._all(
            """
            SELECT status, COUNT(*) AS n
            FROM evaluation_tickets WHERE job_id = :id GROUP BY status
            """,
            {"id": job_id},
        )
        return {row["status"]: int(row["n"]) for row in rows}

    def recompute_progress(self, job_id: uuid.UUID, *, lock: bool = True) -> JobProgress | None:
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
        started = status not in {JobStatus.READY, JobStatus.CREATED}
        row = self._one(
            """
            UPDATE evaluation_jobs
            SET status = :status,
                total_tickets = GREATEST(total_tickets, :total),
                completed_tickets = :done,
                failed_tickets = :failed,
                cancelled_tickets = :cancelled,
                not_applicable_tickets = :not_applicable,
                started_at = CASE WHEN :started THEN COALESCE(started_at, now()) ELSE started_at END,
                completed_at = CASE WHEN :is_terminal THEN COALESCE(completed_at, now()) ELSE NULL END,
                updated_at = now()
            WHERE id = :id
            RETURNING *
            """,
            {
                "status": status.value,
                "total": total,
                "done": done,
                "failed": failed,
                "cancelled": cancelled,
                "not_applicable": not_applicable,
                "started": started,
                "is_terminal": is_terminal,
                "id": job_id,
            },
        )
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
