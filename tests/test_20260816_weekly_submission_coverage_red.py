from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from app.agent2.report_insights import InMemoryReportInsightRepository
from app.agent2.submission_coverage_query import (
    SubmissionCoverageDataError,
    SubmissionCoverageQuery,
    SubmissionCoverageRequest,
)
from app.agent2.tool_calling.canary_config import canary_system_prompt
from app.agent2.tool_calling.contracts import QueryReportInsightsArgs
from app.agent2.tool_calling.production_daily_executor import (
    ProductionDailyExecutor,
)
from app.agent2.tool_calling.production_handlers import (
    ProductionHandlerRequest,
    execute_query_report_insights,
)
from app.agent2.tool_calling.registry import (
    TOOL_REGISTRY,
    validate_tool_arguments,
)
from app.legal_daily_dashboard.domain import (
    DailyReportRecord,
    MemberRecord,
    SubmissionObligation,
    TeamRecord,
)
from app.legal_daily_dashboard.repository import (
    InMemoryDashboardRepository,
)
from app.legal_daily_dashboard.sql_repository import SqlDashboardRepository

SHANGHAI = ZoneInfo("Asia/Shanghai")
TENANT_ID = "tenant-submission-coverage-red"
LEGAL_CENTER = "法务合约中心"
TEAM_ONE = TeamRecord(
    ref="team-law-1",
    name="法务一部",
    department_name=LEGAL_CENTER,
    code="law-1",
)
TEAM_FIVE = TeamRecord(
    ref="team-law-5",
    name="法务五部",
    department_name=LEGAL_CENTER,
    code="law-5",
)
TEAM_SIX = TeamRecord(
    ref="team-law-6",
    name="法务六部",
    department_name=LEGAL_CENTER,
    code="law-6",
)


def _member(
    ref: str,
    name: str,
    *,
    team: TeamRecord = TEAM_FIVE,
    effective_from: date | None = None,
    effective_to: date | None = None,
) -> MemberRecord:
    return MemberRecord(
        ref=ref,
        name=name,
        team_ref=team.ref,
        team_name=team.name,
        department_name=team.department_name,
        team_code=team.code,
        effective_from=effective_from,
        effective_to=effective_to,
    )


def _obligation(
    member: MemberRecord,
    report_date: date,
    *,
    required: bool = True,
    reason: str = "",
    data_complete: bool = True,
    deadline_hour: int = 18,
) -> SubmissionObligation:
    return SubmissionObligation(
        member_ref=member.ref,
        team_ref=member.team_ref,
        report_date=report_date,
        required=required,
        reason=reason,
        deadline_at=datetime(
            report_date.year,
            report_date.month,
            report_date.day,
            deadline_hour,
            tzinfo=SHANGHAI,
        ),
        data_complete=data_complete,
        source="formal_roster_test_snapshot",
    )


def _report(
    member: MemberRecord,
    report_date: date,
    *,
    status: str,
    today_work: tuple[str, ...] = (),
    section_status: dict[str, object] | None = None,
) -> DailyReportRecord:
    return DailyReportRecord(
        ref=f"report-{member.ref}-{report_date.isoformat()}",
        member_ref=member.ref,
        team_ref=member.team_ref,
        report_date=report_date,
        status=status,
        confirmation_type=(
            "user_confirmed" if status == "completed" else "none"
        ),
        confirmed_by_user=status == "completed",
        today_work=today_work,
        problems=("暂无",) if status == "completed" else (),
        tomorrow_plan=("继续跟进",) if status == "completed" else (),
        submitted_at=(
            datetime(
                report_date.year,
                report_date.month,
                report_date.day,
                17,
                tzinfo=SHANGHAI,
            )
            if status == "completed"
            else None
        ),
        section_status=section_status,
    )


