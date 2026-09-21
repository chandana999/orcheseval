-- Sample metric records and evaluation profile for local development.
-- Run after `python scripts/migrate.py upgrade`, against eval_platform:
--
--   psql -U evalorches -d eval_platform -f examples/seed_profile.sql
--
-- The payload files under examples/dataset-folder-001/ reference
-- evaluation_profile_id = 'agent-response-quality-v1'.

BEGIN;

INSERT INTO metric_records (
    metric_record_id, metric_id, metric_code, metric_name, metric_desc,
    metric_type, metric_version_number, definition_payload, is_active_indicator
) VALUES
(
    '11111111-1111-1111-1111-111111111111',
    'summary_present',
    'summary_present',
    'Summary present',
    'Summarizer produced a non-empty summary.',
    'DETERMINISTIC',
    1,
    '{
        "check_id": "summary_present",
        "evaluator": "required_fields",
        "input_mapping": {"summary": "summarizer.output.summary"},
        "params": {"fields": ["summary"]},
        "priority": 5
    }'::jsonb,
    TRUE
),
(
    '22222222-2222-2222-2222-222222222222',
    'workflow_sequence',
    'workflow_sequence',
    'Workflow sequence',
    'Classifier, validator, then summarizer ran in order.',
    'DETERMINISTIC',
    1,
    '{
        "check_id": "workflow_sequence",
        "evaluator": "workflow_order",
        "input_mapping": {"spans": "spans"},
        "params": {
            "expected_order": ["classifier", "validator", "summarizer"],
            "mode": "subsequence"
        }
    }'::jsonb,
    TRUE
),
(
    '33333333-3333-3333-3333-333333333333',
    'policy_tool_called',
    'policy_tool_called',
    'Policy tool called',
    'Validator called policy_lookup and received a response.',
    'DETERMINISTIC',
    1,
    '{
        "check_id": "policy_tool_called",
        "evaluator": "tool_calls",
        "input_mapping": {"tool_calls": "validator.tool_calls"},
        "params": {"expected_tools": ["policy_lookup"], "require_response": true}
    }'::jsonb,
    TRUE
),
(
    '44444444-4444-4444-4444-444444444444',
    'all_agents_ran',
    'all_agents_ran',
    'All agents ran',
    'Required agent spans are present.',
    'DETERMINISTIC',
    1,
    '{
        "check_id": "all_agents_ran",
        "evaluator": "span_exists",
        "input_mapping": {"spans": "spans"},
        "params": {"required_spans": ["classifier", "validator", "summarizer"]},
        "priority": 10
    }'::jsonb,
    TRUE
)
ON CONFLICT (metric_record_id) DO NOTHING;

INSERT INTO evaluation_profiles (
    id, evaluation_profile_id, version, name, description, is_active
) VALUES (
    'aaaaaaaa-0000-0000-0000-00000000aaaa',
    'agent-response-quality-v1',
    1,
    'Agent response quality',
    'Deterministic workflow checks for the support triage agent.',
    TRUE
)
ON CONFLICT (evaluation_profile_id, version) DO NOTHING;

INSERT INTO evaluation_profile_metrics (
    id, profile_id, metric_record_id, metric_id, execution_order
) VALUES
    ('bbbbbbbb-0000-0000-0000-00000000bb01', 'aaaaaaaa-0000-0000-0000-00000000aaaa',
     '11111111-1111-1111-1111-111111111111', 'summary_present', 0),
    ('bbbbbbbb-0000-0000-0000-00000000bb02', 'aaaaaaaa-0000-0000-0000-00000000aaaa',
     '22222222-2222-2222-2222-222222222222', 'workflow_sequence', 1),
    ('bbbbbbbb-0000-0000-0000-00000000bb03', 'aaaaaaaa-0000-0000-0000-00000000aaaa',
     '33333333-3333-3333-3333-333333333333', 'policy_tool_called', 2),
    ('bbbbbbbb-0000-0000-0000-00000000bb04', 'aaaaaaaa-0000-0000-0000-00000000aaaa',
     '44444444-4444-4444-4444-444444444444', 'all_agents_ran', 3)
ON CONFLICT (profile_id, metric_record_id) DO NOTHING;

COMMIT;
