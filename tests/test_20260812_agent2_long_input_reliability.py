from __future__ import annotations

import ast
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.agent2.tool_calling.assembly import TrustedContextRequest
from app.agent2.tool_calling.context import (
    CANARY_STATE_NAMESPACE,
    TrustedContext,
    TrustedPrincipal,
    TrustedRecentMessage,
    TrustedRecentOperation,
    TrustedReportItem,
    TrustedReportReference,
    TrustedReportSnapshot,
)
from app.agent2.tool_calling.contracts import ExecutionMode, ReceiptStatus, ToolReceipt
from app.agent2.tool_calling.current_turn_source import CurrentTurnSource
from app.agent2.tool_calling.daily_add_model_contract import (
    compile_focused_daily_plan_arguments,
    compile_model_add_daily_items,
    model_add_daily_items_schema,
    parse_focused_daily_add_decision,
)
from app.agent2.tool_calling.deepseek_adapter import (
    DeepSeekResponseError,
    DeepSeekTimeoutError,
    DeepSeekToolCallingAdapter,
    _CompletionResponse,
    _parse_focused_daily_completion,
)
from app.agent2.tool_calling.production_contracts import ProductionRuntimeResult
from app.agent2.tool_calling.production_runtime import _prepare_call
from app.agent2.tool_calling.production_store import (
    ProductionContextStore,
    _text_content,
)
from app.agent2.tool_calling.registry import deepseek_tool_schemas
from app.agent2.tool_calling.turn_batching import prepare_recoverable_ingress_payload
from app.agent2.tool_calling.validation import (
    NativeToolCall,
    ShadowCallBinder,
    UnavailableDateResolver,
)
from app.models import MessageIngressClaim, WebhookEvent
from app.repositories import create_webhook_event_once

TENANT_ID = "legal-daily-production-v1"
USER_ID = UUID("10000000-0000-0000-0000-000000000001")
REPORT_ID = UUID("20000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 8, 12, 9, 2, tzinfo=ZoneInfo("Asia/Shanghai"))
LONG_DAILY_TEXT = (
    "今日工作：1.今天完成A合同复核并向业务反馈两项修改意见；"
    "2.随后与财务核对B项目付款节点；"
    "3.参加专项会议并整理会议中明确的三项后续安排；"
    "4.复核项目补充协议并把待确认条款逐项反馈给经办人；"
    "5.更新案件进展台账并核对本周已经收到的证据材料；"
    "6.与外部顾问沟通下次会议时间及需要提前准备的文件。"
    "问题风险：当前仍有一项关键数据等待业务部门确认，在确认前不能形成最终结论。"
    "明日计划：明天继续跟进C案件证据清单，并按实际回复更新处理进展。"
)


def _context(
    *,
    source_message_id: str = "provider-message-1",
    report: TrustedReportSnapshot | None = None,
    recent_messages: tuple[TrustedRecentMessage, ...] = (),
    recent_operations: tuple[TrustedRecentOperation, ...] = (),
    allowed_tool_names: frozenset[str] = frozenset(
        {"add_daily_items", "confirm_report"}
    ),
) -> TrustedContext:
    return TrustedContext(
        namespace=CANARY_STATE_NAMESPACE,
        now=NOW,
        principal=TrustedPrincipal(
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            conversation_id="conversation-1",
            source_message_id=source_message_id,
            timezone="Asia/Shanghai",
            display_name="测试用户",
        ),
        historical_reports=(report,) if report is not None else (),
        recent_messages=recent_messages,
        recent_operations=recent_operations,
        allowed_tool_names=allowed_tool_names,
        gate_decisions={name: True for name in allowed_tool_names},
    )


def _long_daily_call(
    tool_call_id: str = "long-daily-1",
) -> NativeToolCall:
    return NativeToolCall(
        tool_call_id=tool_call_id,
        tool_name="add_daily_items",
        arguments={
            "date_selection": "server_default",
            "date_expression": "今天",
            "proposed_date": "2026-08-12",
            "items": [
                {
                    "field": "today_work",
                    "content": "完成A合同复核并向业务反馈两项修改意见",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "今天完成A合同复核并向业务反馈两项修改意见",
                    },
                },
                {
                    "field": "today_work",
                    "content": "与财务核对B项目付款节点",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "随后与财务核对B项目付款节点",
                    },
                },
                {
                    "field": "tomorrow_plan",
                    "content": "继续跟进C案件证据清单",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "明天继续跟进C案件证据清单",
                    },
                },
            ],
        },
    )


def _compact_long_daily_call(
    tool_call_id: str = "compact-long-daily-1",
) -> NativeToolCall:
    return NativeToolCall(
        tool_call_id=tool_call_id,
        tool_name="add_daily_items",
        arguments={
            "date_selection": "server_default",
            "items": [
                {
                    "field": "today_work",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "今天完成A合同复核并向业务反馈两项修改意见",
                    },
                },
                {
                    "field": "today_work",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "随后与财务核对B项目付款节点",
                    },
                },
                {
                    "field": "tomorrow_plan",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "明天继续跟进C案件证据清单",
                    },
                },
            ],
        },
    )


def _changed_receipt() -> ToolReceipt:
    return ToolReceipt(
        status=ReceiptStatus.SUCCESS,
        tool_name="add_daily_items",
        changed=True,
        target_type="daily_report",
        target_id=str(REPORT_ID),
        before_version=0,
        after_version=1,
        affected_item_ids=("item-1", "item-2", "item-3"),
        safe_user_facts={
            "actual_write": True,
            "report_date": "2026-08-12",
            "report_status": "collecting",
        },
        execution_mode=ExecutionMode.CANARY_EXECUTE,
    )


class _DeferredRuntime:
    mode = ExecutionMode.CANARY_EXECUTE

    def __init__(self) -> None:
        self.execute_count = 0
        self.commit_count = 0
        self.rollback_count = 0
        self.calls: tuple[NativeToolCall, ...] = ()

    async def execute(self, calls, *, defer_finalization=False):
        assert defer_finalization is True
        self.execute_count += 1
        self.calls = calls
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


class _QueryRuntime:
    mode = ExecutionMode.CANARY_EXECUTE

    def __init__(self) -> None:
        self.calls: tuple[NativeToolCall, ...] = ()

    async def execute(self, calls, *, defer_finalization=False):
        assert defer_finalization is False
        self.calls = calls
        assert [call.tool_name for call in calls] == ["query_today_report"]
        return ProductionRuntimeResult(
            status="success",
            receipts=(
                ToolReceipt(
                    status=ReceiptStatus.SUCCESS,
                    tool_name="query_today_report",
                    changed=False,
                    target_type="daily_report",
                    target_id=str(REPORT_ID),
                    safe_user_facts={
                        "today_report": {
                            "report_date": "2026-08-12",
                            "status": "collecting",
                            "today_work": ["完成合同复核"],
                            "problems": [],
                            "tomorrow_plan": [],
                        }
                    },
                    execution_mode=ExecutionMode.CANARY_EXECUTE,
                ),
            ),
            handler_call_count=1,
        )


def _tool_completion(call: NativeToolCall) -> _CompletionResponse:
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
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
            ],
        },
        metadata={"finish_reason": "tool_calls"},
    )


def _malformed_daily_completion() -> _CompletionResponse:
    return _CompletionResponse(
        message={
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "malformed-long-daily-1",
                    "type": "function",
                    "function": {
                        "name": "plan_daily_report",
                        "arguments": '{"date_selection":"server_default","fields":{',
                    },
                }
            ],
        },
        metadata={"finish_reason": "tool_calls"},
    )


def _focused_daily_completion(call: NativeToolCall) -> _CompletionResponse:
    arguments = {
        key: value
        for key, value in call.arguments.items()
        if key != "items"
    }
    arguments["fields"] = {
        field: [
            item["source_evidence"]
            for item in call.arguments.get("items", [])
            if item["field"] == field
        ]
        for field in ("today_work", "problems", "tomorrow_plan")
    }
    return _CompletionResponse(
        message={
            "role": "assistant",
            "content": json.dumps(
                {
                    "decision": "daily_add",
                    "arguments": arguments,
                    "reply": "已按原文完整记录。",
                },
                ensure_ascii=False,
            ),
        },
        metadata={"finish_reason": "stop"},
    )


