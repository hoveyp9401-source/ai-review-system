from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date, timedelta

from app.config import get_settings
from app.db import AsyncSessionLocal
from app.scheduler.jobs import remind_missing_reports
from app.services.dingtalk import DingTalkRobotClient
from app.utils.time import today_in_timezone


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dry-run reminder scheduling without sending DingTalk messages.")
    parser.add_argument("--date", dest="report_date", help="Report date in YYYY-MM-DD. Defaults to today.")
    parser.add_argument("--kind", choices=["daily", "catchup"], default="daily")
    return parser.parse_args()


async def main() -> None:
    args = _parse_args()
    settings = get_settings()
    report_date = date.fromisoformat(args.report_date) if args.report_date else today_in_timezone(settings.timezone)
    if args.kind == "catchup" and not args.report_date:
        report_date = report_date - timedelta(days=1)

    robot = DingTalkRobotClient(settings)
    try:
        async with AsyncSessionLocal() as session:
            result = await remind_missing_reports(
                session,
                settings,
                robot,
                report_date,
                reminder_kind=args.kind,
                dry_run=True,
            )
            await session.rollback()
    finally:
        await robot.close()

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
