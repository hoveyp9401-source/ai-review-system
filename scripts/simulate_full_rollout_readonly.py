from __future__ import annotations

import asyncio
import json
from collections import Counter, defaultdict
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.config import get_settings
from app.db import AsyncSessionLocal
from app.legal_daily_dashboard.domain import (
    DailyReportRecord,
    DashboardRecords,
    SubmissionObligation,
)
from app.legal_daily_dashboard.sql_repository import SqlDashboardRepository
from app.scheduler.runner import (
    DAILY_BRIEFING_SAFE_MESSAGE_CHARS,
    _daily_briefing_report_date,
    _split_daily_briefing_text,
)
from app.services.management_daily_briefing import (
    BriefingRecipient,
    build_management_daily_briefings,
)


SIMULATED_REPORT_DATE = date(2026, 8, 7)
SIMULATED_SEND_DATE = date(2026, 8, 8)
EXPECTED_TEAMS = {
    "法务一部",
    "法务二部",
    "法务三部",
    "法务四部",
    "法务五部",
    "法务六部",
    "综合管理部",
}
EXPECTED_DEPARTMENT = "法务合约中心"
EXPECTED_DEPARTMENT_RECIPIENTS = {"赵卫中", "朱佳佳"}
ROLLOUT_COUNT = 74
CHILD_ROSTER_COUNT = 72
CENTER_LEVEL_TEAM_CODE = "legal-center"
EXPECTED_CENTER_LEVEL_MEMBERS = {"赵卫中", "朱佳佳"}
EXPECTED_CONFIRMED_TEAM_LEADS = {
    "法务二部": "丁益明",
    "法务四部": "薛旭",
}


