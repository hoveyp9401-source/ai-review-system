from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.legal_daily_roster import (
    FORMAL_ROSTER_EFFECTIVE_DATE,
    FormalLegalDailyRoster,
    load_formal_legal_daily_roster,
)
from app.legal_daily_dashboard.domain import (
    DailyReportRecord,
    DashboardRecords,
    MemberRecord,
    TeamRecord,
)
from app.legal_daily_dashboard.service import (
    classify_submission,
    management_review_actions,
    overview_metrics,
    records_for_team,
)
from app.legal_daily_dashboard.sql_repository import SqlDashboardRepository
from app.services.report_risk import problems_acknowledged_empty
from app.utils.time import now_in_timezone

SEPARATOR = "────────────"
HIDDEN_MISSING_DETAIL_MEMBER_NAME = "赵卫中"


@dataclass(frozen=True)
class BriefingRecipient:
    id: str
    name: str
    dingtalk_user_id: str
    role: str
    team_ref: str | None = None


@dataclass(frozen=True)
class TeamSubmissionView:
    team: TeamRecord
    members: tuple[MemberRecord, ...]
    submitted_reports: tuple[DailyReportRecord, ...]
    missing_members: tuple[MemberRecord, ...]
    unknown_members: tuple[MemberRecord, ...]
    exempt_members: tuple[MemberRecord, ...]
    expected_count: int

    @property
    def has_responsibility_data(self) -> bool:
        return bool(self.members or self.expected_count)

    @property
    def submitted_count(self) -> int:
        return len(self.submitted_reports)


