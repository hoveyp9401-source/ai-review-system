BEGIN;

CREATE TABLE IF NOT EXISTS agent2_semantic_admission_traces (
    trace_id uuid PRIMARY KEY,
    tenant_id varchar(128) NOT NULL CHECK (btrim(tenant_id) <> ''),
    user_id varchar(128) NOT NULL CHECK (btrim(user_id) <> ''),
    conversation_id varchar(256) NOT NULL CHECK (btrim(conversation_id) <> ''),
    source_turn_id varchar(256) NOT NULL CHECK (btrim(source_turn_id) <> ''),
    source_message_id varchar(256) NOT NULL CHECK (btrim(source_message_id) <> ''),
    expected_conversation_state_version integer NOT NULL
        CHECK (expected_conversation_state_version >= 0),
    proposal_sha256 char(64) NOT NULL CHECK (proposal_sha256 ~ '^[0-9a-f]{64}$'),
    contract_version varchar(128) NOT NULL DEFAULT 'agent2.domain_admission.v1'
        CHECK (contract_version = 'agent2.domain_admission.v1'),
    policy_version varchar(128) NOT NULL CHECK (btrim(policy_version) <> ''),
    admission_mode varchar(16) NOT NULL
        CONSTRAINT agent2_semantic_admission_trace_mode_check
        CHECK (admission_mode IN ('shadow','enforced')),
    trace_status varchar(32) NOT NULL DEFAULT 'evaluated'
        CHECK (trace_status IN ('evaluated','failed')),
    admission_summary varchar(32) NOT NULL
        CHECK (admission_summary IN (
            'admitted',
            'partially_admitted',
            'blocked',
            'no_op',
            'information_required',
            'review_only',
            'deferred_audit_only'
        )),
    failure_reason varchar(2048) NOT NULL DEFAULT '',
    idempotency_key varchar(512) NOT NULL CHECK (btrim(idempotency_key) <> ''),
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent2_semantic_admission_trace_uuid_v5_check
        CHECK (
            substring(trace_id::text from 15 for 1) = '5'
            AND lower(substring(trace_id::text from 20 for 1)) IN ('8','9','a','b')
        ),
    CONSTRAINT agent2_semantic_admission_trace_scope_key
        UNIQUE (tenant_id, trace_id),
    CONSTRAINT agent2_semantic_admission_trace_idempotency_key
        UNIQUE (tenant_id, idempotency_key)
);
ALTER TABLE agent2_semantic_admission_traces
    ADD COLUMN IF NOT EXISTS admission_mode varchar(16);
UPDATE agent2_semantic_admission_traces
    SET admission_mode = 'shadow'
    WHERE admission_mode IS NULL;
ALTER TABLE agent2_semantic_admission_traces
    ALTER COLUMN admission_mode SET NOT NULL;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'agent2_semantic_admission_traces'::regclass
          AND conname = 'agent2_semantic_admission_trace_mode_check'
    ) THEN
        ALTER TABLE agent2_semantic_admission_traces
            ADD CONSTRAINT agent2_semantic_admission_trace_mode_check
            CHECK (admission_mode IN ('shadow','enforced'));
    END IF;
END
$$;
CREATE INDEX IF NOT EXISTS agent2_semantic_admission_trace_scope_idx
    ON agent2_semantic_admission_traces
    (tenant_id, user_id, conversation_id, created_at DESC);
CREATE INDEX IF NOT EXISTS agent2_semantic_admission_trace_source_idx
    ON agent2_semantic_admission_traces
    (tenant_id, user_id, conversation_id, source_message_id);

