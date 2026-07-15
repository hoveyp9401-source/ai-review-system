from __future__ import annotations

from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

import pytest

from app.agent2.business.case_progress import CaseRecord
from app.agent2.business.composition import Phase2BusinessComposer
from app.agent2.business.contracts import BusinessCommandContext
from app.agent2.business.executor import InMemoryBusinessExecutor
from app.agent2.cognitive_core_v3 import (
    CognitiveCoreV3,
    CognitiveTurn,
    SemanticInterpretation,
)
from app.agent2.command_planner_v3 import CognitiveCommandPlanner, CommandPlanningContext
from app.agent2.conversation_state import ConversationState
from app.agent2.typed_daily_commands import (
    DailyReportMutationSnapshot,
    execute_typed_daily_command,
)


NOW = datetime(2026, 7, 11, 9, 0, tzinfo=timezone.utc)


class _OneTurnInterpreter:
    async def interpret(self, turn, state):
        assert turn.text == "今天联系南京中院推进华东公司执行案，明天去南京，顺便记到日报。"
        return SemanticInterpretation.from_payload(
            {
                "intents": ["case_progress", "travel_event", "daily_append"],
                "segments": [
                    {
                        "segment_id": "work-segment",
                        "text": "今天联系南京中院推进华东公司执行案",
                        "intents": ["case_progress", "daily_append"],
                        "entity_ids": ["case-entity", "daily-entity"],
                        "action_ids": ["record-progress", "capture-daily"],
                    },
                    {
                        "segment_id": "travel-segment",
                        "text": "明天去南京",
                        "intents": ["travel_event"],
                        "entity_ids": ["travel-entity"],
                        "action_ids": ["record-travel"],
                    },
                ],
                "entities": [
                    {
                        "entity_id": "case-entity",
                        "entity_type": "case_ref",
                        "value": "华东公司执行案",
                        "confidence": 0.99,
                        "attributes": {"stage": "execution"},
                    },
                    {
                        "entity_id": "travel-entity",
                        "entity_type": "travel_event",
                        "value": "明天去南京",
                        "confidence": 0.99,
                        "attributes": {
                            "destination": "南京",
                            "date_hint": "tomorrow",
                            "purpose": "出差",
                        },
                    },
                    {
                        "entity_id": "daily-entity",
                        "entity_type": "daily_event",
                        "value": "联系南京中院推进华东公司执行案",
                        "confidence": 0.99,
                        "attributes": {"field": "today_work"},
                    },
                ],
                "confidence": 0.99,
                "required_actions": [
                    {
                        "action_id": "record-progress",
                        "action_type": "record_case_progress",
                        "intent": "case_progress",
                        "entity_ids": ["case-entity"],
                    },
                    {
                        "action_id": "record-travel",
                        "action_type": "record_travel_event",
                        "intent": "travel_event",
                        "entity_ids": ["travel-entity"],
                    },
                    {
                        "action_id": "capture-daily",
                        "action_type": "capture_daily_event",
                        "intent": "daily_append",
                        "entity_ids": ["daily-entity"],
                    },
                ],
                "clarification_need": None,
                "context_update": {
                    "current_goal": "case_progress",
                    "remember_entity_ids": ["case-entity", "travel-entity", "daily-entity"],
                    "remember_turn": True,
                },
            }
        )


class _CaseRepository:
    def __init__(self, cases):
        self.cases = cases

    async def list_visible(self, context):
        return self.cases


class _AsyncBusinessExecutor:
    def __init__(self, delegate):
        self.delegate = delegate

    async def execute(self, command, context):
        return self.delegate.execute(command, context)


@pytest.mark.asyncio
async def test_one_message_executes_daily_case_progress_and_travel_with_independent_receipts():
    message_id = "cross-domain-message-1"
    case_id = str(uuid5(NAMESPACE_URL, "case-huadong"))
    owner_id = uuid5(NAMESPACE_URL, "user-1")
    daily_before = DailyReportMutationSnapshot(
        report_id=uuid5(NAMESPACE_URL, "daily-report-cross-domain"),
        owner_user_id=owner_id,
        version=0,
        status="collecting",
    )
    turn = CognitiveTurn(
        user_id="user-1",
        conversation_id="conversation-cross-domain",
        message_id=message_id,
        text="今天联系南京中院推进华东公司执行案，明天去南京，顺便记到日报。",
        occurred_at=NOW,
    )
    core_result = await CognitiveCoreV3(_OneTurnInterpreter()).process(
        turn,
        ConversationState.empty(user_id="user-1", conversation_id=turn.conversation_id),
    )
    plan = CognitiveCommandPlanner().plan(
        core_result.decision,
        CommandPlanningContext(
            message_id=message_id,
            actor_user_id=owner_id,
            daily_snapshot=daily_before,
        ),
    )

    daily = execute_typed_daily_command(
        plan.daily_commands[0],
        snapshot=daily_before,
        actor_user_id=owner_id,
    )
    cases = (CaseRecord(case_id, "tenant-test", "（2026）苏01执1号", "华东公司执行案", ("华东公司",)),)
    business_delegate = InMemoryBusinessExecutor(cases=cases)
    context = BusinessCommandContext(
        tenant_id="tenant-test",
        company_id="company-test",
        department_id="legal",
        team_id="litigation",
        actor_user_id="user-1",
        actor_role_ids=("lawyer",),
        allowed_case_ids=(case_id,),
        source_message_id=message_id,
        source_channel="dingtalk_stream",
        occurred_at=NOW,
    )
    composer = Phase2BusinessComposer(
        case_repository=_CaseRepository(cases),  # type: ignore[arg-type]
        executor=_AsyncBusinessExecutor(business_delegate),  # type: ignore[arg-type]
    )
    business = await composer.execute(plan.business_commands, context)

    assert [command.command_type for command in plan.daily_commands] == ["append_item"]
    assert [command.command_type for command in plan.business_commands] == [
        "record_case_progress_candidate",
        "record_travel_candidate",
    ]
    assert daily.validation.status == "authorized"
    assert daily.after.today_work == ("联系南京中院推进华东公司执行案",)
    assert daily.audit.message_id == message_id
    assert business.executed_count == 2
    assert business.blocked_count == 0
    assert [action.compiled_command_type for action in business.actions] == [
        "create_case_progress",
        "create_travel_intent",
    ]
    receipts = [action.receipt for action in business.actions]
    assert all(receipt is not None and receipt.source_message_id == message_id for receipt in receipts)
    assert len({receipt.receipt_id for receipt in receipts if receipt is not None}) == 2
    assert len(business_delegate.case_progress) == 1
    assert len(business_delegate.travel_intents) == 1
    assert {entry.source_message_id for entry in business_delegate.audit_log} == {message_id}

    replay = await composer.execute(plan.business_commands, context)

    assert [action.status for action in replay.actions] == ["duplicate", "duplicate"]
    assert len(business_delegate.case_progress) == 1
    assert len(business_delegate.travel_intents) == 1
    assert len(business_delegate.audit_log) == 2
