"""Error envelope, request id, retry classification, and configuration."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app

from app.core.config import Settings
from app.core.database import transaction
from app.models.enums import ErrorClass
from app.services.errors import classify_error
from app.services.ticket_service import claim_tickets, heartbeat_ticket


def _error(response):
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "request_id"}
    return body["error"]


def test_not_found_and_auth_use_the_error_envelope(client):
    missing = client.get(f"/v1/evaluation-jobs/{uuid.uuid4()}")
    assert missing.status_code == 404
    error = _error(missing)
    assert error["code"] == "NOT_FOUND"
    assert error["request_id"] == missing.headers["X-Request-ID"]

    denied = client.get("/v1/evaluation-jobs", headers={"X-API-Key": "wrong"})
    assert denied.status_code == 401
    error = _error(denied)
    assert error["code"] == "UNAUTHORIZED"
    assert "traceback" not in denied.text.lower()


def test_conflict_uses_the_error_envelope(seeded_job, client):
    response = client.post(f"/v1/evaluation-jobs/{seeded_job['job']['id']}/retry-failed")
    assert response.status_code == 409
    error = _error(response)
    assert error["code"] == "CONFLICT"
    assert "no failed tickets" in error["message"]


def test_validation_error_does_not_echo_input(client):
    secret = "super-secret-dataset-value"
    response = client.post("/v1/evaluation-jobs", json={"dataset_id": [secret]})
    assert response.status_code == 422
    error = _error(response)
    assert error["code"] == "VALIDATION_ERROR"
    assert error["message"] == "Request validation failed."
    assert secret not in response.text


def test_value_error_and_unexpected_errors_hide_internals(client, monkeypatch):
    def bad_value(*_args, **_kwargs):
        raise ValueError("password=hunter2 SELECT secret FROM C:\\private\\eval.sql")

    monkeypatch.setattr("app.api.jobs.create_job", bad_value)
    invalid = client.post("/v1/evaluation-jobs", json={"dataset_id": "folder-1"})
    assert invalid.status_code == 400
    assert _error(invalid)["code"] == "BAD_REQUEST"
    assert "hunter2" not in invalid.text
    assert "SELECT" not in invalid.text
    assert "private" not in invalid.text

    class BoomRepository:
        def __init__(self, _session):
            pass

        def list(self, **_kwargs):
            raise RuntimeError("password=hunter2 SELECT secret FROM C:\\private\\eval.sql")

    monkeypatch.setattr("app.api.jobs.JobRepository", BoomRepository)
    with TestClient(app, raise_server_exceptions=False) as quiet:
        quiet.headers.update({"X-API-Key": "test-api-key"})
        failed = quiet.get("/v1/evaluation-jobs")
    assert failed.status_code == 500
    error = _error(failed)
    assert error["code"] == "INTERNAL_SERVER_ERROR"
    assert error["message"] == "An unexpected internal error occurred."
    assert error["request_id"] == failed.headers["X-Request-ID"]
    assert "hunter2" not in failed.text
    assert "SELECT" not in failed.text
    assert "Traceback" not in failed.text
    assert "RuntimeError" not in failed.text


def test_readiness_failure_does_not_expose_the_database_error(client, monkeypatch):
    monkeypatch.setattr(
        "app.api.health.healthcheck",
        lambda: (False, "password=hunter2 postgresql://user:secret@localhost SELECT"),
    )
    ready = client.get("/health/ready")
    assert ready.status_code == 503
    assert ready.json() == {"status": "not_ready", "postgres": "unavailable"}
    assert "hunter2" not in ready.text
    assert "SELECT" not in ready.text
    assert client.get("/health/live").json()["status"] == "alive"


def test_request_id_is_generated_and_preserved(client):
    generated = client.get(f"/v1/evaluation-jobs/{uuid.uuid4()}")
    request_id = generated.headers["X-Request-ID"]
    assert request_id
    assert _error(generated)["request_id"] == request_id

    preserved = client.get(
        f"/v1/evaluation-jobs/{uuid.uuid4()}",
        headers={"X-Request-ID": "req-123"},
    )
    assert preserved.headers["X-Request-ID"] == "req-123"
    assert _error(preserved)["request_id"] == "req-123"


def test_request_id_is_on_request_logs(client, capsys):
    response = client.post("/v1/evaluation-jobs", json={"dataset_id": "does-not-exist"})
    assert response.status_code == 404
    logs = capsys.readouterr().out
    assert "job_creation_failed" in logs
    assert '"service": "eval-platform"' in logs
    assert '"environment": "test"' in logs
    assert response.headers["X-Request-ID"] in logs


def test_cancel_and_lease_events_are_logged(seeded_job, client, capsys):
    with transaction() as conn:
        ticket = claim_tickets(conn, worker_id="w-1", limit=1, lease_seconds=30)[0]
    with transaction() as conn:
        extended = heartbeat_ticket(conn, ticket.id, worker_id="w-1", lease_seconds=120)
    assert extended is not None
    client.post(f"/v1/evaluation-jobs/{seeded_job['job']['id']}/cancel")
    logs = capsys.readouterr().out
    assert "lease_extended" in logs
    assert "job_cancel_requested" in logs
    assert '"service": "eval-platform"' in logs


def test_programming_errors_are_permanent_and_timeouts_stay_transient():
    assert classify_error(AttributeError("missing")) is ErrorClass.PERMANENT
    assert classify_error(TypeError("bad")) is ErrorClass.PERMANENT
    assert classify_error(KeyError("missing")) is ErrorClass.PERMANENT
    assert classify_error(LookupError("missing")) is ErrorClass.PERMANENT
    assert classify_error(TimeoutError()) is ErrorClass.TRANSIENT


@pytest.mark.parametrize(
    "kwargs",
    [
        {"pgport": 0},
        {"pgport": 70000},
        {"runner_max_concurrency": 0},
        {"runner_claim_batch_size": 0},
        {"runner_lease_seconds": 0},
        {"runner_max_attempts": 0},
        {"runner_retry_backoff_seconds": "nope"},
        {"runner_recovery_interval_seconds": 0},
        {"db_statement_timeout_ms": -1},
        {"db_idle_in_transaction_timeout_ms": -1},
    ],
)
def test_invalid_configuration_fails_fast(kwargs):
    with pytest.raises(ValidationError):
        Settings(**kwargs)


def _without_api_key(client):
    return {"X-API-Key": ""}


def test_api_key_comparison_uses_compare_digest(monkeypatch):
    import secrets

    from app.core.security import verify_api_key

    seen: dict[str, tuple[str, str]] = {}
    original = secrets.compare_digest

    def _compare(left: str, right: str) -> bool:
        seen["args"] = (left, right)
        return original(left, right)

    monkeypatch.setattr("app.core.security.secrets.compare_digest", _compare)
    import asyncio

    assert asyncio.run(verify_api_key("test-api-key")) == "test-api-key"
    assert seen["args"] == ("test-api-key", "test-api-key")


def test_authentication_fail_closed_matrix(client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "api_key", "test-api-key")
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "allow_unauthenticated_dev", False)
    assert client.get("/v1/evaluation-jobs").status_code == 200
    assert client.get("/v1/evaluation-jobs", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/v1/evaluation-jobs", headers=_without_api_key(client)).status_code == 401

    monkeypatch.setattr(settings, "api_key", "")
    missing = client.get("/v1/evaluation-jobs", headers=_without_api_key(client))
    assert missing.status_code == 503
    assert missing.json()["error"]["code"] == "SERVICE_UNAVAILABLE"

    monkeypatch.setattr(settings, "app_env", "local")
    monkeypatch.setattr(settings, "allow_unauthenticated_dev", False)
    still_closed = client.get("/v1/evaluation-jobs", headers=_without_api_key(client))
    assert still_closed.status_code == 503

    monkeypatch.setattr(settings, "allow_unauthenticated_dev", True)
    opened = client.get("/v1/evaluation-jobs", headers=_without_api_key(client))
    assert opened.status_code == 200


def test_development_auth_bypass_logs_a_startup_warning(monkeypatch, capsys):
    from app.core.config import settings

    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "app_env", "local")
    monkeypatch.setattr(settings, "allow_unauthenticated_dev", True)
    with TestClient(app) as bypass_client:
        response = bypass_client.get("/v1/evaluation-jobs")
    assert response.status_code == 200
    logs = capsys.readouterr().out
    assert "api_auth_bypass_active" in logs
    assert "test-api-key" not in logs


def test_postgres_statement_timeouts_are_applied(monkeypatch):
    from sqlalchemy import text

    from app.core import database
    from app.core.config import settings

    monkeypatch.setattr(settings, "db_statement_timeout_ms", 45000)
    monkeypatch.setattr(settings, "db_idle_in_transaction_timeout_ms", 20000)
    database.reset_pool()
    try:
        with database.get_engine().connect() as connection:
            statement_timeout = connection.execute(text("SHOW statement_timeout")).scalar_one()
            idle_timeout = connection.execute(
                text("SHOW idle_in_transaction_session_timeout")
            ).scalar_one()
    finally:
        database.reset_pool()

    assert _postgres_timeout_ms(statement_timeout) == 45000
    assert _postgres_timeout_ms(idle_timeout) == 20000


def _postgres_timeout_ms(shown: str) -> int:
    value = str(shown).strip()
    if value in {"0", "0ms"}:
        return 0
    if value.endswith("ms"):
        return int(float(value[:-2]))
    if value.endswith("s"):
        return int(float(value[:-1]) * 1000)
    if ":" in value:
        hours, minutes, seconds = value.split(":")
        total = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        return int(total * 1000)
    return int(float(value))


def test_structured_logs_redact_secrets_and_keep_context(capsys):
    from app.core.logging import get_logger

    log = get_logger("redaction-test")
    log.info(
        "redaction_probe",
        detail="password=hunter2",
        database_url="postgresql://user:secret@localhost/eval",
        request_id="req-keep",
        job_id="job-keep",
        ticket_count=12,
    )
    try:
        raise RuntimeError("api_key=super-secret-value")
    except RuntimeError:
        log.exception("redaction_exception", worker_id="w-1")

    logs = capsys.readouterr().out
    assert "hunter2" not in logs
    assert "super-secret-value" not in logs
    assert "user:secret" not in logs
    assert "password=***" in logs
    assert "req-keep" in logs
    assert "job-keep" in logs
    assert "w-1" in logs
    assert '"ticket_count": 12' in logs
    assert "RuntimeError" in logs
    assert "Traceback" in logs
