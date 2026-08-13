from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from app.agent2.weekly_plan_collection import (
    WeeklyPlanCollectionOrchestrator,
    WeeklyPlanCollectionWindow,
    derive_weekly_plan_collection_schedule,
)
from app.agent2.weekly_plan_domain import create_weekly_plan
from app.agent2.weekly_plan_models import WeeklyPlanRosterMember
from app.agent2.weekly_plan_store import InMemoryWeeklyPlanStore


UTC = timezone.utc
TARGET_WEEK = date(2026, 8, 17)
OPENS_AT = datetime(2026, 8, 14, 8, 0, tzinfo=UTC)
DEADLINE_AT = datetime(2026, 8, 17, 1, 0, tzinfo=UTC)
CANARY_USER_ID = "canary-user-a"


def _member(user_id: str, display_name: str) -> WeeklyPlanRosterMember:
    return WeeklyPlanRosterMember(
        user_id=user_id,
        display_name=display_name,
        department_id="test-department",
        department_name="测试部门",
    )


def test_pang_hao_single_user_canary_opens_one_frozen_roster_plan_for_monday_to_saturday() -> None:
    store = InMemoryWeeklyPlanStore()
    orchestrator = WeeklyPlanCollectionOrchestrator(store)

    opened = orchestrator.open_collection(
        tenant_id="tenant-test",
        target_week_start=TARGET_WEEK,
        source_roster=(
            _member(CANARY_USER_ID, "单人灰度测试用户"),
            _member("outside-canary", "非灰度测试用户"),
        ),
        canary_user_ids=frozenset({CANARY_USER_ID}),
        window=WeeklyPlanCollectionWindow(
            opens_at=OPENS_AT,
            deadline_at=DEADLINE_AT,
        ),
    )

    assert opened.batch.roster == (
        _member(CANARY_USER_ID, "单人灰度测试用户"),
    )
    assert opened.denominator == 1
    assert opened.plans == ()
    assert list(opened.plan_dates) == [
        date(2026, 8, 17),
        date(2026, 8, 18),
        date(2026, 8, 19),
        date(2026, 8, 20),
        date(2026, 8, 21),
        date(2026, 8, 22),
    ]


def test_open_collection_is_stable_when_the_same_friday_job_is_retried() -> None:
    store = InMemoryWeeklyPlanStore()
    orchestrator = WeeklyPlanCollectionOrchestrator(store)
    request = dict(
        tenant_id="tenant-test",
        target_week_start=TARGET_WEEK,
        source_roster=(_member(CANARY_USER_ID, "单人灰度测试用户"),),
        canary_user_ids=frozenset({CANARY_USER_ID}),
        window=WeeklyPlanCollectionWindow(
            opens_at=OPENS_AT,
            deadline_at=DEADLINE_AT,
        ),
    )

    first = orchestrator.open_collection(**request)
    replay = orchestrator.open_collection(**request)

    assert replay == first
    assert replay.batch.batch_id == first.batch.batch_id
    assert replay.plans == ()


def test_sunday_reminder_candidate_for_unfilled_canary_is_private_and_stable() -> None:
    store = InMemoryWeeklyPlanStore()
    orchestrator = WeeklyPlanCollectionOrchestrator(store)
    opened = orchestrator.open_collection(
        tenant_id="tenant-test",
        target_week_start=TARGET_WEEK,
        source_roster=(
            _member(CANARY_USER_ID, "单人灰度测试用户"),
            _member("outside-canary", "非灰度测试用户"),
        ),
        canary_user_ids=frozenset({CANARY_USER_ID}),
        window=WeeklyPlanCollectionWindow(
            opens_at=OPENS_AT,
            deadline_at=DEADLINE_AT,
        ),
    )
    reminder_at = DEADLINE_AT - timedelta(hours=10)

    first = orchestrator.calculate_private_reminder_candidates(
        opening=opened,
        current_plans=opened.plans,
        canary_user_ids=frozenset({CANARY_USER_ID}),
        reminder_at=reminder_at,
    )
    replay = orchestrator.calculate_private_reminder_candidates(
        opening=opened,
        current_plans=opened.plans,
        canary_user_ids=frozenset({CANARY_USER_ID}),
        reminder_at=reminder_at,
    )

    assert replay == first
    assert len(first) == 1
    assert first[0].recipient_internal_user_id == CANARY_USER_ID
    assert first[0].plan_id == ""
    assert first[0].collection_state == "unfilled"
    assert first[0].channel == "private_chat"
    assert first[0].idempotency_key
    assert "outside-canary" not in first[0].idempotency_key


