from __future__ import annotations

import uuid
from enum import Enum
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb


class BaseRepository:
    """Shared helpers for hand-written SQL repositories.

    Every repository takes the caller's connection so the caller owns the
    transaction boundary.
    """

    def __init__(self, conn: Connection) -> None:
        self.conn = conn

    @staticmethod
    def _enum(cls: type[Enum], value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, cls):
            return value
        return cls(value)

    @staticmethod
    def _uuid(value: Any) -> uuid.UUID | None:
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(str(value))

    @staticmethod
    def _json(value: Any) -> Jsonb | None:
        """Wrap a Python value for a JSONB column, preserving explicit nulls."""
        if value is None:
            return None
        return Jsonb(value)
