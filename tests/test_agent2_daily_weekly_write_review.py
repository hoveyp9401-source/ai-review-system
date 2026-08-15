from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

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
    InvalidNativeToolArgumentsError,
    _CompletionResponse,
)
from app.agent2.tool_calling.production_contracts import ProductionRuntimeResult
from app.agent2.weekly_plan_context import (
    TrustedWeeklyPlanContext,
    TrustedWeeklyPlanDay,
)

_USER_ID = UUID("10000000-0000-4000-8000-000000000001")
_PLAN_ID = "20000000-0000-4000-8000-000000000001"


def _context() -> TrustedContext:
    week_start = date(2026, 8, 17)
    weekly = TrustedWeeklyPlanContext(
        plan_id=_PLAN_ID,
        batch_id="30000000-0000-4000-8000-000000000001",
        tenant_id="tenant-a",
        owner_user_id=str(_USER_ID),
        target_week_start=week_start,
        version=0,
        status="collecting",
        days=tuple(
            TrustedWeeklyPlanDay(
                day_id=f"day-{offset}",
                plan_date=week_start + timedelta(days=offset),
                state="unfilled",
            )
            for offset in range(6)
        ),
        roles=("natural_next",),
        natural_next_for_message_indexes=(1,),
    )
    allowed = frozenset({"add_daily_items", "apply_next_weekly_plan"})
    return TrustedContext(
        namespace=CANARY_STATE_NAMESPACE,
        now=datetime(2026, 8, 14, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        principal=TrustedPrincipal(
            tenant_id="tenant-a",
            user_id=_USER_ID,
            conversation_id="direct-user-a",
            source_message_id="message-1",
            timezone="Asia/Shanghai",
            display_name="测试用户甲",
            conversation_kind="direct",
        ),
        weekly_plan=weekly,
        allowed_tool_names=allowed,
        gate_decisions={name: True for name in allowed},
    )


def _daily_only_context() -> TrustedContext:
    return _context().model_copy(
        update={
            "allowed_tool_names": frozenset({"add_daily_items"}),
            "gate_decisions": {"add_daily_items": True},
        }
    )


def _monday_dual_target_context() -> TrustedContext:
    base = _context()
    current = base.weekly_plan.model_copy(
        update={
            "roles": ("active_collection",),
            "natural_next_for_message_indexes": (),
        }
    )
    next_week_start = date(2026, 8, 24)
    natural_next = TrustedWeeklyPlanContext(
        plan_id="20000000-0000-4000-8000-000000000002",
        batch_id="30000000-0000-4000-8000-000000000002",
        tenant_id="tenant-a",
        owner_user_id=str(_USER_ID),
        target_week_start=next_week_start,
        version=0,
        status="collecting",
        days=tuple(
            TrustedWeeklyPlanDay(
                day_id=f"next-day-{offset}",
                plan_date=next_week_start + timedelta(days=offset),
                state="unfilled",
            )
            for offset in range(6)
        ),
        roles=("natural_next",),
        natural_next_for_message_indexes=(1,),
    )
    return TrustedContext(
        namespace=base.namespace,
        now=datetime(2026, 8, 17, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
        principal=base.principal,
        weekly_plan=current,
        weekly_plans=(current, natural_next),
        allowed_tool_names=base.allowed_tool_names,
        gate_decisions=base.gate_decisions,
    )


def _daily_call(*, call_id: str = "daily") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "add_daily_items",
            "arguments": json.dumps(
                {
                    "date_selection": "server_default",
                    "items": [
                        {
                            "field": "today_work",
                            "content": "今天完成合同审核",
                            "source_evidence": {
                                "source_message_index": 1,
                                "exact_quote": "今天完成合同审核",
                            },
                        }
                    ],
                },
                ensure_ascii=False,
            ),
        },
    }


def _daily_items_call(
    *,
    call_id: str,
    items: list[dict],
    date_selection: str = "server_default",
    date_expression: str | None = None,
    proposed_date: str | None = None,
    date_evidence: dict | None = None,
) -> dict:
    arguments = {
        "date_selection": date_selection,
        "items": items,
    }
    if date_expression is not None:
        arguments["date_expression"] = date_expression
    if proposed_date is not None:
        arguments["proposed_date"] = proposed_date
    if date_evidence is not None:
        arguments["date_evidence"] = date_evidence
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "add_daily_items",
            "arguments": json.dumps(arguments, ensure_ascii=False),
        },
    }


def _weekly_call(*, call_id: str = "weekly") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "apply_next_weekly_plan",
            "arguments": json.dumps(
                {
                    "plan_id": _PLAN_ID,
                    "expected_version": 0,
                    "operations": [
                        {
                            "operation_id": "op-1",
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
                ensure_ascii=False,
            ),
        },
    }


def _weekly_recurrence_call(
    *,
    call_id: str,
    days: tuple[int, ...] = (17, 18, 19, 20, 21, 22),
    message: str = "下周每天做日常用印审核",
) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "apply_next_weekly_plan",
            "arguments": json.dumps(
                {
                    "plan_id": _PLAN_ID,
                    "expected_version": 0,
                    "operations": [
                        {
                            "operation_id": f"recurrence-{day}",
                            "operation": "add",
                            "plan_date": f"2026-08-{day:02d}",
                            "content": "日常用印审核",
                            "source_evidence": {
                                "source_message_index": 1,
                                "exact_clause_quote": message,
                                "recurrence_scope_quote": "下周每天",
                            },
                        }
                        for day in days
                    ],
                },
                ensure_ascii=False,
            ),
        },
    }


