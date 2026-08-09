from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from typing import Any, Protocol, Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.agent2.context_pack import KnowledgeEvidenceFrame
from app.agent2.fact_permissions import (
    ALL_ACCESS_DINGTALK_USER_IDS,
    DEPARTMENT_HEAD_ROLES,
    TEAM_LEADER_ROLES,
)
from app.agent2.report_insight_query import StructuredReportInsightQuery
from app.config import get_settings
from app.legal_daily_roster import (
    FormalLegalDailyRoster,
    load_formal_legal_daily_roster,
)
from app.models import Team, User
from app.repositories import (
    count_daily_reports_by_status,
    get_active_teams,
    get_active_users,
    list_reports_between_dates,
)


REPORT_INSIGHT_SOURCE_TYPE = "daily_report_insight"
UNCLOSED_SCOPE_CLARIFICATION_THRESHOLD = 20
UNCLOSED_SCOPE_PERIOD_OPTIONS = (
    "recent_7_days",
    "recent_30_days",
    "all_history",
)


class ReportInsightRepository(Protocol):
    async def list_users(self) -> Sequence[Any]: ...

    async def list_teams(self) -> Sequence[Any]: ...

    async def count_reports(
        self,
        *,
        user_ids: Sequence[str],
        end_date: date,
        team_ids: Sequence[str] | None = None,
    ) -> dict[str, int]: ...

    async def list_reports(
        self,
        *,
        user_ids: Sequence[str] | None,
        start_date: date,
        end_date: date,
        team_ids: Sequence[str] | None = None,
    ) -> Sequence[Any]: ...


@dataclass(frozen=True)
class ReportInsightAnswer:
    text: str
    evidence: KnowledgeEvidenceFrame


class InMemoryReportInsightRepository:
    def __init__(
        self,
        *,
        users: Sequence[Any],
        teams: Sequence[Any],
        reports: Sequence[Any],
    ) -> None:
        self._users = tuple(users)
        self._teams = tuple(teams)
        self._reports = tuple(reports)

    async def list_users(self) -> Sequence[Any]:
        return self._users

    async def list_teams(self) -> Sequence[Any]:
        return self._teams

    async def count_reports(
        self,
        *,
        user_ids: Sequence[str],
        end_date: date,
        team_ids: Sequence[str] | None = None,
    ) -> dict[str, int]:
        allowed_ids = {str(value) for value in user_ids}
        allowed_team_ids = (
            None if team_ids is None else {str(value) for value in team_ids}
        )
        current_team_by_user = {
            str(_value(user, "id") or _value(user, "user_id") or ""): str(
                _value(user, "team_id") or ""
            )
            for user in self._users
        }
        counts: dict[str, int] = {}
        for report in self._reports:
            report_user_id = str(_value(report, "user_id"))
            if report_user_id not in allowed_ids:
                continue
            report_team_id = str(
                _value(report, "team_id")
                or current_team_by_user.get(report_user_id, "")
            )
            if allowed_team_ids is not None and report_team_id not in allowed_team_ids:
                continue
            report_date = _as_date(
                _value(report, "report_date") or _value(report, "date")
            )
            if report_date is None or report_date > end_date:
                continue
            status = str(_value(report, "status") or "collecting")
            counts[status] = counts.get(status, 0) + 1
        return counts

    async def list_reports(
        self,
        *,
        user_ids: Sequence[str] | None,
        start_date: date,
        end_date: date,
        team_ids: Sequence[str] | None = None,
    ) -> Sequence[Any]:
        allowed_ids = None if user_ids is None else {str(value) for value in user_ids}
        allowed_team_ids = (
            None if team_ids is None else {str(value) for value in team_ids}
        )
        current_team_by_user = {
            str(_value(user, "id") or _value(user, "user_id") or ""): str(
                _value(user, "team_id") or ""
            )
            for user in self._users
        }
        matches = []
        for report in self._reports:
            report_user_id = str(_value(report, "user_id"))
            if allowed_ids is not None and report_user_id not in allowed_ids:
                continue
            report_team_id = str(
                _value(report, "team_id")
                or current_team_by_user.get(report_user_id, "")
            )
            if allowed_team_ids is not None and report_team_id not in allowed_team_ids:
                continue
            report_date = _as_date(
                _value(report, "report_date") or _value(report, "date")
            )
            if (
                report_date is None
                or report_date < start_date
                or report_date > end_date
            ):
                continue
            matches.append(report)
        return tuple(
            sorted(
                matches,
                key=lambda report: _as_date(
                    _value(report, "report_date") or _value(report, "date")
                )
                or date.min,
                reverse=True,
            )
        )


class SqlReportInsightRepository:
    def __init__(
        self,
        session: Any,
        *,
        roster_date: date | None = None,
        roster_tenant_id: str = "",
    ) -> None:
        self.session = session
        self.roster_date = roster_date
        self.roster_tenant_id = str(roster_tenant_id or "").strip()
        self._formal_roster: FormalLegalDailyRoster | None = None

    async def list_users(self) -> Sequence[Any]:
        if self.roster_date is None or not self.roster_tenant_id:
            return tuple(await get_active_users(self.session))
        roster = await self._load_formal_roster()
        database_user_ids = _database_user_ids(roster.user_ids)
        if len(database_user_ids) != roster.member_count:
            raise RuntimeError("formal roster contains an invalid user identifier")
        users = tuple(
            (
                await self.session.execute(
                    select(User)
                    .options(selectinload(User.team))
                    .where(User.id.in_(database_user_ids))
                    .order_by(User.team_id, User.name)
                )
            )
            .scalars()
            .all()
        )
        loaded_user_ids = {
            str(_value(user, "id") or _value(user, "user_id") or "")
            for user in users
        }
        if len(users) != roster.member_count or loaded_user_ids != set(roster.user_ids):
            raise RuntimeError("report query users do not match the formal roster")
        return users

    async def list_teams(self) -> Sequence[Any]:
        if self.roster_date is None or not self.roster_tenant_id:
            return await get_active_teams(self.session)
        roster = await self._load_formal_roster()
        database_team_ids = _database_ids(roster.team_ids)
        if len(database_team_ids) != len(roster.team_ids):
            raise RuntimeError("formal roster contains an invalid team identifier")
        teams = tuple(
            (
                await self.session.execute(
                    select(Team)
                    .where(Team.id.in_(database_team_ids))
                    .order_by(Team.active.desc(), Team.name)
                )
            )
            .scalars()
            .all()
        )
        loaded_team_ids = {
            str(_value(team, "id") or _value(team, "team_id") or "")
            for team in teams
        }
        if len(teams) != len(roster.team_ids) or loaded_team_ids != set(roster.team_ids):
            raise RuntimeError("report query teams do not match the formal roster")
        return teams

    async def _load_formal_roster(self) -> FormalLegalDailyRoster:
        if self._formal_roster is None:
            if self.roster_date is None or not self.roster_tenant_id:
                raise RuntimeError("formal roster scope is not configured")
            self._formal_roster = await load_formal_legal_daily_roster(
                self.session,
                tenant_id=self.roster_tenant_id,
                on_date=self.roster_date,
            )
        return self._formal_roster

    async def count_reports(
        self,
        *,
        user_ids: Sequence[str],
        end_date: date,
        team_ids: Sequence[str] | None = None,
    ) -> dict[str, int]:
        database_user_ids = _database_user_ids(user_ids)
        if not database_user_ids:
            return {}
        database_team_ids = _database_ids(team_ids or ())
        if team_ids is not None and not database_team_ids:
            return {}
        return await count_daily_reports_by_status(
            self.session,
            user_ids=database_user_ids,
            end_date=end_date,
            team_ids=database_team_ids if team_ids is not None else None,
        )

    async def list_reports(
        self,
        *,
        user_ids: Sequence[str] | None,
        start_date: date,
        end_date: date,
        team_ids: Sequence[str] | None = None,
    ) -> Sequence[Any]:
        database_user_ids = _database_user_ids(user_ids or ())
        if user_ids is not None and not database_user_ids:
            return ()
        database_team_ids = _database_ids(team_ids or ())
        if team_ids is not None and not database_team_ids:
            return ()
        return tuple(
            await list_reports_between_dates(
                self.session,
                start_date,
                end_date,
                team_ids=database_team_ids if team_ids is not None else None,
                user_ids=database_user_ids if user_ids is not None else None,
            )
        )


class ResolvedReportInsightAdapter:
    source_type = REPORT_INSIGHT_SOURCE_TYPE

    def __init__(self, answer: ReportInsightAnswer | None) -> None:
        self.answer = answer

    def resolve(self, query: Any) -> Sequence[KnowledgeEvidenceFrame]:
        if self.answer is None:
            return ()
        return (self.answer.evidence,)


async def load_live_report_insight_adapter(
    session: Any,
    *,
    requester: Any,
    text: str,
    current_date: date,
) -> ResolvedReportInsightAdapter:
    answer = await load_live_report_insight_answer(
        session,
        requester=requester,
        text=text,
        current_date=current_date,
    )
    return ResolvedReportInsightAdapter(answer)


async def load_live_report_insight_answer(
    session: Any,
    *,
    requester: Any,
    text: str,
    current_date: date,
) -> ReportInsightAnswer | None:
    settings = get_settings()
    roster_tenant_id = str(
        getattr(settings, "legal_daily_dashboard_tenant_id", "") or ""
    ).strip()
    return await ReportInsightModule(
        SqlReportInsightRepository(
            session,
            roster_date=current_date,
            roster_tenant_id=roster_tenant_id,
        )
    ).answer(
        text,
        requester=requester,
        current_date=current_date,
    )


def report_insight_reply_from_context(context_pack: Any | None) -> str:
    if context_pack is None:
        return ""
    for evidence in tuple(getattr(context_pack, "knowledge", ()) or ()):
        if (
            str(getattr(evidence, "source_type", "") or "")
            != REPORT_INSIGHT_SOURCE_TYPE
        ):
            continue
        summary = str(getattr(evidence, "summary", "") or "").strip()
        if summary:
            return summary
    return ""


