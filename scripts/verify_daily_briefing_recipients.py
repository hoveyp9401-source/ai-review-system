from __future__ import annotations

import asyncio
import json
from datetime import date

from app.config import get_settings
from app.db import AsyncSessionLocal
from app.services.management_daily_briefing import (
    ManagementDailyBriefingService,
)


async def main() -> None:
    settings = get_settings()
    async with AsyncSessionLocal() as session:
        briefings = await ManagementDailyBriefingService(settings).build(
            session,
            date(2026, 8, 3),
        )
    messages = list(briefings.get("team_messages") or [])
    department = briefings.get("department_message")
    if department:
        messages.append(department)
    recipients = [
        {
            "scope": message.get("scope"),
            "team_id": message.get("team_id"),
            "team_name": message.get("team_name"),
            "name": recipient.get("name"),
            "dingtalk_user_id": recipient.get("dingtalk_user_id"),
            "role": recipient.get("role"),
        }
        for message in messages
        for recipient in message.get("recipients") or []
    ]
    liu = [row for row in recipients if row["dingtalk_user_id"] == "55264"]
    pang = [row for row in recipients if row["dingtalk_user_id"] == "40842"]
    if len(liu) != 1 or pang:
        raise AssertionError({"liu": liu, "pang": pang})
    if (
        liu[0]["scope"] != "team"
        or liu[0]["team_id"]
        != "783ac5ac-527a-40b0-9a3c-fb53d8d4f951"
        or liu[0]["role"] != "team_lead"
    ):
        raise AssertionError(liu)
    cc = str(
        settings.management_daily_briefing_department_cc_user_ids or ""
    ).strip()
    if cc:
        raise AssertionError(f"department CC is not empty: {cc}")
    print(
        json.dumps(
            {
                "department_cc_empty": True,
                "liu_recipients": liu,
                "pang_recipients": pang,
                "warning_count": len(
                    briefings.get("recipient_warnings") or []
                ),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
