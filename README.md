# eval-platform

Database-first evaluation platform for multi-agent systems. PostgreSQL is the
durable source of truth for metric configuration, evaluation profiles, jobs,
tickets, and results. Payload JSON files are read from a local temporary folder
identified by `dataset_id`; they are not stored in PostgreSQL.

**Stack:** Python, FastAPI, PostgreSQL, psycopg 3, native parameterized SQL,
explicit transactions, JSONB, `SELECT ... FOR UPDATE SKIP LOCKED`, local
filesystem for temporary dataset input.

**Deliberately absent:** SQLAlchemy, Alembic, Redis, RQ, Celery, RabbitMQ, Kafka,
Pub/Sub, any broker queue, S3, MinIO, boto3, cloud object storage, Docker,
Docker Compose, dataset tables, payload tables, and external Agent Registry
lookups. There is no authoritative in-memory queue.

This project is independent of `evalforge-local`, which was used only as a
read-only reference. Nothing in that folder or its databases is modified.

---

## 1. Setup

### 1.1 Create the dedicated databases (administrator, once)

The application role does not need `CREATEDB` or superuser rights at runtime, so
database creation is a one-time administrative step. `eval_platform` and
`eval_platform_test` are dedicated databases.

```powershell
cd eval-platform
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -h localhost -f scripts/setup_databases.sql
```

Or, without `psql` on PATH:

```powershell
$env:PGADMIN_USER = "postgres"; $env:PGADMIN_PASSWORD = "<superuser password>"
python scripts/setup_databases.py
```

Both scripts are idempotent: existing databases are left untouched (creation is
skipped) and only the required privileges are re-granted and verified. Nothing is
dropped. Database names are configurable via the `\set` lines in the `.sql` file
or `--database` / `--test-database` / `PGDATABASE` / `TEST_PGDATABASE` for the
Python variant.

### 1.2 Configure and install

```powershell
copy .env.example .env      # then edit PGPASSWORD, API_KEY, OPENAI_API_KEY
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Required PostgreSQL settings (no other database URL scheme is needed):

| Variable | Meaning |
| --- | --- |
| `PGHOST` | Hostname (default `localhost`) |
| `PGPORT` | Port (default `5432`) |
| `PGDATABASE` | Application database (default `eval_platform`) |
| `PGUSER` | Application role |
| `PGPASSWORD` | Application role password |

`DATABASE_URL`, when set, overrides the `PG*` variables. Tests use
`TEST_PGDATABASE` (`eval_platform_test`).

### 1.3 Migrate

```powershell
.\.venv\Scripts\python.exe scripts\migrate.py upgrade
.\.venv\Scripts\python.exe scripts\migrate.py status
.\.venv\Scripts\python.exe scripts\check_connection.py
```

Migrations are plain `.sql` files applied once each, inside a transaction, under a
PostgreSQL advisory lock, and recorded with a checksum in `schema_migrations`.
Editing an applied migration is rejected: add a new file instead.

### 1.4 Seed a sample profile and dataset

```powershell
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U evalorches -d eval_platform -f examples/seed_profile.sql
New-Item -ItemType Directory -Force temp | Out-Null
Copy-Item -Recurse examples\dataset-folder-001 temp\dataset-folder-001
```

`EVALUATION_TEMP_ROOT` defaults to `./temp` (gitignored runtime folder). The
`dataset_id` in the job request is the folder name under that root.

### 1.5 Run

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload    # API on :8000
.\.venv\Scripts\python.exe -m runner.main                      # evaluation runner
```

The API and the runner are separate processes that share only the database. Run
as many runners as you like; `SKIP LOCKED` keeps them from colliding.

---

## 2. Data model

| Table | Purpose |
| --- | --- |
| `metric_records` | Versioned metric configuration (`definition_payload` JSONB). Each row is an exact metric version. |
| `evaluation_profiles` | Named, versioned collection of metrics. `evaluation_profile_id` is the string carried on each payload. |
| `evaluation_profile_metrics` | Maps a profile to exact `metric_record_id` values. Duplicate mappings are rejected. |
| `evaluation_jobs` | One evaluation request. `dataset_id` is a folder name, not a database FK. |
| `evaluation_tickets` | One payload file × one metric. Holds the immutable metric snapshot. |
| `evaluation_results` | Normalized evaluator output, unique on `ticket_id` (retries upsert). |
| `schema_migrations` | Migration ledger with checksums. |

The pre-profile tables `datasets`, `evaluation_payloads`, and `evaluation_configs`
were dropped by migration `011`, along with the `dataset_status` and
`evaluation_config_status` enums. There is no Dataset API, Payload API, or
evaluation-config API.

