"""Job creation from EVALUATION_TEMP_ROOT, agent matching, and validation."""

from __future__ import annotations

import json

from tests.conftest import (
    DEFAULT_AGENT_ID,
    DETERMINISTIC_CHECKS,
    make_payload,
    write_dataset,
)


def _create(client, dataset_id: str):
    return client.post("/v1/evaluation-jobs", json={"dataset_id": dataset_id})


def test_duplicate_idempotency_key_returns_the_same_job(client, temp_root):
    dataset_id = "dataset-idem-same"
    write_dataset(temp_root, dataset_id, [make_payload()])
    headers = {"Idempotency-Key": "job-key-1"}
    first = client.post("/v1/evaluation-jobs", json={"dataset_id": dataset_id}, headers=headers)
    second = client.post("/v1/evaluation-jobs", json={"dataset_id": dataset_id}, headers=headers)
    assert first.status_code == 201, first.text
    assert second.status_code == 200, second.text
    assert second.json()["job"]["id"] == first.json()["job"]["id"]
    assert second.json()["ticket_count"] == first.json()["ticket_count"]
    listed = client.get("/v1/evaluation-jobs").json()
    assert listed["total"] == 1


def test_same_idempotency_key_with_a_different_request_is_rejected(client, temp_root):
    dataset_id = "dataset-idem-conflict"
    write_dataset(temp_root, dataset_id, [make_payload()])
    headers = {"Idempotency-Key": "job-key-2"}
    first = client.post(
        "/v1/evaluation-jobs",
        json={"dataset_id": dataset_id, "name": "first"},
        headers=headers,
    )
    second = client.post(
        "/v1/evaluation-jobs",
        json={"dataset_id": dataset_id, "name": "second"},
        headers=headers,
    )
    assert first.status_code == 201, first.text
    assert second.status_code == 409
    assert "different request" in second.json()["error"]["message"]


def test_job_creation_with_valid_dataset_folder(client, temp_root):
    dataset_id = "dataset-folder-001"
    write_dataset(temp_root, dataset_id, [make_payload(), make_payload(), make_payload()])
    response = _create(client, dataset_id)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["dataset_id"] == dataset_id
    assert body["payload_count"] == 3
    assert body["ticket_count"] == 9
    assert body["job"]["id"]


def test_missing_dataset_folder(client, temp_root):
    response = _create(client, "does-not-exist")
    assert response.status_code == 404
    assert "not found" in response.json()["error"]["message"]


def test_invalid_dataset_path_and_traversal(client, temp_root):
    assert _create(client, "../etc").status_code == 400
    assert _create(client, "foo/bar").status_code == 400
    assert _create(client, "..").status_code == 400
    assert _create(client, "").status_code == 422
    (temp_root / "not-a-folder.json").write_text("{}", encoding="utf-8")
    response = _create(client, "not-a-folder.json")
    assert response.status_code == 400


def test_empty_dataset_folder(client, temp_root):
    (temp_root / "empty-ds").mkdir()
    response = _create(client, "empty-ds")
    assert response.status_code == 400
    assert "no supported JSON" in response.json()["error"]["message"]


def test_unsupported_file_types_are_skipped_when_json_exists(client, temp_root):
    folder = write_dataset(temp_root, "mixed", [make_payload()])
    (folder / "notes.txt").write_text("ignore me", encoding="utf-8")
    response = _create(client, "mixed")
    assert response.status_code == 201, response.text
    assert response.json()["payload_count"] == 1
    assert any("notes.txt" in w for w in response.json()["warnings"])


def test_invalid_json_payload(client, temp_root):
    folder = temp_root / "bad-json"
    folder.mkdir()
    (folder / "payload_001.json").write_text("{not json", encoding="utf-8")
    response = _create(client, "bad-json")
    assert response.status_code == 400
    assert "invalid JSON" in response.json()["error"]["message"]


def test_payload_with_utf8_bom_is_accepted(client, temp_root):
    folder = write_dataset(temp_root, "bom-ds", [make_payload()])
    (folder / "payload_001.json").write_text(json.dumps(make_payload()), encoding="utf-8-sig")
    response = _create(client, "bom-ds")
    assert response.status_code == 201, response.text
    assert response.json()["payload_count"] == 1


def test_valid_payload_matches_agent_id(client, temp_root):
    dataset_id = "agent-ok"
    write_dataset(temp_root, dataset_id, [make_payload()])
    body = _create(client, dataset_id).json()
    tickets = client.get(f"/v1/evaluation-jobs/{body['job']['id']}/tickets").json()["items"]
    assert {t["evaluation_profile_id"] for t in tickets} == {DEFAULT_AGENT_ID}


def test_missing_agent_registry(client, temp_root):
    write_dataset(temp_root, "no-reg", [make_payload(include_agent_registry=False)])
    response = _create(client, "no-reg")
    assert response.status_code == 400
    assert "agent_registry" in response.json()["error"]["message"]


def test_missing_agent_id(client, temp_root):
    payload = make_payload()
    payload["agent_registry"]["agent_id"] = "  "
    write_dataset(temp_root, "empty-agent", [payload])
    response = _create(client, "empty-agent")
    assert response.status_code == 400
    assert "agent_id" in response.json()["error"]["message"]


def test_agent_not_in_config(client, temp_root):
    write_dataset(temp_root, "unknown-agent", [make_payload(agent_id="missing-agent")])
    response = _create(client, "unknown-agent")
    assert response.status_code == 404
    assert "not found" in response.json()["error"]["message"]


def test_agent_with_no_checks(client, temp_root):
    write_dataset(
        temp_root,
        "no-checks",
        [make_payload()],
        config={"agents": [{"agent_id": DEFAULT_AGENT_ID, "checks": []}]},
    )
    response = _create(client, "no-checks")
    assert response.status_code == 400
    assert "no checks" in response.json()["error"]["message"]


def test_multiple_payloads_with_different_agents(client, temp_root):
    span_check = {
        "check_id": "spans_exist",
        "evaluator": "span_exists",
        "input_mapping": {"spans": "spans"},
        "params": {"required_spans": ["classifier"]},
    }
    write_dataset(
        temp_root,
        "multi-agent",
        [
            make_payload(agent_id=DEFAULT_AGENT_ID),
            make_payload(agent_id="span-agent"),
        ],
        config={
            "agents": [
                {"agent_id": DEFAULT_AGENT_ID, "checks": DETERMINISTIC_CHECKS},
                {"agent_id": "span-agent", "checks": [span_check]},
            ]
        },
    )
    body = _create(client, "multi-agent").json()
    assert body["payload_count"] == 2
    assert body["ticket_count"] == 4  # 3 + 1
    assert set(body["profiles"]) == {DEFAULT_AGENT_ID, "span-agent"}
    assert body["tickets_summary"]["by_agent"][DEFAULT_AGENT_ID] == 3
    assert body["tickets_summary"]["by_agent"]["span-agent"] == 1


def test_correct_payload_times_metric_ticket_count(client, temp_root):
    write_dataset(temp_root, "count-me", [make_payload() for _ in range(3)])
    body = _create(client, "count-me").json()
    assert body["ticket_count"] == 3 * 3


def test_job_creation_is_transactional_on_second_payload_failure(client, temp_root):
    write_dataset(
        temp_root,
        "partial",
        [make_payload(), make_payload(include_agent_registry=False)],
    )
    response = _create(client, "partial")
    assert response.status_code == 400
    listed = client.get("/v1/evaluation-jobs").json()
    assert listed["total"] == 0
