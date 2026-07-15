from __future__ import annotations

import uuid
from datetime import date as date_type, datetime, time as time_type
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, Index, Integer, Numeric, String, Text, Time, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db import Base


class Agent2IdentityBinding(Base):
    __tablename__ = "agent2_identity_bindings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", name="agent2_identity_tenant_user_key"),
        UniqueConstraint("tenant_id", "dingtalk_user_id", name="agent2_identity_tenant_dingtalk_key"),
        Index("agent2_identity_scope_idx", "tenant_id", "company_id", "department_id", "team_id"),
    )

    binding_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    company_id: Mapped[str] = mapped_column(String(128), nullable=False)
    department_id: Mapped[str] = mapped_column(String(128), nullable=False)
    team_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    dingtalk_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    role_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    permission_scope_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class Agent2Case(Base):
    __tablename__ = "agent2_cases"
    __table_args__ = (
        UniqueConstraint("tenant_id", "external_case_id", name="agent2_cases_tenant_external_key"),
        Index("agent2_cases_number_idx", "tenant_id", "case_number"),
        Index("agent2_cases_owner_idx", "tenant_id", "owner_user_id", "status"),
    )

    case_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    company_id: Mapped[str] = mapped_column(String(128), nullable=False)
    department_id: Mapped[str] = mapped_column(String(128), nullable=False)
    team_id: Mapped[str] = mapped_column(String(128), nullable=False)
    external_case_id: Mapped[str] = mapped_column(String(256), nullable=False)
    case_number: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    case_name: Mapped[str] = mapped_column(Text, nullable=False)
    case_type: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="open")
    owner_user_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(512), nullable=False)
    source_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class CaseLifecycleState(Base):
    __tablename__ = "agent2_case_lifecycle_states"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "case_id", name="agent2_case_lifecycle_state_case_key"
        ),
        CheckConstraint(
            "version >= 1", name="agent2_case_lifecycle_state_version_check"
        ),
        Index(
            "agent2_case_lifecycle_state_owner_idx",
            "tenant_id", "assigned_user_id", "stage", "node",
        ),
    )

    lifecycle_state_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    case_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    assigned_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    case_type: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    stage: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    node: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    current_status: Mapped[str] = mapped_column(Text, nullable=False, default="")
    next_actions_json: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    hearing_readiness: Mapped[str] = mapped_column(Text, nullable=False, default="")
    blocking_issues_json: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    last_progress_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CaseFollowupPolicy(Base):
    __tablename__ = "agent2_case_followup_policies"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "case_id", "assigned_user_id",
            name="agent2_case_followup_policy_scope_key",
        ),
        CheckConstraint(
            "policy_source IN ('tenant_default','stage_default','bulk_assignment','case_manual_override')",
            name="agent2_case_followup_policy_source_check",
        ),
        CheckConstraint(
            "cadence_type IN ('daily','weekly','every_15_days','monthly','custom_interval','event_only','manual_only','paused','disabled')",
            name="agent2_case_followup_cadence_check",
        ),
        CheckConstraint("version >= 1", name="agent2_case_followup_policy_version_check"),
        Index("agent2_case_followup_policy_due_idx", "tenant_id", "enabled", "next_due_at"),
    )

    policy_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    case_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    assigned_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    policy_source: Mapped[str] = mapped_column(String(32), nullable=False, default="tenant_default")
    cadence_type: Mapped[str] = mapped_column(String(32), nullable=False, default="event_only")
    cadence_days: Mapped[int | None] = mapped_column(Integer)
    custom_interval_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Shanghai")
    business_days_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    allowed_start_time: Mapped[time_type] = mapped_column(
        Time(timezone=False), nullable=False, default=lambda: time_type(9, 0)
    )
    allowed_end_time: Mapped[time_type] = mapped_column(
        Time(timezone=False), nullable=False, default=lambda: time_type(18, 0)
    )
    event_triggers_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    hearing_reminders_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    stage_transition_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    node_transition_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_meaningful_progress_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_followup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    max_unanswered_reminders: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class CaseFollowupTask(Base):
    __tablename__ = "agent2_case_followup_tasks"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="agent2_case_followup_task_idempotency_key"),
        CheckConstraint(
            "task_status IN ('scheduled','queued','sending','waiting_for_reply','answered','snoozed','cancelled','expired','failed')",
            name="agent2_case_followup_task_status_check",
        ),
        CheckConstraint(
            "message_status IN ('scheduled','queued','sending','accepted_by_provider','delivery_confirmed','failed','cancelled')",
            name="agent2_case_followup_message_status_check",
        ),
        CheckConstraint(
            "response_status IN ('not_requested','awaiting_input','answered','snoozed','cancelled','expired')",
            name="agent2_case_followup_response_status_check",
        ),
        CheckConstraint("version >= 1", name="agent2_case_followup_task_version_check"),
        Index("agent2_case_followup_task_due_idx", "tenant_id", "task_status", "due_at"),
        Index("agent2_case_followup_task_user_idx", "tenant_id", "assigned_user_id", "response_status"),
    )

    followup_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    case_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    assigned_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    policy_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    trigger_type: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger_event_id: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    trigger_sources_json: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    case_type: Mapped[str] = mapped_column(String(32), nullable=False)
    stage: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    node: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    case_version: Mapped[int] = mapped_column(Integer, nullable=False)
    question_type: Mapped[str] = mapped_column(String(64), nullable=False)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    task_status: Mapped[str] = mapped_column(String(32), nullable=False, default="scheduled")
    message_status: Mapped[str] = mapped_column(String(32), nullable=False, default="scheduled")
    response_status: Mapped[str] = mapped_column(String(32), nullable=False, default="not_requested")
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_eligible_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reminder_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_reminders: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    conversation_id: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    pending_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    source_progress_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    provider_message_id: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str] = mapped_column(String(512), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class CaseFollowupPending(Base):
    __tablename__ = "agent2_case_followup_pendings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="agent2_case_followup_pending_idempotency_key"),
        CheckConstraint(
            "pending_type IN ('case_followup','selection','confirmation','information')",
            name="agent2_case_followup_pending_type_check",
        ),
        CheckConstraint(
            "status IN ('active','awaiting_input','consumed','expired','cancelled','conflicted','permission_revoked')",
            name="agent2_case_followup_pending_status_check",
        ),
        CheckConstraint("version >= 1", name="agent2_case_followup_pending_version_check"),
        Index("agent2_case_followup_pending_scope_idx", "tenant_id", "user_id", "conversation_id", "status"),
    )

    pending_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pending_type: Mapped[str] = mapped_column(String(32), nullable=False, default="case_followup")
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    conversation_id: Mapped[str] = mapped_column(String(256), nullable=False)
    domain: Mapped[str] = mapped_column(String(64), nullable=False, default="case")
    operation: Mapped[str] = mapped_column(String(128), nullable=False)
    source_turn_id: Mapped[str] = mapped_column(String(256), nullable=False)
    source_message_id: Mapped[str] = mapped_column(String(256), nullable=False)
    task_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    case_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    followup_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    candidate_refs_json: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    candidate_versions_json: Mapped[dict[str, int]] = mapped_column(JSONB, nullable=False, default=dict)
    candidate_labels_json: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False, default=dict)
    acceptable_answer_forms_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    expected_state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str] = mapped_column(String(512), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class Agent2TaskLedgerEntry(Base):
    __tablename__ = "agent2_task_ledger"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active','awaiting_input','suspended','completed','cancelled','failed','expired')",
            name="agent2_task_ledger_status_check",
        ),
        CheckConstraint(
            "focus_state IN ('focused','active','suspended')",
            name="agent2_task_ledger_focus_check",
        ),
        CheckConstraint("version >= 1", name="agent2_task_ledger_version_check"),
        Index("agent2_task_ledger_scope_idx", "tenant_id", "user_id", "conversation_id", "status"),
    )

    task_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    conversation_id: Mapped[str] = mapped_column(String(256), nullable=False)
    domain: Mapped[str] = mapped_column(String(64), nullable=False)
    operation: Mapped[str] = mapped_column(String(128), nullable=False)
    object_ref_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    focus_state: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source_turn_id: Mapped[str] = mapped_column(String(256), nullable=False)
    pending_requirements_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    resume_policy_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class ReportProjectionRequest(Base):
    __tablename__ = "agent2_report_projection_requests"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="agent2_report_projection_request_key"),
        CheckConstraint(
            "status IN ('pending','processing','succeeded','skipped','failed','cancelled')",
            name="agent2_report_projection_request_status_check",
        ),
        Index("agent2_report_projection_request_claim_idx", "tenant_id", "status", "created_at"),
    )

    request_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    case_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    case_progress_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    followup_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    source_turn_id: Mapped[str] = mapped_column(String(256), nullable=False)
    source_message_id: Mapped[str] = mapped_column(String(256), nullable=False)
    case_receipt_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    decision_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    idempotency_key: Mapped[str] = mapped_column(String(512), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CaseReportProjection(Base):
    __tablename__ = "agent2_case_report_projections"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="agent2_case_report_projection_key"),
        CheckConstraint(
            "status IN ('active','removed','failed')",
            name="agent2_case_report_projection_status_check",
        ),
        CheckConstraint("version >= 1", name="agent2_case_report_projection_version_check"),
        Index("agent2_case_report_projection_case_idx", "tenant_id", "case_id", "status"),
    )

    projection_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    case_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    case_progress_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    report_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    report_item_id: Mapped[str] = mapped_column(String(256), nullable=False)
    report_type: Mapped[str] = mapped_column(String(16), nullable=False)
    projection_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_turn_id: Mapped[str] = mapped_column(String(256), nullable=False)
    source_followup_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    source_message_id: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class Agent2OperationOutcome(Base):
    __tablename__ = "agent2_operation_outcomes"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="agent2_operation_outcome_key"),
        Index("agent2_operation_outcome_source_idx", "tenant_id", "source_turn_id", "created_at"),
    )

    outcome_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    conversation_id: Mapped[str] = mapped_column(String(256), nullable=False)
    source_turn_id: Mapped[str] = mapped_column(String(256), nullable=False)
    domain: Mapped[str] = mapped_column(String(64), nullable=False)
    operation: Mapped[str] = mapped_column(String(128), nullable=False)
    object_type: Mapped[str] = mapped_column(String(128), nullable=False)
    object_id: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    object_label: Mapped[str] = mapped_column(Text, nullable=False, default="")
    object_version: Mapped[int | None] = mapped_column(Integer)
    business_status: Mapped[str] = mapped_column(String(64), nullable=False)
    message_status: Mapped[str] = mapped_column(String(64), nullable=False)
    actual_write: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    would_write: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    changed_fields_json: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    user_visible_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    blocking_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    receipt_refs_json: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    audit_refs_json: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    state_transition_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class PartyEntity(Base):
    __tablename__ = "agent2_party_entities"
    __table_args__ = (
        CheckConstraint("party_type IN ('company', 'person', 'organization', 'government', 'court', 'other')", name="agent2_party_type_check"),
        UniqueConstraint("tenant_id", "party_id", name="agent2_party_tenant_party_key"),
        Index("agent2_party_name_idx", "tenant_id", "canonical_name"),
    )

    party_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    party_type: Mapped[str] = mapped_column(String(32), nullable=False)
    canonical_name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_name: Mapped[str] = mapped_column(Text, nullable=False)
    short_name: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    former_names: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    unified_social_credit_code: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    registration_number: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    legal_representative: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="active")
    registered_address: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(512), nullable=False)
    data_quality: Mapped[str] = mapped_column(String(64), nullable=False, default="unverified")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class PartyAlias(Base):
    __tablename__ = "agent2_party_aliases"
    __table_args__ = (
        UniqueConstraint("tenant_id", "normalized_alias", "party_id", name="agent2_party_alias_unique"),
        Index("agent2_party_alias_lookup_idx", "tenant_id", "normalized_alias"),
    )

    alias_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    party_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    alias: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_alias: Mapped[str] = mapped_column(Text, nullable=False)
    alias_type: Mapped[str] = mapped_column(String(64), nullable=False, default="alias")
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    confirmation_status: Mapped[str] = mapped_column(String(32), nullable=False, default="confirmed")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class PartyIdentifier(Base):
    __tablename__ = "agent2_party_identifiers"
    __table_args__ = (
        UniqueConstraint("tenant_id", "identifier_type", "normalized_value", name="agent2_party_identifier_unique"),
        Index("agent2_party_identifier_lookup_idx", "tenant_id", "normalized_value"),
    )

    identifier_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    party_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    identifier_type: Mapped[str] = mapped_column(String(64), nullable=False)
    identifier_value: Mapped[str] = mapped_column(String(256), nullable=False)
    normalized_value: Mapped[str] = mapped_column(String(256), nullable=False)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    confirmation_status: Mapped[str] = mapped_column(String(32), nullable=False, default="confirmed")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class PartyCaseRole(Base):
    __tablename__ = "agent2_party_case_roles"
    __table_args__ = (
        UniqueConstraint("tenant_id", "party_id", "case_id", "role_type", "effective_from", name="agent2_party_case_role_unique"),
        Index("agent2_party_case_role_party_idx", "tenant_id", "party_id", "role_type"),
        Index("agent2_party_case_role_case_idx", "tenant_id", "case_id"),
    )

    role_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    party_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    case_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    role_type: Mapped[str] = mapped_column(String(64), nullable=False)
    effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_reference: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    confirmation_status: Mapped[str] = mapped_column(String(32), nullable=False, default="confirmed")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class PartyRelation(Base):
    __tablename__ = "agent2_party_relations"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "case_id",
            "from_party_id",
            "to_party_id",
            "relation_type",
            name="agent2_party_relation_unique",
        ),
    )

    relation_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    case_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    from_party_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    to_party_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    relation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_reference: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    confirmation_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending_confirmation")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class PartyCaseClue(Base):
    __tablename__ = "agent2_party_case_clues"
    __table_args__ = (
        CheckConstraint(
            "clue_type IN ('person', 'court', 'payment', 'asset', 'document', 'other')",
            name="agent2_party_case_clue_type_check",
        ),
        UniqueConstraint(
            "tenant_id",
            "case_id",
            "party_id",
            "clue_type",
            "source_type",
            "source_id",
            "source_field",
            name="agent2_party_case_clue_unique",
        ),
        Index("agent2_party_case_clue_lookup_idx", "tenant_id", "party_id", "case_id", "clue_type"),
    )

    clue_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    case_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    party_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    clue_type: Mapped[str] = mapped_column(String(32), nullable=False)
    label: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    currency: Mapped[str] = mapped_column(String(16), nullable=False, default="CNY")
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(512), nullable=False)
    source_field: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    source_reference: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    confirmation_status: Mapped[str] = mapped_column(String(32), nullable=False, default="confirmed")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class PartySourceReference(Base):
    __tablename__ = "agent2_party_source_references"
    __table_args__ = (
        UniqueConstraint("tenant_id", "party_id", "source_type", "source_id", "source_field", name="agent2_party_source_unique"),
    )

    reference_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    party_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(512), nullable=False)
    source_field: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    source_row: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    source_value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    content_origin: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmation_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending_confirmation")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class PartyMergeCandidate(Base):
    __tablename__ = "agent2_party_merge_candidates"
    __table_args__ = (
        CheckConstraint("status IN ('candidate', 'confirmed', 'rejected', 'expired')", name="agent2_party_merge_status_check"),
        UniqueConstraint("tenant_id", "left_party_id", "right_party_id", name="agent2_party_merge_pair_unique"),
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    left_party_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    right_party_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    match_basis: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    match_score: Mapped[Decimal] = mapped_column(Numeric(6, 5), nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="candidate")
    reviewed_by: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class PartyConflict(Base):
    __tablename__ = "agent2_party_conflicts"
    __table_args__ = (
        CheckConstraint("status IN ('open', 'resolved', 'dismissed')", name="agent2_party_conflict_status_check"),
    )

    conflict_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    party_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    field_name: Mapped[str] = mapped_column(String(128), nullable=False)
    competing_values: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    resolution_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    resolved_by: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class TravelIntent(Base):
    __tablename__ = "agent2_travel_intents"
    __table_args__ = (
        CheckConstraint("status IN ('proposed', 'planned', 'confirmed', 'changed', 'cancelled', 'completed')", name="agent2_travel_status_check"),
        UniqueConstraint("tenant_id", "idempotency_key", name="agent2_travel_idempotency_key"),
        Index("agent2_travel_match_idx", "tenant_id", "city_code", "start_at", "end_at", "status"),
    )

    travel_intent_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    company_id: Mapped[str] = mapped_column(String(128), nullable=False)
    department_id: Mapped[str] = mapped_column(String(128), nullable=False)
    team_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    destination_raw: Mapped[str] = mapped_column(Text, nullable=False)
    destination_normalized: Mapped[str] = mapped_column(String(256), nullable=False)
    city_code: Mapped[str] = mapped_column(String(32), nullable=False)
    province_code: Mapped[str] = mapped_column(String(32), nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    time_precision: Mapped[str] = mapped_column(String(32), nullable=False)
    purpose_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    related_case_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    related_matter_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    source_message_id: Mapped[str] = mapped_column(String(256), nullable=False)
    source_channel: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="planned")
    confidence: Mapped[Decimal] = mapped_column(Numeric(6, 5), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(512), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class CaseTravelClarificationPendingRow(Base):
    __tablename__ = "agent2_case_travel_clarification_pendings"
    __table_args__ = (
        CheckConstraint(
            "status IN ('awaiting_confirmation','awaiting_destination','awaiting_city','consumed','cancelled','expired','conflicted')",
            name="agent2_case_travel_pending_status_check",
        ),
        CheckConstraint("version >= 1", name="agent2_case_travel_pending_version_check"),
        UniqueConstraint(
            "tenant_id", "idempotency_key",
            name="agent2_case_travel_pending_idempotency_key",
        ),
        Index(
            "agent2_case_travel_pending_scope_idx",
            "tenant_id", "user_id", "conversation_id", "status", "expires_at",
        ),
    )

    pending_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    conversation_id: Mapped[str] = mapped_column(String(256), nullable=False)
    case_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    case_version: Mapped[int] = mapped_column(Integer, nullable=False)
    case_name: Mapped[str] = mapped_column(Text, nullable=False)
    source_message_id: Mapped[str] = mapped_column(String(256), nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    travel_date: Mapped[date_type] = mapped_column(Date, nullable=False)
    purpose_summary: Mapped[str] = mapped_column(Text, nullable=False)
    suggested_destination: Mapped[str] = mapped_column(Text, nullable=False, default="")
    candidate_destination: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    last_reply_message_id: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    receipt_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    idempotency_key: Mapped[str] = mapped_column(String(512), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class TravelCollaborationCandidate(Base):
    __tablename__ = "agent2_travel_collaboration_candidates"
    __table_args__ = (
        CheckConstraint("status IN ('candidate', 'notified', 'accepted_by_one', 'accepted', 'declined', 'expired', 'cancelled')", name="agent2_travel_candidate_status_check"),
        UniqueConstraint("tenant_id", "deduplication_key", name="agent2_travel_candidate_dedup_key"),
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    company_id: Mapped[str] = mapped_column(String(128), nullable=False)
    department_id: Mapped[str] = mapped_column(String(128), nullable=False)
    team_id: Mapped[str] = mapped_column(String(128), nullable=False)
    travel_intent_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    participant_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    destination: Mapped[str] = mapped_column(String(256), nullable=False)
    overlap_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    overlap_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    match_reason: Mapped[str] = mapped_column(Text, nullable=False)
    match_score: Mapped[Decimal] = mapped_column(Numeric(6, 5), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="candidate")
    notification_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    responses_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    deduplication_key: Mapped[str] = mapped_column(String(512), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class NotificationOutbox(Base):
    __tablename__ = "agent2_notification_outbox"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'processing', 'sent', 'failed', 'dead_letter', 'cancelled')", name="agent2_notification_status_check"),
        UniqueConstraint("tenant_id", "idempotency_key", name="agent2_notification_idempotency_key"),
        Index("agent2_notification_claim_idx", "status", "next_retry_at", "created_at"),
    )

    notification_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    recipient_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    channel: Mapped[str] = mapped_column(String(64), nullable=False, default="dingtalk")
    message_type: Mapped[str] = mapped_column(String(64), nullable=False)
    message_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    external_message_id: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    response_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    dispatch_history_json: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    error_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class CaseProgress(Base):
    __tablename__ = "agent2_case_progress"
    __table_args__ = (
        CheckConstraint("content_origin IN ('human_record', 'imported_record', 'ai_extracted', 'system_fact', 'robot_followup')", name="agent2_case_progress_origin_check"),
        UniqueConstraint("tenant_id", "idempotency_key", name="agent2_case_progress_idempotency_key"),
        Index("agent2_case_progress_case_idx", "tenant_id", "case_id", "occurred_at"),
    )

    progress_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    case_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reporter_id: Mapped[str] = mapped_column(String(128), nullable=False)
    progress_type: Mapped[str] = mapped_column(String(64), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_message_id: Mapped[str] = mapped_column(String(256), nullable=False)
    source_channel: Mapped[str] = mapped_column(String(64), nullable=False)
    content_origin: Mapped[str] = mapped_column(String(32), nullable=False)
    related_party_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    related_document_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    related_travel_intent_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    confidence: Mapped[Decimal] = mapped_column(Numeric(6, 5), nullable=False)
    confirmation_status: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    idempotency_key: Mapped[str] = mapped_column(String(512), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_by: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    delete_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class BusinessCommandReceipt(Base):
    __tablename__ = "agent2_business_command_receipts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="agent2_business_receipt_idempotency_key"),
        Index("agent2_business_receipt_source_idx", "tenant_id", "source_message_id"),
    )

    receipt_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    command_id: Mapped[str] = mapped_column(String(256), nullable=False)
    command_type: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_message_id: Mapped[str] = mapped_column(String(256), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    resource_id: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    before_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    after_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    error_code: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    failed_stage: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    actual_write: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class BusinessAuditEvent(Base):
    __tablename__ = "agent2_business_audit_events"
    __table_args__ = (Index("agent2_business_audit_resource_idx", "tenant_id", "resource_type", "resource_id", "created_at"),)

    audit_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    receipt_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    actor_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_message_id: Mapped[str] = mapped_column(String(256), nullable=False)
    source_channel: Mapped[str] = mapped_column(String(64), nullable=False)
    command_type: Mapped[str] = mapped_column(String(128), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(128), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(256), nullable=False)
    before_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    after_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class TenantRouteControl(Base):
    __tablename__ = "agent2_tenant_route_controls"
    __table_args__ = (
        CheckConstraint("route_mode IN ('agent1', 'agent2_shadow', 'agent2_canary', 'agent2_primary')", name="agent2_route_mode_check"),
        UniqueConstraint("tenant_id", name="agent2_route_control_tenant_key"),
    )

    control_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    route_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="agent1")
    canary_user_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    agent1_rollback_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    changed_by: Mapped[str] = mapped_column(String(128), nullable=False)
    change_reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class RouteControlAudit(Base):
    __tablename__ = "agent2_route_control_audits"

    audit_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_message_id: Mapped[str] = mapped_column(String(256), nullable=False)
    before_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    after_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PeriodicReport(Base):
    __tablename__ = "agent2_periodic_reports"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "owner_user_id",
            "report_type",
            "period_key",
            name="agent2_periodic_report_owner_period_key",
        ),
        CheckConstraint(
            "report_type IN ('weekly', 'monthly')",
            name="agent2_periodic_report_type_check",
        ),
        CheckConstraint(
            "status IN ('collecting', 'completed', 'cancelled')",
            name="agent2_periodic_report_status_check",
        ),
        Index(
            "agent2_periodic_report_scope_idx",
            "tenant_id",
            "team_id",
            "report_type",
            "period_start",
        ),
    )

    report_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    company_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    department_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    team_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    owner_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    report_type: Mapped[str] = mapped_column(String(16), nullable=False)
    period_key: Mapped[str] = mapped_column(String(16), nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sections_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    item_ids_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="collecting")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_channel: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class PeriodicReportCommandReceipt(Base):
    __tablename__ = "agent2_periodic_report_command_receipts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="agent2_periodic_report_receipt_idempotency_key",
        ),
        Index(
            "agent2_periodic_report_receipt_source_idx",
            "tenant_id",
            "source_message_id",
        ),
    )

    receipt_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_message_id: Mapped[str] = mapped_column(String(256), nullable=False)
    source_channel: Mapped[str] = mapped_column(String(64), nullable=False)
    command_id: Mapped[str] = mapped_column(String(256), nullable=False)
    command_type: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    report_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    actual_write: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    before_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    after_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    error_code: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


BUSINESS_TABLES = {
    model.__tablename__: model
    for model in (
        Agent2IdentityBinding,
        Agent2Case,
        CaseLifecycleState,
        CaseFollowupPolicy,
        CaseFollowupTask,
        CaseFollowupPending,
        Agent2TaskLedgerEntry,
        ReportProjectionRequest,
        CaseReportProjection,
        Agent2OperationOutcome,
        PartyEntity,
        PartyAlias,
        PartyIdentifier,
        PartyCaseRole,
        PartyRelation,
        PartyCaseClue,
        PartySourceReference,
        PartyMergeCandidate,
        PartyConflict,
        TravelIntent,
        TravelCollaborationCandidate,
        NotificationOutbox,
        CaseProgress,
        BusinessCommandReceipt,
        BusinessAuditEvent,
        TenantRouteControl,
        RouteControlAudit,
    )
}

REPORT_TABLES = {
    model.__tablename__: model
    for model in (PeriodicReport, PeriodicReportCommandReceipt)
}