def _report_insight_repository(
    *,
    requester_id: str,
    members: tuple[MemberRecord, ...],
    current_team_by_member: dict[str, TeamRecord] | None = None,
) -> InMemoryReportInsightRepository:
    current_teams = current_team_by_member or {}
    return InMemoryReportInsightRepository(
        users=(
            {
                "id": requester_id,
                "name": "赵负责人（脱敏）",
                "team_id": TEAM_ONE.ref,
                "role": "department_head",
            },
            *(
                {
                    "id": member.ref,
                    "name": member.name,
                    "team_id": current_teams.get(member.ref, TEAM_FIVE).ref,
                    "role": "member",
                }
                for member in members
            ),
        ),
        teams=(
            {
                "id": team.ref,
                "name": team.name,
                "department_name": team.department_name,
                "code": team.code,
            }
            for team in (TEAM_ONE, TEAM_FIVE, TEAM_SIX)
        ),
        reports=(),
    )


async def _query_coverage(
    monkeypatch: pytest.MonkeyPatch,
    *,
    repository: InMemoryDashboardRepository,
    members: tuple[MemberRecord, ...],
    now: datetime,
    period_type: str = "current_week",
    current_team_by_member: dict[str, TeamRecord] | None = None,
) -> dict[str, object]:
    requester_uuid = uuid4()
    requester_id = str(requester_uuid)
    report_repository = _report_insight_repository(
        requester_id=requester_id,
        members=members,
        current_team_by_member=current_team_by_member,
    )
    monkeypatch.setattr(
        "app.agent2.tool_calling.production_daily_executor.SqlDashboardRepository",
        lambda _session: repository,
    )
    monkeypatch.setattr(
        "app.agent2.tool_calling.production_daily_executor.SqlReportInsightRepository",
        lambda *_args, **_kwargs: report_repository,
    )
    principal = SimpleNamespace(
        tenant_id=TENANT_ID,
        user_id=requester_uuid,
        timezone="Asia/Shanghai",
    )
    context = SimpleNamespace(
        principal=principal,
        now=now,
        business_glossary={},
    )
    user = SimpleNamespace(
        id=requester_uuid,
        name="赵负责人（脱敏）",
        dingtalk_user_id="dt-redacted-zhao",
        team_id=TEAM_ONE.ref,
        role="department_head",
    )
    executor = ProductionDailyExecutor(
        session=object(),
        user=user,
        context=context,
        settings=SimpleNamespace(
            legal_daily_dashboard_tenant_id=TENANT_ID,
        ),
        bound_calls={},
        source_channel="test",
        source_text_hash="0" * 64,
        date_resolver=object(),
    )
    # RED seam: model_construct lets the request reach the public production
    # handler while the separately asserted tool contract is still missing the
    # new literal. It does not fake any returned business fact.
    arguments = QueryReportInsightsArgs.model_construct(
        query_kind="submission_coverage",
        scope_type="organization",
        scope_name=TEAM_FIVE.name,
        period_type=period_type,
        status_filter="all_saved",
    )
    request = ProductionHandlerRequest(
        tool_call_id="call-submission-coverage",
        tool_name="query_report_insights",
        arguments=arguments,
        executor=executor,
        memory_executor=object(),
    )

    outcome = await execute_query_report_insights(request)

    assert outcome.status_if_unchanged.value == "success"
    assert outcome.target_type == "daily_report_insight"
    assert outcome.before_report is None
    assert outcome.after_report is None
    assert outcome.safe_user_facts is not None
    assert outcome.safe_user_facts["actual_write"] is False
    report_insight = outcome.safe_user_facts["report_insight"]
    facts = report_insight["facts"]
    assert facts["query_kind"] == "submission_coverage"
    assert facts["scope_label"] == TEAM_FIVE.name
    assert facts["period_type"] == period_type
    return facts


def _daily(facts: dict[str, object], report_date: str) -> dict[str, object]:
    return next(
        row
        for row in facts["daily_breakdown"]
        if row["report_date"] == report_date
    )


def _person(facts: dict[str, object], name: str) -> dict[str, object]:
    return next(
        row
        for row in facts["member_breakdown"]
        if row["member_name"] == name
    )


