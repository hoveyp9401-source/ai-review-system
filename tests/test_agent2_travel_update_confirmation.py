from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime
import hashlib
import json
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

import pytest

from app.agent2.business.admission import (
    bind_business_execution_context,
    require_business_execution_admission,
)
from app.agent2.business.compiler import Phase2BusinessCommandCompiler
from app.agent2.business.contracts import (
    BusinessCommandContext,
    BusinessCommandError,
    UpdateTravelIntent,
)
from app.agent2.cognitive_core_v3 import CognitiveCoreV3, CognitiveTurn, SemanticInterpretation
from app.agent2.cognitive_orchestrator_v3 import (
    CognitiveOrchestrationResult,
    finalize_cognitive_state_after_execution,
)
from app.agent2.cognitive_reply_v3 import (
    append_cognitive_clarification,
    pending_lifecycle_reply,
)
from app.agent2.command_planner_v3 import CognitiveCommandPlanner, CommandPlanningContext
from app.agent2.conversation_state import ConversationGoal, ConversationState
from app.agent2.conversation_state_store import InMemoryConversationStateStore
from app.agent2.domain_admission import DomainAdmissionEngine
from app.agent2.runtime.domains import (
    DomainPack,
    DomainPackRegistry,
    TRAVEL_DOMAIN_CONTRACT,
)
from app.agent2.semantic_interpreter_v3 import LLMCognitiveSemanticInterpreter


TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 7, 22, 9, 0, tzinfo=TZ)
TENANT_ID = "tenant-travel-update-test"
ACTOR_ID = "actor-travel-update-test"
USER_KEY = f"{TENANT_ID}:{ACTOR_ID}"
CONVERSATION_ID = "conversation-travel-update-test"
TRAVEL_ID = "10000000-0000-0000-0000-000000000001"


class _Interpreter:
    def __init__(self, factory):
        self._factory = factory

    async def interpret(self, turn, state):
        return self._factory(turn, state)


class _NoModelClient:
    async def complete_json(self, **kwargs):
        raise AssertionError("an exact bound confirmation must not call the model")


class _PayloadClient:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.calls = 0

    async def complete_json(self, **kwargs):
        self.calls += 1
        return json.dumps(self.payload, ensure_ascii=False)


def _daily_only_travel_projection(text: str) -> dict[str, object]:
    return {
        "intents": ["daily_append"],
        "segments": [
            {
                "segment_id": "daily-plan-segment",
                "text": text,
                "intents": ["daily_append"],
                "entity_ids": ["daily-plan"],
                "action_ids": ["capture-daily-plan"],
            }
        ],
        "entities": [
            {
                "entity_id": "daily-plan",
                "entity_type": "daily_event",
                "value": text,
                "confidence": 1.0,
                "attributes": {
                    "field": "tomorrow_plan",
                    "statement_mode": "asserted",
                },
            }
        ],
        "confidence": 1.0,
        "required_actions": [
            {
                "action_id": "capture-daily-plan",
                "action_type": "capture_daily_event",
                "intent": "daily_append",
                "entity_ids": ["daily-plan"],
                "parameters": {},
            }
        ],
        "clarification_need": None,
        "context_update": {"remember_turn": True},
    }


def test_explicit_self_travel_survives_when_model_only_proposes_daily_projection() -> None:
    text = "明天去南京出差"
    client = _PayloadClient(_daily_only_travel_projection(text))
    interpreter = LLMCognitiveSemanticInterpreter(
        client,
        legacy_semantic_enforcers_enabled=False,
    )

    result = asyncio.run(
        interpreter.interpret(
            _turn(text, "daily-only-explicit-travel"),
            ConversationState.empty(
                user_id=USER_KEY,
                conversation_id=CONVERSATION_ID,
            ),
        )
    )

    assert {action.action_type for action in result.required_actions} == {
        "capture_daily_event",
        "record_travel_event",
    }
    travel = next(
        entity for entity in result.entities if entity.entity_type == "travel_event"
    )
    assert travel.attributes == {
        "destination": "南京",
        "date_hint": "tomorrow",
        "purpose": "出差",
        "statement_mode": "asserted",
        "traveler_scope": "self",
        "evidence_spans": [[0, len(text)]],
    }


@pytest.mark.parametrize(
    "text",
    [
        "明天去南京出差吗？",
        "如果明天去南京出差，再准备材料",
        "同事说明天去南京出差",
        "明天不去南京出差了",
    ],
)
def test_nonassertive_or_cancelled_daily_projection_does_not_gain_travel_action(
    text: str,
) -> None:
    interpreter = LLMCognitiveSemanticInterpreter(
        _PayloadClient(_daily_only_travel_projection(text)),
        legacy_semantic_enforcers_enabled=False,
    )

    result = asyncio.run(
        interpreter.interpret(
            _turn(text, f"daily-only-nonassertive-{len(text)}"),
            ConversationState.empty(
                user_id=USER_KEY,
                conversation_id=CONVERSATION_ID,
            ),
        )
    )

    assert "record_travel_event" not in {
        action.action_type for action in result.required_actions
    }


def _travel_resources(
    *,
    version: int = 3,
    duplicate: bool = False,
    utc_storage: bool = False,
) -> dict[str, object]:
    start_at = "2026-07-22T16:00:00+00:00" if utc_storage else "2026-07-23T00:00:00+08:00"
    end_at = "2026-07-23T15:59:59+00:00" if utc_storage else "2026-07-23T23:59:59+08:00"
    rows = [
        {
            "travel_intent_id": TRAVEL_ID,
            "destination": "南京",
            "start_at": start_at,
            "end_at": end_at,
            "status": "planned",
            "version": version,
        }
    ]
    if duplicate:
        rows.append(
            {
                "travel_intent_id": "10000000-0000-0000-0000-000000000002",
                "destination": "南京",
                "start_at": "2026-07-23T08:00:00+08:00",
                "end_at": "2026-07-23T18:00:00+08:00",
                "status": "planned",
                "version": 1,
            }
        )
    return {
        "timezone": "Asia/Shanghai",
        "active_travel_intents": rows,
        "visible_travel_intent_ids": [row["travel_intent_id"] for row in rows],
    }


def _turn(text: str, message_id: str, *, resources: dict[str, object] | None = None) -> CognitiveTurn:
    return CognitiveTurn(
        tenant_id=TENANT_ID,
        actor_user_id=ACTOR_ID,
        user_id=USER_KEY,
        conversation_id=CONVERSATION_ID,
        message_id=message_id,
        text=text,
        occurred_at=NOW,
        resources=resources or _travel_resources(),
    )


def _pending_proposal(
    text: str,
    *,
    new_status: str = "cancelled",
    new_date_hint: str = "",
) -> SemanticInterpretation:
    attributes: dict[str, object] = {
        "travel_intent_id": TRAVEL_ID,
        "expected_version": 3,
        "destination": "南京",
    }
    if new_status:
        attributes["new_status"] = new_status
    if new_date_hint:
        attributes["new_date_hint"] = new_date_hint
    return SemanticInterpretation.from_payload(
        {
            "intents": ["travel_update"],
            "segments": [
                {
                    "segment_id": "travel-update-segment",
                    "text": text,
                    "intents": ["travel_update"],
                    "entity_ids": ["travel-intent-target"],
                    "action_ids": [],
                }
            ],
            "entities": [
                {
                    "entity_id": "travel-intent-target",
                    "entity_type": "travel_intent_ref",
                    "value": "南京出差",
                    "confidence": 0.99,
                    "attributes": attributes,
                }
            ],
            "confidence": 0.99,
            "required_actions": [],
            "clarification_need": {
                "reason": "medium_risk_confirmation_required",
                "missing_fields": ["confirmation"],
                "question": "确认要调整这次南京出差吗？",
            },
            "context_update": {
                "bind_pending": {
                    "pending_id": "model-controlled-pending-id",
                    "intent": "travel_update",
                    "action": "update_travel_event",
                    "entity_ids": ["travel-intent-target"],
                    "expires_in_seconds": 600,
                }
            },
        }
    )


