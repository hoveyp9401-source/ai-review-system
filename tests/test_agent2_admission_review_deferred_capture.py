from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest

from app.agent2.admission_contracts import (
    AdmissionDecision,
    DeferredSemanticEvent,
    SemanticReviewItem,
)
from app.agent2.cognitive_core_v3 import (
    CognitiveCoreV3,
    CognitiveTurn,
    SemanticInterpretation,
)
from app.agent2.conversation_state import ConversationState
from app.agent2.cognitive_runtime_v3 import semantic_admission_capture_policy
from app.agent2.domain_admission import AdmissionCapturePolicy, DomainAdmissionEngine


def _uuid5(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"review-deferred:{label}"))


def _review_item(**overrides) -> SemanticReviewItem:
    values = {
        "review_id": _uuid5("review-helper"),
        "trace_id": _uuid5("trace-helper"),
        "decision_id": _uuid5("decision-helper"),
        "tenant_id": "tenant-1",
        "user_id": "user-1",
        "conversation_id": "conversation-1",
        "source_turn_id": "message-1",
        "source_message_id": "message-1",
        "segment_id": "segment-1",
        "segment_text_sha256": "a" * 64,
        "segment_start_offset": 0,
        "segment_end_offset": 8,
        "domain": "case",
        "operation": "record_case_progress",
        "object_ref": None,
        "reason_code": "case_reference_ambiguous",
        "candidate_snapshot": {
            "action_id": "action-1",
            "decision_verdict": "blocked",
            "evidence_refs": ["segment_sha256:" + "a" * 64],
        },
        "resolution": {},
        "idempotency_key": "review-helper-idempotency",
        "created_at": datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return SemanticReviewItem(**values)


def _deferred_event(**overrides) -> DeferredSemanticEvent:
    created_at = datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)
    values = {
        "deferred_event_id": _uuid5("deferred-helper"),
        "trace_id": _uuid5("deferred-trace-helper"),
        "decision_id": _uuid5("deferred-decision-helper"),
        "tenant_id": "tenant-1",
        "user_id": "user-1",
        "conversation_id": "conversation-1",
        "source_turn_id": "message-1",
        "source_message_id": "message-1",
        "segment_id": "segment-1",
        "segment_text_sha256": "b" * 64,
        "segment_start_offset": 0,
        "segment_end_offset": 8,
        "domain": "case",
        "operation": "record_case_progress",
        "object_ref": None,
        "reason_code": "future_semantic_signal",
        "payload": {
            "action_id": "action-1",
            "decision_verdict": "deferred_audit_only",
            "evidence_refs": ["segment_sha256:" + "b" * 64],
        },
        "not_before": created_at + timedelta(hours=1),
        "expires_at": created_at + timedelta(days=1),
        "idempotency_key": "deferred-helper-idempotency",
        "created_at": created_at,
    }
    values.update(overrides)
    return DeferredSemanticEvent(**values)


def test_semantic_review_candidate_rejects_extra_business_text_field() -> None:
    with pytest.raises(ValueError, match="candidate_snapshot has invalid schema"):
        _review_item(
            candidate_snapshot={
                "action_id": "action-1",
                "decision_verdict": "blocked",
                "evidence_refs": ["segment_sha256:" + "a" * 64],
                "memo": "今天联系法院推进了该案件",
            }
        )


@pytest.mark.parametrize(
    "candidate_snapshot",
    (
        {
            "action_id": "今天联系法院推进案件",
            "decision_verdict": "blocked",
            "evidence_refs": ["segment_sha256:" + "a" * 64],
        },
        {
            "action_id": "action-1",
            "decision_verdict": "model_says_yes",
            "evidence_refs": ["segment_sha256:" + "a" * 64],
        },
        {
            "action_id": "action-1",
            "decision_verdict": "blocked",
            "evidence_refs": ["case_text:法院表示下周重新查控"],
        },
        {
            "action_id": "action-1",
            "decision_verdict": "blocked",
            "evidence_refs": [{"raw_text": "法院表示下周重新查控"}],
        },
    ),
)
def test_semantic_review_candidate_rejects_non_digest_field_values(
    candidate_snapshot,
) -> None:
    with pytest.raises(ValueError):
        _review_item(candidate_snapshot=candidate_snapshot)


