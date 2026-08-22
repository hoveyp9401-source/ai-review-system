\set ON_ERROR_STOP on

BEGIN;
\ir create_agent2_personal_weekly_briefs.sql
DO $$
BEGIN
    IF to_regclass('public.agent2_personal_weekly_briefs') IS NULL THEN
        RAISE EXCEPTION 'weekly brief table was not created';
    END IF;
END
$$;
ROLLBACK;

DO $$
BEGIN
    IF to_regclass('public.agent2_personal_weekly_briefs') IS NOT NULL THEN
        RAISE EXCEPTION 'outer transaction did not remove weekly brief table';
    END IF;
END
$$;

BEGIN;
\ir create_agent2_personal_weekly_briefs.sql
INSERT INTO public.agent2_personal_weekly_briefs (
    brief_id,
    tenant_id,
    owner_user_id,
    conversation_id,
    week_start,
    week_end,
    snapshot_at,
    source_snapshot,
    source_fingerprint,
    status,
    idempotency_key,
    created_at,
    updated_at
) VALUES (
    '00000000-0000-0000-0000-000000000001'::uuid,
    'migration-smoke',
    'synthetic-user',
    'synthetic-conversation',
    DATE '2026-08-17',
    DATE '2026-08-21',
    now(),
    '{}'::jsonb,
    repeat('a', 64),
    'snapshot_ready',
    'migration-smoke:2026-08-17',
    now(),
    now()
);
SAVEPOINT before_nonempty_rollback;
\set ON_ERROR_STOP off
\ir rollback_agent2_personal_weekly_briefs.sql
\set ON_ERROR_STOP on
ROLLBACK TO SAVEPOINT before_nonempty_rollback;

DO $$
BEGIN
    IF (SELECT count(*) FROM public.agent2_personal_weekly_briefs) <> 1 THEN
        RAISE EXCEPTION 'nonempty rollback did not preserve the record';
    END IF;
END
$$;

DELETE FROM public.agent2_personal_weekly_briefs
WHERE brief_id = '00000000-0000-0000-0000-000000000001'::uuid;
\ir rollback_agent2_personal_weekly_briefs.sql
DO $$
BEGIN
    IF to_regclass('public.agent2_personal_weekly_briefs') IS NOT NULL THEN
        RAISE EXCEPTION 'empty rollback did not remove weekly brief table';
    END IF;
END
$$;
ROLLBACK;

DO $$
BEGIN
    IF to_regclass('public.agent2_personal_weekly_briefs') IS NOT NULL THEN
        RAISE EXCEPTION 'migration smoke left a public table behind';
    END IF;
END
$$;
