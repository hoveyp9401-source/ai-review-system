from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
import json
from inspect import signature
from types import SimpleNamespace
import pytest
from uuid import NAMESPACE_URL, uuid5

from app.agent2.command_planner_v3 import (
    CognitiveCommandPlanner,
    CommandPlanningContext,
    DailySnapshotReference,
)
from app.agent2.cognitive_orchestrator_v3 import (
    CognitiveOrchestratorV3,
    finalize_cognitive_state_after_execution,
)
from app.agent2.cognitive_core_v3 import (
    CognitiveCoreV3,
    CognitiveTurn,
    SemanticInterpretation,
)
from app.agent2.conversation_state import BoundPending, ConversationEntity, ConversationGoal, ConversationState, UserConstraints
from app.agent2.conversation_state_store import InMemoryConversationStateStore
from app.agent2.semantic_interpreter_v3 import LLMCognitiveSemanticInterpreter
from app.agent2.typed_daily_commands import (
    DailyReportMutationSnapshot,
    TypedDailyCommand,
    execute_typed_daily_command,
)
from app.agent2.typed_daily_executor import execute_typed_agent2_daily_commands


class QueueSemanticInterpreter:
    def __init__(self, *payloads: dict):
        self._payloads = list(payloads)

    async def interpret(self, turn: CognitiveTurn, state: ConversationState) -> SemanticInterpretation:
        assert turn.conversation_id == state.conversation_id
        return SemanticInterpretation.from_payload(self._payloads.pop(0))


class FakeStructuredCompletionClient:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls: list[dict] = []

    async def complete_json(self, **kwargs) -> str:
        self.calls.append(kwargs)
        return json.dumps(self.payload, ensure_ascii=False)


class SequenceStructuredCompletionClient:
    def __init__(self, *payloads: dict):
        self.payloads = list(payloads)
        self.calls: list[dict] = []

    async def complete_json(self, **kwargs) -> str:
        self.calls.append(kwargs)
        return json.dumps(self.payloads.pop(0), ensure_ascii=False)


def _turn(message_id: str, text: str) -> CognitiveTurn:
    return CognitiveTurn(
        user_id="user-1",
        conversation_id="conversation-1",
        message_id=message_id,
        text=text,
        occurred_at=datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc),
    )


def _daily_deictic_turn(message_id: str, text: str, *, today_work: tuple[str, ...]) -> CognitiveTurn:
    report_id = uuid5(NAMESPACE_URL, "daily-deictic-report")
    return CognitiveTurn(
        user_id="user-1",
        conversation_id="conversation-1",
        message_id=message_id,
        text=text,
        occurred_at=datetime(2026, 7, 13, 8, 30, tzinfo=timezone.utc),
        resources={
            "daily_draft": {
                "report_id": str(report_id),
                "version": 2,
                "status": "collecting",
                "items": [
                    {
                        "item_id": f"today-{index}",
                        "field": "today_work",
                        "field_index": index,
                        "text": value,
                    }
                    for index, value in enumerate(today_work, start=1)
                ],
            },
            "daily_policy": {"current_report_date": "2026-07-13"},
        },
    )


def _faulty_literal_tomorrow_payload(text: str) -> dict:
    return {
        "intents": ["daily_append"],
        "segments": [
            {
                "segment_id": "literal-plan-segment",
                "text": text,
                "intents": ["daily_append"],
                "entity_ids": ["literal-plan"],
                "action_ids": ["literal-plan-action"],
            }
        ],
        "entities": [
            {
                "entity_id": "literal-plan",
                "entity_type": "daily_event",
                "value": text,
                "confidence": 0.99,
                "attributes": {"field": "tomorrow_plan"},
            }
        ],
        "confidence": 0.99,
        "required_actions": [
            {
                "action_id": "literal-plan-action",
                "action_type": "capture_daily_event",
                "intent": "daily_append",
                "entity_ids": ["literal-plan"],
            }
        ],
        "clarification_need": None,
        "context_update": {"current_goal": "daily_append", "remember_turn": True},
    }


@pytest.mark.parametrize(
    "text",
    (
        "明天继续做这两件事情",
        "明天继续做这俩件事情",
        "明日接着推进这俩项工作",
        "明天这两个任务接着做",
    ),
)
def test_daily_deictic_plan_expands_the_unique_two_current_work_items(text):
    today_work = ("今天优化了法务中台网页端", "今天开始搭建全国保证金信息查询工具")
    turn = _daily_deictic_turn("daily-deictic-expand", text, today_work=today_work)
    state = replace(
        ConversationState.empty(user_id="user-1", conversation_id="conversation-1"),
        current_goal=ConversationGoal("daily_append", (), "prior-daily-context"),
    )
    client = FakeStructuredCompletionClient(_faulty_literal_tomorrow_payload(text))

    result = asyncio.run(CognitiveCoreV3(LLMCognitiveSemanticInterpreter(client)).process(turn, state))

    assert [action.action_type for action in result.decision.required_actions] == [
        "copy_current_work_to_tomorrow"
    ]
    snapshot = DailyReportMutationSnapshot(
        report_id=uuid5(NAMESPACE_URL, "daily-deictic-report"),
        owner_user_id=uuid5(NAMESPACE_URL, "daily-deictic-owner"),
        version=2,
        status="collecting",
        today_work=today_work,
        item_ids={"today_work": ("today-1", "today-2")},
    )
    plan = CognitiveCommandPlanner().plan(
        result.decision,
        CommandPlanningContext(
            message_id=turn.message_id,
            actor_user_id=snapshot.owner_user_id,
            daily_snapshot=snapshot,
            daily_history=(DailySnapshotReference(date(2026, 7, 13), snapshot),),
            current_report_date=date(2026, 7, 13),
        ),
    )
    assert plan.blocked_actions == ()
    assert plan.daily_commands[0].command_type == "copy_report"
    assert plan.daily_commands[0].patch["sections"] == {
        "tomorrow_plan": ["继续优化法务中台网页端", "继续搭建全国保证金信息查询工具"]
    }


def test_daily_deictic_plan_with_non_unique_item_count_clarifies_and_writes_nothing():
    text = "明天继续做这两件事情"
    turn = _daily_deictic_turn(
        "daily-deictic-ambiguous",
        text,
        today_work=("事项一", "事项二", "事项三"),
    )
    state = replace(
        ConversationState.empty(user_id="user-1", conversation_id="conversation-1"),
        current_goal=ConversationGoal("daily_append", (), "prior-daily-context"),
    )

    interpretation = asyncio.run(
        LLMCognitiveSemanticInterpreter(
            FakeStructuredCompletionClient(_faulty_literal_tomorrow_payload(text))
        ).interpret(turn, state)
    )

    assert interpretation.required_actions == ()
    assert interpretation.clarification_need is not None
    assert interpretation.clarification_need.reason == "daily_deictic_reference_not_unique"


def test_chat_context_survives_an_inserted_daily_event():
    interpreter = QueueSemanticInterpreter(
        {
            "intents": ["chat"],
            "entities": [],
            "confidence": 0.96,
            "required_actions": [],
            "clarification_need": None,
            "context_update": {"current_goal": "chat"},
        },
        {
            "intents": ["daily_append"],
            "entities": [
                {
                    "entity_id": "daily-event-1",
                    "entity_type": "daily_event",
                    "value": "今天去了法院",
                    "confidence": 0.95,
                    "attributes": {"field": "today_work"},
                }
            ],
            "confidence": 0.95,
            "required_actions": [
                {
                    "action_id": "capture-daily-1",
                    "action_type": "capture_daily_event",
                    "intent": "daily_append",
                    "entity_ids": ["daily-event-1"],
                }
            ],
            "clarification_need": None,
            "context_update": {
                "preserve_current_goal": True,
                "remember_entity_ids": ["daily-event-1"],
                "remember_turn": True,
            },
        },
        {
            "intents": ["chat"],
            "entities": [],
            "confidence": 0.93,
            "required_actions": [],
            "clarification_need": None,
            "context_update": {"preserve_current_goal": True, "remember_turn": True},
        },
    )
    core = CognitiveCoreV3(interpreter)
    state = ConversationState.empty(user_id="user-1", conversation_id="conversation-1")

    first = asyncio.run(core.process(_turn("m1", "聊聊天"), state))
    inserted = asyncio.run(core.process(_turn("m2", "今天去了法院"), first.state))
    continued = asyncio.run(core.process(_turn("m3", "接着聊刚才的话题"), inserted.state))

    assert inserted.decision.intents == ("daily_append",)
    assert [action.action_type for action in inserted.decision.required_actions] == ["capture_daily_event"]
    assert inserted.state.current_goal is not None
    assert inserted.state.current_goal.intent == "chat"
    assert [entity.value for entity in inserted.state.current_entities] == ["今天去了法院"]
    assert continued.state.current_goal is not None
    assert continued.state.current_goal.intent == "chat"
    assert [frame.message_id for frame in continued.state.recent_context] == ["m2", "m3"]


