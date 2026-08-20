from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime

from sqlalchemy import select, text

from app.agent2.tool_calling.production_store import ToolCallCanaryReceipt
from app.db import AsyncSessionLocal, engine
from app.models import ReportInteractionEvent, WebhookEvent


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", type=datetime.fromisoformat, required=True)
    parser.add_argument("--until", type=datetime.fromisoformat, required=True)
    parser.add_argument("--user-id")
    parser.add_argument("--dingtalk-user-id")
    args = parser.parse_args()

    statement = (
        select(ToolCallCanaryReceipt)
        .where(
            ToolCallCanaryReceipt.created_at >= args.since,
            ToolCallCanaryReceipt.created_at <= args.until,
        )
        .order_by(ToolCallCanaryReceipt.created_at)
    )
    if args.user_id:
        statement = statement.where(
            ToolCallCanaryReceipt.user_id == args.user_id
        )

    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(text("SET TRANSACTION READ ONLY"))
            rows = tuple((await session.scalars(statement)).all())
            interactions = tuple(
                (
                    await session.scalars(
                        select(ReportInteractionEvent)
                        .where(
                            ReportInteractionEvent.created_at >= args.since,
                            ReportInteractionEvent.created_at <= args.until,
                            *(
                                (
                                    ReportInteractionEvent.user_id
                                    == args.user_id,
                                )
                                if args.user_id
                                else ()
                            ),
                        )
                        .order_by(ReportInteractionEvent.created_at)
                    )
                ).all()
            )
            events = (
                tuple(
                    (
                        await session.scalars(
                            select(WebhookEvent)
                            .where(
                                WebhookEvent.received_at >= args.since,
                                WebhookEvent.received_at <= args.until,
                                WebhookEvent.dingtalk_user_id
                                == args.dingtalk_user_id,
                            )
                            .order_by(WebhookEvent.received_at)
                        )
                    ).all()
                )
                if args.dingtalk_user_id
                else ()
            )

    print(
        json.dumps(
            {
                "tool_receipts": [
                    {
                    "created_at": row.created_at.isoformat(),
                    "source_message_id": row.source_message_id,
                    "tool_name": row.tool_name,
                    "status": row.status,
                    "changed": row.changed,
                    "error_code": row.error_code,
                    "safe_user_facts": row.safe_user_facts,
                }
                    for row in rows
                ],
                "interaction_events": [
                    {
                        "created_at": row.created_at.isoformat(),
                        "message_text": row.message_text,
                        "backend_action": row.backend_action,
                    }
                    for row in interactions
                ],
                "webhook_events": [
                    {
                        "received_at": row.received_at.isoformat(),
                        "external_message_id": row.external_message_id,
                        "conversation_id": (
                            row.payload.get("conversationId")
                            if isinstance(row.payload, dict)
                            else None
                        ),
                        "message_text": (
                            (row.payload.get("text") or {}).get("content")
                            if isinstance(row.payload, dict)
                            and isinstance(row.payload.get("text"), dict)
                            else None
                        ),
                        "response_text": (
                            (row.response_payload.get("text") or {}).get("content")
                            if isinstance(row.response_payload, dict)
                            and isinstance(
                                row.response_payload.get("text"), dict
                            )
                            else None
                        ),
                        "agent2_transport": (
                            row.response_payload.get("_agent2_tool_call_canary")
                            if isinstance(row.response_payload, dict)
                            else None
                        ),
                        "agent2_observation": (
                            row.response_payload.get("_agent2_turn_observation_v1")
                            if isinstance(row.response_payload, dict)
                            else None
                        ),
                        "status": row.status,
                        "error_message": row.error_message,
                    }
                    for row in events
                ],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
