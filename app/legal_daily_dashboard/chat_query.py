from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

from app.legal_daily_dashboard.domain import (
    DailyReportRecord,
    DashboardActor,
    DashboardRecords,
    MemberRecord,
    TeamRecord,
)
from app.legal_daily_dashboard.repository import DashboardRepository
from app.legal_daily_dashboard.service import (
    DashboardNotFound,
    classify_submission,
    overview_metrics,
    records_for_team,
)


ManagedDailyView = Literal[
    "member_report",
    "team_reports",
    "missing_submissions",
    "department_summary",
]


@dataclass(frozen=True)
class ManagedDailyQueryRequest:
    view: ManagedDailyView
    report_date: date
    member_name: str | None = None
    team_name: str | None = None


class ManagedDailyQueryAmbiguous(LookupError):
    def __init__(self, candidates: tuple[dict[str, str], ...]) -> None:
        super().__init__("managed daily target is ambiguous")
        self.candidates = candidates


class ManagedDailyQuery:
    """Resolve exact daily-report reads within the authenticated tenant."""

    def __init__(self, repository: DashboardRepository) -> None:
        self._repository = repository

    async def execute(
        self,
        *,
        actor: DashboardActor,
        request: ManagedDailyQueryRequest,
        now: datetime,
    ) -> dict[str, Any]:
        visible_teams = await self._repository.list_member_teams(
            tenant_id=actor.tenant_id,
            on_date=request.report_date,
        )
        if request.view == "department_summary":
            if (
                request.member_name is not None
                or request.team_name is not None
            ):
                raise DashboardNotFound(
                    "department summary not found"
                )
        records = await self._repository.load_records(
            tenant_id=actor.tenant_id,
            team_refs=None,
            start_date=request.report_date,
            end_date=request.report_date,
        )
        query_scope_teams = _query_scope_teams(
            visible_teams,
            records.members,
        )
        if request.view == "department_summary":
            return self._department_summary(
                report_date=request.report_date,
                records=records,
                teams=visible_teams,
                now=now,
            )
        if request.view == "team_reports":
            team = _resolve_required_team(
                query_scope_teams,
                request.team_name,
            )
            return self._team_reports(
                request=request,
                team=team,
                records=records,
                now=now,
            )
        if request.view == "missing_submissions":
            selected_team = _resolve_optional_team(
                query_scope_teams,
                request.team_name,
            )
            return self._missing_submissions(
                request=request,
                records=records,
                query_scope_teams=query_scope_teams,
                selected_team=selected_team,
                now=now,
            )
        if request.view != "member_report" or not request.member_name:
            raise ValueError("unsupported managed daily query")
        team = _resolve_optional_team(
            query_scope_teams,
            request.team_name,
        )
        candidates = tuple(
            member
            for member in records.members
            if _same_name(member.name, request.member_name)
            and (team is None or member.team_ref == team.ref)
        )
        if not candidates:
            raise DashboardNotFound("member not found")
        if len(candidates) > 1:
            teams_by_ref = {
                item.ref: item for item in query_scope_teams
            }
            raise ManagedDailyQueryAmbiguous(
                tuple(
                    {
                        "member_name": member.name,
                        **_team_candidate(
                            teams_by_ref.get(member.team_ref)
                        ),
                    }
                    for member in candidates
                )
            )
        member = candidates[0]
        team_by_ref = {item.ref: item for item in query_scope_teams}
        member_team = team_by_ref.get(member.team_ref)
        if member_team is None:
            raise DashboardNotFound("member not found")
        obligation = next(
            (
                item
                for item in records.obligations
                if item.member_ref == member.ref
                and item.report_date == request.report_date
            ),
            None,
        )
        report = next(
            (
                item
                for item in records.reports
                if item.member_ref == member.ref
                and item.report_date == request.report_date
            ),
            None,
        )
        _, status_label, confirmation_label = (
            classify_submission(
                obligation=obligation,
                report=report,
                now=now,
            )
        )
        return {
            "query_kind": "member_report",
            "report_date": request.report_date.isoformat(),
            "member": {
                "name": member.name,
                **_team_candidate(member_team),
            },
            "submission": {
                "status": status_label,
                "submitted_at": (
                    _local_datetime_iso(
                        report.submitted_at,
                        now=now,
                    )
                    if report is not None
                    and report.submitted_at is not None
                    else None
                ),
                "confirmation": confirmation_label,
            },
            "report": (
                _safe_report_content(report)
            ),
        }

    def _team_reports(
        self,
        *,
        request: ManagedDailyQueryRequest,
        team: TeamRecord,
        records: DashboardRecords,
        now: datetime,
    ) -> dict[str, Any]:
        obligation_by_member = {
            item.member_ref: item
            for item in records.obligations
            if item.team_ref == team.ref
            and item.report_date == request.report_date
        }
        report_by_member = {
            item.member_ref: item
            for item in records.reports
            if item.team_ref == team.ref
            and item.report_date == request.report_date
        }
        members = []
        for member in records.members:
            if member.team_ref != team.ref:
                continue
            report = report_by_member.get(member.ref)
            _, status_label, confirmation_label = (
                classify_submission(
                    obligation=obligation_by_member.get(
                        member.ref
                    ),
                    report=report,
                    now=now,
                )
            )
            members.append(
                {
                    "name": member.name,
                    "status": status_label,
                    "submitted_at": (
                        _local_datetime_iso(
                            report.submitted_at,
                            now=now,
                        )
                        if report is not None
                        and report.submitted_at is not None
                        else None
                    ),
                    "confirmation": confirmation_label,
                    "report": _safe_report_content(report),
                }
            )
        return {
            "query_kind": "team_reports",
            "report_date": request.report_date.isoformat(),
            **_team_candidate(team),
            "members": members,
        }

    @staticmethod
    def _department_summary(
        *,
        report_date: date,
        records: DashboardRecords,
        teams: tuple[TeamRecord, ...],
        now: datetime,
    ) -> dict[str, Any]:
        metrics = overview_metrics(
            records=records,
            now=now,
        )
        return {
            "query_kind": "department_summary",
            "report_date": report_date.isoformat(),
            "metrics": _summary_metrics(metrics),
            "teams": [
                {
                    **_team_candidate(team),
                    **_summary_metrics(
                        overview_metrics(
                            records=records_for_team(
                                records,
                                team.ref,
                            ),
                            now=now,
                        )
                    ),
                }
                for team in teams
            ],
        }

    @staticmethod
    def _missing_submissions(
        *,
        request: ManagedDailyQueryRequest,
        records: DashboardRecords,
        query_scope_teams: tuple[TeamRecord, ...],
        selected_team: TeamRecord | None,
        now: datetime,
    ) -> dict[str, Any]:
        team_by_ref = {team.ref: team for team in query_scope_teams}
        selected_team_refs = (
            {selected_team.ref}
            if selected_team is not None
            else None
        )
        obligation_by_member = {
            item.member_ref: item
            for item in records.obligations
            if item.report_date == request.report_date
        }
        report_by_member = {
            item.member_ref: item
            for item in records.reports
            if item.report_date == request.report_date
        }
        grouped: dict[str, list[dict[str, str]]] = {
            "completed": [],
            "partial": [],
            "not_filled": [],
            "overdue": [],
            "not_yet_due": [],
            "responsibility_unknown": [],
            "exempt": [],
        }
        for member in records.members:
            if (
                selected_team_refs is not None
                and member.team_ref not in selected_team_refs
            ):
                continue
            report = report_by_member.get(member.ref)
            state, _, confirmation = classify_submission(
                obligation=obligation_by_member.get(member.ref),
                report=report,
                now=now,
            )
            team = team_by_ref.get(member.team_ref)
            fact = {
                "name": member.name,
                "team_name": (
                    team.name if team is not None else "未分组"
                ),
            }
            if state in {"submitted", "pending_confirmation"}:
                grouped["completed"].append(fact)
            elif _report_has_content(report):
                grouped["partial"].append(fact)
            elif state in {"overdue", "not_yet_due"}:
                grouped["not_filled"].append(fact)
            if state not in grouped:
                continue
            if state == "exempt":
                fact["reason"] = confirmation
            grouped[state].append(fact)
        return {
            "query_kind": "missing_submissions",
            "report_date": request.report_date.isoformat(),
            "scope_name": (
                _team_display_label(selected_team)
                if selected_team is not None
                else _department_scope_name(query_scope_teams)
            ),
            "responsibility_data_complete": not grouped[
                "responsibility_unknown"
            ],
            "confirmed_missing_members": grouped["overdue"],
            "overdue_members": grouped["overdue"],
            "not_yet_due_members": grouped["not_yet_due"],
            "responsibility_unknown_members": grouped[
                "responsibility_unknown"
            ],
            "exempt_members": grouped["exempt"],
            "completed_members": grouped["completed"],
            "partial_members": grouped["partial"],
            "not_filled_members": grouped["not_filled"],
        }


