from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json

from sqlalchemy import func, select

from app.agent2.memory.postgres import (
    PersonalMemoryAuditRecord,
    PersonalMemoryRecord,
)
from app.agent2.tool_calling.context import (
    CANARY_STATE_NAMESPACE,
    TrustedContext,
    TrustedPrincipal,
)
from app.agent2.tool_calling.contracts import RememberPersonalMemoryArgs
from app.agent2.tool_calling.production_handlers import ProductionHandlerRequest
from app.agent2.tool_calling.production_memory_executor import (
    ProductionPersonalMemoryExecutor,
)
from app.config import get_settings
from app.db import AsyncSessionLocal, engine
from app.models import DailyReport, User, WebhookEvent


WANG_DINGTALK_ID = "66527"
CORRECTION_EXTERNAL_MESSAGE_ID = "msgwUkA6jIfbwHp0YyUzVhkWQ=="
EXPECTED_CORRECTION = "不是，是你叫兼爱，我叫王喜"
ASSISTANT_NAME_KEY = "assistant.preferred_name"
SALUTATION_KEY = "response.preferred_salutation"


async def _daily_fingerprint(session, user_id) -> dict[str, object]:
    rows = list(
        (
            await session.scalars(
                select(DailyReport)
                .where(DailyReport.user_id == user_id)
                .order_by(DailyReport.report_date, DailyReport.id)
            )
        ).all()
    )
    return {
        "count": len(rows),
        "versions": [
            {
                "id": str(row.id),
                "date": row.report_date.isoformat(),
                "version": int(getattr(row, "version", 0) or 0),
                "status": str(row.status),
                "updated_at": row.updated_at.isoformat(),
            }
            for row in rows
        ],
    }


async def main() -> None:
    settings = get_settings()
    now = datetime.now(UTC)
    output: dict[str, object] = {
        "dingtalk_send_calls": 0,
        "daily_report_writes": 0,
    }
    async with AsyncSessionLocal() as session:
        user = await session.scalar(
            select(User).where(
                User.dingtalk_user_id == WANG_DINGTALK_ID,
                User.active.is_(True),
            )
        )
        if user is None or user.name != "王喜":
            raise AssertionError("王喜的启用身份无法唯一确认")
        correction = await session.scalar(
            select(WebhookEvent).where(
                WebhookEvent.external_message_id
                == CORRECTION_EXTERNAL_MESSAGE_ID
            )
        )
        if correction is None:
            raise AssertionError("原始纠正消息不存在")
        payload = correction.payload if isinstance(correction.payload, dict) else {}
        text_payload = payload.get("text")
        inbound_text = (
            str(text_payload.get("content") or "").strip()
            if isinstance(text_payload, dict)
            else ""
        )
        if inbound_text != EXPECTED_CORRECTION:
            raise AssertionError(
                f"原始纠正消息不匹配: {inbound_text!r}"
            )
        conversation_id = str(payload.get("conversationId") or "").strip()
        if not conversation_id:
            raise AssertionError("原始纠正消息缺少会话标识")

        salutation = await session.scalar(
            select(PersonalMemoryRecord).where(
                PersonalMemoryRecord.user_id == user.id,
                PersonalMemoryRecord.memory_key == SALUTATION_KEY,
                PersonalMemoryRecord.status == "active",
            )
        )
        if salutation is None or salutation.value_json != {
            "salutation": "王喜"
        }:
            raise AssertionError("王喜当前本人称呼不是预期值")
        source_message_id = str(salutation.source_message_id or "").strip()
        if not source_message_id.startswith("agent2-turn-batch-v1:"):
            raise AssertionError("原始纠正批次来源无法确认")
        tenant_id = str(salutation.tenant_id)

        existing = await session.scalar(
            select(PersonalMemoryRecord).where(
                PersonalMemoryRecord.user_id == user.id,
                PersonalMemoryRecord.memory_key == ASSISTANT_NAME_KEY,
            )
        )
        before_alias = (
            None
            if existing is None
            else {
                "memory_id": str(existing.memory_id),
                "value": dict(existing.value_json),
                "status": existing.status,
                "version": existing.version,
                "source_message_id": existing.source_message_id,
            }
        )
        before_daily = await _daily_fingerprint(session, user.id)
        before_audit_count = int(
            await session.scalar(
                select(func.count(PersonalMemoryAuditRecord.audit_id)).where(
                    PersonalMemoryAuditRecord.user_id == user.id,
                    PersonalMemoryAuditRecord.memory_key
                    == ASSISTANT_NAME_KEY,
                )
            )
            or 0
        )

        context = TrustedContext(
            namespace=CANARY_STATE_NAMESPACE,
            now=now,
            principal=TrustedPrincipal(
                tenant_id=tenant_id,
                user_id=user.id,
                conversation_id=conversation_id,
                source_message_id=source_message_id,
                timezone=settings.timezone,
                display_name=user.name,
            ),
            allowed_tool_names=frozenset({"remember_personal_memory"}),
            gate_decisions={"remember_personal_memory": True},
        )
        executor = ProductionPersonalMemoryExecutor(
            session=session,
            context=context,
        )
        arguments = RememberPersonalMemoryArgs.model_validate(
            {
                "memory_key": ASSISTANT_NAME_KEY,
                "value": {"name": "兼爱"},
            }
        )
        request = ProductionHandlerRequest(
            tool_call_id="assistant-name-backfill-v14",
            tool_name="remember_personal_memory",
            arguments=arguments,
            executor=object(),
            memory_executor=executor,
        )
        outcome = await executor.remember_personal_memory(request)
        await session.commit()

        after = await session.scalar(
            select(PersonalMemoryRecord).where(
                PersonalMemoryRecord.user_id == user.id,
                PersonalMemoryRecord.memory_key == ASSISTANT_NAME_KEY,
                PersonalMemoryRecord.status == "active",
            )
        )
        if after is None or after.value_json != {"name": "兼爱"}:
            raise AssertionError("王喜的机器人名字没有正确保存")
        if after.source_message_id != source_message_id:
            raise AssertionError("机器人名字没有绑定原始纠正批次")
        after_audit_count = int(
            await session.scalar(
                select(func.count(PersonalMemoryAuditRecord.audit_id)).where(
                    PersonalMemoryAuditRecord.user_id == user.id,
                    PersonalMemoryAuditRecord.memory_key
                    == ASSISTANT_NAME_KEY,
                )
            )
            or 0
        )
        after_daily = await _daily_fingerprint(session, user.id)
        if after_daily != before_daily:
            raise AssertionError("补记机器人名字时日报发生了变化")

        output.update(
            {
                "status": "success",
                "user": user.name,
                "user_salutation": dict(salutation.value_json),
                "assistant_name": dict(after.value_json),
                "source_external_message_id": CORRECTION_EXTERNAL_MESSAGE_ID,
                "source_message_id": source_message_id,
                "conversation_id": conversation_id,
                "before_alias": before_alias,
                "after_alias": {
                    "memory_id": str(after.memory_id),
                    "value": dict(after.value_json),
                    "status": after.status,
                    "version": after.version,
                    "source_message_id": after.source_message_id,
                },
                "changed": before_alias != {
                    "memory_id": str(after.memory_id),
                    "value": dict(after.value_json),
                    "status": after.status,
                    "version": after.version,
                    "source_message_id": after.source_message_id,
                },
                "executor_changed": (
                    outcome.after_version != outcome.before_version
                ),
                "assistant_name_audit_count_before": before_audit_count,
                "assistant_name_audit_count_after": after_audit_count,
                "daily_report_count": before_daily["count"],
            }
        )

    await engine.dispose()
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
