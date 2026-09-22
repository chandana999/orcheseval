"""API surface: job creation from a temp folder, status, tickets, cancellation."""

from __future__ import annotations

import json
import uuid

from tests.conftest import (
    DEFAULT_AGENT_ID,
    make_payload,
    write_dataset,
)


def test_health_and_evaluator_catalog(client):
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["services"]["postgres"] == "ok"

    evaluators = client.get("/v1/evaluators").json()["evaluators"]
    names = {e["evaluator"] for e in evaluators}
    assert {"required_fields", "json_schema", "workflow_order", "llm_judge"} <= names


def test_api_key_is_required(client):
    response = client.get("/v1/evaluation-jobs", headers={"X-API-Key": "wrong"})
    assert response.status_code == 401


def test_dataset_payload_and_config_apis_no_longer_exist(client):
    assert client.post("/v1/datasets", json={"name": "x"}).status_code == 404
    assert client.post("/v1/payloads", json={"payload": {}}).status_code == 404
    assert client.get("/v1/evaluation-configs").status_code == 404


def test_job_creation_fans_out_one_ticket_per_payload_and_metric(seeded_job, client):
    created = seeded_job["created"]
    assert created["payload_count"] == 2
    assert created["ticket_count"] == 6  # 2 payloads x 3 metrics
    assert created["dataset_id"] == seeded_job["dataset_id"]
    assert sorted(created["metric_ids"]) == [
        "policy_tool_called",
        "summary_present",
        "workflow_sequence",
    ]
    assert created["profiles"] == [DEFAULT_AGENT_ID]
    assert created["tickets_summary"]["by_agent"][DEFAULT_AGENT_ID] == 6

    job = client.get(f"/v1/evaluation-jobs/{created['job']['id']}").json()
    assert job["status"] == "READY"
    assert job["total_tickets"] == 6
    assert job["ticket_counts"]["READY"] == 6
    assert job["dataset_id"] == seeded_job["dataset_id"]
    assert "evaluation_config_id" not in job

    tickets = client.get(f"/v1/evaluation-jobs/{created['job']['id']}/tickets").json()
    assert tickets["total"] == 6
    assert {t["status"] for t in tickets["items"]} == {"READY"}
    assert tickets["items"][0]["check_id"] == "summary_present"
    assert tickets["items"][0]["priority"] == 5
    assert tickets["items"][0]["evaluation_profile_id"] == DEFAULT_AGENT_ID
    assert tickets["items"][0]["source_payload_ref"].endswith(".json")


def test_job_snapshot_is_immune_to_later_config_changes(client, temp_root):
    dataset_id = f"ds-{uuid.uuid4().hex[:6]}"
    folder = write_dataset(temp_root, dataset_id, [make_payload()])
    created = client.post("/v1/evaluation-jobs", json={"dataset_id": dataset_id}).json()
    job_id = created["job"]["id"]

    config = json.loads((folder / "config.json").read_text(encoding="utf-8"))
    config["agents"][0]["checks"][0]["params"] = {"fields": ["summary", "nonexistent"]}
    (folder / "config.json").write_text(json.dumps(config), encoding="utf-8")

    tickets = client.get(f"/v1/evaluation-jobs/{job_id}/tickets").json()["items"]
    summary = next(t for t in tickets if t["check_id"] == "summary_present")
    detail = client.get(f"/v1/tickets/{summary['id']}").json()
    assert detail["metric_snapshot_json"]["definition_payload"]["params"]["fields"] == ["summary"]
    assert detail["metric_snapshot_json"]["agent_id"] == DEFAULT_AGENT_ID


def test_job_requires_dataset_id(client):
    response = client.post("/v1/evaluation-jobs", json={})
    assert response.status_code == 422


def test_cancellation_cancels_pending_tickets(seeded_job, client):
    job_id = seeded_job["job"]["id"]
    response = client.post(f"/v1/evaluation-jobs/{job_id}/cancel")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cancelled_tickets"] == 6
    assert body["job"]["status"] == "CANCELLED"
    assert body["job"]["cancellation_requested_at"] is not None

    summary = client.get(f"/v1/evaluation-jobs/{job_id}/tickets/summary").json()
    assert summary["counts"] == {"CANCELLED": 6}

    assert client.post(f"/v1/evaluation-jobs/{job_id}/cancel").status_code == 200


def test_retry_failed_requires_failed_tickets(seeded_job, client):
    job_id = seeded_job["job"]["id"]
    response = client.post(f"/v1/evaluation-jobs/{job_id}/retry-failed")
    assert response.status_code == 409
    assert "no failed tickets" in response.json()["error"]["message"]


def test_unknown_ids_return_404(client):
    missing = uuid.uuid4()
    assert client.get(f"/v1/evaluation-jobs/{missing}").status_code == 404
    assert client.get(f"/v1/tickets/{missing}").status_code == 404
    assert client.get(f"/v1/results/{missing}").status_code == 404


def test_only_v1_routes_are_served_and_documented(client):
    """One prefix, one route per operation, and no legacy surface left."""
    assert client.post("/api/v1/evaluation-jobs", json={"dataset_id": "x"}).status_code == 404

    documented = set(client.get("/openapi.json").json()["paths"])
    assert not [path for path in documented if path.startswith("/api/v1")]
    assert "/v1/evaluation-jobs" in documented
    assert not [
        path
        for path in documented
        if any(gone in path for gone in ("datasets", "payloads", "evaluation-configs"))
    ]
