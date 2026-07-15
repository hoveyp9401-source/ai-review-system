from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

import pytest

from app.agent2.admission_contracts import InformationPending
from app.agent2.information_pending import InformationPendingValidation
from app.agent2.information_pending_runtime import (
    InformationContinuationPreprocessRequest,
    InformationPendingContinuationCoordinator,
    extract_exact_travel_date_answer,
)
from app.agent2.operation_outcomes import (
    OperationOutcome,
    OutcomeObjectRef,
    OutcomeReceiptRef,
    OutcomeStateTransition,
)


NOW = datetime(2026, 7, 14, 1, 0, tzinfo=UTC)


def _uuid(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"information-runtime:{label}"))


def _pending(**overrides) -> InformationPending:
    payload = {
        "pending_id": _uuid("pending"),
        "trace_id": _uuid("trace"),
        "decision_id": _uuid("decision"),
        "tenant_id": "tenant-1",
        "user_id": "user-1",
        "conversation_id": "conversation-1",
        "source_turn_id": "message-original",
        "source_message_id": "message-original",
        "segment_id": "segment-original",
        "segment_text_sha256": "a" * 64,
        "segment_start_offset": 0,
        "segment_end_offset": 6,
        "domain": "travel",
        "operation": "record_travel_event",
        "object_ref": {
            "object_type": "travel_intent",
            "stable_id": _uuid("travel-intent"),
            "version": None,
        },
        "expected_conversation_state_version": 4,
        "missing_fields": ("travel_date",),
        "question_snapshot": {
            "question_key": "travel_date_required",
            "destination": "南京",
        },
        "acceptable_answer_forms": {
            "field": "travel_date",
            "value_types": ["relative_date", "iso_date"],
        },
        "created_at": NOW,
        "expires_at": NOW + timedelta(minutes=10),
        "idempotency_key": "pending-key",
        "pending_status": "awaiting_input",
    }
    payload.update(overrides)
    return InformationPending(**payload)


def _request(**overrides) -> InformationContinuationPreprocessRequest:
    payload = {
        "session": object(),
        "tenant_id": "tenant-1",
        "user_id": "user-1",
        "conversation_id": "conversation-1",
        "conversation_state_user_key": "tenant-1:user-1",
        "conversation_state_version": 4,
        "source_message_id": "message-answer",
        "source_text": "明天",
        "occurred_at": NOW + timedelta(minutes=1),
        "timezone_name": "Asia/Shanghai",
    }
    payload.update(overrides)
    return InformationContinuationPreprocessRequest(**payload)


class _Repository:
    def __init__(self, pendings=(), *, duplicate=False):
        self.pendings = tuple(pendings)
        self.duplicate = duplicate
        self.resolutions = []
        self.settlements = []
        self.failed = []

    async def load_scoped(self, context):
        return tuple(
            pending
            for pending in self.pendings
            if pending.tenant_id == context.tenant_id
            and pending.user_id == context.user_id
            and pending.conversation_id == context.conversation_id
        )

    async def source_message_processed(self, context):
        return self.duplicate

    async def persist_resolution(self, *, resolution, context):
        self.resolutions.append((resolution, context))

    async def persist_settlement(self, *, pending, settlement, settled_at):
        self.settlements.append((pending, settlement, settled_at))

    async def persist_failed_settlement(self, *, pending, reason, settled_at):
        self.failed.append((pending, reason, settled_at))


class _Validator:
    def __init__(self, status="valid", reason=""):
        self.status = status
        self.reason = reason

    async def validate(self, pending, context):
        return InformationPendingValidation(self.status, self.reason)


@pytest.mark.asyncio
async def test_exact_short_answer_produces_only_a_fresh_admission_request():
    repository = _Repository((_pending(),))
    result = await InformationPendingContinuationCoordinator().preprocess(
        _request(), repository=repository, validator=_Validator()
    )

    assert result.status == "ready_for_fresh_admission"
    assert result.actual_write is False
    assert result.fresh_admission_request is not None
    assert result.fresh_admission_request.field_values == {
        "travel_date": "2026-07-15"
    }
    assert result.fresh_admission_request.object_ref == _pending().object_ref
    assert result.fresh_admission_request.requires_fresh_admission is True
    assert result.fresh_admission_request.business_write_allowed is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    ("不是明天", "明天去吧", "大概明天", "确认", "2026-02-30"),
)
async def test_non_exact_or_invalid_answer_never_enters_fresh_admission(text):
    result = await InformationPendingContinuationCoordinator().preprocess(
        _request(source_text=text),
        repository=_Repository((_pending(),)),
        validator=_Validator(),
    )

    assert result.status == "clarification_required"
    assert result.fresh_admission_request is None
    assert result.actual_write is False


@pytest.mark.asyncio
async def test_multiple_active_pendings_require_clarification_and_zero_write():
    second = _pending(
        pending_id=_uuid("pending-2"),
        trace_id=_uuid("trace-2"),
        decision_id=_uuid("decision-2"),
        source_turn_id="message-original-2",
        source_message_id="message-original-2",
        idempotency_key="pending-key-2",
    )
    result = await InformationPendingContinuationCoordinator().preprocess(
        _request(),
        repository=_Repository((_pending(), second)),
        validator=_Validator(),
    )

    assert result.status == "clarification_required"
    assert result.reason == "unique_active_pending_required"
    assert result.fresh_admission_request is None


