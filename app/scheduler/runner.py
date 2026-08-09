from __future__ import annotations

import asyncio
import json
import logging
import signal
from datetime import date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from app.agent2.business.models import Agent2IdentityBinding
from app.models import Agent2ConversationState, ReportInteractionEvent
from app.agent2.business.notifications import (
    dispatch_notification_batch,
    reconcile_sent_notification_outcomes,
    recover_stale_notification_claims,
)
from app.agent2.business.travel_pipeline import evaluate_travel_collaboration_candidates
from app.agent2.case_followup_outbox import (
    enqueue_due_case_followup_reminders,
    enqueue_due_case_followups,
    reconcile_case_followup_provider_acceptances,
)
from app.agent2.case_followup_invalidation import expire_due_case_followups
from app.agent2.case_followup_scheduler import plan_due_case_followups
from app.agent2.case_followup_service import CaseFollowupTaskCreator, FollowupCreationContext
from app.agent2.case_followup_sql_store import SqlCaseFollowupTaskStore
from app.config import get_settings
from app.db import AsyncSessionLocal
from app.llm.client import LLMClient
from app.llm.extractor import TeamSummaryGenerator
from app.scheduler.jobs import (
    auto_submit_due_pending_reports,
    ensure_daily_submission_obligations,
    reconcile_pending_scheduler_deliveries,
    remind_missing_reports,
    send_user_message,
)
from app.services.dingtalk import DingTalkRobotClient
from app.services.summary_service import SummaryService
from app.utils.time import today_in_timezone

logger = logging.getLogger(__name__)
DAILY_BRIEFING_SAFE_MESSAGE_CHARS = 3600


def resolve_followup_conversation_id(
    configured: dict[str, str],
    *,
    tenant_id: str,
    user_id: str,
    observed_conversation_ids: tuple[str, ...],
) -> str:
    explicit = str(configured.get(f"{tenant_id}:{user_id}") or "").strip()
    if explicit:
        return explicit
    observed = tuple(dict.fromkeys(
        value.strip() for value in observed_conversation_ids if value.strip()
    ))
    return observed[0] if len(observed) == 1 else ""


