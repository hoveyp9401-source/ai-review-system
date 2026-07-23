from datetime import datetime, timezone
from uuid import UUID

from app.agent2.cognitive_core_v3 import SemanticInterpretation, CognitiveTurn
from app.agent2.conversation_state import ConversationState
from app.agent2.domain_admission import DomainAdmissionEngine


NOW = datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)


def test_generic_case_material_candidate_cannot_steal_an_active_daily_turn():
    text = (
        "日常用印资料审核，来函台账登记，ERP系统来函录入，"
        "案件材料签收、扫描班车带至西环，用印事宜沟通答疑，"
        "帮助律师资料查找，来函闭环跟进"
    )
    turn = CognitiveTurn(
        tenant_id="sandbox-agent2-phase2-20260711",
        user_id="user-pang",
        conversation_id="conversation-admission-tracer",
        message_id="message-admission-tracer",
        text=text,
        occurred_at=NOW,
        resources={
            "admission_scope": {
                "tenant_id": "other-tenant",
                "user_id": "other-user",
                "conversation_id": "conversation-admission-tracer",
            },
            "active_tasks": [
                {
                    "workflow": "daily_report",
                    "task_id": "daily-2026-07-14",
                    "status": "collecting",
                }
            ],
            "daily_draft": {
                "report_id": "daily-report-2026-07-14",
                "report_date": "2026-07-14",
                "status": "collecting",
                "version": 3,
            },
            "visible_cases": [
                {
                    "case_id": "case-1",
                    "case_number": "（2026）苏01民初1号",
                    "case_name": "云璟府物业服务合同纠纷案",
                    "external_case_id": "BGGL-2512-0023",
                    "confirmed_aliases": ["云璟府案"],
                    "version": 1,
                }
            ],
        },
    )
    proposal = SemanticInterpretation.from_payload(
        {
            "intents": ["daily_append", "case_progress"],
            "segments": [
                {
                    "segment_id": "work-list",
                    "text": text,
                    "intents": ["daily_append", "case_progress"],
                    "entity_ids": ["daily-work", "generic-case-material"],
                    "action_ids": ["append-daily", "record-case"],
                }
            ],
            "entities": [
                {
                    "entity_id": "daily-work",
                    "entity_type": "daily_event",
                    "value": text,
                    "confidence": 0.99,
                    "attributes": {"field": "today_work"},
                },
                {
                    "entity_id": "generic-case-material",
                    "entity_type": "case_ref",
                    "value": "案件材料",
                    "confidence": 0.99,
                    "attributes": {},
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
                    "action_id": "record-case",
                    "action_type": "record_case_progress",
                    "intent": "case_progress",
                    "entity_ids": ["generic-case-material"],
                },
            ],
            "clarification_need": None,
            "context_update": {
                "current_goal": "daily_append",
                "remember_entity_ids": ["daily-work"],
                "remember_turn": True,
            },
        }
    )

    result = DomainAdmissionEngine().admit(
        turn,
        ConversationState.empty(
            user_id=turn.user_id,
            conversation_id=turn.conversation_id,
        ),
        proposal,
    )

    assert [action.action_type for action in result.interpretation.required_actions] == [
        "capture_daily_event"
    ]
    decisions = {decision.action_id: decision for decision in result.decisions}
    assert decisions["append-daily"].status == "admitted"
    assert decisions["record-case"].status == "blocked"
    assert decisions["record-case"].reason_code == "case_reference_not_uniquely_authorized"
    assert [ticket.domain for ticket in result.tickets] == ["report"]
    assert result.tickets[0].object_ref == {
        "object_type": "daily_report",
        "stable_id": "daily-report-2026-07-14",
        "version": 3,
    }
    assert result.tickets[0].tenant_id == "sandbox-agent2-phase2-20260711"
    assert result.tickets[0].user_id == "user-pang"


