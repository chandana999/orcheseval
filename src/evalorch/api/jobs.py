"""Evaluation job endpoints.

Creating a job creates durable tickets and returns immediately. No evaluation
runs inside an HTTP request; the runner picks the work up from PostgreSQL.

Mounted under `/v1`.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Header, HTTPException, Query, Response
from sqlalchemy.exc import IntegrityError

from evalorch.core.logging import get_logger
from evalorch.dependencies import ApiKey, DbSession
from evalorch.models.enums import JobStatus, ResultStatus, TicketStatus
from evalorch.repositories.job_repository import JobRepository
from evalorch.repositories.result_repository import ResultRepository
from evalorch.repositories.ticket_repository import TicketRepository
from evalorch.schemas import (
    EvaluationJobCreate,
    EvaluationJobCreateResponse,
    EvaluationJobDetailResponse,
    EvaluationJobListResponse,
    EvaluationJobResponse,
    EvaluationResultDetailResponse,
    EvaluationResultResponse,
    EvaluationTicketDetailResponse,
    EvaluationTicketResponse,
    JobCancellationResponse,
    RecoveryResponse,
    ResultListResponse,
    RetryFailedResponse,
    TicketListResponse,
    TicketSummaryResponse,
)
from evalorch.services.job_service import (
    JobCreationError,
    cancel_job,
    create_job,
    job_status_payload,
    retry_failed_tickets,
)
from evalorch.services.recovery_service import recover_abandoned_tickets

router = APIRouter(tags=["evaluation-jobs"])
logger = get_logger(__name__)


def _http_error(exc: JobCreationError) -> HTTPException:
    return HTTPException(status_code=getattr(exc, "status_code", 400), detail=str(exc))


def _log_job_creation_failed(dataset_id: str, exc: JobCreationError) -> None:
    status_code = int(getattr(exc, "status_code", 400) or 400)
    codes = {400: "BAD_REQUEST", 404: "NOT_FOUND", 409: "CONFLICT"}
    logger.warning(
        "job_creation_failed",
        dataset_id=dataset_id,
        status_code=status_code,
        error_code=codes.get(status_code, "BAD_REQUEST"),
        error_type=type(exc).__name__,
    )


@router.post("/evaluation-jobs", response_model=EvaluationJobCreateResponse, status_code=201)
def create_evaluation_job(
    body: EvaluationJobCreate,
    session: DbSession,
    response: Response,
    _api_key: ApiKey,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> EvaluationJobCreateResponse:
    """Create a job plus one ticket per payload x metric, in one transaction."""

    def _create():
        with session.begin():
            return create_job(
                session,
                dataset_id=body.dataset_id,
                name=body.name,
                priority=body.priority,
                max_attempts=body.max_attempts,
                idempotency_key=idempotency_key,
            )

    try:
        result = _create()
    except IntegrityError:
        try:
            result = _create()
        except JobCreationError as exc:
            _log_job_creation_failed(body.dataset_id, exc)
            raise _http_error(exc) from exc
    except JobCreationError as exc:
        _log_job_creation_failed(body.dataset_id, exc)
        raise _http_error(exc) from exc
    if result.replayed:
        response.status_code = 200

    return EvaluationJobCreateResponse(
        job=EvaluationJobResponse.model_validate(result.job),
        ticket_count=result.ticket_count,
        payload_count=result.payload_count,
        dataset_id=result.dataset_id,
        metric_ids=result.metric_ids,
        profiles=result.profiles,
        tickets_summary=result.tickets_summary,
        warnings=result.warnings,
    )


@router.get("/evaluation-jobs", response_model=EvaluationJobListResponse)
def list_jobs(
    session: DbSession,
    _api_key: ApiKey,
    status: JobStatus | None = None,
    dataset_id: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> EvaluationJobListResponse:
    total, items = JobRepository(session).list(
        status=status, dataset_id=dataset_id, limit=limit, offset=offset
    )
    return EvaluationJobListResponse(
        total=total, items=[EvaluationJobResponse.model_validate(j) for j in items]
    )


@router.get("/evaluation-jobs/{job_id}", response_model=EvaluationJobDetailResponse)
def get_job(job_id: uuid.UUID, session: DbSession, _api_key: ApiKey) -> EvaluationJobDetailResponse:
    """Job detail with counters derived from durable ticket rows."""
    job = JobRepository(session).get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Evaluation job not found")
    extra = job_status_payload(session, job)
    return EvaluationJobDetailResponse(
        **EvaluationJobResponse.model_validate(job).model_dump(),
        config_snapshot_json=job.config_snapshot_json,
        **extra,
    )


@router.post("/evaluation-jobs/{job_id}/cancel", response_model=JobCancellationResponse)
def cancel_evaluation_job(
    job_id: uuid.UUID, session: DbSession, _api_key: ApiKey
) -> JobCancellationResponse:
    """Cancel pending work. Tickets already running finish or settle as CANCELLED;
    completed results are preserved."""
    logger.info("job_cancel_requested", job_id=str(job_id))
    try:
        with session.begin():
            result = cancel_job(session, job_id)
    except JobCreationError as exc:
        raise _http_error(exc) from exc

    return JobCancellationResponse(
        job=EvaluationJobResponse.model_validate(result.job),
        cancelled_tickets=result.cancelled_tickets,
        running_tickets=result.running_tickets,
        detail=(
            f"cancelled {result.cancelled_tickets} pending ticket(s); "
            f"{result.running_tickets} running ticket(s) will settle as CANCELLED"
        ),
    )


@router.post("/evaluation-jobs/{job_id}/retry-failed", response_model=RetryFailedResponse)
def retry_failed(job_id: uuid.UUID, session: DbSession, _api_key: ApiKey) -> RetryFailedResponse:
    """Reset FAILED tickets to READY so the runner retries them with the same snapshot."""
    try:
        with session.begin():
            job, count = retry_failed_tickets(session, job_id)
    except JobCreationError as exc:
        raise _http_error(exc) from exc
    return RetryFailedResponse(job=EvaluationJobResponse.model_validate(job), reset_count=count)


@router.get("/evaluation-jobs/{job_id}/tickets", response_model=TicketListResponse)
def list_job_tickets(
    job_id: uuid.UUID,
    session: DbSession,
    _api_key: ApiKey,
    status: TicketStatus | None = None,
    check_id: str | None = None,
    metric_id: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> TicketListResponse:
    if JobRepository(session).get(job_id) is None:
        raise HTTPException(status_code=404, detail="Evaluation job not found")
    total, items = TicketRepository(session).list_by_job(
        job_id,
        status=status,
        check_id=check_id,
        metric_id=metric_id,
        limit=limit,
        offset=offset,
    )
    return TicketListResponse(
        total=total,
        limit=limit,
        offset=offset,
        items=[EvaluationTicketResponse.model_validate(t) for t in items],
    )


@router.get("/evaluation-jobs/{job_id}/tickets/summary", response_model=TicketSummaryResponse)
def ticket_summary(
    job_id: uuid.UUID, session: DbSession, _api_key: ApiKey
) -> TicketSummaryResponse:
    if JobRepository(session).get(job_id) is None:
        raise HTTPException(status_code=404, detail="Evaluation job not found")
    return TicketSummaryResponse(
        job_id=job_id, counts=TicketRepository(session).counts_by_status(job_id)
    )


@router.get("/evaluation-jobs/{job_id}/results", response_model=ResultListResponse)
def list_job_results(
    job_id: uuid.UUID,
    session: DbSession,
    _api_key: ApiKey,
    status: ResultStatus | None = None,
    check_id: str | None = None,
    payload_id: uuid.UUID | None = None,
    source_payload_ref: str | None = None,
    metric_id: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> ResultListResponse:
    if JobRepository(session).get(job_id) is None:
        raise HTTPException(status_code=404, detail="Evaluation job not found")
    total, items = ResultRepository(session).list_by_job(
        job_id,
        status=status,
        check_id=check_id,
        payload_id=payload_id,
        source_payload_ref=source_payload_ref,
        metric_id=metric_id,
        limit=limit,
        offset=offset,
    )
    return ResultListResponse(
        total=total,
        limit=limit,
        offset=offset,
        items=[EvaluationResultResponse.model_validate(r) for r in items],
    )


@router.get("/evaluation-jobs/{job_id}/results/summary")
def result_summary(job_id: uuid.UUID, session: DbSession, _api_key: ApiKey) -> dict:
    """Pass rates and average scores aggregated in PostgreSQL."""
    job = JobRepository(session).get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Evaluation job not found")
    summary = ResultRepository(session).summary_by_job(job_id)
    summary["job_status"] = job.status.value
    summary["total_tickets"] = job.total_tickets
    return summary


tickets_router = APIRouter(tags=["tickets"])


@tickets_router.get("/tickets/{ticket_id}", response_model=EvaluationTicketDetailResponse)
def get_ticket(
    ticket_id: uuid.UUID, session: DbSession, _api_key: ApiKey
) -> EvaluationTicketDetailResponse:
    ticket = TicketRepository(session).get(ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return EvaluationTicketDetailResponse.model_validate(ticket)


results_router = APIRouter(tags=["results"])


@results_router.get("/results/{result_id}", response_model=EvaluationResultDetailResponse)
def get_result(
    result_id: uuid.UUID, session: DbSession, _api_key: ApiKey
) -> EvaluationResultDetailResponse:
    result = ResultRepository(session).get(result_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Result not found")
    return EvaluationResultDetailResponse.model_validate(result)


ops_router = APIRouter(tags=["operations"])


@ops_router.post("/operations/recover-tickets", response_model=RecoveryResponse)
def recover_tickets(
    session: DbSession,
    _api_key: ApiKey,
    limit: int | None = Query(default=None, ge=1, le=1000),
) -> RecoveryResponse:
    """Manually trigger the recovery sweep the runner performs periodically.

    An omitted limit uses the configured recovery batch size.
    """
    with session.begin():
        report = recover_abandoned_tickets(session, limit=limit)
    return RecoveryResponse(**report.as_dict())
