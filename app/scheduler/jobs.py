from __future__ import annotations

import logging
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
CONFIRMATION_REMINDED_ON_KEY = "_confirmation_reminded_on"
CONFIRMATION_REMINDED_AT_KEY = "_confirmation_reminded_at"
UNRESOLVED_DRAFT_EDIT_KEY = "_unresolved_draft_edit"
logger = logging.getLogger(__name__)


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
    target_users = _filter_reminder_users(target_users, reports_by_user, report_date)

    by_team = defaultdict(list)
    for user in target_users:
        by_team[user.team].append(user)

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
                channel = await send_user_message(robot, [user.dingtalk_user_id for user in grouped_users], text)
                sent += len(grouped_users)
                if channel == "work_notification":
                    sent_by_work_notification += len(grouped_users)
                else:
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
                channel = await send_user_message(robot, [user.dingtalk_user_id for user in grouped_users], text)
                sent += len(grouped_users)
                if channel == "work_notification":
                    sent_by_work_notification += len(grouped_users)
                else:
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
            if report.status == STATUS_PENDING_CONFIRMATION:
                section_status = dict(report.section_status or {})
                section_status[CONFIRMATION_REMINDED_ON_KEY] = report_date.isoformat()
                section_status[CONFIRMATION_REMINDED_AT_KEY] = now.isoformat()
                report.section_status = section_status

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


async def send_user_message(robot: DingTalkRobotClient, user_ids: list[str], text: str, *, markdown: bool = False, title: str = "日报通知") -> str:
    user_ids = [user_id for user_id in user_ids if user_id]
    if not user_ids:
        return "none"
    try:
        if markdown and hasattr(robot, "send_robot_direct_markdown"):
            await robot.send_robot_direct_markdown(user_ids=user_ids, title=title, text=text)
            return "direct_robot_markdown"
        if hasattr(robot, "send_robot_direct_text"):
            await robot.send_robot_direct_text(user_ids=user_ids, text=text)
            return "direct_robot"
        raise AttributeError("robot has no direct send method")
    except Exception as exc:
        logger.warning("direct robot message failed, falling back to work notification: %s", exc)
        if not hasattr(robot, "send_work_notification"):
            raise
        await robot.send_work_notification(user_ids=user_ids, text=text)
        return "work_notification"


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


def _filter_reminder_users(users: list[User], reports_by_user: dict, report_date: date) -> list[User]:
    filtered = []
    for user in users:
        report = reports_by_user.get(user.id)
        status = getattr(report, "status", None)
        if status == STATUS_COMPLETED:
            continue
        if status == STATUS_PENDING_CONFIRMATION and _confirmation_reminded_for_date(report, report_date):
            continue
        filtered.append(user)
    return filtered


def _confirmation_reminded_for_date(report: DailyReport, report_date: date) -> bool:
    section_status = getattr(report, "section_status", None) or {}
    return section_status.get(CONFIRMATION_REMINDED_ON_KEY) == report_date.isoformat()


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