def _focused_daily_plan_completion(call: NativeToolCall) -> _CompletionResponse:
    fields = {
        field: [
            item["source_evidence"]
            for item in call.arguments.get("items", [])
            if item["field"] == field
        ]
        for field in ("today_work", "problems", "tomorrow_plan")
    }
    arguments = {
        "date_selection": "server_default",
        "fields": fields,
        "empty_field_evidence": [],
        "submit_after_write": bool(
            call.arguments.get("submit_after_write", False)
        ),
        "submission_evidence": call.arguments.get(
            "submission_evidence"
        ),
        "reply": "已按原文完整记录。",
    }
    return _CompletionResponse(
        message={
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call.tool_call_id,
                    "type": "function",
                    "function": {
                        "name": "plan_daily_report",
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


def _focused_daily_review_completion(
    *,
    approved: bool,
    reason: str = "candidate is incomplete",
) -> _CompletionResponse:
    payload = (
        {"decision": "approve", "reason": ""}
        if approved
        else {"decision": "fallback", "reason": reason}
    )
    return _CompletionResponse(
        message={
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "focused-daily-review",
                    "type": "function",
                    "function": {
                        "name": "review_daily_plan",
                        "arguments": json.dumps(payload, ensure_ascii=False),
                    },
                }
            ],
        },
        metadata={"finish_reason": "tool_calls"},
    )


def _focused_daily_repair_review_completion(reason: str) -> _CompletionResponse:
    return _CompletionResponse(
        message={
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "focused-daily-repair-review",
                    "type": "function",
                    "function": {
                        "name": "review_daily_plan",
                        "arguments": json.dumps(
                            {"decision": "repair", "reason": reason},
                            ensure_ascii=False,
                        ),
                    },
                }
            ],
        },
        metadata={"finish_reason": "tool_calls"},
    )


def _adapter() -> DeepSeekToolCallingAdapter:
    return DeepSeekToolCallingAdapter(
        http_client=object(),
        model="deepseek-v4-pro",
        timeout_seconds=10,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )


def test_daily_add_model_schema_requires_reviewable_item_content() -> None:
    schema = deepseek_tool_schemas(frozenset({"add_daily_items"}))[0][
        "function"
    ]["parameters"]
    item_contract = schema["$defs"]["DailyItemInput"]

    assert set(item_contract["properties"]) == {
        "field",
        "content",
        "source_evidence",
    }
    assert item_contract["required"] == [
        "field",
        "content",
        "source_evidence",
    ]
    assert "content_reviewed" not in schema["properties"]


def test_model_cannot_self_assert_independent_content_review() -> None:
    compiled = compile_model_add_daily_items(
        {
            "items": [
                {
                    "field": "today_work",
                    "content": "改写后的内容",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "用户原话",
                    },
                }
            ],
            "content_reviewed": True,
        }
    )

    assert compiled["content_reviewed"] is False


def test_explicit_today_report_display_requires_one_fresh_read() -> None:
    description = deepseek_tool_schemas(
        frozenset({"query_today_report"})
    )[0]["function"]["description"]

    assert "call this tool once even when a snapshot is already injected" in (
        description
    )


def test_focused_daily_plan_accepts_one_complete_36_item_report() -> None:
    arguments, reply = compile_focused_daily_plan_arguments(
        {
            "date_selection": "server_default",
            "fields": {
                "today_work": [
                    {
                        "source_message_index": 1,
                        "exact_quote": f"今日工作第{index}项",
                    }
                    for index in range(1, 26)
                ],
                "problems": [
                    {
                        "source_message_index": 1,
                        "exact_quote": f"问题风险第{index}项",
                    }
                    for index in range(1, 6)
                ],
                "tomorrow_plan": [
                    {
                        "source_message_index": 1,
                        "exact_quote": f"明日计划第{index}项",
                    }
                    for index in range(1, 7)
                ],
            },
            "empty_field_evidence": [],
            "submit_after_write": False,
            "reply": "已按原文完整记录。",
        }
    )

    assert len(arguments["items"]) == 36
    assert reply == "已按原文完整记录。"


@pytest.mark.asyncio
async def test_focused_daily_plan_preserves_reviewed_conservative_wording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = (
        "今天：1修复了日报agent的bug。"
        "2.将合同评审技能变成了网页端的网页agent调用速度快了10倍，"
        "明天计划继续找可以做成网页端的agent技能然后"
        "被告案件进行通报与为结案案件的签约 没啥别的问题"
    )
    plan_arguments = {
        "date_selection": "server_default",
        "fields": {
            "today_work": [
                {
                    "content": "修复日报agent的bug",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "修复了日报agent的bug",
                    },
                },
                {
                    "content": "将合同评审技能改造成网页端agent，调用速度提升10倍",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": (
                            "将合同评审技能变成了网页端的网页agent调用速度快了10倍"
                        ),
                    },
                },
            ],
            "problems": [],
            "tomorrow_plan": [
                {
                    "content": "继续寻找可以做成网页端的agent技能",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "明天计划继续找可以做成网页端的agent技能",
                    },
                },
                {
                    "content": "进行被告案件通报及为结案案件签约",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "被告案件进行通报与为结案案件的签约",
                    },
                },
            ],
        },
        "empty_field_evidence": [
            {
                "field": "problems",
                "source_evidence": {"source_message_index": 1},
            }
        ],
        "submit_after_write": False,
        "reply": "已整理并记录。",
    }
    completions = iter(
        (
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "pang-semantic-split",
                            "type": "function",
                            "function": {
                                "name": "plan_daily_report",
                                "arguments": json.dumps(
                                    plan_arguments,
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                },
                metadata={"finish_reason": "tool_calls"},
            ),
            _focused_daily_review_completion(approved=True),
        )
    )
    runtime = _DeferredRuntime()
    adapter = _adapter()
    requests: list[list[dict]] = []

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del tool_schemas, thinking_enabled
        requests.append(messages)
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 full production prompt",
        user_text=source,
        context=_context(),
        runtime_session=runtime,
        thinking_enabled=True,
    )

    assert result.final_content == "已整理并记录。"
    assert [
        (item["field"], item["content"])
        for item in runtime.calls[0].arguments["items"]
    ] == [
        ("today_work", "修复日报agent的bug"),
        ("today_work", "将合同评审技能改造成网页端agent，调用速度提升10倍"),
        ("tomorrow_plan", "继续寻找可以做成网页端的agent技能"),
        ("tomorrow_plan", "进行被告案件通报及为结案案件签约"),
    ]
    review_payload = json.loads(requests[1][1]["content"])
    assert review_payload["candidate"]["items"][1]["content"] == (
        "将合同评审技能改造成网页端agent，调用速度提升10倍"
    )
    assert "adds, removes, generalizes, or changes" in requests[1][0]["content"]
    assert "Audit in two passes" in requests[1][0]["content"]
    assert "one item spans two unrelated work domains" in requests[1][0]["content"]


def test_focused_daily_plan_preserves_explicit_empty_field_evidence() -> None:
    arguments, _ = compile_focused_daily_plan_arguments(
        {
            "date_selection": "server_default",
            "fields": {
                "today_work": [
                    {
                        "source_message_index": 1,
                        "exact_quote": "完成合同复核",
                    }
                ],
                "problems": [],
                "tomorrow_plan": [],
            },
            "empty_field_evidence": [
                {
                    "field": "problems",
                    "source_evidence": {
                        "source_message_index": 1,
                    },
                }
            ],
            "submit_after_write": False,
            "reply": "已按原文完整记录。",
        }
    )

    assert arguments["acknowledged_empty_fields"] == ["problems"]
    assert arguments["empty_field_evidence"] == [
        {
            "field": "problems",
            "source_evidence": {
                "source_message_index": 1,
            },
        }
    ]


def test_focused_daily_plan_accepts_extra_exact_empty_quote() -> None:
    arguments, _ = compile_focused_daily_plan_arguments(
        {
            "date_selection": "server_default",
            "fields": {
                "today_work": [
                    {
                        "content": "完成合同复核",
                        "source_evidence": {
                            "source_message_index": 1,
                            "exact_quote": "完成合同复核",
                        },
                    }
                ],
                "problems": [],
                "tomorrow_plan": [],
            },
            "empty_field_evidence": [
                {
                    "field": "problems",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "问题风险：暂无",
                    },
                }
            ],
            "submit_after_write": False,
            "reply": "已记录。",
        }
    )

    assert arguments["empty_field_evidence"] == [
        {
            "field": "problems",
            "source_evidence": {"source_message_index": 1},
        }
    ]


