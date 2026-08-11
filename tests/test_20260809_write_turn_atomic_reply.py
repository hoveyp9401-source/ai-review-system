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


def _context(
    *,
    allowed_tool_names: frozenset[str] = frozenset({"add_daily_items"}),
    now: datetime | None = None,
) -> TrustedContext:
    return TrustedContext(
        namespace=CANARY_STATE_NAMESPACE,
        now=(now or datetime(2026, 8, 9, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))),
        principal=TrustedPrincipal(
            tenant_id="test-tenant",
            user_id=UUID("10000000-0000-0000-0000-000000000001"),
            conversation_id="test-conversation",
            source_message_id="test-message",
            timezone="Asia/Shanghai",
            display_name="测试用户",
        ),
        allowed_tool_names=allowed_tool_names,
        gate_decisions={name: True for name in allowed_tool_names},
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


def _query_receipt() -> ToolReceipt:
    return ToolReceipt(
        status=ReceiptStatus.SUCCESS,
        tool_name="query_today_report",
        changed=False,
        target_type="daily_report",
        target_id="test-report",
        safe_user_facts={
            "report_date": "2026-08-09",
            "report_status": "collecting",
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
        self.calls = ()

    async def execute(self, calls, *, defer_finalization):
        self.execute_count += 1
        self.calls = calls
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


class _MixedDeferredRuntimeSession:
    mode = ExecutionMode.CANARY_EXECUTE

    def __init__(self) -> None:
        self.commit_count = 0
        self.rollback_count = 0
        self.execute_count = 0
        self.calls = ()
        self.receipts = (_query_receipt(), _changed_receipt())

    async def execute(self, calls, *, defer_finalization):
        self.execute_count += 1
        self.calls = calls
        assert defer_finalization is True
        assert len(calls) == 2
        return ProductionRuntimeResult(
            status="success",
            receipts=self.receipts,
            transaction_opened=True,
            transaction_pending=True,
            handler_call_count=2,
        )

    async def commit_pending(self):
        self.commit_count += 1
        return ProductionRuntimeResult(
            status="success",
            receipts=self.receipts,
            transaction_opened=True,
            committed_to_outer_transaction=True,
            handler_call_count=2,
            business_write_count=1,
            receipt_write_count=2,
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


def _submit_tool_call_completion(
    *,
    call_id: str,
    reviewed: bool,
    date_selection: str = "server_default",
    date_expression: str = "today",
    proposed_date: str = "2026-08-09",
) -> _CompletionResponse:
    arguments = {
        "date_selection": date_selection,
        "date_expression": date_expression,
        "proposed_date": proposed_date,
        "items": [
            {
                "field": "today_work",
                "content": (
                    "核对付款节点" if reviewed else "核对付款节点，没有发现问题"
                ),
                "source_evidence": {"source_message_index": 1},
            },
            {
                "field": "tomorrow_plan",
                "content": "继续跟进回款",
                "source_evidence": {"source_message_index": 1},
            },
        ],
        "acknowledged_empty_fields": ["problems"] if reviewed else [],
        "empty_field_evidence": (
            [
                {
                    "field": "problems",
                    "source_evidence": {"source_message_index": 1},
                }
            ]
            if reviewed
            else []
        ),
        "submit_after_write": True,
    }
    return _CompletionResponse(
        message={
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "add_daily_items",
                        "arguments": json.dumps(
                            arguments,
                            ensure_ascii=False,
                        ),
                    },
                }
            ],
        },
        metadata={"finish_reason": "tool_calls"},
    )


def _mixed_submit_and_query_completion(
    *,
    reviewed: bool = False,
) -> _CompletionResponse:
    submit = _submit_tool_call_completion(call_id="draft", reviewed=reviewed)
    return _CompletionResponse(
        message={
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "original-query",
                    "type": "function",
                    "function": {
                        "name": "query_today_report",
                        "arguments": "{}",
                    },
                },
                *submit.message["tool_calls"],
            ],
        },
        metadata={"finish_reason": "tool_calls"},
    )


