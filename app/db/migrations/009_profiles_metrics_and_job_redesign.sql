-- Evaluation profiles + versioned metric records; jobs keyed by a temporary
-- folder dataset_id; tickets are PAYLOAD x METRIC and carry an immutable
-- metric configuration snapshot. PostgreSQL stays the durable operational store.
--
-- The legacy tables `datasets`, `evaluation_payloads`, and `evaluation_configs`
-- are left in place (they may still hold historical rows) but are no longer
-- referenced by jobs, tickets, or results. Nothing is dropped blindly.
--
-- Every statement is written to be safe to apply to either a database that
-- only has migrations 001-006, or one that already received earlier profile
-- work, so an interrupted apply can always be completed.

-- ---------------------------------------------------------------------------
-- 1. metric_records: versioned metric configuration.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS metric_records (
    metric_record_id UUID PRIMARY KEY,
    metric_id TEXT NOT NULL,
    metric_code TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    metric_desc TEXT,
    metric_type evaluation_check_type NOT NULL,
    metric_version_number INTEGER NOT NULL DEFAULT 1,
    definition_payload JSONB NOT NULL,
    default_threshold_operator TEXT,
    llm_model_name TEXT,
    llm_model_version TEXT,
    llm_deployed_id TEXT,
    is_active_indicator BOOLEAN NOT NULL DEFAULT TRUE,
    previous_metric_record_id UUID REFERENCES metric_records (metric_record_id) ON DELETE SET NULL,
    change_summary TEXT,
    metric_create_timestamp TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_metric_records_id_version UNIQUE (metric_id, metric_version_number),
    CONSTRAINT uq_metric_records_code_version UNIQUE (metric_code, metric_version_number),
    CONSTRAINT ck_metric_records_version CHECK (metric_version_number >= 1),
    CONSTRAINT ck_metric_records_definition CHECK (jsonb_typeof(definition_payload) = 'object')
);

CREATE INDEX IF NOT EXISTS ix_metric_records_code ON metric_records (metric_code);
CREATE INDEX IF NOT EXISTS ix_metric_records_active ON metric_records (is_active_indicator)
    WHERE is_active_indicator IS TRUE;

-- ---------------------------------------------------------------------------
-- 2. evaluation_profiles: named, versioned collection of metrics. The business
--    key `evaluation_profile_id` is what payload.agent_registry carries.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS evaluation_profiles (
    id UUID PRIMARY KEY,
    evaluation_profile_id TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    name VARCHAR(200) NOT NULL,
    description TEXT,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    metadata_json JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_evaluation_profiles_id_version UNIQUE (evaluation_profile_id, version),
    CONSTRAINT ck_evaluation_profiles_version CHECK (version >= 1)
);

CREATE INDEX IF NOT EXISTS ix_evaluation_profiles_key
    ON evaluation_profiles (evaluation_profile_id, version DESC);

-- ---------------------------------------------------------------------------
-- 3. evaluation_profile_metrics: profile -> one exact metric record version.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS evaluation_profile_metrics (
    id UUID PRIMARY KEY,
    profile_id UUID NOT NULL REFERENCES evaluation_profiles (id) ON DELETE CASCADE,
    metric_record_id UUID NOT NULL REFERENCES metric_records (metric_record_id) ON DELETE RESTRICT,
    execution_order INTEGER NOT NULL DEFAULT 0,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_profile_metrics_profile_record UNIQUE (profile_id, metric_record_id)
);

CREATE INDEX IF NOT EXISTS ix_profile_metrics_profile ON evaluation_profile_metrics (profile_id);
CREATE INDEX IF NOT EXISTS ix_profile_metrics_record ON evaluation_profile_metrics (metric_record_id);

-- A profile must not map two versions of the same logical metric, otherwise a
-- payload would fan out into two conflicting tickets for one metric_id.
ALTER TABLE evaluation_profile_metrics
    ADD COLUMN IF NOT EXISTS metric_id TEXT;

UPDATE evaluation_profile_metrics pm
SET metric_id = mr.metric_id
FROM metric_records mr
WHERE mr.metric_record_id = pm.metric_record_id
  AND pm.metric_id IS DISTINCT FROM mr.metric_id;

CREATE UNIQUE INDEX IF NOT EXISTS uq_profile_metric_id
    ON evaluation_profile_metrics (profile_id, metric_id);

-- ---------------------------------------------------------------------------
-- 4. evaluation_jobs: dataset_id is a temp-folder identifier, not a FK. A job
--    may span payloads with different profiles, so there is no job-level
--    profile or evaluation config; config_snapshot_json records what was frozen.
-- ---------------------------------------------------------------------------
ALTER TABLE evaluation_jobs DROP CONSTRAINT IF EXISTS evaluation_jobs_dataset_id_fkey;
ALTER TABLE evaluation_jobs DROP CONSTRAINT IF EXISTS evaluation_jobs_evaluation_config_id_fkey;
ALTER TABLE evaluation_jobs DROP CONSTRAINT IF EXISTS evaluation_jobs_evaluation_profile_id_fkey;

DROP INDEX IF EXISTS ix_evaluation_jobs_config_id;
DROP INDEX IF EXISTS ix_evaluation_jobs_profile;

ALTER TABLE evaluation_jobs
    ALTER COLUMN dataset_id TYPE TEXT USING dataset_id::text;

-- These columns can never be populated under the new model (one job spans many
-- profiles, and evaluation_configs is no longer consulted); they are always
-- NULL, so removing them loses no evaluation state.
ALTER TABLE evaluation_jobs DROP COLUMN IF EXISTS evaluation_config_id;
ALTER TABLE evaluation_jobs DROP COLUMN IF EXISTS evaluation_profile_id;
ALTER TABLE evaluation_jobs DROP COLUMN IF EXISTS evaluation_profile_key;
ALTER TABLE evaluation_jobs DROP COLUMN IF EXISTS evaluation_profile_version;

