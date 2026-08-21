from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select, text

from app.agent2.tool_calling.production_store import _text_content
from app.db import AsyncSessionLocal, engine
from app.models import WebhookEvent

OBSERVATION_KEY = "_agent2_turn_observation_v1"


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", type=datetime.fromisoformat, required=True)
    parser.add_argument("--until", type=datetime.fromisoformat, required=True)
    parser.add_argument("--limit", type=int, default=80)
    args = parser.parse_args()
    if args.since.tzinfo is None or args.until.tzinfo is None:
        raise ValueError("time bounds must include timezone")
    if args.limit < 1 or args.limit > 500:
        raise ValueError("limit must be between 1 and 500")

    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(text("SET TRANSACTION READ ONLY"))
            rows = list(
                (
                    await session.scalars(
                        select(WebhookEvent)
                        .where(
                            WebhookEvent.received_at >= args.since,
                            WebhookEvent.received_at < args.until,
                            WebhookEvent.status.in_(("processed", "failed")),
                        )
                        .order_by(WebhookEvent.received_at)
                    )
                ).all()
            )

    shanghai = ZoneInfo("Asia/Shanghai")
    by_input: dict[str, dict[str, object]] = {}
    event_count = 0
    for row in rows:
        response = (
            row.response_payload
            if isinstance(row.response_payload, dict)
            else {}
        )
        observation = response.get(OBSERVATION_KEY)
        if row.status == "failed":
            business_result = "ingress_failed"
            root_category = "ingress_failure"
        else:
            if not isinstance(observation, dict):
                continue
            business_result = str(
                observation.get("business_result_status") or ""
            )
            if business_result not in {
                "failed",
                "blocked",
                "clarification",
                "clarification_required",
            }:
                continue
            root_category = (
                "model_or_execution_failure"
                if business_result == "failed"
                else (
                    "business_blocked"
                    if business_result == "blocked"
                    else "clarification"
                )
            )
        message_text = _text_content(row.payload, max_length=None).strip()
        event_count += 1
        replayable_text = bool(message_text)
        digest_source = (
            message_text
            if replayable_text
            else f"non-text-event:{row.id}:{row.received_at.isoformat()}"
        )
        digest = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()
        candidate = by_input.get(digest)
        event = {
            "input_sha256": digest,
            "input_characters": len(message_text),
            "first_received_at": row.received_at.astimezone(
                shanghai
            ).isoformat(),
            "event_count": 1,
            "event_status": row.status,
            "business_result": business_result,
            "root_category": root_category,
            "replayable_text": replayable_text,
            "model_result": (
                observation.get("model_result_status")
                if isinstance(observation, dict)
                else None
            ),
            "model_call_count": (
                observation.get("model_call_count")
                if isinstance(observation, dict)
                else None
            ),
            "tool_blocked_count": (
                observation.get("tool_blocked_count")
                if isinstance(observation, dict)
                else None
            ),
            "tool_failure_count": (
                observation.get("tool_failure_count")
                if isinstance(observation, dict)
                else None
            ),
            "identity_included": False,
        }
        if replayable_text:
            event["message_text"] = message_text
        if candidate is None:
            by_input[digest] = event
        else:
            candidate["event_count"] = int(candidate["event_count"]) + 1

    ordered = sorted(
        by_input.values(),
        key=lambda item: (
            -int(item["event_count"]),
            -int(item["input_characters"]),
            str(item["first_received_at"]),
        ),
    )[: args.limit]
    print(
        json.dumps(
            {
                "schema_version": "agent2.recent-failure-inputs.v2",
                "range": {
                    "since": args.since.isoformat(),
                    "until": args.until.isoformat(),
                },
                "adverse_event_count": event_count,
                "unique_input_count": len(by_input),
                "inputs": ordered,
                "identity_fields_included": False,
                "database_writes": 0,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
