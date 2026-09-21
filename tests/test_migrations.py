"""Migrations run against the dedicated database and are idempotent."""

from __future__ import annotations

from app.core.config import settings
from app.db.migration_runner import status, upgrade

EXPECTED_TABLES = {
    "metric_records",
    "evaluation_profiles",
    "evaluation_profile_metrics",
    "evaluation_jobs",
    "evaluation_tickets",
    "evaluation_results",
    "schema_migrations",
}

DROPPED_TABLES = {
    "datasets",
    "evaluation_configs",
    "evaluation_payloads",
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
    # The pre-profile tables are gone; nothing reads or writes them.
    assert not (DROPPED_TABLES & names), DROPPED_TABLES & names


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
    # Enums that belonged to the dropped tables.
    assert labels("dataset_status") == set()
    assert labels("evaluation_config_status") == set()


def test_claim_and_idempotency_indexes_exist(conn):
    rows = conn.execute(
        "SELECT indexname FROM pg_indexes WHERE schemaname = %s",
        (settings.pgschema,),
    ).fetchall()
    names = {r["indexname"] for r in rows}
    assert "ix_tickets_claim" in names
    assert "ix_tickets_lease" in names
    assert "uq_tickets_job_payload_metric" in names
    assert "uq_metric_records_id_version" in names
    assert "uq_profile_metric_id" in names
    assert "uq_results_ticket_id" in names


def test_jobs_no_longer_reference_evaluation_configs(conn):
    rows = conn.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = %s AND table_name = 'evaluation_jobs'
        """,
        (settings.pgschema,),
    ).fetchall()
    columns = {r["column_name"] for r in rows}
    assert "dataset_id" in columns
    assert "evaluation_config_id" not in columns
    assert "config_snapshot_json" in columns

    dataset_type = conn.execute(
        """
        SELECT data_type FROM information_schema.columns
        WHERE table_schema = %s AND table_name = 'evaluation_jobs' AND column_name = 'dataset_id'
        """,
        (settings.pgschema,),
    ).fetchone()["data_type"]
    assert dataset_type in {"text", "character varying"}


def test_duplicate_profile_metric_mapping_is_rejected(conn):
    import uuid

    from psycopg.errors import UniqueViolation
    from psycopg.types.json import Jsonb

    metric_record_id = uuid.uuid4()
    other_record_id = uuid.uuid4()
    profile_id = uuid.uuid4()
    try:
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO metric_records (
                    metric_record_id, metric_id, metric_code, metric_name,
                    metric_type, metric_version_number, definition_payload
                )
                VALUES
                    (%s, 'm1', 'm1-v1', 'm1', 'DETERMINISTIC', 1, %s),
                    (%s, 'm1', 'm1-v2', 'm1', 'DETERMINISTIC', 2, %s)
                """,
                (
                    metric_record_id,
                    Jsonb({"check_id": "m1", "evaluator": "required_fields"}),
                    other_record_id,
                    Jsonb({"check_id": "m1", "evaluator": "required_fields"}),
                ),
            )
            conn.execute(
                "INSERT INTO evaluation_profiles (id, evaluation_profile_id, name) VALUES (%s, 'p1', 'p1')",
                (profile_id,),
            )
            # Two versions of the same logical metric in one profile must fail.
            conn.execute(
                """
                INSERT INTO evaluation_profile_metrics (
                    id, profile_id, metric_record_id, metric_id
                )
                VALUES (%s, %s, %s, 'm1')
                """,
                (uuid.uuid4(), profile_id, metric_record_id),
            )
            conn.execute(
                """
                INSERT INTO evaluation_profile_metrics (
                    id, profile_id, metric_record_id, metric_id
                )
                VALUES (%s, %s, %s, 'm1')
                """,
                (uuid.uuid4(), profile_id, other_record_id),
            )
    except UniqueViolation:
        return
    raise AssertionError("duplicate profile metric mapping should be rejected")
