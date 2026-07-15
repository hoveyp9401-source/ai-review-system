from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, uuid5

import pytest

from app.agent2.admission_contracts import InformationPending
from app.agent2.information_pending import (
    InformationAnswerCandidate,
    InformationAnswerValue,
    InformationPendingContext,
    InformationPendingResolver,
    InformationPendingValidation,
    settle_information_pending,
)
from app.agent2.operation_outcomes import (
    OperationOutcome,
    OutcomeObjectRef,
    OutcomeReceiptRef,
    OutcomeStateTransition,
)


NOW = datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)


def _uuid(name: str) -> str:
    return str(uuid5(NAMESPACE_URL, name))


def _pending(**overrides) -> InformationPending:
    payload = dict(
        pending_id=_uuid("information-pending"),
        trace_id=_uuid("information-trace"),
        decision_id=_uuid("information-decision"),
        tenant_id="sandbox-agent2-phase2-20260711",
        user_id="user-pang",
        conversation_id="conversation-information",
        source_turn_id="turn-original",
        source_message_id="message-original",
        segment_id="travel-segment",
        segment_text_sha256="a" * 64,
        segment_start_offset=0,
        segment_end_offset=8,
        domain="travel",
        operation="record_travel_event",
        object_ref={
            "object_type": "travel_intent",
            "stable_id": _uuid("travel-intent"),
            "version": None,
        },
        expected_conversation_state_version=1,
        missing_fields=("travel_date",),
        question_snapshot={"question_key": "travel_date_required"},
        acceptable_answer_forms={"field": "travel_date"},
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        idempotency_key="information-pending-key",
    )
    payload.update(overrides)
    return InformationPending(**payload)


def _context(**overrides) -> InformationPendingContext:
    payload = dict(
        tenant_id="sandbox-agent2-phase2-20260711",
        user_id="user-pang",
        conversation_id="conversation-information",
        source_message_id="message-answer",
        occurred_at=NOW + timedelta(minutes=1),
        conversation_state_version=1,
    )
    payload.update(overrides)
    return InformationPendingContext(**payload)


def _candidate() -> InformationAnswerCandidate:
    return InformationAnswerCandidate(
        source_message_id="message-answer",
        source_text="明天",
        values=(
            InformationAnswerValue(
                field="travel_date",
                raw_value="明天",
                normalized_value="2026-07-15",
                evidence_start_offset=0,
                evidence_end_offset=2,
            ),
        ),
    )


class _Valid:
    def validate(self, pending, context):
        return InformationPendingValidation("valid")


def test_unique_information_pending_only_produces_fresh_admission_request():
    pending = _pending()
    result = InformationPendingResolver().resolve(
        (pending,), candidate=_candidate(), context=_context(), validator=_Valid()
    )

    assert result.status == "ready_for_fresh_admission"
    assert result.actual_write is False
    assert result.pending_after == pending
    assert result.continuation is not None
    assert result.continuation.requires_fresh_admission is True
    assert result.continuation.business_write_allowed is False
    assert result.continuation.field_values == {"travel_date": "2026-07-15"}
    assert result.continuation.raw_values == {"travel_date": "明天"}


def test_multiple_information_pendings_never_guess_from_a_short_reply():
    result = InformationPendingResolver().resolve(
        (_pending(), _pending(pending_id=_uuid("information-pending-2"))),
        candidate=_candidate(),
        context=_context(),
        validator=_Valid(),
    )

    assert result.status == "no_unique_pending"
    assert result.actual_write is False
    assert result.continuation is None


def test_cross_user_pending_is_not_visible_or_consumable():
    result = InformationPendingResolver().resolve(
        (_pending(user_id="user-liu"),),
        candidate=_candidate(),
        context=_context(),
        validator=_Valid(),
    )

    assert result.status == "no_unique_pending"
    assert result.pending_id == ""
    assert result.actual_write is False


def test_expired_information_pending_is_safely_invalidated():
    pending = _pending(expires_at=NOW + timedelta(seconds=30))
    result = InformationPendingResolver().resolve(
        (pending,), candidate=_candidate(), context=_context(), validator=_Valid()
    )

    assert result.status == "expired"
    assert result.pending_after is not None
    assert result.pending_after.pending_status == "expired"
    assert result.actual_write is False


def test_base_bound_information_pending_conflicts_after_mixed_turn_advances_state():
    pending = _pending(expected_conversation_state_version=1)
    result = InformationPendingResolver().resolve(
        (pending,),
        candidate=_candidate(),
        context=_context(conversation_state_version=2),
        validator=_Valid(),
    )

    assert result.status == "conflicted"
    assert result.actual_write is False
    assert result.continuation is None
    assert result.pending_after is not None
    assert result.pending_after.pending_status == "conflicted"


def test_ungrounded_answer_cannot_fill_information_pending():
    candidate = replace(
        _candidate(),
        values=(
            InformationAnswerValue(
                field="travel_date",
                raw_value="后天",
                normalized_value="2026-07-16",
                evidence_start_offset=0,
                evidence_end_offset=2,
            ),
        ),
    )
    result = InformationPendingResolver().resolve(
        (_pending(),), candidate=candidate, context=_context(), validator=_Valid()
    )

    assert result.status == "insufficient_information"
    assert result.continuation is None
    assert result.actual_write is False


def test_information_pending_closes_only_after_committed_receipt():
    pending = _pending()
    resolution = InformationPendingResolver().resolve(
        (pending,), candidate=_candidate(), context=_context(), validator=_Valid()
    )
    blocked_outcome = OperationOutcome(
        domain="travel",
        operation="register",
        object_ref=OutcomeObjectRef("travel_intent", _uuid("travel-intent"), "南京"),
        business_status="blocked",
        message_status="not_requested",
        changed_fields=(),
        user_visible_snapshot={},
        blocking_reason="version_conflict",
        receipt_refs=(),
        state_transition=OutcomeStateTransition("pending", "blocked"),
        actual_write=False,
    )

    retained = settle_information_pending(
        pending,
        resolution,
        blocked_outcome,
        settled_trace_id=_uuid("settled-trace"),
    )

    assert retained.status == "retained"
    assert retained.pending_after.pending_status == "active"

    committed_outcome = replace(
        blocked_outcome,
        business_status="registered",
        blocking_reason="",
        actual_write=True,
        receipt_refs=(
            OutcomeReceiptRef(
                receipt_id="receipt-travel",
                receipt_type="database",
                status="executed",
                actual_write=True,
            ),
        ),
        state_transition=OutcomeStateTransition("pending", "registered"),
    )
    consumed = settle_information_pending(
        pending,
        resolution,
        committed_outcome,
        settled_trace_id=_uuid("settled-trace"),
    )

    assert consumed.status == "consumed"
    assert consumed.pending_after.pending_status == "consumed"
    assert consumed.pending_after.consumed_by_trace_id == _uuid("settled-trace")
    assert consumed.receipt_refs == ("receipt-travel",)


def test_unknown_information_pending_status_fails_closed():
    with pytest.raises(ValueError, match="unknown status"):
        _pending(pending_status="future_magic_state")
