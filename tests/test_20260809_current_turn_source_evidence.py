from __future__ import annotations

from datetime import date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.agent2.tool_calling.context import (
    CANARY_STATE_NAMESPACE,
    TrustedContext,
    TrustedDailyWriteRetryCandidate,
    TrustedPrincipal,
    TrustedReportItem,
    TrustedReportSnapshot,
)
from app.agent2.tool_calling.contracts import (
    AddDailyItemsArgs,
    RememberPersonalMemoryArgs,
)
from app.agent2.tool_calling.current_turn_source import (
    CurrentTurnSource,
    CurrentTurnSourceEvidenceError,
)
from app.agent2.tool_calling.production_store import report_state_hash
from app.agent2.tool_calling.registry import deepseek_tool_schemas
from app.agent2.tool_calling.validation import (
    NativeToolCall,
    ShadowCallBinder,
    UnavailableDateResolver,
)


def test_daily_item_requires_current_message_evidence() -> None:
    with pytest.raises(ValidationError):
        AddDailyItemsArgs.model_validate(
            {
                "date_expression": "今天",
                "proposed_date": "2026-08-09",
                "items": [
                    {
                        "field": "today_work",
                        "content": "参加经营管理会议",
                    }
                ],
            }
        )


def test_daily_item_tool_schema_requires_exact_quote() -> None:
    tool_schema = deepseek_tool_schemas(frozenset({"add_daily_items"}))[0]
    schema = tool_schema["function"]["parameters"]

    evidence_schema = schema["$defs"]["DailyItemSourceEvidence"]

    assert "exact_quote" in evidence_schema["required"]
    assert evidence_schema["properties"]["exact_quote"]["type"] == "string"


def test_daily_item_input_rejects_missing_exact_quote() -> None:
    with pytest.raises(ValidationError):
        AddDailyItemsArgs.model_validate(
            {
                "items": [
                    {
                        "field": "today_work",
                        "content": "完成合同复核",
                        "source_evidence": {"source_message_index": 1},
                    }
                ]
            }
        )


@pytest.mark.parametrize(
    ("source_message", "exact_quote"),
    [
        ("未完成合同复核", "完成合同复核"),
        ("完成合同复核但未提交", "完成合同复核"),
    ],
)
def test_daily_item_binder_accepts_a_contiguous_current_message_span(
    source_message: str,
    exact_quote: str,
) -> None:
    source = CurrentTurnSource((source_message,))
    arguments = AddDailyItemsArgs.model_validate(
        {
            "items": [
                {
                    "field": "today_work",
                    "content": "完成合同复核",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": exact_quote,
                    },
                }
            ]
        }
    )

    bound = source.bind_tool_arguments(
        "add_daily_items",
        arguments.model_dump(mode="json"),
    )

    assert bound["items"][0]["content"] == exact_quote


@pytest.mark.parametrize(
    "source_message",
    [
        "今日工作：完成合同复核；明日继续跟进",
        "今日工作 完成合同复核 明日继续跟进",
    ],
)
def test_daily_item_quote_accepts_punctuation_or_whitespace_boundaries(
    source_message: str,
) -> None:
    source = CurrentTurnSource((source_message,))
    arguments = {
        "items": [
            {
                "field": "today_work",
                "content": "已完成合同审核",
                "source_evidence": {
                    "source_message_index": 1,
                    "exact_quote": "完成合同复核",
                },
            }
        ]
    }

    bound = source.bind_tool_arguments("add_daily_items", arguments)

    assert bound["items"][0]["content"] == "完成合同复核"


def test_daily_item_quote_accepts_the_complete_joined_sentence() -> None:
    source_message = "今天做了日报的基础功能优化"
    source = CurrentTurnSource((source_message,))

    bound = source.bind_tool_arguments(
        "add_daily_items",
        {
            "items": [
                {
                    "field": "today_work",
                    "content": "完成日报基础功能优化",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": source_message,
                    },
                }
            ]
        },
    )

    assert bound["items"][0]["content"] == source_message


