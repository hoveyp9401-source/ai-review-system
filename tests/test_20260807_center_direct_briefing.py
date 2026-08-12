from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.legal_daily_dashboard.domain import (
    DailyReportRecord,
    DashboardRecords,
    MemberRecord,
    SubmissionObligation,
    TeamRecord,
)
from app.services.management_daily_briefing import (
    BriefingRecipient,
    _hidden_missing_detail_member_refs,
    build_management_daily_briefings,
)
from app.services.summary_service import SummaryService


REPORT_DATE = date(2026, 8, 7)
TEAM_2 = TeamRecord(
    ref="team-2",
    name="法务二部",
    department_name="法务合约中心",
    code="monthly-law-2",
)
TEAM_4 = TeamRecord(
    ref="team-4",
    name="法务四部",
    department_name="法务合约中心",
    code="monthly-law-4",
)
CENTER_REF = "legal-center"


def _records(
    *,
    missing_member_refs: frozenset[str] = frozenset(),
) -> DashboardRecords:
    members = (
        MemberRecord(ref="ding", name="丁益明", team_ref=TEAM_2.ref),
        MemberRecord(ref="xue", name="薛旭", team_ref=TEAM_4.ref),
        MemberRecord(ref="zhao", name="赵卫中", team_ref=CENTER_REF),
        MemberRecord(ref="zhu", name="朱佳佳", team_ref=CENTER_REF),
    )
    obligations = tuple(
        SubmissionObligation(
            member_ref=member.ref,
            team_ref=member.team_ref,
            report_date=REPORT_DATE,
            required=True,
            reason="verified_roster",
            deadline_at=None,
            data_complete=True,
            source="test",
        )
        for member in members
    )
    reports = tuple(
        DailyReportRecord(
            ref=f"report-{member.ref}",
            member_ref=member.ref,
            team_ref=member.team_ref,
            report_date=REPORT_DATE,
            status="completed",
            confirmation_type="manual",
            confirmed_by_user=True,
            today_work=(f"{member.name}今日工作",),
            tomorrow_plan=(f"{member.name}明日计划",),
        )
        for member in members
        if member.ref not in missing_member_refs
    )
    return DashboardRecords(
        members=members,
        obligations=obligations,
        reports=reports,
    )


def _recipients() -> tuple[BriefingRecipient, ...]:
    return (
        BriefingRecipient(
            id="ding",
            name="丁益明",
            dingtalk_user_id="28829",
            role="team_lead",
            team_ref=TEAM_2.ref,
        ),
        BriefingRecipient(
            id="xue",
            name="薛旭",
            dingtalk_user_id="40595",
            role="team_lead",
            team_ref=TEAM_4.ref,
        ),
        BriefingRecipient(
            id="zhao",
            name="赵卫中",
            dingtalk_user_id="zhao-ding",
            role="legal_head",
        ),
        BriefingRecipient(
            id="zhu",
            name="朱佳佳",
            dingtalk_user_id="zhu-ding",
            role="department_cc",
        ),
    )


