from __future__ import annotations

from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

import pytest

from app.agent2.cognitive_core_v3 import CognitiveTurn, SemanticInterpretation
from app.agent2.conversation_state import ConversationState
from app.agent2.domain_admission import (
    DomainAdmissionEngine,
    evaluate_assertion_polarity_contract,
)


NOW = datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)
TENANT_ID = "sandbox-agent2-phase2-20260711"
ACTOR_ID = "blind-canary-user-a"
CURRENT_REPORT_ID = str(uuid5(NAMESPACE_URL, "admission-p0:daily:2026-07-14"))
HISTORICAL_REPORT_ID = str(uuid5(NAMESPACE_URL, "admission-p0:daily:2026-07-13"))


def _state(turn: CognitiveTurn) -> ConversationState:
    return ConversationState.empty(
        user_id=turn.user_id,
        conversation_id=turn.conversation_id,
    )


def _daily_resources(*, historical_mutation_allowed: bool) -> dict[str, object]:
    current = {
        "report_id": CURRENT_REPORT_ID,
        "report_date": "2026-07-14",
        "version": 3,
        "status": "collecting",
        "items": [],
    }
    historical = {
        "report_id": HISTORICAL_REPORT_ID,
        "report_date": "2026-07-13",
        "version": 2,
        "status": "collecting",
        "items": [],
    }
    return {
        "timezone": "Asia/Shanghai",
        "daily_policy": {
            "current_report_date": "2026-07-14",
            "historical_mutation_allowed": historical_mutation_allowed,
        },
        "daily_draft": {
            key: value for key, value in current.items() if key != "report_date"
        },
        "daily_reports": [current, historical],
        "active_tasks": [
            {
                "workflow": "daily_report",
                "task_id": CURRENT_REPORT_ID,
                "status": "collecting",
                "metadata": {"report_date": "2026-07-14"},
            }
        ],
    }


def _historical_daily_proposal(
    text: str,
    *,
    include_report_entity: bool = True,
) -> SemanticInterpretation:
    segment_entity_ids = ["daily-event"]
    entities: list[dict[str, object]] = [
        {
            "entity_id": "daily-event",
            "entity_type": "daily_event",
            "value": "联系了法院确认排期",
            "confidence": 0.99,
            "attributes": {"field": "today_work"},
        }
    ]
    if include_report_entity:
        segment_entity_ids.append("historical-report")
        entities.append(
            {
                "entity_id": "historical-report",
                "entity_type": "daily_report",
                "value": "2026-07-13 日报",
                "confidence": 0.99,
                "attributes": {
                    "report_id": HISTORICAL_REPORT_ID,
                    "report_date": "2026-07-13",
                    "version": 2,
                },
            }
        )
    return SemanticInterpretation.from_payload(
        {
            "intents": ["daily_append"],
            "segments": [
                {
                    "segment_id": "daily-segment",
                    "text": text,
                    "intents": ["daily_append"],
                    "entity_ids": segment_entity_ids,
                    "action_ids": ["capture-daily"],
                }
            ],
            "entities": entities,
            "confidence": 0.99,
            "required_actions": [
                {
                    "action_id": "capture-daily",
                    "action_type": "capture_daily_event",
                    "intent": "daily_append",
                    "entity_ids": ["daily-event"],
                }
            ],
            "clarification_need": None,
            "context_update": {},
        }
    )


def test_explicit_historical_daily_target_never_falls_back_to_current_report() -> None:
    text = "补充昨天的日报：联系了法院确认排期"
    turn = CognitiveTurn(
        tenant_id=TENANT_ID,
        actor_user_id=ACTOR_ID,
        user_id=f"{TENANT_ID}:{ACTOR_ID}",
        conversation_id="admission-p0-historical-daily",
        message_id="admission-p0-historical-daily-message",
        text=text,
        occurred_at=NOW,
        resources=_daily_resources(historical_mutation_allowed=False),
    )

    result = DomainAdmissionEngine().admit(
        turn,
        _state(turn),
        _historical_daily_proposal(text),
    )

    assert result.decisions[0].status == "blocked"
    assert result.decisions[0].reason_code == "historical_daily_mutation_blocked"
    assert result.decisions[0].object_ref is None
    assert result.tickets == ()
    assert result.interpretation.required_actions == ()


