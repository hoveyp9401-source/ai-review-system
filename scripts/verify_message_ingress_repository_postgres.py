#!/usr/bin/env python3
"""Rollback-only production PostgreSQL smoke for the ingress claim repository."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
from uuid import uuid4

from sqlalchemy import func, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import AsyncSessionLocal, engine
from app.models import MessageIngressClaim, WebhookEvent
from app.repositories import create_webhook_event_once


async def verify(output: Path) -> int:
    suffix = uuid4().hex
    external_message_id = f"agent2-p0bc-rollback-{suffix}"
    first_key = f"dingtalk:{external_message_id}"
    replay_key = f"dingtalk-stream:{external_message_id}"
    artifact: dict[str, object]
    try:
        async with AsyncSessionLocal() as session:
            first, first_inserted = await create_webhook_event_once(
                session,
                idempotency_key=first_key,
                external_message_id=external_message_id,
                dingtalk_user_id="synthetic-rollback-only",
                payload={"verification": "rollback_only"},
            )
            replay, replay_inserted = await create_webhook_event_once(
                session,
                idempotency_key=replay_key,
                external_message_id=external_message_id,
                dingtalk_user_id="synthetic-rollback-only",
                payload={"verification": "rollback_only_replay"},
            )
            in_transaction_claims = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(MessageIngressClaim)
                        .where(
                            MessageIngressClaim.external_message_id
                            == external_message_id
                        )
                    )
                ).scalar_one()
            )
            in_transaction_events = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(WebhookEvent)
                        .where(WebhookEvent.external_message_id == external_message_id)
                    )
                ).scalar_one()
            )
            same_event = first.id == replay.id
            await session.rollback()

        async with AsyncSessionLocal() as session:
            persisted_claims = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(MessageIngressClaim)
                        .where(
                            MessageIngressClaim.external_message_id
                            == external_message_id
                        )
                    )
                ).scalar_one()
            )
            persisted_events = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(WebhookEvent)
                        .where(WebhookEvent.external_message_id == external_message_id)
                    )
                ).scalar_one()
            )
            await session.rollback()

        passed = (
            first_inserted
            and not replay_inserted
            and same_event
            and in_transaction_claims == 1
            and in_transaction_events == 1
            and persisted_claims == 0
            and persisted_events == 0
        )
        artifact = {
            "artifact_version": "agent2.message_ingress_repository_rollback_smoke.v1",
            "status": "PASS" if passed else "FAIL",
            "first_inserted": first_inserted,
            "cross_transport_replay_inserted": replay_inserted,
            "same_event": same_event,
            "in_transaction_claims": in_transaction_claims,
            "in_transaction_events": in_transaction_events,
            "persisted_claims_after_rollback": persisted_claims,
            "persisted_events_after_rollback": persisted_events,
            "business_write_committed": False,
        }
    finally:
        await engine.dispose()

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"status": artifact["status"]}))
    return 0 if artifact["status"] == "PASS" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    return asyncio.run(verify(args.output))


if __name__ == "__main__":
    raise SystemExit(main())
