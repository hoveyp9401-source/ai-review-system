from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from uuid import UUID
from zoneinfo import ZoneInfo

from app.agent2.personal_weekly_brief_sources import (
    build_personal_weekly_brief_snapshot,
)
from app.agent2.weekly_plan_models import WeeklyPlan, WeeklyPlanDay, WeeklyPlanItem


NOW = datetime(2026, 8, 22, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
USER_ID = "11111111-1111-4111-8111-111111111111"


def _report(report_date: date, *, report_id: str, **sections):
    return SimpleNamespace(
        id=UUID(report_id),
        user_id=UUID(USER_ID),
        report_date=report_date,
        today_work=sections.get("today_work", []),
        problems=sections.get("problems", []),
        tomorrow_plan=sections.get("tomorrow_plan", []),
        status=sections.get("status", "collecting"),
        created_at=sections.get("created_at", NOW),
        updated_at=sections.get("updated_at", NOW),
    )


def _plan() -> WeeklyPlan:
    return WeeklyPlan(
        plan_id="22222222-2222-4222-8222-222222222222",
        batch_id="33333333-3333-4333-8333-333333333333",
        tenant_id="tenant-a",
        owner_user_id=USER_ID,
        target_week_start=date(2026, 8, 17),
        status="submitted",
        version=3,
        days=(
            WeeklyPlanDay(
                day_id="44444444-4444-4444-8444-444444444444",
                plan_date=date(2026, 8, 17),
                state="planned",
                items=(
                    WeeklyPlanItem(
                        item_id="55555555-5555-4555-8555-555555555555",
                        original_text="推进脱敏案件并在满足付款条件后反馈",
                        source="user_message",
                        source_ref="message-plan",
                        created_at=NOW,
                        updated_at=NOW,
                    ),
                ),
            ),
            WeeklyPlanDay(
                day_id="66666666-6666-4666-8666-666666666666",
                plan_date=date(2026, 8, 22),
                state="planned",
                items=(
                    WeeklyPlanItem(
                        item_id="77777777-7777-4777-8777-777777777777",
                        original_text="周六处理事项不应进入周一至周五口径",
                        source="user_message",
                        source_ref="message-sat",
                        created_at=NOW,
                        updated_at=NOW,
                    ),
                ),
            ),
        ),
    )


def test_sources_use_report_date_not_message_or_creation_time() -> None:
    historical_late_entry = _report(
        date(2026, 8, 20),
        report_id="88888888-8888-4888-8888-888888888888",
        today_work=["周五晚补录的周四工作"],
        created_at=datetime(2026, 8, 21, 23, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    outside_week = _report(
        date(2026, 8, 16),
        report_id="99999999-9999-4999-8999-999999999999",
        today_work=["不应进入本周的内容"],
    )

    snapshot = build_personal_weekly_brief_snapshot(
        tenant_id="tenant-a",
        owner_user_id=USER_ID,
        week_start=date(2026, 8, 17),
        snapshot_at=NOW,
        daily_reports=(outside_week, historical_late_entry),
        weekly_plan=None,
    )

    assert snapshot.daily_report_dates == (date(2026, 8, 20),)
    assert [source.original_text for source in snapshot.sources] == [
        "周五晚补录的周四工作"
    ]


def test_snapshot_keeps_all_daily_sections_and_only_monday_to_friday_plan_days() -> None:
    report = _report(
        date(2026, 8, 18),
        report_id="88888888-8888-4888-8888-888888888888",
        today_work=["整段叙述：沟通、准备材料，尚未完成。"],
        problems=["风险：若8月25日前未反馈，可能影响付款。"],
        tomorrow_plan=["后续安排与甲方再次沟通。"],
    )

    snapshot = build_personal_weekly_brief_snapshot(
        tenant_id="tenant-a",
        owner_user_id=USER_ID,
        week_start=date(2026, 8, 17),
        snapshot_at=NOW,
        daily_reports=(report,),
        weekly_plan=_plan(),
    )

    assert [source.section for source in snapshot.sources] == [
        "today_work",
        "problems",
        "tomorrow_plan",
        "plan_item",
    ]
    assert all(source.source_date <= date(2026, 8, 21) for source in snapshot.sources)
    assert snapshot.weekly_plan_found is True
    assert snapshot.sources[-1].original_text == "推进脱敏案件并在满足付款条件后反馈"
    assert snapshot.sources[-1].source_record_id == _plan().plan_id


def test_snapshot_fingerprint_changes_when_original_source_changes() -> None:
    first = build_personal_weekly_brief_snapshot(
        tenant_id="tenant-a",
        owner_user_id=USER_ID,
        week_start=date(2026, 8, 17),
        snapshot_at=NOW,
        daily_reports=(
            _report(
                date(2026, 8, 18),
                report_id="88888888-8888-4888-8888-888888888888",
                today_work=["原始事实一"],
            ),
        ),
        weekly_plan=None,
    )
    changed = build_personal_weekly_brief_snapshot(
        tenant_id="tenant-a",
        owner_user_id=USER_ID,
        week_start=date(2026, 8, 17),
        snapshot_at=NOW,
        daily_reports=(
            _report(
                date(2026, 8, 18),
                report_id="88888888-8888-4888-8888-888888888888",
                today_work=["原始事实二"],
            ),
        ),
        weekly_plan=None,
    )

    assert first.fingerprint != changed.fingerprint
