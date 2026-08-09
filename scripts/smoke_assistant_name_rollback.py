from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.agent2.memory.postgres import (
    PersonalMemoryAuditRecord,
    PersonalMemoryRecord,
)
from app.agent2.tool_calling import canary_service
from app.agent2.tool_calling.canary_config import (
    CANARY_MODEL_NAME,
    canary_prompt_sha256,
)
from app.agent2.tool_calling.canary_service import (
    process_tool_call_canary_ingress,
)
from app.agent2.tool_calling.canary_store import ToolCallCanaryControl
from app.agent2.tool_calling.production_store import ToolCallCanaryReceipt
from app.agent2.tool_calling.registry import runtime_registry_contract_digest
from app.config import get_settings
from app.db import AsyncSessionLocal, engine
from app.llm.client import LLMClient
from app.models import User

WANG_DINGTALK_ID = "66527"
PANG_DINGTALK_ID = "40842"
ASSISTANT_NAME_KEY = "assistant.preferred_name"
USER_SALUTATION_KEY = "response.preferred_salutation"
CORRECTION_REPEATS = max(
    1,
    int(os.getenv("ASSISTANT_NAME_CORRECTION_REPEATS", "1")),
)
NEGATIVE_REPEATS = max(
    1,
    int(os.getenv("ASSISTANT_NAME_NEGATIVE_REPEATS", "1")),
)
NAMING_INVITATION_MARKERS = (
    "给我重新起名字",
    "给我起名字",
    "如果你希望以后叫我",
    "如果你想叫我",
    "如果哪天你也想叫我",
    "可以直接告诉我，我可以记住",
    "我会记住这个偏好",
)


async def _counts(session) -> dict[str, int]:
    return {
        "memories": int(
            await session.scalar(
                select(func.count(PersonalMemoryRecord.memory_id))
            )
            or 0
        ),
        "memory_audits": int(
            await session.scalar(
                select(func.count(PersonalMemoryAuditRecord.audit_id))
            )
            or 0
        ),
        "receipts": int(
            await session.scalar(
                select(func.count(ToolCallCanaryReceipt.receipt_id))
            )
            or 0
        ),
    }


async def _memory_value(session, user_id, key: str):
    row = await session.scalar(
        select(PersonalMemoryRecord).where(
            PersonalMemoryRecord.user_id == user_id,
            PersonalMemoryRecord.memory_key == key,
            PersonalMemoryRecord.status == "active",
        )
    )
    return None if row is None else dict(row.value_json)


async def _receipts(session, source_message_id: str):
    return list(
        (
            await session.scalars(
                select(ToolCallCanaryReceipt)
                .where(
                    ToolCallCanaryReceipt.source_message_id
                    == source_message_id
                )
                .order_by(ToolCallCanaryReceipt.created_at)
            )
        ).all()
    )


async def _turn(
    session,
    *,
    user,
    text: str,
    conversation_id: str,
    settings,
    llm_client,
    now,
) -> dict[str, object]:
    source_message_id = f"rollback-assistant-name-{uuid4()}"
    model_audits: list[dict[str, object]] = []
    original_audit_recorder = canary_service._record_model_audit_safely

    def capture_model_audit(payload):
        if isinstance(payload, dict):
            turns = payload.get("model_turns")
            model_audits.append(
                {
                    "status": payload.get("status"),
                    "error_type": payload.get("error_type"),
                    "error_message": payload.get("error_message"),
                    "turns": [
                        {
                            "iteration": turn.get("iteration"),
                            "response_metadata": turn.get("response_metadata"),
                            "raw_assistant_message": turn.get(
                                "raw_assistant_message"
                            ),
                        }
                        for turn in turns
                        if isinstance(turn, dict)
                    ]
                    if isinstance(turns, list)
                    else [],
                }
            )
        original_audit_recorder(payload)

    canary_service._record_model_audit_safely = capture_model_audit
    try:
        outcome = await process_tool_call_canary_ingress(
            session,
            user=user,
            dingtalk_user_id=user.dingtalk_user_id,
            user_text=text,
            source_channel="rollback_assistant_name_smoke",
            conversation_id=conversation_id,
            source_message_id=source_message_id,
            settings=settings,
            llm_client=llm_client,
            now=now,
        )
    finally:
        canary_service._record_model_audit_safely = original_audit_recorder
    await session.flush()
    receipts = await _receipts(session, source_message_id)
    assert not any(
        receipt.target_type == "daily_report" and receipt.changed
        for receipt in receipts
    ), receipts
    result = {
        "text": text,
        "owner": outcome.owner,
        "reason": outcome.reason,
        "reply": outcome.message,
        "actual_write": outcome.actual_write,
        "tools": [receipt.tool_name for receipt in receipts],
        "receipts": [
            {
                "tool": receipt.tool_name,
                "target_type": receipt.target_type,
                "changed": receipt.changed,
                "status": receipt.status,
            }
            for receipt in receipts
        ],
        "memory_keys": [
            receipt.safe_user_facts.get("memory_key")
            for receipt in receipts
            if receipt.target_type == "personal_memory"
        ],
        "model_audits": model_audits,
    }
    assert outcome.owner == "tool_call_core", result
    assert outcome.handled is True, result
    return result


