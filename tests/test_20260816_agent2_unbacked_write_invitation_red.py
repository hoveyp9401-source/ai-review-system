from __future__ import annotations

import json
import hashlib
from datetime import date, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

import pytest

from app.agent2.tool_calling.context import (
    CANARY_STATE_NAMESPACE,
    TrustedClearPending,
    TrustedContext,
    TrustedPrincipal,
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
    _CompletionResponse,
    _assert_no_trusted_daily_identifier_leak,
)
from app.agent2.tool_calling.production_contracts import ProductionRuntimeResult
from app.agent2.tool_calling.production_runtime import _safe_report_snapshot
from app.agent2.tool_calling.receipt_provenance import principal_scope_sha256
from app.agent2.weekly_plan_context import (
    TrustedWeeklyPlanContext,
    TrustedWeeklyPlanDay,
)


_USER_ID = UUID("10000000-0000-4000-8000-000000000004")


def _query_server_evidence() -> dict[str, str]:
    return {
        "principal_scope_sha256": principal_scope_sha256(
            tenant_id="tenant-a",
            user_id=_USER_ID,
            conversation_id="direct-user-a",
            source_message_id="message-current",
        )
    }


def _completed_report_state_hash(snapshot: dict) -> str:
    canonical = {
        "report_id": snapshot["report_id"],
        "report_date": snapshot["report_date"],
        "version": snapshot["version"],
        "status": snapshot["status"],
        "fields": {
            field_name: [
                {**item, "provenance": "trusted_context"}
                for item in items
            ]
            for field_name, items in snapshot["fields"].items()
        },
        "acknowledged_empty_fields": snapshot["acknowledged_empty_fields"],
        "provenance": "trusted_context",
    }
    return hashlib.sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


class _NoWriteRuntime:
    mode = ExecutionMode.CANARY_EXECUTE

    async def execute(self, *_args, **_kwargs):
        raise AssertionError("a zero-tool conversational answer must not write")


class _ReadOnlyRuntime:
    mode = ExecutionMode.CANARY_EXECUTE

    def __init__(self) -> None:
        self.execute_count = 0

    async def execute(self, calls, **_kwargs):
        self.execute_count += 1
        assert len(calls) == 1
        assert calls[0].tool_name == "query_today_report"
        receipt = ToolReceipt(
            status=ReceiptStatus.SUCCESS,
            tool_name="query_today_report",
            changed=False,
            target_type="daily_report",
            target_id="report-current",
            before_version=1,
            after_version=1,
            affected_item_ids=(),
            safe_user_facts={"report_snapshot": None},
            execution_mode=ExecutionMode.CANARY_EXECUTE,
        )
        return ProductionRuntimeResult(
            status="success",
            receipts=(receipt,),
            handler_call_count=1,
        )


class _CompletedReportEditRuntime:
    mode = ExecutionMode.CANARY_EXECUTE

    def __init__(
        self,
        *,
        snapshot_report_id: UUID | None = None,
        snapshot_version: int = 4,
        snapshot_hash: str | None = None,
        snapshot_fields: dict | None = None,
        query_safe_fact_overrides: dict | None = None,
        query_affected_item_ids: tuple[str, ...] = (),
        query_target_id: str | None = None,
        query_execution_mode: ExecutionMode = ExecutionMode.CANARY_EXECUTE,
        query_receipt_overrides: dict | None = None,
        query_server_evidence: dict | None = None,
    ) -> None:
        self.report_id = UUID("20000000-0000-4000-8000-000000000004")
        self.snapshot_report_id = snapshot_report_id or self.report_id
        self.snapshot = {
            "report_id": str(self.snapshot_report_id),
            "report_date": "2026-08-11",
            "version": snapshot_version,
            "status": "completed",
            "fields": snapshot_fields
            or {
                "today_work": [
                    {
                        "item_id": "tw-1",
                        "content": "完成合同初稿复核",
                    },
                    {
                        "item_id": "tw-2",
                        "content": "整理付款材料",
                    },
                ],
                "problems": [],
                "tomorrow_plan": [],
            },
            "acknowledged_empty_fields": [],
        }
        self.snapshot["report_state_sha256"] = (
            snapshot_hash or _completed_report_state_hash(self.snapshot)
        )
        self.query_safe_fact_overrides = query_safe_fact_overrides or {}
        self.query_affected_item_ids = query_affected_item_ids
        self.query_target_id = query_target_id or str(self.report_id)
        self.query_execution_mode = query_execution_mode
        self.query_receipt_overrides = query_receipt_overrides or {}
        self.query_server_evidence = (
            (
                _query_server_evidence()
                if query_execution_mode == ExecutionMode.CANARY_EXECUTE
                else {}
            )
            if query_server_evidence is None
            else query_server_evidence
        )
        self.executed_tools: list[str] = []
        self.commit_count = 0
        self.rollback_count = 0
        self._write_receipt: ToolReceipt | None = None

    async def execute(self, calls, **kwargs):
        assert len(calls) == 1
        call = calls[0]
        self.executed_tools.append(call.tool_name)
        if call.tool_name == "query_report_by_date":
            assert kwargs.get("defer_finalization") is None
            return ProductionRuntimeResult(
                status="success",
                receipts=(
                    ToolReceipt(
                        status=ReceiptStatus.SUCCESS,
                        tool_name="query_report_by_date",
                        changed=False,
                        target_type="daily_report",
                        target_id=self.query_target_id,
                        before_version=4,
                        after_version=4,
                        affected_item_ids=self.query_affected_item_ids,
                        safe_user_facts={
                            "actual_write": False,
                            "report_found": True,
                            "report_snapshot": self.snapshot,
                            "report_date": self.snapshot["report_date"],
                            **self.query_safe_fact_overrides,
                        },
                        server_evidence=self.query_server_evidence,
                        execution_mode=self.query_execution_mode,
                        **self.query_receipt_overrides,
                    ),
                ),
                handler_call_count=1,
            )
        assert call.tool_name == "edit_daily_items"
        assert kwargs.get("defer_finalization") is True
        assert call.arguments == {
            "report_id": str(self.report_id),
            "expected_version": 4,
            "target_item_ids": ["tw-1"],
            "replacement": "完成合同终稿复核",
            "replacement_evidence": {
                "source_message_index": 1,
                "exact_quote": "完成合同终稿复核",
            },
            "replacement_reviewed": True,
        }
        self._write_receipt = ToolReceipt(
            status=ReceiptStatus.SUCCESS,
            tool_name="edit_daily_items",
            changed=True,
            target_type="daily_report",
            target_id=str(self.report_id),
            before_version=4,
            after_version=5,
            affected_item_ids=("tw-1",),
            safe_user_facts={"actual_write": True},
            execution_mode=ExecutionMode.CANARY_EXECUTE,
        )
        return ProductionRuntimeResult(
            status="success",
            receipts=(self._write_receipt,),
            transaction_opened=True,
            transaction_pending=True,
            handler_call_count=1,
        )

    async def commit_pending(self):
        assert self._write_receipt is not None
        self.commit_count += 1
        return ProductionRuntimeResult(
            status="success",
            receipts=(self._write_receipt,),
            transaction_opened=True,
            committed_to_outer_transaction=True,
            handler_call_count=1,
            business_write_count=1,
            receipt_write_count=1,
        )

    async def rollback_pending(self):
        self.rollback_count += 1


class _TwoCompletedReportQueryRuntime(_CompletedReportEditRuntime):
    async def execute(self, calls, **kwargs):
        if len(calls) != 2:
            return await super().execute(calls, **kwargs)
        parent_execute = super().execute
        results = [await parent_execute((call,), **kwargs) for call in calls]
        return ProductionRuntimeResult(
            status="success",
            receipts=tuple(
                receipt for result in results for receipt in result.receipts
            ),
            handler_call_count=2,
        )


class _OneForgedOfTwoCompletedReportQueryRuntime(_CompletedReportEditRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.second_report_id = UUID("20000000-0000-4000-8000-000000000005")
        self.second_snapshot = {
            "report_id": str(self.second_report_id),
            "report_date": "2026-08-10",
            "version": 2,
            "status": "completed",
            "fields": {
                "today_work": [
                    {
                        "item_id": "tw-other-1",
                        "content": "Completed a valid earlier task",
                    }
                ],
                "problems": [],
                "tomorrow_plan": [],
            },
            "acknowledged_empty_fields": [],
        }
        self.second_snapshot["report_state_sha256"] = (
            _completed_report_state_hash(self.second_snapshot)
        )
        self.second_snapshot["fields"]["today_work"][0]["content"] = "FORGED"

    async def execute(self, calls, **kwargs):
        if len(calls) != 2:
            return await super().execute(calls, **kwargs)
        assert kwargs.get("defer_finalization") is None
        assert all(call.tool_name == "query_report_by_date" for call in calls)
        self.executed_tools.extend(call.tool_name for call in calls)
        receipts = tuple(
            ToolReceipt(
                status=ReceiptStatus.SUCCESS,
                tool_name="query_report_by_date",
                changed=False,
                target_type="daily_report",
                target_id=snapshot["report_id"],
                before_version=snapshot["version"],
                after_version=snapshot["version"],
                affected_item_ids=(),
                safe_user_facts={
                    "actual_write": False,
                    "report_found": True,
                    "report_snapshot": snapshot,
                    "report_date": snapshot["report_date"],
                },
                server_evidence=_query_server_evidence(),
                execution_mode=ExecutionMode.CANARY_EXECUTE,
            )
            for snapshot in (self.snapshot, self.second_snapshot)
        )
        return ProductionRuntimeResult(
            status="success",
            receipts=receipts,
            handler_call_count=2,
        )


class _NoOpReportQueryRuntime:
    mode = ExecutionMode.CANARY_EXECUTE

    def __init__(self, *, receipt_overrides: dict | None = None) -> None:
        self.executed_tools: list[str] = []
        self.receipt_overrides = receipt_overrides or {}

    async def execute(self, calls, **kwargs):
        assert kwargs.get("defer_finalization") is None
        assert len(calls) == 1
        assert calls[0].tool_name == "query_report_by_date"
        self.executed_tools.append(calls[0].tool_name)
        receipt_values = {
            "status": ReceiptStatus.NO_OP,
            "tool_name": "query_report_by_date",
            "changed": False,
            "target_type": "daily_report",
            "target_id": str(
                uuid5(
                    NAMESPACE_URL,
                    f"agent2-daily-report:{_USER_ID}:2026-08-11",
                )
            ),
            "before_version": None,
            "after_version": None,
            "affected_item_ids": (),
            "safe_user_facts": {
                "actual_write": False,
                "report_found": False,
                "report_snapshot": None,
                "report_date": "2026-08-11",
            },
            "server_evidence": _query_server_evidence(),
            "execution_mode": ExecutionMode.CANARY_EXECUTE,
            **self.receipt_overrides,
        }
        return ProductionRuntimeResult(
            status="success",
            receipts=(
                ToolReceipt(**receipt_values),
            ),
            handler_call_count=1,
        )


class _CompletedReportContentWriteRuntime(_CompletedReportEditRuntime):
    def __init__(self, *, write_tool_name: str, write_arguments: dict) -> None:
        super().__init__()
        self.write_tool_name = write_tool_name
        self.write_arguments = write_arguments

    async def execute(self, calls, **kwargs):
        assert len(calls) == 1
        call = calls[0]
        if call.tool_name == "query_report_by_date":
            return await super().execute(calls, **kwargs)
        self.executed_tools.append(call.tool_name)
        assert call.tool_name == self.write_tool_name
        expected_arguments = (
            {**self.write_arguments, "replacement_reviewed": True}
            if call.tool_name == "edit_daily_items"
            else self.write_arguments
        )
        assert call.arguments == expected_arguments
        assert kwargs.get("defer_finalization") is True
        self._write_receipt = ToolReceipt(
            status=ReceiptStatus.SUCCESS,
            tool_name=call.tool_name,
            changed=True,
            target_type="daily_report",
            target_id=str(self.report_id),
            before_version=4,
            after_version=5,
            affected_item_ids=tuple(
                call.arguments.get("target_item_ids", ("generated-item",))
            ),
            safe_user_facts={"actual_write": True},
            execution_mode=ExecutionMode.CANARY_EXECUTE,
        )
        return ProductionRuntimeResult(
            status="success",
            receipts=(self._write_receipt,),
            transaction_opened=True,
            transaction_pending=True,
            handler_call_count=1,
        )


class _CompletedReportMemoryWriteRuntime(_CompletedReportEditRuntime):
    async def execute(self, calls, **kwargs):
        assert len(calls) == 1
        if calls[0].tool_name == "query_report_by_date":
            return await super().execute(calls, **kwargs)
        assert calls[0].tool_name == "remember_personal_memory"
        assert kwargs.get("defer_finalization") is True
        self.executed_tools.append(calls[0].tool_name)
        self._write_receipt = _reminder_write_receipt()
        return ProductionRuntimeResult(
            status="success",
            receipts=(self._write_receipt,),
            transaction_opened=True,
            transaction_pending=True,
            handler_call_count=1,
        )


def _managed_daily_query_receipt() -> ToolReceipt:
    return ToolReceipt(
        status=ReceiptStatus.SUCCESS,
        tool_name="query_managed_daily_reports",
        changed=False,
        target_type="managed_daily_report",
        target_id="managed-query-1",
        safe_user_facts={
            "actual_write": False,
            "managed_daily_query": {
                "query_kind": "missing_submissions",
                "report_date": "2026-08-07",
                "scope_name": "法务合约中心",
                "completed_members": [
                    {"name": "丁益明", "team_name": "法务二部"}
                ],
                "partial_members": [
                    {"name": "朱佳佳", "team_name": "中心直属"}
                ],
                "not_filled_members": [
                    {"name": "赵卫中", "team_name": "中心直属"}
                ],
            },
        },
        execution_mode=ExecutionMode.CANARY_EXECUTE,
    )


class _CompletedReportAndManagedQueryRuntime(_CompletedReportEditRuntime):
    async def execute(self, calls, **kwargs):
        if not any(
            call.tool_name == "query_managed_daily_reports" for call in calls
        ):
            return await super().execute(calls, **kwargs)
        assert kwargs == {}
        assert [call.tool_name for call in calls] == [
            "query_report_by_date",
            "query_managed_daily_reports",
        ]
        report_result = await super().execute((calls[0],))
        self.executed_tools.append("query_managed_daily_reports")
        return ProductionRuntimeResult(
            status="success",
            receipts=(
                report_result.receipts[0],
                _managed_daily_query_receipt(),
            ),
            handler_call_count=2,
        )


def _reminder_write_receipt() -> ToolReceipt:
    return ToolReceipt(
        status=ReceiptStatus.SUCCESS,
        tool_name="remember_personal_memory",
        changed=True,
        target_type="personal_memory",
        target_id="report.daily_reminders_enabled",
        before_version=0,
        after_version=1,
        affected_item_ids=(),
        safe_user_facts={
            "memory_key": "report.daily_reminders_enabled",
            "memory": {
                "memory_type": "response_preference",
                "memory_key": "report.daily_reminders_enabled",
                "value": {"enabled": False},
                "provenance": "server_personal_memory",
            },
            "forgotten": False,
        },
        execution_mode=ExecutionMode.CANARY_EXECUTE,
    )


class _ReminderWriteRuntime:
    mode = ExecutionMode.CANARY_EXECUTE

    def __init__(self) -> None:
        self.receipt = _reminder_write_receipt()
        self.execute_count = 0
        self.commit_count = 0

    async def execute(self, calls, *, defer_finalization):
        self.execute_count += 1
        assert defer_finalization is True
        assert len(calls) == 1
        assert calls[0].tool_name == "remember_personal_memory"
        return ProductionRuntimeResult(
            status="success",
            receipts=(self.receipt,),
            transaction_opened=True,
            transaction_pending=True,
            handler_call_count=1,
        )

    async def commit_pending(self):
        self.commit_count += 1
        return ProductionRuntimeResult(
            status="success",
            receipts=(self.receipt,),
            transaction_opened=True,
            committed_to_outer_transaction=True,
            handler_call_count=1,
            business_write_count=1,
            receipt_write_count=1,
        )

    async def rollback_pending(self):
        raise AssertionError("a valid memory write must not roll back")


def _daily_write_context(
    *,
    active_clear_pending: TrustedClearPending | None = None,
    today_report: TrustedReportSnapshot | None = None,
    allowed_tool_names: frozenset[str] = frozenset({"add_daily_items"}),
) -> TrustedContext:
    return TrustedContext(
        namespace=CANARY_STATE_NAMESPACE,
        now=datetime(2026, 8, 12, 16, 42, tzinfo=ZoneInfo("Asia/Shanghai")),
        principal=TrustedPrincipal(
            tenant_id="tenant-a",
            user_id=_USER_ID,
            conversation_id="direct-user-a",
            source_message_id="message-current",
            timezone="Asia/Shanghai",
            display_name="测试用户甲",
            conversation_kind="direct",
        ),
        active_clear_pending=active_clear_pending,
        today_report=today_report,
        allowed_tool_names=allowed_tool_names,
        gate_decisions={name: True for name in allowed_tool_names},
    )


def _review_envelope(
    candidate_reply: str,
    *,
    decision: str,
    classification: str,
    pending_reference: str | None = None,
    replacement_reply: str | None = None,
) -> str:
    return json.dumps(
        {
            "decision": decision,
            "classification": classification,
            "reviewed_reply_sha256": hashlib.sha256(
                candidate_reply.encode("utf-8")
            ).hexdigest(),
            "pending_reference": pending_reference,
            "replacement_reply": replacement_reply,
        },
        ensure_ascii=False,
    )


def _follow_through_review_envelope(
    candidate_reply: str,
    *,
    decision: str,
) -> str:
    return json.dumps(
        {
            "decision": decision,
            "reviewed_reply_sha256": hashlib.sha256(
                candidate_reply.encode("utf-8")
            ).hexdigest(),
        },
        ensure_ascii=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_text", "unsafe_reply", "unsafe_continuation", "safe_reply"),
    (
        (
            "为什么日报提交后就不能修改了？",
            (
                "要不要把“优化日报提交后无法修改的限制”加入明日计划？"
                "你回复“要”我就写入。"
            ),
            "回复“要”",
            (
                "你这句是在问现有限制，并没有要求写日报；"
                "我没有保存草稿，也没有建立待确认写入。"
            ),
        ),
        (
            "这段内容你先帮我看看怎么表达更清楚。",
            "我已经整理成草稿，回复“确认”即可写入日报。",
            "回复“确认”",
            "我可以先帮你润色这段表达；目前没有保存日报草稿。",
        ),
    ),
)
async def test_public_agent2_turn_does_not_offer_an_unbacked_next_turn_write(
    monkeypatch: pytest.MonkeyPatch,
    user_text: str,
    unsafe_reply: str,
    unsafe_continuation: str,
    safe_reply: str,
) -> None:
    """A plain assistant reply cannot create write authority for the next turn."""

    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )

    completion_count = 0
    completion_requests: list[tuple[list[dict], list[dict]]] = []

    async def scripted_completion(
        messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        nonlocal completion_count
        del thinking_enabled
        completion_count += 1
        completion_requests.append((messages, tool_schemas))
        if completion_count == 1:
            content = unsafe_reply
        elif completion_count == 2:
            content = _review_envelope(
                unsafe_reply,
                decision="replace",
                classification="unbacked_future_write_invitation",
                replacement_reply=safe_reply,
            )
        else:
            content = _review_envelope(
                safe_reply,
                decision="keep",
                classification="ordinary_reply",
            )
        return _CompletionResponse(
            message={"role": "assistant", "content": content},
            metadata={"finish_reason": "stop"},
        )

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 public turn regression",
        user_text=user_text,
        context=_daily_write_context(),
        runtime_session=_NoWriteRuntime(),
    )

    assert result.receipts == ()
    assert result.runtime_results == ()
    assert completion_count == 3
    review_messages, review_tool_schemas = completion_requests[1]
    assert review_tool_schemas == []
    review_facts = json.loads(review_messages[1]["content"])
    assert set(review_facts) == {
        "ordered_current_user_messages",
        "proposed_assistant_reply",
        "reviewed_reply_sha256",
        "allowed_write_domains",
        "persisted_pending",
    }
    assert review_facts["persisted_pending"] == []
    assert "recent_messages" not in review_facts
    assert "trusted_context" not in review_facts
    assert result.final_content == safe_reply
    assert unsafe_continuation not in result.final_content


