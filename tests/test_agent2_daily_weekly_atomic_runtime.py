from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import date, datetime, timedelta
from types import MethodType, SimpleNamespace
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
    ToolReceipt,
)
from app.agent2.tool_calling.current_turn_source import CurrentTurnSource
from app.agent2.tool_calling.deepseek_adapter import (
    DeepSeekTimeoutError,
    DeepSeekToolCallingAdapter,
    _CompletionResponse,
)
from app.agent2.tool_calling.production_daily_executor import (
    ProductionExecutionError,
    ProductionHandlerOutcome,
)
from app.agent2.tool_calling.production_runtime import ProductionRuntimeSession
from app.agent2.tool_calling.production_store import ProductionStateSnapshot
from app.agent2.tool_calling.validation import (
    NativeToolCall,
    ShadowCallBinder,
    UnavailableDateResolver,
)
from app.agent2.weekly_plan_context import (
    TrustedWeeklyPlanContext,
    TrustedWeeklyPlanDay,
)


NOW = datetime(2026, 8, 14, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
USER_ID = UUID("10000000-0000-4000-8000-000000000001")
REPORT_ID = UUID("20000000-0000-4000-8000-000000000001")
PLAN_ID = UUID("30000000-0000-4000-8000-000000000001")
MESSAGE = "今天完成本周案件材料整理；下周一跟进案件材料提交。"


def _context() -> TrustedContext:
    week_start = date(2026, 8, 17)
    return TrustedContext(
        namespace=CANARY_STATE_NAMESPACE,
        now=NOW,
        principal=TrustedPrincipal(
            tenant_id="tenant-1",
            user_id=USER_ID,
            conversation_id="private-conversation-1",
            source_message_id="message-1",
            timezone="Asia/Shanghai",
            display_name="测试用户甲",
            conversation_kind="direct",
        ),
        today_report=TrustedReportSnapshot(
            report_id=REPORT_ID,
            tenant_id="tenant-1",
            owner_user_id=USER_ID,
            report_date=NOW.date(),
            version=0,
            status="collecting",
        ),
        weekly_plan=TrustedWeeklyPlanContext(
            plan_id=str(PLAN_ID),
            batch_id="40000000-0000-4000-8000-000000000001",
            tenant_id="tenant-1",
            owner_user_id=str(USER_ID),
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
        ),
        allowed_tool_names=frozenset(
            {"add_daily_items", "apply_next_weekly_plan"}
        ),
        gate_decisions={
            "add_daily_items": True,
            "apply_next_weekly_plan": True,
        },
    )


def _dual_calls() -> tuple[NativeToolCall, NativeToolCall]:
    return (
        NativeToolCall(
            tool_call_id="daily-call-1",
            tool_name="add_daily_items",
            arguments={
                "date_selection": "server_default",
                "items": [
                    {
                        "field": "today_work",
                        "content": "完成本周案件材料整理",
                        "source_evidence": {"source_message_index": 1},
                    }
                ],
            },
        ),
        NativeToolCall(
            tool_call_id="weekly-call-1",
            tool_name="apply_next_weekly_plan",
            arguments={
                "plan_id": str(PLAN_ID),
                "expected_version": 0,
                "operations": [
                    {
                        "operation_id": "weekly-operation-1",
                        "operation": "add",
                        "plan_date": "2026-08-17",
                        "content": "跟进案件材料提交",
                        "source_evidence": {
                            "source_message_index": 1,
                            "exact_clause_quote": "下周一跟进案件材料提交",
                        },
                    }
                ],
            },
        ),
    )


def _dual_call_completion(*, prefix: str) -> _CompletionResponse:
    daily, weekly = _dual_calls()
    calls = (
        NativeToolCall(
            tool_call_id=f"{prefix}-daily",
            tool_name=daily.tool_name,
            arguments=daily.arguments,
        ),
        NativeToolCall(
            tool_call_id=f"{prefix}-weekly",
            tool_name=weekly.tool_name,
            arguments=weekly.arguments,
        ),
    )
    return _CompletionResponse(
        message={
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call.tool_call_id,
                    "type": "function",
                    "function": {
                        "name": call.tool_name,
                        "arguments": json.dumps(
                            call.arguments,
                            ensure_ascii=False,
                        ),
                    },
                }
                for call in calls
            ],
        },
        metadata={"finish_reason": "tool_calls"},
    )


