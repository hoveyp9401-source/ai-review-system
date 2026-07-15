from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from uuid import uuid4

import pytest

from app.agent2.selection_pending import (
    SelectionCandidate,
    SelectionContext,
    SelectionPending,
    SelectionPendingResolver,
    SelectionValidation,
    answer_may_target_selection,
    protect_selection_continuation_payload,
    settle_selection,
)
from app.agent2.operation_outcomes import (
    OperationOutcome,
    OutcomeObjectRef,
    OutcomeReceiptRef,
    OutcomeStateTransition,
)
from app.agent2.conversation_state import ConversationState
from app.agent2.command_planner_v3 import PlanningBlock
from app.agent2.selection_pending import SelectionPendingFactory
from app.agent2.selection_runtime import SelectionTurnCoordinator


NOW = datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc)


class _AlwaysValid:
    async def validate(self, pending, candidate, context):
        return SelectionValidation.valid()


class _RejectedCandidate:
    def __init__(self, status):
        self.status = status

    async def validate(self, pending, candidate, context):
        return SelectionValidation(self.status, self.status)


def _pending(**overrides):
    command_id, decision_id, sub_decision_id = uuid4(), uuid4(), uuid4()
    source_text = "法院表示下周重新查控。"
    values = {
        "pending_id": "selection-1",
        "tenant_id": "tenant-a",
        "user_id": "user-a",
        "conversation_id": "conversation-a",
        "domain": "case_progress",
        "operation": "create",
        "source_turn_id": "turn-1",
        "candidates": (
            SelectionCandidate("case-1", 3, "保定锦珑府案"),
            SelectionCandidate("case-2", 5, "保定工程款案"),
        ),
        "acceptable_answer_forms": {"第一个": "case-1", "第二个": "case-2"},
        "expected_conversation_state_version": 7,
        "created_at": NOW,
        "expires_at": NOW + timedelta(minutes=10),
        "status": "active",
        "continuation_payload": {
            "typed_business_command": {
                "command_id": str(command_id),
                "decision_id": str(decision_id),
                "sub_decision_id": str(sub_decision_id),
                "command_type": "record_case_progress_candidate",
                "target_system": "case_progress",
                "entity_ids": ["case-ref"],
                "payload": {
                    "entities": [{
                        "entity_id": "case-ref",
                        "entity_type": "case_ref",
                        "value": "保定案件",
                        "confidence": 0.99,
                        "attributes": {},
                    }],
                    "source_segments": [
                        {
                            "segment_id": "segment-original",
                            "text": source_text,
                            "text_hash": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
                            "start_offset": 0,
                            "end_offset": len(source_text),
                        }
                    ],
                },
                "execution_mode": "candidate",
                "idempotency_key": "selection-command-key",
            },
            "bind": {"entity_type": "case_ref", "attribute": "case_id"},
        },
    }
    values["continuation_payload"] = protect_selection_continuation_payload(
        values["continuation_payload"]
    )
    values.update(overrides)
    return SelectionPending(**values)


def _context(**overrides):
    values = {
        "tenant_id": "tenant-a",
        "user_id": "user-a",
        "conversation_id": "conversation-a",
        "conversation_state_version": 7,
        "source_turn_id": "reply-1",
        "now": NOW + timedelta(minutes=1),
    }
    values.update(overrides)
    return SelectionContext(**values)


def test_persisted_selection_pending_with_unknown_status_is_rejected() -> None:
    payload = _pending().as_dict()
    payload["status"] = "future_unknown_status"

    with pytest.raises(ValueError, match="unknown status"):
        SelectionPending.from_dict(payload)


def test_selection_pending_constructor_rejects_unknown_status() -> None:
    with pytest.raises(ValueError, match="unknown status"):
        _pending(status="future_unknown_status")


@pytest.mark.asyncio
async def test_unique_selection_pending_resolves_second_candidate_without_writing():
    resolution = await SelectionPendingResolver().resolve(
        (_pending(),),
        answer="第二个",
        context=_context(),
        validator=_AlwaysValid(),
    )

    assert resolution.status == "selected"
    assert resolution.selected_candidate_id == "case-2"
    assert resolution.selected_candidate_version == 5


