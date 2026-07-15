BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS agent2_identity_bindings (
    binding_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id varchar(128) NOT NULL,
    company_id varchar(128) NOT NULL,
    department_id varchar(128) NOT NULL,
    team_id varchar(128) NOT NULL,
    user_id varchar(128) NOT NULL,
    dingtalk_user_id varchar(128) NOT NULL,
    display_name varchar(256) NOT NULL DEFAULT '',
    role_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    permission_scope_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent2_identity_tenant_user_key UNIQUE (tenant_id, user_id),
    CONSTRAINT agent2_identity_tenant_dingtalk_key UNIQUE (tenant_id, dingtalk_user_id)
);
CREATE INDEX IF NOT EXISTS agent2_identity_scope_idx
    ON agent2_identity_bindings (tenant_id, company_id, department_id, team_id);

CREATE TABLE IF NOT EXISTS agent2_cases (
    case_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id varchar(128) NOT NULL,
    company_id varchar(128) NOT NULL,
    department_id varchar(128) NOT NULL,
    team_id varchar(128) NOT NULL,
    external_case_id varchar(256) NOT NULL,
    case_number varchar(256) NOT NULL DEFAULT '',
    case_name text NOT NULL,
    case_type varchar(64) NOT NULL DEFAULT '',
    status varchar(64) NOT NULL DEFAULT 'open',
    owner_user_id varchar(128) NOT NULL DEFAULT '',
    source_type varchar(64) NOT NULL,
    source_id varchar(512) NOT NULL,
    source_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    version integer NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent2_cases_tenant_external_key UNIQUE (tenant_id, external_case_id),
    CONSTRAINT agent2_cases_tenant_case_key UNIQUE (tenant_id, case_id)
);
CREATE INDEX IF NOT EXISTS agent2_cases_number_idx ON agent2_cases (tenant_id, case_number);
CREATE INDEX IF NOT EXISTS agent2_cases_owner_idx ON agent2_cases (tenant_id, owner_user_id, status);
CREATE INDEX IF NOT EXISTS agent2_cases_name_trgm_idx
    ON agent2_cases USING gin (case_name gin_trgm_ops);

CREATE TABLE IF NOT EXISTS agent2_party_entities (
    party_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id varchar(128) NOT NULL,
    party_type varchar(32) NOT NULL CHECK (party_type IN ('company', 'person', 'organization', 'government', 'court', 'other')),
    canonical_name text NOT NULL,
    normalized_name text NOT NULL,
    short_name varchar(256) NOT NULL DEFAULT '',
    former_names jsonb NOT NULL DEFAULT '[]'::jsonb,
    unified_social_credit_code varchar(64) NOT NULL DEFAULT '',
    registration_number varchar(64) NOT NULL DEFAULT '',
    legal_representative varchar(256) NOT NULL DEFAULT '',
    status varchar(64) NOT NULL DEFAULT 'active',
    registered_address text NOT NULL DEFAULT '',
    source_type varchar(64) NOT NULL,
    source_id varchar(512) NOT NULL,
    data_quality varchar(64) NOT NULL DEFAULT 'unverified',
    version integer NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent2_party_tenant_party_key UNIQUE (tenant_id, party_id)
);
CREATE INDEX IF NOT EXISTS agent2_party_name_idx ON agent2_party_entities (tenant_id, normalized_name);
CREATE INDEX IF NOT EXISTS agent2_party_name_trgm_idx
    ON agent2_party_entities USING gin (normalized_name gin_trgm_ops);
CREATE UNIQUE INDEX IF NOT EXISTS agent2_party_uscc_unique_idx
    ON agent2_party_entities (tenant_id, unified_social_credit_code)
    WHERE unified_social_credit_code <> '';
CREATE UNIQUE INDEX IF NOT EXISTS agent2_party_registration_unique_idx
    ON agent2_party_entities (tenant_id, registration_number)
    WHERE registration_number <> '';

