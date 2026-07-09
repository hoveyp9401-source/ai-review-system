from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Protocol, Sequence

from app.agent2.context_pack import KnowledgeEvidenceFrame


STRUCTURED_SOURCE_PRIORITY = {
    "org_directory": 0,
    "case_registry": 1,
    "case_table_rag": 2,
    "personnel_registry": 3,
    "daily_report_history": 4,
    "document_repository": 5,
    "vector_rag": 6,
}

ACTIVE_CASE_STATUSES = {"active", "in_progress", "open", "在办", "办理中", "进行中"}
TEAM_LEADER_ROLES = {"team_lead", "team_leader", "team_manager", "leader", "manager", "负责人", "团队负责人"}
DEPARTMENT_HEAD_ROLES = {"department_head", "dept_head", "department_manager", "admin", "部门负责人", "部长"}


@dataclass(frozen=True)
class KnowledgeQuery:
    text: str
    user_id: str = ""
    dingtalk_user_id: str = ""
    intent: str = ""
    source_types: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class KnowledgeResolution:
    query: KnowledgeQuery
    evidence: tuple[KnowledgeEvidenceFrame, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def status(self) -> str:
        return "available" if self.evidence else "no_reliable_evidence"


class KnowledgeAdapter(Protocol):
    source_type: str

    def resolve(self, query: KnowledgeQuery) -> Sequence[KnowledgeEvidenceFrame]:
        ...


def resolve_knowledge(
    query: KnowledgeQuery,
    adapters: Sequence[KnowledgeAdapter],
    *,
    max_evidence: int = 8,
) -> KnowledgeResolution:
    evidence: list[KnowledgeEvidenceFrame] = []
    warnings: list[str] = []
    requested_sources = set(query.source_types)

    for adapter in adapters:
        source_type = str(getattr(adapter, "source_type", "") or "").strip()
        if requested_sources and source_type not in requested_sources:
            continue
        try:
            evidence.extend(_valid_evidence(adapter.resolve(query), source_type=source_type))
        except Exception as exc:  # pragma: no cover - defensive boundary for live connectors.
            warnings.append(f"{source_type or adapter.__class__.__name__}:{exc.__class__.__name__}")

    deduped = _dedupe_and_sort(evidence)
    if not deduped and not warnings:
        warnings.append("no_reliable_evidence")
    return KnowledgeResolution(query=query, evidence=tuple(deduped[:max_evidence]), warnings=tuple(warnings))


@dataclass(frozen=True)
class CaseRegistryRecord:
    case_id: str
    case_name: str
    assignee_user_id: str = ""
    assignee_dingtalk_user_id: str = ""
    assignee_name: str = ""
    status: str = "active"
    updated_at: date | datetime | str = ""
    facts: dict[str, Any] = field(default_factory=dict)


class InMemoryCaseRegistryAdapter:
    source_type = "case_registry"

    def __init__(self, records: Sequence[CaseRegistryRecord | dict[str, Any]]) -> None:
        self._records = tuple(_case_record(record) for record in records)

    def resolve(self, query: KnowledgeQuery) -> Sequence[KnowledgeEvidenceFrame]:
        if not _looks_like_case_query(query.text) and query.intent not in {"case_query", "internal_qa"}:
            return ()

        active_records = [
            record
            for record in self._records
            if _matches_assignee(record, query)
            and str(record.status or "").strip().lower() in ACTIVE_CASE_STATUSES
        ]
        identifier = query.user_id or query.dingtalk_user_id or "unknown"
        names = [record.case_name for record in active_records if record.case_name]
        case_ids = [record.case_id for record in active_records if record.case_id]
        freshness = _latest_freshness(active_records)
        count = len(active_records)

        return (
            KnowledgeEvidenceFrame(
                source_type=self.source_type,
                source_id=f"assignee:{identifier}:active",
                title="承办人在办案件",
                summary=f"案件台账显示该用户当前有 {count} 件在办案件。",
                facts={
                    "assignee_user_id": query.user_id,
                    "assignee_dingtalk_user_id": query.dingtalk_user_id,
                    "active_case_count": count,
                    "case_ids": case_ids,
                    "case_names": names,
                },
                confidence=0.96,
                freshness=freshness,
            ),
        )


@dataclass(frozen=True)
class OrgTeamRecord:
    team_id: str
    team_name: str
    department_name: str = ""
    team_code: str = ""
    active: bool = True


@dataclass(frozen=True)
class OrgUserRecord:
    user_id: str
    name: str
    dingtalk_user_id: str = ""
    employee_no: str = ""
    team_id: str = ""
    team_name: str = ""
    department_name: str = ""
    role: str = "member"
    active: bool = True


class InMemoryOrgDirectoryAdapter:
    source_type = "org_directory"

    def __init__(
        self,
        *,
        users: Sequence[OrgUserRecord | dict[str, Any] | Any],
        teams: Sequence[OrgTeamRecord | dict[str, Any] | Any] = (),
    ) -> None:
        self._teams = tuple(_org_team_record(team) for team in teams)
        teams_by_id = {team.team_id: team for team in self._teams if team.team_id}
        self._users = tuple(_org_user_record(user, teams_by_id=teams_by_id) for user in users)

    def resolve(self, query: KnowledgeQuery) -> Sequence[KnowledgeEvidenceFrame]:
        text = str(query.text or "").strip()
        if not _looks_like_org_query(text) and query.intent not in {"org_query", "internal_qa"}:
            return ()

        evidence: list[KnowledgeEvidenceFrame] = []
        current_user = _current_org_user(self._users, query)
        team = _matched_org_team(self._teams, text, current_user=current_user)
        if current_user is not None and (_asks_current_user(text) or not team):
            evidence.append(_current_user_evidence(current_user, self._users, self._teams))
        if team is not None:
            evidence.append(_team_evidence(team, self._users))
        if _asks_department_heads(text):
            evidence.append(_department_heads_evidence(self._users, self._teams))
        return evidence


async def load_live_org_directory_adapter(session: Any) -> InMemoryOrgDirectoryAdapter:
    from app.repositories import get_active_teams, get_active_users

    teams = await get_active_teams(session)
    users = await get_active_users(session)
    return InMemoryOrgDirectoryAdapter(users=users, teams=teams)


@dataclass(frozen=True)
class DailyReportHistoryRecord:
    report_id: str
    user_id: str
    report_date: date | str
    dingtalk_user_id: str = ""
    status: str = ""
    today_work: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()
    tomorrow_plan: tuple[str, ...] = ()
    updated_at: date | datetime | str = ""


class InMemoryDailyReportHistoryAdapter:
    source_type = "daily_report_history"

    def __init__(self, records: Sequence[DailyReportHistoryRecord | dict[str, Any] | Any]) -> None:
        self._records = tuple(_daily_history_record(record) for record in records)

    def resolve(self, query: KnowledgeQuery) -> Sequence[KnowledgeEvidenceFrame]:
        text = str(query.text or "").strip()
        if not _looks_like_daily_history_query(text) and query.intent not in {
            "daily_history_query",
            "daily_report_history",
            "daily_report",
        }:
            return ()

        records = _owned_daily_records(self._records, query)
        if not records:
            return ()
        current_date = _query_current_date(query, records)

        if _asks_recent_daily_history(text):
            return (_daily_history_summary_evidence(records, current_date=current_date),)

        target = _target_history_report(records, text=text, current_date=current_date)
        if target is None:
            return ()
        if _asks_previous_plan_completion(text):
            return (_previous_plan_completion_evidence(target, query=query),)
        return (_daily_report_history_evidence(target, query=query, current_date=current_date),)


async def load_live_daily_history_adapter(
    session: Any,
    user: Any,
    settings: Any | None = None,
    *,
    current_date: date | None = None,
    lookback_days: int = 7,
    include_current: bool = False,
) -> InMemoryDailyReportHistoryAdapter:
    from sqlalchemy import select

    from app.models import DailyReport
    from app.utils.time import now_in_timezone

    if current_date is None:
        timezone = getattr(user, "timezone", "") or getattr(settings, "timezone", "Asia/Shanghai")
        current_date = now_in_timezone(timezone).date()
    start_date = current_date - timedelta(days=max(1, lookback_days))
    end_date = current_date if include_current else current_date - timedelta(days=1)
    stmt = (
        select(DailyReport)
        .where(DailyReport.user_id == user.id, DailyReport.report_date >= start_date, DailyReport.report_date <= end_date)
        .order_by(DailyReport.report_date.desc(), DailyReport.updated_at.desc())
    )
    result = await session.execute(stmt)
    return InMemoryDailyReportHistoryAdapter(result.scalars().all())


def _valid_evidence(
    values: Sequence[KnowledgeEvidenceFrame] | None,
    *,
    source_type: str,
) -> list[KnowledgeEvidenceFrame]:
    result: list[KnowledgeEvidenceFrame] = []
    for value in values or ():
        if not isinstance(value, KnowledgeEvidenceFrame):
            raise TypeError(f"{source_type or 'adapter'} returned non-evidence")
        result.append(value)
    return result


def _dedupe_and_sort(evidence: list[KnowledgeEvidenceFrame]) -> list[KnowledgeEvidenceFrame]:
    indexed = list(enumerate(evidence))
    ordered = sorted(
        indexed,
        key=lambda pair: (
            STRUCTURED_SOURCE_PRIORITY.get(pair[1].source_type, 99),
            -pair[1].confidence,
            pair[0],
        ),
    )
    result: list[KnowledgeEvidenceFrame] = []
    seen: set[tuple[str, str]] = set()
    for _, item in ordered:
        key = (item.source_type, item.source_id)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _case_record(value: CaseRegistryRecord | dict[str, Any]) -> CaseRegistryRecord:
    if isinstance(value, CaseRegistryRecord):
        return value
    return CaseRegistryRecord(
        case_id=str(value.get("case_id") or value.get("id") or ""),
        case_name=str(value.get("case_name") or value.get("name") or ""),
        assignee_user_id=str(value.get("assignee_user_id") or value.get("user_id") or ""),
        assignee_dingtalk_user_id=str(value.get("assignee_dingtalk_user_id") or value.get("dingtalk_user_id") or ""),
        assignee_name=str(value.get("assignee_name") or value.get("owner") or ""),
        status=str(value.get("status") or "active"),
        updated_at=value.get("updated_at") or "",
        facts=dict(value.get("facts") or {}),
    )


def _org_team_record(value: OrgTeamRecord | dict[str, Any] | Any) -> OrgTeamRecord:
    if isinstance(value, OrgTeamRecord):
        return value
    if isinstance(value, dict):
        return OrgTeamRecord(
            team_id=str(value.get("team_id") or value.get("id") or ""),
            team_name=str(value.get("team_name") or value.get("name") or ""),
            department_name=str(value.get("department_name") or ""),
            team_code=str(value.get("team_code") or value.get("code") or ""),
            active=bool(value.get("active", True)),
        )
    return OrgTeamRecord(
        team_id=str(getattr(value, "team_id", None) or getattr(value, "id", "") or ""),
        team_name=str(getattr(value, "team_name", None) or getattr(value, "name", "") or ""),
        department_name=str(getattr(value, "department_name", "") or ""),
        team_code=str(getattr(value, "team_code", None) or getattr(value, "code", "") or ""),
        active=bool(getattr(value, "active", True)),
    )


def _org_user_record(value: OrgUserRecord | dict[str, Any] | Any, *, teams_by_id: dict[str, OrgTeamRecord]) -> OrgUserRecord:
    if isinstance(value, OrgUserRecord):
        return value
    if isinstance(value, dict):
        team_id = str(value.get("team_id") or "")
        team = teams_by_id.get(team_id)
        return OrgUserRecord(
            user_id=str(value.get("user_id") or value.get("id") or ""),
            name=str(value.get("name") or ""),
            dingtalk_user_id=str(value.get("dingtalk_user_id") or ""),
            employee_no=str(value.get("employee_no") or ""),
            team_id=team_id,
            team_name=str(value.get("team_name") or getattr(team, "team_name", "") or ""),
            department_name=str(value.get("department_name") or getattr(team, "department_name", "") or ""),
            role=str(value.get("role") or "member"),
            active=bool(value.get("active", True)),
        )
    team_obj = getattr(value, "team", None)
    team_id = str(getattr(value, "team_id", "") or getattr(team_obj, "id", "") or "")
    team = teams_by_id.get(team_id)
    return OrgUserRecord(
        user_id=str(getattr(value, "user_id", None) or getattr(value, "id", "") or ""),
        name=str(getattr(value, "name", "") or ""),
        dingtalk_user_id=str(getattr(value, "dingtalk_user_id", "") or ""),
        employee_no=str(getattr(value, "employee_no", "") or ""),
        team_id=team_id,
        team_name=str(getattr(team_obj, "name", "") or getattr(team, "team_name", "") or ""),
        department_name=str(getattr(team_obj, "department_name", "") or getattr(team, "department_name", "") or ""),
        role=str(getattr(value, "role", "") or "member"),
        active=bool(getattr(value, "active", True)),
    )


def _daily_history_record(value: DailyReportHistoryRecord | dict[str, Any] | Any) -> DailyReportHistoryRecord:
    if isinstance(value, DailyReportHistoryRecord):
        return DailyReportHistoryRecord(
            report_id=value.report_id,
            user_id=value.user_id,
            dingtalk_user_id=value.dingtalk_user_id,
            report_date=value.report_date,
            status=value.status,
            today_work=tuple(_clean_text_items(value.today_work)),
            problems=tuple(_clean_text_items(value.problems)),
            tomorrow_plan=tuple(_clean_text_items(value.tomorrow_plan)),
            updated_at=value.updated_at,
        )
    if isinstance(value, dict):
        return DailyReportHistoryRecord(
            report_id=str(value.get("report_id") or value.get("id") or ""),
            user_id=str(value.get("user_id") or ""),
            dingtalk_user_id=str(value.get("dingtalk_user_id") or ""),
            report_date=value.get("report_date") or value.get("date") or "",
            status=str(value.get("status") or ""),
            today_work=tuple(_clean_text_items(value.get("today_work"))),
            problems=tuple(_clean_text_items(value.get("problems"))),
            tomorrow_plan=tuple(_clean_text_items(value.get("tomorrow_plan"))),
            updated_at=value.get("updated_at") or "",
        )
    return DailyReportHistoryRecord(
        report_id=str(getattr(value, "report_id", None) or getattr(value, "id", "") or ""),
        user_id=str(getattr(value, "user_id", "") or ""),
        dingtalk_user_id=str(getattr(value, "dingtalk_user_id", "") or ""),
        report_date=getattr(value, "report_date", None) or getattr(value, "date", "") or "",
        status=str(getattr(value, "status", "") or ""),
        today_work=tuple(_clean_text_items(getattr(value, "today_work", ()))),
        problems=tuple(_clean_text_items(getattr(value, "problems", ()))),
        tomorrow_plan=tuple(_clean_text_items(getattr(value, "tomorrow_plan", ()))),
        updated_at=getattr(value, "updated_at", "") or "",
    )


def _matches_assignee(record: CaseRegistryRecord, query: KnowledgeQuery) -> bool:
    if query.user_id and record.assignee_user_id == query.user_id:
        return True
    if query.dingtalk_user_id and record.assignee_dingtalk_user_id == query.dingtalk_user_id:
        return True
    return False


def _looks_like_org_query(text: str) -> bool:
    compact = str(text or "").strip()
    if not compact:
        return False
    return any(token in compact for token in ("我是谁", "我属于", "我在哪", "哪个团队", "哪个部门", "负责人", "团队成员", "部门负责人", "团队负责人"))


def _asks_current_user(text: str) -> bool:
    return any(token in text for token in ("我是谁", "我属于", "我在哪", "我的团队", "我哪个"))


def _asks_department_heads(text: str) -> bool:
    return "部门负责人" in text or ("负责人" in text and any(token in text for token in ("所有团队", "各团队", "团队负责人们")))


def _current_org_user(users: Sequence[OrgUserRecord], query: KnowledgeQuery) -> OrgUserRecord | None:
    active_users = [user for user in users if user.active]
    for user in active_users:
        if query.user_id and user.user_id == query.user_id:
            return user
        if query.dingtalk_user_id and user.dingtalk_user_id == query.dingtalk_user_id:
            return user
    return None


def _matched_org_team(
    teams: Sequence[OrgTeamRecord],
    text: str,
    *,
    current_user: OrgUserRecord | None,
) -> OrgTeamRecord | None:
    active_teams = [team for team in teams if team.active]
    for team in active_teams:
        if team.team_name and team.team_name in text:
            return team
        if team.team_code and team.team_code in text:
            return team
    if current_user and current_user.team_id:
        return next((team for team in active_teams if team.team_id == current_user.team_id), None)
    return None


def _team_leaders(users: Sequence[OrgUserRecord], team_id: str) -> list[OrgUserRecord]:
    return [
        user
        for user in users
        if user.active and user.team_id == team_id and _normalized_role(user.role) in TEAM_LEADER_ROLES
    ]


def _department_heads(users: Sequence[OrgUserRecord]) -> list[OrgUserRecord]:
    return [user for user in users if user.active and _normalized_role(user.role) in DEPARTMENT_HEAD_ROLES]


def _normalized_role(role: str) -> str:
    return str(role or "").strip().lower()


def _current_user_evidence(
    user: OrgUserRecord,
    users: Sequence[OrgUserRecord],
    teams: Sequence[OrgTeamRecord],
) -> KnowledgeEvidenceFrame:
    team = next((item for item in teams if item.team_id == user.team_id), None)
    team_name = user.team_name or getattr(team, "team_name", "") or ""
    department_name = user.department_name or getattr(team, "department_name", "") or ""
    leaders = _team_leaders(users, user.team_id)
    return KnowledgeEvidenceFrame(
        source_type="org_directory",
        source_id=f"user:{user.user_id or user.dingtalk_user_id}",
        title="当前用户组织关系",
        summary=f"组织台账显示：{user.name} 属于 {department_name or '未标注部门'} / {team_name or '未标注团队'}。",
        facts={
            "user_id": user.user_id,
            "name": user.name,
            "dingtalk_user_id": user.dingtalk_user_id,
            "employee_no": user.employee_no,
            "team_id": user.team_id,
            "team_name": team_name,
            "department_name": department_name,
            "role": user.role,
            "team_leaders": [_serialize_org_user(item) for item in leaders],
        },
        confidence=0.98,
        freshness="live_user_table",
    )


def _team_evidence(team: OrgTeamRecord, users: Sequence[OrgUserRecord]) -> KnowledgeEvidenceFrame:
    members = [user for user in users if user.active and user.team_id == team.team_id]
    leaders = _team_leaders(users, team.team_id)
    leader_names = "、".join(user.name for user in leaders if user.name) or "未配置"
    return KnowledgeEvidenceFrame(
        source_type="org_directory",
        source_id=f"team:{team.team_id}",
        title=f"{team.team_name}组织关系",
        summary=f"组织台账显示：{team.team_name} 负责人：{leader_names}；活跃成员 {len(members)} 人。",
        facts={
            "team_id": team.team_id,
            "team_code": team.team_code,
            "team_name": team.team_name,
            "department_name": team.department_name,
            "member_count": len(members),
            "leaders": [_serialize_org_user(item) for item in leaders],
            "members": [_serialize_org_user(item) for item in members],
        },
        confidence=0.98,
        freshness="live_user_table",
    )


def _department_heads_evidence(users: Sequence[OrgUserRecord], teams: Sequence[OrgTeamRecord]) -> KnowledgeEvidenceFrame:
    heads = _department_heads(users)
    department_name = next((team.department_name for team in teams if team.department_name), "")
    return KnowledgeEvidenceFrame(
        source_type="org_directory",
        source_id="department:heads",
        title="部门负责人",
        summary=f"组织台账显示：{department_name or '当前部门'} 部门负责人 {len(heads)} 人。",
        facts={
            "department_name": department_name,
            "department_heads": [_serialize_org_user(item) for item in heads],
        },
        confidence=0.98,
        freshness="live_user_table",
    )


def _serialize_org_user(user: OrgUserRecord) -> dict[str, Any]:
    return {
        "user_id": user.user_id,
        "name": user.name,
        "dingtalk_user_id": user.dingtalk_user_id,
        "employee_no": user.employee_no,
        "team_id": user.team_id,
        "team_name": user.team_name,
        "department_name": user.department_name,
        "role": user.role,
    }


def _looks_like_daily_history_query(text: str) -> bool:
    compact = str(text or "").strip()
    if not compact:
        return False
    history_markers = ("昨天", "昨日", "昨儿", "前一天", "上一天", "上次", "最近", "历史")
    daily_markers = ("日报", "复盘", "今日工作", "问题", "风险", "明日计划", "明天计划", "计划", "带过来", "复制", "完成")
    return any(marker in compact for marker in history_markers) and any(marker in compact for marker in daily_markers)


def _asks_recent_daily_history(text: str) -> bool:
    return any(token in text for token in ("最近", "历史", "这几天", "近几天")) and any(token in text for token in ("日报", "复盘", "填报"))


def _asks_previous_plan_completion(text: str) -> bool:
    compact = str(text or "")
    return any(token in compact for token in ("昨天", "昨日", "昨儿", "前一天", "上一天")) and any(
        token in compact for token in ("计划完成", "计划都完成", "待办完成", "事项完成", "已经完成", "都做完", "完成了")
    )


def _owned_daily_records(records: Sequence[DailyReportHistoryRecord], query: KnowledgeQuery) -> list[DailyReportHistoryRecord]:
    owned = [
        record
        for record in records
        if (query.user_id and record.user_id == query.user_id)
        or (query.dingtalk_user_id and record.dingtalk_user_id == query.dingtalk_user_id)
        or (not query.user_id and not query.dingtalk_user_id)
    ]
    return sorted(owned, key=lambda record: (_record_date(record) or date.min, _freshness_value(record.updated_at)), reverse=True)


def _query_current_date(query: KnowledgeQuery, records: Sequence[DailyReportHistoryRecord]) -> date:
    for key in ("current_date", "report_date", "target_date"):
        parsed = _parse_date(query.metadata.get(key))
        if parsed is not None:
            return parsed
    latest = max((_record_date(record) for record in records if _record_date(record) is not None), default=None)
    return latest + timedelta(days=1) if latest is not None else date.today()


def _target_history_report(
    records: Sequence[DailyReportHistoryRecord],
    *,
    text: str,
    current_date: date,
) -> DailyReportHistoryRecord | None:
    target_date = _target_history_date(text, current_date=current_date)
    if target_date is not None:
        exact = next((record for record in records if _record_date(record) == target_date), None)
        if exact is not None:
            return exact
    return next((record for record in records if (_record_date(record) or date.min) < current_date), None)


def _target_history_date(text: str, *, current_date: date) -> date | None:
    if any(token in text for token in ("昨天", "昨日", "昨儿", "前一天", "上一天", "上次")):
        return current_date - timedelta(days=1)
    return None


def _daily_history_summary_evidence(
    records: Sequence[DailyReportHistoryRecord],
    *,
    current_date: date,
) -> KnowledgeEvidenceFrame:
    recent = [record for record in records if (_record_date(record) or date.min) < current_date][:5]
    user_key = _daily_history_user_key(recent[0]) if recent else "unknown"
    summaries = [_serialize_daily_record(record) for record in recent]
    return KnowledgeEvidenceFrame(
        source_type="daily_report_history",
        source_id=f"daily_history:{user_key}:recent",
        title="最近日报历史",
        summary=f"日报历史显示：最近可用历史日报 {len(recent)} 份。",
        facts={
            "current_date": current_date.isoformat(),
            "reports": summaries,
        },
        confidence=0.94,
        freshness=_latest_daily_history_freshness(recent),
    )


def _daily_report_history_evidence(
    record: DailyReportHistoryRecord,
    *,
    query: KnowledgeQuery,
    current_date: date,
) -> KnowledgeEvidenceFrame:
    record_date = _record_date(record)
    relation = "yesterday" if record_date == current_date - timedelta(days=1) else "previous"
    label = "昨日日报" if relation == "yesterday" else "历史日报"
    return KnowledgeEvidenceFrame(
        source_type="daily_report_history",
        source_id=f"daily_report:{_daily_history_user_key(record)}:{_record_date_text(record)}",
        title=label,
        summary=(
            f"{label}显示：今日工作 {len(record.today_work)} 条，"
            f"问题/风险 {len(record.problems)} 条，明日计划 {len(record.tomorrow_plan)} 条。"
        ),
        facts={
            **_serialize_daily_record(record),
            "relation": relation,
            "current_date": current_date.isoformat(),
            "can_copy_to_today": bool(record.today_work or record.problems or record.tomorrow_plan),
            "requested_by_user_id": query.user_id,
            "requested_by_dingtalk_user_id": query.dingtalk_user_id,
        },
        confidence=0.95,
        freshness=_freshness_value(record.updated_at) or _record_date_text(record),
    )


def _previous_plan_completion_evidence(record: DailyReportHistoryRecord, *, query: KnowledgeQuery) -> KnowledgeEvidenceFrame:
    candidates = list(record.tomorrow_plan)
    return KnowledgeEvidenceFrame(
        source_type="daily_report_history",
        source_id=f"daily_previous_plan:{_daily_history_user_key(record)}:{_record_date_text(record)}",
        title="昨日明日计划完成候选",
        summary=f"历史日报显示：{_record_date_text(record)} 的明日计划有 {len(candidates)} 条，可作为今日工作完成候选。",
        facts={
            "source_report_date": _record_date_text(record),
            "previous_tomorrow_plan": candidates,
            "today_work_candidates": candidates,
            "suggested_daily_operation": "complete_previous_plan_items",
            "suggested_target_field": "today_work",
            "requested_by_user_id": query.user_id,
            "requested_by_dingtalk_user_id": query.dingtalk_user_id,
        },
        confidence=0.95 if candidates else 0.7,
        freshness=_freshness_value(record.updated_at) or _record_date_text(record),
    )


def _serialize_daily_record(record: DailyReportHistoryRecord) -> dict[str, Any]:
    return {
        "report_id": record.report_id,
        "user_id": record.user_id,
        "dingtalk_user_id": record.dingtalk_user_id,
        "report_date": _record_date_text(record),
        "status": record.status,
        "today_work": list(record.today_work),
        "problems": list(record.problems),
        "tomorrow_plan": list(record.tomorrow_plan),
    }


def _daily_history_user_key(record: DailyReportHistoryRecord) -> str:
    return record.user_id or record.dingtalk_user_id or "unknown"


def _latest_daily_history_freshness(records: Sequence[DailyReportHistoryRecord]) -> str:
    values = [_freshness_value(record.updated_at) or _record_date_text(record) for record in records]
    values = [value for value in values if value]
    return max(values) if values else ""


def _record_date(record: DailyReportHistoryRecord) -> date | None:
    return _parse_date(record.report_date)


def _record_date_text(record: DailyReportHistoryRecord) -> str:
    parsed = _record_date(record)
    return parsed.isoformat() if parsed is not None else str(record.report_date or "")


def _parse_date(value: Any) -> date | None:
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


def _clean_text_items(values: Any) -> list[str]:
    if values is None:
        return []
    candidates = values if isinstance(values, (list, tuple)) else [values]
    return [str(value).strip() for value in candidates if str(value or "").strip()]


def _looks_like_case_query(text: str) -> bool:
    compact = str(text or "").strip()
    if not compact:
        return False
    return "案" in compact and any(token in compact for token in ("几个", "几件", "手底", "名下", "承办", "在办", "进展"))


def _latest_freshness(records: Sequence[CaseRegistryRecord]) -> str:
    values = [_freshness_value(record.updated_at) for record in records]
    values = [value for value in values if value]
    return max(values) if values else ""


def _freshness_value(value: date | datetime | str) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value or "")