CREATE TABLE IF NOT EXISTS agent2_semantic_admission_decisions (
    decision_id uuid PRIMARY KEY,
    trace_id uuid NOT NULL,
    tenant_id varchar(128) NOT NULL CHECK (btrim(tenant_id) <> ''),
    user_id varchar(128) NOT NULL CHECK (btrim(user_id) <> ''),
    conversation_id varchar(256) NOT NULL CHECK (btrim(conversation_id) <> ''),
    source_turn_id varchar(256) NOT NULL CHECK (btrim(source_turn_id) <> ''),
    source_message_id varchar(256) NOT NULL CHECK (btrim(source_message_id) <> ''),
    action_id varchar(256) NOT NULL CHECK (btrim(action_id) <> ''),
    segment_id varchar(256) NOT NULL CHECK (btrim(segment_id) <> ''),
    segment_text_sha256 char(64) NOT NULL
        CHECK (segment_text_sha256 ~ '^[0-9a-f]{64}$'),
    segment_start_offset integer NOT NULL CHECK (segment_start_offset >= 0),
    segment_end_offset integer NOT NULL CHECK (segment_end_offset >= segment_start_offset),
    domain varchar(32) NOT NULL
        CHECK (domain IN ('report','case','travel','knowledge','chat','runtime')),
    operation varchar(128) NOT NULL CHECK (btrim(operation) <> ''),
    object_type varchar(128),
    object_stable_id varchar(512),
    object_version integer CHECK (object_version IS NULL OR object_version >= 0),
    object_label varchar(512),
    expected_conversation_state_version integer NOT NULL
        CHECK (expected_conversation_state_version >= 0),
    verdict varchar(32) NOT NULL
        CHECK (verdict IN (
            'admitted',
            'blocked',
            'no_op',
            'information_required',
            'review_only',
            'deferred_audit_only'
        )),
    reason_code varchar(128) NOT NULL CHECK (btrim(reason_code) <> ''),
    evidence_refs_json jsonb NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(evidence_refs_json) = 'array'),
    ticket_id uuid,
    pending_id uuid,
    idempotency_key varchar(512) NOT NULL CHECK (btrim(idempotency_key) <> ''),
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent2_semantic_admission_decision_uuid_v5_check
        CHECK (
            substring(decision_id::text from 15 for 1) = '5'
            AND lower(substring(decision_id::text from 20 for 1)) IN ('8','9','a','b')
        ),
    CONSTRAINT agent2_semantic_admission_decision_scope_key
        UNIQUE (tenant_id, decision_id),
    CONSTRAINT agent2_semantic_admission_decision_idempotency_key
        UNIQUE (tenant_id, idempotency_key),
    CONSTRAINT agent2_semantic_admission_decision_trace_fk
        FOREIGN KEY (tenant_id, trace_id)
        REFERENCES agent2_semantic_admission_traces (tenant_id, trace_id),
    CONSTRAINT agent2_semantic_admission_decision_object_check
        CHECK (
            (
                object_type IS NULL
                AND object_stable_id IS NULL
                AND object_version IS NULL
                AND object_label IS NULL
            )
            OR (
                object_type IS NOT NULL
                AND btrim(object_type) <> ''
                AND object_stable_id IS NOT NULL
                AND btrim(object_stable_id) <> ''
            )
        ),
    CONSTRAINT agent2_semantic_admission_decision_artifact_check
        CHECK (
            (
                verdict = 'admitted'
                AND operation IN (
                    'query_daily_report',
                    'query_periodic_report',
                    'answer_case_query',
                    'query_case_progress',
                    'query_operation_status',
                    'search_enterprise_knowledge'
                )
                AND ticket_id IS NULL
                AND pending_id IS NULL
            )
            OR (
                verdict = 'admitted'
                AND operation NOT IN (
                    'query_daily_report',
                    'query_periodic_report',
                    'answer_case_query',
                    'query_case_progress',
                    'query_operation_status',
                    'search_enterprise_knowledge'
                )
                AND object_type IS NOT NULL
                AND object_stable_id IS NOT NULL
                AND ticket_id IS NOT NULL
                AND pending_id IS NULL
            )
            OR (
                verdict = 'information_required'
                AND ticket_id IS NULL
                AND pending_id IS NOT NULL
            )
            OR (
                verdict IN ('blocked','no_op','review_only','deferred_audit_only')
                AND ticket_id IS NULL
                AND pending_id IS NULL
            )
        )
);
ALTER TABLE agent2_semantic_admission_decisions
    DROP CONSTRAINT IF EXISTS agent2_semantic_admission_decision_artifact_check;
