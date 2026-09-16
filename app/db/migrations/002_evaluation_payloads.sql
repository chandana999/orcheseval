-- Complete agent execution payloads. PostgreSQL is the authoritative store:
-- `payload_json` holds the entire original payload (payload_metadata,
-- trace_data, session_context, spans, tool calls, outputs, ...) so the context
-- resolver never needs the original file.

CREATE TABLE evaluation_payloads (
    id UUID PRIMARY KEY,
    dataset_id UUID REFERENCES datasets (id) ON DELETE CASCADE,
    external_payload_id TEXT,
    trace_id TEXT,
    session_id TEXT,
    workflow_id TEXT,
    payload_json JSONB NOT NULL,
    payload_version TEXT,
    source_type VARCHAR(32) NOT NULL DEFAULT 'api',
    validation_report JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_payloads_json_object CHECK (jsonb_typeof(payload_json) = 'object'),
    CONSTRAINT ck_payloads_source_type CHECK (source_type IN ('api', 'json', 'jsonl'))
);

CREATE INDEX ix_payloads_dataset_id ON evaluation_payloads (dataset_id, created_at);
CREATE INDEX ix_payloads_trace_id ON evaluation_payloads (trace_id);
CREATE INDEX ix_payloads_session_id ON evaluation_payloads (session_id);
CREATE INDEX ix_payloads_workflow_id ON evaluation_payloads (workflow_id);

-- Containment/existence queries over spans and session context.
CREATE INDEX ix_payloads_payload_json ON evaluation_payloads USING GIN (payload_json);

-- Idempotent ingestion: an external payload id is unique within its dataset,
-- and unique globally among dataset-less payloads.
CREATE UNIQUE INDEX uq_payloads_dataset_external
    ON evaluation_payloads (dataset_id, external_payload_id)
    WHERE dataset_id IS NOT NULL AND external_payload_id IS NOT NULL;

CREATE UNIQUE INDEX uq_payloads_external_standalone
    ON evaluation_payloads (external_payload_id)
    WHERE dataset_id IS NULL AND external_payload_id IS NOT NULL;