class ReportInsightModule:
    def __init__(self, repository: ReportInsightRepository) -> None:
        self.repository = repository

    async def answer(
        self,
        text: str,
        *,
        requester: Any,
        current_date: date,
    ) -> ReportInsightAnswer | None:
        # Historical/offline compatibility only. Production enters through
        # ``answer_query`` after Agent2 has produced a typed semantic proposal.
        from app.agent2.report_insight_intent import (
            standalone_report_insight_query_kind,
        )

        query_text = str(text or "").strip()
        query_kind = standalone_report_insight_query_kind(query_text)
        if query_kind is None:
            return None

        users = tuple(await self.repository.list_users())
        teams = tuple(await self.repository.list_teams())
        if query_kind == "unclosed_work":
            return await self._unclosed_work_answer(
                query_text=query_text,
                query_kind=query_kind,
                requester=requester,
                users=users,
                teams=teams,
                current_date=current_date,
            )
        if query_kind == "department_recent_attention":
            return await self._department_recent_attention_answer(
                query_text=query_text,
                requester=requester,
                users=users,
                teams=teams,
                current_date=current_date,
            )
        if query_kind in {"team_current_week_work", "team_previous_week_work"}:
            return await self._team_period_work_answer(
                query_text=query_text,
                query_kind=query_kind,
                requester=requester,
                users=users,
                teams=teams,
                current_date=current_date,
            )

        named_users = _matched_named_users(query_text, users, teams=teams)
        if not named_users:
            if query_kind == "recent_work":
                return None
            return _resolution_answer(
                scope_type="person",
                resolution_status="not_found",
                current_date=current_date,
            )
        if len(named_users) > 1:
            return _resolution_answer(
                scope_type="person",
                resolution_status="ambiguous",
                current_date=current_date,
            )
        target = named_users[0]
        if not _query_scope_matches_target(
            query_text=query_text,
            query_kind=query_kind,
            target=target,
            teams=teams,
        ):
            return None
        permission_allowed, permission_team_ids = _person_report_scope(
            requester=requester,
            target=target,
            teams=teams,
        )
        if not permission_allowed:
            return _permission_denied_answer(target, current_date=current_date)

        target_id = str(_value(target, "id") or _value(target, "user_id") or "")
        if query_kind == "recent_work":
            return await self._recent_person_work_answer(
                target=target,
                target_id=target_id,
                permission_team_ids=permission_team_ids,
                current_date=current_date,
            )

        status_counts = await self.repository.count_reports(
            user_ids=[target_id],
            end_date=current_date,
            team_ids=permission_team_ids,
        )
        target_name = str(_value(target, "name") or "该人员")
        scope_text = (
            "根据日报台账"
            if permission_team_ids is None
            else "根据你可查看范围内的日报台账"
        )
        if query_kind == "completed_report_count":
            report_count = int(status_counts.get("completed", 0))
            other_counts = {
                status: count
                for status, count in status_counts.items()
                if status != "completed"
            }
            detail = _status_count_text(other_counts)
            reply = f"{scope_text}，{target_name}已完成 {report_count} 份日报"
            if detail:
                reply += f"；另有{detail}"
        else:
            report_count = sum(status_counts.values())
            detail = _status_count_text(status_counts)
            reply = f"{scope_text}，{target_name}目前共有 {report_count} 份日报"
            if detail:
                reply += f"，其中{detail}"
        reply += f"。统计截至 {current_date.isoformat()}，包含已保存的日报记录。"
        evidence = KnowledgeEvidenceFrame(
            source_type=REPORT_INSIGHT_SOURCE_TYPE,
            source_id=f"daily_report_count:{query_kind}:{target_id}:{current_date.isoformat()}",
            title=f"{target_name}日报数量",
            summary=reply,
            facts={
                "query_kind": query_kind,
                "scope_type": "person",
                "scope_label": target_name,
                "target_user_ids": [target_id],
                "report_count": report_count,
                "status_counts": dict(status_counts),
                "permission_scope_team_ids": sorted(permission_team_ids or ()),
                "as_of_date": current_date.isoformat(),
                "permission_allowed": True,
            },
            confidence=0.99,
            freshness=current_date.isoformat(),
        )
        return ReportInsightAnswer(text=reply, evidence=evidence)

    async def answer_query(
        self,
        query: StructuredReportInsightQuery,
        *,
        requester: Any,
        current_date: date,
    ) -> ReportInsightAnswer | None:
        """Answer one Agent2-understood query without re-reading natural language."""

        users = tuple(await self.repository.list_users())
        teams = tuple(await self.repository.list_teams())
        start_date, end_date = _report_insight_period_bounds(
            query.period_type,
            current_date=current_date,
        )
        if query.scope_type == "person":
            named_users = _matched_named_users(query.scope_name, users, teams=teams)
            if not named_users:
                return _resolution_answer(
                    scope_type="person",
                    resolution_status="not_found",
                    current_date=current_date,
                )
            if len(named_users) > 1:
                return _resolution_answer(
                    scope_type="person",
                    resolution_status="ambiguous",
                    current_date=current_date,
                )
            target = named_users[0]
            permission_allowed, permission_team_ids = _person_report_scope(
                requester=requester,
                target=target,
                teams=teams,
            )
            if not permission_allowed:
                return _permission_denied_answer(target, current_date=current_date)
            if query.query_kind == "report_count":
                return await self._person_report_count_answer(
                    target=target,
                    permission_team_ids=permission_team_ids,
                    current_date=current_date,
                    completed_only=query.status_filter == "completed",
                )
            if query.query_kind == "recent_work":
                return await self._recent_person_work_answer(
                    target=target,
                    target_id=str(
                        _value(target, "id") or _value(target, "user_id") or ""
                    ),
                    permission_team_ids=permission_team_ids,
                    current_date=current_date,
                    period_type=query.period_type,
                    start_date_override=start_date or date.min,
                    end_date_override=end_date or current_date,
                )
            if query.query_kind == "unclosed_work":
                return await self._person_unclosed_work_answer(
                    target=target,
                    permission_team_ids=permission_team_ids,
                    current_date=current_date,
                    period_type=query.period_type,
                    plan_start_date=start_date,
                    plan_end_date=end_date,
                )
            return None

        if query.query_kind == "unclosed_work":
            return await self._organization_unclosed_work_answer(
                query_scope=query.scope_name,
                requester=requester,
                users=users,
                teams=teams,
                current_date=current_date,
                period_type=query.period_type,
                plan_start_date=start_date,
                plan_end_date=end_date,
            )
        if query.query_kind == "recent_attention":
            return await self._department_recent_attention_answer(
                query_text=query.query,
                requester=requester,
                users=users,
                teams=teams,
                current_date=current_date,
                requested_scope_override=query.scope_name,
                start_date_override=start_date,
                period_type_override=query.period_type,
            )
        if query.query_kind == "period_work":
            return await self._team_period_work_answer(
                query_text=query.query,
                query_kind=(
                    "team_previous_week_work"
                    if query.period_type == "previous_week"
                    else "team_current_week_work"
                ),
                requester=requester,
                users=users,
                teams=teams,
                current_date=current_date,
                requested_scope_override=query.scope_name,
                period_type_override=query.period_type,
            )
        return None

    async def _person_report_count_answer(
        self,
        *,
        target: Any,
        permission_team_ids: Sequence[str] | None,
        current_date: date,
        completed_only: bool,
    ) -> ReportInsightAnswer:
        target_id = str(_value(target, "id") or _value(target, "user_id") or "")
        target_name = str(_value(target, "name") or "该人员")
        status_counts = await self.repository.count_reports(
            user_ids=[target_id],
            end_date=current_date,
            team_ids=permission_team_ids,
        )
        scope_text = (
            "根据日报台账"
            if permission_team_ids is None
            else "根据你可查看范围内的日报台账"
        )
        if completed_only:
            query_kind = "completed_report_count"
            report_count = int(status_counts.get("completed", 0))
            other_counts = {
                status: count
                for status, count in status_counts.items()
                if status != "completed"
            }
            detail = _status_count_text(other_counts)
            reply = f"{scope_text}，{target_name}已完成 {report_count} 份日报"
            if detail:
                reply += f"；另有{detail}"
        else:
            query_kind = "report_count"
            report_count = sum(status_counts.values())
            detail = _status_count_text(status_counts)
            reply = f"{scope_text}，{target_name}目前共有 {report_count} 份日报"
            if detail:
                reply += f"，其中{detail}"
        reply += f"。统计截至 {current_date.isoformat()}，包含已保存的日报记录。"
        return ReportInsightAnswer(
            text=reply,
            evidence=KnowledgeEvidenceFrame(
                source_type=REPORT_INSIGHT_SOURCE_TYPE,
                source_id=f"daily_report_count:{query_kind}:{target_id}:{current_date.isoformat()}",
                title=f"{target_name}日报数量",
                summary=reply,
                facts={
                    "query_kind": query_kind,
                    "scope_type": "person",
                    "scope_label": target_name,
                    "target_user_ids": [target_id],
                    "report_count": report_count,
                    "status_counts": dict(status_counts),
                    "permission_scope_team_ids": sorted(permission_team_ids or ()),
                    "as_of_date": current_date.isoformat(),
                    "permission_allowed": True,
                },
                confidence=0.99,
                freshness=current_date.isoformat(),
            ),
        )

    async def _unclosed_work_answer(
        self,
        *,
        query_text: str,
        query_kind: str,
        requester: Any,
        users: Sequence[Any],
        teams: Sequence[Any],
        current_date: date,
    ) -> ReportInsightAnswer | None:
        named_users = _matched_named_users(query_text, users, teams=teams)
        if named_users:
            if len(named_users) > 1:
                return _resolution_answer(
                    scope_type="person",
                    resolution_status="ambiguous",
                    current_date=current_date,
                )
            target = named_users[0]
            if not _query_scope_matches_target(
                query_text=query_text,
                query_kind=query_kind,
                target=target,
                teams=teams,
            ):
                return None
            permission_allowed, permission_team_ids = _person_report_scope(
                requester=requester,
                target=target,
                teams=teams,
            )
            if not permission_allowed:
                return _permission_denied_answer(target, current_date=current_date)
            return await self._person_unclosed_work_answer(
                target=target,
                permission_team_ids=permission_team_ids,
                current_date=current_date,
            )

        query_scope = (
            _legacy_report_insight_query_scope(query_text, query_kind)
            .strip()
            .rstrip("的")
        )
        named_scope = _matched_exact_named_scope(query_scope, teams)
        if named_scope is not None or _looks_like_organization_scope(query_scope):
            return await self._organization_unclosed_work_answer(
                query_scope=query_scope,
                requester=requester,
                users=users,
                teams=teams,
                current_date=current_date,
            )
        return None

    async def _person_unclosed_work_answer(
        self,
        *,
        target: Any,
        permission_team_ids: Sequence[str] | None,
        current_date: date,
        period_type: str = "all_history",
        plan_start_date: date | None = None,
        plan_end_date: date | None = None,
    ) -> ReportInsightAnswer:
        target_id = str(_value(target, "id") or _value(target, "user_id") or "")
        target_name = str(_value(target, "name") or "该人员")
        reports = tuple(
            await self.repository.list_reports(
                user_ids=[target_id],
                start_date=plan_start_date or date.min,
                end_date=current_date,
                team_ids=permission_team_ids,
            )
        )
        followup_analysis = _plan_followup_analysis(
            reports,
            user_names={target_id: target_name},
            closure_across_users=False,
            as_of_date=current_date,
            plan_start_date=plan_start_date,
            plan_end_date=plan_end_date,
        )
        preview_limit = 20
        presentation = _build_unclosed_presentation(
            followup_analysis,
            reports=reports,
            period_type=period_type,
            preview_limit=preview_limit,
        )
        unclosed_items = list(presentation.unclosed_items)
        needs_time_scope = presentation.needs_time_scope
        history_start = presentation.history_start
        preview_count = presentation.preview_count
        preview_truncated = presentation.preview_truncated
        scope_text = "日报" if permission_team_ids is None else "可查看范围内日报"
        if needs_time_scope:
            opening = (
                f"已核对 {history_start or '现有首份日报'} 至 {current_date.isoformat()} 的"
                f"{scope_text}，{target_name}共有 {len(unclosed_items)} 项计划未找到后续"
                "工作记录；明细较多，需要先确定查看时间范围。"
            )
        elif plan_start_date is not None and plan_end_date is not None:
            opening = (
                f"根据 {plan_start_date.isoformat()} 至 {plan_end_date.isoformat()} 的工作计划，"
                f"并核对截至 {current_date.isoformat()} 的后续“今日工作”，"
                f"{target_name}共有 {len(unclosed_items)} 项计划未找到后续工作记录。"
            )
        else:
            opening = (
                f"根据截至 {current_date.isoformat()} 的{scope_text}，按计划日期之后的“今日工作”文字核对，"
                f"{target_name}共有 {len(unclosed_items)} 项计划未找到后续工作记录。"
            )
        lines = [opening]
        if needs_time_scope:
            lines.append("可继续查看最近7天、最近30天或全部历史。")
        elif unclosed_items:
            if preview_truncated:
                lines.append(
                    f"以下展示前{preview_count}项（共{len(unclosed_items)}项）："
                )
            lines.extend(
                _unclosed_numbered_lines(
                    unclosed_items,
                    include_user=False,
                    limit=preview_limit,
                )
            )
        else:
            lines.append("- 暂未发现没有后续工作记录的计划。")
        if not needs_time_scope and followup_analysis.in_progress_items:
            lines.append(
                f"另有 {len(followup_analysis.in_progress_items)} 项已在后续日报中继续跟进，"
                "未计入上述数字。"
            )
        if not needs_time_scope and followup_analysis.mentioned_items:
            lines.append(
                f"另有 {len(followup_analysis.mentioned_items)} 项已有后续工作记录，"
                "未计入上述数字。"
            )
        if not needs_time_scope and followup_analysis.pending_evidence_items:
            lines.append(
                f"另有 {len(followup_analysis.pending_evidence_items)} 项在计划后没有可用于核对的"
                "后续日报，暂无法判断。"
            )
        lines.append(
            "说明：同一工作跨日重复计划只算一项；只要后续日期的“今日工作”明确提及"
            "同一事项，就算已有后续记录。“未找到后续记录”不等于实际工作一定未完成。"
        )
        reply = "\n".join(lines)
        evidence = KnowledgeEvidenceFrame(
            source_type=REPORT_INSIGHT_SOURCE_TYPE,
            source_id=f"daily_person_unclosed:{target_id}:{current_date.isoformat()}",
            title=f"{target_name}未闭环工作",
            summary=reply,
            facts={
                "query_kind": "person_unclosed_work",
                "scope_type": "person",
                "scope_label": target_name,
                "target_user_ids": [target_id],
                "permission_scope_team_ids": sorted(permission_team_ids or ()),
                "history_start": history_start,
                "history_end": current_date.isoformat(),
                "period_type": period_type,
                "evaluated_period_type": (
                    "all_history" if period_type == "unspecified" else period_type
                ),
                "period_start": plan_start_date.isoformat()
                if plan_start_date
                else history_start,
                "period_end": plan_end_date.isoformat()
                if plan_end_date
                else current_date.isoformat(),
                "report_count": len(reports),
                **presentation.fact_fields(),
                "closure_rule": "later_today_work_mentions_same_item",
                "permission_allowed": True,
            },
            confidence=0.92,
            freshness=current_date.isoformat(),
        )
        return ReportInsightAnswer(text=reply, evidence=evidence)

    async def _organization_unclosed_work_answer(
        self,
        *,
        query_scope: str,
        requester: Any,
        users: Sequence[Any],
        teams: Sequence[Any],
        current_date: date,
        period_type: str = "all_history",
        plan_start_date: date | None = None,
        plan_end_date: date | None = None,
    ) -> ReportInsightAnswer:
        target_scope = _matched_exact_named_scope(query_scope, teams)
        if target_scope is not None:
            scope_type, scope_label, target_teams = target_scope
            if scope_type == "department":
                if not _can_read_department(
                    requester=requester, department_name=scope_label, teams=teams
                ):
                    return _department_permission_denied_answer(
                        scope_label, current_date=current_date
                    )
            elif not _can_read_team(
                requester=requester, target_team=target_teams[0], teams=teams
            ):
                return _team_permission_denied_answer(
                    target_teams[0], current_date=current_date
                )
        else:
            requested_scope = query_scope
            if requested_scope and not _is_generic_organization_label(requested_scope):
                return _resolution_answer(
                    scope_type="organization",
                    resolution_status="not_found",
                    current_date=current_date,
                )
            requester_team_id = str(_value(requester, "team_id") or "")
            requester_role = str(_value(requester, "role") or "member").strip().lower()
            if requester_role in TEAM_LEADER_ROLES:
                target_teams = [
                    team
                    for team in teams
                    if str(_value(team, "id") or _value(team, "team_id") or "")
                    == requester_team_id
                ]
                if not target_teams:
                    return _resolution_answer(
                        scope_type="organization",
                        resolution_status="not_found",
                        current_date=current_date,
                    )
                scope_type = "team"
                scope_label = str(_value(target_teams[0], "name") or "当前团队")
            else:
                department_name = _team_department(teams, requester_team_id)
                if not department_name:
                    return _resolution_answer(
                        scope_type="organization",
                        resolution_status="not_found",
                        current_date=current_date,
                    )
                if not _can_read_department(
                    requester=requester,
                    department_name=department_name,
                    teams=teams,
                ):
                    return _department_permission_denied_answer(
                        department_name, current_date=current_date
                    )
                scope_type = "department"
                scope_label = department_name
                target_teams = [
                    team
                    for team in teams
                    if str(_value(team, "department_name") or "").strip()
                    == department_name
                ]

        target_team_ids = sorted(
            {
                str(_value(team, "id") or _value(team, "team_id") or "")
                for team in target_teams
                if str(_value(team, "id") or _value(team, "team_id") or "")
            }
        )
        user_names = {
            str(_value(user, "id") or _value(user, "user_id") or ""): str(
                _value(user, "name") or "未命名人员"
            )
            for user in users
        }
        reports = tuple(
            await self.repository.list_reports(
                user_ids=None,
                start_date=plan_start_date or date.min,
                end_date=current_date,
                team_ids=target_team_ids,
            )
        )
        followup_analysis = _plan_followup_analysis(
            reports,
            user_names=user_names,
            closure_across_users=True,
            as_of_date=current_date,
            plan_start_date=plan_start_date,
            plan_end_date=plan_end_date,
        )
        preview_limit = 30
        presentation = _build_unclosed_presentation(
            followup_analysis,
            reports=reports,
            period_type=period_type,
            preview_limit=preview_limit,
        )
        unclosed_items = list(presentation.unclosed_items)
        needs_time_scope = presentation.needs_time_scope
        history_start = presentation.history_start
        preview_count = presentation.preview_count
        preview_truncated = presentation.preview_truncated
        target_user_ids = sorted(
            {str(_value(report, "user_id") or "") for report in reports}
        )
        if needs_time_scope:
            opening = (
                f"已核对 {history_start or '现有首份日报'} 至 {current_date.isoformat()} 的"
                f"可查看日报，{scope_label}共有 {len(unclosed_items)} 项计划未找到后续"
                "工作记录；明细较多，需要先确定查看时间范围。"
            )
        elif plan_start_date is not None and plan_end_date is not None:
            opening = (
                f"根据 {plan_start_date.isoformat()} 至 {plan_end_date.isoformat()} 的工作计划，"
                f"并核对截至 {current_date.isoformat()} 的后续部门成员“今日工作”，"
                f"{scope_label}共有 {len(unclosed_items)} 项计划未找到后续工作记录。"
            )
        else:
            opening = (
                f"根据截至 {current_date.isoformat()} 的可查看日报，按计划日期之后的部门成员“今日工作”文字核对，"
                f"{scope_label}共有 {len(unclosed_items)} 项计划未找到后续工作记录。"
            )
        lines = [opening]
        if needs_time_scope:
            lines.append("可继续查看最近7天、最近30天或全部历史。")
        elif unclosed_items:
            if preview_truncated:
                lines.append(
                    f"以下展示前{preview_count}项（共{len(unclosed_items)}项）："
                )
            lines.extend(
                _unclosed_numbered_lines(
                    unclosed_items,
                    include_user=True,
                    limit=preview_limit,
                )
            )
        else:
            lines.append("- 暂未发现没有后续工作记录的计划。")
        if not needs_time_scope and followup_analysis.in_progress_items:
            lines.append(
                f"另有 {len(followup_analysis.in_progress_items)} 项已由部门成员在后续日报中继续跟进，"
                "未计入上述数字。"
            )
        if not needs_time_scope and followup_analysis.mentioned_items:
            lines.append(
                f"另有 {len(followup_analysis.mentioned_items)} 项已有部门后续工作记录，"
                "未计入上述数字。"
            )
        if not needs_time_scope and followup_analysis.pending_evidence_items:
            lines.append(
                f"另有 {len(followup_analysis.pending_evidence_items)} 项在计划后没有可用于核对的"
                "后续日报，暂无法判断。"
            )
        lines.append(
            "说明：同一工作跨日重复计划只算一项；部门内任一成员在后续日期的“今日工作”"
            "明确提及同一事项，就算已有后续记录。“未找到后续记录”不等于实际工作一定未完成。"
        )
        reply = "\n".join(lines)
        evidence = KnowledgeEvidenceFrame(
            source_type=REPORT_INSIGHT_SOURCE_TYPE,
            source_id=f"daily_org_unclosed:{scope_type}:{scope_label}:{current_date.isoformat()}",
            title=f"{scope_label}未闭环工作",
            summary=reply,
            facts={
                "query_kind": "organization_unclosed_work",
                "scope_type": scope_type,
                "scope_label": scope_label,
                "target_team_ids": target_team_ids,
                "target_user_ids": target_user_ids,
                "history_start": history_start,
                "history_end": current_date.isoformat(),
                "period_type": period_type,
                "evaluated_period_type": (
                    "all_history" if period_type == "unspecified" else period_type
                ),
                "period_start": plan_start_date.isoformat()
                if plan_start_date
                else history_start,
                "period_end": plan_end_date.isoformat()
                if plan_end_date
                else current_date.isoformat(),
                "report_count": len(reports),
                **presentation.fact_fields(),
                "closure_rule": "later_department_today_work_mentions_same_item",
                "permission_allowed": True,
            },
            confidence=0.92,
            freshness=current_date.isoformat(),
        )
        return ReportInsightAnswer(text=reply, evidence=evidence)

    async def _department_recent_attention_answer(
        self,
        *,
        query_text: str,
        requester: Any,
        users: Sequence[Any],
        teams: Sequence[Any],
        current_date: date,
        requested_scope_override: str | None = None,
        start_date_override: date | None = None,
        period_type_override: str | None = None,
    ) -> ReportInsightAnswer | None:
        requested_scope = (
            requested_scope_override.strip().rstrip("的")
            if requested_scope_override is not None
            else _legacy_report_insight_query_scope(
                query_text,
                "department_recent_attention",
            )
            .strip()
            .rstrip("的")
        )
        named_scope = _matched_exact_named_scope(requested_scope, teams)
        requester_team_id = str(_value(requester, "team_id") or "")
        department_name = _team_department(teams, requester_team_id)
        requester_role = str(_value(requester, "role") or "member").strip().lower()
        if named_scope is not None:
            scope_type, scope_label, target_teams = named_scope
            if scope_type == "department":
                if not _can_read_department(
                    requester=requester, department_name=scope_label, teams=teams
                ):
                    return _department_permission_denied_answer(
                        scope_label, current_date=current_date
                    )
            elif not _can_read_team(
                requester=requester, target_team=target_teams[0], teams=teams
            ):
                return _team_permission_denied_answer(
                    target_teams[0], current_date=current_date
                )
        elif requested_scope and not _is_generic_organization_label(requested_scope):
            return _resolution_answer(
                scope_type="organization",
                resolution_status="not_found",
                current_date=current_date,
            )
        elif requester_role in TEAM_LEADER_ROLES:
            target_teams = [
                team
                for team in teams
                if str(_value(team, "id") or _value(team, "team_id") or "")
                == requester_team_id
            ]
            if not target_teams:
                return _resolution_answer(
                    scope_type="organization",
                    resolution_status="not_found",
                    current_date=current_date,
                )
            scope_type = "team"
            scope_label = str(_value(target_teams[0], "name") or "当前团队")
        else:
            if not department_name:
                return _resolution_answer(
                    scope_type="organization",
                    resolution_status="not_found",
                    current_date=current_date,
                )
            target_teams = [
                team
                for team in teams
                if str(_value(team, "department_name") or "").strip() == department_name
            ]
            if not _can_read_department(
                requester=requester, department_name=department_name, teams=teams
            ):
                return _department_permission_denied_answer(
                    department_name, current_date=current_date
                )
            scope_type = "department"
            scope_label = department_name

        target_team_ids = {
            str(_value(team, "id") or _value(team, "team_id") or "")
            for team in target_teams
        }
        user_names = {
            str(_value(user, "id") or _value(user, "user_id") or ""): str(
                _value(user, "name") or "未命名人员"
            )
            for user in users
        }
        start_date = start_date_override or (current_date - timedelta(days=6))
        reports = tuple(
            await self.repository.list_reports(
                user_ids=None,
                start_date=start_date,
                end_date=current_date,
                team_ids=sorted(target_team_ids),
            )
        )
        target_user_ids = sorted(
            {str(_value(report, "user_id") or "") for report in reports}
        )
        problem_analysis = _problem_attention_analysis(
            reports,
            user_names=user_names,
            closure_across_users=True,
            as_of_date=current_date,
        )
        problem_items = list(problem_analysis.active_items)
        plan_analysis = _plan_followup_analysis(
            reports,
            user_names=user_names,
            closure_across_users=True,
            # Attention includes today's newly recorded plan. Treat the next day
            # as the evidence boundary so that it remains pending, rather than
            # disappearing merely because no later report can exist yet.
            as_of_date=current_date + timedelta(days=1),
            plan_start_date=start_date,
            plan_end_date=current_date,
        )
        plan_items = _active_attention_plan_items(plan_analysis)
        lines = [
            f"根据 {start_date.isoformat()} 至 {current_date.isoformat()} 的日报，{scope_label}近期重点关注如下："
        ]
        if problem_items:
            lines.extend(
                ["问题/风险：", *_scoped_numbered_lines(problem_items, limit=16)]
            )
        if plan_items:
            lines.extend(
                ["需跟进事项：", *_scoped_numbered_lines(plan_items, limit=12)]
            )
        if not problem_items and not plan_items:
            lines.append("- 暂无已记录的重点问题、风险或需跟进计划。")
        reply = "\n".join(lines)
        evidence = KnowledgeEvidenceFrame(
            source_type=REPORT_INSIGHT_SOURCE_TYPE,
            source_id=f"daily_org_attention:{scope_type}:{scope_label}:{start_date.isoformat()}:{current_date.isoformat()}",
            title=f"{scope_label}近期重点关注",
            summary=reply,
            facts={
                "query_kind": "department_attention",
                "scope_type": scope_type,
                "scope_label": scope_label,
                "target_team_ids": sorted(target_team_ids),
                "target_user_ids": target_user_ids,
                "period_type": period_type_override or "recent_7_days",
                "period_start": start_date.isoformat(),
                "period_end": current_date.isoformat(),
                "report_count": len(reports),
                "problem_items": problem_items,
                "resolved_problem_count": len(problem_analysis.resolved_items),
                "plan_items": plan_items,
                "completed_attention_plan_count": len(
                    [
                        item
                        for item in plan_analysis.completed_items
                        if _is_attention_plan(item["plan_text"])
                    ]
                ),
                "permission_allowed": True,
            },
            confidence=0.98,
            freshness=current_date.isoformat(),
        )
        return ReportInsightAnswer(text=reply, evidence=evidence)

    async def _team_period_work_answer(
        self,
        *,
        query_text: str,
        query_kind: str,
        requester: Any,
        users: Sequence[Any],
        teams: Sequence[Any],
        current_date: date,
        requested_scope_override: str | None = None,
        period_type_override: str | None = None,
    ) -> ReportInsightAnswer | None:
        requested_scope = (
            requested_scope_override.strip().rstrip("的")
            if requested_scope_override is not None
            else _legacy_report_insight_query_scope(query_text, query_kind)
            .strip()
            .rstrip("的")
        )
        target_scope = _matched_exact_named_scope(requested_scope, teams)
        if target_scope is None:
            return _resolution_answer(
                scope_type="organization",
                resolution_status="not_found",
                current_date=current_date,
            )
        scope_type, scope_label, target_teams = target_scope
        if scope_type == "department":
            if not _can_read_department(
                requester=requester, department_name=scope_label, teams=teams
            ):
                return _department_permission_denied_answer(
                    scope_label, current_date=current_date
                )
        elif not _can_read_team(
            requester=requester, target_team=target_teams[0], teams=teams
        ):
            return _team_permission_denied_answer(
                target_teams[0], current_date=current_date
            )

        target_team_ids = {
            str(_value(team, "id") or _value(team, "team_id") or "")
            for team in target_teams
        }
        user_names = {
            str(_value(user, "id") or _value(user, "user_id") or ""): str(
                _value(user, "name") or "未命名人员"
            )
            for user in users
        }
        current_week_start = current_date - timedelta(days=current_date.weekday())
        if (
            period_type_override or ""
        ) == "previous_week" or query_kind == "team_previous_week_work":
            start_date = current_week_start - timedelta(days=7)
            end_date = current_week_start - timedelta(days=1)
            period_type = "previous_week"
            period_label = "上周"
        else:
            start_date = current_week_start
            end_date = current_date
            period_type = "current_week"
            period_label = "本周"
        reports = tuple(
            await self.repository.list_reports(
                user_ids=None,
                start_date=start_date,
                end_date=end_date,
                team_ids=sorted(target_team_ids),
            )
        )
        target_user_ids = sorted(
            {str(_value(report, "user_id") or "") for report in reports}
        )
        work_items = _scoped_report_items(reports, "today_work", user_names=user_names)
        problem_items = _scoped_report_items(reports, "problems", user_names=user_names)
        plan_items = _scoped_report_items(
            reports, "tomorrow_plan", user_names=user_names
        )
        reporter_count = len(
            {str(_value(report, "user_id") or "") for report in reports}
        )
        lines = [
            (
                f"根据 {start_date.isoformat()} 至 {end_date.isoformat()} 的日报，"
                f"{scope_label}{period_label}共有 {reporter_count} 人、{len(reports)} 份记录。"
            ),
            "主要工作：",
            *(_scoped_numbered_lines(work_items, limit=16) or ["- 暂无已记录工作"]),
        ]
        if problem_items:
            lines.extend(
                ["问题/风险：", *_scoped_numbered_lines(problem_items, limit=10)]
            )
        if plan_items:
            lines.extend(["后续计划：", *_scoped_numbered_lines(plan_items, limit=10)])
        reply = "\n".join(lines)
        evidence = KnowledgeEvidenceFrame(
            source_type=REPORT_INSIGHT_SOURCE_TYPE,
            source_id=f"daily_org_period:{scope_type}:{scope_label}:{start_date.isoformat()}:{end_date.isoformat()}",
            title=f"{scope_label}{period_label}工作",
            summary=reply,
            facts={
                "query_kind": "team_work_summary",
                "scope_type": scope_type,
                "scope_label": scope_label,
                "target_team_ids": sorted(target_team_ids),
                "target_user_ids": target_user_ids,
                "period_type": period_type,
                "period_start": start_date.isoformat(),
                "period_end": end_date.isoformat(),
                "report_count": len(reports),
                "reporter_count": reporter_count,
                "work_items": work_items,
                "problem_items": problem_items,
                "plan_items": plan_items,
                "permission_allowed": True,
            },
            confidence=0.98,
            freshness=end_date.isoformat(),
        )
        return ReportInsightAnswer(text=reply, evidence=evidence)

    async def _recent_person_work_answer(
        self,
        *,
        target: Any,
        target_id: str,
        permission_team_ids: Sequence[str] | None,
        current_date: date,
        period_type: str = "recent_7_days",
        start_date_override: date | None = None,
        end_date_override: date | None = None,
    ) -> ReportInsightAnswer:
        start_date = start_date_override or (current_date - timedelta(days=6))
        end_date = end_date_override or current_date
        reports = tuple(
            await self.repository.list_reports(
                user_ids=[target_id],
                start_date=start_date,
                end_date=end_date,
                team_ids=permission_team_ids,
            )
        )
        target_name = str(_value(target, "name") or "该人员")
        work_items = _report_items(reports, "today_work")
        problem_items = _report_items(reports, "problems")
        plan_items = _report_items(reports, "tomorrow_plan")
        lines = [
            (
                f"根据 {start_date.isoformat()} 至 {end_date.isoformat()} 的"
                f"{'日报' if permission_team_ids is None else '可查看范围内日报'}，"
                f"{target_name}共有 {len(reports)} 份记录。"
            ),
            "主要工作：",
            *(_numbered_lines(work_items, limit=10) or ["- 暂无已记录工作"]),
        ]
        if problem_items:
            lines.extend(["问题/风险：", *_numbered_lines(problem_items, limit=6)])
        if plan_items:
            lines.extend(["后续计划：", *_numbered_lines(plan_items, limit=6)])
        reply = "\n".join(lines)
        evidence = KnowledgeEvidenceFrame(
            source_type=REPORT_INSIGHT_SOURCE_TYPE,
            source_id=f"daily_recent_work:{target_id}:{start_date.isoformat()}:{end_date.isoformat()}",
            title=f"{target_name}近期工作",
            summary=reply,
            facts={
                "query_kind": "recent_work",
                "scope_type": "person",
                "scope_label": target_name,
                "target_user_ids": [target_id],
                "period_type": period_type,
                "period_start": start_date.isoformat(),
                "period_end": end_date.isoformat(),
                "report_count": len(reports),
                "permission_scope_team_ids": sorted(permission_team_ids or ()),
                "work_items": work_items,
                "problem_items": problem_items,
                "plan_items": plan_items,
                "permission_allowed": True,
            },
            confidence=0.98,
            freshness=end_date.isoformat(),
        )
        return ReportInsightAnswer(text=reply, evidence=evidence)