Key indexes: `ix_tickets_claim` (partial, for the claim query), `ix_tickets_lease`
(partial, for lease recovery), `uq_tickets_job_payload_metric`, and
`uq_results_ticket_id`.

### Lifecycles

- **Job:** `CREATED -> READY -> RUNNING -> COMPLETED | PARTIAL_FAILED | FAILED | CANCELLED`
- **Ticket:** `READY -> RUNNING -> DONE | RETRY | FAILED | CANCELLED | NOT_APPLICABLE`,
  `RETRY -> RUNNING`, and `FAILED -> READY` for an operator retry.
- **Result:** `PASSED | FAILED | NOT_APPLICABLE | ERROR`

Ticket transitions are enforced in code (`app/models/enums.py`) against the
committed row, which is locked before the check.

---

## 3. Temporary dataset input

```
{EVALUATION_TEMP_ROOT}/
└── dataset-folder-001/
    ├── payload_001.json
    ├── payload_002.json
    └── payload_003.json
```

`POST /v1/evaluation-jobs` with `{"dataset_id": "dataset-folder-001"}` resolves:

```
{EVALUATION_TEMP_ROOT}/dataset-folder-001/
```

Rules:

- `dataset_id` is a single folder name. Path separators and `..` are rejected.
- The resolved path must stay inside `EVALUATION_TEMP_ROOT`.
- Supported files are `*.json` objects. Other extensions are skipped (warning).
- Invalid JSON fails job creation with HTTP 400; no job or ticket rows are written.
- An empty folder (or a folder with no `.json` files) fails with HTTP 400.
- A missing folder fails with HTTP 404.
- Payloads are not copied into PostgreSQL.

At execution time the runner re-reads the same file. If it was deleted or moved,
the ticket fails permanently with `SOURCE_PAYLOAD_MISSING`.

Each payload must include:

```json
{
  "agent_registry": {
    "agent_id": "support-triage-agent",
    "agent_name": "Support Triage Agent",
    "agent_version": "1.0",
    "evaluation_profile_id": "agent-response-quality-v1"
  }
}
```

The profile is taken only from `payload.agent_registry.evaluation_profile_id`.
It is not inferred from producer, workflow, agent name, or spans. Each payload
is resolved independently; payloads in the same folder may use different
profiles.

---

## 4. Evaluation flow

```
temp folder (dataset_id)                   ->  read *.json payloads
payload.agent_registry.evaluation_profile_id
                                          ->  evaluation_profiles (PostgreSQL)
profile mappings                          ->  metric_records (exact versions)
job creation (one transaction)            ->  evaluation_jobs + evaluation_tickets
                                               (one ticket per payload × metric,
                                                snapshot frozen on the ticket)
runner: claim (FOR UPDATE SKIP LOCKED)    ->  ticket RUNNING with a lease
        load payload from temp folder
        load check from ticket snapshot
        context resolver                  ->  normalized evaluator input
        evaluator                         ->  normalized output
        persist                           ->  evaluation_results (upsert on ticket_id)
                                               ticket DONE/RETRY/FAILED/NOT_APPLICABLE
                                               job counters recomputed from tickets
```

The API never evaluates inside a request: it creates durable rows and returns.
Job counters are always recomputed by aggregating ticket rows, never incremented
from runner memory, so a crashed runner cannot corrupt them.

Retries keep the same job ID, ticket ID, payload file reference, metric record,
metric version, and configuration snapshot. They do not re-resolve the currently
active metric configuration.

---

## 5. Context resolver

Evaluators never dig through raw payloads. Each metric's `definition_payload`
declares an `input_mapping`, and the resolver turns the source payload into the
exact values the evaluator needs, recording how each value was found.

```json
"input_mapping": {
  "history": "session_context.conversation_history",
  "workflow": "trace_data.workflow_id",
  "category": "classifier.output.category",
  "tool": "validator.tool_calls[0].name",
  "second_agent": "order:2.agent_id",
  "agents": "spans.*.agent_id",
  "summary": { "path": "summarizer.output.summary", "required": false, "type": "string" }
}
```

Path forms: payload sections (`payload_metadata`, `trace_data`, `session_context`,
`spans`, `resolved_configuration`, `payload`), span-level paths by agent name, and
explicit selectors `span_id:`, `agent_id:`, `agent_type:`, `order:` (1-based
workflow position). List indexing (`[0]`), wildcards (`*`), and key traversal over
a list of objects are supported. Spans are normalized from either a list or an
agent-keyed object and ordered by explicit `order`, then `start_time`, then
document order.

