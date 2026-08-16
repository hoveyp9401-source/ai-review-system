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
    TrustedRecentOperation,
    TrustedReportReference,
    TrustedReportItem,
    TrustedReportSnapshot,
)
from app.agent2.tool_calling.contracts import (
    ExecutionMode,
    ReceiptStatus,
    ToolReceipt,
)
from app.agent2.tool_calling.deepseek_adapter import (
    DeepSeekResponseError,
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


def _daily_edit_only_context() -> TrustedContext:
    base = _context()
    report_id = UUID("20000000-0000-4000-8000-000000000009")
    report = TrustedReportSnapshot(
        report_id=report_id,
        tenant_id="tenant-a",
        owner_user_id=_USER_ID,
        report_date=date(2026, 8, 14),
        version=9,
        status="collecting",
        items=(
            TrustedReportItem(
                item_id="today-9",
                field="today_work",
                content="旧内容",
                report_id=report_id,
                report_version=9,
            ),
            TrustedReportItem(
                item_id="today-10",
                field="today_work",
                content="另一条旧内容",
                report_id=report_id,
                report_version=9,
            ),
        ),
    )
    return base.model_copy(
        update={
            "today_report": report,
            "allowed_tool_names": frozenset({"edit_daily_items"}),
            "gate_decisions": {"edit_daily_items": True},
        }
    )


def _daily_edit_context_with_recent_focus() -> TrustedContext:
    base = _daily_edit_only_context()
    report = base.today_report
    assert report is not None
    allowed = frozenset(
        {
            "add_daily_items",
            "edit_daily_items",
            "delete_daily_items",
            "move_daily_items",
            "remember_personal_memory",
        }
    )
    operation = TrustedRecentOperation(
        tenant_id=base.principal.tenant_id,
        user_id=base.principal.user_id,
        conversation_id=base.principal.conversation_id,
        source_message_id="previous-message",
        tool_call_id="previous-daily-write",
        tool_name="add_daily_items",
        status=ReceiptStatus.SUCCESS,
        changed=True,
        target_type="daily_report",
        target_id=str(report.report_id),
        before_version=8,
        after_version=report.version,
        affected_item_ids=("today-9",),
        report_reference=TrustedReportReference(
            report_id=report.report_id,
            report_date=report.report_date,
            report_version=report.version,
            report_status=report.status,
            report_state_sha256=report.state_sha256,
        ),
        occurred_at=base.now - timedelta(minutes=1),
    )
    return base.model_copy(
        update={
            "recent_operations": (operation,),
            "allowed_tool_names": allowed,
            "gate_decisions": {name: True for name in allowed},
        }
    )


def _daily_edit_and_weekly_context() -> TrustedContext:
    base = _daily_edit_only_context()
    allowed = frozenset(
        {"edit_daily_items", "apply_next_weekly_plan"}
    )
    return base.model_copy(
        update={
            "allowed_tool_names": allowed,
            "gate_decisions": {name: True for name in allowed},
        }
    )


def _daily_context_with_tools(*tool_names: str) -> TrustedContext:
    base = _daily_edit_only_context()
    allowed = frozenset(tool_names)
    return base.model_copy(
        update={
            "allowed_tool_names": allowed,
            "gate_decisions": {name: True for name in allowed},
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
    report_id: str | None = None,
    expected_version: int | None = None,
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
    if report_id is not None:
        arguments["report_id"] = report_id
    if expected_version is not None:
        arguments["expected_version"] = expected_version
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "add_daily_items",
            "arguments": json.dumps(arguments, ensure_ascii=False),
        },
    }


def _daily_edit_call(
    *,
    call_id: str,
    replacement: str,
    exact_quote: str,
    target_item_ids: list[str] | None = None,
) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "edit_daily_items",
            "arguments": json.dumps(
                {
                    "report_id": "20000000-0000-4000-8000-000000000009",
                    "expected_version": 9,
                    "target_item_ids": target_item_ids or ["today-9"],
                    "replacement": replacement,
                    "replacement_evidence": {
                        "source_message_index": 1,
                        "exact_quote": exact_quote,
                    },
                },
                ensure_ascii=False,
            ),
        },
    }


def _daily_delete_call(
    *,
    call_id: str,
    target_item_ids: list[str],
) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "delete_daily_items",
            "arguments": json.dumps(
                {
                    "report_id": "20000000-0000-4000-8000-000000000009",
                    "expected_version": 9,
                    "target_item_ids": target_item_ids,
                },
                ensure_ascii=False,
            ),
        },
    }


