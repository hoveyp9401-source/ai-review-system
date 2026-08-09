from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.legal_daily_dashboard.domain import (
    DailyReportRecord,
    DashboardRecords,
    MemberRecord,
    SubmissionObligation,
    TeamRecord,
)
from app.services.management_daily_briefing import (
    BriefingRecipient,
    build_management_daily_briefings,
)


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


def _records() -> DashboardRecords:
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
