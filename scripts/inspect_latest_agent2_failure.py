from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import datetime

from sqlalchemy import select, text

from app.agent2.tool_calling.production_store import _text_content
from app.db import AsyncSessionLocal, engine
from app.models import WebhookEvent


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", type=datetime.fromisoformat, required=True)
    args = parser.parse_args()
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(text("SET TRANSACTION READ ONLY"))
            rows = list(
                (
                    await session.scalars(
                        select(WebhookEvent)
                        .where(WebhookEvent.received_at >= args.since)
                        .order_by(WebhookEvent.received_at)
                    )
                ).all()
            )
    output = []
    for row in rows:
        output.append(
            {
                "event_sha256": hashlib.sha256(
                    str(row.id).encode("utf-8")
                ).hexdigest(),
                "received_at": row.received_at.isoformat(),
                "text": _text_content(row.payload, max_length=None),
                "status": row.status,
                "error_message": row.error_message,
                "response_text": _text_content(
                    row.response_payload,
                    max_length=None,
                ),
                "observation": (row.response_payload or {}).get(
                    "_agent2_turn_observation_v1"
                ),
            }
        )
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
