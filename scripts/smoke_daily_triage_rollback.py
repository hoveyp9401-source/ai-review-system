from __future__ import annotations

import asyncio
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text

from app.agent2.tool_calling.assembly import TrustedContextRequest
from app.agent2.tool_calling.context import CANARY_STATE_NAMESPACE
from app.agent2.tool_calling.production_store import ProductionContextStore
from app.config import get_settings
from app.db import AsyncSessionLocal
from app.models import DailyReport, ReportInteractionEvent, User
from app.scheduler.jobs import (
    _load_preferred_salutations,
    auto_submit_due_pending_reports,
    build_report_reminder_text,
)
from app.scheduler.runner import _send_daily_briefings


LIU_USER_ID = "91a1b0b0-201e-490f-8272-ad2d74e803f7"
PANG_USER_ID = "222b1eeb-4faa-40cf-a193-e1892c9377b0"
REPORT_DATE = date(2026, 8, 3)


class FakeRobot:
    async def send_robot_direct_markdown(self, **kwargs):
        if kwargs["user_ids"] != ["55264"]:
            raise AssertionError(kwargs["user_ids"])
        return {"processQueryKey": "rollback-smoke-provider-ref"}


async def main() -> None:
    settings = get_settings()
    now = datetime.now(ZoneInfo(settings.timezone))
    output: dict[str, object] = {}

    async with AsyncSessionLocal() as session:
        users = list(
            (
                await session.scalars(
                    select(User).where(User.id.in_([LIU_USER_ID, PANG_USER_ID]))
                )
            ).all()
        )
        users_by_id = {str(user.id): user for user in users}
        if set(users_by_id) != {LIU_USER_ID, PANG_USER_ID}:
            raise AssertionError(f"users not found: {sorted(users_by_id)}")

        salutations = await _load_preferred_salutations(
            session,
            [user.id for user in users],
            now=now,
        )
        salutation_output = {
            user.name: salutations.get(user.id) for user in users
        }
        if salutation_output != {"刘聪": "四哥", "庞浩": "庞总"}:
            raise AssertionError(salutation_output)
        output["salutations"] = salutation_output
        output["reminder_prefixes"] = {
            user.name: build_report_reminder_text(
                REPORT_DATE,
                user,
                None,
                preferred_salutation=salutations.get(user.id),
            ).split("，", 1)[0]
            for user in users
        }

        report = await session.scalar(
            select(DailyReport).where(
                DailyReport.user_id == LIU_USER_ID,
                DailyReport.report_date == REPORT_DATE,
            )
        )
        if report is None:
            raise AssertionError("Liu report not found")
        original = {
            "status": report.status,
            "confirmation_type": report.confirmation_type,
            "confirmed_by_user": report.confirmed_by_user,
            "submitted_at": report.submitted_at,
            "auto_submit_at": report.auto_submit_at,
        }
        report.status = "collecting"
        report.confirmed_by_user = False
        report.submitted_at = None
        await session.flush()

        auto_submit = await auto_submit_due_pending_reports(
            session,
            settings,
            now=now,
            report_date=REPORT_DATE,
        )
        raw_before_flush = (
            await session.execute(
                text("SELECT status FROM daily_reports WHERE id = :report_id"),
                {"report_id": report.id},
            )
        ).scalar_one()
        if raw_before_flush != "collecting":
            raise AssertionError(raw_before_flush)
        await session.flush()
        raw_after_flush = (
            await session.execute(
                text("SELECT status FROM daily_reports WHERE id = :report_id"),
                {"report_id": report.id},
            )
        ).scalar_one()
        if raw_after_flush != "completed":
            raise AssertionError(raw_after_flush)
        output["auto_submit"] = {
            "count": auto_submit["auto_submitted"],
            "raw_before_runner_flush": raw_before_flush,
            "raw_after_runner_flush": raw_after_flush,
        }

        event_count_before = await session.scalar(
            select(func.count(ReportInteractionEvent.id)).where(
                ReportInteractionEvent.user_id == LIU_USER_ID,
                ReportInteractionEvent.backend_action == "daily_briefing_sent",
            )
        )
        briefing_text = "8月3日综合管理部晨报（回滚演练，不会真实发送）"
        sent = await _send_daily_briefings(
            FakeRobot(),
            {
                "date": REPORT_DATE.isoformat(),
                "team_messages": [
                    {
                        "scope": "team",
                        "team_id": "783ac5ac-527a-40b0-9a3c-fb53d8d4f951",
                        "team_name": "综合管理部",
                        "text": briefing_text,
                        "recipients": [
                            {
                                "id": LIU_USER_ID,
                                "name": "刘聪",
                                "dingtalk_user_id": "55264",
                            }
                        ],
                    }
                ],
            },
            session=session,
            report_date=REPORT_DATE,
        )
        await session.flush()
        event_count_after = await session.scalar(
            select(func.count(ReportInteractionEvent.id)).where(
                ReportInteractionEvent.user_id == LIU_USER_ID,
                ReportInteractionEvent.backend_action == "daily_briefing_sent",
            )
        )
        if sent != 1 or event_count_after != event_count_before + 1:
            raise AssertionError((sent, event_count_before, event_count_after))

        liu = users_by_id[LIU_USER_ID]
        context_store = ProductionContextStore(
            session,
            user=liu,
            tenant_id="sandbox-agent2-phase2-20260711",
            settings=settings,
        )
        recent = await context_store.load_recent_messages(
            TrustedContextRequest(
                tenant_id="sandbox-agent2-phase2-20260711",
                user_id=liu.id,
                conversation_id="rollback-smoke-daily-briefing",
                source_message_id="rollback-smoke-current",
                timezone=settings.timezone,
                server_now=now,
                display_name=liu.name,
            ),
            namespace=CANARY_STATE_NAMESPACE,
            limit=12,
        )
        scheduled = [
            item
            for item in recent
            if item.source_message_id.startswith("daily-briefing:")
        ]
        if not scheduled or scheduled[-1].content != briefing_text:
            raise AssertionError(scheduled)
        output["briefing_audit"] = {
            "event_delta": event_count_after - event_count_before,
            "context_source": scheduled[-1].source_message_id,
            "context_body_matches": scheduled[-1].content == briefing_text,
        }

        for field, value in original.items():
            setattr(report, field, value)
        await session.rollback()

    async with AsyncSessionLocal() as verification_session:
        restored_report = await verification_session.scalar(
            select(DailyReport).where(
                DailyReport.user_id == LIU_USER_ID,
                DailyReport.report_date == REPORT_DATE,
            )
        )
        event_count_restored = await verification_session.scalar(
            select(func.count(ReportInteractionEvent.id)).where(
                ReportInteractionEvent.user_id == LIU_USER_ID,
                ReportInteractionEvent.backend_action == "daily_briefing_sent",
            )
        )
        output["rollback"] = {
            "report_status": restored_report.status,
            "report_confirmation_type": restored_report.confirmation_type,
            "event_count_restored": event_count_restored == event_count_before,
        }
        if (
            restored_report.status != original["status"]
            or restored_report.confirmation_type != original["confirmation_type"]
            or event_count_restored != event_count_before
        ):
            raise AssertionError(output["rollback"])

    print(output)


if __name__ == "__main__":
    asyncio.run(main())