def test_daily_event_and_case_question_become_separate_typed_commands():
    interpreter = QueueSemanticInterpreter(
        {
            "intents": ["daily_append", "case_query"],
            "entities": [
                {
                    "entity_id": "daily-event-1",
                    "entity_type": "daily_event",
                    "value": "今天完成XX",
                    "confidence": 0.96,
                    "attributes": {"field": "today_work"},
                },
                {
                    "entity_id": "case-query-1",
                    "entity_type": "case_query",
                    "value": "王总那个案件风险怎么看",
                    "confidence": 0.91,
                    "attributes": {"matter_hint": "王总那个案件", "question": "风险怎么看"},
                },
            ],
            "confidence": 0.93,
            "required_actions": [
                {
                    "action_id": "capture-daily-1",
                    "action_type": "capture_daily_event",
                    "intent": "daily_append",
                    "entity_ids": ["daily-event-1"],
                },
                {
                    "action_id": "answer-case-1",
                    "action_type": "answer_case_query",
                    "intent": "case_query",
                    "entity_ids": ["case-query-1"],
                },
            ],
            "clarification_need": None,
            "context_update": {
                "current_goal": "case_query",
                "remember_entity_ids": ["daily-event-1", "case-query-1"],
                "remember_turn": True,
            },
        }
    )
    state = ConversationState.empty(user_id="user-1", conversation_id="conversation-1")
    result = asyncio.run(
        CognitiveCoreV3(interpreter).process(
            _turn("m4", "今天完成XX，另外王总那个案件风险怎么看"),
            state,
        )
    )
    report_id = uuid5(NAMESPACE_URL, "report-1")
    owner_id = uuid5(NAMESPACE_URL, "user-1")
    plan = CognitiveCommandPlanner().plan(
        result.decision,
        CommandPlanningContext(
            message_id="m4",
            actor_user_id=owner_id,
            daily_snapshot=DailyReportMutationSnapshot(
                report_id=report_id,
                owner_user_id=owner_id,
                version=3,
                status="collecting",
            ),
            user_constraints=result.state.user_constraints,
        ),
    )

    assert result.decision.intents == ("daily_append", "case_query")
    assert not {"allow_write", "should_write_db", "effects", "commands"} & set(asdict(result.decision))
    assert len(plan.daily_commands) == 1
    assert isinstance(plan.daily_commands[0], TypedDailyCommand)
    assert plan.daily_commands[0].command_type == "append_item"
    assert plan.daily_commands[0].patch == {"field": "today_work", "items": ["今天完成XX"]}
    assert [command.command_type for command in plan.business_commands] == ["query_case_risk"]
    assert plan.business_commands[0].execution_mode == "read_only"


def test_case_discussion_context_can_be_explicitly_reused_for_daily_append():
    case_payloads = [
        {
            "intents": ["case_discussion"],
            "entities": [
                {
                    "entity_id": f"case-fact-{index}",
                    "entity_type": "case_fact",
                    "value": text,
                    "confidence": 0.9,
                }
            ],
            "confidence": 0.9,
            "required_actions": [],
            "clarification_need": None,
            "context_update": {
                "current_goal": "case_discussion",
                "remember_entity_ids": [f"case-fact-{index}"],
                "remember_turn": True,
            },
        }
        for index, text in enumerate(
            [
                "王总案件已经立案",
                "对方提交了补充证据",
                "我方完成质证意见",
                "今天进行了庭审",
                "法院认为证据链完整",
            ],
            start=1,
        )
    ]
    interpreter = QueueSemanticInterpreter(
        *case_payloads,
        {
            "intents": ["daily_append"],
            "entities": [
                {
                    "entity_id": "daily-from-case-context",
                    "entity_type": "daily_event",
                    "value": "刚才那个案件进展",
                    "confidence": 0.94,
                    "attributes": {
                        "field": "today_work",
                        "context_reference": {
                            "intent": "case_discussion",
                            "selection": "latest",
                            "value_source": "summary",
                        },
                    },
                }
            ],
            "confidence": 0.94,
            "required_actions": [
                {
                    "action_id": "capture-recent-case",
                    "action_type": "capture_daily_event",
                    "intent": "daily_append",
                    "entity_ids": ["daily-from-case-context"],
                }
            ],
            "clarification_need": None,
            "context_update": {
                "preserve_current_goal": True,
                "remember_entity_ids": ["daily-from-case-context"],
                "remember_turn": True,
            },
        },
    )
    core = CognitiveCoreV3(interpreter)
    state = ConversationState.empty(user_id="user-1", conversation_id="conversation-1")
    case_texts = [payload["entities"][0]["value"] for payload in case_payloads]
    for index, text in enumerate(case_texts, start=1):
        state = asyncio.run(core.process(_turn(f"case-{index}", text), state)).state

    result = asyncio.run(core.process(_turn("m5", "刚才那个补充到今天日报"), state))
    owner_id = uuid5(NAMESPACE_URL, "user-1")
    plan = CognitiveCommandPlanner().plan(
        result.decision,
        CommandPlanningContext(
            message_id="m5",
            actor_user_id=owner_id,
            daily_snapshot=DailyReportMutationSnapshot(
                report_id=uuid5(NAMESPACE_URL, "report-2"),
                owner_user_id=owner_id,
                version=1,
                status="collecting",
            ),
        ),
    )

    resolved = result.decision.entities[0]
    assert resolved.value == "法院认为证据链完整"
    assert resolved.source_context_id == state.recent_context[-1].context_id
    assert plan.daily_commands[0].patch["items"] == ["法院认为证据链完整"]
    assert result.state.current_goal is not None
    assert result.state.current_goal.intent == "case_discussion"


def test_one_turn_plans_travel_daily_and_case_query_without_cross_writes():
    interpreter = QueueSemanticInterpreter(
        {
            "intents": ["travel_event", "daily_append", "case_query"],
            "entities": [
                {
                    "entity_id": "travel-1",
                    "entity_type": "travel_event",
                    "value": "明天上海开庭",
                    "confidence": 0.97,
                    "attributes": {"destination": "上海", "date_hint": "tomorrow", "purpose": "开庭"},
                },
                {
                    "entity_id": "daily-plan-1",
                    "entity_type": "daily_event",
                    "value": "明天前往上海开庭",
                    "confidence": 0.95,
                    "attributes": {"field": "tomorrow_plan"},
                },
                {
                    "entity_id": "case-query-2",
                    "entity_type": "case_query",
                    "value": "这个案件有没有风险",
                    "confidence": 0.9,
                    "attributes": {"question": "有没有风险"},
                },
            ],
            "confidence": 0.94,
            "required_actions": [
                {
                    "action_id": "record-travel-1",
                    "action_type": "record_travel_event",
                    "intent": "travel_event",
                    "entity_ids": ["travel-1"],
                },
                {
                    "action_id": "capture-daily-plan-1",
                    "action_type": "capture_daily_event",
                    "intent": "daily_append",
                    "entity_ids": ["daily-plan-1"],
                },
                {
                    "action_id": "answer-case-2",
                    "action_type": "answer_case_query",
                    "intent": "case_query",
                    "entity_ids": ["case-query-2"],
                },
            ],
            "clarification_need": None,
            "context_update": {
                "current_goal": "case_query",
                "remember_entity_ids": ["travel-1", "daily-plan-1", "case-query-2"],
                "remember_turn": True,
            },
        }
    )
    result = asyncio.run(
        CognitiveCoreV3(interpreter).process(
            _turn("m6", "明天上海开庭，帮我记一下，然后看看这个案件有没有风险"),
            ConversationState.empty(user_id="user-1", conversation_id="conversation-1"),
        )
    )
    owner_id = uuid5(NAMESPACE_URL, "user-1")
    plan = CognitiveCommandPlanner().plan(
        result.decision,
        CommandPlanningContext(
            message_id="m6",
            actor_user_id=owner_id,
            daily_snapshot=DailyReportMutationSnapshot(
                report_id=uuid5(NAMESPACE_URL, "report-3"),
                owner_user_id=owner_id,
                version=2,
                status="collecting",
            ),
        ),
    )

    assert result.decision.intents == ("travel_event", "daily_append", "case_query")
    assert [command.command_type for command in plan.daily_commands] == ["append_item"]
    assert plan.daily_commands[0].patch == {"field": "tomorrow_plan", "items": ["明天前往上海开庭"]}
    assert [command.command_type for command in plan.business_commands] == [
        "record_travel_candidate",
        "query_case_risk",
    ]
    assert [command.execution_mode for command in plan.business_commands] == ["candidate", "read_only"]
    assert plan.blocked_actions == ()
    daily_execution = execute_typed_daily_command(
        plan.daily_commands[0],
        snapshot=DailyReportMutationSnapshot(
            report_id=uuid5(NAMESPACE_URL, "report-3"),
            owner_user_id=owner_id,
            version=2,
            status="collecting",
        ),
        actor_user_id=owner_id,
    )
    assert daily_execution.audit.message_id == "m6"


def test_daily_idempotency_key_is_stable_when_model_action_id_changes():
    def payload(action_id: str) -> dict:
        return {
            "intents": ["daily_append"],
            "entities": [
                {
                    "entity_id": "daily-stable",
                    "entity_type": "daily_event",
                    "value": "完成合同审核",
                    "confidence": 0.99,
                    "attributes": {"field": "today_work"},
                }
            ],
            "confidence": 0.99,
            "required_actions": [
                {
                    "action_id": action_id,
                    "action_type": "capture_daily_event",
                    "intent": "daily_append",
                    "entity_ids": ["daily-stable"],
                }
            ],
            "clarification_need": None,
            "context_update": {"remember_turn": True},
        }

    turn = _turn("stable-message", "今天完成合同审核")
    state = ConversationState.empty(user_id="user-1", conversation_id="conversation-1")
    owner_id = uuid5(NAMESPACE_URL, "user-1")
    snapshot = DailyReportMutationSnapshot(
        report_id=uuid5(NAMESPACE_URL, "stable-report"),
        owner_user_id=owner_id,
        version=0,
        status="collecting",
    )
    plans = []
    for action_id in ("model-action-a", "model-action-b"):
        result = asyncio.run(CognitiveCoreV3(QueueSemanticInterpreter(payload(action_id))).process(turn, state))
        plans.append(
            CognitiveCommandPlanner().plan(
                result.decision,
                CommandPlanningContext(
                    message_id=turn.message_id,
                    actor_user_id=owner_id,
                    daily_snapshot=snapshot,
                ),
            )
        )

    assert plans[0].daily_commands[0].idempotency_key == plans[1].daily_commands[0].idempotency_key


