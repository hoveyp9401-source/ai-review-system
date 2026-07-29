from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import hashlib
import json
from typing import Any
import uuid
from zoneinfo import ZoneInfo

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.agent2.tool_calling.assembly import TrustedContextRequest
from app.agent2.tool_calling.context import (
    CANARY_STATE_NAMESPACE,
    TrustedClearPending,
    TrustedRecentMessage,
    TrustedRecentOperation,
    TrustedReportItem,
    TrustedReportSnapshot,
)
from app.agent2.tool_calling.registry import ToolDefinition
from app.agent2.tool_calling.validation import DateResolution
from app.agent2.memory import (
    PreferredSalutationValue,
    validate_personal_memory_value,
)
from app.agent2.memory.postgres import (
    PersonalMemoryAuditRecord,
    PersonalMemoryRecord,
)
from app.agent2.personal_memory_reply import (
    strip_server_rendered_salutations,
)
from app.agent2.typed_daily_executor import build_typed_daily_snapshot
from app.db import Base
from app.models import Agent2DailyCommandReceipt, DailyReport, User, WebhookEvent


_RECENT_MESSAGE_MAX_AGE = timedelta(hours=2)
_RECENT_OPERATION_MAX_AGE = timedelta(hours=2)


class ToolCallCanaryClearPending(Base):
    __tablename__ = "agent2_tool_call_clear_pendings"
    __table_args__ = (
        CheckConstraint(
            "namespace = 'agent2.tool_calling.canary.v1'",
            name="agent2_tool_call_clear_pending_namespace_check",
        ),
        CheckConstraint(
            "report_version >= 0",
            name="agent2_tool_call_clear_pending_version_check",
        ),
        Index(
            "agent2_tool_call_clear_pending_scope_idx",
            "tenant_id",
            "user_id",
            "conversation_id",
            "expires_at",
        ),
    )

    pending_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    namespace: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default=CANARY_STATE_NAMESPACE,
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    conversation_id: Mapped[str] = mapped_column(String(256), nullable=False)
    report_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
    )
    report_version: Mapped[int] = mapped_column(Integer, nullable=False)
    report_state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    target_date: Mapped[date] = mapped_column(Date, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    source_message_id: Mapped[str] = mapped_column(String(512), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class ToolCallCanaryReceipt(Base):
    __tablename__ = "agent2_tool_call_receipts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="agent2_tool_call_receipt_idempotency_key",
        ),
        UniqueConstraint(
            "tenant_id",
            "user_id",
            "conversation_id",
            "source_message_id",
            "tool_call_id",
            name="agent2_tool_call_receipt_call_identity_key",
        ),
        UniqueConstraint(
            "tenant_id",
            "operation_fingerprint",
            name="agent2_tool_call_receipt_operation_key",
        ),
        CheckConstraint(
            "status IN ('success', 'no_op', 'blocked', "
            "'clarification_required', 'failed')",
            name="agent2_tool_call_receipt_status_check",
        ),
        CheckConstraint(
            "execution_mode = 'canary_execute'",
            name="agent2_tool_call_receipt_mode_check",
        ),
        Index(
            "agent2_tool_call_receipt_scope_idx",
            "tenant_id",
            "user_id",
            "created_at",
        ),
    )

    receipt_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    conversation_id: Mapped[str] = mapped_column(String(256), nullable=False)
    source_message_id: Mapped[str] = mapped_column(String(512), nullable=False)
    tool_call_id: Mapped[str] = mapped_column(String(256), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    canonical_arguments_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_fingerprint: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    changed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    target_type: Mapped[str] = mapped_column(String(128), nullable=False)
    target_id: Mapped[str] = mapped_column(String(256), nullable=False)
    before_version: Mapped[int | None] = mapped_column(Integer)
    after_version: Mapped[int | None] = mapped_column(Integer)
    affected_item_ids: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
    )
    safe_user_facts: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
    )
    before_state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    after_state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    typed_receipt_ids: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
    )
    error_code: Mapped[str | None] = mapped_column(String(128))
    execution_mode: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="canary_execute",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class ProductionDateResolver:
    """Server date authority; the model-proposed date is comparison-only."""

    def resolve(
        self,
        *,
        expression: str,
        proposed_date: date,
        now: datetime,
        timezone: str,
    ) -> DateResolution:
        from app.services.report_service import _resolve_date_from_text

        local_today = now.astimezone(ZoneInfo(timezone)).date()
        resolved = _resolve_date_from_text(
            "".join(str(expression or "").split()).casefold(),
            local_today,
        )
        if resolved is None:
            try:
                resolved = date.fromisoformat(str(expression).strip())
            except ValueError:
                return DateResolution(
                    None,
                    error_code="DATE_EXPRESSION_UNRESOLVED",
                )
        return DateResolution(
            resolved,
            candidate_matches=resolved == proposed_date,
        )


