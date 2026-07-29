BEGIN;

CREATE TABLE IF NOT EXISTS progress_outbox_events (
    id uuid PRIMARY KEY,
    event_type varchar(64) NOT NULL,
    source_type varchar(64) NOT NULL,
    source_id varchar(256) NOT NULL,
    user_id uuid,
    team_id uuid,
    report_id uuid,
    report_date date,
    payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    raw_text_hash varchar(64) NOT NULL DEFAULT '',
    idempotency_key varchar(256) NOT NULL,
    status varchar(32) NOT NULL DEFAULT 'pending',
    retry_count integer NOT NULL DEFAULT 0,
    next_retry_at timestamp with time zone,
    locked_by varchar(128),
    locked_at timestamp with time zone,
    error_message text,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    processed_at timestamp with time zone,
    CONSTRAINT progress_outbox_events_idempotency_key_key UNIQUE (idempotency_key),
    CONSTRAINT progress_outbox_events_status_check CHECK (status IN ('pending', 'processing', 'processed', 'failed', 'dead_letter'))
);

CREATE INDEX IF NOT EXISTS idx_progress_outbox_events_status_retry
    ON progress_outbox_events(status, next_retry_at, created_at);

CREATE INDEX IF NOT EXISTS idx_progress_outbox_events_report
    ON progress_outbox_events(report_id);

CREATE INDEX IF NOT EXISTS idx_progress_outbox_events_user_date
    ON progress_outbox_events(user_id, report_date);

COMMIT;
