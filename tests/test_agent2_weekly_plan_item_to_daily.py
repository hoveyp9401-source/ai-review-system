from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from app.agent2.tool_calling.context import (
    CANARY_STATE_NAMESPACE,
    TrustedContext,
    TrustedPrincipal,
    TrustedReportSnapshot,
)
from app.agent2.tool_calling.contracts import (
    ExecutionMode,
    ReceiptStatus,
    RecordWeeklyPlanItemsAsTodayWorkArgs,
)
from app.agent2.tool_calling.current_turn_source import CurrentTurnSource
from app.agent2.tool_calling.production_daily_executor import (
    ProductionDailyExecutor,
    ProductionExecutionError,
)
from app.agent2.tool_calling.production_handlers import ProductionHandlerRequest
from app.agent2.tool_calling.registry import (
    ToolArgumentsValidationError,
    validate_tool_arguments,
)
from app.agent2.tool_calling.validation import (
    NativeToolCall,
    ShadowCallBinder,
    UnavailableDateResolver,
)
from app.agent2.weekly_plan_context import (
    TrustedWeeklyPlanContext,
    TrustedWeeklyPlanDay,
    TrustedWeeklyPlanItem,
)
from app.agent2.weekly_plan_models import (
    WeeklyPlan,
    WeeklyPlanDay,
    WeeklyPlanItem,
)

NOW = datetime(2026, 8, 14, 11, 20, tzinfo=ZoneInfo("Asia/Shanghai"))
TENANT_ID = "tenant-weekly-daily-reference"
USER_ID = UUID("10000000-0000-4000-8000-000000000091")
REPORT_ID = UUID("20000000-0000-4000-8000-000000000091")
PLAN_ID = UUID("30000000-0000-4000-8000-000000000091")
ITEM_ID = "40000000-0000-4000-8000-000000000091"
WEEK_START = date(2026, 8, 17)
TOOL_NAME = "record_weekly_plan_items_as_today_work"


def _weekly_plan(
    *,
    plan_id: UUID = PLAN_ID,
    version: int = 4,
    owner_user_id: str | None = None,
    item_id: str = ITEM_ID,
    original_text: str = "日常用印审核",
    week_start: date = WEEK_START,
    batch_id: str = "50000000-0000-4000-8000-000000000091",
) -> TrustedWeeklyPlanContext:
    return TrustedWeeklyPlanContext(
        plan_id=str(plan_id),
        batch_id=batch_id,
        tenant_id=TENANT_ID,
        owner_user_id=owner_user_id or str(USER_ID),
        target_week_start=week_start,
        version=version,
        status="collecting",
        days=tuple(
            TrustedWeeklyPlanDay(
                day_id=f"day-{offset}",
                plan_date=week_start + timedelta(days=offset),
                state="planned" if offset == 0 else "unfilled",
                items=(
                    (
                        TrustedWeeklyPlanItem(
                            item_id=item_id,
                            original_text=original_text,
                            source="manual",
                        ),
                    )
                    if offset == 0
                    else ()
                ),
            )
            for offset in range(6)
        ),
        roles=("active_collection", "natural_next"),
        natural_next_for_message_indexes=(1,),
    )


def _context(
    *,
    weekly_plan: TrustedWeeklyPlanContext | None = None,
    weekly_plans: tuple[TrustedWeeklyPlanContext, ...] | None = None,
) -> TrustedContext:
    plan = weekly_plan or _weekly_plan()
    plans = weekly_plans or (plan,)
    return TrustedContext(
        namespace=CANARY_STATE_NAMESPACE,
        now=NOW,
        principal=TrustedPrincipal(
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            conversation_id="direct-weekly-daily-reference",
            source_message_id="message-weekly-daily-reference",
            timezone="Asia/Shanghai",
            conversation_kind="direct",
        ),
        today_report=TrustedReportSnapshot(
            report_id=REPORT_ID,
            tenant_id=TENANT_ID,
            owner_user_id=USER_ID,
            report_date=NOW.date(),
            version=0,
            status="collecting",
        ),
        weekly_plan=plan,
        weekly_plans=plans,
        allowed_tool_names=frozenset({TOOL_NAME}),
        gate_decisions={TOOL_NAME: True},
    )


