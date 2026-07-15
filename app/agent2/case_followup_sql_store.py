from __future__ import annotations

from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select

from app.agent2.business.models import (
    Agent2Case,
    Agent2IdentityBinding,
    Agent2OperationOutcome,
    Agent2TaskLedgerEntry,
    BusinessAuditEvent,
    BusinessCommandReceipt,
    CaseFollowupTask,
)
from app.agent2.case_followup_service import (
    FollowupCreationContext,
    FollowupWriteReceipt,
)
from app.agent2.case_lifecycle_followup import CaseFollowupTaskPlan


class SqlCaseFollowupTaskStore:
    """PostgreSQL adapter that returns only after its transaction commits."""

    def __init__(self, session_factory: Any):
        self._session_factory = session_factory

    async def persist_task(
        self,
        plan: CaseFollowupTaskPlan,
        context: FollowupCreationContext,
    ) -> FollowupWriteReceipt:
        result: FollowupWriteReceipt | None = None
        async with self._session_factory() as session:
            async with session.begin():
                binding = await session.scalar(
                    select(Agent2IdentityBinding).where(
                        Agent2IdentityBinding.tenant_id == context.tenant_id,
                        Agent2IdentityBinding.user_id == context.user_id,
                        Agent2IdentityBinding.active.is_(True),
                    )
                )
                case = await session.scalar(
                    select(Agent2Case).where(
                        Agent2Case.tenant_id == context.tenant_id,
                        Agent2Case.case_id == UUID(plan.case_id),
                    )
                )
                if binding is None or case is None:
                    raise PermissionError("follow-up requires active binding and Case")
                allowed = {
                    str(value)
                    for value in (binding.permission_scope_json or {}).get(
                        "allowed_case_ids", []
                    )
                    if value
                }
                if plan.case_id not in allowed or case.owner_user_id != context.user_id:
                    raise PermissionError("Case is outside the assigned Canary scope")
                if (
                    case.company_id != binding.company_id
                    or case.department_id != binding.department_id
                    or case.team_id != binding.team_id
                    or case.version != plan.case_version
                ):
                    raise PermissionError("Case organization or version changed")
                existing = await session.scalar(
                    select(CaseFollowupTask).where(
                        CaseFollowupTask.tenant_id == context.tenant_id,
                        CaseFollowupTask.idempotency_key == plan.idempotency_key,
                    )
                )
                if existing is not None:
                    receipt = await session.scalar(
                        select(BusinessCommandReceipt).where(
                            BusinessCommandReceipt.tenant_id == context.tenant_id,
                            BusinessCommandReceipt.idempotency_key == plan.idempotency_key,
                        )
                    )
                    if receipt is None:
                        raise RuntimeError("duplicate task has no committed receipt")
                    audit = await session.scalar(
                        select(BusinessAuditEvent).where(
                            BusinessAuditEvent.tenant_id == context.tenant_id,
                            BusinessAuditEvent.receipt_id == receipt.receipt_id,
                        )
                    )
                    result = FollowupWriteReceipt(
                        str(receipt.receipt_id),
                        "duplicate",
                        False,
                        True,
                        str(audit.audit_id) if audit is not None else "",
                        message_status=existing.message_status,
                    )
                else:
                    result = await self._insert_task(session, plan, context)
        if result is None:
            raise RuntimeError("follow-up transaction produced no receipt")
        # Reaching this line proves the transaction context committed.
        return FollowupWriteReceipt(
            result.receipt_id,
            result.status,
            result.actual_write,
            True,
            result.audit_id,
            result.message_status,
        )

    async def _insert_task(
        self,
        session: Any,
        plan: CaseFollowupTaskPlan,
        context: FollowupCreationContext,
    ) -> FollowupWriteReceipt:
        followup_id = UUID(plan.followup_id)
        receipt_id = uuid5(NAMESPACE_URL, f"{plan.idempotency_key}:receipt")
        audit_id = uuid5(NAMESPACE_URL, f"{plan.idempotency_key}:audit")
        outcome_id = uuid5(NAMESPACE_URL, f"{plan.idempotency_key}:outcome")
        task = CaseFollowupTask(
            followup_id=followup_id,
            tenant_id=context.tenant_id,
            case_id=UUID(plan.case_id),
            assigned_user_id=context.user_id,
            policy_id=UUID(plan.policy_id) if plan.policy_id else None,
            trigger_type=plan.trigger_type,
            trigger_event_id=plan.trigger_event_ids[0],
            trigger_sources_json=[
                {
                    "trigger_type": item.trigger_type,
                    "trigger_event_id": item.trigger_event_id,
                    "question_type": item.question_type,
                    "due_at": item.due_at.isoformat(),
                    "priority": item.priority,
                    "facts": dict(item.facts),
                }
                for item in plan.trigger_sources
            ],
            case_type=plan.case_type,
            stage=plan.stage,
            node=plan.node,
            case_version=plan.case_version,
            question_type=plan.question_type,
            question_text=plan.question_text,
            priority=plan.priority,
            task_status="scheduled",
            message_status="scheduled",
            response_status="not_requested",
            due_at=plan.due_at,
            expires_at=plan.expires_at,
            reminder_count=0,
            max_reminders=1,
            conversation_id=context.conversation_id,
            provider_message_id="",
            idempotency_key=plan.idempotency_key,
            version=1,
            created_at=context.now,
            updated_at=context.now,
        )
        after = {
            "followup_id": plan.followup_id,
            "case_id": plan.case_id,
            "assigned_user_id": context.user_id,
            "task_status": "scheduled",
            "message_status": "scheduled",
            "response_status": "not_requested",
            "due_at": plan.due_at.isoformat(),
            "expires_at": plan.expires_at.isoformat(),
        }
        receipt = BusinessCommandReceipt(
            receipt_id=receipt_id,
            tenant_id=context.tenant_id,
            command_id=f"create-followup:{plan.followup_id}",
            command_type="create_case_followup_task",
            actor_user_id=context.user_id,
            source_message_id=context.source_turn_id,
            idempotency_key=plan.idempotency_key,
            status="executed",
            resource_type="case_followup_task",
            resource_id=plan.followup_id,
            before_json={},
            after_json=after,
            error_code="",
            failed_stage="",
            actual_write=True,
            created_at=context.now,
            updated_at=context.now,
        )
        audit = BusinessAuditEvent(
            audit_id=audit_id,
            tenant_id=context.tenant_id,
            receipt_id=receipt_id,
            actor_user_id=context.user_id,
            source_message_id=context.source_turn_id,
            source_channel="case_followup_scheduler",
            command_type="create_case_followup_task",
            resource_type="case_followup_task",
            resource_id=plan.followup_id,
            before_json={},
            after_json=after,
            created_at=context.now,
        )
        ledger = Agent2TaskLedgerEntry(
            task_id=followup_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            conversation_id=context.conversation_id,
            domain="case_followup",
            operation="answer",
            object_ref_json={"case_id": plan.case_id, "followup_id": plan.followup_id},
            status="active",
            focus_state="active",
            version=1,
            source_turn_id=context.source_turn_id,
            pending_requirements_json={"pending_type": "case_followup"},
            resume_policy_json={"mode": "restore_previous"},
            expires_at=plan.expires_at,
            created_at=context.now,
            updated_at=context.now,
        )
        outcome = Agent2OperationOutcome(
            outcome_id=outcome_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            conversation_id=context.conversation_id,
            source_turn_id=context.source_turn_id,
            domain="followup",
            operation="create_task",
            object_type="case_followup_task",
            object_id=plan.followup_id,
            business_status="succeeded",
            message_status="scheduled",
            actual_write=True,
            would_write=True,
            changed_fields_json=["task_status", "message_status"],
            user_visible_snapshot_json={
                "case_name": plan.case_name,
                "due_at_label": plan.due_at.isoformat(),
                "question_summary": plan.question_text,
            },
            blocking_reason="",
            receipt_refs_json=[{"receipt_id": str(receipt_id), "status": "executed"}],
            audit_refs_json=[{"audit_id": str(audit_id)}],
            state_transition_json={"from": "absent", "to": "scheduled"},
            idempotency_key=plan.idempotency_key,
            created_at=context.now,
        )
        session.add_all((task, receipt, ledger))
        await session.flush()
        session.add_all((audit, outcome))
        await session.flush()
        return FollowupWriteReceipt(
            str(receipt_id), "executed", True, False, str(audit_id), "scheduled"
        )