CREATE TABLE IF NOT EXISTS agent2_party_aliases (
    alias_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id varchar(128) NOT NULL,
    party_id uuid NOT NULL,
    alias text NOT NULL,
    normalized_alias text NOT NULL,
    alias_type varchar(64) NOT NULL DEFAULT 'alias',
    source_type varchar(64) NOT NULL,
    source_id varchar(512) NOT NULL DEFAULT '',
    confirmation_status varchar(32) NOT NULL DEFAULT 'confirmed',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent2_party_alias_unique UNIQUE (tenant_id, normalized_alias, party_id),
    CONSTRAINT agent2_party_alias_party_fk FOREIGN KEY (tenant_id, party_id)
        REFERENCES agent2_party_entities (tenant_id, party_id)
);
CREATE INDEX IF NOT EXISTS agent2_party_alias_lookup_idx ON agent2_party_aliases (tenant_id, normalized_alias);
CREATE INDEX IF NOT EXISTS agent2_party_alias_trgm_idx
    ON agent2_party_aliases USING gin (normalized_alias gin_trgm_ops);

CREATE TABLE IF NOT EXISTS agent2_party_identifiers (
    identifier_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id varchar(128) NOT NULL,
    party_id uuid NOT NULL,
    identifier_type varchar(64) NOT NULL,
    identifier_value varchar(256) NOT NULL,
    normalized_value varchar(256) NOT NULL,
    source_type varchar(64) NOT NULL,
    source_id varchar(512) NOT NULL DEFAULT '',
    confirmation_status varchar(32) NOT NULL DEFAULT 'confirmed',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent2_party_identifier_unique UNIQUE (tenant_id, identifier_type, normalized_value),
    CONSTRAINT agent2_party_identifier_party_fk FOREIGN KEY (tenant_id, party_id)
        REFERENCES agent2_party_entities (tenant_id, party_id)
);
CREATE INDEX IF NOT EXISTS agent2_party_identifier_lookup_idx
    ON agent2_party_identifiers (tenant_id, normalized_value);

CREATE TABLE IF NOT EXISTS agent2_party_case_roles (
    role_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id varchar(128) NOT NULL,
    party_id uuid NOT NULL,
    case_id uuid NOT NULL,
    role_type varchar(64) NOT NULL,
    effective_from timestamptz,
    effective_to timestamptz,
    source_reference jsonb NOT NULL DEFAULT '{}'::jsonb,
    confirmation_status varchar(32) NOT NULL DEFAULT 'confirmed',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent2_party_case_role_unique UNIQUE NULLS NOT DISTINCT
        (tenant_id, party_id, case_id, role_type, effective_from),
    CONSTRAINT agent2_party_case_role_party_fk FOREIGN KEY (tenant_id, party_id)
        REFERENCES agent2_party_entities (tenant_id, party_id),
    CONSTRAINT agent2_party_case_role_case_fk FOREIGN KEY (tenant_id, case_id)
        REFERENCES agent2_cases (tenant_id, case_id),
    CHECK (effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from)
);
CREATE INDEX IF NOT EXISTS agent2_party_case_role_party_idx
    ON agent2_party_case_roles (tenant_id, party_id, role_type);
CREATE INDEX IF NOT EXISTS agent2_party_case_role_case_idx
    ON agent2_party_case_roles (tenant_id, case_id);

CREATE TABLE IF NOT EXISTS agent2_party_relations (
    relation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id varchar(128) NOT NULL,
    case_id uuid NOT NULL,
    from_party_id uuid NOT NULL,
    to_party_id uuid NOT NULL,
    relation_type varchar(64) NOT NULL,
    source_reference jsonb NOT NULL DEFAULT '{}'::jsonb,
    confirmation_status varchar(32) NOT NULL DEFAULT 'pending_confirmation',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent2_party_relation_unique UNIQUE (tenant_id, case_id, from_party_id, to_party_id, relation_type),
    CONSTRAINT agent2_party_relation_case_fk FOREIGN KEY (tenant_id, case_id)
        REFERENCES agent2_cases (tenant_id, case_id),
    CONSTRAINT agent2_party_relation_from_fk FOREIGN KEY (tenant_id, from_party_id)
        REFERENCES agent2_party_entities (tenant_id, party_id),
    CONSTRAINT agent2_party_relation_to_fk FOREIGN KEY (tenant_id, to_party_id)
        REFERENCES agent2_party_entities (tenant_id, party_id),
    CHECK (from_party_id <> to_party_id)
);
ALTER TABLE agent2_party_relations
    ADD COLUMN IF NOT EXISTS case_id uuid;