@pytest.mark.parametrize(
    ("action_type", "intent", "expected_command"),
    (
        ("update_case_progress", "case_progress_update", "update_case_progress_candidate"),
        ("delete_case_progress", "case_progress_delete", "delete_case_progress_candidate"),
        ("query_case_progress", "case_progress_query", "query_case_progress_candidate"),
        ("link_case_progress", "case_progress_update", "link_case_progress_candidate"),
    ),
)
def test_existing_case_progress_actions_plan_structured_candidates(
    action_type,
    intent,
    expected_command,
):
    text = "把刚才那条案件进展改成法院预计本周五反馈"
    attributes = {
        "case_hint": "华东建设案",
        "replacement_summary": "法院预计本周五反馈",
    }
    if action_type == "delete_case_progress":
        text = "删除我刚才误记的华东建设案进展"
        attributes = {"case_hint": "华东建设案", "delete_reason": "用户误记"}
    elif action_type == "query_case_progress":
        text = "查一下华东建设案最近的进展"
        attributes = {"case_hint": "华东建设案"}
    elif action_type == "link_case_progress":
        text = "把刚才的进展关联到这次出差"
        attributes = {"related_travel_intent_ids": ["travel-1"]}
    payload = {
        "intents": [intent],
        "segments": [
            {
                "segment_id": "segment-1",
                "text": text,
                "intents": [intent],
                "entity_ids": ["progress-ref-1"],
                "action_ids": ["action-1"],
            }
        ],
        "entities": [
            {
                "entity_id": "progress-ref-1",
                "entity_type": "case_progress_ref",
                "value": "刚才的案件进展",
                "confidence": 0.98,
                "attributes": attributes,
            }
        ],
        "confidence": 0.98,
        "required_actions": [
            {
                "action_id": "action-1",
                "action_type": action_type,
                "intent": intent,
                "entity_ids": ["progress-ref-1"],
            }
        ],
        "clarification_need": None,
        "context_update": {"remember_entity_ids": ["progress-ref-1"], "remember_turn": True},
    }
    result = asyncio.run(
        CognitiveCoreV3(QueueSemanticInterpreter(payload)).process(
            _turn("case-progress-action", text),
            ConversationState.empty(user_id="user-1", conversation_id="conversation-1"),
        )
    )

    plan = CognitiveCommandPlanner().plan(
        result.decision,
        CommandPlanningContext(
            message_id="case-progress-action",
            actor_user_id=uuid5(NAMESPACE_URL, "user-1"),
        ),
    )

    assert plan.blocked_actions == ()
    assert len(plan.business_commands) == 1
    assert plan.business_commands[0].command_type == expected_command
    assert plan.business_commands[0].payload["source_segments"][0]["text"] == text


def test_travel_collaboration_response_plans_candidate_bound_to_trusted_id():
    candidate_id = "11111111-1111-5111-8111-111111111111"
    payload = {
        "intents": ["travel_collaboration_response"],
        "segments": [
            {
                "segment_id": "segment-1",
                "text": "需要",
                "intents": ["travel_collaboration_response"],
                "entity_ids": ["collaboration-1"],
                "action_ids": ["respond-1"],
            }
        ],
        "entities": [
            {
                "entity_id": "collaboration-1",
                "entity_type": "travel_collaboration_ref",
                "value": "需要",
                "confidence": 1.0,
                "attributes": {"candidate_id": candidate_id, "response": "accept"},
            }
        ],
        "confidence": 1.0,
        "required_actions": [
            {
                "action_id": "respond-1",
                "action_type": "respond_travel_collaboration",
                "intent": "travel_collaboration_response",
                "entity_ids": ["collaboration-1"],
            }
        ],
        "clarification_need": None,
        "context_update": {"remember_entity_ids": ["collaboration-1"], "remember_turn": True},
    }
    result = asyncio.run(
        CognitiveCoreV3(QueueSemanticInterpreter(payload)).process(
            _turn("travel-response", "需要"),
            ConversationState.empty(user_id="user-1", conversation_id="conversation-1"),
        )
    )

    plan = CognitiveCommandPlanner().plan(
        result.decision,
        CommandPlanningContext(
            message_id="travel-response",
            actor_user_id=uuid5(NAMESPACE_URL, "user-1"),
        ),
    )

    assert plan.blocked_actions == ()
    assert plan.business_commands[0].command_type == "respond_travel_collaboration_candidate"
    assert plan.business_commands[0].payload["entities"][0]["attributes"]["candidate_id"] == candidate_id


def test_explicit_daily_submit_is_not_hijacked_by_bound_monthly_pending():
    now = datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc)
    monthly_entity = ConversationEntity(
        entity_id="monthly-report-1",
        entity_type="monthly_report",
        value="2026年7月月报",
        confidence=1.0,
    )
    monthly_pending = BoundPending(
        pending_id="pending-monthly-1",
        user_id="user-1",
        conversation_id="conversation-1",
        intent="monthly_submit",
        action="confirm_monthly_report",
        entity_ids=(monthly_entity.entity_id,),
        context_id="monthly-context-1",
        created_at=now - timedelta(minutes=5),
        expires_at=now + timedelta(minutes=25),
    )
    state = ConversationState(
        user_id="user-1",
        conversation_id="conversation-1",
        current_entities=(monthly_entity,),
        pending=(monthly_pending,),
    )
    interpreter = QueueSemanticInterpreter(
        {
            "intents": ["daily_submit"],
            "entities": [],
            "confidence": 0.99,
            "required_actions": [
                {
                    "action_id": "submit-daily-1",
                    "action_type": "submit_daily_report",
                    "intent": "daily_submit",
                    "entity_ids": [],
                }
            ],
            "clarification_need": None,
            "context_update": {"current_goal": "daily_submit", "remember_turn": True},
        }
    )
    result = asyncio.run(CognitiveCoreV3(interpreter).process(_turn("m7", "提交日报"), state))
    owner_id = uuid5(NAMESPACE_URL, "user-1")
    plan = CognitiveCommandPlanner().plan(
        result.decision,
        CommandPlanningContext(
            message_id="m7",
            actor_user_id=owner_id,
            daily_snapshot=DailyReportMutationSnapshot(
                report_id=uuid5(NAMESPACE_URL, "report-4"),
                owner_user_id=owner_id,
                version=5,
                status="collecting",
                today_work=("完成合同审核",),
                problems=("暂无",),
                tomorrow_plan=("继续跟进",),
            ),
        ),
    )

    assert result.decision.intents == ("daily_submit",)
    assert [command.command_type for command in plan.daily_commands] == ["submit_report"]
    assert plan.daily_commands[0].report_version == 5
    assert plan.blocked_actions == ()
    assert result.state.pending == (monthly_pending,)


def test_pending_created_by_cognitive_core_is_bound_to_intent_entity_action_and_expiry():
    interpreter = QueueSemanticInterpreter(
        {
            "intents": ["daily_clear"],
            "entities": [
                {
                    "entity_id": "daily-report-1",
                    "entity_type": "daily_report",
                    "value": "当前日报",
                    "confidence": 1.0,
                }
            ],
            "confidence": 0.99,
            "required_actions": [],
            "clarification_need": {
                "reason": "high_impact_confirmation_required",
                "missing_fields": [],
                "question": "确认清空当前日报吗？",
            },
            "context_update": {
                "current_goal": "daily_clear",
                "remember_entity_ids": ["daily-report-1"],
                "remember_turn": True,
                "bind_pending": {
                    "pending_id": "pending-clear-1",
                    "intent": "daily_clear",
                    "action": "clear_daily_report",
                    "entity_ids": ["daily-report-1"],
                    "expires_in_seconds": 600,
                },
            },
        }
    )
    turn = _turn("m8", "清空整份日报")
    result = asyncio.run(
        CognitiveCoreV3(interpreter).process(
            turn,
            ConversationState.empty(user_id="user-1", conversation_id="conversation-1"),
        )
    )

    assert len(result.state.pending) == 1
    pending = result.state.pending[0]
    assert pending.pending_id == "pending-clear-1"
    assert pending.user_id == "user-1"
    assert pending.conversation_id == "conversation-1"
    assert pending.intent == "daily_clear"
    assert pending.action == "clear_daily_report"
    assert pending.entity_ids == ("daily-report-1",)
    assert pending.context_id == result.decision.decision_id
    assert pending.expires_at == turn.occurred_at + timedelta(seconds=600)


def test_user_no_write_constraint_persists_and_blocks_daily_command_planning():
    interpreter = QueueSemanticInterpreter(
        {
            "intents": ["daily_append"],
            "entities": [
                {
                    "entity_id": "daily-event-blocked",
                    "entity_type": "daily_event",
                    "value": "今天去了法院",
                    "confidence": 0.9,
                    "attributes": {"field": "today_work"},
                }
            ],
            "confidence": 0.9,
            "required_actions": [
                {
                    "action_id": "capture-blocked-daily",
                    "action_type": "capture_daily_event",
                    "intent": "daily_append",
                    "entity_ids": ["daily-event-blocked"],
                }
            ],
            "clarification_need": None,
            "context_update": {
                "remember_turn": True,
                "user_constraints": {
                    "no_daily_write": True,
                    "sources": ["explicit_user_instruction"],
                },
            },
        },
        {
            "intents": ["chat"],
            "entities": [],
            "confidence": 0.9,
            "required_actions": [],
            "clarification_need": None,
            "context_update": {"remember_turn": True},
        },
    )
    core = CognitiveCoreV3(interpreter)
    first = asyncio.run(
        core.process(
            _turn("m9", "不要写入日报，只聊聊今天去了法院"),
            ConversationState.empty(user_id="user-1", conversation_id="conversation-1"),
        )
    )
    second = asyncio.run(core.process(_turn("m10", "继续聊"), first.state))
    owner_id = uuid5(NAMESPACE_URL, "user-1")
    plan = CognitiveCommandPlanner().plan(
        first.decision,
        CommandPlanningContext(
            message_id="m9",
            actor_user_id=owner_id,
            daily_snapshot=DailyReportMutationSnapshot(
                report_id=uuid5(NAMESPACE_URL, "report-5"),
                owner_user_id=owner_id,
                version=0,
                status="collecting",
            ),
            user_constraints=first.state.user_constraints,
        ),
    )

    assert first.state.user_constraints.no_daily_write is True
    assert second.state.user_constraints.no_daily_write is True
    assert plan.daily_commands == ()
    assert [block.reason_code for block in plan.blocked_actions] == ["user_constraint_blocks_daily_write"]