ALTER TABLE agent2_semantic_admission_decisions
    ADD CONSTRAINT agent2_semantic_admission_decision_artifact_check
    CHECK (
        (
            verdict = 'admitted'
            AND operation IN (
                'query_daily_report',
                'query_periodic_report',
                'answer_case_query',
                'query_case_progress',
                'query_operation_status',
                'search_enterprise_knowledge'
            )
            AND ticket_id IS NULL
            AND pending_id IS NULL
        )
        OR (
            verdict = 'admitted'
            AND operation NOT IN (
                'query_daily_report',
                'query_periodic_report',
                'answer_case_query',
                'query_case_progress',
                'query_operation_status',
                'search_enterprise_knowledge'
            )
            AND object_type IS NOT NULL
            AND object_stable_id IS NOT NULL
            AND ticket_id IS NOT NULL
            AND pending_id IS NULL
        )
        OR (
            verdict = 'information_required'
            AND ticket_id IS NULL
            AND pending_id IS NOT NULL
        )
        OR (
            verdict IN ('blocked','no_op','review_only','deferred_audit_only')
            AND ticket_id IS NULL
            AND pending_id IS NULL
        )
    );
CREATE INDEX IF NOT EXISTS agent2_semantic_admission_decision_scope_idx
    ON agent2_semantic_admission_decisions
    (tenant_id, user_id, conversation_id, verdict, created_at DESC);
CREATE INDEX IF NOT EXISTS agent2_semantic_admission_decision_trace_idx
    ON agent2_semantic_admission_decisions
    (tenant_id, trace_id, segment_id, action_id);

CREATE TABLE IF NOT EXISTS agent2_semantic_admission_tickets (
    ticket_id uuid PRIMARY KEY,
    trace_id uuid NOT NULL,
    decision_id uuid NOT NULL,
    tenant_id varchar(128) NOT NULL CHECK (btrim(tenant_id) <> ''),
    user_id varchar(128) NOT NULL CHECK (btrim(user_id) <> ''),
    conversation_id varchar(256) NOT NULL CHECK (btrim(conversation_id) <> ''),
    source_turn_id varchar(256) NOT NULL CHECK (btrim(source_turn_id) <> ''),
    source_message_id varchar(256) NOT NULL CHECK (btrim(source_message_id) <> ''),
    action_id varchar(256) NOT NULL CHECK (btrim(action_id) <> ''),
    segment_id varchar(256) NOT NULL CHECK (btrim(segment_id) <> ''),
    segment_text_sha256 char(64) NOT NULL
        CHECK (segment_text_sha256 ~ '^[0-9a-f]{64}$'),
    segment_start_offset integer NOT NULL CHECK (segment_start_offset >= 0),
    segment_end_offset integer NOT NULL CHECK (segment_end_offset >= segment_start_offset),
    domain varchar(32) NOT NULL
        CHECK (domain IN ('report','case','travel','knowledge','chat','runtime')),
    operation varchar(128) NOT NULL CHECK (btrim(operation) <> ''),
    object_type varchar(128) NOT NULL CHECK (btrim(object_type) <> ''),
    object_stable_id varchar(512) NOT NULL CHECK (btrim(object_stable_id) <> ''),
    object_version integer CHECK (object_version IS NULL OR object_version >= 0),
    object_label varchar(512),
    expected_conversation_state_version integer NOT NULL
        CHECK (expected_conversation_state_version >= 0),
    authority_scope_json jsonb NOT NULL
        CHECK (jsonb_typeof(authority_scope_json) = 'object'),
    allowed_changed_fields_json jsonb NOT NULL
        CHECK (jsonb_typeof(allowed_changed_fields_json) = 'array'),
    fact_claims_sha256 char(64) NOT NULL
        CHECK (fact_claims_sha256 ~ '^[0-9a-f]{64}$'),
    authorized_command_sha256 char(64) NOT NULL
        CHECK (authorized_command_sha256 ~ '^[0-9a-f]{64}$'),
    policy_version varchar(128) NOT NULL CHECK (btrim(policy_version) <> ''),
    ticket_status varchar(32) NOT NULL DEFAULT 'issued'
        CHECK (ticket_status IN (
            'issued',
            'consumed',
            'expired',
            'cancelled',
            'conflicted',
            'permission_revoked'
        )),
    contract_version varchar(128) NOT NULL DEFAULT 'agent2.domain_admission.v1'
        CHECK (contract_version = 'agent2.domain_admission.v1'),
    issued_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    ttl_seconds integer NOT NULL CHECK (ttl_seconds BETWEEN 1 AND 3600),
    executor_revalidation_required boolean NOT NULL DEFAULT true
        CHECK (executor_revalidation_required),
    proves_business_write boolean NOT NULL DEFAULT false
        CHECK (NOT proves_business_write),
    consumed_at timestamptz,
    consumed_receipt_ref varchar(512),
    invalidation_reason varchar(2048) NOT NULL DEFAULT '',
    idempotency_key varchar(512) NOT NULL CHECK (btrim(idempotency_key) <> ''),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent2_semantic_admission_ticket_uuid_v5_check
        CHECK (
            substring(ticket_id::text from 15 for 1) = '5'
            AND lower(substring(ticket_id::text from 20 for 1)) IN ('8','9','a','b')
        ),
    CONSTRAINT agent2_semantic_admission_ticket_scope_key
        UNIQUE (tenant_id, ticket_id),
    CONSTRAINT agent2_semantic_admission_ticket_decision_link_key
        UNIQUE (tenant_id, ticket_id, decision_id),
    CONSTRAINT agent2_semantic_admission_ticket_idempotency_key
        UNIQUE (tenant_id, idempotency_key),
    CONSTRAINT agent2_semantic_admission_ticket_trace_fk
        FOREIGN KEY (tenant_id, trace_id)
        REFERENCES agent2_semantic_admission_traces (tenant_id, trace_id),
    CONSTRAINT agent2_semantic_admission_ticket_decision_fk
        FOREIGN KEY (tenant_id, decision_id)
        REFERENCES agent2_semantic_admission_decisions (tenant_id, decision_id),
    CONSTRAINT agent2_semantic_admission_ticket_ttl_check
        CHECK (
            expires_at > issued_at
            AND expires_at = issued_at + (ttl_seconds * interval '1 second')
        ),
    CONSTRAINT agent2_semantic_admission_ticket_consumption_check
        CHECK (
            (
                ticket_status = 'consumed'
                AND consumed_at IS NOT NULL
                AND consumed_receipt_ref IS NOT NULL
                AND btrim(consumed_receipt_ref) <> ''
            )
            OR (
                ticket_status <> 'consumed'
                AND consumed_at IS NULL
                AND consumed_receipt_ref IS NULL
            )
        )
);
CREATE UNIQUE INDEX IF NOT EXISTS agent2_semantic_admission_ticket_decision_key
    ON agent2_semantic_admission_tickets (tenant_id, decision_id);
