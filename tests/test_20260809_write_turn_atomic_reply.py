import json
from datetime import date, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.agent2.tool_calling.context import (
    CANARY_STATE_NAMESPACE,
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
    historical_reports: tuple[TrustedReportSnapshot, ...] = (),
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
        historical_reports=historical_reports,
        allowed_tool_names=allowed_tool_names,
        gate_decisions={name: True for name in allowed_tool_names},
    )


def _trusted_historical_report(
    *,
    include_tomorrow_plan: bool,
) -> TrustedReportSnapshot:
    report_id = UUID("20000000-0000-0000-0000-000000000001")
    items = [
        TrustedReportItem(
            item_id="today-work-1",
            field="today_work",
            content="完成合同复核",
            report_id=report_id,
            report_version=2,
        )
    ]
    if include_tomorrow_plan:
        items.append(
            TrustedReportItem(
                item_id="tomorrow-plan-1",
                field="tomorrow_plan",
                content="整理附件",
                report_id=report_id,
                report_version=2,
            )
        )
    return TrustedReportSnapshot(
        report_id=report_id,
        tenant_id="test-tenant",
        owner_user_id=UUID("10000000-0000-0000-0000-000000000001"),
        report_date=date(2026, 8, 8),
        version=2,
        status="collecting",
        items=tuple(items),
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


def _clarification_receipt() -> ToolReceipt:
    return ToolReceipt(
        status=ReceiptStatus.CLARIFICATION_REQUIRED,
        tool_name="apply_next_weekly_plan",
        changed=False,
        safe_user_facts={
            "actual_write": False,
            "clarification_option_labels": ["本周", "下周"],
            "must_ask_user": True,
        },
        error_code="WEEKLY_PLAN_TARGET_WEEK_AMBIGUOUS",
        execution_mode=ExecutionMode.CANARY_EXECUTE,
    )


def test_clarification_reply_must_ask_every_server_provided_option() -> None:
    invalid, invalid_errors = validate_write_reply(
        json.dumps(
            {
                "reply": "请问你指哪一周？",
                "actual_write": False,
                "operation_outcome": "needs_clarification",
            },
            ensure_ascii=False,
        ),
        (_clarification_receipt(),),
    )
    valid, valid_errors = validate_write_reply(
        json.dumps(
            {
                "reply": "你指本周还是下周？这次还没有写入。",
                "actual_write": False,
                "operation_outcome": "needs_clarification",
            },
            ensure_ascii=False,
        ),
        (_clarification_receipt(),),
    )

    assert invalid is None
    assert "本周" in " ".join(invalid_errors)
    assert "下周" in " ".join(invalid_errors)
    assert valid_errors == ()
    assert valid is not None


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


@pytest.mark.asyncio
async def test_flash_thinking_request_is_explicitly_enabled_at_high_effort() -> None:
    class CapturingHttpClient:
        def __init__(self) -> None:
            self.payload = None

        async def post(self, url, **kwargs):
            self.payload = kwargs["json"]
            return httpx.Response(
                200,
                json={
                    "id": "response-thinking",
                    "model": "deepseek-v4-flash",
                    "created": 1,
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": "好的。",
                                "reasoning_content": "checked",
                            },
                        }
                    ],
                    "usage": {},
                },
                request=httpx.Request("POST", url),
            )

    client = CapturingHttpClient()
    adapter = DeepSeekToolCallingAdapter(
        http_client=client,
        model="deepseek-v4-flash",
        timeout_seconds=3.0,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )

    await adapter._complete(
        messages=[{"role": "user", "content": "测试"}],
        tool_schemas=[],
        thinking_enabled=True,
    )

    assert client.payload["model"] == "deepseek-v4-flash"
    assert client.payload["thinking"] == {"type": "enabled"}
    assert client.payload["reasoning_effort"] == "high"


