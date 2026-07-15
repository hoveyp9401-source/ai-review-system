from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
import re
from typing import Any

from sqlalchemy import func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.models import DailyReport, ProgressOutboxEvent, ReportInteractionEvent, Team, TeamSummary, User, UserHabit, WebhookEvent


logger = logging.getLogger(__name__)

USER_HABIT_STATUSES = {"candidate", "active", "rejected", "disabled"}
PROGRESS_OUTBOX_STATUSES = {"pending", "processing", "processed", "failed", "dead_letter"}


async def get_active_user_by_dingtalk_id(session: AsyncSession, dingtalk_user_id: str) -> User | None:
    result = await session.execute(
        select(User)
        .options(selectinload(User.team))
        .where(User.dingtalk_user_id == dingtalk_user_id, User.active.is_(True))
    )
    return result.scalar_one_or_none()


async def get_active_users(session: AsyncSession) -> list[User]:
    result = await session.execute(
        select(User).options(selectinload(User.team)).where(User.active.is_(True)).order_by(User.team_id, User.name)
    )
    return list(result.scalars().all())


async def get_active_teams(session: AsyncSession) -> list[Team]:
    result = await session.execute(select(Team).where(Team.active.is_(True)).order_by(Team.code))
    return list(result.scalars().all())


async def get_report(session: AsyncSession, user_id: uuid.UUID, report_date: date) -> DailyReport | None:
    result = await session.execute(
        select(DailyReport).where(DailyReport.user_id == user_id, DailyReport.report_date == report_date)
    )
    return result.scalar_one_or_none()


async def acquire_daily_report_advisory_lock(
    session: AsyncSession,
    user_id: uuid.UUID,
    report_date: date,
) -> None:
    key1, key2 = daily_report_advisory_lock_keys(user_id, report_date)
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key1, :key2)"),
        {"key1": key1, "key2": key2},
    )


def daily_report_advisory_lock_keys(user_id: uuid.UUID, report_date: date) -> tuple[int, int]:
    seed = f"{user_id}:{report_date.isoformat()}".encode("utf-8")
    digest = hashlib.sha256(seed).digest()
    return (
        int.from_bytes(digest[:4], byteorder="big", signed=True),
        int.from_bytes(digest[4:8], byteorder="big", signed=True),
    )


async def list_reports_for_date(
    session: AsyncSession,
    report_date: date,
    team_id: uuid.UUID | None = None,
) -> list[DailyReport]:
    stmt = select(DailyReport).where(DailyReport.report_date == report_date)
    if team_id is not None:
        stmt = stmt.where(DailyReport.team_id == team_id)
    result = await session.execute(stmt.order_by(DailyReport.team_id, DailyReport.user_id))
    return list(result.scalars().all())


async def list_reports_between_dates(
    session: AsyncSession,
    start_date: date,
    end_date: date,
    team_id: uuid.UUID | None = None,
) -> list[DailyReport]:
    stmt = select(DailyReport).where(DailyReport.report_date >= start_date, DailyReport.report_date <= end_date)
    if team_id is not None:
        stmt = stmt.where(DailyReport.team_id == team_id)
    result = await session.execute(stmt.order_by(DailyReport.report_date.desc(), DailyReport.team_id, DailyReport.user_id))
    return list(result.scalars().all())


async def list_missing_users(session: AsyncSession, report_date: date) -> list[User]:
    report_exists = (
        select(DailyReport.id)
        .where(DailyReport.user_id == User.id, DailyReport.report_date == report_date, DailyReport.status == "completed")
        .exists()
    )
    result = await session.execute(
        select(User)
        .options(selectinload(User.team))
        .where(User.active.is_(True), ~report_exists)
        .order_by(User.team_id, User.name)
    )
    return list(result.scalars().all())


