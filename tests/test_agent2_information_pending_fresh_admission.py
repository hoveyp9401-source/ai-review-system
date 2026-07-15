from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from app.agent2.admission_hashes import admission_claim_hashes_match
from app.agent2.business.admission import bind_business_execution_context
from app.agent2.business.compiler import Phase2BusinessCommandCompiler
from app.agent2.business.contracts import BusinessCommandContext, CreateTravelIntent
from app.agent2.cognitive_core_v3 import CognitiveCoreV3, CognitiveTurn
from app.agent2.command_planner_v3 import CognitiveCommandPlanner, CommandPlanningContext
from app.agent2.conversation_state import ConversationState
from app.agent2.information_pending_admission import (
    InformationContinuationAdmissionEngine,
    InformationContinuationAdmissionError,
    InformationContinuationSemanticInterpreter,
)
from app.agent2.information_pending_runtime import (
    InformationContinuationPreprocessResult,
)
from app.agent2.admission_contracts import InformationPending
from app.agent2.information_pending import (
    InformationContinuationRequest,
    InformationPendingResolution,
)


NOW = datetime(2026, 7, 14, 1, 1, tzinfo=UTC)
TENANT = "sandbox-agent2-phase2-20260711"


def _ready() -> InformationContinuationPreprocessResult:
    pending = InformationPending(
        pending_id="21b742ce-fe40-5bf5-a838-c80f73d569a1",
        trace_id="c796b14d-1d76-58e1-a46f-f62fa9ee8fea",
        decision_id="c33f9bc1-bc2e-5733-b014-190f597a0a7f",
        tenant_id=TENANT,
        user_id="user-1",
        conversation_id="conversation-1",
        source_turn_id="message-original",
        source_message_id="message-original",
        segment_id="segment-original",
        segment_text_sha256="a" * 64,
        segment_start_offset=0,
        segment_end_offset=6,
        domain="travel",
        operation="record_travel_event",
        object_ref={
            "object_type": "travel_intent",
            "stable_id": "2bbefcc9-d7cf-5f2e-a310-e800a434ef29",
            "version": None,
        },
        expected_conversation_state_version=4,
        missing_fields=("travel_date",),
        question_snapshot={"destination": "南京"},
        acceptable_answer_forms={"field": "travel_date"},
        created_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=9),
        idempotency_key="pending-key",
        pending_status="awaiting_input",
    )
    continuation = InformationContinuationRequest(
        pending_id=pending.pending_id,
        tenant_id=TENANT,
        user_id="user-1",
        conversation_id="conversation-1",
        source_message_id="message-answer",
        domain="travel",
        operation="record_travel_event",
        object_ref=pending.object_ref,
        field_values={"travel_date": "2026-07-15"},
        raw_values={"travel_date": "明天"},
        evidence_spans={"travel_date": (0, 2)},
    )
    resolution = InformationPendingResolution(
        status="ready_for_fresh_admission",
        pending_id=pending.pending_id,
        reason="fresh_admission_required",
        pending_after=pending,
        continuation=continuation,
    )
    return InformationContinuationPreprocessResult(
        handled=True,
        status="ready_for_fresh_admission",
        reason="fresh_admission_required",
        pending=pending,
        resolution=resolution,
        fresh_admission_request=continuation,
    )


def _turn() -> CognitiveTurn:
    return CognitiveTurn(
        user_id=f"{TENANT}:user-1",
        actor_user_id="user-1",
        tenant_id=TENANT,
        conversation_id="conversation-1",
        message_id="message-answer",
        text="明天",
        occurred_at=NOW,
        resources={"timezone": "Asia/Shanghai"},
    )


def _state(version=4) -> ConversationState:
    return ConversationState(
        user_id=f"{TENANT}:user-1",
        conversation_id="conversation-1",
        version=version,
    )


