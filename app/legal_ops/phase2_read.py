from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import and_, false, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent2.business.models import (
    Agent2Case,
    Agent2IdentityBinding,
    BusinessAuditEvent,
    BusinessCommandReceipt,
    CaseReportProjection,
    CaseLifecycleState,
    CaseProgress,
    CaseFollowupPending,
    CaseFollowupPolicy,
    CaseFollowupTask,
    NotificationOutbox,
    PartyAlias,
    PartyCaseClue,
    PartyCaseRole,
    PartyEntity,
    PartyConflict,
    PartyIdentifier,
    PartyMergeCandidate,
    PartyRelation,
    PartySourceReference,
    TenantRouteControl,
    TravelCollaborationCandidate,
    TravelIntent,
    PeriodicReport,
    PeriodicReportCommandReceipt,
)
from app.models import Agent2DailyCommandReceipt


async def load_phase2_read_model(
    session: AsyncSession,
    *,
    tenant_id: str,
    limit: int = 100,
    allowed_case_ids: tuple[str, ...] | None = None,
    principal_user_id: str = "",
) -> dict[str, Any]:
    capped = max(1, min(500, int(limit)))
    scoped = allowed_case_ids is not None
    case_scope = _case_uuid_scope(allowed_case_ids or ())
    party_scope = select(PartyCaseRole.party_id).where(
        PartyCaseRole.tenant_id == tenant_id,
        PartyCaseRole.case_id.in_(case_scope),
    )
    parties = await _rows(
        session, PartyEntity, tenant_id, capped, PartyEntity.updated_at.desc(),
        criteria=(PartyEntity.party_id.in_(party_scope),) if scoped else (),
    )
    identities = await _rows(
        session, Agent2IdentityBinding, tenant_id, capped, Agent2IdentityBinding.updated_at.desc(),
        criteria=(Agent2IdentityBinding.user_id == principal_user_id,) if scoped else (),
    )
    party_aliases = await _rows(
        session, PartyAlias, tenant_id, capped, PartyAlias.updated_at.desc(),
        criteria=(PartyAlias.party_id.in_(party_scope),) if scoped else (),
    )
    party_identifiers = await _rows(
        session, PartyIdentifier, tenant_id, capped, PartyIdentifier.updated_at.desc(),
        criteria=(PartyIdentifier.party_id.in_(party_scope),) if scoped else (),
    )
    party_roles = await _rows(
        session, PartyCaseRole, tenant_id, capped, PartyCaseRole.updated_at.desc(),
        criteria=(PartyCaseRole.case_id.in_(case_scope),) if scoped else (),
    )
    party_relations = await _rows(
        session, PartyRelation, tenant_id, capped, PartyRelation.updated_at.desc(),
        criteria=(PartyRelation.case_id.in_(case_scope),) if scoped else (),
    )
    party_clues = await _rows(
        session, PartyCaseClue, tenant_id, capped, PartyCaseClue.updated_at.desc(),
        criteria=(PartyCaseClue.case_id.in_(case_scope),) if scoped else (),
    )
    party_merge_candidates = await _rows(
        session, PartyMergeCandidate, tenant_id, capped, PartyMergeCandidate.updated_at.desc(),
        criteria=(
            and_(
                PartyMergeCandidate.left_party_id.in_(party_scope),
                PartyMergeCandidate.right_party_id.in_(party_scope),
            ),
        ) if scoped else (),
    )
    party_conflicts = await _rows(
        session, PartyConflict, tenant_id, capped, PartyConflict.updated_at.desc(),
        criteria=(PartyConflict.party_id.in_(party_scope),) if scoped else (),
    )
    party_sources = await _rows(
        session, PartySourceReference, tenant_id, capped, PartySourceReference.updated_at.desc(),
        criteria=(PartySourceReference.party_id.in_(party_scope),) if scoped else (),
    )
    case_statement = select(Agent2Case).where(Agent2Case.tenant_id == tenant_id)
    if allowed_case_ids is not None:
        case_statement = case_statement.where(
            Agent2Case.case_id.in_(_case_uuid_scope(allowed_case_ids))
        )
    cases = [
        item
        for item in list(
            (
                await session.scalars(
                    case_statement.order_by(Agent2Case.updated_at.desc()).limit(capped)
                )
            ).all()
        )
        if _case_is_visible(item)
    ]
    lifecycle_states = await _rows(
        session,
        CaseLifecycleState,
        tenant_id,
        capped,
        CaseLifecycleState.updated_at.desc(),
        criteria=(CaseLifecycleState.case_id.in_(case_scope),) if scoped else (),
    )
    followup_policies = await _rows(
        session,
        CaseFollowupPolicy,
        tenant_id,
        capped,
        CaseFollowupPolicy.updated_at.desc(),
        criteria=(CaseFollowupPolicy.case_id.in_(case_scope),) if scoped else (),
    )
    travel = await _rows(
        session, TravelIntent, tenant_id, capped, TravelIntent.updated_at.desc(),
        criteria=(TravelIntent.user_id == principal_user_id,) if scoped else (),
    )
    candidates = await _rows(
        session,
        TravelCollaborationCandidate,
        tenant_id,
        capped,
        TravelCollaborationCandidate.updated_at.desc(),
        criteria=(
            TravelCollaborationCandidate.participant_ids.contains([principal_user_id]),
        ) if scoped else (),
    )
    notifications = await _rows(
        session,
        NotificationOutbox,
        tenant_id,
        capped,
        NotificationOutbox.updated_at.desc(),
        criteria=(NotificationOutbox.recipient_user_id == principal_user_id,) if scoped else (),
    )
    progress = await _rows(
        session, CaseProgress, tenant_id, capped, CaseProgress.updated_at.desc(),
        criteria=(CaseProgress.case_id.in_(case_scope),) if scoped else (),
    )
    business_receipts = await _rows(
        session,
        BusinessCommandReceipt,
        tenant_id,
        capped,
        BusinessCommandReceipt.created_at.desc(),
        criteria=(BusinessCommandReceipt.actor_user_id == principal_user_id,) if scoped else (),
    )
    daily_user_uuid = _optional_uuid(principal_user_id)
    daily_receipts = await _rows(
        session,
        Agent2DailyCommandReceipt,
        tenant_id,
        capped,
        Agent2DailyCommandReceipt.created_at.desc(),
        criteria=(
            Agent2DailyCommandReceipt.user_id == daily_user_uuid
            if daily_user_uuid is not None
            else false(),
        ) if scoped else (),
    )
    periodic_reports = await _rows(
        session,
        PeriodicReport,
        tenant_id,
        capped,
        PeriodicReport.updated_at.desc(),
        criteria=(PeriodicReport.owner_user_id == principal_user_id,) if scoped else (),
    )
    periodic_receipts = await _rows(
        session,
        PeriodicReportCommandReceipt,
        tenant_id,
        capped,
        PeriodicReportCommandReceipt.created_at.desc(),
        criteria=(PeriodicReportCommandReceipt.actor_user_id == principal_user_id,) if scoped else (),
    )
    receipts = sorted(
        [*business_receipts, *daily_receipts, *periodic_receipts],
        key=lambda item: item.created_at.timestamp() if item.created_at is not None else 0.0,
        reverse=True,
    )[:capped]
    business_audits = await _rows(
        session,
        BusinessAuditEvent,
        tenant_id,
        capped,
        BusinessAuditEvent.created_at.desc(),
        criteria=(BusinessAuditEvent.actor_user_id == principal_user_id,) if scoped else (),
    )
    audit_rows = [
        *(_model_json(item) for item in business_audits),
        *(_daily_receipt_audit_json(item) for item in daily_receipts),
        *(_periodic_receipt_audit_json(item) for item in periodic_receipts),
    ]
    route = await session.scalar(
        select(TenantRouteControl).where(TenantRouteControl.tenant_id == tenant_id)
    )
    return {
        "tenant_id": tenant_id,
        "route_control": _model_json(route) if route is not None else None,
        "summary": {
            "parties": len(parties),
            "identity_bindings": len(identities),
            "party_aliases": len(party_aliases),
            "party_identifiers": len(party_identifiers),
            "party_case_roles": len(party_roles),
            "party_relations": len(party_relations),
            "party_case_clues": len(party_clues),
            "party_merge_candidates": len(party_merge_candidates),
            "party_conflicts": len(party_conflicts),
            "party_source_references": len(party_sources),
            "cases": len(cases),
            "case_lifecycle_states": len(lifecycle_states),
            "case_followup_policies": len(followup_policies),
            "travel_intents": len(travel),
            "collaboration_candidates": len(candidates),
            "notifications": len(notifications),
            "case_progress": len(progress),
            "periodic_reports": len(periodic_reports),
            "receipts": len(receipts),
            "audits": len(audit_rows),
            "failures": sum(item.status in {"blocked", "failed"} for item in receipts)
            + sum(item.status in {"failed", "dead_letter"} for item in notifications),
        },
        "parties": [_model_json(item) for item in parties],
        "identity_bindings": [_model_json(item) for item in identities],
        "party_aliases": [_model_json(item) for item in party_aliases],
        "party_identifiers": [_model_json(item) for item in party_identifiers],
        "party_case_roles": [_model_json(item) for item in party_roles],
        "party_relations": [_model_json(item) for item in party_relations],
        "party_case_clues": [_model_json(item) for item in party_clues],
        "party_merge_candidates": [_model_json(item) for item in party_merge_candidates],
        "party_conflicts": [_model_json(item) for item in party_conflicts],
        "party_source_references": [_model_json(item) for item in party_sources],
        "cases": [_model_json(item) for item in cases],
        "case_lifecycle_states": [_model_json(item) for item in lifecycle_states],
        "case_followup_policies": [_model_json(item) for item in followup_policies],
        "travel_intents": [_model_json(item) for item in travel],
        "collaboration_candidates": [_model_json(item) for item in candidates],
        "notifications": [_model_json(item) for item in notifications],
        "case_progress": [_model_json(item) for item in progress],
        "periodic_reports": [_model_json(item) for item in periodic_reports],
        "receipts": [_receipt_json(item) for item in receipts],
        "audits": audit_rows,
    }