def test_case_reference_cannot_borrow_grounding_from_a_sibling_segment():
    daily_text = "日报记：今天整理了案件材料"
    case_text = "云璟府案今天与法院沟通了执行进展"
    turn = CognitiveTurn(
        tenant_id="sandbox-agent2-phase2-20260711",
        user_id="user-pang",
        conversation_id="conversation-exact-segment",
        message_id="message-exact-segment",
        text=f"{daily_text}；{case_text}",
        occurred_at=NOW,
        resources={
            "admission_scope": {
                "tenant_id": "sandbox-agent2-phase2-20260711",
                "user_id": "user-pang",
                "conversation_id": "conversation-exact-segment",
            },
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
                    "case_number": "（2026）苏01民初1号",
                    "case_name": "云璟府物业服务合同纠纷案",
                    "external_case_id": "BGGL-2512-0023",
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
                    "text": daily_text,
                    "intents": ["daily_append", "case_progress"],
                    "entity_ids": ["daily-work", "borrowed-case"],
                    "action_ids": ["append-daily", "record-borrowed-case"],
                },
                {
                    "segment_id": "case-segment",
                    "text": case_text,
                    "intents": ["case_progress"],
                    "entity_ids": ["explicit-case"],
                    "action_ids": ["record-explicit-case"],
                },
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
                {
                    "entity_id": "explicit-case",
                    "entity_type": "case_ref",
                    "value": "云璟府案",
                    "confidence": 0.99,
                    "attributes": {
                        "normalized_fact": "今天与法院沟通了执行进展",
                        "statement_mode": "asserted",
                        "evidence_spans": [[4, len(case_text)]],
                    },
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
                {
                    "action_id": "record-explicit-case",
                    "action_type": "record_case_progress",
                    "intent": "case_progress",
                    "entity_ids": ["explicit-case"],
                },
            ],
            "clarification_need": None,
            "context_update": {
                "current_goal": "daily_append",
                "remember_entity_ids": ["daily-work", "explicit-case"],
                "remember_turn": True,
            },
        }
    )

    result = DomainAdmissionEngine().admit(
        turn,
        ConversationState.empty(
            user_id=turn.user_id,
            conversation_id=turn.conversation_id,
        ),
        proposal,
    )

    assert {action.action_id for action in result.interpretation.required_actions} == {
        "append-daily",
        "record-explicit-case",
    }
    decisions = {decision.action_id: decision for decision in result.decisions}
    assert decisions["record-borrowed-case"].status == "blocked"
    assert (
        decisions["record-borrowed-case"].reason_code
        == "case_reference_not_grounded_in_segment"
    )
    assert decisions["record-explicit-case"].status == "admitted"
    assert {
        (ticket.segment_id, ticket.domain) for ticket in result.tickets
    } == {
        ("daily-segment", "report"),
        ("case-segment", "case"),
    }


def test_explicit_personal_travel_is_admitted_while_daily_task_is_active():
    text = "明天去南京出差，参加云璟府案庭审"
    turn = CognitiveTurn(
        tenant_id="sandbox-agent2-phase2-20260711",
        user_id="user-pang",
        conversation_id="conversation-travel-admission",
        message_id="message-travel-admission",
        text=text,
        occurred_at=NOW,
        resources={
            "admission_scope": {
                "tenant_id": "sandbox-agent2-phase2-20260711",
                "user_id": "user-pang",
                "conversation_id": "conversation-travel-admission",
            },
            "timezone": "Asia/Shanghai",
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
        },
    )
    proposal = SemanticInterpretation.from_payload(
        {
            "intents": ["travel"],
            "segments": [
                {
                    "segment_id": "travel-segment",
                    "text": text,
                    "intents": ["travel"],
                    "entity_ids": ["travel-event"],
                    "action_ids": ["record-travel"],
                }
            ],
            "entities": [
                {
                    "entity_id": "travel-event",
                    "entity_type": "travel_event",
                    "value": text,
                    "confidence": 0.99,
                    "attributes": {
                        "destination": "南京",
                        "date_hint": "明天",
                        "purpose": "参加云璟府案庭审",
                        "statement_mode": "asserted",
                        "traveler_scope": "self",
                        "evidence_spans": [[0, len(text)]],
                    },
                }
            ],
            "confidence": 0.99,
            "required_actions": [
                {
                    "action_id": "record-travel",
                    "action_type": "record_travel_event",
                    "intent": "travel",
                    "entity_ids": ["travel-event"],
                }
            ],
            "clarification_need": None,
            "context_update": {
                "current_goal": "travel",
                "remember_entity_ids": ["travel-event"],
                "remember_turn": True,
            },
        }
    )

    result = DomainAdmissionEngine().admit(
        turn,
        ConversationState.empty(
            user_id=turn.user_id,
            conversation_id=turn.conversation_id,
        ),
        proposal,
    )

    assert [action.action_id for action in result.interpretation.required_actions] == [
        "record-travel"
    ]
    assert result.decisions[0].status == "admitted"
    assert result.decisions[0].domain == "travel"
    ticket = result.tickets[0]
    UUID(result.trace.trace_id)
    UUID(result.decisions[0].decision_id)
    UUID(ticket.ticket_id)
    assert ticket.object_ref["object_type"] == "travel_intent"
    UUID(ticket.object_ref["stable_id"])
    assert ticket.object_ref["version"] is None
    assert ticket.authority_scope["destination"] == "南京"
    assert ticket.authority_scope["travel_date"] == "2026-07-15"
    assert ticket.segment_text_sha256 == result.interpretation.segments[0].text_hash
    assert ticket.segment_start_offset == 0
    assert ticket.segment_end_offset == len(text)
    assert ticket.expected_conversation_state_version == 0
    assert len(ticket.fact_claims_sha256) == 64
    assert len(ticket.authorized_command_sha256) == 64
    assert set(result.as_dict()) == {
        "contract_version",
        "trace",
        "decisions",
        "tickets",
        "review_items",
        "deferred_events",
        "information_pendings",
        "selection_requests",
    }