CREATE INDEX IF NOT EXISTS agent2_semantic_admission_ticket_scope_idx
    ON agent2_semantic_admission_tickets
    (tenant_id, user_id, conversation_id, ticket_status, expires_at);

CREATE TABLE IF NOT EXISTS agent2_semantic_review_items (
    review_id uuid PRIMARY KEY,
    trace_id uuid NOT NULL,
    decision_id uuid,
    tenant_id varchar(128) NOT NULL CHECK (btrim(tenant_id) <> ''),
    user_id varchar(128) NOT NULL CHECK (btrim(user_id) <> ''),
    conversation_id varchar(256) NOT NULL CHECK (btrim(conversation_id) <> ''),
    source_turn_id varchar(256) NOT NULL CHECK (btrim(source_turn_id) <> ''),
    source_message_id varchar(256) NOT NULL CHECK (btrim(source_message_id) <> ''),
    segment_id varchar(256) NOT NULL CHECK (btrim(segment_id) <> ''),
    segment_text_sha256 char(64) NOT NULL
        CHECK (segment_text_sha256 ~ '^[0-9a-f]{64}$'),
    segment_start_offset integer NOT NULL CHECK (segment_start_offset >= 0),
    segment_end_offset integer NOT NULL CHECK (segment_end_offset >= segment_start_offset),
    domain varchar(32) NOT NULL
        CHECK (domain IN ('report','case','travel','knowledge','chat','runtime')),
    operation varchar(128) NOT NULL CHECK (btrim(operation) <> ''),
    object_type varchar(128),
    object_stable_id varchar(512),
    object_version integer CHECK (object_version IS NULL OR object_version >= 0),
    object_label varchar(512),
    reason_code varchar(128) NOT NULL CHECK (btrim(reason_code) <> ''),
    review_status varchar(32) NOT NULL DEFAULT 'pending_human_review'
        CHECK (review_status IN ('pending_human_review','resolved','dismissed','expired')),
    candidate_snapshot_json jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(candidate_snapshot_json) = 'object'),
    resolution_json jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(resolution_json) = 'object'),
    reviewed_by varchar(128) NOT NULL DEFAULT '',
    audit_only boolean NOT NULL DEFAULT true CHECK (audit_only),
    business_write_allowed boolean NOT NULL DEFAULT false CHECK (NOT business_write_allowed),
    idempotency_key varchar(512) NOT NULL CHECK (btrim(idempotency_key) <> ''),
    created_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    CONSTRAINT agent2_semantic_review_uuid_v5_check
        CHECK (
            substring(review_id::text from 15 for 1) = '5'
            AND lower(substring(review_id::text from 20 for 1)) IN ('8','9','a','b')
        ),
    CONSTRAINT agent2_semantic_review_object_check
        CHECK (
            (
                object_type IS NULL
                AND object_stable_id IS NULL
                AND object_version IS NULL
                AND object_label IS NULL
            )
            OR (
                object_type IS NOT NULL
                AND btrim(object_type) <> ''
                AND object_stable_id IS NOT NULL
                AND btrim(object_stable_id) <> ''
            )
        ),
    CONSTRAINT agent2_semantic_review_scope_key UNIQUE (tenant_id, review_id),
    CONSTRAINT agent2_semantic_review_idempotency_key UNIQUE (tenant_id, idempotency_key),
    CONSTRAINT agent2_semantic_review_trace_fk
        FOREIGN KEY (tenant_id, trace_id)
        REFERENCES agent2_semantic_admission_traces (tenant_id, trace_id),
    CONSTRAINT agent2_semantic_review_decision_fk
        FOREIGN KEY (tenant_id, decision_id)
        REFERENCES agent2_semantic_admission_decisions (tenant_id, decision_id)
);
CREATE INDEX IF NOT EXISTS agent2_semantic_review_scope_idx
    ON agent2_semantic_review_items
    (tenant_id, user_id, conversation_id, review_status, created_at DESC);

