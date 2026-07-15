from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent2.business.models import (
    Agent2Case,
    Agent2IdentityBinding,
    BusinessCommandReceipt,
    CaseFollowupPolicy,
    CaseFollowupTask,
)
from app.agent2.case_lifecycle_followup import (
    CaseFollowupPolicySnapshot,
    CaseFollowupSubject,
    CaseFollowupTaskPlan,
    CaseFollowupTaskSnapshot,
    CommittedCaseLifecycleChange,
    FollowupPolicyEngine,
    TriggerPolicyMatrix,
    build_hearing_triggers,
    build_followup_task_plan,
)


def lifecycle_triggers_from_receipt(receipt: BusinessCommandReceipt):
    """Translate a committed lifecycle receipt into closed-set event triggers."""
    after = dict(receipt.after_json or {})
    change = after.get("lifecycle_change")
    case_id = str(after.get("case_id") or "").strip()
    if not isinstance(change, dict) or not case_id:
        return ()
    raw_occurred_at = str(change.get("occurred_at") or "").strip()
    try:
        occurred_at = datetime.fromisoformat(raw_occurred_at.replace("Z", "+00:00"))
        case_version = int(change.get("case_version"))
    except (TypeError, ValueError):
        return ()
    if occurred_at.tzinfo is None or case_version < 1:
        return ()
    committed = CommittedCaseLifecycleChange(
        tenant_id=receipt.tenant_id,
        case_id=case_id,
        case_type=str(change.get("case_type") or ""),
        receipt_id=str(receipt.receipt_id),
        receipt_status=receipt.status,
        actual_write=bool(receipt.actual_write),
        occurred_at=occurred_at,
        from_stage=str(change.get("from_stage") or ""),
        to_stage=str(change.get("to_stage") or ""),
        node=str(change.get("node") or ""),
        case_version=case_version,
    )
    return TriggerPolicyMatrix().from_committed_change(committed)


def hearing_triggers_from_case_source(
    source: dict,
    *,
    case_version: int,
):
    event_id = str(
        source.get("hearing_event_id") or source.get("hearing_id") or ""
    ).strip()
    raw_at = str(source.get("hearing_at") or "").strip()
    if not event_id or not raw_at:
        return ()
    try:
        hearing_at = datetime.fromisoformat(raw_at.replace("Z", "+00:00"))
    except ValueError:
        return ()
    if hearing_at.tzinfo is None:
        return ()
    return build_hearing_triggers(
        hearing_event_id=f"{event_id}:v{case_version}",
        hearing_at=hearing_at,
    )


