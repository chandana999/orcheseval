-- Drop the tables and enums left over from the pre-profile design.
--
-- `datasets`, `evaluation_payloads`, and `evaluation_configs` were created by
-- migrations 001-003. Since 009 nothing reads or writes them: datasets are
-- folders under EVALUATION_TEMP_ROOT, payloads stay on disk, and metric
-- configuration lives in metric_records / evaluation_profiles. Jobs, tickets,
-- and results dropped their foreign keys into these tables in 009, so removing
-- them cannot orphan any evaluation state.

DROP TABLE IF EXISTS evaluation_payloads CASCADE;
DROP TABLE IF EXISTS evaluation_configs CASCADE;
DROP TABLE IF EXISTS datasets CASCADE;

-- Enums used only by the tables above. metric_records uses
-- evaluation_check_type, so that one stays.
DROP TYPE IF EXISTS dataset_status;
DROP TYPE IF EXISTS evaluation_config_status;
