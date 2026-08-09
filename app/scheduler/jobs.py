from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent2.memory.module import (
    PreferredSalutationValue,
    validate_personal_memory_value,
)
from app.agent2.memory.postgres import PersonalMemoryRecord
from app.config import Settings
from app.models import DailyReport, ReportInteractionEvent, User
from app.repositories import list_missing_users
from app.services.dingtalk import DingTalkDeliveryError, DingTalkRobotClient
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
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReminderDispatchEvidence:
    channel: str
    provider_reference: str
    message_status: str = "delivered"
    delivery_verified: bool = True


class MissingProviderEvidenceError(RuntimeError):
    """The provider call returned but supplied no auditable reference."""


class UnverifiedProviderDeliveryError(RuntimeError):
    """The provider returned a reference without confirming delivery."""


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
    delivery_verified_count = 0
    delivery_pending_count = 0
    delivery_failed_count = 0
    dry_run_messages: list[dict[str, Any]] = []
    sent_user_ids = []
    sent_evidence_by_user: dict[Any, ReminderDispatchEvidence] = {}
    reports_by_user = await _load_reports_by_user(session, report_date, [user.id for user in target_users])
    target_users = _filter_reminder_users(target_users, reports_by_user, report_date)
    now = now_in_timezone(settings.timezone)
    preferred_salutations = await _load_preferred_salutations(
        session,
        [user.id for user in target_users],
        now=now,
    )

    by_team = defaultdict(list)
    for user in target_users:
        by_team[user.team].append(user)

    for team, users in by_team.items():
        webhook = team.dingtalk_webhook_url or settings.dingtalk_default_robot_webhook
        secret = team.dingtalk_webhook_secret or settings.dingtalk_default_robot_secret
        grouped_messages = _group_users_by_reminder_text(
            report_date,
            users,
            reports_by_user,
            reminder_kind=reminder_kind,
            preferred_salutations=preferred_salutations,
        )
        has_direct_robot = robot.has_enterprise_app()
        if test_user_ids and has_direct_robot:
            for text, grouped_users in grouped_messages.items():
                if effective_dry_run:
                    would_send += len(grouped_users)
                    dry_run_messages.append(_dry_run_message("direct_robot", team, grouped_users, text))
                    continue
                for user in grouped_users:
                    try:
                        evidence = await send_user_message(
                            robot,
                            [user.dingtalk_user_id],
                            text,
                        )
                    except Exception:
                        delivery_failed_count += 1
                        logger.exception(
                            "daily report reminder failed recipient=%s kind=%s",
                            user.id,
                            reminder_kind,
                        )
                        continue
                    sent += 1
                    if evidence.delivery_verified:
                        delivery_verified_count += 1
                    else:
                        delivery_pending_count += 1
                    if evidence.channel == "work_notification":
                        sent_by_work_notification += 1
                    else:
                        sent_by_direct_robot += 1
                    sent_user_ids.append(user.id)
                    sent_evidence_by_user[user.id] = evidence
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
                try:
                    await robot.send_text(
                        webhook_url=webhook,
                        secret=secret,
                        text=text,
                        at_user_ids=[
                            user.dingtalk_user_id for user in grouped_users
                        ],
                    )
                except Exception:
                    delivery_failed_count += len(grouped_users)
                    logger.exception(
                        "group daily report reminder failed team=%s kind=%s",
                        getattr(team, "id", ""),
                        reminder_kind,
                    )
                    continue
                sent += len(grouped_users)
                sent_by_group_robot += len(grouped_users)
                sent_user_ids.extend(user.id for user in grouped_users)
                sent_evidence_by_user.update(
                    {
                        user.id: ReminderDispatchEvidence(
                            channel="group_robot",
                            provider_reference="",
                            message_status="accepted_by_provider",
                            delivery_verified=False,
                        )
                        for user in grouped_users
                    }
                )
                delivery_pending_count += len(grouped_users)
        elif has_direct_robot:
            for text, grouped_users in grouped_messages.items():
                if effective_dry_run:
                    would_send += len(grouped_users)
                    dry_run_messages.append(_dry_run_message("direct_robot", team, grouped_users, text))
                    continue
                for user in grouped_users:
                    try:
                        evidence = await send_user_message(
                            robot,
                            [user.dingtalk_user_id],
                            text,
                        )
                    except Exception:
                        delivery_failed_count += 1
                        logger.exception(
                            "daily report reminder failed recipient=%s kind=%s",
                            user.id,
                            reminder_kind,
                        )
                        continue
                    sent += 1
                    if evidence.delivery_verified:
                        delivery_verified_count += 1
                    else:
                        delivery_pending_count += 1
                    if evidence.channel == "work_notification":
                        sent_by_work_notification += 1
                    else:
                        sent_by_direct_robot += 1
                    sent_user_ids.append(user.id)
                    sent_evidence_by_user[user.id] = evidence
        else:
            for text, grouped_users in grouped_messages.items():
                dry_run_messages.append(_dry_run_message("skipped_no_channel", team, grouped_users, text))
            skipped += len(users)
            skipped_no_channel += len(users)

    if not effective_dry_run and sent_user_ids:
        sent_user_id_set = set(sent_user_ids)
        for user in target_users:
            if user.id not in sent_user_id_set:
                continue
            dispatch_evidence = sent_evidence_by_user[user.id]
            report = reports_by_user.get(user.id)
            session.add(
                ReportInteractionEvent(
                    user_id=user.id,
                    report_id=getattr(report, "id", None),
                    dingtalk_user_id=str(getattr(user, "dingtalk_user_id", "") or ""),
                    report_date=report_date,
                    message_text=build_report_reminder_text(
                        report_date,
                        user,
                        report,
                        reminder_kind=reminder_kind,
                        preferred_salutation=preferred_salutations.get(
                            user.id
                        ),
                    ),
                    llm_decision_json={
                        "interaction_type": "daily_report_reminder",
                        "reminder_kind": reminder_kind,
                        "target_report_date": report_date.isoformat(),
                        "business_write": False,
                        "message_status": dispatch_evidence.message_status,
                        "provider_reference": dispatch_evidence.provider_reference,
                        "provider_reference_available": bool(
                            dispatch_evidence.provider_reference
                        ),
                        "provider_message_id_available": False,
                        "transport": dispatch_evidence.channel,
                        "delivery_verified": dispatch_evidence.delivery_verified,
                        "dispatches": [
                            {
                                "transport": dispatch_evidence.channel,
                                "provider_reference": dispatch_evidence.provider_reference,
                                "delivery_verified": dispatch_evidence.delivery_verified,
                            }
                        ],
                    },
                    backend_action=(
                        "daily_report_reminder_sent"
                        if dispatch_evidence.delivery_verified
                        else "daily_report_reminder_delivery_pending"
                    ),
                    before_snapshot_json={},
                    after_snapshot_json={},
                )
            )
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
        "delivery_verified": delivery_verified_count,
        "delivery_pending": delivery_pending_count,
        "delivery_failed": delivery_failed_count,
        "skipped": skipped,
        "dry_run_messages": dry_run_messages,
    }


