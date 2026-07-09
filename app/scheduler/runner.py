from __future__ import annotations

import asyncio
import logging
import signal
from datetime import date, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import get_settings
from app.db import AsyncSessionLocal
from app.llm.client import LLMClient
from app.llm.extractor import TeamSummaryGenerator
from app.scheduler.jobs import auto_submit_due_pending_reports, remind_missing_reports, send_user_message
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
        current_date = today_in_timezone(settings.timezone)
        if _scheduler_paused(settings, current_date):
            logger.info("daily report reminder skipped by scheduler pause date=%s", current_date.isoformat())
            return
        if not _reporting_required_on(current_date):
            logger.info("daily report reminder skipped by reporting calendar date=%s", current_date.isoformat())
            return
        async with AsyncSessionLocal() as session:
            await remind_missing_reports(session, settings, robot, current_date)
            await session.commit()

    async def second_reminder_job() -> None:
        current_date = today_in_timezone(settings.timezone)
        if _scheduler_paused(settings, current_date):
            logger.info("second report reminder skipped by scheduler pause date=%s", current_date.isoformat())
            return
        if not _reporting_required_on(current_date):
            logger.info("second report reminder skipped by reporting calendar date=%s", current_date.isoformat())
            return
        async with AsyncSessionLocal() as session:
            await remind_missing_reports(
                session,
                settings,
                robot,
                current_date,
                reminder_kind="second",
            )
            await session.commit()

    async def auto_submit_job() -> None:
        current_date = today_in_timezone(settings.timezone)
        if _scheduler_paused(settings, current_date):
            logger.info("auto submit skipped by scheduler pause date=%s", current_date.isoformat())
            return
        async with AsyncSessionLocal() as session:
            await auto_submit_due_pending_reports(session, settings)
            await session.commit()

    async def catchup_reminder_job() -> None:
        current_date = today_in_timezone(settings.timezone)
        report_date = _catchup_reminder_report_date(current_date)
        if report_date is None:
            logger.info("catchup reminder skipped by reporting calendar current_date=%s", current_date.isoformat())
            return
        if _scheduler_paused(settings, current_date) or _scheduler_paused(settings, report_date):
            logger.info(
                "catchup reminder skipped by scheduler pause current_date=%s report_date=%s",
                current_date.isoformat(),
                report_date.isoformat(),
            )
            return
        async with AsyncSessionLocal() as session:
            await remind_missing_reports(session, settings, robot, report_date, reminder_kind="catchup")
            await session.commit()

    async def summary_job() -> None:
        current_date = today_in_timezone(settings.timezone)
        summary_date = _daily_briefing_report_date(current_date)
        if summary_date is None:
            logger.info("daily briefing skipped by reporting calendar current_date=%s", current_date.isoformat())
            return
        if _scheduler_paused(settings, current_date) or _scheduler_paused(settings, summary_date):
            logger.info(
                "daily briefing skipped by scheduler pause current_date=%s summary_date=%s",
                current_date.isoformat(),
                summary_date.isoformat(),
            )
            return
        async with AsyncSessionLocal() as session:
            briefings = await summary_service.build_daily_briefings(session, summary_date)
            sent = await _send_daily_briefings(robot, briefings)
            logger.info("daily briefing sent date=%s sent=%s", summary_date.isoformat(), sent)
            try:
                await summary_service.generate_for_date(session, summary_date)
            except Exception:
                logger.exception("daily summary generation failed after briefing send")
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
    if settings.catchup_reminder_enabled:
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


async def _send_daily_briefings(robot: DingTalkRobotClient, briefings: dict) -> int:
    sent = 0
    messages = [*briefings.get("team_messages", []), *briefings.get("team_detail_messages", [])]
    if briefings.get("department_message"):
        messages.append(briefings["department_message"])
    if briefings.get("department_detail_message"):
        messages.append(briefings["department_detail_message"])
    for item in messages:
        user_ids = [user.get("dingtalk_user_id") for user in item.get("recipients", []) if user.get("dingtalk_user_id")]
        if not user_ids:
            continue
        title = f"{item.get('team_name') or item.get('department_name') or '部门'}晨报"
        await send_user_message(robot, user_ids, item.get("text") or "", markdown=True, title=title)
        sent += len(user_ids)
    return sent


def _reporting_required_on(target_date: date) -> bool:
    return target_date.weekday() < 5


def _catchup_reminder_report_date(current_date: date) -> date | None:
    if not _reporting_required_on(current_date):
        return None
    report_date = current_date - timedelta(days=1)
    if not _reporting_required_on(report_date):
        return None
    return report_date


def _daily_briefing_report_date(current_date: date) -> date | None:
    report_date = current_date - timedelta(days=1)
    if not _reporting_required_on(report_date):
        return None
    return report_date


def _scheduler_paused(settings, target_date: date) -> bool:
    return target_date in _scheduler_pause_dates(settings)


def _scheduler_pause_dates(settings) -> set[date]:
    raw = getattr(settings, "scheduler_pause_dates", "") or ""
    if not isinstance(raw, str):
        raw = ",".join(str(item) for item in raw)
    dates: set[date] = set()
    for part in raw.replace("\n", ",").replace(";", ",").split(","):
        value = part.strip()
        if not value:
            continue
        try:
            dates.add(date.fromisoformat(value))
        except ValueError:
            logger.warning("ignoring invalid scheduler pause date value=%s", value)
    return dates


if __name__ == "__main__":
    asyncio.run(run_scheduler())
