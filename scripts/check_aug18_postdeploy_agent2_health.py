from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import datetime

from sqlalchemy import func, select, text

from app.db import AsyncSessionLocal, engine
from app.models import ReportInteractionEvent, WebhookEvent


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", type=datetime.fromisoformat, required=True)
    args = parser.parse_args()
    if args.since.tzinfo is None or args.since.utcoffset() is None:
        raise RuntimeError("--since must include a timezone")
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
            repair_events = int(
                await session.scalar(
                    select(func.count(ReportInteractionEvent.id)).where(
                        ReportInteractionEvent.backend_action.in_(
                            {
                                "agent2_aug17_failure_repair",
                                "agent2_aug17_failure_review_no_change",
                            }
                        )
                    )
                )
                or 0
            )
    result_counts: dict[str, int] = {}
    failures: list[dict[str, object]] = []
    for row in rows:
        observation = (row.response_payload or {}).get(
            "_agent2_turn_observation_v1"
        )
        observation = observation if isinstance(observation, dict) else {}
        result = str(observation.get("business_result_status") or "unknown")
        result_counts[result] = result_counts.get(result, 0) + 1
        if result == "failed":
            payload_text = json.dumps(
                row.payload or {},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            failures.append(
                {
                    "event_sha256": hashlib.sha256(
                        str(row.id).encode("utf-8")
                    ).hexdigest(),
                    "payload_characters": len(payload_text),
                    "received_at": row.received_at.isoformat(),
                    "model_result_status": observation.get(
                        "model_result_status"
                    ),
                }
            )
    print(
        json.dumps(
            {
                "status": "pass" if not failures else "failures_observed",
                "since": args.since.isoformat(),
                "events": len(rows),
                "business_results": result_counts,
                "failure_events": failures,
                "verified_repair_audit_events": repair_events,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
