from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent2.business.models import (
    Agent2TaskLedgerEntry,
    CaseFollowupPending,
    CaseFollowupTask,
    NotificationOutbox,
)


class FollowupTaskLike(Protocol):
    followup_id: object
    trigger_type: str
    question_type: str
    task_status: str


@dataclass(frozen=True)
class MeaningfulProgressInvalidationPlan:
    cancel_task_ids: tuple[str, ...]
    cancel_pending_for_task_ids: tuple[str, ...]
    cancel_unsent_outbox_for_task_ids: tuple[str, ...]


def plan_meaningful_progress_invalidation(
    tasks: Iterable[FollowupTaskLike],
    *,
    current_followup_id: str,
) -> MeaningfulProgressInvalidationPlan:
    """Identify cadence questions made obsolete by a committed progress fact.

    Event-specific questions such as hearing readiness remain valid.  The task
    that produced the current reply is settled by the case executor and is not
    invalidated a second time.
    """
    cancellable_states = {"scheduled", "queued", "sending", "waiting_for_reply"}
    selected = tuple(
        item
        for item in tasks
        if str(item.followup_id) != current_followup_id
        and item.trigger_type == "fixed_cadence"
        and item.question_type == "meaningful_progress"
        and item.task_status in cancellable_states
    )
    return MeaningfulProgressInvalidationPlan(
        cancel_task_ids=tuple(str(item.followup_id) for item in selected),
        cancel_pending_for_task_ids=tuple(
            str(item.followup_id)
            for item in selected
            if item.task_status == "waiting_for_reply"
        ),
        cancel_unsent_outbox_for_task_ids=tuple(
            str(item.followup_id)
            for item in selected
            if item.task_status in {"scheduled", "queued", "sending"}
        ),
    )


async def invalidate_stale_cadence_followups(
    session: AsyncSession,
    *,
    tenant_id: str,
    case_id: UUID,
    assigned_user_id: str,
    now: datetime,
    current_followup_id: str = "",
) -> MeaningfulProgressInvalidationPlan:
    """Cancel obsolete cadence Task/Pending/unsent Outbox rows in one transaction."""
    tasks = tuple(
        (
            await session.scalars(
                select(CaseFollowupTask)
                .where(
                    CaseFollowupTask.tenant_id == tenant_id,
                    CaseFollowupTask.case_id == case_id,
                    CaseFollowupTask.assigned_user_id == assigned_user_id,
                    CaseFollowupTask.task_status.in_((
                        "scheduled", "queued", "sending", "waiting_for_reply"
                    )),
                )
                .with_for_update()
            )
        ).all()
    )
    plan = plan_meaningful_progress_invalidation(
        tasks, current_followup_id=current_followup_id
    )
    by_id = {str(item.followup_id): item for item in tasks}
    for task_id in plan.cancel_task_ids:
        task = by_id[task_id]
        task.task_status = "cancelled"
        task.message_status = (
            "cancelled"
            if task.message_status in {"scheduled", "queued", "sending"}
            else task.message_status
        )
        task.response_status = "cancelled"
        task.cancelled_at = now
        task.version += 1
        task.updated_at = now
        ledger = await session.get(Agent2TaskLedgerEntry, task.followup_id)
        if ledger is not None and ledger.status not in {
            "completed", "cancelled", "failed", "expired"
        }:
            ledger.status = "cancelled"
            ledger.focus_state = "active"
            ledger.version += 1
            ledger.updated_at = now

    for task_id in plan.cancel_pending_for_task_ids:
        pendings = tuple(
            (
                await session.scalars(
                    select(CaseFollowupPending)
                    .where(
                        CaseFollowupPending.tenant_id == tenant_id,
                        CaseFollowupPending.followup_id == UUID(task_id),
                        CaseFollowupPending.status.in_(("active", "awaiting_input")),
                    )
                    .with_for_update()
                )
            ).all()
        )
        for pending in pendings:
            pending.status = "cancelled"
            pending.cancelled_at = now
            pending.version += 1
            pending.updated_at = now

    for task_id in plan.cancel_unsent_outbox_for_task_ids:
        events = tuple(
            (
                await session.scalars(
                    select(NotificationOutbox)
                    .where(
                        NotificationOutbox.tenant_id == tenant_id,
                        NotificationOutbox.candidate_id == UUID(task_id),
                        NotificationOutbox.message_type == "case_lifecycle_followup",
                        NotificationOutbox.status.in_(("pending", "processing", "failed")),
                    )
                    .with_for_update()
                )
            ).all()
        )
        for event in events:
            event.status = "cancelled"
            event.error_message = "cancelled_after_meaningful_progress"
            event.locked_by = ""
            event.locked_at = None
            event.next_retry_at = None
            event.updated_at = now

    await session.flush()
    return plan


async def expire_due_case_followups(
    session: AsyncSession,
    *,
    now: datetime,
    allowed_tenant_ids: tuple[str, ...],
    allowed_user_ids: tuple[str, ...],
    allowed_case_ids: tuple[str, ...],
) -> int:
    """Expire bounded cohort tasks and their Pending/Ledger state atomically."""
    try:
        case_ids = tuple(UUID(value) for value in dict.fromkeys(allowed_case_ids))
    except ValueError:
        return 0
    tenant_ids = tuple(dict.fromkeys(filter(None, allowed_tenant_ids)))
    user_ids = tuple(dict.fromkeys(filter(None, allowed_user_ids)))
    if not tenant_ids or not user_ids or not case_ids:
        return 0
    tasks = tuple((await session.scalars(
        select(CaseFollowupTask).where(
            CaseFollowupTask.tenant_id.in_(tenant_ids),
            CaseFollowupTask.assigned_user_id.in_(user_ids),
            CaseFollowupTask.case_id.in_(case_ids),
            CaseFollowupTask.task_status.in_((
                "scheduled", "queued", "sending", "waiting_for_reply", "snoozed"
            )),
            CaseFollowupTask.expires_at <= now,
        ).with_for_update(skip_locked=True)
    )).all())
    for task in tasks:
        task.task_status = "expired"
        if task.message_status in {"scheduled", "queued", "sending"}:
            task.message_status = "cancelled"
        task.response_status = "expired"
        task.version += 1
        task.updated_at = now
        pendings = tuple((await session.scalars(
            select(CaseFollowupPending).where(
                CaseFollowupPending.tenant_id == task.tenant_id,
                CaseFollowupPending.followup_id == task.followup_id,
                CaseFollowupPending.status.in_(("active", "awaiting_input")),
            ).with_for_update()
        )).all())
        for pending in pendings:
            pending.status = "expired"
            pending.version += 1
            pending.updated_at = now
        ledger = await session.get(Agent2TaskLedgerEntry, task.followup_id)
        if ledger is not None and ledger.status not in {
            "completed", "cancelled", "failed", "expired"
        }:
            ledger.status = "expired"
            ledger.focus_state = "active"
            ledger.version += 1
            ledger.updated_at = now
        events = tuple((await session.scalars(
            select(NotificationOutbox).where(
                NotificationOutbox.tenant_id == task.tenant_id,
                NotificationOutbox.candidate_id == task.followup_id,
                NotificationOutbox.message_type == "case_lifecycle_followup",
                NotificationOutbox.status.in_(("pending", "processing", "failed")),
            ).with_for_update()
        )).all())
        for event in events:
            event.status = "cancelled"
            event.error_message = "case_followup_expired"
            event.locked_by = ""
            event.locked_at = None
            event.next_retry_at = None
            event.updated_at = now
    await session.flush()
    return len(tasks)
