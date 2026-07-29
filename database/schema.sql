CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS teams (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code VARCHAR(64) NOT NULL UNIQUE,
    name VARCHAR(128) NOT NULL,
    department_name VARCHAR(128) NOT NULL DEFAULT 'default',
    dingtalk_webhook_url TEXT,
    dingtalk_webhook_secret TEXT,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dingtalk_user_id VARCHAR(128) NOT NULL UNIQUE,
    employee_no VARCHAR(64) UNIQUE,
    name VARCHAR(128) NOT NULL,
    team_id UUID NOT NULL REFERENCES teams(id) ON UPDATE CASCADE,
    role VARCHAR(64) NOT NULL DEFAULT 'member',
    timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Shanghai',
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS daily_reports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    team_id UUID NOT NULL REFERENCES teams(id) ON UPDATE CASCADE,
    date DATE NOT NULL,
    today_work JSONB NOT NULL DEFAULT '[]'::jsonb,
    problems JSONB NOT NULL DEFAULT '[]'::jsonb,
    tomorrow_plan JSONB NOT NULL DEFAULT '[]'::jsonb,
    emotion VARCHAR(64) NOT NULL DEFAULT '',
    raw_input TEXT NOT NULL DEFAULT '',
    input_fragments JSONB NOT NULL DEFAULT '[]'::jsonb,
    section_status JSONB NOT NULL DEFAULT '{}'::jsonb,
    completeness_score NUMERIC(5, 4) NOT NULL DEFAULT 0 CHECK (completeness_score >= 0 AND completeness_score <= 1),
    status VARCHAR(32) NOT NULL DEFAULT 'collecting' CHECK (status IN ('collecting', 'pending_confirmation', 'completed', 'skipped', 'cancelled')),
    confirmation_type VARCHAR(32) NOT NULL DEFAULT 'none' CHECK (confirmation_type IN ('user_confirmed', 'auto_submitted_timeout', 'admin_confirmed', 'none')),
    confirmed_by_user BOOLEAN NOT NULL DEFAULT FALSE,
    quality_warning TEXT,
    last_modified_by_user BOOLEAN NOT NULL DEFAULT FALSE,
    last_modified_at TIMESTAMPTZ,
    pending_confirmation_at TIMESTAMPTZ,
    auto_submit_at TIMESTAMPTZ,
    source VARCHAR(64) NOT NULL DEFAULT 'dingtalk_text',
    llm_model VARCHAR(128),
    llm_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    submitted_at TIMESTAMPTZ,
    last_prompted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, date)
);

CREATE TABLE IF NOT EXISTS team_summaries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope VARCHAR(16) NOT NULL CHECK (scope IN ('team', 'department')),
    team_id UUID REFERENCES teams(id) ON DELETE CASCADE,
    date DATE NOT NULL,
    key_work JSONB NOT NULL DEFAULT '[]'::jsonb,
    major_problems JSONB NOT NULL DEFAULT '[]'::jsonb,
    risks JSONB NOT NULL DEFAULT '[]'::jsonb,
    tomorrow_plan_distribution JSONB NOT NULL DEFAULT '[]'::jsonb,
    raw_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    report_count INTEGER NOT NULL DEFAULT 0 CHECK (report_count >= 0),
    complete_count INTEGER NOT NULL DEFAULT 0 CHECK (complete_count >= 0),
    llm_model VARCHAR(128),
    generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
        (scope = 'team' AND team_id IS NOT NULL)
        OR
        (scope = 'department' AND team_id IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS webhook_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    idempotency_key VARCHAR(256) NOT NULL UNIQUE,
    platform VARCHAR(32) NOT NULL DEFAULT 'dingtalk',
    external_message_id VARCHAR(256),
    dingtalk_user_id VARCHAR(128),
    report_id UUID REFERENCES daily_reports(id) ON DELETE SET NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    response_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(16) NOT NULL DEFAULT 'processing' CHECK (status IN ('processing', 'processed', 'failed')),
    error_message TEXT,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS daily_reports_team_date_user_idx
    ON daily_reports (team_id, date, user_id);

CREATE INDEX IF NOT EXISTS daily_reports_date_status_idx
    ON daily_reports (date, status);

CREATE INDEX IF NOT EXISTS users_team_active_idx
    ON users (team_id, active);

CREATE UNIQUE INDEX IF NOT EXISTS team_summaries_team_unique_idx
    ON team_summaries (team_id, date)
    WHERE scope = 'team';

CREATE UNIQUE INDEX IF NOT EXISTS team_summaries_department_unique_idx
    ON team_summaries (date)
    WHERE scope = 'department';

CREATE INDEX IF NOT EXISTS webhook_events_user_received_idx
    ON webhook_events (dingtalk_user_id, received_at DESC);

CREATE INDEX IF NOT EXISTS webhook_events_status_created_idx
    ON webhook_events (status, created_at DESC);

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_teams_updated_at ON teams;
CREATE TRIGGER trg_teams_updated_at
BEFORE UPDATE ON teams
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_users_updated_at ON users;
CREATE TRIGGER trg_users_updated_at
BEFORE UPDATE ON users
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_daily_reports_updated_at ON daily_reports;
CREATE TRIGGER trg_daily_reports_updated_at
BEFORE UPDATE ON daily_reports
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_team_summaries_updated_at ON team_summaries;
CREATE TRIGGER trg_team_summaries_updated_at
BEFORE UPDATE ON team_summaries
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_webhook_events_updated_at ON webhook_events;
CREATE TRIGGER trg_webhook_events_updated_at
BEFORE UPDATE ON webhook_events
FOR EACH ROW EXECUTE FUNCTION set_updated_at();