def test_llm_semantic_interpreter_receives_conversation_state_and_returns_only_cognition():
    payload = {
        "intents": ["daily_append", "case_query"],
        "entities": [
            {
                "entity_id": "daily-llm-1",
                "entity_type": "daily_event",
                "value": "今天完成合同审核",
                "confidence": 0.94,
                "attributes": {"field": "today_work"},
            },
            {
                "entity_id": "case-llm-1",
                "entity_type": "case_query",
                "value": "王总案件风险怎么看",
                "confidence": 0.9,
            },
        ],
        "confidence": 0.92,
        "required_actions": [
            {
                "action_id": "capture-llm-daily",
                "action_type": "capture_daily_event",
                "intent": "daily_append",
                "entity_ids": ["daily-llm-1"],
            },
            {
                "action_id": "answer-llm-case",
                "action_type": "answer_case_query",
                "intent": "case_query",
                "entity_ids": ["case-llm-1"],
            },
        ],
        "clarification_need": None,
        "context_update": {"current_goal": "case_query", "remember_turn": True},
    }
    client = FakeStructuredCompletionClient(payload)
    state = ConversationState(
        user_id="user-1",
        conversation_id="conversation-1",
        user_constraints=UserConstraints(read_only=True, sources=("explicit_user_instruction",)),
    )
    turn = _turn("m11", "今天完成合同审核，另外王总案件风险怎么看")
    interpretation = asyncio.run(LLMCognitiveSemanticInterpreter(client).interpret(turn, state))

    assert interpretation.intents == ("daily_append", "case_query")
    assert [action.action_type for action in interpretation.required_actions] == [
        "capture_daily_event",
        "answer_case_query",
    ]
    assert len(client.calls) == 1
    prompt = client.calls[0]["user_prompt"]
    assert '"read_only": true' in prompt
    assert '"conversation_id": "conversation-1"' in prompt
    assert "今天完成合同审核" in prompt
    assert "should_write_db" not in asdict(interpretation)


def test_daily_report_opening_is_never_captured_as_report_content():
    text = "我要填日报了"
    payload = {
        "intents": ["daily_append"],
        "segments": [
            {
                "segment_id": "daily-opening",
                "text": text,
                "intents": ["daily_append"],
                "entity_ids": ["daily-opening-event"],
                "action_ids": ["capture-daily-opening"],
            }
        ],
        "entities": [
            {
                "entity_id": "daily-opening-event",
                "entity_type": "daily_event",
                "value": text,
                "confidence": 0.99,
                "attributes": {"field": "today_work"},
            }
        ],
        "confidence": 0.99,
        "required_actions": [
            {
                "action_id": "capture-daily-opening",
                "action_type": "capture_daily_event",
                "intent": "daily_append",
                "entity_ids": ["daily-opening-event"],
            }
        ],
        "clarification_need": None,
        "context_update": {"current_goal": "daily_append", "remember_turn": True},
    }
    client = FakeStructuredCompletionClient(payload)

    interpretation = asyncio.run(
        LLMCognitiveSemanticInterpreter(client).interpret(
            _turn("daily-opening-message", text),
            ConversationState.empty(user_id="user-1", conversation_id="conversation-1"),
        )
    )

    assert interpretation.intents == ("daily_report",)
    assert interpretation.entities == ()
    assert interpretation.required_actions == ()
    assert interpretation.segments[0].text == text
    assert interpretation.context_update.current_goal == "daily_report"


@pytest.mark.parametrize(
    ("text", "expected_intent"),
    (
        ("写个日报吧", "daily_report"),
        ("我想写周报", "weekly_report"),
        ("开始写月报", "monthly_report"),
    ),
)
def test_periodic_report_opening_switches_report_domain_without_persisting_content(
    text,
    expected_intent,
):
    payload = {
        "intents": ["chat"],
        "segments": [
            {
                "segment_id": "wrong-chat",
                "text": text,
                "intents": ["chat"],
                "entity_ids": [],
                "action_ids": [],
            }
        ],
        "entities": [],
        "confidence": 0.7,
        "required_actions": [],
        "clarification_need": None,
        "context_update": {"preserve_current_goal": True, "remember_turn": True},
    }

    interpretation = asyncio.run(
        LLMCognitiveSemanticInterpreter(FakeStructuredCompletionClient(payload)).interpret(
            _turn(f"{expected_intent}-opening", text),
            ConversationState.empty(user_id="user-1", conversation_id="conversation-1"),
        )
    )

    assert interpretation.intents == (expected_intent,)
    assert interpretation.entities == ()
    assert interpretation.required_actions == ()
    assert interpretation.context_update.current_goal == expected_intent


def test_explicit_structured_daily_report_overrides_stale_monthly_goal():
    text = (
        "【今日完成】 1.企查查沟通续签；2.人力中心ai推进沟通；3.待办推进；4.技能迭代。\n"
        "【明日计划】 1.技能继续迭代；2.绩效评估及半年度绩效分析；\n"
        "【风险与问题】 无"
    )
    empty = ConversationState.empty(user_id="user-1", conversation_id="conversation-1")
    state = replace(
        empty,
        current_goal=ConversationGoal(
            intent="monthly_report",
            entity_ids=(),
            source_context_id="stale-monthly-context",
        ),
    )
    wrong_monthly_payload = {
        "intents": ["monthly_report"],
        "segments": [
            {
                "segment_id": "wrong-monthly-segment",
                "text": text,
                "intents": ["monthly_report"],
                "entity_ids": ["wrong-monthly-event"],
                "action_ids": ["wrong-monthly-capture"],
            }
        ],
        "entities": [
            {
                "entity_id": "wrong-monthly-event",
                "entity_type": "report_event",
                "value": text,
                "confidence": 0.99,
                "attributes": {"report_type": "monthly", "field": "accomplishments"},
            }
        ],
        "confidence": 0.99,
        "required_actions": [
            {
                "action_id": "wrong-monthly-capture",
                "action_type": "capture_report_event",
                "intent": "monthly_report",
                "entity_ids": ["wrong-monthly-event"],
            }
        ],
        "clarification_need": None,
        "context_update": {"preserve_current_goal": True, "remember_turn": True},
    }

    interpretation = asyncio.run(
        LLMCognitiveSemanticInterpreter(
            FakeStructuredCompletionClient(wrong_monthly_payload),
            legacy_semantic_enforcers_enabled=False,
        ).interpret(_turn("structured-daily-after-reminder", text), state)
    )

    assert interpretation.intents == ("daily_append",)
    assert {entity.attributes["field"] for entity in interpretation.entities} == {
        "today_work",
        "problems",
        "tomorrow_plan",
    }
    assert [entity.value for entity in interpretation.entities] == [
        "企查查沟通续签",
        "人力中心ai推进沟通",
        "待办推进",
        "技能迭代。",
        "技能继续迭代",
        "绩效评估及半年度绩效分析",
        "无",
    ]
    assert all(
        action.action_type == "capture_daily_event"
        for action in interpretation.required_actions
    )
    assert interpretation.context_update.current_goal == "daily_report"
    assert interpretation.context_update.preserve_current_goal is False


def test_active_monthly_task_exit_is_not_degraded_to_chat_or_report_submission():
    text = "月报任务结束了"
    empty = ConversationState.empty(user_id="user-1", conversation_id="conversation-1")
    state = ConversationState(
        **{
            **empty.__dict__,
            "current_goal": ConversationGoal(
                intent="monthly_report", entity_ids=(), source_context_id="monthly-context"
            ),
        }
    )
    payload = {
        "intents": ["chat"],
        "segments": [{"segment_id": "wrong-chat", "text": text, "intents": ["chat"]}],
        "entities": [],
        "confidence": 0.8,
        "required_actions": [],
        "clarification_need": None,
        "context_update": {"preserve_current_goal": True, "remember_turn": True},
    }

    interpretation = asyncio.run(
        LLMCognitiveSemanticInterpreter(FakeStructuredCompletionClient(payload)).interpret(
            _turn("monthly-exit", text), state
        )
    )

    assert interpretation.intents == ("monthly_report_exit",)
    assert interpretation.required_actions == ()
    assert interpretation.context_update.clear_current_goal is True


def test_report_task_exit_clears_stale_goal_without_resuming_an_old_report():
    state = ConversationState.empty(user_id="user-1", conversation_id="conversation-1")
    state = ConversationState(
        **{
            **state.__dict__,
            "current_goal": ConversationGoal(
                intent="monthly_report", entity_ids=(), source_context_id="monthly-context"
            ),
            "goal_stack": (
                ConversationGoal(
                    intent="daily_report", entity_ids=(), source_context_id="old-daily"
                ),
            ),
        }
    )
    interpreter = QueueSemanticInterpreter(
        {
            "intents": ["monthly_report_exit"],
            "entities": [],
            "confidence": 1.0,
            "required_actions": [],
            "clarification_need": None,
            "context_update": {"clear_current_goal": True, "remember_turn": True},
        }
    )

    result = asyncio.run(
        CognitiveCoreV3(interpreter).process(_turn("monthly-exit-state", "月报任务结束了"), state)
    )

    assert result.state.current_goal is None
    assert [goal.intent for goal in result.state.goal_stack] == ["daily_report"]