def test_focused_daily_submit_marks_reviewed_omitted_sections_empty() -> None:
    arguments, _ = compile_focused_daily_plan_arguments(
        {
            "date_selection": "server_default",
            "fields": {
                "today_work": [
                    {
                        "source_message_index": 1,
                        "exact_quote": "今日完成合同复核",
                    }
                ],
                "problems": [],
                "tomorrow_plan": [
                    {
                        "source_message_index": 1,
                        "exact_quote": "明天继续整理附件",
                    }
                ],
            },
            "empty_field_evidence": [],
            "submit_after_write": True,
            "submission_evidence": {
                "source_message_index": 1,
                "exact_quote": "请立即提交",
            },
            "reply": "已按原文记录并提交。",
        }
    )

    assert arguments["acknowledged_empty_fields"] == ["problems"]
    assert arguments["reviewed_omitted_empty_fields"] == ["problems"]
    assert arguments["empty_field_evidence"] == []


def test_focused_daily_submit_requires_exact_current_authorization() -> None:
    with pytest.raises(ValidationError):
        compile_focused_daily_plan_arguments(
            {
                "date_selection": "server_default",
                "fields": {
                    "today_work": [
                        {
                            "source_message_index": 1,
                            "exact_quote": "今日完成合同复核",
                        }
                    ],
                    "problems": [],
                    "tomorrow_plan": [],
                },
                "empty_field_evidence": [],
                "submit_after_write": True,
                "reply": "已提交。",
            }
        )


def test_focused_daily_draft_does_not_mark_omitted_sections_empty() -> None:
    arguments, _ = compile_focused_daily_plan_arguments(
        {
            "date_selection": "server_default",
            "fields": {
                "today_work": [
                    {
                        "source_message_index": 1,
                        "exact_quote": "今日完成合同复核",
                    }
                ],
                "problems": [],
                "tomorrow_plan": [],
            },
            "empty_field_evidence": [],
            "submit_after_write": False,
            "reply": "已记录，尚缺{{daily_missing_section_labels}}。",
        }
    )

    assert arguments["acknowledged_empty_fields"] == []
    assert arguments.get("reviewed_omitted_empty_fields", []) == []


def test_ordinary_daily_tool_cannot_author_reviewed_omission() -> None:
    assert "reviewed_omitted_empty_fields" not in (
        model_add_daily_items_schema()["properties"]
    )


def test_focused_daily_repair_derives_empty_acknowledgement_from_evidence() -> None:
    decision, arguments, _ = parse_focused_daily_add_decision(
        json.dumps(
            {
                "decision": "daily_add",
                "arguments": {
                    "date_selection": "server_default",
                    "fields": {
                        "today_work": [
                            {
                                "source_message_index": 1,
                                "exact_quote": "完成合同复核",
                            }
                        ],
                        "problems": [],
                        "tomorrow_plan": [],
                    },
                    "empty_field_evidence": [
                        {
                            "field": "problems",
                            "source_evidence": {
                                "source_message_index": 1,
                            },
                        }
                    ],
                },
                "reply": "已按原文记录。",
            },
            ensure_ascii=False,
        )
    )

    assert decision == "daily_add"
    assert arguments is not None
    assert arguments["acknowledged_empty_fields"] == ["problems"]


def test_submit_only_focused_plan_falls_back_to_full_agent2() -> None:
    submit_only = NativeToolCall(
        tool_call_id="submit-only-plan",
        tool_name="add_daily_items",
        arguments={
            "date_selection": "server_default",
            "items": [],
            "submit_after_write": True,
            "submission_evidence": {
                "source_message_index": 1,
                "exact_quote": "确认提交",
            },
        },
    )

    parsed = _parse_focused_daily_completion(
        _focused_daily_plan_completion(submit_only).message,
        call_scope="submit-only",
    )

    assert parsed.tool_calls == ()
    assert json.loads(parsed.assistant_message["content"]) == {
        "decision": "not_daily"
    }


def test_focused_daily_fields_accept_equivalent_source_evidence_wrapper() -> None:
    decision, arguments, reply = parse_focused_daily_add_decision(
        json.dumps(
            {
                "decision": "daily_add",
                "arguments": {
                    "date_selection": "server_default",
                    "fields": {
                        "today_work": [
                            {
                                "source_evidence": {
                                    "source_message_index": 1,
                                    "exact_quote": "完成合同复核",
                                }
                            }
                        ],
                        "problems": [],
                        "tomorrow_plan": [],
                    },
                },
                "reply": "已按原文记录。",
            },
            ensure_ascii=False,
        )
    )

    assert decision == "daily_add"
    assert reply == "已按原文记录。"
    assert arguments is not None
    assert arguments["items"][0]["content"] == "完成合同复核"


def test_focused_daily_repair_accepts_quote_alias_without_reply_draft() -> None:
    decision, arguments, reply = parse_focused_daily_add_decision(
        json.dumps(
            {
                "decision": "daily_add",
                "arguments": {
                    "date_selection": "server_default",
                    "fields": {
                        "today_work": [
                            {
                                "source_message_index": 1,
                                "quote": "完成合同复核",
                            }
                        ],
                        "problems": [],
                        "tomorrow_plan": [],
                    },
                },
            },
            ensure_ascii=False,
        )
    )

    assert decision == "daily_add"
    assert reply is None
    assert arguments is not None
    assert arguments["items"][0]["content"] == "完成合同复核"


@pytest.mark.asyncio
async def test_compact_long_daily_call_is_materialized_and_committed_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _DeferredRuntime()
    adapter = _adapter()
    completions = iter(
        (
            _tool_completion(_compact_long_daily_call()),
            _tool_completion(
                _compact_long_daily_call("reviewed-compact-long-daily-1")
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "reply": "已按原文完整记录这三项内容。",
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
        user_text=(
            "今天完成A合同复核并向业务反馈两项修改意见；随后与财务核对B项目付款节点。"
            "明天继续跟进C案件证据清单。"
        ),
        context=_context(),
        runtime_session=runtime,
    )

    assert result.final_content == "已按原文完整记录这三项内容。"
    assert runtime.execute_count == 1
    assert runtime.commit_count == 1
    assert runtime.rollback_count == 0
    assert [item["content"] for item in runtime.calls[0].arguments["items"]] == [
        "今天完成A合同复核并向业务反馈两项修改意见",
        "随后与财务核对B项目付款节点",
        "明天继续跟进C案件证据清单",
    ]


@pytest.mark.asyncio
async def test_malformed_long_daily_uses_bounded_compact_repair_without_full_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _DeferredRuntime()
    adapter = _adapter()
    completions = iter(
        (
            _malformed_daily_completion(),
            _focused_daily_plan_completion(
                _compact_long_daily_call("compact-repair-1")
            ),
            _focused_daily_review_completion(approved=True),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "reply": "已按原文完整记录。",
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
    requests: list[dict] = []

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        requests.append(
            {
                "system": messages[0]["content"],
                "tool_names": tuple(
                    item["function"]["name"] for item in tool_schemas
                ),
                "thinking_enabled": thinking_enabled,
                "user_content": messages[-1]["content"],
            }
        )
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 full production prompt",
        user_text=LONG_DAILY_TEXT,
        context=_context(),
        runtime_session=runtime,
        thinking_enabled=True,
    )

    assert result.final_content == "已按原文完整记录。"
    assert runtime.execute_count == 1
    assert runtime.commit_count == 1
    assert runtime.rollback_count == 0
    assert len(requests) == 3
    assert requests[0]["tool_names"] == ("plan_daily_report",)
    assert requests[0]["thinking_enabled"] is True
    assert requests[0]["user_content"] == LONG_DAILY_TEXT
    assert requests[0]["system"] != "Agent2 full production prompt"
    assert "self-contained pure Daily" in requests[0]["system"]
    assert "all three keys" in requests[0]["system"]
    assert requests[1]["tool_names"] == ("plan_daily_report",)
    assert requests[1]["thinking_enabled"] is True
    assert requests[1]["system"] != "Agent2 full production prompt"
    assert "previous focused Daily plan" in requests[1]["system"]
    assert requests[2]["tool_names"] == ("review_daily_plan",)
    assert requests[2]["thinking_enabled"] is True
    assert "independent Agent2 Daily plan verifier" in requests[2][
        "system"
    ]
    assert "asking to fill, write, record, draft, or save" in requests[2][
        "system"
    ]
    assert "Never reject or repair a non-submitting draft" in requests[2][
        "system"
    ]


@pytest.mark.asyncio
async def test_exact_quote_mismatch_uses_fast_source_repair_then_full_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "完成合同复核。"
    invalid = NativeToolCall(
        tool_call_id="invalid-source-quote",
        tool_name="add_daily_items",
        arguments={
            "date_selection": "server_default",
            "items": [
                {
                    "field": "today_work",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "完成合同审核",
                    },
                }
            ],
        },
    )
    repaired = NativeToolCall(
        tool_call_id="repaired-source-quote",
        tool_name="add_daily_items",
        arguments={
            "date_selection": "server_default",
            "items": [
                {
                    "field": "today_work",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": source,
                    },
                }
            ],
        },
    )
    completions = iter(
        (
            _focused_daily_plan_completion(invalid),
            _focused_daily_plan_completion(repaired),
            _focused_daily_review_completion(approved=True),
        )
    )
    requests: list[dict[str, object]] = []
    runtime = _DeferredRuntime()
    adapter = _adapter()

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        requests.append(
            {
                "system": messages[0]["content"],
                "tool_names": tuple(
                    item["function"]["name"] for item in tool_schemas
                ),
                "thinking_enabled": thinking_enabled,
            }
        )
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 full production prompt",
        user_text=source,
        context=_context(),
        runtime_session=runtime,
        thinking_enabled=True,
    )

    assert result.final_content == "已按原文完整记录。"
    assert requests[1]["tool_names"] == ("plan_daily_report",)
    assert requests[1]["thinking_enabled"] is False
    assert "previous focused Daily plan" in requests[1]["system"]
    assert requests[2]["tool_names"] == ("review_daily_plan",)
    assert requests[2]["thinking_enabled"] is True
    assert runtime.execute_count == 1
    assert runtime.commit_count == 1


