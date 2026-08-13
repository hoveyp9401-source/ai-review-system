"""Deterministic date checks for model-proposed weekly-plan operations.

Agent2 still decides whether a sentence is the authenticated person's plan and
which operation it requests.  This module only verifies that the model's exact
current-message clause contains one safe day expression and that the resulting
calendar date is the same date proposed by the model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


class WeeklyPlanDateBindingError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class WeeklyPlanDateResolution:
    resolved_date: date
    basis: str
    week_scope_explicit: bool = True


_DAY_NUMBER = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
_RELATIVE_DAY = re.compile(
    # Both “本周三” and the common spoken form “本周周三” carry an
    # explicit week scope.  Put the longer alternatives first so the second
    # “周” is not accidentally re-read as a bare weekday prefix.
    r"(?P<prefix>下周周|本周周|这周周|下周|下星期|本周|本星期|这周|这星期|周|星期)"
    r"(?P<day>[一二三四五六日天])"
)
_ISO_DATE = re.compile(r"(?<!\d)(?P<year>20\d{2})[-/.年](?P<month>\d{1,2})[-/.月](?P<day>\d{1,2})(?:日|号)?(?!\d)")
_MONTH_DAY = re.compile(r"(?<!\d)(?P<month>\d{1,2})月(?P<day>\d{1,2})(?:日|号)?(?!\d)")
_UNSAFE_RELATION = re.compile(r"(?:之前|以前|前完成|前交|左右|附近|前后|~|～|或者|或是|或)")
_MOVE_TARGET = re.compile(r"(?:挪|移动|移|改|调整|放).*(?:到|至|成)?")
_CLAUSE_BOUNDARY = re.compile(r"[。！？!?；;，,:：\n]")


def resolve_explicit_weekly_plan_date(
    *,
    source_message: str,
    exact_clause_quote: str,
    source_occurred_at: datetime,
    business_timezone: str,
    target_week_start: date,
    date_role: str = "single_day",
) -> WeeklyPlanDateResolution:
    """Resolve exactly one day from an exact, complete current-message clause."""

    source = str(source_message)
    clause = str(exact_clause_quote)
    if not source.strip() or not clause.strip() or clause not in source:
        raise WeeklyPlanDateBindingError("WEEKLY_PLAN_DATE_EVIDENCE_MISMATCH")
    if source_occurred_at.tzinfo is None or source_occurred_at.utcoffset() is None:
        raise WeeklyPlanDateBindingError("WEEKLY_PLAN_SOURCE_TIME_REQUIRED")
    if target_week_start.weekday() != 0:
        raise WeeklyPlanDateBindingError("WEEKLY_PLAN_TARGET_WEEK_INVALID")
    if date_role not in {"single_day", "move_target"}:
        raise WeeklyPlanDateBindingError("WEEKLY_PLAN_DATE_ROLE_INVALID")
    if not _is_complete_clause(source, clause):
        raise WeeklyPlanDateBindingError("WEEKLY_PLAN_DATE_CLAUSE_INCOMPLETE")
    if _UNSAFE_RELATION.search(clause):
        raise WeeklyPlanDateBindingError("WEEKLY_PLAN_DAY_AMBIGUOUS")

    local_date = source_occurred_at.astimezone(ZoneInfo(business_timezone)).date()
    this_monday = local_date - timedelta(days=local_date.weekday())
    candidates: list[tuple[date, str, bool]] = []

    for match in _ISO_DATE.finditer(clause):
        try:
            candidates.append(
                (
                    date(
                        int(match.group("year")),
                        int(match.group("month")),
                        int(match.group("day")),
                    ),
                    "absolute_date",
                    True,
                )
            )
        except ValueError as exc:
            raise WeeklyPlanDateBindingError(
                "WEEKLY_PLAN_DATE_INVALID"
            ) from exc
    masked = _ISO_DATE.sub("", clause)
    for match in _MONTH_DAY.finditer(masked):
        month = int(match.group("month"))
        day = int(match.group("day"))
        exact_week_matches = tuple(
            candidate
            for offset in range(6)
            if (candidate := target_week_start + timedelta(days=offset)).month
            == month
            and candidate.day == day
        )
        if len(exact_week_matches) == 1:
            candidates.append(
                (exact_week_matches[0], "absolute_month_day", True)
            )
            continue
        try:
            fallback = date(target_week_start.year, month, day)
        except ValueError as exc:
            raise WeeklyPlanDateBindingError(
                "WEEKLY_PLAN_DATE_INVALID"
            ) from exc
        candidates.append((fallback, "absolute_month_day", True))
    for match in _RELATIVE_DAY.finditer(clause):
        offset = _DAY_NUMBER[match.group("day")]
        prefix = match.group("prefix")
        if prefix in {"下周", "下周周", "下星期"}:
            monday = this_monday + timedelta(days=7)
        elif prefix in {"本周", "本周周", "本星期", "这周", "这周周", "这星期"}:
            monday = this_monday
        else:
            monday = target_week_start
        candidates.append(
            (
                monday + timedelta(days=offset),
                "relative_weekday",
                prefix not in {"周", "星期"},
            )
        )

    unique_dates = {candidate for candidate, _, _ in candidates}
    if not candidates:
        raise WeeklyPlanDateBindingError("WEEKLY_PLAN_DAY_NOT_EXPLICIT")
    if len(candidates) == 1 and len(unique_dates) == 1:
        resolved, basis, week_scope_explicit = candidates[0]
    elif (
        date_role == "move_target"
        and len(candidates) == 2
        and len(unique_dates) == 2
        and _MOVE_TARGET.search(clause)
    ):
        resolved, basis, week_scope_explicit = candidates[-1]
    else:
        raise WeeklyPlanDateBindingError("WEEKLY_PLAN_DAY_AMBIGUOUS")
    if resolved not in {
        target_week_start + timedelta(days=offset) for offset in range(6)
    }:
        if resolved.weekday() == 6:
            raise WeeklyPlanDateBindingError("WEEKLY_PLAN_DAY_OUTSIDE_MONDAY_SATURDAY")
        raise WeeklyPlanDateBindingError("WEEKLY_PLAN_TARGET_WEEK_MISMATCH")
    return WeeklyPlanDateResolution(resolved, basis, week_scope_explicit)


def validate_weekly_plan_date_binding(
    *,
    source_message: str,
    exact_clause_quote: str,
    source_occurred_at: datetime,
    business_timezone: str,
    target_week_start: date,
    proposed_date: date,
    date_role: str = "single_day",
    require_explicit_week_scope: bool = False,
) -> WeeklyPlanDateResolution:
    resolution = resolve_explicit_weekly_plan_date(
        source_message=source_message,
        exact_clause_quote=exact_clause_quote,
        source_occurred_at=source_occurred_at,
        business_timezone=business_timezone,
        target_week_start=target_week_start,
        date_role=date_role,
    )
    if require_explicit_week_scope and not resolution.week_scope_explicit:
        raise WeeklyPlanDateBindingError("WEEKLY_PLAN_TARGET_WEEK_AMBIGUOUS")
    if resolution.resolved_date != proposed_date:
        raise WeeklyPlanDateBindingError("WEEKLY_PLAN_DATE_MISMATCH")
    return resolution


def _is_complete_clause(source: str, quote: str) -> bool:
    start = source.find(quote)
    if start < 0:
        return False
    end = start + len(quote)
    before = source[start - 1] if start else ""
    after = source[end] if end < len(source) else ""
    left_ok = not before or bool(_CLAUSE_BOUNDARY.fullmatch(before))
    right_ok = not after or bool(_CLAUSE_BOUNDARY.fullmatch(after))
    return left_ok and right_ok


__all__ = [
    "WeeklyPlanDateBindingError",
    "WeeklyPlanDateResolution",
    "resolve_explicit_weekly_plan_date",
    "validate_weekly_plan_date_binding",
]