async def main() -> None:
    settings = get_settings()
    now = datetime.now(ZoneInfo(settings.timezone))
    llm_client = LLMClient(settings)
    baseline: dict[str, int]
    baseline_wang_assistant_name: dict[str, str] | None
    results: list[dict[str, object]] = []

    async with AsyncSessionLocal() as session:
        baseline = await _counts(session)
        users = list(
            (
                await session.scalars(
                    select(User).where(
                        User.dingtalk_user_id.in_(
                            [WANG_DINGTALK_ID, PANG_DINGTALK_ID]
                        ),
                        User.active.is_(True),
                    )
                )
            ).all()
        )
        users_by_dingtalk = {
            user.dingtalk_user_id: user for user in users
        }
        assert set(users_by_dingtalk) == {
            WANG_DINGTALK_ID,
            PANG_DINGTALK_ID,
        }
        wang = users_by_dingtalk[WANG_DINGTALK_ID]
        pang = users_by_dingtalk[PANG_DINGTALK_ID]

        assert await _memory_value(
            session, wang.id, USER_SALUTATION_KEY
        ) == {"salutation": "王喜"}
        baseline_wang_assistant_name = await _memory_value(
            session, wang.id, ASSISTANT_NAME_KEY
        )
        assert baseline_wang_assistant_name in (
            None,
            {"name": "兼爱"},
        )
        assert await _memory_value(
            session, pang.id, ASSISTANT_NAME_KEY
        ) is None

        registry_digest = runtime_registry_contract_digest(settings)
        prompt_digest = canary_prompt_sha256()
        controls = list(
            (
                await session.scalars(
                    select(ToolCallCanaryControl).where(
                        ToolCallCanaryControl.user_id.in_(
                            [str(wang.id), str(pang.id)]
                        )
                    )
                )
            ).all()
        )
        assert len(controls) == 2
        for control in controls:
            control.enabled = True
            control.messages_enabled = True
            control.registry_digest = registry_digest
            control.prompt_sha256 = prompt_digest
            control.model_name = CANARY_MODEL_NAME
        await session.flush()

        salutation_row = await session.scalar(
            select(PersonalMemoryRecord).where(
                PersonalMemoryRecord.user_id == wang.id,
                PersonalMemoryRecord.memory_key == USER_SALUTATION_KEY,
                PersonalMemoryRecord.status == "active",
            )
        )
        assert salutation_row is not None
        salutation_row.value_json = {"salutation": "兼爱"}
        salutation_row.version += 1
        salutation_row.updated_at = now
        await session.flush()
        assert await _memory_value(
            session, wang.id, USER_SALUTATION_KEY
        ) == {"salutation": "兼爱"}
        results.append(
            {
                "fixture": "temporarily set user salutation to 兼爱",
                "rolled_back": True,
            }
        )

        for repeat_index in range(CORRECTION_REPEATS):
            if repeat_index:
                salutation_row.value_json = {"salutation": "兼爱"}
                salutation_row.version += 1
                salutation_row.updated_at = now
                await session.flush()
            corrected = await _turn(
                session,
                user=wang,
                text="不是，是你叫兼爱，我叫王喜",
                conversation_id=f"rollback-wang-correction-{uuid4()}",
                settings=settings,
                llm_client=llm_client,
                now=now,
            )
            corrected["correction_repeat"] = repeat_index + 1
            results.append(corrected)
            assert ASSISTANT_NAME_KEY in corrected["memory_keys"], corrected
            assert USER_SALUTATION_KEY in corrected["memory_keys"], corrected
            assert await _memory_value(
                session, wang.id, ASSISTANT_NAME_KEY
            ) == {"name": "兼爱"}
            assert await _memory_value(
                session, wang.id, USER_SALUTATION_KEY
            ) == {"salutation": "王喜"}

        fresh_wang_conversation = f"rollback-wang-fresh-{uuid4()}"
        named = await _turn(
            session,
            user=wang,
            text="你叫什么名字？",
            conversation_id=fresh_wang_conversation,
            settings=settings,
            llm_client=llm_client,
            now=now,
        )
        results.append(named)
        assert "兼爱" in str(named["reply"]), named

        praised = await _turn(
            session,
            user=wang,
            text="回答得正确，厉害！",
            conversation_id=fresh_wang_conversation,
            settings=settings,
            llm_client=llm_client,
            now=now,
        )
        results.append(praised)
        assert "谢谢兼爱" not in str(praised["reply"]), praised
        assert "谢谢王喜" not in str(praised["reply"]), praised

        default_name = await _turn(
            session,
            user=pang,
            text="你叫什么名字？",
            conversation_id=f"rollback-pang-fresh-{uuid4()}",
            settings=settings,
            llm_client=llm_client,
            now=now,
        )
        results.append(default_name)
        assert "小律" in str(default_name["reply"]), default_name
        assert "兼爱" not in str(default_name["reply"]), default_name

        for negative_repeat in range(NEGATIVE_REPEATS):
            for text in (
                "小绿，帮我查一下今天的日报",
                "谢谢小绿",
                "别人叫它小绿",
                "你叫小绿吗？",
            ):
                negative = await _turn(
                    session,
                    user=pang,
                    text=text,
                    conversation_id=f"rollback-pang-negative-{uuid4()}",
                    settings=settings,
                    llm_client=llm_client,
                    now=now,
                )
                negative["negative_repeat"] = negative_repeat + 1
                results.append(negative)
                assert ASSISTANT_NAME_KEY not in negative["memory_keys"], negative
                assert not any(
                    marker in str(negative["reply"])
                    for marker in NAMING_INVITATION_MARKERS
                ), negative
                assert await _memory_value(
                    session,
                    pang.id,
                    ASSISTANT_NAME_KEY,
                ) is None

        explicitly_named = await _turn(
            session,
            user=pang,
            text="以后你就叫小绿",
            conversation_id=f"rollback-pang-positive-{uuid4()}",
            settings=settings,
            llm_client=llm_client,
            now=now,
        )
        results.append(explicitly_named)
        assert ASSISTANT_NAME_KEY in explicitly_named["memory_keys"], (
            explicitly_named
        )
        assert await _memory_value(
            session,
            pang.id,
            ASSISTANT_NAME_KEY,
        ) == {"name": "小绿"}

        await session.rollback()

    async with AsyncSessionLocal() as session:
        after = await _counts(session)
        assert after == baseline, {"before": baseline, "after": after}
        wang = await session.scalar(
            select(User).where(
                User.dingtalk_user_id == WANG_DINGTALK_ID
            )
        )
        assert wang is not None
        assert await _memory_value(
            session, wang.id, USER_SALUTATION_KEY
        ) == {"salutation": "王喜"}
        assert await _memory_value(
            session, wang.id, ASSISTANT_NAME_KEY
        ) == baseline_wang_assistant_name

    await engine.dispose()
    print(
        json.dumps(
            {
                "status": "pass",
                "baseline_wang_assistant_name": (
                    baseline_wang_assistant_name
                ),
                "results": results,
                "rollback_verified": True,
                "dingtalk_send_calls": 0,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