def test_sunday_reminder_retry_uses_logical_slot_not_wall_clock_seconds() -> None:
    store = InMemoryWeeklyPlanStore()
    orchestrator = WeeklyPlanCollectionOrchestrator(store)
    opened = orchestrator.open_collection(
        tenant_id="tenant-test",
        target_week_start=TARGET_WEEK,
        source_roster=(_member(CANARY_USER_ID, "canary"),),
        canary_user_ids=frozenset({CANARY_USER_ID}),
        window=WeeklyPlanCollectionWindow(
            opens_at=OPENS_AT,
            deadline_at=DEADLINE_AT,
        ),
    )

    first = orchestrator.calculate_private_reminder_candidates(
        opening=opened,
        current_plans=opened.plans,
        canary_user_ids=frozenset({CANARY_USER_ID}),
        reminder_at=DEADLINE_AT - timedelta(hours=10),
        reminder_slot="sunday-primary",
    )
    retry = orchestrator.calculate_private_reminder_candidates(
        opening=opened,
        current_plans=opened.plans,
        canary_user_ids=frozenset({CANARY_USER_ID}),
        reminder_at=DEADLINE_AT - timedelta(hours=10) + timedelta(seconds=37),
        reminder_slot="sunday-primary",
    )

    assert retry[0].idempotency_key == first[0].idempotency_key


def test_sunday_candidates_distinguish_unfilled_draft_pending_and_submitted() -> None:
    store = InMemoryWeeklyPlanStore()
    orchestrator = WeeklyPlanCollectionOrchestrator(store)
    members = (
        _member("unfilled-user", "未填写用户"),
        _member("draft-user", "草稿用户"),
        _member("pending-user", "待确认用户"),
        _member("submitted-user", "已提交用户"),
    )
    canary_ids = frozenset(member.user_id for member in members)
    opened = orchestrator.open_collection(
        tenant_id="tenant-test",
        target_week_start=TARGET_WEEK,
        source_roster=members,
        canary_user_ids=canary_ids,
        window=WeeklyPlanCollectionWindow(OPENS_AT, DEADLINE_AT),
    )
    unfilled, draft, pending, submitted = tuple(
        create_weekly_plan(
            batch=opened.batch,
            owner_user_id=member.user_id,
            created_at=OPENS_AT,
        )
        for member in members
    )
    first_day = draft.days[0]
    draft = replace(
        draft,
        version=1,
        days=(
            replace(first_day, state="explicitly_empty"),
            *draft.days[1:],
        ),
    )
    pending = replace(pending, status="pending_confirmation", version=1)
    submitted = replace(
        submitted,
        status="submitted",
        version=1,
        submitted_at=DEADLINE_AT - timedelta(hours=1),
    )

    candidates = orchestrator.calculate_private_reminder_candidates(
        opening=opened,
        current_plans=(unfilled, draft, pending, submitted),
        canary_user_ids=canary_ids,
        reminder_at=DEADLINE_AT - timedelta(hours=2),
    )

    assert {
        candidate.recipient_internal_user_id: candidate.collection_state
        for candidate in candidates
    } == {
        "unfilled-user": "unfilled",
        "draft-user": "draft",
        "pending-user": "pending_confirmation",
    }


def test_monday_snapshot_uses_single_user_denominator_and_stays_frozen_after_late_fill() -> None:
    store = InMemoryWeeklyPlanStore()
    orchestrator = WeeklyPlanCollectionOrchestrator(store)
    opened = orchestrator.open_collection(
        tenant_id="tenant-test",
        target_week_start=TARGET_WEEK,
        source_roster=(
            _member(CANARY_USER_ID, "单人灰度测试用户"),
            _member("outside-canary", "非灰度测试用户"),
        ),
        canary_user_ids=frozenset({CANARY_USER_ID}),
        window=WeeklyPlanCollectionWindow(OPENS_AT, DEADLINE_AT),
    )

    frozen = orchestrator.freeze_monday_snapshot(
        opening=opened,
        snapshot_at=DEADLINE_AT,
    )
    late_plan = replace(
        create_weekly_plan(
            batch=opened.batch,
            owner_user_id=CANARY_USER_ID,
            created_at=DEADLINE_AT + timedelta(minutes=1),
        ),
        status="submitted",
        version=1,
        submitted_at=DEADLINE_AT + timedelta(minutes=10),
        updated_at=DEADLINE_AT + timedelta(minutes=10),
    )
    store.save_plan(late_plan)
    frozen_rerun = orchestrator.freeze_monday_snapshot(
        opening=opened,
        snapshot_at=DEADLINE_AT + timedelta(hours=1),
    )
    reconciliation = orchestrator.reconcile_monday_snapshot(
        opening=opened,
        reconciled_at=DEADLINE_AT + timedelta(hours=1),
    )

    assert frozen.roster_count == 1
    assert frozen.submitted_count == 0
    assert frozen.draft_count == 0
    assert frozen.unfilled_count == 1
    assert frozen.rows[0].plan_status == "unfilled"
    assert frozen_rerun == frozen
    assert reconciliation.current_submitted_count == 1
    assert reconciliation.late_submitted_count == 1
    assert reconciliation.changed_after_snapshot_count == 1
    assert reconciliation.rows[0].submission_timing == "late"


