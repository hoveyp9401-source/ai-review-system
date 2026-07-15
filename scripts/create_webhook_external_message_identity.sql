BEGIN;

-- The legacy webhook_events table is owned by a separate database role. Keep
-- it untouched and establish provider identity in an application-owned claim
-- ledger. The lock closes the historical-backfill race.
LOCK TABLE webhook_events IN SHARE ROW EXCLUSIVE MODE;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM webhook_events
        WHERE external_message_id IS NOT NULL AND external_message_id <> ''
        GROUP BY platform, external_message_id
        HAVING COUNT(*) > 1
    ) THEN
        RAISE EXCEPTION 'duplicate webhook provider identities must be adjudicated before migration';
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS message_ingress_claims (
    idempotency_key varchar(256) PRIMARY KEY,
    platform varchar(32) NOT NULL,
    external_message_id varchar(256),
    webhook_event_id uuid NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS message_ingress_claims_platform_external_message_id_key
    ON message_ingress_claims (platform, external_message_id)
    WHERE external_message_id IS NOT NULL AND external_message_id <> '';

INSERT INTO message_ingress_claims (
    idempotency_key,
    platform,
    external_message_id,
    webhook_event_id,
    created_at
)
SELECT
    idempotency_key,
    platform,
    NULLIF(external_message_id, ''),
    id,
    COALESCE(received_at, created_at, now())
FROM webhook_events
ON CONFLICT DO NOTHING;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM webhook_events event
        LEFT JOIN message_ingress_claims claim
          ON claim.webhook_event_id = event.id
         AND claim.idempotency_key = event.idempotency_key
        WHERE claim.webhook_event_id IS NULL
    ) THEN
        RAISE EXCEPTION 'message ingress claim backfill is incomplete';
    END IF;
END
$$;

COMMIT;
