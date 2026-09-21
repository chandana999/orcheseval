from __future__ import annotations

import uuid
from typing import Any

from app.models.entities import MetricRecord
from app.repositories.base import BaseRepository


class MetricRepository(BaseRepository):
    def _to_entity(self, row: dict[str, Any]) -> MetricRecord:
        return MetricRecord(
            metric_record_id=row["metric_record_id"],
            metric_id=row["metric_id"],
            metric_code=row["metric_code"],
            metric_name=row["metric_name"],
            metric_desc=row["metric_desc"],
            metric_type=row["metric_type"],
            metric_version_number=row["metric_version_number"],
            definition_payload=row["definition_payload"],
            default_threshold_operator=row["default_threshold_operator"],
            llm_model_name=row["llm_model_name"],
            llm_model_version=row["llm_model_version"],
            llm_deployed_id=row["llm_deployed_id"],
            is_active_indicator=row["is_active_indicator"],
            previous_metric_record_id=row["previous_metric_record_id"],
            change_summary=row["change_summary"],
            metric_create_timestamp=row.get("metric_create_timestamp"),
        )

    def insert(self, record: MetricRecord) -> MetricRecord:
        row = self.conn.execute(
            """
            INSERT INTO metric_records (
                metric_record_id, metric_id, metric_code, metric_name, metric_desc,
                metric_type, metric_version_number, definition_payload,
                default_threshold_operator, llm_model_name, llm_model_version,
                llm_deployed_id, is_active_indicator, previous_metric_record_id,
                change_summary
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                record.metric_record_id,
                record.metric_id,
                record.metric_code,
                record.metric_name,
                record.metric_desc,
                record.metric_type,
                record.metric_version_number,
                self._json(record.definition_payload),
                record.default_threshold_operator,
                record.llm_model_name,
                record.llm_model_version,
                record.llm_deployed_id,
                record.is_active_indicator,
                record.previous_metric_record_id,
                record.change_summary,
            ),
        ).fetchone()
        return self._to_entity(row)

    def get(self, metric_record_id: uuid.UUID) -> MetricRecord | None:
        row = self.conn.execute(
            "SELECT * FROM metric_records WHERE metric_record_id = %s",
            (metric_record_id,),
        ).fetchone()
        return self._to_entity(row) if row else None

    def get_by_id_version(self, metric_id: str, version: int) -> MetricRecord | None:
        row = self.conn.execute(
            """
            SELECT * FROM metric_records
            WHERE metric_id = %s AND metric_version_number = %s
            """,
            (metric_id, version),
        ).fetchone()
        return self._to_entity(row) if row else None

    def get_many(self, metric_record_ids: list[uuid.UUID]) -> list[MetricRecord]:
        if not metric_record_ids:
            return []
        rows = self.conn.execute(
            """
            SELECT * FROM metric_records
            WHERE metric_record_id = ANY(%s)
            """,
            (metric_record_ids,),
        ).fetchall()
        by_id = {row["metric_record_id"]: self._to_entity(row) for row in rows}
        return [by_id[mid] for mid in metric_record_ids if mid in by_id]