def test_stale_case_context_cannot_mutate_progress_after_focus_moved_to_daily():
    text = "没其他风险"
    payload = {
        "intents": ["case_progress"],
        "segments": [
            {
                "segment_id": "stale-case-followup",
                "text": text,
                "intents": ["case_progress"],
                "entity_ids": ["stale-progress"],
                "action_ids": ["stale-update"],
            }
        ],
        "entities": [
            {
                "entity_id": "stale-progress",
                "entity_type": "case_progress_ref",
                "value": "预计下周开庭",
                "confidence": 0.9,
                "attributes": {
                    "case_hint": "旧案件",
                    "progress_id": "5b1b2088-8e0d-5cb3-85f7-b414a59ee736",
                    "expected_version": 1,
                    "replacement_summary": "预计下周开庭，没其他风险",
                    "context_reference": {
                        "intent": "case_progress",
                        "context_id": "old-case-context",
                        "selection": "latest",
                        "value_source": "summary",
                    },
                },
            }
        ],
        "confidence": 0.9,
        "required_actions": [
            {
                "action_id": "stale-update",
                "action_type": "update_case_progress",
                "intent": "case_progress",
                "entity_ids": ["stale-progress"],
            }
        ],
        "clarification_need": None,
        "context_update": {"current_goal": "case_progress", "remember_turn": True},
    }
    state = ConversationState(
        user_id="user-1",
        conversation_id="conversation-1",
        current_goal=ConversationGoal(intent="daily_append"),
    )

    interpretation = asyncio.run(
        LLMCognitiveSemanticInterpreter(FakeStructuredCompletionClient(payload)).interpret(
            _turn("stale-case-message", text),
            state,
        )
    )

    assert interpretation.intents == ("chat",)
    assert interpretation.required_actions == ()


def test_current_report_domain_blocks_case_mutation_even_when_model_omits_context_reference():
    text = "没其他风险"
    payload = {
        "intents": ["case_progress"],
        "segments": [{
            "segment_id": "wrong-case", "text": text,
            "intents": ["case_progress"], "entity_ids": ["wrong-progress"],
            "action_ids": ["wrong-update"],
        }],
        "entities": [{
            "entity_id": "wrong-progress", "entity_type": "case_progress_ref",
            "value": text, "confidence": 0.8, "attributes": {},
        }],
        "confidence": 0.8,
        "required_actions": [{
            "action_id": "wrong-update", "action_type": "update_case_progress",
            "intent": "case_progress", "entity_ids": ["wrong-progress"],
        }],
        "clarification_need": None,
        "context_update": {"current_goal": "case_progress"},
    }
    state = ConversationState(
        user_id="user-1",
        conversation_id="conversation-1",
        current_goal=ConversationGoal(intent="daily_report"),
    )

    interpretation = asyncio.run(
        LLMCognitiveSemanticInterpreter(FakeStructuredCompletionClient(payload)).interpret(
            _turn("no-context-reference", text), state
        )
    )

    assert interpretation.intents == ("chat",)
    assert interpretation.required_actions == ()
    assert interpretation.entities == ()


def test_trusted_case_progress_version_can_survive_into_the_next_conversation_turn():
    state = ConversationState(
        user_id="user-1",
        conversation_id="conversation-1",
        current_goal=ConversationGoal(intent="case_progress_update"),
        current_entities=(
            ConversationEntity(
                entity_id="trusted-progress",
                entity_type="case_progress_ref",
                value="刚才的案件进展",
                confidence=1.0,
                attributes={
                    "progress_id": "5b1b2088-8e0d-5cb3-85f7-b414a59ee736",
                    "expected_version": 2,
                },
            ),
        ),
    )
    client = FakeStructuredCompletionClient(
        {
            "intents": ["chat"],
            "segments": [
                {
                    "segment_id": "next-turn",
                    "text": "继续",
                    "intents": ["chat"],
                    "entity_ids": [],
                    "action_ids": [],
                }
            ],
            "entities": [],
            "confidence": 1.0,
            "required_actions": [],
            "clarification_need": None,
            "context_update": {"preserve_current_goal": True},
        }
    )

    interpretation = asyncio.run(
        LLMCognitiveSemanticInterpreter(client).interpret(_turn("trusted-version-next", "继续"), state)
    )

    assert interpretation.intents == ("chat",)
    assert '"expected_version": 2' in client.calls[0]["user_prompt"]


def test_continue_pending_is_removed_when_binding_does_not_match():
    now = datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc)
    monthly_entity = ConversationEntity(
        entity_id="monthly-report-bound",
        entity_type="monthly_report",
        value="7月月报",
        confidence=1.0,
    )
    state = ConversationState(
        user_id="user-1",
        conversation_id="conversation-1",
        current_entities=(monthly_entity,),
        pending=(
            BoundPending(
                pending_id="pending-monthly-bound",
                user_id="user-1",
                conversation_id="conversation-1",
                intent="monthly_submit",
                action="confirm_monthly_report",
                entity_ids=(monthly_entity.entity_id,),
                context_id="monthly-context-bound",
                created_at=now - timedelta(minutes=1),
                expires_at=now + timedelta(minutes=10),
            ),
        ),
    )
    interpreter = QueueSemanticInterpreter(
        {
            "intents": ["daily_clear"],
            "entities": [
                {
                    "entity_id": "daily-report-unbound",
                    "entity_type": "daily_report",
                    "value": "当前日报",
                    "confidence": 1.0,
                }
            ],
            "confidence": 0.8,
            "required_actions": [
                {
                    "action_id": "continue-wrong-pending",
                    "action_type": "continue_pending",
                    "intent": "daily_clear",
                    "entity_ids": ["daily-report-unbound"],
                    "parameters": {
                        "pending_id": "pending-monthly-bound",
                        "bound_action": "clear_daily_report",
                    },
                }
            ],
            "clarification_need": None,
            "context_update": {"remember_turn": True},
        }
    )
    result = asyncio.run(CognitiveCoreV3(interpreter).process(_turn("m12", "是的"), state))

    assert result.decision.required_actions == ()
    assert result.decision.clarification_need is not None
    assert result.decision.clarification_need.reason == "pending_binding_mismatch"
    assert result.state.pending == state.pending


def test_unique_bound_pending_continues_original_supported_action_once():
    now = datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc)
    target = ConversationEntity(
        entity_id="pending-daily-target",
        entity_type="daily_item_target",
        value="第 2 条",
        confidence=1.0,
        attributes={"target_item_ids": ["item-2"]},
    )
    state = ConversationState(
        user_id="user-1",
        conversation_id="conversation-1",
        current_entities=(target,),
        pending=(
            BoundPending(
                pending_id="pending-delete-item-2",
                user_id="user-1",
                conversation_id="conversation-1",
                intent="daily_modify",
                action="delete_daily_item",
                entity_ids=(target.entity_id,),
                context_id="daily-context-bound",
                created_at=now - timedelta(minutes=1),
                expires_at=now + timedelta(minutes=10),
            ),
        ),
    )
    interpreter = QueueSemanticInterpreter(
        {
            "intents": ["daily_modify"],
            "entities": [
                {
                    "entity_id": target.entity_id,
                    "entity_type": target.entity_type,
                    "value": target.value,
                    "confidence": 1.0,
                    "attributes": dict(target.attributes),
                }
            ],
            "confidence": 1.0,
            "required_actions": [
                {
                    "action_id": "continue-delete-item-2",
                    "action_type": "continue_pending",
                    "intent": "daily_modify",
                    "entity_ids": [target.entity_id],
                    "parameters": {
                        "pending_id": "pending-delete-item-2",
                        "bound_action": "delete_daily_item",
                    },
                }
            ],
            "clarification_need": None,
            "context_update": {"remember_turn": True},
        }
    )
    result = asyncio.run(CognitiveCoreV3(interpreter).process(_turn("m12-confirm", "是的"), state))
    plan = CognitiveCommandPlanner().plan(
        result.decision,
        CommandPlanningContext(
            message_id="m12-confirm",
            actor_user_id=uuid5(NAMESPACE_URL, "pending-owner"),
            daily_snapshot=DailyReportMutationSnapshot(
                report_id=uuid5(NAMESPACE_URL, "pending-report"),
                owner_user_id=uuid5(NAMESPACE_URL, "pending-owner"),
                version=4,
                status="collecting",
                today_work=("alpha", "beta"),
                item_ids={"today_work": ("item-1", "item-2")},
            ),
        ),
    )

    assert [action.action_type for action in result.decision.required_actions] == ["delete_daily_item"]
    assert [command.command_type for command in plan.daily_commands] == ["delete_item"]
    assert plan.daily_commands[0].target_item_ids == ("item-2",)
    assert result.state.pending == state.pending