def _configured_followup_conversation_map(raw: str) -> dict[str, str]:
    if not str(raw or "").strip():
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        str(key).strip(): str(item).strip()
        for key, item in value.items()
        if str(key).strip() and str(item).strip()
    }


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
            await ensure_daily_submission_obligations(
                session,
                settings,
                current_date,
            )
            await session.commit()
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
            await ensure_daily_submission_obligations(
                session,
                settings,
                current_date,
            )
            await session.commit()
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
            await ensure_daily_submission_obligations(
                session,
                settings,
                current_date,
            )
            await auto_submit_due_pending_reports(
                session,
                settings,
                report_date=current_date,
            )
            await session.commit()

    async def submission_obligation_job() -> None:
        current_date = today_in_timezone(settings.timezone)
        if _scheduler_paused(settings, current_date):
            logger.info(
                "daily submission scope skipped by scheduler pause date=%s",
                current_date.isoformat(),
            )
            return
        if not _reporting_required_on(current_date):
            logger.info(
                "daily submission scope skipped by reporting calendar date=%s",
                current_date.isoformat(),
            )
            return
        async with AsyncSessionLocal() as session:
            await ensure_daily_submission_obligations(
                session,
                settings,
                current_date,
            )
            await session.commit()

    async def scheduler_delivery_reconciliation_job() -> None:
        async with AsyncSessionLocal() as session:
            result = await reconcile_pending_scheduler_deliveries(
                session,
                robot,
            )
            await session.commit()
        if result["checked"]:
            logger.info(
                "scheduler delivery reconciliation checked=%s verified=%s pending=%s failed=%s",
                result["checked"],
                result["verified"],
                result["still_pending"],
                result["failed"],
            )

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
            await ensure_daily_submission_obligations(
                session,
                settings,
                report_date,
            )
            await session.commit()
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
        async with AsyncSessionLocal() as state_session:
            await ensure_daily_submission_obligations(
                state_session,
                settings,
                summary_date,
            )
            await auto_submit_due_pending_reports(
                state_session,
                settings,
                report_date=summary_date,
            )
            await state_session.commit()
        async with AsyncSessionLocal() as delivery_session:
            briefings = await summary_service.build_daily_briefings(
                delivery_session,
                summary_date,
            )
            sent = await _send_daily_briefings(
                robot,
                briefings,
                session=delivery_session,
                report_date=summary_date,
            )
            await delivery_session.commit()
            logger.info("daily briefing sent date=%s sent=%s", summary_date.isoformat(), sent)
        async with AsyncSessionLocal() as summary_session:
            try:
                await summary_service.generate_for_date(
                    summary_session,
                    summary_date,
                )
                await summary_session.commit()
            except Exception:
                logger.exception("daily summary generation failed after briefing send")
                await summary_session.rollback()

    async def agent2_notification_job() -> None:
        tenant_ids = _configured_agent2_business_tenant_ids(settings)
        if not settings.agent2_business_phase2_enabled or not tenant_ids:
            logger.warning("Agent2 notification dispatch skipped: no enabled test-tenant allowlist")
            return
        travel_enabled = bool(
            settings.agent2_travel_notification_worker_enabled
            and settings.agent2_business_travel_enabled
            and settings.agent2_business_travel_write_enabled
        )
        configured_followup_tenants = set(
            _configured_csv_values(settings.agent2_case_followup_tenant_ids)
        )
        followup_tenant_ids = tuple(
            value for value in tenant_ids if value in configured_followup_tenants
        )
        followup_user_ids = _configured_csv_values(settings.agent2_case_followup_user_ids)
        followup_enabled = bool(
            settings.agent2_case_followup_enabled
            and settings.agent2_case_followup_send_enabled
            and settings.agent2_business_case_progress_enabled
            and settings.agent2_business_case_progress_write_enabled
            and followup_tenant_ids
            and followup_user_ids
        )
        lifecycle_tenant_ids = tuple(
            value for value in tenant_ids
            if value in set(_configured_csv_values(settings.case_followup_tenant_allowlist))
        )
        lifecycle_user_ids = _configured_csv_values(settings.case_followup_user_allowlist)
        lifecycle_evaluation_enabled = bool(
            settings.case_followup_enabled
            and lifecycle_tenant_ids
            and lifecycle_user_ids
        )
        lifecycle_send_enabled = bool(
            lifecycle_evaluation_enabled and settings.case_followup_send_enabled
        )
        allowed_message_types = tuple(
            message_type
            for message_type, enabled in (
                ("travel_collaboration_question", travel_enabled),
                ("case_progress_followup", followup_enabled),
                ("case_lifecycle_followup", lifecycle_send_enabled),
            )
            if enabled
        )
        if not allowed_message_types and not lifecycle_evaluation_enabled:
            logger.warning("Agent2 notification dispatch skipped: all domain/effect switches are closed")
            return

        now = datetime.now(ZoneInfo(settings.timezone))
        scanned_intents = matched_groups = created_candidates = notification_rows = 0
        lifecycle_notification_rows = 0
        notification_outcomes_reconciled = 0
        assigned_case_ids: tuple[str, ...] = ()
        lifecycle_plans = ()
        lifecycle_conversations: dict[tuple[str, str], str] = {}
        async with AsyncSessionLocal() as session:
            async with session.begin():
                if travel_enabled:
                    evaluation = await evaluate_travel_collaboration_candidates(
                        session,
                        now=now,
                        allowed_tenant_ids=tenant_ids,
                    )
                    scanned_intents = evaluation.scanned_intents
                    matched_groups = evaluation.matched_groups
                    created_candidates = evaluation.created_candidates
                    notification_rows = evaluation.notification_rows
                if lifecycle_evaluation_enabled:
                    bindings = (
                        await session.scalars(
                            select(Agent2IdentityBinding).where(
                                Agent2IdentityBinding.tenant_id.in_(lifecycle_tenant_ids),
                                Agent2IdentityBinding.user_id.in_(lifecycle_user_ids),
                                Agent2IdentityBinding.active.is_(True),
                            )
                        )
                    ).all()
                    assigned_case_ids = tuple(
                        dict.fromkeys(
                            str(case_id)
                            for binding in bindings
                            for case_id in (
                                (binding.permission_scope_json or {}).get(
                                    "allowed_case_ids", []
                                )
                            )
                            if case_id
                        )
                    )
                    lifecycle_plans = await plan_due_case_followups(
                        session, now=now,
                        allowed_tenant_ids=lifecycle_tenant_ids,
                        allowed_user_ids=lifecycle_user_ids,
                        allowed_case_ids=assigned_case_ids,
                        allowed_trigger_types=_configured_csv_values(
                            settings.case_followup_trigger_allowlist
                        ),
                    )
                    state_user_keys = tuple(
                        f"{tenant_id}:{user_id}"
                        for tenant_id in lifecycle_tenant_ids
                        for user_id in lifecycle_user_ids
                    )
                    state_rows = tuple((await session.scalars(
                        select(Agent2ConversationState).where(
                            Agent2ConversationState.user_key.in_(state_user_keys)
                        )
                    )).all()) if state_user_keys else ()
                    observed: dict[str, list[str]] = {}
                    for row in state_rows:
                        observed.setdefault(row.user_key, []).append(row.conversation_id)
                    configured_conversations = _configured_followup_conversation_map(
                        settings.case_followup_conversation_map_json
                    )
                    for plan in lifecycle_plans:
                        key = (plan.tenant_id, plan.assigned_user_id)
                        lifecycle_conversations[key] = resolve_followup_conversation_id(
                            configured_conversations,
                            tenant_id=plan.tenant_id,
                            user_id=plan.assigned_user_id,
                            observed_conversation_ids=tuple(
                                observed.get(
                                    f"{plan.tenant_id}:{plan.assigned_user_id}", []
                                )
                            ),
                        )
        if lifecycle_plans:
            creator = CaseFollowupTaskCreator(SqlCaseFollowupTaskStore(AsyncSessionLocal))
            for plan in lifecycle_plans:
                conversation_id = lifecycle_conversations.get(
                    (plan.tenant_id, plan.assigned_user_id), ""
                )
                if not conversation_id:
                    logger.warning(
                        "Agent2 lifecycle plan blocked: conversation scope is not unique "
                        "tenant=%s user=%s followup=%s",
                        plan.tenant_id, plan.assigned_user_id, plan.followup_id,
                    )
                    continue
                await creator.create(
                    plan,
                    FollowupCreationContext(
                        tenant_id=plan.tenant_id,
                        user_id=plan.assigned_user_id,
                        conversation_id=conversation_id,
                        source_turn_id=f"scheduler:{plan.followup_id}",
                        now=now,
                    ),
                )
        if not allowed_message_types:
            logger.info(
                "Agent2 lifecycle shadow planned=%s; message effect switch is closed",
                len(lifecycle_plans),
            )
            return
        async with AsyncSessionLocal() as session:
            async with session.begin():
                notification_outcomes_reconciled = (
                    await reconcile_sent_notification_outcomes(
                        session,
                        now=now,
                        allowed_tenant_ids=tenant_ids,
                        allowed_message_types=allowed_message_types,
                        limit=settings.agent2_travel_notification_batch_size,
                    )
                )
                if lifecycle_send_enabled:
                    await reconcile_case_followup_provider_acceptances(
                        session, now=now,
                        allowed_tenant_ids=lifecycle_tenant_ids,
                        allowed_user_ids=lifecycle_user_ids,
                        limit=settings.agent2_travel_notification_batch_size,
                    )
                    await expire_due_case_followups(
                        session, now=now,
                        allowed_tenant_ids=lifecycle_tenant_ids,
                        allowed_user_ids=lifecycle_user_ids,
                        allowed_case_ids=assigned_case_ids,
                    )
                    lifecycle_events = await enqueue_due_case_followups(
                        session, now=now,
                        allowed_tenant_ids=lifecycle_tenant_ids,
                        allowed_user_ids=lifecycle_user_ids,
                        allowed_case_ids=assigned_case_ids,
                        send_enabled=True,
                        limit=settings.agent2_travel_notification_batch_size,
                        user_daily_limit=settings.case_followup_daily_limit,
                        case_daily_limit=settings.case_followup_case_daily_limit,
                    )
                    reminder_events = await enqueue_due_case_followup_reminders(
                        session, now=now,
                        allowed_tenant_ids=lifecycle_tenant_ids,
                        allowed_user_ids=lifecycle_user_ids,
                        allowed_case_ids=assigned_case_ids,
                        send_enabled=True,
                        reminder_interval_hours=(
                            settings.case_followup_reminder_interval_hours
                        ),
                        max_reminders=settings.case_followup_max_reminders,
                        user_daily_limit=settings.case_followup_daily_limit,
                        case_daily_limit=settings.case_followup_case_daily_limit,
                        limit=settings.agent2_travel_notification_batch_size,
                    )
                    lifecycle_notification_rows = len(lifecycle_events) + len(reminder_events)
                recovered = await recover_stale_notification_claims(
                    session, now=now,
                    stale_before=now - timedelta(
                        minutes=settings.agent2_travel_notification_stale_lock_minutes
                    ),
                    allowed_tenant_ids=tenant_ids,
                    allowed_message_types=allowed_message_types,
                )
            summary = await dispatch_notification_batch(
                session,
                robot,
                worker_id="agent2-notification-scheduler",
                now=now,
                limit=settings.agent2_travel_notification_batch_size,
                max_attempts=settings.agent2_travel_notification_max_attempts,
                retry_base_seconds=settings.agent2_travel_notification_retry_base_seconds,
                allowed_tenant_ids=tenant_ids,
                allowed_message_types=allowed_message_types,
                case_followup_tenant_ids=tuple(
                    dict.fromkeys((*followup_tenant_ids, *lifecycle_tenant_ids))
                ),
                case_followup_user_ids=tuple(
                    dict.fromkeys((*followup_user_ids, *lifecycle_user_ids))
                ),
                case_followup_trigger_types=_configured_csv_values(
                    settings.case_followup_trigger_allowlist
                ),
            )
        if scanned_intents or recovered or summary.claimed or notification_outcomes_reconciled:
            logger.info(
                "Agent2 notifications scanned=%s matches=%s new_candidates=%s "
                "notification_rows=%s lifecycle_notification_rows=%s recovered=%s claimed=%s sent=%s failed=%s "
                "dead_letter=%s cancelled=%s outcomes_reconciled=%s message_types=%s",
                scanned_intents,
                matched_groups,
                created_candidates,
                notification_rows,
                lifecycle_notification_rows,
                len(recovered),
                summary.claimed,
                summary.sent,
                summary.failed,
                summary.dead_letter,
                summary.cancelled,
                notification_outcomes_reconciled,
                allowed_message_types,
            )

    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    scheduler.add_job(
        submission_obligation_job,
        CronTrigger(hour=0, minute=5, timezone=settings.timezone),
        id="daily_report_submission_scope",
        replace_existing=True,
        max_instances=1,
    )
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
    scheduler.add_job(
        scheduler_delivery_reconciliation_job,
        IntervalTrigger(minutes=5, timezone=settings.timezone),
        id="daily_report_delivery_reconciliation",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    if settings.agent2_travel_notification_worker_enabled or (
        settings.agent2_case_followup_enabled and settings.agent2_case_followup_send_enabled
    ):
        scheduler.add_job(
            agent2_notification_job,
            IntervalTrigger(
                seconds=settings.agent2_travel_notification_worker_interval_seconds,
                timezone=settings.timezone,
            ),
            id="agent2_travel_notification_dispatch",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    scheduler.start()
    await stop_event.wait()
    scheduler.shutdown(wait=True)
    await robot.close()
    await llm_client.close()


async def _send_daily_briefings(
    robot: DingTalkRobotClient,
    briefings: dict,
    *,
    session=None,
    report_date: date | None = None,
) -> int:
    sent = 0
    messages = [*briefings.get("team_messages", [])]
    if briefings.get("department_message"):
        messages.append(briefings["department_message"])
    for item in messages:
        title = f"{item.get('team_name') or item.get('department_name') or '部门'}晨报"
        message_parts = _split_daily_briefing_text(item.get("text") or "")
        for recipient in item.get("recipients", []):
            dingtalk_user_id = str(
                recipient.get("dingtalk_user_id") or ""
            ).strip()
            if not dingtalk_user_id:
                continue
            dispatch_evidence = []
            delivery_error = ""
            for index, message_part in enumerate(message_parts, start=1):
                part_title = (
                    title
                    if len(message_parts) == 1
                    else f"{title}（{index}/{len(message_parts)}）"
                )
                try:
                    dispatch_evidence.append(
                        await send_user_message(
                            robot,
                            [dingtalk_user_id],
                            message_part,
                            markdown=True,
                            title=part_title,
                        )
                    )
                except Exception as exc:
                    delivery_error = exc.__class__.__name__
                    logger.exception(
                        "daily briefing delivery failed recipient=%s title=%s part=%s/%s",
                        recipient.get("id"),
                        title,
                        index,
                        len(message_parts),
                    )
                    break
            if not delivery_error and len(dispatch_evidence) == len(
                message_parts
            ):
                sent += 1
            if session is not None:
                _record_daily_briefing_events(
                    session,
                    item=item,
                    recipient=recipient,
                    title=title,
                    report_date=_briefing_report_date(
                        briefings,
                        report_date=report_date,
                    ),
                    dispatch_evidence=dispatch_evidence,
                    intended_part_count=len(message_parts),
                    delivery_error=delivery_error,
                )
    return sent


def _briefing_report_date(
    briefings: dict,
    *,
    report_date: date | None,
) -> date:
    if report_date is not None:
        return report_date
    raw_date = str(briefings.get("date") or "").strip()
    if not raw_date:
        raise ValueError("daily briefing report date is required for audit")
    return date.fromisoformat(raw_date)


def _record_daily_briefing_events(
    session,
    *,
    item: dict,
    recipient: dict,
    title: str,
    report_date: date,
    dispatch_evidence: list,
    intended_part_count: int,
    delivery_error: str,
) -> None:
    provider_references = [
        evidence.provider_reference for evidence in dispatch_evidence
    ]
    transports = list(
        dict.fromkeys(evidence.channel for evidence in dispatch_evidence)
    )
    try:
        user_id = UUID(str(recipient.get("id") or ""))
    except (TypeError, ValueError, AttributeError):
        logger.warning(
            "daily briefing audit skipped: invalid recipient user id"
        )
        return
    all_parts_accepted = (
        not delivery_error
        and len(dispatch_evidence) == intended_part_count
    )
    delivery_verified = all_parts_accepted and all(
        evidence.delivery_verified for evidence in dispatch_evidence
    )
    if delivery_error:
        backend_action = "daily_briefing_failed"
        message_status = "failed"
    elif delivery_verified:
        backend_action = "daily_briefing_sent"
        message_status = "delivered"
    else:
        backend_action = "daily_briefing_delivery_pending"
        message_status = "accepted_by_provider"
    session.add(
        ReportInteractionEvent(
            user_id=user_id,
            report_id=None,
            dingtalk_user_id=str(
                recipient.get("dingtalk_user_id") or ""
            ),
            report_date=report_date,
            message_text=str(item.get("text") or ""),
            llm_decision_json={
                "interaction_type": "daily_briefing",
                "scope": str(item.get("scope") or ""),
                "title": title,
                "team_id": str(item.get("team_id") or ""),
                "team_name": str(item.get("team_name") or ""),
                "department_name": str(
                    item.get("department_name") or ""
                ),
                "target_report_date": report_date.isoformat(),
                "business_write": False,
                "briefing_snapshot": dict(
                    item.get("briefing_snapshot") or {}
                ),
                "message_status": message_status,
                "provider_references": provider_references,
                "provider_reference_available": bool(
                    provider_references
                ),
                "transport": transports,
                "part_count": len(dispatch_evidence),
                "intended_part_count": intended_part_count,
                "delivery_verified": delivery_verified,
                "delivery_error": delivery_error,
                "dispatches": [
                    {
                        "transport": evidence.channel,
                        "provider_reference": evidence.provider_reference,
                        "delivery_verified": evidence.delivery_verified,
                    }
                    for evidence in dispatch_evidence
                ],
            },
            backend_action=backend_action,
            before_snapshot_json={},
            after_snapshot_json={},
        )
    )


def _split_daily_briefing_text(
    text: str,
    *,
    max_chars: int = DAILY_BRIEFING_SAFE_MESSAGE_CHARS,
) -> tuple[str, ...]:
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    if len(text) <= max_chars:
        return (text,)

    remaining = text
    parts: list[str] = []
    while len(remaining) > max_chars:
        cut = remaining.rfind("\n\n", 0, max_chars + 1)
        if cut <= 0:
            cut = remaining.rfind("\n", 0, max_chars + 1)
        if cut <= 0:
            cut = max_chars
        parts.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        parts.append(remaining)
    return tuple(parts)


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


def _configured_agent2_business_tenant_ids(settings) -> tuple[str, ...]:
    return _configured_csv_values(getattr(settings, "agent2_business_tenant_ids", ""))


def _configured_csv_values(raw) -> tuple[str, ...]:
    raw = raw or ""
    if not isinstance(raw, str):
        raw = ",".join(str(value) for value in raw)
    return tuple(
        dict.fromkeys(
            value.strip()
            for value in raw.replace("\n", ",").replace(";", ",").split(",")
            if value.strip()
        )
    )


if __name__ == "__main__":
    asyncio.run(run_scheduler())