def _person_report_scope(
    *,
    requester: Any,
    target: Any,
    teams: Sequence[Any],
) -> tuple[bool, list[str] | None]:
    requester_id = str(_value(requester, "id") or _value(requester, "user_id") or "")
    requester_dingtalk_id = str(_value(requester, "dingtalk_user_id") or "")
    target_id = str(_value(target, "id") or _value(target, "user_id") or "")
    if requester_id and requester_id == target_id:
        return True, None
    if requester_dingtalk_id in ALL_ACCESS_DINGTALK_USER_IDS:
        return True, None

    requester_team_id = str(_value(requester, "team_id") or "")
    target_team_id = str(_value(target, "team_id") or "")
    requester_department = _team_department(teams, requester_team_id)
    target_department = _team_department(teams, target_team_id)
    if requester_department and requester_department == target_department:
        team_ids = sorted(
            str(_value(team, "id") or _value(team, "team_id") or "")
            for team in teams
            if str(_value(team, "department_name") or "").strip()
            == requester_department
        )
        return True, team_ids

    requester_role = str(_value(requester, "role") or "member").strip().lower()
    if requester_role in TEAM_LEADER_ROLES:
        allowed = bool(requester_team_id and requester_team_id == target_team_id)
        return allowed, [requester_team_id] if allowed else []
    if requester_role in DEPARTMENT_HEAD_ROLES:
        if not requester_department or requester_department != target_department:
            return False, []
        team_ids = sorted(
            str(_value(team, "id") or _value(team, "team_id") or "")
            for team in teams
            if str(_value(team, "department_name") or "").strip()
            == requester_department
        )
        return True, team_ids
    return False, []


