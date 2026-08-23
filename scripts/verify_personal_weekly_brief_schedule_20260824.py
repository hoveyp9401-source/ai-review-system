from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-process-id", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def _load_process_environment(pid: int) -> None:
    values = {
        item.split(b"=", 1)[0].decode(): item.split(b"=", 1)[1].decode()
        for item in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        if b"=" in item
    }
    if pid <= 1 or not values:
        raise RuntimeError("scheduler process environment is unavailable")
    os.environ.clear()
    os.environ.update(values)


async def main() -> None:
    args = _args()
    if args.output.exists() or args.output.is_symlink():
        raise RuntimeError("schedule evidence path exists")
    _load_process_environment(args.source_process_id)

    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from app.agent2.personal_weekly_brief_store import SqlPersonalWeeklyBriefStore
    from app.config import get_settings
    from app.db import AsyncSessionLocal, engine
    from app.scheduler.runner import (
        PERSONAL_WEEKLY_BRIEF_TIMEZONE,
        register_personal_weekly_brief_jobs,
    )

    get_settings.cache_clear()
    settings = get_settings()
    scheduler = AsyncIOScheduler(timezone=settings.timezone)

    async def generation_job() -> None:
        return None

    async def reconciliation_job() -> None:
        return None

    registered = register_personal_weekly_brief_jobs(
        scheduler,
        settings=settings,
        generation_job=generation_job,
        reconciliation_job=reconciliation_job,
    )
    generation = scheduler.get_job("agent2_personal_weekly_brief_generate")
    reconciliation = scheduler.get_job("agent2_personal_weekly_brief_reconcile")
    timezone = ZoneInfo(PERSONAL_WEEKLY_BRIEF_TIMEZONE)
    now = datetime.now(timezone)
    next_generation = generation.trigger.get_next_fire_time(None, now)
    tenant_id = str(settings.agent2_personal_weekly_brief_tenant_id or "").strip()
    async with AsyncSessionLocal() as session:
        rows = await SqlPersonalWeeklyBriefStore(session).load_for_week(
            tenant_id=tenant_id,
            week_start=datetime(2026, 8, 17, tzinfo=timezone).date(),
            limit=100,
        )
        await session.rollback()
    statuses = Counter(row.status for row in rows)
    delivered = tuple(row for row in rows if row.status == "delivered")
    exact_receipts = sum(
        bool(
            (receipt := dict(row.delivery_receipt_json or {})).get(
                "delivery_verified"
            )
            is True
            and receipt.get("delivery_status") == "SUCCESS"
            and len(receipt.get("delivered_dingtalk_user_ids") or []) == 1
        )
        for row in delivered
    )
    payload = {
        "status": "PASS",
        "generation_enabled": settings.agent2_personal_weekly_brief_enabled,
        "send_enabled": settings.agent2_personal_weekly_brief_send_enabled,
        "registered_jobs": list(registered),
        "generation_trigger": str(generation.trigger),
        "reconciliation_seconds": int(
            reconciliation.trigger.interval.total_seconds()
        ),
        "next_generation": next_generation.isoformat(),
        "batch_statuses": dict(statuses),
        "delivered": len(delivered),
        "exact_delivery_receipts": exact_receipts,
        "context_recorded": sum(
            row.context_recorded_at is not None for row in rows
        ),
    }
    if not (
        payload["generation_enabled"] is True
        and payload["send_enabled"] is True
        and registered
        == (
            "agent2_personal_weekly_brief_generate",
            "agent2_personal_weekly_brief_reconcile",
        )
        and "day_of_week='sat'" in payload["generation_trigger"]
        and "hour='9'" in payload["generation_trigger"]
        and "minute='0'" in payload["generation_trigger"]
        and payload["reconciliation_seconds"] == 300
        and next_generation.weekday() == 5
        and (next_generation.hour, next_generation.minute) == (9, 0)
        and statuses == {"delivered": 73, "failed": 1}
        and exact_receipts == 73
        and payload["context_recorded"] == 72
    ):
        payload["status"] = "FAIL"
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    args.output.chmod(0o600)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    await engine.dispose()
    if payload["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
