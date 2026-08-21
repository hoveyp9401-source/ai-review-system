from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select

from app.agent2.memory import PersonalMemoryModule, PersonalMemoryScope
from app.agent2.memory.postgres import PostgresPersonalMemoryReadStore
from app.agent2.personal_weekly_brief import (
    PersonalWeeklyBriefSnapshot,
    SourceEvidence,
)
from app.agent2.personal_weekly_brief_scope import PersonalWeeklyBriefTarget
from app.agent2.weekly_plan_models import WeeklyPlan
from app.agent2.weekly_plan_store import SqlWeeklyPlanStore
from app.models import DailyReport


_DAILY_SECTIONS = ("today_work", "problems", "tomorrow_plan")


def build_personal_weekly_brief_snapshot(
    *,
    tenant_id: str,
    owner_user_id: str,
    week_start: date,
    snapshot_at: datetime,
    daily_reports: tuple[Any, ...] | list[Any],
    weekly_plan: WeeklyPlan | None,
) -> PersonalWeeklyBriefSnapshot:
    week_end = week_start + timedelta(days=4)
    owner_uuid = UUID(owner_user_id)
    reports = tuple(
        sorted(
            (
                report
                for report in daily_reports
                if getattr(report, "user_id", None) == owner_uuid
                and week_start <= getattr(report, "report_date", date.min) <= week_end
                and str(getattr(report, "status", ""))
                not in {"cancelled", "skipped"}
            ),
            key=lambda report: (report.report_date, str(report.id)),
        )
    )
    sources: list[SourceEvidence] = []
    for report in reports:
        for section in _DAILY_SECTIONS:
            values = getattr(report, section, ()) or ()
            for position, raw_text in enumerate(values, start=1):
                original_text = str(raw_text or "").strip()
                if not original_text:
                    continue
                sources.append(
                    SourceEvidence(
                        source_id=(
                            f"daily_report:{report.id}:{report.report_date.isoformat()}:"
                            f"{section}:{position}"
                        ),
                        source_kind="daily_report",
                        source_record_id=str(report.id),
                        source_date=report.report_date,
                        section=section,
                        original_text=original_text,
                    )
                )

    plan_found = bool(
        weekly_plan is not None
        and weekly_plan.tenant_id == tenant_id
        and weekly_plan.owner_user_id == owner_user_id
        and weekly_plan.target_week_start == week_start
        and weekly_plan.status != "cancelled"
    )
    if plan_found:
        assert weekly_plan is not None
        for day in sorted(weekly_plan.days, key=lambda item: item.plan_date):
            if not week_start <= day.plan_date <= week_end:
                continue
            for item in day.items:
                original_text = str(item.original_text or "").strip()
                if not original_text:
                    continue
                sources.append(
                    SourceEvidence(
                        source_id=(
                            f"weekly_plan:{weekly_plan.plan_id}:{day.day_id}:{item.item_id}"
                        ),
                        source_kind="weekly_plan",
                        source_record_id=weekly_plan.plan_id,
                        source_date=day.plan_date,
                        section="plan_item",
                        original_text=original_text,
                    )
                )

    return PersonalWeeklyBriefSnapshot(
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        week_start=week_start,
        week_end=week_end,
        snapshot_at=snapshot_at,
        daily_report_dates=tuple(dict.fromkeys(report.report_date for report in reports)),
        weekly_plan_found=plan_found,
        sources=tuple(sources),
    )


class SqlPersonalWeeklyBriefSourceLoader:
    """Read only one authenticated owner's reports, plan and personal memory."""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def load_snapshot(
        self,
        *,
        target: PersonalWeeklyBriefTarget,
        week_start: date,
        snapshot_at: datetime,
    ) -> tuple[PersonalWeeklyBriefSnapshot, dict[str, Any]]:
        week_end = week_start + timedelta(days=4)
        owner_uuid = UUID(target.internal_user_id)
        reports = tuple(
            (
                await self._session.scalars(
                    select(DailyReport)
                    .where(
                        DailyReport.user_id == owner_uuid,
                        DailyReport.report_date >= week_start,
                        DailyReport.report_date <= week_end,
                        DailyReport.status.not_in(("cancelled", "skipped")),
                    )
                    .order_by(DailyReport.report_date, DailyReport.id)
                )
            ).all()
        )
        plan = await SqlWeeklyPlanStore(self._session).load_plan_by_owner_week(
            tenant_id=target.tenant_id,
            owner_user_id=target.internal_user_id,
            target_week_start=week_start,
        )
        memory = await PersonalMemoryModule(
            read_port=PostgresPersonalMemoryReadStore(self._session)
        ).read_for_turn(
            PersonalMemoryScope(
                tenant_id=target.tenant_id,
                user_id=owner_uuid,
                now=snapshot_at,
            )
        )
        memory_payload = memory.model_payload()
        preferred_salutation = next(
            (
                entry.value.salutation
                for entry in memory.entries
                if entry.memory_key == "response.preferred_salutation"
            ),
            "",
        )
        memory_payload["server_preferred_salutation"] = preferred_salutation
        return (
            build_personal_weekly_brief_snapshot(
                tenant_id=target.tenant_id,
                owner_user_id=target.internal_user_id,
                week_start=week_start,
                snapshot_at=snapshot_at,
                daily_reports=reports,
                weekly_plan=plan,
            ),
            memory_payload,
        )


__all__ = [
    "SqlPersonalWeeklyBriefSourceLoader",
    "build_personal_weekly_brief_snapshot",
]
