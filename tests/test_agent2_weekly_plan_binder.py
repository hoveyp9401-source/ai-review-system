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
from app.agent2.tool_calling.runtime import _call_target, conflicting_tool_call_ids
from app.agent2.tool_calling.validation import (
    DateResolution,
    NativeToolCall,
    ShadowCallBinder,
)
from app.agent2.weekly_plan_context import (
    TrustedWeeklyPlanContext,
    TrustedWeeklyPlanDay,
    TrustedWeeklyPlanItem,
    TrustedWeeklyPlanSuggestion,
)

TENANT_ID = "test-tenant"
USER_ID = UUID("10000000-0000-4000-8000-000000000001")
PLAN_ID = UUID("20000000-0000-4000-8000-000000000001")
START = date(2026, 8, 17)


class _DateResolver:
    def resolve(self, *, expression, proposed_date, now, timezone):
        del expression, now, timezone
        return DateResolution(proposed_date, candidate_matches=True)


def _weekly_plan() -> TrustedWeeklyPlanContext:
    days = []
    for offset in range(6):
        items = (
            (
                TrustedWeeklyPlanItem(
                    item_id="existing-item",
                    original_text="整理项目材料",
                    source="manual",
                ),
            )
            if offset == 0
            else ()
        )
        days.append(
            TrustedWeeklyPlanDay(
                day_id=f"day-{offset}",
                plan_date=START + timedelta(days=offset),
                state="planned" if items else "unfilled",
                items=items,
            )
        )
    return TrustedWeeklyPlanContext(
        plan_id=str(PLAN_ID),
        batch_id="30000000-0000-4000-8000-000000000001",
        tenant_id=TENANT_ID,
        owner_user_id=str(USER_ID),
        target_week_start=START,
        version=4,
        status="collecting",
        days=tuple(days),
        suggestions=(
            TrustedWeeklyPlanSuggestion(
                suggestion_id="suggestion-1",
                source_kind="user_original_message",
                source_ref="message-older",
                source_version="1",
                evidence_sha256="a" * 64,
                evidence_excerpt="继续整理项目材料",
                prompt="你本周提到过整理项目材料，要不要放入下周计划？",
            ),
        ),
    )