def _confirmation_proposal(state: ConversationState, text: str = "确认") -> SemanticInterpretation:
    pending = state.pending[0]
    entity = next(item for item in state.current_entities if item.entity_id in pending.entity_ids)
    return SemanticInterpretation.from_payload(
        {
            "intents": [pending.intent],
            "segments": [
                {
                    "segment_id": "travel-confirm-segment",
                    "text": text,
                    "intents": [pending.intent],
                    "entity_ids": list(pending.entity_ids),
                    "action_ids": ["continue-travel-update"],
                }
            ],
            "entities": [
                {
                    "entity_id": entity.entity_id,
                    "entity_type": entity.entity_type,
                    "value": entity.value,
                    "confidence": entity.confidence,
                    "attributes": dict(entity.attributes),
                }
            ],
            "confidence": 1.0,
            "required_actions": [
                {
                    "action_id": "continue-travel-update",
                    "action_type": "continue_pending",
                    "intent": pending.intent,
                    "entity_ids": list(pending.entity_ids),
                    "parameters": {
                        "pending_id": pending.pending_id,
                        "bound_action": pending.action,
                    },
                }
            ],
            "clarification_need": None,
            "context_update": {},
        }
    )


def _process(proposal: SemanticInterpretation, turn: CognitiveTurn, state: ConversationState):
    core = CognitiveCoreV3(
        _Interpreter(lambda _turn, _state: proposal),
        admission_engine=DomainAdmissionEngine(),
        admission_enforced=True,
    )
    return asyncio.run(core.process(turn, state))


def _confirmed_command(
    *,
    first_text: str,
    first_proposal: SemanticInterpretation,
) -> tuple[UpdateTravelIntent, object, ConversationState]:
    base = ConversationState.empty(user_id=USER_KEY, conversation_id=CONVERSATION_ID)
    first = _process(first_proposal, _turn(first_text, "travel-update-request"), base)
    assert len(first.state.pending) == 1
    assert first.state.pending[0].pending_id != "model-controlled-pending-id"

    confirm_turn = _turn("确认", "travel-update-confirm")
    confirmation = _confirmation_proposal(first.state)
    second = _process(confirmation, confirm_turn, first.state)
    plan = CognitiveCommandPlanner().plan(
        second.decision,
        CommandPlanningContext(
            message_id=confirm_turn.message_id,
            actor_user_id=uuid5(NAMESPACE_URL, ACTOR_ID),
        ),
    )
    assert plan.blocked_actions == ()
    assert [item.command_type for item in plan.business_commands] == [
        "update_travel_candidate"
    ]
    business_context = BusinessCommandContext(
        tenant_id=TENANT_ID,
        company_id="company-test",
        department_id="legal",
        team_id="legal",
        actor_user_id=ACTOR_ID,
        actor_role_ids=("legal",),
        allowed_case_ids=(),
        source_message_id=confirm_turn.message_id,
        source_channel="test",
        occurred_at=confirm_turn.occurred_at,
        conversation_id=CONVERSATION_ID,
        conversation_state_version=first.state.version,
        execution_started_at=confirm_turn.occurred_at,
    )
    candidate = plan.business_commands[0]
    compilation = Phase2BusinessCommandCompiler().compile(
        candidate,
        business_context,
        cases=(),
    )
    assert compilation.block is None
    assert isinstance(compilation.command, UpdateTravelIntent)
    bound_context = bind_business_execution_context(candidate, business_context)
    assert bound_context.admission_operation == "update_travel_event"
    require_business_execution_admission(compilation.command, bound_context)
    return compilation.command, second, first.state


def test_unique_travel_cancellation_requires_confirmation_then_compiles_existing_update() -> None:
    command, second, state = _confirmed_command(
        first_text="明天南京出差不去了",
        first_proposal=_pending_proposal("明天南京出差不去了"),
    )

    assert command.travel_intent_id == TRAVEL_ID
    assert command.expected_version == 3
    assert command.status == "cancelled"
    assert command.start_at is None
    assert second.decision.context_update.consumed_pending_ids == (
        state.pending[0].pending_id,
    )


def test_unique_travel_date_correction_requires_confirmation_then_compiles_update() -> None:
    command, _, _ = _confirmed_command(
        first_text="南京出差改成后天",
        first_proposal=_pending_proposal(
            "南京出差改成后天",
            new_status="",
            new_date_hint="后天",
        ),
    )

    assert command.status == "changed"
    assert command.start_at is not None
    assert command.start_at.date().isoformat() == "2026-07-24"
    assert command.end_at is not None


def test_travel_update_ticket_cannot_authorize_hidden_destination_mutation() -> None:
    command, result, _ = _confirmed_command(
        first_text="南京出差改成后天",
        first_proposal=_pending_proposal(
            "南京出差改成后天",
            new_status="",
            new_date_hint="后天",
        ),
    )
    candidate = CognitiveCommandPlanner().plan(
        result.decision,
        CommandPlanningContext(
            message_id="travel-update-confirm",
            actor_user_id=uuid5(NAMESPACE_URL, ACTOR_ID),
        ),
    ).business_commands[0]
    context = bind_business_execution_context(
        candidate,
        BusinessCommandContext(
            tenant_id=TENANT_ID,
            company_id="company-test",
            department_id="legal",
            team_id="legal",
            actor_user_id=ACTOR_ID,
            actor_role_ids=("legal",),
            allowed_case_ids=(),
            source_message_id="travel-update-confirm",
            source_channel="test",
            occurred_at=NOW,
            conversation_id=CONVERSATION_ID,
            conversation_state_version=1,
            execution_started_at=NOW,
        ),
    )
    smuggled = replace(
        command,
        destination_normalized="上海市",
        city_code="310100",
    )

    with pytest.raises(BusinessCommandError) as error:
        require_business_execution_admission(smuggled, context)

    assert error.value.code == "admission_ticket_claims_mismatch"


def test_confirmed_travel_update_action_is_owned_by_runtime_travel_domain() -> None:
    _, result, _ = _confirmed_command(
        first_text="南京出差改成后天",
        first_proposal=_pending_proposal(
            "南京出差改成后天",
            new_status="",
            new_date_hint="后天",
        ),
    )
    registry = DomainPackRegistry((DomainPack(TRAVEL_DOMAIN_CONTRACT),))

    resolution = registry.resolve_actions(result.decision.required_actions)

    assert resolution.unsupported_action_ids == ()
    assert set(resolution.action_domains.values()) == {"travel"}


def test_utc_stored_travel_is_matched_and_rescheduled_by_business_timezone() -> None:
    resources = _travel_resources(utc_storage=True)
    base = ConversationState.empty(user_id=USER_KEY, conversation_id=CONVERSATION_ID)
    first_text = "南京出差改成后天"
    first = _process(
        _pending_proposal(first_text, new_status="", new_date_hint="后天"),
        _turn(first_text, "utc-travel-update-request", resources=resources),
        base,
    )
    assert len(first.state.pending) == 1

    confirm_turn = _turn("确认", "utc-travel-update-confirm", resources=resources)
    second = _process(_confirmation_proposal(first.state), confirm_turn, first.state)
    plan = CognitiveCommandPlanner().plan(
        second.decision,
        CommandPlanningContext(
            message_id=confirm_turn.message_id,
            actor_user_id=uuid5(NAMESPACE_URL, ACTOR_ID),
        ),
    )
    context = BusinessCommandContext(
        tenant_id=TENANT_ID,
        company_id="company-test",
        department_id="legal",
        team_id="legal",
        actor_user_id=ACTOR_ID,
        actor_role_ids=("legal",),
        allowed_case_ids=(),
        source_message_id=confirm_turn.message_id,
        source_channel="test",
        occurred_at=confirm_turn.occurred_at,
        conversation_id=CONVERSATION_ID,
        conversation_state_version=first.state.version,
        execution_started_at=confirm_turn.occurred_at,
    )
    compilation = Phase2BusinessCommandCompiler().compile(
        plan.business_commands[0], context, cases=()
    )
    assert isinstance(compilation.command, UpdateTravelIntent)
    assert compilation.command.start_at is not None
    assert compilation.command.start_at.astimezone(TZ).date().isoformat() == "2026-07-24"
    assert compilation.command.start_at.astimezone(TZ).hour == 0


def test_two_matching_travel_items_do_not_bind_a_confirmation_pending() -> None:
    proposal = _pending_proposal("明天南京出差不去了")
    turn = _turn(
        "明天南京出差不去了",
        "travel-update-ambiguous",
        resources=_travel_resources(duplicate=True),
    )
    result = _process(
        proposal,
        turn,
        ConversationState.empty(user_id=USER_KEY, conversation_id=CONVERSATION_ID),
    )

    assert result.decision.context_update.bind_pending is None
    assert result.state.pending == ()
    assert result.decision.required_actions == ()