ALTER TABLE agent2_party_relations
    DROP CONSTRAINT IF EXISTS agent2_party_relation_unique;
ALTER TABLE agent2_party_relations
    ADD CONSTRAINT agent2_party_relation_unique
    UNIQUE (tenant_id, case_id, from_party_id, to_party_id, relation_type);
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'agent2_party_relation_case_fk'
    ) THEN
        ALTER TABLE agent2_party_relations
            ADD CONSTRAINT agent2_party_relation_case_fk
            FOREIGN KEY (tenant_id, case_id)
            REFERENCES agent2_cases (tenant_id, case_id);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS agent2_party_case_clues (
    clue_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id varchar(128) NOT NULL,
    case_id uuid NOT NULL,
    party_id uuid NOT NULL,
    clue_type varchar(32) NOT NULL CHECK (clue_type IN ('person', 'court', 'payment', 'asset', 'document', 'other')),
    label varchar(256) NOT NULL DEFAULT '',
    summary text NOT NULL,
    amount numeric(20,2),
    currency varchar(16) NOT NULL DEFAULT 'CNY',
    occurred_at timestamptz,
    source_type varchar(64) NOT NULL,
    source_id varchar(512) NOT NULL,
    source_field varchar(256) NOT NULL DEFAULT '',
    source_reference jsonb NOT NULL DEFAULT '{}'::jsonb,
    confirmation_status varchar(32) NOT NULL DEFAULT 'confirmed',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent2_party_case_clue_unique UNIQUE (
        tenant_id, case_id, party_id, clue_type, source_type, source_id, source_field
    ),
    CONSTRAINT agent2_party_case_clue_party_fk FOREIGN KEY (tenant_id, party_id)
        REFERENCES agent2_party_entities (tenant_id, party_id),
    CONSTRAINT agent2_party_case_clue_case_fk FOREIGN KEY (tenant_id, case_id)
        REFERENCES agent2_cases (tenant_id, case_id)
);
CREATE INDEX IF NOT EXISTS agent2_party_case_clue_lookup_idx
    ON agent2_party_case_clues (tenant_id, party_id, case_id, clue_type);

CREATE TABLE IF NOT EXISTS agent2_party_source_references (
    reference_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id varchar(128) NOT NULL,
    party_id uuid NOT NULL,
    source_type varchar(64) NOT NULL,
    source_id varchar(512) NOT NULL,
    source_field varchar(256) NOT NULL DEFAULT '',
    source_row varchar(128) NOT NULL DEFAULT '',
    source_value text NOT NULL DEFAULT '',
    content_origin varchar(64) NOT NULL,
    confirmation_status varchar(32) NOT NULL DEFAULT 'pending_confirmation',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent2_party_source_unique UNIQUE (tenant_id, party_id, source_type, source_id, source_field),
    CONSTRAINT agent2_party_source_party_fk FOREIGN KEY (tenant_id, party_id)
        REFERENCES agent2_party_entities (tenant_id, party_id)
);

CREATE TABLE IF NOT EXISTS agent2_party_merge_candidates (
    candidate_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id varchar(128) NOT NULL,
    left_party_id uuid NOT NULL,
    right_party_id uuid NOT NULL,
    match_basis jsonb NOT NULL DEFAULT '[]'::jsonb,
    match_score numeric(6,5) NOT NULL DEFAULT 0 CHECK (match_score BETWEEN 0 AND 1),
    status varchar(32) NOT NULL DEFAULT 'candidate' CHECK (status IN ('candidate', 'confirmed', 'rejected', 'expired')),
    reviewed_by varchar(128) NOT NULL DEFAULT '',
    reviewed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent2_party_merge_pair_unique UNIQUE (tenant_id, left_party_id, right_party_id),
    CONSTRAINT agent2_party_merge_left_fk FOREIGN KEY (tenant_id, left_party_id)
        REFERENCES agent2_party_entities (tenant_id, party_id),
    CONSTRAINT agent2_party_merge_right_fk FOREIGN KEY (tenant_id, right_party_id)
        REFERENCES agent2_party_entities (tenant_id, party_id),
    CHECK (left_party_id <> right_party_id)
);