def test_collection_window_and_run_times_are_explicit_instead_of_guessing_holidays() -> None:
    store = InMemoryWeeklyPlanStore()
    orchestrator = WeeklyPlanCollectionOrchestrator(store)
    custom_open = datetime(2026, 10, 9, 3, 30, tzinfo=UTC)
    custom_deadline = datetime(2026, 10, 12, 6, 45, tzinfo=UTC)
    custom_target = date(2026, 10, 12)
    opening = orchestrator.open_collection(
        tenant_id="tenant-test",
        target_week_start=custom_target,
        source_roster=(_member(CANARY_USER_ID, "单人灰度测试用户"),),
        canary_user_ids=frozenset({CANARY_USER_ID}),
        window=WeeklyPlanCollectionWindow(custom_open, custom_deadline),
    )

    candidates = orchestrator.calculate_private_reminder_candidates(
        opening=opening,
        current_plans=(),
        canary_user_ids=frozenset({CANARY_USER_ID}),
        reminder_at=datetime(2026, 10, 11, 7, 15, tzinfo=UTC),
    )
    frozen = orchestrator.freeze_monday_snapshot(
        opening=opening,
        snapshot_at=custom_deadline,
    )

    assert opening.window.opens_at == custom_open
    assert candidates[0].reminder_at == datetime(2026, 10, 11, 7, 15, tzinfo=UTC)
    assert frozen.deadline_at == custom_deadline


def test_sunday_reminder_reuses_the_friday_opening_and_monday_snapshot_cutoff() -> None:
    schedules = tuple(
        derive_weekly_plan_collection_schedule(
            observed_at=observed_at,
            timezone_name="Asia/Shanghai",
            collection_open_hour=16,
            collection_open_minute=0,
            snapshot_hour=9,
            snapshot_minute=0,
        )
        for observed_at in (
            datetime(2026, 8, 14, 8, 0, tzinfo=UTC),
            datetime(2026, 8, 16, 7, 0, tzinfo=UTC),
            datetime(2026, 8, 17, 1, 0, tzinfo=UTC),
        )
    )
    assert schedules[1:] == schedules[:1] * 2
    schedule = schedules[0]

    assert schedule.target_week_start == date(2026, 8, 17)
    assert schedule.window.opens_at == datetime(
        2026, 8, 14, 16, 0, tzinfo=timezone(timedelta(hours=8))
    )
    assert schedule.window.deadline_at == datetime(
        2026, 8, 17, 9, 0, tzinfo=timezone(timedelta(hours=8))
    )
    assert schedule.window.late_fill_until == datetime(
        2026, 8, 17, 23, 59, 59, 999999, tzinfo=timezone(timedelta(hours=8))
    )
    assert schedule.window.submission_timing(
        datetime(2026, 8, 17, 8, 59, tzinfo=timezone(timedelta(hours=8)))
    ) == "on_time"
    assert schedule.window.submission_timing(
        datetime(2026, 8, 17, 10, 0, tzinfo=timezone(timedelta(hours=8)))
    ) == "late"
    assert schedule.window.submission_timing(
        datetime(2026, 8, 18, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    ) == "closed"


def test_reminder_calculation_rejects_wrong_batch_plan_and_never_expands_canary_scope() -> None:
    store = InMemoryWeeklyPlanStore()
    orchestrator = WeeklyPlanCollectionOrchestrator(store)
    opening = orchestrator.open_collection(
        tenant_id="tenant-test",
        target_week_start=TARGET_WEEK,
        source_roster=(_member(CANARY_USER_ID, "单人灰度测试用户"),),
        canary_user_ids=frozenset({CANARY_USER_ID}),
        window=WeeklyPlanCollectionWindow(OPENS_AT, DEADLINE_AT),
    )
    unrelated_batch = replace(opening.batch, batch_id="unrelated-batch")
    unrelated_plan = create_weekly_plan(
        batch=unrelated_batch,
        owner_user_id=CANARY_USER_ID,
        created_at=OPENS_AT,
    )

    candidates = orchestrator.calculate_private_reminder_candidates(
        opening=opening,
        current_plans=(unrelated_plan,),
        canary_user_ids=frozenset({CANARY_USER_ID, "outside-canary"}),
        reminder_at=DEADLINE_AT - timedelta(hours=1),
    )

    assert [candidate.recipient_internal_user_id for candidate in candidates] == [
        CANARY_USER_ID
    ]
    assert candidates[0].plan_id == ""
    assert candidates[0].collection_state == "unfilled"