def test_daily_item_binder_preserves_independently_reviewed_cleanup() -> None:
    source_message = "将合同评审技能变成了网页端的网页agent调用速度快了10倍"
    source = CurrentTurnSource((source_message,))

    bound = source.bind_tool_arguments(
        "add_daily_items",
        {
            "items": [
                {
                    "field": "today_work",
                    "content": "将合同评审技能改造成网页端agent，调用速度提升10倍",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": source_message,
                    },
                }
            ],
            "content_reviewed": True,
        },
    )

    assert bound["items"][0]["content"] == (
        "将合同评审技能改造成网页端agent，调用速度提升10倍"
    )


def test_daily_item_binder_removes_only_a_leading_list_marker() -> None:
    source = CurrentTurnSource(("1. 明天继续跟进签约",))

    bound = source.bind_tool_arguments(
        "add_daily_items",
        {
            "items": [
                {
                    "field": "tomorrow_plan",
                    "content": "明天继续跟进签约",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "1. 明天继续跟进签约",
                    },
                }
            ]
        },
    )

    assert bound["items"][0]["content"] == "明天继续跟进签约"


def test_daily_item_quotes_cannot_overlap_in_one_source_message() -> None:
    source = CurrentTurnSource(
        ("目前对方还没寄回盖章版；明天我去催办并同步项目组",)
    )

    with pytest.raises(
        CurrentTurnSourceEvidenceError,
        match="DAILY_ITEM_SOURCE_SPAN_OVERLAP",
    ):
        source.bind_tool_arguments(
            "add_daily_items",
            {
                "items": [
                    {
                        "field": "tomorrow_plan",
                        "content": "催办",
                        "source_evidence": {
                            "source_message_index": 1,
                            "exact_quote": "明天我去催办",
                        },
                    },
                    {
                        "field": "tomorrow_plan",
                        "content": "同步项目组",
                        "source_evidence": {
                            "source_message_index": 1,
                            "exact_quote": "明天我去催办并同步项目组",
                        },
                    },
                ]
            },
        )


def test_daily_item_quote_must_identify_one_source_occurrence() -> None:
    source = CurrentTurnSource(("完成核对；完成核对",))

    with pytest.raises(
        CurrentTurnSourceEvidenceError,
        match="DAILY_ITEM_SOURCE_SPAN_AMBIGUOUS",
    ):
        source.bind_tool_arguments(
            "add_daily_items",
            {
                "items": [
                    {
                        "field": "today_work",
                        "content": "完成核对",
                        "source_evidence": {
                            "source_message_index": 1,
                            "exact_quote": "完成核对",
                        },
                    }
                ]
            },
        )


def test_daily_edit_replacement_is_copied_from_the_current_message() -> None:
    source = CurrentTurnSource(("第9条旧内容改为每周一记录",))

    bound = source.bind_tool_arguments(
        "edit_daily_items",
        {
            "report_id": "20000000-0000-0000-0000-000000000001",
            "expected_version": 9,
            "target_item_ids": ["today-9"],
            "replacement": "每周一记录旧内容",
            "replacement_evidence": {
                "source_message_index": 1,
                "exact_quote": "每周一记录",
            },
        },
    )

    assert bound["replacement"] == "每周一记录"


def test_daily_edit_replacement_quote_must_identify_one_source_occurrence() -> None:
    source = CurrentTurnSource(
        ("第9条旧内容改为每周一记录，另一条仍是每周一记录",)
    )

    with pytest.raises(
        CurrentTurnSourceEvidenceError,
        match="DAILY_EDIT_REPLACEMENT_SPAN_AMBIGUOUS",
    ):
        source.bind_tool_arguments(
            "edit_daily_items",
            {
                "report_id": "20000000-0000-0000-0000-000000000001",
                "expected_version": 9,
                "target_item_ids": ["today-9"],
                "replacement": "每周一记录",
                "replacement_evidence": {
                    "source_message_index": 1,
                    "exact_quote": "每周一记录",
                },
            },
        )