def _tool_completion(*calls: dict) -> _CompletionResponse:
    return _CompletionResponse(
        message={"role": "assistant", "content": None, "tool_calls": list(calls)},
        metadata={"finish_reason": "tool_calls"},
    )


def _query_call(*, call_id: str = "query") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "query_today_report", "arguments": "{}"},
    }


def _terminal_completion(reply: str = "已分别记入今天日报和下周工作计划。") -> _CompletionResponse:
    return _CompletionResponse(
        message={
            "role": "assistant",
            "content": json.dumps(
                {
                    "reply": reply,
                    "actual_write": True,
                    "operation_outcome": "changed",
                },
                ensure_ascii=False,
            ),
        },
        metadata={"finish_reason": "stop"},
    )


def _clarification_completion(reply: str) -> _CompletionResponse:
    return _CompletionResponse(
        message={
            "role": "assistant",
            "content": json.dumps(
                {"decision": "clarification", "reply": reply},
                ensure_ascii=False,
            ),
        },
        metadata={"finish_reason": "stop"},
    )


def _receipt(tool_name: str, index: int) -> ToolReceipt:
    return ToolReceipt(
        status=ReceiptStatus.SUCCESS,
        tool_name=tool_name,
        changed=True,
        target_type=("weekly_plan" if "weekly_plan" in tool_name else "daily_report"),
        target_id=f"target-{index}",
        before_version=0,
        after_version=1,
        affected_item_ids=(f"item-{index}",),
        safe_user_facts={"actual_write": True, "tool_name": tool_name},
        execution_mode=ExecutionMode.CANARY_EXECUTE,
    )


class _RecordingRuntime:
    mode = ExecutionMode.CANARY_EXECUTE

    def __init__(self) -> None:
        self.execute_count = 0
        self.commit_count = 0
        self.rollback_count = 0
        self.calls = ()
        self.receipts: tuple[ToolReceipt, ...] = ()

    async def execute(self, calls, *, defer_finalization=False):
        has_write = any(
            call.tool_name in {"add_daily_items", "apply_next_weekly_plan"}
            for call in calls
        )
        assert defer_finalization is has_write
        self.execute_count += 1
        self.calls = calls
        self.receipts = tuple(
            _receipt(call.tool_name, index)
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
        self.commit_count += 1
        return ProductionRuntimeResult(
            status="success",
            receipts=self.receipts,
            transaction_opened=True,
            committed_to_outer_transaction=True,
            handler_call_count=len(self.receipts),
            business_write_count=len(self.receipts),
            receipt_write_count=len(self.receipts),
        )

    async def rollback_pending(self):
        self.rollback_count += 1


def _schema_names(schemas: list[dict]) -> tuple[str, ...]:
    return tuple(item["function"]["name"] for item in schemas)


def _direct_completion(content: str) -> _CompletionResponse:
    return _CompletionResponse(
        message={"role": "assistant", "content": content},
        metadata={"finish_reason": "stop"},
    )


def _keep_original_completion() -> _CompletionResponse:
    return _CompletionResponse(
        message={
            "role": "assistant",
            "content": json.dumps({"decision": "keep_original"}),
        },
        metadata={"finish_reason": "stop"},
    )


@pytest.mark.asyncio
async def test_daily_partial_quote_is_corrected_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _RecordingRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    draft = _daily_items_call(
        call_id="draft-daily",
        items=[
            {
                "field": "today_work",
                "content": "完成合同复核",
                "source_evidence": {
                    "source_message_index": 1,
                    "exact_quote": "完成合同复核",
                },
            }
        ],
    )
    reviewed = _daily_items_call(
        call_id="reviewed-daily",
        items=[
            {
                "field": "today_work",
                "content": "未完成合同复核",
                "source_evidence": {
                    "source_message_index": 1,
                    "exact_quote": "未完成合同复核",
                },
            }
        ],
    )
    completions = iter(
        (
            _tool_completion(draft),
            _tool_completion(reviewed),
            _terminal_completion("已按原话记入今日日报。"),
        )
    )
    requested_tool_schemas: list[tuple[str, ...]] = []

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, thinking_enabled
        requested_tool_schemas.append(_schema_names(tool_schemas))
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="未完成合同复核",
        context=_daily_only_context(),
        runtime_session=runtime,
    )

    assert runtime.execute_count == 1
    assert runtime.calls[0].arguments["items"] == [
        {
            "field": "today_work",
            "content": "未完成合同复核",
            "source_evidence": {
                "source_message_index": 1,
                "exact_quote": "未完成合同复核",
            },
        }
    ]
    assert requested_tool_schemas[1] == ("add_daily_items",)


