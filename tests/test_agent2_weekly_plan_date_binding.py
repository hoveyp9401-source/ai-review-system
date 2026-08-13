from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.agent2.weekly_plan_date_binding import (
    WeeklyPlanDateBindingError,
    validate_weekly_plan_date_binding,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _validate(
    message: str,
    clause: str,
    proposed: date,
    *,
    occurred_at=None,
    week=None,
    date_role="single_day",
):
    return validate_weekly_plan_date_binding(
        source_message=message,
        exact_clause_quote=clause,
        source_occurred_at=occurred_at
        or datetime(2026, 8, 14, 17, 30, tzinfo=SHANGHAI),
        business_timezone="Asia/Shanghai",
        target_week_start=week or date(2026, 8, 17),
        proposed_date=proposed,
        date_role=date_role,
    )


def test_explicit_next_weekday_is_bound_to_the_model_proposed_calendar_date():
    result = _validate(
        "今天完成合同初稿；下周三继续和业务确认条款。",
        "下周三继续和业务确认条款",
        date(2026, 8, 19),
    )
    assert result.resolved_date == date(2026, 8, 19)

    with pytest.raises(WeeklyPlanDateBindingError, match="WEEKLY_PLAN_DATE_MISMATCH"):
        _validate(
            "今天完成合同初稿；下周三继续和业务确认条款。",
            "下周三继续和业务确认条款",
            date(2026, 8, 18),
        )


@pytest.mark.parametrize(
    ("clause", "proposed"),
    (
        ("补本周三做案件复盘", date(2026, 8, 19)),
        ("补本周周三做案件复盘", date(2026, 8, 19)),
        ("下周三整理证据", date(2026, 8, 26)),
        ("下周周三整理证据", date(2026, 8, 26)),
    ),
)
def test_explicit_week_scope_accepts_standard_and_spoken_double_week_forms(
    clause,
    proposed,
):
    _validate(
        clause,
        clause,
        proposed,
        occurred_at=datetime(2026, 8, 17, 9, 0, tzinfo=SHANGHAI),
        week=proposed - timedelta(days=proposed.weekday()),
    )


@pytest.mark.parametrize(
    "clause",
    (
        "下周二或周三整理案件证据",
        "下周三左右整理案件证据",
        "下周三前完成案件证据",
        "下周一到周三整理案件证据",
    ),
)
def test_ambiguous_or_relational_days_cannot_be_forced_into_one_formal_day(clause):
    with pytest.raises(WeeklyPlanDateBindingError, match="WEEKLY_PLAN_DAY_AMBIGUOUS"):
        _validate(clause, clause, date(2026, 8, 19))


def test_exact_clause_cannot_be_cut_out_of_a_multi_day_sentence():
    with pytest.raises(WeeklyPlanDateBindingError, match="WEEKLY_PLAN_DATE_CLAUSE_INCOMPLETE"):
        _validate(
            "下周二或周三整理案件证据",
            "周三整理案件证据",
            date(2026, 8, 19),
        )


def test_a_move_clause_can_bind_its_explicit_destination_without_losing_the_source_day():
    result = _validate(
        "把下周二的开庭挪到下周三",
        "把下周二的开庭挪到下周三",
        date(2026, 8, 19),
        date_role="move_target",
    )
    assert result.resolved_date == date(2026, 8, 19)


def test_commas_are_safe_clause_boundaries_for_one_message_containing_the_whole_week():
    result = _validate(
        "下周一整理材料，周二去上海开庭，周三复盘案件",
        "周二去上海开庭",
        date(2026, 8, 18),
    )
    assert result.resolved_date == date(2026, 8, 18)


def test_sunday_is_explicit_but_outside_the_monday_to_saturday_product_scope():
    with pytest.raises(
        WeeklyPlanDateBindingError,
        match="WEEKLY_PLAN_DAY_OUTSIDE_MONDAY_SATURDAY",
    ):
        _validate("下周日整理材料", "下周日整理材料", date(2026, 8, 23))


def test_original_message_time_controls_sunday_monday_and_cross_year_boundaries():
    _validate(
        "下周三整理材料",
        "下周三整理材料",
        date(2026, 8, 19),
        occurred_at=datetime(2026, 8, 16, 23, 59, tzinfo=SHANGHAI),
        week=date(2026, 8, 17),
    )
    _validate(
        "下周三整理材料",
        "下周三整理材料",
        date(2026, 8, 26),
        occurred_at=datetime(2026, 8, 17, 0, 1, tzinfo=SHANGHAI),
        week=date(2026, 8, 24),
    )
    _validate(
        "下周三整理材料",
        "下周三整理材料",
        date(2027, 1, 6),
        occurred_at=datetime(2026, 12, 31, 17, 0, tzinfo=SHANGHAI),
        week=date(2027, 1, 4),
    )
    _validate(
        "下周三整理材料",
        "下周三整理材料",
        date(2026, 8, 19),
        occurred_at=datetime(2026, 8, 16, 15, 59, tzinfo=timezone.utc),
        week=date(2026, 8, 17),
    )


@pytest.mark.parametrize(
    ("clause", "proposed"),
    (
        ("12月31日整理材料", date(2025, 12, 31)),
        ("1月2日整理材料", date(2026, 1, 2)),
    ),
)
def test_month_day_matches_the_unique_date_inside_a_cross_year_target_week(
    clause,
    proposed,
):
    result = _validate(
        clause,
        clause,
        proposed,
        occurred_at=datetime(2025, 12, 26, 17, 0, tzinfo=SHANGHAI),
        week=date(2025, 12, 29),
    )

    assert result.resolved_date == proposed


def test_month_day_outside_the_exact_target_week_is_rejected():
    with pytest.raises(
        WeeklyPlanDateBindingError,
        match="WEEKLY_PLAN_TARGET_WEEK_MISMATCH",
    ):
        _validate(
            "1月8日整理材料",
            "1月8日整理材料",
            date(2026, 1, 8),
            occurred_at=datetime(2025, 12, 26, 17, 0, tzinfo=SHANGHAI),
            week=date(2025, 12, 29),
        )


def test_an_undated_next_week_statement_is_not_eligible_for_formal_date_binding():
    with pytest.raises(WeeklyPlanDateBindingError, match="WEEKLY_PLAN_DAY_NOT_EXPLICIT"):
        _validate(
            "下周继续完善合同评审规则",
            "下周继续完善合同评审规则",
            date(2026, 8, 19),
        )
