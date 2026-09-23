"""Verify PostgreSQL connectivity and report the Alembic revision."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(_ROOT / "src"), str(_ROOT)]

from sqlalchemy import text  # noqa: E402

from evalorch.core.config import settings  # noqa: E402
from evalorch.core.database import get_engine  # noqa: E402
from evalorch.db.migrate import current  # noqa: E402


def main() -> int:
    print(
        f"host={settings.pghost} port={settings.pgport} db={settings.pgdatabase} "
        f"user={settings.pguser} schema={settings.pgschema}"
    )
    try:
        with get_engine().connect() as conn:
            version = conn.execute(text("SELECT version() AS v")).mappings().one()["v"]
            print(f"connected: {version.split(',')[0]}")
            tables = conn.execute(
                text(
                    """
                    SELECT table_name FROM information_schema.tables
                    WHERE table_schema = :schema ORDER BY table_name
                    """
                ),
                {"schema": settings.pgschema},
            ).mappings()
            names = [row["table_name"] for row in tables]
            print("tables:", ", ".join(names) or "(none)")
        print(f"alembic revision: {current() or '(none — run scripts/migrate.py upgrade)'}")
    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
