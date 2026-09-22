"""Apply Alembic migrations. Existing hand-built schemas are stamped, not rebuilt."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from app.core.config import settings
from app.core.database import get_engine

ROOT = Path(__file__).resolve().parents[2]
HEAD = "0002_job_idempotency"


def alembic_config() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", settings.sqlalchemy_url)
    return cfg


def upgrade() -> list[str]:
    """Upgrade to head. Stamp when the final tables are already present."""
    cfg = alembic_config()
    engine = get_engine()
    tables = set(inspect(engine).get_table_names())
    if "alembic_version" not in tables and "evaluation_jobs" in tables:
        command.stamp(cfg, "0001_initial")
    command.upgrade(cfg, "head")
    return []


def current() -> str | None:
    from alembic.runtime.migration import MigrationContext

    with get_engine().connect() as connection:
        context = MigrationContext.configure(connection)
        return context.get_current_revision()


def main(argv: list[str] | None = None) -> int:
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    action = args[0] if args else "upgrade"
    if action == "upgrade":
        applied = upgrade()
        print(f"database {settings.pgdatabase}: {applied or 'up to date'} ({current()})")
        return 0
    if action == "status":
        print(f"database {settings.pgdatabase}: revision {current()}")
        return 0
    print("usage: python scripts/migrate.py [upgrade|status]", file=sys.stderr)
    return 2
