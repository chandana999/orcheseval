"""End-to-end: payload files in EVALUATION_TEMP_ROOT -> runner -> results in PostgreSQL."""

from __future__ import annotations

import threading
import time
import uuid

from sqlalchemy import text

from evalorch.core.database import transaction
from evalorch.evaluators.llm.providers.base import LLMResponse
from evalorch.models.enums import JobStatus, TicketStatus
from evalorch.repositories.job_repository import JobRepository
from evalorch.repositories.result_repository import ResultRepository
from evalorch.repositories.ticket_repository import TicketRepository
from evalorch.services.evaluation_service import execute_ticket
from evalorch.services.ticket_service import claim_tickets
from runner.runner import EvaluationRunner
from tests.conftest import make_payload, write_dataset


def drain(**kwargs) -> EvaluationRunner:
    runner = EvaluationRunner(
        max_concurrency=kwargs.pop("max_concurrency", 4),
        claim_batch_size=kwargs.pop("claim_batch_size", 4),
        poll_interval=0.05,
        recovery_interval=1000.0,
        drain=True,
        **kwargs,
    )
    runner.run_forever()
    return runner


def _job_from_payloads(client, temp_root, payloads, metrics=None):
    dataset_id = f"run-{uuid.uuid4().hex[:8]}"
    write_dataset(temp_root, dataset_id, payloads, metrics=metrics)
    response = client.post("/v1/evaluation-jobs", json={"dataset_id": dataset_id})
    assert response.status_code == 201, response.text
    return response.json()["job"], dataset_id


def test_graceful_shutdown_does_not_leave_running_tickets(seeded_job):
    runner = EvaluationRunner(
        worker_id="w-stop",
        max_concurrency=2,
        claim_batch_size=2,
        poll_interval=0.05,
        recovery_interval=1000.0,
        drain=False,
    )
    thread = threading.Thread(target=runner.run_forever)
    thread.start()
    time.sleep(0.2)
    runner.request_stop()
    thread.join(timeout=30)
    assert not thread.is_alive()
    with transaction() as conn:
        counts = TicketRepository(conn).counts_by_status(uuid.UUID(seeded_job["job"]["id"]))
        assert counts.get("RUNNING", 0) == 0


def test_runner_evaluates_every_ticket_and_completes_the_job(seeded_job, client):
    job_id = seeded_job["job"]["id"]
    drain()

    job = client.get(f"/v1/evaluation-jobs/{job_id}").json()
    assert job["status"] == "COMPLETED"
    assert job["completed_tickets"] == 6
    assert job["ticket_counts"] == {"DONE": 6}
    assert job["progress_ratio"] == 1.0

    results = client.get(f"/v1/evaluation-jobs/{job_id}/results").json()
    assert results["total"] == 6
    assert {r["status"] for r in results["items"]} == {"PASSED"}
    for result in results["items"]:
        assert result["passed"] is True
        assert result["execution_time_ms"] >= 0
        assert result["evaluator_version"]
        assert result["metric_id"]
        assert result["metric_version_number"] == 1

    detail = client.get(f"/v1/results/{results['items'][0]['id']}").json()
    assert detail["input_snapshot_json"]["resolutions"]
    assert detail["evidence_json"]

    summary = client.get(f"/v1/evaluation-jobs/{job_id}/results/summary").json()
    assert summary["total_results"] == 6
    assert summary["passed"] == 6
    assert summary["pass_rate"] == 1.0
    assert len(summary["checks"]) == 3


def test_ticket_links_to_its_result(seeded_job, client):
    drain()
    job_id = seeded_job["job"]["id"]
    tickets = client.get(f"/v1/evaluation-jobs/{job_id}/tickets").json()["items"]
    ticket = tickets[0]
    assert ticket["status"] == "DONE"
    assert ticket["result_id"]
    result = client.get(f"/v1/results/{ticket['result_id']}").json()
    assert result["ticket_id"] == ticket["id"]
    assert result["check_id"] == ticket["check_id"]
    assert result["metric_id"] == ticket["metric_id"]


