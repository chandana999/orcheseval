"""Test fixtures. Tests run against a real PostgreSQL database (eval_platform_test).

Nothing is mocked at the database layer: locking, transactions, enums, and JSONB
behaviour are the things under test.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import pytest

# Point every import at the dedicated test database before app modules load.
os.environ.setdefault("APP_ENV", "test")
os.environ["PGDATABASE"] = os.environ.get("TEST_PGDATABASE", "eval_platform_test")
os.environ.pop("DATABASE_URL", None)
os.environ["API_KEY"] = "test-api-key"
os.environ["LOCAL_STORAGE_ROOT"] = str(
    (Path(__file__).resolve().parents[1] / "data" / "storage-test")
)
os.environ["RUNNER_RETRY_BACKOFF_SECONDS"] = "0,0,0"
os.environ["RATE_LIMIT"] = "10000/minute"

from fastapi.testclient import TestClient  # noqa: E402

from app.core import database  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.db.migration_runner import upgrade  # noqa: E402

API_HEADERS = {"X-API-Key": "test-api-key"}

TABLES = [
    "evaluation_results",
    "evaluation_tickets",
    "evaluation_jobs",
    "evaluation_configs",
    "evaluation_payloads",
    "datasets",
]


@pytest.fixture(scope="session", autouse=True)
def database_ready() -> None:
    """Verify connectivity and apply migrations, or skip with a clear reason."""
    try:
        conn = database.connect()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(
            f"PostgreSQL database '{settings.pgdatabase}' is unavailable ({exc}). "
            "Ask an administrator to run scripts/setup_databases.sql as superuser.",
            allow_module_level=True,
        )
    try:
        upgrade(conn)
    finally:
        conn.close()
    yield
    database.close_pool()


@pytest.fixture(autouse=True)
def clean_tables(database_ready) -> None:
    with database.transaction() as conn:
        conn.execute(f"TRUNCATE {', '.join(TABLES)} CASCADE")
    yield


@pytest.fixture
def conn():
    with database.get_pool().connection() as connection:
        yield connection


@pytest.fixture
def client() -> TestClient:
    from app.main import app

    with TestClient(app) as test_client:
        test_client.headers.update(API_HEADERS)
        yield test_client


# --------------------------------------------------------------------- payloads
def make_payload(
    *,
    payload_id: str | None = None,
    category: str = "billing",
    summary: str = "Customer asked about a duplicate charge and was refunded.",
    include_summarizer: bool = True,
    tool_response: bool = True,
    spans_out_of_order: bool = False,
) -> dict[str, Any]:
    """A representative multi-agent execution payload."""
    identifier = payload_id or f"pl-{uuid.uuid4().hex[:8]}"
    classifier = {
        "span_id": f"{identifier}-s1",
        "agent_id": "classifier",
        "agent_type": "classifier",
        "order": 1,
        "start_time": "2026-01-05T10:00:00Z",
        "output": {"category": category, "confidence": 0.91},
        "tool_calls": [],
    }
    validator = {
        "span_id": f"{identifier}-s2",
        "agent_id": "validator",
        "agent_type": "validator",
        "order": 2,
        "start_time": "2026-01-05T10:00:02Z",
        "output": {"is_valid": True},
        "tool_calls": [
            {
                "name": "policy_lookup",
                "arguments": {"category": category},
                **({"response": {"policy": "refund_within_30_days"}} if tool_response else {}),
            }
        ],
    }
    summarizer = {
        "span_id": f"{identifier}-s3",
        "agent_id": "summarizer",
        "agent_type": "summarizer",
        "order": 3,
        "start_time": "2026-01-05T10:00:05Z",
        "output": {"summary": summary, "category": category},
        "tool_calls": [],
    }
    spans = [classifier, validator]
    if include_summarizer:
        spans.append(summarizer)
    if spans_out_of_order:
        spans = list(reversed(spans))

    return {
        "payload_metadata": {"payload_id": identifier, "version": "2026-01"},
        "trace_data": {
            "trace_id": f"tr-{identifier}",
            "session_id": f"se-{identifier}",
            "workflow_id": "support_triage",
        },
        "session_context": {
            "session_id": f"se-{identifier}",
            "conversation_history": [
                {"role": "user", "content": "I was charged twice for my subscription."},
                {"role": "assistant", "content": "I can refund the duplicate charge."},
            ],
            "final_response": "Your duplicate charge has been refunded.",
        },
        "spans": spans,
        "resolved_configuration": {"model": "gpt-4o-mini", "temperature": 0},
    }


DETERMINISTIC_CONFIG: dict[str, Any] = {
    "evaluation_type": "agent_workflow",
    "evaluation_version": "1",
    "target_agents": ["classifier", "validator", "summarizer"],
    "workflow_order": ["classifier", "validator", "summarizer"],
    "on_missing_context": "not_applicable",
    "checks": [
        {
            "check_id": "summary_present",
            "evaluator": "required_fields",
            "input_mapping": {"summary": "summarizer.output.summary"},
            "params": {"fields": ["summary"]},
            "priority": 5,
        },
        {
            "check_id": "workflow_sequence",
            "evaluator": "workflow_order",
            "input_mapping": {"spans": "spans"},
            "params": {
                "expected_order": ["classifier", "validator", "summarizer"],
                "mode": "subsequence",
            },
        },
        {
            "check_id": "policy_tool_called",
            "evaluator": "tool_calls",
            "input_mapping": {"tool_calls": "validator.tool_calls"},
            "params": {"expected_tools": ["policy_lookup"], "require_response": True},
        },
    ],
}


@pytest.fixture
def deterministic_config() -> dict[str, Any]:
    import copy

    return copy.deepcopy(DETERMINISTIC_CONFIG)


@pytest.fixture
def seeded_job(client: TestClient, deterministic_config: dict[str, Any]):
    """Create a config, a dataset with two payloads, and a job over them."""
    config_response = client.post(
        "/api/v1/evaluation-configs",
        json={"name": f"cfg-{uuid.uuid4().hex[:6]}", "config": deterministic_config},
    )
    assert config_response.status_code == 201, config_response.text
    config = config_response.json()

    dataset_response = client.post(
        "/api/v1/datasets",
        json={
            "name": "support-traces",
            "payloads": [make_payload(), make_payload(category="technical")],
        },
    )
    assert dataset_response.status_code == 201, dataset_response.text
    dataset = dataset_response.json()

    job_response = client.post(
        "/api/v1/evaluation-jobs",
        json={
            "evaluation_config_id": config["id"],
            "dataset_id": dataset["dataset_id"],
            "name": "nightly",
        },
    )
    assert job_response.status_code == 201, job_response.text
    return {
        "config": config,
        "dataset": dataset,
        "job": job_response.json()["job"],
        "created": job_response.json(),
    }