@pytest.mark.asyncio
async def test_daily_partial_quote_review_splits_independent_items_before_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _RecordingRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    draft = _daily_items_call(
        call_id="draft-daily",
        items=[
            {
                "field": "today_work",
                "content": "完成A并整理B",
                "source_evidence": {
                    "source_message_index": 1,
                    "exact_quote": "完成A并整理B",
                },
            }
        ],
    )
    reviewed = _daily_items_call(
        call_id="reviewed-daily",
        items=[
            {
                "field": "today_work",
                "content": "完成A",
                "source_evidence": {
                    "source_message_index": 1,
                    "exact_quote": "完成A",
                },
            },
            {
                "field": "today_work",
                "content": "整理B",
                "source_evidence": {
                    "source_message_index": 1,
                    "exact_quote": "整理B",
                },
            },
        ],
    )
    completions = iter(
        (
            _tool_completion(draft),
            _tool_completion(reviewed),
            _terminal_completion("两项工作已分别记入今日日报。"),
        )
    )
    review_system_prompts: list[str] = []

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        if thinking_enabled and tool_schemas:
            review_system_prompts.append(messages[0]["content"])
        del tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="今天完成A并整理B",
        context=_daily_only_context(),
        runtime_session=runtime,
    )

    assert [
        item["content"] for item in runtime.calls[0].arguments["items"]
    ] == ["完成A", "整理B"]
    assert "including every negation, condition" in review_system_prompts[0]
    assert "separate Daily item" in review_system_prompts[0]
    assert "action-object pair" in review_system_prompts[0]
    assert "Count the independently editable matters" in review_system_prompts[0]
    assert "source spans for different items must not overlap" in review_system_prompts[0]
    assert "A second action with a different object is a separate item" in review_system_prompts[0]
    assert "one action about a relationship between two objects remains one item" in review_system_prompts[0]
    assert "unmistakably identify that failed write" in review_system_prompts[0]
    assert "Broad delegation, general permission" in review_system_prompts[0]


@pytest.mark.asyncio
async def test_daily_review_cannot_change_the_draft_date_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _RecordingRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    draft = _daily_items_call(
        call_id="draft-daily",
        items=[
            {
                "field": "today_work",
                "content": "完成A并整理B",
                "source_evidence": {
                    "source_message_index": 1,
                    "exact_quote": "完成A并整理B",
                },
            }
        ],
    )
    reviewed = _daily_items_call(
        call_id="reviewed-daily",
        date_selection="agent2_semantic",
        proposed_date="2026-08-13",
        date_evidence={"source_message_index": 1, "exact_quote": "今天"},
        items=[
            {
                "field": "today_work",
                "content": "完成A",
                "source_evidence": {
                    "source_message_index": 1,
                    "exact_quote": "完成A",
                },
            },
            {
                "field": "today_work",
                "content": "整理B",
                "source_evidence": {
                    "source_message_index": 1,
                    "exact_quote": "整理B",
                },
            },
        ],
    )
    completions = iter(
        (
            _tool_completion(draft),
            _tool_completion(reviewed),
            _terminal_completion("两项工作已分别记入今日日报。"),
        )
    )

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="今天完成A并整理B",
        context=_daily_only_context(),
        runtime_session=runtime,
    )

    assert runtime.calls[0].arguments["date_selection"] == "server_default"
    assert runtime.calls[0].arguments["date_expression"] is None
    assert runtime.calls[0].arguments["proposed_date"] is None
    assert runtime.calls[0].arguments["date_evidence"] is None


@pytest.mark.asyncio
async def test_cross_domain_review_cannot_change_the_daily_draft_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _RecordingRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    draft = _daily_items_call(
        call_id="draft-daily",
        items=[
            {
                "field": "today_work",
                "content": "完成A",
                "source_evidence": {
                    "source_message_index": 1,
                    "exact_quote": "完成A",
                },
            }
        ],
    )
    reviewed_daily = _daily_items_call(
        call_id="reviewed-daily",
        date_selection="agent2_semantic",
        proposed_date="2026-08-13",
        date_evidence={"source_message_index": 1, "exact_quote": "今天"},
        items=[
            {
                "field": "today_work",
                "content": "完成A",
                "source_evidence": {
                    "source_message_index": 1,
                    "exact_quote": "完成A",
                },
            }
        ],
    )
    completions = iter(
        (
            _tool_completion(draft),
            _tool_completion(reviewed_daily, _weekly_call(call_id="reviewed-weekly")),
            _terminal_completion("日报和周计划已记录。"),
        )
    )

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="今天完成A；下周三整理案件材料。",
        context=_context(),
        runtime_session=runtime,
    )

    daily = next(call for call in runtime.calls if call.tool_name == "add_daily_items")
    assert daily.arguments["date_selection"] == "server_default"
    assert daily.arguments["date_expression"] is None
    assert daily.arguments["proposed_date"] is None
    assert daily.arguments["date_evidence"] is None