def test_pending_semantic_review_rejects_nonempty_resolution() -> None:
    with pytest.raises(
        ValueError,
        match="pending semantic review cannot have a resolution",
    ):
        _review_item(resolution={"notes": "法院表示下周重新查控"})


def test_pending_semantic_review_rejects_resolved_timestamp() -> None:
    with pytest.raises(
        ValueError,
        match="pending semantic review cannot have a resolution",
    ):
        _review_item(
            resolved_at=datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc)
        )


def test_resolved_semantic_review_rejects_extra_resolution_text() -> None:
    with pytest.raises(ValueError, match="semantic review resolution has invalid schema"):
        _review_item(
            review_status="resolved",
            resolution={
                "resolution_status": "resolved",
                "resolution_sha256": "b" * 64,
                "notes": "法院表示下周重新查控",
            },
            resolved_at=datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc),
        )


@pytest.mark.parametrize("review_status", ("resolved", "dismissed", "expired"))
def test_terminal_semantic_review_accepts_only_matching_status_and_digest(
    review_status: str,
) -> None:
    item = _review_item(
        review_status=review_status,
        resolution={
            "resolution_status": review_status,
            "resolution_sha256": "b" * 64,
        },
        resolved_at=datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc),
    )

    assert item.as_dict()["resolution"] == {
        "resolution_status": review_status,
        "resolution_sha256": "b" * 64,
    }


@pytest.mark.parametrize(
    "resolution",
    (
        {"resolution_status": "dismissed", "resolution_sha256": "b" * 64},
        {"resolution_status": "resolved", "resolution_sha256": "raw text"},
    ),
)
def test_terminal_semantic_review_rejects_mismatched_or_non_digest_resolution(
    resolution,
) -> None:
    with pytest.raises(ValueError):
        _review_item(
            review_status="resolved",
            resolution=resolution,
            resolved_at=datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc),
        )


def test_deferred_semantic_event_rejects_extra_business_text_field() -> None:
    with pytest.raises(ValueError, match="payload has invalid schema"):
        _deferred_event(
            payload={
                "action_id": "action-1",
                "decision_verdict": "deferred_audit_only",
                "evidence_refs": ["segment_sha256:" + "b" * 64],
                "memo": "法院表示下周重新查控",
            }
        )


def test_semantic_review_item_is_immutable_audit_only_wire_contract() -> None:
    candidate = {
        "action_id": "action-1",
        "decision_verdict": "blocked",
        "evidence_refs": ["segment_sha256:" + "a" * 64],
    }
    item = SemanticReviewItem(
        review_id=_uuid5("review"),
        trace_id=_uuid5("trace"),
        decision_id=_uuid5("decision"),
        tenant_id="tenant-1",
        user_id="user-1",
        conversation_id="conversation-1",
        source_turn_id="message-1",
        source_message_id="message-1",
        segment_id="segment-1",
        segment_text_sha256="a" * 64,
        segment_start_offset=0,
        segment_end_offset=8,
        domain="case",
        operation="record_case_progress",
        object_ref={
            "object_type": "case",
            "stable_id": "case-1",
            "version": 3,
            "label": "案件一",
        },
        reason_code="case_reference_ambiguous",
        candidate_snapshot=candidate,
        resolution={},
        idempotency_key="review-idempotency-1",
        created_at=datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc),
    )

    candidate["action_id"] = "tampered"
    payload = item.as_dict()

    assert payload["candidate_snapshot"]["action_id"] == "action-1"
    assert payload["review_status"] == "pending_human_review"
    assert payload["audit_only"] is True
    assert payload["business_write_allowed"] is False
    assert payload["resolved_at"] is None


