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
from app.models import DailyReport, WebhookEvent
from scripts.smoke_20260811_overnight_daily_rollback import (
    _turn,
    _user_and_control,
)


RUN_ID = f"ctx-repl-{uuid4().hex[:12]}"
REPORT_DATE = date(2026, 4, 25)
CONVERSATION_ID = f"{RUN_ID}-conversation"
COMBINED = (
    "明天计划继续找可以做成网页端的agent技能然后"
    "被告案件进行通报与未结案案件的签约"
)
EXPECTED = [
    "明天计划继续找可以做成网页端的agent技能",
    "被告案件进行通报与未结案案件的签约",
]


async def _residue() -> dict[str, int]:
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
        events = int(
            await session.scalar(
                select(func.count(WebhookEvent.id)).where(
                    WebhookEvent.idempotency_key.like(f"{RUN_ID}%")
                )
            )
            or 0
        )
        await session.rollback()
        return {"reports": reports, "receipts": receipts, "events": events}


async def main() -> None:
    if any((initial := await _residue()).values()):
        raise AssertionError({"preexisting_residue": initial})
    client = LLMClient(get_settings())
    result: dict[str, object] = {}
    try:
        async with AsyncSessionLocal() as session:
            try:
                user, settings = await _user_and_control(session)
                existing = await session.scalar(
                    select(DailyReport.id).where(
                        DailyReport.user_id == user.id,
                        DailyReport.report_date == REPORT_DATE,
                    )
                )
                if existing is not None:
                    raise AssertionError("smoke report date is already occupied")
                report = DailyReport(
                    user_id=user.id,
                    team_id=user.team_id,
                    report_date=REPORT_DATE,
                    today_work=[],
                    problems=[],
                    tomorrow_plan=[COMBINED],
                    section_status={
                        "_agent2_report_version": 4,
                        "_draft_item_ids": {
                            "today_work": [],
                            "problems": [],
                            "tomorrow_plan": ["tomorrow-combined"],
                        },
                        TYPED_AUDIT_KEY: [],
                    },
                    completeness_score=Decimal("0.5"),
                    status="pending_confirmation",
                    confirmation_type="none",
                    confirmed_by_user=False,
                    source=RUN_ID,
                )
                session.add(report)
                await session.flush()
                now = datetime.combine(
                    REPORT_DATE,
                    datetime.min.time().replace(hour=19),
                    tzinfo=ZoneInfo(user.timezone or settings.timezone),
                )
                first = await _turn(
                    session,
                    user=user,
                    settings=settings,
                    llm_client=client,
                    text="补充今日工作：完成合同复核",
                    conversation_id=CONVERSATION_ID,
                    source_message_id=f"{RUN_ID}-seed-write",
                    now=now,
                    accepted_business_results=frozenset({"success"}),
                )
                correction_result = "reply_only"
                try:
                    correction = await _turn(
                        session,
                        user=user,
                        settings=settings,
                        llm_client=client,
                        text="明日计划是两条",
                        conversation_id=CONVERSATION_ID,
                        source_message_id=f"{RUN_ID}-correction",
                        now=now,
                        accepted_business_results=frozenset(
                            {"reply_only", "clarification"}
                        ),
                    )
                    correction_result = correction.user_visible_result
                except AssertionError as exc:
                    if "BUSINESS_RESULT_BLOCKED" not in str(exc):
                        raise
                    correction_result = "blocked_no_write_clarification"
                await session.refresh(report)
                if list(report.tomorrow_plan or ()) != [COMBINED]:
                    raise AssertionError(
                        {"correction_turn_changed_report": report.tomorrow_plan}
                    )
                replacement_audits: list[dict[str, object]] = []
                replacement = await _turn(
                    session,
                    user=user,
                    settings=settings,
                    llm_client=client,
                    text=(
                        "1. 明天计划继续找可以做成网页端的agent技能\n"
                        "2.被告案件进行通报与未结案案件的签约"
                    ),
                    conversation_id=CONVERSATION_ID,
                    source_message_id=f"{RUN_ID}-replacement",
                    now=now,
                    accepted_business_results=frozenset({"success"}),
                    model_audit_sink=replacement_audits,
                )
                await session.flush()
                await session.refresh(report)
                if list(report.tomorrow_plan or ()) != EXPECTED:
                    raise AssertionError(
                        {
                            "unexpected_tomorrow_plan": report.tomorrow_plan,
                            "replacement_model_audits": replacement_audits,
                        }
                    )
                if "完成合同复核" not in list(report.today_work or ()):
                    raise AssertionError({"unexpected_today_work": report.today_work})
                if report.status != "pending_confirmation":
                    raise AssertionError({"status_changed": report.status})
                rows = list(
                    (
                        await session.scalars(
                            select(ToolCallCanaryReceipt)
                            .where(
                                ToolCallCanaryReceipt.source_message_id
                                == f"{RUN_ID}-replacement"
                            )
                            .order_by(ToolCallCanaryReceipt.created_at)
                        )
                    ).all()
                )
                tools = [row.tool_name for row in rows]
                if tools != ["delete_daily_items", "add_daily_items"]:
                    raise AssertionError({"unexpected_tools": tools})
                if not all(value in replacement.message for value in EXPECTED):
                    raise AssertionError(
                        {"updated_snapshot_not_shown": replacement.message}
                    )
                result = {
                    "status": "pass",
                    "tools": tools,
                    "tomorrow_plan": list(report.tomorrow_plan or ()),
                    "report_status": report.status,
                    "seed_result": first.user_visible_result,
                    "correction_result": correction_result,
                    "replacement_result": replacement.user_visible_result,
                    "updated_snapshot_shown": True,
                    "dingtalk_send_calls": 0,
                }
            finally:
                await session.rollback()
    finally:
        await client.close()
    residue = await _residue()
    if any(residue.values()):
        raise AssertionError({"rollback_residue": residue})
    result["rollback_residue"] = residue
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
