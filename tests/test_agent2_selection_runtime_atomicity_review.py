from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from app.agent2.business.composition import (
    BusinessActionResult,
    BusinessCompositionResult,
)
from app.agent2.business.contracts import BusinessCommandContext, BusinessReceipt
from app.agent2.cognitive_orchestrator_v3 import CognitiveOrchestrationResult
from app.agent2.cognitive_runtime_v3 import finalize_cognitive_core_v3_execution
from app.agent2.command_planner_v3 import CognitiveCommandPlan
from app.agent2.cognitive_core_v3 import CognitiveCoreV3, CognitiveTurn
from app.agent2.conversation_state import ConversationState
from app.agent2.conversation_state_store import (
    ConversationStateVersionConflict,
    InMemoryConversationStateStore,
)
from app.agent2.selection_pending import (
    SelectionPendingFactory,
    SelectionValidation,
)
from app.agent2.selection_pending_admission import (
    SelectionContinuationAdmissionEngine,
    SelectionContinuationSemanticInterpreter,
    bind_fresh_selected_business_command,
)
from app.agent2.selection_pending_runtime import (
    SelectionContinuationCoordinator,
    SelectionContinuationPreprocessRequest,
)
from app.agent2.selection_pending_sql import SqlSelectionPendingContinuationAdapter
from app.agent2.turn_runtime import (
    Agent2TurnRuntime,
    SelectionContinuationBlocked,
    VerifiedTurnRejected,
    VerifiedTurnRequest,
)
from app.workflows.intake import IncomingMessageEnvelope
from tests.test_agent2_selection_runtime_shadow_review import (
    TENANT,
    _selection_request,
)


