from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Protocol

from app.legal_daily_dashboard.analysis import AnalysisResult
from app.legal_daily_dashboard.domain import (
    DailyReportRecord,
    DashboardRecords,
    MemberRecord,
    ReviewSuggestionRecord,
    TeamRecord,
    WorkItemRecord,
)


class ReviewAnalyzer(Protocol):
    async def analyze(
        self,
        *,
        member: MemberRecord,
        team: TeamRecord,
        reports: tuple[DailyReportRecord, ...],
        previous_decisions: tuple[dict[str, object], ...] = (),
    ) -> AnalysisResult: ...


class AnalysisRepository(Protocol):
    async def list_teams(self, *, tenant_id: str) -> tuple[TeamRecord, ...]: ...

    async def load_records(
        self,
        *,
        tenant_id: str,
        team_refs: tuple[str, ...] | None,
        start_date: date,
        end_date: date,
    ) -> DashboardRecords: ...

    async def replace_member_analysis(
        self,
        *,
        tenant_id: str,
        member_ref: str,
        start_date: date,
        end_date: date,
        suggestions: tuple[ReviewSuggestionRecord, ...],
        work_items: tuple[WorkItemRecord, ...],
    ) -> None: ...


class ReviewAnalysisNotFound(LookupError):
    pass


class ReviewAnalysisService:
    """Explicit analysis job; it is not exposed as a dashboard page action."""

    def __init__(
        self,
        *,
        repository: AnalysisRepository,
        analyzer: ReviewAnalyzer,
    ) -> None:
        self._repository = repository
        self._analyzer = analyzer

    async def analyze_member(
        self,
        *,
        tenant_id: str,
        team_ref: str,
        member_ref: str,
        end_date: date,
        days: int = 14,
    ) -> dict[str, Any]:
        if days not in {7, 14, 30}:
            raise ValueError("days must be 7, 14, or 30")
        start_date = end_date - timedelta(days=days - 1)
        records = await self._repository.load_records(
            tenant_id=tenant_id,
            team_refs=(team_ref,),
            start_date=start_date,
            end_date=end_date,
        )
        member = next(
            (
                value
                for value in records.members
                if value.ref == member_ref and value.team_ref == team_ref
            ),
            None,
        )
        if member is None:
            raise ReviewAnalysisNotFound("member not found")
        team = next(
            (
                value
                for value in await self._repository.list_teams(
                    tenant_id=tenant_id
                )
                if value.ref == team_ref
            ),
            None,
        )
        if team is None:
            raise ReviewAnalysisNotFound("team not found")
        reports = tuple(
            sorted(
                (
                    report
                    for report in records.reports
                    if report.member_ref == member_ref
                ),
                key=lambda report: report.report_date,
            )
        )
        target_refs = {
            suggestion.ref
            for suggestion in records.suggestions
            if suggestion.member_ref == member_ref
        } | {
            item.ref
            for item in records.work_items
            if member_ref in item.member_refs
        }
        previous_decisions = tuple(
            {
                "target_type": decision.target_type,
                "target_ref": decision.target_ref,
                "decision": decision.decision,
                "note": decision.note,
                "evidence_snapshot": decision.evidence_snapshot,
                "created_at": decision.created_at.isoformat(),
            }
            for decision in records.decisions
            if decision.target_ref in target_refs
        )
        result = await self._analyzer.analyze(
            member=member,
            team=team,
            reports=reports,
            previous_decisions=previous_decisions,
        )
        await self._repository.replace_member_analysis(
            tenant_id=tenant_id,
            member_ref=member_ref,
            start_date=start_date,
            end_date=end_date,
            suggestions=result.suggestions,
            work_items=result.work_items,
        )
        return {
            "member_ref": member_ref,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "report_count": len(reports),
            "suggestion_count": len(result.suggestions),
            "work_item_count": len(result.work_items),
        }
