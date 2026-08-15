from __future__ import annotations

from datetime import date, datetime, timedelta
import json
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from app.agent2.periodic_report_context import TrustedPeriodicReportContext
from app.agent2.tool_calling.canary_config import canary_system_prompt
from app.agent2.tool_calling.context import (
    CANARY_STATE_NAMESPACE,
    TrustedContext,
    TrustedPrincipal,
)
from app.agent2.tool_calling.contracts import (
    ExecutionMode,
    ReceiptStatus,
    ToolReceipt,
)
from app.agent2.tool_calling.deepseek_adapter import (
    DeepSeekToolCallingAdapter,
    _CompletionResponse,
)
from app.agent2.tool_calling.production_contracts import ProductionRuntimeResult
from app.agent2.weekly_plan_context import (
    TrustedWeeklyPlanContext,
    TrustedWeeklyPlanDay,
)


USER_ID = UUID("10000000-0000-4000-8000-000000000001")
PERIODIC_ID = UUID("20000000-0000-4000-8000-000000000001")
PLAN_ID = "30000000-0000-4000-8000-000000000001"


def _context(*, include_weekly_plan: bool) -> TrustedContext:
    allowed = {
        "add_daily_items",
        "query_current_weekly_report",
        "apply_current_weekly_report",
        "submit_current_weekly_report",
    }
    weekly = None
    if include_weekly_plan:
        allowed.add("apply_next_weekly_plan")
        monday = date(2026, 8, 17)
        weekly = TrustedWeeklyPlanContext(
            plan_id=PLAN_ID,
            batch_id="40000000-0000-4000-8000-000000000001",
            tenant_id="tenant-a",
            owner_user_id=str(USER_ID),
            target_week_start=monday,
            version=0,
            status="collecting",
            days=tuple(
                TrustedWeeklyPlanDay(
                    day_id=f"day-{offset}",
                    plan_date=monday + timedelta(days=offset),
                    state="unfilled",
                )
                for offset in range(6)
            ),
            roles=("active_collection", "natural_next"),
            natural_next_for_message_indexes=(1,),
        )
    return TrustedContext(
        namespace=CANARY_STATE_NAMESPACE,
        now=datetime(2026, 8, 14, 17, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
        principal=TrustedPrincipal(
            tenant_id="tenant-a",
            user_id=USER_ID,
            conversation_id="direct-user",
            source_message_id="message-1",
            timezone="Asia/Shanghai",
            conversation_kind="direct",
        ),
        current_weekly_report=TrustedPeriodicReportContext(
            tenant_id="tenant-a",
            owner_user_id=USER_ID,
            report_id=PERIODIC_ID,
            report_type="weekly",
            period_key="2026-W33",
            version=0,
            status="collecting",
        ),
        weekly_plan=weekly,
        allowed_tool_names=frozenset(allowed),
        gate_decisions={name: True for name in allowed},
    )


def _native(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments, ensure_ascii=False),
        },
    }


def _completion(*calls: dict, content: str | None = None) -> _CompletionResponse:
    return _CompletionResponse(
        message={
            "role": "assistant",
            "content": content,
            **({"tool_calls": list(calls)} if calls else {}),
        },
        metadata={"finish_reason": "tool_calls" if calls else "stop"},
    )


def _write_terminal() -> _CompletionResponse:
    return _completion(
        content=json.dumps(
            {
                "reply": "已分别记录到相应资料中。",
                "actual_write": True,
                "operation_outcome": "changed",
            },
            ensure_ascii=False,
        )
    )


class _Runtime:
    mode = ExecutionMode.CANARY_EXECUTE

    def __init__(self) -> None:
        self.calls = ()
        self.executed_batches: list[tuple[str, ...]] = []
        self.receipts: tuple[ToolReceipt, ...] = ()
        self.commits = 0

    async def execute(self, calls, *, defer_finalization=False):
        self.calls = calls
        self.executed_batches.append(tuple(call.tool_name for call in calls))
        write_tools = {
            "add_daily_items",
            "apply_current_weekly_report",
            "submit_current_weekly_report",
            "apply_next_weekly_plan",
        }
        has_write = any(call.tool_name in write_tools for call in calls)
        assert defer_finalization is has_write
        self.receipts = tuple(
            ToolReceipt(
                status=ReceiptStatus.SUCCESS,
                tool_name=call.tool_name,
                changed=call.tool_name in write_tools,
                target_type=(
                    "periodic_report"
                    if "current_weekly_report" in call.tool_name
                    else "weekly_plan"
                    if "weekly_plan" in call.tool_name
                    else "daily_report"
                ),
                target_id=f"target-{index}",
                before_version=0,
                after_version=1 if call.tool_name in write_tools else 0,
                safe_user_facts={"actual_write": call.tool_name in write_tools},
                execution_mode=ExecutionMode.CANARY_EXECUTE,
            )
            for index, call in enumerate(calls, start=1)
        )
        return ProductionRuntimeResult(
            status="success",
            receipts=self.receipts,
            transaction_opened=has_write,
            transaction_pending=has_write,
            handler_call_count=len(calls),
        )

    async def commit_pending(self):
        self.commits += 1
        return ProductionRuntimeResult(
            status="success",
            receipts=self.receipts,
            transaction_opened=True,
            transaction_pending=False,
            committed_to_outer_transaction=True,
            handler_call_count=len(self.receipts),
            business_write_count=len(self.receipts),
            receipt_write_count=len(self.receipts),
        )

    async def rollback_pending(self):
        return None