def test_cognitive_orchestrator_defers_command_state_until_successful_execution():
    interpreter = QueueSemanticInterpreter(
        {
            "intents": ["daily_append", "case_query"],
            "entities": [
                {
                    "entity_id": "orchestrated-daily",
                    "entity_type": "daily_event",
                    "value": "完成合同审核",
                    "confidence": 0.95,
                    "attributes": {"field": "today_work"},
                },
                {
                    "entity_id": "orchestrated-case",
                    "entity_type": "case_query",
                    "value": "这个案件有什么风险",
                    "confidence": 0.9,
                },
            ],
            "confidence": 0.93,
            "required_actions": [
                {
                    "action_id": "orchestrated-capture",
                    "action_type": "capture_daily_event",
                    "intent": "daily_append",
                    "entity_ids": ["orchestrated-daily"],
                },
                {
                    "action_id": "orchestrated-query",
                    "action_type": "answer_case_query",
                    "intent": "case_query",
                    "entity_ids": ["orchestrated-case"],
                },
            ],
            "clarification_need": None,
            "context_update": {
                "current_goal": "case_query",
                "remember_entity_ids": ["orchestrated-daily", "orchestrated-case"],
                "remember_turn": True,
            },
        }
    )
    store = InMemoryConversationStateStore()
    orchestrator = CognitiveOrchestratorV3(
        core=CognitiveCoreV3(interpreter),
        planner=CognitiveCommandPlanner(),
        state_store=store,
    )
    owner_id = uuid5(NAMESPACE_URL, "user-1")
    result = asyncio.run(
        orchestrator.process(
            _turn("m13", "完成合同审核，另外这个案件有什么风险"),
            CommandPlanningContext(
                message_id="m13",
                actor_user_id=owner_id,
                daily_snapshot=DailyReportMutationSnapshot(
                    report_id=uuid5(NAMESPACE_URL, "report-6"),
                    owner_user_id=owner_id,
                    version=4,
                    status="collecting",
                ),
            ),
        )
    )
    stored_before_execution = asyncio.run(
        store.load(user_id="user-1", conversation_id="conversation-1")
    )

    assert result.state.version == 1
    assert result.base_state.version == 0
    assert result.state_persisted is False
    assert stored_before_execution == result.base_state
    assert [command.command_type for command in result.command_plan.daily_commands] == ["append_item"]
    assert [command.command_type for command in result.command_plan.business_commands] == ["query_case_risk"]

    saved = asyncio.run(
        finalize_cognitive_state_after_execution(
            result=result,
            state_store=store,
            execution_succeeded=True,
        )
    )
    stored_after_execution = asyncio.run(
        store.load(user_id="user-1", conversation_id="conversation-1")
    )
    assert saved == result.state
    assert stored_after_execution == result.state


def test_cognitive_orchestrator_persists_no_command_clarification_immediately():
    interpreter = QueueSemanticInterpreter(
        {
            "intents": ["daily_edit"],
            "entities": [],
            "confidence": 0.6,
            "required_actions": [],
            "clarification_need": {
                "reason": "target_missing",
                "question": "Which item should be edited?",
                "candidate_entity_ids": [],
            },
            "context_update": {"current_goal": "daily_edit", "remember_turn": True},
        }
    )
    store = InMemoryConversationStateStore()
    owner_id = uuid5(NAMESPACE_URL, "clarification-owner")
    result = asyncio.run(
        CognitiveOrchestratorV3(
            core=CognitiveCoreV3(interpreter),
            planner=CognitiveCommandPlanner(),
            state_store=store,
        ).process(
            _turn("clarification-message", "edit that item"),
            CommandPlanningContext(
                message_id="clarification-message",
                actor_user_id=owner_id,
                daily_snapshot=DailyReportMutationSnapshot(
                    report_id=uuid5(NAMESPACE_URL, "clarification-report"),
                    owner_user_id=owner_id,
                    version=2,
                    status="collecting",
                ),
            ),
        )
    )

    stored = asyncio.run(store.load(user_id="user-1", conversation_id="conversation-1"))
    assert result.command_plan.daily_commands == ()
    assert result.command_plan.business_commands == ()
    assert result.command_plan.blocked_actions == ()
    assert result.state_persisted is True
    assert stored == result.state


@pytest.mark.parametrize(
    ("decision_status", "include_pending"),
    (("information_required", True), ("blocked", False)),
)
def test_cognitive_orchestrator_never_advances_state_for_enforced_nonadvancing_admission(
    decision_status: str,
    include_pending: bool,
):
    base_state = ConversationState.empty(
        user_id="user-1",
        conversation_id="conversation-1",
    )
    proposed_state = replace(base_state, version=1)
    decision = SimpleNamespace(
        admission_mode="enforced",
        admission_information_pendings=(
            (SimpleNamespace(expected_conversation_state_version=0),)
            if include_pending
            else ()
        ),
        admission_trace=SimpleNamespace(
            decisions=(SimpleNamespace(status=decision_status),)
        ),
    )

    class Core:
        async def process(self, turn, state):
            assert state == base_state
            return SimpleNamespace(decision=decision, state=proposed_state)

    class Planner:
        def plan(self, cognitive_decision, context):
            assert cognitive_decision is decision
            return SimpleNamespace(
                daily_commands=(),
                business_commands=(),
                report_commands=(),
                blocked_actions=(),
            )

    store = InMemoryConversationStateStore((base_state,))
    result = asyncio.run(
        CognitiveOrchestratorV3(
            core=Core(),
            planner=Planner(),
            state_store=store,
        ).process(
            _turn("admission-nonadvancing", "确认"),
            CommandPlanningContext(
                message_id="admission-nonadvancing",
                actor_user_id=uuid5(NAMESPACE_URL, "admission-owner"),
            ),
        )
    )

    stored = asyncio.run(
        store.load(user_id="user-1", conversation_id="conversation-1")
    )
    assert result.state == proposed_state
    assert result.base_state == base_state
    assert result.state_persisted is False
    assert stored == base_state

    finalized = asyncio.run(
        finalize_cognitive_state_after_execution(
            result=result,
            state_store=store,
            execution_succeeded=True,
        )
    )
    assert finalized == base_state
    assert asyncio.run(
        store.load(user_id="user-1", conversation_id="conversation-1")
    ) == base_state


def test_conversation_state_payload_round_trip_preserves_bound_state():
    now = datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc)
    entity = ConversationEntity(
        entity_id="roundtrip-case",
        entity_type="case",
        value="王总案件",
        confidence=0.9,
        attributes={"stage": "hearing"},
        source_context_id="context-roundtrip",
    )
    state = ConversationState(
        user_id="user-roundtrip",
        conversation_id="conversation-roundtrip",
        version=7,
        current_entities=(entity,),
        pending=(
            BoundPending(
                pending_id="pending-roundtrip",
                user_id="user-roundtrip",
                conversation_id="conversation-roundtrip",
                intent="case_update",
                action="confirm_case_update",
                entity_ids=(entity.entity_id,),
                context_id="context-roundtrip",
                created_at=now,
                expires_at=now + timedelta(minutes=15),
            ),
        ),
        user_constraints=UserConstraints(
            read_only=True,
            no_history_mutation=True,
            sources=("explicit_user_instruction",),
        ),
    )

    restored = ConversationState.from_payload(state.as_payload())

    assert restored == state


def test_typed_daily_executor_interface_does_not_accept_natural_language_or_legacy_actions():
    parameters = signature(execute_typed_agent2_daily_commands).parameters

    assert "commands" in parameters
    assert "execution_context" in parameters
    assert "raw_input" not in parameters
    assert "llm_output" not in parameters
    assert "actions" not in parameters


def test_command_planner_deletes_only_an_explicit_semantic_item_id():
    interpreter = QueueSemanticInterpreter(
        {
            "intents": ["daily_modify"],
            "entities": [
                {
                    "entity_id": "daily-target-second",
                    "entity_type": "daily_item_target",
                    "value": "第二条",
                    "confidence": 1.0,
                    "attributes": {"target_item_ids": ["item-2"]},
                }
            ],
            "confidence": 0.99,
            "required_actions": [
                {
                    "action_id": "delete-explicit-second",
                    "action_type": "delete_daily_item",
                    "intent": "daily_modify",
                    "entity_ids": ["daily-target-second"],
                }
            ],
            "clarification_need": None,
            "context_update": {"current_goal": "daily_modify"},
        }
    )
    result = asyncio.run(
        CognitiveCoreV3(interpreter).process(
            _turn("m14", "删除今天工作第2条"),
            ConversationState.empty(user_id="user-1", conversation_id="conversation-1"),
        )
    )
    owner_id = uuid5(NAMESPACE_URL, "user-1")
    plan = CognitiveCommandPlanner().plan(
        result.decision,
        CommandPlanningContext(
            message_id="m14",
            actor_user_id=owner_id,
            daily_snapshot=DailyReportMutationSnapshot(
                report_id=uuid5(NAMESPACE_URL, "report-7"),
                owner_user_id=owner_id,
                version=6,
                status="collecting",
                today_work=("alpha", "beta"),
                item_ids={"today_work": ("item-1", "item-2")},
            ),
        ),
    )

    assert [command.command_type for command in plan.daily_commands] == ["delete_item"]
    assert plan.daily_commands[0].target_item_ids == ("item-2",)
    assert plan.daily_commands[0].patch == {}
    assert plan.blocked_actions == ()


