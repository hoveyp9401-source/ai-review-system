from __future__ import annotations

import json
import hashlib
from datetime import date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from app.agent2.tool_calling.context import (
    CANARY_STATE_NAMESPACE,
    TrustedClearPending,
    TrustedContext,
    TrustedPrincipal,
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
)
from app.agent2.tool_calling.production_contracts import ProductionRuntimeResult
from app.agent2.weekly_plan_context import (
    TrustedWeeklyPlanContext,
    TrustedWeeklyPlanDay,
)


_USER_ID = UUID("10000000-0000-4000-8000-000000000004")


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