def test_travel_with_missing_date_creates_information_pending_and_no_ticket():
    text = "我要去南京出差"
    turn = CognitiveTurn(
        tenant_id="sandbox-agent2-phase2-20260711",
        user_id="user-pang",
        conversation_id="conversation-travel-missing-date",
        message_id="message-travel-missing-date",
        text=text,
        occurred_at=NOW,
        resources={"timezone": "Asia/Shanghai"},
    )
    proposal = SemanticInterpretation.from_payload(
        {
            "intents": ["travel"],
            "segments": [
                {
                    "segment_id": "travel-segment",
                    "text": text,
                    "intents": ["travel"],
                    "entity_ids": ["travel-event"],
                    "action_ids": ["record-travel"],
                }
            ],
            "entities": [
                {
                    "entity_id": "travel-event",
                    "entity_type": "travel_event",
                    "value": text,
                    "confidence": 0.99,
                    "attributes": {
                        "destination": "南京",
                        "date_hint": "",
                        "purpose": "",
                        "statement_mode": "asserted",
                        "traveler_scope": "self",
                        "evidence_spans": [[0, len(text)]],
                    },
                }
            ],
            "confidence": 0.99,
            "required_actions": [
                {
                    "action_id": "record-travel",
                    "action_type": "record_travel_event",
                    "intent": "travel",
                    "entity_ids": ["travel-event"],
                }
            ],
            "clarification_need": None,
            "context_update": {
                "current_goal": "travel",
                "remember_entity_ids": ["travel-event"],
                "remember_turn": True,
            },
        }
    )

    result = DomainAdmissionEngine().admit(
        turn,
        ConversationState.empty(
            user_id=turn.user_id,
            conversation_id=turn.conversation_id,
        ),
        proposal,
    )

    assert result.interpretation.required_actions == ()
    assert result.tickets == ()
    assert result.decisions[0].status == "information_required"
    assert result.decisions[0].ticket_id == ""
    assert len(result.information_pendings) == 1
    pending = result.information_pendings[0]
    assert result.decisions[0].pending_id == pending.pending_id
    assert pending.missing_fields == ("travel_date",)
    assert pending.business_write_allowed is False
    assert pending.tenant_id == turn.tenant_id
    assert pending.user_id == turn.user_id
    assert pending.conversation_id == turn.conversation_id
    assert pending.expected_conversation_state_version == 0
    assert pending.acceptable_answer_forms["field"] == "travel_date"
    assert result.trace.admission_summary == "information_required"
    assert result.as_dict()["information_pendings"] == [pending.as_dict()]


