Copy this project into an empty folder, then install, migrate, and start it. The new folder works when it contains every file below and uses the same database settings as the working copy.

## 1. Create the new folder

Create an empty folder, for example `C:\Users\chand\Projects\eval-platform`. All paths below are inside that folder. Python 3.11+ and PostgreSQL must already be installed.

## 2. Copy these root files

- `.gitignore`
- `.env.example`
- `alembic.ini`
- `pyproject.toml`
- `README.md`
- `requirements.txt`

Also copy the existing `.env` from `C:\Users\chand\Downloads\evalfinal\eval-platform\.env` into the new folder. That file already points at the working database `eval_platform_v2` with user `evalforge`. The sample `.env.example` uses different names (`eval_platform` and `evalforge` placeholders that do not match this machine). Using those names will not reach the database this app is using now.

## 3. Copy `migrations`

The revision folder is `migrations`, not `alembic`. `alembic.ini` stays at the root and already points at `migrations`.

- `migrations\env.py`
- `migrations\script.py.mako`
- `migrations\versions\0001_initial_schema.py`
- `migrations\versions\0002_job_idempotency.py`
- `migrations\versions\0003_ticket_agent_id.py`

## 4. Copy `src\evalorch`

The Python package is `src\evalorch`. Imports are `evalorch.*`. There is no top-level `app` folder.

- `src\evalorch\__init__.py`
- `src\evalorch\dependencies.py`
- `src\evalorch\main.py`
- `src\evalorch\api\__init__.py`
- `src\evalorch\api\errors.py`
- `src\evalorch\api\health.py`
- `src\evalorch\api\jobs.py`
- `src\evalorch\core\__init__.py`
- `src\evalorch\core\config.py`
- `src\evalorch\core\database.py`
- `src\evalorch\core\logging.py`
- `src\evalorch\core\metrics.py`
- `src\evalorch\core\redaction.py`
- `src\evalorch\core\security.py`
- `src\evalorch\db\__init__.py`
- `src\evalorch\db\migrate.py`
- `src\evalorch\db\models.py`
- `src\evalorch\evaluators\__init__.py`
- `src\evalorch\evaluators\base.py`
- `src\evalorch\evaluators\registry.py`
- `src\evalorch\evaluators\deterministic\__init__.py`
- `src\evalorch\evaluators\deterministic\required_fields.py`
- `src\evalorch\evaluators\deterministic\tool_calls.py`
- `src\evalorch\evaluators\deterministic\workflow_order.py`
- `src\evalorch\evaluators\llm\__init__.py`
- `src\evalorch\evaluators\llm\judge.py`
- `src\evalorch\evaluators\llm\providers\__init__.py`
- `src\evalorch\evaluators\llm\providers\base.py`
- `src\evalorch\evaluators\llm\providers\factory.py`
- `src\evalorch\evaluators\llm\providers\openai.py`
- `src\evalorch\models\__init__.py`
- `src\evalorch\models\entities.py`
- `src\evalorch\models\enums.py`
- `src\evalorch\repositories\__init__.py`
- `src\evalorch\repositories\base.py`
- `src\evalorch\repositories\job_repository.py`
- `src\evalorch\repositories\result_repository.py`
- `src\evalorch\repositories\ticket_repository.py`
- `src\evalorch\schemas\__init__.py`
- `src\evalorch\services\__init__.py`
- `src\evalorch\services\context_resolver.py`
- `src\evalorch\services\dataset_folder.py`
- `src\evalorch\services\errors.py`
- `src\evalorch\services\evaluation_service.py`
- `src\evalorch\services\job_service.py`
- `src\evalorch\services\metric_validation.py`
- `src\evalorch\services\payload_validation.py`
- `src\evalorch\services\profile_resolution.py`
- `src\evalorch\services\recovery_service.py`
- `src\evalorch\services\ticket_service.py`

## 5. Copy `runner`, `scripts`, `docs`, and `examples`

