from pathlib import Path


def test_personal_weekly_brief_migration_is_transactional_and_idempotent() -> None:
    sql = Path("scripts/create_agent2_personal_weekly_briefs.sql").read_text(
        encoding="utf-8"
    )

    assert sql.lstrip().startswith("BEGIN;")
    assert sql.rstrip().endswith("COMMIT;")
    assert "CREATE TABLE IF NOT EXISTS agent2_personal_weekly_briefs" in sql
    assert "UNIQUE (tenant_id, owner_user_id, week_start)" in sql
    assert "UNIQUE (tenant_id, idempotency_key)" in sql
    assert "length(source_fingerprint) = 64" in sql
    assert "'delivery_pending', 'delivered'" in sql
    assert "provider_accepted_at IS NOT NULL" in sql
    assert "status = 'delivered'" in sql
    assert "AND delivered_at IS NOT NULL" in sql
    assert "delivery_receipt_json <> '{}'::jsonb" in sql
    assert "context_recorded_at IS NULL" in sql
