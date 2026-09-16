-- Evaluation definitions. `config_json` carries the whole definition:
-- evaluation type/version, target agents, workflow order, deterministic checks,
-- LLM judge checks, input mappings, model/sampling/timeout/retry settings.
-- Jobs never reference this row for execution; they snapshot it instead.

CREATE TABLE evaluation_configs (
    id UUID PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    description TEXT,
    config_json JSONB NOT NULL,
    status evaluation_config_status NOT NULL DEFAULT 'ACTIVE',
    checks_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_evaluation_configs_name_version UNIQUE (name, version),
    CONSTRAINT ck_configs_version CHECK (version >= 1),
    CONSTRAINT ck_configs_json_object CHECK (jsonb_typeof(config_json) = 'object'),
    CONSTRAINT ck_configs_checks_count CHECK (checks_count >= 0)
);

CREATE INDEX ix_evaluation_configs_name ON evaluation_configs (name);
CREATE INDEX ix_evaluation_configs_status ON evaluation_configs (status);