Unresolvable context is never silently treated as a failure. It is reported with a
code (`MISSING_SPAN`, `MISSING_PATH`, `NULL_VALUE`, `INVALID_TYPE`,
`UNSUPPORTED_MAPPING`), and the check's `on_missing_context` policy decides:

- `not_applicable` (default): result `NOT_APPLICABLE`, ticket `NOT_APPLICABLE`
- `fail`: result `FAILED` with `error_code = MISSING_CONTEXT`

Optional mappings (`"required": false`) and defaults keep partially-populated
payloads evaluable. Payloads are not assumed to contain the same agents.

---

## 6. Evaluators

| Evaluator | Type | What it checks |
| --- | --- | --- |
| `required_fields` | deterministic | Mapped values exist and are non-empty (nested paths allowed). |
| `json_schema` | deterministic | Output structure against a JSON Schema subset. |
| `workflow_order` | deterministic | Agents ran in the expected order (`strict` or `subsequence`). |
| `span_exists` | deterministic | Required agent spans are present. |
| `tool_calls` | deterministic | Expected tools called, responses present, forbidden tools absent. |
| `cross_span_consistency` | deterministic | Values from different spans agree (exact/fuzzy/semantic/contains). |
| `field_comparison` | deterministic | Actual vs expected using exact/fuzzy/semantic or a custom verifier (`regex`, `contains_all`, `json_keys`, `length_bounds`). |
| `llm_judge` | LLM | Configurable rubric: faithfulness, clarity, completeness, appropriateness, policy compliance, resolution quality. |

`GET /v1/evaluators` lists them at runtime. Add one by subclassing
`Evaluator` and registering it in `app/evaluators/registry.py`.

The judge is configuration-driven: `criteria`, optional `prompt_template`,
provider/model/temperature/max_tokens, and `pass_threshold`. Its evidence records
the prompt, the raw response, the parsed verdict, token counts, latency, and an
estimated cost. An unparseable verdict is an `ERROR` result (retried while
attempts remain), never a silent failure.

---

## 7. API

Authentication: `X-API-Key` header (set `API_KEY`; optional when `APP_ENV` is a
development value).

All routes are served under a single `/v1` prefix. The dataset, payload, and
evaluation-config endpoints no longer exist in any form.

**Jobs, tickets, results**

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/v1/evaluation-jobs` | Create job + tickets in one transaction from a temp folder. |
| GET | `/v1/evaluation-jobs` · `/{id}` | Detail includes live ticket counts and the job snapshot. |
| POST | `/v1/evaluation-jobs/{id}/cancel` | Cancel pending work; running tickets settle as `CANCELLED`. |
| POST | `/v1/evaluation-jobs/{id}/retry-failed` | Reset `FAILED` tickets to `READY` (same snapshot). |
| GET | `/v1/evaluation-jobs/{id}/tickets` · `/tickets/summary` | Ticket inspection. |
| GET | `/v1/evaluation-jobs/{id}/results` · `/results/summary` | Results and PostgreSQL-side aggregates. |
| GET | `/v1/tickets/{id}` · `/v1/results/{id}` | Single item, including the frozen metric snapshot. |
| POST | `/v1/operations/recover-tickets` | Manually trigger the recovery sweep. |
| GET | `/health` · `/health/live` · `/health/ready` · `/metrics` | Probes and Prometheus metrics. |

### Walkthrough

```powershell
$h = @{ "X-API-Key" = "change-me"; "Content-Type" = "application/json" }

# 1. seed metrics + profile (once)
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U evalorches -d eval_platform -f examples/seed_profile.sql

# 2. copy sample payloads into the temp root
New-Item -ItemType Directory -Force temp | Out-Null
Copy-Item -Recurse examples\dataset-folder-001 temp\dataset-folder-001

# 3. create a job (returns immediately; tickets are durable)
$job = Invoke-RestMethod -Method Post -Uri http://localhost:8000/v1/evaluation-jobs -Headers $h -Body (@{
  dataset_id = "dataset-folder-001"
} | ConvertTo-Json)