CREATE TABLE IF NOT EXISTS agent2_party_conflicts (
    conflict_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id varchar(128) NOT NULL,
    party_id uuid NOT NULL,
    field_name varchar(128) NOT NULL,
    competing_values jsonb NOT NULL DEFAULT '[]'::jsonb,
    status varchar(32) NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved', 'dismissed')),
    resolution_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    resolved_by varchar(128) NOT NULL DEFAULT '',
    resolved_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent2_party_conflict_party_fk FOREIGN KEY (tenant_id, party_id)
        REFERENCES agent2_party_entities (tenant_id, party_id)
);
CREATE INDEX IF NOT EXISTS agent2_party_conflict_open_idx
    ON agent2_party_conflicts (tenant_id, status, created_at);

CREATE TABLE IF NOT EXISTS agent2_travel_intents (
    travel_intent_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id varchar(128) NOT NULL,
    company_id varchar(128) NOT NULL,
    department_id varchar(128) NOT NULL,
    team_id varchar(128) NOT NULL,
    user_id varchar(128) NOT NULL,
    destination_raw text NOT NULL,
    destination_normalized varchar(256) NOT NULL,
    city_code varchar(32) NOT NULL,
    province_code varchar(32) NOT NULL,
    start_at timestamptz NOT NULL,
    end_at timestamptz NOT NULL,
    time_precision varchar(32) NOT NULL,
    purpose_summary text NOT NULL DEFAULT '',
    related_case_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    related_matter_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    source_message_id varchar(256) NOT NULL,
    source_channel varchar(64) NOT NULL,
    status varchar(32) NOT NULL DEFAULT 'planned'
        CHECK (status IN ('proposed', 'planned', 'confirmed', 'changed', 'cancelled', 'completed')),
    confidence numeric(6,5) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    idempotency_key varchar(512) NOT NULL,
    version integer NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent2_travel_idempotency_key UNIQUE (tenant_id, idempotency_key),
    CONSTRAINT agent2_travel_tenant_intent_key UNIQUE (tenant_id, travel_intent_id),
    CHECK (end_at >= start_at)
);
CREATE INDEX IF NOT EXISTS agent2_travel_match_idx
    ON agent2_travel_intents (tenant_id, city_code, start_at, end_at, status);
ALTER TABLE agent2_travel_intents
    ADD COLUMN IF NOT EXISTS team_id varchar(128) NOT NULL DEFAULT '';
ALTER TABLE agent2_travel_intents
    ALTER COLUMN team_id DROP DEFAULT;

CREATE TABLE IF NOT EXISTS agent2_travel_collaboration_candidates (
    candidate_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id varchar(128) NOT NULL,
    company_id varchar(128) NOT NULL,
    department_id varchar(128) NOT NULL,
    team_id varchar(128) NOT NULL,
    travel_intent_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    participant_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    destination varchar(256) NOT NULL,
    overlap_start timestamptz NOT NULL,
    overlap_end timestamptz NOT NULL,
    match_reason text NOT NULL,
    match_score numeric(6,5) NOT NULL CHECK (match_score BETWEEN 0 AND 1),
    status varchar(32) NOT NULL DEFAULT 'candidate'
        CHECK (status IN ('candidate', 'notified', 'accepted_by_one', 'accepted', 'declined', 'expired', 'cancelled')),
    notification_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    responses_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    deduplication_key varchar(512) NOT NULL,
    version integer NOT NULL DEFAULT 1 CHECK (version > 0),
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent2_travel_candidate_dedup_key UNIQUE (tenant_id, deduplication_key),
    CONSTRAINT agent2_travel_candidate_tenant_key UNIQUE (tenant_id, candidate_id),
    CHECK (overlap_end >= overlap_start)
);
ALTER TABLE agent2_travel_collaboration_candidates
    ADD COLUMN IF NOT EXISTS team_id varchar(128) NOT NULL DEFAULT '';
