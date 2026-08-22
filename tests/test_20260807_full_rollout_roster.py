from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.agent2.tool_calling.canary_control import (
    CanaryControlSnapshot,
    CanaryIdentitySnapshot,
    CanaryRuntimeAttestation,
    decide_canary_route,
)
from app.config import Settings
from scripts.manage_full_rollout import (
    CENTER_ROSTER_COUNT,
    CENTER_LEVEL_TEAM_CODE,
    FORMAL_CONFIRMED_CENTER_DIRECT_MEMBER_NAMES,
    EXPECTED_CHILD_TEAMS,
    ROLLOUT_COUNT,
    _validate_roster,
)
from scripts.simulate_full_rollout_readonly import (
    EXPECTED_CONFIRMED_TEAM_LEADS,
    SIMULATED_REPORT_DATE,
    _assert_briefings,
    _assert_roster,
)


CHILD_TEAM_COUNTS = {
    "法务一部": 10,
    "法务二部": 10,
    "法务三部": 10,
    "法务四部": 10,
    "法务五部": 10,
    "法务六部": 10,
    "综合管理部": 10,
}


def _rollout_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    index = 0
    for team_number, (team_name, count) in enumerate(
        CHILD_TEAM_COUNTS.items(), start=1
    ):
        team_id = f"child-team-{team_number}"
        for member_index in range(count):
            index += 1
            member_name = f"子部门成员{index}"
            rows.append(
                {
                    "user_id": f"user-{index}",
                    "name": member_name,
                    "user_name": member_name,
                    "active": False,
                    "dingtalk_user_id": f"ding-{index}",
                    "user_team_id": team_id,
                    "team_id": team_id,
                    "data_complete": True,
                    "team_code": f"child-{team_number}",
                    "team_name": team_name,
                    "department_name": "法务合约中心",
                    "team_active": True,
                }
            )
    center_team_id = "center-team"
    center_names = (
        *sorted(FORMAL_CONFIRMED_CENTER_DIRECT_MEMBER_NAMES),
        "中心直属甲",
        "中心直属乙",
    )
    for name in center_names:
        index += 1
        rows.append(
            {
                "user_id": f"user-{index}",
                "name": name,
                "user_name": name,
                "active": False,
                "dingtalk_user_id": f"ding-{index}",
                "user_team_id": center_team_id,
                "team_id": center_team_id,
                "data_complete": True,
                "team_code": CENTER_LEVEL_TEAM_CODE,
                "team_name": "法务合约中心（中心层级）",
                "department_name": "法务合约中心",
                "team_active": False,
            }
        )
    return rows


def test_full_rollout_roster_is_seven_departments_plus_four_center_members() -> None:
    rows = _rollout_rows()

    _validate_roster(rows)

    assert len(rows) == ROLLOUT_COUNT == 74
    assert sum(bool(row["team_active"]) for row in rows) == 70
    assert sum(not bool(row["team_active"]) for row in rows) == CENTER_ROSTER_COUNT == 4
    assert {
        str(row["team_name"]) for row in rows if bool(row["team_active"])
    } == EXPECTED_CHILD_TEAMS


def test_child_department_only_roster_is_rejected_as_not_full_rollout() -> None:
    child_rows = [row for row in _rollout_rows() if bool(row["team_active"])]

    with pytest.raises(RuntimeError, match="expected 74 roster members, got 70"):
        _validate_roster(child_rows)


def test_readonly_simulation_accepts_74_without_creating_an_eighth_department() -> None:
    teams = tuple(
        SimpleNamespace(name=name, department_name="法务合约中心")
        for name in EXPECTED_CHILD_TEAMS
    )

    _assert_roster(teams, _rollout_rows())

    assert len(teams) == 7