# 4. let the runner drain the queue, then read results
.\.venv\Scripts\python.exe -m runner.main --drain
Invoke-RestMethod -Uri "http://localhost:8000/v1/evaluation-jobs/$($job.job.id)/results/summary" -Headers $h
```

Request body:

```json
{
  "dataset_id": "dataset-folder-001"
}
```

The caller does not supply a payload id, a dataset database id, an evaluation
configuration id, or a profile id. The profile comes from each payload.

Example: 3 payloads × 4 metrics on `agent-response-quality-v1` → 12 tickets.

---

## 8. Runner

```powershell
python -m runner.main                       # long-running
python -m runner.main --drain               # process what is runnable, then exit
python -m runner.main --concurrency 8 --batch-size 10
```

- Claims a batch inside one transaction with `FOR UPDATE SKIP LOCKED`, setting
  `RUNNING`, `worker_id`, `attempt_count`, and `lease_expires_at`.
- Executes with a bounded thread pool. LLM calls happen outside any transaction.
- Loads the source payload from `{EVALUATION_TEMP_ROOT}/{dataset_id}/{file}`.
- Uses the ticket's `metric_snapshot_json`, never the currently active metric row.
- Persists the result and settles the ticket in a single transaction, then
  recomputes job progress from ticket rows.
- Sweeps for recovery every `RUNNER_RECOVERY_INTERVAL_SECONDS`: expired leases go
  back to `RETRY` (or `FAILED` when attempts are exhausted), and running tickets
  on cancelled jobs become `CANCELLED`.
- On `SIGINT` / `SIGTERM` it stops claiming and waits up to
  `RUNNER_SHUTDOWN_GRACE_SECONDS` for in-flight tickets so their results are
  persisted. Anything still running loses its lease and is recovered later.

### Failure handling

| Class | Examples | Behaviour |
| --- | --- | --- |
| Transient | timeout, connection error, 429, 5xx | `RETRY` with backoff `RUNNER_RETRY_BACKOFF_SECONDS`, up to the ticket's `max_attempts`, then `FAILED` with `TRANSIENT_ATTEMPTS_EXHAUSTED`. |
| Permanent | unknown evaluator, invalid check config, 4xx, missing source payload | `FAILED` immediately, with an `ERROR` result for traceability. |
| Missing context | unresolvable mapping | `NOT_APPLICABLE` or `FAILED` per the check's policy. |
| Crash | runner killed | Lease expires; the sweep requeues the ticket. |
| Missing source file | payload deleted after job creation | Ticket `FAILED` with `SOURCE_PAYLOAD_MISSING`. Not retried. |

A retried ticket upserts onto the same `ticket_id` row, so retries never produce
duplicate results and always reuse the frozen snapshot.

---

## 9. Configuration

All settings come from the environment (see `.env.example`). Notable ones:

| Variable | Default | Meaning |
| --- | --- | --- |
| `PGHOST` / `PGPORT` / `PGDATABASE` / `PGUSER` / `PGPASSWORD` | localhost / 5432 / `eval_platform` / … | PostgreSQL connection. |
| `TEST_PGDATABASE` | `eval_platform_test` | Pytest database. |
| `PGSCHEMA` | `public` | Schema inside that database. |
| `DATABASE_URL` | — | Overrides the `PG*` variables when set. |
| `EVALUATION_TEMP_ROOT` | `./temp` | Root of dataset folders (`dataset_id` is a child directory). |
| `DB_POOL_MIN_SIZE` / `DB_POOL_MAX_SIZE` | 1 / 20 | psycopg pool bounds. |
| `RUNNER_MAX_CONCURRENCY` | 4 | Tickets in flight per runner. |
| `RUNNER_CLAIM_BATCH_SIZE` | 5 | Tickets per claim. |
| `RUNNER_LEASE_SECONDS` | 300 | Lease before a ticket is considered abandoned. |
| `RUNNER_MAX_ATTEMPTS` | 3 | Default attempts per ticket. |
| `RUNNER_RETRY_BACKOFF_SECONDS` | `30,60,120` | Backoff per attempt. |
| `DEFAULT_ON_MISSING_CONTEXT` | `not_applicable` | Platform-wide missing-context policy. |

---

## 10. Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Tests run against the real `eval_platform_test` database (migrations are applied
automatically) and skip with a clear message if it does not exist yet. Nothing at
the database layer is mocked: locking, transactions, enums, and JSONB behaviour are
what is being verified. Coverage includes context resolution, every evaluator,
metric definition validation, the migration ledger, job creation from temp folders,
profile extraction, concurrent claiming with two live connections, retry and
backoff, lease recovery, cancellation mid-flight, result idempotency, and two
runners sharing one job.

---

## 11. Layout

```
app/
  core/          config, psycopg pool + transactions, logging, metrics, API key auth
  db/            migration runner + migrations/*.sql
  models/        dataclass entities, enums, ticket state machine
  repositories/  hand-written parameterized SQL per table
  services/      temp-folder input, profile resolution, job/ticket/evaluation/recovery
  evaluators/    contract, registry, deterministic set, LLM judge + providers
  api/           FastAPI routers
  schemas/       pydantic request/response models
runner/          claim loop, concurrency, recovery, graceful shutdown
scripts/         setup_databases.sql|.py, migrate.py, check_connection.py
examples/        seed_profile.sql + sample dataset-folder-001 payloads
tests/           pytest suite against real PostgreSQL
```
