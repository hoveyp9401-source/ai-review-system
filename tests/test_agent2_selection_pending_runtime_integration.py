from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
import hashlib
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from app.agent2.business.contracts import BusinessCommandContext
from app.agent2.cognitive_orchestrator_v3 import (
    CognitiveOrchestrationResult,
    finalize_cognitive_state_after_execution,
)
from app.agent2.cognitive_core_v3 import (
    CognitiveCoreV3,
    CognitiveTurn,
    SemanticInterpretation,
)
from app.agent2.domain_admission import DomainAdmissionEngine
from app.agent2.command_planner_v3 import CognitiveCommandPlan
from app.agent2.conversation_state import (
    ConversationGoal,
    ConversationState,
    UserConstraints,
)
from app.agent2.conversation_state_store import InMemoryConversationStateStore
from app.agent2.selection_pending import (
    SelectionCandidate,
    SelectionPending,
    SelectionResolution,
    protect_selection_continuation_payload,
)
from app.agent2.selection_pending_runtime import (
    SelectionAnswerEvidence,
    SelectionContinuationPreprocessResult,
    SelectionContinuationRequest,
)
from app.agent2.turn_runtime import (
    Agent2TurnRuntime,
    SelectionContinuationBlocked,
    VerifiedTurnRejected,
    VerifiedTurnRequest,
    production_agent2_turn_runtime,
)
from app.workflows.intake import IncomingMessageEnvelope


NOW = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
TENANT = "sandbox-agent2-phase2-20260711"
CASE_1 = str(uuid5(NAMESPACE_URL, "selection-runtime-case-1"))
CASE_2 = str(uuid5(NAMESPACE_URL, "selection-runtime-case-2"))


def _protected_continuation() -> dict:
    source_text = "海花岛案今天联系法院推进，法院表示下周重新查控。"
    source_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    return protect_selection_continuation_payload(
        {
            "typed_business_command": {
                "command_id": str(uuid5(NAMESPACE_URL, "selection-runtime-command")),
                "decision_id": str(uuid5(NAMESPACE_URL, "selection-runtime-decision")),
                "sub_decision_id": str(
                    uuid5(NAMESPACE_URL, "selection-runtime-sub-decision")
                ),
                "command_type": "record_case_progress_candidate",
                "target_system": "case_progress",
                "entity_ids": ["case-ref"],
                "payload": {
                    "entities": [
                        {
                            "entity_id": "case-ref",
                            "entity_type": "case_ref",
                            "value": "海花岛案",
                            "confidence": 0.99,
                            "attributes": {
                                "normalized_fact": source_text,
                                "statement_mode": "asserted",
                                "evidence_spans": [[4, len(source_text)]],
                            },
                        }
                    ],
                    "parameters": {},
                    "source_segments": [
                        {
                            "segment_id": "original-segment",
                            "text": source_text,
                            "text_hash": source_hash,
                            "start_offset": 0,
                            "end_offset": len(source_text),
                        }
                    ],
                },
                "execution_mode": "candidate",
                "idempotency_key": "selection-runtime-command-key",
            },
            "bind": {"entity_type": "case_ref", "attribute": "case_id"},
        }
    )


def _pending(*, expected_version: int = 4) -> SelectionPending:
    return SelectionPending(
        pending_id=str(uuid5(NAMESPACE_URL, "selection-runtime-pending")),
        tenant_id=TENANT,
        user_id="user-1",
        conversation_id="conversation-1",
        domain="case",
        operation="record_case_progress",
        source_turn_id="original-message",
        candidates=(
            SelectionCandidate(CASE_1, 7, "海花岛一期案"),
            SelectionCandidate(CASE_2, 11, "海花岛二期案"),
        ),
        acceptable_answer_forms={"第一个": CASE_1, "第二个": CASE_2},
        expected_conversation_state_version=expected_version,
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        status="active",
        continuation_payload=_protected_continuation(),
    )


