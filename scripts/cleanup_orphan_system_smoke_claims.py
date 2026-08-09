from __future__ import annotations

import asyncio
import json

from sqlalchemy import delete, select

from app.db import AsyncSessionLocal
from app.models import MessageIngressClaim, WebhookEvent


async def main() -> None:
    async with AsyncSessionLocal() as session:
        claims = list(
            (
                await session.scalars(
                    select(MessageIngressClaim).where(
                        MessageIngressClaim.platform == "manual_api",
                        MessageIngressClaim.external_message_id.like(
                            "system-smoke-%"
                        ),
                    )
                )
            ).all()
        )
        event_ids = [claim.webhook_event_id for claim in claims]
        existing_event_ids = set(
            (
                await session.scalars(
                    select(WebhookEvent.id).where(
                        WebhookEvent.id.in_(event_ids)
                    )
                )
            ).all()
        ) if event_ids else set()
        orphan_ids = [
            claim.idempotency_key
            for claim in claims
            if claim.webhook_event_id not in existing_event_ids
        ]
        if orphan_ids:
            await session.execute(
                delete(MessageIngressClaim).where(
                    MessageIngressClaim.idempotency_key.in_(orphan_ids)
                )
            )
        await session.commit()
        print(
            json.dumps(
                {
                    "matched": len(claims),
                    "orphan_removed": len(orphan_ids),
                    "live_claims_preserved": len(existing_event_ids),
                }
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
