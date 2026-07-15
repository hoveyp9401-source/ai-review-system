from __future__ import annotations

import argparse
import asyncio
from datetime import timedelta
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent2.business.case_followups import enqueue_case_progress_followup
from app.config import get_settings
from app.db import AsyncSessionLocal
from app.utils.time import now_in_timezone


CONFIRMATION = "SEND_CASE_PROGRESS_FOLLOWUP"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Enqueue one permission-bound Agent2 case-progress follow-up."
    )
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--recipient-user-id", required=True)
    parser.add_argument("--trigger-id", required=True)
    parser.add_argument("--expires-hours", type=int, default=48)
    parser.add_argument("--confirm", required=True)
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> None:
    settings = get_settings()
    allowed_tenants = {
        value.strip()
        for value in str(settings.agent2_business_tenant_ids or "").split(",")
        if value.strip()
    }
    followup_tenants = tuple(
        value.strip()
        for value in str(settings.agent2_case_followup_tenant_ids or "").split(",")
        if value.strip()
    )
    followup_users = tuple(
        value.strip()
        for value in str(settings.agent2_case_followup_user_ids or "").split(",")
        if value.strip()
    )
    if args.confirm != CONFIRMATION:
        raise SystemExit(f"--confirm must equal {CONFIRMATION}")
    if not settings.agent2_business_phase2_enabled:
        raise SystemExit("Agent2 Business Phase 2 is disabled")
    if not settings.agent2_case_followup_enabled:
        raise SystemExit("Agent2 case follow-up domain is disabled")
    if not settings.agent2_case_followup_send_enabled:
        raise SystemExit("Agent2 case follow-up send effect is disabled")
    if not settings.agent2_business_case_progress_enabled:
        raise SystemExit("Agent2 CaseProgress domain is disabled")
    if not settings.agent2_business_case_progress_write_enabled:
        raise SystemExit("Agent2 CaseProgress writes are disabled")
    if args.tenant_id not in allowed_tenants:
        raise SystemExit("tenant is not in the Agent2 Business allowlist")
    if args.expires_hours < 1 or args.expires_hours > 168:
        raise SystemExit("--expires-hours must be between 1 and 168")

    now = now_in_timezone(settings.timezone)
    async with AsyncSessionLocal() as session:
        async with session.begin():
            event = await enqueue_case_progress_followup(
                session,
                tenant_id=args.tenant_id,
                case_id=args.case_id,
                recipient_user_id=args.recipient_user_id,
                trigger_id=args.trigger_id,
                now=now,
                expires_at=now + timedelta(hours=args.expires_hours),
                allowed_tenant_ids=followup_tenants,
                allowed_user_ids=followup_users,
            )
        print(
            json.dumps(
                {
                    "notification_id": str(event.notification_id),
                    "tenant_id": event.tenant_id,
                    "recipient_user_id": event.recipient_user_id,
                    "message_type": event.message_type,
                    "status": event.status,
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    asyncio.run(_run(_arguments()))
