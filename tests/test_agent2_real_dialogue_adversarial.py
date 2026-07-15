from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from app.agent2.cognitive_core_v3 import CognitiveCoreV3, CognitiveTurn
from app.agent2.business.compiler import Phase2BusinessCommandCompiler
from app.agent2.business.contracts import BusinessCommandContext
from app.agent2.command_planner_v3 import CognitiveCommandPlanner, CommandPlanningContext
from app.agent2.conversation_state import ConversationState
from app.agent2.selection_pending import (
    SelectionCandidate,
    SelectionContext,
    SelectionPending,
    SelectionPendingResolver,
    answer_may_target_selection,
)
from app.agent2.semantic_interpreter_v3 import LLMCognitiveSemanticInterpreter


NOW = datetime(2026, 7, 13, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
PANG_ID = UUID("222b1eeb-4faa-40cf-a193-e1892c9377b0")


class _SemanticClient:
    def __init__(self, payload: dict):
        self.payload = payload

    async def complete_json(self, **_: object) -> str:
        return json.dumps(self.payload, ensure_ascii=False)


class _ValidCandidate:
    async def validate(self, *_: object):
        from app.agent2.selection_pending import SelectionCandidateValidation

        return SelectionCandidateValidation("valid")


def _turn(text: str, *, resources: dict | None = None) -> CognitiveTurn:
    return CognitiveTurn(
        user_id=f"sandbox-agent2-phase2-20260711:{PANG_ID}",
        conversation_id="pang-adversarial-corpus",
        message_id=f"adversarial-{abs(hash(text))}",
        text=text,
        occurred_at=NOW,
        resources=resources or {},
    )


def _chat_payload(text: str) -> dict:
    return {
        "intents": ["chat"],
        "segments": [
            {
                "segment_id": "model-chat",
                "text": text,
                "intents": ["chat"],
                "entity_ids": [],
                "action_ids": [],
            }
        ],
        "entities": [],
        "confidence": 1.0,
        "required_actions": [],
        "clarification_need": None,
        "context_update": {"preserve_current_goal": True, "remember_turn": True},
    }


def _case_query_payload(text: str, *, matter_hint: str = "") -> dict:
    return {
        "intents": ["case_query"],
        "segments": [
            {
                "segment_id": "case-query-segment",
                "text": text,
                "intents": ["case_query"],
                "entity_ids": ["case-query"],
                "action_ids": ["case-query-action"],
            }
        ],
        "entities": [
            {
                "entity_id": "case-query",
                "entity_type": "case_query",
                "value": text,
                "confidence": 1.0,
                "attributes": {"matter_hint": matter_hint, "question": text},
            }
        ],
        "confidence": 1.0,
        "required_actions": [
            {
                "action_id": "case-query-action",
                "action_type": "answer_case_query",
                "intent": "case_query",
                "entity_ids": ["case-query"],
                "parameters": {},
            }
        ],
        "clarification_need": None,
        "context_update": {"current_goal": "case_query", "remember_turn": True},
    }


async def _interpret(text: str, payload: dict, *, resources: dict | None = None):
    turn = _turn(text, resources=resources)
    state = ConversationState.empty(user_id=turn.user_id, conversation_id=turn.conversation_id)
    return await LLMCognitiveSemanticInterpreter(_SemanticClient(payload)).interpret(turn, state)


async def _process(text: str, payload: dict, *, resources: dict | None = None):
    turn = _turn(text, resources=resources)
    state = ConversationState.empty(user_id=turn.user_id, conversation_id=turn.conversation_id)
    return await CognitiveCoreV3(
        LLMCognitiveSemanticInterpreter(_SemanticClient(payload))
    ).process(turn, state)


@pytest.mark.parametrize(
    ("text", "matter_hint"),
    (
        ("我手上有哪些案子？", "我手上有哪些案子？"),
        ("把我负责的案件列一下", "我负责的案件"),
        ("给我看看我的案件清单", "我的案件"),
        ("我名下的全部案件", "我名下的案件"),
    ),
)
def test_assigned_case_inventory_common_phrasings_stay_on_read_only_inventory(
    text: str,
    matter_hint: str,
):
    decision = asyncio.run(
        _process(text, _case_query_payload(text, matter_hint=matter_hint))
    ).decision
    plan = CognitiveCommandPlanner().plan(
        decision,
        CommandPlanningContext(message_id=f"inventory-{abs(hash(text))}", actor_user_id=PANG_ID),
    )

    assert [command.command_type for command in plan.business_commands] == [
        "list_assigned_cases"
    ]
    assert plan.business_commands[0].execution_mode == "read_only"
    compilation = Phase2BusinessCommandCompiler().compile(
        plan.business_commands[0],
        BusinessCommandContext(
            tenant_id="sandbox-agent2-phase2-20260711",
            company_id="company-test",
            department_id="legal",
            team_id="litigation",
            actor_user_id=str(PANG_ID),
            actor_role_ids=("lawyer",),
            allowed_case_ids=(),
            source_message_id=f"inventory-{abs(hash(text))}",
            source_channel="test",
            occurred_at=NOW,
            conversation_id="pang-adversarial-corpus",
        ),
        cases=(),
    )
    assert compilation.command is not None
    assert compilation.command.command_type == "list_assigned_cases"
    assert compilation.block is None


def test_specific_case_risk_question_does_not_degrade_to_inventory():
    text = "人民西路8号院这个案子有什么风险？"
    decision = asyncio.run(
        _process(text, _case_query_payload(text, matter_hint="人民西路8号院"))
    ).decision
    plan = CognitiveCommandPlanner().plan(
        decision,
        CommandPlanningContext(message_id="specific-case-risk", actor_user_id=PANG_ID),
    )

    assert [command.command_type for command in plan.business_commands] == [
        "query_case_risk"
    ]


@pytest.mark.parametrize(
    ("text", "destination", "date_hint"),
    (
        ("我后天去南京出差", "南京", "day_after_tomorrow"),
        ("明日上午出差去昆明", "昆明", "tomorrow"),
        ("下周一准备到上海出差", "上海", "next_monday"),
        ("明天要前往杭州出差", "杭州", "tomorrow"),
    ),
)
@pytest.mark.asyncio
async def test_explicit_future_travel_phrasings_cannot_degrade_to_chat(
    text: str,
    destination: str,
    date_hint: str,
):
    decision = await _interpret(text, _chat_payload(text))

    actions = [action.action_type for action in decision.required_actions]
    travel = next(entity for entity in decision.entities if entity.entity_type == "travel_event")
    assert actions == ["record_travel_event"]
    assert travel.attributes["destination"] == destination
    assert travel.attributes["date_hint"] == date_hint


@pytest.mark.asyncio
async def test_future_visit_without_business_travel_assertion_is_not_force_written():
    text = "明天可能去南京看看"
    decision = await _interpret(text, _chat_payload(text))

    assert decision.required_actions == ()
    assert decision.intents == ("chat",)


@pytest.mark.asyncio
async def test_case_evidence_span_object_is_normalized_to_closed_integer_pair_shape():
    text = "人民西路8号院今天联系法院推进，法院表示下周重新查控。"
    payload = {
        "intents": ["case_progress"],
        "segments": [
            {
                "segment_id": "case-progress",
                "text": text,
                "intents": ["case_progress"],
                "entity_ids": ["case-1"],
                "action_ids": ["record-case-1"],
            }
        ],
        "entities": [
            {
                "entity_id": "case-1",
                "entity_type": "case_ref",
                "value": "人民西路8号院",
                "confidence": 1.0,
                "attributes": {
                    "normalized_fact": text,
                    "factual_progress": ["法院表示下周重新查控"],
                    "completed_actions": ["今天联系法院推进"],
                    "next_actions": [],
                    "blocking_issues": [],
                    "current_status": "法院表示下周重新查控",
                    "action_time_scope": "today",
                    "report_preference": "automatic",
                    "evidence_spans": [{"start": 0, "end": len(text)}],
                },
            }
        ],
        "confidence": 1.0,
        "required_actions": [
            {
                "action_id": "record-case-1",
                "action_type": "record_case_progress",
                "intent": "case_progress",
                "entity_ids": ["case-1"],
                "parameters": {},
            }
        ],
        "clarification_need": None,
        "context_update": {
            "current_goal": "case_progress",
            "remember_entity_ids": ["case-1"],
            "remember_turn": True,
        },
    }

    decision = await _interpret(text, payload)

    case = next(entity for entity in decision.entities if entity.entity_type == "case_ref")
    assert case.attributes["evidence_spans"] == [[0, len(text)]]


@pytest.mark.parametrize(
    "text",
    (
        "刚才写到案件进展里了吗？",
        "刚才那条有没有记录成案件进展？",
        "上一条记入案件进展没有？",
    ),
)
@pytest.mark.asyncio
async def test_case_operation_status_common_phrasings_query_receipts(text: str):
    decision = await _interpret(text, _chat_payload(text))

    assert decision.intents == ("case_progress_query",)
    assert [action.action_type for action in decision.required_actions] == [
        "query_operation_status"
    ]


@pytest.mark.parametrize(
    "text",
    (
        "有没有同事和我出差协同？",
        "协同现在什么状态？",
        "目前有人和我同行吗？",
    ),
)
@pytest.mark.asyncio
async def test_travel_collaboration_status_common_phrasings_query_receipts(text: str):
    decision = await _interpret(text, _chat_payload(text))

    assert decision.intents == ("travel_collaboration_query",)
    assert [action.action_type for action in decision.required_actions] == [
        "query_operation_status"
    ]


@pytest.mark.asyncio
async def test_unowned_case_reference_cannot_be_promoted_to_case_progress():
    text = "人民西路8号院正在和业主沟通调解"
    decision = await _interpret(
        text,
        _chat_payload(text),
        resources={
            "visible_cases": [
                {
                    "case_id": "other-case",
                    "case_name": "昆明乙公司建设工程纠纷案",
                    "case_number": "（2026）云01民初202号",
                    "external_case_id": "D-020",
                    "confirmed_aliases": ["昆明乙公司"],
                    "version": 1,
                }
            ]
        },
    )

    assert decision.required_actions == ()
    assert decision.intents == ("chat",)


@pytest.mark.asyncio
async def test_ambiguous_authorized_case_reference_cannot_be_force_written():
    text = "人民西路8号院正在和业主沟通调解"
    common = {
        "case_number": "",
        "external_case_id": "",
        "confirmed_aliases": ["人民西路8号院"],
        "version": 1,
    }
    decision = await _interpret(
        text,
        _chat_payload(text),
        resources={
            "visible_cases": [
                {"case_id": "case-1", "case_name": "人民西路8号院一期纠纷案", **common},
                {"case_id": "case-2", "case_name": "人民西路8号院二期纠纷案", **common},
            ]
        },
    )

    assert decision.required_actions == ()
    assert decision.intents == ("chat",)


@pytest.mark.asyncio
async def test_ambiguous_label_fragment_does_not_select_a_case():
    pending = SelectionPending(
        pending_id="pending-ambiguous-fragment",
        tenant_id="tenant-a",
        user_id="user-a",
        conversation_id="conversation-a",
        domain="case_progress",
        operation="create",
        source_turn_id="source-1",
        candidates=(
            SelectionCandidate("case-1", 1, "人民西路8号院一期纠纷案"),
            SelectionCandidate("case-2", 1, "人民西路8号院二期纠纷案"),
        ),
        acceptable_answer_forms={"第一个": "case-1", "第二个": "case-2"},
        expected_conversation_state_version=7,
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        status="active",
    )
    context = SelectionContext(
        tenant_id="tenant-a",
        user_id="user-a",
        conversation_id="conversation-a",
        conversation_state_version=7,
        source_turn_id="reply-1",
        now=NOW + timedelta(minutes=1),
    )

    assert answer_may_target_selection((pending,), "人民西路8号院") is False
    resolution = await SelectionPendingResolver().resolve(
        (pending,),
        answer="人民西路8号院",
        context=context,
        validator=_ValidCandidate(),
    )

    assert resolution.status == "clarification_required"
    assert resolution.actual_write is False
    assert resolution.selected_candidate_id == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope_change",
    (
        {"tenant_id": "tenant-b"},
        {"user_id": "liu-cong"},
        {"conversation_id": "other-conversation"},
    ),
)
async def test_short_confirmation_never_consumes_another_scope(scope_change: dict[str, str]):
    pending = SelectionPending(
        pending_id="pending-scope-fence",
        tenant_id="tenant-a",
        user_id="pang-hao",
        conversation_id="pang-conversation",
        domain="case_progress",
        operation="create",
        source_turn_id="source-1",
        candidates=(SelectionCandidate("case-1", 1, "人民西路8号院纠纷案"),),
        acceptable_answer_forms={"确认": "case-1"},
        expected_conversation_state_version=7,
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        status="active",
    )
    values = {
        "tenant_id": "tenant-a",
        "user_id": "pang-hao",
        "conversation_id": "pang-conversation",
        "conversation_state_version": 7,
        "source_turn_id": "reply-1",
        "now": NOW + timedelta(minutes=1),
    }
    values.update(scope_change)

    resolution = await SelectionPendingResolver().resolve(
        (pending,),
        answer="确认",
        context=SelectionContext(**values),
        validator=_ValidCandidate(),
    )

    assert resolution.status == "clarification_required"
    assert resolution.actual_write is False
    assert resolution.selected_candidate_id == ""