def _permission_denied_answer(
    target: Any, *, current_date: date
) -> ReportInsightAnswer:
    target_name = str(_value(target, "name") or "该人员")
    reply = f"你没有权限查看{target_name}的日报。"
    return ReportInsightAnswer(
        text=reply,
        evidence=KnowledgeEvidenceFrame(
            source_type=REPORT_INSIGHT_SOURCE_TYPE,
            source_id=f"daily_report_permission_denied:{target_name}",
            title="日报查询权限校验",
            summary=reply,
            facts={
                "query_kind": "permission_denied",
                "scope_type": "person",
                "scope_label": target_name,
                "permission_allowed": False,
            },
            confidence=1.0,
            freshness=current_date.isoformat(),
        ),
    )


def _resolution_answer(
    *,
    scope_type: str,
    resolution_status: str,
    current_date: date,
) -> ReportInsightAnswer:
    if scope_type == "person" and resolution_status == "ambiguous":
        reply = "当前组织中有多位同名人员，请补充其部门或团队后再试。"
        title = "日报查询人员重名"
    elif scope_type == "person":
        reply = "当前组织中没有找到要查询的人员，请确认姓名后再试。"
        title = "日报查询人员未找到"
    else:
        reply = "当前组织中没有找到要查询的部门或团队，请确认名称后再试。"
        title = "日报查询部门未找到"
    return ReportInsightAnswer(
        text=reply,
        evidence=KnowledgeEvidenceFrame(
            source_type=REPORT_INSIGHT_SOURCE_TYPE,
            source_id=f"daily_report_resolution:{scope_type}:{resolution_status}",
            title=title,
            summary=reply,
            facts={
                "query_kind": "target_resolution",
                "scope_type": scope_type,
                "resolution_status": resolution_status,
                "permission_allowed": False,
            },
            confidence=1.0,
            freshness=current_date.isoformat(),
        ),
    )


