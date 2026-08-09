import json
from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
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
    DeepSeekResponseError,
    DeepSeekTimeoutError,
    DeepSeekToolCallingAdapter,
    MalformedToolCallError,
    _canary_post_write_protocol_message,
    _CompletionResponse,
)
from app.agent2.tool_calling.production_contracts import (
    ProductionRuntimeResult,
)
from app.agent2.tool_calling.write_reply import (
    expected_write_outcome,
    validate_write_reply,
    write_reply_retry_instruction,
)


def _context() -> TrustedContext:
    return TrustedContext(
        namespace=CANARY_STATE_NAMESPACE,
        now=datetime(2026, 8, 9, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        principal=TrustedPrincipal(
            tenant_id="test-tenant",
            user_id=UUID("10000000-0000-0000-0000-000000000001"),
            conversation_id="test-conversation",
            source_message_id="test-message",
            timezone="Asia/Shanghai",
            display_name="测试用户",
        ),
        allowed_tool_names=frozenset({"add_daily_items"}),
        gate_decisions={"add_daily_items": True},
    )


def _changed_receipt() -> ToolReceipt:
    return ToolReceipt(
        status=ReceiptStatus.SUCCESS,
        tool_name="add_daily_items",
        changed=True,
        target_type="daily_report",
        target_id="test-report",
        before_version=0,
        after_version=1,
        affected_item_ids=("test-item",),
        safe_user_facts={
            "actual_write": True,
            "report_date": "2026-08-09",
            "report_status": "collecting",
            "affected_item_ids": ["test-item"],
        },
        execution_mode=ExecutionMode.CANARY_EXECUTE,
    )


def test_write_reply_retry_exposes_server_fields_as_exact_copy_values() -> None:
    instruction = json.loads(
        write_reply_retry_instruction(
            ("operation_outcome does not match server receipts",),
            (_changed_receipt(),),
        )
    )["write_reply_retry"]

    assert instruction["required_exact_fields"] == {
        "actual_write": True,
        "operation_outcome": "changed",
    }
    assert "Copy required_exact_fields unchanged" in instruction["instruction"]
    assert "Do not return blank text" in instruction["instruction"]


def _blocked_receipt() -> ToolReceipt:
    return ToolReceipt(
        status=ReceiptStatus.BLOCKED,
        tool_name="add_daily_items",
        changed=False,
        safe_user_facts={
            "actual_write": False,
            "error_code": "SOURCE_REPORT_DATE_MISMATCH",
            "report_date": "2026-08-09",
        },
        error_code="SOURCE_REPORT_DATE_MISMATCH",
        execution_mode=ExecutionMode.CANARY_EXECUTE,
    )


def test_independent_mixed_write_receipts_are_reported_as_partial() -> None:
    receipts = (_changed_receipt(), _blocked_receipt())
    content = json.dumps(
        {
            "reply": "第一项已写入，第二项没有执行。",
            "actual_write": True,
            "operation_outcome": "partial",
        },
        ensure_ascii=False,
    )

    envelope, errors = validate_write_reply(content, receipts)

    assert expected_write_outcome(receipts) == "partial"
    assert errors == ()
    assert envelope is not None
    assert envelope.operation_outcome == "partial"


class _DeferredRuntimeSession:
    mode = ExecutionMode.CANARY_EXECUTE

    def __init__(self) -> None:
        self.commit_count = 0
        self.rollback_count = 0
        self.execute_count = 0

    async def execute(self, calls, *, defer_finalization):
        self.execute_count += 1
        assert defer_finalization is True
        assert len(calls) == 1
        return ProductionRuntimeResult(
            status="success",
            receipts=(_changed_receipt(),),
            transaction_opened=True,
            transaction_pending=True,
            handler_call_count=1,
        )

    async def commit_pending(self):
        self.commit_count += 1
        return ProductionRuntimeResult(
            status="success",
            receipts=(_changed_receipt(),),
            transaction_opened=True,
            committed_to_outer_transaction=True,
            handler_call_count=1,
            business_write_count=1,
            receipt_write_count=1,
        )

    async def rollback_pending(self):
        self.rollback_count += 1


class _BlockedRuntimeSession:
    mode = ExecutionMode.CANARY_EXECUTE

    def __init__(self) -> None:
        self.commit_count = 0
        self.rollback_count = 0

    async def execute(self, calls, *, defer_finalization):
        assert defer_finalization is True
        return ProductionRuntimeResult(
            status="blocked",
            receipts=(_blocked_receipt(),),
        )

    async def commit_pending(self):
        self.commit_count += 1
        raise AssertionError("a blocked turn has nothing to commit")

    async def rollback_pending(self):
        self.rollback_count += 1


@pytest.mark.asyncio
async def test_terminal_write_reply_requests_native_json_output() -> None:
    class CapturingHttpClient:
        def __init__(self) -> None:
            self.payload = None

        async def post(self, url, **kwargs):
            self.payload = kwargs["json"]
            return httpx.Response(
                200,
                json={
                    "id": "response-1",
                    "model": "model-1",
                    "created": 1,
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(
                                    {
                                        "reply": "已按原话记入今天的日报。",
                                        "actual_write": True,
                                        "operation_outcome": "changed",
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                    "usage": {},
                },
                request=httpx.Request("POST", url),
            )

    http_client = CapturingHttpClient()
    adapter = DeepSeekToolCallingAdapter(
        http_client=http_client,
        model="model-1",
        timeout_seconds=3.0,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )

    await adapter._complete(
        messages=[
            {"role": "user", "content": "记录今天日报"},
            _canary_post_write_protocol_message((_changed_receipt(),)),
        ],
        tool_schemas=[],
        thinking_enabled=False,
    )

    assert http_client.payload["response_format"] == {"type": "json_object"}


def _tool_call_completion() -> _CompletionResponse:
    return _CompletionResponse(
        message={
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "add_daily_items",
                        "arguments": json.dumps(
                            {
                                "date_expression": "今天",
                                "proposed_date": "2026-08-09",
                                "items": [
                                    {
                                        "field": "today_work",
                                        "content": "虚构测试事项",
                                        "source_evidence": {
                                            "source_message_index": 1,
                                        },
                                    }
                                ],
                            },
                            ensure_ascii=False,
                        ),
                    },
                }
            ],
        },
        metadata={"finish_reason": "tool_calls"},
    )