def test_system_prompt_gives_positive_current_weekly_report_instructions() -> None:
    prompt = canary_system_prompt()

    for phrase in (
        "query_current_weekly_report",
        "apply_current_weekly_report",
        "submit_current_weekly_report",
        "accomplishments",
        "risks",
        "next_plan",
        "metrics",
    ):
        assert phrase in prompt


@pytest.mark.asyncio
async def test_no_tool_weekly_report_entry_is_recovered_by_semantic_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    runtime = _Runtime()
    responses = iter(
        (
            _completion(content="可以，请把本周内容告诉我。"),
            _completion(_native("review-query", "query_current_weekly_report", {})),
            _completion(content="已经为你打开本周周报草稿。"),
        )
    )
    requested_tools: list[set[str]] = []

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, thinking_enabled
        requested_tools.append(
            {item["function"]["name"] for item in tool_schemas}
        )
        return next(responses)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt=canary_system_prompt(),
        user_text="我想写本周周报",
        context=_context(include_weekly_plan=False),
        runtime_session=runtime,
    )

    assert result.final_content == "已经为你打开本周周报草稿。"
    assert [call.tool_name for call in runtime.calls] == [
        "query_current_weekly_report"
    ]
    assert "query_current_weekly_report" in requested_tools[1]
    assert runtime.commits == 0


@pytest.mark.asyncio
async def test_semantic_review_keeps_daily_weekly_report_and_weekly_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daily = _native(
        "daily",
        "add_daily_items",
        {
            "date_selection": "server_default",
            "items": [
                {
                    "field": "today_work",
                    "content": "今天完成台账核对",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "今天完成台账核对",
                    },
                }
            ],
        },
    )
    periodic = _native(
        "periodic",
        "apply_current_weekly_report",
        {
            "report_id": str(PERIODIC_ID),
            "expected_version": 0,
            "operations": [
                {
                    "operation_id": "periodic-op",
                    "operation": "append",
                    "field": "accomplishments",
                    "content": "本周完成合同复核",
                    "source_evidence": {"source_message_index": 1},
                }
            ],
        },
    )
    weekly = _native(
        "weekly",
        "apply_next_weekly_plan",
        {
            "plan_id": PLAN_ID,
            "expected_version": 0,
            "operations": [
                {
                    "operation_id": "weekly-op",
                    "operation": "add",
                    "plan_date": "2026-08-21",
                    "content": "向负责人甲汇报案件进展",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_clause_quote": "下周五向负责人甲汇报案件进展",
                    },
                }
            ],
        },
    )
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    runtime = _Runtime()
    responses = iter(
        (
            _completion(daily, periodic, weekly),
            _completion(daily, periodic, weekly),
            _write_terminal(),
        )
    )

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, tool_schemas, thinking_enabled
        return next(responses)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    await adapter.run_canary_turn(
        system_prompt=canary_system_prompt(),
        user_text=(
            "今天完成台账核对；本周周报补充：本周完成合同复核；"
            "下周五向负责人甲汇报案件进展"
        ),
        context=_context(include_weekly_plan=True),
        runtime_session=runtime,
    )

    assert [call.tool_name for call in runtime.calls] == [
        "add_daily_items",
        "apply_current_weekly_report",
        "apply_next_weekly_plan",
    ]
    assert runtime.commits == 1


@pytest.mark.asyncio
async def test_semantic_reviewer_mentions_all_three_enabled_domains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    runtime = _Runtime()
    daily = _native(
        "daily",
        "add_daily_items",
        {
            "date_selection": "server_default",
            "items": [
                {
                    "field": "today_work",
                    "content": "今天完成台账核对",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "今天完成台账核对",
                    },
                }
            ],
        },
    )
    responses = iter(
        (
            _completion(daily),
            _completion(daily),
            _write_terminal(),
        )
    )
    review_prompts: list[str] = []

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del tool_schemas, thinking_enabled
        review_prompts.append(str(messages[0]["content"]))
        return next(responses)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    await adapter.run_canary_turn(
        system_prompt=canary_system_prompt(
            allowed_tool_names=_context(include_weekly_plan=True).allowed_tool_names
        ),
        user_text="今天完成台账核对",
        context=_context(include_weekly_plan=True),
        runtime_session=runtime,
    )

    assert "Daily Report" in review_prompts[1]
    assert "Current Weekly Report" in review_prompts[1]
    assert "Weekly Work Plan" in review_prompts[1]


@pytest.mark.asyncio
async def test_periodic_query_does_not_consume_a_later_write_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    periodic = _native(
        "periodic",
        "apply_current_weekly_report",
        {
            "report_id": str(PERIODIC_ID),
            "expected_version": 0,
            "operations": [
                {
                    "operation_id": "periodic-op",
                    "operation": "append",
                    "field": "accomplishments",
                    "content": "本周完成合同复核",
                    "source_evidence": {"source_message_index": 1},
                }
            ],
        },
    )
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    runtime = _Runtime()
    responses = iter(
        (
            _completion(_native("initial-query", "query_current_weekly_report", {})),
            _completion(_native("review-query", "query_current_weekly_report", {})),
            _completion(periodic),
            _completion(periodic),
            _write_terminal(),
        )
    )

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, tool_schemas, thinking_enabled
        return next(responses)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    await adapter.run_canary_turn(
        system_prompt=canary_system_prompt(),
        user_text="本周周报补充：本周完成合同复核",
        context=_context(include_weekly_plan=False),
        runtime_session=runtime,
    )

    assert runtime.executed_batches == [
        ("query_current_weekly_report",),
        ("apply_current_weekly_report",),
    ]
    assert runtime.commits == 1
