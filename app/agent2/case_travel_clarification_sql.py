from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
import hashlib
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from app.agent2.business.composition import (
    BusinessActionResult,
    BusinessCompositionResult,
)
from app.agent2.business.contracts import (
    BusinessCommandContext,
    CreateTravelIntent,
)
from app.agent2.business.models import (
    Agent2Case,
    CaseTravelClarificationPendingRow,
)
from app.agent2.business.policy import BusinessEffectPolicy
from app.agent2.business.sql_executor import SqlBusinessExecutor
from app.agent2.case_table_rag import (
    DEFAULT_CASE_RAG_INDEX,
    find_case_location_hint_by_identity,
)
from app.agent2.case_travel_clarification import (
    CaseTravelClarificationOffer,
    CaseTravelClarificationPending,
    CaseTravelClarificationRuntime,
    CaseTravelClarificationScope,
    CaseTravelClarificationStore,
    CaseTravelClarificationTurnResult,
    CaseTravelIntentWriter,
    build_case_travel_clarification,
)
from app.agent2.conversation_state_store import SQLAlchemyConversationStateStore


ACTIVE_STATUSES = (
    "awaiting_confirmation",
    "awaiting_destination",
    "awaiting_city",
)


@dataclass(frozen=True)
class PersistedCaseTravelOffer:
    pending: CaseTravelClarificationPending
    question: str
    created: bool


@dataclass(frozen=True)
class SqlCaseTravelClarificationTurn:
    handled: bool
    reply: str
    runtime_result: CaseTravelClarificationTurnResult
    business_result: BusinessCompositionResult | None = None


class SqlCaseTravelClarificationStore(CaseTravelClarificationStore):
    def __init__(self, session: Any) -> None:
        self.session = session

    async def list_active(
        self, scope: CaseTravelClarificationScope
    ) -> tuple[CaseTravelClarificationPending, ...]:
        await self.session.execute(
            update(CaseTravelClarificationPendingRow)
            .where(
                CaseTravelClarificationPendingRow.tenant_id == scope.tenant_id,
                CaseTravelClarificationPendingRow.user_id == scope.user_id,
                CaseTravelClarificationPendingRow.conversation_id
                == scope.conversation_id,
                CaseTravelClarificationPendingRow.status.in_(ACTIVE_STATUSES),
                CaseTravelClarificationPendingRow.expires_at
                <= scope.occurred_at,
            )
            .values(status="expired", updated_at=scope.occurred_at)
        )
        result = await self.session.execute(
            select(CaseTravelClarificationPendingRow)
            .where(
                CaseTravelClarificationPendingRow.tenant_id == scope.tenant_id,
                CaseTravelClarificationPendingRow.user_id == scope.user_id,
                CaseTravelClarificationPendingRow.conversation_id
                == scope.conversation_id,
                CaseTravelClarificationPendingRow.status.in_(ACTIVE_STATUSES),
                CaseTravelClarificationPendingRow.expires_at > scope.occurred_at,
            )
            .order_by(CaseTravelClarificationPendingRow.created_at.desc())
            .with_for_update()
        )
        return tuple(_pending_from_row(row) for row in result.scalars().all())

    async def create(
        self,
        *,
        offer: CaseTravelClarificationOffer,
        scope: CaseTravelClarificationScope,
        ttl: timedelta = timedelta(hours=12),
    ) -> tuple[CaseTravelClarificationPending, bool]:
        key = _digest(
            "case-travel-pending",
            scope.tenant_id,
            scope.user_id,
            scope.conversation_id,
            scope.source_message_id,
            offer.case_id,
            offer.travel_date,
        )
        pending_id = uuid5(NAMESPACE_URL, key)
        values = {
            "pending_id": pending_id,
            "tenant_id": scope.tenant_id,
            "user_id": scope.user_id,
            "conversation_id": scope.conversation_id,
            "case_id": UUID(offer.case_id),
            "case_version": offer.case_version,
            "case_name": offer.case_name,
            "source_message_id": scope.source_message_id,
            "raw_text": offer.purpose_summary,
            "travel_date": datetime.fromisoformat(offer.travel_date).date(),
            "purpose_summary": offer.purpose_summary,
            "suggested_destination": offer.suggested_destination,
            "candidate_destination": "",
            "status": (
                "awaiting_confirmation"
                if offer.suggested_destination
                else "awaiting_destination"
            ),
            "last_reply_message_id": "",
            "idempotency_key": key,
            "version": 1,
            "created_at": scope.occurred_at,
            "expires_at": scope.occurred_at + ttl,
            "updated_at": scope.occurred_at,
        }
        inserted = await self.session.scalar(
            insert(CaseTravelClarificationPendingRow)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=[
                    CaseTravelClarificationPendingRow.tenant_id,
                    CaseTravelClarificationPendingRow.idempotency_key,
                ]
            )
            .returning(CaseTravelClarificationPendingRow.pending_id)
        )
        row = await self.session.get(CaseTravelClarificationPendingRow, pending_id)
        if row is None:
            raise RuntimeError("case travel clarification reservation disappeared")
        return _pending_from_row(row), inserted is not None

    async def save(
        self,
        pending: CaseTravelClarificationPending,
        *,
        expected_version: int,
        reply_message_id: str,
        receipt_id: str = "",
    ) -> CaseTravelClarificationPending:
        values: dict[str, Any] = {
            "status": pending.status,
            "candidate_destination": pending.candidate_destination,
            "last_reply_message_id": reply_message_id,
            "version": pending.version,
            "updated_at": datetime.now().astimezone(),
        }
        if receipt_id:
            values["receipt_id"] = UUID(receipt_id)
        if pending.status == "consumed":
            values["consumed_at"] = values["updated_at"]
        if pending.status == "cancelled":
            values["cancelled_at"] = values["updated_at"]
        result = await self.session.execute(
            update(CaseTravelClarificationPendingRow)
            .where(
                CaseTravelClarificationPendingRow.pending_id
                == UUID(pending.pending_id),
                CaseTravelClarificationPendingRow.tenant_id == pending.tenant_id,
                CaseTravelClarificationPendingRow.user_id == pending.user_id,
                CaseTravelClarificationPendingRow.conversation_id
                == pending.conversation_id,
                CaseTravelClarificationPendingRow.version == expected_version,
                CaseTravelClarificationPendingRow.status.in_(ACTIVE_STATUSES),
            )
            .values(**values)
        )
        if result.rowcount != 1:
            raise RuntimeError("case travel clarification version conflict")
        return pending


