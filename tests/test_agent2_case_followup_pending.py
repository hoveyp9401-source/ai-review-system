from datetime import datetime, timedelta, timezone

import pytest

from app.agent2.case_followup_pending import (
    CaseFollowupPendingContext,
    CaseFollowupPendingResolver,
    CaseFollowupPendingSnapshot,
    CaseFollowupReplyIntent,
    CaseFollowupValidation,
)


NOW = datetime(2026, 7, 13, 9, tzinfo=timezone.utc)


class _Valid:
    async def validate(self, pending, context):
        return CaseFollowupValidation("valid")


class _Forbidden:
    async def validate(self, pending, context):
        return CaseFollowupValidation("forbidden", "case_access_revoked")


def _pending(**overrides):
    values = dict(
        pending_id="pending-1", tenant_id="tenant-a", user_id="user-1",
        conversation_id="conversation-1", task_id="task-1", case_id="case-1",
        followup_id="followup-1", case_version=3, expected_state_version=7,
        source_message_id="provider-message-1", expires_at=NOW + timedelta(days=7),
        status="awaiting_input", version=1,
    )
    values.update(overrides)
    return CaseFollowupPendingSnapshot(**values)


@pytest.mark.asyncio
async def test_unique_followup_pending_binds_typed_fact_reply_without_writing():
    intent = CaseFollowupReplyIntent(
        reply_type="case_fact", source_message_id="reply-message-1",
        raw_text="今天联系法院了，下周重新查控。",
    )
    context = CaseFollowupPendingContext(
        tenant_id="tenant-a", user_id="user-1", conversation_id="conversation-1",
        conversation_state_version=7, now=NOW, source_message_already_processed=False,
    )

    resolution = await CaseFollowupPendingResolver().resolve(
        (_pending(),), intent=intent, context=context, validator=_Valid()
    )

    assert resolution.status == "bound"
    assert resolution.case_id == "case-1"
    assert resolution.followup_id == "followup-1"
    assert resolution.actual_write is False
    assert resolution.pending_after.status == "awaiting_input"


@pytest.mark.asyncio
async def test_multiple_followup_pendings_require_clarification_with_zero_writes():
    intent = CaseFollowupReplyIntent("confirmation", "reply-message-2", "确认")
    context = CaseFollowupPendingContext(
        "tenant-a", "user-1", "conversation-1", 7, NOW, False
    )

    resolution = await CaseFollowupPendingResolver().resolve(
        (_pending(), _pending(pending_id="pending-2", case_id="case-2")),
        intent=intent, context=context, validator=_Valid(),
    )

    assert resolution.status == "clarification_required"
    assert resolution.reason_code == "pending_not_unique"
    assert resolution.actual_write is False
    assert resolution.case_id == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "foreign",
    [
        _pending(tenant_id="tenant-b"),
        _pending(user_id="user-2"),
        _pending(conversation_id="conversation-2"),
    ],
)
async def test_cross_scope_pending_cannot_be_consumed(foreign):
    context = CaseFollowupPendingContext(
        "tenant-a", "user-1", "conversation-1", 7, NOW, False
    )
    resolution = await CaseFollowupPendingResolver().resolve(
        (foreign,), intent=CaseFollowupReplyIntent("confirmation", "reply-x", "确认"),
        context=context, validator=_Valid(),
    )

    assert resolution.status == "clarification_required"
    assert resolution.actual_write is False
    assert resolution.case_id == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pending", "context", "validator", "expected"),
    [
        (_pending(expires_at=NOW), CaseFollowupPendingContext("tenant-a", "user-1", "conversation-1", 7, NOW, False), _Valid(), "expired"),
        (_pending(), CaseFollowupPendingContext("tenant-a", "user-1", "conversation-1", 8, NOW, False), _Valid(), "conflicted"),
        (_pending(), CaseFollowupPendingContext("tenant-a", "user-1", "conversation-1", 7, NOW, True), _Valid(), "duplicate"),
        (_pending(), CaseFollowupPendingContext("tenant-a", "user-1", "conversation-1", 7, NOW, False), _Forbidden(), "permission_revoked"),
    ],
)
async def test_pending_revalidation_failures_are_zero_write(
    pending, context, validator, expected
):
    resolution = await CaseFollowupPendingResolver().resolve(
        (pending,), intent=CaseFollowupReplyIntent("case_fact", "reply-y", "有进展"),
        context=context, validator=validator,
    )

    assert resolution.status == expected
    assert resolution.actual_write is False
    assert resolution.case_id == ""
