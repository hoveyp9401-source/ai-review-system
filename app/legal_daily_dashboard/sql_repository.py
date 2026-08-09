from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.legal_daily_dashboard.domain import (
    DailyReportRecord,
    DashboardActor,
    DashboardRecords,
    DashboardScope,
    EvidenceRecord,
    ManagementTarget,
    ManagerDecisionRecord,
    MemberRecord,
    ReviewSuggestionRecord,
    SubmissionObligation,
    TeamRecord,
    WorkItemEntryRecord,
    WorkItemRecord,
)


class SqlDashboardRepository:
    """Read projection plus isolated dashboard-analysis/decision writes.

    Daily-report rows remain owned by the existing report adapter. This
    repository only reads those rows and never updates their content or
    lifecycle.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve_scope(
        self,
        *,
        actor: DashboardActor,
        on_date: date,
    ) -> DashboardScope | None:
        result = await self._session.execute(
            text(
                """
                SELECT
                    dashboard_role,
                    team_id::text AS team_ref
                FROM legal_daily_access_assignments
                WHERE tenant_id = :tenant_id
                  AND principal_user_id = :principal_user_id
                  AND active IS TRUE
                  AND effective_from <= :on_date
                  AND (effective_to IS NULL OR effective_to >= :on_date)
                ORDER BY
                    CASE dashboard_role
                        WHEN 'legal_head' THEN 0
                        ELSE 1
                    END,
                    team_id
                """
            ).bindparams(
                tenant_id=actor.tenant_id,
                principal_user_id=actor.user_id,
                on_date=on_date,
            )
        )
        rows = result.mappings().all()
        if any(row.get("dashboard_role") == "legal_head" for row in rows):
            return DashboardScope(role="legal_head")
        team_refs: list[str] = []
        for row in rows:
            if row.get("dashboard_role") != "team_lead":
                continue
            team_ref = str(row.get("team_ref") or "").strip()
            if team_ref and team_ref not in team_refs:
                team_refs.append(team_ref)
        if not team_refs:
            return None
        return DashboardScope(
            role="team_lead",
            allowed_team_refs=tuple(team_refs),
        )

    async def list_teams(
        self,
        *,
        tenant_id: str,
        on_date: date | None = None,
    ) -> tuple[TeamRecord, ...]:
        result = await self._session.execute(
            text(
                """
                SELECT
                    teams.id::text AS team_ref,
                    teams.name AS team_name,
                    teams.department_name,
                    teams.code AS team_code
                FROM teams
                WHERE teams.active IS TRUE
                  AND teams.id IN (
                      SELECT memberships.team_id
                      FROM legal_daily_team_memberships memberships
                      WHERE memberships.tenant_id = :tenant_id
                        AND (
                            CAST(:on_date AS DATE) IS NULL
                            OR (
                                memberships.effective_from <= CAST(:on_date AS DATE)
                                AND (
                                    memberships.effective_to IS NULL
                                    OR memberships.effective_to >= CAST(:on_date AS DATE)
                                )
                            )
                        )
                      UNION
                      SELECT assignments.team_id
                      FROM legal_daily_access_assignments assignments
                      WHERE assignments.tenant_id = :tenant_id
                        AND assignments.team_id IS NOT NULL
                        AND assignments.active IS TRUE
                        AND (
                            CAST(:on_date AS DATE) IS NULL
                            OR (
                                assignments.effective_from <= CAST(:on_date AS DATE)
                                AND (
                                    assignments.effective_to IS NULL
                                    OR assignments.effective_to >= CAST(:on_date AS DATE)
                                )
                            )
                        )
                  )
                ORDER BY teams.name
                """
            ).bindparams(tenant_id=tenant_id, on_date=on_date)
        )
        rows = result.mappings().all()
        return tuple(
            TeamRecord(
                ref=str(row.get("team_ref") or ""),
                name=str(row.get("team_name") or ""),
                department_name=str(
                    row.get("department_name") or ""
                ),
                code=str(row.get("team_code") or ""),
            )
            for row in rows
            if row.get("team_ref") and row.get("team_name")
        )

    async def list_member_teams(
        self,
        *,
        tenant_id: str,
        on_date: date | None = None,
    ) -> tuple[TeamRecord, ...]:
        result = await self._session.execute(
            text(
                """
                SELECT DISTINCT
                    teams.id::text AS team_ref,
                    teams.name AS team_name,
                    teams.department_name,
                    teams.code AS team_code
                FROM legal_daily_team_memberships memberships
                JOIN teams ON teams.id = memberships.team_id
                WHERE memberships.tenant_id = :tenant_id
                  AND teams.active IS TRUE
                  AND (
                      CAST(:on_date AS DATE) IS NULL
                      OR (
                          memberships.effective_from <= CAST(:on_date AS DATE)
                          AND (
                              memberships.effective_to IS NULL
                              OR memberships.effective_to >= CAST(:on_date AS DATE)
                          )
                      )
                  )
                ORDER BY teams.name
                """
            ).bindparams(tenant_id=tenant_id, on_date=on_date)
        )
        rows = result.mappings().all()
        return tuple(
            TeamRecord(
                ref=str(row.get("team_ref") or ""),
                name=str(row.get("team_name") or ""),
                department_name=str(
                    row.get("department_name") or ""
                ),
                code=str(row.get("team_code") or ""),
            )
            for row in rows
            if row.get("team_ref") and row.get("team_name")
        )

    async def load_records(
        self,
        *,
        tenant_id: str,
        team_refs: tuple[str, ...] | None,
        start_date: date,
        end_date: date,
    ) -> DashboardRecords:
        team_filter = (
            " AND {alias}.team_id::text = ANY(:team_refs)"
            if team_refs is not None
            else ""
        )
        report_team_filter = (
            " AND membership_scope.team_ref = ANY(:team_refs)"
            if team_refs is not None
            else ""
        )
        parameters: dict[str, object] = {
            "tenant_id": tenant_id,
            "start_date": start_date,
            "end_date": end_date,
        }
        if team_refs is not None:
            parameters["team_refs"] = list(team_refs)

        member_rows = await self._rows(
            f"""
            SELECT DISTINCT
                users.id::text AS member_ref,
                users.name AS member_name,
                memberships.team_id::text AS team_ref,
                teams.name AS team_name,
                teams.department_name,
                teams.code AS team_code
            FROM legal_daily_team_memberships memberships
            JOIN users ON users.id = memberships.user_id
            JOIN teams ON teams.id = memberships.team_id
            WHERE memberships.tenant_id = :tenant_id
              AND memberships.effective_from <= :end_date
              AND (
                  memberships.effective_to IS NULL
                  OR memberships.effective_to >= :start_date
              )
              {team_filter.format(alias="memberships")}
            ORDER BY users.name
            """,
            parameters,
        )
        obligation_rows = await self._rows(
            f"""
            SELECT
                obligations.user_id::text AS member_ref,
                obligations.team_id::text AS team_ref,
                obligations.report_date,
                obligations.required,
                obligations.exemption_reason AS reason,
                obligations.deadline_at,
                obligations.data_complete,
                obligations.source
            FROM legal_daily_submission_obligations obligations
            WHERE obligations.tenant_id = :tenant_id
              AND obligations.report_date BETWEEN :start_date AND :end_date
              {team_filter.format(alias="obligations")}
            ORDER BY obligations.report_date, obligations.user_id
            """,
            parameters,
        )
        report_rows = await self._rows(
            f"""
            SELECT DISTINCT
                daily_reports.id::text AS report_ref,
                daily_reports.user_id::text AS member_ref,
                membership_scope.team_ref AS team_ref,
                daily_reports.date AS report_date,
                daily_reports.status,
                daily_reports.confirmation_type,
                daily_reports.confirmed_by_user,
                daily_reports.today_work,
                daily_reports.problems,
                daily_reports.tomorrow_plan,
                daily_reports.submitted_at,
                daily_reports.raw_input,
                daily_reports.input_fragments,
                daily_reports.section_status
            FROM daily_reports
            JOIN LATERAL (
                SELECT MIN(memberships.team_id::text) AS team_ref
                FROM legal_daily_team_memberships memberships
                WHERE memberships.user_id = daily_reports.user_id
                  AND memberships.tenant_id = :tenant_id
                  AND memberships.effective_from <= daily_reports.date
                  AND (
                      memberships.effective_to IS NULL
                      OR memberships.effective_to >= daily_reports.date
                  )
                HAVING COUNT(*) = 1
            ) membership_scope ON TRUE
            WHERE daily_reports.date BETWEEN :start_date AND :end_date
              {report_team_filter}
            ORDER BY report_date, member_ref
            """,
            parameters,
        )
        suggestion_rows = await self._rows(
            f"""
            SELECT
                suggestions.public_ref AS suggestion_ref,
                suggestions.user_id::text AS member_ref,
                suggestions.team_id::text AS team_ref,
                suggestions.report_date,
                suggestions.reason_type,
                suggestions.reason,
                suggestions.evidence_json AS evidence,
                suggestions.compared_dates_json AS compared_dates,
                suggestions.confidence,
                suggestions.work_item_title,
                suggestions.support_needed,
                suggestions.owner_level,
                suggestions.model_version,
                suggestions.updated_at AS evaluated_at,
                suggestions.active
            FROM legal_daily_review_suggestions suggestions
            WHERE suggestions.tenant_id = :tenant_id
              AND suggestions.report_date BETWEEN :start_date AND :end_date
              {team_filter.format(alias="suggestions")}
            ORDER BY suggestions.report_date, suggestions.created_at
            """,
            parameters,
        )
        work_item_rows = await self._rows(
            f"""
            SELECT
                items.public_ref AS item_ref,
                items.team_id::text AS team_ref,
                items.member_refs_json AS member_refs,
                items.title,
                items.item_status,
                items.summary,
                items.first_seen,
                items.last_seen,
                items.entries_json AS entries,
                items.confidence,
                items.model_version,
                items.updated_at AS evaluated_at,
                items.active
            FROM legal_daily_work_items items
            WHERE items.tenant_id = :tenant_id
              AND items.first_seen <= :end_date
              AND items.last_seen >= :start_date
              {team_filter.format(alias="items")}
            ORDER BY items.last_seen DESC, items.title
            """,
            parameters,
        )
        decision_rows = await self._rows(
            f"""
            SELECT
                decisions.decision_id::text AS decision_ref,
                decisions.tenant_id,
                decisions.target_type,
                decisions.target_ref,
                decisions.team_id::text AS team_ref,
                decisions.decision,
                decisions.note,
                decisions.actor_user_id,
                COALESCE(decision_actors.name, '') AS actor_name,
                decisions.actor_role,
                decisions.evidence_snapshot_json AS evidence_snapshot,
                decisions.idempotency_key,
                decisions.created_at
            FROM legal_daily_manager_decisions decisions
            LEFT JOIN users decision_actors
              ON decision_actors.dingtalk_user_id = decisions.actor_user_id
              OR decision_actors.id::text = decisions.actor_user_id
            WHERE decisions.tenant_id = :tenant_id
              {team_filter.format(alias="decisions")}
            ORDER BY decisions.created_at
            """,
            parameters,
        )
        return DashboardRecords(
            members=tuple(
                MemberRecord(
                    ref=str(row["member_ref"]),
                    name=str(row["member_name"]),
                    team_ref=str(row["team_ref"]),
                    team_name=str(row.get("team_name") or ""),
                    department_name=str(
                        row.get("department_name") or ""
                    ),
                    team_code=str(row.get("team_code") or ""),
                )
                for row in member_rows
            ),
            obligations=tuple(
                SubmissionObligation(
                    member_ref=str(row["member_ref"]),
                    team_ref=str(row["team_ref"]),
                    report_date=_as_date(row["report_date"]),
                    required=bool(row["required"]),
                    reason=str(row.get("reason") or ""),
                    deadline_at=row.get("deadline_at"),  # type: ignore[arg-type]
                    data_complete=bool(row["data_complete"]),
                    source=str(row.get("source") or ""),
                )
                for row in obligation_rows
            ),
            reports=tuple(_report_record(row) for row in report_rows),
            suggestions=tuple(_suggestion_record(row) for row in suggestion_rows),
            work_items=tuple(_work_item_record(row) for row in work_item_rows),
            decisions=tuple(_decision_record(row) for row in decision_rows),
        )

    async def find_management_target(
        self,
        *,
        tenant_id: str,
        target_type: str,
        target_ref: str,
    ) -> ManagementTarget | None:
        if target_type == "review_suggestion":
            query = """
                SELECT
                    'review_suggestion' AS target_type,
                    suggestions.public_ref AS target_ref,
                    suggestions.team_id::text AS team_ref,
                    suggestions.report_date AS target_date,
                    jsonb_build_object(
                        'reason', suggestions.reason,
                        'evidence', suggestions.evidence_json,
                        'compared_dates', suggestions.compared_dates_json,
                        'confidence', suggestions.confidence
                    ) AS evidence_snapshot
                FROM legal_daily_review_suggestions suggestions
                WHERE suggestions.tenant_id = :tenant_id
                  AND suggestions.public_ref = :target_ref
                  AND suggestions.active IS TRUE
            """
        elif target_type == "work_item":
            query = """
                SELECT
                    'work_item' AS target_type,
                    items.public_ref AS target_ref,
                    items.team_id::text AS team_ref,
                    items.last_seen AS target_date,
                    jsonb_build_object(
                        'title', items.title,
                        'summary', items.summary,
                        'entries', items.entries_json,
                        'confidence', items.confidence
                    ) AS evidence_snapshot
                FROM legal_daily_work_items items
                WHERE items.tenant_id = :tenant_id
                  AND items.public_ref = :target_ref
                  AND items.active IS TRUE
            """
        else:
            return None
        rows = await self._rows(
            query,
            {
                "tenant_id": tenant_id,
                "target_ref": target_ref,
            },
        )
        if not rows:
            return None
        row = rows[0]
        snapshot = row.get("evidence_snapshot")
        return ManagementTarget(
            target_type=str(row["target_type"]),  # type: ignore[arg-type]
            target_ref=str(row["target_ref"]),
            team_ref=str(row["team_ref"]),
            target_date=_as_date(row["target_date"]),
            evidence_snapshot=(dict(snapshot) if isinstance(snapshot, dict) else {}),
        )

    async def save_manager_decision(
        self,
        decision: ManagerDecisionRecord,
    ) -> ManagerDecisionRecord:
        parameters: dict[str, object] = {
            "decision_id": decision.ref,
            "tenant_id": decision.tenant_id,
            "target_type": decision.target_type,
            "target_ref": decision.target_ref,
            "team_id": decision.team_ref,
            "decision": decision.decision,
            "note": decision.note,
            "actor_user_id": decision.actor_user_id,
            "actor_role": decision.actor_role,
            "evidence_snapshot": json.dumps(
                decision.evidence_snapshot,
                ensure_ascii=False,
            ),
            "idempotency_key": decision.idempotency_key,
            "created_at": decision.created_at,
        }
        rows = await self._rows(
            """
            INSERT INTO legal_daily_manager_decisions (
                decision_id,
                tenant_id,
                target_type,
                target_ref,
                team_id,
                decision,
                note,
                actor_user_id,
                actor_role,
                evidence_snapshot_json,
                idempotency_key,
                created_at
            )
            VALUES (
                CAST(:decision_id AS uuid),
                :tenant_id,
                :target_type,
                :target_ref,
                CAST(:team_id AS uuid),
                :decision,
                :note,
                :actor_user_id,
                :actor_role,
                CAST(:evidence_snapshot AS jsonb),
                :idempotency_key,
                :created_at
            )
            ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
            RETURNING
                decision_id::text AS decision_ref,
                tenant_id,
                target_type,
                target_ref,
                team_id::text AS team_ref,
                decision,
                note,
                actor_user_id,
                ''::text AS actor_name,
                actor_role,
                evidence_snapshot_json AS evidence_snapshot,
                idempotency_key,
                created_at
            """,
            parameters,
        )
        if not rows:
            rows = await self._rows(
                """
                SELECT
                    decisions.decision_id::text AS decision_ref,
                    decisions.tenant_id,
                    decisions.target_type,
                    decisions.target_ref,
                    decisions.team_id::text AS team_ref,
                    decisions.decision,
                    decisions.note,
                    decisions.actor_user_id,
                    COALESCE(decision_actors.name, '') AS actor_name,
                    decisions.actor_role,
                    decisions.evidence_snapshot_json AS evidence_snapshot,
                    decisions.idempotency_key,
                    decisions.created_at
                FROM legal_daily_manager_decisions decisions
                LEFT JOIN users decision_actors
                  ON decision_actors.dingtalk_user_id = decisions.actor_user_id
                  OR decision_actors.id::text = decisions.actor_user_id
                WHERE decisions.tenant_id = :tenant_id
                  AND decisions.idempotency_key = :idempotency_key
                """,
                parameters,
            )
        if not rows:
            raise RuntimeError("manager decision write did not return a row")
        await self._session.commit()
        return _decision_record(rows[0])

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
        shared_parameters: dict[str, object] = {
            "tenant_id": tenant_id,
            "member_ref": member_ref,
            "member_refs": json.dumps([member_ref]),
            "start_date": start_date,
            "end_date": end_date,
        }
        await self._execute(
            """
            UPDATE legal_daily_review_suggestions
            SET active = FALSE, updated_at = NOW()
            WHERE tenant_id = :tenant_id
              AND user_id = CAST(:member_ref AS uuid)
              AND report_date BETWEEN :start_date AND :end_date
              AND active IS TRUE
            """,
            shared_parameters,
        )
        await self._execute(
            """
            UPDATE legal_daily_work_items
            SET active = FALSE, updated_at = NOW()
            WHERE tenant_id = :tenant_id
              AND member_refs_json @> CAST(:member_refs AS jsonb)
              AND first_seen <= :end_date
              AND last_seen >= :start_date
              AND active IS TRUE
            """,
            shared_parameters,
        )
        for suggestion in suggestions:
            await self._execute(
                """
                INSERT INTO legal_daily_review_suggestions (
                    suggestion_id,
                    public_ref,
                    tenant_id,
                    report_id,
                    user_id,
                    team_id,
                    report_date,
                    reason_type,
                    reason,
                    evidence_json,
                    compared_dates_json,
                    confidence,
                    work_item_title,
                    support_needed,
                    owner_level,
                    model_version,
                    active
                )
                VALUES (
                    CAST(:suggestion_id AS uuid),
                    :public_ref,
                    :tenant_id,
                    NULL,
                    CAST(:member_ref AS uuid),
                    CAST(:team_ref AS uuid),
                    :report_date,
                    :reason_type,
                    :reason,
                    CAST(:evidence AS jsonb),
                    CAST(:compared_dates AS jsonb),
                    :confidence,
                    :work_item_title,
                    :support_needed,
                    :owner_level,
                    :model_version,
                    TRUE
                )
                ON CONFLICT (tenant_id, public_ref) DO UPDATE SET
                    active = TRUE,
                    updated_at = NOW()
                """,
                {
                    "suggestion_id": str(uuid4()),
                    "public_ref": suggestion.ref,
                    "tenant_id": tenant_id,
                    "member_ref": suggestion.member_ref,
                    "team_ref": suggestion.team_ref,
                    "report_date": suggestion.report_date,
                    "reason_type": suggestion.reason_type,
                    "reason": suggestion.reason,
                    "evidence": json.dumps(
                        [
                            {
                                "date": evidence.evidence_date.isoformat(),
                                "section": evidence.section,
                                "quote": evidence.quote,
                            }
                            for evidence in suggestion.evidence
                        ],
                        ensure_ascii=False,
                    ),
                    "compared_dates": json.dumps(
                        [value.isoformat() for value in suggestion.compared_dates]
                    ),
                    "confidence": suggestion.confidence,
                    "work_item_title": suggestion.work_item_title,
                    "support_needed": suggestion.support_needed,
                    "owner_level": suggestion.owner_level,
                    "model_version": suggestion.model_version,
                },
            )
        for item in work_items:
            await self._execute(
                """
                INSERT INTO legal_daily_work_items (
                    item_id,
                    public_ref,
                    tenant_id,
                    team_id,
                    member_refs_json,
                    title,
                    item_status,
                    summary,
                    first_seen,
                    last_seen,
                    entries_json,
                    confidence,
                    model_version,
                    active
                )
                VALUES (
                    CAST(:item_id AS uuid),
                    :public_ref,
                    :tenant_id,
                    CAST(:team_ref AS uuid),
                    CAST(:member_refs AS jsonb),
                    :title,
                    :item_status,
                    :summary,
                    :first_seen,
                    :last_seen,
                    CAST(:entries AS jsonb),
                    :confidence,
                    :model_version,
                    TRUE
                )
                ON CONFLICT (tenant_id, public_ref) DO UPDATE SET
                    active = TRUE,
                    updated_at = NOW()
                """,
                {
                    "item_id": str(uuid4()),
                    "public_ref": item.ref,
                    "tenant_id": tenant_id,
                    "team_ref": item.team_ref,
                    "member_refs": json.dumps(list(item.member_refs)),
                    "title": item.title,
                    "item_status": item.status,
                    "summary": item.summary,
                    "first_seen": item.first_seen,
                    "last_seen": item.last_seen,
                    "entries": json.dumps(
                        [
                            {
                                "date": entry.entry_date.isoformat(),
                                "member_ref": entry.member_ref,
                                "section": entry.section,
                                "quote": entry.quote,
                                "object": entry.object_text,
                                "action": entry.action,
                                "result": entry.result,
                                "next_step": entry.next_step,
                                "blocker": entry.blocker,
                            }
                            for entry in item.entries
                        ],
                        ensure_ascii=False,
                    ),
                    "confidence": item.confidence,
                    "model_version": item.model_version,
                },
            )
        await self._session.commit()

    async def _execute(
        self,
        query: str,
        parameters: dict[str, object],
    ) -> None:
        statement = text(query)
        parameter_names = statement.compile().params
        bound_parameters = {
            key: value for key, value in parameters.items() if key in parameter_names
        }
        await self._session.execute(statement.bindparams(**bound_parameters))

    async def _rows(
        self,
        query: str,
        parameters: dict[str, object],
    ) -> list[dict[str, Any]]:
        statement = text(query)
        parameter_names = statement.compile().params
        bound_parameters = {
            key: value for key, value in parameters.items() if key in parameter_names
        }
        result = await self._session.execute(statement.bindparams(**bound_parameters))
        return list(result.mappings().all())


def _report_record(row: dict[str, Any]) -> DailyReportRecord:
    fragments = tuple(
        item
        for item in _json_list(row.get("input_fragments"))
        if isinstance(item, dict)
    )
    section_status = row.get("section_status")
    return DailyReportRecord(
        ref=str(row["report_ref"]),
        member_ref=str(row["member_ref"]),
        team_ref=str(row["team_ref"]),
        report_date=_as_date(row["report_date"]),
        status=str(row["status"]),
        confirmation_type=str(row["confirmation_type"]),
        confirmed_by_user=bool(row["confirmed_by_user"]),
        today_work=tuple(str(item) for item in _json_list(row.get("today_work"))),
        problems=tuple(str(item) for item in _json_list(row.get("problems"))),
        tomorrow_plan=tuple(str(item) for item in _json_list(row.get("tomorrow_plan"))),
        submitted_at=row.get("submitted_at"),  # type: ignore[arg-type]
        raw_input=str(row.get("raw_input") or ""),
        input_fragments=fragments,
        section_status=(
            dict(section_status) if isinstance(section_status, dict) else {}
        ),
    )


def _suggestion_record(row: dict[str, Any]) -> ReviewSuggestionRecord:
    evidence = tuple(
        EvidenceRecord(
            evidence_date=_as_date(item.get("date")),
            section=str(item.get("section") or ""),
            quote=str(item.get("quote") or ""),
        )
        for item in _json_list(row.get("evidence"))
        if isinstance(item, dict)
    )
    return ReviewSuggestionRecord(
        ref=str(row["suggestion_ref"]),
        member_ref=str(row["member_ref"]),
        team_ref=str(row["team_ref"]),
        report_date=_as_date(row["report_date"]),
        reason_type=str(row["reason_type"]),
        reason=str(row["reason"]),
        evidence=evidence,
        compared_dates=tuple(
            _as_date(item) for item in _json_list(row.get("compared_dates"))
        ),
        confidence=_as_float(row["confidence"]),
        work_item_title=str(row.get("work_item_title") or ""),
        support_needed=str(row.get("support_needed") or ""),
        owner_level=str(row["owner_level"]),  # type: ignore[arg-type]
        model_version=str(row["model_version"]),
        active=bool(row["active"]),
        evaluated_at=row.get("evaluated_at"),  # type: ignore[arg-type]
    )


def _work_item_record(row: dict[str, Any]) -> WorkItemRecord:
    entries = tuple(
        WorkItemEntryRecord(
            entry_date=_as_date(item.get("date")),
            member_ref=str(item.get("member_ref") or ""),
            section=str(item.get("section") or ""),
            quote=str(item.get("quote") or ""),
            object_text=str(item.get("object") or ""),
            action=str(item.get("action") or ""),
            result=str(item.get("result") or ""),
            next_step=str(item.get("next_step") or ""),
            blocker=str(item.get("blocker") or ""),
        )
        for item in _json_list(row.get("entries"))
        if isinstance(item, dict)
    )
    return WorkItemRecord(
        ref=str(row["item_ref"]),
        team_ref=str(row["team_ref"]),
        member_refs=tuple(str(item) for item in _json_list(row.get("member_refs"))),
        title=str(row["title"]),
        status=str(row["item_status"]),  # type: ignore[arg-type]
        summary=str(row.get("summary") or ""),
        first_seen=_as_date(row["first_seen"]),
        last_seen=_as_date(row["last_seen"]),
        entries=entries,
        confidence=_as_float(row["confidence"]),
        model_version=str(row["model_version"]),
        active=bool(row["active"]),
        evaluated_at=row.get("evaluated_at"),  # type: ignore[arg-type]
    )


def _decision_record(row: dict[str, Any]) -> ManagerDecisionRecord:
    evidence_snapshot = row.get("evidence_snapshot")
    return ManagerDecisionRecord(
        ref=str(row["decision_ref"]),
        tenant_id=str(row["tenant_id"]),
        target_type=str(row["target_type"]),  # type: ignore[arg-type]
        target_ref=str(row["target_ref"]),
        team_ref=str(row["team_ref"]),
        decision=str(row["decision"]),  # type: ignore[arg-type]
        note=str(row.get("note") or ""),
        actor_user_id=str(row["actor_user_id"]),
        actor_role=str(row["actor_role"]),  # type: ignore[arg-type]
        evidence_snapshot=(
            dict(evidence_snapshot) if isinstance(evidence_snapshot, dict) else {}
        ),
        idempotency_key=str(row["idempotency_key"]),
        created_at=row["created_at"],
        actor_name=str(row.get("actor_name") or ""),
    )


def _json_list(value: object) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _as_date(value: object) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _as_float(value: object) -> float:
    if isinstance(value, Decimal):
        return float(value)
    return float(value)  # type: ignore[arg-type]