class SqlCaseTravelIntentWriter(CaseTravelIntentWriter):
    def __init__(
        self,
        session: Any,
        *,
        business_context: BusinessCommandContext,
        settings: Any,
    ) -> None:
        self.session = session
        self.business_context = business_context
        self.settings = settings

    async def register(self, pending, resolution, scope):
        travel_date = datetime.fromisoformat(resolution.travel_date).date()
        timezone = scope.occurred_at.tzinfo
        command = CreateTravelIntent(
            command_id=str(
                uuid5(
                    NAMESPACE_URL,
                    _digest(
                        "case-travel-command",
                        pending.pending_id,
                        scope.source_message_id,
                        resolution.destination_raw,
                    ),
                )
            ),
            destination_raw=resolution.destination_raw,
            destination_normalized=resolution.destination_normalized,
            city_code=resolution.city_code,
            province_code=resolution.province_code,
            start_at=datetime.combine(travel_date, time.min, tzinfo=timezone),
            end_at=datetime.combine(travel_date, time.max, tzinfo=timezone),
            time_precision="day",
            purpose_summary=resolution.purpose_summary,
            related_case_ids=(pending.case_id,),
            confidence=1.0,
        )
        context = replace(
            self.business_context,
            source_message_id=scope.source_message_id,
            occurred_at=scope.occurred_at,
            conversation_id=scope.conversation_id,
            admission_required=False,
            admission_ticket={},
            admission_action_id="",
            admission_operation="",
        )
        return await SqlBusinessExecutor(
            self.session,
            effect_policy=BusinessEffectPolicy.from_settings(self.settings),
            execution_authority="legacy_user_compatibility",
        ).execute(command, context)