ALTER TABLE agent2_travel_collaboration_candidates
    ALTER COLUMN team_id DROP DEFAULT;
CREATE INDEX IF NOT EXISTS agent2_travel_candidate_status_idx
    ON agent2_travel_collaboration_candidates (tenant_id, status, expires_at);

CREATE TABLE IF NOT EXISTS agent2_notification_outbox (
    notification_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id varchar(128) NOT NULL,
    candidate_id uuid,
    recipient_user_id varchar(128) NOT NULL,
    channel varchar(64) NOT NULL DEFAULT 'dingtalk',
    message_type varchar(64) NOT NULL,
    message_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    idempotency_key varchar(512) NOT NULL,
    status varchar(32) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'sent', 'failed', 'dead_letter', 'cancelled')),
    retry_count integer NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    next_retry_at timestamptz,
    locked_by varchar(128) NOT NULL DEFAULT '',
    locked_at timestamptz,
    sent_at timestamptz,
    external_message_id varchar(256) NOT NULL DEFAULT '',
    response_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    dispatch_history_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    error_message text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent2_notification_idempotency_key UNIQUE (tenant_id, idempotency_key),
    CONSTRAINT agent2_notification_candidate_fk FOREIGN KEY (tenant_id, candidate_id)
        REFERENCES agent2_travel_collaboration_candidates (tenant_id, candidate_id)
);
CREATE INDEX IF NOT EXISTS agent2_notification_claim_idx
    ON agent2_notification_outbox (status, next_retry_at, created_at);

CREATE TABLE IF NOT EXISTS agent2_case_progress (
    progress_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id varchar(128) NOT NULL,
    case_id uuid NOT NULL,
    occurred_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL,
    reporter_id varchar(128) NOT NULL,
    progress_type varchar(64) NOT NULL,
    summary text NOT NULL,
    details text NOT NULL DEFAULT '',
    source_message_id varchar(256) NOT NULL,
    source_channel varchar(64) NOT NULL,
    content_origin varchar(32) NOT NULL
        CHECK (content_origin IN ('human_record', 'imported_record', 'ai_extracted', 'system_fact', 'robot_followup')),
    related_party_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    related_document_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    related_travel_intent_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    confidence numeric(6,5) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    confirmation_status varchar(64) NOT NULL,
    version integer NOT NULL DEFAULT 1 CHECK (version > 0),
    idempotency_key varchar(512) NOT NULL,
    deleted_at timestamptz,
    deleted_by varchar(128) NOT NULL DEFAULT '',
    delete_reason text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent2_case_progress_idempotency_key UNIQUE (tenant_id, idempotency_key),
    CONSTRAINT agent2_case_progress_tenant_key UNIQUE (tenant_id, progress_id),
    CONSTRAINT agent2_case_progress_case_fk FOREIGN KEY (tenant_id, case_id)
        REFERENCES agent2_cases (tenant_id, case_id)
);
CREATE INDEX IF NOT EXISTS agent2_case_progress_case_idx
    ON agent2_case_progress (tenant_id, case_id, occurred_at DESC)
    WHERE deleted_at IS NULL;

ALTER TABLE agent2_case_progress
    DROP CONSTRAINT IF EXISTS agent2_case_progress_content_origin_check;
ALTER TABLE agent2_case_progress
    ADD CONSTRAINT agent2_case_progress_content_origin_check
    CHECK (content_origin IN (
        'human_record',
        'imported_record',
        'ai_extracted',
        'system_fact',
        'robot_followup'
    ));

