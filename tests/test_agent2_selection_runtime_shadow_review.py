from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from app.agent2.admission_contracts import (
    TrustedSelectionCandidateRef,
    TrustedSelectionRequest,
)
from app.agent2.cognitive_runtime_v3 import (
    admission_block_reply,
    finalize_cognitive_core_v3_execution,
    information_pending_reply,
    selection_request_reply,
)
from app.agent2.cognitive_orchestrator_v3 import CognitiveOrchestrationResult
from app.agent2.business.contracts import BusinessCommandContext
from app.agent2.command_planner_v3 import CognitiveCommandPlan
from app.agent2.conversation_state import ConversationState
from app.agent2.conversation_state_store import InMemoryConversationStateStore
from app.agent2.selection_pending import protect_selection_continuation_payload
from app.agent2.turn_runtime import (
    Agent2TurnRuntime,
    VerifiedTurnRejected,
    VerifiedTurnRequest,
)
from app.workflows.intake import IncomingMessageEnvelope


NOW = datetime(2026, 7, 14, 2, 0, tzinfo=UTC)
TENANT = "sandbox-agent2-phase2-20260711"
FACT = "恒大案件今天联系法院，法院表示下周重新查控。"


def _selection_request(**overrides: object) -> TrustedSelectionRequest:
    fact_hash = hashlib.sha256(FACT.encode("utf-8")).hexdigest()
    request_id = str(uuid5(NAMESPACE_URL, "selection-shadow-review-request"))
    values: dict[str, object] = {
        "selection_request_id": request_id,
        "trace_id": str(uuid5(NAMESPACE_URL, "selection-shadow-review-trace")),
        "decision_id": str(uuid5(NAMESPACE_URL, "selection-shadow-review-decision")),
        "tenant_id": TENANT,
        "user_id": "user-1",
        "conversation_id": "conversation-1",
        "source_turn_id": "message-original",
        "source_message_id": "message-original",
        "action_id": "record-case-progress",
        "segment_id": "segment-original",
        "segment_text_sha256": fact_hash,
        "segment_start_offset": 0,
        "segment_end_offset": len(FACT),
        "domain": "case",
        "operation": "record_case_progress",
        "expected_conversation_state_version": 4,
        "candidates": (
            TrustedSelectionCandidateRef("case-1", 3, "恒大执行案一"),
            TrustedSelectionCandidateRef("case-2", 5, "恒大执行案二"),
        ),
        "acceptable_answer_forms": {
            "第一个": "case-1",
            "第二个": "case-2",
        },
        "continuation_payload": protect_selection_continuation_payload(
            {
                "typed_business_command": {
                    "command_id": str(
                        uuid5(NAMESPACE_URL, "selection-shadow-review-command")
                    ),
                    "decision_id": str(
                        uuid5(NAMESPACE_URL, "selection-shadow-review-decision")
                    ),
                    "sub_decision_id": str(
                        uuid5(NAMESPACE_URL, "selection-shadow-review-sub-decision")
                    ),
                    "command_type": "record_case_progress_candidate",
                    "target_system": "case_progress",
                    "entity_ids": ["case-ref"],
                    "payload": {
                        "entities": [
                            {
                                "entity_id": "case-ref",
                                "entity_type": "case_ref",
                                "value": "恒大案件",
                                "confidence": 0.99,
                                "attributes": {},
                            }
                        ],
                        "parameters": {},
                        "source_segments": [
                            {
                                "segment_id": "segment-original",
                                "text": FACT,
                                "text_hash": fact_hash,
                                "start_offset": 0,
                                "end_offset": len(FACT),
                            }
                        ],
                    },
                    "execution_mode": "candidate",
                    "idempotency_key": "selection-shadow-review-command-key",
                },
                "bind": {"entity_type": "case_ref", "attribute": "case_id"},
            }
        ),
        "created_at": NOW,
        "expires_at": NOW + timedelta(minutes=10),
        "idempotency_key": "selection-shadow-review-request-key",
    }
    values.update(overrides)
    return TrustedSelectionRequest(**values)  # type: ignore[arg-type]


def test_shadow_selection_request_is_not_rendered_to_the_user() -> None:
    """Shadow Admission must remain audit-only and user-invisible."""

    decision = SimpleNamespace(
        admission_mode="shadow",
        admission_selection_requests=(_selection_request(),),
    )

    assert selection_request_reply(decision) == ""