def _status_count_text(status_counts: dict[str, int]) -> str:
    labels = (
        ("completed", "已完成"),
        ("pending_confirmation", "待确认"),
        ("collecting", "填写中"),
        ("skipped", "已跳过"),
        ("cancelled", "已取消"),
    )
    return "、".join(
        f"{label} {int(status_counts.get(status, 0))} 份"
        for status, label in labels
        if int(status_counts.get(status, 0)) > 0
    )


def _report_insight_period_bounds(
    period_type: str,
    *,
    current_date: date,
) -> tuple[date | None, date | None]:
    if period_type in {"all_history", "unspecified"}:
        return None, None
    if period_type == "recent_7_days":
        return current_date - timedelta(days=6), current_date
    if period_type == "recent_30_days":
        return current_date - timedelta(days=29), current_date
    current_week_start = current_date - timedelta(days=current_date.weekday())
    if period_type == "current_week":
        return current_week_start, current_date
    if period_type == "previous_week":
        return (
            current_week_start - timedelta(days=7),
            current_week_start - timedelta(days=1),
        )
    raise ValueError("unsupported report insight period type")


def _legacy_report_insight_query_scope(text: str, query_kind: str) -> str:
    """Lazy bridge for historical parser tests; never used by Agent2 production."""

    from app.agent2.report_insight_intent import report_insight_query_scope

    return report_insight_query_scope(text, query_kind)


def _matched_named_users(
    text: str,
    users: Sequence[Any],
    *,
    teams: Sequence[Any],
) -> list[Any]:
    matched_names = {
        str(_value(user, "name") or "").strip()
        for user in users
        if str(_value(user, "name") or "").strip()
        and str(_value(user, "name") or "").strip() in text
    }
    if not matched_names:
        return []
    longest_length = max(len(name) for name in matched_names)
    longest_names = {name for name in matched_names if len(name) == longest_length}
    candidates = [
        user
        for user in users
        if str(_value(user, "name") or "").strip() in longest_names
    ]
    named_scope = _matched_named_scope(text, teams)
    if named_scope is None:
        return candidates
    target_team_ids = {
        str(_value(team, "id") or _value(team, "team_id") or "")
        for team in named_scope[2]
    }
    return [
        user
        for user in candidates
        if str(_value(user, "team_id") or "") in target_team_ids
    ]


def _query_scope_matches_target(
    *,
    query_text: str,
    query_kind: str,
    target: Any,
    teams: Sequence[Any],
) -> bool:
    scope = (
        _legacy_report_insight_query_scope(query_text, query_kind).strip().rstrip("的")
    )
    target_name = str(_value(target, "name") or "").strip()
    if not scope or not target_name:
        return False
    if scope == target_name:
        return True
    qualified_suffix = f"的{target_name}"
    if not scope.endswith(qualified_suffix):
        return False
    qualifier = scope[: -len(qualified_suffix)]
    named_scope = _matched_exact_named_scope(qualifier, teams)
    if named_scope is None:
        return False
    _scope_type, _resolved_label, resolved_teams = named_scope
    target_team_id = str(_value(target, "team_id") or "")
    return target_team_id in {
        str(_value(team, "id") or _value(team, "team_id") or "")
        for team in resolved_teams
    }


def _report_items(reports: Sequence[Any], field: str) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for report in reports:
        report_date = _as_date(_value(report, "report_date") or _value(report, "date"))
        values = _value(report, field) or ()
        if not isinstance(values, (list, tuple)):
            values = (values,)
        for value in values:
            item_text = str(value or "").strip()
            if not item_text or item_text in seen:
                continue
            seen.add(item_text)
            items.append(
                {
                    "date": report_date.isoformat() if report_date else "",
                    "text": item_text,
                }
            )
    return items


def _numbered_lines(items: Sequence[dict[str, str]], *, limit: int) -> list[str]:
    return [
        f"{index}. {item['text']}（{item['date']}）"
        if item.get("date")
        else f"{index}. {item['text']}"
        for index, item in enumerate(items[:limit], start=1)
    ]


def _scoped_report_items(
    reports: Sequence[Any],
    field: str,
    *,
    user_names: dict[str, str],
) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for report in reports:
        user_id = str(_value(report, "user_id") or "")
        report_date = _as_date(_value(report, "report_date") or _value(report, "date"))
        values = _value(report, field) or ()
        if not isinstance(values, (list, tuple)):
            values = (values,)
        for value in values:
            item_text = str(value or "").strip()
            key = (user_id, item_text)
            if not item_text or key in seen:
                continue
            seen.add(key)
            items.append(
                {
                    "user_id": user_id,
                    "user_name": user_names.get(user_id, "未命名人员"),
                    "date": report_date.isoformat() if report_date else "",
                    "text": item_text,
                }
            )
    return items


def _scoped_numbered_lines(items: Sequence[dict[str, str]], *, limit: int) -> list[str]:
    return [
        (
            f"{index}. {item['user_name']}：{item['text']}（{item['date']}）"
            if item.get("date")
            else f"{index}. {item['user_name']}：{item['text']}"
        )
        for index, item in enumerate(items[:limit], start=1)
    ]


@dataclass(frozen=True)
class _PlanFollowupAnalysis:
    no_followup_items: tuple[dict[str, Any], ...]
    in_progress_items: tuple[dict[str, Any], ...]
    mentioned_items: tuple[dict[str, Any], ...]
    completed_items: tuple[dict[str, Any], ...]
    pending_evidence_items: tuple[dict[str, Any], ...]

    @property
    def followed_up_count(self) -> int:
        return (
            len(self.in_progress_items)
            + len(self.mentioned_items)
            + len(self.completed_items)
        )


@dataclass(frozen=True)
class _UnclosedPresentation:
    analysis: _PlanFollowupAnalysis
    unclosed_items: tuple[dict[str, Any], ...]
    history_start: str
    needs_time_scope: bool
    preview_count: int
    preview_truncated: bool

    def fact_fields(self) -> dict[str, Any]:
        total = len(self.unclosed_items)
        fields: dict[str, Any] = {
            "unclosed_count": total,
            "unclosed_items": (
                [] if self.needs_time_scope else list(self.unclosed_items)
            ),
            "unclosed_items_withheld_count": (
                total if self.needs_time_scope else 0
            ),
            "unclosed_preview_count": self.preview_count,
            "unclosed_preview_truncated": self.preview_truncated,
            "unclosed_remaining_count": (
                total - self.preview_count if self.preview_truncated else 0
            ),
            "classification_counts_withheld_for_scope_choice": (
                self.needs_time_scope
            ),
            "needs_time_scope": self.needs_time_scope,
            "available_period_types": (
                list(UNCLOSED_SCOPE_PERIOD_OPTIONS)
                if self.needs_time_scope
                else []
            ),
        }
        if not self.needs_time_scope:
            fields.update(
                {
                    "followed_up_count": self.analysis.followed_up_count,
                    "in_progress_count": len(self.analysis.in_progress_items),
                    "completed_count": len(self.analysis.completed_items),
                    "pending_evidence_count": len(
                        self.analysis.pending_evidence_items
                    ),
                    "in_progress_items": list(self.analysis.in_progress_items),
                    "pending_evidence_items": list(
                        self.analysis.pending_evidence_items
                    ),
                }
            )
        return fields


def _build_unclosed_presentation(
    analysis: _PlanFollowupAnalysis,
    *,
    reports: Sequence[Any],
    period_type: str,
    preview_limit: int,
) -> _UnclosedPresentation:
    unclosed_items = tuple(analysis.no_followup_items)
    needs_time_scope = _unclosed_scope_requires_clarification(
        analysis,
        period_type=period_type,
    )
    preview_count = (
        0 if needs_time_scope else min(len(unclosed_items), preview_limit)
    )
    return _UnclosedPresentation(
        analysis=analysis,
        unclosed_items=unclosed_items,
        history_start=_first_report_date(reports),
        needs_time_scope=needs_time_scope,
        preview_count=preview_count,
        preview_truncated=(
            not needs_time_scope and len(unclosed_items) > preview_limit
        ),
    )


def _unclosed_scope_requires_clarification(
    analysis: _PlanFollowupAnalysis,
    *,
    period_type: str,
) -> bool:
    if period_type != "unspecified":
        return False
    detailed_item_count = (
        len(analysis.no_followup_items)
        + len(analysis.in_progress_items)
        + len(analysis.pending_evidence_items)
    )
    return detailed_item_count > UNCLOSED_SCOPE_CLARIFICATION_THRESHOLD


def _plan_followup_analysis(
    reports: Sequence[Any],
    *,
    user_names: dict[str, str],
    closure_across_users: bool,
    as_of_date: date,
    plan_start_date: date | None = None,
    plan_end_date: date | None = None,
) -> _PlanFollowupAnalysis:
    plans = [
        item
        for item in _dated_report_field_items(reports, "tomorrow_plan")
        if item["date"] < as_of_date
        and (plan_start_date is None or item["date"] >= plan_start_date)
        and (plan_end_date is None or item["date"] <= plan_end_date)
    ]
    work_entries = [
        item
        for item in _dated_report_field_items(reports, "today_work")
        if item["date"] <= as_of_date
    ]
    plans_by_date = _items_by_date(plans)
    work_by_date = _items_by_date(work_entries)
    tracker = _PlanFollowupTracker(
        user_names=user_names,
        closure_across_users=closure_across_users,
    )
    for report_date in sorted(set(plans_by_date) | set(work_by_date)):
        for work in work_by_date.get(report_date, ()):
            tracker.observe_work(work)
        for plan in plans_by_date.get(report_date, ()):
            tracker.add_or_refresh(plan)

    later_evidence_dates = _followup_evidence_dates(reports, as_of_date=as_of_date)
    classified: dict[str, list[dict[str, Any]]] = {
        "no_followup": [],
        "in_progress": [],
        "mentioned": [],
        "completed": [],
        "pending_evidence": [],
    }
    tracked_items = tracker.items()
    tracked_items.sort(
        key=lambda item: (
            item["first_plan_date"],
            item["last_plan_date"],
            item["user_name"],
            item["plan_text"],
        )
    )
    for item in tracked_items:
        serialized = {
            "user_id": str(item["user_id"]),
            "user_name": str(item["user_name"]),
            "plan_text": str(item["plan_text"]),
            "first_plan_date": item["first_plan_date"].isoformat(),
            "last_plan_date": item["last_plan_date"].isoformat(),
            "repeat_count": int(item.get("repeat_count") or 1),
        }
        followup_date = item.get("latest_followup_date")
        if followup_date is not None and followup_date > item["last_plan_date"]:
            serialized["latest_followup_date"] = followup_date.isoformat()
            serialized["latest_followup_text"] = str(
                item.get("latest_followup_text") or ""
            )
            followup_status = str(item.get("latest_followup_status") or "mentioned")
            classified[
                followup_status
                if followup_status in {"in_progress", "completed"}
                else "mentioned"
            ].append(serialized)
            continue
        if any(value > item["last_plan_date"] for value in later_evidence_dates):
            classified["no_followup"].append(serialized)
        else:
            classified["pending_evidence"].append(serialized)

    return _PlanFollowupAnalysis(
        no_followup_items=tuple(classified["no_followup"]),
        in_progress_items=tuple(classified["in_progress"]),
        mentioned_items=tuple(classified["mentioned"]),
        completed_items=tuple(classified["completed"]),
        pending_evidence_items=tuple(classified["pending_evidence"]),
    )


@dataclass(frozen=True)
class _ProblemAttentionAnalysis:
    active_items: tuple[dict[str, Any], ...]
    resolved_items: tuple[dict[str, Any], ...]


