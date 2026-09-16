"""Plain SQL migration runner built on psycopg 3.

Deliberately not Alembic: Alembic depends on SQLAlchemy, which this project does
not use. Each `.sql` file is applied exactly once inside its own transaction and
recorded in `schema_migrations` with a checksum, under a session advisory lock so
concurrent runners cannot double-apply. Nothing is ever dropped automatically.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

from psycopg import Connection

from app.core.config import settings
from app.core.database import connect

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
ADVISORY_LOCK_KEY = 8_471_102


def _checksum(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _migration_files() -> list[Path]:
    return [p for p in sorted(MIGRATIONS_DIR.glob("*.sql")) if p.is_file()]


def ensure_schema(conn: Connection) -> None:
    """Create the target schema (when not `public`) and the migration ledger."""
    schema = settings.pgschema
    if schema and schema != "public":
        # Identifier validated by Settings; DDL cannot use bind parameters.
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        conn.execute(f"SET search_path TO {schema}, public")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            checksum CHAR(64) NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def applied_migrations(conn: Connection) -> dict[str, str]:
    rows = conn.execute(
        "SELECT version, checksum FROM schema_migrations ORDER BY version"
    ).fetchall()
    return {row["version"]: row["checksum"] for row in rows}


def upgrade(conn: Connection | None = None) -> list[str]:
    """Apply pending migrations. Returns the versions that were applied."""
    owns_connection = conn is None
    if conn is None:
        conn = connect()
    applied: list[str] = []
    try:
        conn.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
        try:
            ensure_schema(conn)
            conn.commit()
            current = applied_migrations(conn)
            for path in _migration_files():
                version = path.stem
                content = path.read_bytes()
                checksum = _checksum(content)
                if version in current:
                    if current[version] != checksum:
                        raise RuntimeError(
                            f"Migration {version} was already applied with a different "
                            "checksum. Applied migrations must never be edited; add a "
                            "new migration file instead."
                        )
                    continue
                with conn.transaction():
                    conn.execute(content.decode("utf-8"))
                    conn.execute(
                        "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                        (version, checksum),
                    )
                applied.append(version)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
            conn.commit()
    finally:
        if owns_connection:
            conn.close()
    return applied


def status() -> list[dict[str, str]]:
    with connect() as conn:
        ensure_schema(conn)
        conn.commit()
        current = applied_migrations(conn)
        rows: list[dict[str, str]] = []
        for path in _migration_files():
            version = path.stem
            checksum = _checksum(path.read_bytes())
            state = "applied" if version in current else "pending"
            if version in current and current[version] != checksum:
                state = "checksum_mismatch"
            rows.append({"version": version, "state": state, "file": path.name})
        return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="eval-platform SQL migrations")
    parser.add_argument("command", choices=["upgrade", "status"])
    args = parser.parse_args(argv)

    target = f"{settings.pgdatabase} (schema {settings.pgschema})"
    if args.command == "upgrade":
        applied = upgrade()
        if applied:
            print(f"Applied to {target}: " + ", ".join(applied))
        else:
            print(f"{target} is already up to date.")
        return 0

    print(f"Migration status for {target}:")
    for row in status():
        print(f"  {row['version']:32} {row['state']:18} {row['file']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
