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
from sqlalchemy import text  # noqa: E402

from evalorch.core import database  # noqa: E402
from evalorch.core.config import settings  # noqa: E402
from evalorch.db.migrate import upgrade  # noqa: E402
API_HEADERS = {"X-API-Key": "test-api-key"}

DEFAULT_AGENT_ID = "3f1a8c2e-6b44-4d7a-9e10-2c8b5a7d6e01"
OTHER_AGENT_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

TABLES = [
    "evaluation_results",
    "evaluation_tickets",
    "evaluation_jobs",
]


@pytest.fixture(scope="session", autouse=True)
def database_ready() -> None:
    """Verify connectivity and apply migrations, or skip with a clear reason."""
    try:
        upgrade()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(
            f"PostgreSQL database '{settings.pgdatabase}' is unavailable ({exc}). "
            "Ask an administrator to run scripts/setup_databases.sql as superuser.",
            allow_module_level=True,
        )
    yield
    database.close_pool()


@pytest.fixture(autouse=True)
def clean_tables(database_ready) -> None:
    with database.transaction() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(TABLES)} CASCADE"))
    yield


@pytest.fixture
def conn():
    session = database.open_session()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def temp_root(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(settings, "evaluation_temp_root", str(tmp_path))
    return tmp_path


@pytest.fixture
def client() -> TestClient:
    from evalorch.main import app

    with TestClient(app) as test_client:
        test_client.headers.update(API_HEADERS)
        yield test_client


def _hex_id(seed: str, length: int) -> str:
    return uuid.uuid5(uuid.NAMESPACE_URL, seed).hex[:length]


def _span(
    *,
    name: str,
    span_id: str,
    parent_span_id: str | None,
    trace_id: str,
    started_at: str,
    attributes: dict[str, Any],
) -> dict[str, Any]:
    return {
        "span_id": span_id,
        "trace_id": trace_id,
        "parent_span_id": parent_span_id,
        "name": name,
        "kind": "internal",
        "started_at": started_at,
        "ended_at": started_at,
        "duration_ms": 100,
        "status": "ok",
        "error_message": None,
        "attributes": attributes,
        "events": [],
        "links": [],
    }


def make_payload(
    *,
    payload_id: str | None = None,
    category: str = "billing",
    summary: str = "Customer asked about a duplicate charge and was refunded.",
    include_summarizer: bool = True,
    tool_response: bool = True,
    spans_out_of_order: bool = False,
    agent_id: str | None = DEFAULT_AGENT_ID,
    include_agent_registry: bool = True,
) -> dict[str, Any]:
    """One execution payload using trace_context.trace.spans."""
    seed = payload_id or uuid.uuid4().hex
    trace_id = _hex_id(f"{seed}:trace", 32)
    classifier_id = _hex_id(f"{seed}:classifier", 16)
    validator_id = _hex_id(f"{seed}:validator", 16)
    summarizer_id = _hex_id(f"{seed}:summarizer", 16)
    times = ["2026-01-05T10:00:00Z", "2026-01-05T10:00:02Z", "2026-01-05T10:00:05Z"]
    tool_call: dict[str, Any] = {
        "name": "policy_lookup",
        "arguments": {"category": category},
    }
    if tool_response:
        tool_call["response"] = {"policy": "refund_within_30_days"}
    conversation = [
        {"role": "user", "content": "I was charged twice for my subscription."},
        {"role": "assistant", "content": "I can refund the duplicate charge."},
    ]
    spans = [
        _span(
            name="classifier",
            span_id=classifier_id,
            parent_span_id=None,
            trace_id=trace_id,
            started_at=times[0],
            attributes={
                "output": {"category": category, "confidence": 0.91},
                "tool_calls": [],
                "conversation_history": conversation,
                "final_response": "Your duplicate charge has been refunded.",
            },
        ),
        _span(
            name="validator",
            span_id=validator_id,
            parent_span_id=classifier_id,
            trace_id=trace_id,
            started_at=times[1],
            attributes={
                "output": {"is_valid": True},
                "tool_calls": [tool_call],
            },
        ),
    ]
    if include_summarizer:
        spans.append(
            _span(
                name="summarizer",
                span_id=summarizer_id,
                parent_span_id=validator_id,
                trace_id=trace_id,
                started_at=times[2],
                attributes={
                    "output": {"summary": summary, "category": category},
                    "tool_calls": [],
                },
            )
        )
    if spans_out_of_order:
        spans = list(reversed(spans))
    session_id = str(uuid.uuid4())
    invocation_id = str(uuid.uuid4())
    payload: dict[str, Any] = {
        "payload_metadata": {
            "payload_id": payload_id or str(uuid.uuid4()),
            "payload_version": "1.0",
            "payload_type": "agent_trace",
            "created_at": "2026-01-05T10:00:06Z",
            "evaluation_mode": "offline",
        },
        "correlation": {
            "event_id": str(uuid.uuid4()),
            "trace_id": trace_id,
            "session_id": session_id,
            "invocation_id": invocation_id,
            "user_id": "cus-8812",
            "app_name": "support-triage",
            "service_name": "support-triage-agent",
            "agent_id": agent_id,
        },
        "trace_context": {
            "trace": {
                "trace_id": trace_id,
                "started_at_raw": times[0],
                "ended_at_raw": "2026-01-05T10:00:06Z",
                "duration_ms": 6000,
                "service_name": "support-triage-agent",
                "root_span_name_raw": "classifier",
                "status": "ok",
                "error_message": None,
                "session_id": session_id,
                "invocation_id": invocation_id,
                "user_id": "cus-8812",
                "app_name": "support-triage",
                "spans": spans,
            }
        },
    }
    if include_agent_registry:
        payload["agent_registry"] = {"agent_id": agent_id}
    return payload


def metric(
    metric_id: str,
    plugin: str,
    mapping: dict[str, Any],
    params: dict[str, Any],
    *,
    metric_type: str = "deterministic",
    span_names: list[str] | None = None,
    weight: int = 0,
    **extra: Any,
) -> dict[str, Any]:
    document = {
        "metricId": metric_id,
        "metricName": metric_id,
        "metricVersion": "1.0",
        "metricType": metric_type,
        "evaluatorPluginId": plugin,
        "enabled": True,
        "spanNames": span_names or [],
        "thresholds": {"pass": 1.0, "fail": 0.0},
        "weight": weight,
        "inputMapping": mapping,
        "params": params,
    }
    document.update(extra)
    return document


DETERMINISTIC_METRICS: list[dict[str, Any]] = [
    metric(
        "summary_present",
        "required_fields",
        {"summary": "summarizer.attributes.output.summary"},
        {"fields": ["summary"]},
        span_names=["summarizer"],
        weight=5,
    ),
    metric(
        "workflow_sequence",
        "workflow_order",
        {"spans": "spans"},
        {
            "expected_order": ["classifier", "validator", "summarizer"],
            "mode": "subsequence",
        },
        span_names=["classifier", "validator"],
    ),
    metric(
        "policy_tool_called",
        "tool_calls",
        {"tool_calls": "validator.attributes.tool_calls"},
        {"expected_tools": ["policy_lookup"], "require_response": True},
        span_names=["validator"],
    ),
]


DETERMINISTIC_CONFIG: dict[str, Any] = {
    "agentId": DEFAULT_AGENT_ID,
    "metrics": DETERMINISTIC_METRICS,
}


@pytest.fixture
def deterministic_config() -> dict[str, Any]:
    import copy

    return copy.deepcopy(DETERMINISTIC_CONFIG)


def write_dataset(
    root: Path,
    dataset_id: str,
    payloads: list[dict[str, Any]],
    *,
    metrics: list[dict[str, Any]] | None = None,
    config: dict[str, Any] | None = None,
    agent_id: str = DEFAULT_AGENT_ID,
) -> Path:
    folder = root / dataset_id
    folder.mkdir(parents=True, exist_ok=True)
    for index, payload in enumerate(payloads, start=1):
        (folder / f"payload_{index:03d}.json").write_text(json.dumps(payload), encoding="utf-8")
    document = config or {"agentId": agent_id, "metrics": metrics or DETERMINISTIC_METRICS}
    (folder / "config.json").write_text(json.dumps(document), encoding="utf-8")
    return folder


@pytest.fixture
def seeded_job(client: TestClient, temp_root: Path, deterministic_config: dict[str, Any]):
    """Two payload files and a config with three checks, then a job over them."""
    dataset_id = f"dataset-{uuid.uuid4().hex[:8]}"
    write_dataset(
        temp_root,
        dataset_id,
        [make_payload(), make_payload(category="technical")],
        metrics=deterministic_config["metrics"],
    )

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