class ProductionContextStore:
    """Read-only trusted context adapter over the authenticated production session."""

    def __init__(
        self,
        session: Any,
        *,
        user: User,
        tenant_id: str,
        settings: object | None = None,
    ) -> None:
        self._session = session
        self._user = user
        self._tenant_id = tenant_id
        self._settings = settings

    async def load_report(
        self,
        request: TrustedContextRequest,
        report_date: date,
    ) -> TrustedReportSnapshot | None:
        self._assert_scope(request.tenant_id, request.user_id)
        return await self._load_snapshot(report_date, provenance="trusted_context")

    async def load_owned_report(
        self,
        *,
        tenant_id: str,
        user_id: uuid.UUID,
        report_date: date,
    ) -> TrustedReportSnapshot | None:
        self._assert_scope(tenant_id, user_id)
        return await self._load_snapshot(report_date, provenance="read_tool")

    async def load_active_clear_pendings(
        self,
        request: TrustedContextRequest,
        *,
        namespace: str,
    ) -> tuple[TrustedClearPending, ...]:
        self._assert_scope(request.tenant_id, request.user_id)
        if namespace != CANARY_STATE_NAMESPACE:
            return ()
        rows = list(
            (
                await self._session.scalars(
                    select(ToolCallCanaryClearPending).where(
                        ToolCallCanaryClearPending.namespace
                        == CANARY_STATE_NAMESPACE,
                        ToolCallCanaryClearPending.tenant_id
                        == request.tenant_id,
                        ToolCallCanaryClearPending.user_id
                        == str(request.user_id),
                        ToolCallCanaryClearPending.conversation_id
                        == request.conversation_id,
                        ToolCallCanaryClearPending.consumed_at.is_(None),
                    )
                )
            ).all()
        )
        return tuple(
            TrustedClearPending(
                pending_id=row.pending_id,
                namespace=CANARY_STATE_NAMESPACE,
                tenant_id=row.tenant_id,
                user_id=uuid.UUID(row.user_id),
                conversation_id=row.conversation_id,
                report_id=row.report_id,
                report_version=row.report_version,
                target_date=row.target_date,
                expires_at=row.expires_at,
                source_message_id=row.source_message_id,
                consumed=False,
            )
            for row in rows
        )

    async def load_recent_messages(
        self,
        request: TrustedContextRequest,
        *,
        namespace: str,
        limit: int,
    ) -> tuple[TrustedRecentMessage, ...]:
        self._assert_scope(request.tenant_id, request.user_id)
        if namespace != CANARY_STATE_NAMESPACE or limit <= 0:
            return ()
        rows = list(
            (
                await self._session.scalars(
                    select(WebhookEvent)
                    .where(
                        WebhookEvent.dingtalk_user_id
                        == self._user.dingtalk_user_id,
                        WebhookEvent.status == "processed",
                        WebhookEvent.idempotency_key
                        != request.source_message_id,
                        WebhookEvent.received_at
                        >= request.server_now - _RECENT_MESSAGE_MAX_AGE,
                        WebhookEvent.payload["conversationId"].astext
                        == request.conversation_id,
                    )
                    .order_by(WebhookEvent.received_at.desc())
                    .limit(limit)
                )
            ).all()
        )
        messages: list[TrustedRecentMessage] = []
        salutations = await _server_rendered_salutations(
            self._session,
            tenant_id=request.tenant_id,
            user_id=request.user_id,
            now=request.server_now,
        )
        for row in reversed(rows):
            if not _event_matches_request(
                row,
                request=request,
                dingtalk_user_id=self._user.dingtalk_user_id,
            ):
                continue
            user_content = _text_content(row.payload)
            if user_content:
                messages.append(
                    TrustedRecentMessage(
                        role="user",
                        content=user_content,
                        source_message_id=row.idempotency_key,
                    )
                )
            assistant_content = _text_content(
                row.response_payload,
                max_length=None,
            )
            if assistant_content:
                assistant_content = strip_server_rendered_salutations(
                    content=assistant_content,
                    salutations=salutations,
                )[:4000]
            if assistant_content:
                messages.append(
                    TrustedRecentMessage(
                        role="assistant",
                        content=assistant_content,
                        source_message_id=(
                            f"{row.idempotency_key}:assistant"
                        ),
                    )
                )
        return tuple(messages[-limit:])

    async def load_recent_operations(
        self,
        request: TrustedContextRequest,
        *,
        namespace: str,
        limit: int,
    ) -> tuple[TrustedRecentOperation, ...]:
        self._assert_scope(request.tenant_id, request.user_id)
        if namespace != CANARY_STATE_NAMESPACE or limit <= 0:
            return ()
        rows = list(
            (
                await self._session.scalars(
                    select(ToolCallCanaryReceipt)
                    .where(
                        ToolCallCanaryReceipt.tenant_id
                        == request.tenant_id,
                        ToolCallCanaryReceipt.user_id
                        == str(request.user_id),
                        ToolCallCanaryReceipt.conversation_id
                        == request.conversation_id,
                        ToolCallCanaryReceipt.source_message_id
                        != request.source_message_id,
                        ToolCallCanaryReceipt.created_at
                        >= request.server_now - _RECENT_OPERATION_MAX_AGE,
                    )
                    .order_by(ToolCallCanaryReceipt.created_at.desc())
                    .limit(limit)
                )
            ).all()
        )
        return tuple(
            TrustedRecentOperation(
                tenant_id=row.tenant_id,
                user_id=uuid.UUID(row.user_id),
                conversation_id=row.conversation_id,
                source_message_id=row.source_message_id,
                tool_call_id=row.tool_call_id,
                tool_name=row.tool_name,
                status=row.status,
                changed=row.changed,
                target_type=row.target_type,
                target_id=row.target_id,
                before_version=row.before_version,
                after_version=row.after_version,
                affected_item_ids=tuple(row.affected_item_ids or ()),
                occurred_at=row.created_at,
            )
            for row in reversed(rows)
        )

    async def permission_allowed(
        self,
        request: TrustedContextRequest,
        definition: ToolDefinition,
    ) -> bool:
        if (
            definition.permission_policy
            == "authenticated_tenant_daily_read"
        ):
            return self._cross_user_daily_read_allowed(request)
        if (
            definition.permission_policy
            == "authenticated_tenant_performance_read"
        ):
            return self._performance_read_allowed(request)
        return (
            request.tenant_id == self._tenant_id
            and request.user_id == self._user.id
            and bool(self._user.active)
        )

    async def gate_allowed(
        self,
        request: TrustedContextRequest,
        definition: ToolDefinition,
    ) -> bool:
        if (
            definition.permission_policy
            == "authenticated_tenant_daily_read"
        ):
            return self._cross_user_daily_read_allowed(request)
        if (
            definition.permission_policy
            == "authenticated_tenant_performance_read"
        ):
            return self._performance_read_allowed(request)
        return (
            request.tenant_id == self._tenant_id
            and request.user_id == self._user.id
            and bool(self._user.active)
        )

    def _cross_user_daily_read_allowed(
        self,
        request: TrustedContextRequest,
    ) -> bool:
        return bool(
            request.tenant_id == self._tenant_id
            and request.user_id == self._user.id
            and self._user.active
            and self._settings is not None
            and getattr(
                self._settings,
                "legal_daily_dashboard_enabled",
                False,
            )
            and getattr(
                self._settings,
                "agent2_cross_user_daily_read_enabled",
                False,
            )
            and str(
                getattr(
                    self._settings,
                    "legal_daily_dashboard_tenant_id",
                    "",
                )
                or ""
            ).strip()
        )

    def _performance_read_allowed(
        self,
        request: TrustedContextRequest,
    ) -> bool:
        return bool(
            request.tenant_id == self._tenant_id
            and request.user_id == self._user.id
            and self._user.active
            and self._settings is not None
            and getattr(
                self._settings,
                "agent2_performance_tool_enabled",
                False,
            )
            and getattr(
                self._settings,
                "legal_ops_data_intake_enabled",
                False,
            )
            and getattr(
                self._settings,
                "agent2_performance_knowledge_enabled",
                False,
            )
            and str(
                getattr(
                    self._settings,
                    "legal_ops_live_tenant_id",
                    "",
                )
                or ""
            ).strip()
            == request.tenant_id
        )

    async def _load_snapshot(
        self,
        report_date: date,
        *,
        provenance: str,
    ) -> TrustedReportSnapshot | None:
        report = await self._session.scalar(
            select(DailyReport).where(
                DailyReport.user_id == self._user.id,
                DailyReport.report_date == report_date,
            )
        )
        if report is None:
            return None
        return trusted_snapshot_from_report(
            user=self._user,
            tenant_id=self._tenant_id,
            report_date=report_date,
            report=report,
            provenance=provenance,
        )

    def _assert_scope(self, tenant_id: str, user_id: uuid.UUID) -> None:
        if tenant_id != self._tenant_id or user_id != self._user.id:
            raise ValueError("production context scope mismatch")


