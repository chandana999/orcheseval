# eval-platform

Database-first evaluation platform for multi-agent systems. PostgreSQL is the
durable source of truth for every piece of evaluation work: payloads,
configurations, jobs, tickets, and results all live in tables, and the runner
takes its work from the database rather than from memory.

**Stack:** Python, FastAPI, PostgreSQL, psycopg 3, native parameterized SQL,
explicit transactions, JSONB, `SELECT ... FOR UPDATE SKIP LOCKED`, local
filesystem for optional file ingestion.

**Deliberately absent:** SQLAlchemy, Alembic, Redis, RQ, Celery, RabbitMQ, Kafka,
Pub/Sub, any broker queue, S3, MinIO, boto3, cloud object storage, Docker,
Docker Compose. There is no authoritative in-memory queue anywhere.

This project is independent of `evalforge-local`, which was used only as a
read-only reference. Nothing in that folder or its databases is modified.

---

## 1. Setup

### 1.1 Create the dedicated databases (administrator, once)

The application role does not need `CREATEDB` or superuser rights at runtime, so
database creation is a one-time administrative step. `eval_platform` and
`eval_platform_test` are dedicated databases; the existing `evalforge_local`
databases are never used.

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

### 1.3 Migrate

```powershell
.\.venv\Scripts\python.exe scripts\migrate.py upgrade
.\.venv\Scripts\python.exe scripts\migrate.py status
.\.venv\Scripts\python.exe scripts\check_connection.py
```

Migrations are plain `.sql` files applied once each, inside a transaction, under a
PostgreSQL advisory lock, and recorded with a checksum in `schema_migrations`.
Editing an applied migration is rejected: add a new file instead.

### 1.4 Run

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
| `datasets` | Logical grouping of payloads. Metadata only. |
| `evaluation_payloads` | Complete agent execution payloads in `payload_json` (JSONB), plus indexed `trace_id` / `session_id` / `workflow_id` / `external_payload_id`. |
| `evaluation_configs` | Versioned evaluation definitions in `config_json` (JSONB), unique on `(name, version)`. |
| `evaluation_jobs` | One evaluation request, with an immutable `config_snapshot_json` and counters derived from tickets. |
| `evaluation_tickets` | One payload x one check. The durable unit of work. |
| `evaluation_results` | Normalized evaluator output, unique on `(job_id, payload_id, check_id)`. |
| `schema_migrations` | Migration ledger with checksums. |

Key indexes: `ix_tickets_claim` (partial, for the claim query), `ix_tickets_lease`
(partial, for lease recovery), `ix_payloads_payload_json` (GIN),
`uq_tickets_job_payload_check`, and `uq_results_job_payload_check`.

### Lifecycles

- **Dataset:** `CREATED -> INGESTING -> READY` (`FAILED`, `ARCHIVED`)
- **Job:** `CREATED -> READY -> RUNNING -> COMPLETED | PARTIAL_FAILED | FAILED | CANCELLED`
- **Ticket:** `READY -> RUNNING -> DONE | RETRY | FAILED | CANCELLED | NOT_APPLICABLE`,
  `RETRY -> RUNNING`, and `FAILED -> READY` for an operator retry.
- **Result:** `PASSED | FAILED | NOT_APPLICABLE | ERROR`

Ticket transitions are enforced in code (`app/models/enums.py`) against the
committed row, which is locked before the check.

---

## 3. Evaluation flow

```
payload ingestion (API or optional file)   ->  evaluation_payloads (JSONB)
evaluation configuration                   ->  evaluation_configs (JSONB, versioned)
job creation                               ->  evaluation_jobs + evaluation_tickets
                                               (one ticket per payload x check)
runner: claim (FOR UPDATE SKIP LOCKED)     ->  ticket RUNNING with a lease
        context resolver                   ->  normalized evaluator input
        evaluator                          ->  normalized output
        persist                            ->  evaluation_results (upsert)
                                               ticket DONE/RETRY/FAILED/NOT_APPLICABLE
                                               job counters recomputed from tickets
```

The API never evaluates inside a request: it creates durable rows and returns.
Job counters are always recomputed by aggregating ticket rows, never incremented
from runner memory, so a crashed runner cannot corrupt them.

