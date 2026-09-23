# Architecture

```
FastAPI
  -> job service
  -> PostgreSQL
       evaluation_jobs
       evaluation_tickets
       evaluation_results
  -> runner
  -> L5 evaluation layer
```

The API creates and reads durable state. It does not evaluate inside the request. The runner is a separate process.

## Queue

PostgreSQL is the durable queue. There is no broker.

`evaluation_jobs` is one requested evaluation. `evaluation_tickets` is one check against one payload file. `evaluation_results` is the outcome for a ticket, unique on `ticket_id`.

A repeated `POST /v1/evaluation-jobs` with the same `Idempotency-Key` and the same request body returns the existing job. The same key with a different body is rejected. The key and a hash of the request are stored on the job.

## Ticket lifecycle

`READY` or `RETRY` can be claimed. Claim sets `RUNNING`, `worker_id`, `lease_expires_at`, and increments `attempt_count`.

The claim query is `FOR UPDATE SKIP LOCKED`, so two workers do not take the same row.

Settlement updates a ticket only when `id`, `status = RUNNING`, and `worker_id` all match the worker that is finishing. A late worker cannot mark the ticket done or replace the current result.

From `RUNNING` a ticket becomes `DONE`, `RETRY`, `FAILED`, `CANCELLED`, or `NOT_APPLICABLE`. `FAILED` can be reset to `READY` by the retry-failed API. Job counters are recomputed from ticket rows in the same transaction as the settlement.

## Lease, retry, recovery

The claim sets a lease. While a ticket is still running, the runner extends that lease only for the owning worker (`lease_extended`). A fast evaluation finishes before the first heartbeat and uses the normal settlement path.

A transient evaluation error moves the ticket to `RETRY` with `available_at` set from the configured backoff plus a small jitter. At `max_attempts` the ticket becomes `FAILED`. Programming errors, including `AttributeError`, are permanent and are not retried as transient failures.

If the runner stops, it stops claiming, lets in-flight tickets finish, and stops their heartbeats. If the process dies, the lease expires. Recovery locks expired `RUNNING` rows with `SKIP LOCKED` and moves them to `RETRY`, `FAILED`, or `CANCELLED`. Startup also recomputes progress for jobs that still have active tickets.

## Evaluation and datasets

The runner loads the ticket snapshot from PostgreSQL and reads the payload file from `EVALUATION_TEMP_ROOT/<dataset_id>/`. `config.json` names the agent in `agentId` and lists `metrics`. Those metrics are copied onto the tickets when the job is created. The payload carries execution data under `agent_registry`, `payload_metadata`, `correlation`, and `trace_context.trace.spans`. Results are upserted on `ticket_id` only after the ownership check succeeds.

Dataset files are not copied into PostgreSQL. They must remain in place until the job no longer needs them.
