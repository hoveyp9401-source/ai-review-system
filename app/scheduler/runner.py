from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import signal
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select, text

from app.agent2.business.models import Agent2IdentityBinding
from app.agent2.business.notifications import (
    dispatch_notification_batch,
    reconcile_sent_notification_outcomes,
    recover_stale_notification_claims,
)
from app.agent2.business.travel_pipeline import evaluate_travel_collaboration_candidates
from app.agent2.case_followup_invalidation import expire_due_case_followups
from app.agent2.case_followup_outbox import (
    enqueue_due_case_followup_reminders,
    enqueue_due_case_followups,
    reconcile_case_followup_provider_acceptances,
)
from app.agent2.case_followup_scheduler import plan_due_case_followups
from app.agent2.case_followup_service import (
    CaseFollowupTaskCreator,
    FollowupCreationContext,
)
from app.agent2.personal_weekly_brief import (
    Agent2PersonalWeeklyBriefGenerator,
    Agent2PersonalWeeklyBriefModelPipeline,
    Agent2PersonalWeeklyBriefReviewer,
    PERSONAL_WEEKLY_BRIEF_CRITICAL_REVIEW_MAX_TOKENS,
    PERSONAL_WEEKLY_BRIEF_CRITICAL_REVIEW_THINKING_ENABLED,
    PERSONAL_WEEKLY_BRIEF_GENERATION_MAX_TOKENS,
    PERSONAL_WEEKLY_BRIEF_GENERATION_THINKING_ENABLED,
    PERSONAL_WEEKLY_BRIEF_MAX_SEMANTIC_ATTEMPTS,
    PERSONAL_WEEKLY_BRIEF_REVIEW_MAX_TOKENS,
    PERSONAL_WEEKLY_BRIEF_REVIEW_THINKING_ENABLED,
    PERSONAL_WEEKLY_BRIEF_REVIEW_VOTES,
    PersonalWeeklyBriefSnapshot,
    derive_personal_weekly_brief_window,
)
from app.agent2.personal_weekly_brief_delivery import (
    DingTalkPersonalWeeklyBriefTransport,
    PersonalWeeklyBriefDispatcher,
    PersonalWeeklyBriefRecipient,
)
from app.agent2.personal_weekly_brief_scope import (
    PersonalWeeklyBriefOverallScopeError,
    PersonalWeeklyBriefTarget,
    load_personal_weekly_brief_targets,
    load_personal_weekly_brief_target_revalidation,
)
from app.agent2.personal_weekly_brief_service import (
    PersonalWeeklyBriefSnapshotService,
)
from app.agent2.personal_weekly_brief_sources import (
    SqlPersonalWeeklyBriefSourceLoader,
)
from app.agent2.personal_weekly_brief_store import (
    PersonalWeeklyBriefRecord,
    SqlPersonalWeeklyBriefStore,
)
from app.agent2.personal_memory_reply import address_with_preferred_salutation
from app.agent2.tool_calling.canary_config import (
    CANARY_MAX_REQUEST_ATTEMPTS,
    CANARY_MODEL_NAME,
    CANARY_THINKING_ENABLED,
    CANARY_TIMEOUT_SECONDS,
)
from app.agent2.tool_calling.outbound_context import (
    record_verified_outbound_context_message,
)
from app.agent2.case_followup_sql_store import SqlCaseFollowupTaskStore
from app.agent2.weekly_plan_collection import derive_weekly_plan_collection_schedule
from app.agent2.weekly_plan_history_pipeline import (
    HistorySuggestionRefreshRequest,
    LLMHistoryFollowUpReviewer,
    WeeklyPlanHistorySuggestionService,
)
from app.agent2.weekly_plan_history_sql_adapter import (
    SqlHistorySuggestionStore,
    SqlTrustedDailyHistorySource,
)
from app.agent2.weekly_plan_models import WeeklyPlanRosterMember
from app.agent2.weekly_plan_reminder_dispatch import (
    DingTalkWeeklyPlanReminderTransport,
    WeeklyPlanReminderDispatcher,
    WeeklyPlanReminderRecipient,
)
from app.agent2.weekly_plan_reminder_outbox import (
    SqlWeeklyPlanReminderOutboxStore,
)
from app.agent2.weekly_plan_sql_collection import (
    SqlWeeklyPlanCollectionOrchestrator,
)
from app.agent2.weekly_plan_store import SqlWeeklyPlanStore
from app.config import Settings, get_settings
from app.db import AsyncSessionLocal
from app.llm.client import LLMClient
from app.llm.extractor import TeamSummaryGenerator
from app.models import Agent2ConversationState, ReportInteractionEvent, User
from app.scheduler.jobs import (
    ReminderDispatchEvidence,
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
_DAILY_BRIEFING_DISPATCH_CONTRACT = "segmented-daily-briefing-v1"
_DAILY_BRIEFING_EVENT_ACTIONS = frozenset(
    {
        "daily_briefing_failed",
        "daily_briefing_delivery_pending",
        "daily_briefing_sent",
    }
)
PERSONAL_WEEKLY_BRIEF_TIMEZONE = "Asia/Shanghai"
PERSONAL_WEEKLY_BRIEF_MODEL_CONCURRENCY = 4
PERSONAL_WEEKLY_BRIEF_SEND_CONCURRENCY = 2
PERSONAL_WEEKLY_BRIEF_CLAIM_TIMEOUT = timedelta(minutes=15)
PERSONAL_WEEKLY_BRIEF_GENERATION_TIMEOUT = timedelta(minutes=15)


class DailyBriefingResumeConflict(RuntimeError):
    """Recorded segments do not match the briefing that would be resumed."""


def _strict_weekly_plan_single_id(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    parts = value.split(",")
    if len(parts) != 1:
        return None
    item = parts[0]
    if (
        item != item.strip()
        or not item.isascii()
        or any(character.isspace() for character in item)
    ):
        return None
    return item


def _strict_weekly_plan_user_ids(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, str) or not value:
        return None
    parts = value.split(",")
    if not 1 <= len(parts) <= 74 or len(parts) != len(set(parts)):
        return None
    if any(
        item != item.strip()
        or not item
        or not item.isascii()
        or any(character.isspace() for character in item)
        for item in parts
    ):
        return None
    return tuple(sorted(parts))


def _strict_weekly_plan_scope(
    settings,
) -> tuple[str, tuple[str, ...]] | None:
    """Return one tenant and at most 74 stable users, with no name matching."""

    tenant_id = _strict_weekly_plan_single_id(
        getattr(settings, "agent2_weekly_plan_tenant_allowlist", "")
    )
    user_ids = _strict_weekly_plan_user_ids(
        getattr(settings, "agent2_weekly_plan_user_allowlist", "")
    )
    return (tenant_id, user_ids) if tenant_id and user_ids else None


def register_weekly_plan_jobs(
    scheduler,
    *,
    settings,
    open_job,
    reminder_job,
    reminder_reconcile_job,
    snapshot_job,
) -> tuple[str, ...]:
    """Register collection jobs and the verified private reminder outbox worker."""

    if (
        getattr(settings, "agent2_weekly_plan_enabled", False) is not True
        or getattr(settings, "agent2_weekly_plan_write_enabled", False) is not True
        or _strict_weekly_plan_scope(settings) is None
    ):
        return ()

    registered: list[str] = []

    def add(identifier: str, func, trigger) -> None:
        scheduler.add_job(
            func,
            trigger,
            id=identifier,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        registered.append(identifier)

    add(
        "agent2_weekly_plan_collection_open",
        open_job,
        CronTrigger(
            day_of_week="fri",
            hour=settings.weekly_plan_collection_open_hour,
            minute=settings.weekly_plan_collection_open_minute,
            timezone=settings.timezone,
        ),
    )
    send_user_ids = (
        _strict_weekly_plan_user_ids(
            getattr(settings, "agent2_weekly_plan_send_user_allowlist", "")
        )
        if getattr(settings, "agent2_weekly_plan_send_enabled", False) is True
        else None
    )
    scope = _strict_weekly_plan_scope(settings)
    if (
        scope is not None
        and send_user_ids is not None
        and set(send_user_ids).issubset(scope[1])
        and (
            settings.weekly_plan_collection_open_hour,
            settings.weekly_plan_collection_open_minute,
        )
        <= (
            settings.weekly_plan_reminder_hour,
            settings.weekly_plan_reminder_minute,
        )
    ):
        add(
            "agent2_weekly_plan_reminder_enqueue",
            reminder_job,
            CronTrigger(
                day_of_week="fri",
                hour=settings.weekly_plan_reminder_hour,
                minute=settings.weekly_plan_reminder_minute,
                timezone=settings.timezone,
            ),
        )
        if reminder_reconcile_job is not None:
            add(
                "agent2_weekly_plan_reminder_reconcile",
                reminder_reconcile_job,
                IntervalTrigger(minutes=5),
            )
    add(
        "agent2_weekly_plan_monday_snapshot",
        snapshot_job,
        CronTrigger(
            day_of_week="mon",
            hour=settings.weekly_plan_snapshot_hour,
            minute=settings.weekly_plan_snapshot_minute,
            timezone=settings.timezone,
        ),
    )
    return tuple(registered)


def _strict_personal_weekly_brief_tenant(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if (
        value != value.strip()
        or not value.isascii()
        or any(character.isspace() for character in value)
        or "," in value
    ):
        return None
    return value


def _personal_weekly_brief_roster_tenant(settings, *, runtime_tenant_id: str) -> str:
    """Keep the formal-roster namespace separate from the Agent2 runtime."""

    return (
        _strict_personal_weekly_brief_tenant(
            getattr(settings, "legal_daily_dashboard_tenant_id", "")
        )
        or runtime_tenant_id
    )


def register_personal_weekly_brief_jobs(
    scheduler,
    *,
    settings,
    generation_job,
    reconciliation_job,
) -> tuple[str, ...]:
    """Register the Saturday Agent2 brief only behind closed-by-default switches."""

    tenant_id = _strict_personal_weekly_brief_tenant(
        getattr(settings, "agent2_personal_weekly_brief_tenant_id", "")
    )
    if (
        getattr(settings, "agent2_personal_weekly_brief_enabled", False) is not True
        or tenant_id is None
    ):
        return ()
    registered: list[str] = []
    scheduler.add_job(
        generation_job,
        CronTrigger(
            day_of_week="sat",
            hour=9,
            minute=0,
            timezone=PERSONAL_WEEKLY_BRIEF_TIMEZONE,
        ),
        id="agent2_personal_weekly_brief_generate",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    registered.append("agent2_personal_weekly_brief_generate")
    scheduler.add_job(
        reconciliation_job,
        IntervalTrigger(
            minutes=5,
            timezone=PERSONAL_WEEKLY_BRIEF_TIMEZONE,
        ),
        id="agent2_personal_weekly_brief_reconcile",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    registered.append("agent2_personal_weekly_brief_reconcile")
    return tuple(registered)


async def run_bounded_personal_weekly_brief_model_batch(
    rows,
    *,
    worker,
    concurrency_limit: int = PERSONAL_WEEKLY_BRIEF_MODEL_CONCURRENCY,
) -> tuple[object, ...]:
    """Run isolated per-owner model work with a fixed concurrency ceiling."""

    if concurrency_limit < 1 or concurrency_limit > 8:
        raise ValueError("personal weekly brief model concurrency is invalid")
    semaphore = asyncio.Semaphore(concurrency_limit)

    async def run_one(row):
        async with semaphore:
            return await worker(row)

    tasks = tuple(asyncio.create_task(run_one(row)) for row in rows)
    try:
        return tuple(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def run_streaming_personal_weekly_brief_batch(
    rows,
    *,
    generation_worker,
    send_worker=None,
) -> tuple[tuple[object, ...], tuple[object, ...]]:
    """Generate with four workers and stream successes to two send workers."""

    if send_worker is None:
        generated = await run_bounded_personal_weekly_brief_model_batch(
            rows,
            worker=generation_worker,
        )
        return generated, ()
    queue: asyncio.Queue[object | None] = asyncio.Queue(maxsize=8)
    send_results: list[object] = []
    fatal_send_errors: list[Exception] = []
    fatal_send_event = asyncio.Event()

    async def generate_and_enqueue(row):
        result = await generation_worker(row)
        if isinstance(result, PersonalWeeklyBriefRecord):
            await queue.put(result)
        return result

    async def sender() -> None:
        while True:
            row = await queue.get()
            try:
                if row is None:
                    return
                if fatal_send_event.is_set():
                    send_results.append(fatal_send_errors[0])
                    continue
                try:
                    send_results.append(await send_worker(row))
                except PersonalWeeklyBriefOverallScopeError as exc:
                    fatal_send_errors.append(exc)
                    fatal_send_event.set()
                    send_results.append(exc)
                except Exception as exc:
                    fatal_send_errors.append(exc)
                    fatal_send_event.set()
                    send_results.append(exc)
            finally:
                queue.task_done()

    sender_tasks = tuple(
        asyncio.create_task(sender())
        for _ in range(PERSONAL_WEEKLY_BRIEF_SEND_CONCURRENCY)
    )
    try:
        generated = await run_bounded_personal_weekly_brief_model_batch(
            rows,
            worker=generate_and_enqueue,
        )
    except BaseException:
        for task in sender_tasks:
            task.cancel()
        await asyncio.gather(*sender_tasks, return_exceptions=True)
        raise
    await queue.join()
    for _ in sender_tasks:
        await queue.put(None)
    await asyncio.gather(*sender_tasks)
    if fatal_send_errors:
        raise fatal_send_errors[0]
    return generated, tuple(send_results)


async def run_bounded_personal_weekly_brief_send_batch(rows, *, worker):
    """Send with two workers; any unexpected/system error stops the batch."""

    semaphore = asyncio.Semaphore(PERSONAL_WEEKLY_BRIEF_SEND_CONCURRENCY)

    async def run_one(row):
        async with semaphore:
            return await worker(row)

    tasks = tuple(asyncio.create_task(run_one(row)) for row in rows)
    try:
        return tuple(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def stage_personal_weekly_brief_target_batch(
    *,
    session,
    targets,
    snapshot_service,
    week_start: date,
    snapshot_at: datetime,
) -> tuple[PersonalWeeklyBriefRecord, ...]:
    """Freeze each owner behind a savepoint so one bad source cannot cancel 73."""

    rows: list[PersonalWeeklyBriefRecord] = []
    for target in targets:
        try:
            async with session.begin_nested():
                row = await snapshot_service.stage_target(
                    target=target,
                    week_start=week_start,
                    snapshot_at=snapshot_at,
                )
        except ValueError as exc:
            async with session.begin_nested():
                row = await snapshot_service.stage_failure(
                    target=target,
                    week_start=week_start,
                    snapshot_at=snapshot_at,
                    error_code=f"snapshot_error:{type(exc).__name__}",
                )
        rows.append(row)
    return tuple(rows)


async def _generate_personal_weekly_brief_record(
    *,
    row: PersonalWeeklyBriefRecord,
    target: PersonalWeeklyBriefTarget,
    tenant_id: str,
    model_pipeline: Agent2PersonalWeeklyBriefModelPipeline,
    generator: Agent2PersonalWeeklyBriefGenerator,
):
    loop = asyncio.get_running_loop()
    owner_started = loop.time()
    async with AsyncSessionLocal() as start_session:
        row = await SqlPersonalWeeklyBriefStore(
            start_session
        ).record_generation_started(
            tenant_id=tenant_id,
            brief_id=row.brief_id,
            changed_at=datetime.now(
                ZoneInfo(PERSONAL_WEEKLY_BRIEF_TIMEZONE)
            ),
        )
        await start_session.commit()
    try:
        snapshot = PersonalWeeklyBriefSnapshot.from_payload(row.source_snapshot)
        if snapshot.fingerprint != row.source_fingerprint:
            raise ValueError("personal weekly brief source fingerprint mismatch")
        model_outcome = await model_pipeline.generate_and_review(
            snapshot=snapshot,
            recipient_name=target.display_name,
            personal_memory=dict(row.personal_memory_json or {}),
        )
        content = model_outcome.content
        salutation = str(
            (row.personal_memory_json or {}).get(
                "server_preferred_salutation",
                "",
            )
            or target.display_name
        ).strip()
        message_text = address_with_preferred_salutation(
            content=content.message_text,
            salutation=salutation,
            authenticated_display_name=target.display_name,
        )
    except (
        ValueError,
        TimeoutError,
        OSError,
        RuntimeError,
        httpx.HTTPError,
    ) as exc:
        logger.exception(
            "personal weekly brief generation failed owner=%s week=%s",
            row.owner_user_id,
            row.week_start,
        )
        async with AsyncSessionLocal() as failure_session:
            try:
                await SqlPersonalWeeklyBriefStore(
                    failure_session
                ).record_generation_failure(
                    tenant_id=tenant_id,
                    brief_id=row.brief_id,
                    error=f"generation_error:{type(exc).__name__}",
                    changed_at=datetime.now(
                        ZoneInfo(PERSONAL_WEEKLY_BRIEF_TIMEZONE)
                    ),
                )
                await failure_session.commit()
            except ValueError:
                await failure_session.rollback()
        return False
    async with AsyncSessionLocal() as write_session:
        generated_row = await SqlPersonalWeeklyBriefStore(
            write_session
        ).record_generation(
            tenant_id=tenant_id,
            brief_id=row.brief_id,
            source_fingerprint=row.source_fingerprint,
            content_json={
                **content.as_payload(),
                "trace": content.trace_payload(),
                "model_review": model_outcome.review,
                "model_metrics": {
                    "model_calls": model_outcome.model_calls,
                    "semantic_attempts": model_outcome.semantic_attempts,
                    "generation_seconds": [
                        round(value, 3)
                        for value in model_outcome.generation_seconds
                    ],
                    "review_seconds": [
                        round(value, 3)
                        for value in model_outcome.review_seconds
                    ],
                    "model_pipeline_seconds": round(
                        model_outcome.total_seconds, 3
                    ),
                    "total_seconds": round(loop.time() - owner_started, 3),
                    "concurrency_limit": PERSONAL_WEEKLY_BRIEF_MODEL_CONCURRENCY,
                    "timeout_seconds_per_attempt": CANARY_TIMEOUT_SECONDS,
                    "max_attempts_per_call": CANARY_MAX_REQUEST_ATTEMPTS,
                },
            },
            message_text=message_text,
            llm_model=generator.model,
            changed_at=datetime.now(
                ZoneInfo(PERSONAL_WEEKLY_BRIEF_TIMEZONE)
            ),
        )
        await write_session.commit()
    return generated_row


async def run_personal_weekly_brief_generation_job(
    settings,
    *,
    llm_client: LLMClient,
    robot: DingTalkRobotClient,
    now: datetime,
) -> dict[str, int]:
    """Freeze 74 owner snapshots, then let Agent2 generate each private brief."""

    tenant_id = _strict_personal_weekly_brief_tenant(
        getattr(settings, "agent2_personal_weekly_brief_tenant_id", "")
    )
    if (
        getattr(settings, "agent2_personal_weekly_brief_enabled", False) is not True
        or tenant_id is None
    ):
        return {
            "staged": 0,
            "snapshot_failed": 0,
            "generated": 0,
            "generation_failed": 0,
            "dispatched": 0,
        }
    local_now = _personal_weekly_local_now(now)
    if local_now.weekday() != 5:
        raise ValueError("personal_weekly_brief_generation_requires_saturday")
    window = derive_personal_weekly_brief_window(
        local_now,
        timezone_name=PERSONAL_WEEKLY_BRIEF_TIMEZONE,
    )
    roster_tenant_id = _personal_weekly_brief_roster_tenant(
        settings,
        runtime_tenant_id=tenant_id,
    )

    # Establish one repeatable database view before reading any of the 74
    # owners.  Model calls happen only after these source snapshots commit.
    async with AsyncSessionLocal() as snapshot_session:
        await snapshot_session.execute(
            text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        )
        targets = await load_personal_weekly_brief_targets(
            snapshot_session,
            tenant_id=tenant_id,
            roster_tenant_id=roster_tenant_id,
            on_date=local_now.date(),
            expected_model_name=CANARY_MODEL_NAME,
        )
        source_loader = SqlPersonalWeeklyBriefSourceLoader(snapshot_session)
        snapshot_service = PersonalWeeklyBriefSnapshotService(
            store=SqlPersonalWeeklyBriefStore(snapshot_session),
            source_loader=source_loader,
        )
        snapshot_rows = await stage_personal_weekly_brief_target_batch(
            session=snapshot_session,
            targets=targets,
            snapshot_service=snapshot_service,
            week_start=window.week_start,
            snapshot_at=window.snapshot_at,
        )
        staged = sum(row.status == "snapshot_ready" for row in snapshot_rows)
        snapshot_failed = sum(
            row.status == "generation_failed" for row in snapshot_rows
        )
        await snapshot_session.commit()

    target_by_user = {target.internal_user_id: target for target in targets}
    async with AsyncSessionLocal() as read_session:
        ready = await SqlPersonalWeeklyBriefStore(read_session).load_for_status(
            tenant_id=tenant_id,
            status="snapshot_ready",
            week_start=window.week_start,
            limit=100,
        )

    generator = Agent2PersonalWeeklyBriefGenerator(
        llm_client,
        model=CANARY_MODEL_NAME,
        thinking_enabled=PERSONAL_WEEKLY_BRIEF_GENERATION_THINKING_ENABLED,
        timeout_seconds=CANARY_TIMEOUT_SECONDS,
        max_retries=CANARY_MAX_REQUEST_ATTEMPTS - 1,
        max_tokens=PERSONAL_WEEKLY_BRIEF_GENERATION_MAX_TOKENS,
    )
    reviewer = Agent2PersonalWeeklyBriefReviewer(
        llm_client,
        model=CANARY_MODEL_NAME,
        thinking_enabled=PERSONAL_WEEKLY_BRIEF_REVIEW_THINKING_ENABLED,
        timeout_seconds=CANARY_TIMEOUT_SECONDS,
        max_retries=CANARY_MAX_REQUEST_ATTEMPTS - 1,
        max_tokens=PERSONAL_WEEKLY_BRIEF_REVIEW_MAX_TOKENS,
    )
    critical_reviewer = Agent2PersonalWeeklyBriefReviewer(
        llm_client,
        model=CANARY_MODEL_NAME,
        thinking_enabled=PERSONAL_WEEKLY_BRIEF_CRITICAL_REVIEW_THINKING_ENABLED,
        timeout_seconds=CANARY_TIMEOUT_SECONDS,
        max_retries=CANARY_MAX_REQUEST_ATTEMPTS - 1,
        max_tokens=PERSONAL_WEEKLY_BRIEF_CRITICAL_REVIEW_MAX_TOKENS,
        review_mode="critical_facts",
    )
    model_pipeline = Agent2PersonalWeeklyBriefModelPipeline(
        generator=generator,
        reviewer=reviewer,
        critical_reviewer=critical_reviewer,
        max_semantic_attempts=PERSONAL_WEEKLY_BRIEF_MAX_SEMANTIC_ATTEMPTS,
        review_votes=PERSONAL_WEEKLY_BRIEF_REVIEW_VOTES,
    )

    async def generate_one(row):
        target = target_by_user.get(row.owner_user_id)
        if target is None:
            raise RuntimeError("personal weekly brief staged owner left exact scope")
        return await _generate_personal_weekly_brief_record(
            row=row,
            target=target,
            tenant_id=tenant_id,
            model_pipeline=model_pipeline,
            generator=generator,
        )

    async def send_one(row):
        return await _dispatch_personal_weekly_brief_record(
            settings,
            robot=robot,
            row=row,
            frozen_targets=targets,
            now=datetime.now(ZoneInfo(PERSONAL_WEEKLY_BRIEF_TIMEZONE)),
        )

    batch_results, send_results = await run_streaming_personal_weekly_brief_batch(
        ready,
        generation_worker=generate_one,
        send_worker=(
            send_one
            if getattr(
                settings,
                "agent2_personal_weekly_brief_send_enabled",
                False,
            )
            is True
            else None
        ),
    )
    generated = sum(
        isinstance(result, PersonalWeeklyBriefRecord) for result in batch_results
    )
    generation_failed = len(batch_results) - generated
    dispatched = sum(
        isinstance(result, PersonalWeeklyBriefRecord)
        and result.status == "delivered"
        for result in send_results
    )
    return {
        "staged": staged,
        "snapshot_failed": snapshot_failed,
        "generated": generated,
        "generation_failed": generation_failed,
        "dispatched": dispatched,
    }


def _frozen_personal_weekly_brief_targets(
    rows: tuple[PersonalWeeklyBriefRecord, ...],
) -> tuple[PersonalWeeklyBriefTarget, ...]:
    targets: list[PersonalWeeklyBriefTarget] = []
    for row in rows:
        raw = (row.source_snapshot or {}).get("recipient_snapshot")
        if not isinstance(raw, dict):
            raise RuntimeError("personal weekly brief frozen recipient is missing")
        target = PersonalWeeklyBriefTarget(
            tenant_id=str(raw.get("tenant_id") or ""),
            internal_user_id=str(raw.get("internal_user_id") or ""),
            dingtalk_user_id=str(raw.get("dingtalk_user_id") or ""),
            display_name=str(raw.get("display_name") or ""),
            conversation_id=str(raw.get("conversation_id") or ""),
        )
        if (
            target.tenant_id != row.tenant_id
            or target.internal_user_id != row.owner_user_id
            or target.conversation_id != row.conversation_id
        ):
            raise RuntimeError("personal weekly brief frozen recipient changed")
        targets.append(target)
    if len(targets) != 74 or len({target.internal_user_id for target in targets}) != 74:
        raise RuntimeError("personal weekly brief frozen recipient set is not exactly 74")
    return tuple(targets)


async def _dispatch_personal_weekly_brief_record(
    settings,
    *,
    robot: DingTalkRobotClient,
    row: PersonalWeeklyBriefRecord,
    frozen_targets: tuple[PersonalWeeklyBriefTarget, ...],
    now: datetime,
) -> PersonalWeeklyBriefRecord:
    tenant_id = row.tenant_id
    local_now = _personal_weekly_local_now(now)
    roster_tenant_id = _personal_weekly_brief_roster_tenant(
        settings,
        runtime_tenant_id=tenant_id,
    )
    async with AsyncSessionLocal() as scope_session:
        revalidation = await load_personal_weekly_brief_target_revalidation(
            scope_session,
            tenant_id=tenant_id,
            roster_tenant_id=roster_tenant_id,
            on_date=local_now.date(),
            expected_model_name=CANARY_MODEL_NAME,
            frozen_targets=frozen_targets,
        )
    reason = revalidation.blocked_reasons.get(row.owner_user_id)
    if reason:
        blocked_at = _personal_weekly_observed_now()
        async with AsyncSessionLocal() as blocked_session:
            blocked = await SqlPersonalWeeklyBriefStore(
                blocked_session
            ).record_pre_send_block(
                tenant_id=tenant_id,
                brief_id=row.brief_id,
                reason=reason,
                changed_at=blocked_at,
            )
            await blocked_session.commit()
        return blocked
    target = revalidation.valid_targets.get(row.owner_user_id)
    if target is None:
        raise RuntimeError("personal weekly brief latest recipient is missing")
    if not _personal_weekly_brief_send_switch_enabled():
        return row
    send_started_at = _personal_weekly_observed_now()
    async with AsyncSessionLocal() as delivery_session:
        delivered = await PersonalWeeklyBriefDispatcher(
            store=SqlPersonalWeeklyBriefStore(delivery_session),
            transport=DingTalkPersonalWeeklyBriefTransport(robot),
            tenant_id=tenant_id,
            allowed_user_ids=frozenset({row.owner_user_id}),
            clock=lambda: datetime.now(
                ZoneInfo(PERSONAL_WEEKLY_BRIEF_TIMEZONE)
            ),
        ).dispatch(
            row=row,
            recipient=_personal_weekly_recipient(target),
            changed_at=send_started_at,
            claim_token=(
                f"personal-weekly:{row.brief_id}:{send_started_at.isoformat()}"
            ),
        )
        await delivery_session.commit()
    if delivered.status == "delivered":
        await _record_personal_weekly_brief_context(
            tenant_id=tenant_id,
            roster_tenant_id=roster_tenant_id,
            row=delivered,
            frozen_targets=frozen_targets,
        )
    return delivered


async def run_personal_weekly_brief_dispatch_job(
    settings,
    *,
    robot: DingTalkRobotClient,
    now: datetime,
    targets: tuple[PersonalWeeklyBriefTarget, ...] | None = None,
    week_start: date | None = None,
) -> int:
    tenant_id = _strict_personal_weekly_brief_tenant(
        getattr(settings, "agent2_personal_weekly_brief_tenant_id", "")
    )
    if (
        getattr(settings, "agent2_personal_weekly_brief_enabled", False) is not True
        or getattr(settings, "agent2_personal_weekly_brief_send_enabled", False) is not True
        or tenant_id is None
    ):
        return 0
    local_now = _personal_weekly_local_now(now)
    roster_tenant_id = _personal_weekly_brief_roster_tenant(
        settings,
        runtime_tenant_id=tenant_id,
    )
    target_week_start = week_start or derive_personal_weekly_brief_window(
        local_now,
        timezone_name=PERSONAL_WEEKLY_BRIEF_TIMEZONE,
    ).week_start
    async with AsyncSessionLocal() as read_session:
        week_rows = await SqlPersonalWeeklyBriefStore(read_session).load_for_week(
            tenant_id=tenant_id,
            week_start=target_week_start,
            limit=100,
        )
        rows = await SqlPersonalWeeklyBriefStore(read_session).load_for_status(
            tenant_id=tenant_id,
            status="generated",
            week_start=target_week_start,
            limit=100,
        )
    frozen_targets = targets or _frozen_personal_weekly_brief_targets(week_rows)
    async with AsyncSessionLocal() as preflight_session:
        await load_personal_weekly_brief_target_revalidation(
            preflight_session,
            tenant_id=tenant_id,
            roster_tenant_id=roster_tenant_id,
            on_date=local_now.date(),
            expected_model_name=CANARY_MODEL_NAME,
            frozen_targets=frozen_targets,
        )

    async def send_row(row):
        return await _dispatch_personal_weekly_brief_record(
            settings,
            robot=robot,
            row=row,
            frozen_targets=frozen_targets,
            now=datetime.now(ZoneInfo(PERSONAL_WEEKLY_BRIEF_TIMEZONE)),
        )

    results = await run_bounded_personal_weekly_brief_send_batch(
        rows,
        worker=send_row,
    )
    return sum(
        isinstance(result, PersonalWeeklyBriefRecord)
        and result.status == "delivered"
        for result in results
    )


async def run_personal_weekly_brief_reconcile_job(
    settings,
    *,
    llm_client: LLMClient,
    robot: DingTalkRobotClient,
    now: datetime,
) -> int:
    tenant_id = _strict_personal_weekly_brief_tenant(
        getattr(settings, "agent2_personal_weekly_brief_tenant_id", "")
    )
    if (
        getattr(settings, "agent2_personal_weekly_brief_enabled", False) is not True
        or tenant_id is None
    ):
        return 0
    local_now = _personal_weekly_local_now(now)
    roster_tenant_id = _personal_weekly_brief_roster_tenant(
        settings,
        runtime_tenant_id=tenant_id,
    )
    send_enabled = bool(
        getattr(settings, "agent2_personal_weekly_brief_send_enabled", False)
    )
    async with AsyncSessionLocal() as recovery_session:
        recovery = await recover_personal_weekly_brief_rows(
            store=SqlPersonalWeeklyBriefStore(recovery_session),
            tenant_id=tenant_id,
            now=local_now,
            send_enabled=send_enabled,
        )
        await recovery_session.commit()
    requeued_generation = list(recovery["generation_requeued"])

    generator = Agent2PersonalWeeklyBriefGenerator(
        llm_client,
        model=CANARY_MODEL_NAME,
        thinking_enabled=PERSONAL_WEEKLY_BRIEF_GENERATION_THINKING_ENABLED,
        timeout_seconds=CANARY_TIMEOUT_SECONDS,
        max_retries=CANARY_MAX_REQUEST_ATTEMPTS - 1,
        max_tokens=PERSONAL_WEEKLY_BRIEF_GENERATION_MAX_TOKENS,
    )
    model_pipeline = Agent2PersonalWeeklyBriefModelPipeline(
        generator=generator,
        reviewer=Agent2PersonalWeeklyBriefReviewer(
            llm_client,
            model=CANARY_MODEL_NAME,
            thinking_enabled=PERSONAL_WEEKLY_BRIEF_REVIEW_THINKING_ENABLED,
            timeout_seconds=CANARY_TIMEOUT_SECONDS,
            max_retries=CANARY_MAX_REQUEST_ATTEMPTS - 1,
            max_tokens=PERSONAL_WEEKLY_BRIEF_REVIEW_MAX_TOKENS,
        ),
        critical_reviewer=Agent2PersonalWeeklyBriefReviewer(
            llm_client,
            model=CANARY_MODEL_NAME,
            thinking_enabled=PERSONAL_WEEKLY_BRIEF_CRITICAL_REVIEW_THINKING_ENABLED,
            timeout_seconds=CANARY_TIMEOUT_SECONDS,
            max_retries=CANARY_MAX_REQUEST_ATTEMPTS - 1,
            max_tokens=PERSONAL_WEEKLY_BRIEF_CRITICAL_REVIEW_MAX_TOKENS,
            review_mode="critical_facts",
        ),
        max_semantic_attempts=PERSONAL_WEEKLY_BRIEF_MAX_SEMANTIC_ATTEMPTS,
        review_votes=PERSONAL_WEEKLY_BRIEF_REVIEW_VOTES,
    )
    delivered_count = 0
    for week_start in sorted({row.week_start for row in requeued_generation}):
        week_ready = tuple(
            row for row in requeued_generation if row.week_start == week_start
        )
        async with AsyncSessionLocal() as week_session:
            week_rows = await SqlPersonalWeeklyBriefStore(
                week_session
            ).load_for_week(
                tenant_id=tenant_id,
                week_start=week_start,
                limit=100,
            )
        frozen_targets = _frozen_personal_weekly_brief_targets(week_rows)
        target_by_user = {
            target.internal_user_id: target for target in frozen_targets
        }

        async def regenerate(row):
            return await _generate_personal_weekly_brief_record(
                row=row,
                target=target_by_user[row.owner_user_id],
                tenant_id=tenant_id,
                model_pipeline=model_pipeline,
                generator=generator,
            )

        async def send_recovered(row):
            return await _dispatch_personal_weekly_brief_record(
                settings,
                robot=robot,
                row=row,
                frozen_targets=frozen_targets,
                now=datetime.now(ZoneInfo(PERSONAL_WEEKLY_BRIEF_TIMEZONE)),
            )

        _, recovered_sends = await run_streaming_personal_weekly_brief_batch(
            week_ready,
            generation_worker=regenerate,
            send_worker=send_recovered if send_enabled else None,
        )
        delivered_count += sum(
            isinstance(result, PersonalWeeklyBriefRecord)
            and result.status == "delivered"
            for result in recovered_sends
        )

    async with AsyncSessionLocal() as read_session:
        store = SqlPersonalWeeklyBriefStore(read_session)
        generated = await store.load_for_status(
            tenant_id=tenant_id,
            status="generated",
            limit=100,
        )
        pending = await store.load_for_status(
            tenant_id=tenant_id,
            status="delivery_pending",
            limit=100,
        )
        context_pending = await store.load_delivered_without_context(
            tenant_id=tenant_id,
            limit=100,
        )

    if send_enabled:
        for week_start in sorted({row.week_start for row in generated}):
            delivered_count += await run_personal_weekly_brief_dispatch_job(
                settings,
                robot=robot,
                now=local_now,
                week_start=week_start,
            )

    frozen_cache: dict[date, tuple[PersonalWeeklyBriefTarget, ...]] = {}
    async def frozen_for(row):
        if row.week_start not in frozen_cache:
            async with AsyncSessionLocal() as week_session:
                week_rows = await SqlPersonalWeeklyBriefStore(
                    week_session
                ).load_for_week(
                    tenant_id=tenant_id,
                    week_start=row.week_start,
                    limit=100,
                )
            frozen_cache[row.week_start] = _frozen_personal_weekly_brief_targets(
                week_rows
            )
        return frozen_cache[row.week_start]

    for row in pending:
        frozen_targets = await frozen_for(row)
        target = next(
            target
            for target in frozen_targets
            if target.internal_user_id == row.owner_user_id
        )
        reconciliation_started_at = _personal_weekly_observed_now()
        async with AsyncSessionLocal() as delivery_session:
            delivered = await PersonalWeeklyBriefDispatcher(
                store=SqlPersonalWeeklyBriefStore(delivery_session),
                transport=DingTalkPersonalWeeklyBriefTransport(robot),
                tenant_id=tenant_id,
                allowed_user_ids=frozenset({row.owner_user_id}),
                clock=lambda: datetime.now(
                    ZoneInfo(PERSONAL_WEEKLY_BRIEF_TIMEZONE)
                ),
            ).reconcile_pending(
                row=row,
                recipient=_personal_weekly_recipient(target),
                changed_at=reconciliation_started_at,
            )
            await delivery_session.commit()
        if delivered.status == "delivered":
            delivered_count += 1
            await _record_personal_weekly_brief_context(
                tenant_id=tenant_id,
                roster_tenant_id=roster_tenant_id,
                row=delivered,
                frozen_targets=frozen_targets,
            )
    for row in context_pending:
        frozen_targets = await frozen_for(row)
        await _record_personal_weekly_brief_context(
            tenant_id=tenant_id,
            roster_tenant_id=roster_tenant_id,
            row=row,
            frozen_targets=frozen_targets,
        )
    return delivered_count


async def recover_personal_weekly_brief_rows(
    *,
    store,
    tenant_id: str,
    now: datetime,
    send_enabled: bool,
) -> dict[str, tuple[PersonalWeeklyBriefRecord, ...]]:
    stale_generation_failed = await store.fail_stale_generations(
        tenant_id=tenant_id,
        stale_before=now - PERSONAL_WEEKLY_BRIEF_GENERATION_TIMEOUT,
        changed_at=now,
        limit=100,
    )
    stale_failed = await store.fail_stale_claims(
        tenant_id=tenant_id,
        stale_before=now - PERSONAL_WEEKLY_BRIEF_CLAIM_TIMEOUT,
        changed_at=now,
        limit=100,
    )
    generation_failed = await store.load_for_status(
        tenant_id=tenant_id,
        status="generation_failed",
        limit=100,
    )
    failed = await store.load_for_status(
        tenant_id=tenant_id,
        status="failed",
        limit=100,
    )
    generation_requeued: list[PersonalWeeklyBriefRecord] = []
    delivery_requeued: list[PersonalWeeklyBriefRecord] = []
    for row in generation_failed:
        try:
            generation_requeued.append(
                await store.requeue_generation_failure(
                    tenant_id=tenant_id,
                    brief_id=row.brief_id,
                    changed_at=now,
                )
            )
        except ValueError:
            continue
    if send_enabled:
        for row in failed:
            try:
                delivery_requeued.append(
                    await store.requeue_recoverable_failure(
                        tenant_id=tenant_id,
                        brief_id=row.brief_id,
                        changed_at=now,
                    )
                )
            except ValueError:
                continue
    return {
        "generation_requeued": tuple(generation_requeued),
        "delivery_requeued": tuple(delivery_requeued),
        "stale_claims_failed": tuple(stale_failed),
        "stale_generations_failed": tuple(stale_generation_failed),
    }


def _personal_weekly_recipient(
    target: PersonalWeeklyBriefTarget,
) -> PersonalWeeklyBriefRecipient:
    return PersonalWeeklyBriefRecipient(
        tenant_id=target.tenant_id,
        internal_user_id=target.internal_user_id,
        dingtalk_user_id=target.dingtalk_user_id,
        conversation_id=target.conversation_id,
    )


def _personal_weekly_local_now(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("personal weekly brief time must be timezone-aware")
    return value.astimezone(ZoneInfo(PERSONAL_WEEKLY_BRIEF_TIMEZONE))


def _personal_weekly_observed_now() -> datetime:
    return datetime.now(ZoneInfo(PERSONAL_WEEKLY_BRIEF_TIMEZONE))


def _personal_weekly_brief_send_switch_enabled() -> bool:
    """Reload the operator kill switch for every actual private send."""

    return Settings().agent2_personal_weekly_brief_send_enabled is True


async def _record_personal_weekly_brief_context(
    *,
    tenant_id: str,
    roster_tenant_id: str | None = None,
    row: PersonalWeeklyBriefRecord,
    frozen_targets: tuple[PersonalWeeklyBriefTarget, ...],
    changed_at: datetime | None = None,
) -> None:
    if row.status != "delivered" or not row.provider_message_id:
        return
    receipt = dict(row.delivery_receipt_json or {})
    delivered_user_ids = receipt.get("delivered_dingtalk_user_ids")
    if not (
        receipt.get("schema_version")
        == "agent2.personal_weekly_brief.delivery.v1"
        and receipt.get("provider_reference") == row.provider_message_id
        and receipt.get("delivery_verified") is True
        and receipt.get("delivery_status") == "SUCCESS"
        and isinstance(delivered_user_ids, list)
        and len(delivered_user_ids) == 1
    ):
        raise RuntimeError("personal weekly brief stored delivery receipt is invalid")
    frozen_owner_targets = tuple(
        target
        for target in frozen_targets
        if target.internal_user_id == row.owner_user_id
        and target.conversation_id == row.conversation_id
    )
    if (
        len(frozen_owner_targets) != 1
        or delivered_user_ids != [frozen_owner_targets[0].dingtalk_user_id]
    ):
        raise RuntimeError("personal weekly brief stored delivery receipt is invalid")
    context_recorded_at = changed_at or _personal_weekly_observed_now()
    async with AsyncSessionLocal() as session:
        revalidation = await load_personal_weekly_brief_target_revalidation(
            session,
            tenant_id=tenant_id,
            roster_tenant_id=roster_tenant_id or tenant_id,
            on_date=context_recorded_at.date(),
            expected_model_name=CANARY_MODEL_NAME,
            frozen_targets=frozen_targets,
        )
        target = revalidation.valid_targets.get(row.owner_user_id)
        if target is None:
            return
        if delivered_user_ids != [target.dingtalk_user_id]:
            raise RuntimeError("personal weekly brief stored delivery receipt is invalid")
        user = await session.scalar(
            select(User).where(
                User.id == UUID(target.internal_user_id),
                User.active.is_(True),
                User.dingtalk_user_id == target.dingtalk_user_id,
            )
        )
        if user is None:
            raise RuntimeError("personal weekly brief delivered user identity changed")
        await record_verified_outbound_context_message(
            session,
            user=user,
            conversation_id=target.conversation_id,
            message_text=row.message_text,
            source_message_id=f"personal-weekly-brief:{row.brief_id}",
            delivery_receipt={
                "deliveryVerified": True,
                "deliveryStatus": "SUCCESS",
                "deliveryRecipientUserIds": delivered_user_ids,
                "invalidStaffIdList": [],
                "filteredStaffIdList": [],
                "flowControlledStaffIdList": [],
                "processQueryKey": receipt["provider_reference"],
            },
            sent_at=row.delivered_at or context_recorded_at,
        )
        await SqlPersonalWeeklyBriefStore(session).record_context(
            tenant_id=tenant_id,
            brief_id=row.brief_id,
            changed_at=context_recorded_at,
        )
        await session.commit()


def _weekly_plan_schedule_facts(settings, *, now: datetime):
    """Derive exact collection dates; business meaning never enters this helper."""

    schedule = derive_weekly_plan_collection_schedule(
        observed_at=now,
        timezone_name=settings.timezone,
        collection_open_hour=settings.weekly_plan_collection_open_hour,
        collection_open_minute=settings.weekly_plan_collection_open_minute,
        snapshot_hour=settings.weekly_plan_snapshot_hour,
        snapshot_minute=settings.weekly_plan_snapshot_minute,
    )
    return schedule.target_week_start, schedule.window


async def _load_weekly_plan_member(
    session,
    *,
    tenant_id: str,
    user_id: str,
) -> WeeklyPlanRosterMember:
    binding = await session.scalar(
        select(Agent2IdentityBinding).where(
            Agent2IdentityBinding.tenant_id == tenant_id,
            Agent2IdentityBinding.user_id == user_id,
            Agent2IdentityBinding.active.is_(True),
        )
    )
    if binding is None:
        raise ValueError("weekly_plan_identity_binding_missing")
    return WeeklyPlanRosterMember(
        user_id=binding.user_id,
        display_name=binding.display_name,
        department_id=binding.department_id,
        team_id=binding.team_id,
    )


async def _load_weekly_plan_reminder_recipients(
    session,
    *,
    tenant_id: str,
    user_ids: tuple[str, ...],
) -> tuple[WeeklyPlanReminderRecipient, ...]:
    bindings = list(
        (
            await session.scalars(
                select(Agent2IdentityBinding).where(
                    Agent2IdentityBinding.tenant_id == tenant_id,
                    Agent2IdentityBinding.user_id.in_(user_ids),
                    Agent2IdentityBinding.active.is_(True),
                )
            )
        ).all()
    )
    by_user_id = {str(binding.user_id): binding for binding in bindings}
    if set(by_user_id) != set(user_ids):
        raise ValueError("weekly_plan_reminder_identity_binding_missing")
    dingtalk_user_ids = {
        str(binding.dingtalk_user_id).strip() for binding in bindings
    }
    if "" in dingtalk_user_ids or len(dingtalk_user_ids) != len(user_ids):
        raise ValueError("weekly_plan_reminder_identity_binding_invalid")
    return tuple(
        WeeklyPlanReminderRecipient(
            tenant_id=tenant_id,
            internal_user_id=user_id,
            dingtalk_user_id=str(by_user_id[user_id].dingtalk_user_id).strip(),
        )
        for user_id in user_ids
    )


async def run_weekly_plan_collection_open_job(settings, *, now: datetime) -> None:
    scope = _strict_weekly_plan_scope(settings)
    if scope is None:
        return
    tenant_id, user_ids = scope
    target_week_start, window = _weekly_plan_schedule_facts(settings, now=now)
    async with AsyncSessionLocal() as session:
        members = tuple(
            [
                await _load_weekly_plan_member(
                    session,
                    tenant_id=tenant_id,
                    user_id=user_id,
                )
                for user_id in user_ids
            ]
        )
        orchestrator = SqlWeeklyPlanCollectionOrchestrator(
            SqlWeeklyPlanStore(session)
        )
        await orchestrator.open_collection(
            tenant_id=tenant_id,
            target_week_start=target_week_start,
            source_roster=members,
            canary_user_ids=frozenset(user_ids),
            window=window,
        )
        await session.commit()


async def run_weekly_plan_history_suggestion_refresh_job(
    settings,
    *,
    llm_client: LLMClient,
    now: datetime,
) -> None:
    """Refresh optional history suggestions after the canary plan exists."""

    scope = _strict_weekly_plan_scope(settings)
    if (
        scope is None
        or getattr(settings, "agent2_weekly_plan_enabled", False) is not True
        or getattr(settings, "agent2_weekly_plan_write_enabled", False) is not True
    ):
        return
    tenant_id, user_ids = scope
    target_week_start, _ = _weekly_plan_schedule_facts(settings, now=now)
    async with AsyncSessionLocal() as session:
        service = WeeklyPlanHistorySuggestionService(
            history_source=SqlTrustedDailyHistorySource(session),
            suggestion_store=SqlHistorySuggestionStore(session),
            reviewer=LLMHistoryFollowUpReviewer(
                llm_client,
                model=getattr(
                    settings,
                    "agent2_cognitive_core_v3_model",
                    None,
                ),
                thinking_enabled=False,
            ),
        )
        for user_id in user_ids:
            await service.refresh(
                HistorySuggestionRefreshRequest(
                    tenant_id=tenant_id,
                    owner_user_id=user_id,
                    target_week_start=target_week_start,
                    as_of=now,
                )
            )
        await session.commit()


async def _load_weekly_plan_opening(settings, *, now: datetime, session):
    scope = _strict_weekly_plan_scope(settings)
    if scope is None:
        return None, None
    tenant_id, user_ids = scope
    target_week_start, window = _weekly_plan_schedule_facts(settings, now=now)
    members = tuple(
        [
            await _load_weekly_plan_member(
                session,
                tenant_id=tenant_id,
                user_id=user_id,
            )
            for user_id in user_ids
        ]
    )
    orchestrator = SqlWeeklyPlanCollectionOrchestrator(
        SqlWeeklyPlanStore(session),
        outbox_store=SqlWeeklyPlanReminderOutboxStore(session),
    )
    opening = await orchestrator.open_collection(
        tenant_id=tenant_id,
        target_week_start=target_week_start,
        source_roster=members,
        canary_user_ids=frozenset(user_ids),
        window=window,
    )
    return opening, orchestrator


async def run_weekly_plan_reminder_enqueue_job(settings, *, now: datetime) -> None:
    scope = _strict_weekly_plan_scope(settings)
    send_user_ids = _strict_weekly_plan_user_ids(
        getattr(settings, "agent2_weekly_plan_send_user_allowlist", "")
    )
    if (
        scope is None
        or getattr(settings, "agent2_weekly_plan_send_enabled", False) is not True
        or send_user_ids is None
        or not set(send_user_ids).issubset(scope[1])
    ):
        return
    async with AsyncSessionLocal() as session:
        opening, orchestrator = await _load_weekly_plan_opening(
            settings, now=now, session=session
        )
        if opening is None or orchestrator is None:
            return
        await orchestrator.enqueue_private_reminders(
            opening=opening,
            canary_user_ids=frozenset(send_user_ids),
            reminder_at=now,
            created_at=now,
            reminder_slot="friday-primary",
        )
        await session.commit()


async def run_weekly_plan_reminder_dispatch_job(
    settings,
    *,
    robot: DingTalkRobotClient,
    now: datetime,
) -> None:
    scope = _strict_weekly_plan_scope(settings)
    send_user_ids = _strict_weekly_plan_user_ids(
        getattr(settings, "agent2_weekly_plan_send_user_allowlist", "")
    )
    if (
        scope is None
        or getattr(settings, "agent2_weekly_plan_send_enabled", False) is not True
        or send_user_ids is None
        or not set(send_user_ids).issubset(scope[1])
    ):
        return
    tenant_id, _user_ids = scope
    async with AsyncSessionLocal() as session:
        recipients = await _load_weekly_plan_reminder_recipients(
            session,
            tenant_id=tenant_id,
            user_ids=send_user_ids,
        )
        outbox = SqlWeeklyPlanReminderOutboxStore(session)
        dispatcher = WeeklyPlanReminderDispatcher(
            outbox=outbox,
            transport=DingTalkWeeklyPlanReminderTransport(robot),
            tenant_allowlist=frozenset({tenant_id}),
            user_allowlist=frozenset(send_user_ids),
        )
        for recipient in recipients:
            rows = await outbox.load_due_queued(
                tenant_id=tenant_id,
                recipient_internal_user_id=recipient.internal_user_id,
                as_of=now,
                limit=1,
            )
            for row in rows:
                await dispatcher.dispatch(
                    row=row,
                    recipient=recipient,
                    changed_at=now,
                    claim_token=(
                        f"weekly-plan:{row.outbox_id}:{now.isoformat()}"
                    ),
                )
        await session.commit()


async def run_weekly_plan_reminder_reconcile_job(
    settings,
    *,
    robot: DingTalkRobotClient,
    now: datetime,
) -> None:
    scope = _strict_weekly_plan_scope(settings)
    send_user_ids = _strict_weekly_plan_user_ids(
        getattr(settings, "agent2_weekly_plan_send_user_allowlist", "")
    )
    if (
        scope is None
        or getattr(settings, "agent2_weekly_plan_send_enabled", False) is not True
        or send_user_ids is None
        or not set(send_user_ids).issubset(scope[1])
    ):
        return
    tenant_id, _user_ids = scope
    async with AsyncSessionLocal() as session:
        recipients = await _load_weekly_plan_reminder_recipients(
            session,
            tenant_id=tenant_id,
            user_ids=send_user_ids,
        )
        outbox = SqlWeeklyPlanReminderOutboxStore(session)
        dispatcher = WeeklyPlanReminderDispatcher(
            outbox=outbox,
            transport=DingTalkWeeklyPlanReminderTransport(robot),
            tenant_allowlist=frozenset({tenant_id}),
            user_allowlist=frozenset(send_user_ids),
        )
        for recipient in recipients:
            rows = await outbox.load_delivery_pending(
                tenant_id=tenant_id,
                recipient_internal_user_id=recipient.internal_user_id,
                limit=10,
            )
            for row in rows:
                await dispatcher.reconcile_pending(
                    row=row,
                    recipient=recipient,
                    changed_at=now,
                )
        await session.commit()


async def run_weekly_plan_reminder_maintenance_job(
    settings,
    *,
    robot: DingTalkRobotClient,
    now: datetime,
) -> None:
    """Recover an abandoned durable claim, then verify accepted deliveries."""

    await run_weekly_plan_reminder_dispatch_job(
        settings,
        robot=robot,
        now=now,
    )
    await run_weekly_plan_reminder_reconcile_job(
        settings,
        robot=robot,
        now=now,
    )


async def run_weekly_plan_monday_snapshot_job(settings, *, now: datetime) -> None:
    # On Monday the target being frozen is the week that starts today, not the
    # following natural week used for new mentions.
    scope = _strict_weekly_plan_scope(settings)
    if scope is None:
        return
    local_now = now.astimezone(ZoneInfo(settings.timezone))
    if local_now.weekday() != 0:
        raise ValueError("weekly_plan_snapshot_requires_monday")
    friday = local_now - timedelta(days=3)
    async with AsyncSessionLocal() as session:
        opening, orchestrator = await _load_weekly_plan_opening(
            settings, now=friday, session=session
        )
        if opening is None or orchestrator is None:
            return
        await orchestrator.freeze_monday_snapshot(
            opening=opening,
            snapshot_at=now,
        )
        await session.commit()


@dataclass(frozen=True)
class _DailyBriefingResumeState:
    next_part_index: int = 1
    dispatches: tuple[dict[str, object], ...] = ()


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
        report_date = _auto_submit_report_date(current_date)
        if report_date is None:
            logger.info(
                "auto submit skipped by reporting calendar current_date=%s",
                current_date.isoformat(),
            )
            return
        if _scheduler_paused(settings, current_date) or _scheduler_paused(
            settings, report_date
        ):
            logger.info(
                "auto submit skipped by scheduler pause current_date=%s report_date=%s",
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
            await auto_submit_due_pending_reports(
                session,
                settings,
                report_date=report_date,
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

    # Weekly-plan reminders remain inside the exact configured private scope.
    # Provider acceptance stays pending until exact-recipient delivery proof.
    async def weekly_plan_open_job() -> None:
        now = datetime.now(ZoneInfo(settings.timezone))
        await run_weekly_plan_collection_open_job(
            settings,
            now=now,
        )
        await run_weekly_plan_history_suggestion_refresh_job(
            settings,
            llm_client=llm_client,
            now=now,
        )

    async def weekly_plan_reminder_job() -> None:
        await run_weekly_plan_reminder_enqueue_job(
            settings,
            now=datetime.now(ZoneInfo(settings.timezone)),
        )
        await run_weekly_plan_reminder_dispatch_job(
            settings,
            robot=robot,
            now=datetime.now(ZoneInfo(settings.timezone)),
        )

    async def weekly_plan_snapshot_job() -> None:
        await run_weekly_plan_monday_snapshot_job(
            settings,
            now=datetime.now(ZoneInfo(settings.timezone)),
        )

    async def weekly_plan_reminder_reconcile_job() -> None:
        await run_weekly_plan_reminder_maintenance_job(
            settings,
            robot=robot,
            now=datetime.now(ZoneInfo(settings.timezone)),
        )

    register_weekly_plan_jobs(
        scheduler,
        settings=settings,
        open_job=weekly_plan_open_job,
        reminder_job=weekly_plan_reminder_job,
        reminder_reconcile_job=weekly_plan_reminder_reconcile_job,
        snapshot_job=weekly_plan_snapshot_job,
    )

    async def personal_weekly_brief_job() -> None:
        result = await run_personal_weekly_brief_generation_job(
            settings,
            llm_client=llm_client,
            robot=robot,
            now=datetime.now(ZoneInfo(PERSONAL_WEEKLY_BRIEF_TIMEZONE)),
        )
        if any(result.values()):
            logger.info("Agent2 personal weekly brief result=%s", result)

    async def personal_weekly_brief_reconcile_job() -> None:
        delivered = await run_personal_weekly_brief_reconcile_job(
            settings,
            llm_client=llm_client,
            robot=robot,
            now=datetime.now(ZoneInfo(PERSONAL_WEEKLY_BRIEF_TIMEZONE)),
        )
        if delivered:
            logger.info(
                "Agent2 personal weekly brief reconciled delivered=%s",
                delivered,
            )

    register_personal_weekly_brief_jobs(
        scheduler,
        settings=settings,
        generation_job=personal_weekly_brief_job,
        reconciliation_job=personal_weekly_brief_reconcile_job,
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
    target_report_date = (
        _briefing_report_date(
            briefings,
            report_date=report_date,
        )
        if session is not None
        else None
    )
    resume_events_by_user = (
        await _load_daily_briefing_resume_events(
            session,
            messages=messages,
            report_date=target_report_date,
        )
        if session is not None
        else {}
    )
    for item in messages:
        title = f"{item.get('team_name') or item.get('department_name') or '部门'}晨报"
        message_text = str(item.get("text") or "")
        message_parts = _split_daily_briefing_text(message_text)
        message_sha256 = _daily_briefing_text_sha256(message_text)
        part_sha256s = tuple(
            _daily_briefing_text_sha256(part) for part in message_parts
        )
        for recipient in item.get("recipients", []):
            dingtalk_user_id = str(
                recipient.get("dingtalk_user_id") or ""
            ).strip()
            if not dingtalk_user_id:
                continue
            recipient_uuid = _daily_briefing_recipient_uuid(recipient)
            if session is not None and recipient_uuid is None:
                logger.error(
                    "daily briefing blocked: invalid recipient user id dingtalk_user_id=%s",
                    dingtalk_user_id,
                )
                continue
            dispatch_key = _daily_briefing_dispatch_key(
                item=item,
                recipient=recipient,
                report_date=target_report_date,
            )
            resume_state = _DailyBriefingResumeState()
            if session is not None:
                try:
                    resume_state = _resolve_daily_briefing_resume_state(
                        events=resume_events_by_user.get(
                            recipient_uuid,
                            (),
                        ),
                        dispatch_key=dispatch_key,
                        message_sha256=message_sha256,
                        part_sha256s=part_sha256s,
                    )
                except DailyBriefingResumeConflict:
                    logger.exception(
                        "daily briefing resume blocked recipient=%s title=%s",
                        recipient.get("id"),
                        title,
                    )
                    continue
            if resume_state.next_part_index > len(message_parts):
                logger.info(
                    "daily briefing already provider-accepted recipient=%s title=%s parts=%s",
                    recipient.get("id"),
                    title,
                    len(message_parts),
                )
                continue
            dispatch_evidence: list[
                tuple[int, ReminderDispatchEvidence]
            ] = []
            delivery_error = ""
            for index in range(
                resume_state.next_part_index,
                len(message_parts) + 1,
            ):
                message_part = message_parts[index - 1]
                part_title = (
                    title
                    if len(message_parts) == 1
                    else f"{title}（{index}/{len(message_parts)}）"
                )
                try:
                    dispatch_evidence.append(
                        (
                            index,
                            await send_user_message(
                                robot,
                                [dingtalk_user_id],
                                message_part,
                                markdown=True,
                                title=part_title,
                            ),
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
            completed_part_count = (
                resume_state.next_part_index
                - 1
                + len(dispatch_evidence)
            )
            if (
                not delivery_error
                and completed_part_count == len(message_parts)
            ):
                sent += 1
            if session is not None:
                recorded_event = _record_daily_briefing_events(
                    session,
                    item=item,
                    recipient=recipient,
                    title=title,
                    report_date=target_report_date,
                    dispatch_key=dispatch_key,
                    message_sha256=message_sha256,
                    part_sha256s=part_sha256s,
                    resumed_from_part_index=(
                        resume_state.next_part_index
                    ),
                    prior_dispatches=resume_state.dispatches,
                    dispatch_evidence=dispatch_evidence,
                    intended_part_count=len(message_parts),
                    delivery_error=delivery_error,
                )
                if recorded_event is not None:
                    resume_events_by_user.setdefault(
                        recorded_event.user_id,
                        [],
                    ).append(recorded_event)
    return sent


def _daily_briefing_text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _daily_briefing_dispatch_key(
    *,
    item: dict,
    recipient: dict,
    report_date: date | None,
) -> str:
    scope = str(item.get("scope") or "").strip()
    scope_ref = (
        str(item.get("team_id") or item.get("team_name") or "").strip()
        if scope == "team"
        else scope
    )
    identity = {
        "contract": _DAILY_BRIEFING_DISPATCH_CONTRACT,
        "report_date": report_date.isoformat() if report_date else "",
        "recipient_user_id": str(recipient.get("id") or "").strip(),
        "dingtalk_user_id": str(
            recipient.get("dingtalk_user_id") or ""
        ).strip(),
        "scope": scope,
        "scope_ref": scope_ref,
    }
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return _daily_briefing_text_sha256(encoded)


def _daily_briefing_recipient_uuid(recipient: dict) -> UUID | None:
    try:
        return UUID(str(recipient.get("id") or ""))
    except (TypeError, ValueError, AttributeError):
        return None


async def _load_daily_briefing_resume_events(
    session,
    *,
    messages: list[dict],
    report_date: date,
) -> dict[UUID, list[ReportInteractionEvent]]:
    scalars = getattr(session, "scalars", None)
    if not callable(scalars):
        raise TypeError(
            "daily briefing audit session must support scalar queries"
        )
    user_ids = tuple(
        dict.fromkeys(
            user_id
            for item in messages
            for recipient in item.get("recipients", [])
            if (
                user_id := _daily_briefing_recipient_uuid(recipient)
            )
        )
    )
    if not user_ids:
        return {}
    events = tuple(
        (
            await scalars(
                select(ReportInteractionEvent)
                .where(
                    ReportInteractionEvent.user_id.in_(user_ids),
                    ReportInteractionEvent.report_date == report_date,
                    ReportInteractionEvent.backend_action.in_(
                        _DAILY_BRIEFING_EVENT_ACTIONS
                    ),
                )
                .order_by(ReportInteractionEvent.created_at)
            )
        ).all()
    )
    by_user: dict[UUID, list[ReportInteractionEvent]] = {}
    for event in events:
        by_user.setdefault(event.user_id, []).append(event)
    return by_user


def _resolve_daily_briefing_resume_state(
    *,
    events: tuple[ReportInteractionEvent, ...]
    | list[ReportInteractionEvent],
    dispatch_key: str,
    message_sha256: str,
    part_sha256s: tuple[str, ...],
) -> _DailyBriefingResumeState:
    accepted: dict[int, dict[str, object]] = {}
    for event in events:
        payload = dict(event.llm_decision_json or {})
        if (
            payload.get("dispatch_contract")
            != _DAILY_BRIEFING_DISPATCH_CONTRACT
            or str(payload.get("dispatch_key") or "") != dispatch_key
        ):
            continue
        raw_dispatches = payload.get("attempt_dispatches")
        if not isinstance(raw_dispatches, list):
            raise DailyBriefingResumeConflict(
                "recorded briefing attempt has no segment evidence"
            )
        if (
            str(payload.get("message_sha256") or "")
            != message_sha256
            or tuple(payload.get("part_sha256s") or ())
            != part_sha256s
        ):
            if (
                not raw_dispatches
                and payload.get("completed_part_count") == 0
            ):
                # No provider acceptance exists, so replacing the unsent
                # payload cannot duplicate or splice a prior briefing.
                continue
            raise DailyBriefingResumeConflict(
                "recorded briefing content differs from retry content"
            )
        attempt_dispatches = [
            dict(dispatch)
            for dispatch in raw_dispatches
            if isinstance(dispatch, dict)
        ]
        if len(attempt_dispatches) != len(raw_dispatches):
            raise DailyBriefingResumeConflict(
                "recorded briefing segment evidence is malformed"
            )
        attempt_indexes = [
            dispatch.get("part_index") for dispatch in attempt_dispatches
        ]
        if payload.get("accepted_part_indexes") != attempt_indexes:
            raise DailyBriefingResumeConflict(
                "recorded briefing segment indexes disagree"
            )
        for dispatch in attempt_dispatches:
            part_index = dispatch.get("part_index")
            if (
                isinstance(part_index, bool)
                or not isinstance(part_index, int)
                or part_index < 1
                or part_index > len(part_sha256s)
            ):
                raise DailyBriefingResumeConflict(
                    "recorded briefing segment index is invalid"
                )
            if (
                str(dispatch.get("part_sha256") or "")
                != part_sha256s[part_index - 1]
                or not str(
                    dispatch.get("provider_reference") or ""
                ).strip()
            ):
                raise DailyBriefingResumeConflict(
                    "recorded briefing segment evidence does not match"
                )
            if part_index in accepted:
                raise DailyBriefingResumeConflict(
                    "a briefing segment has multiple provider acceptances"
                )
            accepted[part_index] = dispatch
        expected_indexes = list(range(1, len(accepted) + 1))
        if sorted(accepted) != expected_indexes:
            raise DailyBriefingResumeConflict(
                "recorded briefing segments are not a contiguous prefix"
            )
        completed_part_count = payload.get("completed_part_count")
        if completed_part_count != len(accepted):
            raise DailyBriefingResumeConflict(
                "recorded briefing progress does not match evidence"
            )
    ordered_dispatches = tuple(
        accepted[index] for index in sorted(accepted)
    )
    return _DailyBriefingResumeState(
        next_part_index=len(ordered_dispatches) + 1,
        dispatches=ordered_dispatches,
    )


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
    dispatch_key: str,
    message_sha256: str,
    part_sha256s: tuple[str, ...],
    resumed_from_part_index: int,
    prior_dispatches: tuple[dict[str, object], ...],
    dispatch_evidence: list[tuple[int, ReminderDispatchEvidence]],
    intended_part_count: int,
    delivery_error: str,
) -> ReportInteractionEvent | None:
    attempt_dispatches = [
        {
            "part_index": part_index,
            "part_sha256": part_sha256s[part_index - 1],
            "transport": evidence.channel,
            "provider_reference": evidence.provider_reference,
            "delivery_verified": evidence.delivery_verified,
        }
        for part_index, evidence in dispatch_evidence
    ]
    cumulative_dispatches = [
        *(dict(dispatch) for dispatch in prior_dispatches),
        *attempt_dispatches,
    ]
    provider_references = [
        str(dispatch.get("provider_reference") or "")
        for dispatch in cumulative_dispatches
    ]
    transports = list(
        dict.fromkeys(
            str(dispatch.get("transport") or "")
            for dispatch in cumulative_dispatches
        )
    )
    try:
        user_id = UUID(str(recipient.get("id") or ""))
    except (TypeError, ValueError, AttributeError):
        logger.warning(
            "daily briefing audit skipped: invalid recipient user id"
        )
        return None
    completed_part_count = len(cumulative_dispatches)
    all_parts_accepted = not delivery_error and (
        completed_part_count == intended_part_count
    )
    delivery_verified = all_parts_accepted and all(
        dispatch.get("delivery_verified") is True
        for dispatch in cumulative_dispatches
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
    event = ReportInteractionEvent(
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
                "dispatch_contract": (
                    _DAILY_BRIEFING_DISPATCH_CONTRACT
                ),
                "dispatch_key": dispatch_key,
                "message_sha256": message_sha256,
                "part_sha256s": list(part_sha256s),
                "resumed_from_part_index": resumed_from_part_index,
                "accepted_part_indexes": [
                    part_index
                    for part_index, _evidence in dispatch_evidence
                ],
                "completed_part_count": completed_part_count,
                "briefing_snapshot": dict(
                    item.get("briefing_snapshot") or {}
                ),
                "message_status": message_status,
                "provider_references": provider_references,
                "provider_reference_available": bool(
                    provider_references
                ),
                "transport": transports,
                "part_count": completed_part_count,
                "attempt_part_count": len(dispatch_evidence),
                "intended_part_count": intended_part_count,
                "delivery_verified": delivery_verified,
                "delivery_error": delivery_error,
                "attempt_dispatches": attempt_dispatches,
                # Reconciliation needs the entire accepted prefix, including
                # segments accepted during an earlier interrupted attempt.
                "dispatches": cumulative_dispatches,
        },
        backend_action=backend_action,
        before_snapshot_json={},
        after_snapshot_json={},
    )
    session.add(event)
    return event


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


def _auto_submit_report_date(current_date: date) -> date | None:
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