def _successful_terminal_completion() -> _CompletionResponse:
    return _CompletionResponse(
        message={
            "role": "assistant",
            "content": json.dumps(
                {
                    "reply": "今天的工作已记入日报，下周一的事项已加入下周计划草稿。",
                    "actual_write": True,
                    "operation_outcome": "changed",
                },
                ensure_ascii=False,
            ),
        },
        metadata={"finish_reason": "stop"},
    )


class _TransactionalLedger:
    def __init__(self) -> None:
        self.working = {
            "daily_reports": [],
            "weekly_plans": [],
            "clear_pendings": [],
            "personal_memories": [],
            "personal_memory_audits": [],
            "receipts": [],
        }
        self.committed = deepcopy(self.working)

    def state(self) -> ProductionStateSnapshot:
        payload = {
            key: deepcopy(value)
            for key, value in self.working.items()
            if key != "receipts"
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return ProductionStateSnapshot(
            canonical_json=canonical,
            canonical_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )


class _NestedTransaction:
    def __init__(self, ledger: _TransactionalLedger) -> None:
        self._ledger = ledger
        self._before = deepcopy(ledger.working)
        self.is_active = True

    async def commit(self) -> None:
        self._ledger.committed = deepcopy(self._ledger.working)
        self.is_active = False

    async def rollback(self) -> None:
        self._ledger.working = deepcopy(self._before)
        self.is_active = False


class _FakeSession:
    def __init__(self, ledger: _TransactionalLedger) -> None:
        self.ledger = ledger

    async def begin_nested(self) -> _NestedTransaction:
        return _NestedTransaction(self.ledger)

    async def flush(self) -> None:
        return None


class _DailyExecutor:
    calls: list[str] = []
    fail = False

    def __init__(self, *, session, **_kwargs) -> None:
        self._ledger = session.ledger

    async def add_daily_items(self, request) -> ProductionHandlerOutcome:
        type(self).calls.append(request.tool_call_id)
        if type(self).fail:
            raise ProductionExecutionError("TEST_DAILY_WRITE_FAILED")
        self._ledger.working["daily_reports"].append(
            {
                "report_id": str(REPORT_ID),
                "today_work": ["完成本周案件材料整理"],
                "version": 1,
            }
        )
        return ProductionHandlerOutcome(
            target_type="daily_report",
            target_id=str(REPORT_ID),
            before_report=None,
            after_report=None,
            idempotency_key="daily-key",
            affected_item_ids=("daily-item-1",),
            safe_user_facts={"actual_write": True},
            before_version=0,
            after_version=1,
        )


class _WeeklyExecutor:
    calls: list[str] = []
    fail = False

    def __init__(self, *, session, **_kwargs) -> None:
        self._ledger = session.ledger

    async def apply_next_weekly_plan(self, request) -> ProductionHandlerOutcome:
        type(self).calls.append(request.tool_call_id)
        if type(self).fail:
            raise ProductionExecutionError("TEST_WEEKLY_WRITE_FAILED")
        self._ledger.working["weekly_plans"].append(
            {
                "plan_id": str(PLAN_ID),
                "monday": ["跟进案件材料提交"],
                "version": 1,
            }
        )
        return ProductionHandlerOutcome(
            target_type="weekly_plan",
            target_id=str(PLAN_ID),
            before_report=None,
            after_report=None,
            idempotency_key="weekly-key",
            affected_item_ids=("weekly-item-1",),
            safe_user_facts={"actual_write": True},
            before_version=0,
            after_version=1,
        )


def _runtime(
    monkeypatch,
    *,
    daily_fails: bool = False,
    weekly_fails: bool = False,
):
    import app.agent2.tool_calling.production_runtime as runtime_module

    ledger = _TransactionalLedger()
    session = _FakeSession(ledger)
    context = _context()
    source = CurrentTurnSource((MESSAGE,), occurred_at=(NOW,))
    binder = ShadowCallBinder(
        context,
        UnavailableDateResolver(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
        current_turn_source=source,
    )
    runtime = ProductionRuntimeSession(
        session=session,
        user=SimpleNamespace(id=USER_ID, active=True),
        settings=SimpleNamespace(),
        context=context,
        capability=SimpleNamespace(),
        source_channel="dingtalk_private",
        source_text_hash=source.sha256,
        current_turn_source=source,
        binder=binder,
        date_resolver=UnavailableDateResolver(),
    )
    _DailyExecutor.calls = []
    _DailyExecutor.fail = daily_fails
    _WeeklyExecutor.calls = []
    _WeeklyExecutor.fail = weekly_fails
    monkeypatch.setattr(runtime_module, "ProductionDailyExecutor", _DailyExecutor)
    monkeypatch.setattr(
        runtime_module,
        "ProductionWeeklyPlanExecutor",
        _WeeklyExecutor,
    )

    async def _control_is_open(_self) -> bool:
        return True

    async def _lock_turn(_self) -> None:
        return None

    async def _load_replays(_self, prepared):
        return tuple(None for _ in prepared)

    async def _state(_self):
        return ledger.state()

    async def _persist_receipt(
        _self,
        *,
        item,
        outcome,
        before,
        after,
        changed,
    ) -> ToolReceipt:
        del before, after
        receipt = ToolReceipt(
            status=(ReceiptStatus.SUCCESS if changed else ReceiptStatus.NO_OP),
            tool_name=item.bound.call.tool_name,
            changed=changed,
            target_type=outcome.target_type,
            target_id=outcome.target_id,
            before_version=outcome.before_version,
            after_version=outcome.after_version,
            affected_item_ids=outcome.affected_item_ids,
            safe_user_facts={
                "actual_write": changed,
                "tool_call_id": item.bound.call.tool_call_id,
            },
            execution_mode=ExecutionMode.CANARY_EXECUTE,
        )
        ledger.working["receipts"].append(
            {
                "tool_call_id": item.bound.call.tool_call_id,
                "tool_name": item.bound.call.tool_name,
            }
        )
        return receipt

    runtime._control_is_open = MethodType(_control_is_open, runtime)
    runtime._lock_turn = MethodType(_lock_turn, runtime)
    runtime._load_replays = MethodType(_load_replays, runtime)
    runtime._state = MethodType(_state, runtime)
    runtime._persist_and_verify_receipt = MethodType(_persist_receipt, runtime)
    return runtime, ledger


@pytest.mark.asyncio
async def test_one_private_message_can_stage_daily_and_weekly_writes_without_tool_conflict(
    monkeypatch,
) -> None:
    runtime, ledger = _runtime(monkeypatch)

    staged = await runtime.execute(_dual_calls(), defer_finalization=True)

    assert staged.status == "success"
    assert staged.transaction_pending is True
    assert staged.committed_to_outer_transaction is False
    assert [receipt.tool_name for receipt in staged.receipts] == [
        "add_daily_items",
        "apply_next_weekly_plan",
    ]
    assert [receipt.safe_user_facts["tool_call_id"] for receipt in staged.receipts] == [
        "daily-call-1",
        "weekly-call-1",
    ]
    assert _DailyExecutor.calls == ["daily-call-1"]
    assert _WeeklyExecutor.calls == ["weekly-call-1"]
    assert ledger.committed["daily_reports"] == []
    assert ledger.committed["weekly_plans"] == []
    assert ledger.committed["receipts"] == []

    committed = await runtime.commit_pending()

    assert committed.status == "success"
    assert committed.committed_to_outer_transaction is True
    assert committed.business_write_count == 2
    assert [item["tool_name"] for item in ledger.committed["receipts"]] == [
        "add_daily_items",
        "apply_next_weekly_plan",
    ]
    assert ledger.committed["daily_reports"][0]["version"] == 1
    assert ledger.committed["weekly_plans"][0]["version"] == 1


@pytest.mark.asyncio
async def test_weekly_failure_rolls_back_daily_write_and_all_context_receipts(
    monkeypatch,
) -> None:
    runtime, ledger = _runtime(monkeypatch, weekly_fails=True)

    result = await runtime.execute(_dual_calls(), defer_finalization=True)

    assert result.status == "failed"
    assert result.error_code == "TEST_WEEKLY_WRITE_FAILED"
    assert result.rolled_back is True
    assert _DailyExecutor.calls == ["daily-call-1"]
    assert _WeeklyExecutor.calls == ["weekly-call-1"]
    assert ledger.working["daily_reports"] == []
    assert ledger.working["weekly_plans"] == []
    assert ledger.working["receipts"] == []
    assert ledger.committed == ledger.working


@pytest.mark.asyncio
async def test_daily_failure_after_weekly_write_rolls_back_weekly_and_its_receipt(
    monkeypatch,
) -> None:
    runtime, ledger = _runtime(monkeypatch, daily_fails=True)
    daily, weekly = _dual_calls()

    result = await runtime.execute(
        (weekly, daily),
        defer_finalization=True,
    )

    assert result.status == "failed"
    assert result.error_code == "TEST_DAILY_WRITE_FAILED"
    assert result.rolled_back is True
    assert _WeeklyExecutor.calls == ["weekly-call-1"]
    assert _DailyExecutor.calls == ["daily-call-1"]
    assert ledger.working["daily_reports"] == []
    assert ledger.working["weekly_plans"] == []
    assert ledger.working["receipts"] == []
    assert ledger.committed == ledger.working


@pytest.mark.asyncio
async def test_terminal_reply_failure_discards_both_staged_domains_before_context_can_advance(
    monkeypatch,
) -> None:
    runtime, ledger = _runtime(monkeypatch)

    staged = await runtime.execute(_dual_calls(), defer_finalization=True)

    assert staged.transaction_pending is True
    assert ledger.working["daily_reports"]
    assert ledger.working["weekly_plans"]
    assert ledger.working["receipts"]
    assert ledger.committed["daily_reports"] == []
    assert ledger.committed["weekly_plans"] == []
    assert ledger.committed["receipts"] == []

    # The model's final reply is validated after writes are staged.  Any
    # failure in that phase calls this same public rollback hook.
    await runtime.rollback_pending()

    assert ledger.working["daily_reports"] == []
    assert ledger.working["weekly_plans"] == []
    assert ledger.working["receipts"] == []
    assert ledger.committed == ledger.working


@pytest.mark.asyncio
async def test_agent2_production_turn_commits_both_domains_only_after_valid_terminal_reply(
    monkeypatch,
) -> None:
    runtime, ledger = _runtime(monkeypatch)
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="test-model",
        timeout_seconds=3,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    completions = iter(
        (
            _dual_call_completion(prefix="draft"),
            _dual_call_completion(prefix="reviewed"),
            _successful_terminal_completion(),
        )
    )
    committed_before_terminal: list[dict] = []

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, tool_schemas, thinking_enabled
        completion = next(completions)
        if completion.message.get("content") is not None:
            committed_before_terminal.append(deepcopy(ledger.committed))
        return completion

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text=MESSAGE,
        context=_context(),
        runtime_session=runtime,
    )

    assert committed_before_terminal[0]["daily_reports"] == []
    assert committed_before_terminal[0]["weekly_plans"] == []
    assert committed_before_terminal[0]["receipts"] == []
    assert result.final_content == (
        "今天的工作已记入日报，下周一的事项已加入下周计划草稿。"
    )
    assert result.runtime_results[-1].committed_to_outer_transaction is True
    assert [receipt.tool_name for receipt in result.receipts] == [
        "add_daily_items",
        "apply_next_weekly_plan",
    ]
    assert [audit.tool_call_id for audit in result.raw_tool_call_audit] == [
        "draft-daily",
        "draft-weekly",
        "reviewed-daily",
        "reviewed-weekly",
    ]
    assert _DailyExecutor.calls == ["reviewed-daily"]
    assert _WeeklyExecutor.calls == ["reviewed-weekly"]
    assert ledger.committed["daily_reports"]
    assert ledger.committed["weekly_plans"]
    assert ledger.committed["receipts"]


@pytest.mark.asyncio
async def test_agent2_terminal_model_failure_automatically_rolls_back_both_domains(
    monkeypatch,
) -> None:
    runtime, ledger = _runtime(monkeypatch)
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="test-model",
        timeout_seconds=3,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    call_count = 0

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        nonlocal call_count
        del messages, tool_schemas, thinking_enabled
        call_count += 1
        if call_count == 1:
            return _dual_call_completion(prefix="draft")
        if call_count == 2:
            return _dual_call_completion(prefix="reviewed")
        raise DeepSeekTimeoutError("terminal reply timeout")

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    with pytest.raises(DeepSeekTimeoutError, match="terminal reply timeout"):
        await adapter.run_canary_turn(
            system_prompt="Agent2 test",
            user_text=MESSAGE,
            context=_context(),
            runtime_session=runtime,
        )

    assert ledger.working["daily_reports"] == []
    assert ledger.working["weekly_plans"] == []
    assert ledger.working["receipts"] == []
    assert ledger.committed == ledger.working
