CREATE TABLE IF NOT EXISTS performance_tasks (
    id uuid PRIMARY KEY,
    title varchar(256) NOT NULL,
    period_label varchar(64) NOT NULL,
    metrics_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    status varchar(32) NOT NULL DEFAULT 'draft',
    created_by varchar(128) NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT performance_tasks_status_check CHECK (status IN ('draft', 'active', 'closed', 'cancelled'))
);

CREATE TABLE IF NOT EXISTS performance_submissions (
    id uuid PRIMARY KEY,
    task_id uuid NOT NULL REFERENCES performance_tasks(id) ON DELETE CASCADE,
    user_id uuid NOT NULL,
    team_id uuid NOT NULL,
    recipient_name varchar(128) NOT NULL DEFAULT '',
    sent_snapshot_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    responses_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    input_fragments_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    status varchar(32) NOT NULL DEFAULT 'collecting',
    confirmed_by_user boolean NOT NULL DEFAULT false,
    submitted_at timestamptz,
    last_prompted_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT performance_submissions_task_user_key UNIQUE (task_id, user_id),
    CONSTRAINT performance_submissions_status_check CHECK (status IN ('collecting', 'pending_confirmation', 'completed', 'cancelled'))
);

CREATE INDEX IF NOT EXISTS performance_submissions_user_status_idx
    ON performance_submissions(user_id, status);

CREATE INDEX IF NOT EXISTS performance_submissions_task_idx
    ON performance_submissions(task_id);