def _problem_attention_analysis(
    reports: Sequence[Any],
    *,
    user_names: dict[str, str],
    closure_across_users: bool,
    as_of_date: date,
) -> _ProblemAttentionAnalysis:
    problems = [
        item
        for item in _dated_report_field_items(reports, "problems")
        if item["date"] <= as_of_date and not _is_empty_problem(item["text"])
    ]
    work_entries = [
        item
        for item in _dated_report_field_items(reports, "today_work")
        if item["date"] <= as_of_date
    ]

    # Repeated descriptions of the same problem by the same person represent one
    # workstream. The newest occurrence is authoritative, because an issue may be
    # resolved and then genuinely recur.
    latest_problems: list[dict[str, Any]] = []
    for problem in problems:
        existing_index = next(
            (
                index
                for index, existing in enumerate(latest_problems)
                if existing["user_id"] == problem["user_id"]
                and _same_work_item(existing["text"], problem["text"])
            ),
            None,
        )
        if existing_index is None:
            latest_problems.append(problem)
        else:
            latest_problems[existing_index] = problem

    active_items: list[dict[str, Any]] = []
    resolved_items: list[dict[str, Any]] = []
    for problem in latest_problems:
        decisive_followups: list[dict[str, Any]] = []
        for work in work_entries:
            if work["date"] <= problem["date"]:
                continue
            if (
                not closure_across_users
                and work["user_id"] != problem["user_id"]
            ):
                continue
            status = _work_text_followup_status(problem["text"], work["text"])
            if status not in {"completed", "in_progress"}:
                continue
            decisive_followups.append({**work, "status": status})

        serialized: dict[str, Any] = {
            "user_id": str(problem["user_id"]),
            "user_name": user_names.get(str(problem["user_id"]), "未命名人员"),
            "date": problem["date"].isoformat(),
            "text": str(problem["text"]),
        }
        if not decisive_followups:
            active_items.append(serialized)
            continue

        latest_date = max(item["date"] for item in decisive_followups)
        latest_followups = [
            item for item in decisive_followups if item["date"] == latest_date
        ]
        # If the same day's records conflict, keep the item visible for review.
        latest_status = (
            "in_progress"
            if any(item["status"] == "in_progress" for item in latest_followups)
            else "completed"
        )
        latest_followup = next(
            item for item in reversed(latest_followups) if item["status"] == latest_status
        )
        serialized.update(
            {
                "latest_followup_date": latest_date.isoformat(),
                "latest_followup_text": str(latest_followup["text"]),
                "latest_followup_status": latest_status,
            }
        )
        if latest_status == "completed":
            resolved_items.append(serialized)
        else:
            active_items.append(serialized)

    def sort_key(item: dict[str, Any]) -> tuple[str, str, str]:
        return (item["date"], item["user_name"], item["text"])

    active_items.sort(key=sort_key)
    resolved_items.sort(key=sort_key)
    return _ProblemAttentionAnalysis(
        active_items=tuple(active_items),
        resolved_items=tuple(resolved_items),
    )


def _active_attention_plan_items(
    analysis: _PlanFollowupAnalysis,
) -> list[dict[str, Any]]:
    active_items: list[dict[str, Any]] = []
    status_groups = (
        ("no_followup", analysis.no_followup_items),
        ("in_progress", analysis.in_progress_items),
        ("pending_evidence", analysis.pending_evidence_items),
    )
    for status, items in status_groups:
        for item in items:
            if not _is_attention_plan(item["plan_text"]):
                continue
            active_items.append(
                {
                    "user_id": str(item["user_id"]),
                    "user_name": str(item["user_name"]),
                    "date": str(item["last_plan_date"]),
                    "text": str(item["plan_text"]),
                    "first_plan_date": str(item["first_plan_date"]),
                    "last_plan_date": str(item["last_plan_date"]),
                    "repeat_count": int(item.get("repeat_count") or 1),
                    "followup_status": status,
                    **(
                        {
                            "latest_followup_date": str(
                                item.get("latest_followup_date") or ""
                            ),
                            "latest_followup_text": str(
                                item.get("latest_followup_text") or ""
                            ),
                        }
                        if status == "in_progress"
                        else {}
                    ),
                }
            )
    active_items.sort(key=lambda item: (item["date"], item["user_name"], item["text"]))
    return active_items


def _unclosed_plan_items(
    reports: Sequence[Any],
    *,
    user_names: dict[str, str],
    closure_across_users: bool,
    as_of_date: date,
    plan_start_date: date | None = None,
    plan_end_date: date | None = None,
) -> list[dict[str, Any]]:
    """Compatibility wrapper for callers that only need missing follow-up rows."""

    return list(
        _plan_followup_analysis(
            reports,
            user_names=user_names,
            closure_across_users=closure_across_users,
            as_of_date=as_of_date,
            plan_start_date=plan_start_date,
            plan_end_date=plan_end_date,
        ).no_followup_items
    )


def _followup_evidence_dates(
    reports: Sequence[Any],
    *,
    as_of_date: date,
) -> tuple[date, ...]:
    evidence_dates: set[date] = set()
    for report in reports:
        report_date = _as_date(_value(report, "report_date") or _value(report, "date"))
        if report_date is None or report_date > as_of_date:
            continue
        status = str(_value(report, "status") or "").strip().lower()
        today_work = _value(report, "today_work") or ()
        if not isinstance(today_work, (list, tuple)):
            today_work = (today_work,)
        has_work = any(
            not _is_empty_plan_or_work(str(item or "")) for item in today_work
        )
        if has_work or status in {"completed", "pending_confirmation"}:
            evidence_dates.add(report_date)
    return tuple(sorted(evidence_dates))


_MAX_FUZZY_WORK_CANDIDATES = 64


class _PlanFollowupTracker:
    def __init__(
        self,
        *,
        user_names: dict[str, str],
        closure_across_users: bool,
    ) -> None:
        self.user_names = user_names
        self.closure_across_users = closure_across_users
        self._next_id = 1
        self._items: dict[int, dict[str, Any]] = {}
        self._variant_index: dict[str, set[int]] = {}
        self._gram_index: dict[str, set[int]] = {}
        self._identifier_index: dict[tuple[str, ...], set[int]] = {}
        self._user_variant_index: dict[tuple[str, str], set[int]] = {}
        self._user_gram_index: dict[tuple[str, str], set[int]] = {}
        self._user_identifier_index: dict[tuple[str, tuple[str, ...]], set[int]] = {}

    def add_or_refresh(self, plan: dict[str, Any]) -> None:
        candidate_ids = self._candidate_ids(plan["text"], user_id=plan["user_id"])
        matching_ids = [
            item_id
            for item_id in sorted(candidate_ids)
            if _same_work_item(self._items[item_id]["plan_text"], plan["text"])
        ]
        existing_id = matching_ids[0] if matching_ids else None
        if existing_id is not None:
            item = self._items[existing_id]
            self._remove_from_indexes(existing_id, item["plan_text"])
            item["last_plan_date"] = plan["date"]
            item["plan_text"] = plan["text"]
            item["repeat_count"] = int(item.get("repeat_count") or 1) + 1
            self._add_to_indexes(existing_id, item["plan_text"])
            for duplicate_id in matching_ids[1:]:
                duplicate = self._items.get(duplicate_id)
                if duplicate is None:
                    continue
                self._remove_from_indexes(duplicate_id, duplicate["plan_text"])
                item["first_plan_date"] = min(
                    item["first_plan_date"], duplicate["first_plan_date"]
                )
                item["repeat_count"] += int(duplicate.get("repeat_count") or 1)
                duplicate_followup_date = duplicate.get("latest_followup_date")
                if duplicate_followup_date is not None and (
                    item.get("latest_followup_date") is None
                    or duplicate_followup_date > item["latest_followup_date"]
                ):
                    item["latest_followup_date"] = duplicate_followup_date
                    item["latest_followup_text"] = duplicate.get(
                        "latest_followup_text", ""
                    )
                    item["latest_followup_status"] = duplicate.get(
                        "latest_followup_status", "mentioned"
                    )
                del self._items[duplicate_id]
            return

        item_id = self._next_id
        self._next_id += 1
        self._items[item_id] = {
            "user_id": plan["user_id"],
            "user_name": self.user_names.get(plan["user_id"], "未命名人员"),
            "plan_text": plan["text"],
            "first_plan_date": plan["date"],
            "last_plan_date": plan["date"],
            "repeat_count": 1,
            "latest_followup_date": None,
            "latest_followup_text": "",
            "latest_followup_status": "",
        }
        self._add_to_indexes(item_id, plan["text"])

    def observe_work(self, work: dict[str, Any]) -> None:
        user_id = None if self.closure_across_users else work["user_id"]
        candidate_ids = self._candidate_ids(work["text"], user_id=user_id)
        for item_id in sorted(candidate_ids):
            item = self._items.get(item_id)
            if item is None or work["date"] <= item["last_plan_date"]:
                continue
            followup_status = _work_text_followup_status(
                item["plan_text"], work["text"]
            )
            if followup_status is None:
                continue
            item["latest_followup_date"] = work["date"]
            item["latest_followup_text"] = work["text"]
            item["latest_followup_status"] = followup_status

    def items(self) -> list[dict[str, Any]]:
        return list(self._items.values())

    def _candidate_ids(self, text: str, *, user_id: str | None) -> set[int]:
        candidate_ids: set[int] = set()
        for fragment in _work_candidate_fragments(text):
            variants = _work_item_variants(fragment)
            identifiers = _work_item_identifiers(fragment)
            if identifiers:
                candidate_ids.update(
                    self._identifier_posting(identifiers, user_id=user_id)
                )
                continue
            candidate_ids.update(
                item_id
                for variant in variants
                for item_id in self._variant_posting(variant, user_id=user_id)
            )
            candidate_ids.update(
                self._limited_fuzzy_candidate_ids(variants, user_id=user_id)
            )
        return candidate_ids

    def _limited_fuzzy_candidate_ids(
        self,
        variants: set[str],
        *,
        user_id: str | None,
    ) -> set[int]:
        postings = [
            values
            for gram in _work_item_grams(variants)
            if (values := self._gram_posting(gram, user_id=user_id))
        ]
        postings.sort(key=len)
        candidate_ids: set[int] = set()
        signatures: set[tuple[str, ...]] = set()
        for posting in postings[:6]:
            for item_id in posting:
                candidate_ids.add(item_id)
                signature = tuple(
                    sorted(_work_item_variants(self._items[item_id]["plan_text"]))
                )
                signatures.add(signature)
                if len(signatures) > _MAX_FUZZY_WORK_CANDIDATES:
                    return set()
        return candidate_ids

    def _variant_posting(
        self,
        variant: str,
        *,
        user_id: str | None,
    ) -> set[int] | tuple[int, ...]:
        if user_id is None:
            return self._variant_index.get(variant, ())
        return self._user_variant_index.get((user_id, variant), ())

    def _gram_posting(
        self,
        gram: str,
        *,
        user_id: str | None,
    ) -> set[int] | tuple[int, ...]:
        if user_id is None:
            return self._gram_index.get(gram, ())
        return self._user_gram_index.get((user_id, gram), ())

    def _identifier_posting(
        self,
        identifiers: tuple[str, ...],
        *,
        user_id: str | None,
    ) -> set[int] | tuple[int, ...]:
        if user_id is None:
            return self._identifier_index.get(identifiers, ())
        return self._user_identifier_index.get((user_id, identifiers), ())

    def _add_to_indexes(self, item_id: int, text: str) -> None:
        user_id = str(self._items[item_id]["user_id"])
        variants = _work_item_variants(text)
        for variant in variants:
            self._variant_index.setdefault(variant, set()).add(item_id)
            self._user_variant_index.setdefault((user_id, variant), set()).add(item_id)
        for gram in _work_item_grams(variants):
            self._gram_index.setdefault(gram, set()).add(item_id)
            self._user_gram_index.setdefault((user_id, gram), set()).add(item_id)
        identifiers = _work_item_identifiers(text)
        if identifiers:
            self._identifier_index.setdefault(identifiers, set()).add(item_id)
            self._user_identifier_index.setdefault((user_id, identifiers), set()).add(
                item_id
            )

    def _remove_from_indexes(self, item_id: int, text: str) -> None:
        user_id = str(self._items[item_id]["user_id"])
        variants = _work_item_variants(text)
        for variant in variants:
            _discard_index_value(self._variant_index, variant, item_id)
            _discard_index_value(self._user_variant_index, (user_id, variant), item_id)
        for gram in _work_item_grams(variants):
            _discard_index_value(self._gram_index, gram, item_id)
            _discard_index_value(self._user_gram_index, (user_id, gram), item_id)
        identifiers = _work_item_identifiers(text)
        if identifiers:
            _discard_index_value(self._identifier_index, identifiers, item_id)
            _discard_index_value(
                self._user_identifier_index,
                (user_id, identifiers),
                item_id,
            )