def _call(
    *,
    plan_id: UUID = PLAN_ID,
    expected_version: int = 4,
    target_item_ids: tuple[str, ...] = (ITEM_ID,),
) -> NativeToolCall:
    return NativeToolCall(
        tool_call_id="weekly-daily-reference-call",
        tool_name=TOOL_NAME,
        arguments={
            "plan_id": str(plan_id),
            "expected_version": expected_version,
            "target_item_ids": list(target_item_ids),
            "source_evidence": {"source_message_index": 1},
        },
    )


def test_ordinary_daily_literal_item_contract_remains_unchanged() -> None:
    validated = validate_tool_arguments(
        "add_daily_items",
        {
            "items": [
                {
                    "field": "today_work",
                    "content": "draft contract",
                    "source_evidence": {"source_message_index": 1},
                }
            ]
        },
    )

    assert validated["items"][0]["field"] == "today_work"
    assert validated["items"][0]["content"] == "draft contract"

    with pytest.raises(ToolArgumentsValidationError):
        validate_tool_arguments(
            "add_daily_items",
            {
                "items": [
                    {
                        "field": "today_work",
                        "weekly_plan_item_id": ITEM_ID,
                        "source_evidence": {"source_message_index": 1},
                    }
                ]
            },
        )


def _live_plan(
    trusted: TrustedWeeklyPlanContext,
    *,
    version: int | None = None,
) -> WeeklyPlan:
    return WeeklyPlan(
        plan_id=trusted.plan_id,
        batch_id=trusted.batch_id,
        tenant_id=trusted.tenant_id,
        owner_user_id=trusted.owner_user_id,
        target_week_start=trusted.target_week_start,
        status=trusted.status,
        version=trusted.version if version is None else version,
        days=tuple(
            WeeklyPlanDay(
                day_id=day.day_id,
                plan_date=day.plan_date,
                state=day.state,
                items=tuple(
                    WeeklyPlanItem(
                        item_id=item.item_id,
                        original_text=item.original_text,
                        source=item.source,
                        created_at=NOW,
                        updated_at=NOW,
                    )
                    for item in day.items
                ),
            )
            for day in trusted.days
        ),
        created_at=NOW,
        updated_at=NOW,
    )


class _WeeklyStore:
    def __init__(self, plan: WeeklyPlan | None) -> None:
        self.plan = plan
        self.calls: list[dict[str, object]] = []

    async def load_plan(self, **kwargs):
        self.calls.append(kwargs)
        return self.plan


