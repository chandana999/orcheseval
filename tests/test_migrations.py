"""Migrations run against the dedicated database and are idempotent."""

from __future__ import annotations

from app.core.config import settings
from app.db.migration_runner import status, upgrade

EXPECTED_TABLES = {
    "datasets",
    "evaluation_configs",
    "evaluation_jobs",
    "evaluation_payloads",
    "evaluation_results",
    "evaluation_tickets",
    "schema_migrations",
}


def test_uses_the_dedicated_database_not_evalforge_local():
    assert settings.pgdatabase == "eval_platform_test"
    assert "evalforge_local" not in settings.pgdatabase


def test_all_expected_tables_exist(conn):
    rows = conn.execute(
        """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = %s
        """,
        (settings.pgschema,),
    ).fetchall()
    names = {r["table_name"] for r in rows}
    assert EXPECTED_TABLES.issubset(names), EXPECTED_TABLES - names


def test_migration_ledger_is_complete_and_rerun_is_a_noop():
    rows = status()
    assert rows, "no migration files found"
    assert all(r["state"] == "applied" for r in rows), rows
    assert upgrade() == []


def test_enum_labels_match_the_domain_model(conn):
    from app.models.enums import JobStatus, ResultStatus, TicketStatus

    def labels(enum_name: str) -> set[str]:
        rows = conn.execute(
            """
            SELECT e.enumlabel AS label
            FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid
            WHERE t.typname = %s
            """,
            (enum_name,),
        ).fetchall()
        return {r["label"] for r in rows}

    assert labels("evaluation_ticket_status") == {s.value for s in TicketStatus}
    assert labels("evaluation_job_status") == {s.value for s in JobStatus}
    assert labels("evaluation_result_status") == {s.value for s in ResultStatus}


def test_claim_and_idempotency_indexes_exist(conn):
    rows = conn.execute(
        "SELECT indexname FROM pg_indexes WHERE schemaname = %s",
        (settings.pgschema,),
    ).fetchall()
    names = {r["indexname"] for r in rows}
    assert "ix_tickets_claim" in names
    assert "ix_tickets_lease" in names
    assert "uq_results_job_payload_check" in names
    assert "uq_tickets_job_payload_check" in names
    assert "ix_payloads_payload_json" in names


def test_payload_json_must_be_an_object(conn):
    import uuid

    from psycopg.errors import CheckViolation
    from psycopg.types.json import Jsonb

    try:
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO evaluation_payloads (id, payload_json, source_type)
                VALUES (%s, %s, 'api')
                """,
                (uuid.uuid4(), Jsonb([1, 2, 3])),
            )
    except CheckViolation:
        pass
    else:  # pragma: no cover - constraint regression
        raise AssertionError("array payload_json should violate ck_payloads_json_object")
