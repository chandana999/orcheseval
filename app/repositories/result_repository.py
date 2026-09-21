from __future__ import annotations

import uuid
from typing import Any

from app.models.entities import EvaluationResult
from app.models.enums import CheckType, ResultStatus
from app.repositories.base import BaseRepository


class ResultRepository(BaseRepository):
    def _to_entity(self, row: dict[str, Any]) -> EvaluationResult:
        return EvaluationResult(
            id=row["id"],
            job_id=row["job_id"],
            ticket_id=row["ticket_id"],
            payload_id=row["payload_id"],
            source_payload_ref=row.get("source_payload_ref"),
            metric_record_id=row.get("metric_record_id"),
            metric_id=row.get("metric_id"),
            metric_version_number=row.get("metric_version_number"),
            check_id=row["check_id"],
            check_type=self._enum(CheckType, row["check_type"]),
            evaluator_type=row["evaluator_type"],
            evaluator_version=row["evaluator_version"],
            status=self._enum(ResultStatus, row["status"]),
            passed=row["passed"],
            score=row["score"],
            explanation=row["explanation"],
            evidence_json=row["evidence_json"],
            input_snapshot_json=row["input_snapshot_json"],
            output_json=row["output_json"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            execution_time_ms=row["execution_time_ms"],
            attempt_count=row["attempt_count"],
            created_at=row.get("created_at"),
            completed_at=row.get("completed_at"),
        )

    def upsert(self, result: EvaluationResult) -> EvaluationResult:
        """Idempotent persistence keyed by ticket_id.

        A retried ticket overwrites its previous result instead of creating a
        duplicate row, so at most one result exists per ticket.
        """
        row = self.conn.execute(
            """
            INSERT INTO evaluation_results (
                id, job_id, ticket_id, payload_id, source_payload_ref,
                metric_record_id, metric_id, metric_version_number,
                check_id, check_type, evaluator_type, evaluator_version, status,
                passed, score, explanation, evidence_json, input_snapshot_json,
                output_json, error_code, error_message, execution_time_ms,
                attempt_count, completed_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (ticket_id) DO UPDATE SET
                payload_id = EXCLUDED.payload_id,
                source_payload_ref = EXCLUDED.source_payload_ref,
                metric_record_id = EXCLUDED.metric_record_id,
                metric_id = EXCLUDED.metric_id,
                metric_version_number = EXCLUDED.metric_version_number,
                check_id = EXCLUDED.check_id,
                check_type = EXCLUDED.check_type,
                evaluator_type = EXCLUDED.evaluator_type,
                evaluator_version = EXCLUDED.evaluator_version,
                status = EXCLUDED.status,
                passed = EXCLUDED.passed,
                score = EXCLUDED.score,
                explanation = EXCLUDED.explanation,
                evidence_json = EXCLUDED.evidence_json,
                input_snapshot_json = EXCLUDED.input_snapshot_json,
                output_json = EXCLUDED.output_json,
                error_code = EXCLUDED.error_code,
                error_message = EXCLUDED.error_message,
                execution_time_ms = EXCLUDED.execution_time_ms,
                attempt_count = EXCLUDED.attempt_count,
                completed_at = now()
            RETURNING *
            """,
            (
                result.id,
                result.job_id,
                result.ticket_id,
                result.payload_id,
                result.source_payload_ref,
                result.metric_record_id,
                result.metric_id,
                result.metric_version_number,
                result.check_id,
                result.check_type.value,
                result.evaluator_type,
                result.evaluator_version,
                result.status.value,
                result.passed,
                result.score,
                result.explanation,
                self._json(result.evidence_json),
                self._json(result.input_snapshot_json),
                self._json(result.output_json),
                result.error_code,
                result.error_message[:4000] if result.error_message else None,
                result.execution_time_ms,
                result.attempt_count,
            ),
        ).fetchone()
        return self._to_entity(row)

    def get(self, result_id: uuid.UUID) -> EvaluationResult | None:
        row = self.conn.execute(
            "SELECT * FROM evaluation_results WHERE id = %s", (result_id,)
        ).fetchone()
        return self._to_entity(row) if row else None

    def get_by_ticket(self, ticket_id: uuid.UUID) -> EvaluationResult | None:
        row = self.conn.execute(
            "SELECT * FROM evaluation_results WHERE ticket_id = %s", (ticket_id,)
        ).fetchone()
        return self._to_entity(row) if row else None

    def list_by_job(
        self,
        job_id: uuid.UUID,
        *,
        status: ResultStatus | None = None,
        check_id: str | None = None,
        payload_id: uuid.UUID | None = None,
        source_payload_ref: str | None = None,
        metric_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[int, list[EvaluationResult]]:
        clauses = ["job_id = %s"]
        params: list[Any] = [job_id]
        if status is not None:
            clauses.append("status = %s")
            params.append(status.value)
        if check_id is not None:
            clauses.append("check_id = %s")
            params.append(check_id)
        if payload_id is not None:
            clauses.append("payload_id = %s")
            params.append(payload_id)
        if source_payload_ref is not None:
            clauses.append("source_payload_ref = %s")
            params.append(source_payload_ref)
        if metric_id is not None:
            clauses.append("metric_id = %s")
            params.append(metric_id)
        where = " AND ".join(clauses)
        total = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM evaluation_results WHERE {where}",  # noqa: S608
            params,
        ).fetchone()["n"]
        rows = self.conn.execute(
            f"""
            SELECT * FROM evaluation_results WHERE {where}
            ORDER BY created_at, check_id
            OFFSET %s LIMIT %s
            """,  # noqa: S608
            [*params, offset, limit],
        ).fetchall()
        return int(total), [self._to_entity(r) for r in rows]

    def count_by_job(self, job_id: uuid.UUID) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM evaluation_results WHERE job_id = %s",
            (job_id,),
        ).fetchone()
        return int(row["n"])

    def summary_by_job(self, job_id: uuid.UUID) -> dict[str, Any]:
        """Aggregate pass rate and score per check, computed in PostgreSQL."""
        overall = self.conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE status = 'PASSED') AS passed,
                COUNT(*) FILTER (WHERE status = 'FAILED') AS failed,
                COUNT(*) FILTER (WHERE status = 'NOT_APPLICABLE') AS not_applicable,
                COUNT(*) FILTER (WHERE status = 'ERROR') AS errored,
                AVG(score) AS avg_score
            FROM evaluation_results WHERE job_id = %s
            """,
            (job_id,),
        ).fetchone()
        per_check = self.conn.execute(
            """
            SELECT check_id,
                   check_type,
                   COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE status = 'PASSED') AS passed,
                   COUNT(*) FILTER (WHERE status = 'FAILED') AS failed,
                   COUNT(*) FILTER (WHERE status = 'NOT_APPLICABLE') AS not_applicable,
                   COUNT(*) FILTER (WHERE status = 'ERROR') AS errored,
                   AVG(score) AS avg_score
            FROM evaluation_results WHERE job_id = %s
            GROUP BY check_id, check_type
            ORDER BY check_id
            """,
            (job_id,),
        ).fetchall()

        total = int(overall["total"] or 0)
        passed = int(overall["passed"] or 0)
        scored = passed + int(overall["failed"] or 0)
        return {
            "job_id": str(job_id),
            "total_results": total,
            "passed": passed,
            "failed": int(overall["failed"] or 0),
            "not_applicable": int(overall["not_applicable"] or 0),
            "errored": int(overall["errored"] or 0),
            "pass_rate": round(passed / scored, 4) if scored else None,
            "avg_score": round(float(overall["avg_score"]), 4)
            if overall["avg_score"] is not None
            else None,
            "checks": [
                {
                    "check_id": r["check_id"],
                    "check_type": r["check_type"],
                    "total": int(r["total"]),
                    "passed": int(r["passed"]),
                    "failed": int(r["failed"]),
                    "not_applicable": int(r["not_applicable"]),
                    "errored": int(r["errored"]),
                    "avg_score": round(float(r["avg_score"]), 4)
                    if r["avg_score"] is not None
                    else None,
                }
                for r in per_check
            ],
        }
