from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from app.legal_daily_dashboard.domain import (
    DailyReportRecord,
    MemberRecord,
    SubmissionObligation,
    TeamRecord,
)
from app.legal_daily_dashboard.repository import DashboardRepository
from app.legal_daily_dashboard.service import classify_submission
from app.legal_daily_roster import (
    FORMAL_CHILD_TEAM_NAMES,
    FORMAL_PARENT_DEPARTMENT,
)
from app.services.state_machine import assess_daily_report_completeness

SubmissionCoveragePeriod = Literal["current_week", "previous_week"]


class SubmissionCoverageNotFound(LookupError):
    pass


class SubmissionCoverageAmbiguous(LookupError):
    pass


class SubmissionCoverageDataError(RuntimeError):
    pass


@dataclass(frozen=True)
class SubmissionCoverageRequest:
    tenant_id: str
    scope_name: str
    period_type: SubmissionCoveragePeriod
    current_date: date
    now: datetime

    def __post_init__(self) -> None:
        if not self.tenant_id.strip():
            raise ValueError("submission coverage tenant is required")
        if not self.scope_name.strip():
            raise ValueError("submission coverage scope is required")
        if self.period_type not in {"current_week", "previous_week"}:
            raise ValueError("submission coverage requires a week period")
        if self.now.tzinfo is None or self.now.utcoffset() is None:
            raise ValueError("submission coverage now must be timezone-aware")


@dataclass(frozen=True)
class SubmissionCoverageResult:
    title: str
    facts: dict[str, Any]
    freshness: str


class SubmissionCoverageQuery:
    """Build one factual team-week submission view from dashboard records."""

    def __init__(self, repository: DashboardRepository) -> None:
        self._repository = repository

    async def execute(
        self,
        request: SubmissionCoverageRequest,
    ) -> SubmissionCoverageResult:
        start_date, end_date = _period_bounds(
            request.period_type,
            current_date=request.current_date,
        )
        visible_teams = await self._repository.list_member_teams(
            tenant_id=request.tenant_id,
            on_date=None,
        )
        team = _resolve_formal_team(
            visible_teams,
            scope_name=request.scope_name,
        )
        records = await self._repository.load_records(
            tenant_id=request.tenant_id,
            team_refs=(team.ref,),
            start_date=start_date,
            end_date=end_date,
        )
        facts = _coverage_facts(
            team=team,
            records=records,
            start_date=start_date,
            end_date=end_date,
            period_type=request.period_type,
            now=request.now,
        )
        period_label = "本周" if request.period_type == "current_week" else "上周"
        return SubmissionCoverageResult(
            title=f"{team.name}{period_label}日报提交情况",
            facts=facts,
            freshness=str(facts["queried_at"]),
        )


def _period_bounds(
    period_type: SubmissionCoveragePeriod,
    *,
    current_date: date,
) -> tuple[date, date]:
    current_week_start = current_date - timedelta(
        days=current_date.weekday()
    )
    if period_type == "current_week":
        return current_week_start, current_date
    if period_type == "previous_week":
        return (
            current_week_start - timedelta(days=7),
            current_week_start - timedelta(days=1),
        )
    raise ValueError("submission coverage requires a week period")


def _resolve_formal_team(
    teams: tuple[TeamRecord, ...],
    *,
    scope_name: str,
) -> TeamRecord:
    requested = _normalized_name(scope_name)
    candidates = tuple(
        team
        for team in teams
        if team.name in FORMAL_CHILD_TEAM_NAMES
        and team.department_name == FORMAL_PARENT_DEPARTMENT
        and _normalized_name(team.name) == requested
    )
    if not candidates:
        raise SubmissionCoverageNotFound("formal legal team not found")
    if len(candidates) != 1:
        raise SubmissionCoverageAmbiguous("formal legal team is ambiguous")
    return candidates[0]