def _malformed_tool_call_completion() -> _CompletionResponse:
    return _CompletionResponse(
        message={
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "malformed-call-1",
                    "type": "function",
                    "function": {
                        "name": "add_daily_items",
                        "arguments": (
                            '{"date_expression":"今天","items":['
                            '{"content":"老板说"要么降薪，要么裁员""}]}'
                        ),
                    },
                }
            ],
        },
        metadata={"finish_reason": "tool_calls"},
    )


@pytest.mark.asyncio
async def test_malformed_pre_execution_tool_json_gets_one_model_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _DeferredRuntimeSession()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-pro",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    completions = iter(
        (
            _malformed_tool_call_completion(),
            _tool_call_completion(),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "reply": "已经按你的原话记录到今天的日报。",
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
    captured_messages = []

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del tool_schemas, thinking_enabled
        captured_messages.append(tuple(messages))
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="今天完成了虚构测试事项",
        context=_context(),
        runtime_session=runtime,
    )

    assert runtime.execute_count == 1
    assert runtime.commit_count == 1
    assert runtime.rollback_count == 0
    assert result.iterations == 3
    assert any(
        message.get("role") == "system"
        and "重新生成一次合法的原生工具调用" in str(message.get("content"))
        for message in captured_messages[1]
    )


@pytest.mark.asyncio
async def test_second_malformed_tool_json_fails_closed_without_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _DeferredRuntimeSession()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-pro",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    completions = iter(
        (
            _malformed_tool_call_completion(),
            _malformed_tool_call_completion(),
        )
    )

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    with pytest.raises(MalformedToolCallError):
        await adapter.run_canary_turn(
            system_prompt="Agent2 test",
            user_text="今天完成了虚构测试事项",
            context=_context(),
            runtime_session=runtime,
        )

    assert runtime.execute_count == 0
    assert runtime.commit_count == 0
    assert runtime.rollback_count == 0