def _ready() -> SelectionContinuationPreprocessResult:
    pending = _pending()
    evidence_text = "第二个"
    evidence = SelectionAnswerEvidence(
        source_message_id="answer-message",
        text=evidence_text,
        text_sha256=hashlib.sha256(evidence_text.encode("utf-8")).hexdigest(),
        start_offset=0,
        end_offset=len(evidence_text),
    )
    protected = pending.continuation_payload["protected_snapshot"]
    fresh = SelectionContinuationRequest(
        pending_id=pending.pending_id,
        tenant_id=TENANT,
        user_id="user-1",
        conversation_id="conversation-1",
        source_message_id="answer-message",
        domain="case",
        operation="record_case_progress",
        candidate_stable_id=CASE_2,
        candidate_version=11,
        evidence=evidence,
        original_source_digest=str(protected["original_source_digest"]),
        original_continuation_sha256=str(
            protected["original_continuation_sha256"]
        ),
        bound_payload_sha256="b" * 64,
        command_type="record_case_progress_candidate",
    )
    resolution = SelectionResolution(
        status="selected",
        pending_id=pending.pending_id,
        selected_candidate_id=CASE_2,
        selected_candidate_version=11,
        reason="",
        actual_write=False,
        pending_after=pending,
    )
    return SelectionContinuationPreprocessResult(
        handled=True,
        status="ready_for_fresh_admission",
        reason="fresh_admission_required",
        pending=pending,
        candidate=pending.candidates[1],
        resolution=resolution,
        fresh_admission_request=fresh,
    )


class _AuditSession:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def execute(self, statement):
        return SimpleNamespace(scalar_one_or_none=lambda: None)

    def add(self, event) -> None:
        self.events.append(event)

    async def flush(self) -> None:
        return None

    @asynccontextmanager
    async def begin_nested(self):
        yield


def _request(
    *, session: object | None = None, source: str = "manual_text"
) -> VerifiedTurnRequest:
    envelope = IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="Test User",
        dingtalk_user_id="ding-user-1",
        source=source,
        raw_text="第二个",
        message_id="answer-message",
        conversation_id="conversation-1",
        received_at=NOW + timedelta(minutes=1),
    )
    return VerifiedTurnRequest(
        session=session or _AuditSession(),
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
            allowed_case_ids=(CASE_1, CASE_2),
            source_message_id="answer-message",
            source_channel={
                "manual_text": "manual_text",
                "agent2_dingtalk_webhook_text": "dingtalk_webhook",
                "agent2_dingtalk_stream_text": "dingtalk_stream",
            }.get(source, source),
            occurred_at=NOW + timedelta(minutes=1),
            conversation_id="conversation-1",
        ),
    )


def _fresh_ticket(ready: SelectionContinuationPreprocessResult):
    fresh = ready.fresh_admission_request
    assert fresh is not None
    pending = ready.pending
    assert pending is not None
    evidence = fresh.evidence.as_dict(segment_id="answer-segment")
    return SimpleNamespace(
        ticket_id="fresh-selection-ticket",
        source_message_id="answer-message",
        source_turn_id="answer-message",
        segment_id="answer-segment",
        segment_text_sha256=fresh.evidence.text_sha256,
        segment_start_offset=0,
        segment_end_offset=3,
        domain="case",
        operation="record_case_progress",
        object_ref={"object_type": "case", "stable_id": CASE_2, "version": 11},
        expected_conversation_state_version=4,
        authority_scope={
            "selection_pending_id": pending.pending_id,
            "selection_pending_source_turn_id": pending.source_turn_id,
            "candidate_stable_id": CASE_2,
            "candidate_version": 11,
            "original_source_digest": fresh.original_source_digest,
            "original_continuation_sha256": fresh.original_continuation_sha256,
            "selection_evidence": evidence,
        },
    )


