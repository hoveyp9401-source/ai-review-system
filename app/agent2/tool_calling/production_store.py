from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
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
    and_,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.agent2.business.models import PeriodicReportCommandReceipt
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
from app.agent2.tool_calling.assembly import TrustedContextRequest
from app.agent2.tool_calling.context import (
    CANARY_STATE_NAMESPACE,
    TrustedClearPending,
    TrustedDailyWriteRetryCandidate,
    TrustedDateCorrectionReference,
    TrustedRecentMessage,
    TrustedRecentOperation,
    TrustedReportItem,
    TrustedReportReference,
    TrustedReportSnapshot,
)
from app.agent2.tool_calling.current_turn_source import CurrentTurnSource
from app.agent2.tool_calling.daily_write_retry import (
    daily_retry_candidate_id,
    validated_daily_retry_evidence,
)
from app.agent2.tool_calling.outbound_context import (
    OUTBOUND_CONTEXT_BACKEND_ACTION,
    trusted_recent_outbound_message,
)
from app.agent2.tool_calling.registry import TOOL_REGISTRY, ToolDefinition
from app.agent2.tool_calling.turn_batching import (
    CANARY_TRANSPORT_MARKER,
    INGRESS_META_KEY,
    is_recoverable_ingress_payload,
)
from app.agent2.tool_calling.validation import DateResolution
from app.agent2.typed_daily_executor import (
    TYPED_AUDIT_KEY,
    build_typed_daily_snapshot,
)
from app.agent2.weekly_plan_access import (
    WeeklyPlanAccessAction,
    WeeklyPlanAccessPolicy,
)
from app.db import Base
from app.models import (
    Agent2DailyCommandReceipt,
    DailyReport,
    ReportInteractionEvent,
    User,
    WebhookEvent,
)
from app.services.dingtalk import extract_voice_text

_RECENT_MESSAGE_MAX_AGE = timedelta(hours=2)
_SCHEDULED_OUTBOUND_MAX_AGE = timedelta(hours=16)
_RECENT_OPERATION_MAX_AGE = timedelta(hours=2)
_TURN_OBSERVATION_KEY = "_agent2_turn_observation_v1"


@dataclass(frozen=True)
class _TrustedTurnObservation:
    source_turn_id: str
    tool_receipt_count: int
    successful_pure_read: bool


def _strict_ascii_allowlist(raw: object) -> frozenset[str]:
    """Parse configuration without normalizing an unsafe identity value."""

    if not isinstance(raw, str) or not raw:
        return frozenset()
    values = raw.split(",")
    if any(
        not value
        or value != value.strip()
        or not value.isascii()
        or any(character.isspace() for character in value)
        for value in values
    ):
        # One malformed entry invalidates the whole list.  Partially applying an
        # identity allowlist would make the production scope hard to reason about.
        return frozenset({"__invalid_weekly_plan_allowlist__"})
    return frozenset(values)


