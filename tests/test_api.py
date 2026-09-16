"""API surface: ingestion, configuration, job creation, cancellation, retry."""

from __future__ import annotations

import io
import json
import uuid

from tests.conftest import make_payload


def test_health_and_evaluator_catalog(client):
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["services"]["postgres"] == "ok"

    evaluators = client.get("/api/v1/evaluators").json()["evaluators"]
    names = {e["evaluator"] for e in evaluators}
    assert {"required_fields", "json_schema", "workflow_order", "llm_judge"} <= names


def test_api_key_is_required(client):
    response = client.get("/api/v1/datasets", headers={"X-API-Key": "wrong"})
    assert response.status_code == 401


def test_payload_insertion_stores_complete_document(client):
    payload = make_payload()
    response = client.post("/api/v1/payloads", json={"payload": payload})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["payload_json"] == payload
    assert body["trace_id"] == payload["trace_data"]["trace_id"]
    assert body["session_id"] == payload["trace_data"]["session_id"]
    assert body["external_payload_id"] == payload["payload_metadata"]["payload_id"]
    assert body["validation_report"]["span_count"] == 3

    detail = client.get(f"/api/v1/payloads/{body['id']}").json()
    assert detail["payload_json"]["spans"][2]["agent_id"] == "summarizer"


def test_dataset_creation_with_payloads_and_export(client):
    response = client.post(
        "/api/v1/datasets",
        json={
            "name": "api-dataset",
            "description": "created without any file",
            "payloads": [make_payload(), make_payload()],
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["payload_count"] == 2
    assert body["dataset"]["record_count"] == 2
    assert body["dataset"]["status"] == "READY"
    assert body["dataset"]["source_path"] is None

    export = client.get(f"/api/v1/datasets/{body['dataset_id']}/export")
    assert export.status_code == 200
    lines = [line for line in export.text.splitlines() if line.strip()]
    assert len(lines) == 2
    assert json.loads(lines[0])["trace_data"]["workflow_id"] == "support_triage"


def test_optional_jsonl_upload_lands_in_postgres(client):
    payloads = [make_payload(), make_payload(category="technical")]
    body = "\n".join(json.dumps(p) for p in payloads).encode("utf-8")
    response = client.post(
        "/api/v1/datasets/upload",
        data={"name": "uploaded", "description": "from file"},
        files={"file": ("traces.jsonl", io.BytesIO(body), "application/x-ndjson")},
    )
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["payload_count"] == 2
    assert result["dataset"]["source_type"] == "jsonl"
    assert result["dataset"]["checksum_sha256"]

    listed = client.get("/api/v1/payloads", params={"dataset_id": result["dataset_id"]}).json()
    assert listed["total"] == 2


def test_invalid_upload_is_rejected(client):
    response = client.post(
        "/api/v1/datasets/upload",
        data={"name": "bad"},
        files={"file": ("traces.jsonl", io.BytesIO(b"{not json}\n"), "application/x-ndjson")},
    )
    assert response.status_code == 400
    assert "invalid JSON" in response.json()["detail"]

    wrong_ext = client.post(
        "/api/v1/datasets/upload",
        data={"name": "bad"},
        files={"file": ("traces.csv", io.BytesIO(b"a,b\n"), "text/csv")},
    )
    assert wrong_ext.status_code == 400


def test_config_validation_endpoint_and_versioning(client, deterministic_config):
    dry_run = client.post(
        "/api/v1/evaluation-configs/validate",
        json={"name": "dry", "config": deterministic_config},
    )
    assert dry_run.status_code == 200
    assert dry_run.json()["checks_count"] == 3

    name = f"cfg-{uuid.uuid4().hex[:6]}"
    first = client.post(
        "/api/v1/evaluation-configs", json={"name": name, "config": deterministic_config}
    ).json()
    second = client.post(
        "/api/v1/evaluation-configs", json={"name": name, "config": deterministic_config}
    ).json()
    assert first["version"] == 1
    assert second["version"] == 2

    conflict = client.post(
        "/api/v1/evaluation-configs",
        json={"name": name, "version": 1, "config": deterministic_config},
    )
    assert conflict.status_code == 409


def test_invalid_config_returns_422(client):
    response = client.post(
        "/api/v1/evaluation-configs",
        json={"name": "broken", "config": {"checks": [{"check_id": "x", "evaluator": "nope"}]}},
    )
    assert response.status_code == 422
    assert "unknown evaluator" in json.dumps(response.json())


def test_job_creation_fans_out_one_ticket_per_payload_and_check(seeded_job, client):
    created = seeded_job["created"]
    assert created["payload_count"] == 2
    assert created["ticket_count"] == 6  # 2 payloads x 3 checks
    assert sorted(created["check_ids"]) == [
        "policy_tool_called",
        "summary_present",
        "workflow_sequence",
    ]

    job = client.get(f"/api/v1/evaluation-jobs/{created['job']['id']}").json()
    assert job["status"] == "READY"
    assert job["total_tickets"] == 6
    assert job["ticket_counts"]["READY"] == 6
    assert job["config_snapshot_json"]["_snapshot"]["config_version"] == 1

    tickets = client.get(f"/api/v1/evaluation-jobs/{created['job']['id']}/tickets").json()
    assert tickets["total"] == 6
    assert {t["status"] for t in tickets["items"]} == {"READY"}
    # Higher priority check is ordered first for claiming.
    assert tickets["items"][0]["check_id"] == "summary_present"
    assert tickets["items"][0]["priority"] == 5


def test_job_snapshot_is_immune_to_later_config_changes(client, deterministic_config, conn):
    name = f"cfg-{uuid.uuid4().hex[:6]}"
    config = client.post(
        "/api/v1/evaluation-configs", json={"name": name, "config": deterministic_config}
    ).json()
    dataset = client.post(
        "/api/v1/datasets", json={"name": "d", "payloads": [make_payload()]}
    ).json()
    job = client.post(
        "/api/v1/evaluation-jobs",
        json={"evaluation_config_id": config["id"], "dataset_id": dataset["dataset_id"]},
    ).json()["job"]

    # A new version of the same configuration must not affect the running job.
    changed = {**deterministic_config, "checks": deterministic_config["checks"][:1]}
    client.post("/api/v1/evaluation-configs", json={"name": name, "config": changed})

    snapshot = client.get(f"/api/v1/evaluation-jobs/{job['id']}").json()["config_snapshot_json"]
    assert len(snapshot["checks"]) == 3


def test_job_requires_dataset_or_payload_ids(client, deterministic_config):
    config = client.post(
        "/api/v1/evaluation-configs",
        json={"name": f"cfg-{uuid.uuid4().hex[:6]}", "config": deterministic_config},
    ).json()
    response = client.post(
        "/api/v1/evaluation-jobs", json={"evaluation_config_id": config["id"]}
    )
    assert response.status_code == 422

    unknown_payload = client.post(
        "/api/v1/evaluation-jobs",
        json={
            "evaluation_config_id": config["id"],
            "payload_ids": [str(uuid.uuid4())],
        },
    )
    assert unknown_payload.status_code == 400
    assert "unknown payload ids" in unknown_payload.json()["detail"]


def test_job_can_target_explicit_payload_ids_with_sampling(client, deterministic_config):
    config = client.post(
        "/api/v1/evaluation-configs",
        json={"name": f"cfg-{uuid.uuid4().hex[:6]}", "config": deterministic_config},
    ).json()
    dataset = client.post(
        "/api/v1/datasets",
        json={"name": "sample", "payloads": [make_payload() for _ in range(4)]},
    ).json()
    created = client.post(
        "/api/v1/evaluation-jobs",
        json={
            "evaluation_config_id": config["id"],
            "dataset_id": dataset["dataset_id"],
            "max_payloads": 2,
        },
    ).json()
    assert created["payload_count"] == 2
    assert created["ticket_count"] == 6


def test_cancellation_cancels_pending_tickets(seeded_job, client):
    job_id = seeded_job["job"]["id"]
    response = client.post(f"/api/v1/evaluation-jobs/{job_id}/cancel")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cancelled_tickets"] == 6
    assert body["job"]["status"] == "CANCELLED"
    assert body["job"]["cancellation_requested_at"] is not None

    summary = client.get(f"/api/v1/evaluation-jobs/{job_id}/tickets/summary").json()
    assert summary["counts"] == {"CANCELLED": 6}

    # Cancelling twice is safe.
    assert client.post(f"/api/v1/evaluation-jobs/{job_id}/cancel").status_code == 200


def test_retry_failed_requires_failed_tickets(seeded_job, client):
    job_id = seeded_job["job"]["id"]
    response = client.post(f"/api/v1/evaluation-jobs/{job_id}/retry-failed")
    assert response.status_code == 409
    assert "no failed tickets" in response.json()["detail"]


def test_unknown_ids_return_404(client):
    missing = uuid.uuid4()
    assert client.get(f"/api/v1/datasets/{missing}").status_code == 404
    assert client.get(f"/api/v1/payloads/{missing}").status_code == 404
    assert client.get(f"/api/v1/evaluation-configs/{missing}").status_code == 404
    assert client.get(f"/api/v1/evaluation-jobs/{missing}").status_code == 404
    assert client.get(f"/api/v1/tickets/{missing}").status_code == 404
    assert client.get(f"/api/v1/results/{missing}").status_code == 404
