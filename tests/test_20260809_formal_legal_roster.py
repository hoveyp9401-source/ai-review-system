from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from app.legal_daily_roster import (
    FORMAL_CENTER_MEMBER_COUNT,
    FORMAL_CHILD_MEMBER_COUNT,
    FORMAL_CHILD_TEAM_NAMES,
    formal_roster_user_ids_for_exact_scope,
    load_formal_legal_daily_roster,
)
from app.legal_daily_dashboard.domain import (
    DashboardRecords,
    MemberRecord,
    SubmissionObligation,
)
from app.services.management_daily_briefing import _validate_briefing_roster


def _formal_rows() -> list[dict[str, object]]:
    team_counts = {
        "法务一部": 10,
        "法务二部": 10,
        "法务三部": 10,
        "法务四部": 10,
        "法务五部": 10,
        "法务六部": 10,
        "综合管理部": 10,
    }
    rows: list[dict[str, object]] = []
    index = 0
    for team_index, (team_name, count) in enumerate(team_counts.items(), start=1):
        for member_index in range(count):
            index += 1
            member_name = f"成员{index}"
            rows.append(
                {
                    "user_id": f"user-{index}",
                    "user_name": member_name,
                    "dingtalk_user_id": f"ding-{index}",
                    "user_team_id": f"team-{team_index}",
                    "team_id": f"team-{team_index}",
                    "team_code": f"monthly-law-{team_index}",
                    "team_name": team_name,
                    "department_name": "法务合约中心",
                    "team_active": True,
                    "data_complete": True,
                }
            )
    for center_name in ("丁益明", "薛旭", "中心直属甲", "中心直属乙"):
        index += 1
        rows.append(
            {
                "user_id": f"user-{index}",
                "user_name": center_name,
                "dingtalk_user_id": f"ding-{index}",
                "user_team_id": "legal-center-team",
                "team_id": "legal-center-team",
                "team_code": "legal-center",
                "team_name": "法务合约中心（中心层级）",
                "department_name": "法务合约中心",
                "team_active": False,
                "data_complete": True,
            }
        )
    return rows


class _Mappings:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return _Mappings(self._rows)


class _Session:
    def __init__(self, rows):
        self._rows = rows
        self.statement = None
        self.parameters = None

    async def execute(self, statement, parameters):
        self.statement = statement
        self.parameters = parameters
        return _Result(self._rows)


@pytest.mark.asyncio
async def test_formal_roster_is_one_validated_70_plus_4_snapshot():
    session = _Session(_formal_rows())

    roster = await load_formal_legal_daily_roster(
        session,  # type: ignore[arg-type]
        tenant_id="legal-daily-production-v1",
        on_date=date(2026, 8, 9),
    )

    assert roster.member_count == 74
    assert len(roster.child_members) == FORMAL_CHILD_MEMBER_COUNT == 70
    assert len(roster.center_members) == FORMAL_CENTER_MEMBER_COUNT == 4
    assert {member.team_name for member in roster.child_members} == FORMAL_CHILD_TEAM_NAMES
    assert {member.user_name for member in roster.center_members} == {
        "丁益明",
        "薛旭",
        "中心直属甲",
        "中心直属乙",
    }
    assert roster.member_by_name("丁益明").center_direct is True
    assert roster.member_by_name("薛旭").center_direct is True
    assert session.parameters == {
        "tenant_id": "legal-daily-production-v1",
        "on_date": date(2026, 8, 9),
        "parent_department": "法务合约中心",
        "center_team_code": "legal-center",
    }


@pytest.mark.asyncio
async def test_formal_roster_rejects_duplicate_membership_instead_of_guessing():
    rows = _formal_rows()
    rows.append(dict(rows[0]))

    with pytest.raises(RuntimeError, match="duplicate current memberships"):
        await load_formal_legal_daily_roster(
            _Session(rows),  # type: ignore[arg-type]
            tenant_id="legal-daily-production-v1",
            on_date=date(2026, 8, 9),
        )


