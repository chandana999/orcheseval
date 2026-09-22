"""Migrations run against the dedicated database and are idempotent."""

from __future__ import annotations

from sqlalchemy import text

from app.core.config import settings
from app.db.migrate import current, upgrade

EXPECTED_TABLES = {
    "evaluation_jobs",
    "evaluation_tickets",
    "evaluation_results",
    "alembic_version",
}

DROPPED_TABLES = {
    "datasets",
    "evaluation_configs",
    "evaluation_payloads",
    "metric_records",
    "evaluation_profiles",
    "evaluation_profile_metrics",
}


def test_uses_the_dedicated_database_not_evalforge_local():
    assert settings.pgdatabase == "eval_platform_test"
    assert "evalforge_local" not in settings.pgdatabase


def test_all_expected_tables_exist(conn):
    rows = conn.execute(
        text(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = :schema
            """
        ),
        {"schema": settings.pgschema},
    ).mappings()
    names = {r["table_name"] for r in rows}
    assert EXPECTED_TABLES.issubset(names), EXPECTED_TABLES - names
    # The pre-profile tables are gone; nothing reads or writes them.
    assert not (DROPPED_TABLES & names), DROPPED_TABLES & names


def test_migration_ledger_is_complete_and_rerun_is_a_noop():
    assert current() == "0002_job_idempotency"
    assert upgrade() == []


def test_enum_labels_match_the_domain_model(conn):
    from app.models.enums import JobStatus, ResultStatus, TicketStatus

    def labels(enum_name: str) -> set[str]:
        rows = conn.execute(
            text(
                """
                SELECT e.enumlabel AS label
                FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid
                WHERE t.typname = :name
                """
            ),
            {"name": enum_name},
        ).mappings()
        return {r["label"] for r in rows}

    assert labels("evaluation_ticket_status") == {s.value for s in TicketStatus}
    assert labels("evaluation_job_status") == {s.value for s in JobStatus}
    assert labels("evaluation_result_status") == {s.value for s in ResultStatus}
    # Enums that belonged to the dropped tables.
    assert labels("dataset_status") == set()
    assert labels("evaluation_config_status") == set()


def test_claim_and_idempotency_indexes_exist(conn):
    rows = conn.execute(
        text("SELECT indexname FROM pg_indexes WHERE schemaname = :schema"),
        {"schema": settings.pgschema},
    ).mappings()
    names = {r["indexname"] for r in rows}
    assert "ix_tickets_claim" in names
    assert "ix_tickets_lease" in names
    assert "uq_tickets_job_payload_metric" in names
    assert "uq_results_ticket_id" in names
    assert "uq_evaluation_jobs_idempotency_key" in names


def test_jobs_no_longer_reference_evaluation_configs(conn):
    rows = conn.execute(
        text(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = :schema AND table_name = 'evaluation_jobs'
            """
        ),
        {"schema": settings.pgschema},
    ).mappings()
    columns = {r["column_name"] for r in rows}
    assert "dataset_id" in columns
    assert "evaluation_config_id" not in columns
    assert "config_snapshot_json" in columns

    dataset_type = conn.execute(
        text(
            """
            SELECT data_type FROM information_schema.columns
            WHERE table_schema = :schema AND table_name = 'evaluation_jobs'
              AND column_name = 'dataset_id'
            """
        ),
        {"schema": settings.pgschema},
    ).mappings().one()["data_type"]
    assert dataset_type in {"text", "character varying"}


def test_profile_catalog_tables_are_gone(conn):
    rows = conn.execute(
        text(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = :schema
            """
        ),
        {"schema": settings.pgschema},
    ).mappings()
    names = {r["table_name"] for r in rows}
    assert "evaluation_profiles" not in names
    assert "evaluation_profile_metrics" not in names
    assert "metric_records" not in names