@pytest.mark.asyncio
async def test_unified_runtime_requires_fresh_selection_admission(monkeypatch):
    ready = _ready()
    calls: list[tuple[str, object]] = []

    class Preprocessor:
        async def preprocess(self, request, **kwargs):
            calls.append(("preprocess", request))
            return ready

    class Sink:
        async def persist(self, request):
            calls.append(("persist", request))

    async def load_state(**kwargs):
        return ConversationState(
            user_id=f"{TENANT}:user-1",
            conversation_id="conversation-1",
            version=4,
            selection_pending=(_pending(),),
        )

    monkeypatch.setattr(
        "app.agent2.turn_runtime._load_verified_conversation_state", load_state
    )

    async def evaluate(**kwargs):
        calls.append(("evaluate", kwargs))
        assert kwargs["selection_continuation"] is ready
        ticket = _fresh_ticket(ready)
        return SimpleNamespace(
            base_state=SimpleNamespace(version=4),
            state_persisted=False,
            decision=SimpleNamespace(
                source_text_hash=hashlib.sha256("第二个".encode("utf-8")).hexdigest(),
                admission_mode="enforced",
                admission_tickets=(ticket,),
                admission_information_pendings=(),
                admission_trace=SimpleNamespace(
                    trace_id="fresh-selection-trace",
                    decisions=(SimpleNamespace(status="admitted"),),
                ),
            ),
        )

    request = _request()
    result = await Agent2TurnRuntime(
        evaluator=evaluate,
        admission_artifact_sink=Sink(),
        selection_continuation_preprocessor=Preprocessor(),
    ).handle(request)

    assert result.selection_continuation is ready
    assert [name for name, _ in calls] == ["preprocess", "evaluate", "persist"]
    assert result.mutation_execution_authority == "semantic_ticket"
    assert len(request.session.events) == 1
    assert (
        request.session.events[0].llm_decision_json["selection_status"]
        == "ready_for_fresh_admission"
    )


@pytest.mark.asyncio
async def test_selection_block_stops_before_semantic_evaluator(monkeypatch):
    ready = _ready()
    blocked = SelectionContinuationPreprocessResult(
        handled=True,
        status="clarification_required",
        reason="selection_answer_not_exact",
        pending=ready.pending,
        candidate=None,
        resolution=ready.resolution,
        fresh_admission_request=None,
    )
    evaluator_calls = 0

    class Preprocessor:
        async def preprocess(self, request, **kwargs):
            return blocked

    async def load_state(**kwargs):
        return ConversationState(
            user_id=f"{TENANT}:user-1",
            conversation_id="conversation-1",
            version=4,
            selection_pending=(_pending(),),
        )

    monkeypatch.setattr(
        "app.agent2.turn_runtime._load_verified_conversation_state", load_state
    )

    async def evaluate(**kwargs):
        nonlocal evaluator_calls
        evaluator_calls += 1

    with pytest.raises(SelectionContinuationBlocked) as error:
        await Agent2TurnRuntime(
            evaluator=evaluate,
            admission_artifact_sink=SimpleNamespace(),
            selection_continuation_preprocessor=Preprocessor(),
        ).handle(_request())

    assert error.value.result.reason == "selection_answer_not_exact"
    assert evaluator_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "reason"),
    (
        ("clarification_required", "selection_answer_not_exact"),
        ("duplicate_source_message", "source_message_already_processed"),
        ("already_consumed", "selection_pending_already_consumed"),
    ),
)
@pytest.mark.parametrize(
    "source",
    (
        "manual_text",
        "agent2_dingtalk_webhook_text",
        "agent2_dingtalk_stream_text",
    ),
)
async def test_nonterminal_selection_block_commits_one_redacted_audit_without_state_change(
    monkeypatch, status, reason, source,
):
    ready = _ready()
    blocked = SelectionContinuationPreprocessResult(
        handled=True,
        status=status,
        reason=reason,
        pending=ready.pending,
        candidate=None,
        resolution=ready.resolution,
        fresh_admission_request=None,
    )
    base = ConversationState(
        user_id=f"{TENANT}:user-1",
        conversation_id="conversation-1",
        version=4,
        selection_pending=(_pending(),),
    )

    class Preprocessor:
        async def preprocess(self, request, **kwargs):
            return blocked

    async def load_state(**kwargs):
        return base

    monkeypatch.setattr(
        "app.agent2.turn_runtime._load_verified_conversation_state", load_state
    )

    class Session:
        def __init__(self) -> None:
            self.events: list[object] = []
            self.committed: list[object] = []

        async def execute(self, statement):
            return SimpleNamespace(scalar_one_or_none=lambda: None)

        def add(self, event) -> None:
            self.events.append(event)

        async def flush(self) -> None:
            return None

        async def commit(self) -> None:
            self.committed.extend(self.events)

        @asynccontextmanager
        async def begin_nested(self):
            yield

    session = Session()
    request = _request(session=session, source=source)

    with pytest.raises(SelectionContinuationBlocked):
        await Agent2TurnRuntime(
            evaluator=lambda **kwargs: pytest.fail("blocked turn reached evaluator"),
            admission_artifact_sink=SimpleNamespace(),
            selection_continuation_preprocessor=Preprocessor(),
        ).handle(request)

    await session.commit()
    assert base.version == 4
    assert base.selection_pending[0].status == "active"
    assert len(session.committed) == 1
    event = session.committed[0]
    assert event.backend_action == "agent2_selection_preprocess"
    assert event.message_text == ""
    assert event.llm_decision_json["selection_status"] == status
    assert event.llm_decision_json["actual_write"] is False
    serialized = str(event.llm_decision_json)
    assert request.envelope.raw_text not in serialized
    assert "continuation_payload" not in serialized
    assert "authority_scope" not in serialized