@pytest.mark.asyncio
async def test_empty_terminal_after_pending_write_gets_one_json_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _DeferredRuntimeSession()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-pro",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    completions = iter(
        (
            _tool_call_completion(),
            _CompletionResponse(
                message={"role": "assistant", "content": "   "},
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "reply": "已经按你的原话记录到今天的日报。",
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

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="今天完成了虚构测试事项",
        context=_context(),
        runtime_session=runtime,
    )

    assert result.final_content == "已经按你的原话记录到今天的日报。"
    assert result.iterations == 3
    assert runtime.execute_count == 1
    assert runtime.commit_count == 1
    assert runtime.rollback_count == 0


@pytest.mark.asyncio
async def test_pending_write_gets_two_bounded_terminal_json_repairs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _DeferredRuntimeSession()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-pro",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    completions = iter(
        (
            _tool_call_completion(),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "reply": "已记录。",
                            "actual_write": True,
                            "operation_outcome": "partial",
                        },
                        ensure_ascii=False,
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={"role": "assistant", "content": "   "},
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "reply": "已经按你的原话记录到今天的日报。",
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

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="今天完成了虚构测试事项",
        context=_context(),
        runtime_session=runtime,
    )

    assert result.iterations == 4
    assert runtime.execute_count == 1
    assert runtime.commit_count == 1
    assert runtime.rollback_count == 0


@pytest.mark.asyncio
async def test_third_empty_terminal_after_pending_write_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _DeferredRuntimeSession()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-pro",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    completions = iter(
        (
            _tool_call_completion(),
            _CompletionResponse(
                message={"role": "assistant", "content": "   "},
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={"role": "assistant", "content": "\n"},
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={"role": "assistant", "content": "\t"},
                metadata={"finish_reason": "stop"},
            ),
        )
    )

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    with pytest.raises(DeepSeekResponseError):
        await adapter.run_canary_turn(
            system_prompt="Agent2 test",
            user_text="今天完成了虚构测试事项",
            context=_context(),
            runtime_session=runtime,
        )

    assert runtime.execute_count == 1
    assert runtime.commit_count == 0
    assert runtime.rollback_count == 1


@pytest.mark.asyncio
async def test_write_reply_is_model_composed_then_committed_as_one_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _DeferredRuntimeSession()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-pro",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    completions = iter(
        (
            _tool_call_completion(),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "reply": "已经按你的原话记录到今天的日报。",
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

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="今天完成了虚构测试事项",
        context=_context(),
        runtime_session=runtime,
    )

    assert result.final_content == "已经按你的原话记录到今天的日报。"
    assert result.runtime_results[-1].committed_to_outer_transaction is True
    assert runtime.commit_count == 1
    assert runtime.rollback_count == 0


@pytest.mark.asyncio
async def test_internal_error_code_is_hidden_and_model_retries_naturally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _BlockedRuntimeSession()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-pro",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    completions = iter(
        (
            _tool_call_completion(),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "reply": (
                                "本次未执行：SOURCE_REPORT_DATE_MISMATCH"
                            ),
                            "actual_write": False,
                            "operation_outcome": "not_executed",
                        },
                        ensure_ascii=False,
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "reply": "这次没有写入日报，请再确认一下日期。",
                            "actual_write": False,
                            "operation_outcome": "not_executed",
                        },
                        ensure_ascii=False,
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
        )
    )
    second_call_messages: list[dict] = []
    call_count = 0

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        nonlocal call_count
        del tool_schemas, thinking_enabled
        call_count += 1
        if call_count == 2:
            second_call_messages.extend(messages)
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="今天完成了虚构测试事项",
        context=_context(),
        runtime_session=runtime,
    )

    assert result.iterations == 3
    assert result.final_content == "这次没有写入日报，请再确认一下日期。"
    assert "SOURCE_REPORT_DATE_MISMATCH" not in json.dumps(
        second_call_messages,
        ensure_ascii=False,
    )
    assert runtime.commit_count == 0
    assert runtime.rollback_count == 0


@pytest.mark.asyncio
async def test_model_failure_after_write_receipts_rolls_back_the_whole_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _DeferredRuntimeSession()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-pro",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    call_count = 0

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        nonlocal call_count
        del messages, tool_schemas, thinking_enabled
        call_count += 1
        if call_count == 1:
            return _tool_call_completion()
        raise DeepSeekTimeoutError("test timeout")

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    with pytest.raises(DeepSeekTimeoutError):
        await adapter.run_canary_turn(
            system_prompt="Agent2 test",
            user_text="今天完成了虚构测试事项",
            context=_context(),
            runtime_session=runtime,
        )

    assert runtime.commit_count == 0
    assert runtime.rollback_count == 1