async def send_user_message(robot: DingTalkRobotClient, user_ids: list[str], text: str, *, markdown: bool = False, title: str = "日报通知") -> ReminderDispatchEvidence:
    user_ids = [user_id for user_id in user_ids if user_id]
    if not user_ids:
        raise ValueError("at least one DingTalk user id is required")
    attempted_channel = "direct_robot_markdown" if markdown else "direct_robot"
    try:
        if markdown and hasattr(robot, "send_robot_direct_markdown_verified"):
            result = await robot.send_robot_direct_markdown_verified(
                user_ids=user_ids,
                title=title,
                text=text,
            )
            return _dispatch_evidence("direct_robot_markdown", result)
        if markdown and hasattr(robot, "send_robot_direct_markdown"):
            result = await robot.send_robot_direct_markdown(
                user_ids=user_ids,
                title=title,
                text=text,
            )
            return _dispatch_evidence("direct_robot_markdown", result)
        if hasattr(robot, "send_robot_direct_text_verified"):
            result = await robot.send_robot_direct_text_verified(
                user_ids=user_ids,
                text=text,
            )
            return _dispatch_evidence("direct_robot", result)
        if hasattr(robot, "send_robot_direct_text"):
            result = await robot.send_robot_direct_text(user_ids=user_ids, text=text)
            return _dispatch_evidence("direct_robot", result)
        raise AttributeError("robot has no direct send method")
    except DingTalkDeliveryError as exc:
        # The first provider call may already have accepted the message. Do not
        # fall back to a second channel and risk a duplicate notification.
        if exc.provider_reference and not exc.terminal_failure:
            return ReminderDispatchEvidence(
                channel=attempted_channel,
                provider_reference=exc.provider_reference,
                message_status="accepted_by_provider",
                delivery_verified=False,
            )
        raise
    except (MissingProviderEvidenceError, UnverifiedProviderDeliveryError):
        raise
    except Exception as exc:
        logger.warning("direct robot message failed, falling back to work notification: %s", exc)
        if not hasattr(robot, "send_work_notification"):
            raise
        result = await robot.send_work_notification(user_ids=user_ids, text=text)
        return _dispatch_evidence("work_notification", result)