@pytest.mark.asyncio
async def test_information_and_selection_collision_persists_one_cross_pending_audit(
    monkeypatch,
):
    selection_ready = _ready()
    information_ready = SimpleNamespace(
        handled=True,
        status="ready_for_fresh_admission",
        reason="fresh_admission_required",
        pending=SimpleNamespace(
            pending_id="information-1",
            pending_status="awaiting_input",
        ),
    )

    class SelectionPreprocessor:
        async def preprocess(self, request, **kwargs):
            return selection_ready

    class InformationPreprocessor:
        async def preprocess(self, request):
            return information_ready

    async def load_information_state(**kwargs):
        return 4

    async def load_selection_state(**kwargs):
        return ConversationState(
            user_id=f"{TENANT}:user-1",
            conversation_id="conversation-1",
            version=4,
            selection_pending=(_pending(),),
        )

    monkeypatch.setattr(
        "app.agent2.turn_runtime._load_verified_conversation_state_version",
        load_information_state,
    )
    monkeypatch.setattr(
        "app.agent2.turn_runtime._load_verified_conversation_state",
        load_selection_state,
    )
    request = _request()

    with pytest.raises(VerifiedTurnRejected, match="pending_context_not_unique"):
        await Agent2TurnRuntime(
            evaluator=lambda **kwargs: pytest.fail("collision reached evaluator"),
            admission_artifact_sink=SimpleNamespace(),
            information_continuation_preprocessor=InformationPreprocessor(),
            selection_continuation_preprocessor=SelectionPreprocessor(),
        ).handle(request)

    assert len(request.session.events) == 1
    event = request.session.events[0]
    assert event.backend_action == "agent2_pending_context_conflict"
    payload = event.llm_decision_json
    assert payload["audit_stage"] == "pending_context_conflict"
    assert payload["selection_status"] == "pending_context_not_unique"
    assert payload["information_pending_id"] == "information-1"
    assert payload["selection_pending_id"] == selection_ready.pending.pending_id
    assert payload["actual_write"] is False
    assert request.envelope.raw_text not in str(payload)


@pytest.mark.asyncio
async def test_repeated_selection_block_reuses_one_audit_record(monkeypatch):
    ready = _ready()
    blocked = SelectionContinuationPreprocessResult(
        handled=True,
        status="clarification_required",
        reason="selection_answer_not_exact",
        pending=ready.pending,
        candidate=None,
        resolution=ready.resolution,
        fresh_admission_request=None,
    )

    class Preprocessor:
        async def preprocess(self, request, **kwargs):
            return blocked

    async def load_state(**kwargs):
        return ConversationState(
            user_id=f"{TENANT}:user-1",
            conversation_id="conversation-1",
            version=4,
            selection_pending=(_pending(),),
        )

    monkeypatch.setattr(
        "app.agent2.turn_runtime._load_verified_conversation_state", load_state
    )

    class Session(_AuditSession):
        async def execute(self, statement):
            existing_id = getattr(self.events[0], "id", None) if self.events else None
            return SimpleNamespace(scalar_one_or_none=lambda: existing_id)

    session = Session()
    request = _request(session=session)
    runtime = Agent2TurnRuntime(
        evaluator=lambda **kwargs: pytest.fail("blocked turn reached evaluator"),
        admission_artifact_sink=SimpleNamespace(),
        selection_continuation_preprocessor=Preprocessor(),
    )

    for _ in range(2):
        with pytest.raises(SelectionContinuationBlocked):
            await runtime.handle(request)

    assert len(session.events) == 1