def _discard_index_value(index: dict[Any, set[int]], key: Any, item_id: int) -> None:
    values = index.get(key)
    if values is None:
        return
    values.discard(item_id)
    if not values:
        del index[key]


def _items_by_date(items: Sequence[dict[str, Any]]) -> dict[date, list[dict[str, Any]]]:
    grouped: dict[date, list[dict[str, Any]]] = {}
    for item in items:
        grouped.setdefault(item["date"], []).append(item)
    return grouped


def _dated_report_field_items(
    reports: Sequence[Any], field: str
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for report in reports:
        report_date = _as_date(_value(report, "report_date") or _value(report, "date"))
        if report_date is None:
            continue
        user_id = str(_value(report, "user_id") or "")
        values = _value(report, field) or ()
        if not isinstance(values, (list, tuple)):
            values = (values,)
        for value in values:
            item_text = str(value or "").strip()
            if _is_empty_plan_or_work(item_text):
                continue
            items.append({"user_id": user_id, "date": report_date, "text": item_text})
    return sorted(items, key=lambda item: (item["date"], item["user_id"], item["text"]))


def _unclosed_numbered_lines(
    items: Sequence[dict[str, str]],
    *,
    include_user: bool,
    limit: int,
) -> list[str]:
    lines: list[str] = []
    for index, item in enumerate(items[:limit], start=1):
        first_date = item.get("first_plan_date", "")
        last_date = item.get("last_plan_date", "")
        if first_date and last_date and first_date != last_date:
            date_text = f"首次计划：{first_date}；最近计划：{last_date}"
        else:
            date_text = f"计划日期：{first_date or last_date}"
        owner = f"{item.get('user_name', '未命名人员')}：" if include_user else ""
        lines.append(f"{index}. {owner}{item['plan_text']}（{date_text}）")
    return lines


def _first_report_date(reports: Sequence[Any]) -> str:
    dates = [
        report_date
        for report in reports
        if (
            report_date := _as_date(
                _value(report, "report_date") or _value(report, "date")
            )
        )
        is not None
    ]
    return min(dates).isoformat() if dates else ""


_WORK_TEXT_SEPARATOR = re.compile(r"[^\u4e00-\u9fffA-Za-z0-9]+")
_CHINESE_FLOOR_NUMBER = re.compile(r"([零〇一二三四五六七八九十]{1,3})(?=楼)")
_WORK_CANDIDATE_SEPARATOR = re.compile(
    r"[，,；;。.!！？、\n]+|(?:同时|另外|此外|并且|以及)|"
    r"并(?=(?:已经|现已|已|完成|整理|跟进|推进|处理|办理|协调|沟通|对接|提交|"
    r"开展|组织|落实|复核|核对|准备|继续|持续|需要|需|待|发送))"
)
_WORK_IDENTIFIER = re.compile(r"[a-z]+|\d+")
_WORK_ACTION_PREFIXES = tuple(
    sorted(
        (
            "下一步",
            "进一步",
            "已完成",
            "明天",
            "明日",
            "后续",
            "预计",
            "计划",
            "准备",
            "需要",
            "继续",
            "持续",
            "完成",
            "推进",
            "跟进",
            "处理",
            "开展",
            "组织",
            "落实",
            "做好",
            "协助",
            "配合",
            "协调",
            "沟通",
            "对接",
            "联系",
            "整理",
            "复核",
            "核对",
            "拟",
            "将",
            "需",
            "待",
        ),
        key=len,
        reverse=True,
    )
)
_WORK_SUBJECT_EDGE_TERMS = tuple(
    sorted(
        {
            *_WORK_ACTION_PREFIXES,
            "收集",
            "催收",
            "已",
            "完毕",
            "了",
            "的",
        },
        key=len,
        reverse=True,
    )
)


def _same_work_item(left: str, right: str) -> bool:
    left_normalized = _normalize_work_item(left)
    right_normalized = _normalize_work_item(right)
    if not left_normalized or not right_normalized:
        return False
    left_identifiers = tuple(_WORK_IDENTIFIER.findall(left_normalized))
    right_identifiers = tuple(_WORK_IDENTIFIER.findall(right_normalized))
    if (
        left_identifiers or right_identifiers
    ) and left_identifiers != right_identifiers:
        return False
    return any(
        _strong_text_overlap(left_variant, right_variant)
        for left_variant in _work_item_variants(left)
        for right_variant in _work_item_variants(right)
    )


def _normalize_work_item(text: str) -> str:
    compact = _WORK_TEXT_SEPARATOR.sub("", str(text or "").lower())
    return _CHINESE_FLOOR_NUMBER.sub(
        lambda match: str(_small_chinese_number(match.group(1))),
        compact,
    )


def _small_chinese_number(value: str) -> int:
    digits = {
        "零": 0,
        "〇": 0,
        "一": 1,
        "二": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    if "十" not in value:
        return int("".join(str(digits[character]) for character in value))
    left, _, right = value.partition("十")
    tens = digits[left] if left else 1
    ones = digits[right] if right else 0
    return tens * 10 + ones


def _work_item_core(text: str) -> str:
    core = text
    changed = True
    while changed and core:
        changed = False
        for prefix in _WORK_ACTION_PREFIXES:
            if core.startswith(prefix) and len(core) > len(prefix):
                core = core[len(prefix) :]
                changed = True
                break
    return core


def _work_item_subject_core(text: str) -> str:
    core = _work_item_core(text)
    changed = True
    while changed and core:
        changed = False
        for term in _WORK_SUBJECT_EDGE_TERMS:
            if core.startswith(term) and len(core) > len(term):
                core = core[len(term) :]
                changed = True
                break
            if core.endswith(term) and len(core) > len(term):
                core = core[: -len(term)]
                changed = True
                break
    return core


def _work_item_variants(text: str) -> set[str]:
    variants: set[str] = set()
    fragments = _work_candidate_fragments(text)
    for fragment in fragments:
        normalized = _normalize_work_item(fragment)
        if not normalized:
            continue
        core = _work_item_core(normalized)
        subject_core = _work_item_subject_core(normalized)
        variants.update(
            value
            for value in (normalized, core, subject_core)
            if value and (len(value) >= 4 or value == normalized)
        )
    return variants


def _work_item_identifiers(text: str) -> tuple[str, ...]:
    return tuple(_WORK_IDENTIFIER.findall(_normalize_work_item(text)))


def _work_candidate_fragments(text: str) -> list[str]:
    fragments = [
        fragment.strip()
        for fragment in _WORK_CANDIDATE_SEPARATOR.split(str(text or ""))
        if fragment.strip()
    ]
    return fragments or [str(text or "")]


def _work_item_grams(variants: set[str]) -> set[str]:
    grams: set[str] = set()
    for variant in variants:
        if len(variant) == 1:
            grams.add(variant)
            continue
        grams.update(variant[index : index + 2] for index in range(len(variant) - 1))
    return grams


def _work_text_closes_plan(plan_text: str, work_text: str) -> bool:
    return _work_text_followup_status(plan_text, work_text) in {
        "mentioned",
        "completed",
    }


def _work_text_followup_status(plan_text: str, work_text: str) -> str | None:
    clauses = _work_status_clause_fragments(work_text)
    matching_clauses: list[str] = []
    for index, clause in enumerate(clauses):
        if not _same_work_item(plan_text, clause):
            continue
        matching_text = clause
        following_index = index + 1
        while following_index < len(clauses) and _work_clause_is_status_only(
            clauses[following_index]
        ):
            matching_text += clauses[following_index]
            following_index += 1
        matching_clauses.append(matching_text)
    if not matching_clauses and _same_work_item(plan_text, work_text):
        matching_clauses = [work_text]
    if not matching_clauses:
        return None
    final_status = _work_text_final_status(matching_clauses[-1])
    if final_status == "completed":
        return "completed"
    if final_status == "unfinished":
        return "in_progress"
    return "mentioned"


_WORK_STATUS_ACTION = (
    r"(?:完成|办结|解决|闭环|结束|开始|启动|开展|通过|获批|批准|处理|办理|"
    r"推进|跟进|落实|确认|审批|提交|协调|沟通|对接|联系|等待)"
)
_WORK_STATUS_MODIFIER = r"(?:(?:继续|进一步|后续|持续|再次|再)){0,2}"
_WORK_UNFINISHED_STATUS_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        rf"(?:尚未|仍未|还未|没有|没|未)(?:能|能够)?{_WORK_STATUS_MODIFIER}{_WORK_STATUS_ACTION}",
        rf"(?:无法|不能|难以){_WORK_STATUS_MODIFIER}{_WORK_STATUS_ACTION}",
        rf"(?:仍在|还在|正在){_WORK_STATUS_MODIFIER}{_WORK_STATUS_ACTION}",
        rf"(?:仍待|尚待|还待|有待|待){_WORK_STATUS_MODIFIER}{_WORK_STATUS_ACTION}",
        rf"(?:仍需|还需|尚需|需要|需){_WORK_STATUS_MODIFIER}{_WORK_STATUS_ACTION}",
        rf"(?:继续|持续|后续|进一步){_WORK_STATUS_ACTION}",
        r"(?:仍需|还需|尚需|需要|需|待)(?:补充|修改|返工|整改|重做|重提|重新提交|完善|修订|调整|补正|重审)",
        r"(?:未获|尚未获|仍未获|还未获)(?:通过|批|批准|同意)",
        r"(?:尚未|仍未|还未|没有|未)(?:成功|办成|达成)",
        r"(?:尚未|仍未|还未|没有|未能|无法|不能)(?:获得|取得)(?:进展|结果|结论|反馈|通过|批准)",
        r"(?:尚未|仍未|还未|还没有|没有|未)(?:全部|完全|彻底)完成",
        r"(?:仅|只)?完成(?:了)?(?:一半|部分|一部分|大部分)(?:工作|事项|任务)?",
        r"完成(?:了)?(?:[一二三四五六七八九]成|[一二三四五六七八九十]+分之[一二三四五六七八九十]+)",
        r"(?:部分|一部分|大部分)(?:工作|事项|任务)?(?:已)?完成",
        r"(?:失败|受阻|受挫|中断|停滞|卡住)",
        r"不(?:予)?(?:通过|批准|同意|受理)",
        r"(?:被|已被)?(?:退回|驳回|拒绝|搁置|暂停|暂缓)",
        r"(?:(?:处于|处在))?(?:进行中|处理中|办理中|推进中|跟进中|审批中|协调中|沟通中|对接中|等待中)",
        r"(?:尚无|暂无|还没|没有)(?:结果|结论|反馈|进展)",
    )
)
_WORK_COMPLETED_STATUS_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"(?:已经|现已|已)(?:完成|办结|解决|闭环|结束|通过|获批|批准)",
        r"(?:完成|办结|解决|闭环|结束|通过|获批|批准)",
        r"(?:已经|现已|已)(?:解决|处理|排除|消除)[\u4e00-\u9fffA-Za-z0-9]{0,30}"
        r"(?:受阻|失败|受挫|中断|停滞|卡住)(?:问题|事项|障碍)?",
        r"(?:没有|不存在|无)(?:任何)?未完成(?:的)?(?:事项|工作|任务|计划)?",
        rf"(?:没有|不存在|无)(?:任何)?需要{_WORK_STATUS_MODIFIER}"
        r"(?:跟进|处理|推进|办理|协调|沟通|对接|审批|确认)(?:的)?(?:事项|工作|任务|计划)?",
        rf"(?:不再需要|不再需|不需要|不需|无需){_WORK_STATUS_MODIFIER}"
        r"(?:跟进|处理|推进|办理|协调|沟通|对接|审批|确认|提交|联系|等待)",
    )
)
_WORK_STATUS_CONTEXT_PREFIX = re.compile(
    r"^(?:(?:当前|目前|后续|现阶段|现在|接下来|之后|随后|暂时|眼下|后又|后来|整体))+"
)