@pytest.mark.asyncio
async def test_incomplete_atomic_submit_draft_gets_model_section_review(
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
            _submit_tool_call_completion(call_id="draft", reviewed=False),
            _submit_tool_call_completion(call_id="reviewed", reviewed=True),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "reply": "日报已按你的原意提交。",
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
    captured_messages: list[tuple[dict, ...]] = []

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del tool_schemas, thinking_enabled
        captured_messages.append(tuple(messages))
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text=(
            "日报内容：今日核对付款节点，没什么问题；明日继续跟进回款，请直接提交。"
        ),
        context=_context(),
        runtime_session=runtime,
    )

    assert result.iterations == 3
    assert runtime.execute_count == 1
    assert runtime.commit_count == 1
    assert runtime.calls[0].tool_call_id == "reviewed"
    assert runtime.calls[0].arguments["acknowledged_empty_fields"] == ["problems"]
    assert runtime.calls[0].arguments["items"][0]["content"] == "核对付款节点"
    assert (
        result.model_turns[0].response_metadata["pre_execution_daily_section_review"]
        is True
    )
    assert any(
        message.get("role") == "system"
        and "isolated Agent2 semantic reviewer" in str(message.get("content"))
        for message in captured_messages[1]
    )
    assert any(
        message.get("role") == "user"
        and "unexecuted_draft_calls" in str(message.get("content"))
        for message in captured_messages[1]
    )


@pytest.mark.asyncio
async def test_daily_section_review_preserves_unrelated_model_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _MixedDeferredRuntimeSession()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-pro",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    completions = iter(
        (
            _mixed_submit_and_query_completion(),
            _submit_tool_call_completion(call_id="reviewed", reviewed=True),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "reply": "The report was submitted and the requested report was read.",
                            "actual_write": True,
                            "operation_outcome": "changed",
                        }
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
        )
    )
    captured_tool_names: list[tuple[str, ...]] = []
    captured_messages: list[tuple[dict, ...]] = []

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del thinking_enabled
        captured_messages.append(tuple(messages))
        captured_tool_names.append(
            tuple(item["function"]["name"] for item in tool_schemas)
        )
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="Submit this report and show my current report.",
        context=_context(
            allowed_tool_names=frozenset({"add_daily_items", "query_today_report"})
        ),
        runtime_session=runtime,
    )

    assert result.iterations == 3
    assert runtime.execute_count == 1
    assert runtime.commit_count == 1
    assert [call.tool_name for call in runtime.calls] == [
        "query_today_report",
        "add_daily_items",
    ]
    assert runtime.calls[0].tool_call_id == "original-query"
    assert runtime.calls[1].tool_call_id == "reviewed"
    assert runtime.calls[1].arguments["acknowledged_empty_fields"] == ["problems"]
    assert captured_tool_names[1] == ("add_daily_items",)
    review_payload = "\n".join(
        str(message.get("content") or "") for message in captured_messages[1]
    )
    assert "query_today_report" not in review_payload


@pytest.mark.asyncio
async def test_incomplete_daily_section_review_fails_before_execution(
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
            _submit_tool_call_completion(call_id="draft", reviewed=False),
            _submit_tool_call_completion(call_id="still-incomplete-1", reviewed=False),
            _submit_tool_call_completion(call_id="still-incomplete-2", reviewed=False),
        )
    )
    captured_messages: list[tuple[dict, ...]] = []

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del tool_schemas, thinking_enabled
        captured_messages.append(tuple(messages))
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    with pytest.raises(
        DeepSeekResponseError,
        match="did not return complete corrected submissions",
    ):
        await adapter.run_canary_turn(
            system_prompt="Agent2 test",
            user_text="Submit my complete report.",
            context=_context(),
            runtime_session=runtime,
        )

    assert runtime.execute_count == 0
    assert runtime.commit_count == 0
    assert runtime.rollback_count == 0


@pytest.mark.asyncio
async def test_incomplete_first_review_gets_one_structural_model_retry(
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
            _submit_tool_call_completion(call_id="draft", reviewed=False),
            _submit_tool_call_completion(call_id="still-incomplete", reviewed=False),
            _submit_tool_call_completion(call_id="corrected", reviewed=True),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "reply": "The corrected report was submitted.",
                            "actual_write": True,
                            "operation_outcome": "changed",
                        }
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
        )
    )
    captured_messages: list[tuple[dict, ...]] = []

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del tool_schemas, thinking_enabled
        captured_messages.append(tuple(messages))
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="Submit my complete report.",
        context=_context(),
        runtime_session=runtime,
    )

    assert result.iterations == 4
    assert runtime.execute_count == 1
    assert runtime.commit_count == 1
    assert runtime.calls[0].tool_call_id == "corrected"
    assert (
        result.model_turns[2].response_metadata["daily_section_semantic_review_attempt"]
        == 2
    )
    retry_payload = "\n".join(
        str(message.get("content") or "") for message in captured_messages[2]
    )
    assert "previous_review_structural_feedback" in retry_payload
    assert '"missing_sections":["problems"]' in retry_payload


