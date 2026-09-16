from __future__ import annotations

import uuid
from typing import Any

from app.models.entities import Dataset
from app.models.enums import DatasetStatus
from app.repositories.base import BaseRepository


class DatasetRepository(BaseRepository):
    def _to_entity(self, row: dict[str, Any]) -> Dataset:
        return Dataset(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            source_type=row["source_type"],
            source_path=row["source_path"],
            record_count=row["record_count"],
            status=self._enum(DatasetStatus, row["status"]),
            checksum_sha256=row["checksum_sha256"],
            validation_report=row["validation_report"],
            metadata_json=row["metadata_json"],
            created_at=row["created_at"],
            updated_at=row.get("updated_at"),
        )

    def insert(self, dataset: Dataset) -> Dataset:
        row = self.conn.execute(
            """
            INSERT INTO datasets (
                id, name, description, source_type, source_path, record_count,
                status, checksum_sha256, validation_report, metadata_json
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                dataset.id,
                dataset.name,
                dataset.description,
                dataset.source_type,
                dataset.source_path,
                dataset.record_count,
                dataset.status.value,
                dataset.checksum_sha256,
                self._json(dataset.validation_report),
                self._json(dataset.metadata_json),
            ),
        ).fetchone()
        return self._to_entity(row)

    def get(self, dataset_id: uuid.UUID) -> Dataset | None:
        row = self.conn.execute(
            "SELECT * FROM datasets WHERE id = %s", (dataset_id,)
        ).fetchone()
        return self._to_entity(row) if row else None

    def list(
        self,
        *,
        status: DatasetStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[int, list[Dataset]]:
        if status is not None:
            total = self.conn.execute(
                "SELECT COUNT(*) AS n FROM datasets WHERE status = %s", (status.value,)
            ).fetchone()["n"]
            rows = self.conn.execute(
                """
                SELECT * FROM datasets WHERE status = %s
                ORDER BY created_at DESC OFFSET %s LIMIT %s
                """,
                (status.value, offset, limit),
            ).fetchall()
        else:
            total = self.conn.execute("SELECT COUNT(*) AS n FROM datasets").fetchone()["n"]
            rows = self.conn.execute(
                "SELECT * FROM datasets ORDER BY created_at DESC OFFSET %s LIMIT %s",
                (offset, limit),
            ).fetchall()
        return int(total), [self._to_entity(r) for r in rows]

    def finalize_ingestion(
        self,
        dataset_id: uuid.UUID,
        *,
        status: DatasetStatus,
        record_count: int | None = None,
        checksum_sha256: str | None = None,
        source_path: str | None = None,
        validation_report: dict[str, Any] | None = None,
    ) -> Dataset:
        """Set post-ingestion metadata. Uses COALESCE so partial updates are safe."""
        row = self.conn.execute(
            """
            UPDATE datasets
            SET status = %s,
                record_count = COALESCE(%s, record_count),
                checksum_sha256 = COALESCE(%s, checksum_sha256),
                source_path = COALESCE(%s, source_path),
                validation_report = COALESCE(%s, validation_report),
                updated_at = now()
            WHERE id = %s
            RETURNING *
            """,
            (
                status.value,
                record_count,
                checksum_sha256,
                source_path,
                self._json(validation_report),
                dataset_id,
            ),
        ).fetchone()
        return self._to_entity(row)

    def refresh_record_count(self, dataset_id: uuid.UUID) -> int:
        """Recount payloads from the authoritative payload table."""
        row = self.conn.execute(
            """
            UPDATE datasets d
            SET record_count = c.n, updated_at = now()
            FROM (
                SELECT COUNT(*) AS n FROM evaluation_payloads WHERE dataset_id = %s
            ) c
            WHERE d.id = %s
            RETURNING d.record_count
            """,
            (dataset_id, dataset_id),
        ).fetchone()
        return int(row["record_count"]) if row else 0
