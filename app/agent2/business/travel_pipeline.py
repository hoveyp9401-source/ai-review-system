from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent2.business.models import (
    NotificationOutbox,
    BusinessAuditEvent,
    BusinessCommandReceipt,
    TravelCollaborationCandidate,
    TravelIntent,
)
from app.agent2.business.notifications import enqueue_travel_candidate_notifications
from app.agent2.business.travel import TravelMatcher


# Only intents created by an actual DingTalk inbound message may trigger a
# collaboration notification.  Acceptance smoke records and imported/seeded
# data remain visible for diagnostics, but can never message a real user.
REAL_USER_TRAVEL_SOURCE_CHANNELS = (
    "dingtalk",
    "dingtalk_stream",
    "dingtalk_stream_text",
    "dingtalk_webhook",
    "dingtalk_webhook_text",
)
TRAVEL_MATCHER_ACTOR_USER_ID = "system:travel-collaboration-matcher"


@dataclass(frozen=True)
class TravelEvaluationSummary:
    scanned_intents: int
    matched_groups: int
    created_candidates: int
    notification_rows: int
    expired_candidates: int


@dataclass(frozen=True)
class _IntentSnapshot:
    travel_intent_id: str
    tenant_id: str
    company_id: str
    department_id: str
    team_id: str
    user_id: str
    city_code: str
    destination_normalized: str
    start_at: datetime
    end_at: datetime
    status: str
    confidence: float


async def evaluate_travel_collaboration_candidates(
    session: AsyncSession,
    *,
    now: datetime,
    allowed_tenant_ids: tuple[str, ...],
) -> TravelEvaluationSummary:
    tenants = tuple(dict.fromkeys(value for value in allowed_tenant_ids if value))
    if not tenants:
        return TravelEvaluationSummary(0, 0, 0, 0, 0)
    expired = await expire_travel_candidates(
        session,
        now=now,
        allowed_tenant_ids=tenants,
    )
    intents = (
        await session.scalars(
            select(TravelIntent).where(
                TravelIntent.tenant_id.in_(tenants),
                TravelIntent.status.in_(tuple(TravelMatcher.VALID_STATUSES)),
                TravelIntent.confidence >= Decimal("0.85"),
                TravelIntent.end_at >= now,
                TravelIntent.source_channel.in_(REAL_USER_TRAVEL_SOURCE_CHANNELS),
            )
        )
    ).all()
    snapshots = tuple(
        _IntentSnapshot(
            travel_intent_id=str(item.travel_intent_id),
            tenant_id=item.tenant_id,
            company_id=item.company_id,
            department_id=item.department_id,
            team_id=item.team_id,
            user_id=item.user_id,
            city_code=item.city_code,
            destination_normalized=item.destination_normalized,
            start_at=item.start_at,
            end_at=item.end_at,
            status=item.status,
            confidence=float(item.confidence),
        )
        for item in intents
    )
    intent_by_id = _index_intents_by_id(intents)
    matches = TravelMatcher().match(snapshots)
    created = notification_rows = 0
    for match in matches:
        first = intent_by_id[match.travel_intent_ids[0]]
        deduplication_key = (
            f"{match.tenant_id}:{'|'.join(match.travel_intent_ids)}:"
            f"{match.overlap_start.isoformat()}:{match.overlap_end.isoformat()}"
        )
        candidate_id = UUID(match.candidate_id)
        inserted_id = (
            await session.execute(
                insert(TravelCollaborationCandidate)
                .values(
                    candidate_id=candidate_id,
                    tenant_id=match.tenant_id,
                    company_id=first.company_id,
                    department_id=first.department_id,
                    team_id=first.team_id,
                    travel_intent_ids=list(match.travel_intent_ids),
                    participant_ids=list(match.participant_ids),
                    destination=match.destination,
                    overlap_start=match.overlap_start,
                    overlap_end=match.overlap_end,
                    match_reason=match.match_reason,
                    match_score=Decimal(str(match.match_score)),
                    status="candidate",
                    notification_ids=[],
                    responses_json={},
                    deduplication_key=deduplication_key,
                    version=1,
                    expires_at=match.overlap_end + timedelta(days=1),
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        TravelCollaborationCandidate.tenant_id,
                        TravelCollaborationCandidate.deduplication_key,
                    ]
                )
                .returning(TravelCollaborationCandidate.candidate_id)
            )
        ).scalar_one_or_none()
        if inserted_id is not None:
            created += 1
        candidate = await session.get(
            TravelCollaborationCandidate,
            inserted_id or candidate_id,
        )
        if candidate is None:
            candidate = await session.scalar(
                select(TravelCollaborationCandidate).where(
                    TravelCollaborationCandidate.tenant_id == match.tenant_id,
                    TravelCollaborationCandidate.deduplication_key == deduplication_key,
                )
            )
        if candidate is None:
            raise RuntimeError("travel candidate idempotency row cannot be loaded")
        if candidate.status in {"declined", "accepted", "expired", "cancelled"}:
            continue
        notifications = await enqueue_travel_candidate_notifications(
            session,
            candidate,
            now=now,
        )
        notification_rows += len(notifications)
        if inserted_id is not None:
            await _record_candidate_creation(
                session,
                candidate,
                notifications=notifications,
                now=now,
            )
    await session.flush()
    return TravelEvaluationSummary(
        scanned_intents=len(intents),
        matched_groups=len(matches),
        created_candidates=created,
        notification_rows=notification_rows,
        expired_candidates=expired,
    )