@pytest.mark.asyncio
async def test_long_daily_probe_uses_strict_plan_before_scope_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _DeferredRuntime()
    adapter = _adapter()
    completions = iter(
        (
            _focused_daily_plan_completion(
                _compact_long_daily_call("focused-json-primary")
            ),
            _focused_daily_review_completion(approved=True),
        )
    )
    requests: list[dict] = []

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        requests.append(
            {
                "system": messages[0]["content"],
                "tool_names": tuple(
                    item["function"]["name"] for item in tool_schemas
                ),
                "thinking_enabled": thinking_enabled,
            }
        )
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 full production prompt",
        user_text=LONG_DAILY_TEXT,
        context=_context(),
        runtime_session=runtime,
        thinking_enabled=True,
    )

    assert result.final_content == "已按原文完整记录。"
    assert requests[0]["tool_names"] == ("plan_daily_report",)
    assert requests[0]["thinking_enabled"] is True
    assert requests[1]["tool_names"] == ("review_daily_plan",)
    assert len(requests) == 2
    assert runtime.execute_count == 1
    assert runtime.commit_count == 1
    assert runtime.rollback_count == 0


@pytest.mark.asyncio
async def test_focused_review_length_without_verdict_gets_one_fast_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _DeferredRuntime()
    adapter = _adapter()
    completions = iter(
        (
            _focused_daily_plan_completion(
                _compact_long_daily_call("focused-review-retry")
            ),
            _CompletionResponse(
                message={"role": "assistant", "content": ""},
                metadata={"finish_reason": "length"},
            ),
            _focused_daily_review_completion(approved=True),
        )
    )
    requests: list[dict[str, object]] = []

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        requests.append(
            {
                "tool_names": tuple(
                    item["function"]["name"] for item in tool_schemas
                ),
                "thinking_enabled": thinking_enabled,
            }
        )
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 full production prompt",
        user_text=(
            "今天完成A合同复核并向业务反馈两项修改意见；"
            "随后与财务核对B项目付款节点。"
            "明天继续跟进C案件证据清单。"
        ),
        context=_context(),
        runtime_session=runtime,
        thinking_enabled=True,
    )

    assert result.final_content == "已按原文完整记录。"
    assert requests[1]["tool_names"] == ("review_daily_plan",)
    assert requests[1]["thinking_enabled"] is True
    assert requests[2]["tool_names"] == ("review_daily_plan",)
    assert requests[2]["thinking_enabled"] is False
    assert result.model_turns[2].response_metadata[
        "focused_daily_review_fast_retry"
    ] is True
    assert runtime.execute_count == 1
    assert runtime.commit_count == 1


@pytest.mark.asyncio
async def test_complex_daily_requires_a_second_independent_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matters = [f"完成第{index}项合同复核" for index in range(1, 9)]
    source = "今日工作：" + "；".join(matters)
    call = NativeToolCall(
        tool_call_id="complex-daily-plan",
        tool_name="add_daily_items",
        arguments={
            "date_selection": "server_default",
            "items": [
                {
                    "field": "today_work",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": matter,
                    },
                }
                for matter in matters
            ],
        },
    )
    completions = iter(
        (
            _focused_daily_plan_completion(call),
            _focused_daily_review_completion(approved=True),
            _focused_daily_review_completion(approved=True),
        )
    )
    requests: list[list[dict]] = []
    runtime = _DeferredRuntime()
    adapter = _adapter()

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del tool_schemas, thinking_enabled
        requests.append(messages)
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 full production prompt",
        user_text=source,
        context=_context(),
        runtime_session=runtime,
        thinking_enabled=True,
    )

    assert result.final_content == "已按原文完整记录。"
    assert len(requests) == 3
    assert "final independent completeness challenger" in requests[2][0][
        "content"
    ]
    assert result.model_turns[2].response_metadata[
        "focused_daily_completeness_challenge"
    ] is True
    assert runtime.execute_count == 1
    assert runtime.commit_count == 1


@pytest.mark.asyncio
async def test_short_daily_add_uses_the_same_strict_plan_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "完成合同复核但尚未提交系统。"
    runtime = _DeferredRuntime()
    adapter = _adapter()
    call = NativeToolCall(
        tool_call_id="short-daily-plan",
        tool_name="add_daily_items",
        arguments={
            "date_selection": "server_default",
            "items": [
                {
                    "field": "today_work",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": source,
                    },
                }
            ],
        },
    )
    completions = iter(
        (
            _focused_daily_plan_completion(call),
            _focused_daily_review_completion(approved=True),
        )
    )
    requests: list[tuple[str, ...]] = []

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, thinking_enabled
        requests.append(
            tuple(item["function"]["name"] for item in tool_schemas)
        )
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 full production prompt",
        user_text=source,
        context=_context(),
        runtime_session=runtime,
        thinking_enabled=True,
    )

    assert requests[0] == ("plan_daily_report",)
    assert result.final_content == "已按原文完整记录。"
    assert runtime.execute_count == 1
    assert runtime.commit_count == 1
    assert runtime.calls[0].arguments["items"][0]["content"] == source


@pytest.mark.asyncio
async def test_batched_daily_sources_keep_each_message_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_messages = (
        "今日工作：完成甲合同复核。",
        "明日计划：继续跟进乙项目。",
    )
    call = NativeToolCall(
        tool_call_id="batched-daily-plan",
        tool_name="add_daily_items",
        arguments={
            "date_selection": "server_default",
            "items": [
                {
                    "field": "today_work",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "完成甲合同复核",
                    },
                },
                {
                    "field": "tomorrow_plan",
                    "source_evidence": {
                        "source_message_index": 2,
                        "exact_quote": "继续跟进乙项目",
                    },
                },
            ],
        },
    )
    completions = iter(
        (
            _focused_daily_plan_completion(call),
            _focused_daily_review_completion(approved=True),
        )
    )
    requests: list[list[dict]] = []
    runtime = _DeferredRuntime()
    adapter = _adapter()

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del tool_schemas, thinking_enabled
        requests.append(messages)
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 full production prompt",
        user_text="\n".join(user_messages),
        user_messages=user_messages,
        context=_context(),
        runtime_session=runtime,
        thinking_enabled=True,
    )

    assert result.final_content == "已按原文完整记录。"
    assert [
        item["source_evidence"]["source_message_index"]
        for item in runtime.calls[0].arguments["items"]
    ] == [1, 2]
    planner_prompt = requests[0][0]["content"]
    assert "one-based source_message_index" in planner_prompt
    assert "use index 1 only when there is one source message" in planner_prompt


