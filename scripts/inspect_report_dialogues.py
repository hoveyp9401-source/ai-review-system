from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.models import WebhookEvent


def text_of(payload) -> str:
    if not isinstance(payload, dict):
        return ""
    text = payload.get("text")
    if isinstance(text, dict):
        return str(text.get("content") or text.get("text") or "")
    return str(text or payload.get("content") or "")


async def main() -> None:
    start = datetime(2026, 8, 3, tzinfo=ZoneInfo("Asia/Shanghai"))
    async with AsyncSessionLocal() as session:
        rows = list(
            (
                await session.scalars(
                    select(WebhookEvent)
                    .where(
                        WebhookEvent.dingtalk_user_id.in_(["55264", "40842"]),
                        WebhookEvent.received_at >= start,
                    )
                    .order_by(WebhookEvent.received_at)
                )
            ).all()
        )
        for row in rows:
            print(
                {
                    "at": row.received_at.isoformat(),
                    "user": row.dingtalk_user_id,
                    "conversation": (row.payload or {}).get("conversationId"),
                    "in": text_of(row.payload),
                    "out": text_of(row.response_payload),
                    "status": row.status,
                }
            )


if __name__ == "__main__":
    asyncio.run(main())
