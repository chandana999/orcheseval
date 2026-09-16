"""Verify PostgreSQL connectivity and report the applied migrations."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.core.database import connect  # noqa: E402


def main() -> int:
    print(f"host={settings.pghost} port={settings.pgport} db={settings.pgdatabase} "
          f"user={settings.pguser} schema={settings.pgschema}")
    try:
        with connect() as conn:
            version = conn.execute("SELECT version() AS v").fetchone()["v"]
            print(f"connected: {version.split(',')[0]}")
            tables = conn.execute(
                """
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = %s ORDER BY table_name
                """,
                (settings.pgschema,),
            ).fetchall()
            print("tables:", ", ".join(t["table_name"] for t in tables) or "(none)")
            has_ledger = any(t["table_name"] == "schema_migrations" for t in tables)
            if has_ledger:
                rows = conn.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
                print("migrations:", ", ".join(r["version"] for r in rows) or "(none)")
            else:
                print("migrations: schema_migrations missing - run scripts/migrate.py upgrade")
    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        print(
            "If the database does not exist, ask a PostgreSQL administrator to run "
            "scripts/setup_databases.sql (superuser).",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
