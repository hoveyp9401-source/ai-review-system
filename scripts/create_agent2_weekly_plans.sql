BEGIN;

CREATE TABLE IF NOT EXISTS agent2_weekly_plan_batches (
    batch_id uuid PRIMARY KEY,
    tenant_id varchar(128) NOT NULL,
    target_week_start date NOT NULL,
    status varchar(32) NOT NULL DEFAULT 'collecting',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent2_weekly_plan_batch_tenant_week_key
        UNIQUE (tenant_id, target_week_start),
    CONSTRAINT agent2_weekly_plan_batch_monday_check
        CHECK (EXTRACT(ISODOW FROM target_week_start) = 1),
    CONSTRAINT agent2_weekly_plan_batch_status_check
        CHECK (status IN ('collecting', 'snapshotted', 'cancelled'))
);

CREATE TABLE IF NOT EXISTS agent2_weekly_plan_roster_members (
    roster_member_id uuid PRIMARY KEY,
    tenant_id varchar(128) NOT NULL,
    batch_id uuid NOT NULL REFERENCES agent2_weekly_plan_batches(batch_id) ON DELETE RESTRICT,
    user_id varchar(128) NOT NULL,
    display_name varchar(256) NOT NULL,
    department_id varchar(128) NOT NULL DEFAULT '',
    department_name varchar(256) NOT NULL DEFAULT '',
    team_id varchar(128) NOT NULL DEFAULT '',
    team_name varchar(256) NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent2_weekly_plan_roster_user_key
        UNIQUE (tenant_id, batch_id, user_id)
);

CREATE TABLE IF NOT EXISTS agent2_weekly_plans (
    plan_id uuid PRIMARY KEY,
    tenant_id varchar(128) NOT NULL,
    batch_id uuid NOT NULL REFERENCES agent2_weekly_plan_batches(batch_id) ON DELETE RESTRICT,
    owner_user_id varchar(128) NOT NULL,
    target_week_start date NOT NULL,
    status varchar(32) NOT NULL DEFAULT 'collecting',
    version integer NOT NULL DEFAULT 0,
    submitted_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent2_weekly_plan_owner_week_key
        UNIQUE (tenant_id, owner_user_id, target_week_start),
    CONSTRAINT agent2_weekly_plan_monday_check
        CHECK (EXTRACT(ISODOW FROM target_week_start) = 1),
    CONSTRAINT agent2_weekly_plan_status_check
        CHECK (status IN ('collecting', 'pending_confirmation', 'submitted', 'cancelled')),
    CONSTRAINT agent2_weekly_plan_version_check CHECK (version >= 0),
    CONSTRAINT agent2_weekly_plan_submission_check
        CHECK (
            (status IN ('collecting', 'pending_confirmation') AND submitted_at IS NULL)
            OR (status = 'submitted' AND submitted_at IS NOT NULL)
            OR status = 'cancelled'
        )
);

CREATE INDEX IF NOT EXISTS agent2_weekly_plan_batch_status_idx
    ON agent2_weekly_plans (tenant_id, batch_id, status);

CREATE TABLE IF NOT EXISTS agent2_weekly_plan_days (
    day_id uuid PRIMARY KEY,
    tenant_id varchar(128) NOT NULL,
    plan_id uuid NOT NULL REFERENCES agent2_weekly_plans(plan_id) ON DELETE RESTRICT,
    plan_date date NOT NULL,
    day_index smallint NOT NULL,
    state varchar(32) NOT NULL DEFAULT 'unfilled',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent2_weekly_plan_day_date_key UNIQUE (tenant_id, plan_id, plan_date),
    CONSTRAINT agent2_weekly_plan_day_index_key UNIQUE (tenant_id, plan_id, day_index),
    CONSTRAINT agent2_weekly_plan_day_index_check CHECK (day_index BETWEEN 1 AND 6),
    CONSTRAINT agent2_weekly_plan_day_state_check
        CHECK (state IN ('unfilled', 'explicitly_empty', 'planned'))
);

CREATE TABLE IF NOT EXISTS agent2_weekly_plan_items (
    item_id uuid PRIMARY KEY,
    tenant_id varchar(128) NOT NULL,
    plan_id uuid NOT NULL REFERENCES agent2_weekly_plans(plan_id) ON DELETE RESTRICT,
    day_id uuid NOT NULL REFERENCES agent2_weekly_plan_days(day_id) ON DELETE RESTRICT,
    original_text text NOT NULL,
    source varchar(64) NOT NULL,
    source_ref varchar(512) NOT NULL DEFAULT '',
    position integer NOT NULL DEFAULT 0,
    deleted_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent2_weekly_plan_item_text_check CHECK (length(btrim(original_text)) > 0),
    CONSTRAINT agent2_weekly_plan_item_position_check CHECK (position >= 0)
);