def test_daily_edit_tool_contract_requires_the_replacement_quote() -> None:
    tool_schema = deepseek_tool_schemas(
        frozenset({"edit_daily_items"})
    )[0]
    schema = tool_schema["function"]["parameters"]

    assert "replacement_evidence" in schema["required"]
    assert (
        "replacement_evidence.exact_quote"
        in tool_schema["function"]["description"]
    )
    assert "exclude the target description" in tool_schema["function"][
        "description"
    ]


@pytest.mark.asyncio
async def test_agent2_daily_edit_binds_the_server_copied_replacement() -> None:
    owner_id = UUID("10000000-0000-0000-0000-000000000001")
    report_id = UUID("20000000-0000-0000-0000-000000000001")
    report = TrustedReportSnapshot(
        report_id=report_id,
        tenant_id="test-tenant",
        owner_user_id=owner_id,
        report_date=date(2026, 8, 9),
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
        ),
    )
    context = TrustedContext(
        namespace=CANARY_STATE_NAMESPACE,
        now=datetime(
            2026,
            8,
            9,
            12,
            0,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
        principal=TrustedPrincipal(
            tenant_id="test-tenant",
            user_id=owner_id,
            conversation_id="test-conversation",
            source_message_id="test-edit-message",
            timezone="Asia/Shanghai",
        ),
        today_report=report,
        allowed_tool_names=frozenset({"edit_daily_items"}),
        gate_decisions={"edit_daily_items": True},
    )

    bound, failure = await ShadowCallBinder(
        context,
        UnavailableDateResolver(),
        None,
        current_turn_source=CurrentTurnSource(
            ("第9条旧内容改为每周一记录",)
        ),
    ).bind(
        NativeToolCall(
            tool_call_id="edit-call-9",
            tool_name="edit_daily_items",
            arguments={
                "report_id": str(report_id),
                "expected_version": 9,
                "target_item_ids": ["today-9"],
                "replacement": "每周一记录旧内容",
                "replacement_evidence": {
                    "source_message_index": 1,
                    "exact_quote": "每周一记录",
                },
            },
        )
    )

    assert failure is None
    assert bound is not None
    assert bound.arguments["replacement"] == "每周一记录"
    assert bound.report == report
    assert bound.target_item_ids == ("today-9",)


def test_empty_acknowledgement_requires_matching_source_evidence() -> None:
    with pytest.raises(ValidationError):
        AddDailyItemsArgs.model_validate(
            {
                "date_expression": "今天",
                "proposed_date": "2026-08-09",
                "items": [],
                "acknowledged_empty_fields": ["problems"],
                "empty_field_evidence": [],
            }
        )


def test_current_turn_source_binds_daily_evidence_across_fragments() -> None:
    source = CurrentTurnSource(
        (
            "今天参加经营管理会议，记录人才培养要求。",
            "风险暂无，明天继续跟进降本方案。",
        )
    )
    arguments = AddDailyItemsArgs.model_validate(
        {
            "date_expression": "今天",
            "proposed_date": date(2026, 8, 9),
            "items": [
                {
                    "field": "today_work",
                    "content": "参加经营管理会议，记录人才培养要求",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "今天参加经营管理会议，记录人才培养要求。",
                    },
                },
                {
                    "field": "tomorrow_plan",
                    "content": "继续跟进降本方案",
                    "source_evidence": {
                        "source_message_index": 2,
                        "exact_quote": "明天继续跟进降本方案。",
                    },
                },
            ],
            "acknowledged_empty_fields": ["problems"],
            "empty_field_evidence": [
                {
                    "field": "problems",
                    "source_evidence": {
                        "source_message_index": 2,
                    },
                }
            ],
        }
    )

    source.validate_tool_arguments(
        "add_daily_items",
        arguments.model_dump(mode="json"),
    )


def test_current_turn_source_rejects_a_non_current_message_index() -> None:
    source = CurrentTurnSource(("今天继续跟进项目。",))
    arguments = AddDailyItemsArgs.model_validate(
        {
            "date_expression": "今天",
            "proposed_date": "2026-08-09",
            "items": [
                {
                    "field": "today_work",
                    "content": "完成合同审核",
                    "source_evidence": {
                        "source_message_index": 2,
                        "exact_quote": "完成合同审核",
                    },
                }
            ],
        }
    )

    with pytest.raises(CurrentTurnSourceEvidenceError) as caught:
        source.bind_tool_arguments(
            "add_daily_items",
            arguments.model_dump(mode="json"),
        )

    assert caught.value.code == "CURRENT_MESSAGE_EVIDENCE_MISMATCH"


