from __future__ import annotations

import asyncio
import base64
from datetime import date
import json
import os

from sqlalchemy import select

from app.db import AsyncSessionLocal, engine
from app.models import DailyReport, User


async def main() -> None:
    encoded_name = str(os.environ["AUDIT_USER_NAME_B64"]).strip()
    user_name = base64.b64decode(encoded_name).decode("utf-8")
    requested_dates = tuple(
        date.fromisoformat(value.strip())
        for value in str(os.environ["AUDIT_REPORT_DATES"]).split(",")
        if value.strip()
    )
    async with AsyncSessionLocal() as session:
        users = tuple(
            (
                await session.scalars(
                    select(User).where(
                        User.name == user_name,
                        User.active.is_(True),
                    )
                )
            ).all()
        )
        result: dict[str, object] = {
            "exact_active_user_count": len(users),
            "requested_dates": [item.isoformat() for item in requested_dates],
            "reports": [],
        }
        if len(users) == 1:
            if str(os.getenv("AUDIT_INCLUDE_USER_ID", "")).lower() == "true":
                result["user_id"] = str(users[0].id)
            reports = tuple(
                (
                    await session.scalars(
                        select(DailyReport)
                        .where(
                            DailyReport.user_id == users[0].id,
                            DailyReport.report_date.in_(requested_dates),
                        )
                        .order_by(DailyReport.report_date)
                    )
                ).all()
            )
            result["reports"] = [
                {
                    "date": report.report_date.isoformat(),
                    "status": str(report.status),
                    "today_work_count": len(report.today_work or []),
                    "problems_count": len(report.problems or []),
                    "tomorrow_plan_count": len(report.tomorrow_plan or []),
                }
                for report in reports
            ]
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
