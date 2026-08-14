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


@dataclass(frozen=True)
class WeeklyPlanDateSetResolution:
    """A bounded recurrence already selected semantically by Agent2."""

    resolved_dates: tuple[date, ...]
    basis: str
    week_scope_explicit: bool = True


_DAY_NUMBER = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
_RELATIVE_DAY = re.compile(
    # Both “本周三” and the common spoken form “本周周三” carry an
    # explicit week scope.  Put the longer alternatives first so the second
    # “周” is not accidentally re-read as a bare weekday prefix.
    r"(?P<prefix>下周周|本周周|这周周|下周|下星期|本周|本星期|这周|这星期|周|星期|礼拜)"
    r"(?P<day>[一二三四五六日天])"
)
_ISO_DATE = re.compile(r"(?<!\d)(?P<year>20\d{2})[-/.年](?P<month>\d{1,2})[-/.月](?P<day>\d{1,2})(?:日|号)?(?!\d)")
_MONTH_DAY = re.compile(r"(?<!\d)(?P<month>\d{1,2})月(?P<day>\d{1,2})(?:日|号)?(?!\d)")
_UNSAFE_RELATION = re.compile(r"(?:之前|以前|前完成|前交|左右|附近|前后|~|～|或者|或是|或)")
_MOVE_TARGET = re.compile(r"(?:挪|移动|移|改|调整|放).*(?:到|至|成)?")
_CLAUSE_BOUNDARY = re.compile(r"[。！？!?；;，,:：\n]")
_DAILY_RECURRENCE = re.compile(r"(?:每天|每日|天天)")
_EXPLICIT_WEEK_SCOPE = re.compile(r"(?:下周|下星期|本周|本星期|这周|这星期)")
_INCLUSIVE_RANGE = re.compile(r"(?:到|至|—|－|-)")
_MONTH_DAY_RANGE = re.compile(
    r"(?<!\d)(?P<start_month>\d{1,2})月(?P<start_day>\d{1,2})(?:日|号)?"
    r"\s*(?:到|至|—|－|-)\s*"
    r"(?:(?P<end_month>\d{1,2})月)?(?P<end_day>\d{1,2})(?:日|号)(?!\d)"
)
_SCOPE_IGNORABLE = re.compile(r"[\s（）()，,、的]")


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
                prefix not in {"周", "星期", "礼拜"},
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