---

## 4. Context resolver

Evaluators never dig through raw payloads. Each check declares an `input_mapping`,
and the resolver turns the stored payload into the exact values the evaluator
needs, recording how each value was found.

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

## 5. Evaluators

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

`GET /api/v1/evaluators` lists them at runtime. Add one by subclassing
`Evaluator` and registering it in `app/evaluators/registry.py`.

The judge is configuration-driven: `criteria`, optional `prompt_template`,
provider/model/temperature/max_tokens, and `pass_threshold`. Its evidence records
the prompt, the raw response, the parsed verdict, token counts, latency, and an
estimated cost. An unparseable verdict is an `ERROR` result (retried while
attempts remain), never a silent failure.

---

## 6. API

Authentication: `X-API-Key` header (set `API_KEY`; optional when `APP_ENV` is a
development value).

**Datasets and payloads**

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/api/v1/datasets` | Create a dataset, optionally with payloads. No file involved. |
| POST | `/api/v1/datasets/upload` | Optional `.json` / `.jsonl` / `.ndjson` ingestion. |
| GET | `/api/v1/datasets` · `/{id}` | List / fetch. |
| POST | `/api/v1/datasets/{id}/payloads` | Add payloads to an existing dataset. |
| GET | `/api/v1/datasets/{id}/export` | Stream payloads back as JSONL from PostgreSQL. |
| POST | `/api/v1/payloads` · `/bulk` | Insert one or many complete payloads. |
| GET | `/api/v1/payloads` · `/{id}` | Filter by dataset, trace, or session. |

**Configurations**

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/api/v1/evaluation-configs` | Validate, normalize, and store a new version. |
| POST | `/api/v1/evaluation-configs/validate` | Dry-run validation. |
| GET | `/api/v1/evaluation-configs` · `/{id}` | List / fetch. |
| PATCH | `/api/v1/evaluation-configs/{id}/status` | `DRAFT` / `ACTIVE` / `DEPRECATED`. Checks stay immutable per version. |

**Jobs, tickets, results**

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/api/v1/evaluation-jobs` | Create job + tickets in one transaction. |
| GET | `/api/v1/evaluation-jobs` · `/{id}` | Detail includes live ticket counts and the config snapshot. |
| POST | `/api/v1/evaluation-jobs/{id}/cancel` | Cancel pending work; running tickets settle as `CANCELLED`. |
| POST | `/api/v1/evaluation-jobs/{id}/retry-failed` | Reset `FAILED` tickets to `READY`. |
| GET | `/api/v1/evaluation-jobs/{id}/tickets` · `/tickets/summary` | Ticket inspection. |
| GET | `/api/v1/evaluation-jobs/{id}/results` · `/results/summary` | Results and PostgreSQL-side aggregates. |
| GET | `/api/v1/tickets/{id}` · `/api/v1/results/{id}` | Single item with input snapshot. |
| POST | `/api/v1/operations/recover-tickets` | Manually trigger the recovery sweep. |
| GET | `/health` · `/health/live` · `/health/ready` · `/metrics` | Probes and Prometheus metrics. |

### Walkthrough

```powershell
$h = @{ "X-API-Key" = "change-me"; "Content-Type" = "application/json" }

# 1. store a payload inside a dataset
$ds = Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/v1/datasets -Headers $h -Body (@{
  name = "support-traces"
  payloads = @((Get-Content examples/payload.json -Raw | ConvertFrom-Json))
} | ConvertTo-Json -Depth 20)

# 2. store an evaluation configuration
$cfg = Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/v1/evaluation-configs -Headers $h `
  -Body (Get-Content examples/evaluation_config.json -Raw)

# 3. create a job (returns immediately; tickets are durable)
$job = Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/v1/evaluation-jobs -Headers $h -Body (@{
  evaluation_config_id = $cfg.id
  dataset_id = $ds.dataset_id
  name = "nightly"
} | ConvertTo-Json)

# 4. let the runner drain the queue, then read results
.\.venv\Scripts\python.exe -m runner.main --drain
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/evaluation-jobs/$($job.job.id)/results/summary" -Headers $h
```

---

## 7. Runner