CREATE TABLE IF NOT EXISTS agent2_business_command_receipts (
    receipt_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id varchar(128) NOT NULL,
    command_id varchar(256) NOT NULL,
    command_type varchar(128) NOT NULL,
    actor_user_id varchar(128) NOT NULL,
    source_message_id varchar(256) NOT NULL,
    idempotency_key varchar(512) NOT NULL,
    status varchar(32) NOT NULL,
    resource_type varchar(128) NOT NULL DEFAULT '',
    resource_id varchar(256) NOT NULL DEFAULT '',
    before_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    after_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    error_code varchar(128) NOT NULL DEFAULT '',
    failed_stage varchar(128) NOT NULL DEFAULT '',
    actual_write boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent2_business_receipt_idempotency_key UNIQUE (tenant_id, idempotency_key),
    CONSTRAINT agent2_business_receipt_tenant_key UNIQUE (tenant_id, receipt_id)
);
CREATE INDEX IF NOT EXISTS agent2_business_receipt_source_idx
    ON agent2_business_command_receipts (tenant_id, source_message_id);

CREATE TABLE IF NOT EXISTS agent2_business_audit_events (
    audit_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id varchar(128) NOT NULL,
    receipt_id uuid NOT NULL,
    actor_user_id varchar(128) NOT NULL,
    source_message_id varchar(256) NOT NULL,
    source_channel varchar(64) NOT NULL,
    command_type varchar(128) NOT NULL,
    resource_type varchar(128) NOT NULL,
    resource_id varchar(256) NOT NULL,
    before_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    after_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent2_business_audit_receipt_fk FOREIGN KEY (tenant_id, receipt_id)
        REFERENCES agent2_business_command_receipts (tenant_id, receipt_id)
);
CREATE INDEX IF NOT EXISTS agent2_business_audit_resource_idx
    ON agent2_business_audit_events (tenant_id, resource_type, resource_id, created_at);

CREATE TABLE IF NOT EXISTS agent2_tenant_route_controls (
    control_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id varchar(128) NOT NULL,
    route_mode varchar(32) NOT NULL DEFAULT 'agent1'
        CHECK (route_mode IN ('agent1', 'agent2_shadow', 'agent2_canary', 'agent2_primary')),
    canary_user_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    agent1_rollback_enabled boolean NOT NULL DEFAULT false,
    version integer NOT NULL DEFAULT 1 CHECK (version > 0),
    changed_by varchar(128) NOT NULL,
    change_reason text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent2_route_control_tenant_key UNIQUE (tenant_id)
);

CREATE TABLE IF NOT EXISTS agent2_route_control_audits (
    audit_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id varchar(128) NOT NULL,
    actor_user_id varchar(128) NOT NULL,
    source_message_id varchar(256) NOT NULL,
    before_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    after_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    reason text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agent2_route_control_audit_tenant_idx
    ON agent2_route_control_audits (tenant_id, created_at DESC);

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

CREATE TABLE IF NOT EXISTS agent2_case_travel_clarification_pendings (
    pending_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id varchar(128) NOT NULL,
    user_id varchar(128) NOT NULL,
    conversation_id varchar(256) NOT NULL,
    case_id uuid NOT NULL,
    case_version integer NOT NULL CHECK (case_version >= 1),
    case_name text NOT NULL,
    source_message_id varchar(256) NOT NULL,
    raw_text text NOT NULL,
    travel_date date NOT NULL,
    purpose_summary text NOT NULL,
    suggested_destination text NOT NULL DEFAULT '',
    candidate_destination text NOT NULL DEFAULT '',
    status varchar(32) NOT NULL
        CHECK (status IN (
            'awaiting_confirmation', 'awaiting_destination', 'awaiting_city',
            'consumed', 'cancelled', 'expired', 'conflicted'
        )),
    last_reply_message_id varchar(256) NOT NULL DEFAULT '',
    receipt_id uuid,
    idempotency_key varchar(512) NOT NULL,
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz,
    cancelled_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent2_case_travel_pending_idempotency_key
        UNIQUE (tenant_id, idempotency_key),
    CONSTRAINT agent2_case_travel_pending_case_fk
        FOREIGN KEY (tenant_id, case_id)
        REFERENCES agent2_cases (tenant_id, case_id)
);
CREATE INDEX IF NOT EXISTS agent2_case_travel_pending_scope_idx
    ON agent2_case_travel_clarification_pendings
       (tenant_id, user_id, conversation_id, status, expires_at);

COMMIT;
