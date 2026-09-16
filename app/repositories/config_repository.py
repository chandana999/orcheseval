from __future__ import annotations

import uuid
from typing import Any

from app.models.entities import EvaluationConfig
from app.models.enums import ConfigStatus
from app.repositories.base import BaseRepository


class ConfigRepository(BaseRepository):
    def _to_entity(self, row: dict[str, Any]) -> EvaluationConfig:
        return EvaluationConfig(
            id=row["id"],
            name=row["name"],
            version=row["version"],
            description=row["description"],
            config_json=row["config_json"],
            status=self._enum(ConfigStatus, row["status"]),
            checks_count=row["checks_count"],
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )

    def insert(self, config: EvaluationConfig) -> EvaluationConfig:
        row = self.conn.execute(
            """
            INSERT INTO evaluation_configs (
                id, name, version, description, config_json, status, checks_count
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                config.id,
                config.name,
                config.version,
                config.description,
                self._json(config.config_json),
                config.status.value,
                config.checks_count,
            ),
        ).fetchone()
        return self._to_entity(row)

    def get(self, config_id: uuid.UUID) -> EvaluationConfig | None:
        row = self.conn.execute(
            "SELECT * FROM evaluation_configs WHERE id = %s", (config_id,)
        ).fetchone()
        return self._to_entity(row) if row else None

    def get_by_name_version(self, name: str, version: int) -> EvaluationConfig | None:
        row = self.conn.execute(
            "SELECT * FROM evaluation_configs WHERE name = %s AND version = %s",
            (name, version),
        ).fetchone()
        return self._to_entity(row) if row else None

    def get_latest_by_name(self, name: str) -> EvaluationConfig | None:
        row = self.conn.execute(
            """
            SELECT * FROM evaluation_configs
            WHERE name = %s ORDER BY version DESC LIMIT 1
            """,
            (name,),
        ).fetchone()
        return self._to_entity(row) if row else None

    def next_version(self, name: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS v FROM evaluation_configs WHERE name = %s",
            (name,),
        ).fetchone()
        return int(row["v"])

    def list(
        self,
        *,
        name: str | None = None,
        status: ConfigStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[int, list[EvaluationConfig]]:
        clauses: list[str] = []
        params: list[Any] = []
        if name is not None:
            clauses.append("name = %s")
            params.append(name)
        if status is not None:
            clauses.append("status = %s")
            params.append(status.value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        total = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM evaluation_configs {where}",  # noqa: S608
            params,
        ).fetchone()["n"]
        rows = self.conn.execute(
            f"""
            SELECT * FROM evaluation_configs {where}
            ORDER BY name, version DESC
            OFFSET %s LIMIT %s
            """,  # noqa: S608
            [*params, offset, limit],
        ).fetchall()
        return int(total), [self._to_entity(r) for r in rows]

    def set_status(self, config_id: uuid.UUID, status: ConfigStatus) -> EvaluationConfig | None:
        row = self.conn.execute(
            """
            UPDATE evaluation_configs
            SET status = %s, updated_at = now()
            WHERE id = %s
            RETURNING *
            """,
            (status.value, config_id),
        ).fetchone()
        return self._to_entity(row) if row else None
