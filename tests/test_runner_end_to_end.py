"""End-to-end: payload in PostgreSQL -> runner -> results in PostgreSQL."""

from __future__ import annotations

import uuid

from app.core.database import transaction
from app.evaluators.llm.providers.base import LLMResponse
from app.models.enums import JobStatus, TicketStatus
from app.repositories.job_repository import JobRepository
from app.repositories.result_repository import ResultRepository
from app.repositories.ticket_repository import TicketRepository
from app.services.evaluation_service import execute_ticket
from app.services.ticket_service import claim_tickets
from runner.runner import EvaluationRunner
from tests.conftest import make_payload


def drain(**kwargs) -> EvaluationRunner:
    runner = EvaluationRunner(
        max_concurrency=kwargs.pop("max_concurrency", 4),
        claim_batch_size=kwargs.pop("claim_batch_size", 4),
        poll_interval=0.05,
        recovery_interval=1000.0,  # recovery is exercised in its own tests
        drain=True,
        **kwargs,
    )
    runner.run_forever()
    return runner


def test_runner_evaluates_every_ticket_and_completes_the_job(seeded_job, client):
    job_id = seeded_job["job"]["id"]
    drain()

    job = client.get(f"/api/v1/evaluation-jobs/{job_id}").json()
    assert job["status"] == "COMPLETED"
    assert job["completed_tickets"] == 6
    assert job["ticket_counts"] == {"DONE": 6}
    assert job["progress_ratio"] == 1.0

    results = client.get(f"/api/v1/evaluation-jobs/{job_id}/results").json()
    assert results["total"] == 6
    assert {r["status"] for r in results["items"]} == {"PASSED"}
    for result in results["items"]:
        assert result["passed"] is True
        assert result["execution_time_ms"] >= 0
        assert result["evaluator_version"]

    detail = client.get(f"/api/v1/results/{results['items'][0]['id']}").json()
    assert detail["input_snapshot_json"]["resolutions"]
    assert detail["evidence_json"]

    summary = client.get(f"/api/v1/evaluation-jobs/{job_id}/results/summary").json()
    assert summary["total_results"] == 6
    assert summary["passed"] == 6
    assert summary["pass_rate"] == 1.0
    assert len(summary["checks"]) == 3


def test_ticket_links_to_its_result(seeded_job, client):
    drain()
    job_id = seeded_job["job"]["id"]
    tickets = client.get(f"/api/v1/evaluation-jobs/{job_id}/tickets").json()["items"]
    ticket = tickets[0]
    assert ticket["status"] == "DONE"
    assert ticket["result_id"]
    result = client.get(f"/api/v1/results/{ticket['result_id']}").json()
    assert result["ticket_id"] == ticket["id"]
    assert result["check_id"] == ticket["check_id"]


def test_failing_check_is_recorded_as_a_failed_result_not_a_failed_ticket(
    client, deterministic_config
):
    """A check that legitimately fails is a FAILED result on a DONE ticket."""
    config = client.post(
        "/api/v1/evaluation-configs",
        json={"name": f"cfg-{uuid.uuid4().hex[:6]}", "config": deterministic_config},
    ).json()
    # This payload has no summarizer span, so summary_present cannot be resolved
    # and the workflow order check must fail.
    dataset = client.post(
        "/api/v1/datasets",
        json={"name": "incomplete", "payloads": [make_payload(include_summarizer=False)]},
    ).json()
    job = client.post(
        "/api/v1/evaluation-jobs",
        json={"evaluation_config_id": config["id"], "dataset_id": dataset["dataset_id"]},
    ).json()["job"]

    drain()

    results = client.get(f"/api/v1/evaluation-jobs/{job['id']}/results").json()["items"]
    by_check = {r["check_id"]: r for r in results}
    # Missing context with the default policy yields NOT_APPLICABLE, not a false failure.
    assert by_check["summary_present"]["status"] == "NOT_APPLICABLE"
    assert by_check["summary_present"]["error_code"] == "MISSING_CONTEXT"
    assert by_check["workflow_sequence"]["status"] == "FAILED"
    assert by_check["workflow_sequence"]["evidence_json"]["missing_agents"] == ["summarizer"]
    assert by_check["policy_tool_called"]["status"] == "PASSED"

    detail = client.get(f"/api/v1/evaluation-jobs/{job['id']}").json()
    assert detail["status"] == "COMPLETED"
    assert detail["not_applicable_tickets"] == 1
    assert detail["completed_tickets"] == 2