async def main() -> None:
    settings = get_settings()
    tenant_id = str(settings.legal_daily_dashboard_tenant_id or "").strip()
    if not tenant_id:
        raise AssertionError("legal daily tenant is not configured")

    async with AsyncSessionLocal() as session:
        repository = SqlDashboardRepository(session)
        teams = await repository.list_teams(
            tenant_id=tenant_id,
            on_date=SIMULATED_REPORT_DATE,
        )
        live_records = await repository.load_records(
            tenant_id=tenant_id,
            team_refs=None,
            start_date=SIMULATED_REPORT_DATE,
            end_date=SIMULATED_REPORT_DATE,
        )
        roster_rows = (
            (
                await session.execute(
                    text(
                        """
                    SELECT
                        teams.id::text AS team_id,
                        teams.name AS team_name,
                        teams.code AS team_code,
                        teams.department_name,
                        teams.active AS team_active,
                        users.id::text AS user_id,
                        users.name AS user_name,
                        users.dingtalk_user_id,
                        users.active AS user_active,
                        users.team_id::text AS user_team_id,
                        memberships.data_complete
                    FROM legal_daily_team_memberships memberships
                    JOIN teams ON teams.id = memberships.team_id
                    JOIN users ON users.id = memberships.user_id
                    WHERE memberships.tenant_id = :tenant_id
                      AND memberships.effective_from <= :report_date
                      AND (
                          memberships.effective_to IS NULL
                          OR memberships.effective_to >= :report_date
                      )
                      AND teams.department_name = :department_name
                      AND (
                          teams.active IS TRUE
                          OR teams.code = :center_level_team_code
                      )
                    ORDER BY teams.code, teams.name, users.name
                    """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "report_date": SIMULATED_REPORT_DATE,
                        "department_name": EXPECTED_DEPARTMENT,
                        "center_level_team_code": CENTER_LEVEL_TEAM_CODE,
                    },
                )
            )
            .mappings()
            .all()
        )
        assignment_rows = (
            (
                await session.execute(
                    text(
                        """
                    SELECT
                        assignments.assignment_id::text AS assignment_id,
                        assignments.dashboard_role,
                        assignments.team_id::text AS team_id,
                        COALESCE(direct_users.id, fallback_users.id)::text AS user_id,
                        COALESCE(direct_users.name, fallback_users.name) AS user_name,
                        COALESCE(
                            direct_users.dingtalk_user_id,
                            fallback_users.dingtalk_user_id
                        ) AS dingtalk_user_id,
                        COALESCE(direct_users.active, fallback_users.active) AS user_active
                    FROM legal_daily_access_assignments assignments
                    LEFT JOIN users direct_users
                      ON direct_users.id::text = assignments.principal_user_id
                    LEFT JOIN users fallback_users
                      ON direct_users.id IS NULL
                     AND fallback_users.dingtalk_user_id = assignments.principal_user_id
                    WHERE assignments.tenant_id = :tenant_id
                      AND assignments.active IS TRUE
                      AND assignments.effective_from <= :report_date
                      AND (
                          assignments.effective_to IS NULL
                          OR assignments.effective_to >= :report_date
                      )
                    ORDER BY assignments.dashboard_role, assignments.team_id
                    """
                    ),
                    {"tenant_id": tenant_id, "report_date": SIMULATED_REPORT_DATE},
                )
            )
            .mappings()
            .all()
        )
        control_rows = (
            (
                await session.execute(
                    text(
                        """
                    SELECT
                        users.id::text AS user_id,
                        controls.tenant_id,
                        controls.enabled,
                        controls.messages_enabled,
                        controls.runtime,
                        controls.model_name
                    FROM legal_daily_team_memberships memberships
                    JOIN users ON users.id = memberships.user_id
                    JOIN teams ON teams.id = memberships.team_id
                    LEFT JOIN agent2_tool_call_canary_controls controls
                      ON controls.user_id = users.id::text
                    WHERE memberships.tenant_id = :tenant_id
                      AND memberships.effective_from <= :report_date
                      AND (
                          memberships.effective_to IS NULL
                          OR memberships.effective_to >= :report_date
                      )
                      AND teams.department_name = :department_name
                      AND (
                          teams.active IS TRUE
                          OR teams.code = :center_level_team_code
                      )
                    ORDER BY users.id
                    """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "report_date": SIMULATED_REPORT_DATE,
                        "department_name": EXPECTED_DEPARTMENT,
                        "center_level_team_code": CENTER_LEVEL_TEAM_CODE,
                    },
                )
            )
            .mappings()
            .all()
        )

    _assert_roster(teams, roster_rows)
    recipients = _simulated_recipients(
        assignment_rows=assignment_rows,
        roster_rows=roster_rows,
        configured_cc=str(
            settings.management_daily_briefing_department_cc_user_ids or ""
        ),
    )
    simulated_records = _simulated_records(live_records)
    briefings = build_management_daily_briefings(
        report_date=SIMULATED_REPORT_DATE,
        now=datetime(2026, 8, 8, 9, 0, tzinfo=ZoneInfo(settings.timezone)),
        teams=teams,
        records=simulated_records,
        recipients=recipients,
    )
    result = _assert_briefings(briefings)

    active_roster_count = sum(bool(row["user_active"]) for row in roster_rows)
    if len(control_rows) != len(roster_rows):
        raise AssertionError("each roster member may have at most one Agent2 control")
    runtime_tenant_ids = {
        str(row["tenant_id"]) for row in control_rows if row.get("tenant_id")
    }
    if len(runtime_tenant_ids) != 1:
        raise AssertionError("current Agent2 controls must share one runtime tenant")
    enabled_control_count = sum(bool(row["enabled"]) for row in control_rows)
    ready_control_count = sum(
        bool(row["enabled"])
        and bool(row["messages_enabled"])
        and row["runtime"] == "canary_execute"
        for row in control_rows
    )
    result.update(
        {
            "simulation_only": True,
            "database_writes": 0,
            "dingtalk_send_calls": 0,
            "report_date": SIMULATED_REPORT_DATE.isoformat(),
            "send_date": SIMULATED_SEND_DATE.isoformat(),
            "scheduler_report_date": _daily_briefing_report_date(
                SIMULATED_SEND_DATE
            ).isoformat(),
            "team_count": len(teams),
            "roster_count": len(roster_rows),
            "child_department_member_count": sum(
                bool(row["team_active"]) for row in roster_rows
            ),
            "center_level_member_count": sum(
                str(row["team_code"]) == CENTER_LEVEL_TEAM_CODE
                for row in roster_rows
            ),
            "currently_active_users": active_roster_count,
            "users_to_activate_at_real_rollout": len(roster_rows) - active_roster_count,
            "currently_enabled_agent2_controls": enabled_control_count,
            "currently_message_ready_agent2_controls": ready_control_count,
            "controls_to_prepare_at_real_rollout": len(roster_rows)
            - ready_control_count,
            "runtime_tenant_count": len(runtime_tenant_ids),
            "current_agent2_active_user_limit": (
                settings.agent2_tool_call_canary_max_active_users
            ),
            "required_agent2_active_user_limit_at_real_rollout": len(roster_rows),
            "summary_time": (
                f"{settings.summary_cron_hour:02d}:{settings.summary_cron_minute:02d}"
            ),
            "scheduler_enabled": bool(settings.scheduler_enabled),
            "scheduler_pause_dates": str(settings.scheduler_pause_dates or ""),
        }
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _assert_roster(teams, roster_rows) -> None:
    team_names = {team.name for team in teams}
    if team_names != EXPECTED_TEAMS:
        raise AssertionError({"unexpected_team_names": sorted(team_names)})
    department_names = {team.department_name for team in teams}
    if department_names != {EXPECTED_DEPARTMENT}:
        raise AssertionError({"unexpected_departments": sorted(department_names)})
    if len(roster_rows) != ROLLOUT_COUNT:
        raise AssertionError({"unexpected_roster_count": len(roster_rows)})
    user_ids = [str(row["user_id"] or "") for row in roster_rows]
    dingtalk_ids = [str(row["dingtalk_user_id"] or "") for row in roster_rows]
    if len(set(user_ids)) != ROLLOUT_COUNT:
        raise AssertionError("each person must belong to exactly one current team")
    if not all(dingtalk_ids) or len(set(dingtalk_ids)) != ROLLOUT_COUNT:
        raise AssertionError(
            f"all {ROLLOUT_COUNT} people need one unique DingTalk identity"
        )
    if any(
        str(row["user_team_id"] or "") != str(row["team_id"] or "")
        for row in roster_rows
    ):
        raise AssertionError("each person's system team must match the current roster")
    if any(not bool(row["data_complete"]) for row in roster_rows):
        raise AssertionError("the rollout roster contains incomplete organization data")

    child_rows = [row for row in roster_rows if bool(row["team_active"])]
    center_rows = [
        row
        for row in roster_rows
        if str(row["team_code"] or "") == CENTER_LEVEL_TEAM_CODE
        and not bool(row["team_active"])
    ]
    if len(child_rows) != CHILD_ROSTER_COUNT:
        raise AssertionError({"unexpected_child_roster_count": len(child_rows)})
    if {str(row["team_name"]) for row in child_rows} != EXPECTED_TEAMS:
        raise AssertionError("child-department names do not match the seven-team structure")
    if len({str(row["team_id"]) for row in child_rows}) != len(EXPECTED_TEAMS):
        raise AssertionError("child-department roster does not resolve to seven teams")
    if {str(row["user_name"]) for row in center_rows} != EXPECTED_CENTER_LEVEL_MEMBERS:
        raise AssertionError("center-level roster does not match the expected two people")


def _simulated_recipients(*, assignment_rows, roster_rows, configured_cc: str):
    roster_by_identifier = {}
    rollout_user_ids = set()
    for row in roster_rows:
        roster_by_identifier[str(row["user_id"])] = row
        roster_by_identifier[str(row["dingtalk_user_id"])] = row
        rollout_user_ids.add(str(row["user_id"]))

    resolved = []
    for row in assignment_rows:
        if not row["user_id"] or not str(row["dingtalk_user_id"] or "").strip():
            raise AssertionError({"unresolved_assignment": row["assignment_id"]})
        if str(row["user_id"]) not in rollout_user_ids:
            raise AssertionError(
                {"management_recipient_outside_rollout": str(row["user_name"] or "")}
            )
        resolved.append(
            BriefingRecipient(
                id=str(row["user_id"]),
                name=str(row["user_name"] or ""),
                dingtalk_user_id=str(row["dingtalk_user_id"]),
                role=str(row["dashboard_role"]),
                team_ref=str(row["team_id"]) if row["team_id"] else None,
            )
        )

    for identifier in _csv_values(configured_cc):
        row = roster_by_identifier.get(identifier)
        if row is None:
            raise AssertionError({"unresolved_department_cc": identifier})
        resolved.append(
            BriefingRecipient(
                id=str(row["user_id"]),
                name=str(row["user_name"]),
                dingtalk_user_id=str(row["dingtalk_user_id"]),
                role="department_cc",
            )
        )

    team_lead_counts = Counter(
        recipient.team_ref for recipient in resolved if recipient.role == "team_lead"
    )
    if len(team_lead_counts) != 7 or set(team_lead_counts.values()) != {1}:
        raise AssertionError({"team_lead_counts": dict(team_lead_counts)})
    department_names = {
        recipient.name
        for recipient in resolved
        if recipient.role in {"legal_head", "department_cc"}
    }
    if department_names != EXPECTED_DEPARTMENT_RECIPIENTS:
        raise AssertionError(
            {"unexpected_department_recipients": sorted(department_names)}
        )
    return tuple(resolved)


def _simulated_records(live_records: DashboardRecords) -> DashboardRecords:
    members_by_team = defaultdict(list)
    for member in live_records.members:
        members_by_team[member.team_ref].append(member)

    obligations = tuple(
        SubmissionObligation(
            member_ref=member.ref,
            team_ref=member.team_ref,
            report_date=SIMULATED_REPORT_DATE,
            required=True,
            reason="full_rollout_readonly_simulation",
            deadline_at=None,
            data_complete=True,
            source="simulation",
        )
        for member in live_records.members
    )
    reports = []
    for team_ref, members in members_by_team.items():
        first = sorted(members, key=lambda member: member.name)[0]
        reports.append(
            DailyReportRecord(
                ref=f"simulation-{team_ref}",
                member_ref=first.ref,
                team_ref=team_ref,
                report_date=SIMULATED_REPORT_DATE,
                status="completed",
                confirmation_type="manual",
                confirmed_by_user=True,
                today_work=("完成全员上线模拟中的示例工作",),
                problems=("暂无",),
                tomorrow_plan=("继续验证日报查询与晨报",),
            )
        )
    return DashboardRecords(
        members=live_records.members,
        obligations=obligations,
        reports=tuple(reports),
    )


def _assert_briefings(briefings: dict[str, object]) -> dict[str, object]:
    team_messages = list(briefings.get("team_messages") or [])
    if len(team_messages) != 7:
        raise AssertionError({"unexpected_team_message_count": len(team_messages)})
    if any(message.get("target_count") != 1 for message in team_messages):
        raise AssertionError("each team briefing must have exactly one team leader")
    team_leads = {
        str(message.get("team_name") or ""): str(
            (message.get("recipients") or [{}])[0].get("name") or ""
        )
        for message in team_messages
    }
    for team_name, expected_lead in EXPECTED_CONFIRMED_TEAM_LEADS.items():
        if team_leads.get(team_name) != expected_lead:
            raise AssertionError(
                {
                    "unexpected_confirmed_team_lead": {
                        "team": team_name,
                        "expected": expected_lead,
                        "actual": team_leads.get(team_name),
                    }
                }
            )
    department_message = dict(briefings.get("department_message") or {})
    if department_message.get("department_name") != EXPECTED_DEPARTMENT:
        raise AssertionError(department_message.get("department_name"))
    department_recipients = {
        str(recipient.get("name") or "")
        for recipient in department_message.get("recipients") or []
    }
    if department_recipients != EXPECTED_DEPARTMENT_RECIPIENTS:
        raise AssertionError(sorted(department_recipients))

    team_snapshot_refs: set[str] = set()
    for message in team_messages:
        snapshot_rows = _assert_briefing_snapshot(
            message,
            expected_scope="team",
        )
        snapshot_refs = {
            str(row.get("member_ref") or "") for row in snapshot_rows
        }
        if team_snapshot_refs & snapshot_refs:
            raise AssertionError("a member appears in more than one team snapshot")
        team_snapshot_refs.update(snapshot_refs)

    department_snapshot_rows = _assert_briefing_snapshot(
        department_message,
        expected_scope="department",
    )
    department_snapshot_refs = {
        str(row.get("member_ref") or "")
        for row in department_snapshot_rows
    }
    if len(team_snapshot_refs) != CHILD_ROSTER_COUNT:
        raise AssertionError(
            {"unexpected_team_snapshot_member_count": len(team_snapshot_refs)}
        )
    if len(department_snapshot_refs) != ROLLOUT_COUNT:
        raise AssertionError(
            {
                "unexpected_department_snapshot_member_count": len(
                    department_snapshot_refs
                )
            }
        )
    if not team_snapshot_refs < department_snapshot_refs:
        raise AssertionError(
            "the department snapshot must contain every team member and center direct members"
        )
    center_snapshot_members = [
        row
        for row in department_snapshot_rows
        if str(row.get("member_ref") or "") not in team_snapshot_refs
    ]
    if {
        str(row.get("member_name") or "") for row in center_snapshot_members
    } != EXPECTED_CENTER_LEVEL_MEMBERS:
        raise AssertionError(
            "the department snapshot does not contain the expected center direct members"
        )

    messages = [*team_messages, department_message]
    part_counts = []
    for message in messages:
        message_text = str(message.get("text") or "")
        if not message_text:
            raise AssertionError("briefing text must not be empty")
        parts = _split_daily_briefing_text(message_text)
        if any(len(part) > DAILY_BRIEFING_SAFE_MESSAGE_CHARS for part in parts):
            raise AssertionError("briefing split exceeds DingTalk safe length")
        part_counts.append(len(parts))
    return {
        "team_briefing_recipients": {
            **team_leads,
        },
        "department_briefing_recipients": sorted(department_recipients),
        "department_name": department_message["department_name"],
        "briefing_message_count": len(messages),
        "briefing_part_count": sum(part_counts),
        "team_snapshot_member_count": len(team_snapshot_refs),
        "department_snapshot_member_count": len(department_snapshot_refs),
        "center_direct_snapshot_members": sorted(
            str(row.get("member_name") or "")
            for row in center_snapshot_members
        ),
        "briefing_content_sections_checked": [
            "填报概览",
            "需要负责人关注",
            "关键进展",
            "重点计划",
            "填报质量提示",
        ],
    }


def _assert_briefing_snapshot(
    message: dict[str, object],
    *,
    expected_scope: str,
) -> list[dict[str, object]]:
    snapshot = message.get("briefing_snapshot")
    if not isinstance(snapshot, dict):
        raise AssertionError("briefing snapshot is missing")
    if snapshot.get("scope") != expected_scope:
        raise AssertionError(
            {
                "unexpected_briefing_snapshot_scope": snapshot.get("scope"),
                "expected": expected_scope,
            }
        )
    if snapshot.get("report_date") != SIMULATED_REPORT_DATE.isoformat():
        raise AssertionError(
            {"unexpected_briefing_snapshot_date": snapshot.get("report_date")}
        )
    if not str(snapshot.get("generated_at") or ""):
        raise AssertionError("briefing snapshot generation time is missing")
    rows = snapshot.get("members")
    if not isinstance(rows, list) or not rows:
        raise AssertionError("briefing snapshot members are missing")
    if any(not isinstance(row, dict) for row in rows):
        raise AssertionError("briefing snapshot contains an invalid member row")
    typed_rows = [dict(row) for row in rows]
    member_refs = [str(row.get("member_ref") or "") for row in typed_rows]
    if not all(member_refs) or len(member_refs) != len(set(member_refs)):
        raise AssertionError("briefing snapshot member references are missing or duplicated")
    required_text_fields = ("member_name", "team_ref", "team_name")
    if any(
        not all(str(row.get(field) or "") for field in required_text_fields)
        for row in typed_rows
    ):
        raise AssertionError("briefing snapshot member identity is incomplete")
    allowed_classifications = {
        "submitted",
        "pending_confirmation",
        "missing",
        "responsibility_unknown",
        "exempt",
    }
    classifications = [
        str(row.get("classification") or "") for row in typed_rows
    ]
    if any(value not in allowed_classifications for value in classifications):
        raise AssertionError(
            {"unexpected_briefing_classifications": sorted(set(classifications))}
        )
    stats = message.get("stats")
    if not isinstance(stats, dict):
        raise AssertionError("briefing statistics are missing")
    if len(typed_rows) != int(stats.get("total") or 0):
        raise AssertionError(
            {
                "snapshot_member_count": len(typed_rows),
                "briefing_total": stats.get("total"),
            }
        )
    if classifications.count("submitted") != int(stats.get("completed") or 0):
        raise AssertionError("briefing snapshot completed count does not match its text facts")
    if classifications.count("missing") != int(stats.get("missing") or 0):
        raise AssertionError("briefing snapshot missing count does not match its text facts")
    if classifications.count("responsibility_unknown") != int(
        stats.get("unknown_responsibility") or 0
    ):
        raise AssertionError(
            "briefing snapshot responsibility count does not match its text facts"
        )
    if classifications.count("exempt") != int(stats.get("exempt") or 0):
        raise AssertionError("briefing snapshot exemption count does not match its text facts")
    return typed_rows


def _csv_values(raw: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            item.strip()
            for item in raw.replace(";", ",").replace("\n", ",").split(",")
            if item.strip()
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