def test_grounded_but_underspecified_travel_date_requests_information() -> None:
    text = "next week I will travel to Beijing for a branch meeting"
    turn = CognitiveTurn(
        tenant_id="sandbox-agent2-phase2-20260711",
        user_id="user-pang",
        conversation_id="conversation-travel-underspecified-date",
        message_id="message-travel-underspecified-date",
        text=text,
        occurred_at=NOW,
        resources={"timezone": "Asia/Shanghai"},
    )
    proposal = SemanticInterpretation.from_payload(
        {
            "intents": ["travel"],
            "segments": [
                {
                    "segment_id": "travel-segment",
                    "text": text,
                    "intents": ["travel"],
                    "entity_ids": ["travel-event"],
                    "action_ids": ["record-travel"],
                }
            ],
            "entities": [
                {
                    "entity_id": "travel-event",
                    "entity_type": "travel_event",
                    "value": text,
                    "confidence": 0.99,
                    "attributes": {
                        "destination": "Beijing",
                        "date_hint": "next week",
                        "purpose": "branch meeting",
                        "statement_mode": "asserted",
                        "traveler_scope": "self",
                    },
                }
            ],
            "confidence": 0.99,
            "required_actions": [
                {
                    "action_id": "record-travel",
                    "action_type": "record_travel_event",
                    "intent": "travel",
                    "entity_ids": ["travel-event"],
                }
            ],
            "clarification_need": None,
            "context_update": {
                "current_goal": "travel",
                "remember_entity_ids": ["travel-event"],
                "remember_turn": True,
            },
        }
    )

    result = DomainAdmissionEngine().admit(
        turn,
        ConversationState.empty(
            user_id=turn.user_id,
            conversation_id=turn.conversation_id,
        ),
        proposal,
    )

    assert result.decisions[0].status == "information_required"
    assert result.decisions[0].reason_code == "travel_time_information_required"
    assert result.tickets == ()
    assert len(result.information_pendings) == 1
    assert result.information_pendings[0].business_write_allowed is False


def test_case_name_without_asserted_grounded_fact_cannot_authorize_progress():
    result = _admit_single_case_fact(
        text="云璟府案",
        attributes={},
    )

    assert result.interpretation.required_actions == ()
    assert result.tickets == ()
    assert result.decisions[0].status == "blocked"
    assert result.decisions[0].reason_code == "case_statement_not_asserted"


def test_case_question_cannot_authorize_progress_even_with_a_visible_case():
    text = "云璟府案今天有新进展吗"
    result = _admit_single_case_fact(
        text=text,
        attributes={
            "normalized_fact": "今天有新进展",
            "statement_mode": "question",
            "evidence_spans": [[4, len(text)]],
        },
    )

    assert result.interpretation.required_actions == ()
    assert result.tickets == ()
    assert result.decisions[0].reason_code == "case_statement_not_asserted"


def test_case_fact_with_model_invented_evidence_span_is_blocked():
    text = "云璟府案今天与法院沟通了执行进展"
    result = _admit_single_case_fact(
        text=text,
        attributes={
            "normalized_fact": "今天与法院沟通了执行进展",
            "statement_mode": "asserted",
            "evidence_spans": [[4, len(text) + 8]],
        },
    )

    assert result.interpretation.required_actions == ()
    assert result.tickets == ()
    assert result.decisions[0].reason_code == "case_evidence_not_grounded"


def test_other_person_or_hypothetical_travel_cannot_authorize_travel_write():
    text = "刘聪明天去南京出差"
    result = _admit_single_travel(
        text=text,
        attributes={
            "destination": "南京",
            "date_hint": "明天",
            "purpose": "",
            "statement_mode": "asserted",
            "traveler_scope": "other",
            "evidence_spans": [[0, len(text)]],
        },
    )

    assert result.interpretation.required_actions == ()
    assert result.tickets == ()
    assert result.decisions[0].reason_code == "travel_not_current_user"


