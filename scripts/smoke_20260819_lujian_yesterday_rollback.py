from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.agent2.tool_calling.production_store import ToolCallCanaryReceipt
from app.config import get_settings
from app.db import AsyncSessionLocal, engine
from app.llm.client import LLMClient
from app.models import DailyReport
from scripts.smoke_20260811_overnight_daily_rollback import (
    _turn,
    _user_and_control,
)


RUN_ID = f"lujian-yday-{uuid4().hex[:10]}"
NOW_DATE = date(2026, 5, 9)
TARGET_DATE = date(2026, 5, 8)
SOURCE = (
    "补一下昨天的\n"
    "今日工作：\n"
    "1. 日常用印的审核\n"
    "2. 未归档合同催收\n"
    "3. 进行绩效技能的测试\n"
    "4.施工合同归档闭环\n\n"
    "问题风险：暂无\n\n"
    "明日计划：\n"
    "1. 日常用印的审核\n"
    "2. 未归档合同催收\n"
    "3. 绩效技能数据源的梳理"
)


async def main() -> None:
    client = LLMClient(get_settings())
    async with AsyncSessionLocal() as session:
        try:
            user, settings = await _user_and_control(session)
            for report_date in (TARGET_DATE, NOW_DATE):
                if await session.scalar(
                    select(DailyReport.id).where(
                        DailyReport.user_id == user.id,
                        DailyReport.report_date == report_date,
                    )
                ):
                    raise AssertionError({"occupied_date": report_date.isoformat()})
            audits: list[dict[str, object]] = []
            outcome = await _turn(
                session,
                user=user,
                settings=settings,
                llm_client=client,
                text=SOURCE,
                conversation_id=f"{RUN_ID}-conversation",
                source_message_id=f"{RUN_ID}-message",
                now=datetime.combine(
                    NOW_DATE,
                    datetime.min.time().replace(hour=8, minute=40),
                    tzinfo=ZoneInfo(user.timezone or settings.timezone),
                ),
                accepted_business_results=frozenset({"success"}),
                model_audit_sink=audits,
            )
            await session.flush()
            report = await session.scalar(
                select(DailyReport).where(
                    DailyReport.user_id == user.id,
                    DailyReport.report_date == TARGET_DATE,
                )
            )
            if report is None:
                raise AssertionError({"missing_target_report": audits})
            counts = {
                "today_work": len(report.today_work or ()),
                "problems": len(report.problems or ()),
                "tomorrow_plan": len(report.tomorrow_plan or ()),
            }
            if counts != {"today_work": 4, "problems": 0, "tomorrow_plan": 3}:
                raise AssertionError({"counts": counts, "audits": audits})
            result = {
                "status": "pass",
                "business_result": outcome.user_visible_result,
                "report_date": report.report_date.isoformat(),
                "field_counts": counts,
            }
        finally:
            await session.rollback()
    await client.close()
    async with AsyncSessionLocal() as session:
        reports = int(
            await session.scalar(
                select(func.count(DailyReport.id)).where(
                    DailyReport.source == RUN_ID
                )
            )
            or 0
        )
        receipts = int(
            await session.scalar(
                select(func.count(ToolCallCanaryReceipt.receipt_id)).where(
                    ToolCallCanaryReceipt.source_message_id.like(f"{RUN_ID}%")
                )
            )
            or 0
        )
        await session.rollback()
    residue = {"reports": reports, "receipts": receipts}
    if any(residue.values()):
        raise AssertionError({"residue": residue})
    result["rollback_residue"] = residue
    result["dingtalk_send_calls"] = 0
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
