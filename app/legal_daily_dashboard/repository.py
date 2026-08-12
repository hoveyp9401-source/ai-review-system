from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import date
from typing import Protocol

from app.legal_daily_dashboard.domain import (
    DailyReportRecord,
    DashboardActor,
    DashboardRecords,
    DashboardScope,
    ManagementTarget,
    ManagerDecisionRecord,
    MemberRecord,
    ReviewSuggestionRecord,
    SubmissionObligation,
    TeamRecord,
    WorkItemRecord,
)


class DashboardRepository(Protocol):
    async def resolve_scope(
        self,
        *,
        actor: DashboardActor,
        on_date: date,
    ) -> DashboardScope | None: ...

    async def list_teams(
        self,
        *,
        tenant_id: str,
        on_date: date | None = None,
    ) -> tuple[TeamRecord, ...]: ...

    async def list_member_teams(
        self,
        *,
        tenant_id: str,
        on_date: date | None = None,
    ) -> tuple[TeamRecord, ...]: ...

    async def load_records(
        self,
        *,
        tenant_id: str,
        team_refs: tuple[str, ...] | None,
        start_date: date,
        end_date: date,
    ) -> DashboardRecords: ...

    async def find_management_target(
        self,
        *,
        tenant_id: str,
        target_type: str,
        target_ref: str,
    ) -> ManagementTarget | None: ...

    async def save_manager_decision(
        self,
        decision: ManagerDecisionRecord,
    ) -> ManagerDecisionRecord: ...