@pytest.mark.asyncio
async def test_focused_daily_can_submit_without_inventing_a_missing_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "今日完成合同复核，明天继续跟进，请立即提交。"
    call = NativeToolCall(
        tool_call_id="focused-incomplete-submit",
        tool_name="add_daily_items",
        arguments={
            "date_selection": "server_default",
            "items": [
                {
                    "field": "today_work",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "今日完成合同复核",
                    },
                },
                {
                    "field": "tomorrow_plan",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "明天继续跟进",
                    },
                },
            ],
            "submit_after_write": True,
            "submission_evidence": {
                "source_message_index": 1,
                "exact_quote": "请立即提交",
            },
        },
    )
    completions = iter(
        (
            _focused_daily_plan_completion(call),
            _focused_daily_review_completion(approved=True),
        )
    )
    request_count = 0
    runtime = _DeferredRuntime()
    adapter = _adapter()

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        nonlocal request_count
        del messages, tool_schemas, thinking_enabled
        request_count += 1
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 full production prompt",
        user_text=source,
        context=_context(),
        runtime_session=runtime,
        thinking_enabled=True,
    )

    assert result.final_content == "已按原文完整记录。"
    assert request_count == 2
    assert runtime.execute_count == 1
    assert runtime.commit_count == 1
    assert runtime.calls[0].arguments["submit_after_write"] is True
    assert runtime.calls[0].arguments["acknowledged_empty_fields"] == [
        "problems"
    ]
    assert runtime.calls[0].arguments["reviewed_omitted_empty_fields"] == [
        "problems"
    ]
    assert {item["field"] for item in runtime.calls[0].arguments["items"]} == {
        "today_work",
        "tomorrow_plan",
    }


@pytest.mark.asyncio
async def test_long_daily_accepts_first_valid_plan_before_trailing_alternative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _DeferredRuntime()
    adapter = _adapter()
    plan_completion = _focused_daily_plan_completion(
        _compact_long_daily_call("focused-plan-trailing-duplicate")
    )
    function = plan_completion.message["tool_calls"][0]["function"]
    complete_arguments = function["arguments"]
    function["arguments"] = complete_arguments + json.dumps(
        {"alternative": "must not replace the first validated plan"}
    )
    completions = iter(
        (
            plan_completion,
            _focused_daily_review_completion(approved=True),
        )
    )

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 full production prompt",
        user_text=LONG_DAILY_TEXT,
        context=_context(),
        runtime_session=runtime,
        thinking_enabled=True,
    )

    assert result.final_content == "已按原文完整记录。"
    assert runtime.execute_count == 1
    assert runtime.commit_count == 1
    assert runtime.rollback_count == 0
    assert any(
        audit.parse_status == "salvaged_focused_daily_tool"
        for audit in result.raw_tool_call_audit
    )


@pytest.mark.asyncio
async def test_focused_review_repairs_over_split_daily_before_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "今天参加专项会议并整理会议纪要。"
    over_split = NativeToolCall(
        tool_call_id="over-split-plan",
        tool_name="add_daily_items",
        arguments={
            "date_selection": "server_default",
            "items": [
                {
                    "field": "today_work",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "参加专项会议",
                    },
                },
                {
                    "field": "today_work",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "整理会议纪要",
                    },
                },
            ],
        },
    )
    repaired = NativeToolCall(
        tool_call_id="repaired-plan",
        tool_name="add_daily_items",
        arguments={
            "date_selection": "server_default",
            "items": [
                {
                    "field": "today_work",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "参加专项会议并整理会议纪要",
                    },
                }
            ],
        },
    )
    completions = iter(
        (
            _focused_daily_plan_completion(over_split),
            _focused_daily_repair_review_completion(
                "Two quotes over-split one dependent action chain."
            ),
            _focused_daily_plan_completion(repaired),
            _focused_daily_review_completion(approved=True),
        )
    )
    requests: list[dict[str, object]] = []
    runtime = _DeferredRuntime()
    adapter = _adapter()

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        requests.append(
            {
                "system": messages[0]["content"],
                "tool_names": tuple(
                    item["function"]["name"] for item in tool_schemas
                ),
                "thinking_enabled": thinking_enabled,
            }
        )
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 full production prompt",
        user_text=source,
        context=_context(),
        runtime_session=runtime,
        thinking_enabled=True,
    )

    assert result.final_content == "已按原文完整记录。"
    assert runtime.execute_count == 1
    assert runtime.commit_count == 1
    assert len(runtime.calls[0].arguments["items"]) == 1
    assert runtime.calls[0].arguments["items"][0]["content"] == (
        "参加专项会议并整理会议纪要"
    )
    assert requests[1]["thinking_enabled"] is True
    assert requests[2]["tool_names"] == ("plan_daily_report",)
    assert requests[2]["thinking_enabled"] is True
    assert requests[3]["tool_names"] == ("review_daily_plan",)
    assert requests[3]["thinking_enabled"] is False


@pytest.mark.asyncio
async def test_focused_daily_allows_one_format_and_one_semantic_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _DeferredRuntime()
    adapter = _adapter()
    completions = iter(
        (
            _malformed_daily_completion(),
            _focused_daily_plan_completion(
                _compact_long_daily_call("first-and-only-repair")
            ),
                _focused_daily_repair_review_completion(
                    "candidate would require another repair"
                ),
                _focused_daily_plan_completion(
                    _compact_long_daily_call("semantic-repair")
                ),
                _focused_daily_review_completion(approved=True),
            )
    )

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, tool_schemas, thinking_enabled
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 full production prompt",
        user_text=LONG_DAILY_TEXT,
        context=_context(),
        runtime_session=runtime,
        thinking_enabled=True,
    )

    assert result.final_content == "已按原文完整记录。"
    assert runtime.execute_count == 1
    assert runtime.commit_count == 1
    assert runtime.rollback_count == 0


@pytest.mark.asyncio
async def test_long_non_daily_probe_falls_back_to_the_unchanged_reasoning_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _DeferredRuntime()
    adapter = _adapter()
    thinking_modes: list[bool] = []
    completion_count = 0
    final_reply = "我已按原有流程分析这项非日报问题。"

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        nonlocal completion_count
        del tool_schemas
        completion_count += 1
        thinking_modes.append(thinking_enabled)
        system_message = str(messages[0]["content"])
        if "Agent2's fast planner" in system_message:
            return _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": json.dumps({"decision": "not_daily"}),
                },
                metadata={"finish_reason": "stop"},
            )
        if "independent Agent2 Daily plan verifier" in system_message:
            return _focused_daily_review_completion(
                approved=False,
                reason="not a pure Daily add",
            )
        if system_message == "Agent2 full production prompt":
            return _CompletionResponse(
                message={"role": "assistant", "content": final_reply},
                metadata={"finish_reason": "stop"},
            )
        review_facts = json.loads(messages[1]["content"])
        return _CompletionResponse(
            message={
                "role": "assistant",
                "content": json.dumps(
                    {
                        "decision": "keep",
                        "classification": "ordinary_reply",
                        "reviewed_reply_sha256": review_facts[
                            "reviewed_reply_sha256"
                        ],
                        "pending_reference": None,
                        "replacement_reply": None,
                    }
                ),
            },
            metadata={"finish_reason": "stop"},
        )

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 full production prompt",
        user_text=(
            "请分析一个与日报填写无关的复杂问题，并结合上下文给出完整说明。"
            "这里补充若干背景信息，用于验证较长输入不会被快速探测直接当作最终答案。"
            "系统应继续沿用原来的高强度分析流程处理非日报任务，不应改变原有功能。"
            "为了达到与线上长消息相近的长度，再补充一段背景描述和限制条件。"
            "最终回答只需要处理这个独立问题，不需要新增、修改或提交任何日报内容。"
        ),
        context=_context(),
        runtime_session=runtime,
        thinking_enabled=True,
    )

    assert result.final_content == final_reply
    assert thinking_modes[:2] == [True, True]
    assert runtime.execute_count == 0
    assert runtime.commit_count == 0
    assert runtime.rollback_count == 0


