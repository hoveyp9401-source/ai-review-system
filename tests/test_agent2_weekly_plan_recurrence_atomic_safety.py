from datetime import date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from app.agent2.tool_calling.context import (
    CANARY_STATE_NAMESPACE,
    TrustedContext,
    TrustedPrincipal,
)
from app.agent2.tool_calling.contracts import ExecutionMode, ReceiptStatus
from app.agent2.tool_calling.current_turn_source import CurrentTurnSource
from app.agent2.tool_calling.validation import (
    DateResolution,
    NativeToolCall,
    ShadowCallBinder,
)
from app.agent2.weekly_plan_access import (
    WeeklyPlanAccessAction,
    WeeklyPlanAccessPolicy,
)
from app.agent2.weekly_plan_context import (
    TrustedWeeklyPlanContext,
    TrustedWeeklyPlanDay,
)
from app.agent2.weekly_plan_date_binding import (
    WeeklyPlanDateBindingError,
    validate_weekly_plan_date_set_binding,
)
from app.agent2.weekly_plan_domain import (
    create_weekly_plan,
    create_weekly_plan_batch,
    execute_weekly_plan_batch,
)
from app.agent2.weekly_plan_models import (
    WeeklyPlanCommand,
    WeeklyPlanRosterMember,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
WEEK_START = date(2026, 8, 17)
FRIDAY_SOURCE_TIME = datetime(2026, 8, 14, 17, 30, tzinfo=SHANGHAI)
TENANT_ID = "test-tenant"
USER_ID = UUID("10000000-0000-4000-8000-000000000001")
PLAN_ID = UUID("20000000-0000-4000-8000-000000000001")


class _DateResolver:
    def resolve(self, *, expression, proposed_date, now, timezone):
        del expression, now, timezone
        return DateResolution(proposed_date, candidate_matches=True)


def _plan_dates(*offsets: int) -> tuple[date, ...]:
    return tuple(WEEK_START + timedelta(days=offset) for offset in offsets)


def _weekly_plan(
    *,
    week_start: date = WEEK_START,
    plan_id: UUID = PLAN_ID,
    version: int = 4,
) -> TrustedWeeklyPlanContext:
    return TrustedWeeklyPlanContext(
        plan_id=str(plan_id),
        batch_id=str(UUID(int=plan_id.int + 10)),
        tenant_id=TENANT_ID,
        owner_user_id=str(USER_ID),
        target_week_start=week_start,
        version=version,
        status="collecting",
        days=tuple(
            TrustedWeeklyPlanDay(
                day_id=f"{plan_id}-day-{offset}",
                plan_date=week_start + timedelta(days=offset),
                state="unfilled",
            )
            for offset in range(6)
        ),
    )


def _context(*, two_open_weeks: bool = False) -> TrustedContext:
    first = _weekly_plan()
    plans = (first,)
    if two_open_weeks:
        plans = (
            first,
            _weekly_plan(
                week_start=WEEK_START + timedelta(days=7),
                plan_id=UUID("20000000-0000-4000-8000-000000000002"),
                version=7,
            ),
        )
    return TrustedContext(
        namespace=CANARY_STATE_NAMESPACE,
        now=FRIDAY_SOURCE_TIME,
        principal=TrustedPrincipal(
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            conversation_id="conversation-1",
            source_message_id="message-1",
            timezone="Asia/Shanghai",
            conversation_kind="direct",
        ),
        weekly_plan=first,
        weekly_plans=plans,
        allowed_tool_names=frozenset({"apply_next_weekly_plan"}),
        gate_decisions={"apply_next_weekly_plan": True},
    )


def _recurring_adds(
    *,
    plan_dates: tuple[date, ...],
    clause: str,
    scope_quote: str,
    content: str = "日常用印审核",
) -> list[dict[str, object]]:
    return [
        {
            "operation_id": f"add-{offset}",
            "operation": "add",
            "plan_date": plan_date.isoformat(),
            "content": content,
            "source_evidence": {
                "source_message_index": 1,
                "exact_clause_quote": clause,
                "recurrence_scope_quote": scope_quote,
            },
        }
        for offset, plan_date in enumerate(plan_dates)
    ]


async def _bind_recurrence(
    *,
    context: TrustedContext,
    selected_plan: TrustedWeeklyPlanContext,
    clause: str,
    scope_quote: str,
    plan_dates: tuple[date, ...],
    occurred_at: datetime = FRIDAY_SOURCE_TIME,
):
    return await ShadowCallBinder(
        context,
        _DateResolver(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
        current_turn_source=CurrentTurnSource(
            (clause,),
            occurred_at=(occurred_at,),
        ),
    ).bind(
        NativeToolCall(
            "call-recurrence",
            "apply_next_weekly_plan",
            {
                "plan_id": selected_plan.plan_id,
                "expected_version": selected_plan.version,
                "operations": _recurring_adds(
                    plan_dates=plan_dates,
                    clause=clause,
                    scope_quote=scope_quote,
                ),
            },
        )
    )


def test_conflicting_absolute_and_weekday_recurrence_ranges_are_blocked() -> None:
    clause = "下周每天（8月18日至22日，周一至周六）做日常用印审核"

    with pytest.raises(
        WeeklyPlanDateBindingError,
        match="WEEKLY_PLAN_DATE_SET_CONFLICT",
    ):
        validate_weekly_plan_date_set_binding(
            source_message=clause,
            exact_clause_quote=clause,
            recurrence_scope_quote="下周每天（8月18日至22日，周一至周六）",
            matter_text="日常用印审核",
            source_occurred_at=FRIDAY_SOURCE_TIME,
            business_timezone="Asia/Shanghai",
            target_week_start=WEEK_START,
            proposed_dates=_plan_dates(0, 1, 2, 3, 4, 5),
        )


def test_absolute_only_recurrence_range_is_bound_to_its_exact_six_dates() -> None:
    clause = "8月17日至22日每天做日常用印审核"

    result = validate_weekly_plan_date_set_binding(
        source_message=clause,
        exact_clause_quote=clause,
        recurrence_scope_quote="8月17日至22日每天",
        matter_text="日常用印审核",
        source_occurred_at=FRIDAY_SOURCE_TIME,
        business_timezone="Asia/Shanghai",
        target_week_start=WEEK_START,
        proposed_dates=_plan_dates(0, 1, 2, 3, 4, 5),
    )

    assert result.resolved_dates == _plan_dates(0, 1, 2, 3, 4, 5)
    assert result.basis == "inclusive_absolute_range_recurrence"


def test_absolute_only_recurrence_range_cannot_be_expanded_past_its_endpoints() -> None:
    clause = "8月18日至22日每天做日常用印审核"

    with pytest.raises(
        WeeklyPlanDateBindingError,
        match="WEEKLY_PLAN_DATE_SET_MISMATCH",
    ):
        validate_weekly_plan_date_set_binding(
            source_message=clause,
            exact_clause_quote=clause,
            recurrence_scope_quote="8月18日至22日每天",
            matter_text="日常用印审核",
            source_occurred_at=FRIDAY_SOURCE_TIME,
            business_timezone="Asia/Shanghai",
            target_week_start=WEEK_START,
            proposed_dates=_plan_dates(0, 1, 2, 3, 4, 5),
        )


def test_disconnected_weekdays_cannot_fall_back_to_all_six_plan_days() -> None:
    clause = "周一和周二每天做日常用印审核"

    with pytest.raises(
        WeeklyPlanDateBindingError,
        match="WEEKLY_PLAN_DAY_AMBIGUOUS",
    ):
        validate_weekly_plan_date_set_binding(
            source_message=clause,
            exact_clause_quote=clause,
            recurrence_scope_quote="周一和周二每天",
            matter_text="日常用印审核",
            source_occurred_at=FRIDAY_SOURCE_TIME,
            business_timezone="Asia/Shanghai",
            target_week_start=WEEK_START,
            proposed_dates=_plan_dates(0, 1, 2, 3, 4, 5),
        )


def test_disconnected_absolute_dates_cannot_fall_back_to_all_six_plan_days() -> None:
    clause = "8月17日、8月22日每天做日常用印审核"

    with pytest.raises(
        WeeklyPlanDateBindingError,
        match="WEEKLY_PLAN_DATE_SET_INVALID",
    ):
        validate_weekly_plan_date_set_binding(
            source_message=clause,
            exact_clause_quote=clause,
            recurrence_scope_quote="8月17日、8月22日每天",
            matter_text="日常用印审核",
            source_occurred_at=FRIDAY_SOURCE_TIME,
            business_timezone="Asia/Shanghai",
            target_week_start=WEEK_START,
            proposed_dates=_plan_dates(0, 1, 2, 3, 4, 5),
        )


@pytest.mark.parametrize(
    ("clause", "scope_quote", "matter_text"),
    (
        ("下周工作日每天做日常用印审核", "下周工作日每天", "日常用印审核"),
        ("下周周末每天做系统维护", "下周周末每天", "系统维护"),
        ("下周节假日每天做值班记录", "下周节假日每天", "值班记录"),
    ),
)
def test_unsupported_recurrence_scope_never_falls_back_to_all_six_days(
    clause: str,
    scope_quote: str,
    matter_text: str,
) -> None:
    with pytest.raises(
        WeeklyPlanDateBindingError,
        match="WEEKLY_PLAN_(?:DATE_SET_UNSUPPORTED|DAY_AMBIGUOUS)",
    ):
        validate_weekly_plan_date_set_binding(
            source_message=clause,
            exact_clause_quote=clause,
            recurrence_scope_quote=scope_quote,
            matter_text=matter_text,
            source_occurred_at=FRIDAY_SOURCE_TIME,
            business_timezone="Asia/Shanghai",
            target_week_start=WEEK_START,
            proposed_dates=_plan_dates(0, 1, 2, 3, 4, 5),
        )


def test_unrecognized_alternating_recurrence_is_not_treated_as_every_day() -> None:
    clause = "下周隔天做日常用印审核"

    with pytest.raises(
        WeeklyPlanDateBindingError,
        match="WEEKLY_PLAN_DAY_NOT_EXPLICIT",
    ):
        validate_weekly_plan_date_set_binding(
            source_message=clause,
            exact_clause_quote=clause,
            recurrence_scope_quote="下周隔天",
            matter_text="日常用印审核",
            source_occurred_at=FRIDAY_SOURCE_TIME,
            business_timezone="Asia/Shanghai",
            target_week_start=WEEK_START,
            proposed_dates=_plan_dates(0, 1, 2, 3, 4, 5),
        )


def test_recurrence_date_set_is_order_independent() -> None:
    clause = "下周一到周五每天做日常用印审核"
    proposed = tuple(reversed(_plan_dates(0, 1, 2, 3, 4)))

    result = validate_weekly_plan_date_set_binding(
        source_message=clause,
        exact_clause_quote=clause,
        recurrence_scope_quote="下周一到周五每天",
        matter_text="日常用印审核",
        source_occurred_at=FRIDAY_SOURCE_TIME,
        business_timezone="Asia/Shanghai",
        target_week_start=WEEK_START,
        proposed_dates=proposed,
    )

    assert set(result.resolved_dates) == set(_plan_dates(0, 1, 2, 3, 4))


def test_business_matter_containing_workday_characters_is_not_a_scope_modifier() -> None:
    clause = "下周每天优化工作日报机器人"

    result = validate_weekly_plan_date_set_binding(
        source_message=clause,
        exact_clause_quote=clause,
        recurrence_scope_quote="下周每天",
        matter_text="优化工作日报机器人",
        source_occurred_at=FRIDAY_SOURCE_TIME,
        business_timezone="Asia/Shanghai",
        target_week_start=WEEK_START,
        proposed_dates=_plan_dates(0, 1, 2, 3, 4, 5),
    )

    assert result.resolved_dates == _plan_dates(0, 1, 2, 3, 4, 5)


def test_explicit_libi_weekday_range_is_supported_without_six_day_expansion() -> None:
    clause = "下周礼拜一到礼拜五每天做日常用印审核"

    result = validate_weekly_plan_date_set_binding(
        source_message=clause,
        exact_clause_quote=clause,
        recurrence_scope_quote="下周礼拜一到礼拜五每天",
        matter_text="日常用印审核",
        source_occurred_at=FRIDAY_SOURCE_TIME,
        business_timezone="Asia/Shanghai",
        target_week_start=WEEK_START,
        proposed_dates=_plan_dates(0, 1, 2, 3, 4),
    )

    assert result.resolved_dates == _plan_dates(0, 1, 2, 3, 4)


def test_unparsed_week_scope_modifier_does_not_default_to_all_plan_days() -> None:
    clause = "下周周中每天做日常用印审核"

    with pytest.raises(
        WeeklyPlanDateBindingError,
        match="WEEKLY_PLAN_DATE_SET_UNSUPPORTED",
    ):
        validate_weekly_plan_date_set_binding(
            source_message=clause,
            exact_clause_quote=clause,
            recurrence_scope_quote="下周周中每天",
            matter_text="日常用印审核",
            source_occurred_at=FRIDAY_SOURCE_TIME,
            business_timezone="Asia/Shanghai",
            target_week_start=WEEK_START,
            proposed_dates=_plan_dates(0, 1, 2, 3, 4, 5),
        )


def test_explicit_full_plan_phrase_after_as_is_supported() -> None:
    clause = "把日常用印审核作为下周每天的计划事项"

    result = validate_weekly_plan_date_set_binding(
        source_message=clause,
        exact_clause_quote=clause,
        recurrence_scope_quote="下周每天",
        matter_text="日常用印审核",
        source_occurred_at=FRIDAY_SOURCE_TIME,
        business_timezone="Asia/Shanghai",
        target_week_start=WEEK_START,
        proposed_dates=_plan_dates(0, 1, 2, 3, 4, 5),
    )

    assert result.resolved_dates == _plan_dates(0, 1, 2, 3, 4, 5)


@pytest.mark.parametrize(
    ("clause", "scope_quote", "matter_text"),
    (
        ("下周每天（周末除外）做日常用印审核", "下周每天（周末除外）", "日常用印审核"),
        ("每天（仅工作日）做日常用印审核", "每天（仅工作日）", "日常用印审核"),
        ("下周每天但只在工作日做日常用印审核", "下周每天但只在工作日", "日常用印审核"),
        ("下周每天，双休日不做系统维护", "下周每天，双休日不做", "系统维护"),
        ("下周每天除了周六做日常用印审核", "下周每天除了周六", "日常用印审核"),
        ("下周每天不包括礼拜五做日常用印审核", "下周每天不包括礼拜五", "日常用印审核"),
        ("下周每天，仅在上班日做日常用印审核", "下周每天，仅在上班日", "日常用印审核"),
        ("下周每天，休息日不做日常用印审核", "下周每天，休息日不做", "日常用印审核"),
        ("下周每天，双休的时候不做系统维护", "下周每天，双休的时候不做", "系统维护"),
        ("下周每天，周末休息，做系统维护", "下周每天，周末休息", "系统维护"),
        ("每天（工作日）做日常用印审核", "每天（工作日）", "日常用印审核"),
        ("下周每天，工作日才做日常用印审核", "下周每天，工作日才做", "日常用印审核"),
        ("下周每天，双休日无需安排系统维护", "下周每天，双休日无需安排", "系统维护"),
        ("下周每天非工作日做系统维护", "下周每天非工作日", "系统维护"),
    ),
)
def test_full_plan_recurrence_with_date_set_restriction_is_blocked(
    clause: str,
    scope_quote: str,
    matter_text: str,
) -> None:
    with pytest.raises(
        WeeklyPlanDateBindingError,
        match="WEEKLY_PLAN_(?:DATE_SET_UNSUPPORTED|DAY_AMBIGUOUS)",
    ):
        validate_weekly_plan_date_set_binding(
            source_message=clause,
            exact_clause_quote=clause,
            recurrence_scope_quote=scope_quote,
            matter_text=matter_text,
            source_occurred_at=FRIDAY_SOURCE_TIME,
            business_timezone="Asia/Shanghai",
            target_week_start=WEEK_START,
            proposed_dates=_plan_dates(0, 1, 2, 3, 4, 5),
        )


@pytest.mark.parametrize("separator", ("，", "；", "。", "\n"))
def test_recurrence_cannot_cite_only_text_before_a_later_qualifier(
    separator: str,
) -> None:
    quoted = "下周每天做日常用印审核"
    source = f"{quoted}{separator}周末除外"

    with pytest.raises(
        WeeklyPlanDateBindingError,
        match="WEEKLY_PLAN_RECURRENCE_SOURCE_EVIDENCE_INCOMPLETE",
    ):
        validate_weekly_plan_date_set_binding(
            source_message=source,
            exact_clause_quote=quoted,
            recurrence_scope_quote="下周每天",
            matter_text="日常用印审核",
            source_occurred_at=FRIDAY_SOURCE_TIME,
            business_timezone="Asia/Shanghai",
            target_week_start=WEEK_START,
            proposed_dates=_plan_dates(0, 1, 2, 3, 4, 5),
        )


def test_recurrence_rejects_repeated_or_overlapping_source_spans() -> None:
    repeated = "下周每天在工作日做工作日"
    with pytest.raises(
        WeeklyPlanDateBindingError,
        match="WEEKLY_PLAN_RECURRENCE_SOURCE_SPAN_AMBIGUOUS",
    ):
        validate_weekly_plan_date_set_binding(
            source_message=repeated,
            exact_clause_quote=repeated,
            recurrence_scope_quote="下周每天",
            matter_text="工作日",
            source_occurred_at=FRIDAY_SOURCE_TIME,
            business_timezone="Asia/Shanghai",
            target_week_start=WEEK_START,
            proposed_dates=_plan_dates(0, 1, 2, 3, 4, 5),
        )

    overlapping = "下周每天做日常用印审核"
    with pytest.raises(
        WeeklyPlanDateBindingError,
        match="WEEKLY_PLAN_RECURRENCE_SOURCE_SPAN_AMBIGUOUS",
    ):
        validate_weekly_plan_date_set_binding(
            source_message=overlapping,
            exact_clause_quote=overlapping,
            recurrence_scope_quote="下周每天做日常用印审核",
            matter_text="日常用印审核",
            source_occurred_at=FRIDAY_SOURCE_TIME,
            business_timezone="Asia/Shanghai",
            target_week_start=WEEK_START,
            proposed_dates=_plan_dates(0, 1, 2, 3, 4, 5),
        )


@pytest.mark.parametrize(
    ("clause", "scope_quote", "matter_text", "expected_offsets"),
    (
        ("每天都做的工作是日常用印审核", "每天", "日常用印审核", (0, 1, 2, 3, 4, 5)),
        ("我周一到周五每天做日常用印审核", "周一到周五每天", "日常用印审核", (0, 1, 2, 3, 4)),
        ("把日常用印审核作为下周每天的计划事项", "下周每天", "日常用印审核", (0, 1, 2, 3, 4, 5)),
        (
            "下周每天优化周末不安排值班制度",
            "下周每天",
            "优化周末不安排值班制度",
            (0, 1, 2, 3, 4, 5),
        ),
    ),
)
def test_complete_recurrence_evidence_accepts_neutral_glue_and_exact_matter(
    clause: str,
    scope_quote: str,
    matter_text: str,
    expected_offsets: tuple[int, ...],
) -> None:
    result = validate_weekly_plan_date_set_binding(
        source_message=clause,
        exact_clause_quote=clause,
        recurrence_scope_quote=scope_quote,
        matter_text=matter_text,
        source_occurred_at=FRIDAY_SOURCE_TIME,
        business_timezone="Asia/Shanghai",
        target_week_start=WEEK_START,
        proposed_dates=_plan_dates(*expected_offsets),
    )

    assert set(result.resolved_dates) == set(_plan_dates(*expected_offsets))


@pytest.mark.asyncio
async def test_every_day_model_omitting_saturday_is_blocked_before_execution() -> None:
    context = _context()
    clause = "下周每天做日常用印审核"

    bound, failure = await _bind_recurrence(
        context=context,
        selected_plan=context.weekly_plan,
        clause=clause,
        scope_quote="下周每天",
        plan_dates=_plan_dates(0, 1, 2, 3, 4),
    )

    assert bound is None
    assert failure is not None
    assert failure.status is ReceiptStatus.BLOCKED
    assert failure.error_code == "WEEKLY_PLAN_DATE_SET_MISMATCH"
    assert failure.safe_user_facts["actual_write"] is False


@pytest.mark.asyncio
async def test_monday_bare_every_day_with_two_open_weeks_requires_clarification() -> None:
    context = _context(two_open_weeks=True)
    selected_plan = context.weekly_plans[0]
    clause = "每天做日常用印审核"

    bound, failure = await _bind_recurrence(
        context=context,
        selected_plan=selected_plan,
        clause=clause,
        scope_quote="每天",
        plan_dates=tuple(day.plan_date for day in selected_plan.days),
        occurred_at=datetime(2026, 8, 17, 9, 0, tzinfo=SHANGHAI),
    )

    assert bound is None
    assert failure is not None
    assert failure.status is ReceiptStatus.CLARIFICATION_REQUIRED
    assert failure.error_code == "WEEKLY_PLAN_TARGET_WEEK_AMBIGUOUS"
    assert failure.safe_user_facts["actual_write"] is False
    assert failure.safe_user_facts["clarification_option_labels"] == [
        "本周",
        "下周",
    ]


@pytest.mark.asyncio
async def test_explicit_next_week_recurrence_cannot_write_current_week_target() -> None:
    context = _context(two_open_weeks=True)
    current_week = context.weekly_plans[0]
    clause = "下周每天做日常用印审核"

    bound, failure = await _bind_recurrence(
        context=context,
        selected_plan=current_week,
        clause=clause,
        scope_quote="下周每天",
        plan_dates=tuple(day.plan_date for day in current_week.days),
        occurred_at=datetime(2026, 8, 17, 9, 0, tzinfo=SHANGHAI),
    )

    assert bound is None
    assert failure is not None
    assert failure.status is ReceiptStatus.BLOCKED
    assert failure.error_code == "WEEKLY_PLAN_TARGET_WEEK_MISMATCH"
    assert failure.safe_user_facts["actual_write"] is False


@pytest.mark.asyncio
async def test_explicit_current_week_recurrence_cannot_write_next_week_target() -> None:
    context = _context(two_open_weeks=True)
    next_week = context.weekly_plans[1]
    clause = "本周每天做日常用印审核"

    bound, failure = await _bind_recurrence(
        context=context,
        selected_plan=next_week,
        clause=clause,
        scope_quote="本周每天",
        plan_dates=tuple(day.plan_date for day in next_week.days),
        occurred_at=datetime(2026, 8, 17, 9, 0, tzinfo=SHANGHAI),
    )

    assert bound is None
    assert failure is not None
    assert failure.status is ReceiptStatus.BLOCKED
    assert failure.error_code == "WEEKLY_PLAN_TARGET_WEEK_MISMATCH"
    assert failure.safe_user_facts["actual_write"] is False


@pytest.mark.asyncio
async def test_or_joined_days_cannot_be_collapsed_to_one_model_selected_day() -> None:
    context = _context()
    clause = "周二或周三每天做日常用印审核"

    bound, failure = await _bind_recurrence(
        context=context,
        selected_plan=context.weekly_plan,
        clause=clause,
        scope_quote="周二或周三每天",
        plan_dates=_plan_dates(1),
    )

    assert bound is None
    assert failure is not None
    assert failure.status is ReceiptStatus.BLOCKED
    assert failure.error_code == "WEEKLY_PLAN_DAY_AMBIGUOUS"
    assert failure.safe_user_facts["actual_write"] is False


@pytest.mark.asyncio
async def test_complete_screenshot_six_day_clause_binds_all_six_dates() -> None:
    context = _context()
    clause = "下周每天（8月17日至22日，周一至周六）做日常用印审核"

    bound, failure = await _bind_recurrence(
        context=context,
        selected_plan=context.weekly_plan,
        clause=clause,
        scope_quote="下周每天（8月17日至22日，周一至周六）",
        plan_dates=_plan_dates(0, 1, 2, 3, 4, 5),
    )

    assert failure is None
    assert bound is not None
    assert bound.weekly_plan == context.weekly_plan


def test_parallel_monday_items_are_atomic_when_one_item_fails() -> None:
    batch = create_weekly_plan_batch(
        tenant_id=TENANT_ID,
        target_week_start=WEEK_START,
        roster=(
            WeeklyPlanRosterMember(
                user_id=str(USER_ID),
                display_name="测试用户甲",
                department_id="department-a",
                team_id="team-a",
            ),
        ),
        created_at=FRIDAY_SOURCE_TIME,
    )
    plan = create_weekly_plan(
        batch=batch,
        owner_user_id=str(USER_ID),
        created_at=FRIDAY_SOURCE_TIME,
    )
    commands = tuple(
        WeeklyPlanCommand(
            command_id=f"monday-item-{ordinal}",
            command_type="add_item",
            tenant_id=TENANT_ID,
            actor_user_id=str(USER_ID),
            plan_id=plan.plan_id,
            expected_version=plan.version,
            idempotency_key=f"idem-monday-item-{ordinal}",
            source_message_id="message-parallel-monday",
            patch={
                "plan_date": WEEK_START.isoformat(),
                "original_text": content,
                "source": "manual",
            },
        )
        for ordinal, content in enumerate(
            ("日常用印审核", " "),
            start=1,
        )
    )

    execution = execute_weekly_plan_batch(
        commands,
        plan=plan,
        executed_at=FRIDAY_SOURCE_TIME,
    )

    assert execution.receipt.status == "blocked"
    assert execution.receipt.reason_code == "empty_original_text"
    assert execution.receipt.actual_write is False
    assert execution.after == plan
    assert execution.after.version == plan.version
    assert all(not day.items for day in execution.after.days)
    assert execution.audit_event is None


def test_monday_to_friday_recurrence_writes_five_days_with_one_version_growth() -> None:
    clause = "周一到周五每天做日常用印审核"
    resolution = validate_weekly_plan_date_set_binding(
        source_message=clause,
        exact_clause_quote=clause,
        recurrence_scope_quote="周一到周五每天",
        matter_text="日常用印审核",
        source_occurred_at=FRIDAY_SOURCE_TIME,
        business_timezone="Asia/Shanghai",
        target_week_start=WEEK_START,
        proposed_dates=_plan_dates(0, 1, 2, 3, 4),
    )
    batch = create_weekly_plan_batch(
        tenant_id=TENANT_ID,
        target_week_start=WEEK_START,
        roster=(
            WeeklyPlanRosterMember(
                user_id=str(USER_ID),
                display_name="测试用户甲",
                department_id="department-a",
                team_id="team-a",
            ),
        ),
        created_at=FRIDAY_SOURCE_TIME,
    )
    plan = create_weekly_plan(
        batch=batch,
        owner_user_id=str(USER_ID),
        created_at=FRIDAY_SOURCE_TIME,
    )
    commands = tuple(
        WeeklyPlanCommand(
            command_id=f"weekday-recurrence-{ordinal}",
            command_type="add_item",
            tenant_id=TENANT_ID,
            actor_user_id=str(USER_ID),
            plan_id=plan.plan_id,
            expected_version=plan.version,
            idempotency_key=f"idem-weekday-recurrence-{ordinal}",
            source_message_id="message-weekday-recurrence",
            patch={
                "plan_date": plan_date.isoformat(),
                "original_text": "日常用印审核",
                "source": "manual",
            },
        )
        for ordinal, plan_date in enumerate(resolution.resolved_dates, start=1)
    )

    execution = execute_weekly_plan_batch(
        commands,
        plan=plan,
        executed_at=FRIDAY_SOURCE_TIME,
    )

    assert len(commands) == 5
    assert execution.receipt.status == "executed"
    assert execution.receipt.actual_write is True
    assert execution.receipt.before_version == plan.version
    assert execution.receipt.after_version == plan.version + 1
    assert execution.after.version == plan.version + 1
    assert [len(day.items) for day in execution.after.days] == [1, 1, 1, 1, 1, 0]
    assert execution.audit_event is not None


@pytest.mark.parametrize(
    ("policy", "user_id", "conversation_kind", "expected_reason"),
    (
        (
            WeeklyPlanAccessPolicy(
                enabled=False,
                write_enabled=True,
                tenant_allowlist=frozenset({TENANT_ID}),
                user_allowlist=frozenset({str(USER_ID)}),
            ),
            str(USER_ID),
            "direct",
            "weekly_plan_disabled",
        ),
        (
            WeeklyPlanAccessPolicy(
                enabled=True,
                write_enabled=True,
                tenant_allowlist=frozenset({TENANT_ID}),
                user_allowlist=frozenset({str(USER_ID)}),
            ),
            "10000000-0000-4000-8000-000000000099",
            "direct",
            "weekly_plan_user_not_allowlisted",
        ),
        (
            WeeklyPlanAccessPolicy(
                enabled=True,
                write_enabled=True,
                tenant_allowlist=frozenset({TENANT_ID}),
                user_allowlist=frozenset({str(USER_ID)}),
            ),
            str(USER_ID),
            "group",
            "weekly_plan_direct_conversation_required",
        ),
    ),
)
def test_recurrence_support_does_not_bypass_single_user_private_canary_scope(
    policy: WeeklyPlanAccessPolicy,
    user_id: str,
    conversation_kind: str,
    expected_reason: str,
) -> None:
    decision = policy.decide(
        action=WeeklyPlanAccessAction.WRITE,
        tenant_id=TENANT_ID,
        user_id=user_id,
        conversation_kind=conversation_kind,
    )

    assert decision.allowed is False
    assert decision.reason == expected_reason
