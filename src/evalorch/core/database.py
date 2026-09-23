"""SQLAlchemy engine and sessions.

Callers own transaction boundaries through `transaction()` or `Session.begin()`.
psycopg 3 is the PostgreSQL driver under SQLAlchemy.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from evalorch.core.config import settings

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine, _session_factory
    if _engine is None:
        options = (
            f"-c statement_timeout={settings.db_statement_timeout_ms} "
            f"-c idle_in_transaction_session_timeout="
            f"{settings.db_idle_in_transaction_timeout_ms}"
        )
        _engine = create_engine(
            settings.sqlalchemy_url,
            pool_size=max(settings.db_pool_max_size, 1),
            max_overflow=0,
            pool_pre_ping=True,
            pool_timeout=settings.db_connect_timeout,
            connect_args={
                "connect_timeout": settings.db_connect_timeout,
                "options": options,
            },
        )
        if settings.pgschema and settings.pgschema != "public":
            schema = settings.pgschema

            @event.listens_for(_engine, "connect")
            def _set_search_path(dbapi_connection, _connection_record) -> None:
                cursor = dbapi_connection.cursor()
                cursor.execute(f"SET search_path TO {schema}, public")
                cursor.close()

        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False, autoflush=False)
    return _engine


def _factory() -> sessionmaker[Session]:
    get_engine()
    assert _session_factory is not None
    return _session_factory


def close_pool() -> None:
    """Dispose the engine. Name kept so the runner and API lifespan stay the same."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
        _engine = None
        _session_factory = None


def reset_pool() -> None:
    close_pool()


@contextmanager
def transaction() -> Generator[Session, None, None]:
    """Commit on success, roll back on error."""
    session = _factory()()
    try:
        with session.begin():
            yield session
    finally:
        session.close()


def open_session() -> Session:
    return _factory()()


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency. The endpoint opens `session.begin()` when it writes."""
    session = _factory()()
    try:
        yield session
    finally:
        session.close()


def healthcheck() -> tuple[bool, str]:
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
        return True, "ok"
    except Exception as exc:  # pragma: no cover - depends on server state
        from evalorch.core.logging import get_logger

        get_logger(__name__).error(
            "database_healthcheck_failed",
            error_type=type(exc).__name__,
            error_code="SERVICE_UNAVAILABLE",
        )
        return False, "unavailable"
