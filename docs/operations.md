# Operations

## Configuration

Copy `.env.example` to `.env`. The process fails at startup when a port, pool size, concurrency, claim batch, lease, max attempts, retry list, recovery interval, or timeout is invalid.

`APP_ENV` is development only for `development`, `dev`, `local`, or `test`. Any other value is production. `API_KEY` is required. The only exception is `ALLOW_UNAUTHENTICATED_DEV=true` together with a development `APP_ENV`, which logs `api_auth_bypass_active` at startup. Production does not run without an API key.

`DB_STATEMENT_TIMEOUT_MS` and `DB_IDLE_IN_TRANSACTION_TIMEOUT_MS` are PostgreSQL session limits (default 30000). They are separate from `DB_CONNECT_TIMEOUT`. `0` disables a limit. Increase them when the same configuration runs long administrative queries.

`CORS_ORIGINS` is a comma-separated list. The default `*` allows browser origins and does not send credentialed CORS headers. Set an explicit origin, such as `http://localhost:3000`, only when a browser client must send credentials.

`EVALUATION_TEMP_ROOT` is the dataset directory. `dataset_id` is one folder name under that root. The API rejects paths that leave the root.

## Database

```powershell
.\.venv\Scripts\python.exe scripts\migrate.py upgrade
.\.venv\Scripts\python.exe scripts\migrate.py status
```

Current revisions: `0001_initial`, `0002_job_idempotency`.

## Processes

API:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

Runner:

```powershell
.\.venv\Scripts\python.exe -m runner.main
```

Stop the runner with SIGTERM or Ctrl+C. It stops claiming, waits for current tickets to finish (`RUNNER_SHUTDOWN_GRACE_SECONDS`), and stops lease heartbeats. Tickets still running after a crash are recovered when their lease expires.

## Health and metrics

| Endpoint | Meaning |
| --- | --- |
| `GET /health/live` | Process is up. Does not contact PostgreSQL. |
| `GET /health/ready` | 200 when PostgreSQL answers. 503 with `postgres: unavailable` when it does not. The database error is logged, not returned. |
| `GET /health` | Same database check, always HTTP 200, with `postgres` of `ok` or `unavailable`. |
| `GET /metrics` | Prometheus counters and gauges. |

Ticket gauges distinguish `READY`, `RETRY`, `RUNNING`, and `FAILED`. Recovery, evaluation duration, evaluation outcomes, and LLM token counters are on the same endpoint.

## Logs

The API and runner write structured JSON to stdout. Records include `timestamp`, `level`, `event`, `service`, and `environment`. Request handling also includes `request_id`, returned as `X-Request-ID`.

Useful events: `job_creation_failed`, `job_cancel_requested`, `tickets_claimed`, `ticket_settled`, `lease_extended`, `tickets_recovered`, `ticket_failed`.

Logs do not include API keys, passwords, or full payloads. API error bodies use `error.code`, `error.message`, and `error.request_id`. They do not include stack traces or database errors.

## Failed jobs and stuck tickets

- Job status and ticket counts: `GET /v1/evaluation-jobs/{job_id}`.
- Ticket error codes: `GET /v1/evaluation-jobs/{job_id}/tickets`.
- Results: `GET /v1/evaluation-jobs/{job_id}/results`.
- Reset `FAILED` tickets to `READY`: `POST /v1/evaluation-jobs/{job_id}/retry-failed`.
- A ticket left `RUNNING` by a dead runner returns to `RETRY` after `lease_expires_at`, or `FAILED` when attempts are exhausted. Search logs for `tickets_recovered`.

## Datasets

Payloads and `config.json` live under `EVALUATION_TEMP_ROOT/<dataset_id>/`. They are read again when each ticket runs. Keep those files until every ticket for that job has finished. Removing a file causes the ticket to fail as a missing source payload.

Dataset files are not stored in PostgreSQL and are not moved to object storage.