class ManagementDailyBriefingService:
    """Build management briefings from the authoritative dashboard projection."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def build(
        self,
        session: AsyncSession,
        report_date: date,
    ) -> dict[str, Any]:
        tenant_id = str(
            getattr(self._settings, "legal_daily_dashboard_tenant_id", "") or ""
        ).strip()
        if not tenant_id:
            raise ValueError("legal_daily_dashboard_tenant_id is required")

        formal_roster = await load_formal_legal_daily_roster(
            session,
            tenant_id=tenant_id,
            on_date=report_date,
        )
        hidden_missing_detail_member_refs = _hidden_missing_detail_member_refs(
            self._settings,
            formal_roster=formal_roster,
        )
        repository = SqlDashboardRepository(session)
        teams = await repository.list_teams(
            tenant_id=tenant_id,
            on_date=report_date,
        )
        records = await repository.load_records(
            tenant_id=tenant_id,
            team_refs=None,
            start_date=report_date,
            end_date=report_date,
        )
        _validate_briefing_roster(records, formal_roster=formal_roster)
        recipients, recipient_warnings = await _load_recipients(
            session,
            tenant_id=tenant_id,
            report_date=report_date,
        )
        cc_recipients, cc_warnings = await _load_department_cc_recipients(
            session,
            configured_identifiers=_configured_identifiers(
                getattr(
                    self._settings,
                    "management_daily_briefing_department_cc_user_ids",
                    "",
                )
            ),
        )
        return build_management_daily_briefings(
            report_date=report_date,
            now=now_in_timezone(self._settings.timezone),
            teams=teams,
            records=records,
            recipients=(*recipients, *cc_recipients),
            recipient_warnings=(*recipient_warnings, *cc_warnings),
            hidden_missing_detail_member_refs=hidden_missing_detail_member_refs,
        )


def _validate_briefing_roster(
    records: DashboardRecords,
    *,
    formal_roster: FormalLegalDailyRoster,
) -> None:
    record_members = {
        member.ref: (member.name, member.team_ref)
        for member in records.members
    }
    formal_members = {
        member.user_id: (member.user_name, member.team_id)
        for member in formal_roster.members
    }
    if len(record_members) != len(records.members) or record_members != formal_members:
        raise RuntimeError(
            "management briefing members and teams do not exactly match the formal roster"
        )
    if formal_roster.on_date < FORMAL_ROSTER_EFFECTIVE_DATE:
        return
    obligations = tuple(
        obligation
        for obligation in records.obligations
        if obligation.report_date == formal_roster.on_date
    )
    obligation_scope = {
        obligation.member_ref: (obligation.team_ref, obligation.data_complete)
        for obligation in obligations
    }
    expected_obligation_scope = {
        member.user_id: (member.team_id, True)
        for member in formal_roster.members
    }
    if (
        len(obligation_scope) != len(obligations)
        or obligation_scope != expected_obligation_scope
    ):
        raise RuntimeError(
            "management briefing obligations do not exactly match the formal roster"
        )


def build_management_daily_briefings(
    *,
    report_date: date,
    now: datetime,
    teams: tuple[TeamRecord, ...],
    records: DashboardRecords,
    recipients: tuple[BriefingRecipient, ...],
    recipient_warnings: tuple[str, ...] = (),
    hidden_missing_detail_member_refs: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    ordered_teams = tuple(sorted(teams, key=_team_sort_key))
    team_views = tuple(
        _build_team_submission_view(
            team=team,
            records=records_for_team(records, team.ref),
            report_date=report_date,
            now=now,
        )
        for team in ordered_teams
    )
    department_direct_view = _build_department_direct_submission_view(
        teams=ordered_teams,
        records=records,
        report_date=report_date,
        now=now,
    )
    team_messages: list[dict[str, Any]] = []
    for view in team_views:
        team_recipients = tuple(
            recipient
            for recipient in recipients
            if recipient.role == "team_lead" and recipient.team_ref == view.team.ref
        )
        if not view.has_responsibility_data:
            continue
        team_records = records_for_team(records, view.team.ref)
        team_messages.append(
            {
                "scope": "team",
                "team_id": view.team.ref,
                "team_name": view.team.name,
                "recipients": _serialize_recipients(team_recipients),
                "target_count": len(team_recipients),
                "stats": _serialize_submission_stats(view),
                "briefing_snapshot": _serialize_briefing_snapshot(
                    report_date=report_date,
                    generated_at=now,
                    scope="team",
                    views=(view,),
                ),
                "text": build_team_management_briefing_text(
                    report_date=report_date,
                    now=now,
                    view=view,
                    records=team_records,
                ),
            }
        )

    legal_head_recipients = _department_briefing_recipients(recipients)
    department_message = {
        "scope": "department",
        "department_name": _department_name(teams),
        "recipients": _serialize_recipients(legal_head_recipients),
        "target_count": len(legal_head_recipients),
        "stats": _aggregate_submission_stats(
            team_views,
            department_direct_view=department_direct_view,
        ),
        "briefing_snapshot": _serialize_briefing_snapshot(
            report_date=report_date,
            generated_at=now,
            scope="department",
            views=(
                *team_views,
                *((department_direct_view,) if department_direct_view else ()),
            ),
        ),
        "text": build_department_management_briefing_text(
            report_date=report_date,
            now=now,
            team_views=team_views,
            department_direct_view=department_direct_view,
            records=records,
            hidden_missing_detail_member_refs=hidden_missing_detail_member_refs,
        ),
    }
    return {
        "date": report_date.isoformat(),
        "team_messages": team_messages,
        "team_detail_messages": [],
        "department_message": department_message,
        "department_detail_message": None,
        "recipient_warnings": list(recipient_warnings),
    }


def build_team_management_briefing_text(
    *,
    report_date: date,
    now: datetime,
    view: TeamSubmissionView,
    records: DashboardRecords,
) -> str:
    submitted_report_refs = {report.ref for report in view.submitted_reports}
    review_actions = management_review_actions(
        records=records,
        teams={view.team.ref: view.team.name},
        report_date=report_date,
        now=now,
    )
    attention = _attention_entries(
        records=records,
        member_names=_member_names(records.members),
        allowed_report_refs=submitted_report_refs,
        review_actions=review_actions,
        include_team=False,
        team_name=view.team.name,
    )
    progress = _report_section_entries(
        records=records,
        field="today_work",
        allowed_report_refs=submitted_report_refs,
        include_team=False,
        team_name=view.team.name,
    )
    plans = _report_section_entries(
        records=records,
        field="tomorrow_plan",
        allowed_report_refs=submitted_report_refs,
        include_team=False,
        team_name=view.team.name,
    )
    quality = _quality_entries(
        review_actions=review_actions,
        include_team=False,
        team_name=view.team.name,
    )
    blocks = [
        f"**【{view.team.name}】{_display_date(report_date)}日报简报**",
        "\n".join(
            (
                "**填报概览**",
                _submission_summary_line(view),
                _missing_line(view.missing_members),
                _unknown_line(view.unknown_members),
                _exempt_line(view.exempt_members),
            )
        ).rstrip(),
        _numbered_section(
            "一、需要负责人关注",
            attention,
            "本期未发现需要负责人额外关注的已记录事项。",
        ),
        _numbered_section(
            "二、昨日关键进展",
            progress,
            "本期暂无已提交的工作进展。",
        ),
        _numbered_section(
            "三、今日重点计划",
            plans,
            "本期暂无已提交的后续计划。",
        ),
        _numbered_section(
            "四、填报质量提示（AI辅助判断）",
            quality,
            "本期未发现已生成的质量复核提示。",
        ),
        "\n".join(
            (
                "**快捷查询**",
                "看具体人员日报｜看未交名单｜看全员明细",
            )
        ),
    ]
    return f"\n\n{SEPARATOR}\n\n".join(blocks)


def build_department_management_briefing_text(
    *,
    report_date: date,
    now: datetime,
    team_views: tuple[TeamSubmissionView, ...],
    department_direct_view: TeamSubmissionView | None,
    records: DashboardRecords,
    hidden_missing_detail_member_refs: frozenset[str] = frozenset(),
) -> str:
    department_views = (
        (*team_views, department_direct_view)
        if department_direct_view is not None
        else team_views
    )
    member_names = _member_names(records.members)
    team_names = {view.team.ref: view.team.name for view in department_views}
    submitted_report_refs = {
        report.ref
        for view in department_views
        for report in view.submitted_reports
    }
    review_actions = management_review_actions(
        records=records,
        teams=team_names,
        report_date=report_date,
        now=now,
    )
    attention = _attention_entries(
        records=records,
        member_names=member_names,
        allowed_report_refs=submitted_report_refs,
        review_actions=review_actions,
        include_team=True,
        team_name="",
        team_names=team_names,
    )
    progress = _report_section_entries(
        records=records,
        field="today_work",
        allowed_report_refs=submitted_report_refs,
        include_team=True,
        team_name="",
        team_names=team_names,
    )
    plans = _report_section_entries(
        records=records,
        field="tomorrow_plan",
        allowed_report_refs=submitted_report_refs,
        include_team=True,
        team_name="",
        team_names=team_names,
    )
    quality = _quality_entries(
        review_actions=review_actions,
        include_team=True,
        team_name="",
        team_names=team_names,
    )
    known_views = tuple(
        view for view in department_views if view.has_responsibility_data
    )
    total_expected = sum(view.expected_count for view in known_views)
    total_submitted = sum(view.submitted_count for view in known_views)
    total_missing = sum(len(view.missing_members) for view in known_views)
    total_unknown = sum(len(view.unknown_members) for view in known_views)
    overview_lines = [
        "**填报概览**",
        (
            f"已交 {total_submitted}/{total_expected}"
            f"｜未交 {total_missing}"
            f"｜责任待核 {total_unknown}"
        ),
    ]
    team_entries = [_department_team_entry(view) for view in department_views]
    missing_entries = [
        _department_missing_entry(
            view,
            hidden_member_refs=hidden_missing_detail_member_refs,
        )
        for view in known_views
        if view.missing_members
    ]
    if total_unknown:
        unknown_names = "、".join(
            member.name for view in known_views for member in view.unknown_members
        )
        overview_lines.append(f"责任待核：{unknown_names}（不计入未交）")

    blocks = [
        (
            f"**【{_department_name(tuple(view.team for view in team_views))}】"
            f"{_display_date(report_date)}日报总览**"
        ),
        "\n".join(overview_lines),
        _numbered_section(
            "一、各团队及中心直属填报情况",
            team_entries,
            "本期没有可展示的正式团队。",
        ),
        _numbered_section(
            "二、未交人员",
            missing_entries,
            "已知应交人员均已提交。",
        ),
        _numbered_section(
            "三、需要部门负责人关注",
            attention,
            "本期未发现需要部门负责人额外关注的已记录事项。",
        ),
        _numbered_section(
            "四、昨日关键进展",
            progress,
            "本期暂无已提交的工作进展。",
        ),
        _numbered_section(
            "五、今日重点计划",
            plans,
            "本期暂无已提交的后续计划。",
        ),
        _numbered_section(
            "六、填报质量提示（AI辅助判断）",
            quality,
            "本期未发现已生成的质量复核提示。",
        ),
        "\n".join(
            (
                "**快捷查询**",
                "看具体团队日报｜看具体人员日报｜看全部未交人员｜看全员明细",
            )
        ),
    ]
    return f"\n\n{SEPARATOR}\n\n".join(blocks)


def _build_team_submission_view(
    *,
    team: TeamRecord,
    records: DashboardRecords,
    report_date: date,
    now: datetime,
) -> TeamSubmissionView:
    members_by_ref = {member.ref: member for member in records.members}
    metrics = overview_metrics(records=records, now=now)
    obligations = {
        obligation.member_ref: obligation
        for obligation in records.obligations
        if obligation.report_date == report_date
    }
    reports = {
        report.member_ref: report
        for report in records.reports
        if report.report_date == report_date
    }
    submitted_reports: list[DailyReportRecord] = []
    missing_members: list[MemberRecord] = []
    exempt_members: list[MemberRecord] = []
    unknown_members: list[MemberRecord] = []
    for member in records.members:
        obligation = obligations.get(member.ref)
        report = reports.get(member.ref)
        if obligation is None or not obligation.data_complete:
            unknown_members.append(member)
            continue
        if not obligation.required:
            exempt_members.append(member)
            continue
        state, _, _ = classify_submission(
            obligation=obligation,
            report=report,
            now=now,
        )
        if state in {"submitted", "pending_confirmation"} and report is not None:
            submitted_reports.append(report)
        else:
            missing_members.append(member)
    return TeamSubmissionView(
        team=team,
        members=tuple(members_by_ref.values()),
        submitted_reports=tuple(submitted_reports),
        missing_members=tuple(missing_members),
        unknown_members=tuple(unknown_members),
        exempt_members=tuple(exempt_members),
        expected_count=int(metrics["expected_known_count"] or 0),
    )


def _build_department_direct_submission_view(
    *,
    teams: tuple[TeamRecord, ...],
    records: DashboardRecords,
    report_date: date,
    now: datetime,
) -> TeamSubmissionView | None:
    """Build the department-only cohort without creating an eighth team message."""

    official_team_refs = {team.ref for team in teams}
    direct_team_refs = {
        member.team_ref
        for member in records.members
        if member.team_ref not in official_team_refs
    }
    if not direct_team_refs:
        return None
    if len(direct_team_refs) != 1:
        raise ValueError(
            "department-direct members must resolve to exactly one roster team"
        )
    direct_team_ref = next(iter(direct_team_refs))
    direct_records = records_for_team(records, direct_team_ref)
    direct_team = TeamRecord(
        ref=direct_team_ref,
        name="中心直属",
        department_name=_department_name(teams),
        code="department-direct",
    )
    return _build_team_submission_view(
        team=direct_team,
        records=direct_records,
        report_date=report_date,
        now=now,
    )


def _submission_summary_line(view: TeamSubmissionView) -> str:
    parts = [
        f"已交 {view.submitted_count}/{view.expected_count}",
        f"未交 {len(view.missing_members)}",
    ]
    if view.exempt_members:
        parts.append(f"免交 {len(view.exempt_members)}")
    return "｜".join(parts)


def _missing_line(members: tuple[MemberRecord, ...]) -> str:
    if not members:
        return "未交：无"
    return "未交：" + "、".join(member.name for member in members)


def _unknown_line(members: tuple[MemberRecord, ...]) -> str:
    if not members:
        return ""
    return (
        "责任待核：" + "、".join(member.name for member in members) + "（不计入未交）"
    )


def _exempt_line(members: tuple[MemberRecord, ...]) -> str:
    if not members:
        return ""
    return "免交：" + "、".join(member.name for member in members)


def _department_team_entry(
    view: TeamSubmissionView,
) -> tuple[str, str]:
    if not view.has_responsibility_data:
        return f"**{view.team.name}**", "成员名单待同步"
    detail = _submission_summary_line(view)
    if view.unknown_members:
        detail += f"｜责任待核 {len(view.unknown_members)}"
    return f"**{view.team.name}**", detail


def _department_missing_entry(
    view: TeamSubmissionView,
    *,
    hidden_member_refs: frozenset[str],
) -> tuple[str, str]:
    """Render missing names without changing the underlying submission totals."""

    visible_members = tuple(
        member
        for member in view.missing_members
        if member.ref not in hidden_member_refs
    )
    visible_names = "、".join(member.name for member in visible_members)
    return (
        f"**{view.team.name}（{len(view.missing_members)}人）**",
        visible_names,
    )


def _attention_entries(
    *,
    records: DashboardRecords,
    member_names: dict[str, str],
    allowed_report_refs: set[str],
    review_actions: tuple[dict[str, Any], ...],
    include_team: bool,
    team_name: str,
    team_names: dict[str, str] | None = None,
) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for report in _sorted_reports(
        records.reports,
        allowed_report_refs=allowed_report_refs,
    ):
        if problems_acknowledged_empty(report):
            continue
        name = member_names.get(report.member_ref, "成员")
        prefix = _entry_prefix(
            name=name,
            team_name=(
                (team_names or {}).get(report.team_ref, "")
                if include_team
                else team_name
            ),
            include_team=include_team,
        )
        for problem in report.problems:
            content = str(problem or "").strip()
            if not content:
                continue
            entries.append((f"**{prefix}｜问题/风险**", content))

    actions = sorted(
        (
            action
            for action in review_actions
            if action.get("target_type") == "work_item"
        ),
        key=lambda action: (
            str(action.get("team_name") or ""),
            str(action.get("member_name") or ""),
            str(action.get("title") or ""),
        ),
    )
    for action in actions:
        name = str(action.get("member_name") or "相关成员")
        prefix = _entry_prefix(
            name=name,
            team_name=(
                str(action.get("team_name") or "") if include_team else team_name
            ),
            include_team=include_team,
        )
        title = str(action.get("title") or "长期事项")
        body = str(
            action.get("reason")
            or action.get("support_needed")
            or "该事项需要负责人结合原始记录复核。"
        ).strip()
        entries.append(
            (
                f"**{prefix}｜{title}**",
                body,
            )
        )
    return entries


def _report_section_entries(
    *,
    records: DashboardRecords,
    field: str,
    allowed_report_refs: set[str],
    include_team: bool,
    team_name: str,
    team_names: dict[str, str] | None = None,
) -> list[tuple[str, str]]:
    member_names = _member_names(records.members)
    entries: list[tuple[str, str]] = []
    for report in _sorted_reports(
        records.reports,
        allowed_report_refs=allowed_report_refs,
    ):
        values = tuple(
            str(value or "").strip()
            for value in getattr(report, field)
            if str(value or "").strip()
        )
        if not values:
            continue
        prefix = _entry_prefix(
            name=member_names.get(report.member_ref, "成员"),
            team_name=(
                (team_names or {}).get(report.team_ref, "")
                if include_team
                else team_name
            ),
            include_team=include_team,
        )
        entries.append((f"**{prefix}**", "；".join(values)))
    return entries


def _quality_entries(
    *,
    review_actions: tuple[dict[str, Any], ...],
    include_team: bool,
    team_name: str,
    team_names: dict[str, str] | None = None,
) -> list[tuple[str, str]]:
    del team_names
    entries: list[tuple[str, str]] = []
    actions = sorted(
        (
            action
            for action in review_actions
            if action.get("target_type") == "review_suggestion"
        ),
        key=lambda action: (
            str(action.get("team_name") or ""),
            str(action.get("member_name") or ""),
            str(action.get("title") or ""),
        ),
    )
    for action in actions:
        name = str(action.get("member_name") or "成员")
        prefix = _entry_prefix(
            name=name,
            team_name=(
                str(action.get("team_name") or "") if include_team else team_name
            ),
            include_team=include_team,
        )
        evidence = next(
            (
                str(item.get("quote") or "").strip()
                for item in action.get("evidence", [])
                if str(item.get("quote") or "").strip()
            ),
            "",
        )
        body = str(action.get("reason") or "").strip()
        if evidence:
            body = f"{body}\n   原文：“{evidence}”"
        entries.append((f"**{prefix}｜质量复核**", body))
    return entries


def _numbered_section(
    title: str,
    entries: Iterable[tuple[str, str]],
    empty_text: str,
) -> str:
    lines = [f"**{title}**"]
    materialized = list(entries)
    if not materialized:
        lines.append(empty_text)
        return "\n\n".join(lines)
    for index, (heading, body) in enumerate(materialized, start=1):
        lines.append(
            f"{index}. {heading}\n   {body}"
            if body
            else f"{index}. {heading}"
        )
    return "\n\n".join(lines)


def _entry_prefix(
    *,
    name: str,
    team_name: str,
    include_team: bool,
) -> str:
    if include_team and team_name:
        return f"{team_name}｜{name}"
    return name


def _member_names(
    members: tuple[MemberRecord, ...],
) -> dict[str, str]:
    return {member.ref: member.name for member in members}


def _sorted_reports(
    reports: tuple[DailyReportRecord, ...],
    *,
    allowed_report_refs: set[str],
) -> tuple[DailyReportRecord, ...]:
    return tuple(
        sorted(
            (report for report in reports if report.ref in allowed_report_refs),
            key=lambda report: (
                report.team_ref,
                report.member_ref,
                report.ref,
            ),
        )
    )


def _serialize_submission_stats(
    view: TeamSubmissionView,
) -> dict[str, int]:
    return {
        "total": view.expected_count,
        "completed": view.submitted_count,
        "missing": len(view.missing_members),
        "unknown_responsibility": len(view.unknown_members),
        "exempt": len(view.exempt_members),
    }


def _serialize_briefing_snapshot(
    *,
    report_date: date,
    generated_at: datetime,
    scope: str,
    views: tuple[TeamSubmissionView, ...],
) -> dict[str, Any]:
    members = [
        _serialize_member_snapshot(
            view,
            member,
            generated_at=generated_at,
        )
        for view in views
        for member in view.members
    ]
    members.sort(
        key=lambda item: (
            str(item["team_name"]),
            str(item["member_name"]),
            str(item["member_ref"]),
        )
    )
    return {
        "generated_at": generated_at.isoformat(),
        "report_date": report_date.isoformat(),
        "scope": scope,
        "members": members,
    }


def _serialize_member_snapshot(
    view: TeamSubmissionView,
    member: MemberRecord,
    *,
    generated_at: datetime,
) -> dict[str, Any]:
    report = next(
        (
            item
            for item in view.submitted_reports
            if item.member_ref == member.ref
        ),
        None,
    )
    if report is not None:
        classification = (
            "submitted"
            if report.status == "completed"
            else "pending_confirmation"
        )
    elif any(item.ref == member.ref for item in view.missing_members):
        classification = "missing"
    elif any(item.ref == member.ref for item in view.unknown_members):
        classification = "responsibility_unknown"
    elif any(item.ref == member.ref for item in view.exempt_members):
        classification = "exempt"
    else:
        classification = "unknown"
    return {
        "member_ref": member.ref,
        "member_name": member.name,
        "team_ref": member.team_ref,
        "team_name": member.team_name or view.team.name,
        "classification": classification,
        "report_status": report.status if report is not None else None,
        "confirmation_type": (
            report.confirmation_type if report is not None else None
        ),
        "submitted_at": (
            _iso_in_generation_timezone(
                report.submitted_at,
                generated_at,
            )
            if report is not None and report.submitted_at is not None
            else None
        ),
    }


def _iso_in_generation_timezone(
    value: datetime,
    generated_at: datetime,
) -> str:
    """Show snapshot times on the same clock as the briefing generation."""

    if value.tzinfo is None or generated_at.tzinfo is None:
        # Do not let the machine's local timezone silently reinterpret legacy
        # naive values. Preserve those values exactly instead.
        return value.isoformat()
    return value.astimezone(generated_at.tzinfo).isoformat()


def _aggregate_submission_stats(
    views: tuple[TeamSubmissionView, ...],
    *,
    department_direct_view: TeamSubmissionView | None = None,
) -> dict[str, int]:
    department_views = (
        (*views, department_direct_view)
        if department_direct_view is not None
        else views
    )
    known = tuple(
        view for view in department_views if view.has_responsibility_data
    )
    return {
        "total": sum(view.expected_count for view in known),
        "completed": sum(view.submitted_count for view in known),
        "missing": sum(len(view.missing_members) for view in known),
        "unknown_responsibility": sum(len(view.unknown_members) for view in known),
        "exempt": sum(len(view.exempt_members) for view in known),
        "teams": len(views),
        "teams_with_responsibility_data": sum(
            view.has_responsibility_data for view in views
        ),
        "center_direct_members": (
            len(department_direct_view.members)
            if department_direct_view is not None
            else 0
        ),
    }


def _serialize_recipients(
    recipients: tuple[BriefingRecipient, ...],
) -> list[dict[str, str]]:
    return [
        {
            "id": recipient.id,
            "name": recipient.name,
            "dingtalk_user_id": recipient.dingtalk_user_id,
            "role": recipient.role,
        }
        for recipient in recipients
    ]


def _unique_recipients(
    recipients: Iterable[BriefingRecipient],
) -> tuple[BriefingRecipient, ...]:
    unique: dict[str, BriefingRecipient] = {}
    for recipient in recipients:
        unique.setdefault(recipient.id, recipient)
    return tuple(unique.values())


def _department_briefing_recipients(
    recipients: tuple[BriefingRecipient, ...],
) -> tuple[BriefingRecipient, ...]:
    cc_user_ids = {
        recipient.id for recipient in recipients if recipient.role == "department_cc"
    }
    return _unique_recipients(
        recipient
        for recipient in recipients
        if (recipient.role == "legal_head" and recipient.id not in cc_user_ids)
        or recipient.role == "department_cc"
    )


def _department_name(teams: tuple[TeamRecord, ...]) -> str:
    names = tuple(
        dict.fromkeys(
            team.department_name.strip()
            for team in teams
            if team.department_name.strip()
        )
    )
    if len(names) == 1:
        return names[0]
    return "法务合约中心"


def _team_sort_key(team: TeamRecord) -> tuple[int, int, str]:
    digits = "".join(character for character in team.code if character.isdigit())
    if digits:
        return 0, int(digits), team.name
    return 1, 0, team.name


def _display_date(value: date) -> str:
    return f"{value.month}月{value.day}日"


async def _load_recipients(
    session: AsyncSession,
    *,
    tenant_id: str,
    report_date: date,
) -> tuple[tuple[BriefingRecipient, ...], tuple[str, ...]]:
    result = await session.execute(
        text(
            """
            SELECT
                assignments.assignment_id::text AS assignment_id,
                assignments.dashboard_role,
                assignments.team_id::text AS team_ref,
                COALESCE(direct_users.id, fallback_users.id)::text AS user_id,
                COALESCE(direct_users.name, fallback_users.name) AS user_name,
                COALESCE(
                    direct_users.dingtalk_user_id,
                    fallback_users.dingtalk_user_id
                ) AS dingtalk_user_id,
                COALESCE(direct_users.active, fallback_users.active) AS active
            FROM legal_daily_access_assignments assignments
            LEFT JOIN users direct_users
              ON direct_users.id::text = assignments.principal_user_id
            LEFT JOIN users fallback_users
              ON direct_users.id IS NULL
             AND fallback_users.dingtalk_user_id =
                 assignments.principal_user_id
            WHERE assignments.tenant_id = :tenant_id
              AND assignments.active IS TRUE
              AND assignments.effective_from <= :report_date
              AND (
                  assignments.effective_to IS NULL
                  OR assignments.effective_to >= :report_date
              )
            ORDER BY
                assignments.assignment_id,
                CASE
                    WHEN direct_users.id IS NOT NULL THEN 0
                    ELSE 1
                END,
                COALESCE(direct_users.id, fallback_users.id)
            """
        ),
        {
            "tenant_id": tenant_id,
            "report_date": report_date,
        },
    )
    rows_by_assignment: dict[str, list[Any]] = {}
    for row in result.mappings().all():
        rows_by_assignment.setdefault(
            str(row.get("assignment_id") or ""),
            [],
        ).append(row)

    recipients: list[BriefingRecipient] = []
    warnings: list[str] = []
    recipient_keys: set[tuple[str, str | None, str]] = set()
    for assignment_id, rows in rows_by_assignment.items():
        resolved = [
            row
            for row in rows
            if row.get("user_id")
            and bool(row.get("active"))
            and str(row.get("dingtalk_user_id") or "").strip()
        ]
        if len(resolved) != 1:
            warnings.append(
                f"assignment {assignment_id} resolved to "
                f"{len(resolved)} active DingTalk users"
            )
            continue
        row = resolved[0]
        recipient = BriefingRecipient(
            id=str(row["user_id"]),
            name=str(row.get("user_name") or ""),
            dingtalk_user_id=str(row["dingtalk_user_id"]),
            role=str(row.get("dashboard_role") or ""),
            team_ref=(str(row["team_ref"]) if row.get("team_ref") else None),
        )
        recipient_key = (
            recipient.role,
            recipient.team_ref,
            recipient.id,
        )
        if recipient_key in recipient_keys:
            warnings.append(
                f"overlapping active access assignment ignored for user {recipient.id}"
            )
            continue
        recipient_keys.add(recipient_key)
        recipients.append(recipient)
    return tuple(recipients), tuple(warnings)


def _configured_identifiers(value: object) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            item.strip() for item in str(value or "").split(",") if item.strip()
        )
    )


def _hidden_missing_detail_member_refs(
    settings: Settings,
    *,
    formal_roster: FormalLegalDailyRoster,
) -> frozenset[str]:
    configured = frozenset(
        _configured_identifiers(
            getattr(
                settings,
                "management_daily_briefing_hidden_missing_detail_user_ids",
                "",
            )
        )
    )
    expected = frozenset(
        {
            formal_roster.member_by_name(
                HIDDEN_MISSING_DETAIL_MEMBER_NAME
            ).user_id
        }
    )
    if configured != expected:
        raise RuntimeError(
            "management briefing hidden-detail users must match Zhao Weizhong exactly"
        )
    return expected


async def _load_department_cc_recipients(
    session: AsyncSession,
    *,
    configured_identifiers: tuple[str, ...],
) -> tuple[tuple[BriefingRecipient, ...], tuple[str, ...]]:
    if not configured_identifiers:
        return (), ()
    result = await session.execute(
        text(
            """
            SELECT
                users.id::text AS user_id,
                users.name AS user_name,
                users.dingtalk_user_id,
                users.active,
                CASE
                    WHEN users.id::text = ANY(:identifiers) THEN
                        users.id::text
                    ELSE users.dingtalk_user_id
                END AS matched_identifier
            FROM users
            WHERE users.id::text = ANY(:identifiers)
               OR users.dingtalk_user_id = ANY(:identifiers)
            ORDER BY users.id
            """
        ),
        {"identifiers": list(configured_identifiers)},
    )
    rows = result.mappings().all()
    rows_by_identifier: dict[str, list[Any]] = {
        identifier: [] for identifier in configured_identifiers
    }
    for row in rows:
        matched = str(row.get("matched_identifier") or "")
        if matched in rows_by_identifier:
            rows_by_identifier[matched].append(row)

    recipients: list[BriefingRecipient] = []
    warnings: list[str] = []
    for identifier, matches in rows_by_identifier.items():
        resolved = [
            row
            for row in matches
            if bool(row.get("active"))
            and str(row.get("dingtalk_user_id") or "").strip()
        ]
        if len(resolved) != 1:
            warnings.append(
                f"department CC identifier resolved to "
                f"{len(resolved)} active DingTalk users"
            )
            continue
        row = resolved[0]
        recipients.append(
            BriefingRecipient(
                id=str(row["user_id"]),
                name=str(row.get("user_name") or ""),
                dingtalk_user_id=str(row["dingtalk_user_id"]),
                role="department_cc",
            )
        )
    return _unique_recipients(recipients), tuple(warnings)