def test_query_report_insights_contract_accepts_weekly_submission_coverage() -> None:
    validated = validate_tool_arguments(
        "query_report_insights",
        {
            "query_kind": "submission_coverage",
            "scope_type": "organization",
            "scope_name": TEAM_FIVE.name,
            "period_type": "current_week",
            "status_filter": "all_saved",
        },
    )

    assert QueryReportInsightsArgs.model_validate(validated)
    assert "submission_coverage" in TOOL_REGISTRY[
        "query_report_insights"
    ].description
    prompt = canary_system_prompt()
    assert "query_kind=submission_coverage" in prompt
    assert "never from the number of saved reports" in prompt
    assert (
        "Keep submitted and pending_confirmation counts separate" in prompt
    )


@pytest.mark.asyncio
async def test_known_responsibility_with_zero_reporters_is_not_zero_expected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_date = date(2026, 8, 10)
    members = (
        _member("member-a", "成员甲"),
        _member("member-b", "成员乙"),
    )
    repository = InMemoryDashboardRepository(
        teams=(TEAM_FIVE,),
        members=members,
        obligations=tuple(_obligation(member, report_date) for member in members),
        reports=(),
    )

    facts = await _query_coverage(
        monkeypatch,
        repository=repository,
        members=members,
        now=datetime(2026, 8, 10, 12, tzinfo=timezone.utc),
    )

    assert facts["period_start"] == "2026-08-10"
    assert facts["period_end"] == "2026-08-10"
    assert facts["queried_at"] == "2026-08-10T20:00:00+08:00"
    assert facts["reporter_count"] == 0
    assert facts["report_count"] == 0
    assert facts["responsibility_data_complete"] is True
    assert facts["member_day_count"] == 2
    assert facts["responsibility_known_count"] == 2
    assert facts["expected_count"] == 2
    assert facts["expected_known_count"] == 2
    assert facts["responsibility_unknown_count"] == 0
    assert facts["not_filled_count"] == 2
    assert facts["overdue_count"] == 2
    assert {
        item["member_name"]: item["submission_state"]
        for item in _daily(facts, "2026-08-10")["not_filled_members"]
    } == {"成员甲": "overdue", "成员乙": "overdue"}
    assert _person(facts, "成员甲")["not_filled_dates"] == [
        {"report_date": "2026-08-10", "submission_state": "overdue"}
    ]


@pytest.mark.asyncio
async def test_coverage_exposes_every_state_and_incomplete_responsibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_date = date(2026, 8, 10)
    completed = _member("member-completed", "已完成人员")
    partial = _member("member-partial", "部分填写人员")
    missing = _member("member-missing", "未填写人员")
    exempt = _member("member-exempt", "豁免人员")
    missing_obligation = _member("member-no-obligation", "责任记录缺失人员")
    incomplete_obligation = _member(
        "member-incomplete-obligation",
        "责任记录不完整人员",
    )
    members = (
        completed,
        partial,
        missing,
        exempt,
        missing_obligation,
        incomplete_obligation,
    )
    repository = InMemoryDashboardRepository(
        teams=(TEAM_FIVE,),
        members=members,
        obligations=(
            _obligation(completed, report_date),
            _obligation(partial, report_date),
            _obligation(missing, report_date),
            _obligation(
                exempt,
                report_date,
                required=False,
                reason="已审批休假",
            ),
            _obligation(
                incomplete_obligation,
                report_date,
                data_complete=False,
            ),
        ),
        reports=(
            _report(
                completed,
                report_date,
                status="completed",
                today_work=("完成合同复核",),
            ),
            _report(
                partial,
                report_date,
                status="collecting",
                today_work=("已填写一项工作",),
            ),
            _report(
                exempt,
                report_date,
                status="completed",
                today_work=("自愿填写但当天已豁免",),
            ),
        ),
    )

    facts = await _query_coverage(
        monkeypatch,
        repository=repository,
        members=members,
        now=datetime(2026, 8, 10, 20, tzinfo=SHANGHAI),
    )

    assert facts["responsibility_data_complete"] is False
    assert facts["member_day_count"] == 6
    assert facts["responsibility_known_count"] == 4
    assert facts["expected_count"] is None
    assert facts["expected_known_count"] == 3
    assert facts["completed_count"] == 1
    assert facts["partial_count"] == 1
    assert facts["not_filled_count"] == 1
    assert facts["exempt_count"] == 1
    assert facts["responsibility_unknown_count"] == 2
    daily = _daily(facts, "2026-08-10")
    assert [row["member_name"] for row in daily["completed_members"]] == [
        "已完成人员"
    ]
    assert [row["member_name"] for row in daily["partial_members"]] == [
        "部分填写人员"
    ]
    assert [row["member_name"] for row in daily["not_filled_members"]] == [
        "未填写人员"
    ]
    assert daily["exempt_members"] == [
        {"member_name": "豁免人员", "reason": "已审批休假"}
    ]
    assert {
        row["member_name"]
        for row in daily["responsibility_unknown_members"]
    } == {"责任记录不完整人员", "责任记录缺失人员"}


