"""Evaluation profiles and their profile -> metric version mappings."""

from __future__ import annotations

import uuid
from typing import Any

from app.models.entities import EvaluationProfile, EvaluationProfileMetric, MetricRecord
from app.repositories.base import BaseRepository
from app.repositories.metric_repository import MetricRepository


class ProfileRepository(BaseRepository):
    def _to_profile(self, row: dict[str, Any]) -> EvaluationProfile:
        return EvaluationProfile(
            id=row["id"],
            evaluation_profile_id=row["evaluation_profile_id"],
            version=row["version"],
            name=row["name"],
            description=row["description"],
            is_active=row["is_active"],
            metadata_json=row.get("metadata_json"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )

    def _to_mapping(self, row: dict[str, Any]) -> EvaluationProfileMetric:
        return EvaluationProfileMetric(
            id=row["id"],
            profile_id=row["profile_id"],
            metric_record_id=row["metric_record_id"],
            metric_id=row.get("metric_id"),
            execution_order=row["execution_order"],
            enabled=row["enabled"],
            created_at=row.get("created_at"),
        )

    def insert(self, profile: EvaluationProfile) -> EvaluationProfile:
        row = self.conn.execute(
            """
            INSERT INTO evaluation_profiles (
                id, evaluation_profile_id, version, name, description,
                is_active, metadata_json
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                profile.id,
                profile.evaluation_profile_id,
                profile.version,
                profile.name,
                profile.description,
                profile.is_active,
                self._json(profile.metadata_json),
            ),
        ).fetchone()
        return self._to_profile(row)

    def get(self, evaluation_profile_id: str) -> EvaluationProfile | None:
        """Return the highest version of a profile key, active or not.

        Activation is reported back to the caller rather than filtered here, so
        job creation can fail with "profile is inactive" instead of "not found".
        """
        row = self.conn.execute(
            """
            SELECT * FROM evaluation_profiles
            WHERE evaluation_profile_id = %s
            ORDER BY version DESC
            LIMIT 1
            """,
            (evaluation_profile_id,),
        ).fetchone()
        return self._to_profile(row) if row else None

    def get_version(self, evaluation_profile_id: str, version: int) -> EvaluationProfile | None:
        row = self.conn.execute(
            """
            SELECT * FROM evaluation_profiles
            WHERE evaluation_profile_id = %s AND version = %s
            """,
            (evaluation_profile_id, version),
        ).fetchone()
        return self._to_profile(row) if row else None

    def add_metric(
        self,
        *,
        profile_id: uuid.UUID,
        metric_record_id: uuid.UUID,
        metric_id: str,
        execution_order: int = 0,
        enabled: bool = True,
    ) -> EvaluationProfileMetric:
        row = self.conn.execute(
            """
            INSERT INTO evaluation_profile_metrics (
                id, profile_id, metric_record_id, metric_id, execution_order, enabled
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                uuid.uuid4(),
                profile_id,
                metric_record_id,
                metric_id,
                execution_order,
                enabled,
            ),
        ).fetchone()
        return self._to_mapping(row)

    def list_mappings(self, profile_id: uuid.UUID) -> list[EvaluationProfileMetric]:
        rows = self.conn.execute(
            """
            SELECT * FROM evaluation_profile_metrics
            WHERE profile_id = %s AND enabled IS TRUE
            ORDER BY execution_order, metric_id
            """,
            (profile_id,),
        ).fetchall()
        return [self._to_mapping(row) for row in rows]

    def list_metric_records(self, profile_id: uuid.UUID) -> list[MetricRecord]:
        """Metric record versions mapped to a profile, in execution order."""
        rows = self.conn.execute(
            """
            SELECT m.*
            FROM evaluation_profile_metrics pm
            JOIN metric_records m ON m.metric_record_id = pm.metric_record_id
            WHERE pm.profile_id = %s AND pm.enabled IS TRUE
            ORDER BY pm.execution_order, m.metric_id, m.metric_version_number
            """,
            (profile_id,),
        ).fetchall()
        metrics = MetricRepository(self.conn)
        return [metrics._to_entity(row) for row in rows]
