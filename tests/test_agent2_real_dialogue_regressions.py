from __future__ import annotations

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest

from app.agent2.business.case_progress import CaseRecord
from app.agent2.business.composition import (
    Phase2BusinessComposer,
    business_composition_reply_text,
)
from app.agent2.business.contracts import BusinessCommandContext, BusinessReceipt
from app.agent2.business.executor import InMemoryBusinessExecutor
from app.agent2.business.travel import LocationRegistry
from app.agent2.case_statement_contract import assess_case_progress_statement
from app.agent2.operation_outcomes import OutcomeReplyComposer
from app.agent2.outcome_adapters import business_composition_outcomes
from app.agent2.cognitive_core_v3 import CognitiveCoreV3, CognitiveTurn
from app.agent2.cognitive_reply_v3 import build_cognitive_side_reply_v3
from app.agent2.command_planner_v3 import (
    CognitiveCommandPlanner,
    CommandPlanningContext,
    TypedBusinessCommand,
)
from app.agent2.conversation_state import ConversationGoal, ConversationState
from app.agent2.domain_admission import DomainAdmissionEngine
from app.agent2.semantic_interpreter_v3 import LLMCognitiveSemanticInterpreter


NOW = datetime(2026, 7, 13, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
ACTOR_ID = UUID("222b1eeb-4faa-40cf-a193-e1892c9377b0")


class _SemanticClient:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls = 0

    async def complete_json(self, **_: object) -> str:
        self.calls += 1
        return json.dumps(self.payload, ensure_ascii=False)


class _SequenceSemanticClient:
    def __init__(self, payloads: list[dict]):
        self.payloads = payloads
        self.calls = 0
        self.requests: list[dict[str, object]] = []

    async def complete_json(self, **kwargs: object) -> str:
        index = min(self.calls, len(self.payloads) - 1)
        self.requests.append(dict(kwargs))
        self.calls += 1
        return json.dumps(self.payloads[index], ensure_ascii=False)


def test_case_progress_without_case_name_clarifies_instead_of_crashing_on_blank_model_entity():
    text = "评估暂时不诉，暂缓诉讼，等一周后看谈判的结果重新评估"
    client = _SemanticClient(
        {
            "intents": ["case_progress"],
            "segments": [
                {
                    "segment_id": "case-progress-without-target",
                    "text": text,
                    "intents": ["case_progress"],
                    "entity_ids": ["missing-case"],
                    "action_ids": ["record-case-progress"],
                }
            ],
            "entities": [
                {
                    "entity_id": "missing-case",
                    "entity_type": "case_ref",
                    "value": "",
                    "confidence": 0.7,
                    "attributes": {
                        "statement_mode": "asserted",
                        "normalized_fact": text,
                    },
                }
            ],
            "confidence": 0.7,
            "required_actions": [
                {
                    "action_id": "record-case-progress",
                    "action_type": "record_case_progress",
                    "intent": "case_progress",
                    "entity_ids": ["missing-case"],
                    "parameters": {},
                }
            ],
            "clarification_need": None,
            "context_update": {
                "current_goal": "case_progress",
                "remember_entity_ids": ["missing-case"],
                "remember_turn": True,
            },
        }
    )
    interpreter = LLMCognitiveSemanticInterpreter(client)

    result = asyncio.run(
        CognitiveCoreV3(interpreter).process(
            _turn(text),
            ConversationState.empty(
                user_id=f"sandbox-agent2-phase2-20260711:{ACTOR_ID}",
                conversation_id="pang-real-dialogue-regression",
            ),
        )
    )

    assert client.calls == 1
    assert result.decision.entities == ()
    assert result.decision.required_actions == ()
    assert result.decision.clarification_need is not None
    assert result.decision.clarification_need.reason == "case_target_required"
    assert "案件编号" in result.decision.clarification_need.question
    assert result.state.current_goal is not None
    assert result.state.current_goal.intent == "case_progress"


def test_blank_case_entity_drops_case_action_even_when_model_mismatches_entity_ids():
    text = "评估暂时不诉，暂缓诉讼，等一周后看谈判的结果重新评估"
    client = _SemanticClient(
        {
            "intents": ["case_progress"],
            "segments": [
                {
                    "segment_id": "case-progress-with-mismatched-target",
                    "text": text,
                    "intents": ["case_progress"],
                    "entity_ids": ["blank-case"],
                    "action_ids": ["record-case-progress"],
                }
            ],
            "entities": [
                {
                    "entity_id": "blank-case",
                    "entity_type": "case_ref",
                    "value": "",
                    "confidence": 0.7,
                    "attributes": {"statement_mode": "asserted"},
                }
            ],
            "confidence": 0.7,
            "required_actions": [
                {
                    "action_id": "record-case-progress",
                    "action_type": "record_case_progress",
                    "intent": "case_progress",
                    "entity_ids": ["different-missing-case"],
                    "parameters": {},
                }
            ],
            "clarification_need": {
                "reason": "ambiguous_case_alias",
                "missing_fields": ["case_reference"],
                "question": "请告诉我具体是哪个案件。",
            },
            "context_update": {
                "current_goal": "case_progress",
                "remember_entity_ids": ["blank-case"],
                "remember_turn": True,
            },
        }
    )

    result = asyncio.run(
        CognitiveCoreV3(LLMCognitiveSemanticInterpreter(client)).process(
            _turn(text),
            ConversationState.empty(
                user_id=f"sandbox-agent2-phase2-20260711:{ACTOR_ID}",
                conversation_id="pang-real-dialogue-regression",
            ),
        )
    )

    assert client.calls == 1
    assert result.decision.entities == ()
    assert result.decision.required_actions == ()
    assert result.decision.clarification_need is not None
    assert result.decision.clarification_need.reason == "case_target_required"


def test_case_progress_edit_does_not_also_create_duplicate_progress():
    text = (
        "把星皓·锦樾项目刚才那条进展改成：评估暂时不诉，暂缓诉讼，"
        "等两周后看谈判结果重新评估"
    )
    client = _SemanticClient(
        {
            "intents": ["case_progress_update", "case_progress"],
            "segments": [
                {
                    "segment_id": "same-edit-segment",
                    "text": text,
                    "intents": ["case_progress_update", "case_progress"],
                    "entity_ids": ["existing-progress", "case-ref"],
                    "action_ids": ["update-progress", "record-progress"],
                }
            ],
            "entities": [
                {
                    "entity_id": "existing-progress",
                    "entity_type": "case_progress_ref",
                    "value": "星皓·锦樾项目刚才那条进展",
                    "confidence": 0.99,
                    "attributes": {
                        "case_hint": "星皓·锦樾项目",
                        "replacement_summary": "评估暂时不诉，暂缓诉讼，等两周后看谈判结果重新评估",
                    },
                },
                {
                    "entity_id": "case-ref",
                    "entity_type": "case_ref",
                    "value": "星皓·锦樾项目",
                    "confidence": 0.99,
                    "attributes": {
                        "statement_mode": "asserted",
                        "normalized_fact": text,
                    },
                },
            ],
            "confidence": 0.99,
            "required_actions": [
                {
                    "action_id": "update-progress",
                    "action_type": "update_case_progress",
                    "intent": "case_progress_update",
                    "entity_ids": ["existing-progress"],
                    "parameters": {},
                },
                {
                    "action_id": "record-progress",
                    "action_type": "record_case_progress",
                    "intent": "case_progress",
                    "entity_ids": ["case-ref"],
                    "parameters": {},
                },
            ],
            "clarification_need": None,
            "context_update": {
                "current_goal": "case_progress_update",
                "remember_entity_ids": ["existing-progress", "case-ref"],
                "remember_turn": True,
            },
        }
    )

    result = asyncio.run(
        CognitiveCoreV3(LLMCognitiveSemanticInterpreter(client)).process(
            _turn(text),
            ConversationState.empty(
                user_id=f"sandbox-agent2-phase2-20260711:{ACTOR_ID}",
                conversation_id="pang-real-dialogue-regression",
            ),
        )
    )

    assert client.calls == 1
    assert [item.action_type for item in result.decision.required_actions] == [
        "update_case_progress"
    ]
    assert result.decision.segments[0].action_ids == ("update-progress",)


def _turn(text: str, *, resources: dict | None = None) -> CognitiveTurn:
    return CognitiveTurn(
        user_id=f"sandbox-agent2-phase2-20260711:{ACTOR_ID}",
        conversation_id="pang-real-dialogue-regression",
        message_id=f"incident-{abs(hash(text))}",
        text=text,
        occurred_at=NOW,
        resources=resources or {},
    )


async def _case_inventory_plan_async():
    text = "我有哪些案件？"
    client = _SemanticClient(
        {
            "intents": ["case_query"],
            "segments": [
                {
                    "segment_id": "case-list",
                    "text": text,
                    "intents": ["case_query"],
                    "entity_ids": ["case-query"],
                    "action_ids": ["answer-case-query"],
                }
            ],
            "entities": [
                {
                    "entity_id": "case-query",
                    "entity_type": "case_query",
                    "value": text,
                    "confidence": 1.0,
                    "attributes": {"matter_hint": "", "question": "列出所有案件"},
                }
            ],
            "confidence": 1.0,
            "required_actions": [
                {
                    "action_id": "answer-case-query",
                    "action_type": "answer_case_query",
                    "intent": "case_query",
                    "entity_ids": ["case-query"],
                    "parameters": {},
                }
            ],
            "clarification_need": None,
            "context_update": {"current_goal": "case_query", "remember_turn": True},
        }
    )

    state = ConversationState.empty(
        user_id=f"sandbox-agent2-phase2-20260711:{ACTOR_ID}",
        conversation_id="pang-real-dialogue-regression",
    )
    core_result = await CognitiveCoreV3(LLMCognitiveSemanticInterpreter(client)).process(
        _turn(text),
        state,
    )
    return CognitiveCommandPlanner().plan(
        core_result.decision,
        CommandPlanningContext(
            message_id="incident-case-list",
            actor_user_id=ACTOR_ID,
        ),
    )


def _case_inventory_plan():
    return asyncio.run(_case_inventory_plan_async())


def test_my_assigned_cases_is_a_typed_inventory_query_not_a_party_risk_query():
    plan = _case_inventory_plan()

    assert [command.command_type for command in plan.business_commands] == [
        "list_assigned_cases"
    ]


class _VisibleCaseRepository:
    def __init__(self, cases: tuple[CaseRecord, ...]):
        self.cases = cases

    async def list_visible(self, _context: BusinessCommandContext):
        return self.cases


class _CaseInventoryExecutor:
    def __init__(self):
        self.commands = []

    async def execute(self, command, context: BusinessCommandContext):
        self.commands.append(command)
        return BusinessReceipt(
            receipt_id=str(uuid4()),
            command_id=command.command_id,
            command_type=command.command_type,
            tenant_id=context.tenant_id,
            actor_user_id=context.actor_user_id,
            source_message_id=context.source_message_id,
            idempotency_key="case-inventory-receipt",
            status="executed",
            resource_type="assigned_case_inventory",
            resource_id=context.actor_user_id,
            before={},
            after={
                "case_count": 2,
                "cases": [
                    {
                        "case_number": "（2026）云01民初101号",
                        "case_name": "昆明甲公司合同纠纷案",
                        "case_type": "plaintiff_case",
                        "stage": "litigation",
                    },
                    {
                        "case_number": "（2026）云01民初202号",
                        "case_name": "昆明乙公司建设工程纠纷案",
                        "case_type": "defendant_case",
                        "stage": "hearing",
                    },
                ],
            },
            error_code=None,
            failed_stage=None,
            actual_write=False,
            created_at=NOW,
        )


class _AsyncInMemoryBusinessExecutor:
    def __init__(self, cases: tuple[CaseRecord, ...]):
        self.delegate = InMemoryBusinessExecutor(cases=cases)

    async def execute(self, command, context: BusinessCommandContext):
        return self.delegate.execute(command, context)


class _OperationStatusExecutor:
    def __init__(self):
        self.commands = []

    async def execute(self, command, context: BusinessCommandContext):
        self.commands.append(command)
        return BusinessReceipt(
            receipt_id=str(uuid4()),
            command_id=command.command_id,
            command_type=command.command_type,
            tenant_id=context.tenant_id,
            actor_user_id=context.actor_user_id,
            source_message_id=context.source_message_id,
            idempotency_key="operation-status-receipt",
            status="executed",
            resource_type="operation_status_query",
            resource_id=context.actor_user_id,
            before={},
            after={
                "requested_domain": "case_progress",
                "answer": "没有。上一条只更新了日报，没有创建案件进展。",
            },
            error_code=None,
            failed_stage=None,
            actual_write=False,
            created_at=NOW,
        )


@pytest.mark.asyncio
async def test_assigned_case_inventory_executes_as_read_only_and_replies_without_internal_ids():
    cases = (
        CaseRecord(
            "case-private-1",
            "sandbox-agent2-phase2-20260711",
            "（2026）云01民初101号",
            "昆明甲公司合同纠纷案",
            ("昆明甲公司",),
        ),
        CaseRecord(
            "case-private-2",
            "sandbox-agent2-phase2-20260711",
            "（2026）云01民初202号",
            "昆明乙公司建设工程纠纷案",
            ("昆明乙公司",),
        ),
    )
    executor = _CaseInventoryExecutor()
    composer = Phase2BusinessComposer(
        case_repository=_VisibleCaseRepository(cases),  # type: ignore[arg-type]
        executor=executor,
    )
    context = BusinessCommandContext(
        tenant_id="sandbox-agent2-phase2-20260711",
        company_id="company-test",
        department_id="legal",
        team_id="litigation",
        actor_user_id=str(ACTOR_ID),
        actor_role_ids=("lawyer",),
        allowed_case_ids=("case-private-1", "case-private-2"),
        source_message_id="incident-case-list",
        source_channel="dingtalk",
        occurred_at=NOW,
        conversation_id="pang-real-dialogue-regression",
    )

    plan = await _case_inventory_plan_async()
    result = await composer.execute(plan.business_commands, context)

    assert result.executed_count == 1
    assert executor.commands[0].command_type == "list_assigned_cases"
    reply = business_composition_reply_text(result)
    assert "你当前负责 2 件案件" in reply
    assert "昆明甲公司合同纠纷案" in reply
    assert "昆明乙公司建设工程纠纷案" in reply
    assert "原告案件 / 诉讼中" in reply
    assert "被告案件 / 开庭" in reply
    assert "plaintiff_case" not in reply
    assert "defendant_case" not in reply
    assert "case-private" not in reply
    assert "回执" not in reply
    production_reply = OutcomeReplyComposer().compose(
        business_composition_outcomes(result)
    )
    assert "你当前负责 2 件案件" in production_reply
    assert "昆明甲公司合同纠纷案" in production_reply
    assert "昆明乙公司建设工程纠纷案" in production_reply
    assert "原告案件 / 诉讼中" in production_reply
    assert "被告案件 / 开庭" in production_reply
    assert "plaintiff_case" not in production_reply
    assert "defendant_case" not in production_reply
    assert "case-private" not in production_reply


@pytest.mark.asyncio
async def test_explicit_travel_plan_cannot_be_swallowed_by_active_daily_context():
    text = "明天计划出差昆明"
    client = _SemanticClient(
        {
            "intents": ["daily_append"],
            "segments": [
                {
                    "segment_id": "daily-only-model-output",
                    "text": text,
                    "intents": ["daily_append"],
                    "entity_ids": ["daily-plan"],
                    "action_ids": ["append-daily-plan"],
                }
            ],
            "entities": [
                {
                    "entity_id": "daily-plan",
                    "entity_type": "daily_event",
                    "value": text,
                    "confidence": 1.0,
                    "attributes": {"field": "tomorrow_plan"},
                }
            ],
            "confidence": 1.0,
            "required_actions": [
                {
                    "action_id": "append-daily-plan",
                    "action_type": "capture_daily_event",
                    "intent": "daily_append",
                    "entity_ids": ["daily-plan"],
                    "parameters": {},
                }
            ],
            "clarification_need": None,
            "context_update": {
                "current_goal": "daily_append",
                "remember_entity_ids": ["daily-plan"],
                "remember_turn": True,
            },
        }
    )
    state = ConversationState(
        user_id=f"sandbox-agent2-phase2-20260711:{ACTOR_ID}",
        conversation_id="pang-real-dialogue-regression",
        current_goal=ConversationGoal("daily_append"),
    )

    result = await CognitiveCoreV3(LLMCognitiveSemanticInterpreter(client)).process(
        _turn(text),
        state,
    )
    action_types = [action.action_type for action in result.decision.required_actions]
    plan = CognitiveCommandPlanner().plan(
        result.decision,
        CommandPlanningContext(
            message_id="incident-travel-in-daily",
            actor_user_id=ACTOR_ID,
        ),
    )

    assert action_types == ["capture_daily_event", "record_travel_event"]
    assert [command.command_type for command in plan.business_commands] == [
        "record_travel_candidate"
    ]
    travel_entity = next(
        entity for entity in result.decision.entities if entity.entity_type == "travel_event"
    )
    assert travel_entity.value == text
    assert travel_entity.attributes == {
        "destination": "昆明",
        "date_hint": "tomorrow",
        "purpose": "出差",
    }


def test_kunming_is_a_resolvable_production_travel_destination():
    resolved = LocationRegistry.default().resolve("昆明")

    assert resolved.status == "resolved"
    assert resolved.destination_normalized == "昆明市"
    assert resolved.city_code == "530100"
    assert resolved.province_code == "530000"


@pytest.mark.asyncio
async def test_unique_authorized_case_progress_cannot_be_swallowed_by_daily_context():
    text = "人民西路8号院 正在和业主沟通调解"
    client = _SemanticClient(
        {
            "intents": ["daily_append"],
            "segments": [
                {
                    "segment_id": "daily-only-case-model-output",
                    "text": text,
                    "intents": ["daily_append"],
                    "entity_ids": ["daily-case-item"],
                    "action_ids": ["append-daily-case-item"],
                }
            ],
            "entities": [
                {
                    "entity_id": "daily-case-item",
                    "entity_type": "daily_event",
                    "value": text,
                    "confidence": 1.0,
                    "attributes": {"field": "today_work"},
                }
            ],
            "confidence": 1.0,
            "required_actions": [
                {
                    "action_id": "append-daily-case-item",
                    "action_type": "capture_daily_event",
                    "intent": "daily_append",
                    "entity_ids": ["daily-case-item"],
                    "parameters": {},
                }
            ],
            "clarification_need": None,
            "context_update": {
                "current_goal": "daily_append",
                "remember_entity_ids": ["daily-case-item"],
                "remember_turn": True,
            },
        }
    )
    state = ConversationState(
        user_id=f"sandbox-agent2-phase2-20260711:{ACTOR_ID}",
        conversation_id="pang-real-dialogue-regression",
        current_goal=ConversationGoal("daily_append"),
    )

    result = await CognitiveCoreV3(LLMCognitiveSemanticInterpreter(client)).process(
        _turn(
            text,
            resources={
                "visible_cases": [
                    {
                        "case_id": "a0cb75be-72fb-4fd1-9ec0-5ebf2572e276",
                        "case_name": "人民西路8号院物业服务合同纠纷案",
                        "case_number": "（2026）云0102民初1888号",
                        "external_case_id": "P-018",
                        "confirmed_aliases": ["人民西路8号院"],
                        "version": 1,
                    }
                ]
            },
        ),
        state,
    )
    action_types = [action.action_type for action in result.decision.required_actions]

    assert action_types == ["capture_daily_event", "record_case_progress"]
    case_entity = next(
        entity for entity in result.decision.entities if entity.entity_type == "case_ref"
    )
    assert case_entity.value == "人民西路8号院"
    assert case_entity.attributes["statement_mode"] == "asserted"
    assert case_entity.attributes["normalized_fact"] == text
    assert case_entity.attributes["evidence_spans"] == [[0, len(text)]]


@pytest.mark.asyncio
async def test_named_case_future_hearing_is_planned_as_case_progress_when_model_calls_it_chat():
    text = "恒大翡翠华庭 后天开庭"
    client = _SemanticClient(
        {
            "intents": ["chat"],
            "segments": [
                {
                    "segment_id": "incorrect-chat-hearing",
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
    )
    state = ConversationState.empty(
        user_id=f"sandbox-agent2-phase2-20260711:{ACTOR_ID}",
        conversation_id="pang-real-dialogue-regression",
    )

    result = await CognitiveCoreV3(LLMCognitiveSemanticInterpreter(client)).process(
        _turn(
            text,
            resources={
                "visible_cases": [
                    {
                        "case_id": "58cb60bc-d084-4805-a5bc-c03ecebf2a08",
                        "case_name": "幕墙事业部扬州恒大建设工程施工合同纠纷",
                        "case_number": "（2026）苏1002民初1888号",
                        "external_case_id": "D-018",
                        "confirmed_aliases": ["恒大翡翠华庭"],
                        "version": 1,
                    }
                ]
            },
        ),
        state,
    )
    plan = CognitiveCommandPlanner().plan(
        result.decision,
        CommandPlanningContext(
            message_id="incident-named-future-hearing",
            actor_user_id=ACTOR_ID,
        ),
    )

    assert [action.action_type for action in result.decision.required_actions] == [
        "record_case_progress"
    ]
    assert [command.command_type for command in plan.business_commands] == [
        "record_case_progress_candidate"
    ]
    cases = (
        CaseRecord(
            "58cb60bc-d084-4805-a5bc-c03ecebf2a08",
            "sandbox-agent2-phase2-20260711",
            "（2026）苏1002民初1888号",
            "幕墙事业部扬州恒大建设工程施工合同纠纷",
            (),
            confirmed_aliases=("恒大翡翠华庭",),
        ),
    )
    executor = _AsyncInMemoryBusinessExecutor(cases)
    composition = await Phase2BusinessComposer(
        case_repository=_VisibleCaseRepository(cases),  # type: ignore[arg-type]
        executor=executor,
    ).execute(
        plan.business_commands,
        BusinessCommandContext(
            tenant_id="sandbox-agent2-phase2-20260711",
            company_id="company-test",
            department_id="legal",
            team_id="litigation",
            actor_user_id=str(ACTOR_ID),
            actor_role_ids=("lawyer",),
            allowed_case_ids=("58cb60bc-d084-4805-a5bc-c03ecebf2a08",),
            source_message_id="incident-named-future-hearing",
            source_channel="dingtalk",
            occurred_at=NOW,
            conversation_id="pang-real-dialogue-regression",
        ),
    )

    assert composition.executed_count == 1
    assert composition.actions[0].receipt is not None
    assert composition.actions[0].receipt.actual_write is True
    assert next(iter(executor.delegate.case_progress.values())).summary == text


@pytest.mark.asyncio
async def test_new_case_progress_accepts_duplicate_grounded_case_hint_from_model():
    text = "恒大翡翠华庭 与法官沟通了案件进展"
    client = _SemanticClient(
        {
            "intents": ["case_progress"],
            "segments": [
                {
                    "segment_id": "real-dingtalk-case-progress",
                    "text": text,
                    "intents": ["case_progress"],
                    "entity_ids": ["case-ref-1"],
                    "action_ids": ["record-case-progress-1"],
                }
            ],
            "entities": [
                {
                    "entity_id": "case-ref-1",
                    "entity_type": "case_ref",
                    "value": "恒大翡翠华庭",
                    "confidence": 1.0,
                    "attributes": {
                        "case_hint": "恒大翡翠华庭",
                        "statement_mode": "asserted",
                        "factual_progress": ["与法官沟通了案件进展"],
                        "normalized_fact": text,
                        "evidence_spans": [[0, len(text)]],
                    },
                }
            ],
            "confidence": 1.0,
            "required_actions": [
                {
                    "action_id": "record-case-progress-1",
                    "action_type": "record_case_progress",
                    "intent": "case_progress",
                    "entity_ids": ["case-ref-1"],
                    "parameters": {},
                }
            ],
            "clarification_need": None,
            "context_update": {
                "current_goal": "case_progress",
                "remember_entity_ids": ["case-ref-1"],
                "remember_turn": True,
            },
        }
    )

    interpretation = await LLMCognitiveSemanticInterpreter(client).interpret(
        _turn(
            text,
            resources={
                "visible_cases": [
                    {
                        "case_id": "58cb60bc-d084-4805-a5bc-c03ecebf2a08",
                        "case_name": "幕墙事业部扬州恒大建设工程施工合同纠纷",
                        "case_number": "（2026）苏1002民初1888号",
                        "external_case_id": "D-018",
                        "confirmed_aliases": ["恒大翡翠华庭"],
                        "version": 1,
                    }
                ]
            },
        ),
        ConversationState.empty(
            user_id=f"sandbox-agent2-phase2-20260711:{ACTOR_ID}",
            conversation_id="pang-real-dialogue-regression",
        ),
    )

    case_entity = next(
        entity for entity in interpretation.entities if entity.entity_type == "case_ref"
    )
    assert case_entity.value == "恒大翡翠华庭"
    assert "case_hint" not in case_entity.attributes
    assert client.calls == 1


@pytest.mark.asyncio
async def test_asserted_named_case_fact_repairs_missing_statement_mode():
    text = "恒大翡翠华庭 后天开庭"
    client = _SemanticClient(
        {
            "intents": ["case_progress"],
            "segments": [
                {
                    "segment_id": "future-hearing-progress",
                    "text": text,
                    "intents": ["case_progress"],
                    "entity_ids": ["case-ref-1"],
                    "action_ids": ["record-case-progress-1"],
                }
            ],
            "entities": [
                {
                    "entity_id": "case-ref-1",
                    "entity_type": "case_ref",
                    "value": "恒大翡翠华庭",
                    "confidence": 1.0,
                    "attributes": {
                        "case_stage": "hearing",
                        "action_time_scope": "future",
                        "hearing_readiness": "scheduled",
                        "evidence_spans": [[0, 6], [7, len(text)]],
                    },
                }
            ],
            "confidence": 1.0,
            "required_actions": [
                {
                    "action_id": "record-case-progress-1",
                    "action_type": "record_case_progress",
                    "intent": "case_progress",
                    "entity_ids": ["case-ref-1"],
                    "parameters": {},
                }
            ],
            "clarification_need": None,
            "context_update": {
                "current_goal": "case_progress",
                "remember_entity_ids": ["case-ref-1"],
                "remember_turn": True,
            },
        }
    )

    interpretation = await LLMCognitiveSemanticInterpreter(
        client,
        legacy_semantic_enforcers_enabled=False,
    ).interpret(
        _turn(
            text,
            resources={
                "visible_cases": [
                    {
                        "case_id": "58cb60bc-d084-4805-a5bc-c03ecebf2a08",
                        "case_name": "幕墙事业部扬州恒大建设工程施工合同纠纷",
                        "case_number": "（2026）苏1002民初1888号",
                        "external_case_id": "D-018",
                        "confirmed_aliases": ["恒大翡翠华庭"],
                        "version": 1,
                    }
                ]
            },
        ),
        ConversationState.empty(
            user_id=f"sandbox-agent2-phase2-20260711:{ACTOR_ID}",
            conversation_id="pang-real-dialogue-regression",
        ),
    )

    case_entity = next(
        entity for entity in interpretation.entities if entity.entity_type == "case_ref"
    )
    assert case_entity.attributes["statement_mode"] == "asserted"
    assert client.calls == 1


@pytest.mark.asyncio
async def test_named_case_completed_communication_is_progress_when_model_calls_it_chat():
    text = "恒大翡翠华庭 与法官沟通了案件进展"
    client = _SemanticClient(
        {
            "intents": ["chat"],
            "segments": [
                {
                    "segment_id": "incorrect-chat-completed-case-work",
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
    )

    result = await CognitiveCoreV3(LLMCognitiveSemanticInterpreter(client)).process(
        _turn(
            text,
            resources={
                "visible_cases": [
                    {
                        "case_id": "58cb60bc-d084-4805-a5bc-c03ecebf2a08",
                        "case_name": "幕墙事业部扬州恒大建设工程施工合同纠纷",
                        "case_number": "（2026）苏1002民初1888号",
                        "external_case_id": "D-018",
                        "confirmed_aliases": ["恒大翡翠华庭"],
                        "version": 1,
                    }
                ]
            },
        ),
        ConversationState.empty(
            user_id=f"sandbox-agent2-phase2-20260711:{ACTOR_ID}",
            conversation_id="pang-real-dialogue-regression",
        ),
    )

    assert [action.action_type for action in result.decision.required_actions] == [
        "record_case_progress"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    (
        "恒大翡翠华庭 后天开庭",
        "恒大翡翠华庭 与分公司核对了材料",
        "恒大翡翠华庭 找了当地资源",
        "恒大翡翠华庭 委托了当地律师",
    ),
)
async def test_enforced_runtime_reassesses_action_free_named_case_work(text: str):
    chat_payload = {
        "intents": ["chat"],
        "segments": [
            {
                "segment_id": "incorrect-action-free-case-turn",
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
    corrected_payload = {
        "intents": ["case_progress"],
        "segments": [
            {
                "segment_id": "reassessed-case-work",
                "text": text,
                "intents": ["case_progress"],
                "entity_ids": ["case-ref-1"],
                "action_ids": ["record-case-progress-1"],
            }
        ],
        "entities": [
            {
                "entity_id": "case-ref-1",
                "entity_type": "case_ref",
                "value": "恒大翡翠华庭",
                "confidence": 1.0,
                "attributes": {
                    "case_hint": "恒大翡翠华庭",
                    "statement_mode": "asserted",
                    "factual_progress": [text],
                    "evidence_spans": [[0, len(text)]],
                },
            }
        ],
        "confidence": 1.0,
        "required_actions": [
            {
                "action_id": "record-case-progress-1",
                "action_type": "record_case_progress",
                "intent": "case_progress",
                "entity_ids": ["case-ref-1"],
                "parameters": {},
            }
        ],
        "clarification_need": None,
        "context_update": {
            "current_goal": "case_progress",
            "remember_entity_ids": ["case-ref-1"],
            "remember_turn": True,
        },
    }
    client = _SequenceSemanticClient([chat_payload, corrected_payload])

    interpretation = await LLMCognitiveSemanticInterpreter(
        client,
        legacy_semantic_enforcers_enabled=False,
    ).interpret(
        _turn(
            text,
            resources={
                "visible_cases": [
                    {
                        "case_id": "58cb60bc-d084-4805-a5bc-c03ecebf2a08",
                        "case_name": "幕墙事业部扬州恒大建设工程施工合同纠纷",
                        "case_number": "（2026）苏1002民初1888号",
                        "external_case_id": "D-018",
                        "confirmed_aliases": ["恒大翡翠华庭"],
                        "version": 1,
                    }
                ]
            },
        ),
        ConversationState.empty(
            user_id=f"sandbox-agent2-phase2-20260711:{ACTOR_ID}",
            conversation_id="pang-real-dialogue-regression",
        ),
    )

    assert [action.action_type for action in interpretation.required_actions] == [
        "record_case_progress"
    ]
    assert client.calls == 2


@pytest.mark.asyncio
async def test_named_case_travel_plan_reassesses_only_the_missing_case_progress_facet():
    text = "明天出差去南京沟通鑫瑞达回款事宜"
    travel_and_daily_payload = {
        "intents": ["travel_event", "daily_append"],
        "segments": [
            {
                "segment_id": "travel-and-daily",
                "text": text,
                "intents": ["travel_event", "daily_append"],
                "entity_ids": ["travel-1", "daily-1"],
                "action_ids": ["record-travel-1", "capture-daily-1"],
            }
        ],
        "entities": [
            {
                "entity_id": "travel-1",
                "entity_type": "travel_event",
                "value": text,
                "confidence": 1.0,
                "attributes": {
                    "destination": "南京",
                    "date_hint": "明天",
                    "purpose": "沟通鑫瑞达回款事宜",
                    "statement_mode": "asserted",
                    "traveler_scope": "self",
                    "evidence_spans": [[0, len(text)]],
                },
            },
            {
                "entity_id": "daily-1",
                "entity_type": "daily_event",
                "value": text,
                "confidence": 1.0,
                "attributes": {"field": "tomorrow_plan"},
            },
        ],
        "confidence": 1.0,
        "required_actions": [
            {
                "action_id": "record-travel-1",
                "action_type": "record_travel_event",
                "intent": "travel_event",
                "entity_ids": ["travel-1"],
            },
            {
                "action_id": "capture-daily-1",
                "action_type": "capture_daily_event",
                "intent": "daily_append",
                "entity_ids": ["daily-1"],
            },
        ],
        "clarification_need": None,
        "context_update": {"current_goal": "travel_event", "remember_turn": True},
    }
    case_payload = {
        "intents": ["travel_event", "case_progress"],
        "segments": [
            {
                "segment_id": "xinruida-case-plan",
                "text": text,
                "intents": ["travel_event", "case_progress"],
                "entity_ids": ["repeated-travel", "xinruida-case-ref"],
                "action_ids": ["repeat-travel", "record-xinruida-progress"],
            }
        ],
        "entities": [
            {
                "entity_id": "repeated-travel",
                "entity_type": "travel_event",
                "value": text,
                "confidence": 1.0,
                "attributes": {
                    "destination": "南京",
                    "date_hint": "明天",
                    "purpose": "沟通鑫瑞达回款事宜",
                    "statement_mode": "asserted",
                    "traveler_scope": "self",
                    "evidence_spans": [[0, len(text)]],
                },
            },
            {
                "entity_id": "xinruida-case-ref",
                "entity_type": "case_ref",
                "value": "鑫瑞达",
                "confidence": 1.0,
                "attributes": {
                    "case_id": "1a4558a7-7a98-5db4-a53f-9f465cd2235d",
                    "case_number": "SSGL-2505-0022",
                    "statement_mode": "asserted",
                    "normalized_fact": text,
                    "factual_progress": [text],
                    "next_actions": [text],
                    "action_time_scope": "future",
                    "evidence_spans": [[0, len(text)]],
                },
            }
        ],
        "confidence": 1.0,
        "required_actions": [
            {
                "action_id": "repeat-travel",
                "action_type": "record_travel_event",
                "intent": "travel_event",
                "entity_ids": ["repeated-travel"],
            },
            {
                "action_id": "record-xinruida-progress",
                "action_type": "record_case_progress",
                "intent": "case_progress",
                "entity_ids": ["xinruida-case-ref"],
            }
        ],
        "clarification_need": None,
        "context_update": {
            "current_goal": "case_progress",
            "remember_entity_ids": ["xinruida-case-ref"],
            "remember_turn": True,
        },
    }
    client = _SequenceSemanticClient([travel_and_daily_payload, case_payload])

    interpretation = await LLMCognitiveSemanticInterpreter(
        client,
        legacy_semantic_enforcers_enabled=False,
    ).interpret(
        _turn(
            text,
            resources={
                "visible_cases": [
                    {
                        "case_id": "1a4558a7-7a98-5db4-a53f-9f465cd2235d",
                        "case_name": "四川鑫瑞达房地产开发有限责任公司质保金再审案",
                        "case_number": "SSGL-2505-0022",
                        "external_case_id": "plaintiff:SSGL-2505-0022",
                        "confirmed_aliases": [],
                        "version": 1,
                    }
                ]
            },
        ),
        ConversationState.empty(
            user_id=f"sandbox-agent2-phase2-20260711:{ACTOR_ID}",
            conversation_id="pang-real-dialogue-regression",
        ),
    )

    assert [action.action_type for action in interpretation.required_actions] == [
        "record_travel_event",
        "capture_daily_event",
        "record_case_progress",
    ]
    assert interpretation.intents == (
        "travel_event",
        "daily_append",
        "case_progress",
    )
    assert client.calls == 2


@pytest.mark.asyncio
async def test_named_case_travel_plan_reassesses_the_missing_daily_plan_facet():
    text = "明天出差去南京沟通鑫瑞达回款事宜"
    travel_and_case_payload = {
        "intents": ["travel_event", "case_progress"],
        "segments": [
            {
                "segment_id": "travel-and-case",
                "text": text,
                "intents": ["travel_event", "case_progress"],
                "entity_ids": ["travel-1", "xinruida-case-ref"],
                "action_ids": ["record-travel-1", "record-xinruida-progress"],
            }
        ],
        "entities": [
            {
                "entity_id": "travel-1",
                "entity_type": "travel_event",
                "value": text,
                "confidence": 1.0,
                "attributes": {
                    "destination": "南京",
                    "date_hint": "明天",
                    "purpose": "沟通鑫瑞达回款事宜",
                    "statement_mode": "asserted",
                    "traveler_scope": "self",
                    "evidence_spans": [[0, len(text)]],
                },
            },
            {
                "entity_id": "xinruida-case-ref",
                "entity_type": "case_ref",
                "value": "鑫瑞达",
                "confidence": 1.0,
                "attributes": {
                    "statement_mode": "asserted",
                    "normalized_fact": text,
                    "factual_progress": [text],
                    "next_actions": [text],
                    "action_time_scope": "future",
                    "evidence_spans": [[0, len(text)]],
                },
            },
        ],
        "confidence": 1.0,
        "required_actions": [
            {
                "action_id": "record-travel-1",
                "action_type": "record_travel_event",
                "intent": "travel_event",
                "entity_ids": ["travel-1"],
            },
            {
                "action_id": "record-xinruida-progress",
                "action_type": "record_case_progress",
                "intent": "case_progress",
                "entity_ids": ["xinruida-case-ref"],
            },
        ],
        "clarification_need": None,
        "context_update": {"current_goal": "case_progress", "remember_turn": True},
    }
    daily_payload = {
        "intents": ["daily_append"],
        "segments": [
            {
                "segment_id": "xinruida-daily-plan",
                "text": text,
                "intents": ["daily_append"],
                "entity_ids": ["daily-1"],
                "action_ids": ["capture-daily-1"],
            }
        ],
        "entities": [
            {
                "entity_id": "daily-1",
                "entity_type": "daily_event",
                "value": text,
                "confidence": 1.0,
                "attributes": {"field": "tomorrow_plan"},
            }
        ],
        "confidence": 1.0,
        "required_actions": [
            {
                "action_id": "capture-daily-1",
                "action_type": "capture_daily_event",
                "intent": "daily_append",
                "entity_ids": ["daily-1"],
            }
        ],
        "clarification_need": None,
        "context_update": {
            "current_goal": "daily_report",
            "remember_entity_ids": ["daily-1"],
            "remember_turn": True,
        },
    }
    client = _SequenceSemanticClient([travel_and_case_payload, daily_payload])

    interpretation = await LLMCognitiveSemanticInterpreter(
        client,
        legacy_semantic_enforcers_enabled=False,
    ).interpret(
        _turn(
            text,
            resources={
                "visible_cases": [
                    {
                        "case_id": "1a4558a7-7a98-5db4-a53f-9f465cd2235d",
                        "case_name": "四川鑫瑞达房地产开发有限责任公司质保金再审案",
                        "case_number": "SSGL-2505-0022",
                        "external_case_id": "plaintiff:SSGL-2505-0022",
                        "confirmed_aliases": [],
                        "version": 1,
                    }
                ]
            },
        ),
        ConversationState.empty(
            user_id=f"sandbox-agent2-phase2-20260711:{ACTOR_ID}",
            conversation_id="pang-real-dialogue-regression",
        ),
    )

    assert [action.action_type for action in interpretation.required_actions] == [
        "record_travel_event",
        "record_case_progress",
        "capture_daily_event",
    ]
    assert client.calls == 1


@pytest.mark.asyncio
async def test_structured_daily_document_merges_one_embedded_case_fact_at_its_source_segment():
    case_item = "鑫瑞达今天与对方沟通回款，对方表示下周反馈"
    text = (
        "【今日完成】\n"
        "1. 优化了法务中台案件页面；\n"
        f"2. {case_item}；\n"
        "3. 完成两份合同审核；\n"
        "【风险与问题】\n"
        "1. 暂无；\n"
        "【明日计划】\n"
        "1. 继续完善案件看板；\n"
        "2. 跟进招聘事项"
    )
    initial_payload = {
        "intents": ["monthly_report"],
        "segments": [
            {
                "segment_id": "stale-monthly-output",
                "text": text,
                "intents": ["monthly_report"],
                "entity_ids": [],
                "action_ids": [],
            }
        ],
        "entities": [],
        "confidence": 0.9,
        "required_actions": [],
        "clarification_need": None,
        "context_update": {"current_goal": "monthly_report", "remember_turn": True},
    }
    case_payload = {
        "intents": ["case_progress"],
        "segments": [
            {
                "segment_id": "embedded-xinruida-case-fact",
                "text": case_item,
                "intents": ["case_progress"],
                "entity_ids": ["embedded-xinruida-ref"],
                "action_ids": ["record-embedded-xinruida-progress"],
            }
        ],
        "entities": [
            {
                "entity_id": "embedded-xinruida-ref",
                "entity_type": "case_ref",
                "value": "鑫瑞达",
                "confidence": 0.99,
                "attributes": {
                    "statement_mode": "asserted",
                    "action_time_scope": "today",
                    "factual_progress": [case_item],
                    "normalized_fact": case_item,
                    "evidence_spans": [
                        # Deliberately provider-shaped/unreliable coordinates;
                        # the exact structured report item is authoritative.
                        [30, 49]
                    ],
                },
            }
        ],
        "confidence": 0.99,
        "required_actions": [
            {
                "action_id": "record-embedded-xinruida-progress",
                "action_type": "record_case_progress",
                "intent": "case_progress",
                "entity_ids": ["embedded-xinruida-ref"],
                "parameters": {},
            }
        ],
        "clarification_need": None,
        "context_update": {
            "current_goal": "case_progress",
            "remember_entity_ids": ["embedded-xinruida-ref"],
            "remember_turn": True,
        },
    }
    visible_case = {
        "case_id": "1a4558a7-7a98-5db4-a53f-9f465cd2235d",
        "case_number": "SSGL-2505-0022",
        "case_name": "四川鑫瑞达房地产开发有限责任公司质保金再审案",
        "external_case_id": "plaintiff:SSGL-2505-0022",
        "confirmed_aliases": [],
        "version": 1,
    }
    client = _SequenceSemanticClient([initial_payload, case_payload])
    turn = CognitiveTurn(
        tenant_id="sandbox-agent2-phase2-20260711",
        user_id=f"sandbox-agent2-phase2-20260711:{ACTOR_ID}",
        actor_user_id=str(ACTOR_ID),
        conversation_id="pang-embedded-daily-case-regression",
        message_id="pang-embedded-daily-case-regression",
        text=text,
        occurred_at=NOW,
        resources={"visible_cases": [visible_case]},
    )
    state = ConversationState.empty(
        user_id=turn.user_id,
        conversation_id=turn.conversation_id,
    )

    interpretation = await LLMCognitiveSemanticInterpreter(
        client,
        legacy_semantic_enforcers_enabled=False,
    ).interpret(turn, state)

    assert client.calls == 2
    focused_prompt = str(client.requests[1]["user_prompt"])
    assert case_item in focused_prompt
    assert "优化了法务中台案件页面" not in focused_prompt
    assert "跟进招聘事项" not in focused_prompt
    action_types = [item.action_type for item in interpretation.required_actions]
    assert action_types.count("capture_daily_event") == 6
    assert action_types.count("record_case_progress") == 1
    matching_segments = [
        item for item in interpretation.segments if "case_progress" in item.intents
    ]
    assert len(matching_segments) == 1
    assert matching_segments[0].text == case_item
    case_entity = next(
        item for item in interpretation.entities if item.entity_type == "case_ref"
    )
    assert case_entity.attributes["normalized_fact"] == case_item


@pytest.mark.asyncio
async def test_structured_daily_document_reassesses_each_distinct_case_item_independently():
    xinruida_item = "鑫瑞达今天与对方沟通回款，对方表示下周反馈"
    binhai_item = "滨海医院今天向法院提交了补充材料"
    text = (
        "【今日完成】\n"
        f"1. {xinruida_item}；\n"
        f"2. {binhai_item}；\n"
        "3. 完成两份合同审核；\n"
        "【风险与问题】\n"
        "1. 暂无；\n"
        "【明日计划】\n"
        "1. 跟进招聘事项"
    )
    initial_payload = {
        "intents": ["daily_append"],
        "segments": [
            {
                "segment_id": "model-daily-document",
                "text": text,
                "intents": ["daily_append"],
                "entity_ids": [],
                "action_ids": [],
            }
        ],
        "entities": [],
        "confidence": 1.0,
        "required_actions": [],
        "clarification_need": None,
        "context_update": {"current_goal": "daily_report", "remember_turn": True},
    }

    def case_payload(*, item: str, reference: str, suffix: str) -> dict:
        return {
            "intents": ["case_progress"],
            "segments": [
                {
                    "segment_id": f"case-segment-{suffix}",
                    "text": item,
                    "intents": ["case_progress"],
                    "entity_ids": [f"case-ref-{suffix}"],
                    "action_ids": [f"case-action-{suffix}"],
                }
            ],
            "entities": [
                {
                    "entity_id": f"case-ref-{suffix}",
                    "entity_type": "case_ref",
                    "value": reference,
                    "confidence": 0.99,
                    "attributes": {
                        "statement_mode": "asserted",
                        "action_time_scope": "today",
                        "normalized_fact": item,
                        "evidence_spans": [[0, len(item)]],
                    },
                }
            ],
            "confidence": 0.99,
            "required_actions": [
                {
                    "action_id": f"case-action-{suffix}",
                    "action_type": "record_case_progress",
                    "intent": "case_progress",
                    "entity_ids": [f"case-ref-{suffix}"],
                    "parameters": {},
                }
            ],
            "clarification_need": None,
            "context_update": {
                "current_goal": "case_progress",
                "remember_entity_ids": [f"case-ref-{suffix}"],
                "remember_turn": True,
            },
        }

    visible_cases = [
        {
            "case_id": "1a4558a7-7a98-5db4-a53f-9f465cd2235d",
            "case_number": "SSGL-2505-0022",
            "case_name": "四川鑫瑞达房地产开发有限责任公司质保金再审案",
            "external_case_id": "plaintiff:SSGL-2505-0022",
            "confirmed_aliases": [],
            "version": 1,
        },
        {
            "case_id": "8961e207-da0d-5cad-bb8b-a6f2b596f542",
            "case_number": "SSGL-2603-0007",
            "case_name": (
                "股份三分（天津）天津市滨海新区妇女儿童医院生态城院区工程"
                "精装修工程2标段施工合同纠纷"
            ),
            "external_case_id": "plaintiff:SSGL-2603-0007",
            "confirmed_aliases": [],
            "version": 1,
        },
    ]
    client = _SequenceSemanticClient(
        [
            initial_payload,
            case_payload(item=xinruida_item, reference="鑫瑞达", suffix="xinruida"),
            case_payload(item=binhai_item, reference="滨海医院", suffix="binhai"),
        ]
    )
    turn = CognitiveTurn(
        tenant_id="sandbox-agent2-phase2-20260711",
        user_id=f"sandbox-agent2-phase2-20260711:{ACTOR_ID}",
        actor_user_id=str(ACTOR_ID),
        conversation_id="pang-two-case-daily-regression",
        message_id="pang-two-case-daily-regression",
        text=text,
        occurred_at=NOW,
        resources={"visible_cases": visible_cases},
    )
    state = ConversationState.empty(
        user_id=turn.user_id,
        conversation_id=turn.conversation_id,
    )

    interpretation = await LLMCognitiveSemanticInterpreter(
        client,
        legacy_semantic_enforcers_enabled=False,
    ).interpret(turn, state)

    assert client.calls == 3
    action_types = [item.action_type for item in interpretation.required_actions]
    assert action_types.count("capture_daily_event") == 5
    assert action_types.count("record_case_progress") == 2
    case_segments = [
        item.text for item in interpretation.segments if "case_progress" in item.intents
    ]
    assert case_segments == [xinruida_item, binhai_item]
    case_facts = [
        item.attributes["normalized_fact"]
        for item in interpretation.entities
        if item.entity_type == "case_ref"
    ]
    assert case_facts == [xinruida_item, binhai_item]


@pytest.mark.asyncio
async def test_action_free_ambiguous_case_progress_is_reassessed_into_selection() -> None:
    text = "·海西高新今日与原告沟通，对方坚持诉状金额，暂未答应"
    chat_payload = {
        "intents": ["chat"],
        "segments": [
            {
                "segment_id": "incorrect-action-free-haixi-turn",
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
    selection_payload = {
        "intents": ["case_progress"],
        "segments": [
            {
                "segment_id": "ambiguous-haixi-case-progress",
                "text": text,
                "intents": ["case_progress"],
                "entity_ids": ["haixi-case-ref"],
                "action_ids": ["record-haixi-case-progress"],
            }
        ],
        "entities": [
            {
                "entity_id": "haixi-case-ref",
                "entity_type": "case_ref",
                "value": "海西高新",
                "confidence": 0.99,
                "attributes": {
                    "statement_mode": "asserted",
                    "action_time_scope": "today",
                    "completed_actions": ["与原告沟通"],
                    "current_status": "对方坚持诉状金额，暂未答应",
                    "normalized_fact": "模型改写后的不可信摘要",
                    "evidence_spans": [[999, 1000]],
                },
            }
        ],
        "confidence": 0.99,
        "required_actions": [
            {
                "action_id": "record-haixi-case-progress",
                "action_type": "record_case_progress",
                "intent": "case_progress",
                "entity_ids": ["haixi-case-ref"],
                "parameters": {},
            }
        ],
        "clarification_need": {
            "reason": "ambiguous_case_alias",
            "missing_fields": ["case_id"],
            "question": (
                "海西高新对应两起案件：SSGL-2603-0013（五期）和"
                "SSGL-2603-0014（三期），请问您指哪一起？"
            ),
        },
        "context_update": {
            "current_goal": "case_progress",
            "remember_entity_ids": ["haixi-case-ref"],
            "remember_turn": True,
        },
    }
    client = _SequenceSemanticClient([chat_payload, selection_payload])
    visible_cases = [
        {
            "case_id": "a7779c97-769b-58e8-b2df-56aa3589b60d",
            "case_name": (
                "美瑞德+东湖·海西高新技术企业港软件开发基地（数字福建VR"
                "“双创“产业孵化基地）五期（C1-2地块）12#、15#、16#装修工程"
                "+装修合同纠纷"
            ),
            "case_number": "SSGL-2603-0013",
            "external_case_id": "plaintiff:SSGL-2603-0013",
            "confirmed_aliases": ["海西高新"],
            "version": 1,
        },
        {
            "case_id": "88394e9e-cbe7-5246-9b94-3d2c98cd465e",
            "case_name": (
                "美瑞德+东湖·海西高新技术企业港软件开发基地（数字福建VR"
                "“双创“产业孵化基地）三期（C1-2地块）1#FFC 16F、19F、20F"
                "装修工程（EPC）合同+装修合同纠纷"
            ),
            "case_number": "SSGL-2603-0014",
            "external_case_id": "plaintiff:SSGL-2603-0014",
            "confirmed_aliases": ["海西高新"],
            "version": 1,
        },
    ]

    interpretation = await LLMCognitiveSemanticInterpreter(
        client,
        legacy_semantic_enforcers_enabled=False,
    ).interpret(
        _turn(text, resources={"visible_cases": visible_cases}),
        ConversationState.empty(
            user_id=f"sandbox-agent2-phase2-20260711:{ACTOR_ID}",
            conversation_id="pang-real-dialogue-regression",
        ),
    )

    assert client.calls == 2
    assert [item.action_type for item in interpretation.required_actions] == [
        "record_case_progress"
    ]
    assert interpretation.clarification_need is not None
    assert interpretation.clarification_need.reason == "ambiguous_case_alias"
    assert interpretation.entities[0].attributes["normalized_fact"] == text
    assert interpretation.entities[0].attributes["evidence_spans"] == [[0, len(text)]]


@pytest.mark.asyncio
async def test_binhai_hospital_natural_abbreviation_is_reassessed_and_admitted() -> None:
    text = "滨海医院预计下周拜访法官沟通回款线索"
    chat_payload = {
        "intents": ["chat"],
        "segments": [
            {
                "segment_id": "incorrect-action-free-binhai-turn",
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
    progress_payload = {
        "intents": ["case_progress"],
        "segments": [
            {
                "segment_id": "binhai-case-progress",
                "text": text,
                "intents": ["case_progress"],
                "entity_ids": ["binhai-case-ref"],
                "action_ids": ["record-binhai-case-progress"],
            }
        ],
        "entities": [
            {
                "entity_id": "binhai-case-ref",
                "entity_type": "case_ref",
                "value": "天津市滨海新区妇女儿童医院生态城院区工程案",
                "confidence": 0.99,
                "attributes": {
                    "statement_mode": "asserted",
                    "action_time_scope": "future",
                    "next_actions": ["预计下周拜访法官沟通回款线索"],
                    "normalized_fact": text,
                    "evidence_spans": [[0, len(text)]],
                },
            }
        ],
        "confidence": 0.99,
        "required_actions": [
            {
                "action_id": "record-binhai-case-progress",
                "action_type": "record_case_progress",
                "intent": "case_progress",
                "entity_ids": ["binhai-case-ref"],
                "parameters": {},
            }
        ],
        "clarification_need": None,
        "context_update": {
            "current_goal": "case_progress",
            "remember_entity_ids": ["binhai-case-ref"],
            "remember_turn": True,
        },
    }
    visible_case = {
        "case_id": "8961e207-da0d-5cad-bb8b-a6f2b596f542",
        "case_name": (
            "股份三分（天津）天津市滨海新区妇女儿童医院生态城院区工程"
            "精装修工程2标段施工合同纠纷"
        ),
        "case_number": "SSGL-2603-0007",
        "external_case_id": "plaintiff:SSGL-2603-0007",
        "confirmed_aliases": ["天津市滨海新区妇女儿童医院生态城院区工程案"],
        "version": 1,
    }
    client = _SequenceSemanticClient([chat_payload, progress_payload])
    turn = CognitiveTurn(
        tenant_id="sandbox-agent2-phase2-20260711",
        user_id=f"sandbox-agent2-phase2-20260711:{ACTOR_ID}",
        actor_user_id=str(ACTOR_ID),
        conversation_id="pang-binhai-regression",
        message_id="pang-binhai-regression",
        text=text,
        occurred_at=NOW,
        resources={"visible_cases": [visible_case]},
    )
    state = ConversationState.empty(
        user_id=turn.user_id,
        conversation_id=turn.conversation_id,
    )

    interpretation = await LLMCognitiveSemanticInterpreter(
        client,
        legacy_semantic_enforcers_enabled=False,
    ).interpret(turn, state)

    assert client.calls == 2
    assert [item.action_type for item in interpretation.required_actions] == [
        "record_case_progress"
    ]
    admission = DomainAdmissionEngine().admit(turn, state, interpretation)
    assert [(item.status, item.reason_code) for item in admission.decisions] == [
        ("admitted", "case_reference_uniquely_authorized")
    ]
    class _FixedInterpreter:
        async def interpret(self, _turn_value, _state_value):
            return interpretation

    core_result = await CognitiveCoreV3(_FixedInterpreter()).process(turn, state)
    plan = CognitiveCommandPlanner().plan(
        core_result.decision,
        CommandPlanningContext(
            message_id=turn.message_id,
            actor_user_id=ACTOR_ID,
        ),
    )
    cases = (
        CaseRecord(
            visible_case["case_id"],
            turn.tenant_id,
            visible_case["case_number"],
            visible_case["case_name"],
            (),
            external_case_id=visible_case["external_case_id"],
            confirmed_aliases=tuple(visible_case["confirmed_aliases"]),
            version=visible_case["version"],
        ),
    )
    executor = _AsyncInMemoryBusinessExecutor(cases)
    composition = await Phase2BusinessComposer(
        case_repository=_VisibleCaseRepository(cases),  # type: ignore[arg-type]
        executor=executor,
    ).execute(
        plan.business_commands,
        BusinessCommandContext(
            tenant_id=turn.tenant_id,
            company_id="company-test",
            department_id="legal",
            team_id="litigation",
            actor_user_id=str(ACTOR_ID),
            actor_role_ids=("lawyer",),
            allowed_case_ids=(visible_case["case_id"],),
            source_message_id=turn.message_id,
            source_channel="dingtalk",
            occurred_at=NOW,
            conversation_id=turn.conversation_id,
        ),
    )

    assert composition.executed_count == 1
    assert composition.actions[0].receipt is not None
    assert composition.actions[0].receipt.actual_write is True
    assert next(iter(executor.delegate.case_progress.values())).summary == text


def test_case_strategy_decision_is_a_real_case_fact() -> None:
    assessment = assess_case_progress_statement(
        "星皓·锦樾项目设计软装合同纠纷，评估暂时不诉，暂缓诉讼，"
        "等一周后看谈判的结果重新评估"
    )

    assert assessment.asserted is True
    assert assessment.fact_kind == "strategy_decision"


@pytest.mark.asyncio
async def test_enforced_runtime_reassesses_liu_case_strategy_update() -> None:
    text = (
        "星皓·锦樾项目设计软装合同纠纷，评估暂时不诉，暂缓诉讼，"
        "等一周后看谈判的结果重新评估"
    )
    chat_payload = {
        "intents": ["chat"],
        "segments": [
            {
                "segment_id": "incorrect-chat-case-strategy",
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
    corrected_payload = {
        "intents": ["case_progress"],
        "segments": [
            {
                "segment_id": "reassessed-case-strategy",
                "text": text,
                "intents": ["case_progress"],
                "entity_ids": ["case-ref-1"],
                "action_ids": ["record-case-strategy-1"],
            }
        ],
        "entities": [
            {
                "entity_id": "case-ref-1",
                "entity_type": "case_ref",
                "value": "星皓·锦樾项目设计软装合同纠纷",
                "confidence": 1.0,
                "attributes": {
                    "statement_mode": "asserted",
                    "current_status": "评估暂时不诉，暂缓诉讼",
                    "next_actions": ["等一周后看谈判的结果重新评估"],
                    "evidence_spans": [[0, len(text)]],
                },
            }
        ],
        "confidence": 1.0,
        "required_actions": [
            {
                "action_id": "record-case-strategy-1",
                "action_type": "record_case_progress",
                "intent": "case_progress",
                "entity_ids": ["case-ref-1"],
                "parameters": {},
            }
        ],
        "clarification_need": None,
        "context_update": {
            "current_goal": "case_progress",
            "remember_entity_ids": ["case-ref-1"],
            "remember_turn": True,
        },
    }
    client = _SequenceSemanticClient([chat_payload, corrected_payload])

    interpretation = await LLMCognitiveSemanticInterpreter(
        client,
        legacy_semantic_enforcers_enabled=False,
    ).interpret(
        _turn(
            text,
            resources={
                "visible_cases": [
                    {
                        "case_id": "ca46f25e-0859-58bd-adf7-3cf750a724bd",
                        "case_name": "星皓·锦樾项目设计软装合同纠纷",
                        "case_number": "SSGL-2604-0013",
                        "external_case_id": "plaintiff:SSGL-2604-0013",
                        "confirmed_aliases": [
                            "星皓·锦樾项目1号楼上、下叠样板间室内软案"
                        ],
                        "version": 1,
                    }
                ]
            },
        ),
        ConversationState.empty(
            user_id=f"sandbox-agent2-phase2-20260711:{ACTOR_ID}",
            conversation_id="liu-real-dialogue-regression",
        ),
    )

    assert [action.action_type for action in interpretation.required_actions] == [
        "record_case_progress"
    ]
    assert client.calls == 2

    core_turn = CognitiveTurn(
        user_id=f"sandbox-agent2-phase2-20260711:{ACTOR_ID}",
        conversation_id="liu-real-dialogue-regression",
        message_id="liu-case-strategy-write",
        text=text,
        occurred_at=NOW,
        resources={
            "visible_cases": [
                {
                    "case_id": "ca46f25e-0859-58bd-adf7-3cf750a724bd",
                    "case_name": "星皓·锦樾项目设计软装合同纠纷",
                    "case_number": "SSGL-2604-0013",
                    "external_case_id": "plaintiff:SSGL-2604-0013",
                    "confirmed_aliases": [
                        "星皓·锦樾项目1号楼上、下叠样板间室内软案"
                    ],
                    "version": 1,
                }
            ]
        },
    )
    core_result = await CognitiveCoreV3(
        LLMCognitiveSemanticInterpreter(
            _SequenceSemanticClient([chat_payload, corrected_payload]),
            legacy_semantic_enforcers_enabled=False,
        )
    ).process(
        core_turn,
        ConversationState.empty(
            user_id=core_turn.user_id,
            conversation_id=core_turn.conversation_id,
        ),
    )
    plan = CognitiveCommandPlanner().plan(
        core_result.decision,
        CommandPlanningContext(
            message_id="liu-case-strategy-write",
            actor_user_id=ACTOR_ID,
        ),
    )
    cases = (
        CaseRecord(
            "ca46f25e-0859-58bd-adf7-3cf750a724bd",
            "sandbox-agent2-phase2-20260711",
            "SSGL-2604-0013",
            "星皓·锦樾项目设计软装合同纠纷",
            (),
            confirmed_aliases=("星皓·锦樾项目1号楼上、下叠样板间室内软案",),
        ),
    )
    executor = _AsyncInMemoryBusinessExecutor(cases)
    composition = await Phase2BusinessComposer(
        case_repository=_VisibleCaseRepository(cases),  # type: ignore[arg-type]
        executor=executor,
    ).execute(
        plan.business_commands,
        BusinessCommandContext(
            tenant_id="sandbox-agent2-phase2-20260711",
            company_id="company-test",
            department_id="legal",
            team_id="litigation",
            actor_user_id=str(ACTOR_ID),
            actor_role_ids=("lawyer",),
            allowed_case_ids=("ca46f25e-0859-58bd-adf7-3cf750a724bd",),
            source_message_id="liu-case-strategy-write",
            source_channel="dingtalk",
            occurred_at=NOW,
            conversation_id="liu-real-dialogue-regression",
        ),
    )

    assert composition.executed_count == 1
    assert composition.actions[0].receipt is not None
    assert composition.actions[0].receipt.actual_write is True
    assert next(iter(executor.delegate.case_progress.values())).summary == text


def test_changzhou_is_a_supported_travel_destination() -> None:
    resolved = LocationRegistry.default().resolve("出差常州，进行项目沟通取证")

    assert resolved.status == "resolved"
    assert resolved.destination_normalized == "常州市"
    assert resolved.city_code == "320400"


@pytest.mark.asyncio
async def test_enforced_runtime_keeps_case_service_request_non_mutating_after_reassessment():
    text = "帮我找恒大翡翠华庭的当地资源"
    payload = {
        "intents": ["chat"],
        "segments": [
            {
                "segment_id": "case-service-request",
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
    client = _SequenceSemanticClient([payload, payload])

    interpretation = await LLMCognitiveSemanticInterpreter(
        client,
        legacy_semantic_enforcers_enabled=False,
    ).interpret(
        _turn(
            text,
            resources={
                "visible_cases": [
                    {
                        "case_id": "58cb60bc-d084-4805-a5bc-c03ecebf2a08",
                        "case_name": "幕墙事业部扬州恒大建设工程施工合同纠纷",
                        "case_number": "（2026）苏1002民初1888号",
                        "external_case_id": "D-018",
                        "confirmed_aliases": ["恒大翡翠华庭"],
                        "version": 1,
                    }
                ]
            },
        ),
        ConversationState.empty(
            user_id=f"sandbox-agent2-phase2-20260711:{ACTOR_ID}",
            conversation_id="pang-real-dialogue-regression",
        ),
    )

    assert interpretation.required_actions == ()
    assert client.calls == 2


@pytest.mark.asyncio
async def test_case_service_request_is_blocked_even_when_model_calls_it_progress():
    text = "帮我找恒大翡翠华庭的当地资源"
    client = _SemanticClient(
        {
            "intents": ["case_progress"],
            "segments": [
                {
                    "segment_id": "incorrect-service-request-progress",
                    "text": text,
                    "intents": ["case_progress"],
                    "entity_ids": ["case-ref-1"],
                    "action_ids": ["record-case-progress-1"],
                }
            ],
            "entities": [
                {
                    "entity_id": "case-ref-1",
                    "entity_type": "case_ref",
                    "value": "恒大翡翠华庭",
                    "confidence": 1.0,
                    "attributes": {
                        "statement_mode": "asserted",
                        "factual_progress": [text],
                        "evidence_spans": [[0, len(text)]],
                    },
                }
            ],
            "confidence": 1.0,
            "required_actions": [
                {
                    "action_id": "record-case-progress-1",
                    "action_type": "record_case_progress",
                    "intent": "case_progress",
                    "entity_ids": ["case-ref-1"],
                    "parameters": {},
                }
            ],
            "clarification_need": None,
            "context_update": {
                "current_goal": "case_progress",
                "remember_entity_ids": ["case-ref-1"],
                "remember_turn": True,
            },
        }
    )
    result = await CognitiveCoreV3(
        LLMCognitiveSemanticInterpreter(
            client,
            legacy_semantic_enforcers_enabled=False,
        )
    ).process(
        _turn(text),
        ConversationState.empty(
            user_id=f"sandbox-agent2-phase2-20260711:{ACTOR_ID}",
            conversation_id="pang-real-dialogue-regression",
        ),
    )
    plan = CognitiveCommandPlanner().plan(
        result.decision,
        CommandPlanningContext(
            message_id="service-request-must-not-write",
            actor_user_id=ACTOR_ID,
        ),
    )
    cases = (
        CaseRecord(
            "58cb60bc-d084-4805-a5bc-c03ecebf2a08",
            "sandbox-agent2-phase2-20260711",
            "（2026）苏1002民初1888号",
            "幕墙事业部扬州恒大建设工程施工合同纠纷",
            (),
            confirmed_aliases=("恒大翡翠华庭",),
        ),
    )
    executor = _AsyncInMemoryBusinessExecutor(cases)

    composition = await Phase2BusinessComposer(
        case_repository=_VisibleCaseRepository(cases),  # type: ignore[arg-type]
        executor=executor,
    ).execute(
        plan.business_commands,
        BusinessCommandContext(
            tenant_id="sandbox-agent2-phase2-20260711",
            company_id="company-test",
            department_id="legal",
            team_id="litigation",
            actor_user_id=str(ACTOR_ID),
            actor_role_ids=("lawyer",),
            allowed_case_ids=("58cb60bc-d084-4805-a5bc-c03ecebf2a08",),
            source_message_id="service-request-must-not-write",
            source_channel="dingtalk",
            occurred_at=NOW,
            conversation_id="pang-real-dialogue-regression",
        ),
    )

    assert composition.executed_count == 0
    assert composition.actions[0].block is not None
    assert composition.actions[0].block.reason_code == "case_progress_not_asserted"
    assert executor.delegate.case_progress == {}


@pytest.mark.asyncio
async def test_conflicting_case_hint_for_new_progress_remains_fail_closed():
    text = "恒大翡翠华庭 与法官沟通了案件进展"
    payload = {
        "intents": ["case_progress"],
        "segments": [
            {
                "segment_id": "conflicting-case-reference",
                "text": text,
                "intents": ["case_progress"],
                "entity_ids": ["case-ref-1"],
                "action_ids": ["record-case-progress-1"],
            }
        ],
        "entities": [
            {
                "entity_id": "case-ref-1",
                "entity_type": "case_ref",
                "value": "恒大翡翠华庭",
                "confidence": 1.0,
                "attributes": {"case_hint": "人民西路8号院"},
            }
        ],
        "confidence": 1.0,
        "required_actions": [
            {
                "action_id": "record-case-progress-1",
                "action_type": "record_case_progress",
                "intent": "case_progress",
                "entity_ids": ["case-ref-1"],
                "parameters": {},
            }
        ],
        "clarification_need": None,
        "context_update": {
            "current_goal": "case_progress",
            "remember_entity_ids": ["case-ref-1"],
            "remember_turn": True,
        },
    }
    client = _SemanticClient(payload)

    with pytest.raises(ValueError, match="case_hint"):
        await LLMCognitiveSemanticInterpreter(client).interpret(
            _turn(text),
            ConversationState.empty(
                user_id=f"sandbox-agent2-phase2-20260711:{ACTOR_ID}",
                conversation_id="pang-real-dialogue-regression",
            ),
        )

    assert client.calls == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    (
        "恒大翡翠华庭法院表示下周重新查控",
        "恒大翡翠华庭跟书记员联系过了",
        "恒大翡翠华庭与对方律师对接完成",
        "恒大翡翠华庭催办了财产查控",
        "恒大翡翠华庭参加了庭审",
        "恒大翡翠华庭与分公司核对了材料",
        "恒大翡翠华庭正在和分公司核对材料",
        "恒大翡翠华庭找了当地资源",
        "恒大翡翠华庭准备找当地资源",
        "恒大翡翠华庭协调了出庭人员",
        "恒大翡翠华庭委托了当地律师",
        "恒大翡翠华庭调取了工商档案",
        "恒大翡翠华庭走访了项目现场",
        "恒大翡翠华庭起草了答辩意见",
        "恒大翡翠华庭今天提交了答辩材料",
        "恒大翡翠华庭对方已履行50万元",
        "恒大翡翠华庭答辩材料还没准备好",
        "恒大翡翠华庭法院暂未通知开庭",
        "恒大翡翠华庭进入履行阶段",
        "恒大翡翠华庭判决支持了全部诉请",
        "恒大翡翠华庭仲裁裁决驳回了对方请求",
        "恒大翡翠华庭调解没谈成",
        "恒大翡翠华庭查控未发现可执行财产",
        "恒大翡翠华庭本次庭审结束",
        "恒大翡翠华庭法官让我们补充证据",
        "恒大翡翠华庭对方提出了分期和解方案",
        "恒大翡翠华庭案件已受理",
        "恒大翡翠华庭执行立案失败",
    ),
)
async def test_named_case_facts_across_lifecycle_are_not_swallowed_as_chat(text: str):
    client = _SemanticClient(
        {
            "intents": ["chat"],
            "segments": [
                {
                    "segment_id": "incorrect-chat-case-fact",
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
    )
    state = ConversationState.empty(
        user_id=f"sandbox-agent2-phase2-20260711:{ACTOR_ID}",
        conversation_id="pang-real-dialogue-regression",
    )

    result = await CognitiveCoreV3(LLMCognitiveSemanticInterpreter(client)).process(
        _turn(
            text,
            resources={
                "visible_cases": [
                    {
                        "case_id": "58cb60bc-d084-4805-a5bc-c03ecebf2a08",
                        "case_name": "幕墙事业部扬州恒大建设工程施工合同纠纷",
                        "case_number": "（2026）苏1002民初1888号",
                        "external_case_id": "D-018",
                        "confirmed_aliases": ["恒大翡翠华庭"],
                        "version": 1,
                    }
                ]
            },
        ),
        state,
    )

    assert [action.action_type for action in result.decision.required_actions] == [
        "record_case_progress"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    (
        "恒大翡翠华庭什么时候开庭？",
        "恒大翡翠华庭怎么联系法官？",
        "帮我找恒大翡翠华庭的当地资源",
        "恒大翡翠华庭的当地资源怎么找",
        "恒大翡翠华庭与分公司核对材料了吗？",
        "如果恒大翡翠华庭后天开庭就准备材料",
        "如果恒大翡翠华庭联系了法官就告诉我",
        "如果恒大翡翠华庭找到了当地资源就告诉我",
        "比如恒大翡翠华庭已经履行了",
        "恒大翡翠华庭后天开庭但不要记录",
        "恒大翡翠华庭与法官沟通了，但不要记录",
        "恒大翡翠华庭准备找当地资源，但不要记录",
        "恒大翡翠华庭开庭材料怎么准备",
        "恒大翡翠华庭没其他风险",
        "恒大翡翠华庭评估暂时不诉吗？",
        "如果恒大翡翠华庭评估暂时不诉，就一周后重新评估",
    ),
)
async def test_named_case_questions_examples_and_opt_outs_remain_zero_write(text: str):
    client = _SemanticClient(
        {
            "intents": ["chat"],
            "segments": [
                {
                    "segment_id": "safe-chat-case-reference",
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
    )
    state = ConversationState.empty(
        user_id=f"sandbox-agent2-phase2-20260711:{ACTOR_ID}",
        conversation_id="pang-real-dialogue-regression",
    )

    result = await CognitiveCoreV3(LLMCognitiveSemanticInterpreter(client)).process(
        _turn(
            text,
            resources={
                "visible_cases": [
                    {
                        "case_id": "58cb60bc-d084-4805-a5bc-c03ecebf2a08",
                        "case_name": "幕墙事业部扬州恒大建设工程施工合同纠纷",
                        "case_number": "（2026）苏1002民初1888号",
                        "external_case_id": "D-018",
                        "confirmed_aliases": ["恒大翡翠华庭"],
                        "version": 1,
                    }
                ]
            },
        ),
        state,
    )

    assert "record_case_progress" not in {
        action.action_type for action in result.decision.required_actions
    }


@pytest.mark.asyncio
async def test_no_other_risk_is_not_a_case_progress_write_even_with_case_focus():
    case_id = "a0cb75be-72fb-4fd1-9ec0-5ebf2572e276"
    text = "没其他风险"
    client = _SemanticClient(
        {
            "intents": ["case_progress"],
            "segments": [
                {
                    "segment_id": "incorrect-case-progress",
                    "text": text,
                    "intents": ["case_progress"],
                    "entity_ids": ["focused-case"],
                    "action_ids": ["incorrect-record-progress"],
                }
            ],
            "entities": [
                {
                    "entity_id": "focused-case",
                    "entity_type": "case_ref",
                    "value": "人民西路8号院",
                    "confidence": 1.0,
                    "attributes": {},
                }
            ],
            "confidence": 1.0,
            "required_actions": [
                {
                    "action_id": "incorrect-record-progress",
                    "action_type": "record_case_progress",
                    "intent": "case_progress",
                    "entity_ids": ["focused-case"],
                    "parameters": {},
                }
            ],
            "clarification_need": None,
            "context_update": {
                "current_goal": "case_progress",
                "remember_entity_ids": ["focused-case"],
                "remember_turn": True,
            },
        }
    )
    state = ConversationState(
        user_id=f"sandbox-agent2-phase2-20260711:{ACTOR_ID}",
        conversation_id="pang-real-dialogue-regression",
        current_goal=ConversationGoal("case_progress"),
    )
    core_result = await CognitiveCoreV3(LLMCognitiveSemanticInterpreter(client)).process(
        _turn(text),
        state,
    )
    plan = CognitiveCommandPlanner().plan(
        core_result.decision,
        CommandPlanningContext(
            message_id="incident-no-other-risk",
            actor_user_id=ACTOR_ID,
        ),
    )
    cases = (
        CaseRecord(
            case_id,
            "sandbox-agent2-phase2-20260711",
            "（2026）云0102民初1888号",
            "人民西路8号院物业服务合同纠纷案",
            ("人民西路8号院业主",),
            confirmed_aliases=("人民西路8号院",),
        ),
    )
    executor = _AsyncInMemoryBusinessExecutor(cases)
    composer = Phase2BusinessComposer(
        case_repository=_VisibleCaseRepository(cases),  # type: ignore[arg-type]
        executor=executor,
    )
    context = BusinessCommandContext(
        tenant_id="sandbox-agent2-phase2-20260711",
        company_id="company-test",
        department_id="legal",
        team_id="litigation",
        actor_user_id=str(ACTOR_ID),
        actor_role_ids=("lawyer",),
        allowed_case_ids=(case_id,),
        source_message_id="incident-no-other-risk",
        source_channel="dingtalk",
        occurred_at=NOW,
        conversation_id="pang-real-dialogue-regression",
    )

    composition = await composer.execute(plan.business_commands, context)

    assert composition.executed_count == 0
    assert composition.blocked_count == 1
    assert composition.actions[0].block is not None
    assert composition.actions[0].block.reason_code == "case_progress_not_asserted"
    assert executor.delegate.case_progress == {}


@pytest.mark.asyncio
async def test_case_write_status_question_is_a_receipt_query_not_chat():
    text = "进入案件进展了吗"
    client = _SemanticClient(
        {
            "intents": ["chat"],
            "segments": [
                {
                    "segment_id": "model-chat-status",
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
    )
    state = ConversationState(
        user_id=f"sandbox-agent2-phase2-20260711:{ACTOR_ID}",
        conversation_id="pang-real-dialogue-regression",
        current_goal=ConversationGoal("case_progress"),
    )

    result = await CognitiveCoreV3(LLMCognitiveSemanticInterpreter(client)).process(
        _turn(text),
        state,
    )
    plan = CognitiveCommandPlanner().plan(
        result.decision,
        CommandPlanningContext(
            message_id="incident-case-write-status",
            actor_user_id=ACTOR_ID,
        ),
    )

    assert result.decision.intents == ("case_progress_query",)
    assert [action.action_type for action in result.decision.required_actions] == [
        "query_operation_status"
    ]
    assert [command.command_type for command in plan.business_commands] == [
        "query_operation_status"
    ]
    assert plan.business_commands[0].payload["entities"][0]["attributes"] == {
        "domain": "case_progress"
    }


@pytest.mark.asyncio
async def test_case_write_status_reply_comes_from_receipt_facts():
    candidate = TypedBusinessCommand(
        command_id=uuid4(),
        decision_id=uuid4(),
        sub_decision_id=uuid4(),
        command_type="query_operation_status",
        target_system="operation_outcomes",
        entity_ids=("case-status",),
        payload={
            "entities": [
                {
                    "entity_id": "case-status",
                    "entity_type": "operation_status_query",
                    "value": "进入案件进展了吗",
                    "confidence": 1.0,
                    "attributes": {"domain": "case_progress"},
                }
            ],
            "parameters": {},
            "source_segments": [
                {
                    "segment_id": "case-status-segment",
                    "text": "进入案件进展了吗",
                    "text_hash": "hash",
                }
            ],
        },
        execution_mode="read_only",
        idempotency_key="operation-status-candidate",
    )
    executor = _OperationStatusExecutor()
    composer = Phase2BusinessComposer(
        case_repository=_VisibleCaseRepository(()),  # type: ignore[arg-type]
        executor=executor,
    )
    context = BusinessCommandContext(
        tenant_id="sandbox-agent2-phase2-20260711",
        company_id="company-test",
        department_id="legal",
        team_id="litigation",
        actor_user_id=str(ACTOR_ID),
        actor_role_ids=("lawyer",),
        allowed_case_ids=(),
        source_message_id="incident-case-write-status",
        source_channel="dingtalk",
        occurred_at=NOW,
        conversation_id="pang-real-dialogue-regression",
    )

    result = await composer.execute((candidate,), context)

    assert result.executed_count == 1
    assert executor.commands[0].command_type == "query_operation_status"
    reply = OutcomeReplyComposer().compose(business_composition_outcomes(result))
    assert reply == "没有。上一条只更新了日报，没有创建案件进展。"
    assert "已记录" not in reply


@pytest.mark.asyncio
async def test_travel_collaboration_status_question_is_not_degraded_to_chat():
    text = "有没有人和我协同？"
    client = _SemanticClient(
        {
            "intents": ["chat"],
            "segments": [
                {
                    "segment_id": "model-chat-travel-status",
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
    )
    state = ConversationState.empty(
        user_id=f"sandbox-agent2-phase2-20260711:{ACTOR_ID}",
        conversation_id="pang-real-dialogue-regression",
    )

    result = await CognitiveCoreV3(LLMCognitiveSemanticInterpreter(client)).process(
        _turn(text),
        state,
    )
    plan = CognitiveCommandPlanner().plan(
        result.decision,
        CommandPlanningContext(
            message_id="incident-travel-status",
            actor_user_id=ACTOR_ID,
        ),
    )

    assert result.decision.intents == ("travel_collaboration_query",)
    assert [action.action_type for action in result.decision.required_actions] == [
        "query_operation_status"
    ]
    assert [command.command_type for command in plan.business_commands] == [
        "query_operation_status"
    ]
    assert plan.business_commands[0].payload["entities"][0]["attributes"] == {
        "domain": "travel"
    }


@pytest.mark.asyncio
async def test_chat_reply_does_not_leak_internal_routing_labels():
    client = _SemanticClient({"reply": "好的，我明白了。"})
    decision = SimpleNamespace(
        segments=(
            SimpleNamespace(
                text="好的",
                intents=("chat",),
            ),
        )
    )

    reply = await build_cognitive_side_reply_v3(
        decision=decision,
        llm_client=client,
        context_pack=None,
    )

    assert reply == "好的，我明白了。"
    assert "闲聊" not in reply
    assert "不写入日报" not in reply
