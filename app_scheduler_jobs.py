from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import DailyReport, User
from app.repositories import list_missing_users
from app.services.dingtalk import DingTalkRobotClient
from app.services.state_machine import (
    CONFIRMATION_AUTO_SUBMITTED_TIMEOUT,
    STATUS_COMPLETED,
    STATUS_PENDING_CONFIRMATION,
)
from app.utils.time import now_in_timezone

SECTION_LABELS = {
    "today_work": "\u4eca\u65e5\u5de5\u4f5c",
    "problems": "\u95ee\u9898/\u98ce\u9669",
    "tomorrow_plan": "\u660e\u65e5\u8ba1\u5212",
}


async def remind_missing_reports(
    session: AsyncSession,
    settings: Settings,
    robot: DingTalkRobotClient,
    report_date: date,
    *,
    reminder_kind: str = "daily",
    dry_run: bool = False,
) -> dict[str, Any]:
    missing_users = await list_missing_users(session, report_date)
    test_user_ids = _configured_test_user_ids(settings)
    target_users, skipped_real_users = _partition_reminder_users(missing_users, test_user_ids)
    requested_dry_run = bool(dry_run or getattr(settings, "reminder_dry_run", True))
    send_enabled = bool(getattr(settings, "reminder_send_enabled", False))
    effective_dry_run = requested_dry_run or not send_enabled or not test_user_ids
    send_block_reasons: list[str] = []
    if requested_dry_run:
        send_block_reasons.append("dry_run")
    if not send_enabled:
        send_block_reasons.append("send_disabled")
    if not test_user_ids:
        send_block_reasons.append("no_test_user_ids")

    by_team = defaultdict(list)
    for user in target_users:
        by_team[user.team].append(user)

    sent = 0
    skipped = 0
    would_send = 0
    skipped_no_channel = 0
    sent_by_group_robot = 0
    sent_by_direct_robot = 0
    sent_by_work_notification = 0
    dry_run_messages: list[dict[str, Any]] = []
    sent_user_ids = []
    reports_by_user = await _load_reports_by_user(session, report_date, [user.id for user in target_users])
    for team, users in by_team.items():
        webhook = team.dingtalk_webhook_url or settings.dingtalk_default_robot_webhook
        secret = team.dingtalk_webhook_secret or settings.dingtalk_default_robot_secret
        grouped_messages = _group_users_by_reminder_text(report_date, users, reports_by_user, reminder_kind=reminder_kind)
        has_direct_robot = robot.has_enterprise_app()
        if test_user_ids and has_direct_robot:
            for text, grouped_users in grouped_messages.items():
                if effective_dry_run:
                    would_send += len(grouped_users)
                    dry_run_messages.append(_dry_run_message("direct_robot", team, grouped_users, text))
                    continue
                await robot.send_robot_direct_text(
                    user_ids=[user.dingtalk_user_id for user in grouped_users],
                    text=text,
                )
                sent += len(grouped_users)
                sent_by_direct_robot += len(grouped_users)
                sent_user_ids.extend(user.id for user in grouped_users)
        elif test_user_ids:
            for text, grouped_users in grouped_messages.items():
                dry_run_messages.append(_dry_run_message("skipped_test_requires_direct_robot", team, grouped_users, text))
            skipped += len(users)
            skipped_no_channel += len(users)
        elif webhook:
            for text, grouped_users in grouped_messages.items():
                if effective_dry_run:
                    would_send += len(grouped_users)
                    dry_run_messages.append(_dry_run_message("group_robot", team, grouped_users, text))
                    continue
                await robot.send_text(
                    webhook_url=webhook,
                    secret=secret,
                    text=text,
                    at_user_ids=[user.dingtalk_user_id for user in grouped_users],
                )
                sent += len(grouped_users)
                sent_by_group_robot += len(grouped_users)
                sent_user_ids.extend(user.id for user in grouped_users)
        elif has_direct_robot:
            for text, grouped_users in grouped_messages.items():
                if effective_dry_run:
                    would_send += len(grouped_users)
                    dry_run_messages.append(_dry_run_message("direct_robot", team, grouped_users, text))
                    continue
                await robot.send_robot_direct_text(
                    user_ids=[user.dingtalk_user_id for user in grouped_users],
                    text=text,
                )
                sent += len(grouped_users)
                sent_by_direct_robot += len(grouped_users)
                sent_user_ids.extend(user.id for user in grouped_users)
        else:
            for text, grouped_users in grouped_messages.items():
                dry_run_messages.append(_dry_run_message("skipped_no_channel", team, grouped_users, text))
            skipped += len(users)
            skipped_no_channel += len(users)

    if not effective_dry_run and sent_user_ids:
        now = now_in_timezone(settings.timezone)
        result = await session.execute(
            select(DailyReport).where(
                DailyReport.report_date == report_date,
                DailyReport.user_id.in_(sent_user_ids),
                DailyReport.status != "completed",
            )
        )
        for report in result.scalars().all():
            report.last_prompted_at = now

    return {
        "date": report_date.isoformat(),
        "reminder_kind": reminder_kind,
        "dry_run": effective_dry_run,
        "requested_dry_run": requested_dry_run,
        "send_enabled": send_enabled,
        "send_block_reasons": send_block_reasons,
        "missing_count": len(missing_users),
        "test_user_ids_configured": len(test_user_ids),
        "target_users": len(target_users),
        "would_send": would_send,
        "real_sent": sent,
        "skipped_real_users": len(skipped_real_users),
        "skipped_no_channel": skipped_no_channel,
        "sent": sent,
        "sent_by_group_robot": sent_by_group_robot,
        "sent_by_direct_robot": sent_by_direct_robot,
        "sent_by_work_notification": sent_by_work_notification,
        "skipped": skipped,
        "dry_run_messages": dry_run_messages,
    }


