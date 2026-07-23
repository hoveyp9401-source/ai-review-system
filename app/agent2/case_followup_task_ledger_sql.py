from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent2.business.models import Agent2TaskLedgerEntry


_REPORT_TASK_NAMESPACE = UUID("1eb9d37f-4cec-5bc4-b22b-c74b16bbc506")
_OPEN_REPORT_STATUSES = frozenset({"collecting", "pending_confirmation"})
_CLOSED_REPORT_STATUSES = frozenset({"completed", "cancelled"})


def _task_status_for_report(report_status: str) -> str:
    if report_status == "collecting":
        return "active"
    if report_status == "pending_confirmation":
        return "awaiting_input"
    return report_status


@dataclass(frozen=True)
class TaskLedgerSqlTransition:
    status: str
    reason_code: str
    restored_task_id: str = ""


def report_task_ledger_id(
    *,
    report_id: UUID,
    tenant_id: str,
    user_id: str,
    conversation_id: str,
    report_type: str,
    period_key: str,
) -> UUID:
    """Return a stable Task Ledger ID for one report in one conversation."""
    payload = json.dumps(
        {
            "conversation_id": conversation_id,
            "period_key": period_key,
            "report_id": str(report_id),
            "report_type": report_type,
            "tenant_id": tenant_id,
            "user_id": user_id,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return uuid5(_REPORT_TASK_NAMESPACE, payload)


async def sync_focused_report_task(
    session: AsyncSession,
    *,
    task_id: UUID,
    tenant_id: str,
    user_id: str,
    conversation_id: str,
    source_turn_id: str,
    report_type: str,
    period_key: str,
    report_status: str,
    now: datetime,
    expires_at: datetime | None = None,
) -> TaskLedgerSqlTransition:
    """Persist report focus only from a successful report receipt path."""
    if not conversation_id:
        return TaskLedgerSqlTransition("blocked", "conversation_scope_required")
    if report_type not in {"daily", "weekly", "monthly"}:
        return TaskLedgerSqlTransition("blocked", "unknown_report_type")
    if report_status not in _OPEN_REPORT_STATUSES | _CLOSED_REPORT_STATUSES:
        return TaskLedgerSqlTransition("blocked", "unknown_report_status")
    scoped_task_id = report_task_ledger_id(
        report_id=task_id,
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
        report_type=report_type,
        period_key=period_key,
    )
    entries = tuple(
        (
            await session.scalars(
                select(Agent2TaskLedgerEntry)
                .where(
                    Agent2TaskLedgerEntry.tenant_id == tenant_id,
                    Agent2TaskLedgerEntry.user_id == user_id,
                    Agent2TaskLedgerEntry.conversation_id == conversation_id,
                )
                .with_for_update()
            )
        ).all()
    )
    source_report_id = str(task_id)
    current = next(
        (
            item
            for item in entries
            if item.domain == "report"
            and (
                item.task_id in {scoped_task_id, task_id}
                or str((item.object_ref_json or {}).get("source_report_id") or "")
                == source_report_id
            )
        ),
        None,
    )
    current_task_id = current.task_id if current is not None else scoped_task_id
    focused = tuple(
        item for item in entries
        if item.task_id != current_task_id
        and item.focus_state == "focused"
        and item.status in {"active", "awaiting_input"}
    )
    if report_status in _OPEN_REPORT_STATUSES and len(focused) > 1:
        return TaskLedgerSqlTransition("blocked", "multiple_focused_tasks")
    if current is None:
        current = Agent2TaskLedgerEntry(
            task_id=scoped_task_id, tenant_id=tenant_id, user_id=user_id,
            conversation_id=conversation_id, domain="report", operation="collect",
            object_ref_json={
                "report_type": report_type,
                "period_key": period_key,
                "source_report_id": source_report_id,
            },
            status=_task_status_for_report(report_status),
            focus_state=(
                "focused" if report_status in _OPEN_REPORT_STATUSES else "active"
            ),
            version=1, source_turn_id=source_turn_id,
            pending_requirements_json={},
            resume_policy_json={
                "mode": "restore_previous", "report_status": report_status,
            },
            expires_at=expires_at, created_at=now, updated_at=now,
        )
        session.add(current)
    else:
        current.object_ref_json = {
            **dict(current.object_ref_json or {}),
            "report_type": report_type,
            "period_key": period_key,
            "source_report_id": source_report_id,
        }
        current.status = _task_status_for_report(report_status)
        current.focus_state = (
            "focused" if report_status in _OPEN_REPORT_STATUSES else "active"
        )
        current.resume_policy_json = {
            **dict(current.resume_policy_json or {}),
            "mode": "restore_previous", "report_status": report_status,
        }
        current.expires_at = expires_at
        current.version += 1
        current.updated_at = now
    if report_status in _OPEN_REPORT_STATUSES and focused:
        previous = focused[0]
        previous.status = "suspended"
        previous.focus_state = "suspended"
        previous.version += 1
        previous.updated_at = now
    await session.flush()
    return TaskLedgerSqlTransition(
        "transitioned",
        (
            "report_focused"
            if report_status in _OPEN_REPORT_STATUSES
            else "report_closed"
        ),
        str(current.task_id),
    )


async def _scoped_entries(
    session: AsyncSession,
    followup: Agent2TaskLedgerEntry,
) -> tuple[Agent2TaskLedgerEntry, ...]:
    return tuple(
        (
            await session.scalars(
                select(Agent2TaskLedgerEntry)
                .where(
                    Agent2TaskLedgerEntry.tenant_id == followup.tenant_id,
                    Agent2TaskLedgerEntry.user_id == followup.user_id,
                    Agent2TaskLedgerEntry.conversation_id == followup.conversation_id,
                )
                .with_for_update()
            )
        ).all()
    )


async def focus_followup_and_suspend_current_task(
    session: AsyncSession,
    *,
    followup_task_id: UUID,
    now: datetime,
    provider_receipt_succeeded: bool,
) -> TaskLedgerSqlTransition:
    if not provider_receipt_succeeded:
        return TaskLedgerSqlTransition("blocked", "provider_receipt_not_succeeded")
    followup = await session.get(Agent2TaskLedgerEntry, followup_task_id)
    if followup is None or followup.domain != "case_followup":
        return TaskLedgerSqlTransition("blocked", "followup_task_not_found")
    entries = await _scoped_entries(session, followup)
    focused = tuple(
        item
        for item in entries
        if item.task_id != followup.task_id
        and item.focus_state == "focused"
        and item.status in {"active", "awaiting_input"}
    )
    if len(focused) > 1:
        return TaskLedgerSqlTransition("blocked", "multiple_focused_tasks")
    if focused:
        previous = focused[0]
        previous.status = "suspended"
        previous.focus_state = "suspended"
        previous.version += 1
        previous.updated_at = now
        followup.resume_policy_json = {
            **dict(followup.resume_policy_json or {}),
            "mode": "restore_exact_previous",
            "previous_task_id": str(previous.task_id),
            "previous_task_version": previous.version,
        }
    followup.status = "awaiting_input"
    followup.focus_state = "focused"
    followup.version += 1
    followup.updated_at = now
    await session.flush()
    return TaskLedgerSqlTransition("transitioned", "followup_focused")


async def complete_followup_and_restore_report(
    session: AsyncSession,
    *,
    followup_task_id: UUID,
    now: datetime,
    case_receipt_succeeded: bool,
) -> TaskLedgerSqlTransition:
    if not case_receipt_succeeded:
        return TaskLedgerSqlTransition("blocked", "case_receipt_not_succeeded")
    followup = await session.get(Agent2TaskLedgerEntry, followup_task_id)
    if followup is None or followup.domain != "case_followup":
        return TaskLedgerSqlTransition("blocked", "followup_task_not_found")
    if followup.focus_state != "focused" or followup.status != "awaiting_input":
        return TaskLedgerSqlTransition("blocked", "followup_not_focused")
    entries = await _scoped_entries(session, followup)
    resumable = tuple(
        item
        for item in entries
        if item.domain == "report"
        and item.status == "suspended"
        and item.focus_state == "suspended"
        and str((item.resume_policy_json or {}).get("report_status") or "")
        in _OPEN_REPORT_STATUSES
        and (item.expires_at is None or item.expires_at > now)
    )
    exact_previous_task_id = str(
        (followup.resume_policy_json or {}).get("previous_task_id") or ""
    )
    if exact_previous_task_id:
        exact_entry = next(
            (item for item in entries if str(item.task_id) == exact_previous_task_id),
            None,
        )
        expected_version = int(
            (followup.resume_policy_json or {}).get("previous_task_version") or -1
        )
        if exact_entry is None:
            return TaskLedgerSqlTransition("blocked", "previous_task_not_found")
        if exact_entry.version != expected_version:
            return TaskLedgerSqlTransition("blocked", "previous_task_version_changed")
        resumable = tuple(
            item for item in resumable if str(item.task_id) == exact_previous_task_id
        )
    followup.status = "completed"
    followup.focus_state = "active"
    followup.version += 1
    followup.updated_at = now
    if len(resumable) > 1:
        await session.flush()
        return TaskLedgerSqlTransition(
            "clarification_required", "multiple_resumable_tasks"
        )
    if not resumable:
        await session.flush()
        return TaskLedgerSqlTransition("transitioned", "followup_completed")
    restored = resumable[0]
    restored.status = _task_status_for_report(
        str((restored.resume_policy_json or {}).get("report_status") or "")
    )
    restored.focus_state = "focused"
    restored.version += 1
    restored.updated_at = now
    await session.flush()
    return TaskLedgerSqlTransition(
        "transitioned", "previous_report_restored", str(restored.task_id)
    )
