BEGIN;

CREATE TABLE IF NOT EXISTS report_interaction_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    report_id uuid NULL REFERENCES daily_reports(id) ON DELETE SET NULL,
    dingtalk_user_id varchar(128),
    report_date date NOT NULL,
    message_text text NOT NULL DEFAULT '',
    llm_decision_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    backend_action varchar(64) NOT NULL DEFAULT '',
    before_snapshot_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    after_snapshot_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    correction_type varchar(64) NOT NULL DEFAULT '',
    correction_from text NOT NULL DEFAULT '',
    correction_to text NOT NULL DEFAULT '',
    confidence numeric(5, 4),
    is_undo boolean NOT NULL DEFAULT false,
    is_repeated_item_edit boolean NOT NULL DEFAULT false,
    asr_suspect_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS report_interaction_events_user_date_idx
    ON report_interaction_events (user_id, report_date, created_at);

CREATE INDEX IF NOT EXISTS report_interaction_events_action_idx
    ON report_interaction_events (backend_action, created_at);

CREATE INDEX IF NOT EXISTS report_interaction_events_correction_idx
    ON report_interaction_events (correction_type, created_at)
    WHERE correction_type <> '';

COMMIT;
