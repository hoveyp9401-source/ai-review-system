from __future__ import annotations

import asyncio
from datetime import date

from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.models import DailyReport


async def main() -> None:
    async with AsyncSessionLocal() as session:
        report = await session.scalar(
            select(DailyReport).where(
                DailyReport.user_id
                == "91a1b0b0-201e-490f-8272-ad2d74e803f7",
                DailyReport.report_date == date(2026, 8, 3),
            )
        )
        print(
            {
                "id": str(report.id),
                "status": report.status,
                "today_work": report.today_work,
                "problems": report.problems,
                "tomorrow_plan": report.tomorrow_plan,
                "section_status": report.section_status,
            }
        )


if __name__ == "__main__":
    asyncio.run(main())