@pytest.mark.asyncio
async def test_formal_roster_rejects_72_plus_2_drift():
    rows = _formal_rows()
    moved_to_child = rows[-1]
    moved_to_child.update(
        {
            "user_team_id": "team-1",
            "team_id": "team-1",
            "team_code": "monthly-law-1",
            "team_name": "法务一部",
            "team_active": True,
        }
    )
    moved_to_child = rows[-2]
    moved_to_child.update(
        {
            "user_team_id": "team-1",
            "team_id": "team-1",
            "team_code": "monthly-law-1",
            "team_name": "法务一部",
            "team_active": True,
        }
    )

    with pytest.raises(RuntimeError, match="expected 70 child members"):
        await load_formal_legal_daily_roster(
            _Session(rows),  # type: ignore[arg-type]
            tenant_id="legal-daily-production-v1",
            on_date=date(2026, 8, 9),
        )


@pytest.mark.asyncio
async def test_formal_roster_rejects_confirmed_manager_moved_into_child_team():
    rows = _formal_rows()
    ding_yiming = next(row for row in rows if row["user_name"] == "丁益明")
    ding_yiming.update(
        {
            "user_team_id": "team-1",
            "team_id": "team-1",
            "team_code": "monthly-law-1",
            "team_name": "法务一部",
        }
    )

    with pytest.raises(RuntimeError, match="丁益明 must be center-direct"):
        await load_formal_legal_daily_roster(
            _Session(rows),  # type: ignore[arg-type]
            tenant_id="legal-daily-production-v1",
            on_date=date(2026, 8, 9),
        )


def test_management_briefing_must_use_the_exact_formal_roster():
    report_date = date(2026, 8, 9)
    records = DashboardRecords(
        members=(
            MemberRecord(ref="user-1", name="成员1", team_ref="team-1"),
            MemberRecord(ref="user-2", name="成员2", team_ref="team-1"),
        ),
        obligations=(
            SubmissionObligation(
                member_ref="user-1",
                team_ref="team-1",
                report_date=report_date,
                required=True,
                reason="",
                deadline_at=None,
                data_complete=True,
            ),
            SubmissionObligation(
                member_ref="user-2",
                team_ref="team-1",
                report_date=report_date,
                required=True,
                reason="",
                deadline_at=None,
                data_complete=True,
            ),
        ),
    )
    formal_roster = SimpleNamespace(
        on_date=report_date,
        members=(
            SimpleNamespace(user_id="user-1", user_name="成员1", team_id="team-1"),
            SimpleNamespace(user_id="user-2", user_name="成员2", team_id="team-1"),
        ),
    )

    _validate_briefing_roster(records, formal_roster=formal_roster)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="members and teams"):
        _validate_briefing_roster(
            DashboardRecords(
                members=records.members[:1],
                obligations=records.obligations,
            ),
            formal_roster=formal_roster,  # type: ignore[arg-type]
        )
    with pytest.raises(RuntimeError, match="obligations"):
        _validate_briefing_roster(
            DashboardRecords(
                members=records.members,
                obligations=records.obligations[:1],
            ),
            formal_roster=formal_roster,  # type: ignore[arg-type]
        )


def test_reminder_configuration_must_cover_exactly_the_formal_roster():
    members = (
        SimpleNamespace(user_id="user-1", user_name="成员1", dingtalk_user_id="ding-1"),
        SimpleNamespace(user_id="user-2", user_name="成员2", dingtalk_user_id="ding-2"),
    )
    roster = SimpleNamespace(
        members=members,
        user_ids=tuple(member.user_id for member in members),
        dingtalk_user_ids=tuple(member.dingtalk_user_id for member in members),
        member_count=2,
    )

    assert formal_roster_user_ids_for_exact_scope(
        roster,
        {"ding-1", "ding-2"},
    ) == ("user-1", "user-2")

    with pytest.raises(RuntimeError, match="exactly match the formal roster"):
        formal_roster_user_ids_for_exact_scope(
            roster,
            {"ding-1", "outside"},
        )

    with pytest.raises(RuntimeError, match="exactly match the formal roster"):
        formal_roster_user_ids_for_exact_scope(
            roster,
            {"ding-1", "user-1"},
        )
