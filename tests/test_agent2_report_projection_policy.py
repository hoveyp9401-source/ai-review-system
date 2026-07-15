from dataclasses import replace
from datetime import date

import pytest

from app.agent2.report_projection_policy import (
    CaseFactExtraction,
    ReportProjectionContext,
    ReportProjectionPolicy,
)


def test_explicit_completed_work_today_is_eligible_for_today_work_projection():
    fact = CaseFactExtraction(
        case_id="case-1",
        actor_user_id="user-1",
        raw_text="今天和法官沟通过了，没什么问题。",
        normalized_fact="今天与法官沟通，未发现新增问题。",
        factual_progress=("未发现新增问题",),
        completed_actions=("与法官沟通",),
        next_actions=(),
        action_time_scope="today",
        report_preference="automatic",
        confidence=0.98,
        evidence_spans=((0, 8),),
    )
    context = ReportProjectionContext(
        tenant_id="tenant-a",
        user_id="user-1",
        source_turn_id="turn-1",
        report_date=date(2026, 7, 13),
        report_exists=True,
        report_writable=True,
        duplicate_projection_id="",
        automatic_projection_enabled=True,
        high_confidence_threshold=0.9,
    )

    decision = ReportProjectionPolicy().decide(fact, context)

    assert decision.eligible is True
    assert decision.report_type == "daily"
    assert decision.section == "today_work"
    assert decision.reason_code == "completed_work_today"
    assert decision.normalized_fact == fact.normalized_fact


def test_status_only_case_reply_does_not_enter_daily_report():
    fact = CaseFactExtraction(
        case_id="case-1", actor_user_id="user-1", raw_text="法院还没通知。",
        normalized_fact="法院尚未通知。", factual_progress=("法院尚未通知",),
        completed_actions=(), next_actions=(), action_time_scope="unknown",
        report_preference="automatic", confidence=0.99, evidence_spans=((0, 7),),
    )
    context = ReportProjectionContext(
        tenant_id="tenant-a", user_id="user-1", source_turn_id="turn-2",
        report_date=date(2026, 7, 13), report_exists=True, report_writable=True,
        duplicate_projection_id="", automatic_projection_enabled=True,
        high_confidence_threshold=0.9,
    )

    decision = ReportProjectionPolicy().decide(fact, context)

    assert decision.eligible is False
    assert decision.reason_code == "status_only"


def test_explicit_future_action_projects_to_daily_plan_not_completed_work():
    fact = CaseFactExtraction(
        case_id="case-1", actor_user_id="user-1",
        raw_text="明天约了对方律师沟通。", normalized_fact="明天与对方律师沟通。",
        factual_progress=(), completed_actions=(), next_actions=("与对方律师沟通",),
        action_time_scope="future", report_preference="automatic", confidence=0.97,
        evidence_spans=((0, 11),),
    )
    context = ReportProjectionContext(
        tenant_id="tenant-a", user_id="user-1", source_turn_id="turn-3",
        report_date=date(2026, 7, 13), report_exists=True, report_writable=True,
        duplicate_projection_id="", automatic_projection_enabled=True,
        high_confidence_threshold=0.9,
    )

    decision = ReportProjectionPolicy().decide(fact, context)

    assert decision.eligible is True
    assert decision.section == "tomorrow_plan"
    assert decision.reason_code == "future_work_plan"


def test_user_case_only_preference_blocks_projection_even_for_completed_work_today():
    fact = CaseFactExtraction(
        case_id="case-1", actor_user_id="user-1", raw_text="今天已提交材料，只记案件。",
        normalized_fact="今天提交材料。", factual_progress=(), completed_actions=("提交材料",),
        next_actions=(), action_time_scope="today", report_preference="case_only",
        confidence=0.99, evidence_spans=((0, 7),),
    )
    context = ReportProjectionContext(
        tenant_id="tenant-a", user_id="user-1", source_turn_id="turn-4",
        report_date=date(2026, 7, 13), report_exists=True, report_writable=True,
        duplicate_projection_id="", automatic_projection_enabled=True,
        high_confidence_threshold=0.9,
    )

    decision = ReportProjectionPolicy().decide(fact, context)

    assert decision.eligible is False
    assert decision.reason_code == "user_opted_out"


def _canonical_today_fact():
    return CaseFactExtraction(
        case_id="case-1", actor_user_id="user-1",
        raw_text="今天提交了补充材料。", normalized_fact="今天提交补充材料。",
        factual_progress=(), completed_actions=("提交补充材料",), next_actions=(),
        action_time_scope="today", report_preference="automatic", confidence=0.98,
        evidence_spans=((0, 9),),
    )


def _canonical_context():
    return ReportProjectionContext(
        tenant_id="tenant-a", user_id="user-1", source_turn_id="turn-policy",
        report_date=date(2026, 7, 13), report_exists=True, report_writable=True,
        duplicate_projection_id="", automatic_projection_enabled=True,
        high_confidence_threshold=0.9,
    )


@pytest.mark.parametrize(
    ("fact_changes", "context_changes", "reason_code"),
    [
        ({"case_id": ""}, {}, "ambiguous_case"),
        ({"actor_user_id": "another-user"}, {}, "ambiguous_actor"),
        ({"completed_actions": (), "factual_progress": (), "next_actions": ()}, {}, "no_business_action"),
        ({"action_time_scope": "unknown"}, {}, "insufficient_time_anchor"),
        ({}, {"duplicate_projection_id": "projection-1"}, "duplicate_projection"),
        ({}, {"report_exists": False}, "report_not_found"),
        ({}, {"report_writable": False}, "report_closed"),
    ],
)
def test_projection_policy_has_deterministic_fail_closed_reasons(
    fact_changes, context_changes, reason_code
):
    fact = replace(_canonical_today_fact(), **fact_changes)
    context = replace(_canonical_context(), **context_changes)

    decision = ReportProjectionPolicy().decide(fact, context)

    assert decision.eligible is False
    assert decision.reason_code == reason_code


def test_medium_confidence_requires_confirmation_without_writing():
    decision = ReportProjectionPolicy().decide(
        replace(_canonical_today_fact(), confidence=0.75), _canonical_context()
    )

    assert decision.eligible is False
    assert decision.projection_mode == "confirmation_required"
    assert decision.reason_code == "policy_blocked"
