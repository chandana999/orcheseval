-- Finish the ticket redesign started in 009.
--
-- 009 could only mark evaluation_tickets.metric_record_id NOT NULL when no
-- pre-redesign ticket row was missing one, because the column is a foreign key
-- and a value cannot be invented for historical rows. Databases that have since
-- retired those rows converge here; databases that still hold them keep the
-- column nullable and are left untouched rather than losing history.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM evaluation_tickets WHERE metric_record_id IS NULL) THEN
        ALTER TABLE evaluation_tickets ALTER COLUMN metric_record_id SET NOT NULL;
    END IF;
END
$$;
