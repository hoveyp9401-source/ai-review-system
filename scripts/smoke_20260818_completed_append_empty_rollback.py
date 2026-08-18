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


RUN_ID = f"completed-followup-{uuid4().hex[:10]}"


def _report(*, user, report_date: date, completed: bool) -> DailyReport:
    return DailyReport(
        user_id=user.id,
        team_id=user.team_id,
        report_date=report_date,
        today_work=["已有工作一", "已有工作二"],
        problems=[],
        tomorrow_plan=["已有明日计划"],
        section_status={
            "_agent2_report_version": 3,
            "_draft_item_ids": {
                "today_work": ["tw-1", "tw-2"],
                "problems": [],
                "tomorrow_plan": ["tp-1"],
            },
            TYPED_AUDIT_KEY: [],
        },
        completeness_score=Decimal("0.67"),
        status="completed" if completed else "collecting",
        confirmation_type="user_confirmed" if completed else "none",
        confirmed_by_user=completed,
        source=RUN_ID,
    )


async def _receipts(session, source_message_id: str) -> list[str]:
    rows = list(
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
    return [row.tool_name for row in rows]


async def _run_case(
    client: LLMClient,
    *,
    report_date: date,
    completed: bool,
    text: str,
    name: str,
) -> dict[str, object]:
    async with AsyncSessionLocal() as session:
        try:
            user, settings = await _user_and_control(session)
            existing = await session.scalar(
                select(DailyReport.id).where(
                    DailyReport.user_id == user.id,
                    DailyReport.report_date == report_date,
                )
            )
            if existing is not None:
                raise AssertionError("smoke date is occupied")
            report = _report(
                user=user,
                report_date=report_date,
                completed=completed,
            )
            session.add(report)
            await session.flush()
            source_message_id = f"{RUN_ID}-{name}"
            outcome = await _turn(
                session,
                user=user,
                settings=settings,
                llm_client=client,
                text=text,
                conversation_id=f"{RUN_ID}-{name}-conversation",
                source_message_id=source_message_id,
                now=datetime.combine(
                    report_date,
                    datetime.min.time().replace(hour=19),
                    tzinfo=ZoneInfo(user.timezone or settings.timezone),
                ),
                accepted_business_results=frozenset({"success"}),
            )
            await session.flush()
            await session.refresh(report)
            tools = await _receipts(session, source_message_id)
            if tools != ["add_daily_items"]:
                raise AssertionError({"tools": tools})
            if completed:
                if not any("中建大兴" in item for item in report.today_work):
                    raise AssertionError({"today_work": report.today_work})
                if report.status != "completed":
                    raise AssertionError({"status": report.status})
            else:
                if not bool(
                    (report.section_status or {}).get(
                        "problems_acknowledged_empty"
                    )
                ):
                    raise AssertionError(
                        {"section_status": report.section_status}
                    )
            return {
                "name": name,
                "status": "pass",
                "business_result": outcome.user_visible_result,
                "tools": tools,
                "report_status": report.status,
            }
        finally:
            await session.rollback()


async def main() -> None:
    client = LLMClient(get_settings())
    try:
        results = [
            await _run_case(
                client,
                report_date=date(2026, 5, 5),
                completed=True,
                text=(
                    "今日工作再补充一点，作为第五点："
                    "中建大兴项目增补协议沟通审核"
                ),
                name="completed_append",
            ),
            await _run_case(
                client,
                report_date=date(2026, 5, 6),
                completed=False,
                text="没有问题了",
                name="acknowledge_empty_problem",
            ),
        ]
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
                    ToolCallCanaryReceipt.source_message_id.like(f"{RUN_ID}%")
                )
            )
            or 0
        )
        await session.rollback()
    residue = {"reports": report_count, "receipts": receipt_count}
    if any(residue.values()):
        raise AssertionError({"rollback_residue": residue})
    print(
        json.dumps(
            {
                "status": "pass",
                "results": results,
                "rollback_residue": residue,
                "dingtalk_send_calls": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