```powershell
python -m runner.main                       # long-running
python -m runner.main --drain               # process what is runnable, then exit
python -m runner.main --concurrency 8 --batch-size 10
```

- Claims a batch inside one transaction with `FOR UPDATE SKIP LOCKED`, setting
  `RUNNING`, `worker_id`, `attempt_count`, and `lease_expires_at`.
- Executes with a bounded thread pool. LLM calls happen outside any transaction.
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
| Permanent | unknown evaluator, invalid check config, 4xx | `FAILED` immediately, with an `ERROR` result for traceability. |
| Missing context | unresolvable mapping | `NOT_APPLICABLE` or `FAILED` per the check's policy. |
| Crash | runner killed | Lease expires; the sweep requeues the ticket. |

A retried ticket upserts onto the same `(job_id, payload_id, check_id)` row, so
retries never produce duplicate results.

---

## 8. Configuration

All settings come from the environment (see `.env.example`). Notable ones:

| Variable | Default | Meaning |
| --- | --- | --- |
| `PGDATABASE` / `TEST_PGDATABASE` | `eval_platform` / `eval_platform_test` | Dedicated databases. |
| `PGSCHEMA` | `public` | Schema inside that database. |
| `DATABASE_URL` | — | Overrides the `PG*` variables when set. |
| `DB_POOL_MIN_SIZE` / `DB_POOL_MAX_SIZE` | 1 / 20 | psycopg pool bounds. |
| `RUNNER_MAX_CONCURRENCY` | 4 | Tickets in flight per runner. |
| `RUNNER_CLAIM_BATCH_SIZE` | 5 | Tickets per claim. |
| `RUNNER_LEASE_SECONDS` | 300 | Lease before a ticket is considered abandoned. |
| `RUNNER_MAX_ATTEMPTS` | 3 | Default attempts per ticket. |
| `RUNNER_RETRY_BACKOFF_SECONDS` | `30,60,120` | Backoff per attempt. |
| `DEFAULT_ON_MISSING_CONTEXT` | `not_applicable` | Platform-wide missing-context policy. |
| `LOCAL_STORAGE_ROOT` | `./data/storage` | Archive for uploads and exports only. |

---

## 9. Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Tests run against the real `eval_platform_test` database (migrations are applied
automatically) and skip with a clear message if it does not exist yet. Nothing at
the database layer is mocked: locking, transactions, enums, and JSONB behaviour are
what is being verified. Coverage includes context resolution, every evaluator,
configuration validation, the migration ledger, the full API surface, concurrent
claiming with two live connections, retry and backoff, lease recovery,
cancellation mid-flight, result idempotency, and two runners sharing one job.

---

## 10. Layout

```
app/
  core/          config, psycopg pool + transactions, logging, metrics, API key auth
  db/            migration runner + migrations/*.sql
  models/        dataclass entities, enums, ticket state machine
  repositories/  hand-written parameterized SQL per table
  services/      ingestion, validation, context resolver, job/ticket/evaluation/recovery
  evaluators/    contract, registry, deterministic set, LLM judge + providers
  api/           FastAPI routers
  schemas/       pydantic request/response models
runner/          claim loop, concurrency, recovery, graceful shutdown
scripts/         setup_databases.sql|.py, migrate.py, check_connection.py
examples/        sample payload and evaluation configuration
tests/           pytest suite against real PostgreSQL
```

## 11. Reused from evalforge-local

Ported with minimal changes: the psycopg pool and transaction helpers, the plain
SQL migration runner (advisory lock + checksum ledger), the repository pattern,
local filesystem storage, the LLM provider abstraction (OpenAI, Anthropic, Ollama,
with tenacity retry), the scoring primitives (`normalize`, exact, fuzzy, semantic,
hash/OpenAI embeddings), the custom verifiers (`regex`, `contains_all`,
`json_keys`, `length_bounds`), cost estimation, structlog setup, and the
Prometheus/health/API-key conventions.

Replaced by design: the record-index-driven run model (`eval_runs`,
`eval_results`, dataset files read at evaluation time) gives way to payload- and
check-addressed tickets with PostgreSQL as the only source of truth; file storage
becomes optional provenance; pandas-based CSV/JSONL record validation becomes
structural payload validation in the standard library.