@pytest.mark.asyncio
async def test_today_report_display_falls_back_and_calls_fresh_read_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _QueryRuntime()
    adapter = _adapter()
    query_issued = False

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        nonlocal query_issued
        del tool_schemas, thinking_enabled
        system_message = str(messages[0]["content"])
        if "Agent2's fast planner" in system_message:
            return _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": json.dumps({"decision": "not_daily"}),
                },
                metadata={"finish_reason": "stop"},
            )
        if system_message == "Agent2 full production prompt":
            if not query_issued:
                query_issued = True
                return _CompletionResponse(
                    message={
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "fresh-today-query",
                                "type": "function",
                                "function": {
                                    "name": "query_today_report",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    },
                    metadata={"finish_reason": "tool_calls"},
                )
            return _CompletionResponse(
                message={"role": "assistant", "content": "今天完成了合同复核。"},
                metadata={"finish_reason": "stop"},
            )
        review_facts = json.loads(messages[1]["content"])
        return _CompletionResponse(
            message={
                "role": "assistant",
                "content": json.dumps(
                    {
                        "decision": "keep",
                        "classification": "ordinary_reply",
                        "reviewed_reply_sha256": review_facts[
                            "reviewed_reply_sha256"
                        ],
                        "pending_reference": None,
                        "replacement_reply": None,
                    }
                ),
            },
            metadata={"finish_reason": "stop"},
        )

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 full production prompt",
        user_text="查看我今天的日报内容",
        context=_context(
            allowed_tool_names=frozenset(
                {"add_daily_items", "query_today_report"}
            )
        ),
        runtime_session=runtime,
        thinking_enabled=True,
    )

    assert result.final_content == "今天完成了合同复核。"
    assert query_issued is True
    assert [call.tool_name for call in runtime.calls] == ["query_today_report"]


@pytest.mark.asyncio
async def test_long_mixed_turn_review_vetoes_daily_write_and_restores_full_agent2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _DeferredRuntime()
    adapter = _adapter()
    requests: list[dict] = []
    full_reply = "我会按完整流程同时处理日报内容和查询请求。"

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        system_message = str(messages[0]["content"])
        requests.append(
            {
                "system": system_message,
                "tool_names": tuple(
                    item["function"]["name"] for item in tool_schemas
                ),
                "thinking_enabled": thinking_enabled,
            }
        )
        if "Agent2's fast planner" in system_message:
            return _focused_daily_completion(
                _compact_long_daily_call("mixed-primary")
            )
        if "independent Agent2 Daily plan verifier" in system_message:
            return _focused_daily_review_completion(
                approved=False,
                reason="mixed task",
            )
        if system_message == "Agent2 full production prompt":
            return _CompletionResponse(
                message={"role": "assistant", "content": full_reply},
                metadata={"finish_reason": "stop"},
            )
        review_facts = json.loads(messages[1]["content"])
        return _CompletionResponse(
            message={
                "role": "assistant",
                "content": json.dumps(
                    {
                        "decision": "keep",
                        "classification": "ordinary_reply",
                        "reviewed_reply_sha256": review_facts[
                            "reviewed_reply_sha256"
                        ],
                        "pending_reference": None,
                        "replacement_reply": None,
                    }
                ),
            },
            metadata={"finish_reason": "stop"},
        )

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 full production prompt",
        user_text=(
            LONG_DAILY_TEXT
            + "另外请查询综合管理部本周尚未闭环的事项，并把查询结果一并告诉我。"
        ),
        context=_context(),
        runtime_session=runtime,
        thinking_enabled=True,
    )

    assert result.final_content == full_reply
    assert requests[0]["thinking_enabled"] is True
    assert requests[1]["thinking_enabled"] is True
    assert requests[2]["system"] == "Agent2 full production prompt"
    assert requests[2]["thinking_enabled"] is True
    assert runtime.execute_count == 0
    assert runtime.commit_count == 0
    assert runtime.rollback_count == 0


@pytest.mark.asyncio
async def test_long_daily_date_disagreement_falls_back_without_executing_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _candidate_report()
    runtime = _DeferredRuntime()
    adapter = _adapter()
    full_reply = "日期判断不一致，已回到完整流程重新处理。"
    trusted_call = _compact_long_daily_call("date-disagreement-primary")
    trusted_call = NativeToolCall(
        tool_call_id=trusted_call.tool_call_id,
        tool_name=trusted_call.tool_name,
        arguments={
            **trusted_call.arguments,
            "date_selection": "trusted_report",
            "report_id": str(report.report_id),
            "expected_version": report.version,
        },
    )
    full_path_seen = False

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        nonlocal full_path_seen
        del tool_schemas, thinking_enabled
        system_message = str(messages[0]["content"])
        if "Agent2's fast planner" in system_message:
            return _focused_daily_completion(trusted_call)
        if "independent Agent2 Daily plan verifier" in system_message:
            return _focused_daily_review_completion(
                approved=False,
                reason="non-default report target",
            )
        if system_message == "Agent2 full production prompt":
            full_path_seen = True
            return _CompletionResponse(
                message={"role": "assistant", "content": full_reply},
                metadata={"finish_reason": "stop"},
            )
        review_facts = json.loads(messages[1]["content"])
        return _CompletionResponse(
            message={
                "role": "assistant",
                "content": json.dumps(
                    {
                        "decision": "keep",
                        "classification": "ordinary_reply",
                        "reviewed_reply_sha256": review_facts[
                            "reviewed_reply_sha256"
                        ],
                        "pending_reference": None,
                        "replacement_reply": None,
                    }
                ),
            },
            metadata={"finish_reason": "stop"},
        )

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 full production prompt",
        user_text=LONG_DAILY_TEXT,
        context=_context(report=report),
        runtime_session=runtime,
        thinking_enabled=True,
    )

    assert result.final_content == full_reply
    assert full_path_seen is True
    assert runtime.execute_count == 0
    assert runtime.commit_count == 0
    assert runtime.rollback_count == 0


@pytest.mark.asyncio
async def test_compact_repaired_long_daily_reuses_model_reply_without_third_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _DeferredRuntime()
    adapter = _adapter()
    completions = iter(
        (
            _malformed_daily_completion(),
            _focused_daily_plan_completion(
                _compact_long_daily_call("compact-repair-rollback")
            ),
            _focused_daily_review_completion(approved=True),
        )
    )

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, tool_schemas, thinking_enabled
        try:
            return next(completions)
        except StopIteration:
            raise DeepSeekTimeoutError("terminal reply timed out")

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 full production prompt",
        user_text=LONG_DAILY_TEXT,
        context=_context(),
        runtime_session=runtime,
        thinking_enabled=True,
    )

    assert result.final_content == "已按原文完整记录。"
    assert runtime.execute_count == 1
    assert runtime.commit_count == 1
    assert runtime.rollback_count == 0