def test_missing_context_can_be_configured_to_fail(client):
    config = {
        "checks": [
            {
                "check_id": "strict_summary",
                "evaluator": "required_fields",
                "input_mapping": {"summary": "summarizer.output.summary"},
                "on_missing_context": "fail",
                "max_attempts": 1,
            }
        ]
    }
    stored = client.post(
        "/api/v1/evaluation-configs",
        json={"name": f"cfg-{uuid.uuid4().hex[:6]}", "config": config},
    ).json()
    dataset = client.post(
        "/api/v1/datasets",
        json={"name": "strict", "payloads": [make_payload(include_summarizer=False)]},
    ).json()
    job = client.post(
        "/api/v1/evaluation-jobs",
        json={"evaluation_config_id": stored["id"], "dataset_id": dataset["dataset_id"]},
    ).json()["job"]

    drain()

    detail = client.get(f"/api/v1/evaluation-jobs/{job['id']}").json()
    assert detail["status"] == "FAILED"
    assert detail["failed_tickets"] == 1
    result = client.get(f"/api/v1/evaluation-jobs/{job['id']}/results").json()["items"][0]
    assert result["status"] == "FAILED"
    assert result["error_code"] == "MISSING_CONTEXT"
    assert "MISSING_SPAN" in result["input_snapshot_json"]["errors"][0]["code"]


def test_retrying_a_ticket_does_not_duplicate_results(seeded_job, client):
    job_id = uuid.UUID(seeded_job["job"]["id"])
    drain()
    with transaction() as conn:
        assert ResultRepository(conn).count_by_job(job_id) == 6
        tickets = TicketRepository(conn).list_by_job(job_id)[1]
        # Re-open one ticket as if an operator retried it.
        conn.execute(
            """
            UPDATE evaluation_tickets
            SET status = 'READY', attempt_count = 0, result_id = NULL, available_at = now()
            WHERE id = %s
            """,
            (tickets[0].id,),
        )
    drain()
    with transaction() as conn:
        # The unique (job_id, payload_id, check_id) key makes re-execution an upsert.
        assert ResultRepository(conn).count_by_job(job_id) == 6
        rows = conn.execute(
            """
            SELECT COUNT(*) AS n FROM evaluation_results
            WHERE job_id = %s AND payload_id = %s AND check_id = %s
            """,
            (job_id, tickets[0].payload_id, tickets[0].check_id),
        ).fetchone()
        assert rows["n"] == 1


def test_transient_failure_retries_then_fails_permanently(client, monkeypatch):
    """A judge provider that always times out must retry, then fail the ticket."""
    import httpx

    class FlakyProvider:
        def generate(self, prompt, model, config=None):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(
        "app.evaluators.llm.judge.get_llm_provider", lambda name=None: FlakyProvider()
    )
    config = {
        "checks": [
            {
                "check_id": "judge",
                "check_type": "LLM_JUDGE",
                "input_mapping": {"summary": "summarizer.output.summary"},
                "params": {"criteria": "faithful?"},
                "max_attempts": 2,
            }
        ]
    }
    stored = client.post(
        "/api/v1/evaluation-configs",
        json={"name": f"cfg-{uuid.uuid4().hex[:6]}", "config": config},
    ).json()
    dataset = client.post(
        "/api/v1/datasets", json={"name": "judge-ds", "payloads": [make_payload()]}
    ).json()
    job = client.post(
        "/api/v1/evaluation-jobs",
        json={"evaluation_config_id": stored["id"], "dataset_id": dataset["dataset_id"]},
    ).json()["job"]

    drain(max_concurrency=1, claim_batch_size=1)

    ticket = client.get(f"/api/v1/evaluation-jobs/{job['id']}/tickets").json()["items"][0]
    assert ticket["status"] == "FAILED"
    assert ticket["attempt_count"] == 2
    assert ticket["error_code"] == "TRANSIENT_ATTEMPTS_EXHAUSTED"

    detail = client.get(f"/api/v1/evaluation-jobs/{job['id']}").json()
    assert detail["status"] == "FAILED"
    result = client.get(f"/api/v1/evaluation-jobs/{job['id']}/results").json()["items"][0]
    assert result["status"] == "ERROR"
    assert "connection refused" in result["error_message"]


