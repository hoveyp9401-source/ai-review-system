from __future__ import annotations

import asyncio
from datetime import date
import json

from sqlalchemy import text

from app.config import get_settings
from app.db import AsyncSessionLocal, engine
from app.scheduler.jobs import ensure_daily_submission_obligations
from app.services.management_daily_briefing import ManagementDailyBriefingService


REPORT_DATE = date(2026, 8, 7)


async def main() -> None:
    settings = get_settings()
    async with AsyncSessionLocal() as session:
        before_count = int(
            await session.scalar(
                text(
                    """
                    SELECT COUNT(*)
                    FROM legal_daily_submission_obligations
                    WHERE tenant_id = :tenant_id
                      AND report_date = :report_date
                    """
                ),
                {
                    "tenant_id": settings.legal_daily_dashboard_tenant_id,
                    "report_date": REPORT_DATE,
                },
            )
            or 0
        )
        obligation_preview = await ensure_daily_submission_obligations(
            session,
            settings,
            REPORT_DATE,
        )
        await session.flush()
        payload = await ManagementDailyBriefingService(settings).build(
            session,
            REPORT_DATE,
        )
        await session.rollback()

    async with AsyncSessionLocal() as verification_session:
        await verification_session.execute(text("SET TRANSACTION READ ONLY"))
        after_rollback_count = int(
            await verification_session.scalar(
                text(
                    """
                    SELECT COUNT(*)
                    FROM legal_daily_submission_obligations
                    WHERE tenant_id = :tenant_id
                      AND report_date = :report_date
                    """
                ),
                {
                    "tenant_id": settings.legal_daily_dashboard_tenant_id,
                    "report_date": REPORT_DATE,
                },
            )
            or 0
        )
        await verification_session.rollback()

    team_messages = []
    for item in payload.get("team_messages", []):
        team_messages.append(
            {
                "team_name": item.get("team_name"),
                "recipients": [
                    recipient.get("name")
                    for recipient in item.get("recipients", [])
                ],
                "recipient_dingtalk_ids_present": all(
                    bool(recipient.get("dingtalk_user_id"))
                    for recipient in item.get("recipients", [])
                ),
                "target_count": item.get("target_count"),
                "stats": item.get("stats"),
                "message_chars": len(str(item.get("text") or "")),
            }
        )
    department = payload.get("department_message") or {}
    output = {
        "report_date": REPORT_DATE.isoformat(),
        "team_message_count": len(team_messages),
        "team_messages": team_messages,
        "department_name": department.get("department_name"),
        "department_recipients": [
            recipient.get("name")
            for recipient in department.get("recipients", [])
        ],
        "department_recipient_dingtalk_ids_present": all(
            bool(recipient.get("dingtalk_user_id"))
            for recipient in department.get("recipients", [])
        ),
        "department_target_count": department.get("target_count"),
        "department_stats": department.get("stats"),
        "department_message_chars": len(str(department.get("text") or "")),
        "recipient_warnings": payload.get("recipient_warnings", []),
        "obligations_before": before_count,
        "obligation_preview": obligation_preview,
        "obligations_after_rollback": after_rollback_count,
        "database_writes_persisted": after_rollback_count - before_count,
        "dingtalk_send_calls": 0,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
