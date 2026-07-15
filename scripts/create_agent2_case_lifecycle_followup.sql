BEGIN;

CREATE TABLE IF NOT EXISTS agent2_case_followup_policies (
    policy_id uuid PRIMARY KEY,
    tenant_id varchar(128) NOT NULL,
    case_id uuid NOT NULL,
    assigned_user_id varchar(128) NOT NULL,
    enabled boolean NOT NULL DEFAULT false,
    policy_source varchar(32) NOT NULL DEFAULT 'tenant_default'
        CHECK (policy_source IN ('tenant_default','stage_default','bulk_assignment','case_manual_override')),
    cadence_type varchar(32) NOT NULL DEFAULT 'event_only'
        CHECK (cadence_type IN ('daily','weekly','every_15_days','monthly','custom_interval','event_only','manual_only','paused','disabled')),
    cadence_days integer,
    custom_interval_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    timezone varchar(64) NOT NULL DEFAULT 'Asia/Shanghai',
    business_days_only boolean NOT NULL DEFAULT false,
    allowed_start_time time NOT NULL DEFAULT '09:00:00',
    allowed_end_time time NOT NULL DEFAULT '18:00:00',
    event_triggers_enabled boolean NOT NULL DEFAULT true,
    hearing_reminders_enabled boolean NOT NULL DEFAULT true,
    stage_transition_enabled boolean NOT NULL DEFAULT true,
    node_transition_enabled boolean NOT NULL DEFAULT true,
    last_meaningful_progress_at timestamptz,
    last_followup_at timestamptz,
    next_due_at timestamptz,
    snoozed_until timestamptz,
    max_unanswered_reminders integer NOT NULL DEFAULT 1,
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS agent2_case_followup_policy_scope_key
    ON agent2_case_followup_policies (tenant_id, case_id, assigned_user_id);
CREATE INDEX IF NOT EXISTS agent2_case_followup_policy_due_idx
    ON agent2_case_followup_policies (tenant_id, enabled, next_due_at);

CREATE TABLE IF NOT EXISTS agent2_case_lifecycle_states (
    lifecycle_state_id uuid PRIMARY KEY,
    tenant_id varchar(128) NOT NULL,
    case_id uuid NOT NULL,
    assigned_user_id varchar(128) NOT NULL,
    case_type varchar(32) NOT NULL DEFAULT '',
    stage varchar(64) NOT NULL DEFAULT '',
    node varchar(64) NOT NULL DEFAULT '',
    current_status text NOT NULL DEFAULT '',
    next_actions_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    hearing_readiness text NOT NULL DEFAULT '',
    blocking_issues_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    last_progress_id uuid,
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS agent2_case_lifecycle_state_case_key
    ON agent2_case_lifecycle_states (tenant_id, case_id);
CREATE INDEX IF NOT EXISTS agent2_case_lifecycle_state_owner_idx
    ON agent2_case_lifecycle_states (tenant_id, assigned_user_id, stage, node);

CREATE TABLE IF NOT EXISTS agent2_case_followup_tasks (
    followup_id uuid PRIMARY KEY,
    tenant_id varchar(128) NOT NULL,
    case_id uuid NOT NULL,
    assigned_user_id varchar(128) NOT NULL,
    policy_id uuid,
    trigger_type varchar(64) NOT NULL,
    trigger_event_id varchar(256) NOT NULL DEFAULT '',
    trigger_sources_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    case_type varchar(32) NOT NULL,
    stage varchar(64) NOT NULL DEFAULT '',
    node varchar(64) NOT NULL DEFAULT '',
    case_version integer NOT NULL,
    question_type varchar(64) NOT NULL,
    question_text text NOT NULL,
    priority integer NOT NULL DEFAULT 0,
    task_status varchar(32) NOT NULL DEFAULT 'scheduled'
        CHECK (task_status IN ('scheduled','queued','sending','waiting_for_reply','answered','snoozed','cancelled','expired','failed')),
    message_status varchar(32) NOT NULL DEFAULT 'scheduled'
        CHECK (message_status IN ('scheduled','queued','sending','accepted_by_provider','delivery_confirmed','failed','cancelled')),
    response_status varchar(32) NOT NULL DEFAULT 'not_requested'
        CHECK (response_status IN ('not_requested','awaiting_input','answered','snoozed','cancelled','expired')),
    due_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    last_sent_at timestamptz,
    next_eligible_at timestamptz,
    reminder_count integer NOT NULL DEFAULT 0,
    max_reminders integer NOT NULL DEFAULT 1,
    conversation_id varchar(256) NOT NULL DEFAULT '',
    pending_id uuid,
    source_progress_id uuid,
    provider_message_id varchar(256) NOT NULL DEFAULT '',
    completed_at timestamptz,
    cancelled_at timestamptz,
    idempotency_key varchar(512) NOT NULL,
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE agent2_case_followup_tasks
    ADD COLUMN IF NOT EXISTS question_text text NOT NULL DEFAULT '';
CREATE UNIQUE INDEX IF NOT EXISTS agent2_case_followup_task_idempotency_key
    ON agent2_case_followup_tasks (tenant_id, idempotency_key);
CREATE INDEX IF NOT EXISTS agent2_case_followup_task_due_idx
    ON agent2_case_followup_tasks (tenant_id, task_status, due_at);
CREATE INDEX IF NOT EXISTS agent2_case_followup_task_user_idx
    ON agent2_case_followup_tasks (tenant_id, assigned_user_id, response_status);

CREATE TABLE IF NOT EXISTS agent2_case_followup_pendings (
    pending_id uuid PRIMARY KEY,
    pending_type varchar(32) NOT NULL DEFAULT 'case_followup'
        CHECK (pending_type IN ('case_followup','selection','confirmation','information')),
    tenant_id varchar(128) NOT NULL,
    user_id varchar(128) NOT NULL,
    conversation_id varchar(256) NOT NULL,
    domain varchar(64) NOT NULL DEFAULT 'case',
    operation varchar(128) NOT NULL,
    source_turn_id varchar(256) NOT NULL,
    source_message_id varchar(256) NOT NULL,
    task_id uuid NOT NULL,
    case_id uuid NOT NULL,
    followup_id uuid NOT NULL,
    candidate_refs_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    candidate_versions_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    candidate_labels_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    acceptable_answer_forms_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    expected_state_version integer NOT NULL,
    expires_at timestamptz NOT NULL,
    status varchar(32) NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','awaiting_input','consumed','expired','cancelled','conflicted','permission_revoked')),
    consumed_at timestamptz,
    cancelled_at timestamptz,
    idempotency_key varchar(512) NOT NULL,
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS agent2_case_followup_pending_idempotency_key
    ON agent2_case_followup_pendings (tenant_id, idempotency_key);
CREATE INDEX IF NOT EXISTS agent2_case_followup_pending_scope_idx
    ON agent2_case_followup_pendings (tenant_id, user_id, conversation_id, status);

CREATE TABLE IF NOT EXISTS agent2_task_ledger (
    task_id uuid PRIMARY KEY,
    tenant_id varchar(128) NOT NULL,
    user_id varchar(128) NOT NULL,
    conversation_id varchar(256) NOT NULL,
    domain varchar(64) NOT NULL,
    operation varchar(128) NOT NULL,
    object_ref_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    status varchar(32) NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','awaiting_input','suspended','completed','cancelled','failed','expired')),
    focus_state varchar(32) NOT NULL DEFAULT 'active'
        CHECK (focus_state IN ('focused','active','suspended')),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    source_turn_id varchar(256) NOT NULL,
    pending_requirements_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    resume_policy_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS agent2_task_ledger_one_focused
    ON agent2_task_ledger (tenant_id, user_id, conversation_id)
    WHERE focus_state = 'focused' AND status IN ('active','awaiting_input');
CREATE INDEX IF NOT EXISTS agent2_task_ledger_scope_idx
    ON agent2_task_ledger (tenant_id, user_id, conversation_id, status);

CREATE TABLE IF NOT EXISTS agent2_report_projection_requests (
    request_id uuid PRIMARY KEY,
    tenant_id varchar(128) NOT NULL,
    user_id varchar(128) NOT NULL,
    case_id uuid NOT NULL,
    case_progress_id uuid NOT NULL,
    followup_id uuid,
    source_turn_id varchar(256) NOT NULL,
    source_message_id varchar(256) NOT NULL,
    case_receipt_id uuid NOT NULL,
    decision_json jsonb NOT NULL,
    status varchar(32) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','processing','succeeded','skipped','failed','cancelled')),
    idempotency_key varchar(512) NOT NULL,
    attempt_count integer NOT NULL DEFAULT 0,
    last_error text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS agent2_report_projection_request_key
    ON agent2_report_projection_requests (tenant_id, idempotency_key);
CREATE INDEX IF NOT EXISTS agent2_report_projection_request_claim_idx
    ON agent2_report_projection_requests (tenant_id, status, created_at);

CREATE TABLE IF NOT EXISTS agent2_case_report_projections (
    projection_id uuid PRIMARY KEY,
    tenant_id varchar(128) NOT NULL,
    user_id varchar(128) NOT NULL,
    case_id uuid NOT NULL,
    case_progress_id uuid NOT NULL,
    report_id uuid NOT NULL,
    report_item_id varchar(256) NOT NULL,
    report_type varchar(16) NOT NULL,
    projection_type varchar(32) NOT NULL,
    source_turn_id varchar(256) NOT NULL,
    source_followup_id uuid,
    source_message_id varchar(256) NOT NULL,
    status varchar(32) NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','removed','failed')),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    removed_at timestamptz,
    idempotency_key varchar(512) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS agent2_case_report_projection_key
    ON agent2_case_report_projections (tenant_id, idempotency_key);
CREATE INDEX IF NOT EXISTS agent2_case_report_projection_case_idx
    ON agent2_case_report_projections (tenant_id, case_id, status);

CREATE TABLE IF NOT EXISTS agent2_operation_outcomes (
    outcome_id uuid PRIMARY KEY,
    tenant_id varchar(128) NOT NULL,
    user_id varchar(128) NOT NULL,
    conversation_id varchar(256) NOT NULL,
    source_turn_id varchar(256) NOT NULL,
    domain varchar(64) NOT NULL,
    operation varchar(128) NOT NULL,
    object_type varchar(128) NOT NULL,
    object_id varchar(256) NOT NULL DEFAULT '',
    object_label text NOT NULL DEFAULT '',
    object_version integer,
    business_status varchar(64) NOT NULL,
    message_status varchar(64) NOT NULL,
    actual_write boolean NOT NULL DEFAULT false,
    would_write boolean NOT NULL DEFAULT false,
    changed_fields_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    user_visible_snapshot_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    blocking_reason text NOT NULL DEFAULT '',
    receipt_refs_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    audit_refs_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    state_transition_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    idempotency_key varchar(512) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE agent2_operation_outcomes
    ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE agent2_operation_outcomes
    ADD COLUMN IF NOT EXISTS object_label text;
ALTER TABLE agent2_operation_outcomes
    ADD COLUMN IF NOT EXISTS object_version integer;
UPDATE agent2_operation_outcomes
SET object_label = ''
WHERE object_label IS NULL;
ALTER TABLE agent2_operation_outcomes
    ALTER COLUMN object_label SET DEFAULT '',
    ALTER COLUMN object_label SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS agent2_operation_outcome_key
    ON agent2_operation_outcomes (tenant_id, idempotency_key);
CREATE INDEX IF NOT EXISTS agent2_operation_outcome_source_idx
    ON agent2_operation_outcomes (tenant_id, source_turn_id, created_at);

COMMIT;