def test_agent2_cohort_limit_accepts_all_74_people() -> None:
    control = CanaryControlSnapshot(
        tenant_id="tenant",
        user_id="user",
        enabled=True,
        runtime="canary_execute",
        messages_enabled=True,
        registry_digest="registry",
        prompt_sha256="prompt",
        model_name="model",
        version=1,
    )
    identity = CanaryIdentitySnapshot(
        tenant_id="tenant",
        user_id="user",
        active=True,
        exact_binding_count=1,
    )
    runtime = CanaryRuntimeAttestation(
        runtime_ready=True,
        runtime_mode="canary_execute",
        registry_digest="registry",
        prompt_sha256="prompt",
        model_name="model",
        production_database_verified=True,
        sandbox_configuration_present=False,
        messages_sender_configured=True,
        api_ingress_ready=True,
        stream_ingress_ready=True,
    )

    decision = decide_canary_route(
        control=control,
        identity=identity,
        runtime=runtime,
        active_canary_control_count=74,
        active_canary_control_limit=74,
    )

    assert decision.owner == "tool_call_core"


def test_settings_accept_74_and_reject_more_than_full_roster() -> None:
    settings = Settings(
        _env_file=None,
        agent2_tool_call_canary_max_active_users=74,
    )
    assert settings.agent2_tool_call_canary_max_active_users == 74

    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            agent2_tool_call_canary_max_active_users=75,
        )


def _briefings_for_confirmed_lead_test() -> dict[str, object]:
    roster = _rollout_rows()

    def snapshot_rows(team_name: str | None = None) -> list[dict[str, object]]:
        selected = [
            row
            for row in roster
            if team_name is None or row["team_name"] == team_name
        ]
        return [
            {
                "member_ref": row["user_id"],
                "member_name": row["name"],
                "team_ref": row["team_id"],
                "team_name": row["team_name"],
                "classification": "missing",
                "report_status": None,
                "confirmation_type": None,
                "submitted_at": None,
            }
            for row in selected
        ]

    def stats(rows: list[dict[str, object]]) -> dict[str, int]:
        return {
            "total": len(rows),
            "completed": 0,
            "missing": len(rows),
            "unknown_responsibility": 0,
            "exempt": 0,
        }

    team_messages = []
    for team_name in CHILD_TEAM_COUNTS:
        lead_name = EXPECTED_CONFIRMED_TEAM_LEADS.get(
            team_name,
            f"{team_name}负责人",
        )
        team_rows = snapshot_rows(team_name)
        team_messages.append(
            {
                "team_name": team_name,
                "target_count": 1,
                "recipients": [{"name": lead_name}],
                "stats": stats(team_rows),
                "briefing_snapshot": {
                    "scope": "team",
                    "report_date": SIMULATED_REPORT_DATE.isoformat(),
                    "generated_at": "2026-08-08T09:00:00+08:00",
                    "members": team_rows,
                },
                "text": "填报概览\n需要负责人关注\n关键进展\n重点计划\n填报质量提示",
            }
        )
    department_rows = snapshot_rows()
    return {
        "team_messages": team_messages,
        "department_message": {
            "department_name": "法务合约中心",
            "recipients": [{"name": "赵卫中"}, {"name": "朱佳佳"}],
            "stats": stats(department_rows),
            "briefing_snapshot": {
                "scope": "department",
                "report_date": SIMULATED_REPORT_DATE.isoformat(),
                "generated_at": "2026-08-08T09:00:00+08:00",
                "members": department_rows,
            },
            "text": "填报概览\n需要负责人关注\n关键进展\n重点计划\n填报质量提示",
        },
    }


def test_center_level_members_can_lead_second_and_fourth_legal_teams() -> None:
    result = _assert_briefings(_briefings_for_confirmed_lead_test())

    assert result["team_briefing_recipients"]["法务二部"] == "丁益明"
    assert result["team_briefing_recipients"]["法务四部"] == "薛旭"
    assert result["department_briefing_recipients"] == ["朱佳佳", "赵卫中"]


def test_confirmed_center_level_team_lead_mismatch_is_rejected() -> None:
    briefings = _briefings_for_confirmed_lead_test()
    second_team = next(
        message
        for message in briefings["team_messages"]
        if message["team_name"] == "法务二部"
    )
    second_team["recipients"] = [{"name": "错误负责人"}]

    with pytest.raises(AssertionError, match="unexpected_confirmed_team_lead"):
        _assert_briefings(briefings)
