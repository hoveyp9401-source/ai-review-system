from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from app.agent2.cognitive_core_v3 import (
    CognitiveCoreV3,
    CognitiveTurn,
    SemanticInterpretation,
)
from app.agent2.conversation_state import ConversationState
from app.agent2.domain_admission import DomainAdmissionEngine
from app.agent2.command_planner_v3 import (
    CognitiveCommandPlanner,
    CommandPlanningContext,
)
from app.agent2.typed_daily_commands import DailyReportMutationSnapshot


class StaticSemanticInterpreter:
    def __init__(self, proposal: SemanticInterpretation) -> None:
        self._proposal = proposal

    async def interpret(
        self,
        turn: CognitiveTurn,
        state: ConversationState,
    ) -> SemanticInterpretation:
        return self._proposal


def test_shadow_carries_native_admission_artifacts_without_enforcing_filtered_interpretation():
    turn = CognitiveTurn(
        tenant_id="sandbox-agent2-phase2-20260711",
        user_id="user-pang",
        conversation_id="conversation-shadow-admission",
        message_id="message-shadow-admission",
        text="记录今天整理了案件材料",
        occurred_at=datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc),
    )
    proposal = SemanticInterpretation.from_payload(
        {
            "intents": ["daily_append"],
            "entities": [
                {
                    "entity_id": "daily-work",
                    "entity_type": "daily_event",
                    "value": "今天整理了案件材料",
                    "confidence": 0.99,
                    "attributes": {"field": "today_work"},
                }
            ],
            "confidence": 0.99,
            "required_actions": [
                {
                    "action_id": "append-daily",
                    "action_type": "capture_daily_event",
                    "intent": "daily_append",
                    "entity_ids": ["daily-work"],
                }
            ],
            "clarification_need": None,
            "context_update": {"remember_turn": True},
        }
    )
    filtered = SemanticInterpretation.from_payload(
        {
            "intents": ["daily_append"],
            "entities": [],
            "confidence": 0.99,
            "required_actions": [],
            "clarification_need": None,
            "context_update": {},
        }
    )
    ticket = object()
    pending = object()
    trace = object()

    class AdmissionEngine:
        def admit(self, received_turn, state, received_proposal):
            assert received_turn == turn
            assert received_proposal == proposal
            return type(
                "AdmissionResult",
                (),
                {
                    "interpretation": filtered,
                    "tickets": (ticket,),
                    "information_pendings": (pending,),
                    "trace": trace,
                },
            )()

    result = asyncio.run(
        CognitiveCoreV3(
            StaticSemanticInterpreter(proposal),
            admission_engine=AdmissionEngine(),
            admission_enforced=False,
        ).process(
            turn,
            ConversationState.empty(
                user_id=turn.user_id,
                conversation_id=turn.conversation_id,
            ),
        )
    )

    assert result.decision.admission_mode == "shadow"
    assert result.decision.admission_tickets == (ticket,)
    assert result.decision.admission_information_pendings == (pending,)
    assert result.decision.admission_trace is trace
    assert [action.action_id for action in result.decision.required_actions] == [
        "append-daily"
    ]
    owner_id = uuid5(NAMESPACE_URL, "shadow-admission-owner")
    plan = CognitiveCommandPlanner().plan(
        result.decision,
        CommandPlanningContext(
            message_id=turn.message_id,
            actor_user_id=owner_id,
            daily_snapshot=DailyReportMutationSnapshot(
                report_id=uuid5(NAMESPACE_URL, "shadow-admission-report"),
                owner_user_id=owner_id,
                version=2,
                status="collecting",
            ),
        ),
    )
    assert len(plan.daily_commands) == 1
    assert plan.blocked_actions == ()


def test_cognitive_core_cannot_bypass_domain_admission_or_remember_blocked_entity():
    text = "日报记：今天整理了案件材料；云璟府案今天与法院沟通了"
    turn = CognitiveTurn(
        tenant_id="sandbox-agent2-phase2-20260711",
        user_id="user-pang",
        conversation_id="conversation-core-admission",
        message_id="message-core-admission",
        text=text,
        occurred_at=datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc),
        resources={
            "active_tasks": [
                {
                    "workflow": "daily_report",
                    "task_id": "daily-2026-07-14",
                    "status": "collecting",
                    "reply_candidate": True,
                    "metadata": {"report_date": "2026-07-14"},
                }
            ],
            "daily_draft": {
                "report_id": "daily-report-2026-07-14",
                "status": "collecting",
                "version": 3,
            },
            "daily_policy": {"current_report_date": "2026-07-14"},
            "visible_cases": [
                {
                    "case_id": "case-1",
                    "case_name": "云璟府物业服务合同纠纷案",
                    "confirmed_aliases": ["云璟府案"],
                    "version": 4,
                }
            ],
        },
    )
    proposal = SemanticInterpretation.from_payload(
        {
            "intents": ["daily_append", "case_progress"],
            "segments": [
                {
                    "segment_id": "daily-segment",
                    "text": "日报记：今天整理了案件材料",
                    "intents": ["daily_append", "case_progress"],
                    "entity_ids": ["daily-work", "borrowed-case"],
                    "action_ids": ["append-daily", "record-borrowed-case"],
                }
            ],
            "entities": [
                {
                    "entity_id": "daily-work",
                    "entity_type": "daily_event",
                    "value": "今天整理了案件材料",
                    "confidence": 0.99,
                    "attributes": {"field": "today_work"},
                },
                {
                    "entity_id": "borrowed-case",
                    "entity_type": "case_ref",
                    "value": "云璟府案",
                    "confidence": 0.99,
                    "attributes": {"normalized_fact": "今天整理了案件材料"},
                },
            ],
            "confidence": 0.99,
            "required_actions": [
                {
                    "action_id": "append-daily",
                    "action_type": "capture_daily_event",
                    "intent": "daily_append",
                    "entity_ids": ["daily-work"],
                },
                {
                    "action_id": "record-borrowed-case",
                    "action_type": "record_case_progress",
                    "intent": "case_progress",
                    "entity_ids": ["borrowed-case"],
                },
            ],
            "clarification_need": None,
            "context_update": {
                "current_goal": "daily_append",
                "remember_entity_ids": ["daily-work", "borrowed-case"],
                "remember_turn": True,
            },
        }
    )
    state = ConversationState.empty(
        user_id=turn.user_id,
        conversation_id=turn.conversation_id,
    )

    result = asyncio.run(
        CognitiveCoreV3(
            StaticSemanticInterpreter(proposal),
            admission_engine=DomainAdmissionEngine(),
        ).process(turn, state)
    )

    assert [action.action_id for action in result.decision.required_actions] == [
        "append-daily"
    ]
    assert [ticket.action_id for ticket in result.decision.admission_tickets] == [
        "append-daily"
    ]
    assert result.decision.admission_trace is not None
    assert {
        entity.entity_id for entity in result.state.current_entities
    } == {"daily-work"}
    assert result.state.recent_context[-1].entity_ids == ("daily-work",)
    plan = CognitiveCommandPlanner().plan(
        result.decision,
        CommandPlanningContext(
            message_id=turn.message_id,
            actor_user_id=uuid5(NAMESPACE_URL, turn.user_id),
        ),
    )
    assert (
        "record-borrowed-case",
        "case_reference_not_grounded_in_segment",
    ) in {
        (block.action_id, block.reason_code) for block in plan.blocked_actions
    }