def test_failing_check_is_recorded_as_a_failed_result_not_a_failed_ticket(
    client, temp_root, deterministic_config
):
    job, _dataset_id = _job_from_payloads(
        client,
        temp_root,
        [make_payload(include_summarizer=False)],
        metrics=deterministic_config["metrics"],
    )
    drain()

    results = client.get(f"/v1/evaluation-jobs/{job['id']}/results").json()["items"]
    by_check = {r["check_id"]: r for r in results}
    assert by_check["summary_present"]["status"] == "NOT_APPLICABLE"
    assert by_check["summary_present"]["error_code"] == "NOT_APPLICABLE"
    assert by_check["workflow_sequence"]["status"] == "FAILED"
    assert by_check["workflow_sequence"]["evidence_json"]["missing_agents"] == ["summarizer"]
    assert by_check["policy_tool_called"]["status"] == "PASSED"

    detail = client.get(f"/v1/evaluation-jobs/{job['id']}").json()
    assert detail["status"] == "COMPLETED"
    assert detail["not_applicable_tickets"] == 1
    assert detail["completed_tickets"] == 2


def test_missing_context_can_be_configured_to_fail(client, temp_root):
    from tests.conftest import metric

    metrics = [
        metric(
            "strict_summary",
            "required_fields",
            {"summary": "summarizer.attributes.output.summary"},
            {"fields": ["summary"]},
            on_missing_context="fail",
            max_attempts=1,
        )
    ]
    job, _ = _job_from_payloads(
        client, temp_root, [make_payload(include_summarizer=False)], metrics=metrics
    )
    drain()

    detail = client.get(f"/v1/evaluation-jobs/{job['id']}").json()
    assert detail["status"] == "FAILED"
    assert detail["failed_tickets"] == 1
    result = client.get(f"/v1/evaluation-jobs/{job['id']}/results").json()["items"][0]
    assert result["status"] == "FAILED"
    assert result["error_code"] == "MISSING_CONTEXT"
    assert "MISSING_SPAN" in result["input_snapshot_json"]["errors"][0]["code"]


def test_retrying_a_ticket_does_not_duplicate_results(seeded_job, client):
    job_id = uuid.UUID(seeded_job["job"]["id"])
    drain()
    with transaction() as conn:
        assert ResultRepository(conn).count_by_job(job_id) == 6
        tickets = TicketRepository(conn).list_by_job(job_id)[1]
        conn.execute(
            text(
                """
                UPDATE evaluation_tickets
                SET status = 'READY', attempt_count = 0, result_id = NULL, available_at = now()
                WHERE id = :id
                """
            ),
            {"id": tickets[0].id},
        )
    drain()
    with transaction() as conn:
        assert ResultRepository(conn).count_by_job(job_id) == 6
        rows = conn.execute(
            text("SELECT COUNT(*) AS n FROM evaluation_results WHERE ticket_id = :id"),
            {"id": tickets[0].id},
        ).mappings().one()
        assert rows["n"] == 1


def test_transient_failure_retries_then_fails_permanently(client, temp_root, monkeypatch):
    import httpx

    class FlakyProvider:
        def generate(self, prompt, model, config=None):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(
        "evalorch.evaluators.llm.judge.get_llm_provider", lambda name=None: FlakyProvider()
    )
    from tests.conftest import metric

    metrics = [
        metric(
            "judge",
            "llm_judge",
            {"summary": "summarizer.attributes.output.summary"},
            {"criteria": "faithful?"},
            metric_type="llm_judge",
            max_attempts=2,
        )
    ]
    job, _ = _job_from_payloads(client, temp_root, [make_payload()], metrics=metrics)
    drain(max_concurrency=1, claim_batch_size=1)

    ticket = client.get(f"/v1/evaluation-jobs/{job['id']}/tickets").json()["items"][0]
    assert ticket["status"] == "FAILED"
    assert ticket["attempt_count"] == 2
    assert ticket["error_code"] == "TRANSIENT_ATTEMPTS_EXHAUSTED"

    detail = client.get(f"/v1/evaluation-jobs/{job['id']}").json()
    assert detail["status"] == "FAILED"
    result = client.get(f"/v1/evaluation-jobs/{job['id']}/results").json()["items"][0]
    assert result["status"] == "ERROR"
    assert "connection refused" in result["error_message"]


