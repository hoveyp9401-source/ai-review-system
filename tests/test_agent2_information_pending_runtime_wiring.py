from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import hashlib
from types import SimpleNamespace

import pytest

from app.agent2.admission_contracts import InformationPending
from app.agent2.business.contracts import BusinessCommandContext
from app.agent2.information_pending import (
    InformationContinuationRequest,
    InformationPendingResolution,
)
from app.agent2.information_pending_runtime import (
    InformationContinuationPreprocessResult,
)
from app.agent2.information_pending_sql import (
    SqlInformationPendingContinuationAdapter,
)
from app.agent2.turn_runtime import (
    Agent2TurnRuntime,
    InformationContinuationBlocked,
    VerifiedTurnRejected,
    VerifiedTurnRequest,
    production_agent2_turn_runtime,
)
from app.workflows.intake import IncomingMessageEnvelope


NOW = datetime(2026, 7, 14, 1, 0, tzinfo=UTC)
TENANT = "sandbox-agent2-phase2-20260711"


def _pending() -> InformationPending:
    return InformationPending(
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
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        idempotency_key="pending-key",
        pending_status="awaiting_input",
    )


def _ready() -> InformationContinuationPreprocessResult:
    pending = _pending()
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


def _request() -> VerifiedTurnRequest:
    envelope = IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="Test User",
        dingtalk_user_id="ding-user-1",
        source="manual_text",
        raw_text="明天",
        message_id="message-answer",
        conversation_id="conversation-1",
        received_at=NOW + timedelta(minutes=1),
    )
    return VerifiedTurnRequest(
        session=object(),
        user=SimpleNamespace(id="user-1", timezone="Asia/Shanghai"),
        envelope=envelope,
        llm_client=object(),
        daily_report=None,
        report_date=date(2026, 7, 14),
        settings=SimpleNamespace(
            agent2_semantic_admission_enabled=True,
            agent2_semantic_admission_enforce=True,
            agent2_semantic_admission_tenant_allowlist=TENANT,
            agent2_semantic_admission_user_allowlist="user-1",
        ),
        business_context=BusinessCommandContext(
            tenant_id=TENANT,
            company_id="company-1",
            department_id="department-1",
            team_id="team-1",
            actor_user_id="user-1",
            actor_role_ids=("lawyer",),
            allowed_case_ids=(),
            source_message_id="message-answer",
            source_channel="manual_text",
            occurred_at=NOW + timedelta(minutes=1),
            conversation_id="conversation-1",
        ),
    )


@pytest.mark.asyncio
async def test_unified_runtime_passes_ready_continuation_to_fresh_admission(
    monkeypatch,
):
    ready = _ready()
    calls = []

    class Preprocessor:
        async def preprocess(self, request):
            calls.append(("preprocess", request))
            return ready

        async def settle(self, *args, **kwargs):
            calls.append(("settle", args, kwargs))
            return "settled"

    class Sink:
        async def persist(self, request):
            calls.append(("persist", request))

    async def load_state(**kwargs):
        return 4

    monkeypatch.setattr(
        "app.agent2.turn_runtime._load_verified_conversation_state_version",
        load_state,
    )

    async def evaluate(**kwargs):
        calls.append(("evaluate", kwargs))
        assert kwargs["information_continuation"] is ready
        ticket = SimpleNamespace(
            ticket_id="fresh-ticket",
            source_message_id="message-answer",
            source_turn_id="message-answer",
            domain="travel",
            operation="record_travel_event",
            object_ref=dict(_pending().object_ref),
            expected_conversation_state_version=4,
            authority_scope={
                "information_pending_id": _pending().pending_id,
                "continuation_source_message_id": "message-answer",
                "continuation_field_values": {"travel_date": "2026-07-15"},
                "continuation_raw_values": {"travel_date": "明天"},
            },
        )
        return SimpleNamespace(
            base_state=SimpleNamespace(version=4),
            state_persisted=False,
            decision=SimpleNamespace(
                source_text_hash=hashlib.sha256("明天".encode()).hexdigest(),
                admission_mode="enforced",
                admission_tickets=(ticket,),
                admission_information_pendings=(),
                admission_trace=SimpleNamespace(
                    trace_id="fresh-trace",
                    decisions=(SimpleNamespace(status="admitted"),),
                ),
            ),
        )

    runtime = Agent2TurnRuntime(
        evaluator=evaluate,
        admission_artifact_sink=Sink(),
        information_continuation_preprocessor=Preprocessor(),
    )
    result = await runtime.handle(_request())

    assert result.information_continuation is ready
    assert [item[0] for item in calls[:3]] == ["preprocess", "evaluate", "persist"]
    settled = await runtime.settle_information_continuation(
        result,
        outcome=object(),
        settled_trace_id="fresh-trace",
        settled_at=NOW + timedelta(minutes=2),
        session=object(),
    )
    assert settled == "settled"