def _trusted_report_reference_from_receipt(
    row: ToolCallCanaryReceipt,
) -> TrustedReportReference | None:
    if (
        row.status not in {"success", "no_op"}
        or row.target_type != "daily_report"
        or not row.target_id
        or row.after_version is None
    ):
        return None
    facts = row.safe_user_facts if isinstance(row.safe_user_facts, dict) else {}
    snapshot = facts.get("report_snapshot")
    if not isinstance(snapshot, dict):
        return None
    try:
        reference = TrustedReportReference(
            report_id=uuid.UUID(str(snapshot.get("report_id") or "")),
            report_date=date.fromisoformat(
                str(snapshot.get("report_date") or "")
            ),
            report_version=int(snapshot.get("version")),
            report_status=str(snapshot.get("status") or ""),
            report_state_sha256=(
                str(snapshot.get("report_state_sha256") or "")
                or None
            ),
        )
    except (TypeError, ValueError):
        return None
    if (
        str(reference.report_id) != row.target_id
        or reference.report_version != row.after_version
    ):
        return None
    return reference


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
        outbound_rows = list(
            (
                await self._session.scalars(
                    select(ReportInteractionEvent)
                    .where(
                        ReportInteractionEvent.user_id
                        == request.user_id,
                        or_(
                            ReportInteractionEvent.backend_action
                            == "daily_briefing_sent",
                            and_(
                                ReportInteractionEvent.backend_action
                                == OUTBOUND_CONTEXT_BACKEND_ACTION,
                                ReportInteractionEvent.llm_decision_json[
                                    "conversation_id"
                                ].astext
                                == request.conversation_id,
                            ),
                        ),
                        ReportInteractionEvent.created_at
                        >= request.server_now
                        - _SCHEDULED_OUTBOUND_MAX_AGE,
                    )
                    .order_by(
                        ReportInteractionEvent.created_at.desc()
                    )
                    .limit(max(1, min(limit, 3)))
                )
            ).all()
        )
        timed_messages: list[
            tuple[datetime, int, TrustedRecentMessage, bool]
        ] = []
        salutations = await _server_rendered_salutations(
            self._session,
            tenant_id=request.tenant_id,
            user_id=request.user_id,
            now=request.server_now,
        )
        scoped_rows = tuple(
            row
            for row in reversed(rows)
            if _event_matches_request(
                row,
                request=request,
                dingtalk_user_id=self._user.dingtalk_user_id,
            )
        )
        turn_observations = {
            row.idempotency_key: observation
            for row in scoped_rows
            if (
                observation := _trusted_turn_observation(row)
            )
            is not None
        }
        leader_observations = {
            str(row.id): observation
            for row in scoped_rows
            if (
                observation := turn_observations.get(
                    row.idempotency_key
                )
            )
            is not None
        }
        source_turn_ids = {
            row.idempotency_key: source_turn_id
            for row in scoped_rows
            if (
                source_turn_id := _trusted_event_source_turn_id(
                    row,
                    observation=turn_observations.get(
                        row.idempotency_key
                    ),
                    leader_observations=leader_observations,
                )
            )
            is not None
        }
        verified_read_sources = await _verified_pure_read_source_ids(
            self._session,
            request=request,
            observations=tuple(turn_observations.values()),
        )
        for row in scoped_rows:
            observation = turn_observations.get(
                row.idempotency_key
            )
            user_content = _text_content(row.payload)
            if user_content:
                timed_messages.append(
                    (
                        row.received_at,
                        0,
                        TrustedRecentMessage(
                            role="user",
                            content=user_content,
                            source_message_id=row.idempotency_key,
                            source_turn_id=source_turn_ids.get(
                                row.idempotency_key
                            ),
                        ),
                        False,
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
                timed_messages.append(
                    (
                        row.received_at,
                        1,
                        TrustedRecentMessage(
                            role="assistant",
                            content=assistant_content,
                            source_message_id=(
                                f"{row.idempotency_key}:assistant"
                            ),
                            source_turn_id=(
                                source_turn_ids.get(
                                    row.idempotency_key
                                )
                            ),
                            read_snapshot_verified=(
                                observation is not None
                                and observation.successful_pure_read
                                and observation.source_turn_id
                                in verified_read_sources
                            ),
                        ),
                        False,
                    )
                )
        for row in outbound_rows:
            if row.backend_action == OUTBOUND_CONTEXT_BACKEND_ACTION:
                message = trusted_recent_outbound_message(
                    row,
                    request=request,
                    dingtalk_user_id=self._user.dingtalk_user_id,
                )
                if message is None:
                    continue
                timed_messages.append(
                    (
                        row.created_at,
                        2,
                        message,
                        True,
                    )
                )
                continue
            content = str(getattr(row, "message_text", "") or "").strip()
            if not content:
                continue
            timed_messages.append(
                (
                    row.created_at,
                    2,
                    TrustedRecentMessage(
                        role="assistant",
                        content=content[:4000],
                        source_message_id=(
                            f"daily-briefing:{row.id}"
                        ),
                    ),
                    True,
                )
            )
        return _select_recent_messages_with_scheduled_outbound(
            timed_messages,
            limit=limit,
        )

    async def load_retryable_daily_write(
        self,
        request: TrustedContextRequest,
        *,
        namespace: str,
    ) -> TrustedDailyWriteRetryCandidate | None:
        """Recover one immediately preceding, server-observed failed write."""

        self._assert_scope(request.tenant_id, request.user_id)
        if (
            namespace != CANARY_STATE_NAMESPACE
            or request.conversation_kind != "direct"
        ):
            return None
        rows = list(
            (
                await self._session.scalars(
                    select(WebhookEvent)
                    .where(
                        WebhookEvent.dingtalk_user_id
                        == self._user.dingtalk_user_id,
                        WebhookEvent.idempotency_key
                        != request.source_message_id,
                        WebhookEvent.received_at
                        >= request.server_now - _RECENT_MESSAGE_MAX_AGE,
                        WebhookEvent.payload["conversationId"].astext
                        == request.conversation_id,
                    )
                    .order_by(WebhookEvent.received_at.desc())
                    .limit(8)
                )
            ).all()
        )
        scoped = sorted(
            (
                row
                for row in rows
                if _event_matches_request(
                    row,
                    request=request,
                    dingtalk_user_id=self._user.dingtalk_user_id,
                    require_processed=False,
                )
                and row.received_at <= request.server_now
            ),
            key=lambda row: (row.received_at, str(row.id)),
            reverse=True,
        )
        if not scoped:
            return None
        latest = scoped[0]
        if latest.status != "processed":
            return None
        latest_evidence = _retry_evidence_from_event(latest)
        if latest_evidence is None:
            return None

        origin = latest
        if latest_evidence["retry_of_candidate_id"]:
            origins = [
                row
                for row in scoped[1:]
                if (
                    (candidate := _retry_evidence_from_event(row))
                    is not None
                    and candidate["candidate_id"]
                    == latest_evidence["candidate_id"]
                    and not candidate["retry_of_candidate_id"]
                )
            ]
            if len(origins) != 1:
                return None
            origin = origins[0]

        source_text = _text_content(origin.payload, max_length=None)
        if not source_text:
            return None
        source = CurrentTurnSource((source_text,))
        if source.sha256 != latest_evidence["source_bundle_sha256"]:
            return None

        expected_candidate_id = daily_retry_candidate_id(
            tenant_id=request.tenant_id,
            user_id=str(request.user_id),
            conversation_id=request.conversation_id,
            origin_source_message_id=origin.idempotency_key,
            source_bundle_sha256=source.sha256,
            target_report_date=latest_evidence[
                "target_report_date"
            ],
            target_state_sha256=latest_evidence[
                "target_state_sha256"
            ],
        )
        if expected_candidate_id != latest_evidence["candidate_id"]:
            return None

        target_date = date.fromisoformat(
            latest_evidence["target_report_date"]
        )
        live = await self._load_snapshot(
            target_date,
            provenance="trusted_context",
        )
        if (
            (live is None) != latest_evidence["target_was_absent"]
            or report_state_hash(live)
            != latest_evidence["target_state_sha256"]
            or (
                live is not None
                and live.version != latest_evidence["target_version"]
            )
        ):
            return None

        return TrustedDailyWriteRetryCandidate(
            candidate_id=latest_evidence["candidate_id"],
            tenant_id=request.tenant_id,
            user_id=request.user_id,
            conversation_id=request.conversation_id,
            origin_source_message_id=origin.idempotency_key,
            origin_received_at=origin.received_at,
            source_messages=source.messages,
            source_bundle_sha256=source.sha256,
            target_date=target_date,
            target_was_absent=latest_evidence["target_was_absent"],
            target_version=latest_evidence["target_version"],
            target_state_sha256=latest_evidence[
                "target_state_sha256"
            ],
            failed_local_date=date.fromisoformat(
                latest_evidence["failed_local_date"]
            ),
            retry_chain_depth=latest_evidence["retry_chain_depth"],
        )

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
        operations: list[TrustedRecentOperation] = []
        for row in reversed(rows):
            operations.append(
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
                report_reference=_trusted_report_reference_from_receipt(row),
                occurred_at=row.created_at,
            )
            )
        return tuple(operations)

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
        if definition.permission_policy in {
            "authenticated_owner_weekly_plan_read",
            "authenticated_owner_weekly_plan_write",
        }:
            return self._weekly_plan_allowed(request, definition)
        if (
            definition.permission_policy
            == "authenticated_owner_current_weekly_report"
        ):
            return self._current_weekly_report_allowed(request)
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
        if definition.permission_policy in {
            "authenticated_owner_weekly_plan_read",
            "authenticated_owner_weekly_plan_write",
        }:
            return self._weekly_plan_allowed(request, definition)
        if (
            definition.permission_policy
            == "authenticated_owner_current_weekly_report"
        ):
            return self._current_weekly_report_allowed(request)
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

    def _weekly_plan_allowed(
        self,
        request: TrustedContextRequest,
        definition: ToolDefinition,
    ) -> bool:
        if (
            request.tenant_id != self._tenant_id
            or request.user_id != self._user.id
            or not bool(self._user.active)
            or self._settings is None
        ):
            return False
        policy = WeeklyPlanAccessPolicy(
            enabled=getattr(
                self._settings,
                "agent2_weekly_plan_enabled",
                False,
            ),
            write_enabled=getattr(
                self._settings,
                "agent2_weekly_plan_write_enabled",
                False,
            ),
            send_enabled=getattr(
                self._settings,
                "agent2_weekly_plan_send_enabled",
                False,
            ),
            tenant_allowlist=_strict_ascii_allowlist(
                getattr(
                    self._settings,
                    "agent2_weekly_plan_tenant_allowlist",
                    "",
                )
            ),
            user_allowlist=_strict_ascii_allowlist(
                getattr(
                    self._settings,
                    "agent2_weekly_plan_user_allowlist",
                    "",
                )
            ),
            send_user_allowlist=_strict_ascii_allowlist(
                getattr(
                    self._settings,
                    "agent2_weekly_plan_send_user_allowlist",
                    "",
                )
            ),
        )
        action = (
            WeeklyPlanAccessAction.WRITE
            if definition.permission_policy
            == "authenticated_owner_weekly_plan_write"
            else WeeklyPlanAccessAction.READ
        )
        return policy.decide(
            action=action,
            tenant_id=request.tenant_id,
            user_id=str(request.user_id),
            conversation_kind=request.conversation_kind,
        ).allowed

    def _current_weekly_report_allowed(
        self,
        request: TrustedContextRequest,
    ) -> bool:
        if (
            request.tenant_id != self._tenant_id
            or request.user_id != self._user.id
            or not bool(self._user.active)
            or request.conversation_kind != "direct"
            or self._settings is None
            or getattr(
                self._settings,
                "agent2_current_weekly_report_enabled",
                False,
            )
            is not True
        ):
            return False
        tenant_allowlist = _strict_ascii_allowlist(
            getattr(
                self._settings,
                "agent2_current_weekly_report_tenant_allowlist",
                "",
            )
        )
        user_allowlist = _strict_ascii_allowlist(
            getattr(
                self._settings,
                "agent2_current_weekly_report_user_allowlist",
                "",
            )
        )
        if len(tenant_allowlist) != 1 or len(user_allowlist) != 1:
            return False
        return bool(
            request.tenant_id in tenant_allowlist
            and str(request.user_id) in user_allowlist
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
    require_processed: bool = True,
) -> bool:
    payload = event.payload if isinstance(event.payload, dict) else {}
    return (
        event.dingtalk_user_id == dingtalk_user_id
        and (not require_processed or event.status == "processed")
        and event.idempotency_key != request.source_message_id
        and str(payload.get("conversationId") or "")
        == request.conversation_id
        and event.received_at >= request.server_now - _RECENT_MESSAGE_MAX_AGE
    )


def _trusted_turn_observation(
    event: WebhookEvent,
) -> _TrustedTurnObservation | None:
    response = (
        event.response_payload
        if isinstance(event.response_payload, Mapping)
        else {}
    )
    raw = response.get(_TURN_OBSERVATION_KEY)
    if not isinstance(raw, Mapping):
        return None
    source_turn_id = raw.get("source_turn_id")
    receipt_count = raw.get("tool_receipt_count")
    successful_pure_read = raw.get("successful_pure_read")
    if (
        raw.get("schema_version")
        != "agent2.turn.observation.v1"
        or raw.get("message_processing_status") != "consumed"
        or raw.get("reply_status") != "formed"
        or raw.get("model_result_status") != "success"
        or not isinstance(source_turn_id, str)
        or not source_turn_id
        or source_turn_id != source_turn_id.strip()
        or len(source_turn_id) > 512
        or type(receipt_count) is not int
        or receipt_count < 0
        or type(successful_pure_read) is not bool
        or (successful_pure_read and receipt_count == 0)
        or (
            successful_pure_read
            and raw.get("business_write_committed") is not False
        )
    ):
        return None
    return _TrustedTurnObservation(
        source_turn_id=source_turn_id,
        tool_receipt_count=receipt_count,
        successful_pure_read=successful_pure_read,
    )


def _trusted_event_source_turn_id(
    event: WebhookEvent,
    *,
    observation: _TrustedTurnObservation | None,
    leader_observations: Mapping[str, _TrustedTurnObservation],
) -> str | None:
    """Recover only server-observed leader or bound follower turn IDs."""

    if observation is not None:
        return observation.source_turn_id
    response = (
        event.response_payload
        if isinstance(event.response_payload, Mapping)
        else {}
    )
    marker = response.get(CANARY_TRANSPORT_MARKER)
    if not isinstance(marker, Mapping):
        return None
    batch_id = marker.get("batch_id")
    leader_event_id = marker.get("leader_event_id")
    if (
        marker.get("delivery") != "batched_follower"
        or not isinstance(batch_id, str)
        or not batch_id
        or batch_id != batch_id.strip()
        or len(batch_id) > 512
        or not isinstance(leader_event_id, str)
        or not leader_event_id
        or leader_event_id != leader_event_id.strip()
        or leader_event_id == str(event.id)
    ):
        return None
    leader_observation = leader_observations.get(leader_event_id)
    if (
        leader_observation is None
        or leader_observation.source_turn_id != batch_id
    ):
        return None
    return batch_id


async def _verified_pure_read_source_ids(
    session: Any,
    *,
    request: TrustedContextRequest,
    observations: tuple[_TrustedTurnObservation, ...],
) -> frozenset[str]:
    expected_counts = {
        observation.source_turn_id: observation.tool_receipt_count
        for observation in observations
        if observation.successful_pure_read
    }
    if not expected_counts:
        return frozenset()
    rows = list(
        (
            await session.scalars(
                select(ToolCallCanaryReceipt).where(
                    ToolCallCanaryReceipt.tenant_id
                    == request.tenant_id,
                    ToolCallCanaryReceipt.user_id
                    == str(request.user_id),
                    ToolCallCanaryReceipt.conversation_id
                    == request.conversation_id,
                    ToolCallCanaryReceipt.source_message_id.in_(
                        tuple(expected_counts)
                    ),
                    ToolCallCanaryReceipt.created_at
                    >= request.server_now - _RECENT_OPERATION_MAX_AGE,
                )
            )
        ).all()
    )
    by_source: dict[str, list[ToolCallCanaryReceipt]] = {}
    for row in rows:
        by_source.setdefault(row.source_message_id, []).append(row)

    verified: set[str] = set()
    for source_turn_id, expected_count in expected_counts.items():
        source_rows = by_source.get(source_turn_id, [])
        if len(source_rows) != expected_count:
            continue
        if all(
            row.status in {"success", "no_op"}
            and (
                definition := TOOL_REGISTRY.get(row.tool_name)
            )
            is not None
            and definition.read_or_write == "read"
            for row in source_rows
        ):
            verified.add(source_turn_id)
    return frozenset(verified)


def _retry_evidence_from_event(
    event: WebhookEvent,
) -> dict[str, Any] | None:
    response = (
        event.response_payload
        if isinstance(event.response_payload, Mapping)
        else {}
    )
    observation = response.get(_TURN_OBSERVATION_KEY)
    if not isinstance(observation, Mapping):
        return None
    if (
        observation.get("message_processing_status") != "consumed"
        or observation.get("business_write_committed") is not False
        or observation.get("tool_success_count") != 0
        or observation.get("tool_no_op_count") != 0
    ):
        return None
    continuation = validated_daily_retry_evidence(
        observation.get("daily_write_retry_continuation")
    )
    if continuation is not None:
        return continuation
    blocks = observation.get("pre_execution_blocks")
    if (
        observation.get("tool_clarification_count") != 0
        or observation.get("tool_failure_count") != 0
        or observation.get("tool_blocked_count") != 1
        or not isinstance(blocks, list)
        or len(blocks) != 1
        or not isinstance(blocks[0], Mapping)
        or blocks[0].get("tool_name") != "add_daily_items"
    ):
        return None
    return validated_daily_retry_evidence(
        blocks[0].get("retry_candidate")
    )


def _select_recent_messages_with_scheduled_outbound(
    timed_messages: list[
        tuple[datetime, int, TrustedRecentMessage, bool]
    ],
    *,
    limit: int,
) -> tuple[TrustedRecentMessage, ...]:
    if limit <= 0 or not timed_messages:
        return ()
    ordered = sorted(timed_messages, key=lambda item: (item[0], item[1]))
    selected = ordered[-limit:]
    outbound = [item for item in ordered if item[3]]
    if outbound and outbound[-1] not in selected:
        if limit == 1:
            selected = [outbound[-1]]
        else:
            selected = sorted(
                [outbound[-1], *selected[-(limit - 1) :]],
                key=lambda item: (item[0], item[1]),
            )
    return tuple(item[2] for item in selected)


def _text_content(
    payload: Any,
    *,
    max_length: int | None = 4000,
) -> str:
    if not isinstance(payload, dict):
        return ""
    if is_recoverable_ingress_payload(payload):
        ingress_meta = payload.get(INGRESS_META_KEY)
        if isinstance(ingress_meta, dict):
            recovered = ingress_meta.get("text")
            if isinstance(recovered, str) and recovered.strip():
                content = recovered.strip()
                return content if max_length is None else content[:max_length]
    recognized_voice = extract_voice_text(payload)
    if recognized_voice:
        return (
            recognized_voice
            if max_length is None
            else recognized_voice[:max_length]
        )
    text = payload.get("text")
    if isinstance(text, dict):
        value = text.get("content") or text.get("text") or ""
    elif isinstance(text, str):
        value = text
    else:
        fallback = payload.get("content") or payload.get("message") or ""
        value = fallback if isinstance(fallback, str) else ""
    content = value.strip() if isinstance(value, str) else ""
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
    include_weekly_plan: bool = False,
    include_periodic_report: bool = False,
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
    weekly_plans: list[dict[str, Any]] = []
    if include_weekly_plan:
        from app.agent2.weekly_plan_store import weekly_plan_state_payload

        weekly_plans = await weekly_plan_state_payload(
            session,
            tenant_id=tenant_id,
            owner_user_id=str(user_id),
        )
    periodic_reports: list[dict[str, Any]] = []
    if include_periodic_report:
        from app.agent2.business.models import PeriodicReport

        rows = list(
            (
                await session.scalars(
                    select(PeriodicReport)
                    .where(
                        PeriodicReport.tenant_id == tenant_id,
                        PeriodicReport.owner_user_id == str(user_id),
                        PeriodicReport.report_type == "weekly",
                    )
                    .order_by(
                        PeriodicReport.period_key,
                        PeriodicReport.report_id,
                    )
                )
            ).all()
        )
        periodic_reports = [
            {
                "report_id": str(row.report_id),
                "report_type": str(row.report_type),
                "period_key": str(row.period_key),
                "sections": row.sections_json or {},
                "item_ids": row.item_ids_json or {},
                "status": str(row.status),
                "version": int(row.version),
                "submitted_at": (
                    row.submitted_at.astimezone(UTC).isoformat()
                    if row.submitted_at is not None
                    else None
                ),
            }
            for row in rows
        ]
    payload = {
        "tenant_id": tenant_id,
        "user_id": str(user_id),
        "conversation_id": conversation_id,
        "weekly_plans": weekly_plans,
        "periodic_reports": periodic_reports,
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
) -> tuple[Any, ...]:
    if not receipt_ids:
        return ()
    parsed_ids = tuple(uuid.UUID(value) for value in receipt_ids)
    daily_rows = list(
        (
            await session.scalars(
                select(Agent2DailyCommandReceipt).where(
                    Agent2DailyCommandReceipt.tenant_id == tenant_id,
                    Agent2DailyCommandReceipt.receipt_id.in_(parsed_ids),
                )
            )
        ).all()
    )
    found_ids = {row.receipt_id for row in daily_rows}
    missing_ids = tuple(value for value in parsed_ids if value not in found_ids)
    periodic_rows = (
        list(
            (
                await session.scalars(
                    select(PeriodicReportCommandReceipt).where(
                        PeriodicReportCommandReceipt.tenant_id == tenant_id,
                        PeriodicReportCommandReceipt.receipt_id.in_(missing_ids),
                    )
                )
            ).all()
        )
        if missing_ids
        else []
    )
    by_id = {
        row.receipt_id: row for row in (*daily_rows, *periodic_rows)
    }
    return tuple(by_id[value] for value in parsed_ids if value in by_id)


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
        acknowledged_empty_fields=typed.acknowledged_empty_fields,
        date_correction_reference=_trusted_date_correction_reference(
            report=report,
            report_id=typed.report_id,
            report_date=report_date,
        ),
        provenance=provenance,
    )