def _work_clause_is_status_only(text: str) -> bool:
    compact = _normalize_work_item(text)
    status_text = _WORK_STATUS_CONTEXT_PREFIX.sub("", compact)
    return any(
        start == 0 and end == len(status_text)
        for start, end, _status in _work_status_events(status_text)
    )


def _work_text_final_status(text: str) -> str | None:
    compact = _normalize_work_item(text)
    events = _work_status_events(compact)
    if not events:
        return None
    return max(events, key=lambda event: (event[0], event[1]))[2]


def _work_status_events(text: str) -> list[tuple[int, int, str]]:
    compact = _normalize_work_item(text)
    unfinished_events = [
        (match.start(), match.end(), "unfinished")
        for pattern in _WORK_UNFINISHED_STATUS_PATTERNS
        for match in pattern.finditer(compact)
    ]
    completed_events = [
        (match.start(), match.end(), "completed")
        for pattern in _WORK_COMPLETED_STATUS_PATTERNS
        for match in pattern.finditer(compact)
    ]
    filtered_unfinished = [
        event
        for event in unfinished_events
        if not any(
            completed_start <= event[0] and event[1] <= completed_end
            for completed_start, completed_end, _status in completed_events
        )
    ]
    filtered_completed = [
        event
        for event in completed_events
        if not any(
            unfinished_start <= event[0] and event[1] <= unfinished_end
            for unfinished_start, unfinished_end, _status in unfinished_events
        )
    ]
    return [*filtered_unfinished, *filtered_completed]


def _work_status_clause_fragments(text: str) -> list[str]:
    clauses: list[str] = []
    for fragment in _work_candidate_fragments(text):
        clauses.extend(_split_status_conjunctions(fragment))
    return clauses


def _split_status_conjunctions(text: str) -> list[str]:
    for match in re.finditer("和", text):
        left = text[: match.start()].strip()
        right = text[match.end() :].strip()
        if not left or not right:
            continue
        if not _work_status_events(left) or not _work_status_events(right):
            continue
        return [
            *_split_status_conjunctions(left),
            *_split_status_conjunctions(right),
        ]
    return [text]


def _strong_text_overlap(left: str, right: str) -> bool:
    if left == right:
        return True
    if _has_conflicting_short_qualifiers(left, right):
        return False
    shorter, longer = sorted((left, right), key=len)
    if len(shorter) >= 2 and shorter in longer:
        return True
    if len(shorter) < 4:
        return False
    left_pairs = {left[index : index + 2] for index in range(len(left) - 1)}
    right_pairs = {right[index : index + 2] for index in range(len(right) - 1)}
    if not left_pairs or not right_pairs:
        return False
    similarity = (
        2 * len(left_pairs & right_pairs) / (len(left_pairs) + len(right_pairs))
    )
    if similarity >= 0.8:
        return True
    matcher = SequenceMatcher(None, left, right, autojunk=False)
    matched_characters = sum(block.size for block in matcher.get_matching_blocks())
    return matched_characters / len(shorter) >= 0.7


def _has_conflicting_short_qualifiers(left: str, right: str) -> bool:
    common_prefix_length = 0
    for index in range(min(len(left), len(right))):
        if left[index] != right[index]:
            break
        common_prefix_length = index + 1

    common_suffix_length = 0
    remaining = min(len(left), len(right)) - common_prefix_length
    for offset in range(1, remaining + 1):
        if left[-offset] != right[-offset]:
            break
        common_suffix_length = offset

    left_end = len(left) - common_suffix_length if common_suffix_length else len(left)
    right_end = (
        len(right) - common_suffix_length if common_suffix_length else len(right)
    )
    left_difference = left[common_prefix_length:left_end]
    right_difference = right[common_prefix_length:right_end]
    common_context_length = common_prefix_length + common_suffix_length
    return bool(
        common_context_length >= 4
        and left_difference
        and right_difference
        and left_difference != right_difference
        and len(left_difference) <= 4
        and len(right_difference) <= 4
    )


def _is_empty_plan_or_work(text: str) -> bool:
    compact = _normalize_work_item(text)
    return compact in {
        "",
        "无",
        "暂无",
        "无计划",
        "暂无计划",
        "无后续计划",
        "无安排",
        "暂无安排",
        "无工作",
        "暂无工作",
    }


def _looks_like_organization_scope(text: str) -> bool:
    if any(marker in text for marker in ("部门", "团队", "工作组", "中心")):
        return True
    if text.endswith(("部", "组")):
        return True
    return (
        re.search(
            r"[\u4e00-\u9fffA-Za-z0-9·]{2,12}部(?:有|还有|存在|没|未|没有|尚未)", text
        )
        is not None
    )


def _is_generic_organization_label(value: str) -> bool:
    return value in {
        "部门",
        "本部门",
        "我们部门",
        "咱们部门",
        "当前部门",
        "团队",
        "本团队",
        "我们团队",
        "咱们团队",
        "当前团队",
        "工作组",
        "本工作组",
        "中心",
        "本中心",
    }


def _matched_named_scope(
    text: str, teams: Sequence[Any]
) -> tuple[str, str, list[Any]] | None:
    matches = [
        team for team in teams if str(_value(team, "name") or "").strip() in text
    ]
    if matches:
        longest = max(len(str(_value(team, "name") or "")) for team in matches)
        longest_matches = [
            team for team in matches if len(str(_value(team, "name") or "")) == longest
        ]
        if len(longest_matches) == 1:
            label = str(_value(longest_matches[0], "name") or "").strip()
            return "team", label, longest_matches

    department_names = {
        str(_value(team, "department_name") or "").strip()
        for team in teams
        if str(_value(team, "department_name") or "").strip()
        and str(_value(team, "department_name") or "").strip() in text
    }
    if department_names:
        label = max(department_names, key=len)
        department_teams = [
            team
            for team in teams
            if str(_value(team, "department_name") or "").strip() == label
        ]
        return "department", label, department_teams

    alias_matches = [
        (alias, team)
        for team in teams
        for alias in _organization_aliases(str(_value(team, "name") or ""))
        if alias in text
    ]
    if alias_matches:
        longest_alias = max(len(alias) for alias, _team in alias_matches)
        longest_alias_matches = [
            (alias, team)
            for alias, team in alias_matches
            if len(alias) == longest_alias
        ]
        unique_teams = {
            str(_value(team, "id") or _value(team, "team_id") or ""): team
            for _alias, team in longest_alias_matches
        }
        if len(unique_teams) == 1:
            target_team = next(iter(unique_teams.values()))
            label = str(_value(target_team, "name") or "").strip()
            return "team", label, [target_team]

    alias_departments = [
        (alias, department_name)
        for department_name in {
            str(_value(team, "department_name") or "").strip()
            for team in teams
            if str(_value(team, "department_name") or "").strip()
        }
        for alias in _organization_aliases(department_name)
        if alias in text
    ]
    if not alias_departments:
        return None
    longest_alias = max(len(alias) for alias, _department in alias_departments)
    labels = {
        department_name
        for alias, department_name in alias_departments
        if len(alias) == longest_alias
    }
    if len(labels) != 1:
        return None
    label = next(iter(labels))
    department_teams = [
        team
        for team in teams
        if str(_value(team, "department_name") or "").strip() == label
    ]
    return "department", label, department_teams


def _matched_exact_named_scope(
    scope: str,
    teams: Sequence[Any],
) -> tuple[str, str, list[Any]] | None:
    requested = "".join(str(scope or "").split()).rstrip("的")
    if not requested:
        return None
    matched = _matched_named_scope(requested, teams)
    if matched is None:
        return None
    _scope_type, resolved_label, _target_teams = matched
    if requested == resolved_label or requested in _organization_aliases(
        resolved_label
    ):
        return matched
    return None


def _organization_aliases(value: str) -> set[str]:
    name = "".join(str(value or "").split())
    aliases: set[str] = set()
    if "管理" in name:
        shortened = name.replace("管理", "")
        if len(shortened) >= 2 and shortened != name:
            aliases.add(shortened)
    return aliases


def _can_read_team(*, requester: Any, target_team: Any, teams: Sequence[Any]) -> bool:
    requester_dingtalk_id = str(_value(requester, "dingtalk_user_id") or "")
    if requester_dingtalk_id in ALL_ACCESS_DINGTALK_USER_IDS:
        return True
    requester_role = str(_value(requester, "role") or "member").strip().lower()
    requester_team_id = str(_value(requester, "team_id") or "")
    target_team_id = str(
        _value(target_team, "id") or _value(target_team, "team_id") or ""
    )
    requester_department = _team_department(teams, requester_team_id)
    target_department = str(_value(target_team, "department_name") or "").strip()
    if requester_department and requester_department == target_department:
        return True
    if requester_role in TEAM_LEADER_ROLES:
        return bool(requester_team_id and requester_team_id == target_team_id)
    if requester_role in DEPARTMENT_HEAD_ROLES:
        return bool(requester_department and requester_department == target_department)
    return False


def _team_permission_denied_answer(
    target_team: Any, *, current_date: date
) -> ReportInsightAnswer:
    team_name = str(_value(target_team, "name") or "该部门")
    reply = f"你没有权限查看{team_name}的部门日报。"
    return ReportInsightAnswer(
        text=reply,
        evidence=KnowledgeEvidenceFrame(
            source_type=REPORT_INSIGHT_SOURCE_TYPE,
            source_id=f"daily_report_team_permission_denied:{team_name}",
            title="部门日报查询权限校验",
            summary=reply,
            facts={
                "query_kind": "permission_denied",
                "scope_type": "team",
                "scope_label": team_name,
                "permission_allowed": False,
            },
            confidence=1.0,
            freshness=current_date.isoformat(),
        ),
    )


def _can_read_department(
    *, requester: Any, department_name: str, teams: Sequence[Any]
) -> bool:
    requester_dingtalk_id = str(_value(requester, "dingtalk_user_id") or "")
    if requester_dingtalk_id in ALL_ACCESS_DINGTALK_USER_IDS:
        return True
    requester_team_id = str(_value(requester, "team_id") or "")
    return _team_department(teams, requester_team_id) == department_name


def _department_permission_denied_answer(
    department_name: str, *, current_date: date
) -> ReportInsightAnswer:
    reply = f"你没有权限查看{department_name}的部门日报。"
    return ReportInsightAnswer(
        text=reply,
        evidence=KnowledgeEvidenceFrame(
            source_type=REPORT_INSIGHT_SOURCE_TYPE,
            source_id=f"daily_report_department_permission_denied:{department_name}",
            title="部门日报查询权限校验",
            summary=reply,
            facts={
                "query_kind": "permission_denied",
                "scope_type": "department",
                "scope_label": department_name,
                "permission_allowed": False,
            },
            confidence=1.0,
            freshness=current_date.isoformat(),
        ),
    )


def _is_empty_problem(text: str) -> bool:
    compact = "".join(str(text or "").split()).strip("。！!，,")
    return compact in {
        "暂无问题",
        "暂无明显问题",
        "无问题",
        "无明显问题",
        "没有问题",
        "没问题",
    }


def _is_attention_plan(text: str) -> bool:
    return any(
        marker in str(text or "")
        for marker in (
            "待",
            "需",
            "跟进",
            "催",
            "确认",
            "协调",
            "风险",
            "问题",
            "异常",
            "未",
            "开庭",
            "诉讼",
            "系统",
            "权限",
        )
    )


def _team_department(teams: Sequence[Any], team_id: str) -> str:
    for team in teams:
        candidate_id = str(_value(team, "id") or _value(team, "team_id") or "")
        if candidate_id == team_id:
            return str(_value(team, "department_name") or "").strip()
    return ""


def _database_user_ids(user_ids: Sequence[str]) -> list[UUID]:
    return _database_ids(user_ids)


def _database_ids(values: Sequence[str]) -> list[UUID]:
    database_values: list[UUID] = []
    for value in values:
        try:
            database_values.append(UUID(str(value)))
        except (TypeError, ValueError, AttributeError):
            continue
    return database_values


def _value(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None