@pytest.mark.asyncio
async def test_public_agent2_turn_replaces_an_unbacked_reminder_setting_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A zero-write reply cannot claim that future reminder behavior changed."""

    unsafe_reply = (
        "我不会再主动提醒你了。不过我还没有改设置，"
        "你说的是日报提醒还是其他提醒？"
    )
    safe_reply = "我还没有改任何提醒设置。你说的是日报提醒还是其他提醒？"
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    completions = iter(
        (
            unsafe_reply,
            _review_envelope(
                unsafe_reply,
                decision="replace",
                classification="unbacked_state_change_claim",
                replacement_reply=safe_reply,
            ),
            _review_envelope(
                safe_reply,
                decision="keep",
                classification="ordinary_reply",
            ),
        )
    )
    completion_count = 0
    completion_requests: list[list[dict]] = []

    async def scripted_completion(
        messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        nonlocal completion_count
        del thinking_enabled
        completion_count += 1
        completion_requests.append(messages)
        if completion_count >= 2:
            assert tool_schemas == []
        return _CompletionResponse(
            message={"role": "assistant", "content": next(completions)},
            metadata={"finish_reason": "stop"},
        )

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 public turn regression",
        user_text="不要再提醒我。",
        context=_daily_write_context(
            allowed_tool_names=frozenset({"remember_personal_memory"})
        ),
        runtime_session=_NoWriteRuntime(),
    )

    assert completion_count == 3
    assert result.receipts == ()
    assert result.runtime_results == ()
    assert result.final_content == safe_reply
    review_instruction = completion_requests[1][0]["content"]
    assert "no successful business-write receipt" in review_instruction
    assert "preference, setting, durable memory" in review_instruction
    assert "future automatic reminder behavior" in review_instruction
    assert "later admission that the setting was not changed" in review_instruction


@pytest.mark.asyncio
async def test_successful_reminder_memory_write_uses_receipt_reply_without_extra_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real preference-write receipt remains the authority for its acknowledgement."""

    reply = "已关闭你自己的日报提醒。"
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    completions = iter(
        (
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "remember-reminder-1",
                            "type": "function",
                            "function": {
                                "name": "remember_personal_memory",
                                "arguments": json.dumps(
                                    {
                                        "memory_key": (
                                            "report.daily_reminders_enabled"
                                        ),
                                        "value": {"enabled": False},
                                        "source_evidence": {
                                            "source_message_index": 1,
                                            "intent": "explicit_preference",
                                        },
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
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
            ),
        )
    )
    completion_count = 0

    async def scripted_completion(
        _messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        nonlocal completion_count
        del tool_schemas, thinking_enabled
        completion_count += 1
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", scripted_completion)
    runtime = _ReminderWriteRuntime()

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 public turn regression",
        user_text="请关闭我自己的日报提醒。",
        context=_daily_write_context(
            allowed_tool_names=frozenset({"remember_personal_memory"})
        ),
        runtime_session=runtime,
    )

    assert completion_count == 2
    assert runtime.execute_count == 1
    assert runtime.commit_count == 1
    assert len(result.receipts) == 1
    assert result.receipts[0].changed is True
    assert result.final_content == reply
    assert all(
        not turn.response_metadata.get("zero_tool_write_invitation_review")
        for turn in result.model_turns
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    (
        "这个问题的原因是当前流程限制；这轮没有改动你的日报。",
        (
            "如果你明确说明要调整哪一类提醒，我可以按你的完整请求处理；"
            "这轮没有更改任何设置。"
        ),
    ),
)
async def test_public_agent2_turn_keeps_an_ordinary_zero_tool_answer(
    monkeypatch: pytest.MonkeyPatch,
    answer: str,
) -> None:
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    completions = iter(
        (
            answer,
            _review_envelope(
                answer,
                decision="keep",
                classification="ordinary_reply",
            ),
        )
    )
    completion_count = 0

    async def scripted_completion(
        _messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        nonlocal completion_count
        del tool_schemas, thinking_enabled
        completion_count += 1
        return _CompletionResponse(
            message={"role": "assistant", "content": next(completions)},
            metadata={"finish_reason": "stop"},
        )

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 public turn regression",
        user_text="为什么日报提交后不能修改？",
        context=_daily_write_context(),
        runtime_session=_NoWriteRuntime(),
    )

    assert completion_count == 2
    assert result.final_content == answer
    assert result.receipts == ()
    assert result.runtime_results == ()


@pytest.mark.asyncio
async def test_read_then_terminal_reply_still_blocks_an_unbacked_write_invitation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unsafe_reply = "查询结果如上；你下轮回复确认，我就把它写入日报。"
    safe_reply = "查询结果如上；这轮没有写入日报。"
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    query_call = {
        "id": "query-1",
        "type": "function",
        "function": {"name": "query_today_report", "arguments": "{}"},
    }
    completions = iter(
        (
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [query_call],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={"role": "assistant", "content": unsafe_reply},
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": _review_envelope(
                        unsafe_reply,
                        decision="replace",
                        classification="unbacked_future_write_invitation",
                        replacement_reply=safe_reply,
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": _review_envelope(
                        safe_reply,
                        decision="keep",
                        classification="ordinary_reply",
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
        )
    )
    completion_count = 0

    async def scripted_completion(
        _messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        nonlocal completion_count
        del thinking_enabled
        completion_count += 1
        if completion_count >= 3:
            assert tool_schemas == []
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", scripted_completion)
    runtime = _ReadOnlyRuntime()

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 public turn regression",
        user_text="先查一下我今天的日报。",
        context=_daily_write_context(
            allowed_tool_names=frozenset(
                {"query_today_report", "add_daily_items"}
            )
        ),
        runtime_session=runtime,
    )

    assert result.final_content == safe_reply
    assert completion_count == 4
    assert runtime.execute_count == 1
    assert len(result.receipts) == 1
    assert result.receipts[0].changed is False


@pytest.mark.asyncio
async def test_completed_report_edit_cannot_end_after_only_loading_the_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit owned-report edit must not be silently dropped after its read."""

    runtime = _CompletedReportEditRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=4,
        endpoint="https://example.invalid/chat/completions",
    )
    query_call = {
        "id": "load-completed-report",
        "type": "function",
        "function": {
            "name": "query_report_by_date",
            "arguments": json.dumps(
                {
                    "date_expression": "昨天",
                    "proposed_date": "2026-08-11",
                },
                ensure_ascii=False,
            ),
        },
    }
    edit_call = {
        "id": "edit-completed-item",
        "type": "function",
        "function": {
            "name": "edit_daily_items",
            "arguments": json.dumps(
                {
                    "report_id": str(runtime.report_id),
                    "expected_version": 4,
                    "target_item_ids": ["tw-1"],
                    "replacement": "完成合同终稿复核",
                    "replacement_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "完成合同终稿复核",
                    },
                },
                ensure_ascii=False,
            ),
        },
    }
    loaded_only_reply = "我已经查到昨天的日报内容。"
    final_reply = "已修改昨天日报今日工作的第一条，日报仍保持已提交状态。"
    completions = iter(
        (
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [query_call],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={"role": "assistant", "content": loaded_only_reply},
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": _follow_through_review_envelope(
                        loaded_only_reply,
                        decision="continue_once",
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [edit_call],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            **edit_call,
                            "id": "reviewed-edit-completed-item",
                        }
                    ],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "reply": final_reply,
                            "actual_write": True,
                            "operation_outcome": "changed",
                        },
                        ensure_ascii=False,
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": _follow_through_review_envelope(
                        final_reply,
                        decision="keep_no_write",
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
        )
    )

    completion_tool_names: list[list[str]] = []
    completion_messages: list[list[dict]] = []

    async def scripted_completion(
        messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        del thinking_enabled
        completion_messages.append(messages)
        completion_tool_names.append(
            [schema["function"]["name"] for schema in tool_schemas]
        )
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 completed-report edit regression",
        user_text="把昨天日报今日工作的第一条改成：完成合同终稿复核。",
        context=_daily_write_context(
            allowed_tool_names=frozenset(
                {
                    "query_report_by_date",
                    "edit_daily_items",
                    "remember_personal_memory",
                }
            )
        ),
        runtime_session=runtime,
    )

    assert runtime.executed_tools == [
        "query_report_by_date",
        "edit_daily_items",
    ]
    assert runtime.commit_count == 1
    assert [receipt.changed for receipt in result.receipts] == [False, True]
    assert result.final_content == final_reply
    assert completion_tool_names[2] == []
    assert completion_tool_names[3] == ["edit_daily_items"]
    follow_through_facts = json.loads(completion_messages[2][1]["content"])
    assert follow_through_facts["allowed_daily_content_write_tools"] == [
        "edit_daily_items"
    ]
    query_summary = follow_through_facts["successful_query_results"]
    assert query_summary[0]["fields"]["today_work"] == [
        {"position": 1, "content": "完成合同初稿复核"},
        {"position": 2, "content": "整理付款材料"},
    ]
    serialized_summary = json.dumps(query_summary, ensure_ascii=False)
    assert "report_id" not in serialized_summary
    assert "item_id" not in serialized_summary
    assert "version" not in serialized_summary
    assert completion_tool_names[6] == []
    pending_review_prompt = completion_messages[6][0]["content"]
    assert "executed write summaries" in pending_review_prompt
    pending_review_facts = json.loads(completion_messages[6][1]["content"])
    assert pending_review_facts["pending_write_review"] is True
    assert pending_review_facts["pending_write_results"] == [
        {
            "tool_name": "edit_daily_items",
            "changed": True,
            "report_date": None,
            "target_matches_query": True,
        }
    ]
    serialized_pending_results = json.dumps(
        pending_review_facts["pending_write_results"],
        ensure_ascii=False,
    )
    assert "report_id" not in serialized_pending_results
    assert "item_id" not in serialized_pending_results
    assert "version" not in serialized_pending_results


@pytest.mark.asyncio
async def test_managed_reply_retry_does_not_disable_one_daily_follow_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _CompletedReportAndManagedQueryRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=4,
        endpoint="https://example.invalid/chat/completions",
    )
    edit_arguments = {
        "report_id": str(runtime.report_id),
        "expected_version": 4,
        "target_item_ids": ["tw-1"],
        "replacement": "完成合同终稿复核",
        "replacement_evidence": {
            "source_message_index": 1,
            "exact_quote": "完成合同终稿复核",
        },
    }
    edit_call = {
        "id": "edit-after-managed-reply-retry",
        "type": "function",
        "function": {
            "name": "edit_daily_items",
            "arguments": json.dumps(edit_arguments, ensure_ascii=False),
        },
    }
    managed_reply = (
        "2026年8月7日，法务合约中心日报填写情况：\n"
        "已完成 1人：丁益明\n"
        "部分填写 1人：朱佳佳\n"
        "未填写 1人：赵卫中"
    )
    final_reply = managed_reply + "\n已按要求修改昨天日报的第一条。"
    completions = iter(
        (
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "load-before-managed-retry",
                            "type": "function",
                            "function": {
                                "name": "query_report_by_date",
                                "arguments": json.dumps(
                                    {
                                        "date_expression": "昨天",
                                        "proposed_date": "2026-08-11",
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        },
                        {
                            "id": "load-managed-before-retry",
                            "type": "function",
                            "function": {
                                "name": "query_managed_daily_reports",
                                "arguments": json.dumps(
                                    {
                                        "view": "missing_submissions",
                                        "report_date_expression": "2026年8月7日",
                                        "proposed_report_date": "2026-08-07",
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        },
                    ],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={"role": "assistant", "content": "已经查到了。"},
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={"role": "assistant", "content": managed_reply},
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": _follow_through_review_envelope(
                        managed_reply,
                        decision="continue_once",
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [edit_call],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{**edit_call, "id": "reviewed-managed-retry-edit"}],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "reply": final_reply,
                            "actual_write": True,
                            "operation_outcome": "changed",
                        },
                        ensure_ascii=False,
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": _follow_through_review_envelope(
                        final_reply,
                        decision="keep_no_write",
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
        )
    )
    completion_tool_names: list[list[str]] = []

    async def scripted_completion(
        _messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        del thinking_enabled
        completion_tool_names.append(
            [schema["function"]["name"] for schema in tool_schemas]
        )
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 managed-reply retry followed by completed edit",
        user_text=(
            "把昨天日报第一条改成：完成合同终稿复核；"
            "并查询2026年8月7日中心日报填写情况。"
        ),
        context=_daily_write_context(
            allowed_tool_names=frozenset(
                {
                    "query_report_by_date",
                    "query_managed_daily_reports",
                    "edit_daily_items",
                }
            )
        ),
        runtime_session=runtime,
    )

    assert runtime.executed_tools == [
        "query_report_by_date",
        "query_managed_daily_reports",
        "edit_daily_items",
    ]
    assert runtime.commit_count == 1
    assert result.final_content == final_reply
    assert completion_tool_names[2] == []
    assert completion_tool_names[3] == []
    assert completion_tool_names[4] == ["edit_daily_items"]
    assert completion_tool_names[5] == ["edit_daily_items"]
    assert completion_tool_names[7] == []


@pytest.mark.parametrize(
    ("write_tool_name", "write_details", "user_text"),
    (
        (
            "add_daily_items",
            {
                "date_selection": "trusted_report",
                "date_expression": None,
                "proposed_date": None,
                "retry_candidate_id": None,
                "date_evidence": None,
                "items": [
                    {
                        "field": "today_work",
                        "content": "新增合同归档事项",
                        "source_evidence": {
                            "source_message_index": 1,
                            "exact_quote": "新增合同归档事项",
                        },
                    }
                ],
                "acknowledged_empty_fields": [],
                "empty_field_evidence": [],
                "submit_after_write": False,
            },
            "在昨天日报今日工作中新增：新增合同归档事项。",
        ),
        (
            "delete_daily_items",
            {"target_item_ids": ["tw-2"]},
            "删除昨天日报今日工作的第二条。",
        ),
        (
            "move_daily_items",
            {
                "target_item_ids": ["tw-1"],
                "source_field": "today_work",
                "target_field": "tomorrow_plan",
            },
            "把昨天日报今日工作的第一条移到明日计划。",
        ),
    ),
)
@pytest.mark.asyncio
async def test_completed_report_content_write_follow_through_uses_existing_review(
    monkeypatch: pytest.MonkeyPatch,
    write_tool_name: str,
    write_details: dict,
    user_text: str,
) -> None:
    report_id = UUID("20000000-0000-4000-8000-000000000004")
    write_arguments = {
        "report_id": str(report_id),
        "expected_version": 4,
        **write_details,
    }
    runtime = _CompletedReportContentWriteRuntime(
        write_tool_name=write_tool_name,
        write_arguments=write_arguments,
    )
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=4,
        endpoint="https://example.invalid/chat/completions",
    )
    query_call = {
        "id": f"load-before-{write_tool_name}",
        "type": "function",
        "function": {
            "name": "query_report_by_date",
            "arguments": json.dumps(
                {
                    "date_expression": "昨天",
                    "proposed_date": "2026-08-11",
                },
                ensure_ascii=False,
            ),
        },
    }
    write_call = {
        "id": f"draft-{write_tool_name}",
        "type": "function",
        "function": {
            "name": write_tool_name,
            "arguments": json.dumps(write_arguments, ensure_ascii=False),
        },
    }
    loaded_only_reply = "我已经查到昨天的日报内容。"
    final_reply = "已按你的要求修改昨天日报，日报仍保持已提交状态。"
    completions = iter(
        (
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [query_call],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={"role": "assistant", "content": loaded_only_reply},
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": _follow_through_review_envelope(
                        loaded_only_reply,
                        decision="continue_once",
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [write_call],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {**write_call, "id": f"reviewed-{write_tool_name}"}
                    ],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "reply": final_reply,
                            "actual_write": True,
                            "operation_outcome": "changed",
                        },
                        ensure_ascii=False,
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": _follow_through_review_envelope(
                        final_reply,
                        decision="keep_no_write",
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
        )
    )
    completion_tool_names: list[list[str]] = []

    async def scripted_completion(
        _messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        del thinking_enabled
        completion_tool_names.append(
            [schema["function"]["name"] for schema in tool_schemas]
        )
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 completed-report content-write review regression",
        user_text=user_text,
        context=_daily_write_context(
            allowed_tool_names=frozenset(
                {"query_report_by_date", write_tool_name, "remember_personal_memory"}
            )
        ),
        runtime_session=runtime,
    )

    assert runtime.executed_tools == ["query_report_by_date", write_tool_name]
    assert runtime.commit_count == 1
    assert result.final_content == final_reply
    assert [receipt.changed for receipt in result.receipts] == [False, True]
    assert completion_tool_names[2] == []
    assert completion_tool_names[3] == [write_tool_name]
    assert completion_tool_names[4] == [write_tool_name]
    assert completion_tool_names[6] == []


@pytest.mark.asyncio
async def test_direct_completed_edit_review_receives_trusted_ordinal_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _CompletedReportEditRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=4,
        endpoint="https://example.invalid/chat/completions",
    )
    edit_arguments = {
        "report_id": str(runtime.report_id),
        "expected_version": 4,
        "target_item_ids": ["tw-1"],
        "replacement": "完成合同终稿复核",
        "replacement_evidence": {
            "source_message_index": 1,
            "exact_quote": "完成合同终稿复核",
        },
    }
    edit_call = {
        "id": "direct-edit-with-trusted-ordinal",
        "type": "function",
        "function": {
            "name": "edit_daily_items",
            "arguments": json.dumps(edit_arguments, ensure_ascii=False),
        },
    }
    final_reply = "已修改昨天日报今日工作的第一条，日报仍保持已提交状态。"
    completions = iter(
        (
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "load-before-trusted-ordinal-edit",
                            "type": "function",
                            "function": {
                                "name": "query_report_by_date",
                                "arguments": json.dumps(
                                    {
                                        "date_expression": "昨天",
                                        "proposed_date": "2026-08-11",
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={"role": "assistant", "content": None, "tool_calls": [edit_call]},
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{**edit_call, "id": "reviewed-trusted-ordinal-edit"}],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "reply": final_reply,
                            "actual_write": True,
                            "operation_outcome": "changed",
                        },
                        ensure_ascii=False,
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": _follow_through_review_envelope(
                        final_reply,
                        decision="keep_no_write",
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
        )
    )
    completion_count = 0
    review_payload: dict | None = None

    async def scripted_completion(
        messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        nonlocal completion_count, review_payload
        del thinking_enabled
        completion_count += 1
        if completion_count == 3:
            assert [
                schema["function"]["name"] for schema in tool_schemas
            ] == ["edit_daily_items"]
            assert "field positions and content" in messages[0]["content"]
            assert "existing binder" in messages[0]["content"]
            review_payload = json.loads(messages[1]["content"])
            assert review_payload["trusted_completed_daily_query_results"] == [
                {
                    "tool_name": "query_report_by_date",
                    "status": "success",
                    "changed": False,
                    "snapshot_available": True,
                    "report_date": "2026-08-11",
                    "report_status": "completed",
                    "fields": {
                        "today_work": [
                            {"position": 1, "content": "完成合同初稿复核"},
                            {"position": 2, "content": "整理付款材料"},
                        ],
                        "problems": [],
                        "tomorrow_plan": [],
                    },
                    "acknowledged_empty_fields": [],
                }
            ]
            assert review_payload["selected_targets"] == [
                {
                    "call_index": 1,
                    "tool_name": "edit_daily_items",
                    "field": "today_work",
                    "position": 1,
                    "content": "完成合同初稿复核",
                }
            ]
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 trusted completed-report ordinal edit",
        user_text="把昨天日报今日工作的第一条改成：完成合同终稿复核。",
        context=_daily_write_context(
            allowed_tool_names=frozenset(
                {"query_report_by_date", "edit_daily_items"}
            )
        ),
        runtime_session=runtime,
    )

    assert review_payload is not None
    serialized_summary = json.dumps(
        review_payload["trusted_completed_daily_query_results"],
        ensure_ascii=False,
    )
    assert str(runtime.report_id) not in serialized_summary
    assert runtime.snapshot["report_state_sha256"] not in serialized_summary
    assert "report_id" not in serialized_summary
    assert "report_state_sha256" not in serialized_summary
    assert "item_id" not in serialized_summary
    assert "version" not in serialized_summary
    assert runtime.executed_tools == ["query_report_by_date", "edit_daily_items"]
    assert runtime.commit_count == 1
    assert result.final_content == final_reply


@pytest.mark.asyncio
async def test_direct_completed_edit_wrong_ordinal_is_rejected_before_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _CompletedReportEditRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=4,
        endpoint="https://example.invalid/chat/completions",
    )
    wrong_edit_call = {
        "id": "direct-edit-wrong-second-item",
        "type": "function",
        "function": {
            "name": "edit_daily_items",
            "arguments": json.dumps(
                {
                    "report_id": str(runtime.report_id),
                    "expected_version": 4,
                    "target_item_ids": ["tw-2"],
                    "replacement": "完成合同终稿复核",
                    "replacement_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "完成合同终稿复核",
                    },
                },
                ensure_ascii=False,
            ),
        },
    }
    clarification = "你指定的是第一条，但当前选择对应第二条；请确认要修改哪一条？"
    completions = iter(
        (
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "load-before-wrong-ordinal-edit",
                            "type": "function",
                            "function": {
                                "name": "query_report_by_date",
                                "arguments": json.dumps(
                                    {
                                        "date_expression": "昨天",
                                        "proposed_date": "2026-08-11",
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [wrong_edit_call],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": json.dumps(
                        {"decision": "clarification", "reply": clarification},
                        ensure_ascii=False,
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": _follow_through_review_envelope(
                        clarification,
                        decision="continue_once",
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
        )
    )
    completion_count = 0
    selected_targets: list[dict] | None = None

    async def scripted_completion(
        messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        nonlocal completion_count, selected_targets
        del tool_schemas, thinking_enabled
        completion_count += 1
        if completion_count == 3:
            review_payload = json.loads(messages[1]["content"])
            selected_targets = review_payload["selected_targets"]
            assert selected_targets == [
                {
                    "call_index": 1,
                    "tool_name": "edit_daily_items",
                    "field": "today_work",
                    "position": 2,
                    "content": "整理付款材料",
                }
            ]
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    with pytest.raises(
        DeepSeekResponseError,
        match="clarification was not independently confirmed",
    ):
        await adapter.run_canary_turn(
            system_prompt="Agent2 wrong completed-report ordinal control",
            user_text="把昨天日报今日工作的第一条改成：完成合同终稿复核。",
            context=_daily_write_context(
                allowed_tool_names=frozenset(
                    {"query_report_by_date", "edit_daily_items"}
                )
            ),
            runtime_session=runtime,
        )

    assert selected_targets is not None
    serialized_targets = json.dumps(selected_targets, ensure_ascii=False)
    assert str(runtime.report_id) not in serialized_targets
    assert runtime.snapshot["report_state_sha256"] not in serialized_targets
    assert "item_id" not in serialized_targets
    assert "version" not in serialized_targets
    assert completion_count == 4
    assert runtime.executed_tools == ["query_report_by_date"]
    assert runtime.commit_count == 0
    assert runtime.rollback_count == 0


@pytest.mark.asyncio
async def test_daily_edit_review_without_query_keeps_its_original_payload_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=4,
        endpoint="https://example.invalid/chat/completions",
    )
    edit_call = {
        "id": "unbound-edit-without-query",
        "type": "function",
        "function": {
            "name": "edit_daily_items",
            "arguments": json.dumps(
                {
                    "report_id": "20000000-0000-4000-8000-000000000004",
                    "expected_version": 4,
                    "target_item_ids": ["tw-1"],
                    "replacement": "完成合同终稿复核",
                    "replacement_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "完成合同终稿复核",
                    },
                },
                ensure_ascii=False,
            ),
        },
    }
    clarification = "请先说明要修改哪一天日报中的哪一条。"
    completions = iter(
        (
            _CompletionResponse(
                message={"role": "assistant", "content": None, "tool_calls": [edit_call]},
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": json.dumps(
                        {"decision": "clarification", "reply": clarification},
                        ensure_ascii=False,
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": _review_envelope(
                        clarification,
                        decision="keep",
                        classification="ordinary_reply",
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
        )
    )
    completion_count = 0

    async def scripted_completion(
        messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        nonlocal completion_count
        del thinking_enabled
        completion_count += 1
        if completion_count == 2:
            assert [
                schema["function"]["name"] for schema in tool_schemas
            ] == ["edit_daily_items"]
            review_payload = json.loads(messages[1]["content"])
            assert set(review_payload) == {
                "ordered_current_user_messages",
                "trusted_context",
                "unexecuted_daily_periodic_weekly_operation_draft",
            }
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 ordinary unbound edit review",
        user_text="把日报里的那条改成：完成合同终稿复核。",
        context=_daily_write_context(
            allowed_tool_names=frozenset({"edit_daily_items"})
        ),
        runtime_session=_NoWriteRuntime(),
    )

    assert result.final_content == clarification
    assert completion_count == 3


@pytest.mark.asyncio
async def test_no_op_daily_query_is_not_injected_into_edit_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _NoOpReportQueryRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=4,
        endpoint="https://example.invalid/chat/completions",
    )
    clarification = "没有查到这一天的日报，无法按条目位置修改。"
    completions = iter(
        (
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "load-missing-before-edit-review",
                            "type": "function",
                            "function": {
                                "name": "query_report_by_date",
                                "arguments": json.dumps(
                                    {
                                        "date_expression": "昨天",
                                        "proposed_date": "2026-08-11",
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "edit-after-missing-query",
                            "type": "function",
                            "function": {
                                "name": "edit_daily_items",
                                "arguments": json.dumps(
                                    {
                                        "report_id": (
                                            "20000000-0000-4000-8000-000000000004"
                                        ),
                                        "expected_version": 4,
                                        "target_item_ids": ["tw-1"],
                                        "replacement": "完成合同终稿复核",
                                        "replacement_evidence": {
                                            "source_message_index": 1,
                                            "exact_quote": "完成合同终稿复核",
                                        },
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": json.dumps(
                        {"decision": "clarification", "reply": clarification},
                        ensure_ascii=False,
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": _review_envelope(
                        clarification,
                        decision="keep",
                        classification="ordinary_reply",
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
        )
    )
    completion_count = 0

    async def scripted_completion(
        messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        nonlocal completion_count
        del tool_schemas, thinking_enabled
        completion_count += 1
        if completion_count == 3:
            review_payload = json.loads(messages[1]["content"])
            assert "trusted_completed_daily_query_results" not in review_payload
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 no-op query edit review control",
        user_text="把昨天日报第一条改成：完成合同终稿复核。",
        context=_daily_write_context(
            allowed_tool_names=frozenset(
                {"query_report_by_date", "edit_daily_items"}
            )
        ),
        runtime_session=runtime,
    )

    assert result.final_content == clarification
    assert completion_count == 4
    assert runtime.executed_tools == ["query_report_by_date"]


@pytest.mark.asyncio
async def test_multiple_daily_queries_are_not_injected_into_edit_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _TwoCompletedReportQueryRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=4,
        endpoint="https://example.invalid/chat/completions",
    )
    clarification = "这一轮查了两个日期，请重新指定要修改哪一天。"
    edit_call = {
        "id": "edit-after-multiple-query-review",
        "type": "function",
        "function": {
            "name": "edit_daily_items",
            "arguments": json.dumps(
                {
                    "report_id": str(runtime.report_id),
                    "expected_version": 4,
                    "target_item_ids": ["tw-1"],
                    "replacement": "完成合同终稿复核",
                    "replacement_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "完成合同终稿复核",
                    },
                },
                ensure_ascii=False,
            ),
        },
    }
    query_calls = [
        {
            "id": f"load-before-multi-review-{index}",
            "type": "function",
            "function": {
                "name": "query_report_by_date",
                "arguments": json.dumps(
                    {
                        "date_expression": expression,
                        "proposed_date": proposed_date,
                    },
                    ensure_ascii=False,
                ),
            },
        }
        for index, (expression, proposed_date) in enumerate(
            (("昨天", "2026-08-11"), ("前天", "2026-08-10")),
            start=1,
        )
    ]
    completions = iter(
        (
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": query_calls,
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={"role": "assistant", "content": None, "tool_calls": [edit_call]},
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": json.dumps(
                        {"decision": "clarification", "reply": clarification},
                        ensure_ascii=False,
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": _follow_through_review_envelope(
                        clarification,
                        decision="keep_clarification",
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": _review_envelope(
                        clarification,
                        decision="keep",
                        classification="ordinary_reply",
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
        )
    )
    completion_count = 0

    async def scripted_completion(
        messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        nonlocal completion_count
        del tool_schemas, thinking_enabled
        completion_count += 1
        if completion_count == 3:
            review_payload = json.loads(messages[1]["content"])
            assert "trusted_completed_daily_query_results" not in review_payload
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 multiple-query edit review control",
        user_text="查昨天和前天，再把第一条改成：完成合同终稿复核。",
        context=_daily_write_context(
            allowed_tool_names=frozenset(
                {"query_report_by_date", "edit_daily_items"}
            )
        ),
        runtime_session=runtime,
    )

    assert result.final_content == clarification
    assert completion_count == 5
    assert runtime.executed_tools == [
        "query_report_by_date",
        "query_report_by_date",
    ]


@pytest.mark.parametrize(
    ("write_tool_name", "write_details", "user_text"),
    (
        (
            "delete_daily_items",
            {"target_item_ids": ["tw-2"]},
            "删除昨天日报今日工作的第二条。",
        ),
        (
            "move_daily_items",
            {
                "target_item_ids": ["tw-1"],
                "source_field": "today_work",
                "target_field": "tomorrow_plan",
            },
            "把昨天日报今日工作的第一条移到明日计划。",
        ),
    ),
)
@pytest.mark.asyncio
async def test_direct_completed_report_delete_and_move_use_existing_review(
    monkeypatch: pytest.MonkeyPatch,
    write_tool_name: str,
    write_details: dict,
    user_text: str,
) -> None:
    write_arguments = {
        "report_id": "20000000-0000-4000-8000-000000000004",
        "expected_version": 4,
        **write_details,
    }
    runtime = _CompletedReportContentWriteRuntime(
        write_tool_name=write_tool_name,
        write_arguments=write_arguments,
    )
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=4,
        endpoint="https://example.invalid/chat/completions",
    )
    write_call = {
        "id": f"direct-{write_tool_name}",
        "type": "function",
        "function": {
            "name": write_tool_name,
            "arguments": json.dumps(write_arguments, ensure_ascii=False),
        },
    }
    final_reply = "已按你的要求修改昨天日报，日报仍保持已提交状态。"
    completions = iter(
        (
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"load-before-direct-{write_tool_name}",
                            "type": "function",
                            "function": {
                                "name": "query_report_by_date",
                                "arguments": json.dumps(
                                    {
                                        "date_expression": "昨天",
                                        "proposed_date": "2026-08-11",
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={"role": "assistant", "content": None, "tool_calls": [write_call]},
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{**write_call, "id": f"reviewed-{write_tool_name}"}],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "reply": final_reply,
                            "actual_write": True,
                            "operation_outcome": "changed",
                        },
                        ensure_ascii=False,
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": _follow_through_review_envelope(
                        final_reply,
                        decision="keep_no_write",
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
        )
    )
    completion_tool_names: list[list[str]] = []
    completion_messages: list[list[dict]] = []

    async def scripted_completion(
        messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        del thinking_enabled
        completion_messages.append(messages)
        completion_tool_names.append(
            [schema["function"]["name"] for schema in tool_schemas]
        )
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 direct completed-report content-write review",
        user_text=user_text,
        context=_daily_write_context(
            allowed_tool_names=frozenset(
                {"query_report_by_date", write_tool_name}
            )
        ),
        runtime_session=runtime,
    )

    assert runtime.executed_tools == ["query_report_by_date", write_tool_name]
    assert runtime.commit_count == 1
    assert result.final_content == final_reply
    assert completion_tool_names[2] == [write_tool_name]
    assert completion_tool_names[4] == []
    selected_targets = json.loads(completion_messages[2][1]["content"])[
        "selected_targets"
    ]
    expected_target = {
        "call_index": 1,
        "tool_name": write_tool_name,
        "field": "today_work",
        "position": 2 if write_tool_name == "delete_daily_items" else 1,
        "content": (
            "整理付款材料"
            if write_tool_name == "delete_daily_items"
            else "完成合同初稿复核"
        ),
    }
    if write_tool_name == "move_daily_items":
        expected_target.update(
            {"source_field": "today_work", "target_field": "tomorrow_plan"}
        )
    assert selected_targets == [expected_target]


@pytest.mark.asyncio
async def test_completed_delete_review_maps_multiple_targets_across_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_arguments = {
        "report_id": "20000000-0000-4000-8000-000000000004",
        "expected_version": 4,
        "target_item_ids": ["tw-1", "p-1"],
    }
    runtime = _CompletedReportContentWriteRuntime(
        write_tool_name="delete_daily_items",
        write_arguments=write_arguments,
    )
    runtime.snapshot["fields"]["problems"] = [
        {"item_id": "p-1", "content": "等待对方确认"}
    ]
    runtime.snapshot["report_state_sha256"] = _completed_report_state_hash(
        runtime.snapshot
    )
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=4,
        endpoint="https://example.invalid/chat/completions",
    )
    delete_call = {
        "id": "delete-completed-cross-field-targets",
        "type": "function",
        "function": {
            "name": "delete_daily_items",
            "arguments": json.dumps(write_arguments, ensure_ascii=False),
        },
    }
    final_reply = "已删除昨天日报今日工作的第一条和问题困难的第一条。"
    completions = iter(
        (
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "load-before-cross-field-delete",
                            "type": "function",
                            "function": {
                                "name": "query_report_by_date",
                                "arguments": json.dumps(
                                    {
                                        "date_expression": "昨天",
                                        "proposed_date": "2026-08-11",
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [delete_call],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {**delete_call, "id": "reviewed-cross-field-delete"}
                    ],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "reply": final_reply,
                            "actual_write": True,
                            "operation_outcome": "changed",
                        },
                        ensure_ascii=False,
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": _follow_through_review_envelope(
                        final_reply,
                        decision="keep_no_write",
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
        )
    )
    completion_count = 0
    selected_targets: list[dict] | None = None

    async def scripted_completion(
        messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        nonlocal completion_count, selected_targets
        del tool_schemas, thinking_enabled
        completion_count += 1
        if completion_count == 3:
            selected_targets = json.loads(messages[1]["content"])[
                "selected_targets"
            ]
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 completed cross-field delete mapping",
        user_text="删除昨天日报今日工作的第一条和问题困难的第一条。",
        context=_daily_write_context(
            allowed_tool_names=frozenset(
                {"query_report_by_date", "delete_daily_items"}
            )
        ),
        runtime_session=runtime,
    )

    assert selected_targets == [
        {
            "call_index": 1,
            "tool_name": "delete_daily_items",
            "field": "today_work",
            "position": 1,
            "content": "完成合同初稿复核",
        },
        {
            "call_index": 1,
            "tool_name": "delete_daily_items",
            "field": "problems",
            "position": 1,
            "content": "等待对方确认",
        },
    ]
    serialized_targets = json.dumps(selected_targets, ensure_ascii=False)
    assert str(runtime.report_id) not in serialized_targets
    assert "tw-1" not in serialized_targets
    assert "p-1" not in serialized_targets
    assert "version" not in serialized_targets
    assert runtime.executed_tools == ["query_report_by_date", "delete_daily_items"]
    assert runtime.commit_count == 1
    assert result.final_content == final_reply


@pytest.mark.parametrize(
    ("tool_name", "argument_overrides"),
    (
        (
            "edit_daily_items",
            {"report_id": "20000000-0000-4000-8000-000000000099"},
        ),
        ("edit_daily_items", {"expected_version": 3}),
        ("edit_daily_items", {"target_item_ids": ["unknown-item"]}),
        (
            "move_daily_items",
            {"source_field": "problems", "target_field": "tomorrow_plan"},
        ),
    ),
)
@pytest.mark.asyncio
async def test_completed_target_draft_mismatch_fails_before_semantic_review(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    argument_overrides: dict,
) -> None:
    runtime = _CompletedReportEditRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=4,
        endpoint="https://example.invalid/chat/completions",
    )
    arguments = {
        "report_id": str(runtime.report_id),
        "expected_version": 4,
        "target_item_ids": ["tw-1"],
        **(
            {
                "replacement": "完成合同终稿复核",
                "replacement_evidence": {
                    "source_message_index": 1,
                    "exact_quote": "完成合同终稿复核",
                },
            }
            if tool_name == "edit_daily_items"
            else {"source_field": "today_work", "target_field": "tomorrow_plan"}
        ),
        **argument_overrides,
    }
    completions = iter(
        (
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "load-before-mismatched-target-draft",
                            "type": "function",
                            "function": {
                                "name": "query_report_by_date",
                                "arguments": json.dumps(
                                    {
                                        "date_expression": "昨天",
                                        "proposed_date": "2026-08-11",
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "mismatched-completed-target-draft",
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(
                                    arguments,
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
        )
    )
    completion_count = 0

    async def scripted_completion(
        _messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        nonlocal completion_count
        del tool_schemas, thinking_enabled
        completion_count += 1
        if completion_count > 2:
            raise AssertionError(
                "a mismatched completed target must fail before semantic review"
            )
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    with pytest.raises(
        DeepSeekResponseError,
        match="does not match the trusted completed query",
    ):
        await adapter.run_canary_turn(
            system_prompt="Agent2 completed target binding control",
            user_text="把昨天日报今日工作的第一条改成：完成合同终稿复核。",
            context=_daily_write_context(
                allowed_tool_names=frozenset(
                    {"query_report_by_date", tool_name}
                )
            ),
            runtime_session=runtime,
        )

    assert completion_count == 2
    assert runtime.executed_tools == ["query_report_by_date"]
    assert runtime.commit_count == 0
    assert runtime.rollback_count == 0


@pytest.mark.parametrize("leak_kind", ("report_id", "state_hash"))
@pytest.mark.asyncio
async def test_completed_report_edit_rolls_back_if_final_reply_changes_identifier_case(
    monkeypatch: pytest.MonkeyPatch,
    leak_kind: str,
) -> None:
    runtime = _CompletedReportEditRuntime()
    if leak_kind == "report_id":
        runtime.report_id = UUID("abcdefab-cdef-4abc-8def-abcdefabcdef")
        runtime.snapshot["report_id"] = str(runtime.report_id)
        runtime.snapshot["report_state_sha256"] = _completed_report_state_hash(
            runtime.snapshot
        )
        runtime.query_target_id = str(runtime.report_id)
    leaked_value = (
        str(runtime.report_id)
        if leak_kind == "report_id"
        else runtime.snapshot["report_state_sha256"]
    )
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=4,
        endpoint="https://example.invalid/chat/completions",
    )
    query_call = {
        "id": "load-before-leaking-edit",
        "type": "function",
        "function": {
            "name": "query_report_by_date",
            "arguments": json.dumps(
                {
                    "date_expression": "昨天",
                    "proposed_date": "2026-08-11",
                },
                ensure_ascii=False,
            ),
        },
    }
    edit_arguments = {
        "report_id": str(runtime.report_id),
        "expected_version": 4,
        "target_item_ids": ["tw-1"],
        "replacement": "完成合同终稿复核",
        "replacement_evidence": {
            "source_message_index": 1,
            "exact_quote": "完成合同终稿复核",
        },
    }
    loaded_only_reply = "我已经查到昨天的日报内容。"
    completions = iter(
        (
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [query_call],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={"role": "assistant", "content": loaded_only_reply},
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": _follow_through_review_envelope(
                        loaded_only_reply,
                        decision="continue_once",
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
            *(
                _CompletionResponse(
                    message={
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": "edit_daily_items",
                                    "arguments": json.dumps(
                                        edit_arguments,
                                        ensure_ascii=False,
                                    ),
                                },
                            }
                        ],
                    },
                    metadata={"finish_reason": "tool_calls"},
                )
                for call_id in ("leaking-edit", "reviewed-leaking-edit")
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "reply": f"已修改日报 {leaked_value.upper()}。",
                            "actual_write": True,
                            "operation_outcome": "changed",
                        },
                        ensure_ascii=False,
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
        )
    )

    async def scripted_completion(
        _messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        del tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    with pytest.raises(
        DeepSeekResponseError,
        match="terminal reply exposed internal Daily identifiers",
    ):
        await adapter.run_canary_turn(
            system_prompt="Agent2 final-reply identifier control",
            user_text="把昨天日报今日工作的第一条改成：完成合同终稿复核。",
            context=_daily_write_context(
                allowed_tool_names=frozenset(
                    {"query_report_by_date", "edit_daily_items"}
                )
            ),
            runtime_session=runtime,
        )

    assert runtime.executed_tools == [
        "query_report_by_date",
        "edit_daily_items",
    ]
    assert runtime.commit_count == 0
    assert runtime.rollback_count == 1


@pytest.mark.parametrize(
    ("user_text", "final_reply", "review_decision", "should_fail"),
    (
        (
            "把昨天日报第一条改成：完成合同终稿复核，并关闭我的日报提醒。",
            "已关闭你的日报提醒，并查到了昨天的日报。",
            "continue_once",
            True,
        ),
        (
            "查看昨天日报，并关闭我的日报提醒。",
            "已关闭你的日报提醒，并查到了昨天的日报。",
            "keep_no_write",
            False,
        ),
        (
            "关闭我的日报提醒，并帮我改一下昨天日报。",
            "已关闭你的日报提醒；昨天日报要修改哪一条、改成什么内容？",
            "keep_clarification",
            True,
        ),
    ),
)
@pytest.mark.asyncio
async def test_completed_query_non_daily_write_batch_gets_final_semantic_gate(
    monkeypatch: pytest.MonkeyPatch,
    user_text: str,
    final_reply: str,
    review_decision: str,
    should_fail: bool,
) -> None:
    runtime = _CompletedReportMemoryWriteRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=4,
        endpoint="https://example.invalid/chat/completions",
    )
    completions = iter(
        (
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "load-before-memory-only-write",
                            "type": "function",
                            "function": {
                                "name": "query_report_by_date",
                                "arguments": json.dumps(
                                    {
                                        "date_expression": "昨天",
                                        "proposed_date": "2026-08-11",
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "memory-only-after-completed-query",
                            "type": "function",
                            "function": {
                                "name": "remember_personal_memory",
                                "arguments": json.dumps(
                                    {
                                        "memory_key": "report.daily_reminders_enabled",
                                        "value": {"enabled": False},
                                        "source_evidence": {
                                            "source_message_index": 1,
                                            "intent": "explicit_preference",
                                        },
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "reply": final_reply,
                            "actual_write": True,
                            "operation_outcome": "changed",
                        },
                        ensure_ascii=False,
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": _follow_through_review_envelope(
                        final_reply,
                        decision=review_decision,
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
        )
    )
    completion_count = 0
    completion_tool_names: list[list[str]] = []

    async def scripted_completion(
        _messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        nonlocal completion_count
        del thinking_enabled
        completion_count += 1
        completion_tool_names.append(
            [schema["function"]["name"] for schema in tool_schemas]
        )
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    if should_fail:
        with pytest.raises(
            DeepSeekResponseError,
            match="Daily content write remained unfulfilled",
        ):
            await adapter.run_canary_turn(
                system_prompt="Agent2 cross-domain write completion control",
                user_text=user_text,
                context=_daily_write_context(
                    allowed_tool_names=frozenset(
                        {
                            "query_report_by_date",
                            "edit_daily_items",
                            "remember_personal_memory",
                        }
                    )
                ),
                runtime_session=runtime,
            )
        result = None
    else:
        result = await adapter.run_canary_turn(
            system_prompt="Agent2 cross-domain write completion control",
            user_text=user_text,
            context=_daily_write_context(
                allowed_tool_names=frozenset(
                    {
                        "query_report_by_date",
                        "edit_daily_items",
                        "remember_personal_memory",
                    }
                )
            ),
            runtime_session=runtime,
        )

    assert completion_count == 4
    assert completion_tool_names[3] == []
    assert runtime.executed_tools == [
        "query_report_by_date",
        "remember_personal_memory",
    ]
    if should_fail:
        assert runtime.commit_count == 0
        assert runtime.rollback_count == 1
    else:
        assert runtime.commit_count == 1
        assert runtime.rollback_count == 0
        assert result is not None
        assert result.final_content == final_reply


@pytest.mark.asyncio
async def test_partial_daily_write_with_true_clarification_rolls_back_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    edit_arguments = {
        "report_id": "20000000-0000-4000-8000-000000000004",
        "expected_version": 4,
        "target_item_ids": ["tw-1"],
        "replacement": "完成合同终稿复核",
        "replacement_evidence": {
            "source_message_index": 1,
            "exact_quote": "完成合同终稿复核",
        },
    }
    runtime = _CompletedReportContentWriteRuntime(
        write_tool_name="edit_daily_items",
        write_arguments=edit_arguments,
    )
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=4,
        endpoint="https://example.invalid/chat/completions",
    )
    edit_call = {
        "id": "partial-completed-report-edit",
        "type": "function",
        "function": {
            "name": "edit_daily_items",
            "arguments": json.dumps(edit_arguments, ensure_ascii=False),
        },
    }
    clarification = "第一条的修改已处理；第二条需要改成什么内容？"
    completions = iter(
        (
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "load-before-partial-edit",
                            "type": "function",
                            "function": {
                                "name": "query_report_by_date",
                                "arguments": json.dumps(
                                    {
                                        "date_expression": "昨天",
                                        "proposed_date": "2026-08-11",
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={"role": "assistant", "content": None, "tool_calls": [edit_call]},
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{**edit_call, "id": "reviewed-partial-edit"}],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "reply": clarification,
                            "actual_write": True,
                            "operation_outcome": "changed",
                        },
                        ensure_ascii=False,
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": _follow_through_review_envelope(
                        clarification,
                        decision="keep_clarification",
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
        )
    )

    async def scripted_completion(
        _messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        del tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    with pytest.raises(
        DeepSeekResponseError,
        match="Daily content write remained unfulfilled",
    ):
        await adapter.run_canary_turn(
            system_prompt="Agent2 atomic completed-report partial edit",
            user_text=(
                "把昨天日报第一条改成：完成合同终稿复核；"
                "第二条也要改，但我还没给新内容，请先问我。"
            ),
            context=_daily_write_context(
                allowed_tool_names=frozenset(
                    {"query_report_by_date", "edit_daily_items"}
                )
            ),
            runtime_session=runtime,
        )

    assert runtime.executed_tools == [
        "query_report_by_date",
        "edit_daily_items",
    ]
    assert runtime.commit_count == 0
    assert runtime.rollback_count == 1


@pytest.mark.parametrize("leak_kind", ("report_id", "state_hash"))
def test_daily_identifier_leak_check_rejects_case_changed_uuid_and_hash(
    leak_kind: str,
) -> None:
    runtime = _CompletedReportEditRuntime()
    runtime.report_id = UUID("abcdefab-cdef-4abc-8def-abcdefabcdef")
    runtime.snapshot["report_id"] = str(runtime.report_id)
    runtime.snapshot["report_state_sha256"] = _completed_report_state_hash(
        runtime.snapshot
    )
    receipt = ToolReceipt(
        status=ReceiptStatus.SUCCESS,
        tool_name="query_report_by_date",
        changed=False,
        target_type="daily_report",
        target_id=str(runtime.report_id),
        before_version=4,
        after_version=4,
        affected_item_ids=(),
        safe_user_facts={
            "actual_write": False,
            "report_found": True,
            "report_snapshot": runtime.snapshot,
            "report_date": runtime.snapshot["report_date"],
        },
        execution_mode=ExecutionMode.CANARY_EXECUTE,
    )
    leaked_value = (
        str(runtime.report_id)
        if leak_kind == "report_id"
        else runtime.snapshot["report_state_sha256"]
    )

    with pytest.raises(
        DeepSeekResponseError,
        match="terminal reply exposed internal Daily identifiers",
    ):
        _assert_no_trusted_daily_identifier_leak(
            f"internal value: {leaked_value.upper()}",
            (receipt,),
        )


@pytest.mark.asyncio
async def test_completed_report_read_can_end_without_being_forced_into_a_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _CompletedReportEditRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=4,
        endpoint="https://example.invalid/chat/completions",
    )
    answer = "昨天日报今日工作有两条，当前状态是已提交。"
    completions = iter(
        (
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "load-completed-report-for-read",
                            "type": "function",
                            "function": {
                                "name": "query_report_by_date",
                                "arguments": json.dumps(
                                    {
                                        "date_expression": "昨天",
                                        "proposed_date": "2026-08-11",
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={"role": "assistant", "content": answer},
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": _follow_through_review_envelope(
                        answer,
                        decision="keep_no_write",
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": _review_envelope(
                        answer,
                        decision="keep",
                        classification="ordinary_reply",
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
        )
    )

    async def scripted_completion(
        _messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        del tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 completed-report read control",
        user_text="查看一下我昨天的日报。",
        context=_daily_write_context(
            allowed_tool_names=frozenset({"query_report_by_date", "edit_daily_items"})
        ),
        runtime_session=runtime,
    )

    assert runtime.executed_tools == ["query_report_by_date"]
    assert runtime.commit_count == 0
    assert [receipt.changed for receipt in result.receipts] == [False]
    assert result.final_content == answer


@pytest.mark.parametrize(
    "leak_kind",
    (
        "report_id",
        "item_id",
        "state_hash",
        "version",
        "version_json",
        "version_fullwidth_colon",
        "expected_version",
        "report_version",
    ),
)
@pytest.mark.asyncio
async def test_completed_report_read_rejects_internal_identifier_leaks(
    monkeypatch: pytest.MonkeyPatch,
    leak_kind: str,
) -> None:
    runtime = _CompletedReportEditRuntime()
    leaked_values = {
        "report_id": str(runtime.report_id),
        "item_id": "tw-1",
        "state_hash": runtime.snapshot["report_state_sha256"],
        "version": "version=4",
        "version_json": '{"version":4}',
        "version_fullwidth_colon": "version：4",
        "expected_version": "expected_version: 4",
        "report_version": '{"report_version":4}',
    }
    leaked_reply = f"查询结果：{leaked_values[leak_kind]}"
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=4,
        endpoint="https://example.invalid/chat/completions",
    )
    completions = iter(
        (
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"load-before-{leak_kind}-leak",
                            "type": "function",
                            "function": {
                                "name": "query_report_by_date",
                                "arguments": json.dumps(
                                    {
                                        "date_expression": "昨天",
                                        "proposed_date": "2026-08-11",
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={"role": "assistant", "content": leaked_reply},
                metadata={"finish_reason": "stop"},
            ),
        )
    )

    async def scripted_completion(
        _messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        del tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    with pytest.raises(
        DeepSeekResponseError,
        match="terminal reply exposed internal Daily identifiers",
    ):
        await adapter.run_canary_turn(
            system_prompt="Agent2 read identifier leak control",
            user_text="查看我昨天的日报。",
            context=_daily_write_context(
                allowed_tool_names=frozenset(
                    {"query_report_by_date", "edit_daily_items"}
                )
            ),
            runtime_session=runtime,
        )

    assert runtime.executed_tools == ["query_report_by_date"]
    assert runtime.commit_count == 0


@pytest.mark.parametrize(
    "safe_fragment",
    (
        "4",
        "日期是2026-08-04",
        "conversion=4",
        "subversion=4",
        "version=40",
        "version=4a",
        "contract version 4 reviewed",
        "Version 4 of the contract",
    ),
)
@pytest.mark.asyncio
async def test_completed_report_read_does_not_overmatch_version_text(
    monkeypatch: pytest.MonkeyPatch,
    safe_fragment: str,
) -> None:
    runtime = _CompletedReportEditRuntime()
    answer = f"查询说明：{safe_fragment}"
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=4,
        endpoint="https://example.invalid/chat/completions",
    )
    completions = iter(
        (
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "load-before-safe-version-text",
                            "type": "function",
                            "function": {
                                "name": "query_report_by_date",
                                "arguments": json.dumps(
                                    {
                                        "date_expression": "昨天",
                                        "proposed_date": "2026-08-11",
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={"role": "assistant", "content": answer},
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": _follow_through_review_envelope(
                        answer,
                        decision="keep_no_write",
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": _review_envelope(
                        answer,
                        decision="keep",
                        classification="ordinary_reply",
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
        )
    )

    async def scripted_completion(
        _messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        del tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 version boundary control",
        user_text="查看我昨天的日报。",
        context=_daily_write_context(
            allowed_tool_names=frozenset(
                {"query_report_by_date", "edit_daily_items"}
            )
        ),
        runtime_session=runtime,
    )

    assert result.final_content == answer
    assert runtime.executed_tools == ["query_report_by_date"]


@pytest.mark.asyncio
async def test_initial_clarification_rejects_snapshot_hash_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _CompletedReportEditRuntime()
    clarification = (
        "请说明要改哪一条。内部状态："
        f"{runtime.snapshot['report_state_sha256']}"
    )
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=4,
        endpoint="https://example.invalid/chat/completions",
    )
    completions = iter(
        (
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "load-before-leaking-clarification",
                            "type": "function",
                            "function": {
                                "name": "query_report_by_date",
                                "arguments": json.dumps(
                                    {
                                        "date_expression": "昨天",
                                        "proposed_date": "2026-08-11",
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={"role": "assistant", "content": clarification},
                metadata={"finish_reason": "stop"},
            ),
        )
    )

    async def scripted_completion(
        _messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        del tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    with pytest.raises(
        DeepSeekResponseError,
        match="terminal reply exposed internal Daily identifiers",
    ):
        await adapter.run_canary_turn(
            system_prompt="Agent2 clarification identifier leak control",
            user_text="把昨天日报今日工作里的那条改一下。",
            context=_daily_write_context(
                allowed_tool_names=frozenset(
                    {"query_report_by_date", "edit_daily_items"}
                )
            ),
            runtime_session=runtime,
        )

    assert runtime.executed_tools == ["query_report_by_date"]


@pytest.mark.asyncio
async def test_completed_report_edit_can_keep_a_genuinely_needed_clarification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _CompletedReportEditRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=4,
        endpoint="https://example.invalid/chat/completions",
    )
    clarification = "昨天日报今日工作有两条。你想改哪一条，改成什么内容？"
    completions = iter(
        (
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "load-completed-report-for-clarification",
                            "type": "function",
                            "function": {
                                "name": "query_report_by_date",
                                "arguments": json.dumps(
                                    {
                                        "date_expression": "昨天",
                                        "proposed_date": "2026-08-11",
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={"role": "assistant", "content": clarification},
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": _follow_through_review_envelope(
                        clarification,
                        decision="keep_clarification",
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": _review_envelope(
                        clarification,
                        decision="keep",
                        classification="ordinary_reply",
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
        )
    )
    async def scripted_completion(
        _messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        del tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 completed-report clarification control",
        user_text="把昨天日报今日工作里的那条改一下。",
        context=_daily_write_context(
            allowed_tool_names=frozenset({"query_report_by_date", "edit_daily_items"})
        ),
        runtime_session=runtime,
    )

    assert runtime.executed_tools == ["query_report_by_date"]
    assert runtime.commit_count == 0
    assert result.final_content == clarification


@pytest.mark.asyncio
async def test_completed_report_follow_through_cannot_end_without_a_write_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _CompletedReportEditRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=4,
        endpoint="https://example.invalid/chat/completions",
    )
    loaded_only_reply = "我已经查到昨天的日报内容。"
    completions = iter(
        (
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "load-before-dropped-edit",
                            "type": "function",
                            "function": {
                                "name": "query_report_by_date",
                                "arguments": json.dumps(
                                    {
                                        "date_expression": "昨天",
                                        "proposed_date": "2026-08-11",
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={"role": "assistant", "content": loaded_only_reply},
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": _follow_through_review_envelope(
                        loaded_only_reply,
                        decision="continue_once",
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": "我还是只查到了日报。",
                },
                metadata={"finish_reason": "stop"},
            ),
        )
    )

    async def scripted_completion(
        _messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        del tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    with pytest.raises(
        DeepSeekResponseError,
        match="daily content follow-through ended without a write call",
    ):
        await adapter.run_canary_turn(
            system_prompt="Agent2 completed-report follow-through bound",
            user_text="把昨天日报今日工作的第一条改成：完成合同终稿复核。",
            context=_daily_write_context(
                allowed_tool_names=frozenset(
                    {"query_report_by_date", "edit_daily_items"}
                )
            ),
            runtime_session=runtime,
        )

    assert runtime.executed_tools == ["query_report_by_date"]
    assert runtime.commit_count == 0


@pytest.mark.parametrize(
    "runtime_kwargs",
    (
        {
            "snapshot_report_id": UUID(
                "20000000-0000-4000-8000-000000000099"
            )
        },
        {"snapshot_version": 3},
        {"snapshot_hash": "G" * 64},
        {
            "snapshot_fields": {
                "today_work": [
                    {
                        "item_id": "tw-1",
                        "content": "完成合同初稿复核",
                    }
                ],
                "problems": [],
            }
        },
        {"query_safe_fact_overrides": {"report_found": False}},
        {"query_safe_fact_overrides": {"report_date": "2026-08-10"}},
        {"query_affected_item_ids": ("tw-1",)},
        {"query_execution_mode": ExecutionMode.SHADOW_PROPOSAL},
        {"query_receipt_overrides": {"error_code": "FORGED"}},
        {"query_receipt_overrides": {"would_change": True}},
        {"query_receipt_overrides": {"validation_errors": ("forged",)}},
        {"query_server_evidence": {}},
        {
            "query_server_evidence": {
                "principal_scope_sha256": principal_scope_sha256(
                    tenant_id="tenant-a",
                    user_id=UUID("10000000-0000-4000-8000-000000000099"),
                    conversation_id="direct-user-a",
                    source_message_id="message-current",
                )
            }
        },
    ),
)
@pytest.mark.asyncio
async def test_inconsistent_success_query_snapshot_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    runtime_kwargs: dict,
) -> None:
    runtime = _CompletedReportEditRuntime(**runtime_kwargs)
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=4,
        endpoint="https://example.invalid/chat/completions",
    )
    completions = iter(
        (
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "load-unbound-completed-report",
                            "type": "function",
                            "function": {
                                "name": "query_report_by_date",
                                "arguments": json.dumps(
                                    {
                                        "date_expression": "昨天",
                                        "proposed_date": "2026-08-11",
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={"role": "assistant", "content": "我还不能可靠处理。"},
                metadata={"finish_reason": "stop"},
            ),
        )
    )

    completion_count = 0

    async def scripted_completion(
        _messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        nonlocal completion_count
        del tool_schemas, thinking_enabled
        completion_count += 1
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    with pytest.raises(
        DeepSeekResponseError,
        match="successful Daily query returned an inconsistent snapshot",
    ):
        await adapter.run_canary_turn(
            system_prompt="Agent2 inconsistent completed-report control",
            user_text="把昨天日报今日工作的第一条改成：完成合同终稿复核。",
            context=_daily_write_context(
                allowed_tool_names=frozenset(
                    {"query_report_by_date", "edit_daily_items"}
                )
            ),
            runtime_session=runtime,
        )

    assert runtime.executed_tools == ["query_report_by_date"]
    assert completion_count == 2
    assert runtime.commit_count == 0
    assert runtime.rollback_count == 0


@pytest.mark.asyncio
async def test_completed_report_query_rejects_forged_content_before_semantic_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _CompletedReportEditRuntime()
    runtime.snapshot["fields"]["today_work"][0]["content"] = "FORGED"
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=4,
        endpoint="https://example.invalid/chat/completions",
    )
    completions = iter(
        (
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "load-forged-completed-report",
                            "type": "function",
                            "function": {
                                "name": "query_report_by_date",
                                "arguments": json.dumps(
                                    {
                                        "date_expression": "yesterday",
                                        "proposed_date": "2026-08-11",
                                    }
                                ),
                            },
                        }
                    ],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": "I found yesterday's completed report.",
                },
                metadata={"finish_reason": "stop"},
            ),
        )
    )
    completion_count = 0

    async def scripted_completion(
        _messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        nonlocal completion_count
        del tool_schemas, thinking_enabled
        completion_count += 1
        if completion_count > 2:
            raise AssertionError(
                "an inconsistent snapshot must be rejected before semantic review"
            )
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    with pytest.raises(
        DeepSeekResponseError,
        match="successful Daily query returned an inconsistent snapshot",
    ):
        await adapter.run_canary_turn(
            system_prompt="Agent2 forged completed-report control",
            user_text="Change the first item in yesterday's report.",
            context=_daily_write_context(
                allowed_tool_names=frozenset(
                    {"query_report_by_date", "edit_daily_items"}
                )
            ),
            runtime_session=runtime,
        )

    assert completion_count == 2
    assert runtime.executed_tools == ["query_report_by_date"]
    assert runtime.commit_count == 0
    assert runtime.rollback_count == 0


@pytest.mark.asyncio
async def test_forged_report_query_blocks_direct_edit_before_write_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _CompletedReportEditRuntime()
    runtime.snapshot["fields"]["today_work"][0]["content"] = "FORGED"
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=4,
        endpoint="https://example.invalid/chat/completions",
    )
    query_call = {
        "id": "load-forged-before-direct-edit",
        "type": "function",
        "function": {
            "name": "query_report_by_date",
            "arguments": json.dumps(
                {
                    "date_expression": "昨天",
                    "proposed_date": "2026-08-11",
                },
                ensure_ascii=False,
            ),
        },
    }
    edit_call = {
        "id": "direct-edit-after-forged-query",
        "type": "function",
        "function": {
            "name": "edit_daily_items",
            "arguments": json.dumps(
                {
                    "report_id": str(runtime.report_id),
                    "expected_version": 4,
                    "target_item_ids": ["tw-1"],
                    "replacement": "完成合同终稿复核",
                    "replacement_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "完成合同终稿复核",
                    },
                },
                ensure_ascii=False,
            ),
        },
    }
    final_reply = "已修改昨天日报今日工作的第一条。"
    completions = iter(
        (
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [query_call],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [edit_call],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {**edit_call, "id": "reviewed-direct-edit-after-forged-query"}
                    ],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "reply": final_reply,
                            "actual_write": True,
                            "operation_outcome": "changed",
                        },
                        ensure_ascii=False,
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
        )
    )
    completion_count = 0

    async def scripted_completion(
        _messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        nonlocal completion_count
        del tool_schemas, thinking_enabled
        completion_count += 1
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    with pytest.raises(
        DeepSeekResponseError,
        match="successful Daily query returned an inconsistent snapshot",
    ):
        await adapter.run_canary_turn(
            system_prompt="Agent2 forged direct-edit control",
            user_text="把昨天日报今日工作的第一条改成：完成合同终稿复核。",
            context=_daily_write_context(
                allowed_tool_names=frozenset(
                    {"query_report_by_date", "edit_daily_items"}
                )
            ),
            runtime_session=runtime,
        )

    assert completion_count == 2
    assert runtime.executed_tools == ["query_report_by_date"]
    assert runtime.commit_count == 0
    assert runtime.rollback_count == 0


@pytest.mark.asyncio
async def test_multiple_queries_with_one_forged_snapshot_block_direct_edit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _OneForgedOfTwoCompletedReportQueryRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=4,
        endpoint="https://example.invalid/chat/completions",
    )
    query_calls = [
        {
            "id": f"load-report-{proposed_date}",
            "type": "function",
            "function": {
                "name": "query_report_by_date",
                "arguments": json.dumps(
                    {
                        "date_expression": date_expression,
                        "proposed_date": proposed_date,
                    },
                    ensure_ascii=False,
                ),
            },
        }
        for date_expression, proposed_date in (
            ("昨天", "2026-08-11"),
            ("前天", "2026-08-10"),
        )
    ]
    edit_call = {
        "id": "direct-edit-after-one-of-two-forged-queries",
        "type": "function",
        "function": {
            "name": "edit_daily_items",
            "arguments": json.dumps(
                {
                    "report_id": str(runtime.report_id),
                    "expected_version": 4,
                    "target_item_ids": ["tw-1"],
                    "replacement": "完成合同终稿复核",
                    "replacement_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "完成合同终稿复核",
                    },
                },
                ensure_ascii=False,
            ),
        },
    }
    completions = iter(
        (
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": query_calls,
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [edit_call],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
        )
    )
    completion_count = 0

    async def scripted_completion(
        _messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        nonlocal completion_count
        del tool_schemas, thinking_enabled
        completion_count += 1
        if completion_count > 2:
            raise AssertionError(
                "a forged query batch must be rejected before write review"
            )
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    with pytest.raises(
        DeepSeekResponseError,
        match="successful Daily query returned an inconsistent snapshot",
    ):
        await adapter.run_canary_turn(
            system_prompt="Agent2 multi-query forged direct-edit control",
            user_text="查昨天和前天，再把昨天日报第一条改成：完成合同终稿复核。",
            context=_daily_write_context(
                allowed_tool_names=frozenset(
                    {"query_report_by_date", "edit_daily_items"}
                )
            ),
            runtime_session=runtime,
        )

    assert completion_count == 2
    assert runtime.executed_tools == [
        "query_report_by_date",
        "query_report_by_date",
    ]
    assert runtime.commit_count == 0
    assert runtime.rollback_count == 0


@pytest.mark.asyncio
async def test_noncanonical_success_report_id_blocks_direct_edit_before_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _CompletedReportEditRuntime()
    braced_report_id = "{" + str(runtime.report_id) + "}"
    runtime.snapshot["report_id"] = braced_report_id
    runtime.query_target_id = braced_report_id
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=4,
        endpoint="https://example.invalid/chat/completions",
    )
    completions = iter(
        (
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "load-braced-report-id",
                            "type": "function",
                            "function": {
                                "name": "query_report_by_date",
                                "arguments": json.dumps(
                                    {
                                        "date_expression": "昨天",
                                        "proposed_date": "2026-08-11",
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "edit-after-braced-report-id",
                            "type": "function",
                            "function": {
                                "name": "edit_daily_items",
                                "arguments": json.dumps(
                                    {
                                        "report_id": str(runtime.report_id),
                                        "expected_version": 4,
                                        "target_item_ids": ["tw-1"],
                                        "replacement": "完成合同终稿复核",
                                        "replacement_evidence": {
                                            "source_message_index": 1,
                                            "exact_quote": "完成合同终稿复核",
                                        },
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
        )
    )
    completion_count = 0

    async def scripted_completion(
        _messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        nonlocal completion_count
        del tool_schemas, thinking_enabled
        completion_count += 1
        if completion_count > 2:
            raise AssertionError(
                "a noncanonical report ID must be rejected before write review"
            )
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    with pytest.raises(
        DeepSeekResponseError,
        match="successful Daily query returned an inconsistent snapshot",
    ):
        await adapter.run_canary_turn(
            system_prompt="Agent2 canonical report-ID control",
            user_text="把昨天日报第一条改成：完成合同终稿复核。",
            context=_daily_write_context(
                allowed_tool_names=frozenset(
                    {"query_report_by_date", "edit_daily_items"}
                )
            ),
            runtime_session=runtime,
        )

    assert completion_count == 2
    assert runtime.executed_tools == ["query_report_by_date"]
    assert runtime.commit_count == 0
    assert runtime.rollback_count == 0


@pytest.mark.asyncio
async def test_completed_report_query_accepts_production_runtime_safe_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _CompletedReportEditRuntime()
    report = TrustedReportSnapshot(
        report_id=runtime.report_id,
        tenant_id="tenant-a",
        owner_user_id=_USER_ID,
        report_date=date(2026, 8, 11),
        version=4,
        status="completed",
        items=(
            TrustedReportItem(
                item_id="tw-1",
                field="today_work",
                content="Completed initial contract review",
                report_id=runtime.report_id,
                report_version=4,
            ),
            TrustedReportItem(
                item_id="tw-2",
                field="today_work",
                content="Organized payment materials",
                report_id=runtime.report_id,
                report_version=4,
            ),
        ),
    )
    runtime.snapshot = _safe_report_snapshot(report)
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=4,
        endpoint="https://example.invalid/chat/completions",
    )
    final_reply = "Yesterday's completed report contains two work items."
    completions = iter(
        (
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "load-production-safe-snapshot",
                            "type": "function",
                            "function": {
                                "name": "query_report_by_date",
                                "arguments": json.dumps(
                                    {
                                        "date_expression": "yesterday",
                                        "proposed_date": "2026-08-11",
                                    }
                                ),
                            },
                        }
                    ],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={"role": "assistant", "content": final_reply},
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": _follow_through_review_envelope(
                        final_reply,
                        decision="keep_no_write",
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": _review_envelope(
                        final_reply,
                        decision="keep",
                        classification="ordinary_reply",
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
        )
    )
    completion_tool_names: list[list[str]] = []

    async def scripted_completion(
        _messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        del thinking_enabled
        completion_tool_names.append(
            [schema["function"]["name"] for schema in tool_schemas]
        )
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 production snapshot compatibility",
        user_text="What was in yesterday's completed report?",
        context=_daily_write_context(
            allowed_tool_names=frozenset({"query_report_by_date"})
        ),
        runtime_session=runtime,
    )

    assert result.final_content == final_reply
    assert runtime.executed_tools == ["query_report_by_date"]
    assert runtime.commit_count == 0
    assert runtime.rollback_count == 0
    assert completion_tool_names[2] == []


@pytest.mark.parametrize(
    ("review_decision", "should_fail"),
    (("keep_clarification", False), ("continue_once", True)),
)
@pytest.mark.asyncio
async def test_multiple_report_queries_cannot_open_edit_follow_through(
    monkeypatch: pytest.MonkeyPatch,
    review_decision: str,
    should_fail: bool,
) -> None:
    runtime = _TwoCompletedReportQueryRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=4,
        endpoint="https://example.invalid/chat/completions",
    )
    clarification = "这轮查到了不止一个日期，请重新指定要修改哪一天。"
    query_calls = [
        {
            "id": f"load-completed-report-{index}",
            "type": "function",
            "function": {
                "name": "query_report_by_date",
                "arguments": json.dumps(
                    {
                        "date_expression": expression,
                        "proposed_date": proposed_date,
                    },
                    ensure_ascii=False,
                ),
            },
        }
        for index, (expression, proposed_date) in enumerate(
            (
                ("昨天", "2026-08-11"),
                ("前天", "2026-08-10"),
            ),
            start=1,
        )
    ]
    completions = iter(
        (
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": query_calls,
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={"role": "assistant", "content": clarification},
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": _follow_through_review_envelope(
                        clarification,
                        decision=review_decision,
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": _review_envelope(
                        clarification,
                        decision="keep",
                        classification="ordinary_reply",
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
        )
    )
    completion_tool_names: list[list[str]] = []
    completion_messages: list[list[dict]] = []

    async def scripted_completion(
        messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        del thinking_enabled
        completion_messages.append(messages)
        completion_tool_names.append(
            [schema["function"]["name"] for schema in tool_schemas]
        )
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    if should_fail:
        with pytest.raises(
            DeepSeekResponseError,
            match="multiple Daily queries cannot authorize write follow-through",
        ):
            await adapter.run_canary_turn(
                system_prompt="Agent2 multiple report-query control",
                user_text="把前两天的日报改一下。",
                context=_daily_write_context(
                    allowed_tool_names=frozenset(
                        {"query_report_by_date", "edit_daily_items"}
                    )
                ),
                runtime_session=runtime,
            )
        result = None
    else:
        result = await adapter.run_canary_turn(
            system_prompt="Agent2 multiple report-query control",
            user_text="把前两天的日报改一下。",
            context=_daily_write_context(
                allowed_tool_names=frozenset(
                    {"query_report_by_date", "edit_daily_items"}
                )
            ),
            runtime_session=runtime,
        )

    assert runtime.executed_tools == [
        "query_report_by_date",
        "query_report_by_date",
    ]
    assert runtime.commit_count == 0
    if result is not None:
        assert len(result.receipts) == 2
        assert result.final_content == clarification
    assert completion_tool_names[2] == []
    follow_through_payload = json.loads(completion_messages[2][1]["content"])
    assert follow_through_payload["query_state"] == "multiple"
    assert follow_through_payload["successful_query_results"] == []
    assert follow_through_payload["allowed_daily_content_write_tools"] == []


@pytest.mark.parametrize(
    ("receipt_overrides", "should_fail"),
    (
        ({}, False),
        ({"target_type": "other"}, True),
        ({"target_id": ""}, True),
        ({"target_id": "2026-08-11"}, True),
        ({"before_version": 1}, True),
        ({"after_version": 1}, True),
        (
            {
                "execution_mode": ExecutionMode.SHADOW_PROPOSAL,
                "server_evidence": {},
            },
            True,
        ),
        (
            {
                "safe_user_facts": {
                    "actual_write": False,
                    "report_found": True,
                    "report_snapshot": None,
                    "report_date": "2026-08-11",
                }
            },
            True,
        ),
        (
            {
                "safe_user_facts": {
                    "actual_write": False,
                    "report_found": False,
                    "report_snapshot": None,
                    "report_date": "",
                }
            },
            True,
        ),
        (
            {
                "safe_user_facts": {
                    "actual_write": False,
                    "report_found": False,
                    "report_snapshot": None,
                    "report_date": "not-a-date",
                }
            },
            True,
        ),
        ({"affected_item_ids": ("tw-1",)}, True),
        ({"error_code": "FORGED"}, True),
        ({"would_change": True}, True),
        ({"validation_errors": ("forged",)}, True),
    ),
)
@pytest.mark.asyncio
async def test_no_op_report_query_without_snapshot_can_answer_normally(
    monkeypatch: pytest.MonkeyPatch,
    receipt_overrides: dict,
    should_fail: bool,
) -> None:
    runtime = _NoOpReportQueryRuntime(receipt_overrides=receipt_overrides)
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=4,
        endpoint="https://example.invalid/chat/completions",
    )
    answer = "昨天没有找到日报。"
    completions = iter(
        (
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "load-missing-report",
                            "type": "function",
                            "function": {
                                "name": "query_report_by_date",
                                "arguments": json.dumps(
                                    {
                                        "date_expression": "昨天",
                                        "proposed_date": "2026-08-11",
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={"role": "assistant", "content": answer},
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": _review_envelope(
                        answer,
                        decision="keep",
                        classification="ordinary_reply",
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
        )
    )
    completion_count = 0

    async def scripted_completion(
        _messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        nonlocal completion_count
        del tool_schemas, thinking_enabled
        completion_count += 1
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    if should_fail:
        with pytest.raises(
            DeepSeekResponseError,
            match="no-op Daily query returned an inconsistent snapshot",
        ):
            await adapter.run_canary_turn(
                system_prompt="Agent2 missing report read control",
                user_text="查看我昨天的日报。",
                context=_daily_write_context(
                    allowed_tool_names=frozenset(
                        {"query_report_by_date", "edit_daily_items"}
                    )
                ),
                runtime_session=runtime,
            )
        result = None
    else:
        result = await adapter.run_canary_turn(
            system_prompt="Agent2 missing report read control",
            user_text="查看我昨天的日报。",
            context=_daily_write_context(
                allowed_tool_names=frozenset(
                    {"query_report_by_date", "edit_daily_items"}
                )
            ),
            runtime_session=runtime,
        )

    assert runtime.executed_tools == ["query_report_by_date"]
    assert completion_count == (2 if should_fail else 3)
    if result is not None:
        assert result.final_content == answer


@pytest.mark.asyncio
async def test_qualified_query_without_exposed_write_tool_fails_if_write_is_unfulfilled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _CompletedReportEditRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=4,
        endpoint="https://example.invalid/chat/completions",
    )
    loaded_only_reply = "我已经查到昨天的日报内容。"
    completions = iter(
        (
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "load-without-exposed-write",
                            "type": "function",
                            "function": {
                                "name": "query_report_by_date",
                                "arguments": json.dumps(
                                    {
                                        "date_expression": "昨天",
                                        "proposed_date": "2026-08-11",
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={"role": "assistant", "content": loaded_only_reply},
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": _follow_through_review_envelope(
                        loaded_only_reply,
                        decision="continue_once",
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
        )
    )
    completion_tool_names: list[list[str]] = []

    async def scripted_completion(
        _messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        del thinking_enabled
        completion_tool_names.append(
            [schema["function"]["name"] for schema in tool_schemas]
        )
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    with pytest.raises(
        DeepSeekResponseError,
        match="daily content follow-through has no allowed write tool",
    ):
        await adapter.run_canary_turn(
            system_prompt="Agent2 unavailable content-write control",
            user_text="把昨天日报今日工作的第一条改成：完成合同终稿复核。",
            context=_daily_write_context(
                allowed_tool_names=frozenset({"query_report_by_date"})
            ),
            runtime_session=runtime,
        )

    assert completion_tool_names[2] == []
    assert runtime.executed_tools == ["query_report_by_date"]


@pytest.mark.asyncio
async def test_follow_through_review_clarification_rejects_labeled_version_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _CompletedReportEditRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=4,
        endpoint="https://example.invalid/chat/completions",
    )
    loaded_only_reply = "我已经查到昨天的日报内容。"
    leaking_clarification = "请补充新内容（expected_version=4）。"
    edit_call = {
        "id": "draft-edit-before-leaking-clarification",
        "type": "function",
        "function": {
            "name": "edit_daily_items",
            "arguments": json.dumps(
                {
                    "report_id": str(runtime.report_id),
                    "expected_version": 4,
                    "target_item_ids": ["tw-1"],
                    "replacement": "改一下",
                    "replacement_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "改一下",
                    },
                },
                ensure_ascii=False,
            ),
        },
    }
    completions = iter(
        (
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "load-before-leaking-review-clarification",
                            "type": "function",
                            "function": {
                                "name": "query_report_by_date",
                                "arguments": json.dumps(
                                    {
                                        "date_expression": "昨天",
                                        "proposed_date": "2026-08-11",
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={"role": "assistant", "content": loaded_only_reply},
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": _follow_through_review_envelope(
                        loaded_only_reply,
                        decision="continue_once",
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [edit_call],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "decision": "clarification",
                            "reply": leaking_clarification,
                        },
                        ensure_ascii=False,
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
        )
    )

    async def scripted_completion(
        _messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        del tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    with pytest.raises(
        DeepSeekResponseError,
        match="terminal reply exposed internal Daily identifiers",
    ):
        await adapter.run_canary_turn(
            system_prompt="Agent2 review clarification identifier leak control",
            user_text="把昨天日报今日工作的第一条改一下。",
            context=_daily_write_context(
                allowed_tool_names=frozenset(
                    {"query_report_by_date", "edit_daily_items"}
                )
            ),
            runtime_session=runtime,
        )

    assert runtime.executed_tools == ["query_report_by_date"]
    assert runtime.commit_count == 0


@pytest.mark.parametrize(
    ("second_review_decision", "should_fail"),
    (
        ("keep_clarification", False),
        ("keep_no_write", True),
        ("continue_once", True),
        (None, True),
    ),
)
@pytest.mark.asyncio
async def test_follow_through_keeps_strict_clarification_from_existing_edit_review(
    monkeypatch: pytest.MonkeyPatch,
    second_review_decision: str | None,
    should_fail: bool,
) -> None:
    runtime = _CompletedReportEditRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=4,
        endpoint="https://example.invalid/chat/completions",
    )
    loaded_only_reply = "我已经查到昨天的日报内容。"
    clarification = "请只提供要替换成的新内容，不要把修改指令混在内容里。"
    edit_call = {
        "id": "draft-edit-needs-clarification",
        "type": "function",
        "function": {
            "name": "edit_daily_items",
            "arguments": json.dumps(
                {
                    "report_id": str(runtime.report_id),
                    "expected_version": 4,
                    "target_item_ids": ["tw-1"],
                    "replacement": "改一下",
                    "replacement_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "改一下",
                    },
                },
                ensure_ascii=False,
            ),
        },
    }
    completions = iter(
        (
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "load-before-review-clarification",
                            "type": "function",
                            "function": {
                                "name": "query_report_by_date",
                                "arguments": json.dumps(
                                    {
                                        "date_expression": "昨天",
                                        "proposed_date": "2026-08-11",
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={"role": "assistant", "content": loaded_only_reply},
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": _follow_through_review_envelope(
                        loaded_only_reply,
                        decision="continue_once",
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [edit_call],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "decision": "clarification",
                            "reply": clarification,
                        },
                        ensure_ascii=False,
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": (
                        "not-json"
                        if second_review_decision is None
                        else _follow_through_review_envelope(
                            clarification,
                            decision=second_review_decision,
                        )
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": _review_envelope(
                        clarification,
                        decision="keep",
                        classification="ordinary_reply",
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
        )
    )

    async def scripted_completion(
        _messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        del tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    if should_fail:
        with pytest.raises(DeepSeekResponseError):
            await adapter.run_canary_turn(
                system_prompt="Agent2 strict edit clarification control",
                user_text="把昨天日报今日工作的第一条改一下。",
                context=_daily_write_context(
                    allowed_tool_names=frozenset(
                        {"query_report_by_date", "edit_daily_items"}
                    )
                ),
                runtime_session=runtime,
            )
        result = None
    else:
        result = await adapter.run_canary_turn(
            system_prompt="Agent2 strict edit clarification control",
            user_text="把昨天日报今日工作的第一条改一下。",
            context=_daily_write_context(
                allowed_tool_names=frozenset(
                    {"query_report_by_date", "edit_daily_items"}
                )
            ),
            runtime_session=runtime,
        )

    assert runtime.executed_tools == ["query_report_by_date"]
    assert runtime.commit_count == 0
    if result is not None:
        assert result.final_content == clarification


@pytest.mark.asyncio
async def test_direct_edit_review_clarification_cannot_reopen_an_unreviewed_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _CompletedReportEditRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=4,
        endpoint="https://example.invalid/chat/completions",
    )
    edit_call = {
        "id": "direct-edit-before-clarification",
        "type": "function",
        "function": {
            "name": "edit_daily_items",
            "arguments": json.dumps(
                {
                    "report_id": str(runtime.report_id),
                    "expected_version": 4,
                    "target_item_ids": ["tw-1"],
                    "replacement": "完成合同终稿复核",
                    "replacement_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "完成合同终稿复核",
                    },
                },
                ensure_ascii=False,
            ),
        },
    }
    clarification = "请补充要替换成的准确内容。"
    final_reply = "已修改昨天日报第一条。"
    completions = iter(
        (
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "load-before-direct-edit-clarification",
                            "type": "function",
                            "function": {
                                "name": "query_report_by_date",
                                "arguments": json.dumps(
                                    {
                                        "date_expression": "昨天",
                                        "proposed_date": "2026-08-11",
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={"role": "assistant", "content": None, "tool_calls": [edit_call]},
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": json.dumps(
                        {"decision": "clarification", "reply": clarification},
                        ensure_ascii=False,
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": _follow_through_review_envelope(
                        clarification,
                        decision="continue_once",
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{**edit_call, "id": "unreviewed-reopened-edit"}],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "reply": final_reply,
                            "actual_write": True,
                            "operation_outcome": "changed",
                        },
                        ensure_ascii=False,
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
        )
    )
    completion_count = 0

    async def scripted_completion(
        _messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        nonlocal completion_count
        del tool_schemas, thinking_enabled
        completion_count += 1
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    with pytest.raises(
        DeepSeekResponseError,
        match="clarification was not independently confirmed",
    ):
        await adapter.run_canary_turn(
            system_prompt="Agent2 direct-edit clarification closure",
            user_text="把昨天日报第一条改成：完成合同终稿复核。",
            context=_daily_write_context(
                allowed_tool_names=frozenset(
                    {"query_report_by_date", "edit_daily_items"}
                )
            ),
            runtime_session=runtime,
        )

    assert completion_count == 4
    assert runtime.executed_tools == ["query_report_by_date"]
    assert runtime.commit_count == 0
    assert runtime.rollback_count == 0


@pytest.mark.asyncio
async def test_follow_through_reviewer_cannot_leak_ids_or_expand_its_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _CompletedReportEditRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=4,
        endpoint="https://example.invalid/chat/completions",
    )
    loaded_only_reply = "我已经查到昨天的日报内容。"
    completions = iter(
        (
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "load-before-invalid-review",
                            "type": "function",
                            "function": {
                                "name": "query_report_by_date",
                                "arguments": json.dumps(
                                    {
                                        "date_expression": "昨天",
                                        "proposed_date": "2026-08-11",
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={"role": "assistant", "content": loaded_only_reply},
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "decision": "continue_once",
                            "reviewed_reply_sha256": hashlib.sha256(
                                loaded_only_reply.encode("utf-8")
                            ).hexdigest(),
                            "report_id": str(runtime.report_id),
                        }
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
        )
    )

    async def scripted_completion(
        _messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        del thinking_enabled
        if tool_schemas:
            assert all(
                schema["function"]["name"]
                in {"query_report_by_date", "edit_daily_items"}
                for schema in tool_schemas
            )
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    with pytest.raises(
        DeepSeekResponseError,
        match="daily content follow-through review failed",
    ):
        await adapter.run_canary_turn(
            system_prompt="Agent2 follow-through envelope control",
            user_text="把昨天日报今日工作的第一条改成：完成合同终稿复核。",
            context=_daily_write_context(
                allowed_tool_names=frozenset(
                    {"query_report_by_date", "edit_daily_items"}
                )
            ),
            runtime_session=runtime,
        )

    assert runtime.executed_tools == ["query_report_by_date"]
    assert runtime.commit_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unexpected_calls",
    (
        (
            {
                "id": "unexpected-second-read",
                "type": "function",
                "function": {
                    "name": "query_report_by_date",
                    "arguments": json.dumps(
                        {
                            "date_expression": "今天",
                            "proposed_date": "2026-08-12",
                        },
                        ensure_ascii=False,
                    ),
                },
            },
        ),
        (
            {
                "id": "unexpected-cross-domain-write",
                "type": "function",
                "function": {
                    "name": "remember_personal_memory",
                    "arguments": json.dumps(
                        {
                            "memory_key": "report.daily_reminders_enabled",
                            "value": {"enabled": False},
                            "source_evidence": {
                                "source_message_index": 1,
                                "intent": "explicit_preference",
                            },
                        }
                    ),
                },
            },
        ),
        (
            {
                "id": "allowed-edit-in-mixed-batch",
                "type": "function",
                "function": {
                    "name": "edit_daily_items",
                    "arguments": json.dumps(
                        {
                            "report_id": "20000000-0000-4000-8000-000000000004",
                            "expected_version": 4,
                            "target_item_ids": ["tw-1"],
                            "replacement": "完成合同终稿复核",
                            "replacement_evidence": {
                                "source_message_index": 1,
                                "exact_quote": "完成合同终稿复核",
                            },
                        },
                        ensure_ascii=False,
                    ),
                },
            },
            {
                "id": "cross-domain-write-in-mixed-batch",
                "type": "function",
                "function": {
                    "name": "remember_personal_memory",
                    "arguments": json.dumps(
                        {
                            "memory_key": "report.daily_reminders_enabled",
                            "value": {"enabled": False},
                            "source_evidence": {
                                "source_message_index": 1,
                                "intent": "explicit_preference",
                            },
                        }
                    ),
                },
            },
        ),
    ),
)
async def test_follow_through_rejects_hidden_reads_and_cross_domain_writes(
    monkeypatch: pytest.MonkeyPatch,
    unexpected_calls: tuple[dict, ...],
) -> None:
    runtime = _CompletedReportEditRuntime()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=4,
        endpoint="https://example.invalid/chat/completions",
    )
    loaded_only_reply = "我已经查到昨天的日报内容。"
    completions = iter(
        (
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "load-before-out-of-scope-call",
                            "type": "function",
                            "function": {
                                "name": "query_report_by_date",
                                "arguments": json.dumps(
                                    {
                                        "date_expression": "昨天",
                                        "proposed_date": "2026-08-11",
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _CompletionResponse(
                message={"role": "assistant", "content": loaded_only_reply},
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": _follow_through_review_envelope(
                        loaded_only_reply,
                        decision="continue_once",
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": list(unexpected_calls),
                },
                metadata={"finish_reason": "tool_calls"},
            ),
        )
    )
    completion_tool_names: list[list[str]] = []

    async def scripted_completion(
        _messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        del thinking_enabled
        completion_tool_names.append(
            [schema["function"]["name"] for schema in tool_schemas]
        )
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    with pytest.raises(
        DeepSeekResponseError,
        match="daily content follow-through selected a disallowed tool",
    ):
        await adapter.run_canary_turn(
            system_prompt="Agent2 follow-through tool-scope control",
            user_text="把昨天日报今日工作的第一条改成：完成合同终稿复核。",
            context=_daily_write_context(
                allowed_tool_names=frozenset(
                    {
                        "query_report_by_date",
                        "edit_daily_items",
                        "remember_personal_memory",
                    }
                )
            ),
            runtime_session=runtime,
        )

    assert completion_tool_names[2] == []
    assert completion_tool_names[3] == ["edit_daily_items"]
    assert runtime.executed_tools == ["query_report_by_date"]
    assert runtime.commit_count == 0


@pytest.mark.asyncio
async def test_public_agent2_turn_keeps_an_invitation_bound_to_a_real_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending_id = UUID("30000000-0000-4000-8000-000000000004")
    now = datetime(2026, 8, 12, 16, 42, tzinfo=ZoneInfo("Asia/Shanghai"))
    pending = TrustedClearPending(
        pending_id=pending_id,
        namespace=CANARY_STATE_NAMESPACE,
        tenant_id="tenant-a",
        user_id=_USER_ID,
        conversation_id="direct-user-a",
        report_id=UUID("20000000-0000-4000-8000-000000000004"),
        report_version=3,
        target_date=date(2026, 8, 12),
        expires_at=now + timedelta(minutes=10),
        source_message_id="message-previous",
    )
    report = TrustedReportSnapshot(
        report_id=pending.report_id,
        tenant_id="tenant-a",
        owner_user_id=_USER_ID,
        report_date=pending.target_date,
        version=pending.report_version,
        status="collecting",
    )
    answer = "清空待确认仍然有效；如果确定清空，回复确认即可。"
    pending_reference = "pending_1"
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    completions = iter(
        (
            answer,
            _review_envelope(
                answer,
                decision="keep",
                classification="matched_persisted_pending",
                pending_reference=pending_reference,
            ),
        )
    )
    completion_count = 0

    review_requests: list[list[dict]] = []

    async def scripted_completion(
        messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        nonlocal completion_count
        del tool_schemas, thinking_enabled
        completion_count += 1
        review_requests.append(messages)
        return _CompletionResponse(
            message={"role": "assistant", "content": next(completions)},
            metadata={"finish_reason": "stop"},
        )

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 public turn regression",
        user_text="我先想一下。",
        context=_daily_write_context(
            active_clear_pending=pending,
            today_report=report,
            allowed_tool_names=frozenset({"confirm_clear_report"}),
        ),
        runtime_session=_NoWriteRuntime(),
    )

    assert completion_count == 2
    assert result.final_content == answer
    assert result.receipts == ()
    assert result.runtime_results == ()
    review_facts = json.loads(review_requests[1][1]["content"])
    serialized_review_facts = review_requests[1][1]["content"]
    assert review_facts["persisted_pending"] == [
        {
            "allows_bare_confirmation": True,
            "executable_now": True,
            "expires_at": pending.expires_at.isoformat(),
            "pending_kind": "daily_report_clear_confirmation",
            "pending_reference": "pending_1",
            "provenance": "server_pending",
            "target": {"report_date": "2026-08-12"},
        }
    ]
    assert str(pending.pending_id) not in serialized_review_facts
    assert str(pending.report_id) not in serialized_review_facts
    assert pending.source_message_id not in serialized_review_facts


@pytest.mark.asyncio
async def test_stale_clear_pending_cannot_support_a_bare_confirmation_promise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending_id = UUID("30000000-0000-4000-8000-000000000014")
    report_id = UUID("20000000-0000-4000-8000-000000000014")
    now = datetime(2026, 8, 12, 16, 42, tzinfo=ZoneInfo("Asia/Shanghai"))
    pending = TrustedClearPending(
        pending_id=pending_id,
        namespace=CANARY_STATE_NAMESPACE,
        tenant_id="tenant-a",
        user_id=_USER_ID,
        conversation_id="direct-user-a",
        report_id=report_id,
        report_version=3,
        target_date=date(2026, 8, 12),
        expires_at=now + timedelta(minutes=10),
        source_message_id="message-previous",
    )
    changed_report = TrustedReportSnapshot(
        report_id=report_id,
        tenant_id="tenant-a",
        owner_user_id=_USER_ID,
        report_date=date(2026, 8, 12),
        version=4,
        status="collecting",
    )
    answer = "清空待确认仍然有效；回复确认即可清空。"
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    completions = iter(
        (
            answer,
            _review_envelope(
                answer,
                decision="keep",
                classification="matched_persisted_pending",
                pending_reference="pending_1",
            ),
        )
    )

    async def scripted_completion(
        _messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        del tool_schemas, thinking_enabled
        return _CompletionResponse(
            message={"role": "assistant", "content": next(completions)},
            metadata={"finish_reason": "stop"},
        )

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    with pytest.raises(
        DeepSeekResponseError,
        match="zero-tool write invitation review failed",
    ):
        await adapter.run_canary_turn(
            system_prompt="Agent2 public turn regression",
            user_text="我先想一下。",
            context=_daily_write_context(
                active_clear_pending=pending,
                today_report=changed_report,
                allowed_tool_names=frozenset({"confirm_clear_report"}),
            ),
            runtime_session=_NoWriteRuntime(),
        )


@pytest.mark.asyncio
async def test_public_agent2_turn_fails_closed_when_promise_review_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unsafe_reply = "回复确认即可把这段内容写入日报。"
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    completions = iter((unsafe_reply, "这不是结构化复核结果"))

    async def scripted_completion(
        _messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        del tool_schemas, thinking_enabled
        return _CompletionResponse(
            message={"role": "assistant", "content": next(completions)},
            metadata={"finish_reason": "stop"},
        )

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    with pytest.raises(
        DeepSeekResponseError,
        match="zero-tool write invitation review failed",
    ):
        await adapter.run_canary_turn(
            system_prompt="Agent2 public turn regression",
            user_text="这段话怎么说更清楚？",
            context=_daily_write_context(),
            runtime_session=_NoWriteRuntime(),
        )


@pytest.mark.asyncio
async def test_reviewer_replacement_must_pass_one_independent_terminal_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A safety reviewer's own wording cannot become an unreviewed promise."""

    original_reply = "我整理好了，回复确认即可写入日报。"
    unsafe_replacement = "你下轮只要回复确认，我就会把它写入日报。"
    safe_replacement = "这轮没有保存日报；如需写入，请重新说明完整内容。"
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    completions = iter(
        (
            original_reply,
            _review_envelope(
                original_reply,
                decision="replace",
                classification="unbacked_future_write_invitation",
                replacement_reply=unsafe_replacement,
            ),
            _review_envelope(
                unsafe_replacement,
                decision="replace",
                classification="unbacked_future_write_invitation",
                replacement_reply=safe_replacement,
            ),
        )
    )
    completion_count = 0

    async def scripted_completion(
        _messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        nonlocal completion_count
        del thinking_enabled
        assert tool_schemas == [] or completion_count == 0
        completion_count += 1
        return _CompletionResponse(
            message={"role": "assistant", "content": next(completions)},
            metadata={"finish_reason": "stop"},
        )

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    with pytest.raises(
        DeepSeekResponseError,
        match="replacement safety review failed",
    ):
        await adapter.run_canary_turn(
            system_prompt="Agent2 public turn regression",
            user_text="只帮我润色，不要写日报。",
            context=_daily_write_context(),
            runtime_session=_NoWriteRuntime(),
        )

    assert completion_count == 3


@pytest.mark.asyncio
async def test_promise_review_cannot_claim_an_unrelated_pending_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending_id = UUID("30000000-0000-4000-8000-000000000005")
    now = datetime(2026, 8, 12, 16, 42, tzinfo=ZoneInfo("Asia/Shanghai"))
    pending = TrustedClearPending(
        pending_id=pending_id,
        namespace=CANARY_STATE_NAMESPACE,
        tenant_id="tenant-a",
        user_id=_USER_ID,
        conversation_id="direct-user-a",
        report_id=UUID("20000000-0000-4000-8000-000000000005"),
        report_version=2,
        target_date=date(2026, 8, 12),
        expires_at=now + timedelta(minutes=10),
        source_message_id="message-previous",
    )
    report = TrustedReportSnapshot(
        report_id=pending.report_id,
        tenant_id="tenant-a",
        owner_user_id=_USER_ID,
        report_date=pending.target_date,
        version=pending.report_version,
        status="collecting",
    )
    answer = "如果确定清空，回复确认即可。"
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    completions = iter(
        (
            answer,
            _review_envelope(
                answer,
                decision="keep",
                classification="matched_persisted_pending",
                pending_reference="pending_999",
            ),
        )
    )

    async def scripted_completion(
        _messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        del tool_schemas, thinking_enabled
        return _CompletionResponse(
            message={"role": "assistant", "content": next(completions)},
            metadata={"finish_reason": "stop"},
        )

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    with pytest.raises(
        DeepSeekResponseError,
        match="zero-tool write invitation review failed",
    ):
        await adapter.run_canary_turn(
            system_prompt="Agent2 public turn regression",
            user_text="我先想一下。",
            context=_daily_write_context(
                active_clear_pending=pending,
                today_report=report,
                allowed_tool_names=frozenset({"confirm_clear_report"}),
            ),
            runtime_session=_NoWriteRuntime(),
        )


@pytest.mark.asyncio
async def test_zero_tool_gate_runs_when_no_write_tool_is_exposed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unsafe_reply = "下轮只回复确认，我就替你写入。"
    safe_reply = "这轮没有执行或保存任何业务修改。"
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    completions = iter(
        (
            unsafe_reply,
            _review_envelope(
                unsafe_reply,
                decision="replace",
                classification="unbacked_future_write_invitation",
                replacement_reply=safe_reply,
            ),
            _review_envelope(
                safe_reply,
                decision="keep",
                classification="ordinary_reply",
            ),
        )
    )
    completion_count = 0

    async def scripted_completion(
        _messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        nonlocal completion_count
        del thinking_enabled
        completion_count += 1
        if completion_count > 1:
            assert tool_schemas == []
        return _CompletionResponse(
            message={"role": "assistant", "content": next(completions)},
            metadata={"finish_reason": "stop"},
        )

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 public turn regression",
        user_text="只回答我的问题，不要执行任何操作。",
        context=_daily_write_context(allowed_tool_names=frozenset()),
        runtime_session=_NoWriteRuntime(),
    )

    assert result.final_content == safe_reply
    assert completion_count == 3
    assert result.receipts == ()


@pytest.mark.asyncio
async def test_review_hash_must_bind_the_exact_terminal_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer = "普通回答。"
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    completions = iter(
        (
            answer,
            _review_envelope(
                "另一段文字",
                decision="keep",
                classification="ordinary_reply",
            ),
        )
    )

    async def scripted_completion(
        _messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        del tool_schemas, thinking_enabled
        return _CompletionResponse(
            message={"role": "assistant", "content": next(completions)},
            metadata={"finish_reason": "stop"},
        )

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    with pytest.raises(
        DeepSeekResponseError,
        match="zero-tool write invitation review failed",
    ):
        await adapter.run_canary_turn(
            system_prompt="Agent2 public turn regression",
            user_text="解释一下。",
            context=_daily_write_context(allowed_tool_names=frozenset()),
            runtime_session=_NoWriteRuntime(),
        )


@pytest.mark.asyncio
async def test_reviewer_replacement_cannot_leak_a_temporary_pending_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 12, 16, 42, tzinfo=ZoneInfo("Asia/Shanghai"))
    pending = TrustedClearPending(
        pending_id=UUID("30000000-0000-4000-8000-000000000024"),
        namespace=CANARY_STATE_NAMESPACE,
        tenant_id="tenant-a",
        user_id=_USER_ID,
        conversation_id="direct-user-a",
        report_id=UUID("20000000-0000-4000-8000-000000000024"),
        report_version=1,
        target_date=date(2026, 8, 12),
        expires_at=now + timedelta(minutes=10),
        source_message_id="message-previous",
    )
    report = TrustedReportSnapshot(
        report_id=pending.report_id,
        tenant_id="tenant-a",
        owner_user_id=_USER_ID,
        report_date=pending.target_date,
        version=pending.report_version,
        status="collecting",
    )
    original_reply = "下轮回复确认就写入。"
    leaking_replacement = "这轮未写入；内部引用 pending_1。"
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    completions = iter(
        (
            original_reply,
            _review_envelope(
                original_reply,
                decision="replace",
                classification="unbacked_future_write_invitation",
                replacement_reply=leaking_replacement,
            ),
        )
    )
    completion_count = 0

    async def scripted_completion(
        _messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        nonlocal completion_count
        del tool_schemas, thinking_enabled
        completion_count += 1
        return _CompletionResponse(
            message={"role": "assistant", "content": next(completions)},
            metadata={"finish_reason": "stop"},
        )

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    with pytest.raises(
        DeepSeekResponseError,
        match="zero-tool write invitation review failed",
    ):
        await adapter.run_canary_turn(
            system_prompt="Agent2 public turn regression",
            user_text="先别改。",
            context=_daily_write_context(
                active_clear_pending=pending,
                today_report=report,
                allowed_tool_names=frozenset({"confirm_clear_report"}),
            ),
            runtime_session=_NoWriteRuntime(),
        )
    assert completion_count == 2


@pytest.mark.asyncio
async def test_multiple_eligible_pending_entries_require_one_exact_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def complete_report(report_id: UUID, report_date: date) -> TrustedReportSnapshot:
        return TrustedReportSnapshot(
            report_id=report_id,
            tenant_id="tenant-a",
            owner_user_id=_USER_ID,
            report_date=report_date,
            version=2,
            status="pending_confirmation",
            acknowledged_empty_fields=frozenset(
                {"today_work", "problems", "tomorrow_plan"}
            ),
        )

    today = complete_report(
        UUID("20000000-0000-4000-8000-000000000031"),
        date(2026, 8, 12),
    )
    historical = complete_report(
        UUID("20000000-0000-4000-8000-000000000032"),
        date(2026, 8, 11),
    )
    context = _daily_write_context(
        today_report=today,
        allowed_tool_names=frozenset({"confirm_report"}),
    ).model_copy(update={"historical_reports": (historical,)})
    answer = "第二份待确认日报仍可在下一轮明确确认。"
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    completions = iter(
        (
            answer,
            _review_envelope(
                answer,
                decision="keep",
                classification="matched_persisted_pending",
                pending_reference="pending_2",
            ),
        )
    )
    review_facts: dict[str, object] = {}

    async def scripted_completion(
        messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        del tool_schemas, thinking_enabled
        if len(messages) == 2 and "persisted_pending" in messages[1]["content"]:
            review_facts.update(json.loads(messages[1]["content"]))
        return _CompletionResponse(
            message={"role": "assistant", "content": next(completions)},
            metadata={"finish_reason": "stop"},
        )

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 public turn regression",
        user_text="先不提交，告诉我稍后怎么处理第二份。",
        context=context,
        runtime_session=_NoWriteRuntime(),
    )

    assert result.final_content == answer
    assert [
        item["pending_reference"]
        for item in review_facts["persisted_pending"]
        if item["executable_now"]
    ] == ["pending_1", "pending_2"]


@pytest.mark.asyncio
async def test_incomplete_daily_pending_cannot_support_bare_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = TrustedReportSnapshot(
        report_id=UUID("20000000-0000-4000-8000-000000000041"),
        tenant_id="tenant-a",
        owner_user_id=_USER_ID,
        report_date=date(2026, 8, 12),
        version=1,
        status="pending_confirmation",
        acknowledged_empty_fields=frozenset({"today_work"}),
    )
    answer = "这份日报稍后回复确认即可提交。"
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    completions = iter(
        (
            answer,
            _review_envelope(
                answer,
                decision="keep",
                classification="matched_persisted_pending",
                pending_reference="pending_1",
            ),
        )
    )

    async def scripted_completion(
        _messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        del tool_schemas, thinking_enabled
        return _CompletionResponse(
            message={"role": "assistant", "content": next(completions)},
            metadata={"finish_reason": "stop"},
        )

    monkeypatch.setattr(adapter, "_complete", scripted_completion)

    with pytest.raises(
        DeepSeekResponseError,
        match="zero-tool write invitation review failed",
    ):
        await adapter.run_canary_turn(
            system_prompt="Agent2 public turn regression",
            user_text="先不提交。",
            context=_daily_write_context(
                today_report=report,
                allowed_tool_names=frozenset({"confirm_report"}),
            ),
            runtime_session=_NoWriteRuntime(),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("now", "day_state"),
    (
        (
            datetime(2026, 8, 12, 16, 42, tzinfo=ZoneInfo("Asia/Shanghai")),
            "unfilled",
        ),
        (
            datetime(2026, 8, 18, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
            "explicitly_empty",
        ),
    ),
)
async def test_unresolved_or_late_weekly_pending_cannot_support_bare_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    now: datetime,
    day_state: str,
) -> None:
    week_start = date(2026, 8, 17)
    plan = TrustedWeeklyPlanContext(
        plan_id="plan-pending-1",
        batch_id="batch-pending-1",
        tenant_id="tenant-a",
        owner_user_id=str(_USER_ID),
        target_week_start=week_start,
        version=2,
        status="pending_confirmation",
        days=tuple(
            TrustedWeeklyPlanDay(
                day_id=f"day-{offset}",
                plan_date=week_start + timedelta(days=offset),
                state=day_state,
            )
            for offset in range(6)
        ),
    )
    answer = "这份周计划稍后回复确认即可提交。"
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    completions = iter(
        (
            answer,
            _review_envelope(
                answer,
                decision="keep",
                classification="matched_persisted_pending",
                pending_reference="pending_1",
            ),
        )
    )

    async def scripted_completion(
        _messages,
        *,
        tool_schemas,
        thinking_enabled,
    ) -> _CompletionResponse:
        del tool_schemas, thinking_enabled
        return _CompletionResponse(
            message={"role": "assistant", "content": next(completions)},
            metadata={"finish_reason": "stop"},
        )

    monkeypatch.setattr(adapter, "_complete", scripted_completion)
    base = _daily_write_context(
        allowed_tool_names=frozenset({"submit_next_weekly_plan"})
    )
    context = base.model_copy(
        update={
            "now": now,
            "today_report": None,
            "weekly_plan": plan,
        }
    )

    with pytest.raises(
        DeepSeekResponseError,
        match="zero-tool write invitation review failed",
    ):
        await adapter.run_canary_turn(
            system_prompt="Agent2 public turn regression",
            user_text="先不提交。",
            context=context,
            runtime_session=_NoWriteRuntime(),
        )
