from __future__ import annotations

from datetime import datetime, timezone
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent2.business.models import (
    Agent2Case,
    Agent2IdentityBinding,
    Agent2TaskLedgerEntry,
    BusinessAuditEvent,
    BusinessCommandReceipt,
    CaseFollowupTask,
    NotificationOutbox,
)
from app.agent2.case_followup_commands import CancelCaseFollowupTask


async def cancel_unsent_case_followup_task(
    session: AsyncSession,
    command: CancelCaseFollowupTask,
    *,
    actor_user_id: str,
    actor_is_tenant_admin: bool,
    now: datetime | None = None,
) -> BusinessCommandReceipt:
    now = now or datetime.now(timezone.utc)
    existing = await session.scalar(select(BusinessCommandReceipt).where(
        BusinessCommandReceipt.tenant_id == command.tenant_id,
        BusinessCommandReceipt.idempotency_key == command.idempotency_key,
    ))
    if existing is not None:
        return existing
    try:
        case_id = UUID(command.case_id)
        followup_id = UUID(command.followup_id)
    except ValueError:
        return await _receipt(
            session, command, actor_user_id=actor_user_id, now=now,
            status="failed", error_code="followup_not_found",
            failed_stage="validation", actual_write=False,
        )
    case = await session.scalar(select(Agent2Case).where(
        Agent2Case.tenant_id == command.tenant_id,
        Agent2Case.case_id == case_id,
        Agent2Case.owner_user_id == command.assigned_user_id,
    ))
    binding = await session.scalar(select(Agent2IdentityBinding).where(
        Agent2IdentityBinding.tenant_id == command.tenant_id,
        Agent2IdentityBinding.user_id == command.assigned_user_id,
        Agent2IdentityBinding.active.is_(True),
    ))
    allowed = {
        str(value) for value in ((binding.permission_scope_json if binding else {}) or {}).get(
            "allowed_case_ids", []
        ) if value
    }
    if (
        not actor_is_tenant_admin or case is None or binding is None
        or command.case_id not in allowed
    ):
        return await _receipt(
            session, command, actor_user_id=actor_user_id, now=now,
            status="failed", error_code="permission_denied",
            failed_stage="authorization", actual_write=False,
        )
    task = await session.scalar(select(CaseFollowupTask).where(
        CaseFollowupTask.tenant_id == command.tenant_id,
        CaseFollowupTask.followup_id == followup_id,
        CaseFollowupTask.case_id == case_id,
        CaseFollowupTask.assigned_user_id == command.assigned_user_id,
    ).with_for_update())
    if task is None:
        return await _receipt(
            session, command, actor_user_id=actor_user_id, now=now,
            status="failed", error_code="followup_not_found",
            failed_stage="validation", actual_write=False,
        )
    if task.version != command.expected_version:
        return await _receipt(
            session, command, actor_user_id=actor_user_id, now=now,
            status="failed", error_code="version_conflict",
            failed_stage="optimistic_lock", actual_write=False,
        )
    events = tuple((await session.scalars(select(NotificationOutbox).where(
        NotificationOutbox.tenant_id == command.tenant_id,
        NotificationOutbox.candidate_id == followup_id,
        NotificationOutbox.message_type == "case_lifecycle_followup",
    ).with_for_update())).all())
    if task.task_status not in {"scheduled", "queued"} or any(
        item.status in {"processing", "sent"} or bool(item.external_message_id)
        for item in events
    ):
        return await _receipt(
            session, command, actor_user_id=actor_user_id, now=now,
            status="failed", error_code="followup_already_sending_or_sent",
            failed_stage="message_state", actual_write=False,
        )
    before = {
        "followup_id": command.followup_id, "task_status": task.task_status,
        "message_status": task.message_status, "version": task.version,
    }
    task.task_status = "cancelled"
    task.message_status = "cancelled"
    task.response_status = "cancelled"
    task.cancelled_at = now
    task.version += 1
    task.updated_at = now
    for event in events:
        if event.status in {"pending", "failed"}:
            event.status = "cancelled"
            event.error_message = "cancelled_by_legal_ops"
            event.next_retry_at = None
            event.updated_at = now
    ledger = await session.get(Agent2TaskLedgerEntry, followup_id)
    if ledger is not None and ledger.status not in {
        "completed", "cancelled", "failed", "expired"
    }:
        ledger.status = "cancelled"
        ledger.focus_state = "active"
        ledger.version += 1
        ledger.updated_at = now
    after = {
        "followup_id": command.followup_id, "task_status": task.task_status,
        "message_status": task.message_status, "version": task.version,
    }
    return await _receipt(
        session, command, actor_user_id=actor_user_id, now=now,
        status="executed", error_code="", failed_stage="", actual_write=True,
        before=before, after=after,
    )


async def _receipt(
    session: AsyncSession,
    command: CancelCaseFollowupTask,
    *,
    actor_user_id: str,
    now: datetime,
    status: str,
    error_code: str,
    failed_stage: str,
    actual_write: bool,
    before: dict | None = None,
    after: dict | None = None,
) -> BusinessCommandReceipt:
    before = before or {}
    after = after or {}
    receipt_id = uuid5(NAMESPACE_URL, f"cancel-followup-receipt:{command.idempotency_key}")
    receipt = BusinessCommandReceipt(
        receipt_id=receipt_id, tenant_id=command.tenant_id,
        command_id=command.command_id, command_type=command.command_type,
        actor_user_id=actor_user_id, source_message_id=command.source_turn_id,
        idempotency_key=command.idempotency_key, status=status,
        resource_type="case_followup_task",
        resource_id=(command.followup_id if actual_write else ""),
        before_json=before, after_json=after, error_code=error_code,
        failed_stage=failed_stage, actual_write=actual_write,
        created_at=now, updated_at=now,
    )
    audit = BusinessAuditEvent(
        audit_id=uuid5(NAMESPACE_URL, f"cancel-followup-audit:{receipt_id}"),
        tenant_id=command.tenant_id, receipt_id=receipt_id,
        actor_user_id=actor_user_id, source_message_id=command.source_turn_id,
        source_channel="legal_ops", command_type=command.command_type,
        resource_type="case_followup_task", resource_id=receipt.resource_id,
        before_json=before,
        after_json=(after if actual_write else {
            "status": status, "error_code": error_code,
            "failed_stage": failed_stage, "actual_write": False,
        }),
        created_at=now,
    )
    session.add(receipt)
    await session.flush()
    session.add(audit)
    await session.flush()
    return receipt