@pytest.mark.asyncio
async def test_unique_meaningful_label_fragment_resolves_pending_before_daily_routing():
    pending = _pending(
        candidates=(
            SelectionCandidate("case-1", 3, "人民西路8号院物业服务合同纠纷案"),
            SelectionCandidate("case-2", 5, "人民东路16号院物业服务合同纠纷案"),
        ),
        acceptable_answer_forms={"第一个": "case-1", "第二个": "case-2"},
    )

    assert answer_may_target_selection((pending,), "人民西路") is True
    resolution = await SelectionPendingResolver().resolve(
        (pending,),
        answer="人民西路",
        context=_context(),
        validator=_AlwaysValid(),
    )

    assert resolution.status == "selected"
    assert resolution.selected_candidate_id == "case-1"
    assert resolution.actual_write is False
    assert resolution.pending_after.status == "active"


@pytest.mark.asyncio
async def test_multiple_selection_pendings_make_confirmation_clarify_with_zero_writes():
    resolution = await SelectionPendingResolver().resolve(
        (_pending(), _pending(pending_id="selection-2", domain="travel")),
        answer="确认",
        context=_context(),
        validator=_AlwaysValid(),
    )

    assert resolution.status == "clarification_required"
    assert resolution.reason == "selection_pending_not_unique"
    assert resolution.actual_write is False
    assert resolution.selected_candidate_id == ""


@pytest.mark.asyncio
async def test_expired_selection_pending_is_safely_invalidated():
    resolution = await SelectionPendingResolver().resolve(
        (_pending(expires_at=NOW + timedelta(seconds=30)),),
        answer="第二个",
        context=_context(now=NOW + timedelta(minutes=1)),
        validator=_AlwaysValid(),
    )

    assert resolution.status == "expired"
    assert resolution.pending_after.status == "expired"
    assert resolution.actual_write is False


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ("not_found", "version_conflict", "forbidden", "illegal"))
async def test_candidate_is_revalidated_before_selection_can_continue(status):
    resolution = await SelectionPendingResolver().resolve(
        (_pending(),),
        answer="第一个",
        context=_context(),
        validator=_RejectedCandidate(status),
    )

    assert resolution.status == "invalidated"
    assert resolution.reason == status
    assert resolution.pending_after.invalidation_reason == status
    assert resolution.actual_write is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "context_change",
    (
        {"tenant_id": "tenant-b"},
        {"user_id": "user-b"},
        {"conversation_id": "conversation-b"},
    ),
)
async def test_selection_pending_never_crosses_tenant_user_or_conversation(context_change):
    resolution = await SelectionPendingResolver().resolve(
        (_pending(),),
        answer="第一个",
        context=_context(**context_change),
        validator=_AlwaysValid(),
    )

    assert resolution.status == "clarification_required"
    assert resolution.actual_write is False
    assert resolution.selected_candidate_id == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "expected_id"),
    (
        ("把第一条删了", "case-1"),
        ("就刚才那个", "case-1"),
        ("不是这个，是另一个", "case-2"),
        ("需要", "case-1"),
        ("确认", "case-1"),
    ),
)
async def test_selection_pending_supports_declared_short_answer_forms(answer, expected_id):
    pending = _pending(
        acceptable_answer_forms={
            "就刚才那个": "case-1",
            "不是这个，是另一个": "case-2",
            "需要": "case-1",
            "确认": "case-1",
        }
    )
    resolution = await SelectionPendingResolver().resolve(
        (pending,), answer=answer, context=_context(), validator=_AlwaysValid()
    )

    assert resolution.status == "selected"
    assert resolution.selected_candidate_id == expected_id


@pytest.mark.asyncio
async def test_selection_pending_is_consumed_only_after_successful_receipt():
    pending = _pending()
    resolution = await SelectionPendingResolver().resolve(
        (pending,), answer="第二个", context=_context(), validator=_AlwaysValid()
    )
    outcome = OperationOutcome(
        domain="case_progress",
        operation="create",
        object_ref=OutcomeObjectRef("case_progress", "progress-1", "保定工程款案", 1),
        business_status="succeeded",
        message_status="not_applicable",
        changed_fields=("summary",),
        user_visible_snapshot={"case_name": "保定工程款案", "content": "法院已受理"},
        blocking_reason="",
        receipt_refs=(OutcomeReceiptRef("receipt-1", "database", "executed", True),),
        state_transition=OutcomeStateTransition("absent", "recorded"),
        actual_write=True,
        source_turn_id="reply-1",
    )

    settlement = settle_selection(pending, resolution, outcome, settled_at=_context().now)

    assert settlement.pending_after.status == "consumed"
    assert settlement.pending_after.consumed_receipt_id == "receipt-1"
    assert settlement.audit.result == "consumed"


