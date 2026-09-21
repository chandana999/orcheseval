"""Test fixtures. Tests run against a real PostgreSQL database (eval_platform_test).

Nothing is mocked at the database layer: locking, transactions, enums, and JSONB
behaviour are the things under test.
"""

from __future__ import annotations

import json
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
os.environ["EVALUATION_TEMP_ROOT"] = str(
    (Path(__file__).resolve().parents[1] / "data" / "temp-test")
)
os.environ["RUNNER_RETRY_BACKOFF_SECONDS"] = "0,0,0"
os.environ["RATE_LIMIT"] = "10000/minute"

from fastapi.testclient import TestClient  # noqa: E402

from app.core import database  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.db.migration_runner import upgrade  # noqa: E402
from app.models.entities import EvaluationProfile, MetricRecord  # noqa: E402
from app.models.enums import CheckType  # noqa: E402
from app.repositories.metric_repository import MetricRepository  # noqa: E402
from app.repositories.profile_repository import ProfileRepository  # noqa: E402

API_HEADERS = {"X-API-Key": "test-api-key"}

DEFAULT_PROFILE_ID = "agent-response-quality-v1"

TABLES = [
    "evaluation_results",
    "evaluation_tickets",
    "evaluation_jobs",
    "evaluation_profile_metrics",
    "evaluation_profiles",
    "metric_records",
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
def temp_root(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(settings, "evaluation_temp_root", str(tmp_path))
    return tmp_path


@pytest.fixture
def client() -> TestClient:
    from app.main import app

    with TestClient(app) as test_client:
        test_client.headers.update(API_HEADERS)
        yield test_client


def make_payload(
    *,
    payload_id: str | None = None,
    category: str = "billing",
    summary: str = "Customer asked about a duplicate charge and was refunded.",
    include_summarizer: bool = True,
    tool_response: bool = True,
    spans_out_of_order: bool = False,
    evaluation_profile_id: str | None = DEFAULT_PROFILE_ID,
    include_agent_registry: bool = True,
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

    payload: dict[str, Any] = {
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
    if include_agent_registry:
        payload["agent_registry"] = {
            "agent_id": "support-triage-agent",
            "agent_name": "Support Triage Agent",
            "agent_version": "1.0",
            "evaluation_profile_id": evaluation_profile_id,
        }
    return payload


DETERMINISTIC_CHECKS: list[dict[str, Any]] = [
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
]


DETERMINISTIC_CONFIG: dict[str, Any] = {
    "evaluation_type": "agent_workflow",
    "evaluation_version": "1",
    "target_agents": ["classifier", "validator", "summarizer"],
    "workflow_order": ["classifier", "validator", "summarizer"],
    "on_missing_context": "not_applicable",
    "checks": DETERMINISTIC_CHECKS,
}


@pytest.fixture
def deterministic_config() -> dict[str, Any]:
    import copy

    return copy.deepcopy(DETERMINISTIC_CONFIG)


def write_dataset(root: Path, dataset_id: str, payloads: list[dict[str, Any]]) -> Path:
    folder = root / dataset_id
    folder.mkdir(parents=True, exist_ok=True)
    for index, payload in enumerate(payloads, start=1):
        (folder / f"payload_{index:03d}.json").write_text(json.dumps(payload), encoding="utf-8")
    return folder


def seed_metric(
    conn,
    check: dict[str, Any],
    *,
    metric_id: str | None = None,
    version: int = 1,
    active: bool = True,
) -> MetricRecord:
    check_id = check["check_id"]
    evaluator = check.get("evaluator") or (
        "llm_judge"
        if str(check.get("check_type", "")).upper() == "LLM_JUDGE"
        else "required_fields"
    )
    metric_type = (
        CheckType.LLM_JUDGE.value
        if evaluator == "llm_judge" or str(check.get("check_type", "")).upper() == "LLM_JUDGE"
        else CheckType.DETERMINISTIC.value
    )
    record = MetricRecord(
        metric_record_id=uuid.uuid4(),
        metric_id=metric_id or check_id,
        metric_code=check_id,
        metric_name=check_id.replace("_", " "),
        metric_desc=check.get("description"),
        metric_type=metric_type,
        metric_version_number=version,
        definition_payload=dict(check),
        default_threshold_operator=None,
        llm_model_name=None,
        llm_model_version=None,
        llm_deployed_id=None,
        is_active_indicator=active,
        previous_metric_record_id=None,
        change_summary=None,
    )
    return MetricRepository(conn).insert(record)


def seed_profile(
    conn,
    records: list[MetricRecord],
    *,
    profile_id: str = DEFAULT_PROFILE_ID,
    active: bool = True,
    name: str | None = None,
) -> EvaluationProfile:
    profiles = ProfileRepository(conn)
    profile = profiles.insert(
        EvaluationProfile(
            evaluation_profile_id=profile_id,
            name=name or profile_id,
            version=1,
            description=None,
            is_active=active,
        )
    )
    for index, record in enumerate(records):
        profiles.add_metric(
            profile_id=profile.id,
            metric_record_id=record.metric_record_id,
            metric_id=record.metric_id,
            execution_order=index,
        )
    return profile


def seed_default_profile(conn, checks: list[dict[str, Any]] | None = None) -> list[MetricRecord]:
    records = [seed_metric(conn, check) for check in (checks or DETERMINISTIC_CHECKS)]
    seed_profile(conn, records)
    return records


@pytest.fixture
def seeded_job(client: TestClient, temp_root: Path, deterministic_config: dict[str, Any]):
    """Create a profile with three metrics, two payload files, and a job over them."""
    with database.transaction() as conn:
        seed_default_profile(conn, deterministic_config["checks"])

    dataset_id = f"dataset-{uuid.uuid4().hex[:8]}"
    write_dataset(temp_root, dataset_id, [make_payload(), make_payload(category="technical")])

    job_response = client.post(
        "/v1/evaluation-jobs", json={"dataset_id": dataset_id, "name": "nightly"}
    )
    assert job_response.status_code == 201, job_response.text
    return {
        "dataset_id": dataset_id,
        "temp_root": temp_root,
        "job": job_response.json()["job"],
        "created": job_response.json(),
    }
