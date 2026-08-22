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
    assert "'snapshot_ready', 'generating', 'generation_failed'" in sql
    assert "provider_accepted_at IS NOT NULL" in sql
    assert "status = 'delivered'" in sql
    assert "AND delivered_at IS NOT NULL" in sql
    assert "delivery_receipt_json <> '{}'::jsonb" in sql
    assert "generation_started_at timestamptz" in sql
    assert "generated_at timestamptz" in sql
    assert "send_started_at timestamptz" in sql
    assert "final_verified_at timestamptz" in sql
    assert "recovery_json jsonb" in sql
    assert "jsonb_typeof(recovery_json) = 'array'" in sql
    assert "context_recorded_at IS NULL" in sql


def test_personal_weekly_brief_rollback_refuses_nonempty_table() -> None:
    sql = Path("scripts/rollback_agent2_personal_weekly_briefs.sql").read_text(
        encoding="utf-8"
    )

    assert sql.lstrip().startswith("BEGIN;")
    assert sql.rstrip().endswith("COMMIT;")
    assert "LOCK TABLE agent2_personal_weekly_briefs" in sql
    assert "backup and manual handling required" in sql
    assert "DROP TABLE agent2_personal_weekly_briefs" in sql


def test_isolated_postgres_gate_covers_apply_rollback_apply_and_permissions() -> None:
    source = Path(
        "scripts/verify_agent2_personal_weekly_brief_postgres_isolated.py"
    ).read_text(encoding="utf-8")

    for required_check in (
        "empty_table_rollback",
        "apply_after_rollback",
        "nonempty_rollback_refused",
        "backup_required_before_nonempty_rollback",
        "public_table_privileges_absent",
        "final_empty_rollback_zero_residual",
    ):
        assert required_check in source
    assert 'parser.add_argument("--rollback"' in source