@pytest.mark.asyncio
async def test_selection_preprocess_audit_failure_is_fail_closed(monkeypatch):
    ready = _ready()
    blocked = SelectionContinuationPreprocessResult(
        handled=True,
        status="clarification_required",
        reason="selection_answer_not_exact",
        pending=ready.pending,
        candidate=None,
        resolution=ready.resolution,
        fresh_admission_request=None,
    )
    base = ConversationState(
        user_id=f"{TENANT}:user-1",
        conversation_id="conversation-1",
        version=4,
        selection_pending=(_pending(),),
    )

    class Preprocessor:
        async def preprocess(self, request, **kwargs):
            return blocked

    async def load_state(**kwargs):
        return base

    monkeypatch.setattr(
        "app.agent2.turn_runtime._load_verified_conversation_state", load_state
    )

    class FailingSession(_AuditSession):
        async def flush(self) -> None:
            raise RuntimeError("audit unavailable")

        @asynccontextmanager
        async def begin_nested(self):
            original_length = len(self.events)
            try:
                yield
            except Exception:
                del self.events[original_length:]
                raise

    evaluator_calls = 0

    async def evaluate(**kwargs):
        nonlocal evaluator_calls
        evaluator_calls += 1

    session = FailingSession()
    with pytest.raises(
        VerifiedTurnRejected, match="selection_preprocess_audit_failed"
    ):
        await Agent2TurnRuntime(
            evaluator=evaluate,
            admission_artifact_sink=SimpleNamespace(),
            selection_continuation_preprocessor=Preprocessor(),
        ).handle(_request(session=session))

    assert evaluator_calls == 0
    assert session.events == []
    assert base.version == 4
    assert base.selection_pending[0].status == "active"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "pending_status_after"),
    (("invalidated", "invalidated"), ("permission_revoked", "invalidated")),
)
async def test_terminal_selection_block_uses_only_atomic_terminal_audit(
    monkeypatch, status, pending_status_after,
):
    pending = _pending()
    base = ConversationState(
        user_id=f"{TENANT}:user-1",
        conversation_id="conversation-1",
        version=4,
        selection_pending=(pending,),
    )
    pending_after = replace(
        pending,
        status=pending_status_after,
        invalidation_reason=status,
    )
    resolution = replace(
        _ready().resolution,
        status=status,
        reason=status,
        pending_after=pending_after,
    )
    blocked = SelectionContinuationPreprocessResult(
        handled=True,
        status=status,
        reason=status,
        pending=pending,
        candidate=None,
        resolution=resolution,
        fresh_admission_request=None,
    )

    class Preprocessor:
        async def preprocess(self, request, **kwargs):
            return blocked

    store = InMemoryConversationStateStore((base,))
    monkeypatch.setattr(
        "app.agent2.conversation_state_store.SQLAlchemyConversationStateStore",
        lambda session: store,
    )
    session = _AuditSession()

    with pytest.raises(SelectionContinuationBlocked):
        await Agent2TurnRuntime(
            evaluator=lambda **kwargs: pytest.fail("terminal turn reached evaluator"),
            admission_artifact_sink=SimpleNamespace(),
            selection_continuation_preprocessor=Preprocessor(),
        ).handle(_request(session=session))

    saved = await store.load(
        user_id=base.user_id,
        conversation_id=base.conversation_id,
    )
    assert saved.version == 5
    assert saved.selection_pending[0].status == pending_status_after
    assert len(session.events) == 1
    assert (
        session.events[0].backend_action
        == "agent2_selection_pending_terminal_resolution"
    )


