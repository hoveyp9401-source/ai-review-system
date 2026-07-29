BEGIN;

CREATE TABLE IF NOT EXISTS agent2_personal_memories (
    memory_id uuid PRIMARY KEY,
    tenant_id varchar(128) NOT NULL,
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    memory_type varchar(32) NOT NULL,
    memory_key varchar(128) NOT NULL,
    value_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_kind varchar(32) NOT NULL,
    source_message_id varchar(512),
    status varchar(32) NOT NULL DEFAULT 'active',
    version integer NOT NULL DEFAULT 1,
    expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent2_personal_memory_type_check
        CHECK (
            memory_type IN (
                'response_preference',
                'saved_view',
                'terminology_alias'
            )
        ),
    CONSTRAINT agent2_personal_memory_source_check
        CHECK (source_kind IN ('explicit_user', 'server_verified')),
    CONSTRAINT agent2_personal_memory_status_check
        CHECK (status IN ('active', 'superseded', 'forgotten')),
    CONSTRAINT agent2_personal_memory_version_check
        CHECK (version >= 1)
);

CREATE INDEX IF NOT EXISTS agent2_personal_memory_scope_idx
    ON agent2_personal_memories (
        tenant_id,
        user_id,
        status,
        updated_at
    );

CREATE UNIQUE INDEX IF NOT EXISTS
    agent2_personal_memory_one_active_key_idx
    ON agent2_personal_memories (
        tenant_id,
        user_id,
        memory_key
    )
    WHERE status = 'active';

COMMIT;