@pytest.mark.asyncio
async def test_one_full_message_daily_item_receives_exactly_one_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _RecordingRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    message = "今天做了日报的基础功能优化"
    draft = _daily_items_call(
        call_id="draft-daily",
        items=[
            {
                "field": "today_work",
                "content": message,
                "source_evidence": {
                    "source_message_index": 1,
                    "exact_quote": message,
                },
            }
        ],
    )
    completions = iter(
        (
            _tool_completion(draft),
            _tool_completion(
                _daily_items_call(
                    call_id="reviewed-daily",
                    items=[
                        {
                            "field": "today_work",
                            "content": message,
                            "source_evidence": {
                                "source_message_index": 1,
                                "exact_quote": message,
                            },
                        }
                    ],
                )
            ),
            _terminal_completion("已按原话记入今日日报。"),
        )
    )
    requested_tool_schemas: list[tuple[str, ...]] = []

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, thinking_enabled
        requested_tool_schemas.append(_schema_names(tool_schemas))
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text=message,
        context=_daily_only_context(),
        runtime_session=runtime,
    )

    assert runtime.execute_count == 1
    assert len(result.model_turns) == 3
    assert sum(
        bool(
            turn.response_metadata.get(
                "daily_weekly_write_semantic_review"
            )
        )
        for turn in result.model_turns
    ) == 1
    assert requested_tool_schemas == [
        ("add_daily_items",),
        ("add_daily_items",),
        (),
    ]


@pytest.mark.asyncio
async def test_independent_review_restores_a_weekly_write_omitted_from_daily_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _RecordingRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    completions = iter(
        (
            _tool_completion(_daily_call(call_id="draft-daily")),
            _tool_completion(
                _daily_call(call_id="reviewed-daily"),
                _weekly_call(call_id="reviewed-weekly"),
            ),
            _terminal_completion(),
        )
    )
    requested_tool_schemas: list[tuple[str, ...]] = []

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, thinking_enabled
        requested_tool_schemas.append(_schema_names(tool_schemas))
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="今天完成合同审核；下周三整理案件材料。",
        context=_context(),
        runtime_session=runtime,
    )

    assert result.final_content == "已分别记入今天日报和下周工作计划。"
    assert runtime.execute_count == 1
    assert runtime.commit_count == 1
    assert [call.tool_name for call in runtime.calls] == [
        "add_daily_items",
        "apply_next_weekly_plan",
    ]
    assert set(requested_tool_schemas[1]) == {
        "add_daily_items",
        "apply_next_weekly_plan",
    }
    assert result.model_turns[0].response_metadata[
        "pre_execution_daily_weekly_write_review"
    ] is True
    assert result.model_turns[1].response_metadata[
        "daily_weekly_write_semantic_review"
    ] is True


@pytest.mark.asyncio
async def test_recurrence_write_uses_existing_independent_semantic_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _RecordingRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    completions = iter(
        (
            _tool_completion(_weekly_recurrence_call(call_id="draft")),
            _tool_completion(_weekly_recurrence_call(call_id="review")),
            _terminal_completion("已把该事项安排到下周周一至周六。"),
        )
    )

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="下周每天做日常用印审核",
        context=_context(),
        runtime_session=runtime,
    )

    assert result.final_content == "已把该事项安排到下周周一至周六。"
    assert runtime.execute_count == 1
    assert runtime.commit_count == 1
    assert len(runtime.calls[0].arguments["operations"]) == 6
    assert result.model_turns[1].response_metadata[
        "daily_weekly_write_semantic_review"
    ] is True


@pytest.mark.asyncio
async def test_recurrence_semantic_review_can_ask_naturally_and_write_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _RecordingRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    clarification = "你是要安排周一至周五，还是周一至周六每天都做？"
    completions = iter(
        (
            _tool_completion(_weekly_recurrence_call(call_id="draft")),
            _clarification_completion(clarification),
        )
    )

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="下周每天做日常用印审核",
        context=_context(),
        runtime_session=runtime,
    )

    assert result.final_content == clarification
    assert runtime.execute_count == 0
    assert runtime.commit_count == 0
    assert runtime.rollback_count == 0


@pytest.mark.asyncio
async def test_daily_and_recurrence_are_confirmed_and_written_in_one_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _RecordingRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    message = "今天完成合同审核；下周每天做日常用印审核"

    def batch(label: str) -> _CompletionResponse:
        return _tool_completion(
            _daily_call(call_id=f"{label}-daily"),
            _weekly_recurrence_call(
                call_id=f"{label}-weekly",
                message=message,
            ),
        )

    completions = iter(
        (
            batch("draft"),
            batch("review"),
            _terminal_completion(),
        )
    )

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text=message,
        context=_context(),
        runtime_session=runtime,
    )

    assert [call.tool_name for call in runtime.calls] == [
        "add_daily_items",
        "apply_next_weekly_plan",
    ]
    assert runtime.execute_count == 1
    assert runtime.commit_count == 1
    assert result.model_turns[0].response_metadata[
        "system_prompt_sha256"
    ] == hashlib.sha256(b"Agent2 test").hexdigest()