@pytest.mark.parametrize("reply", ["算了", "不确认", "如果确认呢？"])
def test_non_confirmation_reply_cannot_execute_a_bound_travel_update(reply: str) -> None:
    base = ConversationState.empty(user_id=USER_KEY, conversation_id=CONVERSATION_ID)
    first = _process(
        _pending_proposal("明天南京出差不去了"),
        _turn("明天南京出差不去了", "travel-update-request"),
        base,
    )
    proposal = _confirmation_proposal(first.state, text=reply)
    result = _process(proposal, _turn(reply, "travel-update-not-confirmed"), first.state)

    assert result.decision.required_actions == ()
    assert result.decision.admission_tickets == ()
    if reply in {"算了", "不确认"}:
        assert result.decision.clarification_need is None
        assert result.decision.context_update.invalidated_pending_ids == (
            first.state.pending[0].pending_id,
        )
        assert result.state.pending == ()
    else:
        assert result.decision.clarification_need is not None
        assert result.state.pending == first.state.pending


@pytest.mark.parametrize("confirmation", ["确认", "好的", "对", "没错"])
def test_exact_confirmation_reuses_trusted_pending_entity_without_model_recopy(
    confirmation: str,
) -> None:
    base = ConversationState.empty(user_id=USER_KEY, conversation_id=CONVERSATION_ID)
    first = _process(
        _pending_proposal("明天南京出差不去了"),
        _turn("明天南京出差不去了", "trusted-confirmation-request"),
        base,
    )
    interpreter = LLMCognitiveSemanticInterpreter(_NoModelClient())

    interpretation = asyncio.run(
        interpreter.interpret(
            _turn(confirmation, "trusted-confirmation-response"),
            first.state,
        )
    )

    assert [item.action_type for item in interpretation.required_actions] == [
        "continue_pending"
    ]
    assert interpretation.required_actions[0].parameters == {
        "pending_id": first.state.pending[0].pending_id,
        "bound_action": "update_travel_event",
    }
    assert interpretation.entities[0].attributes["expected_version"] == 3
    assert interpreter.source_for("trusted-confirmation-response") == "deterministic_contract"


def test_exact_confirmation_without_active_pending_is_deterministic_zero_write() -> None:
    interpreter = LLMCognitiveSemanticInterpreter(_NoModelClient())
    core = CognitiveCoreV3(
        interpreter,
        admission_engine=DomainAdmissionEngine(),
        admission_enforced=True,
    )
    state = replace(
        ConversationState.empty(user_id=USER_KEY, conversation_id=CONVERSATION_ID),
        current_goal=ConversationGoal(intent="travel_event"),
    )

    result = asyncio.run(core.process(_turn("确认", "stale-confirmation"), state))

    assert result.decision.required_actions == ()
    assert result.decision.admission_tickets == ()
    assert result.decision.clarification_need is not None
    assert result.decision.clarification_need.reason == "pending_binding_mismatch"
    assert result.state.pending == ()
    assert interpreter.source_for("stale-confirmation") == "deterministic_contract"


def test_informational_question_cannot_become_invalid_mutation_or_schema_retry() -> None:
    text = "这个风险怎么解决？"
    client = _PayloadClient(
        {
            "intents": ["daily_modify"],
            "segments": [
                {
                    "segment_id": "provider-question-mutation",
                    "text": text,
                    "intents": ["daily_modify"],
                    "entity_ids": ["provider-report"],
                    "action_ids": ["provider-replace"],
                }
            ],
            "entities": [
                {
                    "entity_id": "provider-report",
                    "entity_type": "daily_report",
                    "value": "问题与风险",
                    "confidence": 1.0,
                    "attributes": {
                        "report_id": "30000000-0000-0000-0000-000000000003",
                        "version": 1,
                        "field": "problems",
                    },
                }
            ],
            "confidence": 1.0,
            "required_actions": [
                {
                    "action_id": "provider-replace",
                    "action_type": "replace_daily_section",
                    "intent": "daily_modify",
                    "entity_ids": ["provider-report"],
                    "parameters": {"replacement": "provider-invented-value"},
                }
            ],
            "clarification_need": None,
            "context_update": {"current_goal": "daily_report"},
        }
    )
    interpreter = LLMCognitiveSemanticInterpreter(client)
    state = replace(
        ConversationState.empty(user_id=USER_KEY, conversation_id=CONVERSATION_ID),
        current_goal=ConversationGoal(intent="daily_report"),
    )

    interpretation = asyncio.run(
        interpreter.interpret(_turn(text, "informational-question"), state)
    )

    assert client.calls == 1
    assert interpretation.intents == ("chat",)
    assert interpretation.required_actions == ()
    assert interpretation.entities == ()
    assert interpreter.source_for("informational-question") == "live_model"