CREATE TABLE IF NOT EXISTS agent2_deferred_semantic_events (
    deferred_event_id uuid PRIMARY KEY,
    trace_id uuid NOT NULL,
    decision_id uuid,
    tenant_id varchar(128) NOT NULL CHECK (btrim(tenant_id) <> ''),
    user_id varchar(128) NOT NULL CHECK (btrim(user_id) <> ''),
    conversation_id varchar(256) NOT NULL CHECK (btrim(conversation_id) <> ''),
    source_turn_id varchar(256) NOT NULL CHECK (btrim(source_turn_id) <> ''),
    source_message_id varchar(256) NOT NULL CHECK (btrim(source_message_id) <> ''),
    segment_id varchar(256) NOT NULL CHECK (btrim(segment_id) <> ''),
    segment_text_sha256 char(64) NOT NULL
        CHECK (segment_text_sha256 ~ '^[0-9a-f]{64}$'),
    segment_start_offset integer NOT NULL CHECK (segment_start_offset >= 0),
    segment_end_offset integer NOT NULL CHECK (segment_end_offset >= segment_start_offset),
    domain varchar(32) NOT NULL
        CHECK (domain IN ('report','case','travel','knowledge','chat','runtime')),
    operation varchar(128) NOT NULL CHECK (btrim(operation) <> ''),
    object_type varchar(128),
    object_stable_id varchar(512),
    object_version integer CHECK (object_version IS NULL OR object_version >= 0),
    object_label varchar(512),
    reason_code varchar(128) NOT NULL CHECK (btrim(reason_code) <> ''),
    event_status varchar(32) NOT NULL DEFAULT 'recorded'
        CHECK (event_status IN ('recorded','superseded','reviewed','cancelled','expired')),
    payload_json jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(payload_json) = 'object'),
    not_before timestamptz,
    expires_at timestamptz,
    audit_only boolean NOT NULL DEFAULT true CHECK (audit_only),
    business_write_allowed boolean NOT NULL DEFAULT false CHECK (NOT business_write_allowed),
    requires_fresh_admission boolean NOT NULL DEFAULT true CHECK (requires_fresh_admission),
    idempotency_key varchar(512) NOT NULL CHECK (btrim(idempotency_key) <> ''),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent2_deferred_semantic_event_uuid_v5_check
        CHECK (
            substring(deferred_event_id::text from 15 for 1) = '5'
            AND lower(substring(deferred_event_id::text from 20 for 1)) IN ('8','9','a','b')
        ),
    CONSTRAINT agent2_deferred_semantic_event_object_check
        CHECK (
            (
                object_type IS NULL
                AND object_stable_id IS NULL
                AND object_version IS NULL
                AND object_label IS NULL
            )
            OR (
                object_type IS NOT NULL
                AND btrim(object_type) <> ''
                AND object_stable_id IS NOT NULL
                AND btrim(object_stable_id) <> ''
            )
        ),
    CONSTRAINT agent2_deferred_semantic_event_scope_key
        UNIQUE (tenant_id, deferred_event_id),
    CONSTRAINT agent2_deferred_semantic_event_idempotency_key
        UNIQUE (tenant_id, idempotency_key),
    CONSTRAINT agent2_deferred_semantic_event_trace_fk
        FOREIGN KEY (tenant_id, trace_id)
        REFERENCES agent2_semantic_admission_traces (tenant_id, trace_id),
    CONSTRAINT agent2_deferred_semantic_event_decision_fk
        FOREIGN KEY (tenant_id, decision_id)
        REFERENCES agent2_semantic_admission_decisions (tenant_id, decision_id),
    CONSTRAINT agent2_deferred_semantic_event_window_check
        CHECK (expires_at IS NULL OR not_before IS NULL OR expires_at > not_before)
);
CREATE INDEX IF NOT EXISTS agent2_deferred_semantic_event_scope_idx
    ON agent2_deferred_semantic_events
    (tenant_id, user_id, conversation_id, event_status, created_at DESC);