@pytest.mark.asyncio
async def test_independent_review_restores_a_daily_write_omitted_from_weekly_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _RecordingRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    completions = iter(
        (
            _tool_completion(_weekly_call(call_id="draft-weekly")),
            _tool_completion(
                _daily_call(call_id="reviewed-daily"),
                _weekly_call(call_id="reviewed-weekly"),
            ),
            _terminal_completion(),
        )
    )

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="今天完成合同审核；下周三整理案件材料。",
        context=_context(),
        runtime_session=runtime,
    )

    assert runtime.execute_count == 1
    assert [call.tool_name for call in runtime.calls] == [
        "add_daily_items",
        "apply_next_weekly_plan",
    ]


@pytest.mark.asyncio
async def test_ambiguous_friday_sentence_stays_a_question_and_executes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _RecordingRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    question = (
        "你说的周五汇报，是今天已经做完要记日报，"
        "还是下周五计划要做？"
    )
    completions = iter(
        (
            _tool_completion(_daily_call(call_id="unsafe-guess")),
            _clarification_completion(question),
        )
    )

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="周五汇报案件进展。",
        context=_context(),
        runtime_session=runtime,
    )

    assert result.final_content == question
    assert result.receipts == ()
    assert runtime.execute_count == 0
    assert runtime.commit_count == 0
    assert runtime.rollback_count == 0


@pytest.mark.asyncio
async def test_clear_daily_only_intent_is_not_forced_into_a_weekly_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _RecordingRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    completions = iter(
        (
            _tool_completion(_daily_call(call_id="draft-daily")),
            _tool_completion(_daily_call(call_id="reviewed-daily")),
            _terminal_completion("已记入今天的日报。"),
        )
    )

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="今天完成合同审核，记到今天日报。",
        context=_context(),
        runtime_session=runtime,
    )

    assert result.final_content == "已记入今天的日报。"
    assert [call.tool_name for call in runtime.calls] == ["add_daily_items"]


@pytest.mark.asyncio
async def test_invalid_reviewed_batch_fails_closed_before_runtime_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _RecordingRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    invalid_weekly = _weekly_call(call_id="invalid-reviewed-weekly")
    invalid_arguments = json.loads(invalid_weekly["function"]["arguments"])
    invalid_arguments["expected_version"] = -1
    invalid_weekly["function"]["arguments"] = json.dumps(
        invalid_arguments,
        ensure_ascii=False,
    )
    completions = iter(
        (
            _tool_completion(_daily_call(call_id="draft-daily")),
            _tool_completion(invalid_weekly),
        )
    )

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    with pytest.raises(
        InvalidNativeToolArgumentsError,
        match="invalid tool arguments",
    ):
        await adapter.run_canary_turn(
            system_prompt="Agent2 test",
            user_text="今天完成合同审核；下周三整理案件材料。",
            context=_context(),
            runtime_session=runtime,
        )

    assert runtime.execute_count == 0
    assert runtime.commit_count == 0
    assert runtime.rollback_count == 0


@pytest.mark.asyncio
async def test_argument_repair_is_followed_by_complete_cross_domain_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _RecordingRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    invalid_daily = _daily_call(call_id="invalid-daily")
    invalid_arguments = json.loads(invalid_daily["function"]["arguments"])
    invalid_arguments["date_selection"] = "trusted_report"
    invalid_daily["function"]["arguments"] = json.dumps(
        invalid_arguments,
        ensure_ascii=False,
    )
    completions = iter(
        (
            _tool_completion(invalid_daily),
            _tool_completion(_daily_call(call_id="repaired-daily")),
            _tool_completion(
                _daily_call(call_id="reviewed-daily"),
                _weekly_call(call_id="reviewed-weekly"),
            ),
            _terminal_completion(),
        )
    )

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="今天完成合同审核；下周三整理案件材料。",
        context=_context(),
        runtime_session=runtime,
    )

    assert [call.tool_name for call in runtime.calls] == [
        "add_daily_items",
        "apply_next_weekly_plan",
    ]
    assert result.model_turns[0].response_metadata[
        "pre_execution_tool_argument_repair"
    ] is True
    assert result.model_turns[1].response_metadata[
        "pre_execution_daily_weekly_write_review"
    ] is True


@pytest.mark.asyncio
async def test_review_preserves_an_unrelated_read_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    allowed = frozenset((*context.allowed_tool_names, "query_today_report"))
    context = context.model_copy(
        update={
            "allowed_tool_names": allowed,
            "gate_decisions": {name: True for name in allowed},
        }
    )
    runtime = _RecordingRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    completions = iter(
        (
            _tool_completion(
                _query_call(call_id="original-query"),
                _daily_call(call_id="draft-daily"),
            ),
            _tool_completion(
                _daily_call(call_id="reviewed-daily"),
                _weekly_call(call_id="reviewed-weekly"),
            ),
            _terminal_completion(),
        )
    )

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="今天完成合同审核；下周三整理案件材料；再显示今天日报。",
        context=context,
        runtime_session=runtime,
    )

    assert [call.tool_name for call in runtime.calls] == [
        "query_today_report",
        "add_daily_items",
        "apply_next_weekly_plan",
    ]
    assert runtime.calls[0].tool_call_id == "original-query"


