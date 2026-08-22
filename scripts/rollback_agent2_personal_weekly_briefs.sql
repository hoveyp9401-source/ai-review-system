BEGIN;

DO $$
DECLARE
    record_count bigint;
BEGIN
    IF to_regclass('public.agent2_personal_weekly_briefs') IS NULL THEN
        RETURN;
    END IF;

    LOCK TABLE agent2_personal_weekly_briefs IN ACCESS EXCLUSIVE MODE;
    SELECT count(*) INTO record_count
    FROM agent2_personal_weekly_briefs;

    IF record_count > 0 THEN
        RAISE EXCEPTION
            'agent2_personal_weekly_briefs contains % records; backup and manual handling required',
            record_count;
    END IF;

    DROP TABLE agent2_personal_weekly_briefs;
END
$$;

COMMIT;