CREATE TABLE IF NOT EXISTS agent2_information_pendings (
    pending_id uuid PRIMARY KEY,
    pending_type varchar(32) NOT NULL DEFAULT 'information'
        CHECK (pending_type = 'information'),
    trace_id uuid NOT NULL,
    decision_id uuid NOT NULL,
    tenant_id varchar(128) NOT NULL CHECK (btrim(tenant_id) <> ''),
    user_id varchar(128) NOT NULL CHECK (btrim(user_id) <> ''),
    conversation_id varchar(256) NOT NULL CHECK (btrim(conversation_id) <> ''),
    source_turn_id varchar(256) NOT NULL CHECK (btrim(source_turn_id) <> ''),
    source_message_id varchar(256) NOT NULL CHECK (btrim(source_message_id) <> ''),
    segment_id varchar(256) NOT NULL CHECK (btrim(segment_id) <> ''),
    segment_text_sha256 char(64) NOT NULL
        CHECK (segment_text_sha256 ~ '^[0-9a-f]{64}$'),
    segment_start_offset integer NOT NULL CHECK (segment_start_offset >= 0),
    segment_end_offset integer NOT NULL CHECK (segment_end_offset >= segment_start_offset),
    domain varchar(32) NOT NULL
        CHECK (domain IN ('report','case','travel','knowledge','chat','runtime')),
    operation varchar(128) NOT NULL CHECK (btrim(operation) <> ''),
    object_type varchar(128),
    object_stable_id varchar(512),
    object_version integer CHECK (object_version IS NULL OR object_version >= 0),
    object_label varchar(512),
    expected_conversation_state_version integer NOT NULL
        CHECK (expected_conversation_state_version >= 0),
    missing_fields_json jsonb NOT NULL
        CHECK (
            jsonb_typeof(missing_fields_json) = 'array'
            AND jsonb_array_length(missing_fields_json) > 0
        ),
    question_snapshot_json jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(question_snapshot_json) = 'object'),
    acceptable_answer_forms_json jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(acceptable_answer_forms_json) = 'object'),
    pending_status varchar(32) NOT NULL DEFAULT 'active'
        CHECK (pending_status IN (
            'active',
            'awaiting_input',
            'consumed',
            'expired',
            'cancelled',
            'conflicted',
            'permission_revoked'
        )),
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    ttl_seconds integer NOT NULL CHECK (ttl_seconds BETWEEN 1 AND 604800),
    consumed_at timestamptz,
    consumed_by_trace_id uuid,
    invalidation_reason varchar(2048) NOT NULL DEFAULT '',
    business_write_allowed boolean NOT NULL DEFAULT false CHECK (NOT business_write_allowed),
    idempotency_key varchar(512) NOT NULL CHECK (btrim(idempotency_key) <> ''),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent2_information_pending_uuid_v5_check
        CHECK (
            substring(pending_id::text from 15 for 1) = '5'
            AND lower(substring(pending_id::text from 20 for 1)) IN ('8','9','a','b')
        ),
    CONSTRAINT agent2_information_pending_object_check
        CHECK (
            (
                object_type IS NULL
                AND object_stable_id IS NULL
                AND object_version IS NULL
                AND object_label IS NULL
            )
            OR (
                object_type IS NOT NULL
                AND btrim(object_type) <> ''
                AND object_stable_id IS NOT NULL
                AND btrim(object_stable_id) <> ''
            )
        ),
    CONSTRAINT agent2_information_pending_scope_key UNIQUE (tenant_id, pending_id),
    CONSTRAINT agent2_information_pending_decision_link_key
        UNIQUE (tenant_id, pending_id, decision_id),
    CONSTRAINT agent2_information_pending_idempotency_key UNIQUE (tenant_id, idempotency_key),
    CONSTRAINT agent2_information_pending_trace_fk
        FOREIGN KEY (tenant_id, trace_id)
        REFERENCES agent2_semantic_admission_traces (tenant_id, trace_id),
    CONSTRAINT agent2_information_pending_decision_fk
        FOREIGN KEY (tenant_id, decision_id)
        REFERENCES agent2_semantic_admission_decisions (tenant_id, decision_id),
    CONSTRAINT agent2_information_pending_consumed_trace_fk
        FOREIGN KEY (tenant_id, consumed_by_trace_id)
        REFERENCES agent2_semantic_admission_traces (tenant_id, trace_id),
    CONSTRAINT agent2_information_pending_ttl_check
        CHECK (
            expires_at > created_at
            AND expires_at = created_at + (ttl_seconds * interval '1 second')
        ),
    CONSTRAINT agent2_information_pending_consumption_check
        CHECK (
            (
                pending_status = 'consumed'
                AND consumed_at IS NOT NULL
                AND consumed_by_trace_id IS NOT NULL
            )
            OR (
                pending_status <> 'consumed'
                AND consumed_at IS NULL
                AND consumed_by_trace_id IS NULL
            )
        )
);
CREATE UNIQUE INDEX IF NOT EXISTS agent2_information_pending_decision_key
    ON agent2_information_pendings (tenant_id, decision_id);