NOW = datetime(2026, 7, 14, 2, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_selection_pending_cannot_consume_an_unrelated_success_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Settlement must bind the receipt to the freshly admitted planned command."""

    pending = SelectionPendingFactory().from_trusted_request(
        _selection_request(expected_conversation_state_version=4)
    )
    base = ConversationState(
        user_id=f"{TENANT}:user-1",
        conversation_id="conversation-1",
        version=4,
        selection_pending=(pending,),
    )
    proposed = ConversationState(
        user_id=base.user_id,
        conversation_id=base.conversation_id,
        version=5,
        selection_pending=(pending,),
    )
    store = InMemoryConversationStateStore((base,))
    monkeypatch.setattr(
        "app.agent2.cognitive_runtime_v3.SQLAlchemyConversationStateStore",
        lambda session: store,
    )
    expected_command_id = str(
        uuid5(NAMESPACE_URL, "selection-atomicity-expected-command")
    )
    result = CognitiveOrchestrationResult(
        decision=SimpleNamespace(
            admission_mode="enforced",
            admission_selection_requests=(),
            admission_information_pendings=(),
            admission_trace=SimpleNamespace(decisions=()),
            context_update=SimpleNamespace(consumed_pending_ids=()),
        ),
        base_state=base,
        state=proposed,
        command_plan=CognitiveCommandPlan(
            decision_id=UUID(
                str(uuid5(NAMESPACE_URL, "selection-atomicity-plan"))
            ),
            business_commands=(
                SimpleNamespace(command_id=expected_command_id),  # type: ignore[arg-type]
            ),
        ),
        state_persisted=False,
    )
    unrelated_command_id = str(
        uuid5(NAMESPACE_URL, "selection-atomicity-unrelated-command")
    )
    unrelated_receipt = BusinessReceipt(
        receipt_id="receipt-unrelated",
        command_id=unrelated_command_id,
        command_type="create_case_progress",
        tenant_id=TENANT,
        actor_user_id="user-1",
        source_message_id="message-answer",
        idempotency_key="unrelated-command-key",
        status="executed",
        resource_type="case_progress",
        resource_id="progress-unrelated",
        before={},
        after={
            "case_id": "case-1",
            "case_name": "无关案件",
            "summary": "无关进展",
            "version": 1,
        },
        error_code=None,
        failed_stage=None,
        actual_write=True,
        created_at=NOW,
    )
    business_result = BusinessCompositionResult(
        source_message_id="message-answer",
        actions=(
            BusinessActionResult(
                semantic_command_id=unrelated_command_id,
                semantic_command_type="record_case_progress_candidate",
                compiled_command_type="create_case_progress",
                receipt=unrelated_receipt,
                outcome_context={"case_name": "无关案件"},
            ),
        ),
    )
    selection_continuation = SimpleNamespace(
        pending=pending,
        resolution=SimpleNamespace(
            pending_id=pending.pending_id,
            status="selected",
            selected_candidate_id="case-2",
        ),
    )
    context = BusinessCommandContext(
        tenant_id=TENANT,
        company_id="company-1",
        department_id="legal",
        team_id="litigation",
        actor_user_id="user-1",
        actor_role_ids=("lawyer",),
        allowed_case_ids=("case-1", "case-2"),
        source_message_id="message-answer",
        source_channel="manual_text",
        occurred_at=NOW,
        conversation_id="conversation-1",
        execution_started_at=NOW + timedelta(seconds=1),
        conversation_state_version=4,
    )

    with pytest.raises(ValueError):
        await finalize_cognitive_core_v3_execution(
            session=object(),
            result=result,
            command_results=[],
            business_result=business_result,
            report_results=[],
            business_context=context,
            selection_continuation=selection_continuation,
        )


@pytest.mark.asyncio
async def test_expired_selection_pending_is_cas_invalidated_before_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocked continuation must not leave the same active Pending behind."""

    pending = SelectionPendingFactory().from_trusted_request(
        _selection_request(
            expected_conversation_state_version=4,
            created_at=NOW - timedelta(minutes=20),
            expires_at=NOW - timedelta(minutes=10),
        )
    )
    base = ConversationState(
        user_id=f"{TENANT}:user-1",
        conversation_id="conversation-1",
        version=4,
        selection_pending=(pending,),
    )
    store = InMemoryConversationStateStore((base,))
    monkeypatch.setattr(
        "app.agent2.conversation_state_store.SQLAlchemyConversationStateStore",
        lambda session: store,
    )

    class QueryResult:
        def scalar_one_or_none(self) -> None:
            return None

    class Session:
        def __init__(self) -> None:
            self.audit_events: list[object] = []

        async def execute(self, statement: object) -> QueryResult:
            return QueryResult()

        def add(self, event: object) -> None:
            self.audit_events.append(event)

        async def flush(self) -> None:
            return None

        @asynccontextmanager
        async def begin_nested(self):
            yield

    async def evaluator(**kwargs: object) -> SimpleNamespace:
        raise AssertionError("expired selection must stop before semantic evaluation")

    envelope = IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="测试用户",
        dingtalk_user_id="ding-user-1",
        source="manual_text",
        raw_text="第二个",
        message_id="message-expired-answer",
        conversation_id="conversation-1",
        received_at=NOW,
    )
    context = BusinessCommandContext(
        tenant_id=TENANT,
        company_id="company-1",
        department_id="legal",
        team_id="litigation",
        actor_user_id="user-1",
        actor_role_ids=("lawyer",),
        allowed_case_ids=("case-1", "case-2"),
        source_message_id=envelope.message_id,
        source_channel="manual_text",
        occurred_at=NOW,
        conversation_id="conversation-1",
    )
    settings = SimpleNamespace(
        agent2_semantic_admission_enabled=True,
        agent2_semantic_admission_enforce=True,
        agent2_semantic_admission_tenant_allowlist=TENANT,
        agent2_semantic_admission_user_allowlist="user-1",
        timezone="Asia/Shanghai",
    )

    session = Session()
    with pytest.raises(SelectionContinuationBlocked):
        await Agent2TurnRuntime(
            evaluator=evaluator,
            selection_continuation_preprocessor=(
                SqlSelectionPendingContinuationAdapter()
            ),
        ).handle(
            VerifiedTurnRequest(
                session=session,
                user=SimpleNamespace(id="user-1", timezone="Asia/Shanghai"),
                envelope=envelope,
                llm_client=object(),
                daily_report=None,
                report_date=NOW.date(),
                settings=settings,
                business_context=context,
            )
        )

    persisted = await store.load(
        user_id=base.user_id,
        conversation_id=base.conversation_id,
    )
    assert persisted.version == 5
    assert persisted.selection_pending[0].status == "expired"
    assert persisted.selection_pending[0].invalidation_reason == "expired"
    assert len(session.audit_events) == 1
    assert (
        session.audit_events[0].backend_action
        == "agent2_selection_pending_terminal_resolution"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "source_channel"),
    (
        ("manual_text", "manual_text"),
        ("agent2_dingtalk_webhook_text", "dingtalk_webhook"),
        ("agent2_dingtalk_stream_text", "dingtalk_stream"),
    ),
)
async def test_terminal_selection_cas_conflict_rolls_back_audit_savepoint(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    source_channel: str,
) -> None:
    """Entrypoints may commit their safe reply, but never an orphan terminal audit."""

    pending = SelectionPendingFactory().from_trusted_request(
        _selection_request(
            expected_conversation_state_version=4,
            created_at=NOW - timedelta(minutes=20),
            expires_at=NOW - timedelta(minutes=10),
        )
    )
    base = ConversationState(
        user_id=f"{TENANT}:user-1",
        conversation_id="conversation-1",
        version=4,
        selection_pending=(pending,),
    )

    class ConflictingStore:
        def __init__(self, session: object) -> None:
            self.session = session

        async def load(self, *, user_id: str, conversation_id: str):
            return base

        async def save(self, state, *, expected_version: int):
            raise ConversationStateVersionConflict("concurrent state update")

    monkeypatch.setattr(
        "app.agent2.conversation_state_store.SQLAlchemyConversationStateStore",
        ConflictingStore,
    )

    class QueryResult:
        def scalar_one_or_none(self) -> None:
            return None

    class Session:
        def __init__(self) -> None:
            self.audit_events: list[object] = []
            self.committed_audit_events: list[object] = []
            self.savepoints_started = 0

        async def execute(self, statement: object) -> QueryResult:
            return QueryResult()

        def add(self, event: object) -> None:
            self.audit_events.append(event)

        async def flush(self) -> None:
            return None

        async def commit(self) -> None:
            self.committed_audit_events.extend(self.audit_events)

        @asynccontextmanager
        async def begin_nested(self):
            self.savepoints_started += 1
            original_length = len(self.audit_events)
            try:
                yield
            except Exception:
                del self.audit_events[original_length:]
                raise

    async def evaluator(**kwargs: object) -> SimpleNamespace:
        raise AssertionError("terminal conflict must stop before semantic evaluation")

    envelope = IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="测试用户",
        dingtalk_user_id="ding-user-1",
        source=source,
        raw_text="第二个",
        message_id="message-terminal-conflict",
        conversation_id="conversation-1",
        received_at=NOW,
    )
    context = BusinessCommandContext(
        tenant_id=TENANT,
        company_id="company-1",
        department_id="legal",
        team_id="litigation",
        actor_user_id="user-1",
        actor_role_ids=("lawyer",),
        allowed_case_ids=("case-1", "case-2"),
        source_message_id=envelope.message_id,
        source_channel=source_channel,
        occurred_at=NOW,
        conversation_id="conversation-1",
    )
    settings = SimpleNamespace(
        agent2_semantic_admission_enabled=True,
        agent2_semantic_admission_enforce=True,
        agent2_semantic_admission_tenant_allowlist=TENANT,
        agent2_semantic_admission_user_allowlist="user-1",
        timezone="Asia/Shanghai",
    )
    session = Session()

    with pytest.raises(
        VerifiedTurnRejected,
        match="selection_terminal_state_cas_conflict",
    ):
        await Agent2TurnRuntime(
            evaluator=evaluator,
            selection_continuation_preprocessor=(
                SqlSelectionPendingContinuationAdapter()
            ),
        ).handle(
            VerifiedTurnRequest(
                session=session,
                user=SimpleNamespace(id="user-1", timezone="Asia/Shanghai"),
                envelope=envelope,
                llm_client=object(),
                daily_report=None,
                report_date=NOW.date(),
                settings=settings,
                business_context=context,
            )
        )

    # Webhook and Stream explicitly commit their safe blocked response; Manual
    # is committed by its outer request transaction.  All three must see the
    # audit-free transaction produced by the shared runtime.
    await session.commit()
    assert session.savepoints_started == 1
    assert session.audit_events == []
    assert session.committed_audit_events == []


