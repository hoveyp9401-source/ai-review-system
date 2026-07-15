CREATE TABLE IF NOT EXISTS agent2_periodic_reports (
    report_id uuid PRIMARY KEY,
    tenant_id varchar(128) NOT NULL,
    company_id varchar(128) NOT NULL DEFAULT '',
    department_id varchar(128) NOT NULL DEFAULT '',
    team_id varchar(128) NOT NULL DEFAULT '',
    owner_user_id varchar(128) NOT NULL,
    report_type varchar(16) NOT NULL,
    period_key varchar(16) NOT NULL,
    period_start timestamptz NOT NULL,
    period_end timestamptz NOT NULL,
    sections_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    item_ids_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    status varchar(32) NOT NULL DEFAULT 'collecting',
    version integer NOT NULL DEFAULT 0,
    source_channel varchar(64) NOT NULL DEFAULT '',
    submitted_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent2_periodic_report_owner_period_key UNIQUE
        (tenant_id, owner_user_id, report_type, period_key),
    CONSTRAINT agent2_periodic_report_type_check
        CHECK (report_type IN ('weekly', 'monthly')),
    CONSTRAINT agent2_periodic_report_status_check
        CHECK (status IN ('collecting', 'completed', 'cancelled'))
);

CREATE INDEX IF NOT EXISTS agent2_periodic_report_scope_idx
    ON agent2_periodic_reports (tenant_id, team_id, report_type, period_start);

CREATE TABLE IF NOT EXISTS agent2_periodic_report_command_receipts (
    receipt_id uuid PRIMARY KEY,
    tenant_id varchar(128) NOT NULL,
    actor_user_id varchar(128) NOT NULL,
    source_message_id varchar(256) NOT NULL,
    source_channel varchar(64) NOT NULL,
    command_id varchar(256) NOT NULL,
    command_type varchar(64) NOT NULL,
    idempotency_key varchar(128) NOT NULL,
    report_id uuid NOT NULL,
    status varchar(32) NOT NULL,
    actual_write boolean NOT NULL DEFAULT false,
    before_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    after_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    error_code varchar(128) NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent2_periodic_report_receipt_idempotency_key
        UNIQUE (tenant_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS agent2_periodic_report_receipt_source_idx
    ON agent2_periodic_report_command_receipts (tenant_id, source_message_id);
