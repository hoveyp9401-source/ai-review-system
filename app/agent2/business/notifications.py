from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import String, cast, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent2.business.models import (
    Agent2OperationOutcome,
    Agent2Case,
    Agent2IdentityBinding,
    BusinessAuditEvent,
    BusinessCommandReceipt,
    CaseFollowupTask,
    NotificationOutbox,
    TravelCollaborationCandidate,
)
from app.agent2.operation_outcome_store import persist_operation_outcomes
from app.agent2.outcome_adapters import notification_outcome


logger = logging.getLogger(__name__)


class DirectMessageTransport(Protocol):
    async def send_robot_direct_text(self, *, user_ids: list[str], text: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class NotificationDispatchSummary:
    claimed: int
    sent: int
    failed: int
    dead_letter: int
    cancelled: int = 0


class NotificationDispatchPolicyError(RuntimeError):
    pass


def build_travel_notification_message(
    *,
    recipient_name: str,
    peer_names: tuple[str, ...],
    destination: str,
    overlap_start: datetime,
    overlap_end: datetime,
) -> dict[str, str]:
    del recipient_name  # The direct channel already identifies the recipient.
    peers = "、".join(name for name in peer_names if name)
    start = overlap_start.date().isoformat()
    end = overlap_end.date().isoformat()
    dates = start if start == end else f"{start} 至 {end}"
    return {
        "text": f"你计划在 {dates} 前往{destination}。{peers}同期也有行程，是否需要协同安排？",
        "destination": destination,
        "overlap_start": start,
        "overlap_end": end,
    }


async def enqueue_travel_candidate_notifications(
    session: AsyncSession,
    candidate: TravelCollaborationCandidate,
    *,
    now: datetime,
) -> tuple[NotificationOutbox, ...]:
    participant_ids = tuple(dict.fromkeys(str(value) for value in (candidate.participant_ids or []) if value))
    if len(participant_ids) < 2:
        return ()
    bindings = (
        await session.scalars(
            select(Agent2IdentityBinding).where(
                Agent2IdentityBinding.tenant_id == candidate.tenant_id,
                Agent2IdentityBinding.user_id.in_(participant_ids),
                Agent2IdentityBinding.active.is_(True),
            )
        )
    ).all()
    display_names = {
        item.user_id: (item.display_name or item.user_id)
        for item in bindings
    }

    events: list[NotificationOutbox] = []
    for recipient in participant_ids:
        peer_names = tuple(
            display_names.get(peer, peer)
            for peer in participant_ids
            if peer != recipient
        )
        message = build_travel_notification_message(
            recipient_name=display_names.get(recipient, recipient),
            peer_names=peer_names,
            destination=candidate.destination,
            overlap_start=candidate.overlap_start,
            overlap_end=candidate.overlap_end,
        )
        idempotency_key = f"travel-collaboration:{candidate.candidate_id}:{recipient}"
        notification_id = uuid5(NAMESPACE_URL, idempotency_key)
        inserted_id = (
            await session.execute(
                insert(NotificationOutbox)
                .values(
                    notification_id=notification_id,
                    tenant_id=candidate.tenant_id,
                    candidate_id=candidate.candidate_id,
                    recipient_user_id=recipient,
                    channel="dingtalk",
                    message_type="travel_collaboration_question",
                    message_json=message,
                    idempotency_key=idempotency_key,
                    status="pending",
                    retry_count=0,
                    response_json={},
                    dispatch_history_json=[],
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(
                    index_elements=[NotificationOutbox.tenant_id, NotificationOutbox.idempotency_key]
                )
                .returning(NotificationOutbox.notification_id)
            )
        ).scalar_one_or_none()
        event = await session.get(NotificationOutbox, inserted_id or notification_id)
        if event is None:
            event = await session.scalar(
                select(NotificationOutbox).where(
                    NotificationOutbox.tenant_id == candidate.tenant_id,
                    NotificationOutbox.idempotency_key == idempotency_key,
                )
            )
        if event is None:
            raise RuntimeError("notification outbox idempotency row cannot be loaded")
        events.append(event)

    notification_ids = [str(item.notification_id) for item in events]
    if candidate.notification_ids != notification_ids or candidate.status == "candidate":
        candidate.notification_ids = notification_ids
        candidate.status = "notified"
        candidate.version += 1
        candidate.updated_at = now
    await session.flush()
    return tuple(events)


async def claim_notification_outbox(
    session: AsyncSession,
    *,
    worker_id: str,
    now: datetime,
    limit: int = 50,
    allowed_tenant_ids: tuple[str, ...] = (),
    allowed_message_types: tuple[str, ...] = ("travel_collaboration_question",),
) -> tuple[NotificationOutbox, ...]:
    allowed_tenants = tuple(dict.fromkeys(value for value in allowed_tenant_ids if value))
    allowed_types = tuple(dict.fromkeys(value for value in allowed_message_types if value))
    if not allowed_tenants or not allowed_types:
        return ()
    events = (
        await session.scalars(
            select(NotificationOutbox)
            .where(
                NotificationOutbox.tenant_id.in_(allowed_tenants),
                NotificationOutbox.message_type.in_(allowed_types),
                NotificationOutbox.status.in_(("pending", "failed")),
                or_(
                    NotificationOutbox.next_retry_at.is_(None),
                    NotificationOutbox.next_retry_at <= now,
                ),
            )
            .order_by(NotificationOutbox.created_at, NotificationOutbox.notification_id)
            .limit(max(1, limit))
            .with_for_update(skip_locked=True)
        )
    ).all()
    for event in events:
        event.status = "processing"
        event.locked_by = worker_id
        event.locked_at = now
        event.error_message = ""
        event.updated_at = now
    await session.flush()
    return tuple(events)


async def mark_notification_sent(
    session: AsyncSession,
    event: NotificationOutbox,
    *,
    now: datetime,
    response: dict[str, Any],
) -> None:
    external_id = _transport_external_message_id(response)
    if not external_id:
        raise ValueError("DingTalk transport receipt is missing an external message id")
    attempt = int(event.retry_count or 0) + 1
    before = _notification_attempt_state(event)
    event.status = "sent"
    event.sent_at = now
    event.external_message_id = external_id
    event.response_json = dict(response)
    event.error_message = ""
    event.next_retry_at = None
    event.locked_by = ""
    event.locked_at = None
    event.updated_at = now
    event.dispatch_history_json = [
        *(event.dispatch_history_json or []),
        {
            "attempt": attempt,
            "occurred_at": now.isoformat(),
            "outcome": "sent",
            "external_message_id": external_id,
        },
    ]
    await _record_notification_attempt(
        session,
        event,
        attempt=attempt,
        now=now,
        outcome="sent",
        before=before,
    )
    await session.flush()


async def mark_notification_failed(
    session: AsyncSession,
    event: NotificationOutbox,
    *,
    now: datetime,
    error_message: str,
    max_attempts: int,
    retry_base_seconds: int = 30,
) -> None:
    before = _notification_attempt_state(event)
    attempts = int(event.retry_count or 0) + 1
    dead = attempts >= max(1, max_attempts)
    event.retry_count = attempts
    event.status = "dead_letter" if dead else "failed"
    event.error_message = str(error_message or "")[:2000]
    event.next_retry_at = None if dead else now + timedelta(
        seconds=min(3600, max(1, retry_base_seconds) * (2 ** (attempts - 1)))
    )
    event.locked_by = ""
    event.locked_at = None
    event.updated_at = now
    event.dispatch_history_json = [
        *(event.dispatch_history_json or []),
        {
            "attempt": attempts,
            "occurred_at": now.isoformat(),
            "outcome": "dead_letter" if dead else "failed",
            "error": event.error_message,
        },
    ]
    await _record_notification_attempt(
        session,
        event,
        attempt=attempts,
        now=now,
        outcome="dead_letter" if dead else "failed",
        before=before,
    )
    await session.flush()


async def mark_notification_cancelled(
    session: AsyncSession,
    event: NotificationOutbox,
    *,
    now: datetime,
    reason: str,
) -> None:
    before = _notification_attempt_state(event)
    event.status = "cancelled"
    event.error_message = str(reason or "notification_policy_cancelled")[:2000]
    event.next_retry_at = None
    event.locked_by = ""
    event.locked_at = None
    event.updated_at = now
    event.dispatch_history_json = [
        *(event.dispatch_history_json or []),
        {
            "attempt": int(event.retry_count or 0) + 1,
            "occurred_at": now.isoformat(),
            "outcome": "cancelled",
            "error": event.error_message,
        },
    ]
    await _record_notification_attempt(
        session,
        event,
        attempt=int(event.retry_count or 0) + 1,
        now=now,
        outcome="cancelled",
        before=before,
    )
    await session.flush()


async def recover_stale_notification_claims(
    session: AsyncSession,
    *,
    now: datetime,
    stale_before: datetime,
    limit: int = 100,
    allowed_tenant_ids: tuple[str, ...] = (),
    allowed_message_types: tuple[str, ...] = ("travel_collaboration_question",),
) -> tuple[NotificationOutbox, ...]:
    allowed_tenants = tuple(dict.fromkeys(value for value in allowed_tenant_ids if value))
    allowed_types = tuple(dict.fromkeys(value for value in allowed_message_types if value))
    if not allowed_tenants or not allowed_types:
        return ()
    events = (
        await session.scalars(
            select(NotificationOutbox)
            .where(
                NotificationOutbox.tenant_id.in_(allowed_tenants),
                NotificationOutbox.message_type.in_(allowed_types),
                NotificationOutbox.status == "processing",
                NotificationOutbox.locked_at.is_not(None),
                NotificationOutbox.locked_at < stale_before,
            )
            .order_by(NotificationOutbox.locked_at, NotificationOutbox.notification_id)
            .limit(max(1, limit))
            .with_for_update(skip_locked=True)
        )
    ).all()
    for event in events:
        before = _notification_attempt_state(event)
        attempt = int(event.retry_count or 0) + 1
        event.retry_count = attempt
        event.status = "dead_letter"
        event.locked_by = ""
        event.locked_at = None
        event.next_retry_at = None
        event.error_message = "delivery_outcome_unknown_after_stale_processing"
        event.updated_at = now
        event.dispatch_history_json = [
            *(event.dispatch_history_json or []),
            {
                "attempt": attempt,
                "occurred_at": now.isoformat(),
                "outcome": "delivery_unknown",
                "error": event.error_message,
            },
        ]
        await _record_notification_attempt(
            session,
            event,
            attempt=attempt,
            now=now,
            outcome="delivery_unknown",
            before=before,
        )
    await session.flush()
    return tuple(events)


async def dispatch_notification_batch(
    session: AsyncSession,
    transport: DirectMessageTransport,
    *,
    worker_id: str,
    now: datetime,
    limit: int = 50,
    max_attempts: int = 5,
    retry_base_seconds: int = 30,
    allowed_tenant_ids: tuple[str, ...] = (),
    allowed_message_types: tuple[str, ...] = ("travel_collaboration_question",),
    case_followup_tenant_ids: tuple[str, ...] = (),
    case_followup_user_ids: tuple[str, ...] = (),
    case_followup_trigger_types: tuple[str, ...] = (),
) -> NotificationDispatchSummary:
    events = await claim_notification_outbox(
        session,
        worker_id=worker_id,
        now=now,
        limit=limit,
        allowed_tenant_ids=allowed_tenant_ids,
        allowed_message_types=allowed_message_types,
    )
    if not events:
        return NotificationDispatchSummary(0, 0, 0, 0)

    # Persist the claim before any network I/O. A crash after delivery but
    # before the final state transition leaves an uncertain `processing` row;
    # stale recovery dead-letters it instead of risking a duplicate send.
    await session.commit()
    sent = failed = dead_letter = cancelled = 0
    for event in events:
        binding = await session.scalar(
            select(Agent2IdentityBinding).where(
                Agent2IdentityBinding.tenant_id == event.tenant_id,
                Agent2IdentityBinding.user_id == event.recipient_user_id,
                Agent2IdentityBinding.active.is_(True),
            )
        )
        text = str((event.message_json or {}).get("text") or "").strip()
        if event.message_type in {"case_progress_followup", "case_lifecycle_followup"}:
            try:
                await _validate_case_followup_dispatch(
                    session,
                    event,
                    binding=binding,
                    now=now,
                    allowed_tenant_ids=case_followup_tenant_ids,
                    allowed_user_ids=case_followup_user_ids,
                    allowed_trigger_types=case_followup_trigger_types,
                )
            except NotificationDispatchPolicyError as exc:
                await mark_notification_cancelled(
                    session,
                    event,
                    now=now,
                    reason=str(exc),
                )
                await session.commit()
                cancelled += 1
                continue
        # End the read transaction before calling DingTalk.
        await session.commit()
        try:
            if binding is None or not binding.dingtalk_user_id:
                raise RuntimeError("active_dingtalk_identity_binding_missing")
            if not text:
                raise RuntimeError("notification_text_missing")
            response = await transport.send_robot_direct_text(
                user_ids=[binding.dingtalk_user_id],
                text=text,
            )
            rejected = [
                *response.get("invalidStaffIdList", []),
                *response.get("filteredStaffIdList", []),
                *response.get("flowControlledStaffIdList", []),
            ]
            if rejected:
                raise RuntimeError(f"dingtalk_recipient_rejected:{','.join(map(str, rejected))}")
            if not _transport_external_message_id(response):
                raise RuntimeError("dingtalk_transport_receipt_missing")
        except Exception as exc:
            await mark_notification_failed(
                session,
                event,
                now=now,
                error_message=f"{type(exc).__name__}: {exc}",
                max_attempts=max_attempts,
                retry_base_seconds=retry_base_seconds,
            )
            await session.commit()
            if event.status == "dead_letter":
                dead_letter += 1
            else:
                failed += 1
        else:
            await mark_notification_sent(session, event, now=now, response=response)
            # Persist provider evidence before advancing the domain Pending.
            # A later database failure must never erase proof of an external send.
            await session.commit()
            await _persist_notification_dispatch_outcome(session, event, now=now)
            await session.commit()
            if event.message_type == "case_lifecycle_followup":
                from app.agent2.case_followup_outbox import (
                    apply_case_followup_provider_acceptance,
                )
                try:
                    await apply_case_followup_provider_acceptance(session, event, now=now)
                    await session.commit()
                except Exception:
                    await session.rollback()
                    logger.exception(
                        "provider receipt committed but follow-up Pending advancement failed",
                        extra={"notification_id": str(event.notification_id)},
                    )
            sent += 1
    return NotificationDispatchSummary(len(events), sent, failed, dead_letter, cancelled)


async def _persist_notification_dispatch_outcome(
    session: AsyncSession,
    event: NotificationOutbox,
    *,
    now: datetime,
) -> int:
    payload = dict(event.message_json or {})
    is_case_followup = event.message_type in {
        "case_progress_followup", "case_lifecycle_followup"
    }
    source_turn_id = (
        f"followup:{event.candidate_id}"
        if is_case_followup and event.candidate_id is not None
        else f"notification:{event.notification_id}"
    )
    conversation_id = str(payload.get("conversation_id") or "").strip() or (
        f"notification:{event.notification_id}"
    )
    outcome = notification_outcome(
        event,
        travel_snapshot={
            key: value
            for key, value in payload.items()
            if key in {
                "case_name", "destination", "date_label", "purpose",
                "overlap_start", "overlap_end",
            }
        },
        source_turn_id=source_turn_id,
        domain="followup" if is_case_followup else "travel",
        conversation_id=conversation_id,
    )
    return await persist_operation_outcomes(
        session,
        (outcome,),
        tenant_id=event.tenant_id,
        user_id=event.recipient_user_id,
        conversation_id=conversation_id,
        source_turn_id=source_turn_id,
        now=now,
    )


async def reconcile_sent_notification_outcomes(
    session: AsyncSession,
    *,
    now: datetime,
    allowed_tenant_ids: tuple[str, ...],
    allowed_message_types: tuple[str, ...],
    limit: int = 100,
) -> int:
    """Backfill an Outcome from committed provider evidence without resending."""
    tenant_ids = tuple(dict.fromkeys(filter(None, allowed_tenant_ids)))
    message_types = tuple(dict.fromkeys(filter(None, allowed_message_types)))
    if not tenant_ids or not message_types:
        return 0
    events = (
        await session.scalars(
            select(NotificationOutbox)
            .where(
                NotificationOutbox.tenant_id.in_(tenant_ids),
                NotificationOutbox.message_type.in_(message_types),
                NotificationOutbox.status == "sent",
                NotificationOutbox.external_message_id != "",
                ~select(Agent2OperationOutcome.outcome_id).where(
                    Agent2OperationOutcome.tenant_id == NotificationOutbox.tenant_id,
                    Agent2OperationOutcome.object_id
                    == cast(NotificationOutbox.notification_id, String),
                    Agent2OperationOutcome.operation == "notify",
                    Agent2OperationOutcome.message_status == "accepted_by_provider",
                ).exists(),
            )
            .order_by(NotificationOutbox.sent_at, NotificationOutbox.notification_id)
            .limit(max(1, limit))
            .with_for_update(skip_locked=True)
        )
    ).all()
    reconciled = 0
    for event in events:
        reconciled += await _persist_notification_dispatch_outcome(
            session, event, now=now
        )
    await session.flush()
    return reconciled


async def _validate_case_followup_dispatch(
    session: AsyncSession,
    event: NotificationOutbox,
    *,
    binding: Agent2IdentityBinding | None,
    now: datetime,
    allowed_tenant_ids: tuple[str, ...],
    allowed_user_ids: tuple[str, ...],
    allowed_trigger_types: tuple[str, ...] = (),
) -> None:
    if event.tenant_id not in set(allowed_tenant_ids) or event.recipient_user_id not in set(
        allowed_user_ids
    ):
        raise NotificationDispatchPolicyError("case_followup_outside_configured_cohort")
    if binding is None or not binding.dingtalk_user_id:
        raise NotificationDispatchPolicyError("active_dingtalk_identity_binding_missing")
    payload = dict(event.message_json or {})
    try:
        case_id = UUID(str(payload.get("case_id") or ""))
        expires_at = datetime.fromisoformat(str(payload.get("expires_at") or ""))
    except (TypeError, ValueError) as exc:
        raise NotificationDispatchPolicyError("case_followup_payload_invalid") from exc
    if expires_at.tzinfo is None or now >= expires_at:
        raise NotificationDispatchPolicyError("case_followup_expired")
    case = await session.scalar(
        select(Agent2Case).where(
            Agent2Case.tenant_id == event.tenant_id,
            Agent2Case.case_id == case_id,
        )
    )
    allowed_cases = {
        str(value)
        for value in (binding.permission_scope_json or {}).get("allowed_case_ids", [])
        if value
    }
    if case is None or str(case_id) not in allowed_cases:
        raise NotificationDispatchPolicyError("case_followup_case_access_revoked")
    if (
        binding.company_id != case.company_id
        or binding.department_id != case.department_id
        or binding.team_id != case.team_id
    ):
        raise NotificationDispatchPolicyError("case_followup_organization_scope_changed")
    if event.message_type == "case_lifecycle_followup":
        await session.refresh(event)
        if event.status != "processing":
            raise NotificationDispatchPolicyError(
                "case_followup_outbox_no_longer_sendable"
            )
        task = await session.scalar(
            select(CaseFollowupTask)
            .where(
                CaseFollowupTask.tenant_id == event.tenant_id,
                CaseFollowupTask.followup_id == event.candidate_id,
                CaseFollowupTask.case_id == case_id,
                CaseFollowupTask.assigned_user_id == event.recipient_user_id,
            )
            .with_for_update()
        )
        is_reminder = str(payload.get("is_reminder") or "") == "true"
        initial_sendable = (
            task is not None
            and task.task_status == "queued"
            and task.message_status == "queued"
        )
        reminder_sendable = (
            task is not None
            and is_reminder
            and task.task_status == "waiting_for_reply"
            and task.message_status in {"accepted_by_provider", "delivery_confirmed"}
            and task.response_status == "awaiting_input"
            and bool(task.pending_id)
            and int(payload.get("reminder_number") or 0) == task.reminder_count + 1
        )
        if task is None or not task.conversation_id or not (
            initial_sendable or reminder_sendable
        ):
            raise NotificationDispatchPolicyError("case_followup_task_no_longer_sendable")
        if task.case_version != case.version or int(payload.get("case_version") or -1) != case.version:
            raise NotificationDispatchPolicyError("case_followup_case_version_changed")
        if task.trigger_type not in set(allowed_trigger_types):
            raise NotificationDispatchPolicyError("case_followup_trigger_kill_switch_closed")


def notification_id_for(candidate_id: UUID, recipient_user_id: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"travel-collaboration:{candidate_id}:{recipient_user_id}")


def _transport_external_message_id(response: dict[str, Any]) -> str:
    return str(
        response.get("processQueryKey")
        or response.get("taskId")
        or response.get("messageId")
        or ""
    ).strip()


def _notification_attempt_state(event: NotificationOutbox) -> dict[str, Any]:
    return {
        "notification_id": str(event.notification_id),
        "candidate_id": str(event.candidate_id) if event.candidate_id is not None else "",
        "recipient_user_id": event.recipient_user_id,
        "channel": event.channel,
        "status": event.status,
        "retry_count": int(event.retry_count or 0),
        "external_message_id": event.external_message_id or "",
        "error_message": event.error_message or "",
        "response_json": dict(event.response_json or {}),
    }


async def _record_notification_attempt(
    session: AsyncSession,
    event: NotificationOutbox,
    *,
    attempt: int,
    now: datetime,
    outcome: str,
    before: dict[str, Any],
) -> None:
    case_followup = event.message_type in {
        "case_progress_followup", "case_lifecycle_followup"
    }
    kind = "case-progress-followup-notification" if case_followup else "travel-notification"
    command_type = (
        "dispatch_case_progress_followup" if case_followup else "dispatch_travel_notification"
    )
    resource_type = (
        "case_progress_followup_notification" if case_followup else "travel_notification"
    )
    actor_user_id = (
        "system:agent2_case_followup_worker"
        if case_followup
        else "system:agent2_travel_notification_worker"
    )
    idempotency_key = f"{event.tenant_id}:{kind}:{event.notification_id}:attempt:{attempt}"
    receipt_id = uuid5(NAMESPACE_URL, f"business-receipt:{idempotency_key}")
    after = _notification_attempt_state(event)
    successful = outcome == "sent"
    inserted_receipt = await session.scalar(
        insert(BusinessCommandReceipt)
        .values(
            receipt_id=receipt_id,
            tenant_id=event.tenant_id,
            command_id=f"{kind}:{event.notification_id}:attempt:{attempt}",
            command_type=command_type,
            actor_user_id=actor_user_id,
            source_message_id=f"{kind}:{event.notification_id}",
            idempotency_key=idempotency_key,
            status="executed" if successful else "failed",
            resource_type=resource_type,
            resource_id=str(event.notification_id),
            before_json=before,
            after_json=after,
            error_code="" if successful else "notification_dispatch_failed",
            failed_stage="" if successful else "transport",
            actual_write=True,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(
            index_elements=[
                BusinessCommandReceipt.tenant_id,
                BusinessCommandReceipt.idempotency_key,
            ]
        )
        .returning(BusinessCommandReceipt.receipt_id)
    )
    if inserted_receipt is None:
        return
    session.add(
        BusinessAuditEvent(
            audit_id=uuid5(NAMESPACE_URL, f"business-audit:{receipt_id}"),
            tenant_id=event.tenant_id,
            receipt_id=receipt_id,
            actor_user_id=actor_user_id,
            source_message_id=f"{kind}:{event.notification_id}",
            source_channel="dingtalk",
            command_type=command_type,
            resource_type=resource_type,
            resource_id=str(event.notification_id),
            before_json=before,
            after_json=after,
            created_at=now,
        )
    )
