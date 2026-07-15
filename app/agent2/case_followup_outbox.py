from __future__ import annotations

from datetime import datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent2.business.models import (
    Agent2Case,
    Agent2IdentityBinding,
    CaseFollowupPending,
    CaseFollowupPolicy,
    CaseFollowupTask,
    NotificationOutbox,
)
from app.agent2.case_followup_dispatch import (
    FollowupDispatchState,
    accept_provider_receipt,
    build_followup_outbox_plan,
    build_followup_reminder_outbox_plan,
)
from app.agent2.case_followup_task_ledger_sql import (
    focus_followup_and_suspend_current_task,
)
from app.models import Agent2ConversationState


def _dispatch_state(task: CaseFollowupTask, case_name: str) -> FollowupDispatchState:
    return FollowupDispatchState(
        followup_id=str(task.followup_id),
        tenant_id=task.tenant_id,
        user_id=task.assigned_user_id,
        conversation_id=task.conversation_id,
        case_id=str(task.case_id),
        case_name=case_name,
        case_version=task.case_version,
        question_text=task.question_text,
        task_status=task.task_status,
        message_status=task.message_status,
        response_status=task.response_status,
        expires_at=task.expires_at,
        version=task.version,
        provider_message_id=task.provider_message_id,
    )


async def enqueue_due_case_followups(
    session: AsyncSession,
    *,
    now: datetime,
    allowed_tenant_ids: tuple[str, ...],
    allowed_user_ids: tuple[str, ...],
    allowed_case_ids: tuple[str, ...],
    send_enabled: bool,
    limit: int = 50,
    user_daily_limit: int = 3,
    case_daily_limit: int = 1,
) -> tuple[NotificationOutbox, ...]:
    """Claim due tasks and atomically create one idempotent outbox row each."""
    if not send_enabled:
        return ()
    tenant_ids = tuple(dict.fromkeys(filter(None, allowed_tenant_ids)))
    user_ids = tuple(dict.fromkeys(filter(None, allowed_user_ids)))
    try:
        case_ids = tuple(UUID(value) for value in dict.fromkeys(filter(None, allowed_case_ids)))
    except ValueError:
        return ()
    if not tenant_ids or not user_ids or not case_ids:
        return ()

    tasks = (
        await session.scalars(
            select(CaseFollowupTask)
            .where(
                CaseFollowupTask.tenant_id.in_(tenant_ids),
                CaseFollowupTask.assigned_user_id.in_(user_ids),
                CaseFollowupTask.case_id.in_(case_ids),
                CaseFollowupTask.task_status == "scheduled",
                CaseFollowupTask.message_status == "scheduled",
                CaseFollowupTask.due_at <= now,
                CaseFollowupTask.expires_at > now,
            )
            .order_by(
                CaseFollowupTask.priority.desc(),
                CaseFollowupTask.due_at,
                CaseFollowupTask.followup_id,
            )
            .limit(max(1, limit))
            .with_for_update(skip_locked=True)
        )
    ).all()
    events: list[NotificationOutbox] = []
    for task in tasks:
        policy = await session.get(CaseFollowupPolicy, task.policy_id) if task.policy_id else None
        if policy is not None:
            local_now = now.astimezone(ZoneInfo(policy.timezone))
            if policy.snoozed_until is not None and now < policy.snoozed_until:
                continue
            if not (
                policy.allowed_start_time <= local_now.time().replace(tzinfo=None)
                <= policy.allowed_end_time
            ):
                continue
        else:
            local_now = now.astimezone(ZoneInfo("Asia/Shanghai"))
        local_day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        await _acquire_quota_locks(
            session, tenant_id=task.tenant_id,
            user_id=task.assigned_user_id, case_id=str(task.case_id),
            local_day=local_now.date().isoformat(),
        )
        active_message_states = (
            "queued", "sending", "accepted_by_provider", "delivery_confirmed"
        )
        user_messages_today = int(
            await session.scalar(
                select(func.count(CaseFollowupTask.followup_id)).where(
                    CaseFollowupTask.tenant_id == task.tenant_id,
                    CaseFollowupTask.assigned_user_id == task.assigned_user_id,
                    CaseFollowupTask.message_status.in_(active_message_states),
                    CaseFollowupTask.updated_at >= local_day_start,
                )
            ) or 0
        )
        case_messages_today = int(
            await session.scalar(
                select(func.count(CaseFollowupTask.followup_id)).where(
                    CaseFollowupTask.tenant_id == task.tenant_id,
                    CaseFollowupTask.case_id == task.case_id,
                    CaseFollowupTask.message_status.in_(active_message_states),
                    CaseFollowupTask.updated_at >= local_day_start,
                )
            ) or 0
        )
        if (
            user_messages_today >= max(1, user_daily_limit)
            or case_messages_today >= max(1, case_daily_limit)
        ):
            continue
        case = await session.scalar(
            select(Agent2Case).where(
                Agent2Case.tenant_id == task.tenant_id,
                Agent2Case.case_id == task.case_id,
                Agent2Case.owner_user_id == task.assigned_user_id,
            )
        )
        binding = await session.scalar(
            select(Agent2IdentityBinding).where(
                Agent2IdentityBinding.tenant_id == task.tenant_id,
                Agent2IdentityBinding.user_id == task.assigned_user_id,
                Agent2IdentityBinding.active.is_(True),
            )
        )
        permitted = {
            str(value)
            for value in ((binding.permission_scope_json if binding else {}) or {}).get(
                "allowed_case_ids", []
            )
            if value
        }
        if case is None or binding is None or str(task.case_id) not in permitted:
            task.task_status = "cancelled"
            task.message_status = "cancelled"
            task.response_status = "cancelled"
            task.cancelled_at = now
            task.version += 1
            continue
        if case.version != task.case_version:
            task.task_status = "cancelled"
            task.message_status = "cancelled"
            task.response_status = "cancelled"
            task.cancelled_at = now
            task.version += 1
            continue

        plan = build_followup_outbox_plan(_dispatch_state(task, case.case_name), now=now)
        inserted_id = (
            await session.execute(
                insert(NotificationOutbox)
                .values(
                    notification_id=UUID(plan.notification_id),
                    tenant_id=task.tenant_id,
                    candidate_id=task.followup_id,
                    recipient_user_id=task.assigned_user_id,
                    channel="dingtalk",
                    message_type="case_lifecycle_followup",
                    message_json=plan.payload,
                    idempotency_key=plan.idempotency_key,
                    status="pending",
                    retry_count=0,
                    response_json={},
                    dispatch_history_json=[],
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        NotificationOutbox.tenant_id,
                        NotificationOutbox.idempotency_key,
                    ]
                )
                .returning(NotificationOutbox.notification_id)
            )
        ).scalar_one_or_none()
        event = await session.get(
            NotificationOutbox, inserted_id or UUID(plan.notification_id)
        )
        if event is None:
            raise RuntimeError("follow-up outbox idempotency row cannot be loaded")
        task.task_status = plan.state_after.task_status
        task.message_status = plan.state_after.message_status
        task.version = plan.state_after.version
        task.updated_at = now
        events.append(event)
    await session.flush()
    return tuple(events)