@pytest.mark.parametrize(
    "content",
    ("日常用印审核", "完成日常用印审核"),
)
def test_daily_item_text_must_be_grounded_in_current_message(
    content: str,
) -> None:
    source = CurrentTurnSource(("今天也做了这个",))
    arguments = AddDailyItemsArgs.model_validate(
        {
            "date_selection": "server_default",
            "items": [
                {
                    "field": "today_work",
                    "content": content,
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": content,
                    },
                }
            ],
        }
    )

    with pytest.raises(CurrentTurnSourceEvidenceError) as caught:
        source.bind_tool_arguments(
            "add_daily_items",
            arguments.model_dump(mode="json"),
        )

    assert caught.value.code == "DAILY_ITEM_CONTENT_NOT_GROUNDED"


def test_quote_bearing_daily_item_can_bind_the_whole_current_message() -> None:
    source = CurrentTurnSource(
        ("老板原话是“要么降薪，要么裁员”",)
    )
    arguments = AddDailyItemsArgs.model_validate(
        {
            "date_expression": "今天",
            "proposed_date": "2026-08-09",
            "items": [
                {
                    "field": "today_work",
                    "content": "老板原话：要么降薪，要么裁员",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "老板原话是“要么降薪，要么裁员”",
                    },
                }
            ],
        }
    )

    source.validate_tool_arguments(
        "add_daily_items",
        arguments.model_dump(mode="json"),
    )


@pytest.mark.parametrize(
    ("memory_key", "intent"),
    [
        ("assistant.preferred_name", "user_salutation_assignment"),
        ("response.preferred_salutation", "assistant_name_assignment"),
    ],
)
def test_memory_contract_rejects_role_mixed_evidence(
    memory_key: str,
    intent: str,
) -> None:
    value = (
        {"name": "兼爱"}
        if memory_key == "assistant.preferred_name"
        else {"salutation": "王喜"}
    )
    with pytest.raises(ValidationError):
        RememberPersonalMemoryArgs.model_validate(
            {
                "memory_key": memory_key,
                "value": value,
                "source_evidence": {
                    "source_message_index": 1,
                    "intent": intent,
                },
            }
        )


def test_current_turn_source_accepts_explicit_assistant_name_correction() -> None:
    source = CurrentTurnSource(("不是，是你叫兼爱，我叫王喜",))
    arguments = RememberPersonalMemoryArgs.model_validate(
        {
            "memory_key": "assistant.preferred_name",
            "value": {"name": "兼爱"},
            "source_evidence": {
                "source_message_index": 1,
                "intent": "assistant_name_correction",
            },
        }
    )

    source.validate_tool_arguments(
        "remember_personal_memory",
        arguments.model_dump(mode="json"),
    )


def test_current_turn_source_rejects_memory_value_not_present_in_message() -> None:
    source = CurrentTurnSource(("以后你就叫小绿",))
    arguments = RememberPersonalMemoryArgs.model_validate(
        {
            "memory_key": "assistant.preferred_name",
            "value": {"name": "兼爱"},
            "source_evidence": {
                "source_message_index": 1,
                "intent": "assistant_name_assignment",
            },
        }
    )

    with pytest.raises(CurrentTurnSourceEvidenceError) as caught:
        source.validate_tool_arguments(
            "remember_personal_memory",
            arguments.model_dump(mode="json"),
        )

    assert caught.value.code == "MEMORY_VALUE_NOT_GROUNDED"


def test_current_turn_source_hash_is_stable_and_order_sensitive() -> None:
    first = CurrentTurnSource(("第一句", "第二句"))
    same = CurrentTurnSource(("第一句", "第二句"))
    reversed_source = CurrentTurnSource(("第二句", "第一句"))

    assert first.sha256 == same.sha256
    assert first.sha256 != reversed_source.sha256


