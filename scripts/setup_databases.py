"""Administrator setup: create the dedicated eval-platform databases.

Equivalent to scripts/setup_databases.sql, for admins without psql on PATH.
Run once with a superuser account; the application never needs these rights.

    set PGADMIN_USER=postgres
    set PGADMIN_PASSWORD=...
    python scripts/setup_databases.py

Safe to re-run: existing databases are left alone and only privileges are
verified. Nothing is dropped, and the evalforge_local databases are untouched.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(_ROOT / "src"), str(_ROOT)]

import psycopg  # noqa: E402
from psycopg import sql  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from evalorch.core.config import settings  # noqa: E402


def admin_conninfo(args: argparse.Namespace, dbname: str) -> str:
    return (
        f"host={args.host} port={args.port} dbname={dbname} "
        f"user={args.admin_user} password={args.admin_password} connect_timeout=10"
    )


def ensure_role(conn: psycopg.Connection, role: str, password: str) -> str:
    exists = conn.execute(
        "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)
    ).fetchone()
    if exists:
        return f"role {role}: already exists (password unchanged)"
    conn.execute(
        sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
            sql.Identifier(role), sql.Literal(password)
        )
    )
    return f"role {role}: created"


def ensure_database(conn: psycopg.Connection, dbname: str, owner: str) -> str:
    exists = conn.execute(
        "SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)
    ).fetchone()
    if exists:
        return f"database {dbname}: already exists (skipped creation)"
    # CREATE DATABASE cannot run inside a transaction block.
    conn.execute(
        sql.SQL("CREATE DATABASE {} OWNER {}").format(
            sql.Identifier(dbname), sql.Identifier(owner)
        )
    )
    return f"database {dbname}: created"


def grant_privileges(conn: psycopg.Connection, dbname: str, role: str) -> None:
    conn.execute(
        sql.SQL("GRANT ALL PRIVILEGES ON DATABASE {} TO {}").format(
            sql.Identifier(dbname), sql.Identifier(role)
        )
    )


def configure_schema(args: argparse.Namespace, dbname: str, role: str) -> None:
    with psycopg.connect(admin_conninfo(args, dbname), autocommit=True) as conn:
        conn.execute(
            sql.SQL("ALTER SCHEMA public OWNER TO {}").format(sql.Identifier(role))
        )
        conn.execute(
            sql.SQL("GRANT ALL ON SCHEMA public TO {}").format(sql.Identifier(role))
        )


def verify(args: argparse.Namespace, databases: list[str], role: str) -> list[str]:
    findings: list[str] = []
    with psycopg.connect(admin_conninfo(args, "postgres"), row_factory=dict_row) as conn:
        for dbname in databases:
            row = conn.execute(
                """
                SELECT has_database_privilege(%s, %s, 'CREATE') AS can_create,
                       has_database_privilege(%s, %s, 'CONNECT') AS can_connect,
                       (SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = %s) AS owner
                """,
                (role, dbname, role, dbname, dbname),
            ).fetchone()
            findings.append(
                f"{dbname}: owner={row['owner']} create={row['can_create']} "
                f"connect={row['can_connect']}"
            )
            if not (row["can_create"] and row["can_connect"]):
                findings.append(f"  WARNING: {role} lacks required privileges on {dbname}")
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create eval-platform databases (superuser)")
    parser.add_argument("--host", default=os.environ.get("PGHOST", settings.pghost))
    parser.add_argument("--port", default=os.environ.get("PGPORT", str(settings.pgport)))
    parser.add_argument("--admin-user", default=os.environ.get("PGADMIN_USER", "postgres"))
    parser.add_argument("--admin-password", default=os.environ.get("PGADMIN_PASSWORD", ""))
    parser.add_argument("--role", default=os.environ.get("PGUSER", settings.pguser))
    parser.add_argument(
        "--role-password", default=os.environ.get("PGPASSWORD", settings.pgpassword)
    )
    parser.add_argument("--database", default=os.environ.get("PGDATABASE", settings.pgdatabase))
    parser.add_argument(
        "--test-database",
        default=os.environ.get("TEST_PGDATABASE", settings.test_pgdatabase),
    )
    args = parser.parse_args(argv)

    databases = [args.database, args.test_database]
    print(f"connecting to {args.host}:{args.port} as {args.admin_user}")
    try:
        with psycopg.connect(admin_conninfo(args, "postgres"), autocommit=True) as conn:
            print(ensure_role(conn, args.role, args.role_password))
            for dbname in databases:
                print(ensure_database(conn, dbname, args.role))
                grant_privileges(conn, dbname, args.role)
                print(f"database {dbname}: privileges granted to {args.role}")
    except psycopg.OperationalError as exc:
        print(f"FAILED to connect as {args.admin_user}: {exc}", file=sys.stderr)
        print(
            "Provide superuser credentials with --admin-user/--admin-password "
            "or PGADMIN_USER/PGADMIN_PASSWORD.",
            file=sys.stderr,
        )
        return 2

    for dbname in databases:
        configure_schema(args, dbname, args.role)

    print("\nverification:")
    for line in verify(args, databases, args.role):
        print(" ", line)
    print("\nNext: python scripts/migrate.py upgrade")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