def _dispatch_evidence(channel: str, result: Any) -> ReminderDispatchEvidence:
    payload = result if isinstance(result, dict) else {}
    provider_reference = next(
        (
            str(payload.get(key) or "").strip()
            for key in (
                "processQueryKey",
                "task_id",
                "taskId",
                "request_id",
                "requestId",
            )
            if str(payload.get(key) or "").strip()
        ),
        "",
    )
    if not provider_reference:
        raise MissingProviderEvidenceError(
            f"DingTalk {channel} response is missing a provider reference"
        )
    delivery_verified = payload.get("deliveryVerified") is True
    return ReminderDispatchEvidence(
        channel=channel,
        provider_reference=provider_reference,
        message_status=(
            "delivered" if delivery_verified else "accepted_by_provider"
        ),
        delivery_verified=delivery_verified,
    )


async def reconcile_pending_scheduler_deliveries(
    session: AsyncSession,
    robot: DingTalkRobotClient,
    *,
    now: datetime | None = None,
    limit: int = 200,
) -> dict[str, int]:
    """Verify provider-accepted scheduler messages without ever resending them."""

    checked_at = now or datetime.now(UTC)
    rows = list(
        (
            await session.scalars(
                select(ReportInteractionEvent)
                .where(
                    ReportInteractionEvent.backend_action.in_(
                        {
                            "daily_report_reminder_delivery_pending",
                            "daily_briefing_delivery_pending",
                        }
                    )
                )
                .order_by(ReportInteractionEvent.created_at)
                .limit(max(1, limit))
            )
        ).all()
    )
    verified = 0
    still_pending = 0
    failed = 0
    checked = 0
    deferred = 0
    verification_unavailable = False
    for event in rows:
        payload = dict(event.llm_decision_json or {})
        next_check_raw = str(
            payload.get("next_delivery_check_at") or ""
        ).strip()
        if next_check_raw:
            try:
                next_check = datetime.fromisoformat(next_check_raw)
            except ValueError:
                next_check = None
            if next_check is not None and next_check.tzinfo is None:
                next_check = next_check.replace(tzinfo=UTC)
            if next_check is not None and next_check > checked_at:
                deferred += 1
                continue
        if verification_unavailable:
            payload["next_delivery_check_at"] = (
                checked_at + timedelta(hours=6)
            ).isoformat()
            payload["delivery_check_deferred_reason"] = (
                "provider_verification_unavailable"
            )
            event.llm_decision_json = payload
            still_pending += 1
            deferred += 1
            continue
        raw_dispatches = payload.get("dispatches")
        dispatches = (
            [dict(item) for item in raw_dispatches if isinstance(item, dict)]
            if isinstance(raw_dispatches, list)
            else []
        )
        if not dispatches:
            still_pending += 1
            continue
        expected_user_ids = [
            str(event.dingtalk_user_id or "").strip()
        ]
        expected_user_ids = [value for value in expected_user_ids if value]
        all_verified = bool(expected_user_ids)
        terminal_failure = False
        unavailable_for_event = False
        checked += 1
        for dispatch in dispatches:
            provider_reference = str(
                dispatch.get("provider_reference") or ""
            ).strip()
            transport = str(dispatch.get("transport") or "").strip()
            if not provider_reference:
                all_verified = False
                continue
            try:
                if transport == "work_notification":
                    await robot.wait_for_work_notification_delivery(
                        task_id=provider_reference,
                        expected_user_ids=expected_user_ids,
                        attempts=1,
                        interval_seconds=0,
                    )
                else:
                    await robot.wait_for_robot_direct_delivery(
                        process_query_key=provider_reference,
                        expected_user_ids=expected_user_ids,
                        attempts=1,
                        interval_seconds=0,
                    )
            except DingTalkDeliveryError as exc:
                all_verified = False
                terminal_failure = (
                    terminal_failure or exc.terminal_failure
                )
                unavailable_for_event = exc.verification_unavailable
                verification_unavailable = (
                    verification_unavailable
                    or unavailable_for_event
                )
                break
            except Exception:
                all_verified = False
                break
            dispatch["delivery_verified"] = True
        payload["dispatches"] = dispatches
        payload["last_delivery_check_at"] = checked_at.isoformat()
        attempt_count = int(
            payload.get("delivery_check_attempt_count") or 0
        ) + 1
        payload["delivery_check_attempt_count"] = attempt_count
        if terminal_failure:
            event.backend_action = (
                "daily_briefing_failed"
                if payload.get("interaction_type") == "daily_briefing"
                else "daily_report_reminder_failed"
            )
            payload["message_status"] = "failed"
            payload["delivery_verified"] = False
            payload.pop("next_delivery_check_at", None)
            payload.pop("delivery_check_deferred_reason", None)
            failed += 1
        elif all_verified:
            event.backend_action = (
                "daily_briefing_sent"
                if payload.get("interaction_type") == "daily_briefing"
                else "daily_report_reminder_sent"
            )
            payload["message_status"] = "delivered"
            payload["delivery_verified"] = True
            payload["delivery_verified_at"] = checked_at.isoformat()
            payload.pop("next_delivery_check_at", None)
            payload.pop("delivery_check_deferred_reason", None)
            verified += 1
        else:
            delay_minutes = (
                360
                if unavailable_for_event
                else min(5 * (2 ** (attempt_count - 1)), 360)
            )
            payload["next_delivery_check_at"] = (
                checked_at + timedelta(minutes=delay_minutes)
            ).isoformat()
            payload["delivery_check_deferred_reason"] = (
                "provider_verification_unavailable"
                if unavailable_for_event
                else "delivery_not_yet_confirmed"
            )
            still_pending += 1
        event.llm_decision_json = payload
    return {
        "loaded": len(rows),
        "checked": checked,
        "deferred": deferred,
        "verified": verified,
        "still_pending": still_pending,
        "failed": failed,
    }