@pytest.mark.asyncio
async def test_production_binder_blocks_invalid_source_index_before_date_resolution() -> None:
    context = TrustedContext(
        namespace=CANARY_STATE_NAMESPACE,
        now=datetime(
            2026,
            8,
            9,
            12,
            0,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
        principal=TrustedPrincipal(
            tenant_id="test-tenant",
            user_id=UUID("10000000-0000-0000-0000-000000000001"),
            conversation_id="test-conversation",
            source_message_id="test-message",
            timezone="Asia/Shanghai",
        ),
        allowed_tool_names=frozenset({"add_daily_items"}),
        gate_decisions={"add_daily_items": True},
    )
    binder = ShadowCallBinder(
        context,
        UnavailableDateResolver(),
        None,
        current_turn_source=CurrentTurnSource(("今天继续跟进项目。",)),
    )

    bound, failure = await binder.bind(
        NativeToolCall(
            tool_call_id="call-1",
            tool_name="add_daily_items",
            arguments={
                "date_expression": "今天",
                "proposed_date": "2026-08-09",
                "items": [
                    {
                        "field": "today_work",
                        "content": "完成合同审核",
                        "source_evidence": {
                            "source_message_index": 2,
                            "exact_quote": "完成合同审核",
                        },
                    }
                ],
            },
        )
    )

    assert bound is None
    assert failure is not None
    assert failure.error_code == "CURRENT_MESSAGE_EVIDENCE_MISMATCH"


@pytest.mark.asyncio
async def test_trusted_failed_write_is_blocked_when_the_target_changed() -> None:
    now = datetime(
        2026,
        8,
        9,
        12,
        0,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    user_id = UUID("10000000-0000-0000-0000-000000000001")
    target_date = date(2026, 8, 9)
    source_text = "reviewed the contract payment terms"
    source = CurrentTurnSource((source_text,))
    candidate = TrustedDailyWriteRetryCandidate(
        candidate_id="a" * 64,
        tenant_id="test-tenant",
        user_id=user_id,
        conversation_id="test-conversation",
        origin_source_message_id="failed-message",
        origin_received_at=now - timedelta(minutes=1),
        source_messages=(source_text,),
        source_bundle_sha256=source.sha256,
        target_date=target_date,
        target_was_absent=True,
        target_version=None,
        target_state_sha256=report_state_hash(None),
        failed_local_date=target_date,
    )
    context = TrustedContext(
        namespace=CANARY_STATE_NAMESPACE,
        now=now,
        principal=TrustedPrincipal(
            tenant_id="test-tenant",
            user_id=user_id,
            conversation_id="test-conversation",
            source_message_id="retry-message",
            timezone="Asia/Shanghai",
            conversation_kind="direct",
        ),
        retryable_daily_write=candidate,
        allowed_tool_names=frozenset({"add_daily_items"}),
        gate_decisions={"add_daily_items": True},
    )

    class _TargetNowExists:
        async def load_owned_report(self, **_kwargs):
            return TrustedReportSnapshot(
                report_id=UUID(
                    "20000000-0000-0000-0000-000000000001"
                ),
                tenant_id="test-tenant",
                owner_user_id=user_id,
                report_date=target_date,
                version=0,
                status="collecting",
                provenance="read_tool",
            )

    bound, failure = await ShadowCallBinder(
        context,
        UnavailableDateResolver(),
        _TargetNowExists(),
        current_turn_source=CurrentTurnSource(("try again",)),
    ).bind(
        NativeToolCall(
            tool_call_id="retry-call",
            tool_name="add_daily_items",
            arguments={
                "date_selection": "trusted_failed_write",
                "retry_candidate_id": candidate.candidate_id,
                "items": [
                    {
                        "field": "today_work",
                        "content": source_text,
                        "source_evidence": {
                            "source_message_index": 1,
                            "exact_quote": source_text,
                        },
                    }
                ],
            },
        )
    )

    assert bound is None
    assert failure is not None
    assert failure.error_code == "DAILY_RETRY_TARGET_STALE"