async def create_webhook_event_once(
    session: AsyncSession,
    *,
    idempotency_key: str,
    external_message_id: str | None,
    dingtalk_user_id: str | None,
    payload: dict[str, Any],
) -> tuple[WebhookEvent, bool]:
    stmt = (
        insert(WebhookEvent)
        .values(
            idempotency_key=idempotency_key,
            external_message_id=external_message_id,
            dingtalk_user_id=dingtalk_user_id,
            payload=payload,
            status="processing",
        )
        .on_conflict_do_nothing(index_elements=[WebhookEvent.idempotency_key])
        .returning(WebhookEvent.id)
    )
    result = await session.execute(stmt)
    inserted_id = result.scalar_one_or_none()
    if inserted_id is not None:
        event = await session.get(WebhookEvent, inserted_id)
        if event is None:
            raise RuntimeError("Inserted webhook event cannot be loaded.")
        return event, True

    existing = await session.execute(select(WebhookEvent).where(WebhookEvent.idempotency_key == idempotency_key))
    event = existing.scalar_one()
    return event, False


async def mark_webhook_event_processed(
    session: AsyncSession,
    event: WebhookEvent,
    *,
    report_id: uuid.UUID | None,
    response_payload: dict[str, Any],
    now: datetime,
) -> None:
    legacy_report_id = None
    if report_id is not None:
        # WebhookEvent.report_id is a legacy FK to daily_reports. Unified
        # Report-domain snapshots use their own stable IDs and must not be
        # written into this legacy relationship unless that row actually exists.
        legacy_report = await session.get(DailyReport, report_id)
        if legacy_report is not None:
            legacy_report_id = report_id
    event.status = "processed"
    event.report_id = legacy_report_id
    event.response_payload = response_payload
    event.processed_at = now
    event.error_message = None
    await session.flush()


async def mark_webhook_event_failed(
    session: AsyncSession,
    event: WebhookEvent,
    *,
    error_message: str,
    response_payload: dict[str, Any],
    now: datetime,
) -> None:
    event.status = "failed"
    event.error_message = error_message[:2000]
    event.response_payload = response_payload
    event.processed_at = now
    await session.flush()


async def create_progress_outbox_event_once(
    session: AsyncSession,
    *,
    event_type: str,
    source_type: str,
    source_id: str,
    user_id: uuid.UUID | None,
    team_id: uuid.UUID | None,
    report_id: uuid.UUID | None,
    report_date: date | None,
    payload_json: dict[str, Any],
    raw_text_hash: str,
    idempotency_key: str,
) -> tuple[ProgressOutboxEvent, bool]:
    stmt = (
        insert(ProgressOutboxEvent)
        .values(
            event_type=event_type,
            source_type=source_type,
            source_id=source_id,
            user_id=user_id,
            team_id=team_id,
            report_id=report_id,
            report_date=report_date,
            payload_json=payload_json or {},
            raw_text_hash=raw_text_hash or "",
            idempotency_key=idempotency_key,
            status="pending",
        )
        .on_conflict_do_nothing(index_elements=[ProgressOutboxEvent.idempotency_key])
        .returning(ProgressOutboxEvent.id)
    )
    result = await session.execute(stmt)
    inserted_id = result.scalar_one_or_none()
    if inserted_id is not None:
        event = await session.get(ProgressOutboxEvent, inserted_id)
        if event is None:
            raise RuntimeError("Inserted progress outbox event cannot be loaded.")
        return event, True

    existing = await session.execute(
        select(ProgressOutboxEvent).where(ProgressOutboxEvent.idempotency_key == idempotency_key)
    )
    return existing.scalar_one(), False


