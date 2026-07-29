BEGIN;

CREATE TABLE IF NOT EXISTS user_habits (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    habit_type varchar(64) NOT NULL,
    trigger_text varchar(128) NOT NULL,
    meaning text NOT NULL,
    confidence numeric(5, 4) NOT NULL DEFAULT 0,
    evidence_count integer NOT NULL DEFAULT 0,
    counterexample_count integer NOT NULL DEFAULT 0,
    status varchar(32) NOT NULL DEFAULT 'candidate',
    evidence_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_observed_at timestamp with time zone,
    activated_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT user_habits_unique_signal UNIQUE (user_id, habit_type, trigger_text, meaning),
    CONSTRAINT user_habits_status_check CHECK (status IN ('candidate', 'active', 'rejected', 'disabled'))
);

CREATE INDEX IF NOT EXISTS idx_user_habits_user_status
    ON user_habits(user_id, status);

CREATE INDEX IF NOT EXISTS idx_user_habits_updated_at
    ON user_habits(updated_at);

COMMIT;