@pytest.mark.asyncio
async def test_typed_pending_block_stops_before_llm_or_any_business_command(
    monkeypatch,
):
    ready = _ready()
    blocked = replace_result(
        ready,
        status="clarification_required",
        reason="unique_active_pending_required",
    )
    evaluator_calls = 0

    class Preprocessor:
        async def preprocess(self, request):
            return blocked

        async def settle(self, *args, **kwargs):
            raise AssertionError("blocked continuation must not settle")

    async def evaluate(**kwargs):
        nonlocal evaluator_calls
        evaluator_calls += 1

    async def load_state(**kwargs):
        return 4

    monkeypatch.setattr(
        "app.agent2.turn_runtime._load_verified_conversation_state_version",
        load_state,
    )
    with pytest.raises(InformationContinuationBlocked) as error:
        await Agent2TurnRuntime(
            evaluator=evaluate,
            admission_artifact_sink=SimpleNamespace(),
            information_continuation_preprocessor=Preprocessor(),
        ).handle(_request())

    assert error.value.result.status == "clarification_required"
    assert evaluator_calls == 0


@pytest.mark.asyncio
async def test_ready_continuation_rejects_a_ticket_not_bound_to_original_pending(
    monkeypatch,
):
    class Preprocessor:
        async def preprocess(self, request):
            return _ready()

    async def load_state(**kwargs):
        return 4

    monkeypatch.setattr(
        "app.agent2.turn_runtime._load_verified_conversation_state_version",
        load_state,
    )

    async def evaluate(**kwargs):
        return SimpleNamespace(
            base_state=SimpleNamespace(version=4),
            state_persisted=False,
            decision=SimpleNamespace(
                source_text_hash=hashlib.sha256("明天".encode()).hexdigest(),
                admission_mode="enforced",
                admission_tickets=(
                    SimpleNamespace(
                        source_message_id="message-answer",
                        source_turn_id="message-answer",
                        domain="travel",
                        operation="record_travel_event",
                        object_ref=dict(_pending().object_ref),
                        expected_conversation_state_version=4,
                        authority_scope={"information_pending_id": "wrong"},
                    ),
                ),
                admission_information_pendings=(),
                admission_trace=SimpleNamespace(
                    trace_id="fresh-trace",
                    decisions=(SimpleNamespace(status="admitted"),),
                ),
            ),
        )

    with pytest.raises(
        VerifiedTurnRejected,
        match="information_continuation_ticket_claims_mismatch",
    ):
        await Agent2TurnRuntime(
            evaluator=evaluate,
            admission_artifact_sink=SimpleNamespace(),
            information_continuation_preprocessor=Preprocessor(),
        ).handle(_request())


def test_production_runtime_factory_wires_sql_sink_and_pending_adapter():
    runtime = production_agent2_turn_runtime()

    assert runtime._admission_artifact_sink.__class__.__name__ == "SqlAdmissionArtifactSink"
    assert isinstance(
        runtime._information_continuation_preprocessor,
        SqlInformationPendingContinuationAdapter,
    )


def replace_result(result, *, status, reason):
    return InformationContinuationPreprocessResult(
        handled=True,
        status=status,
        reason=reason,
        pending=result.pending,
        resolution=result.resolution,
        fresh_admission_request=None,
    )