def _case_is_visible(case: Any) -> bool:
    source = getattr(case, "source_json", None)
    return not (isinstance(source, dict) and source.get("display_hidden") is True)


def _case_uuid_scope(values: tuple[str, ...]) -> tuple[UUID, ...]:
    parsed: list[UUID] = []
    for value in values:
        try:
            parsed.append(UUID(str(value)))
        except (TypeError, ValueError):
            continue
    return tuple(dict.fromkeys(parsed))


def _optional_uuid(value: str) -> UUID | None:
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


async def load_phase2_case_detail(
    session: AsyncSession,
    *,
    tenant_id: str,
    case_id: str,
    allowed_case_ids: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    try:
        case_uuid = UUID(case_id)
    except (TypeError, ValueError) as exc:
        raise LookupError("case not found") from exc
    if allowed_case_ids is not None and case_uuid not in _case_uuid_scope(allowed_case_ids):
        raise LookupError("case not found")
    case = await session.scalar(
        select(Agent2Case).where(
            Agent2Case.tenant_id == tenant_id,
            Agent2Case.case_id == case_uuid,
        )
    )
    if case is None:
        raise LookupError("case not found")
    progress = list(
        (
            await session.scalars(
                select(CaseProgress)
                .where(
                    CaseProgress.tenant_id == tenant_id,
                    CaseProgress.case_id == case_uuid,
                )
                .order_by(CaseProgress.occurred_at, CaseProgress.created_at)
            )
        ).all()
    )
    party_rows = (
        await session.execute(
            select(PartyCaseRole, PartyEntity)
            .join(
                PartyEntity,
                (PartyEntity.tenant_id == PartyCaseRole.tenant_id)
                & (PartyEntity.party_id == PartyCaseRole.party_id),
            )
            .where(
                PartyCaseRole.tenant_id == tenant_id,
                PartyCaseRole.case_id == case_uuid,
                PartyCaseRole.confirmation_status == "confirmed",
            )
        )
    ).all()
    clues = list(
        (
            await session.scalars(
                select(PartyCaseClue)
                .where(
                    PartyCaseClue.tenant_id == tenant_id,
                    PartyCaseClue.case_id == case_uuid,
                )
                .order_by(PartyCaseClue.clue_type, PartyCaseClue.occurred_at.desc().nullslast())
            )
        ).all()
    )
    progress_ids = [str(item.progress_id) for item in progress]
    audits = []
    if progress_ids:
        audits = list(
            (
                await session.scalars(
                    select(BusinessAuditEvent)
                    .where(
                        BusinessAuditEvent.tenant_id == tenant_id,
                        BusinessAuditEvent.resource_type == "case_progress",
                        BusinessAuditEvent.resource_id.in_(progress_ids),
                    )
                    .order_by(BusinessAuditEvent.created_at)
                )
            ).all()
        )
    report_projections = list(
        (
            await session.scalars(
                select(CaseReportProjection)
                .where(
                    CaseReportProjection.tenant_id == tenant_id,
                    CaseReportProjection.case_id == case_uuid,
                )
                .order_by(CaseReportProjection.created_at.desc())
            )
        ).all()
    )
    return {
        "tenant_id": tenant_id,
        "case": _model_json(case),
        "parties": [
            {
                "party": _model_json(party),
                "role": _model_json(role),
            }
            for role, party in party_rows
        ],
        "business_clues": [_model_json(item) for item in clues],
        "lifecycle": {
            "internal_progress_nodes": [
                {
                    "node_id": f"case-progress:{item.progress_id}",
                    "kind": "internal_progress",
                    "progress_id": str(item.progress_id),
                    "occurred_at": item.occurred_at.isoformat(),
                    "recorded_at": item.recorded_at.isoformat(),
                    "title": item.summary,
                    "details": item.details,
                    "progress_type": item.progress_type,
                    "content_origin": item.content_origin,
                    "confirmation_status": item.confirmation_status,
                    "source_message_id": item.source_message_id,
                    "source_channel": item.source_channel,
                    "reporter_id": item.reporter_id,
                    "version": item.version,
                    "deleted": item.deleted_at is not None,
                    "deleted_at": item.deleted_at.isoformat() if item.deleted_at else None,
                }
                for item in progress
            ]
        },
        "audits": [_model_json(item) for item in audits],
        "report_projections": [_model_json(item) for item in report_projections],
    }


async def load_case_followup_configuration(
    session: AsyncSession,
    *,
    tenant_id: str,
    case_id: str,
    allowed_case_ids: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    try:
        case_uuid = UUID(case_id)
    except (TypeError, ValueError) as exc:
        raise LookupError("case not found") from exc
    if allowed_case_ids is not None and case_uuid not in _case_uuid_scope(allowed_case_ids):
        raise LookupError("case not found")
    case = await session.scalar(
        select(Agent2Case).where(
            Agent2Case.tenant_id == tenant_id,
            Agent2Case.case_id == case_uuid,
        )
    )
    if case is None or not _case_is_visible(case):
        raise LookupError("case not found")
    policy = await session.scalar(
        select(CaseFollowupPolicy).where(
            CaseFollowupPolicy.tenant_id == tenant_id,
            CaseFollowupPolicy.case_id == case_uuid,
            CaseFollowupPolicy.assigned_user_id == case.owner_user_id,
        )
    )
    lifecycle_state = await session.scalar(
        select(CaseLifecycleState).where(
            CaseLifecycleState.tenant_id == tenant_id,
            CaseLifecycleState.case_id == case_uuid,
            CaseLifecycleState.assigned_user_id == case.owner_user_id,
        )
    )
    tasks = list(
        (
            await session.scalars(
                select(CaseFollowupTask)
                .where(
                    CaseFollowupTask.tenant_id == tenant_id,
                    CaseFollowupTask.case_id == case_uuid,
                )
                .order_by(CaseFollowupTask.created_at.desc())
                .limit(100)
            )
        ).all()
    )
    pending = await session.scalar(
        select(CaseFollowupPending)
        .where(
            CaseFollowupPending.tenant_id == tenant_id,
            CaseFollowupPending.case_id == case_uuid,
            CaseFollowupPending.status.in_(("active", "awaiting_input")),
        )
        .order_by(CaseFollowupPending.created_at.desc())
    )
    latest = tasks[0] if tasks else None
    return {
        "tenant_id": tenant_id,
        "case": {
            "case_id": str(case.case_id),
            "case_name": case.case_name,
            "case_number": case.case_number,
            "case_type": case.case_type,
            "owner_user_id": case.owner_user_id,
            "version": case.version,
        },
        "policy": _model_json(policy) if policy is not None else None,
        "lifecycle_state": (
            _model_json(lifecycle_state) if lifecycle_state is not None else None
        ),
        "latest_status": {
            "last_followup_at": (
                policy.last_followup_at.isoformat()
                if policy is not None and policy.last_followup_at else None
            ),
            "next_due_at": (
                policy.next_due_at.isoformat()
                if policy is not None and policy.next_due_at else None
            ),
            "waiting_for_reply": pending is not None,
            "last_message_status": latest.message_status if latest is not None else None,
            "snoozed_until": (
                policy.snoozed_until.isoformat()
                if policy is not None and policy.snoozed_until else None
            ),
        },
        "pending": (
            {
                "pending_id": str(pending.pending_id),
                "status": pending.status,
                "expires_at": pending.expires_at.isoformat(),
                "version": pending.version,
            }
            if pending is not None else None
        ),
        "history": [
            {
                "followup_id": str(item.followup_id),
                "trigger_type": item.trigger_type,
                "trigger_sources": item.trigger_sources_json,
                "question_type": item.question_type,
                "question_text": item.question_text,
                "task_status": item.task_status,
                "message_status": item.message_status,
                "response_status": item.response_status,
                "version": item.version,
                "due_at": item.due_at.isoformat(),
                "expires_at": item.expires_at.isoformat(),
                "created_at": item.created_at.isoformat(),
            }
            for item in tasks
        ],
    }


async def search_phase2_parties(
    session: AsyncSession,
    *,
    tenant_id: str,
    query: str = "",
    limit: int = 50,
    allowed_case_ids: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    capped = max(1, min(100, int(limit)))
    statement = (
        select(PartyEntity)
        .outerjoin(
            PartyAlias,
            (PartyAlias.tenant_id == PartyEntity.tenant_id)
            & (PartyAlias.party_id == PartyEntity.party_id),
        )
        .outerjoin(
            PartyIdentifier,
            (PartyIdentifier.tenant_id == PartyEntity.tenant_id)
            & (PartyIdentifier.party_id == PartyEntity.party_id),
        )
        .where(PartyEntity.tenant_id == tenant_id)
    )
    if allowed_case_ids is not None:
        statement = statement.where(
            PartyEntity.party_id.in_(
                select(PartyCaseRole.party_id).where(
                    PartyCaseRole.tenant_id == tenant_id,
                    PartyCaseRole.case_id.in_(_case_uuid_scope(allowed_case_ids)),
                )
            )
        )
    needle = str(query or "").strip()
    if needle:
        pattern = f"%{needle}%"
        statement = statement.where(
            or_(
                PartyEntity.canonical_name.ilike(pattern),
                PartyEntity.short_name.ilike(pattern),
                PartyEntity.unified_social_credit_code.ilike(pattern),
                PartyAlias.alias.ilike(pattern),
                PartyIdentifier.identifier_value.ilike(pattern),
            )
        )
    parties = list(
        (
            await session.scalars(
                statement.distinct().order_by(PartyEntity.canonical_name).limit(capped)
            )
        ).all()
    )
    return [_model_json(item) for item in parties]


async def load_phase2_party_detail(
    session: AsyncSession,
    *,
    tenant_id: str,
    party_id: str,
    allowed_case_ids: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    try:
        party_uuid = UUID(party_id)
    except (TypeError, ValueError) as exc:
        raise LookupError("party not found") from exc
    party_statement = select(PartyEntity).where(
        PartyEntity.tenant_id == tenant_id,
        PartyEntity.party_id == party_uuid,
    )
    case_scope = _case_uuid_scope(allowed_case_ids or ())
    if allowed_case_ids is not None:
        party_statement = party_statement.where(
            PartyEntity.party_id.in_(
                select(PartyCaseRole.party_id).where(
                    PartyCaseRole.tenant_id == tenant_id,
                    PartyCaseRole.case_id.in_(case_scope),
                )
            )
        )
    party = await session.scalar(party_statement)
    if party is None:
        raise LookupError("party not found")
    aliases = list(
        (
            await session.scalars(
                select(PartyAlias).where(
                    PartyAlias.tenant_id == tenant_id,
                    PartyAlias.party_id == party_uuid,
                )
            )
        ).all()
    )
    identifiers = list(
        (
            await session.scalars(
                select(PartyIdentifier).where(
                    PartyIdentifier.tenant_id == tenant_id,
                    PartyIdentifier.party_id == party_uuid,
                )
            )
        ).all()
    )
    role_rows = (
        await session.execute(
            select(PartyCaseRole, Agent2Case)
            .join(
                Agent2Case,
                (Agent2Case.tenant_id == PartyCaseRole.tenant_id)
                & (Agent2Case.case_id == PartyCaseRole.case_id),
            )
            .where(
                PartyCaseRole.tenant_id == tenant_id,
                PartyCaseRole.party_id == party_uuid,
                *(
                    (PartyCaseRole.case_id.in_(case_scope),)
                    if allowed_case_ids is not None else ()
                ),
            )
            .order_by(PartyCaseRole.role_type, Agent2Case.case_number)
        )
    ).all()
    relations = list(
        (
            await session.scalars(
                select(PartyRelation).where(
                    PartyRelation.tenant_id == tenant_id,
                    *(
                        (PartyRelation.case_id.in_(case_scope),)
                        if allowed_case_ids is not None else ()
                    ),
                    or_(
                        PartyRelation.from_party_id == party_uuid,
                        PartyRelation.to_party_id == party_uuid,
                    ),
                )
            )
        ).all()
    )
    merges = list(
        (
            await session.scalars(
                select(PartyMergeCandidate).where(
                    PartyMergeCandidate.tenant_id == tenant_id,
                    or_(
                        PartyMergeCandidate.left_party_id == party_uuid,
                        PartyMergeCandidate.right_party_id == party_uuid,
                    ),
                )
            )
        ).all()
    )
    conflicts = list(
        (
            await session.scalars(
                select(PartyConflict).where(
                    PartyConflict.tenant_id == tenant_id,
                    PartyConflict.party_id == party_uuid,
                )
            )
        ).all()
    )
    sources = list(
        (
            await session.scalars(
                select(PartySourceReference).where(
                    PartySourceReference.tenant_id == tenant_id,
                    PartySourceReference.party_id == party_uuid,
                )
            )
        ).all()
    )
    return {
        "tenant_id": tenant_id,
        "party": _model_json(party),
        "aliases": [_model_json(item) for item in aliases],
        "identifiers": [_model_json(item) for item in identifiers],
        "case_roles": [
            {"role": _model_json(role), "case": _model_json(case)}
            for role, case in role_rows
        ],
        "relations": [_model_json(item) for item in relations],
        "merge_candidates": [_model_json(item) for item in merges],
        "conflicts": [_model_json(item) for item in conflicts],
        "sources": [_model_json(item) for item in sources],
    }


async def _rows(
    session: AsyncSession,
    model: type,
    tenant_id: str,
    limit: int,
    order_by: Any,
    *,
    criteria: tuple[Any, ...] = (),
) -> list[Any]:
    statement = select(model).where(model.tenant_id == tenant_id)
    if criteria:
        statement = statement.where(*criteria)
    return list(
        (
            await session.scalars(
                statement.order_by(order_by).limit(limit)
            )
        ).all()
    )


def _model_json(model: Any) -> dict[str, Any]:
    data = {
        column.name: _json_value(getattr(model, column.name))
        for column in model.__table__.columns
    }
    data["data_origin"] = _data_origin(data)
    return data


def _data_origin(data: dict[str, Any]) -> str:
    message_json = data.get("message_json")
    message_json = message_json if isinstance(message_json, dict) else {}
    after_json = data.get("after_json")
    after_json = after_json if isinstance(after_json, dict) else {}
    if (
        str(data.get("content_origin") or "") == "robot_followup"
        or str(data.get("message_type") or "") == "case_progress_followup"
        or "case_progress_followup" in str(data.get("resource_type") or "")
        or str(message_json.get("content_origin") or "") == "robot_followup"
        or str(after_json.get("content_origin") or "") == "robot_followup"
    ):
        return "robot_followup"
    source = " ".join(
        str(data.get(key) or "").lower()
        for key in ("source_type", "source_channel", "source_id", "source_message_id")
    )
    if "server_acceptance_smoke" in source or "phase2-" in source and "message" in source:
        return "server_acceptance_smoke"
    if "server_natural_language_greytest" in source or "server_authorized_user_simulation" in source:
        return "sandbox_persisted"
    if "fixture" in source:
        return "sandbox_fixture"
    if "dingtalk" in source:
        return "real_user_message"
    if "robot_followup" in source:
        return "robot_followup"
    if data.get("content_origin"):
        return str(data["content_origin"])
    return "system_generated"


def _receipt_json(model: Any) -> dict[str, Any]:
    data = _model_json(model)
    data.setdefault("source_message_id", data.get("message_id", ""))
    data.setdefault("failed_stage", data.get("reason_code", ""))
    data.setdefault("error_code", data.get("reason_code", ""))
    if isinstance(model, PeriodicReportCommandReceipt):
        data.setdefault("resource_type", "periodic_report")
        data.setdefault("resource_id", data.get("report_id", ""))
    return data


def _periodic_receipt_audit_json(model: PeriodicReportCommandReceipt) -> dict[str, Any]:
    return {
        "audit_id": str(model.receipt_id),
        "receipt_id": str(model.receipt_id),
        "actor_user_id": model.actor_user_id,
        "source_message_id": model.source_message_id,
        "source_channel": model.source_channel,
        "command_type": model.command_type,
        "resource_type": "periodic_report",
        "resource_id": str(model.report_id),
        "before_json": _json_value(model.before_json),
        "after_json": _json_value(model.after_json),
        "created_at": _json_value(model.created_at),
        "data_origin": _data_origin(
            {
                "source_message_id": model.source_message_id,
                "source_channel": model.source_channel,
            }
        ),
    }


def _daily_receipt_audit_json(model: Agent2DailyCommandReceipt) -> dict[str, Any]:
    audit = _json_value(dict(model.audit_json or {}))
    return {
        **audit,
        "audit_id": audit.get("interaction_id", ""),
        "receipt_id": str(model.receipt_id),
        "actor_user_id": str(model.user_id),
        "source_message_id": model.message_id,
        "command_type": model.command_type,
        "resource_type": model.resource_type,
        "resource_id": model.resource_id,
        "before_json": _json_value(model.before_json),
        "after_json": _json_value(model.after_json),
        "created_at": _json_value(model.created_at),
        "data_origin": _data_origin(
            {
                "source_message_id": model.message_id,
                "source_channel": str(audit.get("source") or "daily_report"),
                "content_origin": "daily_report",
            }
        ),
    }


def _json_value(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    return value
