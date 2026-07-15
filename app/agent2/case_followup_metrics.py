from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent2.business.models import (
    BusinessCommandReceipt,
    CaseFollowupPending,
    CaseFollowupPolicy,
    CaseFollowupTask,
    CaseReportProjection,
    NotificationOutbox,
    ReportProjectionRequest,
)


async def load_case_followup_metrics(
    session: AsyncSession,
    *,
    tenant_id: str,
    user_id: str = "",
    case_id: str = "",
    trigger_type: str = "",
    policy_type: str = "",
    start_at: datetime | None = None,
    end_at: datetime | None = None,
) -> dict:
    """Return tenant-fenced, reproducible counters and their evidence support."""
    case_uuid = UUID(case_id) if case_id else None
    policies = tuple((await session.scalars(_scope(
        select(CaseFollowupPolicy), CaseFollowupPolicy, tenant_id,
        user_id=user_id, case_uuid=case_uuid, start_at=start_at, end_at=end_at,
    ))).all())
    tasks = tuple((await session.scalars(_scope(
        select(CaseFollowupTask), CaseFollowupTask, tenant_id,
        user_id=user_id, case_uuid=case_uuid, start_at=start_at, end_at=end_at,
    ))).all())
    pending = tuple((await session.scalars(_scope(
        select(CaseFollowupPending), CaseFollowupPending, tenant_id,
        user_id=user_id, case_uuid=case_uuid, start_at=start_at, end_at=end_at,
    ))).all())
    receipts = tuple((await session.scalars(
        select(BusinessCommandReceipt).where(
            BusinessCommandReceipt.tenant_id == tenant_id,
            *(
                (BusinessCommandReceipt.actor_user_id == user_id,)
                if user_id else ()
            ),
            *((BusinessCommandReceipt.created_at >= start_at,) if start_at else ()),
            *((BusinessCommandReceipt.created_at <= end_at,) if end_at else ()),
        )
    )).all())
    requests = tuple((await session.scalars(_scope(
        select(ReportProjectionRequest), ReportProjectionRequest, tenant_id,
        user_id=user_id, case_uuid=case_uuid, start_at=start_at, end_at=end_at,
    ))).all())
    projections = tuple((await session.scalars(_scope(
        select(CaseReportProjection), CaseReportProjection, tenant_id,
        user_id=user_id, case_uuid=case_uuid, start_at=start_at, end_at=end_at,
    ))).all())
    notifications = tuple((await session.scalars(
        select(NotificationOutbox).where(
            NotificationOutbox.tenant_id == tenant_id,
            NotificationOutbox.message_type == "case_lifecycle_followup",
            *((NotificationOutbox.recipient_user_id == user_id,) if user_id else ()),
            *((NotificationOutbox.created_at >= start_at,) if start_at else ()),
            *((NotificationOutbox.created_at <= end_at,) if end_at else ()),
        )
    )).all())
    if trigger_type:
        tasks = tuple(item for item in tasks if item.trigger_type == trigger_type)
    if policy_type:
        policies = tuple(item for item in policies if item.cadence_type == policy_type)
        policy_ids = {item.policy_id for item in policies}
        tasks = tuple(item for item in tasks if item.policy_id in policy_ids)
    task_ids = {item.followup_id for item in tasks}
    if case_uuid is not None or trigger_type or policy_type:
        pending = tuple(item for item in pending if item.followup_id in task_ids)
        notifications = tuple(
            item for item in notifications if item.candidate_id in task_ids
        )
    error_codes = [str(item.error_code or "") for item in receipts]
    values = {
        "followup_policies_enabled": sum(item.enabled for item in policies),
        "followup_tasks_created": len(tasks),
        "followup_tasks_merged": sum(len(item.trigger_sources_json or []) > 1 for item in tasks),
        "followup_tasks_cancelled": sum(item.task_status == "cancelled" for item in tasks),
        "followup_messages_queued": sum(item.status in {"pending", "processing"} for item in notifications),
        "followup_messages_provider_accepted": sum(bool(item.external_message_id) and item.status == "sent" for item in notifications),
        "followup_messages_delivery_confirmed": sum(item.message_status == "delivery_confirmed" for item in tasks),
        "followup_send_failed": sum(item.status in {"failed", "dead_letter"} for item in notifications),
        "followup_replies_received": sum(item.response_status in {"answered", "snoozed"} for item in tasks),
        "followup_expired": sum(item.task_status == "expired" for item in tasks),
        "followup_snoozed": sum(item.task_status == "snoozed" for item in tasks),
        "wrong_user_context_blocked": sum("user" in code and "context" in code for code in error_codes),
        "wrong_tenant_context_blocked": sum("tenant" in code and "context" in code for code in error_codes),
        "ambiguous_reply_blocked": sum("clarification" in code or "ambiguous" in code for code in error_codes),
        "case_updates_succeeded": sum(item.command_type == "create_case_progress" and item.status == "executed" for item in receipts),
        "case_updates_failed": sum(item.command_type == "create_case_progress" and item.status in {"blocked", "failed"} for item in receipts),
        "report_projection_candidates": len(requests),
        "report_projections_succeeded": sum(item.status == "succeeded" for item in requests),
        "report_projections_skipped": sum(item.status == "skipped" for item in requests),
        "report_projections_failed": sum(item.status == "failed" for item in requests),
        "report_projection_undo": sum(item.status == "removed" for item in projections),
        "pending_version_conflicts": sum("version" in code for code in error_codes),
    }
    unavailable = (
        "duplicate_followups_prevented",
        "repeat_message_deduplicated",
    )
    values.update({name: 0 for name in unavailable})
    return {
        "tenant_id": tenant_id,
        "filters": {
            "user_id": user_id, "case_id": case_id,
            "trigger_type": trigger_type, "policy_type": policy_type,
            "start_at": start_at.isoformat() if start_at else None,
            "end_at": end_at.isoformat() if end_at else None,
        },
        "metrics": values,
        "evidence_unavailable": list(unavailable),
    }


def _scope(statement, model, tenant_id, *, user_id, case_uuid, start_at, end_at):
    conditions = [model.tenant_id == tenant_id]
    user_column = getattr(model, "assigned_user_id", None)
    if user_column is None:
        user_column = getattr(model, "user_id", None)
    if user_id and user_column is not None:
        conditions.append(user_column == user_id)
    case_column = getattr(model, "case_id", None)
    if case_uuid is not None and case_column is not None:
        conditions.append(case_column == case_uuid)
    created_column = getattr(model, "created_at", None)
    if start_at is not None and created_column is not None:
        conditions.append(created_column >= start_at)
    if end_at is not None and created_column is not None:
        conditions.append(created_column <= end_at)
    return statement.where(*conditions)
