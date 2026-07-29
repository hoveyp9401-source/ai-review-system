BEGIN;

-- The existing users, teams, and daily_reports tables are owned by the
-- original report service. Dashboard references remain plain UUID values
-- so this additive schema needs no new privileges or cascade behavior on
-- those production tables.

-- Server-side dashboard authorization. Credentials only identify the actor;
-- this table decides the effective role and team scope for the target date.
CREATE TABLE IF NOT EXISTS legal_daily_access_assignments (
    assignment_id uuid PRIMARY KEY,
    tenant_id varchar(128) NOT NULL,
    principal_user_id varchar(128) NOT NULL,
    dashboard_role varchar(32) NOT NULL,
    team_id uuid NULL,
    effective_from date NOT NULL,
    effective_to date NULL,
    active boolean NOT NULL DEFAULT true,
    source varchar(128) NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT NOW(),
    updated_at timestamptz NOT NULL DEFAULT NOW(),
    CONSTRAINT legal_daily_access_role_check CHECK (dashboard_role IN ('team_lead', 'legal_head')),
    CONSTRAINT legal_daily_access_team_check CHECK (
        (dashboard_role = 'legal_head' AND team_id IS NULL)
        OR (dashboard_role = 'team_lead' AND team_id IS NOT NULL)
    ),
    CONSTRAINT legal_daily_access_dates_check CHECK (
        effective_to IS NULL OR effective_to >= effective_from
    ),
    CONSTRAINT legal_daily_access_unique UNIQUE (
        tenant_id,
        principal_user_id,
        dashboard_role,
        team_id,
        effective_from
    )
);

CREATE INDEX IF NOT EXISTS legal_daily_access_effective_idx
    ON legal_daily_access_assignments (
        tenant_id,
        principal_user_id,
        active,
        effective_from,
        effective_to
    );

-- Effective-dated membership for historical team queries.
CREATE TABLE IF NOT EXISTS legal_daily_team_memberships (
    membership_id uuid PRIMARY KEY,
    tenant_id varchar(128) NOT NULL,
    user_id uuid NOT NULL,
    team_id uuid NOT NULL,
    member_role varchar(64) NOT NULL DEFAULT 'member',
    effective_from date NOT NULL,
    effective_to date NULL,
    source varchar(128) NOT NULL DEFAULT '',
    data_complete boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT NOW(),
    updated_at timestamptz NOT NULL DEFAULT NOW(),
    CONSTRAINT legal_daily_membership_dates_check CHECK (
        effective_to IS NULL OR effective_to >= effective_from
    ),
    CONSTRAINT legal_daily_membership_unique UNIQUE (
        tenant_id,
        user_id,
        team_id,
        effective_from
    )
);

CREATE INDEX IF NOT EXISTS legal_daily_membership_effective_idx
    ON legal_daily_team_memberships (
        tenant_id,
        team_id,
        effective_from,
        effective_to
    );

-- One explicit responsibility fact per person and date. Missing rows remain
-- unknown; the dashboard must not infer leave, employment, or exemption.
CREATE TABLE IF NOT EXISTS legal_daily_submission_obligations (
    obligation_id uuid PRIMARY KEY,
    tenant_id varchar(128) NOT NULL,
    user_id uuid NOT NULL,
    team_id uuid NOT NULL,
    report_date date NOT NULL,
    required boolean NOT NULL,
    exemption_reason varchar(512) NOT NULL DEFAULT '',
    deadline_at timestamptz NULL,
    source varchar(128) NOT NULL DEFAULT '',
    data_complete boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT NOW(),
    updated_at timestamptz NOT NULL DEFAULT NOW(),
    CONSTRAINT legal_daily_obligation_unique UNIQUE (
        tenant_id,
        user_id,
        report_date
    )
);

CREATE INDEX IF NOT EXISTS legal_daily_obligation_team_date_idx
    ON legal_daily_submission_obligations (
        tenant_id,
        team_id,
        report_date
    );