@pytest.mark.asyncio
async def test_consumed_selection_pending_rejects_duplicate_reply_with_zero_writes():
    consumed = _pending(status="consumed", consumed_receipt_id="receipt-1")

    resolution = await SelectionPendingResolver().resolve(
        (consumed,), answer="第二个", context=_context(), validator=_AlwaysValid()
    )

    assert resolution.status == "already_consumed"
    assert resolution.reason == "selection_pending_already_consumed"
    assert resolution.actual_write is False


def test_selection_pending_round_trips_separately_in_conversation_state():
    state = ConversationState(
        user_id="tenant-a:user-a",
        conversation_id="conversation-a",
        version=7,
        selection_pending=(_pending(),),
    )

    restored = ConversationState.from_payload(state.as_payload())

    assert restored.selection_pending == state.selection_pending
    assert restored.pending == ()


def test_selection_pending_factory_uses_only_stable_versioned_block_candidates():
    block = PlanningBlock(
        "action-1",
        "case_target_needs_clarification",
        metadata={
            "selection": {
                "domain": "case_progress",
                "operation": "create",
                "candidates": [
                    {"stable_id": "case-1", "version": 3, "label": "案件一"},
                    {"stable_id": "case-2", "version": 4, "label": "案件二"},
                ],
            }
        },
    )

    pending = SelectionPendingFactory().from_block(
        block,
        tenant_id="tenant-a",
        user_id="user-a",
        conversation_id="conversation-a",
        source_turn_id="turn-1",
        expected_conversation_state_version=7,
        now=NOW,
        expires_in_seconds=600,
    )

    assert pending is not None
    assert [item.stable_id for item in pending.candidates] == ["case-1", "case-2"]
    assert pending.acceptable_answer_forms["第二个"] == "case-2"


@pytest.mark.asyncio
async def test_legacy_selection_coordinator_requires_fresh_admission_before_executor():
    pending = _pending()
    state = ConversationState(
        user_id="tenant-a:user-a",
        conversation_id="conversation-a",
        version=7,
        selection_pending=(pending,),
    )
    executed = []

    async def execute(command):
        executed.append(command)
        return OperationOutcome(
            domain="case_progress",
            operation="create",
            object_ref=OutcomeObjectRef("case_progress", "progress-1", "保定工程款案", 1),
            business_status="succeeded",
            message_status="not_applicable",
            changed_fields=("summary",),
            user_visible_snapshot={
                "case_name": "保定工程款案",
                "content": "法院表示下周重新查控。",
            },
            blocking_reason="",
            receipt_refs=(OutcomeReceiptRef("receipt-1", "database", "executed", True),),
            state_transition=OutcomeStateTransition("absent", "recorded"),
            actual_write=True,
            source_turn_id="reply-1",
        )

    result = await SelectionTurnCoordinator().handle(
        state,
        answer="第二个",
        context=_context(),
        validator=_AlwaysValid(),
        executor=execute,
    )

    assert executed == []
    assert result.actual_write is False
    assert result.state_after.selection_pending[0].status == "invalidated"
    assert result.state_after.selection_pending[0].consumed_receipt_id == ""
    assert result.state_after.selection_pending[0].invalidation_reason == (
        "fresh_selection_admission_required"
    )
    assert "没有写入" in result.reply


@pytest.mark.asyncio
async def test_selection_coordinator_does_not_call_executor_without_unique_pending():
    first = _pending(pending_id="selection-1")
    second = _pending(pending_id="selection-2")
    state = ConversationState(
        user_id="tenant-a:user-a",
        conversation_id="conversation-a",
        version=7,
        selection_pending=(first, second),
    )

    async def must_not_execute(command):
        raise AssertionError("ambiguous selection must have zero writes")

    result = await SelectionTurnCoordinator().handle(
        state,
        answer="确认",
        context=_context(),
        validator=_AlwaysValid(),
        executor=must_not_execute,
    )

    assert result.actual_write is False
    assert result.state_after == state
    assert "没有写入" in result.reply
