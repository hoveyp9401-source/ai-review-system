"""Evaluate Daily/Weekly cross-turn routing through the real DeepSeek adapter.

This is an isolated evaluation harness.  It presents trusted, already-written
weekly-plan state to the model, but every proposed operation is bound against
the real deterministic validator and then recorded by an in-memory no-op
runtime.  It never imports a database client or a message provider.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import run_agent2_tri_domain_full_adapter_matrix_live as matrix

from app.agent2.tool_calling.context import (
    TrustedRecentMessage,
    TrustedRecentOperation,
    TrustedReportItem,
)
from app.agent2.tool_calling.contracts import (
    RecordWeeklyPlanItemsAsTodayWorkArgs,
)
from app.agent2.tool_calling.current_turn_source import CurrentTurnSource
from app.agent2.tool_calling.deepseek_adapter import (
    DeepSeekToolCallingAdapter,
    DeepSeekToolCallingError,
    ModelTurnAudit,
)
from app.agent2.tool_calling.production_daily_executor import (
    ProductionDailyExecutor,
)
from app.agent2.tool_calling.production_handlers import ProductionHandlerRequest
from app.agent2.tool_calling.registry import (
    deepseek_tool_schemas,
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

_ROOT = Path(__file__).resolve().parents[1]
_PLAN_ID = str(matrix._FRIDAY_PLAN_ID)
_PLAN_VERSION = matrix._FRIDAY_PLAN_VERSION
_PLAN_WEEK_START = date(2026, 8, 17)
_DAILY_REPORT_ID = matrix._DAILY_REPORT_ID
_DAILY_VERSION = matrix._DAILY_VERSION
_REFERENCE_TOOL = "record_weekly_plan_items_as_today_work"
_ALL_ALLOWED_TOOLS = matrix._TOOLS | frozenset({_REFERENCE_TOOL})
_WEEKLY_TOOLS = frozenset(
    {
        "query_next_weekly_plan",
        "apply_next_weekly_plan",
        "submit_next_weekly_plan",
    }
)
_DAILY_TOOLS = frozenset(
    {
        "query_today_report",
        "add_daily_items",
        "confirm_report",
        _REFERENCE_TOOL,
    }
)
_WRITE_TOOLS = frozenset(
    {
        "add_daily_items",
        "confirm_report",
        "apply_current_weekly_report",
        "submit_current_weekly_report",
        "apply_next_weekly_plan",
        "submit_next_weekly_plan",
        _REFERENCE_TOOL,
    }
)


@dataclass(frozen=True)
class CrossTurnCase:
    case_id: str
    category: Literal[
        "unique_reference",
        "ambiguous_reference",
        "daily_detour",
        "weekly_resume",
        "dual_domain",
    ]
    user_text: str
    plan_items: tuple[tuple[str, str], ...]
    recent_messages: tuple[tuple[Literal["user", "assistant"], str], ...]
    expected_daily_contents: tuple[str, ...] = ()
    expected_weekly_additions: tuple[tuple[str, str], ...] = ()
    existing_daily_contents: tuple[str, ...] = ()
    expects_clarification: bool = False
    plan_version: int = _PLAN_VERSION
    forbid_date_clarification: bool = False
    note: str = ""


CASES = (
    CrossTurnCase(
        case_id="unique_plan_reference_to_today_daily",
        category="unique_reference",
        user_text="今天也做了这个",
        plan_items=(("2026-08-17", "日常用印审核"),),
        recent_messages=(
            ("user", "下周一做日常用印审核"),
            ("assistant", "已把“日常用印审核”加入下周一的工作计划。"),
        ),
        expected_daily_contents=("日常用印审核",),
        note=(
            "The sole trusted prior weekly-plan matter is the only possible "
            "referent; this turn records it only as today's completed work."
        ),
    ),
    CrossTurnCase(
        case_id="two_plan_references_require_clarification",
        category="ambiguous_reference",
        user_text="今天也做了这个",
        plan_items=(
            ("2026-08-17", "日常用印审核"),
            ("2026-08-18", "合同台账复核"),
        ),
        recent_messages=(
            ("user", "下周一做日常用印审核，下周二做合同台账复核"),
            ("assistant", "两项都已加入下周工作计划。"),
        ),
        expects_clarification=True,
        note=(
            "Two trusted prior matters make 'this' ambiguous; the safe result "
            "is one natural question naming both candidates and zero tools."
        ),
    ),
    CrossTurnCase(
        case_id="repeated_plan_matter_references_one_semantic_matter",
        category="unique_reference",
        user_text="今天也做了这个",
        plan_items=tuple(
            (f"2026-08-{day:02d}", "日常用印审核")
            for day in range(17, 22)
        ),
        recent_messages=(
            ("user", "周一到周五每天日常用印审核"),
            (
                "assistant",
                "已把“日常用印审核”加入下周一到周五每天的工作计划。",
            ),
        ),
        expected_daily_contents=("日常用印审核",),
        note=(
            "Five physical plan items carry one repeated semantic matter.  "
            "The singular referent records it once in today_work and must not "
            "ask the user to choose among identical copies."
        ),
    ),
    CrossTurnCase(
        case_id="daily_detour_preserves_weekly_focus",
        category="daily_detour",
        user_text="今天日报：完成X",
        plan_items=(("2026-08-17", "日常用印审核"),),
        recent_messages=(
            ("user", "下周一做日常用印审核"),
            ("assistant", "已加入下周一计划，可以继续补充其他日期。"),
        ),
        expected_daily_contents=("完成X",),
        note=(
            "An explicit Daily Report detour writes only today_work.  The "
            "trusted open weekly-plan target remains the same plan and version."
        ),
    ),
    CrossTurnCase(
        case_id="resume_same_weekly_plan_after_daily_detour",
        category="weekly_resume",
        user_text="继续填下周计划，周二加Y",
        plan_items=(("2026-08-17", "日常用印审核"),),
        existing_daily_contents=("完成X",),
        recent_messages=(
            ("user", "下周一做日常用印审核"),
            ("assistant", "已加入下周一计划，可以继续补充其他日期。"),
            ("user", "今天日报：完成X"),
            ("assistant", "已记入今天日报；下周计划仍可继续补充。"),
        ),
        expected_weekly_additions=(("2026-08-18", "Y"),),
        note=(
            "After a Daily Report detour, an explicit resume must use the same "
            "trusted plan_id and unchanged version, then add Y to Tuesday."
        ),
    ),
    CrossTurnCase(
        case_id="daily_and_weekly_same_sentence",
        category="dual_domain",
        user_text="今天日报：完成X；下周周三加Y",
        plan_items=(("2026-08-17", "日常用印审核"),),
        recent_messages=(
            ("user", "下周一做日常用印审核"),
            ("assistant", "已加入下周一计划，可以继续补充其他日期。"),
        ),
        expected_daily_contents=("完成X",),
        expected_weekly_additions=(("2026-08-19", "Y"),),
        note=(
            "One explicit sentence authorizes both independent records.  Both "
            "writes must reach the same in-memory atomic batch or neither does."
        ),
    ),
    CrossTurnCase(
        case_id="relationship_phrase_stays_one_weekly_matter",
        category="weekly_resume",
        user_text="下周一研究A和B的关联",
        plan_items=(),
        recent_messages=(
            ("user", "帮我填下周计划"),
            ("assistant", "下周计划已就绪，请告诉我具体安排。"),
        ),
        expected_weekly_additions=(("2026-08-17", "研究A和B的关联"),),
        forbid_date_clarification=True,
        note=(
            "The conjunction belongs inside one relationship-analysis matter; "
            "it must not be mechanically split into A and B."
        ),
    ),
    CrossTurnCase(
        case_id="production_failed_range_then_monday_parallel_retry",
        category="weekly_resume",
        user_text="周一日常用印审核 优化日报机器人",
        plan_items=(),
        recent_messages=(
            ("user", "帮我填下周计划"),
            (
                "assistant",
                "下周计划（8月17日—22日，周一至周六）已就绪，请告诉我具体事项和对应日期。",
            ),
            ("user", "每天都做的工作是日常用印审核"),
            (
                "assistant",
                "这次操作没有执行成功，下周计划暂未更新，请再发一次。",
            ),
            (
                "user",
                "把“日常用印审核”作为下周每天（8月17日至22日，周一至周六）的计划事项",
            ),
            (
                "assistant",
                "这次操作没有执行成功，下周计划暂未写入“日常用印审核”，请再发一次。",
            ),
        ),
        expected_weekly_additions=(
            ("2026-08-17", "日常用印审核"),
            ("2026-08-17", "优化日报机器人"),
        ),
        plan_version=0,
        forbid_date_clarification=True,
        note=(
            "Replay the production conversation exactly at the retry boundary. "
            "The two failed prior range attempts are conversational evidence, "
            "not authorization or committed state; only the current Monday "
            "clause may produce two Monday additions."
        ),
    ),
)


def _weekly_plan(case: CrossTurnCase) -> TrustedWeeklyPlanContext:
    items_by_date: dict[str, list[str]] = {}
    for plan_date, content in case.plan_items:
        items_by_date.setdefault(plan_date, []).append(content)

    days: list[TrustedWeeklyPlanDay] = []
    item_number = 0
    for offset in range(6):
        plan_date = _PLAN_WEEK_START + timedelta(days=offset)
        contents = items_by_date.get(plan_date.isoformat(), [])
        items: list[TrustedWeeklyPlanItem] = []
        for content in contents:
            item_number += 1
            items.append(
                TrustedWeeklyPlanItem(
                    item_id=f"trusted-weekly-item-{item_number}",
                    original_text=content,
                    source="manual",
                )
            )
        days.append(
            TrustedWeeklyPlanDay(
                day_id=f"{_PLAN_ID}-day-{offset}",
                plan_date=plan_date,
                state="planned" if items else "unfilled",
                items=tuple(items),
            )
        )
    return TrustedWeeklyPlanContext(
        plan_id=_PLAN_ID,
        batch_id=f"batch-{_PLAN_WEEK_START.isoformat()}",
        tenant_id="eval-tenant",
        owner_user_id=str(matrix._USER_ID),
        target_week_start=_PLAN_WEEK_START,
        version=case.plan_version,
        status="collecting",
        days=tuple(days),
        roles=("active_collection", "natural_next"),
        natural_next_for_message_indexes=(1,),
    )


def _context(case: CrossTurnCase, round_number: int):
    seed = matrix.MatrixCase(
        case_id=f"cross-turn-seed-{case.case_id}",
        category="collision",
        user_text=case.user_text,
        expected_tools=frozenset(),
    )
    base = matrix._context(seed, round_number)
    principal = base.principal.model_copy(
        update={
            "conversation_id": f"eval-cross-turn-{case.case_id}-{round_number}",
            "source_message_id": f"eval-cross-turn-message-{case.case_id}-{round_number}",
        }
    )
    weekly_plan = _weekly_plan(case)

    daily_version = _DAILY_VERSION + (1 if case.existing_daily_contents else 0)
    daily_items = tuple(
        TrustedReportItem(
            item_id=f"trusted-daily-item-{index}",
            field="today_work",
            content=content,
            report_id=_DAILY_REPORT_ID,
            report_version=daily_version,
        )
        for index, content in enumerate(case.existing_daily_contents, start=1)
    )
    today_report = base.today_report.model_copy(
        update={"version": daily_version, "items": daily_items}
    )

    recent_operations: list[TrustedRecentOperation] = []
    weekly_item_ids = tuple(
        item.item_id for day in weekly_plan.days for item in day.items
    )
    if weekly_item_ids:
        recent_operations.append(
            TrustedRecentOperation(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                conversation_id=principal.conversation_id,
                source_message_id="prior-weekly-message",
                tool_call_id="prior-weekly-write",
                tool_name="apply_next_weekly_plan",
                status="success",
                changed=True,
                target_type="weekly_plan",
                target_id=_PLAN_ID,
                before_version=case.plan_version - 1,
                after_version=case.plan_version,
                affected_item_ids=weekly_item_ids,
                occurred_at=base.now - timedelta(minutes=4),
            )
        )
    if daily_items:
        recent_operations.append(
            TrustedRecentOperation(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                conversation_id=principal.conversation_id,
                source_message_id="prior-daily-message",
                tool_call_id="prior-daily-write",
                tool_name="add_daily_items",
                status="success",
                changed=True,
                target_type="daily_report",
                target_id=str(_DAILY_REPORT_ID),
                before_version=daily_version - 1,
                after_version=daily_version,
                affected_item_ids=tuple(item.item_id for item in daily_items),
                occurred_at=base.now - timedelta(minutes=2),
            )
        )

    recent_messages = tuple(
        TrustedRecentMessage(
            role=role,
            content=content,
            source_message_id=f"trusted-recent-message-{index}",
        )
        for index, (role, content) in enumerate(case.recent_messages, start=1)
    )
    return base.model_copy(
        update={
            "principal": principal,
            "today_report": today_report,
            "weekly_plan": weekly_plan,
            "weekly_plans": (weekly_plan,),
            "recent_messages": recent_messages,
            "recent_operations": tuple(recent_operations),
            "allowed_tool_names": base.allowed_tool_names
            | frozenset({_REFERENCE_TOOL}),
            "gate_decisions": {
                **base.gate_decisions,
                _REFERENCE_TOOL: True,
            },
        }
    )


def _live_weekly_plan(
    trusted: TrustedWeeklyPlanContext,
    *,
    now: datetime,
) -> WeeklyPlan:
    return WeeklyPlan(
        plan_id=trusted.plan_id,
        batch_id=trusted.batch_id,
        tenant_id=trusted.tenant_id,
        owner_user_id=trusted.owner_user_id,
        target_week_start=trusted.target_week_start,
        status=trusted.status,
        version=trusted.version,
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
                        source_ref="",
                        created_at=now,
                        updated_at=now,
                    )
                    for item in day.items
                ),
            )
            for day in trusted.days
        ),
        created_at=now,
        updated_at=now,
    )


async def _materialize_reference_without_database(
    *,
    context,
    case: CrossTurnCase,
    calls: list[dict[str, Any]],
) -> dict[str, Any]:
    selected = [call for call in calls if call.get("name") == _REFERENCE_TOOL]
    if not selected:
        return {
            "called": False,
            "database_connected": False,
            "messages_sent": False,
            "server_copied_today_work": [],
        }
    if len(selected) != 1 or not isinstance(selected[0].get("arguments"), dict):
        return {
            "called": False,
            "database_connected": False,
            "messages_sent": False,
            "error": "reference materialization requires exactly one valid call",
            "server_copied_today_work": [],
        }

    arguments = validate_tool_arguments(
        _REFERENCE_TOOL,
        selected[0]["arguments"],
    )
    native = NativeToolCall(
        tool_call_id=f"materialize-{case.case_id}",
        tool_name=_REFERENCE_TOOL,
        arguments=arguments,
    )
    binder = ShadowCallBinder(
        context,
        UnavailableDateResolver(),
        None,
        execution_mode=matrix.ExecutionMode.CANARY_EXECUTE,
        current_turn_source=CurrentTurnSource(
            (case.user_text,),
            occurred_at=(context.now,),
        ),
    )
    bound, failure = await binder.bind(native)
    if failure is not None or bound is None or bound.weekly_plan is None:
        return {
            "called": False,
            "database_connected": False,
            "messages_sent": False,
            "error": (
                failure.error_code if failure is not None else "binding missing"
            ),
            "server_copied_today_work": [],
        }

    class _LockedInMemoryWeeklyStore:
        def __init__(self, plan: WeeklyPlan) -> None:
            self.plan = plan
            self.calls: list[dict[str, Any]] = []

        async def load_plan(self, **kwargs):
            self.calls.append(kwargs)
            return self.plan

    store = _LockedInMemoryWeeklyStore(
        _live_weekly_plan(bound.weekly_plan, now=context.now)
    )
    executor = ProductionDailyExecutor(
        session=SimpleNamespace(),
        user=SimpleNamespace(id=context.principal.user_id, active=True),
        context=context,
        settings=SimpleNamespace(),
        bound_calls={native.tool_call_id: bound},
        source_channel="isolated_full_adapter_eval",
        source_text_hash="d" * 64,
        date_resolver=UnavailableDateResolver(),
        weekly_plan_store=store,
    )
    captured: dict[str, Any] = {"commands": ()}

    async def snapshot(_report_date):
        return context.today_report

    async def typed_snapshot(_report_date):
        return SimpleNamespace(
            report_id=context.today_report.report_id,
            version=context.today_report.version,
            today_work=[
                item.content
                for item in context.today_report.items
                if item.field == "today_work"
            ],
        )

    async def execute_typed(report_date, commands, **_kwargs):
        captured["report_date"] = report_date
        captured["commands"] = tuple(commands)
        return ("isolated-typed-receipt",) if captured["commands"] else ()

    executor._snapshot = snapshot
    executor._typed_snapshot = typed_snapshot
    executor._execute_typed = execute_typed
    executor._outcome = lambda *_args, **_kwargs: SimpleNamespace()
    request = ProductionHandlerRequest(
        tool_call_id=native.tool_call_id,
        tool_name=native.tool_name,
        arguments=RecordWeeklyPlanItemsAsTodayWorkArgs.model_validate(
            bound.arguments
        ),
        executor=executor,
        memory_executor=executor,
    )
    await executor.record_weekly_plan_items_as_today_work(request)
    commands = tuple(captured["commands"])
    copied = tuple(
        value
        for command in commands
        for value in command.patch.get("items", ())
    )
    return {
        "called": True,
        "database_connected": False,
        "messages_sent": False,
        "locked_live_plan": bool(store.calls)
        and store.calls[0].get("for_update") is True,
        "selected_item_ids": list(arguments.get("target_item_ids") or ()),
        "server_copied_today_work": list(copied),
        "daily_command_count": len(commands),
        "target_report_date": (
            captured.get("report_date").isoformat()
            if captured.get("report_date") is not None
            else None
        ),
    }


def _calls_by_name(calls: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for call in calls:
        name = str(call.get("name") or "")
        arguments = call.get("arguments")
        if isinstance(arguments, dict):
            result.setdefault(name, []).append(arguments)
    return result


def _score(
    case: CrossTurnCase,
    *,
    calls: list[dict[str, Any]],
    assistant_content: str | None,
    runtime_batches: tuple[tuple[str, ...], ...] = (),
) -> tuple[bool, tuple[str, ...]]:
    errors: list[str] = []
    source = CurrentTurnSource((case.user_text,))
    validated: list[dict[str, Any]] = []
    for index, call in enumerate(calls):
        name = str(call.get("name") or "")
        arguments = call.get("arguments")
        if name not in _ALL_ALLOWED_TOOLS or not isinstance(arguments, dict):
            errors.append(f"call[{index}] has an invalid tool or argument envelope")
            continue
        try:
            sealed = validate_tool_arguments(name, arguments)
            source.validate_tool_arguments(name, sealed)
        except (TypeError, ValueError) as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            continue
        validated.append({"name": name, "arguments": sealed})

    by_name = _calls_by_name(validated)
    names = frozenset(by_name)
    writes = names & _WRITE_TOOLS
    weekly_names = names & _WEEKLY_TOOLS
    daily_names = names & _DAILY_TOOLS

    if case.expects_clarification:
        if calls:
            errors.append("ambiguous reference must produce zero tools")
        reply = (assistant_content or "").strip()
        for candidate in ("日常用印审核", "合同台账复核"):
            if candidate not in reply:
                errors.append(f"clarification omitted candidate {candidate}")
        if not any(marker in reply for marker in ("哪个", "哪项", "指", "还是", "？", "?")):
            errors.append("ambiguous reference did not ask a natural question")
        return not errors, tuple(errors)

    daily_calls = by_name.get("add_daily_items", [])
    reference_calls = by_name.get(_REFERENCE_TOOL, [])
    if case.expected_daily_contents:
        if case.category == "unique_reference":
            if daily_calls:
                errors.append(
                    "trusted weekly reference must not use model-authored add_daily_items content"
                )
            if len(reference_calls) != 1:
                errors.append(
                    "expected exactly one record_weekly_plan_items_as_today_work call"
                )
            else:
                arguments = reference_calls[0]
                if (
                    str(arguments.get("plan_id")) != _PLAN_ID
                    or arguments.get("expected_version") != case.plan_version
                ):
                    errors.append(
                        "weekly-to-daily reference selected a different plan or version"
                    )
                trusted_by_id = {
                    f"trusted-weekly-item-{index}": content
                    for index, (_plan_date, content) in enumerate(
                        case.plan_items,
                        start=1,
                    )
                }
                target_ids = tuple(arguments.get("target_item_ids") or ())
                if not target_ids or any(
                    item_id not in trusted_by_id for item_id in target_ids
                ):
                    errors.append(
                        "weekly-to-daily reference did not bind trusted item IDs"
                    )
                copied_verbatim = tuple(
                    dict.fromkeys(
                        trusted_by_id[item_id]
                        for item_id in target_ids
                        if item_id in trusted_by_id
                    )
                )
                if copied_verbatim != case.expected_daily_contents:
                    errors.append(
                        "server-resolved original_text differs: expected "
                        f"{case.expected_daily_contents}, got {copied_verbatim}"
                    )
        else:
            if reference_calls:
                errors.append("literal Daily Report content unexpectedly used a weekly reference")
            if len(daily_calls) != 1:
                errors.append("expected exactly one add_daily_items call")
            elif tuple(
                item.get("content", "") for item in daily_calls[0].get("items", ())
            ) != case.expected_daily_contents:
                errors.append(
                    "daily today_work contents differ: expected "
                    f"{case.expected_daily_contents}, got "
                    f"{tuple(item.get('content', '') for item in daily_calls[0].get('items', ()))}"
                )
            elif any(
                item.get("field") != "today_work"
                for item in daily_calls[0].get("items", ())
            ):
                errors.append("daily detour wrote outside today_work")
    elif daily_calls or reference_calls:
        errors.append("unexpected Daily Report write")

    weekly_calls = by_name.get("apply_next_weekly_plan", [])
    if case.expected_weekly_additions:
        if len(weekly_calls) != 1:
            errors.append("expected exactly one apply_next_weekly_plan call")
        else:
            arguments = weekly_calls[0]
            if (
                str(arguments.get("plan_id")) != _PLAN_ID
                or arguments.get("expected_version") != case.plan_version
            ):
                errors.append("weekly resume selected a different plan or version")
            actual = tuple(
                (str(operation.get("plan_date") or ""), str(operation.get("content") or ""))
                for operation in arguments.get("operations", ())
                if operation.get("operation") == "add"
            )
            if actual != case.expected_weekly_additions:
                errors.append(
                    "weekly additions differ: expected "
                    f"{case.expected_weekly_additions}, got {actual}"
                )
    elif weekly_calls:
        errors.append("unexpected Weekly Work Plan write")

    if "confirm_report" in writes or "submit_next_weekly_plan" in writes:
        errors.append("case unexpectedly submitted a record")
    if names & frozenset(
        {"apply_current_weekly_report", "submit_current_weekly_report"}
    ):
        errors.append("case leaked into the Current Weekly Report domain")

    if case.category in {"unique_reference", "daily_detour"}:
        if weekly_names:
            errors.append("Daily-only turn touched a weekly-plan tool")
        if not daily_names:
            errors.append("Daily-only turn did not select the Daily Report domain")
    elif case.category == "weekly_resume":
        if daily_names:
            errors.append("weekly resume touched a Daily Report tool")
        if not weekly_names:
            errors.append("weekly resume did not select the Weekly Work Plan domain")
    elif case.category == "dual_domain":
        if not daily_names or not weekly_names:
            errors.append("same-sentence request did not preserve both domains")
        if runtime_batches and not any(
            "add_daily_items" in batch and "apply_next_weekly_plan" in batch
            for batch in runtime_batches
        ):
            errors.append("two writes did not reach one atomic runtime batch")
    if case.forbid_date_clarification:
        reply = (assistant_content or "").strip()
        if any(
            marker in reply
            for marker in (
                "哪一天",
                "具体日期",
                "未指定日期",
                "安排在哪天",
                "需要哪天",
                "周几",
            )
        ):
            errors.append("clear weekly date triggered a redundant clarification")
    return not errors, tuple(errors)


def _wrong_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "arguments": arguments}


def _negative_controls() -> dict[str, Any]:
    daily_wrong = _wrong_call(
        "add_daily_items",
        {
            "date_selection": "server_default",
            "items": [
                {
                    "field": "today_work",
                    "content": "错误内容",
                    "source_evidence": {"source_message_index": 1},
                }
            ],
        },
    )
    rewritten_reference_wrong = _wrong_call(
        "add_daily_items",
        {
            "date_selection": "server_default",
            "items": [
                {
                    "field": "today_work",
                    "content": "完成日常用印审核",
                    "source_evidence": {"source_message_index": 1},
                }
            ],
        },
    )
    weekly_wrong = _wrong_call(
        "apply_next_weekly_plan",
        {
            "plan_id": _PLAN_ID,
            "expected_version": _PLAN_VERSION,
            "operations": [
                {
                    "operation_id": "negative-operation",
                    "operation": "add",
                    "plan_date": "2026-08-17",
                    "content": "今天也做了这个",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_clause_quote": "今天也做了这个",
                    },
                }
            ],
        },
    )
    controls: dict[str, list[dict[str, Any]]] = {
        "unique_plan_reference_to_today_daily": [rewritten_reference_wrong],
        "two_plan_references_require_clarification": [daily_wrong],
        "repeated_plan_matter_references_one_semantic_matter": [
            rewritten_reference_wrong
        ],
        "daily_detour_preserves_weekly_focus": [weekly_wrong],
        "resume_same_weekly_plan_after_daily_detour": [daily_wrong],
        "daily_and_weekly_same_sentence": [daily_wrong],
        "relationship_phrase_stays_one_weekly_matter": [daily_wrong],
        "production_failed_range_then_monday_parallel_retry": [daily_wrong],
    }
    results: dict[str, Any] = {}
    for case in CASES:
        passed, errors = _score(
            case,
            calls=controls[case.case_id],
            assistant_content="错误结果",
        )
        if passed:
            raise AssertionError(
                f"negative control unexpectedly passed for {case.case_id}"
            )
        results[case.case_id] = {
            "expected_red": True,
            "observed_red": True,
            "errors": list(errors),
        }
    return results


def _raw_calls(turn: ModelTurnAudit) -> list[dict[str, Any]]:
    return matrix._raw_calls(turn)


def _review_stages(turns: tuple[ModelTurnAudit, ...]) -> list[dict[str, Any]]:
    stages: list[dict[str, Any]] = []
    for turn in turns:
        metadata = turn.response_metadata
        review_kind = None
        if metadata.get("daily_weekly_write_semantic_review"):
            review_kind = "daily_weekly_write_semantic_review"
        elif metadata.get("daily_weekly_zero_draft_write_confirmation"):
            review_kind = "daily_weekly_zero_draft_write_confirmation"
        if review_kind is None:
            continue
        stages.append(
            {
                "iteration": turn.iteration,
                "review_kind": review_kind,
                "tool_calls": _raw_calls(turn),
                "assistant_content": turn.raw_assistant_message.get("content"),
                "assistant_message_sha256": turn.assistant_message_sha256,
            }
        )
    return stages


def _plan_focus(context) -> dict[str, Any]:
    plan = context.weekly_plan
    payload = plan.model_payload()
    return {
        "plan_id": plan.plan_id,
        "version": plan.version,
        "status": plan.status,
        "target_week_start": plan.target_week_start.isoformat(),
        "roles": list(plan.roles),
        "item_contents": [
            item.original_text for day in plan.days for item in day.items
        ],
        "payload_sha256": matrix._sha256_json(payload),
    }


async def _evaluate_one(
    *,
    adapter: DeepSeekToolCallingAdapter,
    case: CrossTurnCase,
    round_number: int,
) -> dict[str, Any]:
    context = _context(case, round_number)
    focus_before = _plan_focus(context)
    runtime = matrix._BinderZeroWriteRuntime(context, case.user_text)
    started = perf_counter()
    try:
        result = await adapter.run_canary_turn(
            system_prompt=matrix.canary_system_prompt(),
            user_text=case.user_text,
            context=context,
            runtime_session=runtime,
            thinking_enabled=True,
        )
    except DeepSeekToolCallingError as exc:
        runtime.assert_zero_side_effects()
        return {
            "round": round_number,
            "case_id": case.case_id,
            "category": case.category,
            "valid_adapter_result": False,
            "overall_pass": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "initial": {
                "tool_calls": _raw_calls(exc.model_turns[0])
                if exc.model_turns
                else [],
                "assistant_content": (
                    exc.model_turns[0].raw_assistant_message.get("content")
                    if exc.model_turns
                    else None
                ),
            },
            "review_stages": _review_stages(exc.model_turns),
            "final_tool_calls": [],
            "blocked_binding_attempts": [
                [
                    {"name": call.tool_name, "arguments": call.arguments}
                    for call in attempt
                ]
                for attempt in runtime.blocked_attempts
            ],
            "weekly_focus_before": focus_before,
            "weekly_focus_after": focus_before,
            "zero_write_proof": {
                "business_database_connected": False,
                "business_data_written": False,
                "production_handlers_called": False,
                "messages_sent": False,
                "runtime_batches": [],
                "in_memory_commit_count": runtime.commit_count,
                "in_memory_rollback_count": runtime.rollback_count,
            },
            "elapsed_ms": round((perf_counter() - started) * 1000, 1),
        }

    runtime.assert_zero_side_effects()
    if any(receipt.changed for receipt in result.receipts):
        raise AssertionError("evaluation receipt claimed a business change")
    if any(
        item.actual_write
        or item.business_write_count
        or item.pending_write_count
        or item.memory_write_count
        or item.memory_audit_write_count
        or item.receipt_write_count
        or item.handler_call_count
        for item in result.runtime_results
    ):
        raise AssertionError("evaluation runtime claimed a side effect")

    initial_calls = _raw_calls(result.model_turns[0])
    final_calls = matrix._calls_from_runtime(runtime)
    runtime_batches = tuple(
        tuple(call.tool_name for call in batch.calls) for batch in runtime.batches
    )
    initial_ok, initial_errors = _score(
        case,
        calls=initial_calls,
        assistant_content=result.model_turns[0].raw_assistant_message.get(
            "content"
        ),
    )
    final_ok, final_errors = _score(
        case,
        calls=final_calls,
        assistant_content=result.final_content,
        runtime_batches=runtime_batches,
    )
    reference_materialization = await _materialize_reference_without_database(
        context=context,
        case=case,
        calls=final_calls,
    )
    if case.category == "unique_reference":
        materialization_errors: list[str] = []
        if not reference_materialization.get("called"):
            materialization_errors.append(
                "server reference materialization did not run"
            )
        if not reference_materialization.get("locked_live_plan"):
            materialization_errors.append(
                "server reference materialization did not lock the live plan"
            )
        if tuple(
            reference_materialization.get("server_copied_today_work", ())
        ) != case.expected_daily_contents:
            materialization_errors.append(
                "production executor did not copy exact trusted original_text"
            )
        if reference_materialization.get("daily_command_count") != 1:
            materialization_errors.append(
                "production executor did not produce exactly one deduplicated Daily command"
            )
        if materialization_errors:
            final_errors = (*final_errors, *materialization_errors)
            final_ok = False
    review_stages = _review_stages(result.model_turns)
    focus_after = _plan_focus(context)
    focus_preserved = focus_after == focus_before
    if case.category == "daily_detour" and not focus_preserved:
        final_errors = (*final_errors, "daily detour changed weekly focus")
        final_ok = False
    overall = bool(final_ok and review_stages and focus_preserved)
    return {
        "round": round_number,
        "case_id": case.case_id,
        "category": case.category,
        "user_text": case.user_text,
        "note": case.note,
        "valid_adapter_result": True,
        "initial": {
            "tool_calls": initial_calls,
            "assistant_content": result.model_turns[0].raw_assistant_message.get(
                "content"
            ),
            "pass": initial_ok,
            "errors": list(initial_errors),
            "assistant_message_sha256": result.model_turns[
                0
            ].assistant_message_sha256,
        },
        "review_stages": review_stages,
        "final_tool_calls": final_calls,
        "final_content": result.final_content,
        "final_pass": final_ok,
        "final_errors": list(final_errors),
        "reference_materialization": reference_materialization,
        "blocked_binding_attempts": [
            [
                {"name": call.tool_name, "arguments": call.arguments}
                for call in attempt
            ]
            for attempt in runtime.blocked_attempts
        ],
        "runtime_receipts": [
            {
                "tool_name": receipt.tool_name,
                "status": receipt.status.value,
                "changed": receipt.changed,
                "error_code": receipt.error_code,
                "before_version": receipt.before_version,
                "after_version": receipt.after_version,
                "safe_user_facts": receipt.safe_user_facts,
            }
            for receipt in result.receipts
        ],
        "weekly_focus_before": focus_before,
        "weekly_focus_after": focus_after,
        "weekly_focus_preserved": focus_preserved,
        "zero_write_proof": {
            "business_database_connected": False,
            "business_data_written": False,
            "receipt_store_written": False,
            "production_handlers_called": False,
            "production_executor_reference_materialization": (
                "locked_in_memory_weekly_store_and_captured_daily_command"
            ),
            "production_executor_no_db_materialization_called": bool(
                reference_materialization.get("called")
            ),
            "messages_sent": False,
            "runtime_batches": [list(batch) for batch in runtime_batches],
            "in_memory_commit_count": runtime.commit_count,
            "in_memory_rollback_count": runtime.rollback_count,
            "runtime_business_write_count": runtime.business_write_count,
            "runtime_message_send_count": runtime.message_send_count,
            "all_receipts_unchanged": all(
                not receipt.changed for receipt in result.receipts
            ),
            "all_runtime_write_counters_zero": all(
                not (
                    item.actual_write
                    or item.business_write_count
                    or item.pending_write_count
                    or item.memory_write_count
                    or item.memory_audit_write_count
                    or item.receipt_write_count
                    or item.handler_call_count
                )
                for item in result.runtime_results
            ),
        },
        "model_content_sha256": result.model_content_sha256,
        "elapsed_ms": round((perf_counter() - started) * 1000, 1),
        "overall_pass": overall,
        "failure_reason": None
        if overall
        else "; ".join(
            (
                *final_errors,
                *(
                    ("adapter semantic review did not run",)
                    if not review_stages
                    else ()
                ),
                *(
                    ("weekly focus was not preserved",)
                    if not focus_preserved
                    else ()
                ),
            )
        ),
    }


def _case_manifest(
    cases: tuple[CrossTurnCase, ...] = CASES,
) -> list[dict[str, Any]]:
    return [
        {
            "case_id": case.case_id,
            "category": case.category,
            "user_text": case.user_text,
            "plan_items": [
                {"plan_date": plan_date, "content": content}
                for plan_date, content in case.plan_items
            ],
            "recent_messages": [
                {"role": role, "content": content}
                for role, content in case.recent_messages
            ],
            "expected_daily_contents": list(case.expected_daily_contents),
            "expected_weekly_additions": [
                {"plan_date": plan_date, "content": content}
                for plan_date, content in case.expected_weekly_additions
            ],
            "existing_daily_contents": list(case.existing_daily_contents),
            "expects_clarification": case.expects_clarification,
            "plan_version": case.plan_version,
            "forbid_date_clarification": case.forbid_date_clarification,
        }
        for case in cases
    ]


async def _run(args: argparse.Namespace) -> int:
    requested_case_ids = tuple(args.case_id or ())
    known_case_ids = {case.case_id for case in CASES}
    unknown_case_ids = set(requested_case_ids) - known_case_ids
    if unknown_case_ids:
        raise ValueError(
            "unknown case IDs: " + ", ".join(sorted(unknown_case_ids))
        )
    selected_cases = (
        tuple(case for case in CASES if case.case_id in requested_case_ids)
        if requested_case_ids
        else CASES
    )
    negative_controls = _negative_controls()
    print(
        json.dumps(
            {
                "negative_control": {
                    "expected_red": True,
                    "all_wrong_batches_rejected": True,
                    "case_count": len(negative_controls),
                }
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )
    if args.negative_control_only:
        print(
            json.dumps(
                {
                    "intentional_red_exit": True,
                    "reason": "strict scorer rejected every deliberately wrong batch",
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            flush=True,
        )
        return 2

    api_key = os.environ.get(args.api_key_env, "").strip()
    if not api_key:
        raise RuntimeError(f"missing credential in {args.api_key_env}")
    endpoint = args.base_url.rstrip("/") + "/chat/completions"
    parts = urlsplit(endpoint)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("base URL must be an absolute HTTP(S) URL")

    results: list[dict[str, Any]] = []
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(
        headers=headers,
        timeout=httpx.Timeout(args.timeout_seconds),
    ) as client:
        adapter = DeepSeekToolCallingAdapter(
            http_client=client,
            model=args.model,
            timeout_seconds=args.timeout_seconds,
            max_tool_loops=args.max_tool_loops,
            max_request_attempts=args.max_attempts,
            endpoint=endpoint,
        )
        for round_number in range(1, args.rounds + 1):
            for case in selected_cases:
                result = await _evaluate_one(
                    adapter=adapter,
                    case=case,
                    round_number=round_number,
                )
                results.append(result)
                print(
                    json.dumps(
                        {
                            "round": round_number,
                            "case_id": case.case_id,
                            "initial_tools": [
                                item["name"]
                                for item in result.get("initial", {}).get(
                                    "tool_calls", ()
                                )
                            ],
                            "review_tools": [
                                [item["name"] for item in stage["tool_calls"]]
                                for stage in result.get("review_stages", ())
                            ],
                            "final_tools": [
                                item["name"]
                                for item in result.get("final_tool_calls", ())
                            ],
                            "pass": result["overall_pass"],
                            "failure": result.get("failure_reason")
                            or result.get("error"),
                            "elapsed_ms": result["elapsed_ms"],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    flush=True,
                )

    passed = sum(bool(item["overall_pass"]) for item in results)
    categories = sorted({case.category for case in selected_cases})
    per_category = {
        category: {
            "runs": sum(item["category"] == category for item in results),
            "passed": sum(
                item["category"] == category and item["overall_pass"]
                for item in results
            ),
        }
        for category in categories
    }
    summary = {
        "requested_case_runs": len(results),
        "unique_cases": len(selected_cases),
        "rounds": args.rounds,
        "passed_case_runs": passed,
        "failed_case_runs": len(results) - passed,
        "strict_pass_rate": round(passed / len(results), 4),
        "per_category": per_category,
        "all_negative_controls_rejected": True,
        "all_runs_have_review_stage": all(
            bool(item.get("review_stages")) for item in results
        ),
        "all_weekly_focus_checks_passed": all(
            item.get("weekly_focus_before") == item.get("weekly_focus_after")
            for item in results
        ),
        "zero_business_write_assertions_passed": all(
            item.get("zero_write_proof", {}).get("business_data_written")
            is False
            and item.get("zero_write_proof", {}).get("messages_sent") is False
            for item in results
        ),
        "messages_sent": False,
    }
    artifact = {
        "schema_version": "agent2.daily-weekly.cross-turn.full-adapter-live-eval.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": args.model,
        "adapter_path": "DeepSeekToolCallingAdapter.run_canary_turn",
        "negative_controls": negative_controls,
        "runtime": {
            "kind": "real_binder_plus_ephemeral_in_memory_no_op_recorder",
            "private_direct_context_only": True,
            "production_handlers_called": False,
            "production_executor_reference_materialization": (
                "locked_in_memory_weekly_store_and_captured_daily_command"
            ),
            "business_database_connected": False,
            "business_data_written": False,
            "receipt_store_written": False,
            "messages_sent": False,
        },
        "provider_endpoint_origin_sha256": hashlib.sha256(
            f"{parts.scheme}://{parts.netloc}".encode()
        ).hexdigest(),
        "system_prompt_sha256": hashlib.sha256(
            matrix.canary_system_prompt().encode("utf-8")
        ).hexdigest(),
        "tool_schemas_sha256": matrix._sha256_json(
            deepseek_tool_schemas(_ALL_ALLOWED_TOOLS)
        ),
        "case_manifest_sha256": matrix._sha256_json(
            _case_manifest(selected_cases)
        ),
        "evaluated_source_sha256": {
            "tests/run_agent2_weekly_cross_turn_full_adapter_live.py": matrix._sha256_file(
                Path(__file__)
            ),
            "tests/run_agent2_tri_domain_full_adapter_matrix_live.py": matrix._sha256_file(
                _ROOT / "tests/run_agent2_tri_domain_full_adapter_matrix_live.py"
            ),
            "app/agent2/tool_calling/canary_config.py": matrix._sha256_file(
                _ROOT / "app/agent2/tool_calling/canary_config.py"
            ),
            "app/agent2/tool_calling/deepseek_adapter.py": matrix._sha256_file(
                _ROOT / "app/agent2/tool_calling/deepseek_adapter.py"
            ),
            "app/agent2/tool_calling/registry.py": matrix._sha256_file(
                _ROOT / "app/agent2/tool_calling/registry.py"
            ),
            "app/agent2/tool_calling/validation.py": matrix._sha256_file(
                _ROOT / "app/agent2/tool_calling/validation.py"
            ),
        },
        "summary": summary,
        "case_manifest": _case_manifest(selected_cases),
        "results": results,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(output),
                "artifact_sha256": matrix._sha256_file(output),
                "summary": summary,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )
    return 0 if passed == len(results) else 2


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Isolated Daily/Weekly cross-turn full-adapter live evaluation"
    )
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=float, default=90.0)
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--max-tool-loops", type=int, default=4)
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("DEEPSEEK_API_BASE")
        or os.environ.get("LLM_BASE_URL")
        or "https://api.deepseek.com/v1",
    )
    parser.add_argument("--model", default=matrix.CANARY_MODEL_NAME)
    parser.add_argument(
        "--output",
        default="artifacts/weekly_cross_turn_full_adapter_2rounds.json",
    )
    parser.add_argument("--negative-control-only", action="store_true")
    parser.add_argument(
        "--case-id",
        action="append",
        default=[],
        help="Run only this case ID; repeat to select several cases.",
    )
    args = parser.parse_args()
    if args.rounds < 2:
        parser.error("rounds must be at least 2")
    if (
        args.timeout_seconds <= 0
        or args.max_attempts < 1
        or args.max_tool_loops < 1
    ):
        parser.error("timeouts, attempts, and tool loops must be positive")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