@pytest.mark.asyncio
async def test_review_preserves_a_weekly_read_beside_a_daily_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    allowed = frozenset((*context.allowed_tool_names, "query_next_weekly_plan"))
    context = context.model_copy(
        update={
            "allowed_tool_names": allowed,
            "gate_decisions": {name: True for name in allowed},
        }
    )
    runtime = _RecordingRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    weekly_query = {
        "id": "original-weekly-query",
        "type": "function",
        "function": {"name": "query_next_weekly_plan", "arguments": "{}"},
    }
    completions = iter(
        (
            _tool_completion(weekly_query, _daily_call(call_id="draft-daily")),
            _tool_completion(_daily_call(call_id="reviewed-daily")),
            _terminal_completion("日报已记录，并展示了下周计划。"),
        )
    )

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="今天完成合同审核；再显示下周计划。",
        context=context,
        runtime_session=runtime,
    )

    assert [call.tool_name for call in runtime.calls] == [
        "query_next_weekly_plan",
        "add_daily_items",
    ]
    assert runtime.calls[0].tool_call_id == "original-weekly-query"


@pytest.mark.asyncio
async def test_zero_tool_draft_requires_matching_second_review_before_new_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _RecordingRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    completions = iter(
        (
            _direct_completion("好的。"),
            _tool_completion(
                _daily_call(call_id="first-review-daily"),
                _weekly_call(call_id="first-review-weekly"),
            ),
            _tool_completion(
                _daily_call(call_id="confirmation-daily"),
                _weekly_call(call_id="confirmation-weekly"),
            ),
            _terminal_completion(),
        )
    )

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="今天完成合同审核；下周三整理案件材料。",
        context=_context(),
        runtime_session=runtime,
    )

    assert [call.tool_name for call in runtime.calls] == [
        "add_daily_items",
        "apply_next_weekly_plan",
    ]
    assert result.model_turns[2].response_metadata[
        "daily_weekly_zero_draft_write_confirmation"
    ] is True


@pytest.mark.asyncio
async def test_zero_tool_review_agreement_ignores_ephemeral_operation_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _RecordingRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    first = _weekly_call(call_id="first-review-weekly")
    second = _weekly_call(call_id="second-review-weekly")
    second_arguments = json.loads(second["function"]["arguments"])
    second_arguments["operations"][0]["operation_id"] = "independent-op-id"
    second["function"]["arguments"] = json.dumps(
        second_arguments,
        ensure_ascii=False,
    )
    completions = iter(
        (
            _direct_completion("好的。"),
            _tool_completion(first),
            _tool_completion(second),
            _terminal_completion("已记入下周工作计划。"),
        )
    )

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="下周三整理案件材料。",
        context=_context(),
        runtime_session=runtime,
    )

    assert result.final_content == "已记入下周工作计划。"
    assert [call.tool_name for call in runtime.calls] == [
        "apply_next_weekly_plan"
    ]
    assert runtime.calls[0].arguments["operations"][0]["operation_id"] == "op-1"


@pytest.mark.asyncio
async def test_zero_tool_draft_write_disagreement_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _RecordingRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    different_weekly = _weekly_call(call_id="confirmation-weekly")
    different_arguments = json.loads(different_weekly["function"]["arguments"])
    different_arguments["operations"][0]["content"] = "另一项材料"
    different_weekly["function"]["arguments"] = json.dumps(
        different_arguments,
        ensure_ascii=False,
    )
    completions = iter(
        (
            _direct_completion("好的。"),
            _tool_completion(
                _daily_call(call_id="first-review-daily"),
                _weekly_call(call_id="first-review-weekly"),
            ),
            _tool_completion(
                _daily_call(call_id="confirmation-daily"),
                different_weekly,
            ),
        )
    )

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    with pytest.raises(Exception, match="did not independently agree"):
        await adapter.run_canary_turn(
            system_prompt="Agent2 test",
            user_text="今天完成合同审核；下周三整理案件材料。",
            context=_context(),
            runtime_session=runtime,
        )

    assert runtime.execute_count == 0
    assert runtime.commit_count == 0
    assert runtime.rollback_count == 0


@pytest.mark.asyncio
async def test_zero_tool_write_confirmation_cannot_drop_one_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _RecordingRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    completions = iter(
        (
            _direct_completion("好的。"),
            _tool_completion(
                _daily_call(call_id="first-review-daily"),
                _weekly_call(call_id="first-review-weekly"),
            ),
            _tool_completion(_daily_call(call_id="confirmation-daily")),
        )
    )

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    with pytest.raises(Exception, match="did not independently agree"):
        await adapter.run_canary_turn(
            system_prompt="Agent2 test",
            user_text="今天完成合同审核；下周三整理案件材料。",
            context=_context(),
            runtime_session=runtime,
        )

    assert runtime.execute_count == 0


@pytest.mark.asyncio
async def test_zero_tool_write_disagreement_becomes_a_model_clarification_without_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _RecordingRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    first = _weekly_call(call_id="first-review")
    second = _weekly_call(call_id="second-review")
    second_arguments = json.loads(second["function"]["arguments"])
    second_arguments["plan_id"] = "20000000-0000-4000-8000-000000000002"
    second_arguments["operations"][0]["plan_date"] = "2026-08-26"
    second["function"]["arguments"] = json.dumps(
        second_arguments,
        ensure_ascii=False,
    )
    reply = "你指的是本周还是下周？"
    completions = iter(
        (
            _direct_completion("好的。"),
            _tool_completion(first),
            _tool_completion(second),
            _clarification_completion(reply),
        )
    )

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="周三整理案件材料。",
        context=_monday_dual_target_context(),
        runtime_session=runtime,
    )

    assert result.final_content == reply
    assert runtime.execute_count == 0
    assert runtime.commit_count == 0


