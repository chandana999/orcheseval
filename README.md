# eval-platform

Internal service that evaluates agent execution payloads. The API records jobs and tickets in PostgreSQL. A separate runner claims that work and writes results. Dataset files stay on disk under `EVALUATION_TEMP_ROOT`.

## Architecture

```
FastAPI
  -> job service
  -> PostgreSQL (evaluation_jobs, evaluation_tickets, evaluation_results)
  -> runner
  -> L5 evaluators
```

PostgreSQL is the queue. Claiming uses `FOR UPDATE SKIP LOCKED`. See `docs/architecture.md` for the ticket lifecycle and `docs/operations.md` for running it.

The Python package is `src/evalorch`. Imports are `evalorch.*`. `pip install -e .` is what puts that package on the path for uvicorn and the runner.

## Prerequisites

- Python 3.11+
- PostgreSQL
- A virtual environment with the packages in `requirements.txt`

Development tools (not required to run the service):

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

## Configuration

Copy `.env.example` to `.env`. Important variables:

| Variable | Purpose |
| --- | --- |
| `APP_ENV` | Development when the value is `development`, `dev`, `local`, or `test`. Any other value is production. |
| `API_KEY` | Sent as `X-API-Key`. Required unless the development bypass below is turned on. |
| `ALLOW_UNAUTHENTICATED_DEV` | Default `false`. Set `true` only to allow requests with no API key, and only when `APP_ENV` is a development value. Production never skips authentication. |
| `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD` | PostgreSQL connection. |
| `DB_STATEMENT_TIMEOUT_MS` | PostgreSQL `statement_timeout` in milliseconds. Default `30000`. `0` disables it. |
| `DB_IDLE_IN_TRANSACTION_TIMEOUT_MS` | PostgreSQL `idle_in_transaction_session_timeout` in milliseconds. Default `30000`. `0` disables it. Raise both if this configuration is also used for long migrations. |
| `EVALUATION_TEMP_ROOT` | Directory whose subfolders are datasets. |
| `CORS_ORIGINS` | Comma-separated browser origins. `*` allows any origin and does not enable credentialed CORS. |
| `RUNNER_LEASE_SECONDS`, `RUNNER_MAX_ATTEMPTS`, `RUNNER_RETRY_BACKOFF_SECONDS` | Lease and retry policy. |

Invalid ports, concurrency, lease, attempt, backoff, and recovery values fail at startup. Do not put real secrets in logs or in this file.

## Database

Create the application database, then apply migrations:

```powershell
.\.venv\Scripts\python.exe scripts\migrate.py upgrade
```

Alembic revisions: `0001_initial`, then `0002_job_idempotency`.

## Run

API:

```powershell
.\.venv\Scripts\python.exe -m uvicorn evalorch.main:app --host 127.0.0.1 --port 8001
```

Runner (separate process):

```powershell
.\.venv\Scripts\python.exe -m runner.main
```

Create a job with `POST /v1/evaluation-jobs` and `{"dataset_id": "<folder>"}`. The folder must exist under `EVALUATION_TEMP_ROOT` and contain payload JSON files plus `config.json`. Repeat the same create with header `Idempotency-Key` to get the original job back.

## Checks

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
```

Pytest uses `TEST_PGDATABASE` (default `eval_platform_test`).

## Health and metrics

- `GET /health` — API and PostgreSQL summary
- `GET /health/live` — process is up; does not check PostgreSQL
- `GET /health/ready` — 200 only when PostgreSQL answers; 503 otherwise
- `GET /metrics` — Prometheus metrics

Failed API calls return:

```json
{"error": {"code": "NOT_FOUND", "message": "...", "request_id": "..."}}
```

The same id is returned in `X-Request-ID`.
