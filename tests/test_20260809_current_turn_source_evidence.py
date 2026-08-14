from __future__ import annotations

from datetime import date, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.agent2.tool_calling.context import (
    CANARY_STATE_NAMESPACE,
    TrustedContext,
    TrustedPrincipal,
)
from app.agent2.tool_calling.contracts import (
    AddDailyItemsArgs,
    RememberPersonalMemoryArgs,
)
from app.agent2.tool_calling.current_turn_source import (
    CurrentTurnSource,
    CurrentTurnSourceEvidenceError,
)
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
                    },
                },
                {
                    "field": "tomorrow_plan",
                    "content": "继续跟进降本方案",
                    "source_evidence": {
                        "source_message_index": 2,
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
                    },
                }
            ],
        }
    )

    with pytest.raises(CurrentTurnSourceEvidenceError) as caught:
        source.validate_tool_arguments(
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
                    "source_evidence": {"source_message_index": 1},
                }
            ],
        }
    )

    with pytest.raises(CurrentTurnSourceEvidenceError) as caught:
        source.validate_tool_arguments(
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
                    "source_evidence": {"source_message_index": 1},
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
                        },
                    }
                ],
            },
        )
    )

    assert bound is None
    assert failure is not None
    assert failure.error_code == "CURRENT_MESSAGE_EVIDENCE_MISMATCH"