@pytest.mark.asyncio
async def test_long_daily_actions_bind_every_item_to_the_complete_current_source() -> None:
    source = CurrentTurnSource(
        (
            "今天完成A合同复核并向业务反馈两项修改意见；随后与财务核对B项目付款节点。"
            "明天继续跟进C案件证据清单。",
        )
    )
    call = _long_daily_call()

    source.validate_tool_arguments(call.tool_name, call.arguments)
    bound, failure = await ShadowCallBinder(
        _context(),
        UnavailableDateResolver(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
        current_turn_source=source,
    ).bind(call)

    assert failure is None
    assert bound is not None
    assert [item["source_evidence"] for item in bound.arguments["items"]] == [
        {
            "source_message_index": 1,
            "exact_quote": "今天完成A合同复核并向业务反馈两项修改意见",
        },
        {
            "source_message_index": 1,
            "exact_quote": "随后与财务核对B项目付款节点",
        },
        {
            "source_message_index": 1,
            "exact_quote": "明天继续跟进C案件证据清单",
        },
    ]


@pytest.mark.asyncio
async def test_final_model_timeout_after_long_write_draft_rolls_back_the_whole_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _DeferredRuntime()
    adapter = _adapter()
    completions = iter(
        (
            _tool_completion(_long_daily_call()),
            _tool_completion(_long_daily_call("reviewed-long-daily-1")),
        )
    )

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del messages, tool_schemas, thinking_enabled
        try:
            return next(completions)
        except StopIteration:
            raise DeepSeekTimeoutError("terminal reply timed out")

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    with pytest.raises(DeepSeekTimeoutError):
        await adapter.run_canary_turn(
            system_prompt="Agent2 test",
            user_text=(
                "今天完成A合同复核并向业务反馈两项修改意见；随后与财务核对B项目付款节点。"
                "明天继续跟进C案件证据清单。"
            ),
            context=_context(),
            runtime_session=runtime,
        )

    assert runtime.execute_count == 1
    assert runtime.commit_count == 0
    assert runtime.rollback_count == 1


@pytest.mark.asyncio
async def test_persistently_empty_final_model_reply_rolls_back_the_whole_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _DeferredRuntime()
    adapter = _adapter()
    completions = iter(
        (
            _tool_completion(_long_daily_call()),
            _tool_completion(_long_daily_call("reviewed-long-daily-1")),
            _CompletionResponse(
                message={"role": "assistant", "content": "  "},
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
            user_text="今天完成三项较长工作，按原话完整记录。",
            context=_context(),
            runtime_session=runtime,
        )

    assert runtime.execute_count == 1
    assert runtime.commit_count == 0
    assert runtime.rollback_count == 1


class _ScalarResult:
    def __init__(self, value=None, rows=()):
        self.value = value
        self.rows = tuple(rows)

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return list(self.rows)


@pytest.mark.asyncio
async def test_same_provider_message_reuses_the_original_ingress_claim_without_a_second_event() -> None:
    event_id = uuid4()
    original = SimpleNamespace(
        id=event_id,
        idempotency_key="dingtalk:provider-message-1",
        response_payload={"msgtype": "text", "text": {"content": "已记录。"}},
    )
    claim = MessageIngressClaim(
        idempotency_key=original.idempotency_key,
        platform="dingtalk",
        external_message_id="provider-message-1",
        webhook_event_id=event_id,
    )

    class Session:
        execute_count = 0

        async def execute(self, _statement):
            self.execute_count += 1
            return (
                _ScalarResult(None)
                if self.execute_count == 1
                else _ScalarResult(rows=(claim,))
            )

        async def get(self, model, identity):
            assert model is WebhookEvent
            assert identity == event_id
            return original

    session = Session()
    event, inserted = await create_webhook_event_once(
        session,  # type: ignore[arg-type]
        idempotency_key="dingtalk:provider-message-1",
        external_message_id="provider-message-1",
        dingtalk_user_id="ding-user-1",
        payload={"text": {"content": "重复重投的长日报"}},
    )

    assert inserted is False
    assert event is original
    assert session.execute_count == 2


def _candidate_report() -> TrustedReportSnapshot:
    return TrustedReportSnapshot(
        report_id=REPORT_ID,
        tenant_id=TENANT_ID,
        owner_user_id=USER_ID,
        report_date=date(2026, 8, 11),
        version=5,
        status="collecting",
        items=(
            TrustedReportItem(
                item_id="work-1",
                field="today_work",
                content="完成合同复核",
                report_id=REPORT_ID,
                report_version=5,
            ),
        ),
    )


def _candidate_operation() -> TrustedRecentOperation:
    return TrustedRecentOperation(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        conversation_id="conversation-1",
        source_message_id="previous-message",
        tool_call_id="previous-add",
        tool_name="add_daily_items",
        status="success",
        changed=True,
        target_type="daily_report",
        target_id=str(REPORT_ID),
        before_version=4,
        after_version=5,
        occurred_at=NOW - timedelta(minutes=1),
        report_reference=TrustedReportReference(
            report_id=REPORT_ID,
            report_date=date(2026, 8, 11),
            report_version=5,
            report_status="collecting",
        ),
    )


@pytest.mark.asyncio
async def test_natural_confirmation_uses_model_selected_trusted_report_not_program_phrases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _candidate_report()
    context = _context(
        source_message_id="confirmation-message",
        report=report,
        recent_messages=(
            TrustedRecentMessage(
                role="assistant",
                content="我已把昨天的工作候选整理出来，需要我按这份日报提交吗？",
                source_message_id="previous-message:assistant",
            ),
        ),
        recent_operations=(_candidate_operation(),),
    )
    confirm_call = NativeToolCall(
        tool_call_id="model-confirmed",
        tool_name="confirm_report",
        arguments={"report_id": str(REPORT_ID), "expected_version": 5},
    )
    runtime = _DeferredRuntime()
    runtime_receipt = ToolReceipt(
        status=ReceiptStatus.SUCCESS,
        tool_name="confirm_report",
        changed=True,
        target_type="daily_report",
        target_id=str(REPORT_ID),
        before_version=5,
        after_version=6,
        safe_user_facts={"actual_write": True, "report_status": "completed"},
        execution_mode=ExecutionMode.CANARY_EXECUTE,
    )

    async def execute(calls, *, defer_finalization):
        assert defer_finalization is True
        runtime.execute_count += 1
        runtime.calls = calls
        return ProductionRuntimeResult(
            status="success",
            receipts=(runtime_receipt,),
            transaction_opened=True,
            transaction_pending=True,
            handler_call_count=1,
        )

    async def commit_pending():
        runtime.commit_count += 1
        return ProductionRuntimeResult(
            status="success",
            receipts=(runtime_receipt,),
            transaction_opened=True,
            committed_to_outer_transaction=True,
            handler_call_count=1,
            business_write_count=1,
            receipt_write_count=1,
        )

    runtime.execute = execute  # type: ignore[method-assign]
    runtime.commit_pending = commit_pending  # type: ignore[method-assign]
    adapter = _adapter()
    completions = iter(
        (
            _tool_completion(confirm_call),
            _tool_completion(
                NativeToolCall(
                    tool_call_id="independent-confirm-review",
                    tool_name="confirm_report",
                    arguments={
                        "report_id": str(REPORT_ID),
                        "expected_version": 5,
                    },
                )
            ),
            _CompletionResponse(
                message={
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "reply": "好的，已按刚才那份日报提交。",
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
    captured_first_request: list[dict] = []

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        del tool_schemas, thinking_enabled
        if not captured_first_request:
            captured_first_request.extend(messages)
        return next(completions)

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text="行，照刚才那个来",
        context=context,
        runtime_session=runtime,
    )

    assert result.final_content == "好的，已按刚才那份日报提交。"
    assert len(runtime.calls) == 1
    assert runtime.calls[0].tool_name == "confirm_report"
    assert runtime.calls[0].arguments == confirm_call.arguments
    first_user_payload = json.loads(captured_first_request[1]["content"])
    assert first_user_payload["user_message"] == "行，照刚才那个来"
    assert first_user_payload["trusted_context"]["recent_messages"][0][
        "content"
    ].endswith("需要我按这份日报提交吗？")
    assert first_user_payload["trusted_context"]["recent_operations"][0][
        "report_reference"
    ]["report_id"] == str(REPORT_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_text", "model_reply"),
    [
        ("算了，先不提交", "好的，这份日报保持未提交。"),
        ("换个话题，查下刘聪最近的工作", "好的，我先处理你现在的新问题。"),
    ],
)
async def test_correction_or_topic_change_does_not_reuse_a_prior_write_candidate(
    monkeypatch: pytest.MonkeyPatch,
    user_text: str,
    model_reply: str,
) -> None:
    report = _candidate_report()
    context = _context(
        source_message_id="new-topic-message",
        report=report,
        recent_messages=(
            TrustedRecentMessage(
                role="assistant",
                content="我已把昨天的工作候选整理出来，需要我按这份日报提交吗？",
                source_message_id="previous-message:assistant",
            ),
        ),
        recent_operations=(_candidate_operation(),),
    )
    runtime = _DeferredRuntime()
    adapter = _adapter()
    completion_count = 0

    async def fake_complete(messages, *, tool_schemas, thinking_enabled):
        nonlocal completion_count
        del tool_schemas, thinking_enabled
        completion_count += 1
        content = model_reply
        if completion_count == 2:
            review_facts = json.loads(messages[1]["content"])
            content = json.dumps(
                {
                    "decision": "keep",
                    "classification": "ordinary_reply",
                    "reviewed_reply_sha256": review_facts[
                        "reviewed_reply_sha256"
                    ],
                    "pending_reference": None,
                    "replacement_reply": None,
                }
            )
        return _CompletionResponse(
            message={"role": "assistant", "content": content},
            metadata={"finish_reason": "stop"},
        )

    monkeypatch.setattr(adapter, "_complete", fake_complete)

    result = await adapter.run_canary_turn(
        system_prompt="Agent2 test",
        user_text=user_text,
        context=context,
        runtime_session=runtime,
    )

    assert result.final_content == model_reply
    assert completion_count == 2
    assert runtime.execute_count == 0
    assert runtime.commit_count == 0
    assert runtime.rollback_count == 0


def test_http_webhook_persists_the_post_asr_text_before_creating_the_event() -> None:
    webhook_path = Path(__file__).resolve().parents[1] / "app" / "api" / "webhook.py"
    tree = ast.parse(webhook_path.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "dingtalk_webhook"
    )
    calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)]
    recovery_call = next(
        call
        for call in calls
        if isinstance(call.func, ast.Name)
        and call.func.id == "prepare_recoverable_ingress_payload"
    )
    text_keyword = next(
        keyword for keyword in recovery_call.keywords if keyword.arg == "text"
    )
    assert ast.unparse(text_keyword.value) == "incoming.text"

    create_call = next(
        call
        for call in calls
        if isinstance(call.func, ast.Name)
        and call.func.id == "create_webhook_event_once"
    )
    payload_keyword = next(
        keyword for keyword in create_call.keywords if keyword.arg == "payload"
    )
    assert ast.unparse(payload_keyword.value) == "persisted_payload"


def test_successful_voice_asr_payload_preserves_the_recognized_original() -> None:
    persisted = prepare_recoverable_ingress_payload(
        {
            "senderStaffId": "ding-user-1",
            "msgId": "voice-message-1",
            "conversationId": "conversation-1",
            "msgtype": "voice",
            "content": {"downloadCode": "voice-download-1"},
        },
        text="今天完成合同复核；明天继续跟进证据清单",
        message_type="voice",
        voice_download_seconds=0.0,
        voice_transcribe_seconds=0.0,
    )

    assert persisted["_agent2_stream_ingress_v1"]["text"] == (
        "今天完成合同复核；明天继续跟进证据清单"
    )
    assert _text_content(persisted) == "今天完成合同复核；明天继续跟进证据清单"


def test_failed_voice_asr_payload_never_persists_download_metadata_as_text() -> None:
    persisted = prepare_recoverable_ingress_payload(
        {
            "senderStaffId": "ding-user-1",
            "msgId": "voice-message-2",
            "conversationId": "conversation-1",
            "msgtype": "voice",
            "content": {"downloadCode": "voice-download-2"},
        },
        text="",
        message_type="voice",
        voice_download_seconds=0.0,
        voice_transcribe_seconds=0.0,
    )

    assert persisted["_agent2_stream_ingress_v1"]["text"] == ""
    assert _text_content(persisted) == ""


def test_http_normal_text_payload_keeps_exact_original_without_fallback() -> None:
    persisted = prepare_recoverable_ingress_payload(
        {
            "senderStaffId": "ding-user-1",
            "msgId": "text-message-1",
            "conversationId": "conversation-1",
            "msgtype": "text",
            "text": {"content": "原始文本里的第一项和第二项"},
        },
        text="原始文本里的第一项和第二项",
        message_type="text",
        voice_download_seconds=0.0,
        voice_transcribe_seconds=0.0,
    )

    assert persisted["_agent2_stream_ingress_v1"]["text"] == (
        "原始文本里的第一项和第二项"
    )
    assert _text_content(persisted) == "原始文本里的第一项和第二项"


def test_stream_recognized_voice_text_is_preserved_as_recoverable_original_input() -> None:
    payload = prepare_recoverable_ingress_payload(
        {
            "conversationId": "conversation-1",
            "msgtype": "voice",
            "content": {"downloadCode": "download-only"},
        },
        text="今天完成A合同复核；明天继续跟进C案件证据清单。",
        message_type="voice",
        voice_download_seconds=0.1,
        voice_transcribe_seconds=1.2,
    )

    assert payload["_agent2_stream_ingress_v1"]["text"] == (
        "今天完成A合同复核；明天继续跟进C案件证据清单。"
    )


@pytest.mark.asyncio
async def test_webhook_asr_text_must_be_persisted_for_the_next_turn_context() -> None:
    event = SimpleNamespace(
        dingtalk_user_id="ding-user-1",
        status="processed",
        idempotency_key="dingtalk:voice-provider-1",
        payload={
            "conversationId": "conversation-1",
            "msgtype": "voice",
            "content": {"downloadCode": "download-only"},
            "_agent2_stream_ingress_v1": {
                "message_type": "voice",
                "recoverable": True,
                "text": "今天完成A合同复核；明天继续跟进C案件证据清单。",
                "voice_download_seconds": 0.0,
                "voice_transcribe_seconds": 0.0,
            },
        },
        response_payload={
            "msgtype": "text",
            "text": {"content": "我整理出了一个写入候选，要继续吗？"},
        },
        received_at=NOW - timedelta(minutes=1),
    )

    class Rows:
        def __init__(self, values):
            self.values = list(values)

        def all(self):
            return list(self.values)

    class Session:
        def __init__(self):
            self.results = [[event], [], [], []]

        async def scalars(self, _statement):
            return Rows(self.results.pop(0))

        async def execute(self, _statement):
            return Rows(self.results.pop(0))

    store = ProductionContextStore(
        Session(),
        user=SimpleNamespace(
            id=USER_ID,
            dingtalk_user_id="ding-user-1",
            timezone="Asia/Shanghai",
            active=True,
        ),
        tenant_id=TENANT_ID,
    )
    request = TrustedContextRequest(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        conversation_id="conversation-1",
        source_message_id="next-message",
        timezone="Asia/Shanghai",
        server_now=NOW,
    )

    messages = await store.load_recent_messages(
        request,
        namespace=CANARY_STATE_NAMESPACE,
        limit=6,
    )

    assert [(message.role, message.content) for message in messages] == [
        (
            "user",
            "今天完成A合同复核；明天继续跟进C案件证据清单。",
        ),
        ("assistant", "我整理出了一个写入候选，要继续吗？"),
    ]


def test_failed_voice_asr_never_turns_download_metadata_into_user_text() -> None:
    payload = prepare_recoverable_ingress_payload(
        {
            "conversationId": "conversation-1",
            "msgtype": "voice",
            "content": {"downloadCode": "download-only"},
        },
        text="",
        message_type="voice",
        voice_download_seconds=0.0,
        voice_transcribe_seconds=1.2,
    )

    assert _text_content(payload) == ""


def test_normal_text_persists_and_reads_the_exact_original_text() -> None:
    payload = prepare_recoverable_ingress_payload(
        {
            "conversationId": "conversation-1",
            "msgtype": "text",
            "text": {"content": "原始文本里的第一项和第二项"},
        },
        text="原始文本里的第一项和第二项",
        message_type="text",
        voice_download_seconds=0.0,
        voice_transcribe_seconds=0.0,
    )

    assert _text_content(payload) == "原始文本里的第一项和第二项"


@pytest.mark.asyncio
async def test_same_provider_write_uses_the_same_operation_fingerprint() -> None:
    source = CurrentTurnSource(("今天完成A合同复核。",))
    call = NativeToolCall(
        tool_call_id="provider-call-1",
        tool_name="add_daily_items",
        arguments={
            "date_selection": "server_default",
            "date_expression": "今天",
            "proposed_date": "2026-08-12",
            "items": [
                {
                    "field": "today_work",
                    "content": "完成A合同复核",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "今天完成A合同复核",
                    },
                }
            ],
        },
    )
    first_context = _context(source_message_id="dingtalk:provider-message-1")
    replay_context = _context(source_message_id="dingtalk:provider-message-1")
    first, first_failure = await ShadowCallBinder(
        first_context,
        UnavailableDateResolver(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
        current_turn_source=source,
    ).bind(call)
    replay, replay_failure = await ShadowCallBinder(
        replay_context,
        UnavailableDateResolver(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
        current_turn_source=source,
    ).bind(call)

    assert first_failure is None and replay_failure is None
    assert first is not None and replay is not None
    assert _prepare_call(first_context, first).operation_fingerprint == _prepare_call(
        replay_context, replay
    ).operation_fingerprint
