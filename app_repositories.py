from __future__ import annotations

import hashlib
import uuid
from datetime import date, datetime
from decimal import Decimal
import re
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.models import DailyReport, Team, TeamSummary, User, WebhookEvent


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
    event.status = "processed"
    event.report_id = report_id
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
) -> DailyReport:
    user_id = user.id
    team_id = user.team_id
    existing = await get_report(session, user_id, report_date)
    fragment = {
        "received_at": received_at.isoformat(),
        "source": source,
        "raw_input": raw_input,
        "structured": llm_payload,
    }

    reloaded_after_insert_conflict = False
    if existing is None:
        report = DailyReport(
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
            return report
        except IntegrityError:
            await session.rollback()
            await acquire_daily_report_advisory_lock(session, user_id, report_date)
            existing = await get_report(session, user_id, report_date)
            reloaded_after_insert_conflict = True
            if existing is None:
                raise

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
    elif existing.submitted_at is None and confirmation_type == "none":
        existing.submitted_at = None
    await session.flush()
    return existing


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