@pytest.mark.asyncio
async def test_fresh_selection_ticket_must_bind_the_selected_candidate(monkeypatch):
    ready = _ready()

    class Preprocessor:
        async def preprocess(self, request, **kwargs):
            return ready

    async def load_state(**kwargs):
        return ConversationState(
            user_id=f"{TENANT}:user-1",
            conversation_id="conversation-1",
            version=4,
            selection_pending=(_pending(),),
        )

    monkeypatch.setattr(
        "app.agent2.turn_runtime._load_verified_conversation_state", load_state
    )

    async def evaluate(**kwargs):
        ticket = _fresh_ticket(ready)
        ticket.authority_scope["candidate_stable_id"] = CASE_1
        return SimpleNamespace(
            base_state=SimpleNamespace(version=4),
            state_persisted=False,
            decision=SimpleNamespace(
                source_text_hash=hashlib.sha256("第二个".encode("utf-8")).hexdigest(),
                admission_mode="enforced",
                admission_tickets=(ticket,),
                admission_information_pendings=(),
                admission_trace=SimpleNamespace(
                    trace_id="fresh-selection-trace",
                    decisions=(SimpleNamespace(status="admitted"),),
                ),
            ),
        )

    with pytest.raises(
        VerifiedTurnRejected, match="selection_continuation_ticket_claims_mismatch"
    ):
        await Agent2TurnRuntime(
            evaluator=evaluate,
            admission_artifact_sink=SimpleNamespace(),
            selection_continuation_preprocessor=Preprocessor(),
        ).handle(_request())


def test_production_runtime_factory_wires_selection_preprocessor():
    runtime = production_agent2_turn_runtime()

    assert (
        runtime._selection_continuation_preprocessor.__class__.__name__
        == "SqlSelectionPendingContinuationAdapter"
    )


@pytest.mark.asyncio
async def test_receipt_backed_selection_pending_can_advance_state_once():
    pending = _pending(expected_version=4)
    base = ConversationState.empty(
        user_id=f"{TENANT}:user-1", conversation_id="conversation-1"
    )
    base = replace(base, version=3)
    proposed = replace(base, version=4)
    decision = SimpleNamespace(
        admission_mode="enforced",
        admission_information_pendings=(),
        admission_trace=SimpleNamespace(
            decisions=(SimpleNamespace(status="blocked"),)
        ),
        context_update=SimpleNamespace(consumed_pending_ids=()),
    )
    result = CognitiveOrchestrationResult(
        decision=decision,
        base_state=base,
        state=proposed,
        command_plan=CognitiveCommandPlan(
            decision_id=UUID(str(uuid5(NAMESPACE_URL, "selection-plan"))),
            daily_commands=(),
            business_commands=(),
            report_commands=(),
            blocked_actions=(),
        ),
        state_persisted=False,
    )
    store = InMemoryConversationStateStore((base,))

    saved = await finalize_cognitive_state_after_execution(
        result=result,
        state_store=store,
        execution_succeeded=False,
        selection_pending=(pending,),
    )

    assert saved.version == 4
    assert saved.selection_pending == (pending,)


@pytest.mark.asyncio
async def test_failed_sibling_action_persists_only_selection_on_base_state():
    pending = _pending(expected_version=4)
    base = replace(
        ConversationState.empty(
            user_id=f"{TENANT}:user-1",
            conversation_id="conversation-1",
        ),
        version=3,
        current_goal=ConversationGoal(intent="weekly_report"),
    )
    proposed = replace(
        base,
        version=4,
        current_goal=ConversationGoal(intent="case_progress"),
        goal_stack=(ConversationGoal(intent="daily_report"),),
        user_constraints=UserConstraints(read_only=True, sources=("uncommitted",)),
    )
    decision = SimpleNamespace(
        admission_mode="enforced",
        admission_information_pendings=(),
        admission_trace=SimpleNamespace(
            decisions=(SimpleNamespace(status="admitted"),)
        ),
        context_update=SimpleNamespace(consumed_pending_ids=("must-not-consume",)),
    )
    result = CognitiveOrchestrationResult(
        decision=decision,
        base_state=base,
        state=proposed,
        command_plan=CognitiveCommandPlan(
            decision_id=UUID(str(uuid5(NAMESPACE_URL, "selection-partial-plan"))),
            daily_commands=(SimpleNamespace(command_type="append_item"),),
            business_commands=(),
            report_commands=(),
            blocked_actions=(),
        ),
        state_persisted=False,
    )
    store = InMemoryConversationStateStore((base,))

    saved = await finalize_cognitive_state_after_execution(
        result=result,
        state_store=store,
        execution_succeeded=False,
        selection_pending=(pending,),
    )

    assert saved.version == 4
    assert saved.selection_pending == (pending,)
    assert saved.current_goal == base.current_goal
    assert saved.goal_stack == base.goal_stack
    assert saved.user_constraints == base.user_constraints