async def plan_due_case_followups(
    session: AsyncSession,
    *,
    now: datetime,
    allowed_tenant_ids: tuple[str, ...],
    allowed_user_ids: tuple[str, ...],
    allowed_case_ids: tuple[str, ...],
    allowed_trigger_types: tuple[str, ...] = (),
    limit: int = 200,
) -> tuple[CaseFollowupTaskPlan, ...]:
    supported_triggers = {
        "fixed_cadence", "hearing_proximity", "hearing_result",
        "stage_transition", "node_transition", "manual",
    }
    trigger_allowlist = {
        value for value in allowed_trigger_types if value in supported_triggers
    }
    if not trigger_allowlist:
        return ()
    tenant_ids = tuple(dict.fromkeys(filter(None, allowed_tenant_ids)))
    user_ids = tuple(dict.fromkeys(filter(None, allowed_user_ids)))
    try:
        case_ids = tuple(UUID(value) for value in dict.fromkeys(filter(None, allowed_case_ids)))
    except ValueError:
        return ()
    if not tenant_ids or not user_ids or not case_ids:
        return ()
    policies = (
        await session.scalars(
            select(CaseFollowupPolicy)
            .where(
                CaseFollowupPolicy.tenant_id.in_(tenant_ids),
                CaseFollowupPolicy.assigned_user_id.in_(user_ids),
                CaseFollowupPolicy.case_id.in_(case_ids),
                CaseFollowupPolicy.enabled.is_(True),
                CaseFollowupPolicy.cadence_type.notin_(("disabled", "paused")),
            )
            .order_by(CaseFollowupPolicy.next_due_at, CaseFollowupPolicy.policy_id)
            .limit(max(1, limit))
        )
    ).all()
    lifecycle_receipts = (
        await session.scalars(
            select(BusinessCommandReceipt)
            .where(
                BusinessCommandReceipt.tenant_id.in_(tenant_ids),
                BusinessCommandReceipt.actor_user_id.in_(user_ids),
                BusinessCommandReceipt.command_type == "create_case_progress",
                BusinessCommandReceipt.status == "executed",
                BusinessCommandReceipt.actual_write.is_(True),
                BusinessCommandReceipt.after_json.has_key("lifecycle_change"),  # noqa: W601
            )
            .order_by(BusinessCommandReceipt.created_at.desc())
            .limit(max(1, limit * 10))
        )
    ).all()
    lifecycle_by_case: dict[tuple[str, str, str], list] = {}
    for receipt in lifecycle_receipts:
        after = dict(receipt.after_json or {})
        key = (
            receipt.tenant_id,
            receipt.actor_user_id,
            str(after.get("case_id") or ""),
        )
        lifecycle_by_case.setdefault(key, []).extend(
            lifecycle_triggers_from_receipt(receipt)
        )
    engine = FollowupPolicyEngine()
    plans: list[CaseFollowupTaskPlan] = []
    for policy in policies:
        if policy.snoozed_until is not None and now < policy.snoozed_until:
            continue
        case = await session.scalar(
            select(Agent2Case).where(
                Agent2Case.tenant_id == policy.tenant_id,
                Agent2Case.case_id == policy.case_id,
                Agent2Case.owner_user_id == policy.assigned_user_id,
            )
        )
        binding = await session.scalar(
            select(Agent2IdentityBinding).where(
                Agent2IdentityBinding.tenant_id == policy.tenant_id,
                Agent2IdentityBinding.user_id == policy.assigned_user_id,
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
        if case is None or binding is None or str(policy.case_id) not in permitted:
            continue
        existing = (
            await session.scalars(
                select(CaseFollowupTask).where(
                    CaseFollowupTask.tenant_id == policy.tenant_id,
                    CaseFollowupTask.case_id == policy.case_id,
                    CaseFollowupTask.assigned_user_id == policy.assigned_user_id,
                    CaseFollowupTask.task_status.in_((
                        "scheduled", "queued", "sending", "waiting_for_reply"
                    )),
                )
            )
        ).all()
        existing_snapshots = tuple(
            CaseFollowupTaskSnapshot(
                str(item.followup_id), item.tenant_id, str(item.case_id),
                item.assigned_user_id, item.trigger_type, item.question_type,
                item.task_status,
            )
            for item in existing
        )
        source = dict(case.source_json or {})
        subject = CaseFollowupSubject(
            tenant_id=case.tenant_id, case_id=str(case.case_id),
            assigned_user_id=case.owner_user_id, case_type=case.case_type,
            stage=str(source.get("major_stage") or source.get("stage") or ""),
            node=str(source.get("minor_stage") or source.get("node") or ""),
            case_version=case.version, case_name=case.case_name,
        )
        snapshot = CaseFollowupPolicySnapshot(
            policy_id=str(policy.policy_id), tenant_id=policy.tenant_id,
            case_id=str(policy.case_id), assigned_user_id=policy.assigned_user_id,
            enabled=policy.enabled, cadence_type=policy.cadence_type,
            timezone=policy.timezone,
            last_meaningful_progress_at=policy.last_meaningful_progress_at,
            next_due_at=policy.next_due_at, version=policy.version,
            policy_source=policy.policy_source,
            event_triggers_enabled=policy.event_triggers_enabled,
            hearing_reminders_enabled=policy.hearing_reminders_enabled,
            stage_transition_enabled=policy.stage_transition_enabled,
            node_transition_enabled=policy.node_transition_enabled,
            business_days_only=policy.business_days_only,
            custom_interval_days=policy.cadence_days,
            snoozed_until=policy.snoozed_until,
        )
        triggers = hearing_triggers_from_case_source(
            source, case_version=case.version
        )
        triggers = (*triggers, *lifecycle_by_case.get(
            (policy.tenant_id, policy.assigned_user_id, str(policy.case_id)), ()
        ))
        triggers = tuple(
            item for item in triggers if item.trigger_type in trigger_allowlist
        )
        evaluation_snapshot = (
            snapshot
            if "fixed_cadence" in trigger_allowlist
            else replace(snapshot, cadence_type="event_only")
        )
        evaluation = engine.evaluate_triggers(
            subject, evaluation_snapshot, triggers=triggers, now=now,
            existing_tasks=existing_snapshots,
        )
        if not evaluation.eligible:
            continue
        plans.append(
            build_followup_task_plan(
                subject, evaluation, expires_at=now + timedelta(days=7),
                policy_id=str(policy.policy_id),
            )
        )
    return tuple(plans)