def test_authorized_historical_daily_append_binds_the_explicit_trusted_snapshot() -> None:
    text = "补充昨天的日报：联系了法院确认排期"
    turn = CognitiveTurn(
        tenant_id=TENANT_ID,
        actor_user_id=ACTOR_ID,
        user_id=f"{TENANT_ID}:{ACTOR_ID}",
        conversation_id="admission-p0-authorized-historical-daily",
        message_id="admission-p0-authorized-historical-daily-message",
        text=text,
        occurred_at=NOW,
        resources=_daily_resources(historical_mutation_allowed=True),
    )

    result = DomainAdmissionEngine().admit(
        turn,
        _state(turn),
        _historical_daily_proposal(text),
    )

    assert result.decisions[0].status == "admitted"
    assert result.decisions[0].object_ref == {
        "object_type": "daily_report",
        "stable_id": HISTORICAL_REPORT_ID,
        "version": 2,
    }
    assert len(result.tickets) == 1
    assert result.tickets[0].authority_scope["report_id"] == HISTORICAL_REPORT_ID
    assert result.tickets[0].authority_scope["version"] == 2


def test_historical_daily_wording_without_a_bound_report_entity_fails_closed() -> None:
    text = "补充昨天的日报：联系了法院确认排期"
    turn = CognitiveTurn(
        tenant_id=TENANT_ID,
        actor_user_id=ACTOR_ID,
        user_id=f"{TENANT_ID}:{ACTOR_ID}",
        conversation_id="admission-p0-unbound-historical-daily",
        message_id="admission-p0-unbound-historical-daily-message",
        text=text,
        occurred_at=NOW,
        resources=_daily_resources(historical_mutation_allowed=True),
    )

    result = DomainAdmissionEngine().admit(
        turn,
        _state(turn),
        _historical_daily_proposal(text, include_report_entity=False),
    )

    assert result.decisions[0].status == "blocked"
    assert (
        result.decisions[0].reason_code
        == "daily_report_snapshot_not_uniquely_authorized"
    )
    assert result.tickets == ()


def test_model_cannot_bind_a_historical_report_to_current_report_wording() -> None:
    text = "补充今天的日报：联系了法院确认排期"
    turn = CognitiveTurn(
        tenant_id=TENANT_ID,
        actor_user_id=ACTOR_ID,
        user_id=f"{TENANT_ID}:{ACTOR_ID}",
        conversation_id="admission-p0-report-date-drift",
        message_id="admission-p0-report-date-drift-message",
        text=text,
        occurred_at=NOW,
        resources=_daily_resources(historical_mutation_allowed=True),
    )

    result = DomainAdmissionEngine().admit(
        turn,
        _state(turn),
        _historical_daily_proposal(text),
    )

    assert result.decisions[0].status == "blocked"
    assert (
        result.decisions[0].reason_code
        == "daily_report_snapshot_not_uniquely_authorized"
    )
    assert result.tickets == ()


def test_current_daily_fact_is_not_blocked_by_a_historical_sibling_clause() -> None:
    text = "昨天联系了法院，补到昨天日报；日报再记：今天完成合同审核"
    current_fact = "今天完成合同审核"
    turn = CognitiveTurn(
        tenant_id=TENANT_ID,
        actor_user_id=ACTOR_ID,
        user_id=f"{TENANT_ID}:{ACTOR_ID}",
        conversation_id="admission-p0-current-daily-sibling",
        message_id="admission-p0-current-daily-sibling-message",
        text=text,
        occurred_at=NOW,
        resources=_daily_resources(historical_mutation_allowed=False),
    )
    proposal = SemanticInterpretation.from_payload(
        {
            "intents": ["daily_append"],
            "segments": [
                {
                    "segment_id": "combined-report-segment",
                    "text": text,
                    "intents": ["daily_append"],
                    "entity_ids": ["current-daily-event"],
                    "action_ids": ["capture-current-daily"],
                }
            ],
            "entities": [
                {
                    "entity_id": "current-daily-event",
                    "entity_type": "daily_event",
                    "value": current_fact,
                    "confidence": 0.99,
                    "attributes": {"field": "today_work"},
                }
            ],
            "confidence": 0.99,
            "required_actions": [
                {
                    "action_id": "capture-current-daily",
                    "action_type": "capture_daily_event",
                    "intent": "daily_append",
                    "entity_ids": ["current-daily-event"],
                }
            ],
            "clarification_need": None,
            "context_update": {},
        }
    )

    result = DomainAdmissionEngine().admit(turn, _state(turn), proposal)

    assert result.decisions[0].status == "admitted"
    assert result.decisions[0].object_ref == {
        "object_type": "daily_report",
        "stable_id": CURRENT_REPORT_ID,
        "version": 3,
    }
    assert len(result.tickets) == 1


