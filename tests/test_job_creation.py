"""Job creation from EVALUATION_TEMP_ROOT, profile extraction, and validation."""

from __future__ import annotations

import json

from app.core import database
from tests.conftest import (
    DEFAULT_PROFILE_ID,
    DETERMINISTIC_CHECKS,
    make_payload,
    seed_default_profile,
    seed_metric,
    seed_profile,
    write_dataset,
)


def _create(client, dataset_id: str):
    return client.post("/v1/evaluation-jobs", json={"dataset_id": dataset_id})


def test_job_creation_with_valid_dataset_folder(client, temp_root):
    with database.transaction() as conn:
        seed_default_profile(conn)
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
    assert "not found" in response.json()["detail"]


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
    assert "no supported JSON" in response.json()["detail"]


def test_unsupported_file_types_are_skipped_when_json_exists(client, temp_root):
    with database.transaction() as conn:
        seed_default_profile(conn)
    folder = temp_root / "mixed"
    folder.mkdir()
    (folder / "notes.txt").write_text("ignore me", encoding="utf-8")
    (folder / "payload_001.json").write_text(json.dumps(make_payload()), encoding="utf-8")
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
    assert "invalid JSON" in response.json()["detail"]


def test_payload_with_utf8_bom_is_accepted(client, temp_root):
    with database.transaction() as conn:
        seed_default_profile(conn)
    folder = temp_root / "bom-ds"
    folder.mkdir()
    (folder / "payload_001.json").write_text(json.dumps(make_payload()), encoding="utf-8-sig")
    response = _create(client, "bom-ds")
    assert response.status_code == 201, response.text
    assert response.json()["payload_count"] == 1


def test_valid_payload_profile_extraction(client, temp_root):
    with database.transaction() as conn:
        seed_default_profile(conn)
    dataset_id = "profile-ok"
    write_dataset(temp_root, dataset_id, [make_payload()])
    body = _create(client, dataset_id).json()
    tickets = client.get(f"/v1/evaluation-jobs/{body['job']['id']}/tickets").json()["items"]
    assert {t["evaluation_profile_id"] for t in tickets} == {DEFAULT_PROFILE_ID}


def test_missing_agent_registry(client, temp_root):
    with database.transaction() as conn:
        seed_default_profile(conn)
    write_dataset(temp_root, "no-reg", [make_payload(include_agent_registry=False)])
    response = _create(client, "no-reg")
    assert response.status_code == 400
    assert "agent_registry" in response.json()["detail"]


def test_missing_evaluation_profile_id(client, temp_root):
    with database.transaction() as conn:
        seed_default_profile(conn)
    payload = make_payload()
    payload["agent_registry"]["evaluation_profile_id"] = "  "
    write_dataset(temp_root, "empty-profile", [payload])
    response = _create(client, "empty-profile")
    assert response.status_code == 400
    assert "evaluation_profile_id" in response.json()["detail"]


def test_profile_not_found(client, temp_root):
    write_dataset(
        temp_root, "unknown-profile", [make_payload(evaluation_profile_id="missing-profile")]
    )
    response = _create(client, "unknown-profile")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_inactive_profile(client, temp_root):
    with database.transaction() as conn:
        records = [seed_metric(conn, check) for check in DETERMINISTIC_CHECKS]
        seed_profile(conn, records, profile_id="inactive-profile", active=False)
    write_dataset(temp_root, "inactive", [make_payload(evaluation_profile_id="inactive-profile")])
    response = _create(client, "inactive")
    assert response.status_code == 409
    assert "inactive" in response.json()["detail"]


def test_multiple_payloads_with_different_profiles(client, temp_root):
    with database.transaction() as conn:
        seed_default_profile(conn)
        extra = seed_metric(
            conn,
            {
                "check_id": "spans_exist",
                "evaluator": "span_exists",
                "input_mapping": {"spans": "spans"},
                "params": {"required_spans": ["classifier"]},
            },
        )
        seed_profile(conn, [extra], profile_id="span-profile", name="span-profile")

    write_dataset(
        temp_root,
        "multi-profile",
        [
            make_payload(evaluation_profile_id=DEFAULT_PROFILE_ID),
            make_payload(evaluation_profile_id="span-profile"),
        ],
    )
    body = _create(client, "multi-profile").json()
    assert body["payload_count"] == 2
    assert body["ticket_count"] == 4  # 3 + 1
    assert set(body["profiles"]) == {DEFAULT_PROFILE_ID, "span-profile"}
    assert body["tickets_summary"]["by_profile"][DEFAULT_PROFILE_ID] == 3
    assert body["tickets_summary"]["by_profile"]["span-profile"] == 1


def test_correct_payload_times_metric_ticket_count(client, temp_root):
    with database.transaction() as conn:
        seed_default_profile(conn)
    write_dataset(temp_root, "count-me", [make_payload() for _ in range(3)])
    body = _create(client, "count-me").json()
    assert body["ticket_count"] == 3 * 3


def test_job_creation_is_transactional_on_second_payload_failure(client, temp_root):
    with database.transaction() as conn:
        seed_default_profile(conn)
    write_dataset(
        temp_root,
        "partial",
        [make_payload(), make_payload(include_agent_registry=False)],
    )
    response = _create(client, "partial")
    assert response.status_code == 400
    listed = client.get("/v1/evaluation-jobs").json()
    assert listed["total"] == 0