@pytest.mark.asyncio
async def test_fresh_continuation_admission_uses_current_answer_segment_and_old_trusted_object():
    ready = _ready()
    core = CognitiveCoreV3(
        InformationContinuationSemanticInterpreter(ready),
        admission_engine=InformationContinuationAdmissionEngine(ready),
        admission_enforced=True,
    )

    result = await core.process(_turn(), _state())

    decision = result.decision
    assert decision.admission_mode == "enforced"
    assert len(decision.admission_tickets) == 1
    ticket = decision.admission_tickets[0]
    assert ticket.trace_id != ready.pending.trace_id
    assert ticket.source_message_id == "message-answer"
    assert ticket.segment_text_sha256 == decision.segments[0].text_hash
    assert decision.segments[0].text == "明天"
    assert ticket.object_ref == ready.pending.object_ref
    assert ticket.authority_scope["destination"] == "南京"
    assert ticket.authority_scope["travel_date"] == "2026-07-15"
    assert ticket.authority_scope["raw_fact"] == "明天"
    assert ticket.authority_scope["information_pending_id"] == ready.pending.pending_id
    assert admission_claim_hashes_match(ticket.as_dict())
    assert result.state.version == 5


@pytest.mark.asyncio
async def test_fresh_ticket_plans_and_compiles_the_preallocated_travel_intent():
    ready = _ready()
    core_result = await CognitiveCoreV3(
        InformationContinuationSemanticInterpreter(ready),
        admission_engine=InformationContinuationAdmissionEngine(ready),
        admission_enforced=True,
    ).process(_turn(), _state())
    plan = CognitiveCommandPlanner().plan(
        core_result.decision,
        CommandPlanningContext(
            message_id="message-answer",
            actor_user_id=UUID("e307c593-7c8d-5793-aae2-71d0730f7d43"),
        ),
    )
    assert len(plan.business_commands) == 1
    assert plan.blocked_actions == ()
    candidate = plan.business_commands[0]
    base_context = BusinessCommandContext(
        tenant_id=TENANT,
        company_id="company-1",
        department_id="department-1",
        team_id="team-1",
        actor_user_id="user-1",
        actor_role_ids=("lawyer",),
        allowed_case_ids=(),
        source_message_id="message-answer",
        source_channel="manual_text",
        occurred_at=NOW,
        conversation_id="conversation-1",
        execution_started_at=NOW + timedelta(seconds=1),
        conversation_state_version=4,
    )
    compiled = Phase2BusinessCommandCompiler().compile(
        candidate,
        bind_business_execution_context(candidate, base_context),
        cases=(),
    )

    assert compiled.block is None
    assert isinstance(compiled.command, CreateTravelIntent)
    assert compiled.command.destination_raw == "南京"
    assert compiled.command.start_at.date().isoformat() == "2026-07-15"
    assert candidate.admission_ticket["object_ref"]["stable_id"] == str(
        ready.pending.object_ref["stable_id"]
    )


@pytest.mark.asyncio
async def test_modified_answer_evidence_or_state_drift_fails_before_ticket_issuance():
    ready = _ready()
    altered_request = replace(
        ready.fresh_admission_request,
        raw_values={"travel_date": "后天"},
    )
    altered = InformationContinuationPreprocessResult(
        handled=True,
        status="ready_for_fresh_admission",
        reason="fresh_admission_required",
        pending=ready.pending,
        resolution=replace(ready.resolution, continuation=altered_request),
        fresh_admission_request=altered_request,
    )
    with pytest.raises(
        InformationContinuationAdmissionError,
        match="not grounded",
    ):
        await CognitiveCoreV3(
            InformationContinuationSemanticInterpreter(altered),
            admission_engine=InformationContinuationAdmissionEngine(altered),
            admission_enforced=True,
        ).process(_turn(), _state())

    with pytest.raises(
        InformationContinuationAdmissionError,
        match="scope changed",
    ):
        await CognitiveCoreV3(
            InformationContinuationSemanticInterpreter(ready),
            admission_engine=InformationContinuationAdmissionEngine(ready),
            admission_enforced=True,
        ).process(_turn(), _state(version=5))
