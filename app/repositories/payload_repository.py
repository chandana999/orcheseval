from __future__ import annotations

import uuid
from typing import Any

from app.models.entities import EvaluationPayload
from app.repositories.base import BaseRepository


class PayloadRepository(BaseRepository):
    """Complete agent execution payloads. The runner reads payloads only from here."""

    def _to_entity(self, row: dict[str, Any]) -> EvaluationPayload:
        return EvaluationPayload(
            id=row["id"],
            dataset_id=row["dataset_id"],
            external_payload_id=row["external_payload_id"],
            trace_id=row["trace_id"],
            session_id=row["session_id"],
            workflow_id=row["workflow_id"],
            payload_json=row["payload_json"],
            payload_version=row["payload_version"],
            source_type=row["source_type"],
            validation_report=row["validation_report"],
            created_at=row.get("created_at"),
        )

    def insert(self, payload: EvaluationPayload) -> EvaluationPayload:
        row = self.conn.execute(
            """
            INSERT INTO evaluation_payloads (
                id, dataset_id, external_payload_id, trace_id, session_id,
                workflow_id, payload_json, payload_version, source_type,
                validation_report
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                payload.id,
                payload.dataset_id,
                payload.external_payload_id,
                payload.trace_id,
                payload.session_id,
                payload.workflow_id,
                self._json(payload.payload_json),
                payload.payload_version,
                payload.source_type,
                self._json(payload.validation_report),
            ),
        ).fetchone()
        return self._to_entity(row)

    def bulk_insert(self, payloads: list[EvaluationPayload]) -> list[EvaluationPayload]:
        """Insert many payloads in one round trip, preserving the complete JSON."""
        if not payloads:
            return []
        with self.conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO evaluation_payloads (
                    id, dataset_id, external_payload_id, trace_id, session_id,
                    workflow_id, payload_json, payload_version, source_type,
                    validation_report
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        p.id,
                        p.dataset_id,
                        p.external_payload_id,
                        p.trace_id,
                        p.session_id,
                        p.workflow_id,
                        self._json(p.payload_json),
                        p.payload_version,
                        p.source_type,
                        self._json(p.validation_report),
                    )
                    for p in payloads
                ],
            )
        return payloads

    def get(self, payload_id: uuid.UUID) -> EvaluationPayload | None:
        row = self.conn.execute(
            "SELECT * FROM evaluation_payloads WHERE id = %s", (payload_id,)
        ).fetchone()
        return self._to_entity(row) if row else None

    def get_payload_json(self, payload_id: uuid.UUID) -> dict[str, Any] | None:
        """Load just the payload document (runner hot path)."""
        row = self.conn.execute(
            "SELECT payload_json FROM evaluation_payloads WHERE id = %s", (payload_id,)
        ).fetchone()
        return row["payload_json"] if row else None

    def list(
        self,
        *,
        dataset_id: uuid.UUID | None = None,
        trace_id: str | None = None,
        session_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[int, list[EvaluationPayload]]:
        clauses: list[str] = []
        params: list[Any] = []
        if dataset_id is not None:
            clauses.append("dataset_id = %s")
            params.append(dataset_id)
        if trace_id is not None:
            clauses.append("trace_id = %s")
            params.append(trace_id)
        if session_id is not None:
            clauses.append("session_id = %s")
            params.append(session_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        total = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM evaluation_payloads {where}",  # noqa: S608 - fixed clauses
            params,
        ).fetchone()["n"]
        rows = self.conn.execute(
            f"""
            SELECT * FROM evaluation_payloads {where}
            ORDER BY created_at, id
            OFFSET %s LIMIT %s
            """,  # noqa: S608 - fixed clauses, values still bound
            [*params, offset, limit],
        ).fetchall()
        return int(total), [self._to_entity(r) for r in rows]

    def list_ids_by_dataset(
        self, dataset_id: uuid.UUID, *, limit: int | None = None
    ) -> list[uuid.UUID]:
        """Payload ids for job creation, ordered deterministically."""
        if limit is None:
            rows = self.conn.execute(
                """
                SELECT id FROM evaluation_payloads
                WHERE dataset_id = %s ORDER BY created_at, id
                """,
                (dataset_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT id FROM evaluation_payloads
                WHERE dataset_id = %s ORDER BY created_at, id LIMIT %s
                """,
                (dataset_id, limit),
            ).fetchall()
        return [r["id"] for r in rows]

    def existing_ids(self, payload_ids: list[uuid.UUID]) -> list[uuid.UUID]:
        if not payload_ids:
            return []
        rows = self.conn.execute(
            "SELECT id FROM evaluation_payloads WHERE id = ANY(%s)",
            (payload_ids,),
        ).fetchall()
        return [r["id"] for r in rows]

    def count_by_dataset(self, dataset_id: uuid.UUID) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM evaluation_payloads WHERE dataset_id = %s",
            (dataset_id,),
        ).fetchone()
        return int(row["n"])

    def iter_payload_json_by_dataset(self, dataset_id: uuid.UUID):
        """Stream payload documents for dataset export."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT payload_json FROM evaluation_payloads
                WHERE dataset_id = %s ORDER BY created_at, id
                """,
                (dataset_id,),
            )
            for row in cur:
                yield row["payload_json"]