def _admit_single_case_fact(*, text: str, attributes: dict):
    turn = CognitiveTurn(
        tenant_id="sandbox-agent2-phase2-20260711",
        user_id="user-pang",
        conversation_id="conversation-case-fact-contract",
        message_id=f"message-case-fact-{len(text)}-{len(attributes)}",
        text=text,
        occurred_at=NOW,
        resources={
            "visible_cases": [
                {
                    "case_id": "case-1",
                    "case_number": "（2026）苏01民初1号",
                    "case_name": "云璟府物业服务合同纠纷案",
                    "confirmed_aliases": ["云璟府案"],
                    "version": 4,
                }
            ]
        },
    )
    proposal = SemanticInterpretation.from_payload(
        {
            "intents": ["case_progress"],
            "segments": [
                {
                    "segment_id": "case-segment",
                    "text": text,
                    "intents": ["case_progress"],
                    "entity_ids": ["case-ref"],
                    "action_ids": ["record-case"],
                }
            ],
            "entities": [
                {
                    "entity_id": "case-ref",
                    "entity_type": "case_ref",
                    "value": "云璟府案",
                    "confidence": 0.99,
                    "attributes": attributes,
                }
            ],
            "confidence": 0.99,
            "required_actions": [
                {
                    "action_id": "record-case",
                    "action_type": "record_case_progress",
                    "intent": "case_progress",
                    "entity_ids": ["case-ref"],
                }
            ],
            "clarification_need": None,
            "context_update": {"preserve_current_goal": True},
        }
    )
    return DomainAdmissionEngine().admit(
        turn,
        ConversationState.empty(
            user_id=turn.user_id,
            conversation_id=turn.conversation_id,
        ),
        proposal,
    )


def _admit_single_travel(*, text: str, attributes: dict):
    turn = CognitiveTurn(
        tenant_id="sandbox-agent2-phase2-20260711",
        user_id="user-pang",
        conversation_id="conversation-travel-assertion-contract",
        message_id=f"message-travel-contract-{len(text)}",
        text=text,
        occurred_at=NOW,
        resources={"timezone": "Asia/Shanghai"},
    )
    proposal = SemanticInterpretation.from_payload(
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
                    "value": text,
                    "confidence": 0.99,
                    "attributes": attributes,
                }
            ],
            "confidence": 0.99,
            "required_actions": [
                {
                    "action_id": "record-travel",
                    "action_type": "record_travel_event",
                    "intent": "travel_event",
                    "entity_ids": ["travel-event"],
                }
            ],
            "clarification_need": None,
            "context_update": {"preserve_current_goal": True},
        }
    )
    return DomainAdmissionEngine().admit(
        turn,
        ConversationState.empty(
            user_id=turn.user_id,
            conversation_id=turn.conversation_id,
        ),
        proposal,
    )


def test_no_other_risk_is_a_report_no_op_not_a_write():
    text = "没其他风险"
    turn = CognitiveTurn(
        tenant_id="sandbox-agent2-phase2-20260711",
        user_id="user-pang",
        conversation_id="conversation-no-risk",
        message_id="message-no-risk",
        text=text,
        occurred_at=NOW,
        resources={
            "admission_scope": {
                "tenant_id": "sandbox-agent2-phase2-20260711",
                "user_id": "user-pang",
                "conversation_id": "conversation-no-risk",
            },
            "active_tasks": [
                {
                    "workflow": "daily_report",
                    "task_id": "daily-2026-07-14",
                    "status": "collecting",
                    "reply_candidate": True,
                    "metadata": {
                        "report_date": "2026-07-14",
                        "expected_field": "problems",
                        "acceptable_no_item_answers": ["没其他风险"],
                    },
                }
            ],
            "daily_draft": {
                "report_id": "daily-report-2026-07-14",
                "status": "collecting",
                "version": 3,
            },
            "daily_policy": {"current_report_date": "2026-07-14"},
        },
    )
    proposal = SemanticInterpretation.from_payload(
        {
            "intents": ["daily_append"],
            "segments": [
                {
                    "segment_id": "risk-segment",
                    "text": text,
                    "intents": ["daily_append"],
                    "entity_ids": ["risk-answer"],
                    "action_ids": ["append-risk"],
                }
            ],
            "entities": [
                {
                    "entity_id": "risk-answer",
                    "entity_type": "daily_event",
                    "value": text,
                    "confidence": 0.99,
                    "attributes": {"field": "problems"},
                }
            ],
            "confidence": 0.99,
            "required_actions": [
                {
                    "action_id": "append-risk",
                    "action_type": "capture_daily_event",
                    "intent": "daily_append",
                    "entity_ids": ["risk-answer"],
                }
            ],
            "clarification_need": None,
            "context_update": {
                "current_goal": "daily_append",
                "remember_entity_ids": ["risk-answer"],
                "remember_turn": True,
            },
        }
    )

    result = DomainAdmissionEngine().admit(
        turn,
        ConversationState.empty(
            user_id=turn.user_id,
            conversation_id=turn.conversation_id,
        ),
        proposal,
    )

    assert result.interpretation.required_actions == ()
    assert result.decisions[0].status == "no_op"
    assert result.decisions[0].reason_code == "report_no_new_item"
    assert result.tickets == ()