def _trusted_date_correction_reference(
    *,
    report: DailyReport,
    report_id: uuid.UUID,
    report_date: date,
) -> TrustedDateCorrectionReference | None:
    section_status = (
        report.section_status
        if isinstance(report.section_status, Mapping)
        else {}
    )
    raw_audits = section_status.get(TYPED_AUDIT_KEY)
    audits = raw_audits if isinstance(raw_audits, list) else []
    for raw in reversed(audits):
        if (
            not isinstance(raw, Mapping)
            or raw.get("command_type") != "correct_report_date"
            or raw.get("result") != "executed"
            or raw.get("actual_write") is not True
        ):
            continue
        try:
            reference = TrustedDateCorrectionReference(
                report_id=uuid.UUID(str(raw.get("report_id") or "")),
                source_message_id=str(raw.get("source_message_id") or ""),
                source_report_date=date.fromisoformat(
                    str(raw.get("source_report_date") or "")
                ),
                target_report_date=date.fromisoformat(
                    str(raw.get("target_report_date") or "")
                ),
            )
        except (TypeError, ValueError):
            continue
        if (
            reference.report_id == report_id
            and reference.target_report_date == report_date
        ):
            return reference
    return None


def report_state_hash(snapshot: TrustedReportSnapshot | None) -> str:
    payload = snapshot.safe_snapshot() if snapshot is not None else None
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