def _configured_test_user_ids(settings: Settings) -> set[str]:
    raw = getattr(settings, "reminder_test_user_ids", "") or ""
    if isinstance(raw, str):
        parts = raw.replace("\n", ",").split(",")
    else:
        parts = list(raw)
    return {str(part).strip() for part in parts if str(part).strip()}


def _partition_reminder_users(users: list[User], test_user_ids: set[str]) -> tuple[list[User], list[User]]:
    if not test_user_ids:
        return [], list(users)

    target_users = []
    skipped_users = []
    for user in users:
        ids = {str(getattr(user, "id", "")), str(getattr(user, "dingtalk_user_id", ""))}
        if ids & test_user_ids:
            target_users.append(user)
        else:
            skipped_users.append(user)
    return target_users, skipped_users


def _dry_run_message(channel: str, team, users: list[User], text: str) -> dict[str, Any]:
    return {
        "channel": channel,
        "team": getattr(team, "name", ""),
        "target_count": len(users),
        "user_ids": [user.dingtalk_user_id for user in users],
        "names": [user.name for user in users],
        "text": text,
    }


async def _load_reports_by_user(
    session: AsyncSession,
    report_date: date,
    user_ids: list,
) -> dict:
    if not user_ids:
        return {}
    result = await session.execute(
        select(DailyReport).where(DailyReport.report_date == report_date, DailyReport.user_id.in_(user_ids))
    )
    return {report.user_id: report for report in result.scalars().all()}


def _group_users_by_reminder_text(
    report_date: date,
    users: list[User],
    reports_by_user: dict,
    *,
    reminder_kind: str,
) -> dict[str, list[User]]:
    grouped: dict[str, list[User]] = defaultdict(list)
    for user in users:
        text = build_report_reminder_text(report_date, user, reports_by_user.get(user.id), reminder_kind=reminder_kind)
        grouped[text].append(user)
    return grouped