@pytest.mark.asyncio
async def test_before_nine_date_disagreement_gets_independent_tiebreaker(
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
    first_review = _submit_tool_call_completion(
        call_id="date-review-1",
        reviewed=True,
        date_selection="user_explicit",
        date_expression="today",
        proposed_date="2026-08-11",
    )
    first_review_arguments = json.loads(
        first_review.message["tool_calls"][0]["function"]["arguments"]
    )
    first_review_arguments["items"][0]["content"] = "reviewer must not change this"
    first_review.message["tool_calls"][0]["function"]["arguments"] = json.dumps(
        first_review_arguments
    )
    tiebreak_review = _submit_tool_call_completion(
        call_id="date-review-2",
        reviewed=True,
        date_selection="user_explicit",
        date_expression="today",
        proposed_date="2026-08-11",
    )
    completions = iter(
        (
            _submit_tool_call_completion(
                call_id="draft",
                reviewed=True,
                date_selection="server_default",
                date_expression="default",
                proposed_date="2026-08-10",
            ),
            first_review,
            tiebreak_review,
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "reply": "The report was submitted for the reviewed date.",
                            "actual_write": True,
                            "operation_outcome": "changed",
                        }
                    ),
                },
                metadata={"finish_reason": "stop"},
            ),
        )
    )
    captured_messages: list[tuple[dict, ...]] = []

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del tool_schemas, thinking_enabled
        captured_messages.append(tuple(messages))
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="This report explicitly belongs to today; submit it.",
        context=_context(
            now=datetime(
                2026,
                8,
                11,
                8,
                20,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            )
        ),
        runtime_session=runtime,
    )

    assert result.iterations == 4
    assert runtime.execute_count == 1
    assert runtime.commit_count == 1
    assert runtime.calls[0].tool_call_id == "date-review-2"
    assert runtime.calls[0].arguments["date_selection"] == "user_explicit"
    assert runtime.calls[0].arguments["proposed_date"] == "2026-08-11"
    assert runtime.calls[0].arguments["items"][0]["content"] == "核对付款节点"
    assert (
        result.model_turns[1].response_metadata[
            "daily_write_date_semantic_review_attempt"
        ]
        == 1
    )
    assert (
        result.model_turns[2].response_metadata[
            "daily_write_date_semantic_review_attempt"
        ]
        == 2
    )
    first_review_payload = json.loads(captured_messages[1][1]["content"])
    first_protected_arguments = first_review_payload["unexecuted_daily_drafts"][0][
        "protected_non_date_arguments"
    ]
    assert "date_selection" not in first_protected_arguments
    assert "date_expression" not in first_protected_arguments
    assert "proposed_date" not in first_protected_arguments
    tiebreak_payload = json.loads(captured_messages[2][1]["content"])
    assert tiebreak_payload["prior_model_disagreement"] == [
        {
            "original_draft_sequence": 1,
            "review_mode": "independent_tiebreak",
            "sequence": 1,
        }
    ]


@pytest.mark.asyncio
async def test_before_nine_date_review_preserves_unrelated_query_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _MixedDeferredRuntimeSession()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-pro",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    completions = iter(
        (
            _mixed_submit_and_query_completion(reviewed=True),
            _submit_tool_call_completion(
                call_id="date-reviewed",
                reviewed=True,
                date_selection="server_default",
                date_expression="default",
                proposed_date="2026-08-10",
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "reply": "The report was submitted and the report was read.",
                            "actual_write": True,
                            "operation_outcome": "changed",
                        }
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
        user_text="Submit the undated report and show my current report.",
        context=_context(
            allowed_tool_names=frozenset({"add_daily_items", "query_today_report"}),
            now=datetime(
                2026,
                8,
                11,
                8,
                20,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            ),
        ),
        runtime_session=runtime,
    )

    assert result.iterations == 3
    assert [call.tool_name for call in runtime.calls] == [
        "query_today_report",
        "add_daily_items",
    ]
    assert runtime.calls[0].tool_call_id == "original-query"
    assert runtime.calls[1].tool_call_id == "date-reviewed"


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
                            "reply": ("本次未执行：SOURCE_REPORT_DATE_MISMATCH"),
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
