from __future__ import annotations

from datetime import datetime
from typing import Any, Awaitable, Callable, Mapping
from uuid import UUID

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID

from app.agent2.admission_contracts import InformationPending
from app.agent2.business.models import TravelIntent
from app.agent2.information_pending import (
    InformationPendingContext,
    InformationPendingResolution,
    InformationPendingSettlement,
    InformationPendingValidation,
)
from app.agent2.information_pending_runtime import (
    InformationContinuationPreprocessRequest,
    InformationContinuationPreprocessResult,
    InformationPendingContinuationCoordinator,
)


_metadata = MetaData()
_information_pendings = Table(
    "agent2_information_pendings",
    _metadata,
    Column("pending_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("pending_type", String(32), nullable=False),
    Column("trace_id", PG_UUID(as_uuid=True), nullable=False),
    Column("decision_id", PG_UUID(as_uuid=True), nullable=False),
    Column("tenant_id", String(128), nullable=False),
    Column("user_id", String(128), nullable=False),
    Column("conversation_id", String(256), nullable=False),
    Column("source_turn_id", String(256), nullable=False),
    Column("source_message_id", String(256), nullable=False),
    Column("segment_id", String(256), nullable=False),
    Column("segment_text_sha256", String(64), nullable=False),
    Column("segment_start_offset", Integer, nullable=False),
    Column("segment_end_offset", Integer, nullable=False),
    Column("domain", String(32), nullable=False),
    Column("operation", String(128), nullable=False),
    Column("object_type", String(128)),
    Column("object_stable_id", String(512)),
    Column("object_version", Integer),
    Column("object_label", String(512)),
    Column("expected_conversation_state_version", Integer, nullable=False),
    Column("missing_fields_json", JSONB, nullable=False),
    Column("question_snapshot_json", JSONB, nullable=False),
    Column("acceptable_answer_forms_json", JSONB, nullable=False),
    Column("pending_status", String(32), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("ttl_seconds", Integer, nullable=False),
    Column("consumed_at", DateTime(timezone=True)),
    Column("consumed_by_trace_id", PG_UUID(as_uuid=True)),
    Column("invalidation_reason", String(2048), nullable=False),
    Column("business_write_allowed", Boolean, nullable=False),
    Column("idempotency_key", String(512), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
_traces = Table(
    "agent2_semantic_admission_traces",
    _metadata,
    Column("trace_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("user_id", String(128), nullable=False),
    Column("conversation_id", String(256), nullable=False),
    Column("source_message_id", String(256), nullable=False),
)


class InformationPendingPersistenceConflict(RuntimeError):
    pass


class SqlInformationPendingRepository:
    """PostgreSQL source of truth for scoped Information Pending lifecycle."""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def load_scoped(
        self,
        context: InformationPendingContext,
    ) -> tuple[InformationPending, ...]:
        result = await self._session.execute(
            select(_information_pendings)
            .where(
                _information_pendings.c.tenant_id == context.tenant_id,
                _information_pendings.c.user_id == context.user_id,
                _information_pendings.c.conversation_id == context.conversation_id,
                _information_pendings.c.pending_status.in_(
                    ("active", "awaiting_input")
                ),
            )
            .order_by(_information_pendings.c.created_at.desc())
        )
        return tuple(_pending_from_row(dict(row)) for row in result.mappings().all())

    async def source_message_processed(
        self,
        context: InformationPendingContext,
    ) -> bool:
        result = await self._session.execute(
            select(_traces.c.trace_id)
            .where(
                _traces.c.tenant_id == context.tenant_id,
                _traces.c.user_id == context.user_id,
                _traces.c.conversation_id == context.conversation_id,
                _traces.c.source_message_id == context.source_message_id,
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def persist_resolution(
        self,
        *,
        resolution: InformationPendingResolution,
        context: InformationPendingContext,
    ) -> None:
        pending_after = resolution.pending_after
        if (
            pending_after is None
            or not resolution.pending_id
            or pending_after.pending_status in {"active", "awaiting_input"}
        ):
            return
        await self._transition(
            pending=pending_after,
            status=pending_after.pending_status,
            now=context.occurred_at,
            invalidation_reason=resolution.reason,
        )

    async def persist_settlement(
        self,
        *,
        pending: InformationPending,
        settlement: InformationPendingSettlement,
        settled_at: datetime,
    ) -> None:
        if settlement.status != "consumed":
            raise ValueError("consumed settlement required")
        result = await self._session.execute(
            update(_information_pendings)
            .where(
                _information_pendings.c.tenant_id == pending.tenant_id,
                _information_pendings.c.user_id == pending.user_id,
                _information_pendings.c.conversation_id == pending.conversation_id,
                _information_pendings.c.pending_id == _uuid(pending.pending_id),
                _information_pendings.c.expected_conversation_state_version
                == pending.expected_conversation_state_version,
                _information_pendings.c.pending_status.in_(
                    ("active", "awaiting_input")
                ),
            )
            .values(
                pending_status="consumed",
                consumed_at=_aware(settled_at),
                consumed_by_trace_id=_uuid(
                    settlement.pending_after.consumed_by_trace_id
                ),
                invalidation_reason="",
                updated_at=_aware(settled_at),
            )
        )
        if result.rowcount != 1:
            raise InformationPendingPersistenceConflict(
                "information pending changed before committed receipt settlement"
            )

    async def persist_failed_settlement(
        self,
        *,
        pending: InformationPending,
        reason: str,
        settled_at: datetime,
    ) -> None:
        await self._transition(
            pending=pending,
            status="conflicted",
            now=settled_at,
            invalidation_reason=reason or "fresh_admission_execution_failed",
        )

    async def _transition(
        self,
        *,
        pending: InformationPending,
        status: str,
        now: datetime,
        invalidation_reason: str,
    ) -> None:
        result = await self._session.execute(
            update(_information_pendings)
            .where(
                _information_pendings.c.tenant_id == pending.tenant_id,
                _information_pendings.c.user_id == pending.user_id,
                _information_pendings.c.conversation_id == pending.conversation_id,
                _information_pendings.c.pending_id == _uuid(pending.pending_id),
                _information_pendings.c.expected_conversation_state_version
                == pending.expected_conversation_state_version,
                _information_pendings.c.pending_status.in_(
                    ("active", "awaiting_input")
                ),
            )
            .values(
                pending_status=status,
                invalidation_reason=str(invalidation_reason or "")[:2048],
                updated_at=_aware(now),
            )
        )
        if result.rowcount != 1:
            raise InformationPendingPersistenceConflict(
                "information pending changed before lifecycle transition"
            )


PermissionChecker = Callable[[InformationPending], Awaitable[bool]]


class SqlInformationPendingValidator:
    """Revalidate live object and permission facts before fresh Admission."""

    def __init__(
        self,
        session: Any,
        *,
        tenant_id: str,
        user_id: str,
        permission_checker: PermissionChecker | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._user_id = user_id
        self._permission_checker = permission_checker

    async def validate(
        self,
        pending: InformationPending,
        context: InformationPendingContext,
    ) -> InformationPendingValidation:
        if (
            context.tenant_id != self._tenant_id
            or context.user_id != self._user_id
            or pending.tenant_id != self._tenant_id
            or pending.user_id != self._user_id
        ):
            return InformationPendingValidation(
                "permission_revoked", "information_pending_scope_changed"
            )
        if self._permission_checker is not None and not await self._permission_checker(
            pending
        ):
            return InformationPendingValidation(
                "permission_revoked", "information_pending_permission_revoked"
            )
        if (
            pending.domain != "travel"
            or pending.operation != "record_travel_event"
            or pending.missing_fields != ("travel_date",)
        ):
            return InformationPendingValidation(
                "operation_illegal", "unsupported_information_pending_operation"
            )
        object_ref = pending.object_ref
        if (
            not isinstance(object_ref, Mapping)
            or object_ref.get("object_type") != "travel_intent"
            or object_ref.get("version") is not None
        ):
            return InformationPendingValidation(
                "operation_illegal", "information_pending_object_contract_changed"
            )
        try:
            intent_id = _uuid(object_ref.get("stable_id"))
        except ValueError:
            return InformationPendingValidation(
                "operation_illegal", "information_pending_object_identity_invalid"
            )
        existing = await self._session.scalar(
            select(TravelIntent.travel_intent_id).where(
                TravelIntent.tenant_id == context.tenant_id,
                TravelIntent.travel_intent_id == intent_id,
            )
        )
        if existing is not None:
            return InformationPendingValidation(
                "version_conflict", "travel_intent_preallocated_id_already_exists"
            )
        return InformationPendingValidation("valid")


class SqlInformationPendingContinuationAdapter:
    """SQL-backed preprocessor/post-receipt seam used by Agent2TurnRuntime."""

    def __init__(
        self,
        *,
        coordinator: InformationPendingContinuationCoordinator | None = None,
        permission_checker: PermissionChecker | None = None,
    ) -> None:
        self._coordinator = coordinator or InformationPendingContinuationCoordinator()
        self._permission_checker = permission_checker

    async def preprocess(
        self,
        request: InformationContinuationPreprocessRequest,
    ) -> InformationContinuationPreprocessResult:
        repository = SqlInformationPendingRepository(request.session)
        validator = SqlInformationPendingValidator(
            request.session,
            tenant_id=request.tenant_id,
            user_id=request.user_id,
            permission_checker=self._permission_checker,
        )
        return await self._coordinator.preprocess(
            request,
            repository=repository,
            validator=validator,
        )

    async def settle(
        self,
        result: InformationContinuationPreprocessResult,
        *,
        outcome: Any,
        settled_trace_id: str,
        settled_at: datetime,
        session: Any,
    ) -> InformationPendingSettlement:
        return await self._coordinator.settle(
            result,
            repository=SqlInformationPendingRepository(session),
            outcome=outcome,
            settled_trace_id=settled_trace_id,
            settled_at=settled_at,
        )


def _pending_from_row(row: Mapping[str, Any]) -> InformationPending:
    object_ref = None
    if row.get("object_type") is not None:
        object_ref = {
            "object_type": str(row.get("object_type") or ""),
            "stable_id": str(row.get("object_stable_id") or ""),
            "version": row.get("object_version"),
        }
        if row.get("object_label") is not None:
            object_ref["label"] = str(row.get("object_label") or "")
    return InformationPending(
        pending_id=str(row["pending_id"]),
        pending_type=str(row.get("pending_type") or "information"),
        trace_id=str(row["trace_id"]),
        decision_id=str(row["decision_id"]),
        tenant_id=str(row["tenant_id"]),
        user_id=str(row["user_id"]),
        conversation_id=str(row["conversation_id"]),
        source_turn_id=str(row["source_turn_id"]),
        source_message_id=str(row["source_message_id"]),
        segment_id=str(row["segment_id"]),
        segment_text_sha256=str(row["segment_text_sha256"]),
        segment_start_offset=int(row["segment_start_offset"]),
        segment_end_offset=int(row["segment_end_offset"]),
        domain=str(row["domain"]),
        operation=str(row["operation"]),
        object_ref=object_ref,
        expected_conversation_state_version=int(
            row["expected_conversation_state_version"]
        ),
        missing_fields=tuple(str(item) for item in row["missing_fields_json"]),
        question_snapshot=dict(row.get("question_snapshot_json") or {}),
        acceptable_answer_forms=dict(
            row.get("acceptable_answer_forms_json") or {}
        ),
        pending_status=str(row["pending_status"]),
        created_at=_aware(row["created_at"]),
        expires_at=_aware(row["expires_at"]),
        consumed_by_trace_id=str(row.get("consumed_by_trace_id") or ""),
        business_write_allowed=bool(row.get("business_write_allowed", False)),
        idempotency_key=str(row["idempotency_key"]),
    )


def _uuid(value: Any) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value or ""))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("information pending UUID is invalid") from exc


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("information pending timestamp must be timezone-aware")
    return value