@pytest.mark.asyncio
async def test_not_filled_before_deadline_is_not_reported_as_overdue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_date = date(2026, 8, 10)
    member = _member("member-before-deadline", "截止前未填写人员")
    repository = InMemoryDashboardRepository(
        teams=(TEAM_FIVE,),
        members=(member,),
        obligations=(_obligation(member, report_date),),
        reports=(),
    )

    facts = await _query_coverage(
        monkeypatch,
        repository=repository,
        members=(member,),
        now=datetime(2026, 8, 10, 16, tzinfo=SHANGHAI),
    )

    assert facts["not_filled_count"] == 1
    assert facts["not_yet_due_count"] == 1
    assert facts["overdue_count"] == 0
    assert _daily(facts, "2026-08-10")["not_filled_members"] == [
        {
            "member_name": "截止前未填写人员",
            "submission_state": "not_yet_due",
        }
    ]


@pytest.mark.asyncio
async def test_previous_week_uses_membership_and_obligations_from_that_week(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    historical_member = _member(
        "member-transferred",
        "已调岗人员",
        effective_from=date(2026, 8, 3),
        effective_to=date(2026, 8, 9),
    )
    period_dates = tuple(date(2026, 8, day) for day in range(3, 10))
    obligations = tuple(
        _obligation(
            historical_member,
            report_date,
            required=report_date.weekday() < 5,
            reason="周末" if report_date.weekday() >= 5 else "",
        )
        for report_date in period_dates
    )
    reports = tuple(
        _report(
            historical_member,
            report_date,
            status="completed",
            today_work=(f"{report_date.isoformat()}历史工作",),
        )
        for report_date in period_dates
        if report_date.weekday() < 5
    )
    repository = InMemoryDashboardRepository(
        teams=(TEAM_FIVE,),
        members=(historical_member,),
        obligations=obligations,
        reports=reports,
    )

    facts = await _query_coverage(
        monkeypatch,
        repository=repository,
        members=(historical_member,),
        current_team_by_member={historical_member.ref: TEAM_SIX},
        now=datetime(2026, 8, 16, 10, tzinfo=SHANGHAI),
        period_type="previous_week",
    )

    assert facts["period_start"] == "2026-08-03"
    assert facts["period_end"] == "2026-08-09"
    assert facts["responsibility_data_complete"] is True
    assert facts["member_day_count"] == 7
    assert facts["responsibility_known_count"] == 7
    assert facts["expected_count"] == 5
    assert facts["completed_count"] == 5
    assert facts["exempt_count"] == 2
    assert facts["report_count"] == 5
    assert facts["reporter_count"] == 1
    assert [row["report_date"] for row in facts["daily_breakdown"]] == [
        value.isoformat() for value in period_dates
    ]
    history = _person(facts, "已调岗人员")
    assert history["team_name"] == TEAM_FIVE.name
    assert history["completed_dates"] == [
        value.isoformat() for value in period_dates if value.weekday() < 5
    ]
    assert history["exempt_dates"] == [
        {"report_date": value.isoformat(), "reason": "周末"}
        for value in period_dates
        if value.weekday() >= 5
    ]


@pytest.mark.asyncio
async def test_previous_week_matches_production_weekday_obligation_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member = _member(
        "member-production-week",
        "生产周口径人员",
        effective_from=date(2026, 8, 3),
    )
    reporting_dates = tuple(
        date(2026, 8, day) for day in range(3, 8)
    )
    repository = InMemoryDashboardRepository(
        teams=(TEAM_FIVE,),
        members=(member,),
        obligations=tuple(
            _obligation(member, report_date)
            for report_date in reporting_dates
        ),
        reports=tuple(
            _report(
                member,
                report_date,
                status="completed",
                today_work=(f"{report_date.isoformat()}工作",),
            )
            for report_date in reporting_dates
        ),
    )

    facts = await _query_coverage(
        monkeypatch,
        repository=repository,
        members=(member,),
        now=datetime(2026, 8, 16, 10, tzinfo=SHANGHAI),
        period_type="previous_week",
    )

    assert facts["period_start"] == "2026-08-03"
    assert facts["period_end"] == "2026-08-09"
    assert facts["default_reporting_dates"] == [
        value.isoformat() for value in reporting_dates
    ]
    assert facts["default_non_reporting_dates"] == [
        "2026-08-08",
        "2026-08-09",
    ]
    assert facts["responsibility_data_complete"] is True
    assert facts["member_day_count"] == 5
    assert facts["responsibility_known_count"] == 5
    assert facts["responsibility_unknown_count"] == 0
    assert facts["expected_count"] == 5
    assert facts["completed_count"] == 5


@pytest.mark.asyncio
async def test_previous_week_resolves_team_from_period_not_sunday_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member = _member(
        "member-left-friday",
        "周五调出人员",
        effective_from=date(2026, 8, 3),
        effective_to=date(2026, 8, 7),
    )
    reporting_dates = tuple(
        date(2026, 8, day) for day in range(3, 8)
    )

    class _DateAwareRepository(InMemoryDashboardRepository):
        async def list_member_teams(
            self,
            *,
            tenant_id: str,
            on_date: date | None = None,
        ) -> tuple[TeamRecord, ...]:
            del tenant_id
            if on_date is None or (
                (member.effective_from is None or member.effective_from <= on_date)
                and (member.effective_to is None or member.effective_to >= on_date)
            ):
                return (TEAM_FIVE,)
            return ()

    repository = _DateAwareRepository(
        teams=(TEAM_FIVE,),
        members=(member,),
        obligations=tuple(
            _obligation(member, report_date)
            for report_date in reporting_dates
        ),
        reports=tuple(
            _report(member, report_date, status="completed")
            for report_date in reporting_dates
        ),
    )

    facts = await _query_coverage(
        monkeypatch,
        repository=repository,
        members=(member,),
        now=datetime(2026, 8, 16, 10, tzinfo=SHANGHAI),
        period_type="previous_week",
    )

    assert facts["scope_label"] == TEAM_FIVE.name
    assert facts["member_day_count"] == 5
    assert facts["completed_count"] == 5


@pytest.mark.asyncio
async def test_pending_confirmation_is_separate_from_submitted_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_date = date(2026, 8, 10)
    submitted = _member("member-submitted", "已提交人员")
    pending = _member("member-pending", "待确认人员")
    repository = InMemoryDashboardRepository(
        teams=(TEAM_FIVE,),
        members=(submitted, pending),
        obligations=(
            _obligation(submitted, report_date),
            _obligation(pending, report_date),
        ),
        reports=(
            _report(submitted, report_date, status="completed"),
            _report(
                pending,
                report_date,
                status="pending_confirmation",
                today_work=("已填写但尚未确认",),
            ),
        ),
    )

    facts = await _query_coverage(
        monkeypatch,
        repository=repository,
        members=(submitted, pending),
        now=datetime(2026, 8, 10, 20, tzinfo=SHANGHAI),
    )

    assert facts["completed_count"] == 1
    assert facts["pending_confirmation_count"] == 1
    daily = _daily(facts, "2026-08-10")
    assert daily["completed_members"] == [
        {"member_name": "已提交人员", "submission_state": "submitted"}
    ]
    assert daily["pending_confirmation_members"] == [
        {
            "member_name": "待确认人员",
            "submission_state": "pending_confirmation",
        }
    ]
    assert _person(facts, "待确认人员")[
        "pending_confirmation_dates"
    ] == ["2026-08-10"]


@pytest.mark.asyncio
async def test_empty_collecting_with_internal_state_is_not_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_date = date(2026, 8, 10)
    member = _member("member-empty-collecting", "空草稿人员")
    repository = InMemoryDashboardRepository(
        teams=(TEAM_FIVE,),
        members=(member,),
        obligations=(_obligation(member, report_date),),
        reports=(
            _report(
                member,
                report_date,
                status="collecting",
                section_status={"_agent2_report_version": 3},
            ),
        ),
    )

    facts = await _query_coverage(
        monkeypatch,
        repository=repository,
        members=(member,),
        now=datetime(2026, 8, 10, 20, tzinfo=SHANGHAI),
    )

    assert facts["partial_count"] == 0
    assert facts["not_filled_count"] == 1
    assert _daily(facts, "2026-08-10")["not_filled_members"] == [
        {"member_name": "空草稿人员", "submission_state": "overdue"}
    ]


@pytest.mark.asyncio
async def test_overlapping_membership_blocks_instead_of_guessing_team() -> None:
    report_date = date(2026, 8, 10)
    member = SimpleNamespace(
        ref="member-overlap",
        name="归属重叠人员",
        team_ref=TEAM_FIVE.ref,
        team_name=TEAM_FIVE.name,
        department_name=TEAM_FIVE.department_name,
        team_code=TEAM_FIVE.code,
        effective_from=report_date,
        effective_to=None,
        membership_unambiguous=False,
    )
    repository = InMemoryDashboardRepository(
        teams=(TEAM_FIVE,),
        members=(member,),
        obligations=(),
        reports=(),
    )

    with pytest.raises(SubmissionCoverageDataError):
        await SubmissionCoverageQuery(repository).execute(
            SubmissionCoverageRequest(
                tenant_id=TENANT_ID,
                scope_name=TEAM_FIVE.name,
                period_type="current_week",
                current_date=report_date,
                now=datetime(2026, 8, 10, 20, tzinfo=SHANGHAI),
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("duplicate_kind", ["obligation", "report"])
async def test_duplicate_period_facts_block_instead_of_double_counting(
    duplicate_kind: str,
) -> None:
    report_date = date(2026, 8, 10)
    member = _member("member-duplicate", "重复事实人员")
    obligation = _obligation(member, report_date)
    report = _report(member, report_date, status="completed")
    repository = InMemoryDashboardRepository(
        teams=(TEAM_FIVE,),
        members=(member,),
        obligations=(
            (obligation, obligation)
            if duplicate_kind == "obligation"
            else (obligation,)
        ),
        reports=(
            (report, report)
            if duplicate_kind == "report"
            else (report,)
        ),
    )

    with pytest.raises(SubmissionCoverageDataError):
        await SubmissionCoverageQuery(repository).execute(
            SubmissionCoverageRequest(
                tenant_id=TENANT_ID,
                scope_name=TEAM_FIVE.name,
                period_type="current_week",
                current_date=report_date,
                now=datetime(2026, 8, 10, 20, tzinfo=SHANGHAI),
            )
        )


@pytest.mark.asyncio
async def test_sql_period_read_marks_overlapping_memberships() -> None:
    class _Rows:
        def mappings(self) -> _Rows:
            return self

        def all(self) -> list[dict[str, object]]:
            return []

    class _Session:
        def __init__(self) -> None:
            self.statements: list[object] = []

        async def execute(self, statement: object) -> _Rows:
            self.statements.append(statement)
            return _Rows()

    session = _Session()
    await SqlDashboardRepository(session).load_records(  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        team_refs=(TEAM_FIVE.ref,),
        start_date=date(2026, 8, 3),
        end_date=date(2026, 8, 9),
    )

    member_sql = str(session.statements[0])
    assert "AS membership_unambiguous" in member_sql
    assert "competing.user_id = memberships.user_id" in member_sql
    assert "GREATEST(" in member_sql
    assert "LEAST(" in member_sql
