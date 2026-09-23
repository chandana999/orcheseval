from __future__ import annotations

import uuid
from typing import Any

from evalorch.models.entities import EvaluationResult
from evalorch.models.enums import CheckType, ResultStatus
from evalorch.repositories.base import BaseRepository, _json


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
        row = self._one(
            """
            INSERT INTO evaluation_results (
                id, job_id, ticket_id, payload_id, source_payload_ref,
                metric_record_id, metric_id, metric_version_number,
                check_id, check_type, evaluator_type, evaluator_version, status,
                passed, score, explanation, evidence_json, input_snapshot_json,
                output_json, error_code, error_message, execution_time_ms,
                attempt_count, completed_at
            )
            VALUES (
                :id, :job_id, :ticket_id, :payload_id, :source_payload_ref,
                :metric_record_id, :metric_id, :metric_version_number,
                :check_id, :check_type, :evaluator_type, :evaluator_version, :status,
                :passed, :score, :explanation,
                CAST(:evidence_json AS jsonb), CAST(:input_snapshot_json AS jsonb),
                CAST(:output_json AS jsonb), :error_code, :error_message,
                :execution_time_ms, :attempt_count, now()
            )
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
            {
                "id": result.id,
                "job_id": result.job_id,
                "ticket_id": result.ticket_id,
                "payload_id": result.payload_id,
                "source_payload_ref": result.source_payload_ref,
                "metric_record_id": result.metric_record_id,
                "metric_id": result.metric_id,
                "metric_version_number": result.metric_version_number,
                "check_id": result.check_id,
                "check_type": result.check_type.value,
                "evaluator_type": result.evaluator_type,
                "evaluator_version": result.evaluator_version,
                "status": result.status.value,
                "passed": result.passed,
                "score": result.score,
                "explanation": result.explanation,
                "evidence_json": _json(result.evidence_json),
                "input_snapshot_json": _json(result.input_snapshot_json),
                "output_json": _json(result.output_json),
                "error_code": result.error_code,
                "error_message": result.error_message[:4000] if result.error_message else None,
                "execution_time_ms": result.execution_time_ms,
                "attempt_count": result.attempt_count,
            },
        )
        return self._to_entity(row)

    def get(self, result_id: uuid.UUID) -> EvaluationResult | None:
        row = self._one("SELECT * FROM evaluation_results WHERE id = :id", {"id": result_id})
        return self._to_entity(row) if row else None

    def get_by_ticket(self, ticket_id: uuid.UUID) -> EvaluationResult | None:
        row = self._one(
            "SELECT * FROM evaluation_results WHERE ticket_id = :ticket_id",
            {"ticket_id": ticket_id},
        )
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
        clauses = ["job_id = :job_id"]
        params: dict[str, Any] = {"job_id": job_id, "limit": limit, "offset": offset}
        if status is not None:
            clauses.append("status = :status")
            params["status"] = status.value
        if check_id is not None:
            clauses.append("check_id = :check_id")
            params["check_id"] = check_id
        if payload_id is not None:
            clauses.append("payload_id = :payload_id")
            params["payload_id"] = payload_id
        if source_payload_ref is not None:
            clauses.append("source_payload_ref = :source_payload_ref")
            params["source_payload_ref"] = source_payload_ref
        if metric_id is not None:
            clauses.append("metric_id = :metric_id")
            params["metric_id"] = metric_id
        where = " AND ".join(clauses)
        filters = {key: value for key, value in params.items() if key not in {"limit", "offset"}}
        total = self._one(
            f"SELECT COUNT(*) AS n FROM evaluation_results WHERE {where}", filters
        )["n"]
        rows = self._all(
            f"""
            SELECT * FROM evaluation_results WHERE {where}
            ORDER BY created_at, check_id
            OFFSET :offset LIMIT :limit
            """,
            params,
        )
        return int(total), [self._to_entity(row) for row in rows]

    def count_by_job(self, job_id: uuid.UUID) -> int:
        row = self._one(
            "SELECT COUNT(*) AS n FROM evaluation_results WHERE job_id = :job_id",
            {"job_id": job_id},
        )
        return int(row["n"])

    def summary_by_job(self, job_id: uuid.UUID) -> dict[str, Any]:
        overall = self._one(
            """
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE status = 'PASSED') AS passed,
                COUNT(*) FILTER (WHERE status = 'FAILED') AS failed,
                COUNT(*) FILTER (WHERE status = 'NOT_APPLICABLE') AS not_applicable,
                COUNT(*) FILTER (WHERE status = 'ERROR') AS errored,
                AVG(score) AS avg_score
            FROM evaluation_results WHERE job_id = :job_id
            """,
            {"job_id": job_id},
        )
        per_check = self._all(
            """
            SELECT check_id, check_type, COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE status = 'PASSED') AS passed,
                   COUNT(*) FILTER (WHERE status = 'FAILED') AS failed,
                   COUNT(*) FILTER (WHERE status = 'NOT_APPLICABLE') AS not_applicable,
                   COUNT(*) FILTER (WHERE status = 'ERROR') AS errored,
                   AVG(score) AS avg_score
            FROM evaluation_results WHERE job_id = :job_id
            GROUP BY check_id, check_type
            ORDER BY check_id
            """,
            {"job_id": job_id},
        )
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
                    "check_id": row["check_id"],
                    "check_type": row["check_type"],
                    "total": int(row["total"]),
                    "passed": int(row["passed"]),
                    "failed": int(row["failed"]),
                    "not_applicable": int(row["not_applicable"]),
                    "errored": int(row["errored"]),
                    "avg_score": round(float(row["avg_score"]), 4)
                    if row["avg_score"] is not None
                    else None,
                }
                for row in per_check
            ],
        }