def test_model_invented_segment_cannot_authorize_a_real_visible_case():
    turn = CognitiveTurn(
        tenant_id="sandbox-agent2-phase2-20260711",
        user_id="user-pang",
        conversation_id="conversation-invented-segment",
        message_id="message-invented-segment",
        text="今天整理了案件材料",
        occurred_at=NOW,
        resources={
            "visible_cases": [
                {
                    "case_id": "case-1",
                    "case_name": "云璟府物业服务合同纠纷案",
                    "confirmed_aliases": ["云璟府案"],
                    "version": 4,
                }
            ]
        },
    )
    proposal = SemanticInterpretation.from_payload(
        {
            "intents": ["case_progress"],
            "segments": [
                {
                    "segment_id": "invented-case-segment",
                    "text": "云璟府案今天与法院沟通了执行进展",
                    "intents": ["case_progress"],
                    "entity_ids": ["case-ref"],
                    "action_ids": ["record-case"],
                }
            ],
            "entities": [
                {
                    "entity_id": "case-ref",
                    "entity_type": "case_ref",
                    "value": "云璟府案",
                    "confidence": 0.99,
                    "attributes": {},
                }
            ],
            "confidence": 0.99,
            "required_actions": [
                {
                    "action_id": "record-case",
                    "action_type": "record_case_progress",
                    "intent": "case_progress",
                    "entity_ids": ["case-ref"],
                }
            ],
            "clarification_need": None,
            "context_update": {
                "current_goal": "case_progress",
                "remember_entity_ids": ["case-ref"],
                "remember_turn": True,
            },
        }
    )

    result = DomainAdmissionEngine().admit(
        turn,
        ConversationState.empty(
            user_id=turn.user_id,
            conversation_id=turn.conversation_id,
        ),
        proposal,
    )

    assert result.tickets == ()
    assert result.interpretation.required_actions == ()
    assert result.interpretation.entities == ()
    assert result.interpretation.segments == ()
    assert result.interpretation.intents == ()
    assert result.decisions[0].status == "blocked"
    assert result.decisions[0].reason_code == "segment_not_grounded_in_turn"


def test_action_bound_to_multiple_segments_is_blocked_without_guessing():
    text = "云璟府案今天联系法院；云璟府案今天补交材料"
    turn = CognitiveTurn(
        tenant_id="sandbox-agent2-phase2-20260711",
        user_id="user-pang",
        conversation_id="conversation-duplicate-action-segment",
        message_id="message-duplicate-action-segment",
        text=text,
        occurred_at=NOW,
        resources={
            "visible_cases": [
                {
                    "case_id": "case-1",
                    "case_name": "云璟府物业服务合同纠纷案",
                    "confirmed_aliases": ["云璟府案"],
                    "version": 4,
                }
            ]
        },
    )
    proposal = SemanticInterpretation.from_payload(
        {
            "intents": ["case_progress"],
            "segments": [
                {
                    "segment_id": "case-one",
                    "text": "云璟府案今天联系法院",
                    "intents": ["case_progress"],
                    "entity_ids": ["case-ref"],
                    "action_ids": ["record-case"],
                },
                {
                    "segment_id": "case-two",
                    "text": "云璟府案今天补交材料",
                    "intents": ["case_progress"],
                    "entity_ids": ["case-ref"],
                    "action_ids": ["record-case"],
                },
            ],
            "entities": [
                {
                    "entity_id": "case-ref",
                    "entity_type": "case_ref",
                    "value": "云璟府案",
                    "confidence": 0.99,
                    "attributes": {},
                }
            ],
            "confidence": 0.99,
            "required_actions": [
                {
                    "action_id": "record-case",
                    "action_type": "record_case_progress",
                    "intent": "case_progress",
                    "entity_ids": ["case-ref"],
                }
            ],
            "clarification_need": None,
            "context_update": {},
        }
    )

    result = DomainAdmissionEngine().admit(
        turn,
        ConversationState.empty(
            user_id=turn.user_id,
            conversation_id=turn.conversation_id,
        ),
        proposal,
    )

    assert result.tickets == ()
    assert result.interpretation.required_actions == ()
    assert result.decisions[0].reason_code == "action_segment_ambiguous"