@pytest.mark.asyncio
async def test_provider_model_mismatch_fails_closed_before_any_tool_execution() -> None:
    class WrongModelHttpClient:
        async def post(self, url, **kwargs):
            return httpx.Response(
                200,
                json={
                    "id": "response-wrong-model",
                    "model": "deepseek-v4-pro",
                    "created": 1,
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": "不应采用这次响应。",
                                "reasoning_content": "wrong served model",
                            },
                        }
                    ],
                    "usage": {},
                },
                request=httpx.Request("POST", url),
            )

    adapter = DeepSeekToolCallingAdapter(
        http_client=WrongModelHttpClient(),
        model="deepseek-v4-flash",
        timeout_seconds=3.0,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )

    with pytest.raises(DeepSeekResponseError, match="unexpected model"):
        await adapter._complete(
            messages=[{"role": "user", "content": "测试"}],
            tool_schemas=[],
            thinking_enabled=True,
        )


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
    date_evidence_quote: str | None = None,
    reasoning_content: str | None = None,
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
    if date_evidence_quote is not None:
        arguments["date_evidence"] = {
            "source_message_index": 1,
            "exact_quote": date_evidence_quote,
        }
    message = {
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
    }
    if reasoning_content is not None:
        message["reasoning_content"] = reasoning_content
    return _CompletionResponse(
        message=message,
        metadata={"finish_reason": "tool_calls"},
    )


def _trusted_report_empty_problem_submit_completion(
    *,
    report: TrustedReportSnapshot,
    call_id: str = "trusted-followup",
) -> _CompletionResponse:
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
                            {
                                "date_selection": "trusted_report",
                                "report_id": str(report.report_id),
                                "expected_version": report.version,
                                "items": [],
                                "acknowledged_empty_fields": ["problems"],
                                "empty_field_evidence": [
                                    {
                                        "field": "problems",
                                        "source_evidence": {
                                            "source_message_index": 1,
                                        },
                                    }
                                ],
                                "submit_after_write": True,
                            },
                            ensure_ascii=False,
                        ),
                    },
                }
            ],
        },
        metadata={"finish_reason": "tool_calls"},
    )