def _event_matches_request(
    event: WebhookEvent,
    *,
    request: TrustedContextRequest,
    dingtalk_user_id: str,
) -> bool:
    payload = event.payload if isinstance(event.payload, dict) else {}
    return (
        event.dingtalk_user_id == dingtalk_user_id
        and event.status == "processed"
        and event.idempotency_key != request.source_message_id
        and str(payload.get("conversationId") or "")
        == request.conversation_id
        and event.received_at >= request.server_now - _RECENT_MESSAGE_MAX_AGE
    )


def _text_content(
    payload: Any,
    *,
    max_length: int | None = 4000,
) -> str:
    if not isinstance(payload, dict):
        return ""
    text = payload.get("text")
    if isinstance(text, dict):
        value = text.get("content") or text.get("text") or ""
    elif isinstance(text, str):
        value = text
    else:
        value = payload.get("content") or payload.get("message") or ""
    content = str(value).strip()
    return content if max_length is None else content[:max_length]


async def _server_rendered_salutations(
    session: Any,
    *,
    tenant_id: str,
    user_id: uuid.UUID,
    now: datetime,
) -> tuple[str, ...]:
    current_values = list(
        (
            await session.scalars(
                select(PersonalMemoryRecord.value_json).where(
                    PersonalMemoryRecord.tenant_id == tenant_id,
                    PersonalMemoryRecord.user_id == user_id,
                    PersonalMemoryRecord.memory_key
                    == "response.preferred_salutation",
                )
            )
        ).all()
    )
    audit_rows = list(
        (
            await session.execute(
                select(
                    PersonalMemoryAuditRecord.before_json,
                    PersonalMemoryAuditRecord.after_json,
                ).where(
                    PersonalMemoryAuditRecord.tenant_id == tenant_id,
                    PersonalMemoryAuditRecord.user_id == user_id,
                    PersonalMemoryAuditRecord.memory_key
                    == "response.preferred_salutation",
                    PersonalMemoryAuditRecord.occurred_at
                    >= now - _RECENT_MESSAGE_MAX_AGE,
                )
            )
        ).all()
    )
    raw_values: list[Any] = list(current_values)
    for before, after in audit_rows:
        for payload in (before, after):
            if isinstance(payload, Mapping):
                raw_values.append(payload.get("value"))

    salutations: set[str] = set()
    for raw_value in raw_values:
        try:
            validated = validate_personal_memory_value(
                "response_preference",
                "response.preferred_salutation",
                raw_value,
            )
        except ValueError:
            continue
        if isinstance(validated, PreferredSalutationValue):
            salutations.add(validated.salutation)
    return tuple(sorted(salutations, key=lambda value: (-len(value), value)))