def test_deferred_semantic_event_is_fresh_admission_only_and_has_a_valid_window() -> None:
    created_at = datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)
    payload_source = {
        "action_id": "action-1",
        "decision_verdict": "deferred_audit_only",
        "evidence_refs": ["segment_sha256:" + "b" * 64],
    }
    event = DeferredSemanticEvent(
        deferred_event_id=_uuid5("deferred"),
        trace_id=_uuid5("trace"),
        decision_id=_uuid5("decision"),
        tenant_id="tenant-1",
        user_id="user-1",
        conversation_id="conversation-1",
        source_turn_id="message-1",
        source_message_id="message-1",
        segment_id="segment-1",
        segment_text_sha256="b" * 64,
        segment_start_offset=0,
        segment_end_offset=8,
        domain="case",
        operation="record_case_progress",
        object_ref=None,
        reason_code="future_semantic_signal",
        payload=payload_source,
        not_before=created_at + timedelta(hours=1),
        expires_at=created_at + timedelta(days=1),
        idempotency_key="deferred-idempotency-1",
        created_at=created_at,
    )

    payload_source["action_id"] = "tampered"
    serialized = event.as_dict()

    assert serialized["payload"]["action_id"] == "action-1"
    assert serialized["event_status"] == "recorded"
    assert serialized["audit_only"] is True
    assert serialized["business_write_allowed"] is False
    assert serialized["requires_fresh_admission"] is True


def _blocked_turn_and_proposal() -> tuple[CognitiveTurn, ConversationState, SemanticInterpretation]:
    occurred_at = datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)
    turn = CognitiveTurn(
        tenant_id="tenant-1",
        user_id="user-1",
        actor_user_id="user-1",
        conversation_id="conversation-1",
        message_id="message-1",
        text="执行未知动作",
        occurred_at=occurred_at,
    )
    state = ConversationState(
        user_id=turn.user_id,
        conversation_id=turn.conversation_id,
        version=4,
    )
    proposal = SemanticInterpretation.from_payload(
        {
            "intents": ["chat"],
            "segments": [
                {
                    "segment_id": "segment-1",
                    "text": turn.text,
                    "intents": ["chat"],
                    "entity_ids": [],
                    "action_ids": ["action-1"],
                }
            ],
            "entities": [],
            "confidence": 1.0,
            "required_actions": [
                {
                    "action_id": "action-1",
                    "action_type": "unknown_action",
                    "intent": "chat",
                    "entity_ids": [],
                }
            ],
            "clarification_need": None,
            "context_update": {"preserve_current_goal": True},
        }
    )
    return turn, state, proposal


def test_shadow_review_capture_adds_one_minimal_artifact_without_changing_verdict() -> None:
    turn, state, proposal = _blocked_turn_and_proposal()
    baseline = DomainAdmissionEngine().admit(turn, state, proposal)

    captured = DomainAdmissionEngine(
        capture_policy=AdmissionCapturePolicy(
            mode="shadow",
            review_enabled=True,
            deferred_enabled=False,
        )
    ).admit(turn, state, proposal)

    assert captured.decisions == baseline.decisions
    assert captured.interpretation == baseline.interpretation
    assert captured.tickets == baseline.tickets
    assert len(captured.review_items) == 1
    assert captured.deferred_events == ()
    review = captured.review_items[0]
    assert review.decision_id == captured.decisions[0].decision_id
    assert review.candidate_snapshot == {
        "action_id": "action-1",
        "decision_verdict": "blocked",
        "evidence_refs": (
            "segment_sha256:" + captured.decisions[0].segment_text_sha256,
        ),
    }
    assert captured.as_dict()["review_items"] == [review.as_dict()]


class _StaticInterpreter:
    def __init__(self, proposal: SemanticInterpretation) -> None:
        self._proposal = proposal

    async def interpret(
        self,
        turn: CognitiveTurn,
        state: ConversationState,
    ) -> SemanticInterpretation:
        return self._proposal


