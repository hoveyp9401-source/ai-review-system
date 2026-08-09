from pathlib import Path

from app.agent2.business.models import (
    Agent2OperationOutcome,
    Agent2TaskLedgerEntry,
    CaseFollowupPending,
    CaseFollowupPolicy,
    CaseFollowupTask,
    CaseLifecycleState,
    CaseReportProjection,
    ReportProjectionRequest,
)


def test_case_followup_policy_is_a_tenant_case_user_scoped_versioned_model():
    columns = set(CaseFollowupPolicy.__table__.columns.keys())
    assert {
        "policy_id", "tenant_id", "case_id", "assigned_user_id", "enabled",
        "policy_source", "cadence_type", "cadence_days", "custom_interval_json",
        "timezone", "business_days_only", "allowed_start_time", "allowed_end_time",
        "event_triggers_enabled", "hearing_reminders_enabled", "stage_transition_enabled",
        "node_transition_enabled", "last_meaningful_progress_at", "last_followup_at",
        "next_due_at", "snoozed_until", "max_unanswered_reminders", "version",
        "created_at", "updated_at",
    } <= columns


def test_case_followup_task_separates_task_message_and_response_lifecycles():
    columns = set(CaseFollowupTask.__table__.columns.keys())
    assert {
        "followup_id", "tenant_id", "case_id", "assigned_user_id", "policy_id",
        "trigger_type", "trigger_event_id", "trigger_sources_json", "case_type",
        "stage", "node", "case_version", "question_type", "question_text", "priority", "task_status",
        "message_status", "response_status", "due_at", "expires_at", "last_sent_at",
        "next_eligible_at", "reminder_count", "max_reminders", "conversation_id",
        "pending_id", "source_progress_id", "provider_message_id", "completed_at",
        "cancelled_at", "idempotency_key", "version", "created_at", "updated_at",
    } <= columns
    assert "status" not in columns


def test_case_lifecycle_state_keeps_plan_readiness_and_stage_separate_from_progress_text():
    columns = set(CaseLifecycleState.__table__.columns.keys())
    assert {
        "lifecycle_state_id", "tenant_id", "case_id", "assigned_user_id",
        "case_type", "stage", "node", "current_status", "next_actions_json",
        "hearing_readiness", "blocking_issues_json", "last_progress_id",
        "version", "created_at", "updated_at",
    } <= columns


def test_case_followup_pending_has_full_scope_version_and_consumption_identity():
    columns = set(CaseFollowupPending.__table__.columns.keys())
    assert {
        "pending_id", "pending_type", "tenant_id", "user_id", "conversation_id",
        "domain", "operation", "source_turn_id", "source_message_id", "task_id",
        "case_id", "followup_id", "candidate_refs_json", "candidate_versions_json",
        "candidate_labels_json", "acceptable_answer_forms_json", "expected_state_version",
        "expires_at", "status", "consumed_at", "cancelled_at", "idempotency_key",
        "version", "created_at", "updated_at",
    } <= columns


def test_task_ledger_entry_supports_focused_active_and_suspended_tasks():
    columns = set(Agent2TaskLedgerEntry.__table__.columns.keys())
    assert {
        "task_id", "tenant_id", "user_id", "conversation_id", "domain", "operation",
        "object_ref_json", "status", "focus_state", "version", "source_turn_id",
        "pending_requirements_json", "resume_policy_json", "expires_at", "created_at",
        "updated_at",
    } <= columns


def test_report_projection_request_and_relation_keep_case_and_report_writes_separate():
    request_columns = set(ReportProjectionRequest.__table__.columns.keys())
    relation_columns = set(CaseReportProjection.__table__.columns.keys())
    assert {
        "request_id", "tenant_id", "user_id", "case_id", "case_progress_id",
        "followup_id", "source_turn_id", "source_message_id", "case_receipt_id",
        "decision_json", "status", "idempotency_key", "attempt_count", "last_error",
        "created_at", "updated_at", "completed_at",
    } <= request_columns
    assert {
        "projection_id", "tenant_id", "user_id", "case_id", "case_progress_id",
        "report_id", "report_item_id", "report_type", "projection_type",
        "source_turn_id", "source_followup_id", "source_message_id", "status",
        "version", "removed_at", "idempotency_key", "created_at", "updated_at",
    } <= relation_columns


def test_operation_outcome_has_a_persistent_replayable_audit_envelope():
    columns = set(Agent2OperationOutcome.__table__.columns.keys())
    assert {
        "outcome_id", "tenant_id", "user_id", "conversation_id", "source_turn_id",
        "domain", "operation", "object_type", "object_id", "business_status",
        "message_status", "actual_write", "would_write", "changed_fields_json",
        "user_visible_snapshot_json", "blocking_reason", "receipt_refs_json",
        "audit_refs_json", "state_transition_json", "idempotency_key", "created_at",
    } <= columns


def test_case_followup_migration_is_idempotent_and_creates_every_phase1_table():
    sql = Path("scripts/create_agent2_case_lifecycle_followup.sql").read_text(
        encoding="utf-8"
    )
    for table in (
        "agent2_case_followup_policies",
        "agent2_case_followup_tasks",
        "agent2_case_lifecycle_states",
        "agent2_case_followup_pendings",
        "agent2_task_ledger",
        "agent2_report_projection_requests",
        "agent2_case_report_projections",
        "agent2_operation_outcomes",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS" in sql
    assert "accepted_by_provider" in sql
    assert "delivery_confirmed" in sql