class InMemoryDashboardRepository:
    """Test adapter for the dashboard interface."""

    def __init__(
        self,
        *,
        scopes: Mapping[tuple[str, str], DashboardScope] | None = None,
        teams: tuple[TeamRecord, ...] = (),
        members: tuple[MemberRecord, ...] = (),
        obligations: tuple[SubmissionObligation, ...] = (),
        reports: tuple[DailyReportRecord, ...] = (),
        suggestions: tuple[ReviewSuggestionRecord, ...] = (),
        work_items: tuple[WorkItemRecord, ...] = (),
    ) -> None:
        self._scopes = dict(scopes or {})
        self._teams = teams
        self._members = members
        self._obligations = obligations
        self._reports = reports
        self._suggestions = suggestions
        self._work_items = work_items
        self._decisions: list[ManagerDecisionRecord] = []
        self._decisions_by_idempotency: dict[
            tuple[str, str],
            ManagerDecisionRecord,
        ] = {}

    async def resolve_scope(
        self,
        *,
        actor: DashboardActor,
        on_date: date,
    ) -> DashboardScope | None:
        del on_date
        return self._scopes.get((actor.tenant_id, actor.user_id))

    async def list_teams(
        self,
        *,
        tenant_id: str,
        on_date: date | None = None,
    ) -> tuple[TeamRecord, ...]:
        del tenant_id, on_date
        return self._teams

    async def list_member_teams(
        self,
        *,
        tenant_id: str,
        on_date: date | None = None,
    ) -> tuple[TeamRecord, ...]:
        del tenant_id, on_date
        return self._teams

    async def load_records(
        self,
        *,
        tenant_id: str,
        team_refs: tuple[str, ...] | None,
        start_date: date,
        end_date: date,
    ) -> DashboardRecords:
        del tenant_id

        def team_visible(team_ref: str) -> bool:
            return team_refs is None or team_ref in team_refs

        visible_suggestions = tuple(
            suggestion
            for suggestion in self._suggestions
            if team_visible(suggestion.team_ref)
            and start_date <= suggestion.report_date <= end_date
        )
        visible_work_items = tuple(
            item
            for item in self._work_items
            if team_visible(item.team_ref)
            and item.first_seen <= end_date
            and item.last_seen >= start_date
        )
        visible_target_refs = {
            *(suggestion.ref for suggestion in visible_suggestions),
            *(item.ref for item in visible_work_items),
        }
        return DashboardRecords(
            members=tuple(
                member
                for member in self._members
                if team_visible(member.team_ref)
            ),
            obligations=tuple(
                obligation
                for obligation in self._obligations
                if team_visible(obligation.team_ref)
                and start_date <= obligation.report_date <= end_date
            ),
            reports=tuple(
                report
                for report in self._reports
                if team_visible(report.team_ref)
                and start_date <= report.report_date <= end_date
            ),
            suggestions=visible_suggestions,
            work_items=visible_work_items,
            decisions=tuple(
                decision
                for decision in self._decisions
                if decision.target_ref in visible_target_refs
            ),
        )

    async def find_management_target(
        self,
        *,
        tenant_id: str,
        target_type: str,
        target_ref: str,
    ) -> ManagementTarget | None:
        del tenant_id
        if target_type == "review_suggestion":
            suggestion = next(
                (
                    item
                    for item in self._suggestions
                    if item.ref == target_ref and item.active
                ),
                None,
            )
            if suggestion is None:
                return None
            return ManagementTarget(
                target_type="review_suggestion",
                target_ref=suggestion.ref,
                team_ref=suggestion.team_ref,
                target_date=suggestion.report_date,
                evidence_snapshot={
                    "reason": suggestion.reason,
                    "evidence": [
                        {
                            "date": evidence.evidence_date.isoformat(),
                            "section": evidence.section,
                            "quote": evidence.quote,
                        }
                        for evidence in suggestion.evidence
                    ],
                },
            )
        if target_type == "work_item":
            item = next(
                (
                    candidate
                    for candidate in self._work_items
                    if candidate.ref == target_ref and candidate.active
                ),
                None,
            )
            if item is None:
                return None
            return ManagementTarget(
                target_type="work_item",
                target_ref=item.ref,
                team_ref=item.team_ref,
                target_date=item.last_seen,
                evidence_snapshot={
                    "title": item.title,
                    "summary": item.summary,
                    "timeline": [
                        {
                            "date": entry.entry_date.isoformat(),
                            "section": entry.section,
                            "quote": entry.quote,
                        }
                        for entry in item.entries
                    ],
                },
            )
        return None

    async def save_manager_decision(
        self,
        decision: ManagerDecisionRecord,
    ) -> ManagerDecisionRecord:
        idempotency_scope = (
            decision.tenant_id,
            decision.idempotency_key,
        )
        existing = self._decisions_by_idempotency.get(idempotency_scope)
        if existing is not None:
            return existing
        self._decisions_by_idempotency[idempotency_scope] = decision
        self._decisions.append(decision)
        return decision

    async def replace_member_analysis(
        self,
        *,
        tenant_id: str,
        member_ref: str,
        start_date: date,
        end_date: date,
        suggestions: tuple[ReviewSuggestionRecord, ...],
        work_items: tuple[WorkItemRecord, ...],
    ) -> None:
        del tenant_id
        suggestion_history = [
            replace(suggestion, active=False)
            if (
                suggestion.member_ref == member_ref
                and start_date <= suggestion.report_date <= end_date
                and suggestion.active
            )
            else suggestion
            for suggestion in self._suggestions
        ]
        suggestion_positions = {
            suggestion.ref: index
            for index, suggestion in enumerate(suggestion_history)
        }
        for suggestion in suggestions:
            position = suggestion_positions.get(suggestion.ref)
            if position is None:
                suggestion_positions[suggestion.ref] = len(
                    suggestion_history
                )
                suggestion_history.append(suggestion)
            else:
                suggestion_history[position] = replace(
                    suggestion_history[position],
                    active=True,
                    evaluated_at=(
                        suggestion.evaluated_at
                        or suggestion_history[position].evaluated_at
                    ),
                )
        self._suggestions = tuple(suggestion_history)

        item_history = [
            replace(item, active=False)
            if (
                member_ref in item.member_refs
                and item.first_seen <= end_date
                and item.last_seen >= start_date
                and item.active
            )
            else item
            for item in self._work_items
        ]
        item_positions = {
            item.ref: index for index, item in enumerate(item_history)
        }
        for item in work_items:
            position = item_positions.get(item.ref)
            if position is None:
                item_positions[item.ref] = len(item_history)
                item_history.append(item)
            else:
                item_history[position] = replace(
                    item_history[position],
                    active=True,
                    evaluated_at=(
                        item.evaluated_at
                        or item_history[position].evaluated_at
                    ),
                )
        self._work_items = tuple(item_history)
