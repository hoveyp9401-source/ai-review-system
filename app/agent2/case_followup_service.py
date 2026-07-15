from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from app.agent2.case_lifecycle_followup import CaseFollowupTaskPlan
from app.agent2.operation_outcomes import (
    OperationOutcome,
    OutcomeObjectRef,
    OutcomeReceiptRef,
    OutcomeStateTransition,
)


@dataclass(frozen=True)
class FollowupCreationContext:
    tenant_id: str
    user_id: str
    conversation_id: str
    source_turn_id: str
    now: datetime


@dataclass(frozen=True)
class FollowupWriteReceipt:
    receipt_id: str
    status: str
    actual_write: bool
    committed: bool
    audit_id: str
    message_status: str = "scheduled"


class CaseFollowupTaskStore(Protocol):
    async def persist_task(
        self,
        plan: CaseFollowupTaskPlan,
        context: FollowupCreationContext,
    ) -> FollowupWriteReceipt: ...


class CaseFollowupTaskCreator:
    """Create a receipt-backed Follow-up Outcome through one persistence seam."""

    def __init__(self, store: CaseFollowupTaskStore):
        self._store = store

    async def create(
        self,
        plan: CaseFollowupTaskPlan,
        context: FollowupCreationContext,
    ) -> OperationOutcome:
        if (
            plan.tenant_id != context.tenant_id
            or plan.assigned_user_id != context.user_id
        ):
            raise ValueError("follow-up plan and creation context scope mismatch")
        try:
            receipt = await self._store.persist_task(plan, context)
        except Exception:
            receipt = FollowupWriteReceipt("", "failed", False, False, "")
        succeeded = bool(
            receipt.committed
            and receipt.actual_write
            and receipt.status == "executed"
            and receipt.receipt_id
        )
        duplicate = bool(
            receipt.committed
            and receipt.status == "duplicate"
            and receipt.receipt_id
        )
        receipt_refs = (
            (
                OutcomeReceiptRef(
                    receipt.receipt_id, "database", "executed", True
                ),
            )
            if succeeded
            else (
                OutcomeReceiptRef(
                    receipt.receipt_id, "database", "duplicate", False
                ),
            )
            if duplicate
            else ()
        )
        outcome_id = str(
            uuid5(
                NAMESPACE_URL,
                f"followup-outcome:{context.tenant_id}:{plan.followup_id}:create",
            )
        )
        return OperationOutcome(
            outcome_id=outcome_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            conversation_id=context.conversation_id,
            source_turn_id=context.source_turn_id,
            domain="followup",
            operation="create_task",
            object_ref=OutcomeObjectRef(
                "case_followup_task", plan.followup_id, f"{plan.case_name}追问"
            ),
            business_status="succeeded" if succeeded else "duplicate" if duplicate else "failed",
            message_status=receipt.message_status if succeeded or duplicate else "failed",
            actual_write=succeeded,
            would_write=True,
            changed_fields=("task_status", "message_status"),
            user_visible_snapshot={
                "case_name": plan.case_name,
                "due_at_label": plan.due_at.isoformat(),
                "question_summary": plan.question_text,
            },
            blocking_reason=(
                "" if succeeded or duplicate
                else "task_persistence_failed" if not receipt.receipt_id
                else "task_persistence_not_committed"
            ),
            receipt_refs=receipt_refs,
            audit_refs=(receipt.audit_id,) if (succeeded or duplicate) and receipt.audit_id else (),
            state_transition=OutcomeStateTransition(
                "absent", "scheduled" if succeeded else "failed"
            ),
            idempotency_key=plan.idempotency_key,
            created_at=context.now,
        )