def test_department_briefing_counts_seven_team_members_and_two_center_direct_once() -> None:
    briefings = build_management_daily_briefings(
        report_date=REPORT_DATE,
        now=datetime(2026, 8, 8, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        teams=(TEAM_2, TEAM_4),
        records=_records(),
        recipients=_recipients(),
    )

    team_messages = briefings["team_messages"]
    assert [message["team_name"] for message in team_messages] == [
        "法务二部",
        "法务四部",
    ]
    assert [message["stats"]["total"] for message in team_messages] == [1, 1]

    department = briefings["department_message"]
    assert department["stats"] == {
        "total": 4,
        "completed": 4,
        "missing": 0,
        "unknown_responsibility": 0,
        "exempt": 0,
        "teams": 2,
        "teams_with_responsibility_data": 2,
        "center_direct_members": 2,
    }
    assert [recipient["name"] for recipient in department["recipients"]] == [
        "赵卫中",
        "朱佳佳",
    ]
    assert "中心直属" in department["text"]
    assert "中心直属｜赵卫中" in department["text"]
    assert "中心直属｜朱佳佳" in department["text"]
    assert "丁益明" in department["text"]
    assert "薛旭" in department["text"]


def test_center_direct_does_not_create_an_eighth_team_message() -> None:
    briefings = build_management_daily_briefings(
        report_date=REPORT_DATE,
        now=datetime(2026, 8, 8, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        teams=(TEAM_2, TEAM_4),
        records=_records(),
        recipients=_recipients(),
    )

    assert all(
        message["team_name"] != "中心直属"
        for message in briefings["team_messages"]
    )
    assert briefings["department_message"]["stats"]["teams"] == 2


def test_legal_head_stays_in_missing_totals_but_name_is_hidden() -> None:
    briefings = build_management_daily_briefings(
        report_date=REPORT_DATE,
        now=datetime(2026, 8, 8, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        teams=(TEAM_2, TEAM_4),
        records=_records(missing_member_refs=frozenset({"zhao"})),
        recipients=_recipients(),
        hidden_missing_detail_member_refs=frozenset({"zhao"}),
    )

    department = briefings["department_message"]
    assert department["stats"] == {
        "total": 4,
        "completed": 3,
        "missing": 1,
        "unknown_responsibility": 0,
        "exempt": 0,
        "teams": 2,
        "teams_with_responsibility_data": 2,
        "center_direct_members": 2,
    }
    assert "已交 3/4｜未交 1" in department["text"]
    assert "**中心直属**\n   已交 1/2｜未交 1" in department["text"]
    assert "**中心直属（1人）**" not in department["text"]
    assert "未交计数已记录，本栏无需要单独通报的人员。" in department["text"]
    assert "已知应交人员均已提交。" not in department["text"]
    assert "赵卫中" not in department["text"]

    zhao_snapshot = next(
        member
        for member in department["briefing_snapshot"]["members"]
        if member["member_ref"] == "zhao"
    )
    assert zhao_snapshot["classification"] == "missing"


def test_other_missing_names_remain_visible_when_legal_head_is_hidden() -> None:
    briefings = build_management_daily_briefings(
        report_date=REPORT_DATE,
        now=datetime(2026, 8, 8, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        teams=(TEAM_2, TEAM_4),
        records=_records(missing_member_refs=frozenset({"zhao", "zhu"})),
        recipients=_recipients(),
        hidden_missing_detail_member_refs=frozenset({"zhao"}),
    )

    department = briefings["department_message"]
    assert department["stats"]["missing"] == 2
    assert "已交 2/4｜未交 2" in department["text"]
    assert "**中心直属**\n   已交 0/2｜未交 2" in department["text"]
    assert "**中心直属（1人）**\n   朱佳佳" in department["text"]
    assert "**中心直属（2人）**" not in department["text"]
    assert "赵卫中" not in department["text"]


def test_a_different_legal_head_is_not_hidden_by_role() -> None:
    recipients = (
        *(_recipients()[0:2]),
        BriefingRecipient(
            id="other-head",
            name="其他负责人",
            dingtalk_user_id="other-head-ding",
            role="legal_head",
        ),
        _recipients()[3],
    )
    briefings = build_management_daily_briefings(
        report_date=REPORT_DATE,
        now=datetime(2026, 8, 8, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        teams=(TEAM_2, TEAM_4),
        records=_records(missing_member_refs=frozenset({"zhao", "zhu"})),
        recipients=recipients,
        hidden_missing_detail_member_refs=frozenset({"zhao"}),
    )

    text = briefings["department_message"]["text"]
    assert "**中心直属（1人）**\n   朱佳佳" in text
    assert "赵卫中" not in text


def test_center_direct_missing_detail_stays_visible_when_only_zhu_is_missing() -> None:
    briefings = build_management_daily_briefings(
        report_date=REPORT_DATE,
        now=datetime(2026, 8, 8, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        teams=(TEAM_2, TEAM_4),
        records=_records(missing_member_refs=frozenset({"zhu"})),
        recipients=_recipients(),
        hidden_missing_detail_member_refs=frozenset({"zhao"}),
    )

    department = briefings["department_message"]
    assert department["stats"]["missing"] == 1
    assert "**中心直属（1人）**\n   朱佳佳" in department["text"]
    assert "未交计数已记录，本栏无需要单独通报的人员。" not in department["text"]


def test_other_team_missing_detail_remains_when_zhao_is_the_only_center_missing_member() -> None:
    briefings = build_management_daily_briefings(
        report_date=REPORT_DATE,
        now=datetime(2026, 8, 8, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        teams=(TEAM_2, TEAM_4),
        records=_records(missing_member_refs=frozenset({"ding", "zhao"})),
        recipients=_recipients(),
        hidden_missing_detail_member_refs=frozenset({"zhao"}),
    )

    department = briefings["department_message"]
    assert department["stats"]["missing"] == 2
    assert "**法务二部（1人）**\n   丁益明" in department["text"]
    assert "**中心直属（" not in department["text"]
    assert "未交计数已记录，本栏无需要单独通报的人员。" not in department["text"]


def test_no_missing_members_keeps_the_all_submitted_empty_text() -> None:
    briefings = build_management_daily_briefings(
        report_date=REPORT_DATE,
        now=datetime(2026, 8, 8, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        teams=(TEAM_2, TEAM_4),
        records=_records(),
        recipients=_recipients(),
        hidden_missing_detail_member_refs=frozenset({"zhao"}),
    )

    department = briefings["department_message"]
    assert department["stats"]["missing"] == 0
    assert "已知应交人员均已提交。" in department["text"]
    assert "未交计数已记录，本栏无需要单独通报的人员。" not in department["text"]


@pytest.mark.asyncio
async def test_management_briefing_fails_closed_without_formal_roster() -> None:
    service = SummaryService(
        SimpleNamespace(legal_daily_dashboard_tenant_id=""),
        SimpleNamespace(),
    )

    with pytest.raises(
        RuntimeError,
        match="formal legal daily roster is required",
    ):
        await service.build_daily_briefings(SimpleNamespace(), REPORT_DATE)


@pytest.mark.parametrize("configured", ("", "zhu", "zhao,zhu"))
def test_hidden_detail_identity_must_be_exactly_zhao(configured: str) -> None:
    class Roster:
        @staticmethod
        def member_by_name(name: str) -> SimpleNamespace:
            assert name == "赵卫中"
            return SimpleNamespace(user_id="zhao")

    settings = SimpleNamespace(
        management_daily_briefing_hidden_missing_detail_user_ids=configured
    )
    with pytest.raises(
        RuntimeError,
        match="must match Zhao Weizhong exactly",
    ):
        _hidden_missing_detail_member_refs(
            settings,
            formal_roster=Roster(),
        )


def test_hidden_detail_identity_accepts_only_zhao() -> None:
    class Roster:
        @staticmethod
        def member_by_name(name: str) -> SimpleNamespace:
            assert name == "赵卫中"
            return SimpleNamespace(user_id="zhao")

    assert _hidden_missing_detail_member_refs(
        SimpleNamespace(
            management_daily_briefing_hidden_missing_detail_user_ids="zhao"
        ),
        formal_roster=Roster(),
    ) == frozenset({"zhao"})