def _daily_move_call(
    *,
    call_id: str,
    target_item_ids: list[str],
) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "move_daily_items",
            "arguments": json.dumps(
                {
                    "report_id": "20000000-0000-4000-8000-000000000009",
                    "expected_version": 9,
                    "target_item_ids": target_item_ids,
                    "source_field": "today_work",
                    "target_field": "tomorrow_plan",
                },
                ensure_ascii=False,
            ),
        },
    }


def _memory_call(*, call_id: str = "memory-draft") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "remember_personal_memory",
            "arguments": json.dumps(
                {
                    "memory_key": "response.preferred_salutation",
                    "value": {"salutation": "复核付款条件"},
                    "source_evidence": {
                        "source_message_index": 1,
                        "intent": "user_salutation_assignment",
                    },
                },
                ensure_ascii=False,
            ),
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
    target_type = (
        "personal_memory"
        if "personal_memory" in tool_name
        else ("weekly_plan" if "weekly_plan" in tool_name else "daily_report")
    )
    return ToolReceipt(
        status=ReceiptStatus.SUCCESS,
        tool_name=tool_name,
        changed=True,
        target_type=target_type,
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
            call.tool_name
            in {
                "add_daily_items",
                "edit_daily_items",
                "apply_next_weekly_plan",
                "remember_personal_memory",
            }
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


async def _run_scripted_write_review(
    monkeypatch: pytest.MonkeyPatch,
    *,
    runtime: _RecordingRuntime,
    context: TrustedContext,
    user_text: str,
    draft_calls: tuple[dict, ...],
    reviewed_calls: tuple[dict, ...],
    adjudicated_calls: tuple[dict, ...] | None = None,
    captured_review_payloads: list[dict] | None = None,
):
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    scripted = [
        _tool_completion(*draft_calls),
        _tool_completion(*reviewed_calls),
    ]
    if adjudicated_calls is not None:
        scripted.append(_tool_completion(*adjudicated_calls))
    scripted.append(_terminal_completion("日报已按原话处理。"))
    completions = iter(scripted)

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        if (
            captured_review_payloads is not None
            and thinking_enabled
            and tool_schemas
            and "isolated Agent2 semantic reviewer" in messages[0]["content"]
        ):
            captured_review_payloads.append(json.loads(messages[1]["content"]))
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)
    return await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text=user_text,
        context=context,
        runtime_session=runtime,
    )


def _keep_original_completion() -> _CompletionResponse:
    return _CompletionResponse(
        message={
            "role": "assistant",
            "content": json.dumps({"decision": "keep_original"}),
        },
        metadata={"finish_reason": "stop"},
    )


def _zero_tool_keep_completion(candidate_reply: str) -> _CompletionResponse:
    return _CompletionResponse(
        message={
            "role": "assistant",
            "content": json.dumps(
                {
                    "decision": "keep",
                    "classification": "ordinary_reply",
                    "reviewed_reply_sha256": hashlib.sha256(
                        candidate_reply.encode("utf-8")
                    ).hexdigest(),
                    "pending_reference": None,
                    "replacement_reply": None,
                }
            ),
        },
        metadata={"finish_reason": "stop"},
    )


@pytest.mark.asyncio
async def test_recent_daily_followup_cannot_be_silently_written_as_memory(
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
    corrected_edit = _daily_edit_call(
        call_id="daily-focus-review",
        replacement="复核付款条件",
        exact_quote="复核付款条件",
    )
    reviewed_edit = _daily_edit_call(
        call_id="daily-semantic-review",
        replacement="复核付款条件",
        exact_quote="复核付款条件",
    )
    completions = iter(
        (
            _tool_completion(_memory_call()),
            _direct_completion("这不是个人记忆，需要结合刚才的日报处理。"),
            _tool_completion(corrected_edit),
            _tool_completion(reviewed_edit),
            _terminal_completion("已把刚才第一条改为复核付款条件。"),
        )
    )
    requested_schemas: list[tuple[str, ...]] = []

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, thinking_enabled
        requested_schemas.append(_schema_names(tool_schemas))
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="把刚才第一条改成复核付款条件",
        context=_daily_edit_context_with_recent_focus(),
        runtime_session=runtime,
    )

    assert result.final_content == "已把刚才第一条改为复核付款条件。"
    assert runtime.execute_count == 1
    assert runtime.commit_count == 1
    assert runtime.rollback_count == 0
    assert [call.tool_name for call in runtime.calls] == ["edit_daily_items"]
    assert "remember_personal_memory" in requested_schemas[1]
    assert "edit_daily_items" in requested_schemas[1]
    assert set(requested_schemas[2]) == {
        "add_daily_items",
        "delete_daily_items",
        "edit_daily_items",
        "move_daily_items",
    }
    assert set(requested_schemas[3]) == {
        "delete_daily_items",
        "edit_daily_items",
        "move_daily_items",
    }


