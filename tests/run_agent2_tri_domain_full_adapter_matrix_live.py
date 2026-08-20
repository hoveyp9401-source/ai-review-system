"""Run an isolated, strict three-domain matrix through the full Agent2 adapter.

This is an evaluation harness, not a production entry point.  It uses the real
configured DeepSeek endpoint while replacing every business executor with an
ephemeral in-memory recorder.  No database client or message provider is
imported, and every proposed write receives a non-changing ``no_op`` receipt.
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
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent2.periodic_report_context import (
    TrustedPeriodicReportContext,
    TrustedPeriodicReportItem,
)
from app.agent2.tool_calling.canary_config import (
    CANARY_MODEL_NAME,
    canary_system_prompt,
)
from app.agent2.tool_calling.context import (
    CANARY_STATE_NAMESPACE,
    TrustedContext,
    TrustedPrincipal,
    TrustedRecentMessage,
    TrustedReportItem,
    TrustedReportSnapshot,
    TrustedRuntimeIdentity,
)
from app.agent2.tool_calling.contracts import (
    ExecutionMode,
    ReceiptStatus,
    ToolReceipt,
)
from app.agent2.tool_calling.current_turn_source import CurrentTurnSource
from app.agent2.tool_calling.deepseek_adapter import (
    DeepSeekCanaryResult,
    DeepSeekToolCallingAdapter,
    DeepSeekToolCallingError,
    ModelTurnAudit,
)
from app.agent2.tool_calling.production_contracts import ProductionRuntimeResult
from app.agent2.tool_calling.registry import (
    TOOL_REGISTRY,
    deepseek_tool_schemas,
    validate_tool_arguments,
)
from app.agent2.tool_calling.validation import NativeToolCall
from app.agent2.tool_calling.validation import ShadowCallBinder
from app.agent2.tool_calling.production_store import ProductionDateResolver
from app.agent2.weekly_plan_context import (
    TrustedWeeklyPlanContext,
    TrustedWeeklyPlanDay,
    TrustedWeeklyPlanItem,
)


_ROOT = Path(__file__).resolve().parents[1]
_USER_ID = UUID("10000000-0000-4000-8000-000000000001")
_DAILY_REPORT_ID = UUID("a9bb2812-89ed-4d2c-b2c4-ed2476744963")
_PERIODIC_REPORT_ID = UUID("b9bb2812-89ed-4d2c-b2c4-ed2476744963")
_FRIDAY_PLAN_ID = UUID("4fa86875-6f8a-477b-b324-8602010d809b")
_MONDAY_CURRENT_PLAN_ID = UUID("5fa86875-6f8a-477b-b324-8602010d809b")
_MONDAY_NEXT_PLAN_ID = UUID("6fa86875-6f8a-477b-b324-8602010d809b")
_DAILY_VERSION = 7
_PERIODIC_VERSION = 3
_FRIDAY_PLAN_VERSION = 4
_MONDAY_CURRENT_VERSION = 5
_MONDAY_NEXT_VERSION = 2
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_FRIDAY_NOW = datetime(2026, 8, 14, 17, 30, tzinfo=_SHANGHAI)
_MONDAY_NOW = datetime(2026, 8, 17, 8, 30, tzinfo=_SHANGHAI)

_TOOLS = frozenset(
    {
        "query_today_report",
        "add_daily_items",
        "confirm_report",
        "query_current_weekly_report",
        "apply_current_weekly_report",
        "submit_current_weekly_report",
        "query_next_weekly_plan",
        "apply_next_weekly_plan",
        "submit_next_weekly_plan",
    }
)


@dataclass(frozen=True)
class MatrixCase:
    case_id: str
    category: str
    user_text: str
    expected_tools: frozenset[str]
    context_variant: Literal[
        "base",
        "daily_ready",
        "periodic_ready",
        "plan_ready",
        "monday_dual",
    ] = "base"
    expected_daily_fields: frozenset[str] = frozenset()
    expected_periodic_fields: frozenset[str] = frozenset()
    expected_plan_bindings: tuple[
        tuple[str, int, frozenset[str], frozenset[str]], ...
    ] = ()
    expected_plan_additions: tuple[tuple[str, str], ...] = ()
    expected_plan_terms_by_date: tuple[
        tuple[str, tuple[str, ...]], ...
    ] = ()
    expected_daily_submit: bool = False
    expected_periodic_submit: bool = False
    expected_plan_submit: tuple[str, int] | None = None
    response_kind: Literal[
        "any",
        "daily_open",
        "clarification",
        "no_submit",
        "no_clarification",
    ] = "any"
    recent_messages: tuple[tuple[Literal["user", "assistant"], str], ...] = ()
    note: str = ""


def _binding(
    plan_id: UUID,
    version: int,
    dates: tuple[str, ...],
    operations: tuple[str, ...] = ("add",),
) -> tuple[str, int, frozenset[str], frozenset[str]]:
    return (
        str(plan_id),
        version,
        frozenset(dates),
        frozenset(operations),
    )


CASES = (
    # Daily Report lifecycle.
    MatrixCase(
        "daily_open",
        "daily_lifecycle",
        "我现在开始写今天的工作日报。",
        frozenset(),
        response_kind="daily_open",
        note="Entering an empty Daily Report should invite content without opening either weekly domain.",
    ),
    MatrixCase(
        "daily_supplement",
        "daily_lifecycle",
        "补充今天日报：今天完成合同初稿，并核对保证金台账。",
        frozenset({"add_daily_items"}),
        expected_daily_fields=frozenset({"today_work"}),
    ),
    MatrixCase(
        "daily_confirm_without_submit",
        "daily_lifecycle",
        "这份今天日报内容确认无误，但先不要提交。",
        frozenset(),
        context_variant="daily_ready",
        response_kind="no_submit",
    ),
    MatrixCase(
        "daily_submit",
        "daily_lifecycle",
        "这是我刚看过的完整日报，确认提交今天日报。",
        frozenset({"confirm_report"}),
        context_variant="daily_ready",
        expected_daily_submit=True,
        recent_messages=(("assistant", "这是今天日报的完整预览，确认后我再提交。"),),
    ),
    # Current ISO-week retrospective report lifecycle.
    MatrixCase(
        "periodic_open",
        "periodic_lifecycle",
        "我想打开并填写本周周报。",
        frozenset({"query_current_weekly_report"}),
    ),
    MatrixCase(
        "periodic_supplement",
        "periodic_lifecycle",
        "补充本周周报：本周完成合同复核；风险是付款材料还没齐；下周计划继续向财务催材料。",
        frozenset({"apply_current_weekly_report"}),
        expected_periodic_fields=frozenset({"accomplishments", "risks", "next_plan"}),
    ),
    MatrixCase(
        "periodic_confirm_without_submit",
        "periodic_lifecycle",
        "这份本周周报内容确认无误，但先不要提交。",
        frozenset(),
        context_variant="periodic_ready",
        response_kind="no_submit",
    ),
    MatrixCase(
        "periodic_submit",
        "periodic_lifecycle",
        "这是我刚看过的本周周报完整预览，确认提交本周周报。",
        frozenset({"submit_current_weekly_report"}),
        context_variant="periodic_ready",
        expected_periodic_submit=True,
        recent_messages=(("assistant", "这是本周周报的完整预览，确认后我再提交。"),),
    ),
    # Monday-to-Saturday Weekly Work Plan lifecycle.
    MatrixCase(
        "plan_open",
        "plan_lifecycle",
        "打开我的下周工作计划，我要开始填写。",
        frozenset({"query_next_weekly_plan"}),
    ),
    MatrixCase(
        "plan_supplement_six_days",
        "plan_lifecycle",
        "补充下周工作计划：周一整理案件材料；周二去上海开庭；周三优化合同评审技能；周四跟进甲项目；周五汇报中台进展；周六暂无安排。",
        frozenset({"apply_next_weekly_plan"}),
        expected_plan_bindings=(
            _binding(
                _FRIDAY_PLAN_ID,
                _FRIDAY_PLAN_VERSION,
                tuple(f"2026-08-{day:02d}" for day in range(17, 23)),
                ("add", "set_day_empty"),
            ),
        ),
    ),
    MatrixCase(
        "plan_parallel_items_share_monday_scope",
        "plan_lifecycle",
        "周一日常用印审核 优化日报机器人",
        frozenset({"apply_next_weekly_plan"}),
        expected_plan_bindings=(
            _binding(
                _FRIDAY_PLAN_ID,
                _FRIDAY_PLAN_VERSION,
                ("2026-08-17",),
                ("add",),
            ),
        ),
        expected_plan_additions=(
            ("2026-08-17", "日常用印审核"),
            ("2026-08-17", "优化日报机器人"),
        ),
        response_kind="no_clarification",
        note=(
            "A space naturally separates two parallel plan items; the leading Monday "
            "scope applies to both, so neither item may become an undated suggestion."
        ),
    ),
    MatrixCase(
        "plan_every_day_scope",
        "plan_lifecycle",
        "每天都做的工作是日常用印审核",
        frozenset({"apply_next_weekly_plan"}),
        expected_plan_bindings=(
            _binding(
                _FRIDAY_PLAN_ID,
                _FRIDAY_PLAN_VERSION,
                tuple(
                    f"2026-08-{day:02d}" for day in range(17, 23)
                ),
                ("add",),
            ),
        ),
        expected_plan_additions=tuple(
            (f"2026-08-{day:02d}", "日常用印审核")
            for day in range(17, 23)
        ),
        response_kind="no_clarification",
        note=(
            "In one open weekly-plan target, every day means all six exact plan "
            "dates and must not be downgraded to an undated suggestion."
        ),
        recent_messages=(
            ("user", "帮我填下周计划"),
            (
                "assistant",
                "下周计划（8月17日至22日，周一至周六）已就绪，请告诉我具体安排。",
            ),
        ),
    ),
    MatrixCase(
        "plan_monday_to_friday_daily_scope",
        "plan_lifecycle",
        "我周一到周五每天做日常用印审核",
        frozenset({"apply_next_weekly_plan"}),
        expected_plan_bindings=(
            _binding(
                _FRIDAY_PLAN_ID,
                _FRIDAY_PLAN_VERSION,
                tuple(
                    f"2026-08-{day:02d}" for day in range(17, 22)
                ),
                ("add",),
            ),
        ),
        expected_plan_additions=tuple(
            (f"2026-08-{day:02d}", "日常用印审核")
            for day in range(17, 22)
        ),
        response_kind="no_clarification",
        note=(
            "An explicit Monday-to-Friday recurrence expands to exactly five "
            "trusted dates in one atomic weekly-plan call."
        ),
    ),
    MatrixCase(
        "plan_confirm_without_submit",
        "plan_lifecycle",
        "这份下周工作计划内容确认无误，但先不要提交。",
        frozenset(),
        context_variant="plan_ready",
        response_kind="no_submit",
    ),
    MatrixCase(
        "plan_submit",
        "plan_lifecycle",
        "这是我刚看过的完整下周工作计划，确认提交这份下周计划。",
        frozenset({"submit_next_weekly_plan"}),
        context_variant="plan_ready",
        expected_plan_submit=(str(_FRIDAY_PLAN_ID), _FRIDAY_PLAN_VERSION),
        recent_messages=(("assistant", "这是下周工作计划的完整预览，确认后我再提交。"),),
    ),
    # Domain collisions and containment.
    MatrixCase(
        "friday_bare_ambiguous",
        "collision",
        "周五整理A项目材料。",
        frozenset(),
        response_kind="clarification",
        note="On Friday this could mean today's report or next Friday's plan.",
    ),
    MatrixCase(
        "friday_explicit_daily",
        "collision",
        "写进今天日报：周五完成A项目材料整理。",
        frozenset({"add_daily_items"}),
        expected_daily_fields=frozenset({"today_work"}),
    ),
    MatrixCase(
        "daily_mentions_completed_weekly_report",
        "collision",
        "写进今天日报：今天完成本周周报汇总。",
        frozenset({"add_daily_items"}),
        expected_daily_fields=frozenset({"today_work"}),
        note="The phrase 周报 is the object of completed daily work, not a report-edit command.",
    ),
    MatrixCase(
        "periodic_next_plan_stays_periodic",
        "collision",
        "补充本周周报的下周计划：下周周一继续向财务催付款材料。",
        frozenset({"apply_current_weekly_report"}),
        expected_periodic_fields=frozenset({"next_plan"}),
        note="A Weekly Report next_plan must not silently become a dated Weekly Work Plan.",
    ),
    MatrixCase(
        "daily_and_plan_same_turn",
        "multi_domain",
        "写进今天日报：今天完成合同初稿；另外下周五工作计划是和业务确认条款。",
        frozenset({"add_daily_items", "apply_next_weekly_plan"}),
        expected_daily_fields=frozenset({"today_work"}),
        expected_plan_bindings=(
            _binding(_FRIDAY_PLAN_ID, _FRIDAY_PLAN_VERSION, ("2026-08-21",)),
        ),
    ),
    MatrixCase(
        "periodic_and_plan_same_turn",
        "multi_domain",
        "补充本周周报：本周完成合同复核；另外下周五工作计划向负责人甲汇报案件进展。",
        frozenset({"apply_current_weekly_report", "apply_next_weekly_plan"}),
        expected_periodic_fields=frozenset({"accomplishments"}),
        expected_plan_bindings=(
            _binding(_FRIDAY_PLAN_ID, _FRIDAY_PLAN_VERSION, ("2026-08-21",)),
        ),
    ),
    MatrixCase(
        "all_three_same_turn",
        "multi_domain",
        "写进今天日报：今天完成台账核对；补充本周周报：本周完成合同复核；另外下周五工作计划向负责人甲汇报案件进展。",
        frozenset({"add_daily_items", "apply_current_weekly_report", "apply_next_weekly_plan"}),
        expected_daily_fields=frozenset({"today_work"}),
        expected_periodic_fields=frozenset({"accomplishments"}),
        expected_plan_bindings=(
            _binding(_FRIDAY_PLAN_ID, _FRIDAY_PLAN_VERSION, ("2026-08-21",)),
        ),
    ),
    # Monday exposes both current-week late fill and the natural following week.
    MatrixCase(
        "monday_fill_current_week",
        "monday_dual_target",
        "我周一才来补本周工作计划：本周周二整理证据，周三开庭。",
        frozenset({"apply_next_weekly_plan"}),
        context_variant="monday_dual",
        expected_plan_bindings=(
            _binding(
                _MONDAY_CURRENT_PLAN_ID,
                _MONDAY_CURRENT_VERSION,
                ("2026-08-18", "2026-08-19"),
            ),
        ),
    ),
    MatrixCase(
        "monday_fill_natural_next_week",
        "monday_dual_target",
        "填写下周工作计划：下周周二整理证据，周三开庭。",
        frozenset({"apply_next_weekly_plan"}),
        context_variant="monday_dual",
        expected_plan_bindings=(
            _binding(
                _MONDAY_NEXT_PLAN_ID,
                _MONDAY_NEXT_VERSION,
                ("2026-08-25", "2026-08-26"),
            ),
        ),
    ),
    MatrixCase(
        "monday_bare_weekday_ambiguous",
        "monday_dual_target",
        "周三整理证据。",
        frozenset(),
        context_variant="monday_dual",
        response_kind="clarification",
        note="With two exact plan targets, a bare weekday cannot select a week.",
    ),
)


@dataclass(frozen=True)
class _RecordedBatch:
    calls: tuple[NativeToolCall, ...]
    defer_finalization: bool


class _ZeroWriteRuntime:
    """Adapter-compatible recorder with no business dependencies or side effects."""

    mode = ExecutionMode.CANARY_EXECUTE

    def __init__(self, context: TrustedContext) -> None:
        self.context = context
        self.batches: list[_RecordedBatch] = []
        self._pending: tuple[ToolReceipt, ...] | None = None
        self.commit_count = 0
        self.rollback_count = 0
        self.business_write_count = 0
        self.message_send_count = 0

    @property
    def all_calls(self) -> tuple[NativeToolCall, ...]:
        return tuple(call for batch in self.batches for call in batch.calls)

    async def execute(
        self,
        calls: tuple[NativeToolCall, ...],
        *,
        defer_finalization: bool = False,
    ) -> ProductionRuntimeResult:
        has_write = any(TOOL_REGISTRY[call.tool_name].read_or_write == "write" for call in calls)
        if not calls or defer_finalization is not has_write:
            raise AssertionError("evaluation received an invalid execution batch")
        if self._pending is not None:
            raise AssertionError("evaluation already has a pending in-memory batch")
        self.batches.append(_RecordedBatch(calls=calls, defer_finalization=defer_finalization))
        receipts = tuple(self._receipt(call) for call in calls)
        if has_write:
            self._pending = receipts
        return self._result(receipts, pending=has_write, committed=False)

    async def commit_pending(self) -> ProductionRuntimeResult:
        if self._pending is None:
            raise AssertionError("evaluation has no pending in-memory batch")
        receipts = self._pending
        self._pending = None
        self.commit_count += 1
        return self._result(receipts, pending=False, committed=True)

    async def rollback_pending(self) -> None:
        if self._pending is not None:
            self._pending = None
            self.rollback_count += 1

    def assert_zero_side_effects(self) -> None:
        if self._pending is not None:
            raise AssertionError("evaluation left a pending in-memory batch")
        if self.business_write_count or self.message_send_count:
            raise AssertionError("evaluation runtime reported an external side effect")

    def _receipt(self, call: NativeToolCall) -> ToolReceipt:
        name = call.tool_name
        definition = TOOL_REGISTRY[name]
        is_write = definition.read_or_write == "write"
        safe_facts: dict[str, Any] = {
            "actual_write": False,
            "evaluation_only": True,
            "production_handler_called": False,
        }
        if (
            definition.transaction_target_policy == "weekly_plan"
            or name == "query_next_weekly_plan"
        ):
            requested = call.arguments.get("plan_id")
            plan = self.context.weekly_plan_by_id(str(requested)) if requested else self.context.weekly_plan
            assert plan is not None
            if name == "query_next_weekly_plan":
                safe_facts["weekly_plan"] = plan.model_payload()
            target_type, target_id, version = "weekly_plan", plan.plan_id, plan.version
        elif (
            definition.transaction_target_policy == "periodic_report"
            or name == "query_current_weekly_report"
        ):
            report = self.context.current_weekly_report
            assert report is not None
            if name == "query_current_weekly_report":
                safe_facts["current_weekly_report"] = report.safe_snapshot()
            target_type, target_id, version = "periodic_report", str(report.report_id), report.version
        else:
            report = self.context.today_report
            assert report is not None
            if name == "query_today_report":
                safe_facts["today_report"] = report.safe_snapshot()
            target_type, target_id, version = "daily_report", str(report.report_id), report.version
        return ToolReceipt(
            status=ReceiptStatus.NO_OP if is_write else ReceiptStatus.SUCCESS,
            tool_name=name,
            changed=False,
            target_type=target_type,
            target_id=target_id,
            before_version=version,
            after_version=version,
            safe_user_facts=safe_facts,
            execution_mode=ExecutionMode.CANARY_EXECUTE,
        )

    @staticmethod
    def _result(
        receipts: tuple[ToolReceipt, ...],
        *,
        pending: bool,
        committed: bool,
    ) -> ProductionRuntimeResult:
        return ProductionRuntimeResult(
            status="success",
            receipts=receipts,
            transaction_opened=pending or committed,
            transaction_pending=pending,
            committed_to_outer_transaction=committed,
            handler_call_count=0,
            business_write_count=0,
            pending_write_count=0,
            memory_write_count=0,
            memory_audit_write_count=0,
            receipt_write_count=0,
        )


class _BinderZeroWriteRuntime(_ZeroWriteRuntime):
    """Run the real deterministic binder while keeping every business port inert."""

    def __init__(self, context: TrustedContext, user_text: str) -> None:
        super().__init__(context)
        self.blocked_attempts: list[tuple[NativeToolCall, ...]] = []
        source = CurrentTurnSource(
            (user_text,),
            occurred_at=(context.now,),
        )
        self._binder = ShadowCallBinder(
            context,
            ProductionDateResolver(),
            report_read_port=None,
            execution_mode=ExecutionMode.CANARY_EXECUTE,
            current_turn_source=source,
        )

    async def execute(
        self,
        calls: tuple[NativeToolCall, ...],
        *,
        defer_finalization: bool = False,
    ) -> ProductionRuntimeResult:
        self._binder.begin_batch()
        bound = []
        failures: list[ToolReceipt] = []
        for call in calls:
            item, failure = await self._binder.bind(call)
            if failure is not None:
                failures.append(
                    failure.model_copy(
                        update={
                            "execution_mode": ExecutionMode.CANARY_EXECUTE,
                            "safe_user_facts": {
                                **failure.safe_user_facts,
                                "actual_write": False,
                                "execution_mode": "canary_execute",
                            },
                        }
                    )
                )
            else:
                bound.append(item)
        if failures:
            if defer_finalization is not any(
                TOOL_REGISTRY[call.tool_name].read_or_write == "write"
                for call in calls
            ):
                raise AssertionError("evaluation received an invalid execution batch")
            # Keep blocked proposals separate from runtime batches.  A failed
            # binding is an audited model attempt, never an executed tool call.
            self.blocked_attempts.append(calls)
            receipts = tuple(
                failures
                + [
                    ToolReceipt(
                        status=ReceiptStatus.BLOCKED,
                        tool_name=item.call.tool_name,
                        changed=False,
                        error_code="ATOMIC_GROUP_PREVALIDATION_FAILED",
                        safe_user_facts={
                            "actual_write": False,
                            "execution_mode": "canary_execute",
                        },
                        execution_mode=ExecutionMode.CANARY_EXECUTE,
                    )
                    for item in bound
                ]
            )
            return ProductionRuntimeResult(
                status="blocked",
                receipts=receipts,
                error_code=receipts[0].error_code,
            )
        return await super().execute(
            calls,
            defer_finalization=defer_finalization,
        )


def _plan(
    *,
    plan_id: UUID,
    target: date,
    version: int,
    status: str = "collecting",
    ready: bool = False,
    roles: tuple[str, ...],
    natural_indexes: tuple[int, ...] = (),
) -> TrustedWeeklyPlanContext:
    days: list[TrustedWeeklyPlanDay] = []
    for offset in range(6):
        plan_date = target + timedelta(days=offset)
        if ready and offset < 5:
            days.append(
                TrustedWeeklyPlanDay(
                    day_id=f"{plan_id}-day-{offset}",
                    plan_date=plan_date,
                    state="planned",
                    items=(
                        TrustedWeeklyPlanItem(
                            item_id=f"{plan_id}-item-{offset}",
                            original_text=f"{plan_date.isoformat()} 已确认计划",
                            source="manual",
                        ),
                    ),
                )
            )
        else:
            days.append(
                TrustedWeeklyPlanDay(
                    day_id=f"{plan_id}-day-{offset}",
                    plan_date=plan_date,
                    state="explicitly_empty" if ready else "unfilled",
                )
            )
    return TrustedWeeklyPlanContext(
        plan_id=str(plan_id),
        batch_id=f"batch-{target.isoformat()}",
        tenant_id="eval-tenant",
        owner_user_id=str(_USER_ID),
        target_week_start=target,
        version=version,
        status=status,
        days=tuple(days),
        roles=roles,
        natural_next_for_message_indexes=natural_indexes,
    )


def _context(case: MatrixCase, round_number: int) -> TrustedContext:
    now = _MONDAY_NOW if case.context_variant == "monday_dual" else _FRIDAY_NOW
    daily_ready = case.context_variant == "daily_ready"
    periodic_ready = case.context_variant == "periodic_ready"
    plan_ready = case.context_variant == "plan_ready"

    daily_items = (
        TrustedReportItem(
            item_id="daily-work-1",
            field="today_work",
            content="完成合同初稿",
            report_id=_DAILY_REPORT_ID,
            report_version=_DAILY_VERSION,
        ),
        TrustedReportItem(
            item_id="daily-plan-1",
            field="tomorrow_plan",
            content="整理案件材料",
            report_id=_DAILY_REPORT_ID,
            report_version=_DAILY_VERSION,
        ),
    ) if daily_ready else ()
    today = TrustedReportSnapshot(
        report_id=_DAILY_REPORT_ID,
        tenant_id="eval-tenant",
        owner_user_id=_USER_ID,
        report_date=now.date(),
        version=_DAILY_VERSION,
        status="pending_confirmation" if daily_ready else "collecting",
        items=daily_items,
        acknowledged_empty_fields=frozenset({"problems"}) if daily_ready else frozenset(),
    )

    iso_year, iso_week, _ = now.date().isocalendar()
    periodic_items = (
        TrustedPeriodicReportItem(
            item_id="periodic-accomplishment-1",
            field="accomplishments",
            content="完成合同复核",
        ),
        TrustedPeriodicReportItem(
            item_id="periodic-risk-1",
            field="risks",
            content="付款材料还没齐",
        ),
        TrustedPeriodicReportItem(
            item_id="periodic-next-1",
            field="next_plan",
            content="下周继续向财务催材料",
        ),
    ) if periodic_ready else ()
    periodic = TrustedPeriodicReportContext(
        tenant_id="eval-tenant",
        owner_user_id=_USER_ID,
        report_id=_PERIODIC_REPORT_ID,
        report_type="weekly",
        period_key=f"{iso_year}-W{iso_week:02d}",
        version=_PERIODIC_VERSION,
        status="collecting",
        items=periodic_items,
    )

    if case.context_variant == "monday_dual":
        plans = (
            _plan(
                plan_id=_MONDAY_CURRENT_PLAN_ID,
                target=date(2026, 8, 17),
                version=_MONDAY_CURRENT_VERSION,
                roles=("active_collection",),
            ),
            _plan(
                plan_id=_MONDAY_NEXT_PLAN_ID,
                target=date(2026, 8, 24),
                version=_MONDAY_NEXT_VERSION,
                roles=("natural_next",),
                natural_indexes=(1,),
            ),
        )
    else:
        plans = (
            _plan(
                plan_id=_FRIDAY_PLAN_ID,
                target=date(2026, 8, 17),
                version=_FRIDAY_PLAN_VERSION,
                status="pending_confirmation" if plan_ready else "collecting",
                ready=plan_ready,
                roles=("active_collection", "natural_next"),
                natural_indexes=(1,),
            ),
        )

    principal = TrustedPrincipal(
        tenant_id="eval-tenant",
        user_id=_USER_ID,
        conversation_id=f"eval-matrix-{case.case_id}-{round_number}",
        source_message_id=f"eval-message-{case.case_id}-{round_number}",
        timezone="Asia/Shanghai",
        display_name="模型评测用户",
        conversation_kind="direct",
    )
    return TrustedContext(
        namespace=CANARY_STATE_NAMESPACE,
        now=now,
        principal=principal,
        runtime_identity=TrustedRuntimeIdentity(
            provider_name="DeepSeek",
            model_name=CANARY_MODEL_NAME,
        ),
        today_report=today,
        current_weekly_report=periodic,
        weekly_plan=plans[0],
        weekly_plans=plans,
        recent_messages=tuple(
            TrustedRecentMessage(
                role=role,
                content=content,
                source_message_id=f"recent-{index}",
            )
            for index, (role, content) in enumerate(case.recent_messages, start=1)
        ),
        allowed_tool_names=_TOOLS,
        gate_decisions={name: True for name in _TOOLS},
    )


def _raw_calls(turn: ModelTurnAudit) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for item in turn.raw_assistant_message.get("tool_calls") or ():
        try:
            calls.append(
                {
                    "name": item["function"]["name"],
                    "arguments": json.loads(item["function"]["arguments"]),
                }
            )
        except (KeyError, TypeError, json.JSONDecodeError):
            continue
    return calls


def _calls_from_runtime(runtime: _ZeroWriteRuntime) -> list[dict[str, Any]]:
    return [
        {"name": call.tool_name, "arguments": call.arguments}
        for call in runtime.all_calls
    ]


def _response_check(case: MatrixCase, content: str | None) -> tuple[bool, str]:
    reply = (content or "").strip()
    if case.response_kind == "any":
        return True, "matched"
    if not reply:
        return False, "expected a natural response but received none"
    if any(marker in reply for marker in ("已提交", "已经提交", "提交成功")):
        return False, "response falsely claimed submission"
    if case.response_kind == "daily_open":
        if "日报" not in reply and "今天" not in reply:
            return False, "daily entry response did not remain in the Daily Report domain"
        return True, "matched"
    if case.response_kind == "no_submit":
        return True, "matched"
    if case.response_kind == "no_clarification":
        redundant_scope_questions = (
            "仅周一",
            "还是每天",
            "哪一天",
            "具体日期",
            "未指定日期",
            "请确认是否",
        )
        if any(marker in reply for marker in redundant_scope_questions):
            return False, "parallel Monday items triggered a redundant date clarification"
        return True, "matched"
    if not (
        any(token in reply for token in ("今天", "日报", "本周"))
        and any(token in reply for token in ("下周", "工作计划", "哪一周"))
    ):
        return False, "clarification did not distinguish the competing date/domain targets"
    return True, "matched"


def _score(
    case: MatrixCase,
    *,
    calls: list[dict[str, Any]],
    assistant_content: str | None,
) -> tuple[bool, tuple[str, ...]]:
    errors: list[str] = []
    names = frozenset(str(call.get("name") or "") for call in calls)
    if names != case.expected_tools:
        errors.append(f"expected tools {sorted(case.expected_tools)}, got {sorted(names)}")

    source = CurrentTurnSource((case.user_text,))
    validated: list[tuple[str, dict[str, Any]]] = []
    for index, call in enumerate(calls):
        name = str(call.get("name") or "")
        arguments = call.get("arguments")
        if name not in _TOOLS or not isinstance(arguments, dict):
            errors.append(f"call[{index}] has an invalid tool or argument envelope")
            continue
        try:
            sealed = validate_tool_arguments(name, arguments)
            source.validate_tool_arguments(name, sealed)
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            continue
        validated.append((name, sealed))

    by_name: dict[str, list[dict[str, Any]]] = {}
    for name, arguments in validated:
        by_name.setdefault(name, []).append(arguments)

    daily = by_name.get("add_daily_items", [])
    if daily:
        if len(daily) != 1:
            errors.append("daily writes were split into multiple calls")
        fields = frozenset(item["field"] for item in daily[0].get("items", ()))
        if fields != case.expected_daily_fields:
            errors.append(f"expected daily fields {sorted(case.expected_daily_fields)}, got {sorted(fields)}")
        if daily[0].get("date_selection") == "trusted_report" and (
            str(daily[0].get("report_id")) != str(_DAILY_REPORT_ID)
            or daily[0].get("expected_version") != _DAILY_VERSION
        ):
            errors.append("daily write selected the wrong trusted report")

    confirms = by_name.get("confirm_report", [])
    if bool(confirms) != case.expected_daily_submit:
        errors.append("daily submit presence did not match the expected lifecycle step")
    for arguments in confirms:
        if str(arguments.get("report_id")) != str(_DAILY_REPORT_ID) or arguments.get("expected_version") != _DAILY_VERSION:
            errors.append("daily submit selected the wrong report or version")

    periodic = by_name.get("apply_current_weekly_report", [])
    if periodic:
        if len(periodic) != 1:
            errors.append("current Weekly Report writes were split into multiple calls")
        arguments = periodic[0]
        if str(arguments.get("report_id")) != str(_PERIODIC_REPORT_ID) or arguments.get("expected_version") != _PERIODIC_VERSION:
            errors.append("current Weekly Report selected the wrong record or version")
        fields = frozenset(
            operation["field"]
            for operation in arguments.get("operations", ())
            if operation.get("operation") == "append"
        )
        if fields != case.expected_periodic_fields:
            errors.append(f"expected Weekly Report fields {sorted(case.expected_periodic_fields)}, got {sorted(fields)}")

    periodic_submits = by_name.get("submit_current_weekly_report", [])
    if bool(periodic_submits) != case.expected_periodic_submit:
        errors.append("current Weekly Report submit presence did not match the expected lifecycle step")
    for arguments in periodic_submits:
        if str(arguments.get("report_id")) != str(_PERIODIC_REPORT_ID) or arguments.get("expected_version") != _PERIODIC_VERSION:
            errors.append("current Weekly Report submit selected the wrong record or version")

    actual_plan_bindings: list[tuple[str, int, frozenset[str], frozenset[str]]] = []
    actual_plan_additions: list[tuple[str, str]] = []
    for arguments in by_name.get("apply_next_weekly_plan", []):
        operations = arguments.get("operations", ())
        dates = frozenset(
            str(operation.get("plan_date") or operation.get("target_plan_date"))
            for operation in operations
            if operation.get("plan_date") or operation.get("target_plan_date")
        )
        operation_names = frozenset(str(operation.get("operation") or "") for operation in operations)
        actual_plan_bindings.append(
            (
                str(arguments.get("plan_id")),
                int(arguments.get("expected_version", -1)),
                dates,
                operation_names,
            )
        )
        actual_plan_additions.extend(
            (
                str(operation.get("plan_date") or ""),
                str(operation.get("content") or ""),
            )
            for operation in operations
            if operation.get("operation") == "add"
        )
    if sorted(actual_plan_bindings) != sorted(case.expected_plan_bindings):
        errors.append(
            "weekly-plan bindings differ: expected "
            f"{sorted(case.expected_plan_bindings)}, got {sorted(actual_plan_bindings)}"
        )
    normalized_actual_plan_additions = tuple(
        (plan_date, content.strip().rstrip("。；;"))
        for plan_date, content in actual_plan_additions
    )
    normalized_expected_plan_additions = tuple(
        (plan_date, content.strip().rstrip("。；;"))
        for plan_date, content in case.expected_plan_additions
    )
    if (
        case.expected_plan_additions
        and normalized_actual_plan_additions
        != normalized_expected_plan_additions
    ):
        errors.append(
            "weekly-plan additions differ: expected "
            f"{case.expected_plan_additions}, got {tuple(actual_plan_additions)}"
        )
    for plan_date, required_terms in case.expected_plan_terms_by_date:
        combined_content = "\n".join(
            content
            for actual_date, content in actual_plan_additions
            if actual_date == plan_date
        )
        missing_terms = tuple(
            term for term in required_terms if term not in combined_content
        )
        if missing_terms:
            errors.append(
                f"weekly-plan date {plan_date} is missing required matter terms "
                f"{missing_terms}; got {combined_content!r}"
            )

    plan_submits = by_name.get("submit_next_weekly_plan", [])
    if bool(plan_submits) != bool(case.expected_plan_submit):
        errors.append("Weekly Work Plan submit presence did not match the expected lifecycle step")
    if case.expected_plan_submit and plan_submits:
        expected_id, expected_version = case.expected_plan_submit
        arguments = plan_submits[0]
        if str(arguments.get("plan_id")) != expected_id or arguments.get("expected_version") != expected_version:
            errors.append("Weekly Work Plan submit selected the wrong plan or version")

    response_ok, response_reason = _response_check(case, assistant_content)
    if not response_ok:
        errors.append(response_reason)
    return not errors, tuple(errors)


def _turn_summary(turn: ModelTurnAudit) -> dict[str, Any]:
    metadata = turn.response_metadata
    return {
        "iteration": turn.iteration,
        "tool_names": [call["name"] for call in _raw_calls(turn)],
        "semantic_review": bool(metadata.get("daily_weekly_write_semantic_review")),
        "zero_draft_confirmation": bool(metadata.get("daily_weekly_zero_draft_write_confirmation")),
        "argument_repair": bool(metadata.get("pre_execution_tool_argument_repair")),
        "request_attempt_count": int(metadata.get("request_attempt_count", 1)),
        "transport_retry_count": int(metadata.get("transport_retry_count", 0)),
        "assistant_message_sha256": turn.assistant_message_sha256,
    }


async def _evaluate_one(
    *,
    adapter: DeepSeekToolCallingAdapter,
    case: MatrixCase,
    round_number: int,
) -> dict[str, Any]:
    context = _context(case, round_number)
    runtime = (
        _BinderZeroWriteRuntime(context, case.user_text)
        if case.case_id in {
            "monday_bare_weekday_ambiguous",
            "plan_parallel_items_share_monday_scope",
            "plan_every_day_scope",
            "plan_monday_to_friday_daily_scope",
        }
        or case.category.startswith("daily_derived")
        else _ZeroWriteRuntime(context)
    )
    started = perf_counter()
    try:
        result = await adapter.run_canary_turn(
            system_prompt=canary_system_prompt(),
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
            "user_text": case.user_text,
            "expected_tools": sorted(case.expected_tools),
            "valid_adapter_result": False,
            "overall_pass": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "semantic_review_count": sum(
                bool(turn.response_metadata.get("daily_weekly_write_semantic_review"))
                for turn in exc.model_turns
            ),
            "model_turns": [_turn_summary(turn) for turn in exc.model_turns],
            "unexecuted_model_calls": [
                _raw_calls(turn) for turn in exc.model_turns
            ],
            "runtime_batches": [[call.tool_name for call in batch.calls] for batch in runtime.batches],
            "zero_business_writes": True,
            "messages_sent": False,
            "elapsed_ms": round((perf_counter() - started) * 1000, 1),
        }

    runtime.assert_zero_side_effects()
    if any(receipt.changed for receipt in result.receipts):
        raise AssertionError("an evaluation receipt claimed a business change")
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
        raise AssertionError("an evaluation runtime result claimed a side effect")

    initial_calls = _raw_calls(result.model_turns[0])
    final_calls = _calls_from_runtime(runtime)
    initial_ok, initial_errors = _score(
        case,
        calls=initial_calls,
        assistant_content=result.model_turns[0].raw_assistant_message.get("content"),
    )
    final_ok, final_errors = _score(
        case,
        calls=final_calls,
        assistant_content=result.final_content,
    )
    review_count = sum(
        bool(turn.response_metadata.get("daily_weekly_write_semantic_review"))
        for turn in result.model_turns
    )
    changed = json.dumps(initial_calls, sort_keys=True, ensure_ascii=False) != json.dumps(final_calls, sort_keys=True, ensure_ascii=False)
    overall = bool(final_ok and review_count >= 1)
    return {
        "round": round_number,
        "case_id": case.case_id,
        "category": case.category,
        "user_text": case.user_text,
        "note": case.note,
        "expected_tools": sorted(case.expected_tools),
        "valid_adapter_result": True,
        "initial_calls": initial_calls,
        "initial_pass": initial_ok,
        "initial_errors": list(initial_errors),
        "final_calls": final_calls,
        "final_content": result.final_content,
        "final_pass": final_ok,
        "final_errors": list(final_errors),
        "semantic_review_count": review_count,
        "review_changed_draft": changed,
        "recovered_by_review": bool(changed and final_ok and not initial_ok),
        "harmed_by_review": bool(changed and initial_ok and not final_ok),
        "runtime_batches": [[call.tool_name for call in batch.calls] for batch in runtime.batches],
        "receipt_error_codes": [
            receipt.error_code for receipt in result.receipts
        ],
        "blocked_attempts": [
            [
                {"name": call.tool_name, "arguments": call.arguments}
                for call in attempt
            ]
            for attempt in getattr(runtime, "blocked_attempts", ())
        ],
        "in_memory_commit_count": runtime.commit_count,
        "in_memory_rollback_count": runtime.rollback_count,
        "zero_business_writes": True,
        "production_handlers_called": False,
        "messages_sent": False,
        "model_turns": [_turn_summary(turn) for turn in result.model_turns],
        "model_content_sha256": result.model_content_sha256,
        "elapsed_ms": round((perf_counter() - started) * 1000, 1),
        "overall_pass": overall,
        "failure_reason": None if overall else "; ".join(
            (*final_errors, *(("adapter semantic review did not run",) if not review_count else ()))
        ),
    }


def _sha256_json(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _case_manifest() -> list[dict[str, Any]]:
    return [
        {
            "case_id": case.case_id,
            "category": case.category,
            "user_text": case.user_text,
            "expected_tools": sorted(case.expected_tools),
            "context_variant": case.context_variant,
            "expected_daily_fields": sorted(case.expected_daily_fields),
            "expected_periodic_fields": sorted(case.expected_periodic_fields),
            "expected_plan_bindings": [
                {
                    "plan_id": plan_id,
                    "expected_version": version,
                    "dates": sorted(dates),
                    "operations": sorted(operations),
                }
                for plan_id, version, dates, operations in case.expected_plan_bindings
            ],
            "expected_plan_additions": [
                {"plan_date": plan_date, "content": content}
                for plan_date, content in case.expected_plan_additions
            ],
            "expected_plan_terms_by_date": [
                {"plan_date": plan_date, "terms": list(terms)}
                for plan_date, terms in case.expected_plan_terms_by_date
            ],
            "expected_daily_submit": case.expected_daily_submit,
            "expected_periodic_submit": case.expected_periodic_submit,
            "expected_plan_submit": case.expected_plan_submit,
            "response_kind": case.response_kind,
        }
        for case in CASES
    ]


def _self_check() -> dict[str, Any]:
    if len(CASES) != len({case.case_id for case in CASES}):
        raise AssertionError("matrix case IDs must be unique")
    categories = {case.category for case in CASES}
    required = {"daily_lifecycle", "periodic_lifecycle", "plan_lifecycle", "collision", "multi_domain", "monday_dual_target"}
    if not required.issubset(categories):
        raise AssertionError("matrix does not cover every required category")
    for case in CASES:
        context = _context(case, 1)
        if context.principal.conversation_kind != "direct":
            raise AssertionError("matrix must remain private-chat only")
        if context.allowed_tool_names != _TOOLS:
            raise AssertionError("matrix tool surface drifted")
    bad_case = next(case for case in CASES if case.case_id == "periodic_next_plan_stays_periodic")
    bad_ok, _ = _score(
        bad_case,
        calls=[
            {
                "name": "apply_next_weekly_plan",
                "arguments": {
                    "plan_id": str(_FRIDAY_PLAN_ID),
                    "expected_version": _FRIDAY_PLAN_VERSION,
                    "operations": [],
                },
            }
        ],
        assistant_content=None,
    )
    if bad_ok:
        raise AssertionError("strict scorer accepted a cross-domain write")
    return {
        "case_count": len(CASES),
        "categories": sorted(categories),
        "private_chat_only": True,
        "business_database_imported": False,
        "message_provider_imported": False,
        "strict_negative_control_passed": True,
    }


async def _run(args: argparse.Namespace) -> int:
    self_check = _self_check()
    print(json.dumps({"self_check": self_check}, ensure_ascii=False, sort_keys=True), flush=True)
    if args.self_check_only:
        return 0

    api_key = os.environ.get(args.api_key_env, "").strip()
    if not api_key:
        raise RuntimeError(f"missing credential in {args.api_key_env}")
    endpoint = args.base_url.rstrip("/") + "/chat/completions"
    parts = urlsplit(endpoint)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("base URL must be an absolute HTTP(S) URL")

    selected_ids = set(args.case_id or ())
    selected = [case for case in CASES if not selected_ids or case.case_id in selected_ids]
    missing = selected_ids - {case.case_id for case in selected}
    if missing:
        raise ValueError(f"unknown case IDs: {sorted(missing)}")

    results: list[dict[str, Any]] = []
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(args.timeout_seconds)) as client:
        adapter = DeepSeekToolCallingAdapter(
            http_client=client,
            model=args.model,
            timeout_seconds=args.timeout_seconds,
            max_tool_loops=args.max_tool_loops,
            max_request_attempts=args.max_attempts,
            endpoint=endpoint,
        )
        for round_number in range(1, args.rounds + 1):
            for case in selected:
                result = await _evaluate_one(adapter=adapter, case=case, round_number=round_number)
                results.append(result)
                print(
                    json.dumps(
                        {
                            "round": round_number,
                            "case_id": case.case_id,
                            "initial": [item["name"] for item in result.get("initial_calls", ())],
                            "final": [item["name"] for item in result.get("final_calls", ())],
                            "review_count": result.get("semantic_review_count", 0),
                            "corrected": result.get("recovered_by_review", False),
                            "pass": result["overall_pass"],
                            "failure": result.get("failure_reason") or result.get("error"),
                            "elapsed_ms": result["elapsed_ms"],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    flush=True,
                )

    passed = sum(bool(item["overall_pass"]) for item in results)
    summary = {
        "requested_case_runs": len(results),
        "unique_cases": len(selected),
        "rounds": args.rounds,
        "passed_case_runs": passed,
        "failed_case_runs": len(results) - passed,
        "strict_pass_rate": round(passed / len(results), 4) if results else 0,
        "semantic_review_run_count": sum(int(item.get("semantic_review_count", 0)) for item in results),
        "runs_with_semantic_review": sum(bool(item.get("semantic_review_count")) for item in results),
        "review_recovery_count": sum(bool(item.get("recovered_by_review")) for item in results),
        "review_harm_count": sum(bool(item.get("harmed_by_review")) for item in results),
        "zero_business_write_assertions_passed": all(item.get("zero_business_writes") is True for item in results),
        "messages_sent": False,
    }
    artifact = {
        "schema_version": "agent2.tri-domain.full-adapter-matrix-live-eval.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": args.model,
        "adapter_path": "DeepSeekToolCallingAdapter.run_canary_turn",
        "self_check": self_check,
        "runtime": {
            "kind": "ephemeral_in_memory_no_op_recorder",
            "production_handlers_called": False,
            "business_database_connected": False,
            "business_data_written": False,
            "receipt_store_written": False,
            "messages_sent": False,
        },
        "provider_endpoint_origin_sha256": hashlib.sha256(f"{parts.scheme}://{parts.netloc}".encode()).hexdigest(),
        "system_prompt_sha256": hashlib.sha256(canary_system_prompt().encode("utf-8")).hexdigest(),
        "tool_schemas_sha256": _sha256_json(deepseek_tool_schemas(_TOOLS)),
        "case_manifest_sha256": _sha256_json(_case_manifest()),
        "evaluated_source_sha256": {
            "tests/run_agent2_tri_domain_full_adapter_matrix_live.py": _sha256_file(Path(__file__)),
            "app/agent2/tool_calling/canary_config.py": _sha256_file(_ROOT / "app/agent2/tool_calling/canary_config.py"),
            "app/agent2/tool_calling/deepseek_adapter.py": _sha256_file(_ROOT / "app/agent2/tool_calling/deepseek_adapter.py"),
            "app/agent2/tool_calling/registry.py": _sha256_file(_ROOT / "app/agent2/tool_calling/registry.py"),
        },
        "summary": summary,
        "case_manifest": _case_manifest(),
        "results": results,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"output": str(output), "artifact_sha256": _sha256_file(output), "summary": summary}, ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if passed == len(results) else 2


def main() -> int:
    parser = argparse.ArgumentParser(description="Strict isolated three-domain full-adapter live matrix")
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=float, default=90.0)
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--max-tool-loops", type=int, default=4)
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    parser.add_argument("--base-url", default=os.environ.get("DEEPSEEK_API_BASE") or os.environ.get("LLM_BASE_URL") or "https://api.deepseek.com/v1")
    parser.add_argument("--model", default=CANARY_MODEL_NAME)
    parser.add_argument("--output", default="artifacts/tri_domain_full_adapter_matrix_2rounds.json")
    parser.add_argument("--case-id", action="append")
    parser.add_argument("--self-check-only", action="store_true")
    args = parser.parse_args()
    if args.rounds < 2:
        parser.error("rounds must be at least 2")
    if args.timeout_seconds <= 0 or args.max_attempts < 1 or args.max_tool_loops < 1:
        parser.error("timeouts, attempts, and tool loops must be positive")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
