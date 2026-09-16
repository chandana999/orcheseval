-- One evaluation execution request. `config_snapshot_json` is written once at
-- creation and never updated, so a running job is immune to later config edits.
--
-- Counters are a cache of durable ticket state: they are always recomputed by
-- aggregating evaluation_tickets, never incremented from runner memory.

CREATE TABLE evaluation_jobs (
    id UUID PRIMARY KEY,
    name VARCHAR(255),
    dataset_id UUID REFERENCES datasets (id) ON DELETE SET NULL,
    evaluation_config_id UUID NOT NULL REFERENCES evaluation_configs (id) ON DELETE RESTRICT,
    config_snapshot_json JSONB NOT NULL,
    status evaluation_job_status NOT NULL DEFAULT 'CREATED',
    payload_count INTEGER NOT NULL DEFAULT 0,
    total_tickets INTEGER NOT NULL DEFAULT 0,
    completed_tickets INTEGER NOT NULL DEFAULT 0,
    failed_tickets INTEGER NOT NULL DEFAULT 0,
    cancelled_tickets INTEGER NOT NULL DEFAULT 0,
    not_applicable_tickets INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    cancellation_requested_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    error_message TEXT,
    CONSTRAINT ck_jobs_json_object CHECK (jsonb_typeof(config_snapshot_json) = 'object'),
    CONSTRAINT ck_jobs_counters CHECK (
        payload_count >= 0
        AND total_tickets >= 0
        AND completed_tickets >= 0
        AND failed_tickets >= 0
        AND cancelled_tickets >= 0
        AND not_applicable_tickets >= 0
        AND completed_tickets + failed_tickets + cancelled_tickets + not_applicable_tickets
            <= total_tickets
    )
);

CREATE INDEX ix_evaluation_jobs_status ON evaluation_jobs (status, created_at);
CREATE INDEX ix_evaluation_jobs_dataset_id ON evaluation_jobs (dataset_id);
CREATE INDEX ix_evaluation_jobs_config_id ON evaluation_jobs (evaluation_config_id);
