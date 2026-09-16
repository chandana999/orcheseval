"""PostgreSQL access built directly on psycopg 3.

There is no ORM here by design: repositories issue native parameterized SQL and
callers control transaction boundaries explicitly through `transaction()`.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.core.config import settings

_pool: ConnectionPool | None = None


def _configure_connection(conn: Connection) -> None:
    """Pin every pooled connection to the configured schema."""
    if settings.pgschema and settings.pgschema != "public":
        # Identifier validated by Settings; SET does not accept bind parameters.
        conn.execute(f"SET search_path TO {settings.pgschema}, public")
        conn.commit()


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            conninfo=settings.conninfo,
            min_size=settings.db_pool_min_size,
            max_size=settings.db_pool_max_size,
            kwargs={"row_factory": dict_row, "autocommit": False},
            configure=_configure_connection,
            open=True,
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def reset_pool() -> None:
    """Drop the pool so the next call rebuilds it from current settings (tests)."""
    close_pool()


def connect(*, autocommit: bool = False) -> Connection:
    """Open a standalone connection outside the pool (migrations, scripts)."""
    conn = Connection.connect(settings.conninfo, row_factory=dict_row, autocommit=autocommit)
    if settings.pgschema and settings.pgschema != "public":
        conn.execute(f"SET search_path TO {settings.pgschema}, public")
        if not autocommit:
            conn.commit()
    return conn


def get_conn() -> Generator[Connection, None, None]:
    """FastAPI dependency yielding a pooled connection."""
    with get_pool().connection() as conn:
        yield conn


@contextmanager
def transaction() -> Generator[Connection, None, None]:
    """Explicit transaction boundary: commits on success, rolls back on error."""
    with get_pool().connection() as conn:
        with conn.transaction():
            yield conn


def healthcheck() -> tuple[bool, str]:
    try:
        with get_pool().connection() as conn:
            conn.execute("SELECT 1")
        return True, "ok"
    except Exception as exc:  # pragma: no cover - depends on server state
        return False, f"error: {exc}"