def test_action_free_question_does_not_destroy_active_daily_goal() -> None:
    text = "合同复核完成了吗？"
    client = _PayloadClient(
        {
            "intents": ["chat"],
            "segments": [
                {
                    "segment_id": "provider-question",
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
            "context_update": {
                "current_goal": "chat",
                "remember_turn": True,
            },
        }
    )
    interpreter = LLMCognitiveSemanticInterpreter(client)
    core = CognitiveCoreV3(
        interpreter,
        admission_engine=DomainAdmissionEngine(),
        admission_enforced=True,
    )
    state = replace(
        ConversationState.empty(user_id=USER_KEY, conversation_id=CONVERSATION_ID),
        current_goal=ConversationGoal(intent="daily_report"),
    )

    result = asyncio.run(
        core.process(_turn(text, "question-preserves-daily-goal"), state)
    )

    assert client.calls == 1
    assert result.decision.required_actions == ()
    assert result.decision.admission_tickets == ()
    assert result.decision.context_update.preserve_current_goal is True
    assert result.state.current_goal == state.current_goal

    followup = asyncio.run(
        interpreter.interpret(
            _turn("合同复核已经完成", "fact-after-question"),
            result.state,
        )
    )
    assert client.calls == 1
    assert [item.action_type for item in followup.required_actions] == [
        "capture_daily_event"
    ]
    assert interpreter.source_for("fact-after-question") == "deterministic_contract"


def test_informational_question_filter_preserves_independent_positive_segment() -> None:
    question = "这个风险怎么解决？"
    fact = "今天完成合同审核。"
    text = question + fact
    client = _PayloadClient(
        {
            "intents": ["daily_modify", "daily_append"],
            "segments": [
                {
                    "segment_id": "bad-question-mutation",
                    "text": question,
                    "intents": ["daily_modify"],
                    "entity_ids": ["bad-report"],
                    "action_ids": ["bad-replace"],
                },
                {
                    "segment_id": "independent-fact",
                    "text": fact,
                    "intents": ["daily_append"],
                    "entity_ids": ["daily-fact"],
                    "action_ids": ["capture-fact"],
                },
            ],
            "entities": [
                {
                    "entity_id": "bad-report",
                    "entity_type": "daily_report",
                    "value": "问题与风险",
                    "confidence": 1.0,
                    "attributes": {"field": "problems", "version": 1},
                },
                {
                    "entity_id": "daily-fact",
                    "entity_type": "daily_event",
                    "value": "完成合同审核",
                    "confidence": 1.0,
                    "attributes": {
                        "field": "today_work",
                        "statement_mode": "asserted",
                    },
                },
            ],
            "confidence": 1.0,
            "required_actions": [
                {
                    "action_id": "bad-replace",
                    "action_type": "replace_daily_section",
                    "intent": "daily_modify",
                    "entity_ids": ["bad-report"],
                    "parameters": {"replacement": "provider-invented-value"},
                },
                {
                    "action_id": "capture-fact",
                    "action_type": "capture_daily_event",
                    "intent": "daily_append",
                    "entity_ids": ["daily-fact"],
                    "parameters": {},
                },
            ],
            "clarification_need": None,
            "context_update": {
                "current_goal": "daily_append",
                "remember_entity_ids": ["bad-report", "daily-fact"],
                "remember_turn": True,
            },
        }
    )

    interpretation = asyncio.run(
        LLMCognitiveSemanticInterpreter(client).interpret(
            _turn(text, "question-plus-independent-fact"),
            ConversationState.empty(user_id=USER_KEY, conversation_id=CONVERSATION_ID),
        )
    )

    assert client.calls == 1
    assert [item.action_type for item in interpretation.required_actions] == [
        "capture_daily_event"
    ]
    assert [item.entity_id for item in interpretation.entities] == ["daily-fact"]
    assert [item.text for item in interpretation.segments] == [fact]


def test_valid_fact_plus_read_only_question_keeps_both_model_segments() -> None:
    fact = "今天完成合同复核"
    question = "证据目录什么时候提交？"
    text = f"{fact}；{question}"
    client = _PayloadClient(
        {
            "intents": ["daily_append", "case_query"],
            "segments": [
                {
                    "segment_id": "fact-segment",
                    "text": fact,
                    "intents": ["daily_append"],
                    "entity_ids": ["daily-fact"],
                    "action_ids": ["capture-fact"],
                },
                {
                    "segment_id": "question-segment",
                    "text": question,
                    "intents": ["case_query"],
                    "entity_ids": ["case-question"],
                    "action_ids": ["answer-question"],
                },
            ],
            "entities": [
                {
                    "entity_id": "daily-fact",
                    "entity_type": "daily_event",
                    "value": fact,
                    "confidence": 1.0,
                    "attributes": {
                        "field": "today_work",
                        "statement_mode": "asserted",
                    },
                },
                {
                    "entity_id": "case-question",
                    "entity_type": "case_query",
                    "value": question,
                    "confidence": 1.0,
                    "attributes": {"question": question},
                },
            ],
            "confidence": 1.0,
            "required_actions": [
                {
                    "action_id": "capture-fact",
                    "action_type": "capture_daily_event",
                    "intent": "daily_append",
                    "entity_ids": ["daily-fact"],
                    "parameters": {},
                },
                {
                    "action_id": "answer-question",
                    "action_type": "answer_case_query",
                    "intent": "case_query",
                    "entity_ids": ["case-question"],
                    "parameters": {},
                },
            ],
            "clarification_need": None,
            "context_update": {
                "current_goal": "daily_append",
                "remember_entity_ids": ["daily-fact", "case-question"],
                "remember_turn": True,
            },
        }
    )

    interpretation = asyncio.run(
        LLMCognitiveSemanticInterpreter(client).interpret(
            _turn(text, "fact-plus-read-only-question"),
            ConversationState.empty(user_id=USER_KEY, conversation_id=CONVERSATION_ID),
        )
    )

    assert client.calls == 1
    assert [item.action_type for item in interpretation.required_actions] == [
        "capture_daily_event",
        "answer_case_query",
    ]
    assert [item.text for item in interpretation.segments] == [fact, question]


def test_valid_fact_survives_when_model_combines_it_with_question_segment() -> None:
    fact = "今天完成合同复核"
    question = "这个案件最近有什么风险？"
    text = f"{fact}，{question}"
    client = _PayloadClient(
        {
            "intents": ["daily_append", "case_query"],
            "segments": [
                {
                    "segment_id": "combined-segment",
                    "text": text,
                    "intents": ["daily_append", "case_query"],
                    "entity_ids": ["daily-fact", "case-question"],
                    "action_ids": ["capture-fact", "answer-question"],
                }
            ],
            "entities": [
                {
                    "entity_id": "daily-fact",
                    "entity_type": "daily_event",
                    "value": fact,
                    "confidence": 1.0,
                    "attributes": {
                        "field": "today_work",
                        "statement_mode": "asserted",
                    },
                },
                {
                    "entity_id": "case-question",
                    "entity_type": "case_query",
                    "value": question,
                    "confidence": 1.0,
                    "attributes": {"question": question},
                },
            ],
            "confidence": 1.0,
            "required_actions": [
                {
                    "action_id": "capture-fact",
                    "action_type": "capture_daily_event",
                    "intent": "daily_append",
                    "entity_ids": ["daily-fact"],
                    "parameters": {},
                },
                {
                    "action_id": "answer-question",
                    "action_type": "answer_case_query",
                    "intent": "case_query",
                    "entity_ids": ["case-question"],
                    "parameters": {},
                },
            ],
            "clarification_need": None,
            "context_update": {
                "current_goal": "daily_append",
                "remember_entity_ids": ["daily-fact", "case-question"],
                "remember_turn": True,
            },
        }
    )

    interpretation = asyncio.run(
        LLMCognitiveSemanticInterpreter(
            client,
            legacy_semantic_enforcers_enabled=False,
        ).interpret(
            _turn(text, "combined-fact-plus-question"),
            ConversationState.empty(
                user_id=USER_KEY,
                conversation_id=CONVERSATION_ID,
            ),
        )
    )

    assert [item.action_type for item in interpretation.required_actions] == [
        "capture_daily_event",
        "answer_case_query",
    ]


def test_action_free_explicit_fact_segment_is_recovered_beside_question() -> None:
    question = "证据目录什么时候提交？"
    fact = "今天完成了合同审核。"
    text = f"{question}{fact}"
    client = _PayloadClient(
        {
            "intents": ["internal_query", "chat"],
            "segments": [
                {
                    "segment_id": "question",
                    "text": question,
                    "intents": ["internal_query"],
                    "entity_ids": ["query"],
                    "action_ids": ["search"],
                },
                {
                    "segment_id": "fact",
                    "text": fact,
                    "intents": ["chat"],
                    "entity_ids": [],
                    "action_ids": [],
                },
            ],
            "entities": [
                {
                    "entity_id": "query",
                    "entity_type": "knowledge_query",
                    "value": question,
                    "confidence": 1.0,
                    "attributes": {"query": question, "topic": "evidence_submission"},
                }
            ],
            "confidence": 1.0,
            "required_actions": [
                {
                    "action_id": "search",
                    "action_type": "search_enterprise_knowledge",
                    "intent": "internal_query",
                    "entity_ids": ["query"],
                    "parameters": {},
                }
            ],
            "clarification_need": None,
            "context_update": {"remember_turn": True},
        }
    )

    interpretation = asyncio.run(
        LLMCognitiveSemanticInterpreter(client).interpret(
            _turn(text, "question-and-action-free-fact"),
            ConversationState.empty(user_id=USER_KEY, conversation_id=CONVERSATION_ID),
        )
    )

    assert [item.action_type for item in interpretation.required_actions] == [
        "search_enterprise_knowledge",
        "capture_daily_event",
    ]
    daily = next(item for item in interpretation.entities if item.entity_type == "daily_event")
    assert daily.attributes["field"] == "today_work"
    assert daily.attributes["statement_mode"] == "asserted"
    assert fact in [item.text for item in interpretation.segments]


def test_action_free_explicit_fact_segment_is_recovered_beside_quoted_claim() -> None:
    quoted = "同事说某案件已经结束；"
    fact = "我今天完成了合同归档。"
    text = f"{quoted}{fact}"
    client = _PayloadClient(
        {
            "intents": ["case_discussion", "chat"],
            "segments": [
                {
                    "segment_id": "quoted",
                    "text": quoted,
                    "intents": ["case_discussion"],
                    "entity_ids": ["quoted-case"],
                    "action_ids": [],
                },
                {
                    "segment_id": "fact",
                    "text": fact,
                    "intents": ["chat"],
                    "entity_ids": [],
                    "action_ids": [],
                },
            ],
            "entities": [
                {
                    "entity_id": "quoted-case",
                    "entity_type": "case_ref",
                    "value": "某案件",
                    "confidence": 1.0,
                    "attributes": {"statement_mode": "quoted"},
                }
            ],
            "confidence": 1.0,
            "required_actions": [],
            "clarification_need": None,
            "context_update": {"remember_turn": True},
        }
    )

    interpretation = asyncio.run(
        LLMCognitiveSemanticInterpreter(client).interpret(
            _turn(text, "quote-and-action-free-fact"),
            ConversationState.empty(user_id=USER_KEY, conversation_id=CONVERSATION_ID),
        )
    )

    assert [item.action_type for item in interpretation.required_actions] == [
        "capture_daily_event"
    ]
    assert not any(
        item.action_type in {"record_case_progress", "update_case_progress"}
        for item in interpretation.required_actions
    )
    daily = next(item for item in interpretation.entities if item.entity_type == "daily_event")
    assert daily.attributes["field"] == "today_work"
    assert daily.attributes["statement_mode"] == "asserted"


def test_explicit_fact_is_recovered_when_model_merges_quote_and_fact_segment() -> None:
    text = "同事说某案件已经结束；我今天完成了合同归档。"
    client = _PayloadClient(
        {
            "intents": ["chat"],
            "segments": [
                {
                    "segment_id": "provider-merged-quoted-segment",
                    "text": text,
                    "intents": ["chat"],
                    "entity_ids": ["quoted-case"],
                    "action_ids": [],
                    "start_offset": 0,
                    "end_offset": len(text),
                }
            ],
            "entities": [
                {
                    "entity_id": "quoted-case",
                    "entity_type": "case_ref",
                    "value": "某案件",
                    "confidence": 1.0,
                    "attributes": {
                        "statement_mode": "quoted",
                        "evidence_spans": [[0, len(text)]],
                    },
                }
            ],
            "confidence": 1.0,
            "required_actions": [],
            "clarification_need": None,
            "context_update": {"remember_turn": True},
        }
    )

    interpretation = asyncio.run(
        LLMCognitiveSemanticInterpreter(client).interpret(
            _turn(text, "provider-merged-quote-and-fact"),
            ConversationState.empty(
                user_id=USER_KEY,
                conversation_id=CONVERSATION_ID,
            ),
        )
    )

    assert [item.action_type for item in interpretation.required_actions] == [
        "capture_daily_event"
    ]
    assert not any(
        item.action_type in {"record_case_progress", "update_case_progress"}
        for item in interpretation.required_actions
    )
    daily = next(
        item for item in interpretation.entities if item.entity_type == "daily_event"
    )
    assert daily.value == "完成了合同归档"
    assert daily.attributes["field"] == "today_work"
    assert daily.attributes["statement_mode"] == "asserted"
    daily_action_id = next(
        item.action_id
        for item in interpretation.required_actions
        if item.action_type == "capture_daily_event"
    )
    daily_segment = next(
        item for item in interpretation.segments if daily_action_id in item.action_ids
    )
    assert daily_segment.text == "我今天完成了合同归档。"
    assert daily_segment.start_offset == text.index("我今天")
    assert daily_segment.end_offset == len(text)
    quoted_segment = next(
        item for item in interpretation.segments if "同事说某案件已经结束" in item.text
    )
    assert quoted_segment.action_ids == ()
    assert quoted_segment.start_offset == 0
    assert quoted_segment.end_offset == text.index("我今天")


@pytest.mark.parametrize(
    "text",
    (
        "如果法院回复，再更新；我今天完成了合同归档。",
        "云璟府案件结束了吗？我今天完成了合同归档。",
        "会议纪要写着案件已经结束。\n我今天完成了合同归档。",
    ),
)
def test_explicit_fact_is_recovered_beside_independent_nonassertive_clause(
    text: str,
) -> None:
    client = _PayloadClient(
        {
            "intents": ["chat"],
            "segments": [
                {
                    "segment_id": "provider-merged-nonassertive-segment",
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
            "context_update": {"remember_turn": True},
        }
    )

    message_id = f"merged-nonassertive-{hashlib.sha256(text.encode()).hexdigest()[:8]}"
    interpretation = asyncio.run(
        LLMCognitiveSemanticInterpreter(client).interpret(
            _turn(text, message_id),
            ConversationState.empty(
                user_id=USER_KEY,
                conversation_id=CONVERSATION_ID,
            ),
        )
    )

    assert [item.action_type for item in interpretation.required_actions] == [
        "capture_daily_event"
    ]
    daily = next(
        item for item in interpretation.entities if item.entity_type == "daily_event"
    )
    assert daily.value == "完成了合同归档"
    assert sum(bool(item.action_ids) for item in interpretation.segments) == 1


@pytest.mark.parametrize(
    "text",
    (
        "如果我今天完成了合同归档；再更新。",
        "同事问我今天完成了合同归档吗？",
        "会议纪要写着我今天完成了合同归档。",
    ),
)
def test_merged_nonassertive_clauses_do_not_create_daily_mutation(text: str) -> None:
    client = _PayloadClient(
        {
            "intents": ["chat"],
            "segments": [
                {
                    "segment_id": "provider-nonassertive-only-segment",
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
            "context_update": {"remember_turn": True},
        }
    )

    message_id = f"nonassertive-only-{hashlib.sha256(text.encode()).hexdigest()[:8]}"
    interpretation = asyncio.run(
        LLMCognitiveSemanticInterpreter(client).interpret(
            _turn(text, message_id),
            ConversationState.empty(
                user_id=USER_KEY,
                conversation_id=CONVERSATION_ID,
            ),
        )
    )

    assert not any(
        item.action_type == "capture_daily_event"
        for item in interpretation.required_actions
    )


def test_read_only_question_drops_unbound_authority_ref_without_losing_query_action() -> None:
    text = "这个出差需要审批吗？"
    client = _PayloadClient(
        {
            "intents": ["travel_event", "internal_query"],
            "segments": [
                {
                    "segment_id": "travel-policy-question",
                    "text": text,
                    "intents": ["travel_event", "internal_query"],
                    "entity_ids": ["unused-travel-ref", "policy-query"],
                    "action_ids": ["search-policy"],
                }
            ],
            "entities": [
                {
                    "entity_id": "unused-travel-ref",
                    "entity_type": "travel_intent_ref",
                    "value": "当前出差",
                    "confidence": 1.0,
                    "attributes": {
                        "travel_intent_id": TRAVEL_ID,
                        "expected_version": 3,
                        "requested_change": "approval_status",
                    },
                },
                {
                    "entity_id": "policy-query",
                    "entity_type": "knowledge_query",
                    "value": text,
                    "confidence": 1.0,
                    "attributes": {
                        "query": text,
                        "topic": "travel_approval",
                    },
                },
            ],
            "confidence": 1.0,
            "required_actions": [
                {
                    "action_id": "search-policy",
                    "action_type": "search_enterprise_knowledge",
                    "intent": "internal_query",
                    "entity_ids": ["policy-query"],
                    "parameters": {},
                }
            ],
            "clarification_need": None,
            "context_update": {
                "preserve_current_goal": True,
                "remember_entity_ids": ["unused-travel-ref", "policy-query"],
                "remember_turn": True,
            },
        }
    )
    interpreter = LLMCognitiveSemanticInterpreter(client)
    state = replace(
        ConversationState.empty(user_id=USER_KEY, conversation_id=CONVERSATION_ID),
        current_goal=ConversationGoal(intent="travel_event"),
    )

    interpretation = asyncio.run(
        interpreter.interpret(_turn(text, "travel-policy-question"), state)
    )

    assert client.calls == 1
    assert [item.action_type for item in interpretation.required_actions] == [
        "search_enterprise_knowledge"
    ]
    assert [item.entity_id for item in interpretation.entities] == ["policy-query"]
    assert interpretation.context_update.remember_entity_ids == ("policy-query",)


def test_partial_success_reply_keeps_confirmation_question_visible() -> None:
    reply = append_cognitive_clarification(
        "记下了。\n当前日报：\n今日工作\n1. 完成合同审核",
        type(
            "Decision",
            (),
            {
                "clarification_need": type(
                    "Clarification",
                    (),
                    {"question": "确认取消明天南京的出差安排吗？"},
                )()
            },
        )(),
    )

    assert "完成合同审核" in reply
    assert "确认取消明天南京的出差安排吗？" in reply


def test_exact_nevermind_invalidates_bound_pending_without_model_or_action() -> None:
    base = ConversationState.empty(user_id=USER_KEY, conversation_id=CONVERSATION_ID)
    first = _process(
        _pending_proposal("明天南京出差不去了"),
        _turn("明天南京出差不去了", "pending-cancel-request"),
        base,
    )
    interpreter = LLMCognitiveSemanticInterpreter(_NoModelClient())
    core = CognitiveCoreV3(
        interpreter,
        admission_engine=DomainAdmissionEngine(),
        admission_enforced=True,
    )

    cancelled = asyncio.run(
        core.process(
            _turn("算了", "pending-cancel-nevermind"),
            first.state,
        )
    )

    assert cancelled.decision.required_actions == ()
    assert cancelled.decision.context_update.invalidated_pending_ids == (
        first.state.pending[0].pending_id,
    )
    assert cancelled.decision.context_update.pending_invalidation_reason == (
        "cancelled_by_user"
    )
    assert cancelled.state.pending == ()
    assert pending_lifecycle_reply(cancelled.decision) == (
        "好的，已取消这次待确认操作，原操作不会执行。"
    )
    assert interpreter.source_for("pending-cancel-nevermind") == (
        "deterministic_contract"
    )


def test_successful_independent_mutation_supersedes_old_confirmation_authority() -> None:
    base = ConversationState.empty(user_id=USER_KEY, conversation_id=CONVERSATION_ID)
    first = _process(
        _pending_proposal("明天南京出差不去了"),
        _turn("明天南京出差不去了", "pending-supersede-request"),
        base,
    )
    text = "日报补完成合同复核"
    proposal = SemanticInterpretation.from_payload(
        {
            "intents": ["daily_append"],
            "segments": [
                {
                    "segment_id": "new-daily-instruction",
                    "text": text,
                    "intents": ["daily_append"],
                    "entity_ids": ["new-daily-fact"],
                    "action_ids": ["capture-new-daily-fact"],
                }
            ],
            "entities": [
                {
                    "entity_id": "new-daily-fact",
                    "entity_type": "daily_event",
                    "value": "完成合同复核",
                    "confidence": 1.0,
                    "attributes": {"field": "today_work"},
                }
            ],
            "confidence": 1.0,
            "required_actions": [
                {
                    "action_id": "capture-new-daily-fact",
                    "action_type": "capture_daily_event",
                    "intent": "daily_append",
                    "entity_ids": ["new-daily-fact"],
                }
            ],
            "clarification_need": None,
            "context_update": {
                "current_goal": "daily_report",
                "remember_entity_ids": ["new-daily-fact"],
                "remember_turn": True,
            },
        }
    )
    core = CognitiveCoreV3(_Interpreter(lambda _turn, _state: proposal))

    superseded = asyncio.run(
        core.process(
            _turn(text, "pending-supersede-new-command"),
            first.state,
        )
    )

    assert superseded.decision.context_update.invalidated_pending_ids == (
        first.state.pending[0].pending_id,
    )
    assert superseded.decision.context_update.pending_invalidation_reason == (
        "superseded_by_new_instruction"
    )
    assert superseded.state.pending == ()
    assert [action.action_type for action in superseded.decision.required_actions] == [
        "capture_daily_event"
    ]

    plan = CognitiveCommandPlanner().plan(
        superseded.decision,
        CommandPlanningContext(
            message_id="pending-supersede-new-command",
            actor_user_id=uuid5(NAMESPACE_URL, ACTOR_ID),
        ),
    )
    orchestration = CognitiveOrchestrationResult(
        decision=superseded.decision,
        base_state=first.state,
        state=superseded.state,
        command_plan=plan,
        state_persisted=False,
    )
    store = InMemoryConversationStateStore((first.state,))

    failed = asyncio.run(
        finalize_cognitive_state_after_execution(
            result=orchestration,
            state_store=store,
            execution_succeeded=False,
        )
    )

    assert failed.pending == ()
    assert failed.version == first.state.version + 1
    assert failed.current_goal == first.state.current_goal
    assert all(
        item.entity_id != "new-daily-fact" for item in failed.current_entities
    )


def test_unbound_travel_correction_returns_clarification_without_schema_retries() -> None:
    text = "不是明天，是后天"
    client = _PayloadClient(
        {
            "intents": ["travel_event"],
            "segments": [
                {
                    "segment_id": "unbound-travel-correction",
                    "text": text,
                    "intents": ["travel_event"],
                    "entity_ids": ["unbound-travel-ref"],
                    "action_ids": [],
                }
            ],
            "entities": [
                {
                    "entity_id": "unbound-travel-ref",
                    "entity_type": "travel_intent_ref",
                    "value": text,
                    "confidence": 1.0,
                    "attributes": {
                        "new_date_hint": "后天",
                        "context_reference": None,
                    },
                }
            ],
            "confidence": 1.0,
            "required_actions": [],
            "clarification_need": {
                "reason": "travel_target_required",
                "missing_fields": ["travel_intent_id"],
                "question": "你要修改哪一项出差安排？",
            },
            "context_update": {"preserve_current_goal": True},
        }
    )
    interpreter = LLMCognitiveSemanticInterpreter(client)

    interpretation = asyncio.run(
        interpreter.interpret(
            _turn(text, "unbound-travel-correction"),
            ConversationState.empty(
                user_id=USER_KEY,
                conversation_id=CONVERSATION_ID,
            ),
        )
    )

    assert client.calls == 1
    assert interpretation.required_actions == ()
    assert interpretation.entities == ()
    assert interpretation.clarification_need is not None
    assert interpretation.clarification_need.reason == "travel_target_required"


@pytest.mark.parametrize(
    ("text", "expected_hint"),
    [
        ("不是明天，是后天", "后天"),
        ("出差日期改为后天", "后天"),
        ("出差时间调整到2026-07-25", "2026-07-25"),
    ],
)
def test_unique_active_travel_context_recovers_explicit_date_correction_from_model_no_op(
    text: str,
    expected_hint: str,
) -> None:
    client = _PayloadClient(
        {
            "intents": ["chat"],
            "segments": [
                {
                    "segment_id": "model-no-op",
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
            "context_update": {"preserve_current_goal": True},
        }
    )
    interpreter = LLMCognitiveSemanticInterpreter(client)
    core = CognitiveCoreV3(
        interpreter,
        admission_engine=DomainAdmissionEngine(),
        admission_enforced=True,
    )
    state = replace(
        ConversationState.empty(user_id=USER_KEY, conversation_id=CONVERSATION_ID),
        current_goal=ConversationGoal(intent="travel_event"),
    )

    result = asyncio.run(core.process(_turn(text, f"recover-{expected_hint}"), state))

    assert client.calls == 1
    assert result.decision.required_actions == ()
    assert result.decision.admission_tickets == ()
    assert result.decision.clarification_need is not None
    assert result.decision.clarification_need.reason == "medium_risk_confirmation_required"
    assert len(result.state.pending) == 1
    pending = result.state.pending[0]
    assert pending.action == "update_travel_event"
    assert len(pending.entity_ids) == 1
    entity = next(
        item for item in result.state.current_entities if item.entity_id == pending.entity_ids[0]
    )
    assert entity.entity_type == "travel_intent_ref"
    assert entity.attributes["travel_intent_id"] == TRAVEL_ID
    assert entity.attributes["expected_version"] == 3
    assert entity.attributes["new_date_hint"] == expected_hint


def test_unique_active_travel_context_reanchors_provider_pending_to_source_and_trusted_row() -> None:
    text = "不是明天，是后天"
    client = _PayloadClient(
        {
            "intents": ["travel_event"],
            "segments": [
                {
                    "segment_id": "provider-travel-correction",
                    "text": text,
                    "intents": ["travel_event"],
                    "entity_ids": ["provider-travel-ref"],
                    "action_ids": [],
                }
            ],
            "entities": [
                {
                    "entity_id": "provider-travel-ref",
                    "entity_type": "travel_intent_ref",
                    "value": "出差",
                    "confidence": 1.0,
                    "attributes": {
                        "travel_intent_id": "20000000-0000-0000-0000-000000000002",
                        "expected_version": 99,
                        "new_date_hint": "tomorrow",
                    },
                }
            ],
            "confidence": 1.0,
            "required_actions": [],
            "clarification_need": {
                "reason": "medium_risk_confirmation_required",
                "missing_fields": ["confirmation"],
                "question": "确认修改日期吗？",
            },
            "context_update": {
                "bind_pending": {
                    "pending_id": "provider-pending",
                    "intent": "travel_event",
                    "action": "update_travel_event",
                    "entity_ids": ["provider-travel-ref"],
                    "expires_in_seconds": 600,
                }
            },
        }
    )
    core = CognitiveCoreV3(
        LLMCognitiveSemanticInterpreter(client),
        admission_engine=DomainAdmissionEngine(),
        admission_enforced=True,
    )
    state = replace(
        ConversationState.empty(user_id=USER_KEY, conversation_id=CONVERSATION_ID),
        current_goal=ConversationGoal(intent="travel_event"),
    )

    result = asyncio.run(core.process(_turn(text, "reanchor-provider-pending"), state))

    assert client.calls == 1
    assert len(result.state.pending) == 1
    pending = result.state.pending[0]
    entity = next(
        item for item in result.state.current_entities if item.entity_id == pending.entity_ids[0]
    )
    assert entity.attributes["travel_intent_id"] == TRAVEL_ID
    assert entity.attributes["expected_version"] == 3
    assert entity.attributes["new_date_hint"] == "后天"


def test_unique_travel_correction_reanchors_pending_when_model_only_edits_daily_projection() -> None:
    text = "不是明天，是后天"
    client = _PayloadClient(
        {
            "intents": ["travel_event", "daily_modify"],
            "segments": [
                {
                    "segment_id": "provider-coupled-correction",
                    "text": text,
                    "intents": ["travel_event", "daily_modify"],
                    "entity_ids": ["provider-travel", "daily-target"],
                    "action_ids": ["edit-projection"],
                }
            ],
            "entities": [
                {
                    "entity_id": "provider-travel",
                    "entity_type": "travel_intent_ref",
                    "value": "出差",
                    "confidence": 1.0,
                    "attributes": {
                        "travel_intent_id": TRAVEL_ID,
                        "expected_version": 3,
                        "new_date_hint": "后天",
                    },
                },
                {
                    "entity_id": "daily-target",
                    "entity_type": "daily_item_target",
                    "value": "明天的出差安排",
                    "confidence": 1.0,
                    "attributes": {
                        "target_item_ids": ["daily-item-1"],
                        "replacement": "后天的出差安排",
                    },
                },
            ],
            "confidence": 1.0,
            "required_actions": [
                {
                    "action_id": "edit-projection",
                    "action_type": "edit_daily_item",
                    "intent": "daily_modify",
                    "entity_ids": ["daily-target"],
                    "parameters": {},
                }
            ],
            "clarification_need": None,
            "context_update": {
                "bind_pending": {
                    "pending_id": "provider-pending",
                    "intent": "travel_event",
                    "action": "update_travel_event",
                    "entity_ids": ["provider-travel"],
                    "expires_in_seconds": 600,
                }
            },
        }
    )
    core = CognitiveCoreV3(
        LLMCognitiveSemanticInterpreter(client),
        admission_engine=DomainAdmissionEngine(),
        admission_enforced=True,
    )
    state = replace(
        ConversationState.empty(user_id=USER_KEY, conversation_id=CONVERSATION_ID),
        current_goal=ConversationGoal(intent="travel_event"),
    )

    result = asyncio.run(core.process(_turn(text, "coupled-provider-pending"), state))

    assert client.calls == 1
    assert result.decision.required_actions == ()
    assert result.decision.admission_tickets == ()
    assert result.decision.clarification_need is not None
    assert result.decision.clarification_need.reason == "medium_risk_confirmation_required"
    assert len(result.state.pending) == 1
    pending = result.state.pending[0]
    entity = next(
        item for item in result.state.current_entities if item.entity_id == pending.entity_ids[0]
    )
    assert entity.attributes["travel_intent_id"] == TRAVEL_ID
    assert entity.attributes["expected_version"] == 3
    assert entity.attributes["new_date_hint"] == "后天"


@pytest.mark.parametrize(
    ("text", "active_goal", "duplicate"),
    [
        ("不是明天，是后天", False, False),
        ("不是明天，是后天", True, True),
        ("出差日期改成后天吗？", True, False),
        ("如果出差日期改成后天", True, False),
    ],
)
def test_travel_date_correction_recovery_stays_fail_closed_without_exact_authority(
    text: str,
    active_goal: bool,
    duplicate: bool,
) -> None:
    client = _PayloadClient(
        {
            "intents": ["chat"],
            "segments": [
                {
                    "segment_id": "model-no-op",
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
            "context_update": {"preserve_current_goal": True},
        }
    )
    core = CognitiveCoreV3(
        LLMCognitiveSemanticInterpreter(client),
        admission_engine=DomainAdmissionEngine(),
        admission_enforced=True,
    )
    state = ConversationState.empty(user_id=USER_KEY, conversation_id=CONVERSATION_ID)
    if active_goal:
        state = replace(state, current_goal=ConversationGoal(intent="travel_event"))

    result = asyncio.run(
        core.process(
            _turn(text, "fail-closed-correction", resources=_travel_resources(duplicate=duplicate)),
            state,
        )
    )

    assert client.calls == 1
    assert result.decision.required_actions == ()
    assert result.decision.admission_tickets == ()
    assert result.state.pending == ()


def test_travel_confirmation_binding_discards_non_travel_sibling_entity() -> None:
    text = "不是明天，是后天"
    client = _PayloadClient(
        {
            "intents": ["travel_event", "daily_modify"],
            "segments": [
                {
                    "segment_id": "travel-and-daily-correction",
                    "text": text,
                    "intents": ["travel_event", "daily_modify"],
                    "entity_ids": ["travel-ref", "daily-target"],
                    "action_ids": ["update-travel", "edit-daily"],
                }
            ],
            "entities": [
                {
                    "entity_id": "travel-ref",
                    "entity_type": "travel_intent_ref",
                    "value": "南京出差",
                    "confidence": 1.0,
                    "attributes": {
                        "travel_intent_id": TRAVEL_ID,
                        "expected_version": 3,
                        "destination": "南京",
                        "new_date_hint": "后天",
                    },
                },
                {
                    "entity_id": "daily-target",
                    "entity_type": "daily_item_target",
                    "value": "明天南京出差",
                    "confidence": 1.0,
                    "attributes": {
                        "target_item_ids": ["daily-item-1"],
                        "replacement": "后天南京出差",
                    },
                },
            ],
            "confidence": 1.0,
            "required_actions": [
                {
                    "action_id": "update-travel",
                    "action_type": "update_travel_event",
                    "intent": "travel_event",
                    "entity_ids": ["travel-ref"],
                    "parameters": {},
                },
                {
                    "action_id": "edit-daily",
                    "action_type": "edit_daily_item",
                    "intent": "daily_modify",
                    "entity_ids": ["daily-target"],
                    "parameters": {},
                },
            ],
            "clarification_need": {
                "reason": "medium_risk_confirmation_required",
                "missing_fields": [],
                "question": "确认修改南京出差日期吗？",
            },
            "context_update": {
                "bind_pending": {
                    "pending_id": "model-pending",
                    "intent": "travel_event",
                    "action": "update_travel_event",
                    "entity_ids": ["travel-ref", "daily-target"],
                    "expires_in_seconds": 600,
                }
            },
        }
    )
    interpreter = LLMCognitiveSemanticInterpreter(client)

    interpretation = asyncio.run(
        interpreter.interpret(
            _turn(text, "narrow-travel-pending"),
            ConversationState.empty(
                user_id=USER_KEY,
                conversation_id=CONVERSATION_ID,
            ),
        )
    )

    assert client.calls == 1
    assert interpretation.context_update.bind_pending is not None
    assert interpretation.context_update.bind_pending.entity_ids == ("travel-ref",)
    assert [item.action_type for item in interpretation.required_actions] == [
        "update_travel_event"
    ]
    assert [item.entity_id for item in interpretation.entities] == ["travel-ref"]


def test_travel_confirmation_keeps_an_independent_daily_segment() -> None:
    text = "日报补合同审核，另外南京出差改成后天"
    client = _PayloadClient(
        {
            "intents": ["daily_append", "travel_event"],
            "segments": [
                {
                    "segment_id": "daily-segment",
                    "text": "日报补合同审核",
                    "intents": ["daily_append"],
                    "entity_ids": ["daily-event"],
                    "action_ids": ["capture-daily"],
                },
                {
                    "segment_id": "travel-segment",
                    "text": "南京出差改成后天",
                    "intents": ["travel_event"],
                    "entity_ids": ["travel-ref"],
                    "action_ids": ["update-travel"],
                },
            ],
            "entities": [
                {
                    "entity_id": "daily-event",
                    "entity_type": "daily_event",
                    "value": "合同审核",
                    "confidence": 1.0,
                    "attributes": {
                        "field": "today_work",
                        "statement_mode": "asserted",
                    },
                },
                {
                    "entity_id": "travel-ref",
                    "entity_type": "travel_intent_ref",
                    "value": "南京出差",
                    "confidence": 1.0,
                    "attributes": {
                        "travel_intent_id": TRAVEL_ID,
                        "expected_version": 3,
                        "destination": "南京",
                        "new_date_hint": "后天",
                    },
                },
            ],
            "confidence": 1.0,
            "required_actions": [
                {
                    "action_id": "capture-daily",
                    "action_type": "capture_daily_event",
                    "intent": "daily_append",
                    "entity_ids": ["daily-event"],
                    "parameters": {},
                },
                {
                    "action_id": "update-travel",
                    "action_type": "update_travel_event",
                    "intent": "travel_event",
                    "entity_ids": ["travel-ref"],
                    "parameters": {},
                },
            ],
            "clarification_need": {
                "reason": "medium_risk_confirmation_required",
                "missing_fields": [],
                "question": "确认修改南京出差日期吗？",
            },
            "context_update": {
                "bind_pending": {
                    "pending_id": "model-pending",
                    "intent": "travel_event",
                    "action": "update_travel_event",
                    "entity_ids": ["travel-ref"],
                    "expires_in_seconds": 600,
                }
            },
        }
    )
    interpreter = LLMCognitiveSemanticInterpreter(client)

    interpretation = asyncio.run(
        interpreter.interpret(
            _turn(text, "independent-daily-and-travel"),
            ConversationState.empty(
                user_id=USER_KEY,
                conversation_id=CONVERSATION_ID,
            ),
        )
    )

    assert [item.action_type for item in interpretation.required_actions] == [
        "capture_daily_event",
        "update_travel_event",
    ]
    assert [item.entity_id for item in interpretation.entities] == [
        "daily-event",
        "travel-ref",
    ]


def test_travel_pending_repairs_one_missing_segment_entity_reference() -> None:
    text = "move the Nanjing trip to the day after tomorrow"
    client = _PayloadClient(
        {
            "intents": ["travel_event"],
            "segments": [
                {
                    "segment_id": "travel-segment",
                    "text": text,
                    "start_offset": 0,
                    "end_offset": len(text),
                    "intents": ["travel_event"],
                    "entity_ids": [],
                    "action_ids": [],
                }
            ],
            "entities": [
                {
                    "entity_id": "travel-ref",
                    "entity_type": "travel_intent_ref",
                    "value": "Nanjing trip",
                    "confidence": 1.0,
                    "attributes": {
                        "travel_intent_id": TRAVEL_ID,
                        "expected_version": 3,
                        "destination": "Nanjing",
                        "new_date_hint": "day_after_tomorrow",
                        "evidence_spans": [[0, len(text)]],
                    },
                }
            ],
            "confidence": 1.0,
            "required_actions": [],
            "clarification_need": {
                "reason": "medium_risk_confirmation_required",
                "missing_fields": [],
                "question": "Confirm the trip date change?",
            },
            "context_update": {
                "bind_pending": {
                    "pending_id": "model-pending",
                    "intent": "travel_event",
                    "action": "update_travel_event",
                    "entity_ids": ["travel-ref"],
                    "expires_in_seconds": 600,
                }
            },
        }
    )

    interpretation = asyncio.run(
        LLMCognitiveSemanticInterpreter(client).interpret(
            _turn(text, "repair-one-travel-segment-reference"),
            ConversationState.empty(
                user_id=USER_KEY,
                conversation_id=CONVERSATION_ID,
            ),
        )
    )

    assert client.calls == 1
    assert interpretation.context_update.bind_pending is not None
    assert interpretation.context_update.bind_pending.entity_ids == ("travel-ref",)
    assert interpretation.segments[0].entity_ids == ("travel-ref",)
    assert "evidence_spans" not in interpretation.entities[0].attributes


def test_travel_confirmation_suppresses_overlapping_daily_projection_segment() -> None:
    text = "not tomorrow, move the Nanjing trip to the day after tomorrow"
    client = _PayloadClient(
        {
            "intents": ["travel_event", "daily_modify"],
            "segments": [
                {
                    "segment_id": "travel-segment",
                    "text": text,
                    "start_offset": 0,
                    "end_offset": len(text),
                    "intents": ["travel_event"],
                    "entity_ids": ["travel-ref"],
                    "action_ids": [],
                },
                {
                    "segment_id": "duplicate-daily-projection",
                    "text": text,
                    "start_offset": 0,
                    "end_offset": len(text),
                    "intents": ["daily_modify"],
                    "entity_ids": ["daily-target"],
                    "action_ids": [],
                },
            ],
            "entities": [
                {
                    "entity_id": "travel-ref",
                    "entity_type": "travel_intent_ref",
                    "value": "Nanjing trip",
                    "confidence": 1.0,
                    "attributes": {
                        "travel_intent_id": TRAVEL_ID,
                        "expected_version": 3,
                        "destination": "Nanjing",
                        "new_date_hint": "day_after_tomorrow",
                    },
                },
                {
                    "entity_id": "daily-target",
                    "entity_type": "daily_item_target",
                    "value": "tomorrow Nanjing trip",
                    "confidence": 1.0,
                    "attributes": {
                        "target_item_ids": ["daily-item-1"],
                        "replacement": "day-after-tomorrow Nanjing trip",
                    },
                },
            ],
            "confidence": 1.0,
            "required_actions": [],
            "clarification_need": {
                "reason": "medium_risk_confirmation_required",
                "missing_fields": [],
                "question": "Confirm the trip date change?",
            },
            "context_update": {
                "bind_pending": {
                    "pending_id": "model-pending",
                    "intent": "travel_event",
                    "action": "update_travel_event",
                    "entity_ids": ["travel-ref"],
                    "expires_in_seconds": 600,
                }
            },
        }
    )

    interpretation = asyncio.run(
        LLMCognitiveSemanticInterpreter(client).interpret(
            _turn(text, "suppress-overlapping-daily-projection"),
            ConversationState.empty(
                user_id=USER_KEY,
                conversation_id=CONVERSATION_ID,
            ),
        )
    )

    assert client.calls == 1
    assert interpretation.context_update.bind_pending is not None
    assert interpretation.required_actions == ()
    assert [item.entity_id for item in interpretation.entities] == ["travel-ref"]


def test_asserted_travel_repairs_provider_evidence_to_exact_source_segment() -> None:
    text = "\u660e\u5929\u53bb\u82cf\u5dde\u51fa\u5dee\u53c2\u52a0\u9879\u76ee\u6c9f\u901a"
    client = _PayloadClient(
        {
            "intents": ["travel_event"],
            "segments": [
                {
                    "segment_id": "travel-segment",
                    "text": text,
                    "intents": ["travel_event"],
                    "entity_ids": ["travel-event"],
                    "action_ids": ["record-travel"],
                }
            ],
            "entities": [
                {
                    "entity_id": "travel-event",
                    "entity_type": "travel_event",
                    "value": "\u82cf\u5dde\u51fa\u5dee",
                    "confidence": 1.0,
                    "attributes": {
                        "destination": "\u82cf\u5dde",
                        "date_hint": "tomorrow",
                        "purpose": "\u9879\u76ee\u6c9f\u901a",
                        "statement_mode": "asserted",
                        "traveler_scope": "self",
                        "evidence_spans": [[0, len(text) + 10]],
                    },
                }
            ],
            "confidence": 1.0,
            "required_actions": [
                {
                    "action_id": "record-travel",
                    "action_type": "record_travel_event",
                    "intent": "travel_event",
                    "entity_ids": ["travel-event"],
                    "parameters": {},
                }
            ],
            "clarification_need": None,
            "context_update": {
                "current_goal": "travel_event",
                "remember_entity_ids": ["travel-event"],
                "remember_turn": True,
            },
        }
    )
    turn = _turn(text, "repair-travel-source-evidence", resources={"timezone": "Asia/Shanghai"})
    state = ConversationState.empty(
        user_id=USER_KEY,
        conversation_id=CONVERSATION_ID,
    )

    interpretation = asyncio.run(
        LLMCognitiveSemanticInterpreter(client).interpret(turn, state)
    )
    admission = DomainAdmissionEngine().admit(turn, state, interpretation)

    assert client.calls == 1
    assert interpretation.entities[0].value == text
    assert interpretation.entities[0].attributes["evidence_spans"] == [
        [0, len(text)]
    ]
    assert admission.decisions[0].status == "admitted"
    assert len(admission.tickets) == 1