def _coverage_facts(
    *,
    team: TeamRecord,
    records: Any,
    start_date: date,
    end_date: date,
    period_type: SubmissionCoveragePeriod,
    now: datetime,
) -> dict[str, Any]:
    period_dates = _dates_between(start_date, end_date)
    membership_rows = _membership_rows(
        records.members,
        team_ref=team.ref,
    )
    members = _unique_member_identities(membership_rows)
    member_by_ref = {member.ref: member for member in members}
    obligations = _unique_obligations(
        records.obligations,
        team_ref=team.ref,
        start_date=start_date,
        end_date=end_date,
    )
    reports = _unique_reports(
        records.reports,
        team_ref=team.ref,
        start_date=start_date,
        end_date=end_date,
    )
    invalid_record_keys = {
        key
        for key in (*obligations.keys(), *reports.keys())
        if key[0] not in member_by_ref
        or not _member_active_on(
            membership_rows,
            member_ref=key[0],
            report_date=key[1],
        )
    }
    if invalid_record_keys:
        raise SubmissionCoverageDataError(
            "submission coverage contains a record without period membership"
        )

    daily_by_date = {
        report_date: {
            "report_date": report_date.isoformat(),
            "responsibility_data_complete": True,
            "completed_members": [],
            "pending_confirmation_members": [],
            "partial_members": [],
            "not_filled_members": [],
            "exempt_members": [],
            "responsibility_unknown_members": [],
        }
        for report_date in period_dates
    }
    member_by_id = {
        member.ref: {
            "member_name": member.name,
            "team_name": team.name,
            "completed_dates": [],
            "pending_confirmation_dates": [],
            "partial_dates": [],
            "not_filled_dates": [],
            "exempt_dates": [],
            "responsibility_unknown_dates": [],
        }
        for member in members
    }
    counts = {
        "completed_count": 0,
        "pending_confirmation_count": 0,
        "partial_count": 0,
        "not_filled_count": 0,
        "exempt_count": 0,
        "responsibility_unknown_count": 0,
        "overdue_count": 0,
        "not_yet_due_count": 0,
        "responsibility_known_count": 0,
        "expected_known_count": 0,
    }

    for report_date in period_dates:
        daily = daily_by_date[report_date]
        active_members = tuple(
            member
            for member in members
            if _member_active_on(
                membership_rows,
                member_ref=member.ref,
                report_date=report_date,
            )
        )
        for member in active_members:
            key = (member.ref, report_date)
            obligation = obligations.get(key)
            if not _member_day_in_submission_scope(
                report_date=report_date,
                obligation=obligation,
            ):
                continue
            report = reports.get(key)
            state, _status_label, _confirmation = classify_submission(
                obligation=obligation,
                report=report,
                now=now,
            )
            person = member_by_id[member.ref]
            if obligation is None or not obligation.data_complete:
                counts["responsibility_unknown_count"] += 1
                daily["responsibility_data_complete"] = False
                daily["responsibility_unknown_members"].append(
                    {"member_name": member.name}
                )
                person["responsibility_unknown_dates"].append(
                    report_date.isoformat()
                )
                continue

            counts["responsibility_known_count"] += 1
            if obligation.required:
                counts["expected_known_count"] += 1
            if not obligation.required:
                counts["exempt_count"] += 1
                exempt_reason = obligation.reason or "当天无需提交"
                exempt_fact = {
                    "member_name": member.name,
                    "reason": exempt_reason,
                }
                daily["exempt_members"].append(exempt_fact)
                person["exempt_dates"].append(
                    {
                        "report_date": report_date.isoformat(),
                        "reason": exempt_reason,
                    }
                )
                continue
            if state == "submitted":
                counts["completed_count"] += 1
                daily["completed_members"].append(
                    {
                        "member_name": member.name,
                        "submission_state": state,
                    }
                )
                person["completed_dates"].append(report_date.isoformat())
                continue
            if state == "pending_confirmation":
                counts["pending_confirmation_count"] += 1
                daily["pending_confirmation_members"].append(
                    {
                        "member_name": member.name,
                        "submission_state": state,
                    }
                )
                person["pending_confirmation_dates"].append(
                    report_date.isoformat()
                )
                continue
            if _report_has_content(report):
                counts["partial_count"] += 1
                daily["partial_members"].append(
                    {
                        "member_name": member.name,
                        "submission_state": "partial",
                    }
                )
                person["partial_dates"].append(report_date.isoformat())
                continue
            if state not in {"overdue", "not_yet_due"}:
                raise SubmissionCoverageDataError(
                    "submission coverage contains an unsupported state"
                )
            counts["not_filled_count"] += 1
            counts[f"{state}_count"] += 1
            missing_fact = {
                "member_name": member.name,
                "submission_state": state,
            }
            daily["not_filled_members"].append(missing_fact)
            person["not_filled_dates"].append(
                {
                    "report_date": report_date.isoformat(),
                    "submission_state": state,
                }
            )

    member_day_count = sum(
        1
        for report_date in period_dates
        for member in members
        if _member_active_on(
            membership_rows,
            member_ref=member.ref,
            report_date=report_date,
        )
        and _member_day_in_submission_scope(
            report_date=report_date,
            obligation=obligations.get((member.ref, report_date)),
        )
    )
    responsibility_data_complete = (
        counts["responsibility_unknown_count"] == 0
    )
    report_rows = tuple(reports.values())
    return {
        "query_kind": "submission_coverage",
        "scope_type": "team",
        "scope_label": team.name,
        "period_type": period_type,
        "period_start": start_date.isoformat(),
        "period_end": end_date.isoformat(),
        "default_reporting_dates": [
            value.isoformat() for value in period_dates if value.weekday() < 5
        ],
        "default_non_reporting_dates": [
            value.isoformat() for value in period_dates if value.weekday() >= 5
        ],
        "queried_at": now.astimezone(
            ZoneInfo("Asia/Shanghai")
        ).isoformat(),
        "count_unit": "member_day",
        "member_day_count": member_day_count,
        "responsibility_data_complete": responsibility_data_complete,
        "responsibility_known_count": counts["responsibility_known_count"],
        "expected_count": (
            counts["expected_known_count"]
            if responsibility_data_complete
            else None
        ),
        "expected_known_count": counts["expected_known_count"],
        "reporter_count": len({report.member_ref for report in report_rows}),
        "report_count": len(report_rows),
        **counts,
        "daily_breakdown": list(daily_by_date.values()),
        "member_breakdown": list(member_by_id.values()),
        "permission_allowed": True,
    }