CREATE INDEX IF NOT EXISTS agent2_weekly_plan_item_day_idx
    ON agent2_weekly_plan_items (tenant_id, day_id, position)
    WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS agent2_weekly_plan_suggestions (
    suggestion_id varchar(80) PRIMARY KEY,
    tenant_id varchar(128) NOT NULL,
    plan_id uuid NOT NULL REFERENCES agent2_weekly_plans(plan_id) ON DELETE RESTRICT,
    owner_user_id varchar(128) NOT NULL,
    target_week_start date NOT NULL,
    source_kind varchar(64) NOT NULL,
    source_ref varchar(512) NOT NULL,
    source_version varchar(128) NOT NULL,
    evidence_text text NOT NULL,
    evidence_sha256 varchar(64) NOT NULL,
    matter_excerpt text NOT NULL,
    expires_at timestamptz NOT NULL,
    status varchar(32) NOT NULL DEFAULT 'available',
    decision_ref varchar(512),
    accepted_item_id uuid REFERENCES agent2_weekly_plan_items(item_id) ON DELETE RESTRICT,
    decided_at timestamptz,
    superseded_by_id varchar(80),
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent2_weekly_plan_suggestion_source_key
        UNIQUE (tenant_id, plan_id, source_kind, source_ref, source_version),
    CONSTRAINT agent2_weekly_plan_suggestion_text_check
        CHECK (
            length(btrim(evidence_text)) > 0
            AND length(btrim(matter_excerpt)) > 0
            AND length(evidence_sha256) = 64
        ),
    CONSTRAINT agent2_weekly_plan_suggestion_source_kind_check
        CHECK (source_kind IN ('user_original_message', 'confirmed_record')),
    CONSTRAINT agent2_weekly_plan_suggestion_status_check
        CHECK (status IN ('available', 'accepted', 'rejected', 'expired', 'superseded')),
    CONSTRAINT agent2_weekly_plan_suggestion_resolution_check
        CHECK (
            (
                status = 'available' AND decision_ref IS NULL AND decided_at IS NULL
                AND superseded_by_id IS NULL AND accepted_item_id IS NULL
            )
            OR (
                status = 'rejected' AND decision_ref IS NOT NULL AND decided_at IS NOT NULL
                AND superseded_by_id IS NULL AND accepted_item_id IS NULL
            )
            OR (
                status = 'accepted' AND decision_ref IS NOT NULL AND decided_at IS NOT NULL
                AND superseded_by_id IS NULL AND accepted_item_id IS NOT NULL
            )
            OR (
                status = 'expired' AND decision_ref IS NULL AND decided_at IS NOT NULL
                AND superseded_by_id IS NULL AND accepted_item_id IS NULL
            )
            OR (
                status = 'superseded' AND decision_ref IS NULL AND decided_at IS NOT NULL
                AND superseded_by_id IS NOT NULL AND accepted_item_id IS NULL
            )
        )
);

CREATE TABLE IF NOT EXISTS agent2_weekly_plan_command_receipts (
    receipt_id uuid PRIMARY KEY,
    tenant_id varchar(128) NOT NULL,
    idempotency_key varchar(512) NOT NULL,
    command_id varchar(256) NOT NULL,
    command_type varchar(64) NOT NULL,
    actor_user_id varchar(128) NOT NULL,
    source_message_id varchar(512) NOT NULL,
    request_sha256 varchar(64) NOT NULL,
    plan_id uuid NOT NULL REFERENCES agent2_weekly_plans(plan_id) ON DELETE RESTRICT,
    status varchar(32) NOT NULL,
    reason_code varchar(128) NOT NULL DEFAULT '',
    actual_write boolean NOT NULL DEFAULT false,
    before_version integer NOT NULL,
    after_version integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent2_weekly_plan_receipt_idempotency_key
        UNIQUE (tenant_id, idempotency_key),
    CONSTRAINT agent2_weekly_plan_receipt_status_check
        CHECK (status IN ('executed', 'duplicate', 'blocked')),
    CONSTRAINT agent2_weekly_plan_receipt_version_check
        CHECK (before_version >= 0 AND after_version >= 0),
    CONSTRAINT agent2_weekly_plan_receipt_request_hash_check
        CHECK (length(request_sha256) = 64)
);

CREATE INDEX IF NOT EXISTS agent2_weekly_plan_receipt_source_idx
    ON agent2_weekly_plan_command_receipts (tenant_id, source_message_id, created_at DESC);