def _query_scope_teams(
    visible_teams: tuple[TeamRecord, ...],
    members: tuple[MemberRecord, ...],
) -> tuple[TeamRecord, ...]:
    """Add factual non-subdepartment scopes without inventing an eighth team."""

    result = list(visible_teams)
    known_refs = {team.ref for team in visible_teams}
    common_department = _department_scope_name(visible_teams)
    for member in members:
        if member.team_ref in known_refs:
            continue
        raw_name = member.team_name.strip()
        department_name = (
            member.department_name.strip()
            or (
                common_department
                if common_department != "全部人员"
                else ""
            )
        )
        team_name = (
            "中心直属"
            if _is_center_direct_scope(
                team_name=raw_name,
                department_name=department_name,
                team_code=member.team_code,
            )
            else (raw_name or "未分组")
        )
        result.append(
            TeamRecord(
                ref=member.team_ref,
                name=team_name,
                department_name=department_name,
                code=member.team_code,
            )
        )
        known_refs.add(member.team_ref)
    return tuple(result)


def _is_center_direct_scope(
    *,
    team_name: str,
    department_name: str,
    team_code: str,
) -> bool:
    normalized_code = team_code.strip().casefold()
    if normalized_code == "legal-center":
        return True
    if not department_name:
        return False
    return team_name in {
        department_name,
        f"{department_name}（中心层级）",
    }