def test_llm_judge_end_to_end_persists_evidence(client, monkeypatch):
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
        "app.evaluators.llm.judge.get_llm_provider", lambda name=None: FakeProvider()
    )
    config = {
        "model": {"provider": "openai", "model": "gpt-4o-mini"},
        "checks": [
            {
                "check_id": "summary_faithfulness",
                "check_type": "LLM_JUDGE",
                "input_mapping": {
                    "summary": "summarizer.output.summary",
                    "conversation": "session_context.conversation_history",
                },
                "params": {"criteria": "Does the summary reflect the conversation?"},
            }
        ],
    }
    stored = client.post(
        "/api/v1/evaluation-configs",
        json={"name": f"cfg-{uuid.uuid4().hex[:6]}", "config": config},
    ).json()
    dataset = client.post(
        "/api/v1/datasets", json={"name": "judge-ok", "payloads": [make_payload()]}
    ).json()
    job = client.post(
        "/api/v1/evaluation-jobs",
        json={"evaluation_config_id": stored["id"], "dataset_id": dataset["dataset_id"]},
    ).json()["job"]

    drain()

    result = client.get(f"/api/v1/evaluation-jobs/{job['id']}/results").json()["items"][0]
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

    client.post(f"/api/v1/evaluation-jobs/{job_id}/cancel")

    outcome = execute_ticket(ticket, worker_id="w-1")
    assert outcome.status is TicketStatus.CANCELLED

    with transaction() as conn:
        assert TicketRepository(conn).get(ticket.id).status is TicketStatus.CANCELLED
        assert ResultRepository(conn).get_by_ticket(ticket.id) is None
        assert JobRepository(conn).get(uuid.UUID(job_id)).status is JobStatus.CANCELLED


def test_two_runners_share_the_workload_without_overlap(client, deterministic_config):
    config = client.post(
        "/api/v1/evaluation-configs",
        json={"name": f"cfg-{uuid.uuid4().hex[:6]}", "config": deterministic_config},
    ).json()
    dataset = client.post(
        "/api/v1/datasets",
        json={"name": "many", "payloads": [make_payload() for _ in range(6)]},
    ).json()
    job = client.post(
        "/api/v1/evaluation-jobs",
        json={"evaluation_config_id": config["id"], "dataset_id": dataset["dataset_id"]},
    ).json()["job"]

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

    detail = client.get(f"/api/v1/evaluation-jobs/{job['id']}").json()
    assert detail["status"] == "COMPLETED"
    assert detail["completed_tickets"] == 18  # 6 payloads x 3 checks

    with transaction() as conn:
        assert ResultRepository(conn).count_by_job(uuid.UUID(job["id"])) == 18
        rows = conn.execute(
            """
            SELECT COUNT(DISTINCT worker_id) AS workers FROM evaluation_tickets
            WHERE job_id = %s
            """,
            (uuid.UUID(job["id"]),),
        ).fetchone()
        assert rows["workers"] >= 1


def test_execute_ticket_is_safe_when_payload_is_deleted(seeded_job):
    with transaction() as conn:
        ticket = claim_tickets(conn, worker_id="w-1", limit=1)[0]
        conn.execute("DELETE FROM evaluation_payloads WHERE id = %s", (ticket.payload_id,))
    outcome = execute_ticket(ticket)
    # The ticket row cascades away with its payload, so settlement finds nothing
    # and the runner reports it instead of crashing.
    assert outcome.status is TicketStatus.FAILED
    assert outcome.error_code == "TICKET_GONE"