@pytest.mark.asyncio
async def test_trusted_weekly_item_reference_binds_without_model_supplied_daily_text() -> None:
    context = _context()
    bound, failure = await ShadowCallBinder(
        context,
        UnavailableDateResolver(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
        current_turn_source=CurrentTurnSource(("今天也做了这个",), occurred_at=(NOW,)),
    ).bind(_call())

    assert failure is None
    assert bound is not None
    assert bound.weekly_plan == context.weekly_plan
    assert bound.report == context.today_report
    assert bound.arguments == {
        "plan_id": str(PLAN_ID),
        "expected_version": 4,
        "target_item_ids": [ITEM_ID],
        "source_evidence": {"source_message_index": 1},
    }


@pytest.mark.asyncio
async def test_executor_copies_trusted_weekly_original_text_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    bound, failure = await ShadowCallBinder(
        context,
        UnavailableDateResolver(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
        current_turn_source=CurrentTurnSource(("今天也做了这个",), occurred_at=(NOW,)),
    ).bind(_call())
    assert failure is None
    assert bound is not None

    executor = ProductionDailyExecutor(
        session=SimpleNamespace(),
        user=SimpleNamespace(id=USER_ID, active=True),
        context=context,
        settings=SimpleNamespace(),
        bound_calls={bound.call.tool_call_id: bound},
        source_channel="dingtalk_private",
        source_text_hash="a" * 64,
        date_resolver=UnavailableDateResolver(),
        weekly_plan_store=_WeeklyStore(_live_plan(context.weekly_plan)),
    )
    captured: dict[str, object] = {}

    async def snapshot(_report_date):
        return context.today_report

    async def typed_snapshot(_report_date):
        return SimpleNamespace(
            report_id=REPORT_ID,
            version=0,
            today_work=[],
        )

    async def execute_typed(report_date, commands, **_kwargs):
        captured["report_date"] = report_date
        captured["commands"] = tuple(commands)
        return ("typed-receipt-1",)

    def outcome(_request, **kwargs):
        captured["outcome"] = kwargs
        return SimpleNamespace()

    monkeypatch.setattr(executor, "_snapshot", snapshot)
    monkeypatch.setattr(executor, "_typed_snapshot", typed_snapshot)
    monkeypatch.setattr(executor, "_execute_typed", execute_typed)
    monkeypatch.setattr(executor, "_outcome", outcome)
    arguments = RecordWeeklyPlanItemsAsTodayWorkArgs.model_validate(
        bound.arguments
    )
    request = ProductionHandlerRequest(
        tool_call_id=bound.call.tool_call_id,
        tool_name=bound.call.tool_name,
        arguments=arguments,
        executor=executor,
        memory_executor=executor,
    )

    await executor.record_weekly_plan_items_as_today_work(request)

    commands = captured["commands"]
    assert isinstance(commands, tuple)
    assert len(commands) == 1
    assert commands[0].patch == {
        "field": "today_work",
        "items": ["日常用印审核"],
    }
    assert captured["report_date"] == NOW.date()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call", "error_code"),
    (
        (
            _call(
                plan_id=UUID("30000000-0000-4000-8000-000000000099")
            ),
            "UNTRUSTED_WEEKLY_PLAN_ID",
        ),
        (_call(expected_version=3), "STALE_WEEKLY_PLAN_VERSION"),
        (
            _call(
                target_item_ids=(
                    "40000000-0000-4000-8000-000000000099",
                )
            ),
            "UNTRUSTED_WEEKLY_PLAN_ITEM_ID",
        ),
    ),
)
async def test_forged_stale_or_missing_weekly_reference_fails_closed(
    call: NativeToolCall,
    error_code: str,
) -> None:
    bound, failure = await ShadowCallBinder(
        _context(),
        UnavailableDateResolver(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
        current_turn_source=CurrentTurnSource(("今天也做了这个",), occurred_at=(NOW,)),
    ).bind(call)

    assert bound is None
    assert failure is not None
    assert failure.status is ReceiptStatus.BLOCKED
    assert failure.error_code == error_code


@pytest.mark.asyncio
async def test_item_from_another_trusted_plan_cannot_cross_selected_plan() -> None:
    other_plan_id = UUID("30000000-0000-4000-8000-000000000092")
    other_item_id = "40000000-0000-4000-8000-000000000092"
    first = _weekly_plan()
    second = _weekly_plan(
        plan_id=other_plan_id,
        version=7,
        item_id=other_item_id,
        original_text="优化日报机器人",
        week_start=WEEK_START + timedelta(days=7),
        batch_id="50000000-0000-4000-8000-000000000092",
    )
    context = _context(weekly_plan=first, weekly_plans=(first, second))

    bound, failure = await ShadowCallBinder(
        context,
        UnavailableDateResolver(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
        current_turn_source=CurrentTurnSource(("今天也做了这个",), occurred_at=(NOW,)),
    ).bind(_call(target_item_ids=(other_item_id,)))

    assert bound is None
    assert failure is not None
    assert failure.status is ReceiptStatus.BLOCKED
    assert failure.error_code == "UNTRUSTED_WEEKLY_PLAN_ITEM_ID"


@pytest.mark.parametrize(
    "untrusted_field",
    (
        {"content": "完成日常用印审核"},
        {"field": "problems"},
        {"report_date": "2026-08-13"},
        {"date_selection": "agent2_semantic"},
    ),
)
def test_reference_tool_forbids_model_supplied_text_field_or_date(
    untrusted_field: dict[str, str],
) -> None:
    arguments = _call().arguments | untrusted_field

    with pytest.raises(ToolArgumentsValidationError):
        validate_tool_arguments(TOOL_NAME, arguments)


def test_cross_user_weekly_context_is_rejected_before_tool_binding() -> None:
    with pytest.raises(
        ValueError,
        match="trusted weekly plans must match the authenticated principal",
    ):
        _context(
            weekly_plan=_weekly_plan(
                owner_user_id="10000000-0000-4000-8000-000000000099"
            )
        )


@pytest.mark.asyncio
async def test_same_text_from_five_physical_weekly_items_is_written_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _weekly_plan()
    item_ids = tuple(
        f"40000000-0000-4000-8000-{index:012d}"
        for index in range(101, 106)
    )
    days = tuple(
        day.model_copy(
            update={
                "state": "planned" if offset < 5 else "unfilled",
                "items": (
                    (
                        TrustedWeeklyPlanItem(
                            item_id=item_ids[offset],
                            original_text="日常用印审核",
                            source="manual",
                        ),
                    )
                    if offset < 5
                    else ()
                ),
            }
        )
        for offset, day in enumerate(base.days)
    )
    plan = base.model_copy(update={"days": days})
    context = _context(weekly_plan=plan, weekly_plans=(plan,))
    bound, failure = await ShadowCallBinder(
        context,
        UnavailableDateResolver(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
        current_turn_source=CurrentTurnSource(("今天也做了这个",), occurred_at=(NOW,)),
    ).bind(_call(target_item_ids=item_ids))
    assert failure is None
    assert bound is not None

    executor = ProductionDailyExecutor(
        session=SimpleNamespace(),
        user=SimpleNamespace(id=USER_ID, active=True),
        context=context,
        settings=SimpleNamespace(),
        bound_calls={bound.call.tool_call_id: bound},
        source_channel="dingtalk_private",
        source_text_hash="b" * 64,
        date_resolver=UnavailableDateResolver(),
        weekly_plan_store=_WeeklyStore(_live_plan(context.weekly_plan)),
    )
    captured: dict[str, object] = {}

    async def snapshot(_report_date):
        return context.today_report

    async def typed_snapshot(_report_date):
        return SimpleNamespace(
            report_id=REPORT_ID,
            version=0,
            today_work=[],
        )

    async def execute_typed(_report_date, commands, **_kwargs):
        captured["commands"] = tuple(commands)
        return ("typed-receipt-1",)

    monkeypatch.setattr(executor, "_snapshot", snapshot)
    monkeypatch.setattr(executor, "_typed_snapshot", typed_snapshot)
    monkeypatch.setattr(executor, "_execute_typed", execute_typed)
    monkeypatch.setattr(
        executor,
        "_outcome",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )
    request = ProductionHandlerRequest(
        tool_call_id=bound.call.tool_call_id,
        tool_name=bound.call.tool_name,
        arguments=RecordWeeklyPlanItemsAsTodayWorkArgs.model_validate(
            bound.arguments
        ),
        executor=executor,
        memory_executor=executor,
    )
    weekly_before = context.weekly_plan.model_dump(mode="json")

    await executor.record_weekly_plan_items_as_today_work(request)

    commands = captured["commands"]
    assert isinstance(commands, tuple)
    assert len(commands) == 1
    assert commands[0].patch == {
        "field": "today_work",
        "items": ["日常用印审核"],
    }
    assert context.weekly_plan.model_dump(mode="json") == weekly_before


@pytest.mark.asyncio
async def test_weekly_reference_that_becomes_stale_after_binding_is_zero_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    bound, failure = await ShadowCallBinder(
        context,
        UnavailableDateResolver(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
        current_turn_source=CurrentTurnSource(("今天也做了这个",), occurred_at=(NOW,)),
    ).bind(_call())
    assert failure is None
    assert bound is not None

    store = _WeeklyStore(_live_plan(context.weekly_plan, version=5))
    executor = ProductionDailyExecutor(
        session=SimpleNamespace(),
        user=SimpleNamespace(id=USER_ID, active=True),
        context=context,
        settings=SimpleNamespace(),
        bound_calls={bound.call.tool_call_id: bound},
        source_channel="dingtalk_private",
        source_text_hash="c" * 64,
        date_resolver=UnavailableDateResolver(),
        weekly_plan_store=store,
    )
    wrote_daily = False

    async def snapshot(_report_date):
        nonlocal wrote_daily
        wrote_daily = True
        return context.today_report

    monkeypatch.setattr(executor, "_snapshot", snapshot)
    request = ProductionHandlerRequest(
        tool_call_id=bound.call.tool_call_id,
        tool_name=bound.call.tool_name,
        arguments=RecordWeeklyPlanItemsAsTodayWorkArgs.model_validate(
            bound.arguments
        ),
        executor=executor,
        memory_executor=executor,
    )

    with pytest.raises(ProductionExecutionError) as exc_info:
        await executor.record_weekly_plan_items_as_today_work(request)

    assert exc_info.value.code == "STALE_WEEKLY_PLAN_VERSION"
    assert wrote_daily is False
    assert store.calls == [
        {
            "tenant_id": TENANT_ID,
            "plan_id": str(PLAN_ID),
            "owner_user_id": str(USER_ID),
            "for_update": True,
        }
    ]