async def create_case_travel_offers_from_business_result(
    session: Any,
    *,
    business_result: BusinessCompositionResult,
    business_context: BusinessCommandContext,
    raw_text: str,
    case_index_path: str | Path = DEFAULT_CASE_RAG_INDEX,
) -> tuple[PersistedCaseTravelOffer, ...]:
    if any(
        action.compiled_command_type == "create_travel_intent"
        and action.receipt is not None
        and action.receipt.status in {"executed", "duplicate"}
        for action in business_result.actions
    ):
        return ()
    store = SqlCaseTravelClarificationStore(session)
    scope = CaseTravelClarificationScope(
        tenant_id=business_context.tenant_id,
        user_id=business_context.actor_user_id,
        conversation_id=business_context.conversation_id,
        source_message_id=business_context.source_message_id,
        occurred_at=business_context.occurred_at,
    )
    offers: list[PersistedCaseTravelOffer] = []
    for action in business_result.actions:
        receipt = action.receipt
        if (
            action.compiled_command_type != "create_case_progress"
            or receipt is None
            or receipt.status not in {"executed", "duplicate"}
        ):
            continue
        after = dict(receipt.after or {})
        case_id = str(after.get("case_id") or "")
        if not case_id:
            continue
        case = await session.get(Agent2Case, UUID(case_id))
        if case is None or case.tenant_id != business_context.tenant_id:
            continue
        hint = find_case_location_hint_by_identity(
            case_index_path,
            case_number=case.case_number,
            case_name=case.case_name,
            source_id=case.source_id,
        )
        offer = build_case_travel_clarification(
            raw_text=raw_text,
            case_id=str(case.case_id),
            case_version=case.version,
            case_name=case.case_name,
            suggested_destination=(hint.court_or_location if hint else ""),
            occurred_at=business_context.occurred_at,
        )
        if offer is None:
            continue
        pending, created = await store.create(offer=offer, scope=scope)
        offers.append(PersistedCaseTravelOffer(pending, offer.question, created))
    return tuple(offers)


async def execute_case_travel_clarification_turn(
    session: Any,
    *,
    business_context: BusinessCommandContext,
    raw_text: str,
    settings: Any,
) -> SqlCaseTravelClarificationTurn | None:
    scope = CaseTravelClarificationScope(
        tenant_id=business_context.tenant_id,
        user_id=business_context.actor_user_id,
        conversation_id=business_context.conversation_id,
        source_message_id=business_context.source_message_id,
        occurred_at=business_context.occurred_at,
    )
    runtime = CaseTravelClarificationRuntime(
        store=SqlCaseTravelClarificationStore(session),
        writer=SqlCaseTravelIntentWriter(
            session,
            business_context=business_context,
            settings=settings,
        ),
    )
    state = await SQLAlchemyConversationStateStore(session).load(
        user_id=(
            f"{business_context.tenant_id}:{business_context.actor_user_id}"
        ),
        conversation_id=business_context.conversation_id,
    )
    other_active_pending_count = len(state.active_pending(scope.occurred_at)) + sum(
        1
        for pending in state.selection_pending
        if pending.status == "active" and pending.expires_at > scope.occurred_at
    )
    result = await runtime.handle_reply(
        scope=scope,
        raw_text=raw_text,
        allowed_case_ids=business_context.allowed_case_ids,
        other_active_pending_count=other_active_pending_count,
    )
    if result is None:
        return None
    business_result = None
    if result.status == "registered" and result.receipt is not None:
        resolution = result.resolution
        assert resolution is not None
        business_result = BusinessCompositionResult(
            source_message_id=scope.source_message_id,
            actions=(
                BusinessActionResult(
                    semantic_command_id=f"case-travel:{result.pending.pending_id}",
                    semantic_command_type="record_travel_candidate",
                    compiled_command_type="create_travel_intent",
                    receipt=result.receipt,
                    outcome_context={
                        "destination": resolution.destination_raw,
                        "date_label": _date_label(resolution.travel_date),
                        "purpose": resolution.purpose_summary,
                        "case_name": result.pending.case_name,
                    },
                ),
            ),
        )
    return SqlCaseTravelClarificationTurn(
        handled=True,
        reply=result.reply,
        runtime_result=result,
        business_result=business_result,
    )


def _pending_from_row(row: CaseTravelClarificationPendingRow) -> CaseTravelClarificationPending:
    return CaseTravelClarificationPending(
        pending_id=str(row.pending_id),
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        conversation_id=row.conversation_id,
        case_id=str(row.case_id),
        case_version=row.case_version,
        case_name=row.case_name,
        source_message_id=row.source_message_id,
        raw_text=row.raw_text,
        travel_date=row.travel_date.isoformat(),
        purpose_summary=row.purpose_summary,
        suggested_destination=row.suggested_destination,
        status=row.status,
        version=row.version,
        candidate_destination=row.candidate_destination,
    )


def _digest(*values: str) -> str:
    return hashlib.sha256("\x1f".join(values).encode("utf-8")).hexdigest()


def _date_label(iso_date: str) -> str:
    value = datetime.fromisoformat(iso_date).date()
    return f"{value.month}月{value.day}日"