def test_llm_judge_end_to_end_persists_evidence(client, temp_root, monkeypatch):
    class FakeProvider:
        def generate(self, prompt, model, config=None):
            return LLMResponse(
                output='{"passed": true, "score": 0.91, "explanation": "accurate summary"}',
                latency_ms=10.0,
                prompt_tokens=250,
                completion_tokens=30,
                model=model or "fake-model",
            )

    monkeypatch.setattr(
        "evalorch.evaluators.llm.judge.get_llm_provider", lambda name=None: FakeProvider()
    )
    from tests.conftest import metric

    metrics = [
        metric(
            "summary_faithfulness",
            "llm_judge",
            {
                "summary": "summarizer.attributes.output.summary",
                "conversation": "classifier.attributes.conversation_history",
            },
            {"criteria": "Does the summary reflect the conversation?"},
            metric_type="llm_judge",
        )
    ]
    job, _ = _job_from_payloads(client, temp_root, [make_payload()], metrics=metrics)
    drain()

    result = client.get(f"/v1/evaluation-jobs/{job['id']}/results").json()["items"][0]
    assert result["status"] == "PASSED"
    assert result["score"] == 0.91
    assert result["check_type"] == "LLM_JUDGE"
    assert result["evidence_json"]["model"] == "gpt-4o-mini"
    assert result["evidence_json"]["prompt_tokens"] == 250
    assert result["evidence_json"]["estimated_cost_usd"] > 0
    assert "summary" in result["input_snapshot_json"]["values"]


def test_cancellation_while_running_settles_ticket_without_result(seeded_job, client):
    job_id = seeded_job["job"]["id"]
    with transaction() as conn:
        ticket = claim_tickets(conn, worker_id="w-1", limit=1)[0]

    client.post(f"/v1/evaluation-jobs/{job_id}/cancel")

    outcome = execute_ticket(ticket, worker_id="w-1")
    assert outcome.status is TicketStatus.CANCELLED

    with transaction() as conn:
        assert TicketRepository(conn).get(ticket.id).status is TicketStatus.CANCELLED
        assert ResultRepository(conn).get_by_ticket(ticket.id) is None
        assert JobRepository(conn).get(uuid.UUID(job_id)).status is JobStatus.CANCELLED


def test_two_runners_share_the_workload_without_overlap(client, temp_root, deterministic_config):
    job, _ = _job_from_payloads(
        client,
        temp_root,
        [make_payload() for _ in range(6)],
        metrics=deterministic_config["metrics"],
    )

    import threading

    runners = [
        EvaluationRunner(
            worker_id=f"w-{i}",
            max_concurrency=2,
            claim_batch_size=2,
            poll_interval=0.02,
            recovery_interval=1000.0,
            drain=True,
        )
        for i in range(2)
    ]
    threads = [threading.Thread(target=r.run_forever) for r in runners]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    detail = client.get(f"/v1/evaluation-jobs/{job['id']}").json()
    assert detail["status"] == "COMPLETED"
    assert detail["completed_tickets"] == 18

    with transaction() as conn:
        assert ResultRepository(conn).count_by_job(uuid.UUID(job["id"])) == 18
        rows = conn.execute(
            text(
                """
                SELECT COUNT(DISTINCT worker_id) AS workers FROM evaluation_tickets
                WHERE job_id = :job_id
                """
            ),
            {"job_id": uuid.UUID(job["id"])},
        ).mappings().one()
        assert rows["workers"] >= 1


def test_execute_ticket_fails_when_source_payload_is_deleted(seeded_job):
    with transaction() as conn:
        ticket = claim_tickets(conn, worker_id="w-1", limit=1)[0]
    source = seeded_job["temp_root"] / seeded_job["dataset_id"] / ticket.source_payload_ref
    source.unlink()
    outcome = execute_ticket(ticket)
    assert outcome.status is TicketStatus.FAILED
    assert outcome.error_code == "SOURCE_PAYLOAD_MISSING"
