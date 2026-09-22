"""Execution of a single evaluation ticket.

Flow, all of it PostgreSQL-driven for durable state:

    load ticket snapshot from PostgreSQL
        -> load source payload from EVALUATION_TEMP_ROOT
        -> context resolver builds normalized evaluator input
        -> evaluator runs
        -> result upserted, ticket transitioned, job progress recomputed

    The payload is re-read from the temporary dataset folder using the ticket's
    source_dataset_id + source_payload_ref. The check is taken from the ticket's
    immutable metric_snapshot_json, which was copied from config.json at job
    creation. LLM calls happen outside any database transaction.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from psycopg.errors import DeadlockDetected
from sqlalchemy.exc import OperationalError

from app.core.config import settings
from app.core.database import transaction
from app.core.logging import get_logger
from app.core.metrics import EVALUATION_DURATION, EVALUATIONS_TOTAL
from app.evaluators.base import EvaluatorOutput, ExecutionContext
from app.evaluators.registry import get_evaluator
from app.models.entities import EvaluationResult, EvaluationTicket
from app.models.enums import (
    CheckType,
    ErrorClass,
    JobStatus,
    OnMissingContext,
    ResultStatus,
    TicketStatus,
)
from app.repositories.job_repository import JobRepository
from app.repositories.result_repository import ResultRepository
from app.repositories.ticket_repository import StaleWorkerError, TicketRepository
from app.services.context_resolver import ResolvedInput, resolve_input_mapping
from app.services.dataset_folder import load_source_payload
from app.services.errors import (
    EvaluationError,
    MissingContextError,
    PermanentEvaluationError,
    SourcePayloadMissingError,
    classify_error,
    error_code_for,
)
from app.services.profile_resolution import check_from_snapshot
from app.services.ticket_service import settle_ticket

logger = get_logger(__name__)


@dataclass
class TicketOutcome:
    ticket_id: uuid.UUID
    status: TicketStatus
    result_id: uuid.UUID | None = None
    result_status: ResultStatus | None = None
    error_code: str | None = None
    error_message: str | None = None
    retry_in_seconds: float | None = None


class JobCancelled(RuntimeError):
    pass


def _applies(
    check: dict[str, Any], resolved: ResolvedInput, payload: dict[str, Any]
) -> tuple[bool, str]:
    """Evaluate an optional `applies_when` guard for the check."""
    condition = check.get("applies_when")
    if not condition:
        return True, ""
    from app.services.context_resolver import resolve_path

    for path, expected in (condition.get("equals") or {}).items():
        value, code, _ = resolve_path(payload, path)
        if code is not None:
            return False, f"applies_when: '{path}' unresolved ({code})"
        if value != expected:
            return False, f"applies_when: '{path}' is {value!r}, expected {expected!r}"

    required_spans = condition.get("required_spans") or []
    if required_spans:
        from app.services.context_resolver import extract_spans

        spans = extract_spans(payload)
        available = {
            str(span.get(key))
            for span in spans
            for key in ("agent_id", "agent_type", "span_id", "name")
            if span.get(key)
        }
        missing = [name for name in required_spans if str(name) not in available]
        if missing:
            return False, f"applies_when: missing span(s) {', '.join(missing)}"
    return True, ""


_DEADLOCK_SQLSTATE = "40P01"


def is_postgres_deadlock(exc: BaseException) -> bool:
    """True when the exception is a PostgreSQL deadlock (SQLSTATE 40P01).

    SQLAlchemy surfaces the psycopg error as OperationalError.orig.
    """
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, DeadlockDetected):
            return True
        if getattr(current, "sqlstate", None) == _DEADLOCK_SQLSTATE:
            return True
        nxt = getattr(current, "orig", None)
        if not isinstance(nxt, BaseException):
            nxt = current.__cause__
        current = nxt if isinstance(nxt, BaseException) else None
    return False


def _persist(
    ticket: EvaluationTicket,
    output: EvaluatorOutput,
    resolved: ResolvedInput | None,
    *,
    target: TicketStatus,
    worker_id: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    delay_seconds: float | None = None,
) -> TicketOutcome:
    """Upsert the result and settle the ticket in one transaction.

    The parent job row is locked first so concurrent settlements of tickets
    on the same job cannot deadlock on the tickets→jobs foreign key.

    A PostgreSQL deadlock during settlement is retried here. That persistence
    retry does not consume an evaluation attempt. SQLAlchemy reports the
    deadlock as OperationalError, with the psycopg error on ``orig``.
    """
    snapshot = resolved.as_snapshot() if resolved else None
    last_deadlock: BaseException | None = None
    for attempt in range(5):
        try:
            return _persist_once(
                ticket,
                output,
                snapshot,
                target=target,
                worker_id=worker_id,
                error_code=error_code,
                error_message=error_message,
                delay_seconds=delay_seconds,
            )
        except StaleWorkerError:
            return _stale_outcome(ticket, worker_id)
        except OperationalError as exc:
            if not is_postgres_deadlock(exc):
                raise
            last_deadlock = exc
            time.sleep(0.05 * (attempt + 1))
        except DeadlockDetected as exc:
            last_deadlock = exc
            time.sleep(0.05 * (attempt + 1))
    assert last_deadlock is not None
    raise last_deadlock


def _persist_once(
    ticket: EvaluationTicket,
    output: EvaluatorOutput,
    snapshot: dict[str, Any] | None,
    *,
    target: TicketStatus,
    worker_id: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    delay_seconds: float | None = None,
) -> TicketOutcome:
    with transaction() as conn:
        if TicketRepository(conn).get(ticket.id) is None:
            logger.warning("ticket_vanished_before_settlement", ticket_id=str(ticket.id))
            return TicketOutcome(
                ticket_id=ticket.id,
                status=TicketStatus.FAILED,
                error_code="TICKET_GONE",
                error_message="ticket no longer exists",
            )

        # Lock the job before inserting results or updating the ticket.
        JobRepository(conn).get_for_update(ticket.job_id)
        state = JobRepository(conn).get_cancellation_state(ticket.job_id)
        if state is not None and state[1] and target is not TicketStatus.CANCELLED:
            settle_ticket(
                conn,
                ticket.id,
                TicketStatus.CANCELLED,
                error_code="CANCELLED",
                error_message="job cancelled while ticket was running",
                job_id=ticket.job_id,
                worker_id=worker_id,
            )
            return TicketOutcome(
                ticket_id=ticket.id,
                status=TicketStatus.CANCELLED,
                error_code="CANCELLED",
            )

        result_id: uuid.UUID | None = None
        if target in {TicketStatus.DONE, TicketStatus.NOT_APPLICABLE, TicketStatus.FAILED}:
            result = ResultRepository(conn).upsert(
                EvaluationResult(
                    id=uuid.uuid4(),
                    job_id=ticket.job_id,
                    ticket_id=ticket.id,
                    payload_id=ticket.payload_id,
                    source_payload_ref=ticket.source_payload_ref,
                    metric_record_id=ticket.metric_record_id,
                    metric_id=ticket.metric_id,
                    metric_version_number=ticket.metric_version_number,
                    check_id=ticket.check_id,
                    check_type=ticket.check_type,
                    evaluator_type=output.evaluator_type or ticket.evaluator,
                    evaluator_version=output.evaluator_version,
                    status=output.status,
                    passed=output.passed,
                    score=output.score,
                    explanation=output.explanation,
                    evidence_json=output.evidence or None,
                    input_snapshot_json=snapshot,
                    output_json=output.as_output_json(),
                    error_code=output.error_code or error_code,
                    error_message=output.error_message or error_message,
                    execution_time_ms=output.execution_time_ms,
                    attempt_count=ticket.attempt_count,
                )
            )
            result_id = result.id

        settle_ticket(
            conn,
            ticket.id,
            target,
            result_id=result_id,
            error_code=error_code,
            error_message=error_message,
            delay_seconds=delay_seconds,
            input_snapshot=snapshot,
            job_id=ticket.job_id,
            worker_id=worker_id,
        )

    return TicketOutcome(
        ticket_id=ticket.id,
        status=target,
        result_id=result_id,
        result_status=output.status,
        error_code=error_code or output.error_code,
        error_message=error_message or output.error_message,
        retry_in_seconds=delay_seconds,
    )


def _stale_outcome(ticket: EvaluationTicket, worker_id: str | None) -> TicketOutcome:
    logger.warning(
        "stale_worker_settlement_rejected",
        ticket_id=str(ticket.id),
        job_id=str(ticket.job_id),
        worker_id=worker_id,
    )
    return TicketOutcome(
        ticket_id=ticket.id,
        status=TicketStatus.RUNNING,
        error_code="STALE_WORKER",
        error_message="ticket is no longer owned by this worker",
    )


def execute_ticket(ticket: EvaluationTicket, *, worker_id: str | None = None) -> TicketOutcome:
    """Evaluate one claimed ticket and persist everything durably."""
    resolved: ResolvedInput | None = None
    owner = worker_id or ticket.worker_id
    try:
        # ---------------------------------------------------------- load state
        with transaction() as conn:
            job = JobRepository(conn).get(ticket.job_id)
            if job is None:
                raise PermanentEvaluationError(
                    f"job {ticket.job_id} disappeared", error_code="JOB_MISSING"
                )
            if job.cancellation_requested_at is not None or job.status is JobStatus.CANCELLED:
                raise JobCancelled()
            stored = TicketRepository(conn).get(ticket.id)
            snapshot = (
                stored.metric_snapshot_json
                if stored and stored.metric_snapshot_json
                else (ticket.metric_snapshot_json or {})
            )

        check = check_from_snapshot(snapshot)
        if check is None:
            raise PermanentEvaluationError(
                f"metric snapshot on ticket {ticket.id} is missing a check definition",
                error_code="CHECK_NOT_IN_SNAPSHOT",
            )

        try:
            payload_json = load_source_payload(
                dataset_id=ticket.source_dataset_id or (job.dataset_id or ""),
                payload_ref=ticket.source_payload_ref,
            )
        except SourcePayloadMissingError:
            raise

        policy = OnMissingContext(
            check.get("on_missing_context") or settings.default_on_missing_context
        )

        # ------------------------------------------------- resolve + evaluate
        resolved = resolve_input_mapping(payload_json, check.get("input_mapping"))

        applicable, reason = _applies(check, resolved, payload_json)
        if not applicable:
            evaluator = get_evaluator(check["evaluator"])
            output = evaluator.not_applicable(
                explanation=reason,
                evidence={"resolution": resolved.as_snapshot()},
                error_code="NOT_APPLICABLE",
            )
            EVALUATIONS_TOTAL.labels(
                evaluator_type=check["evaluator"], status=ResultStatus.NOT_APPLICABLE.value
            ).inc()
            return _persist(
                ticket,
                output,
                resolved,
                target=TicketStatus.NOT_APPLICABLE,
                worker_id=owner,
                error_code="NOT_APPLICABLE",
                error_message=reason,
            )

        if not resolved.ok:
            message = (
                f"required context unavailable ({', '.join(resolved.error_codes())}) "
                f"for: {', '.join(resolved.missing_required)}"
            )
            if policy is OnMissingContext.NOT_APPLICABLE:
                evaluator = get_evaluator(check["evaluator"])
                output = evaluator.not_applicable(
                    explanation=message,
                    evidence={"resolution": resolved.as_snapshot()},
                    error_code="MISSING_CONTEXT",
                )
                EVALUATIONS_TOTAL.labels(
                    evaluator_type=check["evaluator"],
                    status=ResultStatus.NOT_APPLICABLE.value,
                ).inc()
                return _persist(
                    ticket,
                    output,
                    resolved,
                    target=TicketStatus.NOT_APPLICABLE,
                    worker_id=owner,
                    error_code="MISSING_CONTEXT",
                    error_message=message,
                )
            raise MissingContextError(message)

        evaluator = get_evaluator(check["evaluator"])
        context = ExecutionContext(
            job_id=ticket.job_id,
            ticket_id=ticket.id,
            payload_id=ticket.payload_id,
            check_id=ticket.check_id,
            check_type=ticket.check_type,
            attempt=ticket.attempt_count,
            max_attempts=ticket.max_attempts,
            worker_id=owner,
            config_snapshot=snapshot,
        )
        output = evaluator.run(dict(resolved.values), check, context)

        EVALUATION_DURATION.labels(evaluator_type=evaluator.name).observe(
            output.execution_time_ms / 1000
        )
        EVALUATIONS_TOTAL.labels(evaluator_type=evaluator.name, status=output.status.value).inc()

        # An evaluator-level ERROR (for example an unparseable judge verdict) is
        # retried while attempts remain, then recorded as FAILED.
        if output.status is ResultStatus.ERROR:
            if ticket.attempt_count < ticket.max_attempts:
                delay = settings.backoff_for_attempt(ticket.attempt_count)
                return _persist(
                    ticket,
                    output,
                    resolved,
                    target=TicketStatus.RETRY,
                    worker_id=owner,
                    error_code=output.error_code or "EVALUATOR_ERROR",
                    error_message=output.error_message or output.explanation,
                    delay_seconds=delay,
                )
            return _persist(
                ticket,
                output,
                resolved,
                target=TicketStatus.FAILED,
                worker_id=owner,
                error_code=output.error_code or "EVALUATOR_ERROR",
                error_message=output.error_message or output.explanation,
            )

        if output.status is ResultStatus.NOT_APPLICABLE:
            return _persist(
                ticket,
                output,
                resolved,
                target=TicketStatus.NOT_APPLICABLE,
                worker_id=owner,
                error_code=output.error_code or "NOT_APPLICABLE",
                error_message=output.explanation,
            )

        # PASSED and FAILED verdicts both mean the evaluation ran: ticket is DONE.
        return _persist(ticket, output, resolved, target=TicketStatus.DONE, worker_id=owner)

    except JobCancelled:
        try:
            with transaction() as conn:
                if TicketRepository(conn).get(ticket.id) is None:
                    return TicketOutcome(
                        ticket_id=ticket.id, status=TicketStatus.FAILED, error_code="TICKET_GONE"
                    )
                settle_ticket(
                    conn,
                    ticket.id,
                    TicketStatus.CANCELLED,
                    error_code="CANCELLED",
                    error_message="job cancellation requested",
                    job_id=ticket.job_id,
                    worker_id=owner,
                )
        except StaleWorkerError:
            return _stale_outcome(ticket, owner)
        return TicketOutcome(
            ticket_id=ticket.id, status=TicketStatus.CANCELLED, error_code="CANCELLED"
        )

    except StaleWorkerError:
        return _stale_outcome(ticket, owner)

    except Exception as exc:  # noqa: BLE001 - failures are classified, not swallowed
        return _handle_failure(ticket, exc, resolved, worker_id=owner)


def _handle_failure(
    ticket: EvaluationTicket,
    exc: BaseException,
    resolved: ResolvedInput | None,
    *,
    worker_id: str | None = None,
) -> TicketOutcome:
    """Classify a failure and move the ticket to RETRY, FAILED, or NOT_APPLICABLE."""
    error_class = classify_error(exc)
    code = error_code_for(exc)
    message = str(exc)[:2000]
    log_fields = {
        "ticket_id": str(ticket.id),
        "job_id": str(ticket.job_id),
        "worker_id": worker_id,
        "check_id": ticket.check_id,
        "error_class": error_class.value,
        "error_code": code,
        "error": message,
        "attempt": ticket.attempt_count,
        "max_attempts": ticket.max_attempts,
    }
    if isinstance(exc, EvaluationError):
        logger.warning("ticket_failed", **log_fields)
    else:
        logger.exception("ticket_failed", **log_fields)

    if error_class is ErrorClass.MISSING_CONTEXT:
        output = EvaluatorOutput(
            status=ResultStatus.FAILED,
            passed=False,
            score=0.0,
            explanation=message,
            evidence={"resolution": resolved.as_snapshot()} if resolved else {},
            evaluator_type=ticket.evaluator,
            error_code=code,
            error_message=message,
        )
        return _persist(
            ticket,
            output,
            resolved,
            target=TicketStatus.FAILED,
            worker_id=worker_id,
            error_code=code,
            error_message=message,
        )

    if error_class is ErrorClass.TRANSIENT and ticket.attempt_count < ticket.max_attempts:
        delay = settings.backoff_for_attempt(ticket.attempt_count)
        try:
            with transaction() as conn:
                if TicketRepository(conn).get(ticket.id) is None:
                    return TicketOutcome(
                        ticket_id=ticket.id, status=TicketStatus.FAILED, error_code="TICKET_GONE"
                    )
                settle_ticket(
                    conn,
                    ticket.id,
                    TicketStatus.RETRY,
                    error_code=code,
                    error_message=message,
                    delay_seconds=delay,
                    job_id=ticket.job_id,
                    worker_id=worker_id,
                )
        except StaleWorkerError:
            return _stale_outcome(ticket, worker_id)
        return TicketOutcome(
            ticket_id=ticket.id,
            status=TicketStatus.RETRY,
            error_code=code,
            error_message=message,
            retry_in_seconds=delay,
        )

    final_code = "TRANSIENT_ATTEMPTS_EXHAUSTED" if error_class is ErrorClass.TRANSIENT else code
    output = EvaluatorOutput(
        status=ResultStatus.ERROR,
        passed=None,
        score=None,
        explanation=message,
        evidence={"resolution": resolved.as_snapshot()} if resolved else {},
        evaluator_type=ticket.evaluator,
        error_code=final_code,
        error_message=message,
    )
    return _persist(
        ticket,
        output,
        resolved,
        target=TicketStatus.FAILED,
        worker_id=worker_id,
        error_code=final_code,
        error_message=message,
    )


def evaluate_payload_check(
    payload_json: dict[str, Any],
    check: dict[str, Any],
    *,
    config_snapshot: dict[str, Any] | None = None,
) -> tuple[ResolvedInput, EvaluatorOutput]:
    """Resolve and evaluate without touching the database (used by tests/tools)."""
    resolved = resolve_input_mapping(payload_json, check.get("input_mapping"))
    evaluator = get_evaluator(check["evaluator"])
    context = ExecutionContext(
        job_id=uuid.uuid4(),
        ticket_id=uuid.uuid4(),
        payload_id=uuid.uuid4(),
        check_id=check.get("check_id", "adhoc"),
        check_type=CheckType(check.get("check_type", CheckType.DETERMINISTIC.value)),
        attempt=1,
        max_attempts=1,
        config_snapshot=config_snapshot or {},
    )
    return resolved, evaluator.run(dict(resolved.values), check, context)
