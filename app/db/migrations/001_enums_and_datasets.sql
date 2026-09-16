-- eval-platform baseline: enum types + datasets.
--
-- Status labels are uppercase to match the documented lifecycle names used by
-- the API, the runner, and the ticket state machine.

CREATE TYPE dataset_status AS ENUM (
    'CREATED',
    'INGESTING',
    'READY',
    'FAILED',
    'ARCHIVED'
);

CREATE TYPE evaluation_config_status AS ENUM (
    'DRAFT',
    'ACTIVE',
    'DEPRECATED'
);

CREATE TYPE evaluation_job_status AS ENUM (
    'CREATED',
    'READY',
    'RUNNING',
    'COMPLETED',
    'PARTIAL_FAILED',
    'FAILED',
    'CANCELLED'
);

CREATE TYPE evaluation_ticket_status AS ENUM (
    'READY',
    'RUNNING',
    'RETRY',
    'DONE',
    'FAILED',
    'CANCELLED',
    'NOT_APPLICABLE'
);

CREATE TYPE evaluation_check_type AS ENUM (
    'DETERMINISTIC',
    'LLM_JUDGE'
);

CREATE TYPE evaluation_result_status AS ENUM (
    'PASSED',
    'FAILED',
    'NOT_APPLICABLE',
    'ERROR'
);

-- Dataset metadata only. `source_path` is provenance for optional file
-- ingestion and is nullable: datasets created through the API have no file,
-- and an ingested file is never read again after ingestion.
CREATE TABLE datasets (
    id UUID PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    source_type VARCHAR(32) NOT NULL DEFAULT 'api',
    source_path VARCHAR(1024),
    record_count INTEGER NOT NULL DEFAULT 0,
    status dataset_status NOT NULL DEFAULT 'CREATED',
    checksum_sha256 CHAR(64),
    validation_report JSONB,
    metadata_json JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_datasets_record_count CHECK (record_count >= 0),
    CONSTRAINT ck_datasets_source_type CHECK (source_type IN ('api', 'json', 'jsonl'))
);

CREATE INDEX ix_datasets_name ON datasets (name);
CREATE INDEX ix_datasets_status ON datasets (status);
