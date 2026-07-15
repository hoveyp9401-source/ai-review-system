from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.agent2.case_lifecycle_followup import (
    CaseFollowupPolicySnapshot,
    CaseFollowupSubject,
    CaseFollowupTaskSnapshot,
    CaseFollowupTrigger,
    CommittedCaseLifecycleChange,
    FollowupPolicyEngine,
    FollowupEvaluation,
    TriggerPolicyMatrix,
    FollowupQuestionComposer,
    build_followup_task_plan,
    calculate_next_due_at,
)


def test_weekly_policy_becomes_due_from_last_meaningful_progress_but_not_while_waiting():
    policy = CaseFollowupPolicySnapshot(
        policy_id="policy-1",
        tenant_id="tenant-a",
        case_id="case-1",
        assigned_user_id="user-1",
        enabled=True,
        cadence_type="weekly",
        timezone="Asia/Shanghai",
        last_meaningful_progress_at=datetime(2026, 7, 1, 9, tzinfo=timezone.utc),
        next_due_at=datetime(2026, 7, 8, 9, tzinfo=timezone.utc),
        version=1,
    )
    subject = CaseFollowupSubject(
        tenant_id="tenant-a",
        case_id="case-1",
        assigned_user_id="user-1",
        case_type="plaintiff",
        stage="诉讼中",
        node="财产查控",
        case_version=3,
        case_name="南京工程款案",
    )
    now = datetime(2026, 7, 8, 10, tzinfo=timezone.utc)

    due = FollowupPolicyEngine().evaluate(subject, policy, now=now, existing_tasks=())

    assert due.eligible is True
    assert due.trigger_type == "fixed_cadence"
    assert due.question_type == "meaningful_progress"
    assert due.due_at == policy.next_due_at

    waiting = CaseFollowupTaskSnapshot(
        followup_id="followup-1",
        tenant_id="tenant-a",
        case_id="case-1",
        assigned_user_id="user-1",
        trigger_type="fixed_cadence",
        question_type="meaningful_progress",
        task_status="waiting_for_reply",
    )
    blocked = FollowupPolicyEngine().evaluate(
        subject, policy, now=now, existing_tasks=(waiting,)
    )

    assert blocked.eligible is False
    assert blocked.reason_code == "waiting_for_reply_exists"


def test_monthly_cadence_clamps_to_the_last_local_calendar_day():
    last_progress = datetime.fromisoformat("2026-01-31T16:30:00+08:00")

    next_due = calculate_next_due_at(
        last_progress,
        cadence_type="monthly",
        timezone_name="Asia/Shanghai",
    )

    assert next_due.isoformat() == "2026-02-28T16:30:00+08:00"


def test_business_day_policy_moves_a_weekend_due_time_to_monday():
    friday = datetime.fromisoformat("2026-07-10T09:00:00+08:00")

    next_due = calculate_next_due_at(
        friday,
        cadence_type="daily",
        timezone_name="Asia/Shanghai",
        business_days_only=True,
    )

    assert next_due.isoformat() == "2026-07-13T09:00:00+08:00"


def test_hearing_and_fixed_cadence_triggers_merge_into_one_high_priority_evaluation():
    policy = CaseFollowupPolicySnapshot(
        policy_id="policy-1", tenant_id="tenant-a", case_id="case-1",
        assigned_user_id="user-1", enabled=True, cadence_type="weekly",
        timezone="Asia/Shanghai",
        last_meaningful_progress_at=datetime.fromisoformat("2026-07-06T09:00:00+08:00"),
        next_due_at=datetime.fromisoformat("2026-07-13T09:00:00+08:00"), version=1,
    )
    subject = CaseFollowupSubject(
        tenant_id="tenant-a", case_id="case-1", assigned_user_id="user-1",
        case_type="defendant", stage="开庭", node="确定开庭日期", case_version=4,
        case_name="南京工程款案",
    )
    hearing = CaseFollowupTrigger(
        trigger_type="hearing_proximity",
        trigger_event_id="hearing-1:t-3d",
        question_type="hearing_readiness",
        due_at=datetime.fromisoformat("2026-07-13T09:00:00+08:00"),
        priority=500,
        facts={"hearing_date": "2026-07-16"},
    )

    result = FollowupPolicyEngine().evaluate_triggers(
        subject,
        policy,
        triggers=(hearing,),
        now=datetime.fromisoformat("2026-07-13T10:00:00+08:00"),
        existing_tasks=(),
    )

    assert result.eligible is True
    assert result.trigger_type == "hearing_proximity"
    assert result.question_type == "hearing_readiness"
    assert [item.trigger_type for item in result.trigger_sources] == [
        "hearing_proximity", "fixed_cadence"
    ]