def _configured_test_user_ids(settings: Settings) -> set[str]:
    raw = getattr(settings, "reminder_test_user_ids", "") or ""
    if isinstance(raw, str):
        parts = raw.replace("\n", ",").split(",")
    else:
        parts = list(raw)
    return {str(part).strip() for part in parts if str(part).strip()}


async def ensure_daily_submission_obligations(
    session: AsyncSession,
    settings: Settings,
    report_date: date,
) -> dict[str, Any]:
    """Persist the server-owned submission scope used by management reads."""

    configured_user_ids = sorted(
        _configured_test_user_ids(settings)
    )
    tenant_id = str(
        getattr(
            settings,
            "legal_daily_dashboard_tenant_id",
            "",
        )
        or ""
    ).strip()
    if not configured_user_ids or not tenant_id:
        return {
            "report_date": report_date.isoformat(),
            "configured_identifiers": len(configured_user_ids),
            "inserted": 0,
            "effective_obligations": 0,
        }
    deadline_at = datetime.combine(
        report_date + timedelta(days=1),
        time(9),
        tzinfo=ZoneInfo(settings.timezone),
    )
    parameters = {
        "tenant_id": tenant_id,
        "report_date": report_date,
        "deadline_at": deadline_at,
        "configured_user_ids": configured_user_ids,
    }
    inserted = (
        await session.execute(
            text(
                """
                INSERT INTO legal_daily_submission_obligations (
                    obligation_id,
                    tenant_id,
                    user_id,
                    team_id,
                    report_date,
                    required,
                    exemption_reason,
                    deadline_at,
                    source,
                    data_complete
                )
                SELECT
                    CAST(
                        MD5(
                            :tenant_id || ':' || users.id::text || ':'
                            || CAST(:report_date AS text)
                        )
                        AS uuid
                    ),
                    :tenant_id,
                    users.id,
                    membership_scope.team_id,
                    :report_date,
                    TRUE,
                    '',
                    :deadline_at,
                    'scheduler_test_allowlist',
                    TRUE
                FROM users
                JOIN LATERAL (
                    SELECT
                        CAST(
                            MIN(memberships.team_id::text)
                            AS uuid
                        ) AS team_id
                    FROM legal_daily_team_memberships memberships
                    WHERE memberships.tenant_id = :tenant_id
                      AND memberships.user_id = users.id
                      AND memberships.effective_from <= :report_date
                      AND (
                          memberships.effective_to IS NULL
                          OR memberships.effective_to >= :report_date
                      )
                    HAVING COUNT(*) = 1
                ) membership_scope ON TRUE
                WHERE users.active IS TRUE
                  AND (
                      users.id::text = ANY(
                          CAST(:configured_user_ids AS text[])
                      )
                      OR users.dingtalk_user_id = ANY(
                          CAST(:configured_user_ids AS text[])
                      )
                  )
                ON CONFLICT (tenant_id, user_id, report_date)
                DO NOTHING
                RETURNING user_id::text
                """
            ),
            parameters,
        )
    ).scalars().all()
    effective_obligations = int(
        (
            await session.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM legal_daily_submission_obligations obligations
                    JOIN users ON users.id = obligations.user_id
                    WHERE obligations.tenant_id = :tenant_id
                      AND obligations.report_date = :report_date
                      AND obligations.data_complete IS TRUE
                      AND (
                          users.id::text = ANY(
                              CAST(:configured_user_ids AS text[])
                          )
                          OR users.dingtalk_user_id = ANY(
                              CAST(:configured_user_ids AS text[])
                          )
                      )
                    """
                ),
                parameters,
            )
        ).scalar_one()
    )
    return {
        "report_date": report_date.isoformat(),
        "configured_identifiers": len(configured_user_ids),
        "inserted": len(inserted),
        "effective_obligations": effective_obligations,
    }


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
        if status in {STATUS_COMPLETED, STATUS_PENDING_CONFIRMATION}:
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


async def _load_preferred_salutations(
    session: AsyncSession,
    user_ids: list,
    *,
    now: datetime,
) -> dict[Any, str]:
    """Return one unambiguous, active preferred salutation per user."""

    if not user_ids or not hasattr(session, "execute"):
        return {}
    result = await session.execute(
        select(
            PersonalMemoryRecord.user_id,
            PersonalMemoryRecord.value_json,
        ).where(
            PersonalMemoryRecord.user_id.in_(user_ids),
            PersonalMemoryRecord.memory_key
            == "response.preferred_salutation",
            PersonalMemoryRecord.status == "active",
            or_(
                PersonalMemoryRecord.expires_at.is_(None),
                PersonalMemoryRecord.expires_at > now,
            ),
        )
    )
    values_by_user: dict[Any, set[str]] = defaultdict(set)
    for user_id, raw_value in result.all():
        try:
            validated = validate_personal_memory_value(
                "response_preference",
                "response.preferred_salutation",
                raw_value,
            )
        except ValueError:
            continue
        if isinstance(validated, PreferredSalutationValue):
            values_by_user[user_id].add(validated.salutation)
    return {
        user_id: next(iter(values))
        for user_id, values in values_by_user.items()
        if len(values) == 1
    }


def _group_users_by_reminder_text(
    report_date: date,
    users: list[User],
    reports_by_user: dict,
    *,
    reminder_kind: str,
    preferred_salutations: dict[Any, str] | None = None,
) -> dict[str, list[User]]:
    grouped: dict[str, list[User]] = defaultdict(list)
    preferred_salutations = preferred_salutations or {}
    for user in users:
        text = build_report_reminder_text(
            report_date,
            user,
            reports_by_user.get(user.id),
            reminder_kind=reminder_kind,
            preferred_salutation=preferred_salutations.get(user.id),
        )
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
            f"{date_text} \u590d\u76d8\u8bb0\u5f55\uff1a{name}\uff0c"
            f"\u6211\u5df2\u7ecf\u5e2e\u4f60\u6574\u7406\u597d{period_text}\uff0c"
            "\u7cfb\u7edf\u4f1a\u6309\u65f6\u81ea\u52a8\u63d0\u4ea4\uff0c\u65e0\u9700\u518d\u786e\u8ba4\u3002"
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
    preferred_salutation: str | None = None,
) -> str:
    name = str(preferred_salutation or "").strip() or user.name
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
        return (
            f"{name}，我已经帮你整理好{period_text}。"
            "系统会按时自动提交，无需再确认；需要调整的话，直接告诉我要改哪一段。"
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
    report_date: date | None = None,
) -> dict[str, Any]:
    now = now or now_in_timezone(settings.timezone)
    query = select(DailyReport).where(
        DailyReport.status.in_(
            [STATUS_PENDING_CONFIRMATION, "collecting"]
        )
    )
    if report_date is not None:
        query = query.where(DailyReport.report_date == report_date)
    result = await session.execute(query)
    reports = [
        report
        for report in result.scalars().all()
        if (
            report_date is None
            or getattr(report, "report_date", None) == report_date
        )
        if _report_has_any_content(report)
    ]
    for report in reports:
        mark_report_auto_submitted(report, now)
    return {
        "report_date": report_date.isoformat() if report_date else None,
        "auto_submitted": len(reports),
        "report_ids": [str(report.id) for report in reports],
    }


def _report_has_any_content(report: DailyReport) -> bool:
    return bool(
        report.today_work
        or report.problems
        or report.tomorrow_plan
        or (report.section_status or {}).get("problems_acknowledged_empty")
    )


def mark_report_auto_submitted(report: DailyReport, now: datetime) -> None:
    report.status = STATUS_COMPLETED
    report.confirmation_type = CONFIRMATION_AUTO_SUBMITTED_TIMEOUT
    report.confirmed_by_user = False
    report.submitted_at = now
    report.auto_submit_at = None
