BEGIN;
ALTER TABLE daily_reports
    DROP CONSTRAINT IF EXISTS daily_reports_status_check;

ALTER TABLE daily_reports
    ADD COLUMN IF NOT EXISTS confirmation_type VARCHAR(32) NOT NULL DEFAULT 'none',
    ADD COLUMN IF NOT EXISTS confirmed_by_user BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS quality_warning TEXT,
    ADD COLUMN IF NOT EXISTS last_modified_by_user BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS last_modified_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS pending_confirmation_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS auto_submit_at TIMESTAMPTZ;

UPDATE daily_reports
SET status = CASE
    WHEN status = 'complete' THEN 'completed'
    WHEN status = 'partial' THEN 'collecting'
    ELSE status
END
WHERE status IN ('complete', 'partial');

ALTER TABLE daily_reports
    ALTER COLUMN status TYPE VARCHAR(32),
    ALTER COLUMN status SET DEFAULT 'collecting';

ALTER TABLE daily_reports
    ADD CONSTRAINT daily_reports_status_check
        CHECK (status IN ('collecting', 'pending_confirmation', 'completed', 'skipped', 'cancelled'));

ALTER TABLE daily_reports
    DROP CONSTRAINT IF EXISTS daily_reports_confirmation_type_check;

ALTER TABLE daily_reports
    ADD CONSTRAINT daily_reports_confirmation_type_check
        CHECK (confirmation_type IN ('user_confirmed', 'auto_submitted_timeout', 'admin_confirmed', 'none'));

COMMIT;
