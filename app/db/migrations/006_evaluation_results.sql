-- Normalized evaluator output.
--
-- uq_results_job_payload_check is the idempotency key: retrying a ticket
-- upserts the same row instead of creating a duplicate result.

CREATE TABLE evaluation_results (
    id UUID PRIMARY KEY,
    job_id UUID NOT NULL REFERENCES evaluation_jobs (id) ON DELETE CASCADE,
    ticket_id UUID NOT NULL REFERENCES evaluation_tickets (id) ON DELETE CASCADE,
    payload_id UUID NOT NULL REFERENCES evaluation_payloads (id) ON DELETE CASCADE,
    check_id TEXT NOT NULL,
    check_type evaluation_check_type NOT NULL,
    evaluator_type TEXT NOT NULL,
    evaluator_version TEXT,
    status evaluation_result_status NOT NULL,
    passed BOOLEAN,
    score DOUBLE PRECISION,
    explanation TEXT,
    evidence_json JSONB,
    input_snapshot_json JSONB,
    output_json JSONB,
    error_code TEXT,
    error_message TEXT,
    execution_time_ms DOUBLE PRECISION,
    attempt_count INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    CONSTRAINT uq_results_job_payload_check UNIQUE (job_id, payload_id, check_id)
);

CREATE INDEX ix_results_job_status ON evaluation_results (job_id, status);
CREATE INDEX ix_results_payload ON evaluation_results (payload_id);
CREATE INDEX ix_results_ticket ON evaluation_results (ticket_id);
CREATE INDEX ix_results_check_id ON evaluation_results (job_id, check_id);

-- Completes the ticket -> result link now that the results table exists.
ALTER TABLE evaluation_tickets
    ADD CONSTRAINT fk_tickets_result
    FOREIGN KEY (result_id) REFERENCES evaluation_results (id) ON DELETE SET NULL;