@pytest.mark.asyncio
async def test_enforced_selection_happy_path_consumes_exact_receipt_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ordinal answer selects an object; the protected fact remains the write."""

    pending = SelectionPendingFactory().from_trusted_request(
        _selection_request(expected_conversation_state_version=4)
    )
    base = ConversationState(
        user_id=f"{TENANT}:user-1",
        conversation_id="conversation-1",
        version=4,
        selection_pending=(pending,),
    )

    class Validator:
        async def validate(self, pending, candidate, context) -> SelectionValidation:
            return SelectionValidation.valid()

    class Ledger:
        async def source_message_processed(self, context) -> bool:
            return False

    ready = await SelectionContinuationCoordinator().preprocess(
        SelectionContinuationPreprocessRequest(
            tenant_id=TENANT,
            user_id="user-1",
            conversation_id="conversation-1",
            conversation_state_version=4,
            source_message_id="message-answer",
            source_text="第二个",
            occurred_at=NOW,
            pendings=(pending,),
        ),
        validator=Validator(),
        source_ledger=Ledger(),
    )
    turn = CognitiveTurn(
        user_id=base.user_id,
        actor_user_id="user-1",
        tenant_id=TENANT,
        conversation_id="conversation-1",
        message_id="message-answer",
        text="第二个",
        occurred_at=NOW,
        resources={},
    )
    core_result = await CognitiveCoreV3(
        SelectionContinuationSemanticInterpreter(ready),
        admission_engine=SelectionContinuationAdmissionEngine(ready),
        admission_enforced=True,
    ).process(turn, base)
    ticket = core_result.decision.admission_tickets[0]
    bound = bind_fresh_selected_business_command(ready, ticket)
    result = CognitiveOrchestrationResult(
        decision=core_result.decision,
        base_state=base,
        state=core_result.state,
        command_plan=CognitiveCommandPlan(
            decision_id=UUID(core_result.decision.decision_id),
            business_commands=(bound,),
        ),
        state_persisted=False,
    )
    receipt = BusinessReceipt(
        receipt_id="receipt-selected-case-2",
        command_id=str(bound.command_id),
        command_type="create_case_progress",
        tenant_id=TENANT,
        actor_user_id="user-1",
        source_message_id="message-answer",
        idempotency_key=bound.idempotency_key,
        status="executed",
        resource_type="case_progress",
        resource_id="progress-selected-case-2",
        before={},
        after={
            "case_id": "case-2",
            "case_name": "恒大执行案二",
            "summary": (
                bound.payload["source_segments"][0]["text"]
            ),
            "version": 1,
        },
        error_code=None,
        failed_stage=None,
        actual_write=True,
        created_at=NOW,
    )
    business_result = BusinessCompositionResult(
        source_message_id="message-answer",
        actions=(
            BusinessActionResult(
                semantic_command_id=str(bound.command_id),
                semantic_command_type=bound.command_type,
                compiled_command_type="create_case_progress",
                receipt=receipt,
                outcome_context={"case_name": "恒大执行案二"},
            ),
        ),
    )
    context = BusinessCommandContext(
        tenant_id=TENANT,
        company_id="company-1",
        department_id="legal",
        team_id="litigation",
        actor_user_id="user-1",
        actor_role_ids=("lawyer",),
        allowed_case_ids=("case-1", "case-2"),
        source_message_id="message-answer",
        source_channel="manual_text",
        occurred_at=NOW,
        conversation_id="conversation-1",
        execution_started_at=NOW + timedelta(seconds=1),
        conversation_state_version=4,
    )
    store = InMemoryConversationStateStore((base,))
    monkeypatch.setattr(
        "app.agent2.cognitive_runtime_v3.SQLAlchemyConversationStateStore",
        lambda session: store,
    )
    settlement_audits: list[object] = []

    async def persist_audit(*, session, audit, outcome, business_context):
        settlement_audits.append(audit)
        assert audit.receipt_ids == ("receipt-selected-case-2",)
        assert outcome.receipt_refs[0].receipt_id == "receipt-selected-case-2"
        assert business_context is context
        return "selection-audit-1"

    monkeypatch.setattr(
        "app.agent2.cognitive_runtime_v3.persist_selection_settlement_audit",
        persist_audit,
    )

    saved = await finalize_cognitive_core_v3_execution(
        session=object(),
        result=result,
        command_results=[],
        business_result=business_result,
        report_results=[],
        business_context=context,
        selection_continuation=ready,
    )

    consumed = saved.selection_pending[0]
    assert saved.version == 5
    assert consumed.status == "consumed"
    assert consumed.consumed_receipt_id == "receipt-selected-case-2"
    assert len(settlement_audits) == 1
    assert bound.payload["entities"][0]["value"] == "case-2"
    assert bound.payload["source_segments"][0]["text"] != "第二个"