@dataclass(frozen=True)
class ProductionStateSnapshot:
    """Canonical business and Tool-Call Pending state for one authenticated user."""

    canonical_json: str
    canonical_hash: str

    def payload(self) -> dict[str, Any]:
        return json.loads(self.canonical_json)


async def capture_production_state(
    session: Any,
    *,
    tenant_id: str,
    user_id: uuid.UUID,
    conversation_id: str,
) -> ProductionStateSnapshot:
    reports = list(
        (
            await session.scalars(
                select(DailyReport)
                .where(DailyReport.user_id == user_id)
                .order_by(DailyReport.report_date, DailyReport.id)
            )
        ).all()
    )
    pendings = list(
        (
            await session.scalars(
                select(ToolCallCanaryClearPending)
                .where(
                    ToolCallCanaryClearPending.namespace
                    == CANARY_STATE_NAMESPACE,
                    ToolCallCanaryClearPending.tenant_id == tenant_id,
                    ToolCallCanaryClearPending.user_id == str(user_id),
                    ToolCallCanaryClearPending.conversation_id
                    == conversation_id,
                )
                .order_by(ToolCallCanaryClearPending.pending_id)
            )
        ).all()
    )
    personal_memories = list(
        (
            await session.scalars(
                select(PersonalMemoryRecord)
                .where(
                    PersonalMemoryRecord.tenant_id == tenant_id,
                    PersonalMemoryRecord.user_id == user_id,
                )
                .order_by(
                    PersonalMemoryRecord.memory_key,
                    PersonalMemoryRecord.memory_id,
                )
            )
        ).all()
    )
    personal_memory_audits = list(
        (
            await session.scalars(
                select(PersonalMemoryAuditRecord)
                .where(
                    PersonalMemoryAuditRecord.tenant_id == tenant_id,
                    PersonalMemoryAuditRecord.user_id == user_id,
                )
                .order_by(
                    PersonalMemoryAuditRecord.occurred_at,
                    PersonalMemoryAuditRecord.audit_id,
                )
            )
        ).all()
    )
    payload = {
        "tenant_id": tenant_id,
        "user_id": str(user_id),
        "conversation_id": conversation_id,
        "daily_reports": [
            {
                "report_id": str(report.id),
                "report_date": report.report_date.isoformat(),
                "today_work": list(report.today_work or ()),
                "problems": list(report.problems or ()),
                "tomorrow_plan": list(report.tomorrow_plan or ()),
                "status": report.status,
                "confirmation_type": report.confirmation_type,
                "confirmed_by_user": bool(report.confirmed_by_user),
                "section_status": report.section_status or {},
            }
            for report in reports
        ],
        "clear_pendings": [
            {
                "pending_id": str(pending.pending_id),
                "report_id": str(pending.report_id),
                "report_version": pending.report_version,
                "report_state_hash": pending.report_state_hash,
                "target_date": pending.target_date.isoformat(),
                "source_message_id": pending.source_message_id,
                "expires_at": pending.expires_at.isoformat(),
                "consumed_at": (
                    pending.consumed_at.isoformat()
                    if pending.consumed_at is not None
                    else None
                ),
            }
            for pending in pendings
        ],
        "personal_memories": [
            {
                "memory_id": str(memory.memory_id),
                "tenant_id": memory.tenant_id,
                "user_id": str(memory.user_id),
                "memory_type": memory.memory_type,
                "memory_key": memory.memory_key,
                "value": memory.value_json,
                "source_kind": memory.source_kind,
                "source_message_id": memory.source_message_id,
                "status": memory.status,
                "version": memory.version,
                "expires_at": (
                    memory.expires_at.astimezone(UTC).isoformat()
                    if memory.expires_at is not None
                    else None
                ),
                "created_at": memory.created_at.astimezone(UTC).isoformat(),
                "updated_at": memory.updated_at.astimezone(UTC).isoformat(),
            }
            for memory in personal_memories
        ],
        "personal_memory_audits": [
            {
                "audit_id": str(audit.audit_id),
                "memory_id": str(audit.memory_id),
                "conversation_id": audit.conversation_id,
                "source_message_id": audit.source_message_id,
                "tool_call_id": audit.tool_call_id,
                "tool_name": audit.tool_name,
                "memory_key": audit.memory_key,
                "action": audit.action,
                "before": audit.before_json,
                "after": audit.after_json,
                "idempotency_key": audit.idempotency_key,
                "occurred_at": audit.occurred_at.astimezone(UTC).isoformat(),
            }
            for audit in personal_memory_audits
        ],
    }
    canonical_json = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return ProductionStateSnapshot(
        canonical_json=canonical_json,
        canonical_hash=hashlib.sha256(
            canonical_json.encode("utf-8")
        ).hexdigest(),
    )