def test_cognitive_decision_carries_shadow_review_items_without_changing_planner_input() -> None:
    turn, state, proposal = _blocked_turn_and_proposal()
    result = asyncio.run(
        CognitiveCoreV3(
            _StaticInterpreter(proposal),
            admission_engine=DomainAdmissionEngine(
                capture_policy=AdmissionCapturePolicy(
                    mode="shadow",
                    review_enabled=True,
                    deferred_enabled=False,
                )
            ),
            admission_enforced=False,
        ).process(turn, state)
    )

    assert result.decision.admission_mode == "shadow"
    assert result.decision.required_actions == proposal.required_actions
    assert len(result.decision.admission_review_items) == 1
    assert result.decision.admission_deferred_events == ()
    assert result.decision.as_dict()["admission_review_items"] == [
        result.decision.admission_review_items[0].as_dict()
    ]


def test_runtime_capture_policy_uses_trusted_scope_and_does_not_wire_shadow_replay() -> None:
    settings = SimpleNamespace(
        agent2_semantic_admission_enabled=True,
        agent2_semantic_admission_enforce=False,
        agent2_semantic_admission_tenant_allowlist="tenant-1",
        agent2_semantic_admission_user_allowlist="user-1",
        agent2_semantic_admission_review_capture=False,
        agent2_semantic_admission_deferred_capture=False,
        agent2_semantic_admission_shadow_replay=True,
    )

    allowed = semantic_admission_capture_policy(
        settings,
        tenant_id="tenant-1",
        user_id="user-1",
    )
    outside = semantic_admission_capture_policy(
        settings,
        tenant_id="tenant-other",
        user_id="user-1",
    )

    assert allowed == AdmissionCapturePolicy(
        mode="shadow",
        review_enabled=False,
        deferred_enabled=False,
    )
    assert outside == AdmissionCapturePolicy()


class _FixedVerdictAdmissionEngine(DomainAdmissionEngine):
    def __init__(self, *, status: str, capture_policy: AdmissionCapturePolicy) -> None:
        super().__init__(capture_policy=capture_policy)
        self._status = status

    def _decide(self, **kwargs):
        action = kwargs["action"]
        segment_id = kwargs["segment_id"]
        return (
            AdmissionDecision(
                action_id=action.action_id,
                segment_id=segment_id,
                domain="runtime",
                operation=action.action_type,
                status=self._status,
                reason_code=f"{self._status}_reason",
            ),
            {},
        )


def test_deferred_verdict_captures_only_deferred_event_even_in_shadow_review_mode() -> None:
    turn, state, proposal = _blocked_turn_and_proposal()

    result = _FixedVerdictAdmissionEngine(
        status="deferred_audit_only",
        capture_policy=AdmissionCapturePolicy(
            mode="shadow",
            review_enabled=True,
            deferred_enabled=True,
        ),
    ).admit(turn, state, proposal)

    assert result.decisions[0].status == "deferred_audit_only"
    assert result.review_items == ()
    assert len(result.deferred_events) == 1
    assert result.deferred_events[0].payload["decision_verdict"] == (
        "deferred_audit_only"
    )


def test_non_shadow_review_capture_accepts_only_explicit_review_only_verdict() -> None:
    turn, state, proposal = _blocked_turn_and_proposal()
    policy = AdmissionCapturePolicy(
        mode="enforced",
        review_enabled=True,
        deferred_enabled=False,
    )

    blocked = _FixedVerdictAdmissionEngine(
        status="blocked",
        capture_policy=policy,
    ).admit(turn, state, proposal)
    review_only = _FixedVerdictAdmissionEngine(
        status="review_only",
        capture_policy=policy,
    ).admit(turn, state, proposal)

    assert blocked.review_items == ()
    assert len(review_only.review_items) == 1
    assert review_only.decisions[0].status == "review_only"
