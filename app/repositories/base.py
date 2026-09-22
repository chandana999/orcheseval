"""Session helpers shared by repositories."""

from __future__ import annotations

import json
from enum import Enum
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value)


class BaseRepository:
    """Repositories take the caller's Session so the caller owns the transaction."""

    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _enum(cls: type[Enum], value: Any) -> Any:
        if value is None or isinstance(value, cls):
            return value
        return cls(value)

    def _one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        return self.session.execute(text(sql), params or {}).mappings().first()

    def _all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        return list(self.session.execute(text(sql), params or {}).mappings().all())

    def _run(self, sql: str, params: dict[str, Any] | None = None) -> CursorResult[Any]:
        return self.session.execute(text(sql), params or {})

    def _all_in(self, sql: str, *, key: str, values: list[Any], extra: dict[str, Any] | None = None) -> list[Any]:
        statement = text(sql).bindparams(bindparam(key, expanding=True))
        params = {key: list(values), **(extra or {})}
        return list(self.session.execute(statement, params).mappings().all())

    def _run_in(self, sql: str, *, key: str, values: list[Any], extra: dict[str, Any] | None = None) -> CursorResult[Any]:
        statement = text(sql).bindparams(bindparam(key, expanding=True))
        params = {key: list(values), **(extra or {})}
        return self.session.execute(statement, params)
