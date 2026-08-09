from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from datetime import date

from app.agent2.report_insight_query import StructuredReportInsightQuery
from app.agent2.report_insights import ReportInsightModule, SqlReportInsightRepository
from app.config import get_settings
from app.db import AsyncSessionLocal
from app.legal_daily_dashboard.sql_repository import SqlDashboardRepository
from app.legal_daily_roster import (
    FORMAL_CENTER_MEMBER_NAMES,
    FORMAL_CHILD_MEMBER_COUNT,
    FORMAL_CHILD_TEAM_NAMES,
    FORMAL_CONFIRMED_TEAM_LEADS,
    FORMAL_ROSTER_MEMBER_COUNT,
    formal_roster_user_ids_for_exact_scope,
    load_formal_legal_daily_roster,
)
from app.scheduler.jobs import (
    _configured_test_user_ids,
    ensure_daily_submission_obligations,
)
from app.services.management_daily_briefing import (
    ManagementDailyBriefingService,
    _validate_briefing_roster,
)


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-date", type=date.fromisoformat, required=True)
    return parser.parse_args()


async def audit(report_date: date) -> dict[str, object]:
    settings = get_settings()
    tenant_id = str(settings.legal_daily_dashboard_tenant_id or "").strip()
    if not tenant_id:
        raise RuntimeError("legal daily tenant is not configured")

    async with AsyncSessionLocal() as session:
        roster = await load_formal_legal_daily_roster(
            session,
            tenant_id=tenant_id,
            on_date=report_date,
        )
        configured_identifiers = _configured_test_user_ids(settings)
        formal_roster_user_ids_for_exact_scope(roster, configured_identifiers)

        dashboard_repository = SqlDashboardRepository(session)
        records = await dashboard_repository.load_records(
            tenant_id=tenant_id,
            team_refs=None,
            start_date=report_date,
            end_date=report_date,
        )
        _validate_briefing_roster(records, formal_roster=roster)

        insight_repository = SqlReportInsightRepository(
            session,
            roster_date=report_date,
            roster_tenant_id=tenant_id,
        )
        query_users = tuple(await insight_repository.list_users())
        query_teams = tuple(await insight_repository.list_teams())
        requester = next(
            user for user in query_users if str(getattr(user, "name", "")) == "庞浩"
        )
        query_answer = await ReportInsightModule(insight_repository).answer_query(
            StructuredReportInsightQuery(
                query="法务合约中心本周都做了什么",
                query_kind="period_work",
                scope_type="organization",
                scope_name="法务合约中心",
                period_type="current_week",
            ),
            requester=requester,
            current_date=report_date,
        )
        briefings = await ManagementDailyBriefingService(settings).build(
            session,
            report_date,
        )
        obligation_ensure_result = await ensure_daily_submission_obligations(
            session,
            settings,
            report_date,
        )
        await session.rollback()

    team_counts = Counter(
        member.team_name for member in roster.members if not member.center_direct
    )
    department_message = briefings.get("department_message") or {}
    department_snapshot = department_message.get("briefing_snapshot") or {}
    team_briefing_recipients = {
        str(message.get("team_name") or ""): sorted(
            str(recipient.get("name") or "")
            for recipient in message.get("recipients", [])
        )
        for message in briefings.get("team_messages", [])
    }
    department_recipients = {
        str(recipient.get("name") or "")
        for recipient in department_message.get("recipients", [])
    }
    if set(team_counts) != set(FORMAL_CHILD_TEAM_NAMES):
        raise RuntimeError("briefing audit found an unexpected child department")
    if len(query_users) != FORMAL_ROSTER_MEMBER_COUNT:
        raise RuntimeError("report query user count changed")
    if len(query_teams) != len(FORMAL_CHILD_TEAM_NAMES) + 1:
        raise RuntimeError("report query team scope changed")
    if query_answer is None or not query_answer.evidence.facts.get(
        "permission_allowed"
    ):
        raise RuntimeError("formal department report query was not allowed")
    query_target_team_ids = tuple(
        query_answer.evidence.facts.get("target_team_ids") or ()
    )
    if len(query_target_team_ids) != len(FORMAL_CHILD_TEAM_NAMES) + 1:
        raise RuntimeError("formal department query omitted a team scope")
    if len(department_snapshot.get("members", [])) != FORMAL_ROSTER_MEMBER_COUNT:
        raise RuntimeError("department briefing snapshot is incomplete")
    if set(team_briefing_recipients) != set(FORMAL_CHILD_TEAM_NAMES) or any(
        len(names) != 1 for names in team_briefing_recipients.values()
    ):
        raise RuntimeError("team briefing recipients are incomplete or ambiguous")
    if any(
        team_briefing_recipients.get(team_name) != [expected_lead]
        for team_name, expected_lead in FORMAL_CONFIRMED_TEAM_LEADS.items()
    ):
        raise RuntimeError("confirmed team briefing recipient changed")
    if department_recipients != FORMAL_CENTER_MEMBER_NAMES:
        raise RuntimeError("department briefing recipients changed")

    return {
        "simulation_only": True,
        "database_writes": 0,
        "dingtalk_send_calls": 0,
        "report_date": report_date.isoformat(),
        "formal_roster_count": roster.member_count,
        "child_member_count": len(roster.child_members),
        "center_member_count": len(roster.center_members),
        "child_team_counts": dict(sorted(team_counts.items())),
        "center_member_names": sorted(
            member.user_name for member in roster.center_members
        ),
        "configured_reminder_scope_count": len(configured_identifiers),
        "dashboard_member_count": len(records.members),
        "dashboard_obligation_count": len(records.obligations),
        "report_query_user_count": len(query_users),
        "report_query_team_scope_count": len(query_teams),
        "department_query_target_team_count": len(query_target_team_ids),
        "department_query_permission_allowed": True,
        "obligation_ensure_inserted": obligation_ensure_result["inserted"],
        "obligation_ensure_effective_count": obligation_ensure_result[
            "effective_obligations"
        ],
        "team_briefing_count": len(briefings.get("team_messages", [])),
        "team_briefing_recipients": team_briefing_recipients,
        "department_snapshot_member_count": len(
            department_snapshot.get("members", [])
        ),
        "department_recipients": sorted(department_recipients),
        "expected_formal_roster_count": FORMAL_ROSTER_MEMBER_COUNT,
        "expected_child_member_count": FORMAL_CHILD_MEMBER_COUNT,
    }


async def main() -> None:
    result = await audit(_args().report_date)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