def test_stage_and_node_triggers_require_a_committed_write_and_closed_allowlists():
    matrix = TriggerPolicyMatrix()
    committed = CommittedCaseLifecycleChange(
        tenant_id="tenant-a",
        case_id="case-1",
        case_type="plaintiff",
        receipt_id="receipt-1",
        receipt_status="executed",
        actual_write=True,
        occurred_at=datetime.fromisoformat("2026-07-13T11:00:00+08:00"),
        from_stage="诉讼中",
        to_stage="执行中",
        node="财产查控",
        case_version=5,
    )

    triggers = matrix.from_committed_change(committed)

    assert [item.trigger_type for item in triggers] == [
        "stage_transition", "node_transition"
    ]
    assert triggers[0].facts["receipt_id"] == "receipt-1"

    not_committed = CommittedCaseLifecycleChange(
        **{**committed.__dict__, "receipt_status": "blocked", "actual_write": False}
    )
    assert matrix.from_committed_change(not_committed) == ()

    unknown_node = CommittedCaseLifecycleChange(
        **{**committed.__dict__, "from_stage": "执行中", "to_stage": "执行中", "node": "修改联系人"}
    )
    assert matrix.from_committed_change(unknown_node) == ()


def test_hearing_question_uses_structured_date_without_claiming_readiness_facts():
    subject = CaseFollowupSubject(
        tenant_id="tenant-a", case_id="case-1", assigned_user_id="user-1",
        case_type="defendant", stage="开庭", node="确定开庭日期", case_version=4,
        case_name="南京工程款案",
    )
    trigger = CaseFollowupTrigger(
        trigger_type="hearing_proximity", trigger_event_id="hearing-1:t-3d",
        question_type="hearing_readiness",
        due_at=datetime.fromisoformat("2026-07-13T09:00:00+08:00"), priority=500,
        facts={"hearing_date": "2026-07-16"},
    )
    evaluation = FollowupPolicyEngine().evaluate_triggers(
        subject,
        CaseFollowupPolicySnapshot(
            policy_id="policy-1", tenant_id="tenant-a", case_id="case-1",
            assigned_user_id="user-1", enabled=True, cadence_type="event_only",
            timezone="Asia/Shanghai", last_meaningful_progress_at=None,
            next_due_at=None, version=1,
        ),
        triggers=(trigger,),
        now=datetime.fromisoformat("2026-07-13T10:00:00+08:00"),
        existing_tasks=(),
    )

    message = FollowupQuestionComposer().compose(subject, evaluation)

    assert "南京工程款案" in message
    assert "2026-07-16" in message
    assert "准备得怎么样" in message
    assert "已经准备好" not in message
    assert "法院已通知" not in message


def test_task_plan_has_stable_idempotency_for_the_same_case_trigger_and_question():
    subject = CaseFollowupSubject(
        tenant_id="tenant-a", case_id="case-1", assigned_user_id="user-1",
        case_type="plaintiff", stage="诉讼中", node="", case_version=3,
        case_name="南京工程款案",
    )
    trigger = CaseFollowupTrigger(
        trigger_type="fixed_cadence", trigger_event_id="cadence:2026-07-13",
        question_type="meaningful_progress",
        due_at=datetime.fromisoformat("2026-07-13T09:00:00+08:00"), priority=100,
    )
    evaluation = FollowupEvaluation(
        eligible=True, reason_code="fixed_cadence_due", trigger_type="fixed_cadence",
        question_type="meaningful_progress", due_at=trigger.due_at,
        trigger_sources=(trigger,),
    )

    first = build_followup_task_plan(
        subject, evaluation,
        expires_at=datetime.fromisoformat("2026-07-20T09:00:00+08:00"),
    )
    replay = build_followup_task_plan(
        subject, evaluation,
        expires_at=datetime.fromisoformat("2026-07-20T09:00:00+08:00"),
    )

    assert replay.followup_id == first.followup_id
    assert replay.idempotency_key == first.idempotency_key
    assert first.trigger_event_ids == ("cadence:2026-07-13",)


def test_hearing_trigger_schedule_has_unique_7_3_1_day_and_post_hearing_slots():
    from app.agent2.case_lifecycle_followup import build_hearing_triggers

    hearing_at = datetime(2026, 7, 20, 9, tzinfo=timezone.utc)
    triggers = build_hearing_triggers(
        hearing_event_id="hearing-1", hearing_at=hearing_at,
        post_hearing_delay_hours=4,
    )

    assert [item.trigger_event_id for item in triggers] == [
        "hearing-1:before-7d", "hearing-1:before-3d", "hearing-1:before-1d",
        "hearing-1:after-4h",
    ]
    assert [item.due_at for item in triggers] == [
        hearing_at - timedelta(days=7), hearing_at - timedelta(days=3),
        hearing_at - timedelta(days=1), hearing_at + timedelta(hours=4),
    ]
    assert triggers[-1].question_type == "hearing_result"