def _dates_between(start_date: date, end_date: date) -> tuple[date, ...]:
    return tuple(
        start_date + timedelta(days=offset)
        for offset in range((end_date - start_date).days + 1)
    )


def _membership_rows(
    members: tuple[MemberRecord, ...],
    *,
    team_ref: str,
) -> tuple[MemberRecord, ...]:
    selected = tuple(
        sorted(
            (member for member in members if member.team_ref == team_ref),
            key=lambda item: (
                item.name,
                item.ref,
                item.effective_from or date.min,
                item.effective_to or date.max,
            ),
        )
    )
    if any(not member.membership_unambiguous for member in selected):
        raise SubmissionCoverageDataError(
            "submission coverage contains overlapping memberships"
        )
    return selected


def _unique_member_identities(
    membership_rows: tuple[MemberRecord, ...],
) -> tuple[MemberRecord, ...]:
    result: dict[str, MemberRecord] = {}
    for member in membership_rows:
        existing = result.get(member.ref)
        if existing is not None and (
            existing.name != member.name
            or existing.team_ref != member.team_ref
            or existing.team_name != member.team_name
            or existing.department_name != member.department_name
            or existing.team_code != member.team_code
        ):
            raise SubmissionCoverageDataError(
                "submission coverage contains conflicting memberships"
            )
        result.setdefault(member.ref, member)
    return tuple(
        sorted(result.values(), key=lambda item: (item.name, item.ref))
    )


def _member_active_on(
    membership_rows: tuple[MemberRecord, ...],
    *,
    member_ref: str,
    report_date: date,
) -> bool:
    return any(
        member.ref == member_ref
        and (
            member.effective_from is None
            or member.effective_from <= report_date
        )
        and (
            member.effective_to is None
            or member.effective_to >= report_date
        )
        for member in membership_rows
    )


def _unique_obligations(
    obligations: tuple[SubmissionObligation, ...],
    *,
    team_ref: str,
    start_date: date,
    end_date: date,
) -> dict[tuple[str, date], SubmissionObligation]:
    return _unique_period_records(
        obligations,
        team_ref=team_ref,
        start_date=start_date,
        end_date=end_date,
        record_label="responsibility",
    )


def _unique_reports(
    reports: tuple[DailyReportRecord, ...],
    *,
    team_ref: str,
    start_date: date,
    end_date: date,
) -> dict[tuple[str, date], DailyReportRecord]:
    return _unique_period_records(
        reports,
        team_ref=team_ref,
        start_date=start_date,
        end_date=end_date,
        record_label="report",
    )


def _unique_period_records(
    records: tuple[Any, ...],
    *,
    team_ref: str,
    start_date: date,
    end_date: date,
    record_label: str,
) -> dict[tuple[str, date], Any]:
    result: dict[tuple[str, date], Any] = {}
    for item in records:
        if (
            item.team_ref != team_ref
            or item.report_date < start_date
            or item.report_date > end_date
        ):
            continue
        key = (item.member_ref, item.report_date)
        if key in result:
            raise SubmissionCoverageDataError(
                f"submission coverage contains duplicate {record_label} records"
            )
        result[key] = item
    return result


def _report_has_content(report: DailyReportRecord | None) -> bool:
    if report is None:
        return False
    assessment = assess_daily_report_completeness(
        today_work=report.today_work,
        problems=report.problems,
        tomorrow_plan=report.tomorrow_plan,
        section_status=report.section_status,
    )
    return assessment.completeness_score > 0


def _member_day_in_submission_scope(
    *,
    report_date: date,
    obligation: SubmissionObligation | None,
) -> bool:
    """Match the producer: weekdays are reporting days unless a saved fact says more."""

    return report_date.weekday() < 5 or obligation is not None


def _normalized_name(value: str) -> str:
    return "".join(
        unicodedata.normalize("NFKC", str(value or ""))
        .strip()
        .casefold()
        .split()
    )
