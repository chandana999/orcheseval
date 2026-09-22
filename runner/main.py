"""Entry point: python -m runner.main"""

from __future__ import annotations

import argparse
import sys

from runner.runner import EvaluationRunner, main as run_default


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="eval-platform evaluation runner")
    parser.add_argument("--worker-id", default=None)
    parser.add_argument("--concurrency", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=None)
    parser.add_argument("--poll-interval", type=float, default=None)
    parser.add_argument("--lease-seconds", type=int, default=None)
    parser.add_argument(
        "--drain",
        action="store_true",
        help="process everything currently runnable, then exit",
    )
    args = parser.parse_args(argv)

    if not any(
        [
            args.worker_id,
            args.concurrency,
            args.batch_size,
            args.poll_interval,
            args.lease_seconds,
            args.drain,
        ]
    ):
        return run_default()

    from app.core.database import close_pool  # noqa: F401 — disposed in finally
    from app.core.logging import setup_logging

    setup_logging()
    runner = EvaluationRunner(
        worker_id=args.worker_id,
        max_concurrency=args.concurrency,
        claim_batch_size=args.batch_size,
        poll_interval=args.poll_interval,
        lease_seconds=args.lease_seconds,
        drain=args.drain,
    )
    runner.install_signal_handlers()
    try:
        runner.run_forever()
    finally:
        close_pool()
    return 0


if __name__ == "__main__":
    sys.exit(main())