@pytest.mark.asyncio
async def test_friday_daily_vs_weekly_disagreement_becomes_a_model_clarification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _RecordingRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    reply = "你是要记入今日日报，还是安排到下周工作计划？"
    daily = _daily_call(call_id="first-review-daily")
    daily_arguments = json.loads(daily["function"]["arguments"])
    daily_arguments["items"][0]["content"] = "整理A项目材料"
    daily["function"]["arguments"] = json.dumps(
        daily_arguments,
        ensure_ascii=False,
    )
    weekly = _weekly_call(call_id="second-review-weekly")
    weekly_arguments = json.loads(weekly["function"]["arguments"])
    weekly_arguments["operations"][0]["content"] = "整理A项目材料"
    weekly_arguments["operations"][0]["source_evidence"][
        "exact_clause_quote"
    ] = "周五整理A项目材料"
    weekly["function"]["arguments"] = json.dumps(
        weekly_arguments,
        ensure_ascii=False,
    )
    completions = iter(
        (
            _direct_completion("好的。"),
            _tool_completion(daily),
            _tool_completion(weekly),
            _clarification_completion(reply),
        )
    )

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="周五整理A项目材料。",
        context=_context(),
        runtime_session=runtime,
    )

    assert result.final_content == reply
    assert runtime.execute_count == 0
    assert runtime.commit_count == 0


def test_daily_vs_weekly_disagreement_requires_same_grounded_matter() -> None:
    from app.agent2.tool_calling.deepseek_adapter import (
        _parse_assistant_turn,
        _zero_draft_disagreement_is_daily_vs_weekly,
    )

    daily = _parse_assistant_turn(
        _tool_completion(_daily_call(call_id="daily")).message
    ).tool_calls
    weekly = _parse_assistant_turn(
        _tool_completion(_weekly_call(call_id="weekly")).message
    ).tool_calls

    assert not _zero_draft_disagreement_is_daily_vs_weekly(
        first=daily,
        second=weekly,
    )


@pytest.mark.asyncio
async def test_invalid_zero_tool_clarification_envelope_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _RecordingRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    completions = iter(
        (
            _direct_completion("好的。"),
            _direct_completion("请问你指的是哪一个周五？"),
            _direct_completion(
                json.dumps({"decision": "invalid"}, ensure_ascii=False)
            ),
        )
    )

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    with pytest.raises(Exception, match="invalid replacement"):
        await adapter.run_canary_turn(
            system_prompt="Agent2 test",
            user_text="周五汇报案件进展。",
            context=_context(),
            runtime_session=runtime,
        )

    assert runtime.execute_count == 0


@pytest.mark.asyncio
async def test_plain_text_review_clarification_is_repaired_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _RecordingRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    repaired_reply = "你指的是本周还是下周？"
    completions = iter(
        (
            _tool_completion(_weekly_call(call_id="ambiguous-draft")),
            _direct_completion("请问你指的是哪一周？"),
            _clarification_completion(repaired_reply),
        )
    )

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="周三整理案件材料。",
        context=_monday_dual_target_context(),
        runtime_session=runtime,
    )

    assert result.final_content == repaired_reply
    assert runtime.execute_count == 0
    assert runtime.commit_count == 0


@pytest.mark.asyncio
async def test_plain_text_clarification_repair_cannot_introduce_a_write_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _RecordingRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    completions = iter(
        (
            _tool_completion(_weekly_call(call_id="ambiguous-draft")),
            _direct_completion("请问你指的是本周还是下周？"),
            _tool_completion(_weekly_call(call_id="repair-must-not-write")),
            _terminal_completion(),
        )
    )

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    with pytest.raises(Exception, match="invalid replacement"):
        await adapter.run_canary_turn(
            system_prompt="Agent2 test",
            user_text="周三整理案件材料。",
            context=_monday_dual_target_context(),
            runtime_session=runtime,
        )

    assert runtime.execute_count == 0
    assert runtime.commit_count == 0


@pytest.mark.asyncio
async def test_zero_tool_chat_keeps_the_original_answer_without_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _RecordingRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    completions = iter(
        (
            _direct_completion("下午好，有什么需要我一起处理的？"),
            _keep_original_completion(),
        )
    )

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="下午好。",
        context=_context(),
        runtime_session=runtime,
    )

    assert result.final_content == "下午好，有什么需要我一起处理的？"
    assert runtime.execute_count == 0