async def claim_pending_progress_outbox_events(
    session: AsyncSession,
    *,
    worker_id: str,
    limit: int,
    now: datetime,
) -> list[ProgressOutboxEvent]:
    stmt = (
        select(ProgressOutboxEvent)
        .where(
            ProgressOutboxEvent.status.in_(("pending", "failed")),
            or_(ProgressOutboxEvent.next_retry_at.is_(None), ProgressOutboxEvent.next_retry_at <= now),
        )
        .order_by(ProgressOutboxEvent.created_at, ProgressOutboxEvent.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    result = await session.execute(stmt)
    events = list(result.scalars().all())
    for event in events:
        event.status = "processing"
        event.locked_by = worker_id
        event.locked_at = now
        event.error_message = None
    await session.flush()
    return events


async def mark_progress_outbox_processed(
    session: AsyncSession,
    event: ProgressOutboxEvent,
    *,
    now: datetime,
) -> None:
    event.status = "processed"
    event.processed_at = now
    event.error_message = None
    event.locked_by = None
    event.locked_at = None
    await session.flush()


async def mark_progress_outbox_failed(
    session: AsyncSession,
    event: ProgressOutboxEvent,
    *,
    error_message: str,
    now: datetime,
    max_retries: int,
    retry_delay_seconds: int = 60,
) -> None:
    event.retry_count = int(event.retry_count or 0) + 1
    event.error_message = (error_message or "")[:2000]
    event.locked_by = None
    event.locked_at = None
    if event.retry_count > max_retries:
        event.status = "dead_letter"
        event.next_retry_at = None
        event.processed_at = now
    else:
        event.status = "failed"
        event.next_retry_at = now + timedelta(seconds=max(1, retry_delay_seconds))
    await session.flush()


async def recover_stale_progress_outbox_events(
    session: AsyncSession,
    *,
    stale_before: datetime,
    now: datetime,
    limit: int = 100,
) -> list[ProgressOutboxEvent]:
    stmt = (
        select(ProgressOutboxEvent)
        .where(
            ProgressOutboxEvent.status == "processing",
            ProgressOutboxEvent.locked_at.is_not(None),
            ProgressOutboxEvent.locked_at < stale_before,
        )
        .order_by(ProgressOutboxEvent.locked_at, ProgressOutboxEvent.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    result = await session.execute(stmt)
    events = list(result.scalars().all())
    for event in events:
        event.status = "pending"
        event.locked_by = None
        event.locked_at = None
        event.next_retry_at = None
        event.error_message = None
    await session.flush()
    return events


async def progress_outbox_status_snapshot(session: AsyncSession) -> dict[str, Any]:
    counts_result = await session.execute(
        select(ProgressOutboxEvent.status, func.count(ProgressOutboxEvent.id)).group_by(ProgressOutboxEvent.status)
    )
    counts = {status: int(count) for status, count in counts_result.all()}
    oldest_pending_result = await session.execute(
        select(func.min(ProgressOutboxEvent.created_at)).where(ProgressOutboxEvent.status == "pending")
    )
    latest_processed_result = await session.execute(
        select(func.max(ProgressOutboxEvent.processed_at)).where(ProgressOutboxEvent.status == "processed")
    )
    recent_failed_result = await session.execute(
        select(ProgressOutboxEvent.error_message)
        .where(ProgressOutboxEvent.status.in_(("failed", "dead_letter")))
        .order_by(ProgressOutboxEvent.created_at.desc())
        .limit(1)
    )
    return {
        "pending": counts.get("pending", 0),
        "processing": counts.get("processing", 0),
        "processed": counts.get("processed", 0),
        "failed": counts.get("failed", 0),
        "dead_letter": counts.get("dead_letter", 0),
        "oldest_pending_at": oldest_pending_result.scalar_one_or_none(),
        "latest_processed_at": latest_processed_result.scalar_one_or_none(),
        "recent_failed_error": recent_failed_result.scalar_one_or_none() or "",
    }


async def upsert_daily_report(
    session: AsyncSession,
    *,
    user: User,
    report_date: date,
    raw_input: str,
    source: str,
    today_work: list[str],
    problems: list[str],
    tomorrow_plan: list[str],
    emotion: str,
    completeness_score: float,
    status: str,
    section_status: dict[str, bool],
    llm_model: str | None,
    llm_payload: dict[str, Any],
    received_at: datetime,
    confirmation_type: str,
    confirmed_by_user: bool,
    quality_warning: str | None,
    last_modified_by_user: bool,
    last_modified_at: datetime | None,
    pending_confirmation_at: datetime | None,
    auto_submit_at: datetime | None,
    replace_sections: bool = False,
    report_id_override: uuid.UUID | None = None,
) -> DailyReport:
    user_id = user.id
    team_id = user.team_id
    existing = await get_report(session, user_id, report_date)
    before_snapshot = build_report_interaction_snapshot(existing) if existing is not None else {}
    fragment = {
        "received_at": received_at.isoformat(),
        "source": source,
        "raw_input": raw_input,
        "structured": llm_payload,
    }

    reloaded_after_insert_conflict = False
    if existing is None:
        report = DailyReport(
            id=report_id_override or uuid.uuid4(),
            user_id=user_id,
            team_id=team_id,
            report_date=report_date,
            today_work=today_work,
            problems=problems,
            tomorrow_plan=tomorrow_plan,
            emotion=emotion,
            raw_input=raw_input,
            input_fragments=[fragment],
            section_status=section_status,
            completeness_score=Decimal(str(round(completeness_score, 4))),
            status=status,
            confirmation_type=confirmation_type,
            confirmed_by_user=confirmed_by_user,
            quality_warning=quality_warning,
            last_modified_by_user=last_modified_by_user,
            last_modified_at=last_modified_at,
            pending_confirmation_at=pending_confirmation_at,
            auto_submit_at=auto_submit_at,
            source=source,
            llm_model=llm_model,
            llm_payload=llm_payload,
            submitted_at=received_at if status == "completed" else None,
        )
        session.add(report)
        try:
            await session.flush()
            await maybe_create_report_interaction_event(
                session,
                user=user,
                report=report,
                report_date=report_date,
                message_text=raw_input,
                llm_decision_json=llm_payload,
                backend_action=infer_backend_action(llm_payload=llm_payload, source=source, status=status),
                before_snapshot_json=before_snapshot,
                after_snapshot_json=build_report_interaction_snapshot(report),
            )
            return report
        except IntegrityError:
            await session.rollback()
            await acquire_daily_report_advisory_lock(session, user_id, report_date)
            existing = await get_report(session, user_id, report_date)
            reloaded_after_insert_conflict = True
            if existing is None:
                raise
            before_snapshot = build_report_interaction_snapshot(existing)

    if replace_sections and not reloaded_after_insert_conflict:
        existing.today_work = today_work
        existing.problems = problems
        existing.tomorrow_plan = tomorrow_plan
    else:
        existing.today_work = merge_ordered(existing.today_work, today_work)
        existing.problems = merge_ordered(existing.problems, problems)
        existing.tomorrow_plan = merge_ordered(existing.tomorrow_plan, tomorrow_plan)
    existing.emotion = emotion or existing.emotion
    existing.raw_input = join_raw_input(existing.raw_input, raw_input)
    existing.input_fragments = [*existing.input_fragments, fragment]
    existing.section_status = section_status
    existing.completeness_score = Decimal(str(round(completeness_score, 4)))
    existing.status = status
    existing.confirmation_type = confirmation_type
    existing.confirmed_by_user = confirmed_by_user
    existing.quality_warning = quality_warning
    existing.last_modified_by_user = last_modified_by_user
    existing.last_modified_at = last_modified_at
    existing.pending_confirmation_at = pending_confirmation_at
    existing.auto_submit_at = auto_submit_at
    existing.source = source
    existing.llm_model = llm_model
    existing.llm_payload = llm_payload
    if status == "completed":
        existing.submitted_at = received_at
    else:
        existing.submitted_at = None
    await session.flush()
    await maybe_create_report_interaction_event(
        session,
        user=user,
        report=existing,
        report_date=report_date,
        message_text=raw_input,
        llm_decision_json=llm_payload,
        backend_action=infer_backend_action(llm_payload=llm_payload, source=source, status=status),
        before_snapshot_json=before_snapshot,
        after_snapshot_json=build_report_interaction_snapshot(existing),
    )
    return existing


async def maybe_create_report_interaction_event(
    session: AsyncSession,
    *,
    user: User,
    report: DailyReport | None,
    report_date: date,
    message_text: str,
    llm_decision_json: dict[str, Any],
    backend_action: str,
    before_snapshot_json: dict[str, Any],
    after_snapshot_json: dict[str, Any],
    report_id_override: Any | None = None,
) -> None:
    if not get_settings().shadow_memory_enabled:
        return
    correction = infer_shadow_correction(message_text)
    confidence = infer_shadow_confidence(llm_decision_json)
    try:
        async with session.begin_nested():
            event = ReportInteractionEvent(
                user_id=user.id,
                report_id=report_id_override or getattr(report, "id", None),
                dingtalk_user_id=getattr(user, "dingtalk_user_id", None),
                report_date=report_date,
                message_text=message_text,
                llm_decision_json=llm_decision_json or {},
                backend_action=backend_action,
                before_snapshot_json=before_snapshot_json or {},
                after_snapshot_json=after_snapshot_json or {},
                correction_type=correction.get("correction_type", ""),
                correction_from=correction.get("correction_from", ""),
                correction_to=correction.get("correction_to", ""),
                confidence=Decimal(str(round(confidence, 4))) if confidence is not None else None,
                is_undo=is_shadow_undo(message_text, llm_decision_json),
                is_repeated_item_edit=is_repeated_item_edit(before_snapshot_json, llm_decision_json),
                asr_suspect_json=correction.get("asr_suspect_json", {}),
            )
            session.add(event)
            try:
                await _maybe_observe_user_habit(
                    session,
                    user=user,
                    message_text=message_text,
                    backend_action=backend_action,
                    after_snapshot_json=after_snapshot_json or {},
                    correction=correction,
                    event=event,
                )
            except Exception:
                logger.exception("user habit observation failed")
            await session.flush()
    except Exception:
        logger.exception("shadow memory event write failed")


async def list_active_user_habits(session: AsyncSession, user_id: uuid.UUID, *, limit: int = 12) -> list[UserHabit]:
    if not hasattr(session, "execute"):
        return []
    result = await session.execute(
        select(UserHabit)
        .where(UserHabit.user_id == user_id, UserHabit.status == "active")
        .order_by(UserHabit.confidence.desc(), UserHabit.evidence_count.desc(), UserHabit.updated_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def set_user_habit_status(session: AsyncSession, habit_id: uuid.UUID, status: str) -> UserHabit | None:
    result = await session.execute(select(UserHabit).where(UserHabit.id == habit_id))
    habit = result.scalar_one_or_none()
    if habit is None:
        return None
    apply_user_habit_status(habit, status)
    await session.flush()
    return habit


def apply_user_habit_status(habit: UserHabit, status: str) -> UserHabit:
    normalized_status = str(status or "").strip()
    if normalized_status not in USER_HABIT_STATUSES:
        raise ValueError(f"Unsupported user habit status: {status}")
    habit.status = normalized_status
    habit.updated_at = func.now()
    if normalized_status == "active":
        habit.activated_at = getattr(habit, "activated_at", None) or func.now()
        current_confidence = float(getattr(habit, "confidence", 0) or 0)
        if current_confidence < 0.8:
            habit.confidence = Decimal("0.8000")
    elif normalized_status == "candidate":
        habit.activated_at = None
    return habit


async def _maybe_observe_user_habit(
    session: AsyncSession,
    *,
    user: User,
    message_text: str,
    backend_action: str,
    after_snapshot_json: dict[str, Any],
    correction: dict[str, Any],
    event: ReportInteractionEvent,
) -> None:
    for candidate in infer_user_habit_candidates(
        message_text=message_text,
        backend_action=backend_action,
        after_snapshot_json=after_snapshot_json,
        correction=correction,
    ):
        await _upsert_user_habit_candidate(session, user=user, candidate=candidate, event=event)


async def _upsert_user_habit_candidate(
    session: AsyncSession,
    *,
    user: User,
    candidate: dict[str, Any],
    event: ReportInteractionEvent,
) -> None:
    habit_type = str(candidate.get("habit_type") or "")[:64]
    trigger_text = str(candidate.get("trigger_text") or "")[:128]
    meaning = str(candidate.get("meaning") or "")
    if not habit_type or not trigger_text or not meaning:
        return
    stmt = select(UserHabit).where(
        UserHabit.user_id == user.id,
        UserHabit.habit_type == habit_type,
        UserHabit.trigger_text == trigger_text,
        UserHabit.meaning == meaning,
    )
    result = await session.execute(stmt)
    habit = result.scalar_one_or_none()
    now_expr = func.now()
    if habit is None:
        habit = UserHabit(
            user_id=user.id,
            habit_type=habit_type,
            trigger_text=trigger_text,
            meaning=meaning,
            confidence=Decimal("0.34"),
            evidence_count=1,
            counterexample_count=0,
            status="candidate",
            evidence_json={"event_ids": [str(getattr(event, "id", "") or "")], "examples": [str(candidate.get("example") or "")[:300]]},
            last_observed_at=now_expr,
        )
        session.add(habit)
        return
    habit.evidence_count = int(habit.evidence_count or 0) + 1
    habit.counterexample_count = int(habit.counterexample_count or 0)
    confidence = min(0.95, 0.34 + habit.evidence_count * 0.18 - habit.counterexample_count * 0.25)
    habit.confidence = Decimal(str(round(confidence, 4)))
    evidence = dict(habit.evidence_json or {})
    event_ids = list(evidence.get("event_ids") or [])
    event_ids.append(str(getattr(event, "id", "") or ""))
    examples = list(evidence.get("examples") or [])
    example = str(candidate.get("example") or "")[:300]
    if example and example not in examples:
        examples.append(example)
    evidence["event_ids"] = event_ids[-20:]
    evidence["examples"] = examples[-5:]
    habit.evidence_json = evidence
    habit.last_observed_at = now_expr
    if habit.status == "candidate" and habit.evidence_count >= 3 and habit.counterexample_count == 0 and confidence >= 0.8:
        habit.status = "active"
        habit.activated_at = now_expr


def infer_user_habit_candidates(
    *,
    message_text: str,
    backend_action: str,
    after_snapshot_json: dict[str, Any],
    correction: dict[str, Any],
) -> list[dict[str, Any]]:
    text_value = (message_text or "").strip()
    compact = re.sub(r"\s+", "", text_value)
    candidates: list[dict[str, Any]] = []
    if not compact:
        return candidates
    problems = [str(item) for item in (after_snapshot_json or {}).get("problems") or []]
    if any(token in compact for token in ("没事", "没啥事", "没什么事")) and any("暂无" in item and "问题" in item for item in problems):
        candidates.append(
            {
                "habit_type": "phrase_meaning",
                "trigger_text": "没事",
                "meaning": "用户说“没事”通常表示 problems=暂无明显问题",
                "example": text_value,
            }
        )
    if compact in {"发我下", "发我一下", "发我看下"} and backend_action in {"current_report_query", "agent_query_current"}:
        candidates.append(
            {
                "habit_type": "command_meaning",
                "trigger_text": "发我下",
                "meaning": "用户说“发我下”通常是在查看当前日报草稿",
                "example": text_value,
            }
        )
    today_work = [str(item) for item in (after_snapshot_json or {}).get("today_work") or []]
    if "昨天" in compact and any(token in compact for token in ("计划", "待办", "安排")) and any(token in compact for token in ("完成", "做完", "搞定")):
        if today_work:
            candidates.append(
                {
                    "habit_type": "previous_plan_rollover",
                    "trigger_text": "昨天计划完成",
                    "meaning": "用户说昨天计划/待办完成时，通常是在把昨天明日计划转成今天完成事项",
                    "example": text_value,
                }
            )
    if correction.get("correction_type") == "asr_correction" and correction.get("correction_from") and correction.get("correction_to"):
        candidates.append(
            {
                "habit_type": "asr_correction",
                "trigger_text": str(correction.get("correction_from")),
                "meaning": f"用户常把“{correction.get('correction_from')}”纠正为“{correction.get('correction_to')}”",
                "example": text_value,
            }
        )
    return candidates


def build_report_interaction_snapshot(report: DailyReport | None) -> dict[str, Any]:
    if report is None:
        return {}
    today_work = list(getattr(report, "today_work", None) or [])
    problems = list(getattr(report, "problems", None) or [])
    tomorrow_plan = list(getattr(report, "tomorrow_plan", None) or [])
    section_status = getattr(report, "section_status", None) or {}
    return {
        "report_id": str(getattr(report, "id", "") or ""),
        "status": str(getattr(report, "status", "") or ""),
        "today_work_count": len(today_work),
        "problems_count": len(problems),
        "tomorrow_plan_count": len(tomorrow_plan),
        "today_work": today_work[:20],
        "problems": problems[:20],
        "tomorrow_plan": tomorrow_plan[:20],
        "item_ids": section_status.get("_draft_item_ids") if isinstance(section_status, dict) else None,
        "previous_snapshot_present": bool(isinstance(section_status, dict) and section_status.get("_previous_draft_snapshot")),
        "unresolved_draft_edit_present": bool(isinstance(section_status, dict) and section_status.get("_unresolved_draft_edit")),
    }


def infer_backend_action(*, llm_payload: dict[str, Any], source: str, status: str) -> str:
    payload = llm_payload or {}
    operation = str(payload.get("operation") or "").strip()
    decision_type = str(payload.get("decision_type") or "").strip()
    if operation:
        return operation[:64]
    if decision_type:
        return decision_type[:64]
    if source == "system_confirm":
        return "confirm_submit"
    if status == "completed":
        return "complete_report"
    return "report_update"


def infer_shadow_confidence(llm_payload: dict[str, Any]) -> float | None:
    value = (llm_payload or {}).get("confidence")
    try:
        return float(value)
    except (TypeError, ValueError):
        structured = (llm_payload or {}).get("report_agent")
        if isinstance(structured, dict):
            confidence = str(structured.get("confidence") or "").lower()
            return {"high": 0.9, "medium": 0.6, "low": 0.3}.get(confidence)
    return None


def infer_shadow_correction(message_text: str) -> dict[str, Any]:
    text = (message_text or "").strip()
    if not text:
        return {}
    patterns = [
        r"不是(?P<from>[^，,。；;]{1,40})[，,。；;\s]*是(?P<to>[^，,。；;]{1,40})",
        r"(?P<from>[^，,。；;]{1,40})改成(?P<to>[^，,。；;]{1,40})",
        r"(?P<from>[^，,。；;]{1,40})改为(?P<to>[^，,。；;]{1,40})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        source = _clean_shadow_correction_text(match.group("from"))
        target = _clean_shadow_correction_text(match.group("to"))
        if not source or not target or source == target:
            continue
        asr = _infer_asr_suspect(source, target, text)
        return {
            "correction_type": "asr_correction" if asr else "text_correction",
            "correction_from": source,
            "correction_to": target,
            "asr_suspect_json": asr,
        }
    return {}


def _clean_shadow_correction_text(value: str) -> str:
    return re.sub(r"^(第一条|第二条|第三条|第四条|第五条|第六条|第七条|第八条|第九条|第十条|这个|那个|应?该)", "", value.strip())[:200]


def _infer_asr_suspect(source: str, target: str, message_text: str) -> dict[str, Any]:
    pairs = {("用眼", "用印"), ("印章", "用章")}
    if (source, target) in pairs or (target, source) in pairs:
        return {
            "from": source,
            "to": target,
            "reason": "known homophone or ASR correction in legal/report context",
        }
    if "不是" in message_text and source and target and len(source) <= 8 and len(target) <= 8:
        return {
            "from": source,
            "to": target,
            "reason": "short explicit correction candidate",
        }
    return {}


def is_shadow_undo(message_text: str, llm_payload: dict[str, Any]) -> bool:
    compact = re.sub(r"\s+", "", message_text or "")
    payload = llm_payload or {}
    restore_previous = payload.get("restore_previous")
    return (
        "撤回" in compact
        or "恢复上一步" in compact
        or str(payload.get("operation") or "") == "restore_previous"
        or bool(isinstance(restore_previous, dict) and restore_previous.get("enabled"))
    )


def is_repeated_item_edit(before_snapshot: dict[str, Any], llm_payload: dict[str, Any]) -> bool:
    item_ids = (before_snapshot or {}).get("item_ids")
    if not item_ids:
        return False
    payload = llm_payload or {}
    refs: list[int] = []
    refs.extend(_positive_ints(payload.get("item_refs")))
    for update in payload.get("field_updates") or []:
        if isinstance(update, dict):
            refs.extend(_positive_ints(update.get("item_refs")))
    for item in [*(payload.get("move_items") or []), *(payload.get("delete_items") or [])]:
        if isinstance(item, dict):
            refs.extend(_positive_ints(item.get("item_refs")))
    return len(refs) != len(set(refs))


def _positive_ints(value: Any) -> list[int]:
    if value is None:
        return []
    candidates = value if isinstance(value, list) else [value]
    result: list[int] = []
    for item in candidates:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number > 0:
            result.append(number)
    return result


async def upsert_team_summary(
    session: AsyncSession,
    *,
    scope: str,
    team_id: uuid.UUID | None,
    summary_date: date,
    key_work: list[str],
    major_problems: list[str],
    risks: list[str],
    tomorrow_plan_distribution: list[dict[str, Any]],
    raw_summary: dict[str, Any],
    report_count: int,
    complete_count: int,
    llm_model: str | None,
    generated_at: datetime,
) -> TeamSummary:
    stmt = select(TeamSummary).where(TeamSummary.scope == scope, TeamSummary.summary_date == summary_date)
    if team_id is None:
        stmt = stmt.where(TeamSummary.team_id.is_(None))
    else:
        stmt = stmt.where(TeamSummary.team_id == team_id)
    result = await session.execute(stmt)
    summary = result.scalar_one_or_none()

    if summary is None:
        summary = TeamSummary(scope=scope, team_id=team_id, summary_date=summary_date)
        session.add(summary)

    summary.key_work = key_work
    summary.major_problems = major_problems
    summary.risks = risks
    summary.tomorrow_plan_distribution = tomorrow_plan_distribution
    summary.raw_summary = raw_summary
    summary.report_count = report_count
    summary.complete_count = complete_count
    summary.llm_model = llm_model
    summary.generated_at = generated_at
    await session.flush()
    return summary


async def count_users_by_team(session: AsyncSession) -> list[tuple[uuid.UUID, int]]:
    result = await session.execute(
        select(User.team_id, func.count(User.id)).where(User.active.is_(True)).group_by(User.team_id)
    )
    return [(team_id, count) for team_id, count in result.all()]


def merge_ordered(existing: list[str] | None, incoming: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for item in [*(existing or []), *incoming]:
        key = item.strip()
        normalized_key = normalize_report_item(key)
        if key and normalized_key not in seen:
            seen.add(normalized_key)
            merged.append(key)
    return merged


def normalize_report_item(item: str) -> str:
    text = item.strip().lower()
    text = re.sub(r"[\s，。,.、；;：:！!？?（）()【】\\[\\]\"'“”‘’]+", "", text)
    text = re.sub(r"^(今天|今日|本日|我|我们|还|又|已|已经)+", "", text)
    text = re.sub(r"(了|的|一下|一下子)", "", text)
    text = text.replace("兩", "两").replace("俩", "两")
    return text or item.strip()


def join_raw_input(existing: str, incoming: str) -> str:
    incoming = incoming.strip()
    if not existing:
        return incoming
    if not incoming:
        return existing
    if incoming in existing:
        return existing
    return f"{existing}\n---\n{incoming}"