def _context(*, weekly=True) -> TrustedContext:
    return TrustedContext(
        namespace=CANARY_STATE_NAMESPACE,
        now=datetime(2026, 8, 13, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        principal=TrustedPrincipal(
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            conversation_id="conversation-1",
            source_message_id="message-1",
            timezone="Asia/Shanghai",
            conversation_kind="direct",
        ),
        weekly_plan=_weekly_plan() if weekly else None,
        allowed_tool_names=frozenset(
            {
                "query_next_weekly_plan",
                "apply_next_weekly_plan",
                "submit_next_weekly_plan",
            }
        ),
        gate_decisions={
            "query_next_weekly_plan": True,
            "apply_next_weekly_plan": True,
            "submit_next_weekly_plan": True,
        },
    )


def _dual_target_context() -> TrustedContext:
    first = _weekly_plan()
    second_start = START + timedelta(days=7)
    second = TrustedWeeklyPlanContext(
        plan_id="20000000-0000-4000-8000-000000000002",
        batch_id="30000000-0000-4000-8000-000000000002",
        tenant_id=TENANT_ID,
        owner_user_id=str(USER_ID),
        target_week_start=second_start,
        version=7,
        status="collecting",
        days=tuple(
            TrustedWeeklyPlanDay(
                day_id=f"second-day-{offset}",
                plan_date=second_start + timedelta(days=offset),
                state="unfilled",
            )
            for offset in range(6)
        ),
    )
    return _context().model_copy(
        update={"weekly_plan": first, "weekly_plans": (first, second)}
    )


@pytest.mark.asyncio
async def test_weekly_plan_query_without_plan_id_fails_closed_for_two_targets():
    bound, failure = await ShadowCallBinder(
        _dual_target_context(),
        _DateResolver(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
    ).bind(NativeToolCall("call-query", "query_next_weekly_plan", {}))

    assert bound is None
    assert failure is not None
    assert failure.status is ReceiptStatus.BLOCKED
    assert failure.error_code == "WEEKLY_PLAN_QUERY_PLAN_ID_REQUIRED"


@pytest.mark.asyncio
async def test_weekly_plan_query_without_plan_id_keeps_single_target_compatibility():
    context = _context()
    bound, failure = await ShadowCallBinder(
        context,
        _DateResolver(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
    ).bind(NativeToolCall("call-query", "query_next_weekly_plan", {}))

    assert failure is None
    assert bound is not None
    assert bound.weekly_plan == context.weekly_plan


@pytest.mark.asyncio
@pytest.mark.parametrize("target_index", (0, 1))
async def test_weekly_plan_query_with_trusted_plan_id_binds_exact_target(
    target_index,
):
    context = _dual_target_context()
    target = context.weekly_plans[target_index]
    bound, failure = await ShadowCallBinder(
        context,
        _DateResolver(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
    ).bind(
        NativeToolCall(
            f"call-query-{target_index}",
            "query_next_weekly_plan",
            {"plan_id": target.plan_id},
        )
    )

    assert failure is None
    assert bound is not None
    assert bound.weekly_plan == target


@pytest.mark.asyncio
async def test_weekly_plan_query_rejects_untrusted_plan_id():
    bound, failure = await ShadowCallBinder(
        _dual_target_context(),
        _DateResolver(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
    ).bind(
        NativeToolCall(
            "call-query-untrusted",
            "query_next_weekly_plan",
            {"plan_id": "40000000-0000-4000-8000-000000000001"},
        )
    )

    assert bound is None
    assert failure is not None
    assert failure.status is ReceiptStatus.BLOCKED
    assert failure.error_code == "UNTRUSTED_WEEKLY_PLAN_ID"


@pytest.mark.asyncio
async def test_one_leading_weekday_binds_all_parallel_items_in_the_same_clause():
    message = "周一日常用印审核 优化日报机器人"
    call = NativeToolCall(
        "call-parallel-monday-items",
        "apply_next_weekly_plan",
        {
            "plan_id": str(PLAN_ID),
            "expected_version": 4,
            "operations": [
                {
                    "operation_id": "add-imprint-review",
                    "operation": "add",
                    "plan_date": "2026-08-17",
                    "content": "日常用印审核",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_clause_quote": "周一日常用印审核",
                    },
                },
                {
                    "operation_id": "add-daily-bot",
                    "operation": "add",
                    "plan_date": "2026-08-17",
                    "content": "优化日报机器人",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_clause_quote": message,
                    },
                },
            ],
        },
    )

    bound, failure = await ShadowCallBinder(
        _context(),
        _DateResolver(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
        current_turn_source=CurrentTurnSource(
            (message,),
            occurred_at=(
                datetime(2026, 8, 14, 11, 17, tzinfo=ZoneInfo("Asia/Shanghai")),
            ),
        ),
    ).bind(call)

    assert failure is None
    assert bound is not None
    assert bound.weekly_plan is not None
    assert bound.weekly_plan.plan_id == str(PLAN_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target_index", "plan_date"),
    ((0, "2026-08-19"), (1, "2026-08-26")),
)
async def test_bare_weekday_requires_clarification_when_two_target_weeks_are_open(
    target_index,
    plan_date,
):
    context = _dual_target_context()
    selected_plan = context.weekly_plans[target_index]
    clause = "周三整理证据"
    call = NativeToolCall(
        "call-ambiguous-week",
        "apply_next_weekly_plan",
        {
            "plan_id": selected_plan.plan_id,
            "expected_version": selected_plan.version,
            "operations": [
                {
                    "operation_id": "add-ambiguous-week",
                    "operation": "add",
                    "plan_date": plan_date,
                    "content": clause,
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_clause_quote": clause,
                    },
                }
            ],
        },
    )

    bound, failure = await ShadowCallBinder(
        context,
        _DateResolver(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
        current_turn_source=CurrentTurnSource(
            (clause,),
            occurred_at=(
                datetime(2026, 8, 17, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
            ),
        ),
    ).bind(call)

    assert bound is None
    assert failure is not None
    assert failure.status is ReceiptStatus.CLARIFICATION_REQUIRED
    assert failure.error_code == "WEEKLY_PLAN_TARGET_WEEK_AMBIGUOUS"
    assert failure.safe_user_facts["actual_write"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target_index", "clause", "plan_date"),
    (
        (0, "本周三整理证据", "2026-08-19"),
        (0, "这周三整理证据", "2026-08-19"),
        (1, "下周三整理证据", "2026-08-26"),
        (0, "8月19日整理证据", "2026-08-19"),
        (1, "8月26日整理证据", "2026-08-26"),
    ),
)
async def test_explicit_week_scope_binds_exact_target_when_two_weeks_are_open(
    target_index,
    clause,
    plan_date,
):
    context = _dual_target_context()
    target = context.weekly_plans[target_index]
    call = NativeToolCall(
        f"call-explicit-week-{target_index}-{plan_date}",
        "apply_next_weekly_plan",
        {
            "plan_id": target.plan_id,
            "expected_version": target.version,
            "operations": [
                {
                    "operation_id": "add-explicit-week",
                    "operation": "add",
                    "plan_date": plan_date,
                    "content": clause,
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_clause_quote": clause,
                    },
                }
            ],
        },
    )

    bound, failure = await ShadowCallBinder(
        context,
        _DateResolver(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
        current_turn_source=CurrentTurnSource(
            (clause,),
            occurred_at=(
                datetime(2026, 8, 17, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
            ),
        ),
    ).bind(call)

    assert failure is None
    assert bound is not None
    assert bound.weekly_plan == target


@pytest.mark.asyncio
async def test_explicit_current_week_cannot_be_written_to_the_next_week_target():
    context = _dual_target_context()
    next_week = context.weekly_plans[1]
    clause = "本周三整理证据"

    bound, failure = await ShadowCallBinder(
        context,
        _DateResolver(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
        current_turn_source=CurrentTurnSource(
            (clause,),
            occurred_at=(
                datetime(2026, 8, 17, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
            ),
        ),
    ).bind(
        NativeToolCall(
            "call-wrong-explicit-week",
            "apply_next_weekly_plan",
            {
                "plan_id": next_week.plan_id,
                "expected_version": next_week.version,
                "operations": [
                    {
                        "operation_id": "add-wrong-explicit-week",
                        "operation": "add",
                        "plan_date": "2026-08-26",
                        "content": clause,
                        "source_evidence": {
                            "source_message_index": 1,
                            "exact_clause_quote": clause,
                        },
                    }
                ],
            },
        )
    )

    assert bound is None
    assert failure is not None
    assert failure.status is ReceiptStatus.BLOCKED
    assert failure.error_code == "WEEKLY_PLAN_TARGET_WEEK_MISMATCH"


@pytest.mark.asyncio
async def test_bare_weekday_remains_valid_when_only_one_target_week_is_open():
    context = _context()
    clause = "周三整理证据"

    bound, failure = await ShadowCallBinder(
        context,
        _DateResolver(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
        current_turn_source=CurrentTurnSource(
            (clause,),
            occurred_at=(
                datetime(2026, 8, 14, 17, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
            ),
        ),
    ).bind(
        NativeToolCall(
            "call-single-week-bare-day",
            "apply_next_weekly_plan",
            {
                "plan_id": context.weekly_plan.plan_id,
                "expected_version": context.weekly_plan.version,
                "operations": [
                    {
                        "operation_id": "add-single-week-bare-day",
                        "operation": "add",
                        "plan_date": "2026-08-19",
                        "content": clause,
                        "source_evidence": {
                            "source_message_index": 1,
                            "exact_clause_quote": clause,
                        },
                    }
                ],
            },
        )
    )

    assert failure is None
    assert bound is not None


def _apply_call(**overrides) -> NativeToolCall:
    arguments = {
        "plan_id": str(PLAN_ID),
        "expected_version": 4,
        "operations": [
            {
                "operation_id": "op-1",
                "operation": "move",
                "item_id": "existing-item",
                "target_plan_date": "2026-08-18",
                "source_evidence": {
                    "source_message_index": 1,
                    "exact_clause_quote": "把周一的整理项目材料挪到周二",
                },
            }
        ],
    }
    arguments.update(overrides)
    return NativeToolCall("call-1", "apply_next_weekly_plan", arguments)


@pytest.mark.asyncio
async def test_weekly_plan_write_binds_exact_trusted_plan_version_items_and_dates():
    context = _context()
    bound, failure = await ShadowCallBinder(
        context,
        _DateResolver(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
        current_turn_source=CurrentTurnSource(
            ("把周一的整理项目材料挪到周二",),
            occurred_at=(
                datetime(
                    2026,
                    8,
                    13,
                    12,
                    0,
                    tzinfo=ZoneInfo("Asia/Shanghai"),
                ),
            ),
        ),
    ).bind(_apply_call())

    assert failure is None
    assert bound is not None
    assert _call_target(context, bound.call, bound) == str(PLAN_ID)


@pytest.mark.asyncio
async def test_weekly_plan_write_binds_the_exact_plan_selected_from_two_targets():
    first = _weekly_plan()
    second_start = START + timedelta(days=7)
    second_plan_id = "20000000-0000-4000-8000-000000000002"
    second = TrustedWeeklyPlanContext(
        plan_id=second_plan_id,
        batch_id="30000000-0000-4000-8000-000000000002",
        tenant_id=TENANT_ID,
        owner_user_id=str(USER_ID),
        target_week_start=second_start,
        version=7,
        status="collecting",
        days=tuple(
            TrustedWeeklyPlanDay(
                day_id=f"second-day-{offset}",
                plan_date=second_start + timedelta(days=offset),
                state="unfilled",
            )
            for offset in range(6)
        ),
    )
    context = _context().model_copy(
        update={"weekly_plan": first, "weekly_plans": (first, second)}
    )
    call = NativeToolCall(
        "call-second",
        "submit_next_weekly_plan",
        {
            "plan_id": second_plan_id,
            "expected_version": 7,
            "confirmation_evidence": {"source_message_index": 1},
        },
    )

    bound, failure = await ShadowCallBinder(
        context,
        _DateResolver(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
        current_turn_source=CurrentTurnSource(("确认提交下周工作计划",)),
    ).bind(call)

    assert failure is None
    assert bound is not None
    assert bound.weekly_plan == second
    assert _call_target(context, call, bound) == second_plan_id


@pytest.mark.asyncio
async def test_second_plan_rejects_first_plan_version_item_suggestion_and_date():
    first = _weekly_plan()
    second_start = START + timedelta(days=7)
    second = TrustedWeeklyPlanContext(
        plan_id="20000000-0000-4000-8000-000000000002",
        batch_id="30000000-0000-4000-8000-000000000002",
        tenant_id=TENANT_ID,
        owner_user_id=str(USER_ID),
        target_week_start=second_start,
        version=9,
        status="collecting",
        days=tuple(
            TrustedWeeklyPlanDay(
                day_id=f"second-day-{offset}",
                plan_date=second_start + timedelta(days=offset),
                state="unfilled",
            )
            for offset in range(6)
        ),
    )
    context = _context().model_copy(
        update={"weekly_plan": first, "weekly_plans": (first, second)}
    )
    source = CurrentTurnSource(
        ("8月17日安排材料",),
        occurred_at=(
            datetime(2026, 8, 17, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        ),
    )
    cases = (
        (
            {
                "plan_id": second.plan_id,
                "expected_version": first.version,
                "operations": [
                    {
                        "operation_id": "add-stale",
                        "operation": "add",
                        "plan_date": "2026-08-24",
                        "content": "8月17日安排材料",
                        "source_evidence": {
                            "source_message_index": 1,
                            "exact_clause_quote": "8月17日安排材料",
                        },
                    }
                ],
            },
            "STALE_WEEKLY_PLAN_VERSION",
        ),
        (
            {
                "plan_id": second.plan_id,
                "expected_version": second.version,
                "operations": [
                    {
                        "operation_id": "edit-cross",
                        "operation": "edit",
                        "item_id": "existing-item",
                        "content": "8月17日安排材料",
                        "source_evidence": {"source_message_index": 1},
                    }
                ],
            },
            "UNTRUSTED_WEEKLY_PLAN_ITEM_ID",
        ),
        (
            {
                "plan_id": second.plan_id,
                "expected_version": second.version,
                "operations": [
                    {
                        "operation_id": "reject-cross",
                        "operation": "reject_suggestion",
                        "suggestion_id": "suggestion-1",
                        "source_evidence": {"source_message_index": 1},
                    }
                ],
            },
            "UNTRUSTED_WEEKLY_PLAN_SUGGESTION_ID",
        ),
        (
            {
                "plan_id": second.plan_id,
                "expected_version": second.version,
                "operations": [
                    {
                        "operation_id": "add-cross-date",
                        "operation": "add",
                        "plan_date": "2026-08-17",
                        "content": "8月17日安排材料",
                        "source_evidence": {
                            "source_message_index": 1,
                            "exact_clause_quote": "8月17日安排材料",
                        },
                    }
                ],
            },
            "UNTRUSTED_WEEKLY_PLAN_DATE",
        ),
    )

    for index, (arguments, error_code) in enumerate(cases, start=1):
        bound, failure = await ShadowCallBinder(
            context,
            _DateResolver(),
            None,
            execution_mode=ExecutionMode.CANARY_EXECUTE,
            current_turn_source=source,
        ).bind(
            NativeToolCall(
                f"cross-target-{index}",
                "apply_next_weekly_plan",
                arguments,
            )
        )
        assert bound is None
        assert failure is not None
        assert failure.error_code == error_code


@pytest.mark.asyncio
async def test_two_broad_applies_to_different_weekly_plans_do_not_conflict():
    first = _weekly_plan()
    second_start = START + timedelta(days=7)
    second = TrustedWeeklyPlanContext(
        plan_id="20000000-0000-4000-8000-000000000002",
        batch_id="30000000-0000-4000-8000-000000000002",
        tenant_id=TENANT_ID,
        owner_user_id=str(USER_ID),
        target_week_start=second_start,
        version=0,
        status="collecting",
        days=tuple(
            TrustedWeeklyPlanDay(
                day_id=f"second-day-{offset}",
                plan_date=second_start + timedelta(days=offset),
                state="unfilled",
            )
            for offset in range(6)
        ),
    )
    context = _context().model_copy(
        update={"weekly_plan": first, "weekly_plans": (first, second)}
    )
    calls = (
        NativeToolCall(
            "call-current",
            "apply_next_weekly_plan",
            {
                "plan_id": first.plan_id,
                "expected_version": first.version,
                "operations": [
                    {
                        "operation_id": "current-add",
                        "operation": "add",
                        "plan_date": "2026-08-17",
                        "content": "8月17日补本周计划",
                        "source_evidence": {
                            "source_message_index": 1,
                            "exact_clause_quote": "8月17日补本周计划",
                        },
                    }
                ],
            },
        ),
        NativeToolCall(
            "call-next",
            "apply_next_weekly_plan",
            {
                "plan_id": second.plan_id,
                "expected_version": second.version,
                "operations": [
                    {
                        "operation_id": "next-add",
                        "operation": "add",
                        "plan_date": "2026-08-24",
                        "content": "下周一整理材料",
                        "source_evidence": {
                            "source_message_index": 2,
                            "exact_clause_quote": "下周一整理材料",
                        },
                    }
                ],
            },
        ),
    )
    source = CurrentTurnSource(
        ("8月17日补本周计划", "下周一整理材料"),
        occurred_at=(
            datetime(2026, 8, 17, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
            datetime(2026, 8, 17, 9, 1, tzinfo=ZoneInfo("Asia/Shanghai")),
        ),
    )
    binder = ShadowCallBinder(
        context,
        _DateResolver(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
        current_turn_source=source,
    )
    bound_calls = []
    for call in calls:
        bound, failure = await binder.bind(call)
        assert failure is None
        assert bound is not None
        bound_calls.append(bound)

    assert conflicting_tool_call_ids(context, bound_calls) == frozenset()


@pytest.mark.asyncio
async def test_two_broad_applies_to_the_same_weekly_plan_still_conflict():
    context = _context()
    source = CurrentTurnSource(
        ("8月17日补计划", "8月18日补计划"),
        occurred_at=(
            datetime(2026, 8, 13, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
            datetime(2026, 8, 13, 9, 1, tzinfo=ZoneInfo("Asia/Shanghai")),
        ),
    )
    calls = tuple(
        NativeToolCall(
            f"call-{offset}",
            "apply_next_weekly_plan",
            {
                "plan_id": str(PLAN_ID),
                "expected_version": 4,
                "operations": [
                    {
                        "operation_id": f"add-{offset}",
                        "operation": "add",
                        "plan_date": f"2026-08-{16 + offset:02d}",
                        "content": "8月17日补计划" if offset == 1 else "8月18日补计划",
                        "source_evidence": {
                            "source_message_index": offset,
                            "exact_clause_quote": "8月17日补计划" if offset == 1 else "8月18日补计划",
                        },
                    }
                ],
            },
        )
        for offset in (1, 2)
    )
    binder = ShadowCallBinder(
        context,
        _DateResolver(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
        current_turn_source=source,
    )
    bound_calls = []
    for call in calls:
        bound, failure = await binder.bind(call)
        assert failure is None
        assert bound is not None
        bound_calls.append(bound)

    assert conflicting_tool_call_ids(context, bound_calls) == frozenset(
        {"call-1", "call-2"}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "error_code"),
    (
        ({"plan_id": "40000000-0000-4000-8000-000000000001"}, "UNTRUSTED_WEEKLY_PLAN_ID"),
        ({"expected_version": 3}, "STALE_WEEKLY_PLAN_VERSION"),
        (
            {
                "operations": [
                    {
                        "operation_id": "op-1",
                        "operation": "set_day_empty",
                        "plan_date": "2026-08-23",
                        "source_evidence": {
                            "source_message_index": 1,
                            "exact_clause_quote": "下周日暂无安排",
                        },
                    }
                ]
            },
            "UNTRUSTED_WEEKLY_PLAN_DATE",
        ),
        (
            {
                "operations": [
                    {
                        "operation_id": "op-1",
                        "operation": "delete",
                        "item_id": "invented-item",
                        "source_evidence": {"source_message_index": 1},
                    }
                ]
            },
            "UNTRUSTED_WEEKLY_PLAN_ITEM_ID",
        ),
        (
            {
                "operations": [
                    {
                        "operation_id": "op-1",
                        "operation": "accept_suggestion",
                        "suggestion_id": "invented-suggestion",
                        "plan_date": "2026-08-18",
                        "source_evidence": {
                            "source_message_index": 1,
                            "exact_clause_quote": "把这个建议放到周二",
                        },
                    }
                ]
            },
            "UNTRUSTED_WEEKLY_PLAN_SUGGESTION_ID",
        ),
    ),
)
async def test_weekly_plan_write_rejects_model_supplied_untrusted_targets(
    overrides,
    error_code,
):
    bound, failure = await ShadowCallBinder(
        _context(),
        _DateResolver(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
        current_turn_source=CurrentTurnSource(
            (
                (
                    "把周一的整理项目材料挪到周二；"
                    "下周日暂无安排；把这个建议放到周二"
                ),
            ),
            occurred_at=(
                datetime(
                    2026,
                    8,
                    13,
                    12,
                    0,
                    tzinfo=ZoneInfo("Asia/Shanghai"),
                ),
            ),
        ),
    ).bind(_apply_call(**overrides))

    assert bound is None
    assert failure is not None
    assert failure.error_code == error_code


@pytest.mark.asyncio
async def test_weekly_plan_tools_fail_closed_without_server_weekly_context():
    bound, failure = await ShadowCallBinder(
        _context(weekly=False),
        _DateResolver(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
    ).bind(NativeToolCall("call-1", "query_next_weekly_plan", {}))

    assert bound is None
    assert failure is not None
    assert failure.error_code == "WEEKLY_PLAN_CONTEXT_REQUIRED"


def _delayed_cross_midnight_add_call() -> NativeToolCall:
    return NativeToolCall(
        "call-delayed",
        "apply_next_weekly_plan",
        {
            "plan_id": str(PLAN_ID),
            "expected_version": 4,
            "operations": [
                {
                    "operation_id": "op-delayed",
                    "operation": "add",
                    "plan_date": "2026-08-19",
                    "content": "整理案件材料",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_clause_quote": "下周三整理案件材料",
                    },
                }
            ],
        },
    )


@pytest.mark.asyncio
async def test_weekly_plan_relative_day_uses_persisted_message_time_not_processing_time():
    context = _context().model_copy(
        update={
            "now": datetime(
                2026,
                8,
                17,
                0,
                1,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            )
        }
    )
    source = CurrentTurnSource(
        ("下周三整理案件材料",),
        occurred_at=(
            datetime(
                2026,
                8,
                16,
                23,
                59,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            ),
        ),
    )

    bound, failure = await ShadowCallBinder(
        context,
        _DateResolver(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
        current_turn_source=source,
    ).bind(_delayed_cross_midnight_add_call())

    assert failure is None
    assert bound is not None


@pytest.mark.asyncio
async def test_weekly_plan_relative_day_fails_closed_without_persisted_message_time():
    bound, failure = await ShadowCallBinder(
        _context(),
        _DateResolver(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
        current_turn_source=CurrentTurnSource(("下周三整理案件材料",)),
    ).bind(_delayed_cross_midnight_add_call())

    assert bound is None
    assert failure is not None
    assert failure.error_code == "WEEKLY_PLAN_SOURCE_TIME_REQUIRED"