def _confirm_trusted_report_completion(
    *,
    report: TrustedReportSnapshot,
    call_id: str = "confirm-draft",
) -> _CompletionResponse:
    return _CompletionResponse(
        message={
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "confirm_report",
                        "arguments": json.dumps(
                            {
                                "report_id": str(report.report_id),
                                "expected_version": report.version,
                            },
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
async def test_trusted_report_existing_sections_allow_empty_problem_submit_without_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _trusted_historical_report(include_tomorrow_plan=True)
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
            _trusted_report_empty_problem_submit_completion(report=report),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "reply": "已确认问题与风险为空，并提交上一份日报。",
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
        user_text="风险这块没遇到什么，前面那版就这么定了。",
        context=_context(historical_reports=(report,)),
        runtime_session=runtime,
    )

    assert result.iterations == 2
    assert runtime.execute_count == 1
    assert runtime.commit_count == 1
    assert runtime.calls[0].tool_call_id == "trusted-followup"
    assert "pre_execution_daily_section_review" not in (
        result.model_turns[0].response_metadata
    )


@pytest.mark.asyncio
async def test_incomplete_trusted_report_confirm_draft_gets_agent2_review_before_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _trusted_historical_report(include_tomorrow_plan=True)
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
            _confirm_trusted_report_completion(report=report),
            _trusted_report_empty_problem_submit_completion(
                report=report,
                call_id="reviewed-add-and-submit",
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "reply": "已按你的原话补充空栏并提交上一份日报。",
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
    captured_tool_names: list[tuple[str, ...]] = []

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
        user_text="问题与风险没有，前面的内容就按这个提交。",
        context=_context(
            allowed_tool_names=frozenset({"add_daily_items", "confirm_report"}),
            historical_reports=(report,),
        ),
        runtime_session=runtime,
    )

    assert result.iterations == 3
    assert runtime.execute_count == 1
    assert runtime.commit_count == 1
    assert runtime.calls[0].tool_name == "add_daily_items"
    assert runtime.calls[0].tool_call_id == "reviewed-add-and-submit"
    assert runtime.calls[0].arguments["acknowledged_empty_fields"] == ["problems"]
    assert runtime.calls[0].arguments["submit_after_write"] is True
    assert (
        result.model_turns[0].response_metadata[
            "pre_execution_incomplete_confirm_review"
        ]
        is True
    )
    assert set(captured_tool_names[1]) == {
        "add_daily_items",
        "confirm_report",
    }
    review_payload = "\n".join(
        str(message.get("content") or "") for message in captured_messages[1]
    )
    assert "isolated Agent2 semantic reviewer" in review_payload
    assert "unexecuted_confirm_drafts" in review_payload
    assert "problems" in review_payload


@pytest.mark.asyncio
async def test_incomplete_report_review_preserves_a_pure_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _trusted_historical_report(include_tomorrow_plan=True)
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
            _confirm_trusted_report_completion(report=report),
            _confirm_trusted_report_completion(
                report=report,
                call_id="reviewed-confirm",
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "reply": "已按你的确认处理。",
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
        user_text="就按前面这份确认。",
        context=_context(
            allowed_tool_names=frozenset({"add_daily_items", "confirm_report"}),
            historical_reports=(report,),
        ),
        runtime_session=runtime,
    )

    assert result.iterations == 3
    assert runtime.execute_count == 1
    assert runtime.calls[0].tool_name == "confirm_report"
    assert runtime.calls[0].tool_call_id == "reviewed-confirm"


@pytest.mark.asyncio
async def test_incomplete_confirm_review_cannot_change_trusted_report_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _trusted_historical_report(include_tomorrow_plan=True)
    runtime = _DeferredRuntimeSession()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-pro",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    wrong_report = report.model_copy(
        update={
            "report_id": UUID("20000000-0000-0000-0000-000000000099"),
        }
    )
    completions = iter(
        (
            _confirm_trusted_report_completion(report=report),
            _trusted_report_empty_problem_submit_completion(
                report=wrong_report,
                call_id="wrong-report-review",
            ),
        )
    )

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    with pytest.raises(
        DeepSeekResponseError,
        match="invalid replacement",
    ):
        await adapter.run_canary_turn(
            system_prompt="Agent2 test",
            user_text="问题与风险没有，前面的内容就按这个提交。",
            context=_context(
                allowed_tool_names=frozenset({"add_daily_items", "confirm_report"}),
                historical_reports=(report,),
            ),
            runtime_session=runtime,
        )

    assert runtime.execute_count == 0
    assert runtime.commit_count == 0
    assert runtime.rollback_count == 0


@pytest.mark.asyncio
async def test_incomplete_confirm_review_rejects_add_without_current_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _trusted_historical_report(include_tomorrow_plan=True)
    runtime = _DeferredRuntimeSession()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-pro",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    empty_add = _CompletionResponse(
        message={
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "reviewed-empty-add",
                    "type": "function",
                    "function": {
                        "name": "add_daily_items",
                        "arguments": json.dumps(
                            {
                                "date_selection": "trusted_report",
                                "report_id": str(report.report_id),
                                "expected_version": report.version,
                                "items": [],
                                "acknowledged_empty_fields": [],
                                "empty_field_evidence": [],
                                "submit_after_write": True,
                            }
                        ),
                    },
                }
            ],
        },
        metadata={"finish_reason": "tool_calls"},
    )
    completions = iter(
        (
            _confirm_trusted_report_completion(report=report),
            empty_add,
        )
    )

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    with pytest.raises(
        DeepSeekResponseError,
        match="invalid replacement",
    ):
        await adapter.run_canary_turn(
            system_prompt="Agent2 test",
            user_text="就按前面这份确认。",
            context=_context(
                allowed_tool_names=frozenset({"add_daily_items", "confirm_report"}),
                historical_reports=(report,),
            ),
            runtime_session=runtime,
        )

    assert runtime.execute_count == 0
    assert runtime.commit_count == 0
    assert runtime.rollback_count == 0


@pytest.mark.asyncio
async def test_complete_trusted_report_confirmation_does_not_add_a_review_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _trusted_historical_report(include_tomorrow_plan=True).model_copy(
        update={"acknowledged_empty_fields": frozenset({"problems"})}
    )
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
            _confirm_trusted_report_completion(report=report),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "reply": "已按你的确认处理。",
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
        user_text="确认提交。",
        context=_context(
            allowed_tool_names=frozenset({"add_daily_items", "confirm_report"}),
            historical_reports=(report,),
        ),
        runtime_session=runtime,
    )

    assert result.iterations == 2
    assert runtime.execute_count == 1
    assert runtime.calls[0].tool_name == "confirm_report"
    assert "pre_execution_incomplete_confirm_review" not in (
        result.model_turns[0].response_metadata
    )


@pytest.mark.asyncio
async def test_reviewed_trusted_report_followup_stays_on_that_report_before_nine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _trusted_historical_report(include_tomorrow_plan=True)
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
            _confirm_trusted_report_completion(report=report),
            _trusted_report_empty_problem_submit_completion(
                report=report,
                call_id="reviewed-before-nine",
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "reply": "已补充并提交指定的上一份日报。",
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
        user_text="问题与风险没有，前面的内容就按这个提交。",
        context=_context(
            allowed_tool_names=frozenset({"add_daily_items", "confirm_report"}),
            historical_reports=(report,),
            now=datetime(
                2026,
                8,
                9,
                8,
                0,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            ),
        ),
        runtime_session=runtime,
    )

    assert result.iterations == 3
    assert runtime.calls[0].arguments["date_selection"] == "trusted_report"
    assert runtime.calls[0].arguments["report_id"] == str(report.report_id)
    assert "daily_write_date_semantic_review" not in (
        result.model_turns[1].response_metadata
    )


