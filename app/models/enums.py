"""Lifecycle enums and the ticket state machine.

Enum values match the PostgreSQL enum labels exactly (uppercase).
"""

from __future__ import annotations

import enum


class JobStatus(str, enum.Enum):
    CREATED = "CREATED"
    READY = "READY"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL_FAILED = "PARTIAL_FAILED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TicketStatus(str, enum.Enum):
    READY = "READY"
    RUNNING = "RUNNING"
    RETRY = "RETRY"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class CheckType(str, enum.Enum):
    DETERMINISTIC = "DETERMINISTIC"
    LLM_JUDGE = "LLM_JUDGE"


class ResultStatus(str, enum.Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    ERROR = "ERROR"


class OnMissingContext(str, enum.Enum):
    """Documented policy for a check whose required context cannot be resolved."""

    NOT_APPLICABLE = "not_applicable"
    FAIL = "fail"


class ErrorClass(str, enum.Enum):
    TRANSIENT = "TRANSIENT"
    PERMANENT = "PERMANENT"
    MISSING_CONTEXT = "MISSING_CONTEXT"


JOB_TERMINAL_STATUSES: frozenset[JobStatus] = frozenset(
    {
        JobStatus.COMPLETED,
        JobStatus.PARTIAL_FAILED,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
    }
)

TICKET_TERMINAL_STATUSES: frozenset[TicketStatus] = frozenset(
    {
        TicketStatus.DONE,
        TicketStatus.FAILED,
        TicketStatus.CANCELLED,
        TicketStatus.NOT_APPLICABLE,
    }
)

# READY -> RUNNING -> {DONE, RETRY, FAILED, CANCELLED, NOT_APPLICABLE}
# FAILED -> READY is the operator-triggered retry of failed tickets.
ALLOWED_TICKET_TRANSITIONS: dict[TicketStatus, frozenset[TicketStatus]] = {
    TicketStatus.READY: frozenset({TicketStatus.RUNNING, TicketStatus.CANCELLED}),
    TicketStatus.RUNNING: frozenset(
        {
            TicketStatus.DONE,
            TicketStatus.RETRY,
            TicketStatus.FAILED,
            TicketStatus.CANCELLED,
            TicketStatus.NOT_APPLICABLE,
        }
    ),
    TicketStatus.RETRY: frozenset(
        {TicketStatus.RUNNING, TicketStatus.CANCELLED, TicketStatus.FAILED}
    ),
    TicketStatus.DONE: frozenset(),
    TicketStatus.FAILED: frozenset({TicketStatus.READY}),
    TicketStatus.CANCELLED: frozenset(),
    TicketStatus.NOT_APPLICABLE: frozenset(),
}


class IllegalTicketTransition(RuntimeError):
    def __init__(self, current: TicketStatus, target: TicketStatus) -> None:
        super().__init__(f"illegal ticket transition {current.value} -> {target.value}")
        self.current = current
        self.target = target


def assert_ticket_transition(current: TicketStatus, target: TicketStatus) -> None:
    if target not in ALLOWED_TICKET_TRANSITIONS.get(current, frozenset()):
        raise IllegalTicketTransition(current, target)


def is_terminal_ticket_status(status: TicketStatus) -> bool:
    return status in TICKET_TERMINAL_STATUSES


def is_terminal_job_status(status: JobStatus) -> bool:
    return status in JOB_TERMINAL_STATUSES