@pytest.mark.asyncio
async def test_explicit_memory_change_remains_allowed_during_daily_focus(
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
            _tool_completion(_memory_call()),
            _tool_completion(_memory_call(call_id="memory-reviewed")),
            _terminal_completion("已记住你希望这样称呼。"),
        )
    )

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="以后请这样称呼我",
        context=_daily_edit_context_with_recent_focus(),
        runtime_session=runtime,
    )

    assert result.final_content == "已记住你希望这样称呼。"
    assert [call.tool_name for call in runtime.calls] == [
        "remember_personal_memory"
    ]
    assert runtime.commit_count == 1
    assert runtime.rollback_count == 0


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

    result = await adapter.run_canary_turn(
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
    assert result.final_content == "已按原话记入今日日报。"
    assert runtime.commit_count == 1
    assert not any(
        turn.response_metadata.get("zero_tool_write_invitation_review")
        or turn.response_metadata.get(
            "zero_tool_write_invitation_replacement_review"
        )
        for turn in result.model_turns
    )


@pytest.mark.asyncio
async def test_daily_edit_negation_partial_quote_is_corrected_before_any_write(
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
    draft = _daily_edit_call(
        call_id="draft-edit",
        replacement="每周一记录旧内容",
        exact_quote="每周一记录",
    )
    reviewed = _daily_edit_call(
        call_id="reviewed-edit",
        replacement="复核模型生成的文字也不能直接写入",
        exact_quote="不再每周一记录",
    )
    completions = iter(
        (
            _tool_completion(draft),
            _tool_completion(reviewed),
            _terminal_completion("已按原话修改第9条。"),
        )
    )
    requested_tool_schemas: list[tuple[str, ...]] = []
    review_system_prompts: list[str] = []

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        requested_tool_schemas.append(_schema_names(tool_schemas))
        if thinking_enabled and tool_schemas:
            review_system_prompts.append(messages[0]["content"])
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="第9条旧内容改为不再每周一记录",
        context=_daily_edit_only_context(),
        runtime_session=runtime,
    )

    assert runtime.execute_count == 1
    assert runtime.calls[0].arguments == {
        "report_id": "20000000-0000-4000-8000-000000000009",
        "expected_version": 9,
        "target_item_ids": ["today-9"],
        "replacement": "每周一记录旧内容",
        "replacement_evidence": {
            "source_message_index": 1,
            "exact_quote": "不再每周一记录",
        },
    }
    assert requested_tool_schemas == [
        ("edit_daily_items",),
        ("edit_daily_items",),
        (),
    ]
    assert "For edit_daily_items" in review_system_prompts[0]
    assert result.model_turns[0].response_metadata[
        "pre_execution_daily_weekly_write_review"
    ] is True
    assert [audit.tool_call_id for audit in result.raw_tool_call_audit] == [
        "draft-edit",
        "reviewed-edit",
    ]


@pytest.mark.asyncio
async def test_daily_edit_partial_quote_review_can_fail_closed(
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
    question = "第9条是改为‘不再每周一记录’吗？"
    completions = iter(
        (
            _tool_completion(
                _daily_edit_call(
                    call_id="draft-edit",
                    replacement="每周一记录旧内容",
                    exact_quote="每周一记录",
                )
            ),
            _clarification_completion(question),
            _zero_tool_keep_completion(question),
        )
    )
    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="第9条旧内容改为不再每周一记录",
        context=_daily_edit_only_context(),
        runtime_session=runtime,
    )

    assert result.final_content == question
    assert runtime.execute_count == 0
    assert runtime.commit_count == 0
    assert runtime.rollback_count == 0


@pytest.mark.asyncio
async def test_daily_edit_review_cannot_change_the_stable_target(
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
    changed_target = _daily_edit_call(
        call_id="reviewed-edit",
        replacement="不再每周一记录",
        exact_quote="不再每周一记录",
    )
    changed_arguments = json.loads(
        changed_target["function"]["arguments"]
    )
    changed_arguments["target_item_ids"] = ["today-1"]
    changed_target["function"]["arguments"] = json.dumps(
        changed_arguments,
        ensure_ascii=False,
    )
    completions = iter(
        (
            _tool_completion(
                _daily_edit_call(
                    call_id="draft-edit",
                    replacement="每周一记录旧内容",
                    exact_quote="每周一记录",
                )
            ),
            _tool_completion(changed_target),
        )
    )

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    with pytest.raises(
        ValueError,
        match="cannot change report, version, or stable item IDs",
    ):
        await adapter.run_canary_turn(
            system_prompt="Agent2 test",
            user_text="第9条旧内容改为不再每周一记录",
            context=_daily_edit_only_context(),
            runtime_session=runtime,
        )

    assert runtime.execute_count == 0
    assert runtime.commit_count == 0


@pytest.mark.parametrize(
    "call_factory",
    (_daily_delete_call, _daily_move_call),
    ids=("delete", "move"),
)
@pytest.mark.asyncio
async def test_daily_targeted_review_cannot_switch_delete_or_move_target(
    monkeypatch: pytest.MonkeyPatch,
    call_factory,
) -> None:
    runtime = _RecordingRuntime()

    with pytest.raises(
        ValueError,
        match="cannot change non-edit Daily write arguments",
    ):
        await _run_scripted_write_review(
            monkeypatch,
            runtime=runtime,
            context=_daily_context_with_tools(
                "delete_daily_items",
                "move_daily_items",
            ),
            user_text="Apply the requested change to the first Daily item.",
            draft_calls=(
                call_factory(
                    call_id="draft-targeted-write",
                    target_item_ids=["today-10"],
                ),
            ),
            reviewed_calls=(
                call_factory(
                    call_id="reviewed-targeted-write",
                    target_item_ids=["today-9"],
                ),
            ),
        )

    assert runtime.execute_count == 0
    assert runtime.commit_count == 0
    assert runtime.rollback_count == 0


@pytest.mark.asyncio
async def test_daily_targeted_review_can_correct_delete_draft_to_edit_same_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _RecordingRuntime()
    review_payloads: list[dict] = []
    await _run_scripted_write_review(
        monkeypatch,
        runtime=runtime,
        context=_daily_context_with_tools(
            "add_daily_items",
            "edit_daily_items",
            "delete_daily_items",
            "move_daily_items",
        ),
        user_text="把第一条改成新的完整内容",
        draft_calls=(
            _daily_delete_call(
                call_id="draft-delete",
                target_item_ids=["today-9"],
            ),
        ),
        reviewed_calls=(
            _daily_edit_call(
                call_id="reviewed-edit",
                replacement="新的完整内容",
                exact_quote="新的完整内容",
                target_item_ids=["today-9"],
            ),
        ),
        captured_review_payloads=review_payloads,
    )

    assert [call.tool_name for call in runtime.calls] == ["edit_daily_items"]
    assert runtime.calls[0].arguments["target_item_ids"] == ["today-9"]
    assert runtime.commit_count == 1
    assert review_payloads[0]["unexecuted_daily_periodic_weekly_operation_draft"] == [
        {
            "draft_kind": "targeted_daily_items",
            "immutable_target": {
                "report_id": "20000000-0000-4000-8000-000000000009",
                "expected_version": 9,
                "target_item_ids": ["today-9"],
            },
        }
    ]


@pytest.mark.asyncio
async def test_daily_targeted_review_cannot_escalate_edit_draft_to_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _RecordingRuntime()
    with pytest.raises(ValueError, match="cannot escalate or redirect"):
        await _run_scripted_write_review(
            monkeypatch,
            runtime=runtime,
            context=_daily_context_with_tools(
                "edit_daily_items",
                "delete_daily_items",
            ),
            user_text="把第一条改成新的完整内容",
            draft_calls=(
                _daily_edit_call(
                    call_id="draft-edit",
                    replacement="新的完整内容",
                    exact_quote="新的完整内容",
                    target_item_ids=["today-9"],
                ),
            ),
            reviewed_calls=(
                _daily_delete_call(
                    call_id="reviewed-delete",
                    target_item_ids=["today-9"],
                ),
            ),
        )

    assert runtime.execute_count == 0
    assert runtime.commit_count == 0


@pytest.mark.asyncio
async def test_daily_delete_cannot_be_dropped_from_atomic_weekly_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _RecordingRuntime()

    with pytest.raises(
        ValueError,
        match="preserve non-edit Daily write names and order",
    ):
        await _run_scripted_write_review(
            monkeypatch,
            runtime=runtime,
            context=_daily_context_with_tools(
                "delete_daily_items",
                "apply_next_weekly_plan",
            ),
            user_text="Delete the selected Daily item and keep the weekly update.",
            draft_calls=(
                _daily_delete_call(
                    call_id="draft-delete",
                    target_item_ids=["today-10"],
                ),
                _weekly_call(call_id="draft-weekly"),
            ),
            reviewed_calls=(_weekly_call(call_id="reviewed-weekly"),),
        )

    assert runtime.execute_count == 0
    assert runtime.commit_count == 0
    assert runtime.rollback_count == 0


@pytest.mark.asyncio
async def test_weekly_write_cannot_be_dropped_from_atomic_daily_delete_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _RecordingRuntime()

    with pytest.raises(
        ValueError,
        match="preserve every reviewed write domain",
    ):
        await _run_scripted_write_review(
            monkeypatch,
            runtime=runtime,
            context=_daily_context_with_tools(
                "delete_daily_items",
                "apply_next_weekly_plan",
            ),
            user_text="Delete the selected Daily item and keep the weekly update.",
            draft_calls=(
                _daily_delete_call(
                    call_id="draft-delete",
                    target_item_ids=["today-10"],
                ),
                _weekly_call(call_id="draft-weekly"),
            ),
            reviewed_calls=(
                _daily_delete_call(
                    call_id="reviewed-delete",
                    target_item_ids=["today-10"],
                ),
            ),
        )

    assert runtime.execute_count == 0
    assert runtime.commit_count == 0
    assert runtime.rollback_count == 0


@pytest.mark.asyncio
async def test_daily_edit_replacement_rule_is_present_with_weekly_tools_enabled(
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
    draft_edit = _daily_edit_call(
        call_id="draft-edit",
        replacement="每周一记录旧内容",
        exact_quote="每周一记录",
    )
    reviewed_edit = _daily_edit_call(
        call_id="reviewed-edit",
        replacement="不再每周一记录旧内容",
        exact_quote="不再每周一记录",
    )
    completions = iter(
        (
            _tool_completion(draft_edit),
            _tool_completion(reviewed_edit),
            _terminal_completion("已按原话修改第9条。"),
        )
    )
    requested_tool_schemas: list[tuple[str, ...]] = []
    review_system_prompts: list[str] = []

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        requested_tool_schemas.append(_schema_names(tool_schemas))
        if thinking_enabled and tool_schemas:
            review_system_prompts.append(messages[0]["content"])
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="第9条旧内容改为不再每周一记录",
        context=_daily_edit_and_weekly_context(),
        runtime_session=runtime,
    )

    assert requested_tool_schemas == [
        ("apply_next_weekly_plan", "edit_daily_items"),
        ("apply_next_weekly_plan", "edit_daily_items"),
        (),
    ]
    assert "For edit_daily_items" in review_system_prompts[0]
    assert "complete contiguous new replacement" in review_system_prompts[0]
    assert [call.tool_name for call in runtime.calls] == [
        "edit_daily_items"
    ]


def _daily_add_call(*, call_id: str) -> dict:
    return _daily_items_call(
        call_id=call_id,
        items=[
            {
                "field": "today_work",
                "content": "新增事项",
                "source_evidence": {
                    "source_message_index": 1,
                    "exact_quote": "新增事项",
                },
            }
        ],
    )


@pytest.mark.asyncio
async def test_daily_edit_review_restores_a_dropped_sibling_add_only_after_adjudication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _RecordingRuntime()
    edit = _daily_edit_call(
        call_id="draft-edit",
        replacement="每周一记录",
        exact_quote="每周一记录",
    )

    result = await _run_scripted_write_review(
        monkeypatch,
        runtime=runtime,
        context=_daily_context_with_tools(
            "add_daily_items",
            "edit_daily_items",
        ),
        user_text="新增事项；第9条旧内容改为每周一记录",
        draft_calls=(_daily_add_call(call_id="draft-add"), edit),
        reviewed_calls=(
            _daily_edit_call(
                call_id="reviewed-edit",
                replacement="每周一记录",
                exact_quote="每周一记录",
            ),
        ),
        adjudicated_calls=(_daily_add_call(call_id="adjudicated-add"),),
    )

    assert [call.tool_name for call in runtime.calls] == [
        "add_daily_items",
        "edit_daily_items",
    ]
    assert [call.tool_call_id for call in runtime.calls] == [
        "adjudicated-add",
        "reviewed-edit",
    ]
    assert runtime.execute_count == 1
    assert runtime.commit_count == 1
    assert result.model_turns[2].response_metadata[
        "dropped_daily_add_independent_adjudication"
    ] is True


@pytest.mark.asyncio
async def test_multiple_daily_adds_dropped_beside_weekly_fail_before_adjudication(
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
            _tool_completion(
                _daily_call(call_id="draft-daily-1"),
                _daily_add_call(call_id="draft-daily-2"),
                _weekly_call(call_id="draft-weekly"),
            ),
            _tool_completion(_weekly_call(call_id="reviewed-weekly")),
        )
    )
    request_count = 0

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        nonlocal request_count
        del messages, tool_schemas, thinking_enabled
        request_count += 1
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    with pytest.raises(
        DeepSeekResponseError,
        match="multiple Daily add drafts cannot enter adjudication",
    ):
        await adapter.run_canary_turn(
            system_prompt="Agent2 test",
            user_text="今天完成合同审核并新增事项；下周三整理案件材料。",
            context=_context(),
            runtime_session=runtime,
        )

    assert request_count == 2
    assert runtime.execute_count == 0
    assert runtime.commit_count == 0
    assert runtime.rollback_count == 0


@pytest.mark.asyncio
async def test_daily_edit_review_cannot_reorder_a_sibling_add(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _RecordingRuntime()

    with pytest.raises(
        ValueError,
        match="preserve non-edit Daily write names and order",
    ):
        await _run_scripted_write_review(
            monkeypatch,
            runtime=runtime,
            context=_daily_context_with_tools(
                "add_daily_items",
                "edit_daily_items",
            ),
            user_text="新增事项；第9条旧内容改为每周一记录",
            draft_calls=(
                _daily_add_call(call_id="draft-add"),
                _daily_edit_call(
                    call_id="draft-edit",
                    replacement="每周一记录",
                    exact_quote="每周一记录",
                ),
            ),
            reviewed_calls=(
                _daily_edit_call(
                    call_id="reviewed-edit",
                    replacement="每周一记录",
                    exact_quote="每周一记录",
                ),
                _daily_add_call(call_id="reviewed-add"),
            ),
        )

    assert runtime.execute_count == 0


@pytest.mark.asyncio
async def test_daily_edit_review_cannot_add_a_sibling_daily_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _RecordingRuntime()

    with pytest.raises(
        ValueError,
        match="preserve non-edit Daily write names and order",
    ):
        await _run_scripted_write_review(
            monkeypatch,
            runtime=runtime,
            context=_daily_context_with_tools(
                "add_daily_items",
                "edit_daily_items",
                "apply_next_weekly_plan",
            ),
            user_text="第9条旧内容改为每周一记录",
            draft_calls=(
                _daily_edit_call(
                    call_id="draft-edit",
                    replacement="每周一记录",
                    exact_quote="每周一记录",
                ),
            ),
            reviewed_calls=(
                _daily_add_call(call_id="reviewed-add"),
                _daily_edit_call(
                    call_id="reviewed-edit",
                    replacement="每周一记录",
                    exact_quote="每周一记录",
                ),
            ),
        )

    assert runtime.execute_count == 0


@pytest.mark.asyncio
async def test_daily_edit_review_cannot_change_a_sibling_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _RecordingRuntime()

    with pytest.raises(
        ValueError,
        match="cannot change non-edit Daily write arguments",
    ):
        await _run_scripted_write_review(
            monkeypatch,
            runtime=runtime,
            context=_daily_context_with_tools(
                "edit_daily_items",
                "delete_daily_items",
                "apply_next_weekly_plan",
            ),
            user_text="第9条改为每周一记录，并删除第10条",
            draft_calls=(
                _daily_edit_call(
                    call_id="draft-edit",
                    replacement="每周一记录",
                    exact_quote="每周一记录",
                ),
                _daily_delete_call(
                    call_id="draft-delete",
                    target_item_ids=["today-10"],
                ),
            ),
            reviewed_calls=(
                _daily_edit_call(
                    call_id="reviewed-edit",
                    replacement="每周一记录",
                    exact_quote="每周一记录",
                ),
                _daily_delete_call(
                    call_id="reviewed-delete",
                    target_item_ids=["today-9"],
                ),
            ),
        )

    assert runtime.execute_count == 0


@pytest.mark.asyncio
async def test_daily_edit_review_keeps_multiple_same_text_edit_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _RecordingRuntime()
    result = await _run_scripted_write_review(
        monkeypatch,
        runtime=runtime,
        context=_daily_context_with_tools(
            "edit_daily_items",
            "apply_next_weekly_plan",
        ),
        user_text="第9条和第10条都改为每周一记录",
        draft_calls=(
            _daily_edit_call(
                call_id="draft-edit-9",
                replacement="每周一记录旧内容",
                exact_quote="每周一记录",
                target_item_ids=["today-9"],
            ),
            _daily_edit_call(
                call_id="draft-edit-10",
                replacement="每周一记录另一条旧内容",
                exact_quote="每周一记录",
                target_item_ids=["today-10"],
            ),
        ),
        reviewed_calls=(
            _daily_edit_call(
                call_id="reviewed-edit-10",
                replacement="复核模型文字10",
                exact_quote="每周一记录",
                target_item_ids=["today-10"],
            ),
            _daily_edit_call(
                call_id="reviewed-edit-9",
                replacement="复核模型文字9",
                exact_quote="每周一记录",
                target_item_ids=["today-9"],
            ),
        ),
    )

    assert [
        tuple(call.arguments["target_item_ids"])
        for call in runtime.calls
    ] == [("today-10",), ("today-9",)]
    assert all(
        call.arguments["replacement_evidence"]["exact_quote"]
        == "每周一记录"
        for call in runtime.calls
    )
    assert result.model_turns[1].response_metadata[
        "daily_weekly_write_semantic_review"
    ] is True


def _daily_edit_weekly_batch(label: str) -> tuple[dict, dict]:
    return (
        _daily_edit_call(
            call_id=f"{label}-edit",
            replacement="每周一记录",
            exact_quote="每周一记录",
        ),
        _weekly_call(call_id=f"{label}-weekly"),
    )


@pytest.mark.asyncio
async def test_daily_edit_and_weekly_write_remain_one_atomic_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _RecordingRuntime()
    await _run_scripted_write_review(
        monkeypatch,
        runtime=runtime,
        context=_daily_edit_and_weekly_context(),
        user_text="第9条改为每周一记录；下周三整理案件材料",
        draft_calls=_daily_edit_weekly_batch("draft"),
        reviewed_calls=_daily_edit_weekly_batch("reviewed"),
    )

    assert [call.tool_name for call in runtime.calls] == [
        "edit_daily_items",
        "apply_next_weekly_plan",
    ]
    assert runtime.execute_count == 1
    assert runtime.commit_count == 1
    assert runtime.rollback_count == 0


@pytest.mark.asyncio
async def test_daily_edit_and_weekly_batch_rolls_back_on_terminal_failure(
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
            _tool_completion(*_daily_edit_weekly_batch("draft")),
            _tool_completion(*_daily_edit_weekly_batch("reviewed")),
            _direct_completion("   "),
            _direct_completion("\n"),
            _direct_completion("\t"),
        )
    )

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    with pytest.raises(DeepSeekResponseError):
        await adapter.run_canary_turn(
            system_prompt="Agent2 test",
            user_text="第9条改为每周一记录；下周三整理案件材料",
            context=_daily_edit_and_weekly_context(),
            runtime_session=runtime,
        )

    assert runtime.execute_count == 1
    assert runtime.commit_count == 0
    assert runtime.rollback_count == 1


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
async def test_daily_review_rebinds_server_default_to_exact_trusted_open_report(
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
    trusted = _daily_edit_only_context().today_report
    assert trusted is not None
    historical = trusted.model_copy(update={"report_date": date(2026, 8, 13)})
    context = _daily_only_context().model_copy(
        update={"historical_reports": (historical,)}
    )
    reviewed = _daily_items_call(
        call_id="reviewed-daily",
        date_selection="trusted_report",
        report_id=str(historical.report_id),
        expected_version=historical.version,
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
    review_payloads: list[dict] = []

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        if thinking_enabled and tool_schemas:
            review_payloads.append(json.loads(messages[1]["content"]))
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="今天完成A并整理B",
        context=context,
        runtime_session=runtime,
    )

    assert runtime.calls[0].arguments["date_selection"] == "trusted_report"
    assert runtime.calls[0].arguments["report_id"] == str(historical.report_id)
    assert runtime.calls[0].arguments["expected_version"] == historical.version
    draft_payload = review_payloads[0][
        "unexecuted_daily_periodic_weekly_operation_draft"
    ][0]
    assert draft_payload["tool_name"] == "add_daily_items"
    assert "date_selection" not in draft_payload["arguments_without_fallible_date_target"]


@pytest.mark.asyncio
async def test_cross_domain_review_fails_closed_on_untrusted_daily_target_change(
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

    with pytest.raises(
        ValueError,
        match="cannot change an untrusted report-date binding",
    ):
        await adapter.run_canary_turn(
            system_prompt="Agent2 test",
            user_text="今天完成A；下周三整理案件材料。",
            context=_context(),
            runtime_session=runtime,
        )

    assert runtime.execute_count == 0
    assert runtime.commit_count == 0


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
@pytest.mark.parametrize(
    "inner_tool_name",
    (None, "add_daily_items", "delete_daily_items"),
    ids=("arguments-only", "matching-tool", "mismatched-tool"),
)
async def test_daily_write_review_accepts_exact_arguments_envelope(
    monkeypatch: pytest.MonkeyPatch,
    inner_tool_name: str | None,
) -> None:
    runtime = _RecordingRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    message = "今天完成合同复核"
    item = {
        "field": "today_work",
        "content": message,
        "source_evidence": {
            "source_message_index": 1,
            "exact_quote": message,
        },
    }
    draft = _daily_items_call(call_id="draft-daily", items=[item])
    reviewed = _daily_items_call(call_id="reviewed-daily", items=[item])
    reviewed_arguments = json.loads(reviewed["function"]["arguments"])
    envelope = {"arguments": reviewed_arguments}
    if inner_tool_name is not None:
        envelope["tool_name"] = inner_tool_name
    reviewed["function"]["arguments"] = json.dumps(envelope, ensure_ascii=False)
    completions = iter(
        (
            _tool_completion(draft),
            _tool_completion(reviewed),
            _terminal_completion("已按原话记入今日日报。"),
        )
    )

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    if inner_tool_name == "delete_daily_items":
        with pytest.raises(InvalidNativeToolArgumentsError):
            await adapter.run_canary_turn(
                system_prompt="Agent2 test",
                user_text=message,
                context=_daily_only_context(),
                runtime_session=runtime,
            )
        assert runtime.execute_count == 0
        assert runtime.commit_count == 0
        return

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text=message,
        context=_daily_only_context(),
        runtime_session=runtime,
    )

    assert result.final_content == "已按原话记入今日日报。"
    assert runtime.execute_count == 1
    assert runtime.commit_count == 1
    assert runtime.rollback_count == 0


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
            _zero_tool_keep_completion(clarification),
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
            _clarification_completion(question),
            _zero_tool_keep_completion(question),
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
async def test_invalid_daily_review_arguments_get_one_full_review_retry(
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
    invalid_daily = _daily_call(call_id="invalid-reviewed-daily")
    invalid_arguments = json.loads(invalid_daily["function"]["arguments"])
    invalid_arguments["date_selection"] = "trusted_report"
    invalid_daily["function"]["arguments"] = json.dumps(
        invalid_arguments,
        ensure_ascii=False,
    )
    completions = iter(
        (
            _tool_completion(_daily_call(call_id="draft-daily")),
            _tool_completion(invalid_daily),
            _tool_completion(_daily_call(call_id="retried-review")),
            _terminal_completion("已记入今天的日报。"),
        )
    )
    review_retry_seen = False

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        nonlocal review_retry_seen
        del tool_schemas, thinking_enabled
        review_retry_seen = review_retry_seen or any(
            "previous Daily review returned invalid arguments" in str(message.get("content") or "")
            for message in messages
        )
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="今天完成合同审核，记到今天日报。",
        context=_context(),
        runtime_session=runtime,
    )

    assert review_retry_seen is True
    assert result.final_content == "已记入今天的日报。"
    assert runtime.execute_count == 1
    assert runtime.commit_count == 1
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
            _zero_tool_keep_completion(reply),
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
            _zero_tool_keep_completion(reply),
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
            _zero_tool_keep_completion(repaired_reply),
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
            _zero_tool_keep_completion("下午好，有什么需要我一起处理的？"),
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
async def test_zero_tool_clarification_is_independently_reviewed_before_reply(
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
    unsafe_clarification = "请问是日报还是周计划？回复确认后我就替你写入。"
    safe_clarification = "请问你指的是今天的日报，还是下周工作计划？"
    replacement_review = _CompletionResponse(
        message={
            "role": "assistant",
            "content": json.dumps(
                {
                    "decision": "replace",
                    "classification": "unbacked_future_write_invitation",
                    "reviewed_reply_sha256": hashlib.sha256(
                        unsafe_clarification.encode("utf-8")
                    ).hexdigest(),
                    "pending_reference": None,
                    "replacement_reply": safe_clarification,
                },
                ensure_ascii=False,
            ),
        },
        metadata={"finish_reason": "stop"},
    )
    completions = iter(
        (
            _direct_completion("好的。"),
            _clarification_completion(unsafe_clarification),
            replacement_review,
            _zero_tool_keep_completion(safe_clarification),
        )
    )
    request_count = 0

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        nonlocal request_count
        del messages, thinking_enabled
        request_count += 1
        if request_count >= 3:
            assert tool_schemas == []
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="周五处理一下。",
        context=_context(),
        runtime_session=runtime,
    )

    assert result.final_content == safe_clarification
    assert request_count == 4
    assert runtime.execute_count == 0
    assert runtime.commit_count == 0


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
            _zero_tool_keep_completion("早上好，有什么需要我一起处理的？"),
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
    assert request_count == 3
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
        return (
            _direct_completion("早上好。")
            if request_count == 1
            else _zero_tool_keep_completion("早上好。")
        )

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="早上好。",
        context=context,
        runtime_session=runtime,
    )

    assert result.final_content == "早上好。"
    assert request_count == 2
    assert not any(
        turn.response_metadata.get("daily_weekly_write_semantic_review")
        for turn in result.model_turns
    )
    assert result.model_turns[-1].response_metadata[
        "zero_tool_write_invitation_review"
    ] is True
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
        answer = "下午好，有什么需要我一起处理的？"
        return (
            _direct_completion(answer)
            if request_count == 1
            else _zero_tool_keep_completion(answer)
        )

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="下午好。",
        context=context,
        runtime_session=runtime,
    )

    assert result.final_content == "下午好，有什么需要我一起处理的？"
    assert request_count == 2
    assert not any(
        turn.response_metadata.get("daily_weekly_write_semantic_review")
        for turn in result.model_turns
    )
    assert result.model_turns[-1].response_metadata[
        "zero_tool_write_invitation_review"
    ] is True
    assert runtime.execute_count == 0
