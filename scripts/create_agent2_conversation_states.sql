CREATE TABLE IF NOT EXISTS agent2_conversation_states (
    id uuid PRIMARY KEY,
    user_key varchar(128) NOT NULL,
    conversation_id varchar(256) NOT NULL,
    version integer NOT NULL DEFAULT 0,
    state_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_message_id varchar(256) NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent2_conversation_states_user_conversation_key
        UNIQUE (user_key, conversation_id),
    CONSTRAINT agent2_conversation_states_version_nonnegative
        CHECK (version >= 0)
);

CREATE INDEX IF NOT EXISTS agent2_conversation_states_updated_idx
    ON agent2_conversation_states (updated_at DESC);
