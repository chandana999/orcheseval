"""Evaluation job endpoints.

Creating a job creates durable tickets and returns immediately. No evaluation
runs inside an HTTP request; the runner picks the work up from PostgreSQL.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query

from app.dependencies import ApiKey, DbConnection
from app.models.enums import JobStatus, ResultStatus, TicketStatus
from app.repositories.job_repository import JobRepository
from app.repositories.result_repository import ResultRepository
from app.repositories.ticket_repository import TicketRepository
from app.schemas import (
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
from app.services.job_service import (
    JobCreationError,
    cancel_job,
    create_job,
    job_status_payload,
    retry_failed_tickets,
)
from app.services.recovery_service import recover_abandoned_tickets

router = APIRouter(prefix="/api/v1/evaluation-jobs", tags=["evaluation-jobs"])


@router.post("", response_model=EvaluationJobCreateResponse, status_code=201)
def create_evaluation_job(
    body: EvaluationJobCreate,
    conn: DbConnection,
    _api_key: ApiKey,
) -> EvaluationJobCreateResponse:
    """Create a job plus one ticket per payload x check, in one transaction."""
    try:
        with conn.transaction():
            result = create_job(
                conn,
                evaluation_config_id=body.evaluation_config_id,
                dataset_id=body.dataset_id,
                payload_ids=body.payload_ids,
                name=body.name,
                priority=body.priority,
                max_attempts=body.max_attempts,
                max_payloads=body.max_payloads,
            )
    except JobCreationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return EvaluationJobCreateResponse(
        job=EvaluationJobResponse.model_validate(result.job),
        ticket_count=result.ticket_count,
        payload_count=result.payload_count,
        check_ids=result.check_ids,
    )


@router.get("", response_model=EvaluationJobListResponse)
def list_jobs(
    conn: DbConnection,
    _api_key: ApiKey,
    status: JobStatus | None = None,
    dataset_id: uuid.UUID | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> EvaluationJobListResponse:
    total, items = JobRepository(conn).list(
        status=status, dataset_id=dataset_id, limit=limit, offset=offset
    )
    return EvaluationJobListResponse(
        total=total, items=[EvaluationJobResponse.model_validate(j) for j in items]
    )


@router.get("/{job_id}", response_model=EvaluationJobDetailResponse)
def get_job(
    job_id: uuid.UUID, conn: DbConnection, _api_key: ApiKey
) -> EvaluationJobDetailResponse:
    """Job detail with counters derived from durable ticket rows."""
    job = JobRepository(conn).get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Evaluation job not found")
    extra = job_status_payload(conn, job)
    return EvaluationJobDetailResponse(
        **EvaluationJobResponse.model_validate(job).model_dump(),
        config_snapshot_json=job.config_snapshot_json,
        **extra,
    )


@router.post("/{job_id}/cancel", response_model=JobCancellationResponse)
def cancel_evaluation_job(
    job_id: uuid.UUID, conn: DbConnection, _api_key: ApiKey
) -> JobCancellationResponse:
    """Cancel pending work. Tickets already running finish or settle as CANCELLED;
    completed results are preserved."""
    try:
        with conn.transaction():
            result = cancel_job(conn, job_id)
    except JobCreationError as exc:
        status_code = 404 if "not found" in str(exc) else 409
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc

    return JobCancellationResponse(
        job=EvaluationJobResponse.model_validate(result.job),
        cancelled_tickets=result.cancelled_tickets,
        running_tickets=result.running_tickets,
        detail=(
            f"cancelled {result.cancelled_tickets} pending ticket(s); "
            f"{result.running_tickets} running ticket(s) will settle as CANCELLED"
        ),
    )


@router.post("/{job_id}/retry-failed", response_model=RetryFailedResponse)
def retry_failed(
    job_id: uuid.UUID, conn: DbConnection, _api_key: ApiKey
) -> RetryFailedResponse:
    """Reset FAILED tickets to READY so the runner retries them."""
    try:
        with conn.transaction():
            job, count = retry_failed_tickets(conn, job_id)
    except JobCreationError as exc:
        status_code = 404 if "not found" in str(exc) else 409
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return RetryFailedResponse(
        job=EvaluationJobResponse.model_validate(job), reset_count=count
    )


@router.get("/{job_id}/tickets", response_model=TicketListResponse)
def list_job_tickets(
    job_id: uuid.UUID,
    conn: DbConnection,
    _api_key: ApiKey,
    status: TicketStatus | None = None,
    check_id: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> TicketListResponse:
    if JobRepository(conn).get(job_id) is None:
        raise HTTPException(status_code=404, detail="Evaluation job not found")
    total, items = TicketRepository(conn).list_by_job(
        job_id, status=status, check_id=check_id, limit=limit, offset=offset
    )
    return TicketListResponse(
        total=total,
        limit=limit,
        offset=offset,
        items=[EvaluationTicketResponse.model_validate(t) for t in items],
    )


@router.get("/{job_id}/tickets/summary", response_model=TicketSummaryResponse)
def ticket_summary(
    job_id: uuid.UUID, conn: DbConnection, _api_key: ApiKey
) -> TicketSummaryResponse:
    if JobRepository(conn).get(job_id) is None:
        raise HTTPException(status_code=404, detail="Evaluation job not found")
    return TicketSummaryResponse(
        job_id=job_id, counts=TicketRepository(conn).counts_by_status(job_id)
    )


@router.get("/{job_id}/results", response_model=ResultListResponse)
def list_job_results(
    job_id: uuid.UUID,
    conn: DbConnection,
    _api_key: ApiKey,
    status: ResultStatus | None = None,
    check_id: str | None = None,
    payload_id: uuid.UUID | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> ResultListResponse:
    if JobRepository(conn).get(job_id) is None:
        raise HTTPException(status_code=404, detail="Evaluation job not found")
    total, items = ResultRepository(conn).list_by_job(
        job_id,
        status=status,
        check_id=check_id,
        payload_id=payload_id,
        limit=limit,
        offset=offset,
    )
    return ResultListResponse(
        total=total,
        limit=limit,
        offset=offset,
        items=[EvaluationResultResponse.model_validate(r) for r in items],
    )


@router.get("/{job_id}/results/summary")
def result_summary(job_id: uuid.UUID, conn: DbConnection, _api_key: ApiKey) -> dict:
    """Pass rates and average scores aggregated in PostgreSQL."""
    job = JobRepository(conn).get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Evaluation job not found")
    summary = ResultRepository(conn).summary_by_job(job_id)
    summary["job_status"] = job.status.value
    summary["total_tickets"] = job.total_tickets
    return summary


# --------------------------------------------------------------- single items
tickets_router = APIRouter(prefix="/api/v1/tickets", tags=["tickets"])


@tickets_router.get("/{ticket_id}", response_model=EvaluationTicketDetailResponse)
def get_ticket(
    ticket_id: uuid.UUID, conn: DbConnection, _api_key: ApiKey
) -> EvaluationTicketDetailResponse:
    ticket = TicketRepository(conn).get(ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return EvaluationTicketDetailResponse.model_validate(ticket)


results_router = APIRouter(prefix="/api/v1/results", tags=["results"])


@results_router.get("/{result_id}", response_model=EvaluationResultDetailResponse)
def get_result(
    result_id: uuid.UUID, conn: DbConnection, _api_key: ApiKey
) -> EvaluationResultDetailResponse:
    result = ResultRepository(conn).get(result_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Result not found")
    return EvaluationResultDetailResponse.model_validate(result)


# ----------------------------------------------------------------- operations
ops_router = APIRouter(prefix="/api/v1/operations", tags=["operations"])


@ops_router.post("/recover-tickets", response_model=RecoveryResponse)
def recover_tickets(
    conn: DbConnection,
    _api_key: ApiKey,
    limit: int = Query(100, ge=1, le=1000),
) -> RecoveryResponse:
    """Manually trigger the recovery sweep the runner performs periodically."""
    with conn.transaction():
        report = recover_abandoned_tickets(conn, limit=limit)
    return RecoveryResponse(**report.as_dict())
