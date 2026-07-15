from datetime import datetime, timezone

import pytest

from app.agent2.case_followup_service import (
    CaseFollowupTaskCreator,
    FollowupCreationContext,
    FollowupWriteReceipt,
)
from app.agent2.case_lifecycle_followup import (
    CaseFollowupTaskPlan,
    CaseFollowupTrigger,
)


class _Store:
    def __init__(self, receipt):
        self.receipt = receipt

    async def persist_task(self, plan, context):
        return self.receipt


class _FailingStore:
    async def persist_task(self, plan, context):
        raise RuntimeError("database unavailable")


def _plan():
    due = datetime(2026, 7, 13, 9, tzinfo=timezone.utc)
    return CaseFollowupTaskPlan(
        followup_id="followup-1", tenant_id="tenant-a", case_id="case-1",
        assigned_user_id="user-1", case_name="南京工程款案", case_type="plaintiff",
        stage="诉讼中", node="", policy_id="policy-1", trigger_type="fixed_cadence",
        trigger_event_ids=("cadence-1",),
        trigger_sources=(CaseFollowupTrigger(
            "fixed_cadence", "cadence-1", "meaningful_progress", due, 100,
        ),),
        question_type="meaningful_progress", priority=100, due_at=due,
        expires_at=datetime(2026, 7, 20, 9, tzinfo=timezone.utc), case_version=3,
        question_text="案件目前有没有新进展？", idempotency_key="case-followup:1",
    )


@pytest.mark.asyncio
async def test_task_creator_only_returns_success_after_committed_store_receipt():
    context = FollowupCreationContext(
        tenant_id="tenant-a", user_id="user-1", conversation_id="conversation-1",
        source_turn_id="scheduler-1", now=datetime(2026, 7, 13, 9, tzinfo=timezone.utc),
    )
    committed = FollowupWriteReceipt(
        receipt_id="receipt-1", status="executed", actual_write=True,
        committed=True, audit_id="audit-1",
    )

    success = await CaseFollowupTaskCreator(_Store(committed)).create(_plan(), context)

    assert success.business_status == "succeeded"
    assert success.actual_write is True
    assert success.receipt_refs[0].receipt_id == "receipt-1"
    assert success.audit_refs == ("audit-1",)

    failed = await CaseFollowupTaskCreator(
        _Store(FollowupWriteReceipt("receipt-2", "failed", False, False, "audit-2"))
    ).create(_plan(), context)

    assert failed.business_status == "failed"
    assert failed.actual_write is False


@pytest.mark.asyncio
async def test_task_creator_expresses_committed_duplicate_as_no_repeat_write_not_failure():
    context = FollowupCreationContext(
        tenant_id="tenant-a", user_id="user-1", conversation_id="conversation-1",
        source_turn_id="scheduler-replay",
        now=datetime(2026, 7, 13, 9, tzinfo=timezone.utc),
    )
    duplicate = FollowupWriteReceipt(
        receipt_id="receipt-original", status="duplicate", actual_write=False,
        committed=True, audit_id="audit-original",
    )

    outcome = await CaseFollowupTaskCreator(_Store(duplicate)).create(_plan(), context)

    assert outcome.business_status == "duplicate"
    assert outcome.actual_write is False
    assert outcome.blocking_reason == ""


@pytest.mark.asyncio
async def test_task_creator_converts_store_exception_to_failed_zero_write_outcome():
    context = FollowupCreationContext(
        tenant_id="tenant-a", user_id="user-1", conversation_id="conversation-1",
        source_turn_id="scheduler-failed",
        now=datetime(2026, 7, 13, 9, tzinfo=timezone.utc),
    )

    outcome = await CaseFollowupTaskCreator(_FailingStore()).create(_plan(), context)

    assert outcome.business_status == "failed"
    assert outcome.actual_write is False
    assert outcome.receipt_refs == ()
    assert outcome.blocking_reason == "task_persistence_failed"