@pytest.mark.asyncio
async def test_monday_zero_tool_omission_is_restored_only_after_two_matching_reviews(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _RecordingRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    first_weekly = _weekly_call(call_id="first-review-current-week")
    second_weekly = _weekly_call(call_id="second-review-current-week")
    completions = iter(
        (
            _direct_completion("好的。"),
            _tool_completion(first_weekly),
            _tool_completion(second_weekly),
            _terminal_completion("已记入本周工作计划。"),
        )
    )

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="补一下本周三的工作计划：整理案件材料。",
        context=_monday_dual_target_context(),
        runtime_session=runtime,
    )

    assert result.final_content == "已记入本周工作计划。"
    assert [call.tool_name for call in runtime.calls] == [
        "apply_next_weekly_plan"
    ]
    assert runtime.calls[0].arguments["plan_id"] == _PLAN_ID
    assert result.model_turns[2].response_metadata[
        "daily_weekly_zero_draft_write_confirmation"
    ] is True


@pytest.mark.asyncio
async def test_ordinary_monday_chat_is_reviewed_then_kept_without_a_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _RecordingRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    completions = iter(
        (
            _direct_completion("早上好，有什么需要我一起处理的？"),
            _keep_original_completion(),
        )
    )
    request_count = 0

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        nonlocal request_count
        del messages, tool_schemas, thinking_enabled
        request_count += 1
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="早上好。",
        context=_monday_dual_target_context(),
        runtime_session=runtime,
    )

    assert result.final_content == "早上好，有什么需要我一起处理的？"
    assert request_count == 2
    assert runtime.execute_count == 0
    assert runtime.commit_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe_context", ("group", "no_target_role", "no_weekly_tool"))
async def test_monday_zero_tool_review_requires_private_role_bound_weekly_capability(
    monkeypatch: pytest.MonkeyPatch,
    unsafe_context: str,
) -> None:
    context = _monday_dual_target_context()
    if unsafe_context == "group":
        context = context.model_copy(
            update={
                "principal": context.principal.model_copy(
                    update={"conversation_kind": "group"}
                )
            }
        )
    elif unsafe_context == "no_target_role":
        roleless = tuple(
            target.model_copy(
                update={
                    "roles": (),
                    "natural_next_for_message_indexes": (),
                }
            )
            for target in context.weekly_plans
        )
        context = context.model_copy(
            update={"weekly_plan": roleless[0], "weekly_plans": roleless}
        )
    else:
        context = context.model_copy(
            update={
                "allowed_tool_names": frozenset({"add_daily_items"}),
                "gate_decisions": {"add_daily_items": True},
            }
        )

    runtime = _RecordingRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    request_count = 0

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        nonlocal request_count
        del messages, tool_schemas, thinking_enabled
        request_count += 1
        return _direct_completion("早上好。")

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="早上好。",
        context=context,
        runtime_session=runtime,
    )

    assert result.final_content == "早上好。"
    assert request_count == 1
    assert runtime.execute_count == 0


@pytest.mark.asyncio
async def test_same_sentence_current_and_next_friday_contrast_can_route_both_domains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _RecordingRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    friday_daily = _daily_call(call_id="friday-daily")
    daily_arguments = json.loads(friday_daily["function"]["arguments"])
    daily_arguments["items"][0]["content"] = "完成合同审核"
    friday_daily["function"]["arguments"] = json.dumps(
        daily_arguments,
        ensure_ascii=False,
    )
    friday_weekly = _weekly_call(call_id="next-friday-weekly")
    weekly_arguments = json.loads(friday_weekly["function"]["arguments"])
    weekly_arguments["operations"][0].update(
        {
            "plan_date": "2026-08-21",
            "content": "汇报案件进展",
            "source_evidence": {
                "source_message_index": 1,
                "exact_clause_quote": "下周五汇报案件进展",
            },
        }
    )
    friday_weekly["function"]["arguments"] = json.dumps(
        weekly_arguments,
        ensure_ascii=False,
    )
    completions = iter(
        (
            _tool_completion(friday_daily, friday_weekly),
            _tool_completion(friday_daily, friday_weekly),
            _terminal_completion(),
        )
    )
    review_prompts: list[str] = []

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del tool_schemas, thinking_enabled
        review_prompts.append("\n".join(str(item.get("content") or "") for item in messages))
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="周五的工作：完成合同审核；下周五汇报案件进展。",
        context=_context(),
        runtime_session=runtime,
    )

    assert [call.tool_name for call in runtime.calls] == [
        "add_daily_items",
        "apply_next_weekly_plan",
    ]
    assert "standalone" in review_prompts[1]
    assert "explicitly contrasts" in review_prompts[1]
    assert "Current Weekly Report" not in review_prompts[1]
    assert "query_current_weekly_report" not in review_prompts[1]


@pytest.mark.asyncio
async def test_zero_tool_recovery_is_bounded_to_friday_through_monday(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context().model_copy(
        update={
            "now": datetime(
                2026,
                8,
                13,
                16,
                0,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            )
        }
    )
    runtime = _RecordingRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    request_count = 0

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        nonlocal request_count
        del messages, tool_schemas, thinking_enabled
        request_count += 1
        return _direct_completion("下午好，有什么需要我一起处理的？")

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="下午好。",
        context=context,
        runtime_session=runtime,
    )

    assert result.final_content == "下午好，有什么需要我一起处理的？"
    assert request_count == 1
    assert runtime.execute_count == 0