CREATE INDEX IF NOT EXISTS agent2_information_pending_scope_idx
    ON agent2_information_pendings
    (tenant_id, user_id, conversation_id, pending_status, expires_at);
CREATE UNIQUE INDEX IF NOT EXISTS agent2_information_pending_one_active_action
    ON agent2_information_pendings (
        tenant_id,
        user_id,
        conversation_id,
        domain,
        operation,
        COALESCE(object_type, ''),
        COALESCE(object_stable_id, '')
    )
    WHERE pending_status IN ('active','awaiting_input');

-- Decision-to-artifact references are deferred because Decision and its Ticket or
-- Information Pending are created in the same transaction and reference each other.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'agent2_semantic_admission_decisions'::regclass
          AND conname = 'agent2_semantic_admission_decision_ticket_fk'
    ) THEN
        ALTER TABLE agent2_semantic_admission_decisions
            ADD CONSTRAINT agent2_semantic_admission_decision_ticket_fk
            FOREIGN KEY (tenant_id, ticket_id, decision_id)
            REFERENCES agent2_semantic_admission_tickets
                (tenant_id, ticket_id, decision_id)
            DEFERRABLE INITIALLY DEFERRED;
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'agent2_semantic_admission_decisions'::regclass
          AND conname = 'agent2_semantic_admission_decision_pending_fk'
    ) THEN
        ALTER TABLE agent2_semantic_admission_decisions
            ADD CONSTRAINT agent2_semantic_admission_decision_pending_fk
            FOREIGN KEY (tenant_id, pending_id, decision_id)
            REFERENCES agent2_information_pendings
                (tenant_id, pending_id, decision_id)
            DEFERRABLE INITIALLY DEFERRED;
    END IF;
END
$$;

COMMENT ON TABLE agent2_semantic_admission_traces IS
    'Digest-only semantic trace. Raw proposal text is prohibited.';
COMMENT ON TABLE agent2_semantic_admission_tickets IS
    'Short-lived authorization to attempt one exact operation; Executor revalidation and committed receipt remain mandatory.';
COMMENT ON TABLE agent2_semantic_review_items IS
    'Audit-only semantic review artifact; never authorizes a business write.';
COMMENT ON TABLE agent2_deferred_semantic_events IS
    'Audit-only deferred signal; later action requires a fresh admission cycle.';
COMMENT ON TABLE agent2_information_pendings IS
    'Scoped missing-information protocol; the Pending itself never authorizes a business write.';

COMMIT;