- `runner\__init__.py`
- `runner\main.py`
- `runner\runner.py`
- `scripts\check_connection.py`
- `scripts\migrate.py`
- `scripts\setup_databases.py`
- `scripts\setup_databases.sql`
- `docs\architecture.md`
- `docs\operations.md`
- `examples\dataset-folder-001\config.json`
- `examples\dataset-folder-001\payload_001.json`
- `examples\dataset-folder-001\payload_002.json`
- `examples\dataset-folder-001\payload_003.json`

## 6. Copy `tests`

- `tests\__init__.py`
- `tests\conftest.py`
- `tests\test_api.py`
- `tests\test_concurrency_scenarios.py`
- `tests\test_context_resolver.py`
- `tests\test_deadlock_retry.py`
- `tests\test_evaluators.py`
- `tests\test_hardening.py`
- `tests\test_job_creation.py`
- `tests\test_metric_validation.py`
- `tests\test_migrations.py`
- `tests\test_orchestration.py`
- `tests\test_payload_config_contract.py`
- `tests\test_runner_end_to_end.py`

## 7. Leave these out

Do not copy `.venv`, `.git`, `__pycache__`, `.pytest_cache`, `.ruff_cache`, or `eval_platform.egg-info`. Those are created again on the new folder.

Do not copy an old `app` folder, an old `alembic` folder, Anthropic or Ollama providers, `json_schema.py`, `cross_span_consistency.py`, `scoring.py`, or `verifiers.py`. Those are not part of this copy.

## 8. Put the sample dataset where the API reads it

Create `temp\dataset-folder-001` in the new folder and copy these four files into it:

- `config.json`
- `payload_001.json`
- `payload_002.json`
- `payload_003.json`

Take them from `examples\dataset-folder-001`.

## 9. Install

In the new folder:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

## 10. Confirm the database

On this computer the databases already exist. From the new folder:

```powershell
if ($env:PGDATABASE -eq 'eval_platform_test') { Remove-Item Env:PGDATABASE }
.\.venv\Scripts\python.exe scripts\check_connection.py
.\.venv\Scripts\python.exe scripts\migrate.py upgrade
.\.venv\Scripts\python.exe scripts\migrate.py status
```

`check_connection` should show database `eval_platform_v2`. `status` should show revision `0003_ticket_agent_id`.

## 11. Start the API

Port 8088 may still be held by an older process. Use 8110.

```powershell
if ($env:PGDATABASE -eq 'eval_platform_test') { Remove-Item Env:PGDATABASE }
.\.venv\Scripts\python.exe -m uvicorn evalorch.main:app --host 127.0.0.1 --port 8110
```

The startup log should contain `"database": "eval_platform_v2"` and `"event": "api_started"`.

## 12. Start the runner

Open a second terminal in the new folder:

```powershell
if ($env:PGDATABASE -eq 'eval_platform_test') { Remove-Item Env:PGDATABASE }
.\.venv\Scripts\python.exe -m runner.main
```

The log should contain `"event": "runner_started"` and `"database": "eval_platform_v2"`.

## 13. Check that it works

- `GET http://127.0.0.1:8110/health` returns `"status":"healthy"`
- `GET http://127.0.0.1:8110/health/ready` returns `"status":"ready"`
- `GET http://127.0.0.1:8110/v1/evaluators` returns HTTP 200
- `GET http://127.0.0.1:8110/v1/evaluation-jobs` with header `X-API-Key` set to the key in the new `.env` returns HTTP 200

A new job is `POST http://127.0.0.1:8110/v1/evaluation-jobs` with that same header and body:

```json
{"dataset_id": "dataset-folder-001", "name": "dataset-check"}
```

The runner then claims the tickets. The job finishes as `COMPLETED` with 12 tickets: 3 payloads × 4 metrics (`summary_present`, `workflow_sequence`, `policy_tool_called`, `all_agents_ran`).