def test_assertion_contract_rejects_a_negated_travel_registration() -> None:
    text = "明天不去南京出差了"

    assessment = evaluate_assertion_polarity_contract(
        domain="travel",
        segment_text=text,
        statement_mode="asserted",
        evidence_fragments=(text,),
        claim_anchors=("南京",),
    )

    assert assessment.authorizes_mutation is False
    assert assessment.classification == "negated_or_absent"
    assert assessment.reason_code == "travel_assertion_negated_or_cancelled"


def _admit_travel(text: str, attributes: dict[str, object]):
    turn = CognitiveTurn(
        tenant_id=TENANT_ID,
        actor_user_id=ACTOR_ID,
        user_id=f"{TENANT_ID}:{ACTOR_ID}",
        conversation_id=f"admission-p0-travel-{len(text)}",
        message_id=f"admission-p0-travel-message-{len(text)}",
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
                    "value": text.removesuffix("了"),
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
            "context_update": {},
        }
    )
    return DomainAdmissionEngine().admit(turn, _state(turn), proposal)


def test_negated_travel_model_proposal_cannot_receive_a_ticket() -> None:
    text = "明天不去南京出差了"
    result = _admit_travel(
        text,
        {
            "destination": "南京",
            "date_hint": "明天",
            "purpose": "出差",
            "statement_mode": "asserted",
            "traveler_scope": "self",
            "evidence_spans": [[0, len(text)]],
        },
    )

    assert result.decisions[0].status == "blocked"
    assert (
        result.decisions[0].reason_code
        == "travel_assertion_negated_or_cancelled"
    )
    assert result.tickets == ()
    assert result.interpretation.required_actions == ()