def test_command_planner_versions_explicit_edit_and_merge_commands_in_order():
    interpreter = QueueSemanticInterpreter(
        {
            "intents": ["daily_modify"],
            "entities": [
                {
                    "entity_id": "edit-target",
                    "entity_type": "daily_item_target",
                    "value": "第一条",
                    "confidence": 1.0,
                    "attributes": {"target_item_ids": ["item-1"], "replacement": "完成4份合同审核"},
                },
                {
                    "entity_id": "merge-targets",
                    "entity_type": "daily_item_target",
                    "value": "第二、三条",
                    "confidence": 1.0,
                    "attributes": {
                        "target_item_ids": ["item-2", "item-3"],
                        "replacement": "完成材料整理和服务器方案沟通",
                    },
                },
            ],
            "confidence": 0.99,
            "required_actions": [
                {
                    "action_id": "edit-explicit-first",
                    "action_type": "edit_daily_item",
                    "intent": "daily_modify",
                    "entity_ids": ["edit-target"],
                },
                {
                    "action_id": "merge-explicit-two-three",
                    "action_type": "merge_daily_items",
                    "intent": "daily_modify",
                    "entity_ids": ["merge-targets"],
                },
            ],
            "clarification_need": None,
            "context_update": {"current_goal": "daily_modify"},
        }
    )
    result = asyncio.run(
        CognitiveCoreV3(interpreter).process(
            _turn("m15", "把第一条改成完成4份合同审核，再合并第二、三条"),
            ConversationState.empty(user_id="user-1", conversation_id="conversation-1"),
        )
    )
    owner_id = uuid5(NAMESPACE_URL, "user-1")
    plan = CognitiveCommandPlanner().plan(
        result.decision,
        CommandPlanningContext(
            message_id="m15",
            actor_user_id=owner_id,
            daily_snapshot=DailyReportMutationSnapshot(
                report_id=uuid5(NAMESPACE_URL, "report-8"),
                owner_user_id=owner_id,
                version=6,
                status="collecting",
                today_work=("审核合同", "整理材料", "沟通服务器方案"),
                item_ids={"today_work": ("item-1", "item-2", "item-3")},
            ),
        ),
    )

    assert [command.command_type for command in plan.daily_commands] == ["edit_item", "merge_items"]
    assert [command.report_version for command in plan.daily_commands] == [6, 7]
    assert plan.daily_commands[0].patch == {"replacement": "完成4份合同审核"}
    assert plan.daily_commands[1].target_item_ids == ("item-2", "item-3")
    assert plan.blocked_actions == ()


def test_semantic_interpreter_receives_typed_daily_resources_for_exact_target_resolution():
    client = FakeStructuredCompletionClient(
        {
            "intents": ["daily_modify"],
            "entities": [
                {
                    "entity_id": "resource-target",
                    "entity_type": "daily_item_target",
                    "value": "第二条",
                    "confidence": 1.0,
                    "attributes": {"target_item_ids": ["item-2"]},
                }
            ],
            "confidence": 1.0,
            "required_actions": [
                {
                    "action_id": "resource-delete",
                    "action_type": "delete_daily_item",
                    "intent": "daily_modify",
                    "entity_ids": ["resource-target"],
                }
            ],
            "clarification_need": None,
            "context_update": {"current_goal": "daily_modify"},
        }
    )
    turn = CognitiveTurn(
        user_id="user-1",
        conversation_id="conversation-1",
        message_id="m16",
        text="删除第二条",
        occurred_at=datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc),
        resources={
            "daily_draft": {
                "report_id": "report-resource",
                "version": 3,
                "items": [
                    {"item_id": "item-1", "field": "today_work", "text": "alpha"},
                    {"item_id": "item-2", "field": "today_work", "text": "beta"},
                ],
            }
        },
    )
    asyncio.run(
        LLMCognitiveSemanticInterpreter(client).interpret(
            turn,
            ConversationState.empty(user_id="user-1", conversation_id="conversation-1"),
        )
    )

    prompt = client.calls[0]["user_prompt"]
    assert '"item_id": "item-2"' in prompt
    assert '"version": 3' in prompt


def test_semantic_interpretation_rejects_execution_and_database_fields():
    with pytest.raises(ValueError, match="cannot contain execution fields"):
        SemanticInterpretation.from_payload(
            {
                "intents": ["daily_append"],
                "entities": [],
                "confidence": 0.9,
                "required_actions": [],
                "clarification_need": None,
                "context_update": {},
                "should_write_db": True,
            }
        )


def test_semantic_interpreter_repairs_submit_that_was_wrongly_turned_into_pending():
    report_entity = {
        "entity_id": "current-daily-report",
        "entity_type": "daily_report",
        "value": "当前日报",
        "confidence": 1.0,
    }
    invalid = {
        "intents": ["daily_submit"],
        "entities": [report_entity],
        "confidence": 1.0,
        "required_actions": [],
        "clarification_need": {
            "reason": "high_impact_confirmation_required",
            "missing_fields": [],
            "question": "确认提交当前日报吗？",
        },
        "context_update": {
            "bind_pending": {
                "pending_id": "wrong-submit-pending",
                "intent": "daily_submit",
                "action": "submit_daily_report",
                "entity_ids": ["current-daily-report"],
                "expires_in_seconds": 600,
            }
        },
    }
    valid = {
        "intents": ["daily_submit"],
        "entities": [report_entity],
        "confidence": 1.0,
        "required_actions": [
            {
                "action_id": "submit-current-daily",
                "action_type": "submit_daily_report",
                "intent": "daily_submit",
                "entity_ids": [],
            }
        ],
        "clarification_need": None,
        "context_update": {"current_goal": "daily_submit", "remember_turn": True},
    }
    client = SequenceStructuredCompletionClient(invalid, valid)
    turn = CognitiveTurn(
        user_id="user-1",
        conversation_id="conversation-1",
        message_id="m17",
        text="提交日报",
        occurred_at=datetime(2026, 7, 10, 13, 0, tzinfo=timezone.utc),
        resources={
            "daily_draft": {
                "report_id": "report-17",
                "version": 2,
                "status": "collecting",
                "items": [],
            }
        },
    )

    result = asyncio.run(
        LLMCognitiveSemanticInterpreter(client).interpret(
            turn,
            ConversationState.empty(user_id="user-1", conversation_id="conversation-1"),
        )
    )

    assert [action.action_type for action in result.required_actions] == ["submit_daily_report"]
    assert result.context_update.bind_pending is None
    assert len(client.calls) == 2


def test_v3_planner_queries_and_copies_a_trusted_previous_daily_snapshot():
    owner_id = uuid5(NAMESPACE_URL, "daily-history-owner")
    current = DailyReportMutationSnapshot(
        report_id=uuid5(NAMESPACE_URL, "daily-current"),
        owner_user_id=owner_id,
        version=2,
        status="collecting",
        today_work=("current work",),
        item_ids={"today_work": ("current-1",)},
    )
    previous = DailyReportMutationSnapshot(
        report_id=uuid5(NAMESPACE_URL, "daily-previous"),
        owner_user_id=owner_id,
        version=5,
        status="completed",
        today_work=("previous work",),
        problems=("previous risk",),
        tomorrow_plan=("previous plan",),
        item_ids={
            "today_work": ("previous-1",),
            "problems": ("previous-2",),
            "tomorrow_plan": ("previous-3",),
        },
    )
    interpreter = QueueSemanticInterpreter(
        {
            "intents": ["daily_query", "daily_copy_previous"],
            "segments": [],
            "entities": [
                {
                    "entity_id": "previous-report",
                    "entity_type": "daily_report",
                    "value": "previous daily report",
                    "confidence": 1.0,
                    "attributes": {
                        "report_id": str(previous.report_id),
                        "version": previous.version,
                        "report_date": "2026-07-09",
                    },
                }
            ],
            "confidence": 1.0,
            "required_actions": [
                {
                    "action_id": "query-previous",
                    "action_type": "query_daily_report",
                    "intent": "daily_query",
                    "entity_ids": ["previous-report"],
                },
                {
                    "action_id": "copy-previous",
                    "action_type": "copy_previous_daily_report",
                    "intent": "daily_copy_previous",
                    "entity_ids": ["previous-report"],
                },
            ],
            "clarification_need": None,
            "context_update": {"remember_turn": True},
        }
    )
    state = ConversationState.empty(user_id="user-1", conversation_id="conversation-1")
    decision = asyncio.run(core_result := CognitiveCoreV3(interpreter).process(
        _turn("daily-history-message", "query and copy previous report"), state
    )).decision
    plan = CognitiveCommandPlanner().plan(
        decision,
        CommandPlanningContext(
            message_id="daily-history-message",
            actor_user_id=owner_id,
            daily_snapshot=current,
            daily_history=(DailySnapshotReference(date(2026, 7, 9), previous),),
        ),
    )

    assert [command.command_type for command in plan.daily_commands] == [
        "query_report",
        "copy_report",
    ]
    assert plan.daily_commands[0].report_id == previous.report_id
    assert plan.daily_commands[0].patch == {"report_date": "2026-07-09"}
    assert plan.daily_commands[1].report_id == current.report_id
    assert plan.daily_commands[1].patch == {
        "sections": {
            "today_work": ["previous work"],
            "problems": ["previous risk"],
            "tomorrow_plan": ["previous plan"],
        },
        "source_report_date": "2026-07-09",
        "source_report_id": str(previous.report_id),
    }
    assert plan.blocked_actions == ()


