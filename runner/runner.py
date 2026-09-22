"""Local evaluation runner.

PostgreSQL is the queue. The loop is:

    claim tickets (FOR UPDATE SKIP LOCKED, inside a transaction)
        -> execute each one with bounded concurrency
        -> persist result, transition ticket, recompute job progress
    periodically recover tickets whose runner died

There is no broker and no in-memory queue: if this process is killed, the leases
expire and another runner (or this one, restarted) picks the work back up.
"""

from __future__ import annotations

import signal
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor

from app.core.config import settings
from app.core.database import close_pool, get_engine, transaction
from app.core.logging import get_logger, setup_logging
from app.models.entities import EvaluationTicket
from app.services.evaluation_service import execute_ticket
from app.services.recovery_service import reconcile_active_jobs, recover_abandoned_tickets
from app.services.ticket_service import (
    build_worker_id,
    claim_tickets,
    heartbeat_ticket,
    refresh_ticket_gauges,
)

logger = get_logger(__name__)


class EvaluationRunner:
    def __init__(
        self,
        *,
        worker_id: str | None = None,
        max_concurrency: int | None = None,
        claim_batch_size: int | None = None,
        poll_interval: float | None = None,
        lease_seconds: int | None = None,
        recovery_interval: float | None = None,
        max_iterations: int | None = None,
        drain: bool = False,
    ) -> None:
        self.worker_id = worker_id or build_worker_id()
        self.max_concurrency = max_concurrency or settings.runner_max_concurrency
        self.claim_batch_size = claim_batch_size or settings.runner_claim_batch_size
        self.poll_interval = (
            poll_interval if poll_interval is not None else settings.runner_poll_interval_seconds
        )
        self.lease_seconds = lease_seconds or settings.runner_lease_seconds
        self.recovery_interval = (
            recovery_interval
            if recovery_interval is not None
            else settings.runner_recovery_interval_seconds
        )
        self.max_iterations = max_iterations
        self.drain = drain

        self._stop = threading.Event()
        self._inflight: set[Future] = set()
        self._lock = threading.Lock()
        self._last_recovery = 0.0
        self.processed = 0

    # ---------------------------------------------------------------- signals
    def install_signal_handlers(self) -> None:
        def handler(signum, _frame):
            logger.info("shutdown_signal_received", signal=signum, worker_id=self.worker_id)
            self._stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):  # pragma: no cover - non-main thread
                pass

    def request_stop(self) -> None:
        self._stop.set()

    # -------------------------------------------------------------- execution
    def _capacity(self) -> int:
        with self._lock:
            return max(0, self.max_concurrency - len(self._inflight))

    def _heartbeat(self, ticket: EvaluationTicket, stop: threading.Event) -> None:
        """Keep a long evaluation leased. A fast evaluation returns before this fires."""
        interval = max(1.0, self.lease_seconds / 3)
        while not stop.wait(interval):
            try:
                with transaction() as conn:
                    extended = heartbeat_ticket(
                        conn,
                        ticket.id,
                        worker_id=self.worker_id,
                        lease_seconds=self.lease_seconds,
                    )
                if extended is None:
                    return
            except Exception as exc:
                logger.warning(
                    "lease_heartbeat_failed",
                    ticket_id=str(ticket.id),
                    job_id=str(ticket.job_id),
                    worker_id=self.worker_id,
                    error=str(exc),
                )

    def _run_ticket(self, ticket: EvaluationTicket) -> None:
        stop_heartbeat = threading.Event()
        beater = threading.Thread(
            target=self._heartbeat, args=(ticket, stop_heartbeat), daemon=True
        )
        beater.start()
        try:
            outcome = execute_ticket(ticket, worker_id=self.worker_id)
            logger.info(
                "ticket_settled",
                ticket_id=str(ticket.id),
                job_id=str(ticket.job_id),
                worker_id=self.worker_id,
                check_id=ticket.check_id,
                evaluator=ticket.evaluator,
                status=outcome.status.value,
                result_status=outcome.result_status.value if outcome.result_status else None,
                error_code=outcome.error_code,
                attempt=ticket.attempt_count,
            )
        except Exception as exc:  # pragma: no cover - execute_ticket handles its own errors
            # Reaching here means persistence itself failed; the lease will expire
            # and the recovery sweep will requeue the ticket.
            logger.error(
                "ticket_settlement_failed",
                ticket_id=str(ticket.id),
                job_id=str(ticket.job_id),
                worker_id=self.worker_id,
                error=str(exc),
                exc_info=True,
            )
        finally:
            # Counted here rather than in a done callback: waiters are notified
            # before callbacks run, so the shutdown log could undercount.
            stop_heartbeat.set()
            with self._lock:
                self.processed += 1

    def _submit(self, executor: ThreadPoolExecutor, tickets: list[EvaluationTicket]) -> None:
        for ticket in tickets:
            future = executor.submit(self._run_ticket, ticket)
            with self._lock:
                self._inflight.add(future)

            def done(fut: Future, _self=self) -> None:
                with _self._lock:
                    _self._inflight.discard(fut)

            future.add_done_callback(done)

    def _recover(self) -> None:
        now = time.monotonic()
        if now - self._last_recovery < self.recovery_interval:
            return
        self._last_recovery = now
        try:
            with transaction() as conn:
                recover_abandoned_tickets(conn)
                refresh_ticket_gauges(conn)
        except Exception as exc:
            logger.error("recovery_sweep_failed", error=str(exc))

    def claim_once(self, limit: int | None = None) -> list[EvaluationTicket]:
        with transaction() as conn:
            return claim_tickets(
                conn,
                worker_id=self.worker_id,
                limit=limit or self.claim_batch_size,
                lease_seconds=self.lease_seconds,
            )

    def run_once(self, executor: ThreadPoolExecutor) -> int:
        """One iteration: recover, claim up to capacity, dispatch.

        Returns the number of tickets claimed, or -1 when every worker was busy
        and no claim query ran. Drain mode must not treat that as "no work
        left", or it would exit with claimable tickets still in the database.
        """
        self._recover()
        capacity = self._capacity()
        if capacity == 0:
            return -1
        try:
            tickets = self.claim_once(min(capacity, self.claim_batch_size))
        except Exception as exc:
            logger.error("claim_failed", error=str(exc))
            return 0
        if tickets:
            logger.info("tickets_claimed", count=len(tickets), worker_id=self.worker_id)
            self._submit(executor, tickets)
        return len(tickets)

    def run_forever(self) -> None:
        get_engine()
        logger.info(
            "runner_started",
            worker_id=self.worker_id,
            max_concurrency=self.max_concurrency,
            claim_batch_size=self.claim_batch_size,
            poll_interval=self.poll_interval,
            lease_seconds=self.lease_seconds,
            database=settings.pgdatabase,
        )
        try:
            with transaction() as conn:
                reconciled = reconcile_active_jobs(conn)
                recover_abandoned_tickets(conn)
            logger.info("startup_reconcile_complete", jobs=reconciled)
        except Exception as exc:
            logger.error("startup_reconcile_failed", error=str(exc))

        iterations = 0
        with ThreadPoolExecutor(
            max_workers=self.max_concurrency, thread_name_prefix="eval"
        ) as executor:
            try:
                while not self._stop.is_set():
                    claimed = self.run_once(executor)
                    iterations += 1
                    if self.max_iterations is not None and iterations >= self.max_iterations:
                        break
                    if self.drain and claimed == 0 and self._capacity() == self.max_concurrency:
                        break
                    if claimed <= 0:
                        self._stop.wait(self.poll_interval)
            finally:
                self._await_inflight()
        logger.info("runner_stopped", worker_id=self.worker_id, processed=self.processed)

    def _await_inflight(self) -> None:
        """Let claimed tickets finish so their results are persisted before exit."""
        deadline = time.monotonic() + settings.runner_shutdown_grace_seconds
        while time.monotonic() < deadline:
            with self._lock:
                pending = list(self._inflight)
            if not pending:
                return
            logger.info("waiting_for_inflight_tickets", count=len(pending))
            for future in pending:
                remaining = max(0.0, deadline - time.monotonic())
                try:
                    future.result(timeout=remaining)
                except Exception:
                    pass
        with self._lock:
            if self._inflight:
                # Anything still running loses its lease and is recovered later.
                logger.warning(
                    "shutdown_grace_exceeded",
                    abandoned=len(self._inflight),
                    note="expired leases will be recovered by a runner",
                )


def main() -> int:
    setup_logging()
    runner = EvaluationRunner()
    runner.install_signal_handlers()
    try:
        runner.run_forever()
    finally:
        close_pool()
    return 0