def _local_datetime_iso(
    value: datetime,
    *,
    now: datetime,
) -> str:
    """Expose timestamps in the authenticated query timezone when available."""

    if value.tzinfo is None or now.tzinfo is None:
        return value.isoformat()
    return value.astimezone(now.tzinfo).isoformat()


def _department_scope_name(
    teams: tuple[TeamRecord, ...],
) -> str:
    names = tuple(
        dict.fromkeys(
            team.department_name.strip()
            for team in teams
            if team.department_name.strip()
        )
    )
    return names[0] if len(names) == 1 else "全部人员"


def _resolve_optional_team(
    teams: tuple[TeamRecord, ...],
    team_name: str | None,
) -> TeamRecord | None:
    if not team_name:
        return None
    candidates = tuple(
        team
        for team in teams
        if _matches_team_name(team.name, team_name)
        or _same_name(_team_display_label(team), team_name)
        or _same_name(_team_label(team), team_name)
    )
    if not candidates:
        raise DashboardNotFound("team not found")
    if len(candidates) > 1:
        raise ManagedDailyQueryAmbiguous(
            tuple(_team_candidate(team) for team in candidates)
        )
    return candidates[0]


def _resolve_required_team(
    teams: tuple[TeamRecord, ...],
    team_name: str | None,
) -> TeamRecord:
    if team_name:
        team = _resolve_optional_team(teams, team_name)
        if team is None:
            raise DashboardNotFound("team not found")
        return team
    if len(teams) == 1:
        return teams[0]
    if not teams:
        raise DashboardNotFound("team not found")
    raise ManagedDailyQueryAmbiguous(
        tuple(_team_candidate(team) for team in teams)
    )


def _safe_report_content(
    report: DailyReportRecord | None,
) -> dict[str, list[str]] | None:
    if report is None:
        return None
    return {
        "today_work": list(report.today_work),
        "problems": list(report.problems),
        "tomorrow_plan": list(report.tomorrow_plan),
    }


def _report_has_content(report: DailyReportRecord | None) -> bool:
    if report is None:
        return False
    return any(
        str(item).strip()
        for values in (
            report.today_work,
            report.problems,
            report.tomorrow_plan,
        )
        for item in values
    )


def _team_candidate(
    team: TeamRecord | None,
) -> dict[str, str]:
    if team is None:
        return {"team_name": ""}
    candidate = {"team_name": team.name}
    # Safe user-facing facts may include the business hierarchy, but not
    # internal storage codes such as ``monthly-admin`` or ``team-01``.
    label = _team_display_label(team)
    if label != team.name:
        candidate["team_label"] = label
    return candidate


def _team_display_label(team: TeamRecord) -> str:
    parts = [
        value.strip()
        for value in (team.department_name, team.name)
        if value.strip()
    ]
    return " / ".join(dict.fromkeys(parts)) or team.name


def _team_label(team: TeamRecord) -> str:
    parts = [
        value.strip()
        for value in (team.department_name, team.name, team.code)
        if value.strip()
    ]
    return " / ".join(dict.fromkeys(parts)) or team.name


def _summary_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    return {
        "responsibility_data_complete": metrics[
            "responsibility_data_complete"
        ],
        "expected_count": metrics["expected_count"],
        "submitted_count": metrics["submitted_count"],
        "pending_confirmation_count": metrics[
            "pending_confirmation_count"
        ],
        "overdue_count": metrics["overdue_count"],
        "unknown_responsibility_count": metrics[
            "unknown_responsibility_count"
        ],
    }


def _same_name(left: str, right: str) -> bool:
    return _normalized_name(left) == _normalized_name(right)


def _matches_team_name(official_name: str, requested_name: str) -> bool:
    official = _normalized_name(official_name).replace(" ", "")
    requested = _normalized_name(requested_name).replace(" ", "")
    if not official or not requested:
        return False
    if official == requested:
        return True
    # Accept a conservative organizational abbreviation such as
    # “综合部” -> “综合管理部”. The resolver still rejects the match when
    # more than one visible team shares that prefix (for example “法务部”).
    for suffix in ("中心", "部门", "部", "团队", "组", "室", "科", "处"):
        if not requested.endswith(suffix) or not official.endswith(suffix):
            continue
        prefix = requested[: -len(suffix)]
        if len(prefix) >= 2 and official.startswith(prefix):
            return True
    return False


def _normalized_name(value: str) -> str:
    return " ".join(
        unicodedata.normalize("NFKC", value).strip().casefold().split()
    )