@pytest.mark.asyncio
async def test_initial_enforced_ambiguous_case_request_passes_runtime_scope_fence():
    text = "云璟府今天联系法院推进，法院表示下周重新查控。"
    base = ConversationState.empty(
        user_id=f"{TENANT}:user-1",
        conversation_id="conversation-initial-selection",
    )
    cases = (
        {
            "case_id": CASE_1,
            "case_name": "云璟府物业服务合同执行案",
            "case_number": "（2026）苏01执100号",
            "confirmed_aliases": ["云璟府"],
            "version": 7,
        },
        {
            "case_id": CASE_2,
            "case_name": "云璟府建设工程争议案",
            "case_number": "（2026）苏01民初200号",
            "confirmed_aliases": ["云璟府"],
            "version": 11,
        },
    )
    proposal = SemanticInterpretation.from_payload(
        {
            "intents": ["case_progress"],
            "segments": [
                {
                    "segment_id": "ambiguous-case-segment",
                    "text": text,
                    "intents": ["case_progress"],
                    "entity_ids": ["case-ref"],
                    "action_ids": ["record-case-progress"],
                }
            ],
            "entities": [
                {
                    "entity_id": "case-ref",
                    "entity_type": "case_ref",
                    "value": "云璟府",
                    "confidence": 0.99,
                    "attributes": {
                        "statement_mode": "asserted",
                        "action_time_scope": "today",
                        "factual_progress": ["联系法院推进"],
                        "current_status": "法院表示下周重新查控",
                        "normalized_fact": text,
                        "evidence_spans": [[0, len(text)]],
                    },
                }
            ],
            "confidence": 0.99,
            "required_actions": [
                {
                    "action_id": "record-case-progress",
                    "action_type": "record_case_progress",
                    "intent": "case_progress",
                    "entity_ids": ["case-ref"],
                }
            ],
            "clarification_need": None,
            "context_update": {"preserve_current_goal": True},
        }
    )

    class Interpreter:
        async def interpret(self, turn, state):
            return proposal

    async def evaluator(**kwargs):
        envelope = kwargs["envelope"]
        context = kwargs["business_context"]
        turn = CognitiveTurn(
            user_id=f"{context.tenant_id}:{context.actor_user_id}",
            actor_user_id=context.actor_user_id,
            tenant_id=context.tenant_id,
            conversation_id=envelope.conversation_id,
            message_id=envelope.message_id,
            text=envelope.raw_text,
            occurred_at=envelope.received_at,
            resources={"visible_cases": list(cases)},
        )
        core = await CognitiveCoreV3(
            Interpreter(),
            admission_engine=DomainAdmissionEngine(),
            admission_enforced=True,
        ).process(turn, base)
        return CognitiveOrchestrationResult(
            decision=core.decision,
            base_state=base,
            state=core.state,
            command_plan=CognitiveCommandPlan(
                decision_id=UUID(core.decision.decision_id),
            ),
            state_persisted=False,
        )

    persisted: list[object] = []

    class Sink:
        async def persist(self, request):
            persisted.append(request)

    envelope = IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="Test User",
        dingtalk_user_id="ding-user-1",
        source="manual_text",
        raw_text=text,
        message_id="message-initial-selection",
        conversation_id="conversation-initial-selection",
        received_at=NOW,
    )
    context = BusinessCommandContext(
        tenant_id=TENANT,
        company_id="company-1",
        department_id="legal",
        team_id="litigation",
        actor_user_id="user-1",
        actor_role_ids=("lawyer",),
        allowed_case_ids=(CASE_1, CASE_2),
        source_message_id=envelope.message_id,
        source_channel="manual_text",
        occurred_at=NOW,
        conversation_id=envelope.conversation_id,
    )
    result = await Agent2TurnRuntime(
        evaluator=evaluator,
        admission_artifact_sink=Sink(),
    ).handle(
        VerifiedTurnRequest(
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
            business_context=context,
        )
    )

    requests = result.orchestration.decision.admission_selection_requests
    assert len(requests) == 1
    assert requests[0].tenant_id == TENANT
    assert requests[0].expected_conversation_state_version == 1
    assert {item.stable_id for item in requests[0].candidates} == {CASE_1, CASE_2}
    assert result.admission_tickets == ()
    assert len(persisted) == 1