async def load_tool_call_receipt_by_call(
    session: Any,
    *,
    tenant_id: str,
    user_id: str,
    conversation_id: str,
    source_message_id: str,
    tool_call_id: str,
) -> ToolCallCanaryReceipt | None:
    return await session.scalar(
        select(ToolCallCanaryReceipt).where(
            ToolCallCanaryReceipt.tenant_id == tenant_id,
            ToolCallCanaryReceipt.user_id == user_id,
            ToolCallCanaryReceipt.conversation_id == conversation_id,
            ToolCallCanaryReceipt.source_message_id == source_message_id,
            ToolCallCanaryReceipt.tool_call_id == tool_call_id,
        )
    )


async def load_tool_call_receipt_by_operation(
    session: Any,
    *,
    tenant_id: str,
    operation_fingerprint: str,
) -> ToolCallCanaryReceipt | None:
    return await session.scalar(
        select(ToolCallCanaryReceipt).where(
            ToolCallCanaryReceipt.tenant_id == tenant_id,
            ToolCallCanaryReceipt.operation_fingerprint
            == operation_fingerprint,
        )
    )


async def load_typed_receipts(
    session: Any,
    *,
    tenant_id: str,
    receipt_ids: tuple[str, ...],
) -> tuple[Agent2DailyCommandReceipt, ...]:
    if not receipt_ids:
        return ()
    parsed_ids = tuple(uuid.UUID(value) for value in receipt_ids)
    rows = list(
        (
            await session.scalars(
                select(Agent2DailyCommandReceipt).where(
                    Agent2DailyCommandReceipt.tenant_id == tenant_id,
                    Agent2DailyCommandReceipt.receipt_id.in_(parsed_ids),
                )
            )
        ).all()
    )
    return tuple(rows)


def trusted_snapshot_from_report(
    *,
    user: User,
    tenant_id: str,
    report_date: date,
    report: DailyReport,
    provenance: str = "trusted_context",
) -> TrustedReportSnapshot:
    typed = build_typed_daily_snapshot(
        user=user,
        report_date=report_date,
        report=report,
    )
    items: list[TrustedReportItem] = []
    for field in ("today_work", "problems", "tomorrow_plan"):
        values = getattr(typed, field)
        item_ids = typed.item_ids.get(field, ())
        items.extend(
            TrustedReportItem(
                item_id=item_id,
                field=field,
                content=value,
                report_id=typed.report_id,
                report_version=typed.version,
                provenance=provenance,
            )
            for item_id, value in zip(item_ids, values, strict=True)
        )
    return TrustedReportSnapshot(
        report_id=typed.report_id,
        tenant_id=tenant_id,
        owner_user_id=user.id,
        report_date=report_date,
        version=typed.version,
        status=typed.status,
        items=tuple(items),
        provenance=provenance,
    )


def report_state_hash(snapshot: TrustedReportSnapshot | None) -> str:
    payload = snapshot.safe_snapshot() if snapshot is not None else None
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
