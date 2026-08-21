BEGIN;

CREATE TABLE IF NOT EXISTS agent2_personal_weekly_briefs (
    brief_id uuid PRIMARY KEY,
    tenant_id varchar(128) NOT NULL,
    owner_user_id varchar(128) NOT NULL,
    conversation_id varchar(256) NOT NULL,
    week_start date NOT NULL,
    week_end date NOT NULL,
    snapshot_at timestamptz NOT NULL,
    source_snapshot jsonb NOT NULL,
    source_fingerprint varchar(64) NOT NULL,
    personal_memory_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    content_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    message_text text NOT NULL DEFAULT '',
    llm_model varchar(128) NOT NULL DEFAULT '',
    status varchar(32) NOT NULL,
    idempotency_key varchar(512) NOT NULL,
    claim_token varchar(256) NOT NULL DEFAULT '',
    provider_message_id varchar(512) NOT NULL DEFAULT '',
    provider_accepted_at timestamptz,
    delivery_receipt_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    delivered_at timestamptz,
    context_recorded_at timestamptz,
    failed_at timestamptz,
    last_error text NOT NULL DEFAULT '',
    retry_count integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CONSTRAINT agent2_personal_weekly_brief_owner_week_key
        UNIQUE (tenant_id, owner_user_id, week_start),
    CONSTRAINT agent2_personal_weekly_brief_idempotency_key
        UNIQUE (tenant_id, idempotency_key),
    CONSTRAINT agent2_personal_weekly_brief_monday_check
        CHECK (EXTRACT(ISODOW FROM week_start) = 1),
    CONSTRAINT agent2_personal_weekly_brief_week_end_check
        CHECK (week_end = week_start + 4),
    CONSTRAINT agent2_personal_weekly_brief_fingerprint_check
        CHECK (length(source_fingerprint) = 64),
    CONSTRAINT agent2_personal_weekly_brief_retry_check
        CHECK (retry_count >= 0),
    CONSTRAINT agent2_personal_weekly_brief_status_check
        CHECK (status IN (
            'snapshot_ready', 'generation_failed', 'generated', 'claimed',
            'delivery_pending', 'delivered', 'failed', 'cancelled'
        )),
    CONSTRAINT agent2_personal_weekly_brief_generated_content_check
        CHECK (
            status IN ('snapshot_ready', 'generation_failed', 'cancelled')
            OR (
                length(btrim(message_text)) > 0
                AND length(btrim(llm_model)) > 0
                AND content_json <> '{}'::jsonb
            )
        ),
    CONSTRAINT agent2_personal_weekly_brief_provider_check
        CHECK (
            status NOT IN ('delivery_pending', 'delivered')
            OR (
                length(btrim(provider_message_id)) > 0
                AND provider_accepted_at IS NOT NULL
            )
        ),
    CONSTRAINT agent2_personal_weekly_brief_delivery_check
        CHECK (
            (
                status = 'delivered'
                AND delivered_at IS NOT NULL
                AND delivery_receipt_json <> '{}'::jsonb
            )
            OR (status <> 'delivered' AND delivered_at IS NULL)
        )
);

CREATE INDEX IF NOT EXISTS agent2_personal_weekly_brief_status_idx
    ON agent2_personal_weekly_briefs
    (tenant_id, status, week_start, owner_user_id);

CREATE INDEX IF NOT EXISTS agent2_personal_weekly_brief_context_idx
    ON agent2_personal_weekly_briefs
    (tenant_id, delivered_at, owner_user_id)
    WHERE status = 'delivered' AND context_recorded_at IS NULL;

COMMIT;