CREATE INDEX IF NOT EXISTS ix_evaluation_jobs_dataset_id ON evaluation_jobs (dataset_id);

-- ---------------------------------------------------------------------------
-- 5. evaluation_tickets: one source payload file x one metric record version.
--    payload_id stays a stable synthetic UUID; the source JSON is loaded from
--    EVALUATION_TEMP_ROOT at evaluation time and never stored here.
-- ---------------------------------------------------------------------------
ALTER TABLE evaluation_tickets DROP CONSTRAINT IF EXISTS evaluation_tickets_payload_id_fkey;
ALTER TABLE evaluation_tickets DROP CONSTRAINT IF EXISTS evaluation_tickets_evaluation_config_id_fkey;
ALTER TABLE evaluation_tickets DROP CONSTRAINT IF EXISTS uq_tickets_job_payload_check;

ALTER TABLE evaluation_tickets DROP COLUMN IF EXISTS evaluation_config_id;

ALTER TABLE evaluation_tickets
    ADD COLUMN IF NOT EXISTS source_dataset_id TEXT,
    ADD COLUMN IF NOT EXISTS source_payload_ref TEXT,
    ADD COLUMN IF NOT EXISTS source_payload_id TEXT,
    ADD COLUMN IF NOT EXISTS metric_record_id UUID
        REFERENCES metric_records (metric_record_id) ON DELETE RESTRICT,
    ADD COLUMN IF NOT EXISTS metric_id TEXT,
    ADD COLUMN IF NOT EXISTS metric_version_number INTEGER,
    ADD COLUMN IF NOT EXISTS evaluation_profile_id TEXT,
    ADD COLUMN IF NOT EXISTS metric_snapshot_json JSONB;

-- Give pre-redesign rows a reconstructable source reference. metric_record_id
-- is left alone: it is a foreign key and cannot be invented for old rows.
UPDATE evaluation_tickets
SET source_dataset_id = COALESCE(source_dataset_id, ''),
    source_payload_ref = COALESCE(source_payload_ref, payload_id::text),
    metric_id = COALESCE(metric_id, check_id),
    metric_version_number = COALESCE(metric_version_number, 1),
    evaluation_profile_id = COALESCE(evaluation_profile_id, ''),
    metric_snapshot_json = COALESCE(metric_snapshot_json, input_snapshot_json, '{}'::jsonb)
WHERE source_dataset_id IS NULL
   OR source_payload_ref IS NULL
   OR metric_id IS NULL
   OR metric_version_number IS NULL
   OR evaluation_profile_id IS NULL
   OR metric_snapshot_json IS NULL;

ALTER TABLE evaluation_tickets
    ALTER COLUMN source_dataset_id SET NOT NULL,
    ALTER COLUMN source_payload_ref SET NOT NULL,
    ALTER COLUMN metric_id SET NOT NULL,
    ALTER COLUMN metric_version_number SET NOT NULL,
    ALTER COLUMN evaluation_profile_id SET NOT NULL,
    ALTER COLUMN metric_snapshot_json SET NOT NULL;

-- metric_record_id can only become NOT NULL when no legacy row lacks one.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM evaluation_tickets WHERE metric_record_id IS NULL) THEN
        ALTER TABLE evaluation_tickets ALTER COLUMN metric_record_id SET NOT NULL;
    END IF;
END
$$;

DO $$
BEGIN
    ALTER TABLE evaluation_tickets
        ADD CONSTRAINT ck_tickets_snapshot_object
            CHECK (jsonb_typeof(metric_snapshot_json) = 'object');
EXCEPTION
    WHEN duplicate_object THEN NULL;
END
$$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_tickets_job_payload_metric
    ON evaluation_tickets (job_id, source_payload_ref, metric_record_id);

CREATE INDEX IF NOT EXISTS ix_tickets_profile ON evaluation_tickets (job_id, evaluation_profile_id);
CREATE INDEX IF NOT EXISTS ix_tickets_metric_record ON evaluation_tickets (metric_record_id);
CREATE INDEX IF NOT EXISTS ix_tickets_source_payload ON evaluation_tickets (job_id, source_payload_ref);

-- ---------------------------------------------------------------------------
-- 6. evaluation_results: one durable row per ticket, idempotent on ticket_id.
-- ---------------------------------------------------------------------------
ALTER TABLE evaluation_results DROP CONSTRAINT IF EXISTS evaluation_results_payload_id_fkey;
ALTER TABLE evaluation_results DROP CONSTRAINT IF EXISTS uq_results_job_payload_check;

ALTER TABLE evaluation_results
    ADD COLUMN IF NOT EXISTS source_payload_ref TEXT,
    ADD COLUMN IF NOT EXISTS metric_record_id UUID
        REFERENCES metric_records (metric_record_id) ON DELETE RESTRICT,
    ADD COLUMN IF NOT EXISTS metric_id TEXT,
    ADD COLUMN IF NOT EXISTS metric_version_number INTEGER;

UPDATE evaluation_results
SET source_payload_ref = COALESCE(source_payload_ref, payload_id::text),
    metric_id = COALESCE(metric_id, check_id)
WHERE source_payload_ref IS NULL
   OR metric_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_results_ticket_id ON evaluation_results (ticket_id);

CREATE INDEX IF NOT EXISTS ix_results_metric ON evaluation_results (job_id, metric_id);
CREATE INDEX IF NOT EXISTS ix_results_source_payload ON evaluation_results (job_id, source_payload_ref);