@pytest.mark.asyncio
async def test_cross_scope_pending_is_invisible_not_a_recent_object_fallback():
    result = await InformationPendingContinuationCoordinator().preprocess(
        _request(user_id="user-2", conversation_state_user_key="tenant-1:user-2"),
        repository=_Repository((_pending(),)),
        validator=_Validator(),
    )

    assert result.handled is False
    assert result.status == "no_pending"
    assert result.fresh_admission_request is None


@pytest.mark.asyncio
async def test_expiry_and_state_drift_are_typed_terminal_blocks():
    expired_repository = _Repository(
        (_pending(expires_at=NOW + timedelta(seconds=30)),)
    )
    expired = await InformationPendingContinuationCoordinator().preprocess(
        _request(occurred_at=NOW + timedelta(minutes=1)),
        repository=expired_repository,
        validator=_Validator(),
    )
    assert expired.status == "expired"
    assert expired_repository.resolutions[0][0].pending_after.pending_status == "expired"

    conflict_repository = _Repository((_pending(),))
    conflict = await InformationPendingContinuationCoordinator().preprocess(
        _request(conversation_state_version=5),
        repository=conflict_repository,
        validator=_Validator(),
    )
    assert conflict.status == "conflicted"
    assert conflict_repository.resolutions[0][0].pending_after.pending_status == "conflicted"


@pytest.mark.asyncio
async def test_permission_or_object_change_invalidates_without_a_write():
    result = await InformationPendingContinuationCoordinator().preprocess(
        _request(),
        repository=_Repository((_pending(),)),
        validator=_Validator("permission_revoked", "actor_no_longer_allowed"),
    )
    assert result.status == "permission_revoked"
    assert result.fresh_admission_request is None

    missing = await InformationPendingContinuationCoordinator().preprocess(
        _request(),
        repository=_Repository((_pending(),)),
        validator=_Validator("object_missing", "object_removed"),
    )
    assert missing.status == "invalidated"
    assert missing.fresh_admission_request is None


@pytest.mark.asyncio
async def test_duplicate_source_message_is_blocked_before_answer_resolution():
    repository = _Repository((_pending(),), duplicate=True)
    result = await InformationPendingContinuationCoordinator().preprocess(
        _request(), repository=repository, validator=_Validator()
    )

    assert result.status == "duplicate_source_message"
    assert result.fresh_admission_request is None
    assert repository.resolutions == []


@pytest.mark.asyncio
async def test_duplicate_source_message_remains_blocked_after_pending_was_consumed():
    result = await InformationPendingContinuationCoordinator().preprocess(
        _request(), repository=_Repository((), duplicate=True), validator=_Validator()
    )

    assert result.handled is True
    assert result.status == "duplicate_source_message"
    assert result.fresh_admission_request is None


@pytest.mark.asyncio
async def test_pending_consumes_only_after_committed_fresh_command_receipt():
    coordinator = InformationPendingContinuationCoordinator()
    repository = _Repository((_pending(),))
    ready = await coordinator.preprocess(
        _request(), repository=repository, validator=_Validator()
    )
    blocked = _outcome(status="blocked", actual_write=False, receipt=False)
    retained = await coordinator.settle(
        ready,
        repository=repository,
        outcome=blocked,
        settled_trace_id=_uuid("fresh-trace"),
        settled_at=NOW + timedelta(minutes=2),
    )
    assert retained.status == "retained"
    assert repository.settlements == []
    assert repository.failed[0][1] == "fresh_admission_failed"

    successful_repository = _Repository((_pending(),))
    ready = await coordinator.preprocess(
        _request(), repository=successful_repository, validator=_Validator()
    )
    consumed = await coordinator.settle(
        ready,
        repository=successful_repository,
        outcome=_outcome(status="registered", actual_write=True, receipt=True),
        settled_trace_id=_uuid("fresh-trace"),
        settled_at=NOW + timedelta(minutes=2),
    )
    assert consumed.status == "consumed"
    assert len(successful_repository.settlements) == 1
    assert successful_repository.failed == []


def test_exact_date_extractor_preserves_source_offsets_and_shanghai_date():
    value = extract_exact_travel_date_answer(
        "  明天。 ", occurred_at=NOW, timezone_name="Asia/Shanghai"
    )
    assert value is not None
    assert value.raw_value == "明天"
    assert "  明天。 "[value.evidence_start_offset : value.evidence_end_offset] == "明天"
    assert value.normalized_value == "2026-07-15"


def _outcome(*, status, actual_write, receipt):
    receipts = (
        (
            OutcomeReceiptRef(
                receipt_id="receipt-1",
                receipt_type="database",
                status="executed",
                actual_write=True,
            ),
        )
        if receipt
        else ()
    )
    return OperationOutcome(
        domain="travel",
        operation="register",
        object_ref=OutcomeObjectRef(
            "travel_intent", _uuid("travel-intent"), "南京出差"
        ),
        business_status=status,
        message_status="not_requested",
        changed_fields=(),
        user_visible_snapshot={},
        blocking_reason="" if actual_write else "fresh_admission_failed",
        receipt_refs=receipts,
        state_transition=OutcomeStateTransition("pending", status),
        actual_write=actual_write,
    )