def build_report_reminder_text(
    report_date: date,
    user: User,
    report: DailyReport | None,
    *,
    reminder_kind: str = "daily",
) -> str:
    name = user.name
    date_text = report_date.isoformat()
    is_catchup = reminder_kind == "catchup"
    period_text = "\u6628\u5929\u7684\u590d\u76d8" if is_catchup else "\u4eca\u5929\u7684\u590d\u76d8"
    if is_catchup and report is None:
        return (
            f"{date_text} \u8865\u4ea4\u63d0\u9192\uff1a{name}\uff0c"
            "\u6628\u5929\u7684\u590d\u76d8\u8fd8\u6ca1\u6709\u6536\u5230\u3002"
            "\u5982\u679c\u65b9\u4fbf\uff0c\u53ef\u4ee5\u73b0\u5728\u8865\u4e00\u53e5\uff0c"
            "\u6211\u4f1a\u5f52\u6863\u5230\u6628\u65e5\u590d\u76d8\u4e2d\u3002"
            "\u4f8b\u5982\uff1a\u6628\u5929\u505a\u4e86\u4ec0\u4e48\u3001\u6709\u6ca1\u6709\u95ee\u9898\u3001\u4eca\u5929\u51c6\u5907\u600e\u4e48\u5b89\u6392\u3002"
        )
    if report is None:
        return (
            f"{date_text} \u590d\u76d8\u63d0\u9192\uff1a{name}\uff0c"
            "\u4eca\u5929\u7684\u590d\u76d8\u8fd8\u6ca1\u6709\u6536\u5230\u3002"
            "\u5982\u679c\u65b9\u4fbf\uff0c\u53ef\u4ee5\u7b80\u5355\u8bf4\u4e00\u53e5\uff1a"
            "\u4eca\u5929\u505a\u4e86\u4ec0\u4e48\u3001\u6709\u6ca1\u6709\u95ee\u9898\u3001\u660e\u5929\u8ba1\u5212\u3002"
            "\u4e0d\u7528\u5199\u5f97\u5f88\u6b63\u5f0f\uff0c\u6211\u4f1a\u5e2e\u4f60\u6574\u7406\u3002"
        )
    if report.status == "pending_confirmation":
        return (
            f"{date_text} \u590d\u76d8\u786e\u8ba4\u63d0\u9192\uff1a{name}\uff0c"
            f"\u6211\u5df2\u7ecf\u5e2e\u4f60\u6574\u7406\u597d{period_text}\uff0c"
            "\u8fd8\u5dee\u6700\u540e\u4e00\u6b65\u786e\u8ba4\u3002"
            "\u5982\u679c\u5185\u5bb9\u6ca1\u95ee\u9898\uff0c\u56de\u590d\u201c\u786e\u8ba4\u201d\u5373\u53ef\uff1b"
            "\u5982\u679c\u9700\u8981\u4fee\u6539\uff0c\u53ef\u4ee5\u76f4\u63a5\u8bf4\u8981\u6539\u54ea\u4e00\u6bb5\u3002"
        )
    missing = _missing_report_fields(report)
    missing_text = "\u3001".join(SECTION_LABELS[field] for field in missing) if missing else "\u672a\u5b8c\u6210\u90e8\u5206"
    reminder_label = "\u8865\u4ea4\u63d0\u9192" if is_catchup else "\u590d\u76d8\u63d0\u9192"
    return (
        f"{date_text} {reminder_label}\uff1a{name}\uff0c"
        f"\u4f60{period_text}\u6211\u5df2\u7ecf\u8bb0\u5f55\u4e86\u4e00\u90e8\u5206\u3002"
        f"\u8fd8\u5dee\u4e00\u70b9\u70b9\uff1a{missing_text}\u3002"
        "\u7b80\u5355\u8bf4\u4e00\u53e5\u5c31\u884c\u3002"
    )


def _missing_report_fields(report: DailyReport) -> list[str]:
    missing: list[str] = []
    if not report.today_work:
        missing.append("today_work")
    problems_done = bool(report.problems) or bool((report.section_status or {}).get("problems_acknowledged_empty"))
    if not problems_done:
        missing.append("problems")
    if not report.tomorrow_plan:
        missing.append("tomorrow_plan")
    return missing


async def auto_submit_due_pending_reports(
    session: AsyncSession,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or now_in_timezone(settings.timezone)
    result = await session.execute(
        select(DailyReport).where(
            DailyReport.status == STATUS_PENDING_CONFIRMATION,
            DailyReport.auto_submit_at.is_not(None),
            DailyReport.auto_submit_at <= now,
        )
    )
    reports = list(result.scalars().all())
    for report in reports:
        mark_report_auto_submitted(report, now)
    return {"auto_submitted": len(reports), "report_ids": [str(report.id) for report in reports]}


def mark_report_auto_submitted(report: DailyReport, now: datetime) -> None:
    report.status = STATUS_COMPLETED
    report.confirmation_type = CONFIRMATION_AUTO_SUBMITTED_TIMEOUT
    report.confirmed_by_user = False
    report.submitted_at = now
    report.auto_submit_at = None