@pytest.mark.asyncio
async def test_trusted_report_missing_another_section_still_requires_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _trusted_historical_report(include_tomorrow_plan=False)
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
            _trusted_report_empty_problem_submit_completion(report=report),
            _trusted_report_empty_problem_submit_completion(
                report=report,
                call_id="still-missing-plan-1",
            ),
            _trusted_report_empty_problem_submit_completion(
                report=report,
                call_id="still-missing-plan-2",
            ),
        )
    )

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    with pytest.raises(
        DeepSeekResponseError,
        match="did not return complete corrected submissions",
    ):
        await adapter.run_canary_turn(
            system_prompt="Agent2 test",
            user_text="风险这块没遇到什么，前面那版就这么定了。",
            context=_context(historical_reports=(report,)),
            runtime_session=runtime,
        )

    assert runtime.execute_count == 0
    assert runtime.commit_count == 0


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
async def test_before_nine_main_agent_date_decision_executes_without_a_second_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _DeferredRuntimeSession()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    main_reasoning = "The user assigned this report to the new local calendar day."
    completions = iter(
        (
            _submit_tool_call_completion(
                call_id="draft",
                reviewed=True,
                date_selection="user_explicit",
                date_expression="2026-08-11",
                proposed_date="2026-08-11",
                date_evidence_quote="belongs to August 11",
                reasoning_content=main_reasoning,
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "reply": "The report was submitted for August 11.",
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
        user_text="This report belongs to August 11; submit it.",
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

    assert result.iterations == 2
    assert runtime.execute_count == 1
    assert runtime.commit_count == 1
    assert runtime.calls[0].arguments["date_selection"] == "user_explicit"
    assert runtime.calls[0].arguments["proposed_date"] == "2026-08-11"
    assert runtime.calls[0].arguments["date_evidence"] == {
        "source_message_index": 1,
        "exact_quote": "belongs to August 11",
    }
    assert captured_messages[1][-3]["reasoning_content"] == main_reasoning
    assert all(
        turn.response_metadata.get("daily_write_date_semantic_review") is not True
        for turn in result.model_turns
    )


@pytest.mark.asyncio
async def test_before_nine_server_default_preserves_unrelated_query_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _MixedDeferredRuntimeSession()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    completions = iter(
        (
            _mixed_submit_and_query_completion(reviewed=True),
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
            now=datetime(2026, 8, 11, 8, 20, tzinfo=ZoneInfo("Asia/Shanghai")),
        ),
        runtime_session=runtime,
    )

    assert result.iterations == 2
    assert [call.tool_name for call in runtime.calls] == [
        "query_today_report",
        "add_daily_items",
    ]
    assert runtime.calls[1].arguments["date_selection"] == "server_default"
    assert runtime.calls[1].arguments["proposed_date"] == "2026-08-09"


@pytest.mark.asyncio
async def test_section_reviewer_replacement_keeps_main_reasoning_in_tool_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _DeferredRuntimeSession()
    adapter = DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )
    main_reasoning = "Main Agent2 understood the complete user request."
    main = _submit_tool_call_completion(
        call_id="main-incomplete",
        reviewed=False,
        reasoning_content=main_reasoning,
    )
    review = _submit_tool_call_completion(
        call_id="review-replacement",
        reviewed=True,
        reasoning_content="isolated reviewer reasoning must remain audit-only",
    )
    completions = iter(
        (
            main,
            review,
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "reply": "The complete report was submitted.",
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
        user_text=(
            "日报内容：今日核对付款节点，没什么问题；明日继续跟进回款，请直接提交。"
        ),
        context=_context(),
        runtime_session=runtime,
        thinking_enabled=True,
    )

    assert result.iterations == 3
    tool_loop_messages = captured_messages[2]
    assistant_tool_message = next(
        message
        for message in reversed(tool_loop_messages)
        if message.get("role") == "assistant"
        and message.get("tool_calls")
    )
    assert assistant_tool_message["reasoning_content"] == main_reasoning
    assert "isolated reviewer reasoning" not in json.dumps(
        tool_loop_messages,
        ensure_ascii=False,
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