def validate_weekly_plan_date_set_binding(
    *,
    source_message: str,
    exact_clause_quote: str,
    recurrence_scope_quote: str,
    matter_text: str,
    source_occurred_at: datetime,
    business_timezone: str,
    target_week_start: date,
    proposed_dates: tuple[date, ...],
    require_explicit_week_scope: bool = False,
) -> WeeklyPlanDateSetResolution:
    """Validate one matter repeated on every day of the selected plan.

    Agent2 and its independent semantic reviewer decide that the user meant a
    recurrence.  This deterministic guard does not decide the business intent;
    it only proves that the current-message clause is complete, explicitly
    recurrent, and that the proposed date set is exactly the trusted full-plan
    recurrence or one explicit inclusive weekday range.
    """

    source = str(source_message)
    clause = str(exact_clause_quote)
    scope = str(recurrence_scope_quote)
    matter = str(matter_text)
    if not source.strip() or not clause.strip() or clause not in source:
        raise WeeklyPlanDateBindingError("WEEKLY_PLAN_DATE_EVIDENCE_MISMATCH")
    if (
        not scope.strip()
        or scope not in clause
        or not matter.strip()
        or matter not in clause
    ):
        raise WeeklyPlanDateBindingError(
            "WEEKLY_PLAN_RECURRENCE_SCOPE_EVIDENCE_MISMATCH"
        )
    if source_occurred_at.tzinfo is None or source_occurred_at.utcoffset() is None:
        raise WeeklyPlanDateBindingError("WEEKLY_PLAN_SOURCE_TIME_REQUIRED")
    if target_week_start.weekday() != 0:
        raise WeeklyPlanDateBindingError("WEEKLY_PLAN_TARGET_WEEK_INVALID")
    # Recurrence authorization is bound to the complete immutable current
    # message, not a model-selected subclause.  This prevents a draft from
    # hiding a later qualifier after punctuation.  Agent2's independent
    # reviewers still own the semantic decision across Daily/Weekly matters.
    if clause != source:
        raise WeeklyPlanDateBindingError(
            "WEEKLY_PLAN_RECURRENCE_SOURCE_EVIDENCE_INCOMPLETE"
        )
    if source.count(scope) != 1 or source.count(matter) != 1:
        raise WeeklyPlanDateBindingError(
            "WEEKLY_PLAN_RECURRENCE_SOURCE_SPAN_AMBIGUOUS"
        )
    scope_start = source.index(scope)
    matter_start = source.index(matter)
    if not (
        scope_start + len(scope) <= matter_start
        or matter_start + len(matter) <= scope_start
    ):
        raise WeeklyPlanDateBindingError(
            "WEEKLY_PLAN_RECURRENCE_SOURCE_SPAN_AMBIGUOUS"
        )
    if _UNSAFE_RELATION.search(scope):
        raise WeeklyPlanDateBindingError("WEEKLY_PLAN_DAY_AMBIGUOUS")
    if not _DAILY_RECURRENCE.search(scope):
        raise WeeklyPlanDateBindingError("WEEKLY_PLAN_DAY_NOT_EXPLICIT")
    if require_explicit_week_scope and not _EXPLICIT_WEEK_SCOPE.search(scope):
        raise WeeklyPlanDateBindingError("WEEKLY_PLAN_TARGET_WEEK_AMBIGUOUS")

    local_date = source_occurred_at.astimezone(
        ZoneInfo(business_timezone)
    ).date()
    this_monday = local_date - timedelta(days=local_date.weekday())
    explicit_week_starts = {
        (
            this_monday + timedelta(days=7)
            if match.group(0) in {"下周", "下星期"}
            else this_monday
        )
        for match in _EXPLICIT_WEEK_SCOPE.finditer(scope)
    }
    if len(explicit_week_starts) > 1:
        raise WeeklyPlanDateBindingError("WEEKLY_PLAN_TARGET_WEEK_AMBIGUOUS")
    if explicit_week_starts and target_week_start not in explicit_week_starts:
        raise WeeklyPlanDateBindingError("WEEKLY_PLAN_TARGET_WEEK_MISMATCH")

    if not proposed_dates or len(proposed_dates) != len(set(proposed_dates)):
        raise WeeklyPlanDateBindingError("WEEKLY_PLAN_DATE_SET_INVALID")
    trusted_dates = tuple(
        target_week_start + timedelta(days=offset) for offset in range(6)
    )
    expected_dates = trusted_dates
    basis = "all_plan_days_recurrence"

    absolute_ranges = tuple(_MONTH_DAY_RANGE.finditer(scope))
    if len(absolute_ranges) > 1:
        raise WeeklyPlanDateBindingError("WEEKLY_PLAN_DATE_SET_INVALID")
    scope_outside_absolute_range = scope
    if absolute_ranges:
        absolute_match = absolute_ranges[0]
        scope_outside_absolute_range = (
            scope[: absolute_match.start()] + scope[absolute_match.end() :]
        )
    if _ISO_DATE.search(scope_outside_absolute_range) or _MONTH_DAY.search(
        scope_outside_absolute_range
    ):
        raise WeeklyPlanDateBindingError("WEEKLY_PLAN_DATE_SET_INVALID")
    absolute_dates: tuple[date, ...] | None = None
    if absolute_ranges:
        absolute_match = absolute_ranges[0]

        def _trusted_month_day(month: int, day: int) -> date:
            matches = tuple(
                candidate
                for candidate in trusted_dates
                if candidate.month == month and candidate.day == day
            )
            if len(matches) != 1:
                raise WeeklyPlanDateBindingError(
                    "WEEKLY_PLAN_TARGET_WEEK_MISMATCH"
                )
            return matches[0]

        start_month = int(absolute_match.group("start_month"))
        start = _trusted_month_day(
            start_month,
            int(absolute_match.group("start_day")),
        )
        end = _trusted_month_day(
            int(absolute_match.group("end_month") or start_month),
            int(absolute_match.group("end_day")),
        )
        if start > end:
            raise WeeklyPlanDateBindingError("WEEKLY_PLAN_DATE_SET_INVALID")
        absolute_dates = tuple(
            start + timedelta(days=offset)
            for offset in range((end - start).days + 1)
        )
        expected_dates = absolute_dates
        basis = "inclusive_absolute_range_recurrence"

    relative_matches = tuple(_RELATIVE_DAY.finditer(scope))
    has_relative_range = len(relative_matches) == 2 and bool(
        _INCLUSIVE_RANGE.search(
            scope[relative_matches[0].end() : relative_matches[1].start()]
        )
    )
    if relative_matches and not has_relative_range:
        raise WeeklyPlanDateBindingError("WEEKLY_PLAN_DAY_AMBIGUOUS")
    if has_relative_range:
        def _relative_date(match: re.Match[str]) -> date:
            prefix = match.group("prefix")
            if prefix in {"下周", "下周周", "下星期"}:
                monday = this_monday + timedelta(days=7)
            elif prefix in {
                "本周",
                "本周周",
                "本星期",
                "这周",
                "这周周",
                "这星期",
            }:
                monday = this_monday
            else:
                monday = target_week_start
            return monday + timedelta(days=_DAY_NUMBER[match.group("day")])

        start = _relative_date(relative_matches[0])
        end = _relative_date(relative_matches[1])
        if start > end:
            raise WeeklyPlanDateBindingError("WEEKLY_PLAN_DATE_SET_INVALID")
        relative_dates = tuple(
            start + timedelta(days=offset)
            for offset in range((end - start).days + 1)
        )
        if not set(relative_dates).issubset(set(trusted_dates)):
            raise WeeklyPlanDateBindingError(
                "WEEKLY_PLAN_DAY_OUTSIDE_MONDAY_SATURDAY"
            )
        if absolute_dates is not None and absolute_dates != relative_dates:
            raise WeeklyPlanDateBindingError("WEEKLY_PLAN_DATE_SET_CONFLICT")
        expected_dates = relative_dates
        basis = (
            "consistent_absolute_and_weekday_range_recurrence"
            if absolute_dates is not None
            else "inclusive_weekday_range_recurrence"
        )
    scope_residual = scope
    for pattern in (
        _MONTH_DAY_RANGE,
        _ISO_DATE,
        _MONTH_DAY,
        _RELATIVE_DAY,
        _EXPLICIT_WEEK_SCOPE,
        _DAILY_RECURRENCE,
        _INCLUSIVE_RANGE,
    ):
        scope_residual = pattern.sub("", scope_residual)
    scope_residual = _SCOPE_IGNORABLE.sub("", scope_residual)
    if scope_residual:
        raise WeeklyPlanDateBindingError(
            "WEEKLY_PLAN_DATE_SET_UNSUPPORTED"
        )
    if set(proposed_dates) != set(expected_dates):
        raise WeeklyPlanDateBindingError("WEEKLY_PLAN_DATE_SET_MISMATCH")
    return WeeklyPlanDateSetResolution(
        resolved_dates=expected_dates,
        basis=basis,
        week_scope_explicit=bool(_EXPLICIT_WEEK_SCOPE.search(scope)),
    )


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
    "WeeklyPlanDateSetResolution",
    "resolve_explicit_weekly_plan_date",
    "validate_weekly_plan_date_binding",
    "validate_weekly_plan_date_set_binding",
]