-- Model output is stored separately from the employee's report.
CREATE TABLE IF NOT EXISTS legal_daily_review_suggestions (
    suggestion_id uuid PRIMARY KEY,
    public_ref varchar(64) NOT NULL,
    tenant_id varchar(128) NOT NULL,
    report_id uuid NULL,
    user_id uuid NOT NULL,
    team_id uuid NOT NULL,
    report_date date NOT NULL,
    reason_type varchar(64) NOT NULL,
    reason text NOT NULL,
    evidence_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    compared_dates_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    confidence numeric(5,4) NOT NULL,
    work_item_title varchar(512) NOT NULL DEFAULT '',
    support_needed text NOT NULL DEFAULT '',
    owner_level varchar(32) NOT NULL,
    model_version varchar(128) NOT NULL,
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT NOW(),
    updated_at timestamptz NOT NULL DEFAULT NOW(),
    CONSTRAINT legal_daily_suggestion_confidence_check CHECK (
        confidence >= 0 AND confidence <= 1
    ),
    CONSTRAINT legal_daily_suggestion_owner_check CHECK (
        owner_level IN ('team_lead', 'legal_head')
    ),
    CONSTRAINT legal_daily_suggestion_ref_unique UNIQUE (tenant_id, public_ref)
);

CREATE INDEX IF NOT EXISTS legal_daily_suggestion_team_date_idx
    ON legal_daily_review_suggestions (
        tenant_id,
        team_id,
        report_date,
        active
    );

-- Cross-date work items and their source timeline stay independent from the
-- original report rows.
CREATE TABLE IF NOT EXISTS legal_daily_work_items (
    item_id uuid PRIMARY KEY,
    public_ref varchar(64) NOT NULL,
    tenant_id varchar(128) NOT NULL,
    team_id uuid NOT NULL,
    member_refs_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    title varchar(512) NOT NULL,
    item_status varchar(64) NOT NULL,
    summary text NOT NULL DEFAULT '',
    first_seen date NOT NULL,
    last_seen date NOT NULL,
    entries_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    confidence numeric(5,4) NOT NULL,
    model_version varchar(128) NOT NULL,
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT NOW(),
    updated_at timestamptz NOT NULL DEFAULT NOW(),
    CONSTRAINT legal_daily_item_status_check CHECK (
        item_status IN (
            'normal_progress',
            'no_new_progress',
            'plan_delayed',
            'unresolved_problem',
            'disappeared_without_completion',
            'completed',
            'waiting_external',
            'normal_continuing'
        )
    ),
    CONSTRAINT legal_daily_item_dates_check CHECK (last_seen >= first_seen),
    CONSTRAINT legal_daily_item_confidence_check CHECK (
        confidence >= 0 AND confidence <= 1
    ),
    CONSTRAINT legal_daily_item_ref_unique UNIQUE (tenant_id, public_ref)
);

CREATE INDEX IF NOT EXISTS legal_daily_item_team_status_idx
    ON legal_daily_work_items (
        tenant_id,
        team_id,
        item_status,
        active,
        last_seen
    );

-- Append-only manager decisions. Idempotency prevents duplicate clicks.
CREATE TABLE IF NOT EXISTS legal_daily_manager_decisions (
    decision_id uuid PRIMARY KEY,
    tenant_id varchar(128) NOT NULL,
    target_type varchar(32) NOT NULL,
    target_ref varchar(64) NOT NULL,
    team_id uuid NOT NULL,
    decision varchar(32) NOT NULL,
    note text NOT NULL DEFAULT '',
    actor_user_id varchar(128) NOT NULL,
    actor_role varchar(32) NOT NULL,
    evidence_snapshot_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    idempotency_key varchar(256) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT NOW(),
    CONSTRAINT legal_daily_decision_target_check CHECK (
        target_type IN ('review_suggestion', 'work_item')
    ),
    CONSTRAINT legal_daily_decision_value_check CHECK (decision IN ('normal', 'waiting_external', 'followup', 'completed', 'system_error')),
    CONSTRAINT legal_daily_decision_actor_role_check CHECK (
        actor_role IN ('team_lead', 'legal_head')
    ),
    CONSTRAINT legal_daily_decision_idempotency_unique UNIQUE (
        tenant_id,
        idempotency_key
    )
);

CREATE INDEX IF NOT EXISTS legal_daily_decision_target_idx
    ON legal_daily_manager_decisions (
        tenant_id,
        target_type,
        target_ref,
        created_at DESC
    );

COMMIT;
