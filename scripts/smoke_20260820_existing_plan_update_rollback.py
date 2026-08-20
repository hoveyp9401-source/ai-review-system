from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.agent2.tool_calling.production_store import ToolCallCanaryReceipt
from app.agent2.typed_daily_executor import TYPED_AUDIT_KEY
from app.config import get_settings
from app.db import AsyncSessionLocal, engine
from app.llm.client import LLMClient
from app.models import DailyReport
from scripts.smoke_20260811_overnight_daily_rollback import (
    _turn,
    _user_and_control,
)


RUN_ID = f"existing-plan-update-{uuid4().hex[:12]}"
REPORT_DATE = date(2026, 5, 12)
SOURCE = (
    "你写的没有问题，不改\n\n"
    "计划，跟进苏宁38家债权，优先债权部分的确认情况，"
    "齐河智慧产业园上诉状答辩资料准备"
)


async def main() -> None:
    client = LLMClient(get_settings())
    result: dict[str, object] = {}
    try:
        async with AsyncSessionLocal() as session:
            try:
                user, settings = await _user_and_control(session)
                if await session.scalar(
                    select(DailyReport.id).where(
                        DailyReport.user_id == user.id,
                        DailyReport.report_date == REPORT_DATE,
                    )
                ):
                    raise AssertionError("rollback report date is occupied")
                report = DailyReport(
                    user_id=user.id,
                    team_id=user.team_id,
                    report_date=REPORT_DATE,
                    today_work=[
                        "齐河智慧产业园收到上诉状，整理相关答辩意见",
                        "广州融创沟通化债方案",
                        "宋都信息公开进展跟进",
                    ],
                    problems=[],
                    tomorrow_plan=[],
                    section_status={
                        "_agent2_report_version": 5,
                        "_draft_item_ids": {
                            "today_work": ["tw-1", "tw-2", "tw-3"],
                            "problems": [],
                            "tomorrow_plan": [],
                        },
                        "problems_acknowledged_empty": True,
                        TYPED_AUDIT_KEY: [],
                    },
                    completeness_score=Decimal("0.67"),
                    status="collecting",
                    confirmation_type="none",
                    confirmed_by_user=False,
                    source=RUN_ID,
                )
                session.add(report)
                await session.flush()
                source_message_id = f"{RUN_ID}-message"
                outcome = await _turn(
                    session,
                    user=user,
                    settings=settings,
                    llm_client=client,
                    text=SOURCE,
                    conversation_id=f"{RUN_ID}-conversation",
                    source_message_id=source_message_id,
                    now=datetime(
                        2026,
                        5,
                        13,
                        7,
                        18,
                        tzinfo=ZoneInfo(
                            user.timezone or settings.timezone
                        ),
                    ),
                    accepted_business_results=frozenset({"success"}),
                )
                await session.flush()
                await session.refresh(report)
                plans = list(report.tomorrow_plan or ())
                if len(plans) != 2 or not all(
                    any(fragment in item for item in plans)
                    for fragment in (
                        "苏宁38家债权",
                        "齐河智慧产业园",
                    )
                ):
                    raise AssertionError({"tomorrow_plan": plans})
                receipts = tuple(
                    (
                        await session.scalars(
                            select(ToolCallCanaryReceipt)
                            .where(
                                ToolCallCanaryReceipt.source_message_id
                                == source_message_id
                            )
                            .order_by(ToolCallCanaryReceipt.created_at)
                        )
                    ).all()
                )
                result = {
                    "status": "pass",
                    "business_result": outcome.user_visible_result,
                    "reply": outcome.message,
                    "tomorrow_plan": plans,
                    "report_status": report.status,
                    "receipts": [
                        {
                            "tool_name": row.tool_name,
                            "status": row.status,
                        }
                        for row in receipts
                    ],
                }
            finally:
                await session.rollback()
    finally:
        await client.close()

    async with AsyncSessionLocal() as session:
        report_count = int(
            await session.scalar(
                select(func.count(DailyReport.id)).where(
                    DailyReport.source == RUN_ID
                )
            )
            or 0
        )
        receipt_count = int(
            await session.scalar(
                select(func.count(ToolCallCanaryReceipt.receipt_id)).where(
                    ToolCallCanaryReceipt.source_message_id.like(
                        f"{RUN_ID}%"
                    )
                )
            )
            or 0
        )
        await session.rollback()
    residue = {"reports": report_count, "receipts": receipt_count}
    if any(residue.values()):
        raise AssertionError({"rollback_residue": residue})
    result["rollback_residue"] = residue
    result["dingtalk_send_calls"] = 0
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