async def enqueue_due_case_followup_reminders(
    session: AsyncSession,
    *,
    now: datetime,
    allowed_tenant_ids: tuple[str, ...],
    allowed_user_ids: tuple[str, ...],
    allowed_case_ids: tuple[str, ...],
    send_enabled: bool,
    reminder_interval_hours: int,
    max_reminders: int,
    user_daily_limit: int = 3,
    case_daily_limit: int = 1,
    limit: int = 50,
) -> tuple[NotificationOutbox, ...]:
    """Create at most one idempotent reminder attempt for an unanswered task."""
    if not send_enabled or max_reminders <= 0 or reminder_interval_hours < 1:
        return ()
    try:
        case_ids = tuple(UUID(value) for value in dict.fromkeys(allowed_case_ids))
    except ValueError:
        return ()
    tenant_ids = tuple(dict.fromkeys(filter(None, allowed_tenant_ids)))
    user_ids = tuple(dict.fromkeys(filter(None, allowed_user_ids)))
    if not tenant_ids or not user_ids or not case_ids:
        return ()
    cutoff = now - timedelta(hours=reminder_interval_hours)
    tasks = (
        await session.scalars(
            select(CaseFollowupTask)
            .where(
                CaseFollowupTask.tenant_id.in_(tenant_ids),
                CaseFollowupTask.assigned_user_id.in_(user_ids),
                CaseFollowupTask.case_id.in_(case_ids),
                CaseFollowupTask.task_status == "waiting_for_reply",
                CaseFollowupTask.message_status.in_((
                    "accepted_by_provider", "delivery_confirmed"
                )),
                CaseFollowupTask.response_status == "awaiting_input",
                CaseFollowupTask.provider_message_id != "",
                CaseFollowupTask.last_sent_at <= cutoff,
                CaseFollowupTask.expires_at > now,
                CaseFollowupTask.reminder_count < max_reminders,
                CaseFollowupTask.reminder_count < CaseFollowupTask.max_reminders,
            )
            .order_by(CaseFollowupTask.last_sent_at, CaseFollowupTask.followup_id)
            .limit(max(1, limit))
            .with_for_update(skip_locked=True)
        )
    ).all()
    events: list[NotificationOutbox] = []
    for task in tasks:
        policy = await session.get(CaseFollowupPolicy, task.policy_id) if task.policy_id else None
        timezone_name = policy.timezone if policy is not None else "Asia/Shanghai"
        local_now = now.astimezone(ZoneInfo(timezone_name))
        local_day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        await _acquire_quota_locks(
            session, tenant_id=task.tenant_id,
            user_id=task.assigned_user_id, case_id=str(task.case_id),
            local_day=local_now.date().isoformat(),
        )
        counted_statuses = ("pending", "processing", "sent")
        user_messages_today = int(await session.scalar(
            select(func.count(NotificationOutbox.notification_id)).where(
                NotificationOutbox.tenant_id == task.tenant_id,
                NotificationOutbox.recipient_user_id == task.assigned_user_id,
                NotificationOutbox.message_type == "case_lifecycle_followup",
                NotificationOutbox.status.in_(counted_statuses),
                NotificationOutbox.created_at >= local_day_start,
            )
        ) or 0)
        case_messages_today = int(await session.scalar(
            select(func.count(NotificationOutbox.notification_id)).where(
                NotificationOutbox.tenant_id == task.tenant_id,
                NotificationOutbox.message_type == "case_lifecycle_followup",
                NotificationOutbox.status.in_(counted_statuses),
                NotificationOutbox.created_at >= local_day_start,
                NotificationOutbox.message_json["case_id"].astext == str(task.case_id),
            )
        ) or 0)
        if (
            user_messages_today >= max(1, user_daily_limit)
            or case_messages_today >= max(1, case_daily_limit)
        ):
            continue
        case = await session.scalar(
            select(Agent2Case).where(
                Agent2Case.tenant_id == task.tenant_id,
                Agent2Case.case_id == task.case_id,
                Agent2Case.owner_user_id == task.assigned_user_id,
                Agent2Case.version == task.case_version,
            )
        )
        binding = await session.scalar(
            select(Agent2IdentityBinding).where(
                Agent2IdentityBinding.tenant_id == task.tenant_id,
                Agent2IdentityBinding.user_id == task.assigned_user_id,
                Agent2IdentityBinding.active.is_(True),
            )
        )
        permitted = {
            str(value)
            for value in ((binding.permission_scope_json if binding else {}) or {}).get(
                "allowed_case_ids", []
            )
            if value
        }
        if case is None or binding is None or str(task.case_id) not in permitted:
            task.task_status = "cancelled"
            task.message_status = "cancelled"
            task.response_status = "cancelled"
            task.cancelled_at = now
            task.version += 1
            continue
        reminder_number = task.reminder_count + 1
        plan = build_followup_reminder_outbox_plan(
            _dispatch_state(task, case.case_name),
            reminder_number=reminder_number,
            now=now,
        )
        inserted_id = (
            await session.execute(
                insert(NotificationOutbox)
                .values(
                    notification_id=UUID(plan.notification_id),
                    tenant_id=task.tenant_id, candidate_id=task.followup_id,
                    recipient_user_id=task.assigned_user_id, channel="dingtalk",
                    message_type="case_lifecycle_followup",
                    message_json=plan.payload, idempotency_key=plan.idempotency_key,
                    status="pending", retry_count=0, response_json={},
                    dispatch_history_json=[], created_at=now, updated_at=now,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        NotificationOutbox.tenant_id,
                        NotificationOutbox.idempotency_key,
                    ]
                )
                .returning(NotificationOutbox.notification_id)
            )
        ).scalar_one_or_none()
        event = await session.get(
            NotificationOutbox, inserted_id or UUID(plan.notification_id)
        )
        if event is None:
            raise RuntimeError("follow-up reminder outbox row cannot be loaded")
        events.append(event)
    await session.flush()
    return tuple(events)


