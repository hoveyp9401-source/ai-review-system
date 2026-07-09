from __future__ import annotations

import asyncio
import logging
import signal
from datetime import timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import get_settings
from app.db import AsyncSessionLocal
from app.llm.client import LLMClient
from app.llm.extractor import TeamSummaryGenerator
from app.scheduler.jobs import auto_submit_due_pending_reports, remind_missing_reports
from app.services.dingtalk import DingTalkRobotClient
from app.services.summary_service import SummaryService
from app.utils.time import today_in_timezone

logger = logging.getLogger(__name__)


async def run_scheduler() -> None:
    settings = get_settings()
    if not settings.scheduler_enabled:
        logger.warning("scheduler is disabled by SCHEDULER_ENABLED=false")
        return

    llm_client = LLMClient(settings)
    robot = DingTalkRobotClient(settings)
    summary_service = SummaryService(settings, TeamSummaryGenerator(llm_client))
    stop_event = asyncio.Event()

    async def reminder_job() -> None:
        async with AsyncSessionLocal() as session:
            await remind_missing_reports(session, settings, robot, today_in_timezone(settings.timezone))
            await session.commit()

    async def second_reminder_job() -> None:
        async with AsyncSessionLocal() as session:
            await remind_missing_reports(session, settings, robot, today_in_timezone(settings.timezone))
            await session.commit()

    async def auto_submit_job() -> None:
        async with AsyncSessionLocal() as session:
            await auto_submit_due_pending_reports(session, settings)
            await session.commit()

    async def catchup_reminder_job() -> None:
        async with AsyncSessionLocal() as session:
            report_date = today_in_timezone(settings.timezone) - timedelta(days=1)
            await remind_missing_reports(session, settings, robot, report_date, reminder_kind="catchup")
            await session.commit()

    async def summary_job() -> None:
        async with AsyncSessionLocal() as session:
            summary_date = today_in_timezone(settings.timezone) - timedelta(days=1)
            await summary_service.generate_for_date(session, summary_date)
            await session.commit()

    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    scheduler.add_job(
        reminder_job,
        CronTrigger(hour=settings.reminder_cron_hour, minute=0, timezone=settings.timezone),
        id="daily_report_reminder",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.add_job(
        second_reminder_job,
        CronTrigger(hour=settings.second_reminder_cron_hour, minute=0, timezone=settings.timezone),
        id="daily_report_second_reminder",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.add_job(
        auto_submit_job,
        CronTrigger(hour=settings.auto_submit_cron_hour, minute=0, timezone=settings.timezone),
        id="daily_report_auto_submit",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.add_job(
        catchup_reminder_job,
        CronTrigger(hour=settings.catchup_reminder_cron_hour, minute=0, timezone=settings.timezone),
        id="daily_report_catchup_reminder",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.add_job(
        summary_job,
        CronTrigger(hour=settings.summary_cron_hour, minute=settings.summary_cron_minute, timezone=settings.timezone),
        id="daily_team_summary",
        replace_existing=True,
        max_instances=1,
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    scheduler.start()
    await stop_event.wait()
    scheduler.shutdown(wait=True)
    await robot.close()
    await llm_client.close()


if __name__ == "__main__":
    asyncio.run(run_scheduler())