CREATE TABLE IF NOT EXISTS agent2_weekly_plan_audit_events (
    audit_id uuid PRIMARY KEY,
    tenant_id varchar(128) NOT NULL,
    receipt_id uuid NOT NULL REFERENCES agent2_weekly_plan_command_receipts(receipt_id) ON DELETE RESTRICT,
    plan_id uuid NOT NULL REFERENCES agent2_weekly_plans(plan_id) ON DELETE RESTRICT,
    actor_user_id varchar(128) NOT NULL,
    command_type varchar(64) NOT NULL,
    source_message_id varchar(512) NOT NULL,
    before_json jsonb NOT NULL,
    after_json jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS agent2_weekly_plan_audit_plan_idx
    ON agent2_weekly_plan_audit_events (tenant_id, plan_id, created_at DESC);

CREATE TABLE IF NOT EXISTS agent2_weekly_plan_monday_snapshots (
    snapshot_id uuid PRIMARY KEY,
    tenant_id varchar(128) NOT NULL,
    batch_id uuid NOT NULL REFERENCES agent2_weekly_plan_batches(batch_id) ON DELETE RESTRICT,
    target_week_start date NOT NULL,
    as_of timestamptz NOT NULL,
    deadline_at timestamptz NOT NULL,
    roster_count integer NOT NULL,
    submitted_count integer NOT NULL,
    draft_count integer NOT NULL,
    unfilled_count integer NOT NULL,
    rows_json jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent2_weekly_plan_monday_batch_key UNIQUE (tenant_id, batch_id),
    CONSTRAINT agent2_weekly_plan_monday_date_check
        CHECK (EXTRACT(ISODOW FROM target_week_start) = 1),
    CONSTRAINT agent2_weekly_plan_monday_counts_check
        CHECK (
            roster_count >= 0
            AND submitted_count >= 0
            AND draft_count >= 0
            AND unfilled_count >= 0
            AND roster_count = submitted_count + draft_count + unfilled_count
    )
);

CREATE TABLE IF NOT EXISTS agent2_weekly_plan_reminder_outbox (
    outbox_id uuid PRIMARY KEY,
    tenant_id varchar(128) NOT NULL,
    batch_id uuid NOT NULL REFERENCES agent2_weekly_plan_batches(batch_id) ON DELETE RESTRICT,
    plan_id uuid REFERENCES agent2_weekly_plans(plan_id) ON DELETE RESTRICT,
    target_week_start date NOT NULL,
    recipient_internal_user_id varchar(128) NOT NULL,
    collection_state varchar(32) NOT NULL,
    reminder_at timestamptz NOT NULL,
    channel varchar(32) NOT NULL DEFAULT 'private_chat',
    idempotency_key varchar(512) NOT NULL,
    status varchar(32) NOT NULL DEFAULT 'queued',
    retry_count integer NOT NULL DEFAULT 0,
    claim_token varchar(256) NOT NULL DEFAULT '',
    provider_message_id varchar(512) NOT NULL DEFAULT '',
    provider_accepted_at timestamptz,
    delivered_at timestamptz,
    failed_at timestamptz,
    cancelled_at timestamptz,
    last_error text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent2_weekly_plan_reminder_idempotency_key
        UNIQUE (tenant_id, idempotency_key),
    CONSTRAINT agent2_weekly_plan_reminder_status_check
        CHECK (status IN (
            'queued', 'claimed', 'delivery_pending', 'delivered', 'failed', 'cancelled'
        )),
    CONSTRAINT agent2_weekly_plan_reminder_channel_check
        CHECK (channel = 'private_chat'),
    CONSTRAINT agent2_weekly_plan_reminder_retry_check CHECK (retry_count >= 0),
    CONSTRAINT agent2_weekly_plan_reminder_delivery_check
        CHECK (
            (status = 'queued' AND claim_token = '' AND provider_message_id = ''
                AND provider_accepted_at IS NULL AND delivered_at IS NULL)
            OR (status = 'claimed' AND claim_token <> '' AND delivered_at IS NULL)
            OR (status = 'delivery_pending' AND provider_message_id <> ''
                AND provider_accepted_at IS NOT NULL AND delivered_at IS NULL)
            OR (status = 'delivered' AND provider_message_id <> ''
                AND provider_accepted_at IS NOT NULL AND delivered_at IS NOT NULL)
            OR (status = 'failed' AND failed_at IS NOT NULL)
            OR (status = 'cancelled' AND cancelled_at IS NOT NULL)
        )
);

CREATE INDEX IF NOT EXISTS agent2_weekly_plan_reminder_claim_idx
    ON agent2_weekly_plan_reminder_outbox (status, reminder_at, created_at)
    WHERE status = 'queued';

-- Keep reruns safe if an earlier development schema created the snapshot table
-- before deadline_at was introduced.
ALTER TABLE agent2_weekly_plan_monday_snapshots
    ADD COLUMN IF NOT EXISTS deadline_at timestamptz;
UPDATE agent2_weekly_plan_monday_snapshots
    SET deadline_at = as_of
    WHERE deadline_at IS NULL;
ALTER TABLE agent2_weekly_plan_monday_snapshots
    ALTER COLUMN deadline_at SET NOT NULL;

-- Compatibility for pre-release databases created before the collection
-- status name was clarified from physical row creation to business meaning.
ALTER TABLE agent2_weekly_plan_monday_snapshots
    ADD COLUMN IF NOT EXISTS unfilled_count integer;
UPDATE agent2_weekly_plan_monday_snapshots
    SET unfilled_count = roster_count - submitted_count - draft_count
    WHERE unfilled_count IS NULL;
ALTER TABLE agent2_weekly_plan_monday_snapshots
    ALTER COLUMN unfilled_count SET NOT NULL;

COMMIT;
