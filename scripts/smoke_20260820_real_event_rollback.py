from __future__ import annotations

import argparse
import asyncio
import json
from uuid import UUID, uuid4

from sqlalchemy import func, select

from app.agent2.tool_calling.canary_config import (
    CANARY_MODEL_NAME,
    canary_prompt_sha256,
)
from app.agent2.tool_calling.canary_store import ToolCallCanaryControl
from app.agent2.tool_calling.production_store import ToolCallCanaryReceipt
from app.agent2.tool_calling.registry import runtime_registry_contract_digest
from app.config import get_settings
from app.db import AsyncSessionLocal, engine
from app.llm.client import LLMClient
from app.models import DailyReport, User, WebhookEvent
from scripts.smoke_20260811_overnight_daily_rollback import _turn


def _message_text(event: WebhookEvent) -> str:
    payload = event.payload if isinstance(event.payload, dict) else {}
    text = payload.get("text")
    if not isinstance(text, dict) or not isinstance(text.get("content"), str):
        raise AssertionError("event has no text content")
    return text["content"]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--external-message-id", required=True)
    args = parser.parse_args()
    run_id = f"real-event-rollback-{uuid4().hex[:12]}"
    client = LLMClient(get_settings())
    result: dict[str, object] = {}
    try:
        async with AsyncSessionLocal() as session:
            try:
                event = await session.scalar(
                    select(WebhookEvent).where(
                        WebhookEvent.external_message_id
                        == args.external_message_id
                    )
                )
                if event is None or not event.dingtalk_user_id:
                    raise AssertionError("event or user binding is missing")
                user = await session.scalar(
                    select(User).where(
                        User.dingtalk_user_id == event.dingtalk_user_id
                    )
                )
                if user is None:
                    raise AssertionError("bound user is missing")
                control = await session.scalar(
                    select(ToolCallCanaryControl).where(
                        ToolCallCanaryControl.user_id == str(user.id)
                    )
                )
                if control is None:
                    raise AssertionError("Agent2 control is missing")
                settings = get_settings()
                control.enabled = True
                control.messages_enabled = False
                control.registry_digest = runtime_registry_contract_digest(
                    settings
                )
                control.prompt_sha256 = canary_prompt_sha256()
                control.model_name = CANARY_MODEL_NAME
                await session.flush()
                payload = event.payload if isinstance(event.payload, dict) else {}
                outcome = await _turn(
                    session,
                    user=user,
                    settings=settings,
                    llm_client=client,
                    text=_message_text(event),
                    conversation_id=str(payload.get("conversationId") or ""),
                    source_message_id=f"{run_id}-message",
                    now=event.received_at,
                    accepted_business_results=frozenset(
                        {
                            "success",
                            "no_op",
                            "reply_only",
                            "clarification_required",
                            "blocked",
                        }
                    ),
                )
                report = (
                    await session.get(DailyReport, UUID(outcome.report_id))
                    if outcome.report_id
                    else None
                )
                result = {
                    "status": "pass",
                    "business_result": outcome.user_visible_result,
                    "reason": outcome.reason,
                    "reply": outcome.message,
                    "messages_enabled": outcome.messages_enabled,
                    "report_snapshot": (
                        {
                            "report_date": report.report_date.isoformat(),
                            "status": report.status,
                            "confirmation_type": report.confirmation_type,
                            "confirmed_by_user": report.confirmed_by_user,
                            "today_work": report.today_work,
                            "problems": report.problems,
                            "tomorrow_plan": report.tomorrow_plan,
                        }
                        if report is not None
                        else None
                    ),
                }
            finally:
                await session.rollback()
    finally:
        await client.close()

    async with AsyncSessionLocal() as session:
        receipt_count = int(
            await session.scalar(
                select(func.count(ToolCallCanaryReceipt.receipt_id)).where(
                    ToolCallCanaryReceipt.source_message_id.like(
                        f"{run_id}%"
                    )
                )
            )
            or 0
        )
        await session.rollback()
    if receipt_count:
        raise AssertionError({"rollback_receipt_residue": receipt_count})
    result["rollback_receipt_residue"] = receipt_count
    result["dingtalk_send_calls"] = 0
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
