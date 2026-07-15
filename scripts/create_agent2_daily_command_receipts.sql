BEGIN;

CREATE TABLE IF NOT EXISTS agent2_daily_command_receipts (
    receipt_id uuid PRIMARY KEY,
    tenant_id varchar(128) NOT NULL,
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    report_id uuid REFERENCES daily_reports(id) ON DELETE SET NULL,
    report_date date NOT NULL,
    message_id varchar(512) NOT NULL,
    command_id uuid NOT NULL,
    decision_id uuid NOT NULL,
    sub_decision_id uuid NOT NULL,
    command_type varchar(64) NOT NULL,
    idempotency_key varchar(512) NOT NULL,
    status varchar(32) NOT NULL,
    validation_status varchar(32) NOT NULL,
    actual_write boolean NOT NULL DEFAULT false,
    resource_type varchar(64) NOT NULL DEFAULT 'daily_report',
    resource_id varchar(128) NOT NULL DEFAULT '',
    reason_code varchar(128) NOT NULL DEFAULT '',
    before_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    after_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    audit_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent2_daily_receipts_tenant_idempotency_key
        UNIQUE (tenant_id, idempotency_key),
    CONSTRAINT agent2_daily_receipts_status_check
        CHECK (status IN ('executed', 'duplicate', 'blocked'))
);

CREATE INDEX IF NOT EXISTS agent2_daily_receipts_tenant_created_idx
    ON agent2_daily_command_receipts (tenant_id, created_at DESC);

CREATE INDEX IF NOT EXISTS agent2_daily_receipts_user_date_idx
    ON agent2_daily_command_receipts (user_id, report_date DESC);

COMMIT;