def _index_intents_by_id(intents: tuple[TravelIntent, ...] | list[TravelIntent]) -> dict[str, TravelIntent]:
    """Match the string ID contract emitted by ``TravelMatcher``.

    PostgreSQL returns ``travel_intent_id`` as ``UUID`` while matcher snapshots
    intentionally serialize it to ``str``. Normalizing at this boundary keeps
    real database evaluation consistent with the in-memory matcher contract.
    """

    return {str(item.travel_intent_id): item for item in intents}


async def expire_travel_candidates(
    session: AsyncSession,
    *,
    now: datetime,
    allowed_tenant_ids: tuple[str, ...],
) -> int:
    tenants = tuple(dict.fromkeys(value for value in allowed_tenant_ids if value))
    if not tenants:
        return 0
    candidate_ids = select(TravelCollaborationCandidate.candidate_id).where(
        TravelCollaborationCandidate.tenant_id.in_(tenants),
        TravelCollaborationCandidate.status.in_(("candidate", "notified", "accepted_by_one")),
        TravelCollaborationCandidate.expires_at < now,
    )
    await session.execute(
        update(NotificationOutbox)
        .where(
            NotificationOutbox.candidate_id.in_(candidate_ids),
            NotificationOutbox.status.in_(("pending", "failed")),
        )
        .values(status="cancelled", updated_at=now, error_message="candidate_expired")
    )
    result = await session.execute(
        update(TravelCollaborationCandidate)
        .where(TravelCollaborationCandidate.candidate_id.in_(candidate_ids))
        .values(
            status="expired",
            version=TravelCollaborationCandidate.version + 1,
            updated_at=now,
        )
    )
    return int(result.rowcount or 0)


async def _record_candidate_creation(
    session: AsyncSession,
    candidate: TravelCollaborationCandidate,
    *,
    notifications: tuple[NotificationOutbox, ...],
    now: datetime,
) -> None:
    idempotency_key = f"{candidate.tenant_id}:travel-candidate:{candidate.deduplication_key}"
    receipt_id = uuid5(NAMESPACE_URL, f"business-receipt:{idempotency_key}")
    source_message_id = f"travel-match:{candidate.candidate_id}"
    after = {
        "candidate_id": str(candidate.candidate_id),
        "travel_intent_ids": list(candidate.travel_intent_ids or []),
        "participant_ids": list(candidate.participant_ids or []),
        "destination": candidate.destination,
        "overlap_start": candidate.overlap_start.isoformat(),
        "overlap_end": candidate.overlap_end.isoformat(),
        "status": candidate.status,
        "notification_ids": [str(item.notification_id) for item in notifications],
    }
    inserted_receipt = await session.scalar(
        insert(BusinessCommandReceipt)
        .values(
            receipt_id=receipt_id,
            tenant_id=candidate.tenant_id,
            command_id=f"travel-candidate:{candidate.candidate_id}",
            command_type="create_travel_collaboration_candidate",
            actor_user_id=TRAVEL_MATCHER_ACTOR_USER_ID,
            source_message_id=source_message_id,
            idempotency_key=idempotency_key,
            status="executed",
            resource_type="travel_collaboration",
            resource_id=str(candidate.candidate_id),
            before_json={},
            after_json=after,
            actual_write=True,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(
            index_elements=[BusinessCommandReceipt.tenant_id, BusinessCommandReceipt.idempotency_key]
        )
        .returning(BusinessCommandReceipt.receipt_id)
    )
    if inserted_receipt is None:
        return
    session.add(
        BusinessAuditEvent(
            audit_id=uuid5(NAMESPACE_URL, f"business-audit:{receipt_id}"),
            tenant_id=candidate.tenant_id,
            receipt_id=receipt_id,
            actor_user_id=TRAVEL_MATCHER_ACTOR_USER_ID,
            source_message_id=source_message_id,
            source_channel="agent2_background_worker",
            command_type="create_travel_collaboration_candidate",
            resource_type="travel_collaboration",
            resource_id=str(candidate.candidate_id),
            before_json={},
            after_json=after,
            created_at=now,
        )
    )