def test_travel_information_pending_asks_only_for_missing_date() -> None:
    decision = SimpleNamespace(
        admission_mode="enforced",
        admission_information_pendings=(
            SimpleNamespace(
                question_snapshot={
                    "question_key": "travel_date_required",
                    "destination": "常州",
                }
            ),
        ),
    )

    assert information_pending_reply(decision) == (
        "去常州的出差我已经识别到了，还差出发日期。哪天去？本次还没有登记。"
    )


def test_unknown_case_reference_gets_plain_language_correction() -> None:
    decision = SimpleNamespace(
        admission_mode="enforced",
        admission_trace=SimpleNamespace(
            decisions=(
                SimpleNamespace(
                    status="blocked",
                    reason_code="case_reference_not_uniquely_authorized",
                ),
            )
        ),
    )

    assert admission_block_reply(decision) == (
        "我没在你当前分配的案件中找到这个案件编号或名称。"
        "请核对后再发一次；本次没有登记。"
    )


@pytest.mark.asyncio
async def test_shadow_selection_request_never_enters_production_conversation_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Shadow-only Selection artifact cannot create an executable Pending."""

    base = ConversationState(
        user_id=f"{TENANT}:user-1",
        conversation_id="conversation-1",
        version=3,
    )
    proposed = ConversationState(
        user_id=base.user_id,
        conversation_id=base.conversation_id,
        version=4,
    )
    store = InMemoryConversationStateStore((base,))
    monkeypatch.setattr(
        "app.agent2.cognitive_runtime_v3.SQLAlchemyConversationStateStore",
        lambda session: store,
    )
    decision = SimpleNamespace(
        admission_mode="shadow",
        admission_selection_requests=(_selection_request(),),
        admission_information_pendings=(),
        admission_trace=SimpleNamespace(decisions=()),
        context_update=SimpleNamespace(consumed_pending_ids=()),
    )
    result = CognitiveOrchestrationResult(
        decision=decision,
        base_state=base,
        state=proposed,
        command_plan=CognitiveCommandPlan(
            decision_id=UUID(
                str(uuid5(NAMESPACE_URL, "selection-shadow-review-plan"))
            )
        ),
        state_persisted=False,
    )

    await finalize_cognitive_core_v3_execution(
        session=object(),
        result=result,
        command_results=[],
        business_result=None,
        report_results=[],
        business_context=None,
    )

    persisted = await store.load(
        user_id=base.user_id,
        conversation_id=base.conversation_id,
    )
    assert persisted.selection_pending == ()


@pytest.mark.asyncio
async def test_enforced_runtime_rejects_cross_tenant_selection_request() -> None:
    """Evaluator output cannot smuggle a foreign Selection Pending into state/reply."""

    text = FACT
    source_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

    async def evaluator(**kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            base_state=SimpleNamespace(version=3),
            state_persisted=False,
            decision=SimpleNamespace(
                source_text_hash=source_hash,
                admission_mode="enforced",
                admission_tickets=(),
                admission_information_pendings=(),
                admission_selection_requests=(
                    _selection_request(tenant_id="foreign-tenant"),
                ),
                admission_trace=SimpleNamespace(
                    trace_id="trace-current-turn",
                    decisions=(SimpleNamespace(status="blocked"),),
                ),
            ),
        )

    class Sink:
        async def persist(self, request: object) -> None:
            return None

    envelope = IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="测试用户",
        dingtalk_user_id="ding-user-1",
        source="manual_text",
        raw_text=text,
        message_id="message-original",
        conversation_id="conversation-1",
        received_at=NOW,
    )
    business_context = BusinessCommandContext(
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
        conversation_id=envelope.conversation_id,
    )
    settings = SimpleNamespace(
        agent2_semantic_admission_enabled=True,
        agent2_semantic_admission_enforce=True,
        agent2_semantic_admission_tenant_allowlist=TENANT,
        agent2_semantic_admission_user_allowlist="user-1",
        timezone="Asia/Shanghai",
    )

    with pytest.raises(VerifiedTurnRejected):
        await Agent2TurnRuntime(
            evaluator=evaluator,
            admission_artifact_sink=Sink(),
        ).handle(
            VerifiedTurnRequest(
                session=object(),
                user=SimpleNamespace(id="user-1", timezone="Asia/Shanghai"),
                envelope=envelope,
                llm_client=object(),
                daily_report=None,
                report_date=NOW.date(),
                settings=settings,
                business_context=business_context,
            )
        )