async def _acquire_quota_locks(
    session: AsyncSession,
    *,
    tenant_id: str,
    user_id: str,
    case_id: str,
    local_day: str,
) -> None:
    identities = (
        f"case-followup-quota:user:{tenant_id}:{user_id}:{local_day}",
        f"case-followup-quota:case:{tenant_id}:{case_id}:{local_day}",
    )
    keys = sorted(
        int.from_bytes(uuid5(NAMESPACE_URL, value).bytes[:8], "big", signed=True)
        for value in identities
    )
    for key in keys:
        await session.execute(select(func.pg_advisory_xact_lock(key)))


async def apply_case_followup_provider_acceptance(
    session: AsyncSession,
    event: NotificationOutbox,
    *,
    now: datetime,
) -> CaseFollowupPending:
    if event.message_type != "case_lifecycle_followup":
        raise ValueError("notification is not a lifecycle follow-up")
    if not event.external_message_id:
        raise ValueError("provider message id is required")
    task = await session.scalar(
        select(CaseFollowupTask)
        .where(
            CaseFollowupTask.tenant_id == event.tenant_id,
            CaseFollowupTask.followup_id == event.candidate_id,
        )
        .with_for_update()
    )
    if task is None:
        raise RuntimeError("follow-up task missing for accepted notification")
    is_reminder = str((event.message_json or {}).get("is_reminder") or "") == "true"
    if is_reminder:
        if (
            task.task_status != "waiting_for_reply"
            or task.response_status != "awaiting_input"
            or task.message_status not in {
                "accepted_by_provider", "delivery_confirmed"
            }
            or not task.pending_id
        ):
            raise RuntimeError("follow-up reminder no longer has an active pending")
        pending = await session.get(CaseFollowupPending, task.pending_id)
        if pending is None or pending.status not in {"active", "awaiting_input"}:
            raise RuntimeError("follow-up reminder pending is no longer active")
        expected_number = task.reminder_count + 1
        if int((event.message_json or {}).get("reminder_number") or 0) != expected_number:
            raise RuntimeError("follow-up reminder sequence changed")
        task.reminder_count = expected_number
        task.last_sent_at = now
        task.version += 1
        task.updated_at = now
        await session.flush()
        return pending
    case = await session.get(Agent2Case, task.case_id)
    if case is None or case.tenant_id != task.tenant_id:
        raise RuntimeError("follow-up case missing for accepted notification")
    conversation_state = await session.scalar(
        select(Agent2ConversationState).where(
            Agent2ConversationState.user_key == (
                f"{task.tenant_id}:{task.assigned_user_id}"
            ),
            Agent2ConversationState.conversation_id == task.conversation_id,
        )
    )
    expected_state_version = (
        conversation_state.version if conversation_state is not None else 0
    )
    accepted = accept_provider_receipt(
        _dispatch_state(task, case.case_name),
        external_message_id=event.external_message_id,
        now=now,
        expected_state_version=expected_state_version,
    )
    ledger_transition = await focus_followup_and_suspend_current_task(
        session,
        followup_task_id=task.followup_id,
        now=now,
        provider_receipt_succeeded=True,
    )
    if ledger_transition.status != "transitioned":
        raise RuntimeError(
            f"case follow-up task ledger focus blocked: {ledger_transition.reason_code}"
        )
    pending = accepted.pending
    await session.execute(
        insert(CaseFollowupPending)
        .values(
            pending_id=UUID(pending.pending_id),
            pending_type="case_followup",
            tenant_id=pending.tenant_id,
            user_id=pending.user_id,
            conversation_id=pending.conversation_id,
            domain="case",
            operation="answer_case_followup",
            source_turn_id=f"followup:{task.followup_id}",
            source_message_id=pending.source_message_id,
            task_id=UUID(pending.task_id),
            case_id=UUID(pending.case_id),
            followup_id=UUID(pending.followup_id),
            candidate_refs_json=[pending.case_id],
            candidate_versions_json={pending.case_id: pending.case_version},
            candidate_labels_json={pending.case_id: case.case_name},
            acceptable_answer_forms_json={"type": "natural_case_followup_reply"},
            expected_state_version=pending.expected_state_version,
            expires_at=pending.expires_at,
            status=pending.status,
            idempotency_key=(
                f"case-followup-pending:{pending.tenant_id}:{pending.followup_id}:"
                f"{pending.source_message_id}"
            ),
            version=1,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(
            index_elements=[CaseFollowupPending.tenant_id, CaseFollowupPending.idempotency_key]
        )
    )
    task.task_status = accepted.state_after.task_status
    task.message_status = accepted.state_after.message_status
    task.response_status = accepted.state_after.response_status
    task.provider_message_id = accepted.state_after.provider_message_id
    task.pending_id = UUID(pending.pending_id)
    task.last_sent_at = now
    task.version = accepted.state_after.version
    task.updated_at = now
    await session.flush()
    stored = await session.get(CaseFollowupPending, UUID(pending.pending_id))
    if stored is None:
        raise RuntimeError("follow-up pending could not be loaded")
    return stored


async def reconcile_case_followup_provider_acceptances(
    session: AsyncSession,
    *,
    now: datetime,
    allowed_tenant_ids: tuple[str, ...],
    allowed_user_ids: tuple[str, ...],
    limit: int = 50,
) -> int:
    """Retry only the DB state transition after provider evidence was committed."""
    tenant_ids = tuple(dict.fromkeys(filter(None, allowed_tenant_ids)))
    user_ids = tuple(dict.fromkeys(filter(None, allowed_user_ids)))
    if not tenant_ids or not user_ids:
        return 0
    events = tuple((await session.scalars(
        select(NotificationOutbox).where(
            NotificationOutbox.tenant_id.in_(tenant_ids),
            NotificationOutbox.recipient_user_id.in_(user_ids),
            NotificationOutbox.message_type == "case_lifecycle_followup",
            NotificationOutbox.status == "sent",
            NotificationOutbox.external_message_id != "",
        ).order_by(NotificationOutbox.updated_at).limit(max(1, limit))
        .with_for_update(skip_locked=True)
    )).all())
    advanced = 0
    for event in events:
        task = await session.scalar(select(CaseFollowupTask).where(
            CaseFollowupTask.tenant_id == event.tenant_id,
            CaseFollowupTask.followup_id == event.candidate_id,
        ).with_for_update())
        if task is None:
            continue
        is_reminder = str((event.message_json or {}).get("is_reminder") or "") == "true"
        if is_reminder:
            expected = int((event.message_json or {}).get("reminder_number") or 0)
            if task.reminder_count >= expected:
                continue
        elif (
            task.pending_id is not None
            and task.task_status == "waiting_for_reply"
            and task.message_status in {"accepted_by_provider", "delivery_confirmed"}
        ):
            continue
        await apply_case_followup_provider_acceptance(session, event, now=now)
        advanced += 1
    await session.flush()
    return advanced
