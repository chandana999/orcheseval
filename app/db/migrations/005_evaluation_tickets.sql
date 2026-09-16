-- One executable evaluation unit: ONE PAYLOAD + ONE EVALUATION CHECK.
-- Claimed atomically with SELECT ... FOR UPDATE SKIP LOCKED.

CREATE TABLE evaluation_tickets (
    id UUID PRIMARY KEY,
    job_id UUID NOT NULL REFERENCES evaluation_jobs (id) ON DELETE CASCADE,
    payload_id UUID NOT NULL REFERENCES evaluation_payloads (id) ON DELETE CASCADE,
    evaluation_config_id UUID NOT NULL REFERENCES evaluation_configs (id) ON DELETE RESTRICT,
    check_id TEXT NOT NULL,
    check_type evaluation_check_type NOT NULL,
    evaluator TEXT NOT NULL,
    status evaluation_ticket_status NOT NULL DEFAULT 'READY',
    priority INTEGER NOT NULL DEFAULT 0,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    worker_id TEXT,
    claimed_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    lease_expires_at TIMESTAMPTZ,
    result_id UUID,
    input_snapshot_json JSONB,
    error_code TEXT,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_tickets_job_payload_check UNIQUE (job_id, payload_id, check_id),
    CONSTRAINT ck_tickets_max_attempts CHECK (max_attempts >= 1),
    CONSTRAINT ck_tickets_attempts CHECK (
        attempt_count >= 0 AND attempt_count <= max_attempts + 1
    )
);

-- Claim path: status IN ('READY','RETRY') AND available_at <= now()
-- ORDER BY priority DESC, created_at.
CREATE INDEX ix_tickets_claim
    ON evaluation_tickets (priority DESC, created_at, available_at)
    WHERE status IN ('READY', 'RETRY');

-- Lease expiry recovery path.
CREATE INDEX ix_tickets_lease
    ON evaluation_tickets (lease_expires_at)
    WHERE status = 'RUNNING';

CREATE INDEX ix_tickets_job_status ON evaluation_tickets (job_id, status);
CREATE INDEX ix_tickets_payload ON evaluation_tickets (payload_id);
