from __future__ import annotations

import asyncio
from datetime import date, datetime
from typing import Any

import pytest

from app.legal_daily_dashboard.chat_query import (
    ManagedDailyQuery,
    ManagedDailyQueryAmbiguous,
    ManagedDailyQueryRequest,
)
from app.legal_daily_dashboard.domain import DashboardActor, TeamRecord
from app.legal_daily_dashboard.repository import InMemoryDashboardRepository
from app.legal_daily_dashboard.sql_repository import SqlDashboardRepository


class _Rows:
    def __init__(self, values: list[dict[str, Any]]) -> None:
        self._values = values

    def mappings(self) -> "_Rows":
        return self

    def all(self) -> list[dict[str, Any]]:
        return self._values


class _CapturingSession:
    def __init__(self) -> None:
        self.statement: Any = None

    async def execute(self, statement: Any) -> _Rows:
        self.statement = statement
        return _Rows([])


class _DateCapturingRepository(InMemoryDashboardRepository):
    def __init__(self, *, teams: tuple[TeamRecord, ...]) -> None:
        super().__init__(teams=teams)
        self.requested_date: date | None = None

    async def list_member_teams(
        self,
        *,
        tenant_id: str,
        on_date: date | None = None,
    ) -> tuple[TeamRecord, ...]:
        del tenant_id
        self.requested_date = on_date
        return self._teams


def test_member_team_query_filters_memberships_by_requested_date() -> None:
    session = _CapturingSession()
    requested_date = date(2026, 8, 3)

    asyncio.run(
        SqlDashboardRepository(session).list_member_teams(
            tenant_id="legal-daily-production-v1",
            on_date=requested_date,
        )
    )

    sql = str(session.statement)
    assert "memberships.effective_from <= CAST(:on_date AS DATE)" in sql
    assert "memberships.effective_to >= CAST(:on_date AS DATE)" in sql
    assert session.statement.compile().params["on_date"] == requested_date


def test_briefing_team_query_filters_memberships_and_assignments_by_date() -> None:
    session = _CapturingSession()
    requested_date = date(2026, 8, 3)

    asyncio.run(
        SqlDashboardRepository(session).list_teams(
            tenant_id="legal-daily-production-v1",
            on_date=requested_date,
        )
    )

    sql = str(session.statement)
    assert "memberships.effective_from <= CAST(:on_date AS DATE)" in sql
    assert "memberships.effective_to >= CAST(:on_date AS DATE)" in sql
    assert "assignments.effective_from <= CAST(:on_date AS DATE)" in sql
    assert "assignments.effective_to >= CAST(:on_date AS DATE)" in sql
    assert session.statement.compile().params["on_date"] == requested_date


def test_managed_daily_query_uses_report_date_for_team_visibility() -> None:
    requested_date = date(2026, 8, 3)
    team = TeamRecord(
        ref="formal-team",
        name="综合管理部",
        department_name="法务合约中心",
        code="team-01",
    )
    repository = _DateCapturingRepository(teams=(team,))

    result = asyncio.run(
        ManagedDailyQuery(repository).execute(
            actor=DashboardActor(
                tenant_id="legal-daily-production-v1",
                user_id="manager",
            ),
            request=ManagedDailyQueryRequest(
                view="team_reports",
                report_date=requested_date,
                team_name="综合部",
            ),
            now=datetime(2026, 8, 4, 10, 43),
        )
    )

    assert repository.requested_date == requested_date
    assert result["team_name"] == "综合管理部"
    assert result["team_label"] == "法务合约中心 / 综合管理部"
    assert "team-01" not in str(result)
    assert "monthly-admin" not in str(result)


def test_managed_daily_query_does_not_guess_a_non_unique_team_shorthand() -> None:
    requested_date = date(2026, 8, 3)
    teams = (
        TeamRecord(
            ref="legal-team-1",
            name="法务一部",
            department_name="法务合约中心",
            code="law-1",
        ),
        TeamRecord(
            ref="legal-team-2",
            name="法务二部",
            department_name="法务合约中心",
            code="law-2",
        ),
    )
    repository = _DateCapturingRepository(teams=teams)

    with pytest.raises(ManagedDailyQueryAmbiguous):
        asyncio.run(
            ManagedDailyQuery(repository).execute(
                actor=DashboardActor(
                    tenant_id="legal-daily-production-v1",
                    user_id="manager",
                ),
                request=ManagedDailyQueryRequest(
                    view="team_reports",
                    report_date=requested_date,
                    team_name="法务部",
                ),
                now=datetime(2026, 8, 4, 10, 43),
            )
        )
