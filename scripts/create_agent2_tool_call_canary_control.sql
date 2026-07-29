BEGIN;

CREATE TABLE IF NOT EXISTS agent2_tool_call_canary_controls (
    control_id uuid PRIMARY KEY,
    control_key varchar(128) NOT NULL,
    tenant_id varchar(128) NOT NULL,
    user_id varchar(128) NOT NULL,
    enabled boolean NOT NULL DEFAULT false,
    runtime varchar(32) NOT NULL DEFAULT 'canary_execute',
    messages_enabled boolean NOT NULL DEFAULT false,
    registry_digest varchar(64) NOT NULL,
    prompt_sha256 varchar(64) NOT NULL,
    model_name varchar(128) NOT NULL,
    version integer NOT NULL DEFAULT 1,
    changed_by varchar(128) NOT NULL,
    change_reason text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent2_tool_call_canary_runtime_mode_check
        CHECK (runtime = 'canary_execute'),
    CONSTRAINT agent2_tool_call_canary_version_check
        CHECK (version >= 1),
    CONSTRAINT agent2_tool_call_canary_control_key
        UNIQUE (control_key),
    CONSTRAINT agent2_tool_call_canary_tenant_user_key
        UNIQUE (tenant_id, user_id)
);

CREATE INDEX IF NOT EXISTS agent2_tool_call_canary_route_idx
    ON agent2_tool_call_canary_controls (
        enabled,
        tenant_id,
        user_id
    );

-- The reviewed cohort size is enforced by the server-owned runtime setting,
-- audited controls, and exact tenant/user bindings.  The original single-user
-- partial index would reject every bounded cohort above one user.
DROP INDEX IF EXISTS agent2_tool_call_one_enabled_idx;

CREATE TABLE IF NOT EXISTS agent2_tool_call_canary_control_audits (
    audit_id uuid PRIMARY KEY,
    control_key varchar(128) NOT NULL,
    actor_user_id varchar(128) NOT NULL,
    source_change_id varchar(256) NOT NULL,
    before_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    after_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    reason text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent2_tool_call_canary_change_id_key
        UNIQUE (source_change_id)
);

CREATE INDEX IF NOT EXISTS agent2_tool_call_canary_audit_key_idx
    ON agent2_tool_call_canary_control_audits (
        control_key,
        created_at
    );

CREATE TABLE IF NOT EXISTS agent2_tool_call_clear_pendings (
    pending_id uuid PRIMARY KEY,
    namespace varchar(64) NOT NULL
        DEFAULT 'agent2.tool_calling.canary.v1',
    tenant_id varchar(128) NOT NULL,
    user_id varchar(128) NOT NULL,
    conversation_id varchar(256) NOT NULL,
    report_id uuid NOT NULL,
    report_version integer NOT NULL,
    report_state_hash varchar(64) NOT NULL,
    target_date date NOT NULL,
    expires_at timestamptz NOT NULL,
    source_message_id varchar(512) NOT NULL,
    consumed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent2_tool_call_clear_pending_namespace_check
        CHECK (namespace = 'agent2.tool_calling.canary.v1'),
    CONSTRAINT agent2_tool_call_clear_pending_version_check
        CHECK (report_version >= 0)
);

CREATE INDEX IF NOT EXISTS agent2_tool_call_clear_pending_scope_idx
    ON agent2_tool_call_clear_pendings (
        tenant_id,
        user_id,
        conversation_id,
        expires_at
    );

CREATE TABLE IF NOT EXISTS agent2_tool_call_receipts (
    receipt_id uuid PRIMARY KEY,
    tenant_id varchar(128) NOT NULL,
    user_id varchar(128) NOT NULL,
    conversation_id varchar(256) NOT NULL,
    source_message_id varchar(512) NOT NULL,
    tool_call_id varchar(256) NOT NULL,
    tool_name varchar(128) NOT NULL,
    idempotency_key varchar(256) NOT NULL,
    canonical_arguments_hash varchar(64) NOT NULL,
    request_fingerprint varchar(64) NOT NULL,
    operation_fingerprint varchar(64) NOT NULL,
    status varchar(32) NOT NULL,
    changed boolean NOT NULL DEFAULT false,
    target_type varchar(128) NOT NULL,
    target_id varchar(256) NOT NULL,
    before_version integer,
    after_version integer,
    affected_item_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    safe_user_facts jsonb NOT NULL DEFAULT '{}'::jsonb,
    before_state_hash varchar(64) NOT NULL,
    after_state_hash varchar(64) NOT NULL,
    typed_receipt_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    error_code varchar(128),
    execution_mode varchar(32) NOT NULL DEFAULT 'canary_execute',
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent2_tool_call_receipt_idempotency_key
        UNIQUE (tenant_id, idempotency_key),
    CONSTRAINT agent2_tool_call_receipt_call_identity_key
        UNIQUE (
            tenant_id,
            user_id,
            conversation_id,
            source_message_id,
            tool_call_id
        ),
    CONSTRAINT agent2_tool_call_receipt_operation_key
        UNIQUE (tenant_id, operation_fingerprint),
    CONSTRAINT agent2_tool_call_receipt_status_check
        CHECK (
            status IN (
                'success',
                'no_op',
                'blocked',
                'clarification_required',
                'failed'
            )
        ),
    CONSTRAINT agent2_tool_call_receipt_mode_check
        CHECK (execution_mode = 'canary_execute')
);

CREATE INDEX IF NOT EXISTS agent2_tool_call_receipt_scope_idx
    ON agent2_tool_call_receipts (
        tenant_id,
        user_id,
        created_at
    );

COMMIT;
