from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent2.business.models import (
    Agent2Case,
    Agent2IdentityBinding,
    BusinessAuditEvent,
    BusinessCommandReceipt,
    CaseFollowupPolicy,
)
from app.agent2.case_followup_commands import (
    UpdateCaseFollowupPolicy,
    execute_policy_update,
)
from app.agent2.case_lifecycle_followup import CaseFollowupPolicySnapshot


def _snapshot(policy: CaseFollowupPolicy) -> CaseFollowupPolicySnapshot:
    return CaseFollowupPolicySnapshot(
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


async def apply_case_followup_policy_command(
    session: AsyncSession,
    command: UpdateCaseFollowupPolicy,
    *,
    actor_user_id: str,
    actor_is_tenant_admin: bool,
    now: datetime | None = None,
) -> BusinessCommandReceipt:
    now = now or datetime.now(timezone.utc)
    existing_receipt = await session.scalar(
        select(BusinessCommandReceipt).where(
            BusinessCommandReceipt.tenant_id == command.tenant_id,
            BusinessCommandReceipt.idempotency_key == command.idempotency_key,
        )
    )
    if existing_receipt is not None:
        return existing_receipt
    try:
        case_id = UUID(command.case_id)
    except ValueError:
        return await _failed_receipt(
            session, command, actor_user_id, now, "case_not_found", "validation"
        )
    case = await session.scalar(
        select(Agent2Case).where(
            Agent2Case.tenant_id == command.tenant_id,
            Agent2Case.case_id == case_id,
        )
    )
    binding = await session.scalar(
        select(Agent2IdentityBinding).where(
            Agent2IdentityBinding.tenant_id == command.tenant_id,
            Agent2IdentityBinding.user_id == command.assigned_user_id,
            Agent2IdentityBinding.active.is_(True),
        )
    )
    assigned = {
        str(value)
        for value in ((binding.permission_scope_json if binding else {}) or {}).get(
            "allowed_case_ids", []
        )
        if value
    }
    actor_allowed = actor_is_tenant_admin or actor_user_id == command.assigned_user_id
    if (
        not actor_allowed or case is None or binding is None
        or case.owner_user_id != command.assigned_user_id
        or command.case_id not in assigned
    ):
        return await _failed_receipt(
            session, command, actor_user_id, now, "permission_denied", "authorization"
        )

    policy = await session.scalar(
        select(CaseFollowupPolicy)
        .where(
            CaseFollowupPolicy.tenant_id == command.tenant_id,
            CaseFollowupPolicy.case_id == case_id,
            CaseFollowupPolicy.assigned_user_id == command.assigned_user_id,
        )
        .with_for_update()
    )
    new_policy = policy is None
    if policy is None:
        if command.expected_version != 0:
            return await _failed_receipt(
                session, command, actor_user_id, now, "version_conflict", "optimistic_lock"
            )
        policy = CaseFollowupPolicy(
            policy_id=uuid5(
                NAMESPACE_URL,
                f"case-followup-policy:{command.tenant_id}:{command.case_id}:"
                f"{command.assigned_user_id}",
            ),
            tenant_id=command.tenant_id,
            case_id=case_id,
            assigned_user_id=command.assigned_user_id,
            enabled=False,
            policy_source="tenant_default",
            cadence_type="event_only",
            cadence_days=None,
            custom_interval_json={},
            timezone="Asia/Shanghai",
            business_days_only=False,
            event_triggers_enabled=True,
            hearing_reminders_enabled=True,
            stage_transition_enabled=True,
            node_transition_enabled=True,
            max_unanswered_reminders=1,
            version=0,
            created_at=now,
            updated_at=now,
        )
        session.add(policy)
    before = _snapshot(policy)
    execution = execute_policy_update(
        before, command, actor_has_case_permission=True, now=now
    )
    if not execution.actual_write:
        if new_policy:
            session.expunge(policy)
        return await _failed_receipt(
            session, command, actor_user_id, now, execution.reason_code, "policy"
        )

    after = execution.after
    policy.enabled = after.enabled
    policy.policy_source = after.policy_source
    policy.cadence_type = after.cadence_type
    policy.cadence_days = after.custom_interval_days
    policy.business_days_only = after.business_days_only
    policy.next_due_at = after.next_due_at
    policy.snoozed_until = after.snoozed_until
    policy.hearing_reminders_enabled = after.hearing_reminders_enabled
    policy.stage_transition_enabled = after.stage_transition_enabled
    policy.node_transition_enabled = after.node_transition_enabled
    policy.version = after.version
    policy.updated_at = now
    receipt_id = uuid5(NAMESPACE_URL, f"policy-receipt:{command.idempotency_key}")
    before_json = _snapshot_json(before)
    after_json = _snapshot_json(after)
    receipt = BusinessCommandReceipt(
        receipt_id=receipt_id, tenant_id=command.tenant_id,
        command_id=command.command_id, command_type="update_case_followup_policy",
        actor_user_id=actor_user_id, source_message_id=command.source_turn_id,
        idempotency_key=command.idempotency_key, status="executed",
        resource_type="case_followup_policy", resource_id=str(policy.policy_id),
        before_json=before_json, after_json=after_json, error_code="", failed_stage="",
        actual_write=True, created_at=now, updated_at=now,
    )
    audit = BusinessAuditEvent(
        audit_id=uuid5(NAMESPACE_URL, f"policy-audit:{receipt_id}"),
        tenant_id=command.tenant_id, receipt_id=receipt_id,
        actor_user_id=actor_user_id, source_message_id=command.source_turn_id,
        source_channel="legal_ops", command_type="update_case_followup_policy",
        resource_type="case_followup_policy", resource_id=str(policy.policy_id),
        before_json=before_json, after_json=after_json, created_at=now,
    )
    session.add(receipt)
    await session.flush()
    session.add(audit)
    await session.flush()
    return receipt


def _snapshot_json(snapshot: CaseFollowupPolicySnapshot) -> dict[str, object]:
    value = asdict(snapshot)
    for key in ("last_meaningful_progress_at", "next_due_at", "snoozed_until"):
        item = value.get(key)
        value[key] = item.isoformat() if isinstance(item, datetime) else None
    return value


async def _failed_receipt(
    session: AsyncSession,
    command: UpdateCaseFollowupPolicy,
    actor_user_id: str,
    now: datetime,
    error_code: str,
    failed_stage: str,
) -> BusinessCommandReceipt:
    receipt = BusinessCommandReceipt(
        receipt_id=uuid5(NAMESPACE_URL, f"policy-receipt:{command.idempotency_key}"),
        tenant_id=command.tenant_id, command_id=command.command_id,
        command_type="update_case_followup_policy", actor_user_id=actor_user_id,
        source_message_id=command.source_turn_id,
        idempotency_key=command.idempotency_key, status="failed",
        resource_type="case_followup_policy", resource_id="", before_json={}, after_json={},
        error_code=error_code, failed_stage=failed_stage, actual_write=False,
        created_at=now, updated_at=now,
    )
    audit = BusinessAuditEvent(
        audit_id=uuid5(NAMESPACE_URL, f"policy-audit:{receipt.receipt_id}"),
        tenant_id=command.tenant_id, receipt_id=receipt.receipt_id,
        actor_user_id=actor_user_id, source_message_id=command.source_turn_id,
        source_channel="legal_ops", command_type="update_case_followup_policy",
        resource_type="case_followup_policy", resource_id="",
        before_json={}, after_json={
            "status": "failed", "error_code": error_code,
            "failed_stage": failed_stage, "actual_write": False,
        }, created_at=now,
    )
    session.add(receipt)
    await session.flush()
    session.add(audit)
    await session.flush()
    return receipt
