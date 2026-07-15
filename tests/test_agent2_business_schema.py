from __future__ import annotations

from pathlib import Path

from app.agent2.business.models import BUSINESS_TABLES


BASE_EXPECTED_TABLES = {
    "agent2_identity_bindings",
    "agent2_cases",
    "agent2_party_entities",
    "agent2_party_aliases",
    "agent2_party_identifiers",
    "agent2_party_case_roles",
    "agent2_party_relations",
    "agent2_party_case_clues",
    "agent2_party_source_references",
    "agent2_party_merge_candidates",
    "agent2_party_conflicts",
    "agent2_travel_intents",
    "agent2_travel_collaboration_candidates",
    "agent2_notification_outbox",
    "agent2_case_progress",
    "agent2_business_command_receipts",
    "agent2_business_audit_events",
    "agent2_tenant_route_controls",
    "agent2_route_control_audits",
}

FOLLOWUP_TABLES = {
    "agent2_case_lifecycle_states",
    "agent2_case_followup_policies",
    "agent2_case_followup_tasks",
    "agent2_case_followup_pendings",
    "agent2_task_ledger",
    "agent2_report_projection_requests",
    "agent2_case_report_projections",
    "agent2_operation_outcomes",
}

EXPECTED_TABLES = BASE_EXPECTED_TABLES | FOLLOWUP_TABLES


def test_business_schema_declares_every_phase2_table():
    assert set(BUSINESS_TABLES) == EXPECTED_TABLES


def test_every_business_table_has_tenant_boundary_and_timestamps():
    for table_name, model in BUSINESS_TABLES.items():
        columns = model.__table__.columns
        assert "tenant_id" in columns, table_name
        assert "created_at" in columns, table_name
        if table_name not in {"agent2_business_audit_events", "agent2_route_control_audits"}:
            assert "updated_at" in columns, table_name


def test_case_progress_has_source_version_soft_delete_and_idempotency_fields():
    columns = BUSINESS_TABLES["agent2_case_progress"].__table__.columns
    assert {
        "progress_id",
        "tenant_id",
        "case_id",
        "occurred_at",
        "recorded_at",
        "reporter_id",
        "progress_type",
        "summary",
        "details",
        "source_message_id",
        "source_channel",
        "content_origin",
        "confidence",
        "confirmation_status",
        "version",
        "idempotency_key",
        "deleted_at",
        "deleted_by",
        "delete_reason",
    } <= set(columns.keys())


def test_party_case_clues_are_typed_permission_joinable_and_source_traceable():
    columns = BUSINESS_TABLES["agent2_party_case_clues"].__table__.columns
    assert {
        "clue_id",
        "tenant_id",
        "case_id",
        "party_id",
        "clue_type",
        "label",
        "summary",
        "amount",
        "currency",
        "occurred_at",
        "source_type",
        "source_id",
        "source_field",
        "source_reference",
        "confirmation_status",
    } <= set(columns.keys())


def test_party_relations_are_case_scoped_for_permission_filtering():
    columns = BUSINESS_TABLES["agent2_party_relations"].__table__.columns
    assert {"case_id", "from_party_id", "to_party_id", "source_reference"} <= set(columns.keys())


def test_travel_and_notification_tables_have_retry_and_dedup_fields():
    travel = BUSINESS_TABLES["agent2_travel_intents"].__table__.columns
    candidate = BUSINESS_TABLES["agent2_travel_collaboration_candidates"].__table__.columns
    notification = BUSINESS_TABLES["agent2_notification_outbox"].__table__.columns

    assert {
        "company_id",
        "department_id",
        "team_id",
        "source_message_id",
        "status",
        "confidence",
        "version",
    } <= set(travel.keys())
    assert {
        "company_id",
        "department_id",
        "team_id",
        "travel_intent_ids",
        "participant_ids",
        "notification_ids",
        "responses_json",
        "expires_at",
    } <= set(candidate.keys())
    assert {
        "idempotency_key",
        "status",
        "retry_count",
        "next_retry_at",
        "locked_at",
        "response_json",
        "dispatch_history_json",
        "error_message",
    } <= set(notification.keys())


def test_route_control_schema_requires_explicit_rollback_and_audit():
    control = BUSINESS_TABLES["agent2_tenant_route_controls"].__table__.columns
    audit = BUSINESS_TABLES["agent2_route_control_audits"].__table__.columns

    assert {"route_mode", "agent1_rollback_enabled", "version", "changed_by"} <= set(control.keys())
    assert {"before_json", "after_json", "actor_user_id", "reason", "source_message_id"} <= set(audit.keys())


def test_postgres_migration_is_transactional_and_contains_trigram_indexes():
    migration = Path("scripts/create_agent2_business_phase2_tables.sql").read_text(encoding="utf-8")

    assert migration.lstrip().startswith("BEGIN;")
    assert migration.rstrip().endswith("COMMIT;")
    assert "CREATE EXTENSION IF NOT EXISTS pg_trgm" in migration
    assert "gin_trgm_ops" in migration
    for table in BASE_EXPECTED_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in migration
    followup_migration = Path(
        "scripts/create_agent2_case_lifecycle_followup.sql"
    ).read_text(encoding="utf-8")
    for table in FOLLOWUP_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in followup_migration