def _build_report_reminder_text_legacy(
    report_date: date,
    user: User,
    report: DailyReport | None,
    *,
    reminder_kind: str = "daily",
) -> str:
    name = user.name
    date_text = report_date.isoformat()
    is_catchup = reminder_kind == "catchup"
    is_second_reminder = reminder_kind == "second"
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
        reminder_label = "\u590d\u76d8\u4e8c\u6b21\u63d0\u9192" if is_second_reminder else "\u590d\u76d8\u63d0\u9192"
        return (
            f"{date_text} {reminder_label}\uff1a{name}\uff0c"
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
    if is_catchup:
        reminder_label = "\u8865\u4ea4\u63d0\u9192"
    elif is_second_reminder:
        reminder_label = "\u590d\u76d8\u4e8c\u6b21\u63d0\u9192"
    else:
        reminder_label = "\u590d\u76d8\u63d0\u9192"
    return (
        f"{date_text} {reminder_label}\uff1a{name}\uff0c"
        f"\u4f60{period_text}\u6211\u5df2\u7ecf\u8bb0\u5f55\u4e86\u4e00\u90e8\u5206\u3002"
        f"\u8fd8\u5dee\u4e00\u70b9\u70b9\uff1a{missing_text}\u3002"
        "\u7b80\u5355\u8bf4\u4e00\u53e5\u5c31\u884c\u3002"
    )


def build_report_reminder_text(
    report_date: date,
    user: User,
    report: DailyReport | None,
    *,
    reminder_kind: str = "daily",
) -> str:
    name = user.name
    is_catchup = reminder_kind == "catchup"
    is_second_reminder = reminder_kind == "second"
    period_text = "昨天的复盘" if is_catchup else "今天的复盘"
    if is_catchup and report is None:
        return (
            f"{name}，早上好。{report_date.isoformat()} 的复盘还没有开始，"
            "我会先在今天的汇总里标记为未开始。方便的话，你可以补充一下："
            "昨天主要做了什么？遇到哪些问题或风险？今天有什么工作计划？"
        )
    if report is None:
        if is_second_reminder:
            return (
                f"{name}，今天的复盘还没开始做。我们先从三件事开始吧："
                "今天主要做了什么？碰到了什么问题或风险？明天有什么工作计划？"
                "你直接按口语说就行，我来帮你整理成日报。"
            )
        return (
            f"{name}，到了今天的复盘时间了。"
            "可以和我说说今天主要做了什么、碰到了什么问题或风险、明天有什么工作计划。"
            "不用写得很正式，你按自己的话说，我来帮你整理。"
        )
    if report.status == STATUS_PENDING_CONFIRMATION:
        if is_second_reminder:
            return (
                f"{name}，今天的复盘我已经整理好了，还差最后确认。"
                "如果内容没问题，可以回复“确认”；如果今晚不再修改，后续我会按当前内容自动确认提交。"
                "需要调整的话，直接告诉我要改哪一段。"
            )
        return (
            f"{name}，我已经帮你整理好{period_text}，还差最后确认。"
            "内容没问题的话回复“确认”即可；需要修改的话，直接告诉我要改哪一段。"
        )
    missing = _missing_report_fields(report)
    missing_text = "、".join(SECTION_LABELS[field] for field in missing) if missing else "未完成部分"
    if is_second_reminder:
        return (
            f"{name}，你的{period_text}还有{missing_text}没有填写。"
            "请方便时补充一下；如果今晚不再补充，后续我会按当前已填写内容自动确认提交。"
        )
    return (
        f"{name}，你{period_text}我已经记录了一部分，还差{missing_text}。"
        "你简单补一句就行，我会继续帮你整理。"
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
    result = await session.execute(select(DailyReport).where(DailyReport.status.in_([STATUS_PENDING_CONFIRMATION, "collecting"])))
    reports = [
        report
        for report in result.scalars().all()
        if _report_has_any_content(report)
        and not _report_has_unresolved_draft_edit(report)
        and (
            getattr(report, "status", None) == "collecting"
            or (
                getattr(report, "status", None) == STATUS_PENDING_CONFIRMATION
                and getattr(report, "auto_submit_at", None) is not None
                and report.auto_submit_at <= now
            )
        )
    ]
    for report in reports:
        mark_report_auto_submitted(report, now)
    return {"auto_submitted": len(reports), "report_ids": [str(report.id) for report in reports]}


def _report_has_any_content(report: DailyReport) -> bool:
    return bool(
        report.today_work
        or report.problems
        or report.tomorrow_plan
        or (report.section_status or {}).get("problems_acknowledged_empty")
    )


def _report_has_unresolved_draft_edit(report: DailyReport) -> bool:
    return bool((report.section_status or {}).get(UNRESOLVED_DRAFT_EDIT_KEY))


def mark_report_auto_submitted(report: DailyReport, now: datetime) -> None:
    report.status = STATUS_COMPLETED
    report.confirmation_type = CONFIRMATION_AUTO_SUBMITTED_TIMEOUT
    report.confirmed_by_user = False
    report.submitted_at = now
    report.auto_submit_at = None
