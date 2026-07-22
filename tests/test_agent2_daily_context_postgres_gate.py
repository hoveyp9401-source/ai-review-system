from __future__ import annotations

import pytest

from scripts.verify_agent2_daily_context_postgres import (
    EvidenceSafetyError,
    GateConfig,
    RUNTIME_ENV_FENCES,
    _safe_failure_symbol,
    build_isolation_plan,
    evaluate_gate_checks,
    isolated_search_path,
    validate_public_artifact,
)


def test_gate_config_requires_allowlisted_run_id_and_explicit_confirmation() -> None:
    with pytest.raises(ValueError, match="run id"):
        GateConfig.create(
            run_id="unsafe-schema-name",
            confirmation="RUN_ISOLATED_AGENT2_DAILY_POSTGRES_GATE",
        )

    with pytest.raises(ValueError, match="confirmation"):
        GateConfig.create(
            run_id="release_20260722_a1b2c3d4",
            confirmation="",
        )

    config = GateConfig.create(
        run_id="release_20260722_a1b2c3d4",
        confirmation="RUN_ISOLATED_AGENT2_DAILY_POSTGRES_GATE",
    )

    assert config.schema == "agent2_daily_gate_release_20260722_a1b2c3d4"


def test_public_artifact_rejects_credentials_and_business_plaintext() -> None:
    with pytest.raises(EvidenceSafetyError, match="forbidden evidence key"):
        validate_public_artifact(
            {"database_url": "postgresql+asyncpg://user:secret@db/review"}
        )

    with pytest.raises(EvidenceSafetyError, match="forbidden evidence key"):
        validate_public_artifact({"checks": {"raw_input": "synthetic body"}})

    safe = {
        "status": "passed",
        "checks": {
            "committed_snapshot_sha256": "a" * 64,
            "receipt_count": 6,
        },
    }
    assert validate_public_artifact(safe) == safe


def test_isolation_plan_clones_only_required_tables_into_allowlisted_schema() -> None:
    config = GateConfig.create(
        run_id="release_20260722_a1b2c3d4",
        confirmation="RUN_ISOLATED_AGENT2_DAILY_POSTGRES_GATE",
    )

    plan = build_isolation_plan(config)

    assert plan.schema == config.schema
    assert plan.tables == (
        "teams",
        "users",
        "daily_reports",
        "report_interaction_events",
        "agent2_daily_command_receipts",
    )
    assert all(
        statement.startswith(f'CREATE TABLE "{config.schema}".')
        and "LIKE public." in statement
        and statement.endswith(" INCLUDING ALL)")
        for statement in plan.clone_statements
    )
    assert "public" not in plan.drop_statement.lower()
    assert plan.drop_statement == f'DROP SCHEMA "{config.schema}" CASCADE'


def test_gate_search_path_cannot_fall_through_to_public() -> None:
    config = GateConfig.create(
        run_id="release_20260722_a1b2c3d4",
        confirmation="RUN_ISOLATED_AGENT2_DAILY_POSTGRES_GATE",
    )

    assert isolated_search_path(config) == (
        "agent2_daily_gate_release_20260722_a1b2c3d4,pg_catalog"
    )
    assert "public" not in isolated_search_path(config).split(",")


def test_gate_decision_fails_closed_when_any_hard_check_is_false() -> None:
    checks = {
        "stable_item_id_move_atomic": True,
        "partial_update_preserved_other_sections": True,
        "three_command_versions_advanced": True,
        "duplicate_delivery_idempotent": True,
        "concurrent_duplicate_serialized": True,
        "noop_preserved_row_and_version": True,
        "reply_matches_committed_snapshot": True,
        "transaction_rollback_atomic": True,
        "public_schema_unchanged": True,
        "cleanup_confirmed": True,
        "database_write_outside_isolated_schema": 0,
        "external_api_calls": 0,
        "model_calls": 0,
        "message_sends": 0,
    }

    assert evaluate_gate_checks(checks)["decision"] == "PASS"

    failed = dict(checks, noop_preserved_row_and_version=False)
    decision = evaluate_gate_checks(failed)
    assert decision["decision"] == "NO_GO"
    assert decision["failed_checks"] == ["noop_preserved_row_and_version"]


def test_gate_forces_all_external_side_effect_features_off() -> None:
    assert RUNTIME_ENV_FENCES == {
        "AGENT2_CASE_FOLLOWUP_SEND_ENABLED": "false",
        "CASE_FOLLOWUP_SEND_ENABLED": "false",
        "PROGRESS_ENABLED": "false",
        "PROGRESS_OUTBOX_ENABLED": "false",
        "PROGRESS_WORKER_ENABLED": "false",
        "REMINDER_SEND_ENABLED": "false",
        "SHADOW_MEMORY_ENABLED": "false",
    }


def test_failure_diagnostics_expose_only_safe_symbol_names() -> None:
    missing = AttributeError("safe diagnostic", name="command_results")
    assert _safe_failure_symbol(missing) == "command_results"
    assert _safe_failure_symbol(KeyError("before_version")) == "before_version"
    assert _safe_failure_symbol(KeyError("postgresql://secret")) == ""