def _admit_case(text: str, attributes: dict[str, object]):
    turn = CognitiveTurn(
        tenant_id=TENANT_ID,
        actor_user_id=ACTOR_ID,
        user_id=f"{TENANT_ID}:{ACTOR_ID}",
        conversation_id=f"admission-p0-case-{len(text)}",
        message_id=f"admission-p0-case-message-{len(text)}",
        text=text,
        occurred_at=NOW,
        resources={
            "visible_cases": [
                {
                    "case_id": str(uuid5(NAMESPACE_URL, "admission-p0:case:yun-jing-fu")),
                    "case_name": "云璟府物业服务合同执行案",
                    "case_number": "（2026）苏01执100号",
                    "confirmed_aliases": ["云璟府案"],
                    "version": 7,
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
            "context_update": {},
        }
    )
    return DomainAdmissionEngine().admit(turn, _state(turn), proposal)


def test_negated_case_progress_model_proposal_cannot_receive_a_ticket() -> None:
    text = "云璟府案今天没有联系法院"
    result = _admit_case(
        text,
        {
            "action_time_scope": "unknown",
            "blocking_issues": [],
            "completed_actions": [],
            "current_status": "今天没有联系法院",
            "evidence_spans": [[0, len(text)]],
            "factual_progress": [],
            "next_actions": [],
            "normalized_fact": "今天没有联系法院",
            "statement_mode": "asserted",
        },
    )

    assert result.decisions[0].status == "blocked"
    assert result.decisions[0].reason_code == "case_assertion_negated_or_absent"
    assert result.tickets == ()
    assert result.interpretation.required_actions == ()


@pytest.mark.parametrize(
    ("domain", "text", "anchors", "expected_classification"),
    (
        ("travel", "明天不去南京出差了", ("南京",), "negated_or_absent"),
        ("travel", "取消明天南京出差行程", ("南京",), "negated_or_absent"),
        ("case", "法院尚未通知开庭", ("尚未通知开庭",), "negated_or_absent"),
        ("case", "这个案子暂无新进展", ("暂无新进展",), "negated_or_absent"),
        ("travel", "如果明天去南京出差，就提前订票", ("南京",), "hypothetical"),
        ("case", "只是举例：今天联系法院确认排期", ("联系法院",), "hypothetical"),
        ("travel", "他说“明天去南京出差”", ("南京",), "quoted"),
        ("case", "今天联系法院确认排期了吗？", ("联系法院",), "question"),
    ),
)
def test_assertion_contract_fails_closed_for_non_mutating_proposals(
    domain: str,
    text: str,
    anchors: tuple[str, ...],
    expected_classification: str,
) -> None:
    assessment = evaluate_assertion_polarity_contract(
        domain=domain,
        segment_text=text,
        statement_mode="asserted",
        evidence_fragments=(text,),
        claim_anchors=anchors,
    )

    assert assessment.authorizes_mutation is False
    assert assessment.classification == expected_classification
    assert assessment.reason_code


@pytest.mark.parametrize(
    ("domain", "text", "anchors"),
    (
        ("travel", "明天去南京出差", ("南京",)),
        ("case", "今天联系法院确认了排期", ("联系法院确认了排期",)),
        ("travel", "不是不去南京，明天照常出差", ("南京",)),
        ("case", "不是没有联系法院，今天已经确认了排期", ("联系法院",)),
        ("travel", "明天不去南京出差，改去上海出差", ("上海",)),
    ),
)
def test_assertion_contract_preserves_grounded_affirmative_and_double_negative_facts(
    domain: str,
    text: str,
    anchors: tuple[str, ...],
) -> None:
    assessment = evaluate_assertion_polarity_contract(
        domain=domain,
        segment_text=text,
        statement_mode="asserted",
        evidence_fragments=(text,),
        claim_anchors=anchors,
    )

    assert assessment.authorizes_mutation is True
    assert assessment.classification == "affirmative"
    assert assessment.reason_code == ""


@pytest.mark.parametrize(
    "text",
    (
        "明天不去南京出差了",
        "明天 不去 南京 出差了",
        "明天不去南京出差了。",
        "明天不去南京出差了！",
        "明天没法去南京出差",
        "明天不方便去南京出差",
        "明天去不了南京出差",
    ),
)
def test_whitespace_and_terminal_punctuation_cannot_flip_negated_travel_to_positive(
    text: str,
) -> None:
    assessment = evaluate_assertion_polarity_contract(
        domain="travel",
        segment_text=text,
        statement_mode="asserted",
        evidence_fragments=(text,),
        claim_anchors=("南京",),
    )

    assert assessment.authorizes_mutation is False
    assert assessment.classification == "negated_or_absent"


@pytest.mark.parametrize(
    ("text", "fact", "reason_code"),
    (
        (
            "云璟府案法院尚未通知开庭",
            "法院尚未通知开庭",
            "case_assertion_negated_or_absent",
        ),
        (
            "云璟府案只是举例：今天联系法院确认排期",
            "今天联系法院确认排期",
            "case_assertion_not_direct",
        ),
        (
            "云璟府案今天联系法院确认排期了吗？",
            "今天联系法院确认排期",
            "case_assertion_not_direct",
        ),
    ),
)
def test_case_engine_never_signs_status_absence_example_or_question(
    text: str,
    fact: str,
    reason_code: str,
) -> None:
    result = _admit_case(
        text,
        {
            "current_status": fact,
            "evidence_spans": [[0, len(text)]],
            "normalized_fact": fact,
            "statement_mode": "asserted",
        },
    )

    assert result.decisions[0].status == "blocked"
    assert result.decisions[0].reason_code == reason_code
    assert result.tickets == ()


def test_case_engine_keeps_a_grounded_double_negative_as_affirmative() -> None:
    text = "云璟府案不是没有联系法院，今天已经联系法院确认排期"
    result = _admit_case(
        text,
        {
            "completed_actions": ["今天已经联系法院确认排期"],
            "evidence_spans": [[0, len(text)]],
            "normalized_fact": "今天已经联系法院确认排期",
            "statement_mode": "asserted",
        },
    )

    assert result.decisions[0].status == "admitted"
    assert len(result.tickets) == 1
    assert result.tickets[0].domain == "case"


def test_travel_engine_keeps_a_grounded_double_negative_as_affirmative() -> None:
    text = "不是不去南京，明天照常去南京出差"
    result = _admit_travel(
        text,
        {
            "destination": "南京",
            "date_hint": "明天",
            "purpose": "出差",
            "statement_mode": "asserted",
            "traveler_scope": "self",
            "evidence_spans": [[0, len(text)]],
        },
    )

    assert result.decisions[0].status == "admitted"
    assert len(result.tickets) == 1
    assert result.tickets[0].domain == "travel"
