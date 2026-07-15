import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.db import Base


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    department_name: Mapped[str] = mapped_column(String(128), nullable=False, default="default")
    dingtalk_webhook_url: Mapped[str | None] = mapped_column(Text)
    dingtalk_webhook_secret: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    users: Mapped[list["User"]] = relationship(back_populates="team")


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dingtalk_user_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    employee_no: Mapped[str | None] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    team_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("teams.id", onupdate="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(String(64), nullable=False, default="member")
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Shanghai")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    team: Mapped[Team] = relationship(back_populates="users")
    daily_reports: Mapped[list["DailyReport"]] = relationship(back_populates="user")


class Agent2ConversationState(Base):
    __tablename__ = "agent2_conversation_states"
    __table_args__ = (
        UniqueConstraint(
            "user_key",
            "conversation_id",
            name="agent2_conversation_states_user_conversation_key",
        ),
        CheckConstraint("version >= 0", name="agent2_conversation_states_version_nonnegative"),
        Index("agent2_conversation_states_updated_idx", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_key: Mapped[str] = mapped_column(String(128), nullable=False)
    conversation_id: Mapped[str] = mapped_column(String(256), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    state_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    last_message_id: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Agent2DailyCommandReceipt(Base):
    __tablename__ = "agent2_daily_command_receipts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="agent2_daily_receipts_tenant_idempotency_key",
        ),
        CheckConstraint(
            "status IN ('executed', 'duplicate', 'blocked')",
            name="agent2_daily_receipts_status_check",
        ),
        Index("agent2_daily_receipts_tenant_created_idx", "tenant_id", "created_at"),
        Index("agent2_daily_receipts_user_date_idx", "user_id", "report_date"),
    )

    receipt_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    report_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("daily_reports.id", ondelete="SET NULL"),
    )
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    message_id: Mapped[str] = mapped_column(String(512), nullable=False)
    command_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    decision_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    sub_decision_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    command_type: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    validation_status: Mapped[str] = mapped_column(String(32), nullable=False)
    actual_write: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False, default="daily_report")
    resource_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    reason_code: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    before_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    after_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    audit_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class DailyReport(Base):
    __tablename__ = "daily_reports"
    __table_args__ = (
        UniqueConstraint("user_id", "date", name="daily_reports_user_date_key"),
        CheckConstraint("completeness_score >= 0 AND completeness_score <= 1", name="daily_reports_completeness_range"),
        CheckConstraint(
            "status IN ('collecting', 'pending_confirmation', 'completed', 'skipped', 'cancelled')",
            name="daily_reports_status_check",
        ),
        CheckConstraint(
            "confirmation_type IN ('user_confirmed', 'auto_submitted_timeout', 'admin_confirmed', 'none')",
            name="daily_reports_confirmation_type_check",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    team_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("teams.id", onupdate="CASCADE"), nullable=False)
    report_date: Mapped[date] = mapped_column("date", Date, nullable=False)
    today_work: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    problems: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    tomorrow_plan: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    emotion: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    raw_input: Mapped[str] = mapped_column(Text, nullable=False, default="")
    input_fragments: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    section_status: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    completeness_score: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="collecting")
    confirmation_type: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    confirmed_by_user: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    quality_warning: Mapped[str | None] = mapped_column(Text)
    last_modified_by_user: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pending_confirmation_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    auto_submit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="dingtalk_text")
    llm_model: Mapped[str | None] = mapped_column(String(128))
    llm_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_prompted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    user: Mapped[User] = relationship(back_populates="daily_reports")


class TeamSummary(Base):
    __tablename__ = "team_summaries"
    __table_args__ = (
        CheckConstraint("scope IN ('team', 'department')", name="team_summaries_scope_check"),
        CheckConstraint(
            "(scope = 'team' AND team_id IS NOT NULL) OR (scope = 'department' AND team_id IS NULL)",
            name="team_summaries_scope_team_check",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    team_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"))
    summary_date: Mapped[date] = mapped_column("date", Date, nullable=False)
    key_work: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    major_problems: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    risks: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    tomorrow_plan_distribution: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    raw_summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    report_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    complete_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    llm_model: Mapped[str | None] = mapped_column(String(128))
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class PerformanceTask(Base):
    __tablename__ = "performance_tasks"
    __table_args__ = (
        CheckConstraint("status IN ('draft', 'active', 'closed', 'cancelled')", name="performance_tasks_status_check"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    period_label: Mapped[str] = mapped_column(String(64), nullable=False)
    metrics_json: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    created_by: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class PerformanceSubmission(Base):
    __tablename__ = "performance_submissions"
    __table_args__ = (
        UniqueConstraint("task_id", "user_id", name="performance_submissions_task_user_key"),
        CheckConstraint(
            "status IN ('collecting', 'pending_confirmation', 'completed', 'cancelled')",
            name="performance_submissions_status_check",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("performance_tasks.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    team_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    recipient_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    sent_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    responses_json: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    input_fragments_json: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="collecting")
    confirmed_by_user: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_prompted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    __table_args__ = (
        CheckConstraint("status IN ('processing', 'processed', 'failed')", name="webhook_events_status_check"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    idempotency_key: Mapped[str] = mapped_column(String(256), unique=True, nullable=False)
    platform: Mapped[str] = mapped_column(String(32), nullable=False, default="dingtalk")
    external_message_id: Mapped[str | None] = mapped_column(String(256))
    dingtalk_user_id: Mapped[str | None] = mapped_column(String(128))
    report_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("daily_reports.id", ondelete="SET NULL"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    response_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="processing")
    error_message: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class MessageIngressClaim(Base):
    """Database-authoritative ownership claim for one inbound provider event."""

    __tablename__ = "message_ingress_claims"
    __table_args__ = (
        Index(
            "message_ingress_claims_platform_external_message_id_key",
            "platform",
            "external_message_id",
            unique=True,
            postgresql_where=text(
                "external_message_id IS NOT NULL AND external_message_id <> ''"
            ),
        ),
    )

    idempotency_key: Mapped[str] = mapped_column(String(256), primary_key=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    external_message_id: Mapped[str | None] = mapped_column(String(256))
    webhook_event_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), unique=True, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ProgressOutboxEvent(Base):
    __tablename__ = "progress_outbox_events"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="progress_outbox_events_idempotency_key_key"),
        CheckConstraint(
            "status IN ('pending', 'processing', 'processed', 'failed', 'dead_letter')",
            name="progress_outbox_events_status_check",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(256), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    team_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    report_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    report_date: Mapped[date | None] = mapped_column(Date)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    raw_text_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(128))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReportInteractionEvent(Base):
    __tablename__ = "report_interaction_events"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    report_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("daily_reports.id", ondelete="SET NULL"))
    dingtalk_user_id: Mapped[str | None] = mapped_column(String(128))
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    message_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    llm_decision_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    backend_action: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    before_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    after_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    correction_type: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    correction_from: Mapped[str] = mapped_column(Text, nullable=False, default="")
    correction_to: Mapped[str] = mapped_column(Text, nullable=False, default="")
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    is_undo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_repeated_item_edit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    asr_suspect_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class UserHabit(Base):
    __tablename__ = "user_habits"
    __table_args__ = (
        UniqueConstraint("user_id", "habit_type", "trigger_text", "meaning", name="user_habits_unique_signal"),
        CheckConstraint("status IN ('candidate', 'active', 'rejected', 'disabled')", name="user_habits_status_check"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    habit_type: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger_text: Mapped[str] = mapped_column(String(128), nullable=False)
    meaning: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False, default=0)
    evidence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    counterexample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="candidate")
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    last_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