def test_v3_planner_rejects_clear_on_completed_report_and_maps_reopen():
    owner_id = uuid5(NAMESPACE_URL, "daily-lifecycle-owner")
    snapshot = DailyReportMutationSnapshot(
        report_id=uuid5(NAMESPACE_URL, "daily-lifecycle-report"),
        owner_user_id=owner_id,
        version=7,
        status="completed",
        today_work=("done",),
        problems=("risk",),
        tomorrow_plan=("next",),
    )
    interpreter = QueueSemanticInterpreter(
        {
            "intents": ["daily_clear", "daily_reopen"],
            "segments": [],
            "entities": [
                {
                    "entity_id": "current-report",
                    "entity_type": "daily_report",
                    "value": "current report",
                    "confidence": 1.0,
                    "attributes": {
                        "report_id": str(snapshot.report_id),
                        "version": snapshot.version,
                        "report_date": "2026-07-10",
                    },
                }
            ],
            "confidence": 1.0,
            "required_actions": [
                {
                    "action_id": "clear-confirmed",
                    "action_type": "clear_daily_report",
                    "intent": "daily_clear",
                    "entity_ids": ["current-report"],
                    "parameters": {"confirmed_pending_id": "pending-clear"},
                },
                {
                    "action_id": "reopen-current",
                    "action_type": "reopen_daily_report",
                    "intent": "daily_reopen",
                    "entity_ids": ["current-report"],
                },
            ],
            "clarification_need": None,
            "context_update": {"remember_turn": True},
        }
    )
    state = ConversationState.empty(user_id="user-1", conversation_id="conversation-1")
    decision = asyncio.run(
        CognitiveCoreV3(interpreter).process(
            _turn("daily-lifecycle-message", "confirm clear then reopen"), state
        )
    ).decision
    plan = CognitiveCommandPlanner().plan(
        decision,
        CommandPlanningContext(
            message_id="daily-lifecycle-message",
            actor_user_id=owner_id,
            daily_snapshot=snapshot,
            daily_history=(DailySnapshotReference(date(2026, 7, 10), snapshot),),
        ),
    )

    assert [command.command_type for command in plan.daily_commands] == ["reopen_report"]
    assert plan.daily_commands[0].patch == {"report_date": "2026-07-10"}
    assert plan.blocked_actions == ()
    assert decision.clarification_need is not None
    assert decision.clarification_need.reason == "pending_binding_mismatch"

    after_cutoff = CognitiveCommandPlanner().plan(
        decision,
        CommandPlanningContext(
            message_id="daily-lifecycle-message",
            actor_user_id=owner_id,
            daily_snapshot=snapshot,
            daily_history=(DailySnapshotReference(date(2026, 7, 10), snapshot),),
            current_report_date=date(2026, 7, 11),
            historical_mutation_allowed=False,
        ),
    )
    assert after_cutoff.daily_commands == ()
    assert [block.reason_code for block in after_cutoff.blocked_actions] == [
        "historical_daily_mutation_blocked_after_cutoff"
    ]


def test_v3_planner_maps_confirmed_clear_for_exact_collecting_report():
    owner_id = uuid5(NAMESPACE_URL, "daily-clear-owner")
    snapshot = DailyReportMutationSnapshot(
        report_id=uuid5(NAMESPACE_URL, "daily-clear-report"),
        owner_user_id=owner_id,
        version=3,
        status="collecting",
        today_work=("done",),
    )
    target = ConversationEntity(
        entity_id="clear-report",
        entity_type="daily_report",
        value="current report",
        confidence=1.0,
        attributes={
            "report_id": str(snapshot.report_id),
            "version": snapshot.version,
            "report_date": "2026-07-10",
        },
    )
    now = datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc)
    state = ConversationState(
        user_id="user-1",
        conversation_id="conversation-1",
        current_entities=(target,),
        pending=(
            BoundPending(
                pending_id="pending-clear",
                user_id="user-1",
                conversation_id="conversation-1",
                intent="daily_clear",
                action="clear_daily_report",
                entity_ids=(target.entity_id,),
                context_id="clear-context",
                created_at=now - timedelta(minutes=1),
                expires_at=now + timedelta(minutes=10),
            ),
        ),
    )
    interpreter = QueueSemanticInterpreter(
        {
            "intents": ["daily_clear"],
            "segments": [],
            "entities": [
                {
                    "entity_id": target.entity_id,
                    "entity_type": target.entity_type,
                    "value": target.value,
                    "confidence": target.confidence,
                    "attributes": dict(target.attributes),
                }
            ],
            "confidence": 1.0,
            "required_actions": [
                {
                    "action_id": "clear-confirmed",
                    "action_type": "continue_pending",
                    "intent": "daily_clear",
                    "entity_ids": ["clear-report"],
                    "parameters": {
                        "pending_id": "pending-clear",
                        "bound_action": "clear_daily_report",
                    },
                }
            ],
            "clarification_need": None,
            "context_update": {"remember_turn": True},
        }
    )
    core_result = asyncio.run(
        CognitiveCoreV3(interpreter).process(
            _turn("daily-clear-message", "confirm clear"),
            state,
        )
    )
    decision = core_result.decision
    plan = CognitiveCommandPlanner().plan(
        decision,
        CommandPlanningContext(
            message_id="daily-clear-message",
            actor_user_id=owner_id,
            daily_snapshot=snapshot,
            daily_history=(DailySnapshotReference(date(2026, 7, 10), snapshot),),
        ),
    )

    assert [command.command_type for command in plan.daily_commands] == ["clear_report"]
    assert plan.daily_commands[0].patch == {"field": "all"}
    assert plan.blocked_actions == ()
    assert core_result.state.pending == state.pending

    orchestration_result = SimpleNamespace(
        decision=decision,
        base_state=state,
        state=core_result.state,
        state_persisted=False,
    )
    store = InMemoryConversationStateStore((state,))
    retained = asyncio.run(
        finalize_cognitive_state_after_execution(
            result=orchestration_result,
            state_store=store,
            execution_succeeded=False,
        )
    )
    assert retained == state
    assert asyncio.run(
        store.load(user_id=state.user_id, conversation_id=state.conversation_id)
    ) == state
    finalized = asyncio.run(
        finalize_cognitive_state_after_execution(
            result=orchestration_result,
            state_store=store,
            execution_succeeded=True,
        )
    )
    assert finalized.pending == ()
    assert finalized.version == state.version + 1


@pytest.mark.parametrize(
    ("action_type", "intent", "entity_attributes", "expected_type", "expected_sections"),
    (
        (
            "clear_daily_section",
            "daily_clear",
            {"field": "problems"},
            "clear_report",
            None,
        ),
        (
            "copy_current_work_to_tomorrow",
            "daily_modify",
            {},
            "copy_report",
            {"tomorrow_plan": ["current work"]},
        ),
    ),
)
def test_v3_planner_supports_remaining_current_report_parity_actions(
    action_type,
    intent,
    entity_attributes,
    expected_type,
    expected_sections,
):
    owner_id = uuid5(NAMESPACE_URL, f"{action_type}-owner")
    snapshot = DailyReportMutationSnapshot(
        report_id=uuid5(NAMESPACE_URL, f"{action_type}-report"),
        owner_user_id=owner_id,
        version=2,
        status="collecting",
        today_work=("current work",),
        problems=("current risk",),
        tomorrow_plan=("existing plan",),
    )
    attributes = {
        "report_id": str(snapshot.report_id),
        "version": snapshot.version,
        "report_date": "2026-07-10",
        **entity_attributes,
    }
    interpreter = QueueSemanticInterpreter(
        {
            "intents": [intent],
            "segments": [],
            "entities": [
                {
                    "entity_id": "current-report",
                    "entity_type": "daily_report",
                    "value": "current report",
                    "confidence": 1.0,
                    "attributes": attributes,
                }
            ],
            "confidence": 1.0,
            "required_actions": [
                {
                    "action_id": "parity-action",
                    "action_type": action_type,
                    "intent": intent,
                    "entity_ids": ["current-report"],
                }
            ],
            "clarification_need": None,
            "context_update": {"remember_turn": True},
        }
    )
    decision = asyncio.run(
        CognitiveCoreV3(interpreter).process(
            _turn(f"{action_type}-message", action_type),
            ConversationState.empty(user_id="user-1", conversation_id="conversation-1"),
        )
    ).decision
    plan = CognitiveCommandPlanner().plan(
        decision,
        CommandPlanningContext(
            message_id=f"{action_type}-message",
            actor_user_id=owner_id,
            daily_snapshot=snapshot,
            daily_history=(DailySnapshotReference(date(2026, 7, 10), snapshot),),
        ),
    )

    assert [command.command_type for command in plan.daily_commands] == [expected_type]
    if expected_sections is None:
        assert plan.daily_commands[0].patch == {"field": "problems"}
    else:
        assert plan.daily_commands[0].patch["sections"] == expected_sections
    assert plan.blocked_actions == ()


def test_v3_planner_completes_a_trusted_previous_plan_into_today_work():
    owner_id = uuid5(NAMESPACE_URL, "complete-previous-plan-owner")
    current = DailyReportMutationSnapshot(
        report_id=uuid5(NAMESPACE_URL, "complete-previous-plan-current"),
        owner_user_id=owner_id,
        version=1,
        status="collecting",
    )
    previous = DailyReportMutationSnapshot(
        report_id=uuid5(NAMESPACE_URL, "complete-previous-plan-source"),
        owner_user_id=owner_id,
        version=4,
        status="completed",
        tomorrow_plan=("follow up filing", "prepare hearing"),
    )
    interpreter = QueueSemanticInterpreter(
        {
            "intents": ["daily_modify"],
            "segments": [],
            "entities": [
                {
                    "entity_id": "previous-report",
                    "entity_type": "daily_report",
                    "value": "previous report",
                    "confidence": 1.0,
                    "attributes": {
                        "report_id": str(previous.report_id),
                        "version": previous.version,
                        "report_date": "2026-07-09",
                    },
                }
            ],
            "confidence": 1.0,
            "required_actions": [
                {
                    "action_id": "complete-previous-plan",
                    "action_type": "complete_previous_daily_plan",
                    "intent": "daily_modify",
                    "entity_ids": ["previous-report"],
                }
            ],
            "clarification_need": None,
            "context_update": {"remember_turn": True},
        }
    )
    decision = asyncio.run(
        CognitiveCoreV3(interpreter).process(
            _turn("complete-previous-plan-message", "complete previous plan"),
            ConversationState.empty(user_id="user-1", conversation_id="conversation-1"),
        )
    ).decision
    plan = CognitiveCommandPlanner().plan(
        decision,
        CommandPlanningContext(
            message_id="complete-previous-plan-message",
            actor_user_id=owner_id,
            daily_snapshot=current,
            daily_history=(
                DailySnapshotReference(date(2026, 7, 10), current),
                DailySnapshotReference(date(2026, 7, 9), previous),
            ),
        ),
    )

    assert [command.command_type for command in plan.daily_commands] == ["copy_report"]
    assert plan.daily_commands[0].patch["sections"] == {
        "today_work": ["follow up filing", "prepare hearing"]
    }
    assert plan.blocked_actions == ()
