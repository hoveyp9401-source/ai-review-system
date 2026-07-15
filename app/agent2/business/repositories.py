from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent2.business.case_progress import CaseRecord
from app.agent2.business.contracts import BusinessCommandContext
from app.agent2.business.models import (
    Agent2Case,
    CaseProgress,
    CaseFollowupPolicy,
    PartyAlias,
    PartyCaseRole,
    PartyEntity,
    PartyIdentifier,
)
from app.agent2.business.compiler import CaseFollowupPolicyRecord
from app.agent2.business.party import (
    PartyCandidate,
    PartyCaseRoleRecord,
    PartyResolution,
    normalize_identifier,
    normalize_party_name,
)


@dataclass(frozen=True)
class PartyQueryScope:
    tenant_id: str
    allowed_case_ids: tuple[str, ...]

    def case_uuids(self) -> tuple[UUID, ...]:
        values: list[UUID] = []
        for value in self.allowed_case_ids:
            try:
                values.append(UUID(value))
            except (TypeError, ValueError):
                continue
        return tuple(values)


class CaseFollowupPolicySqlRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_visible(
        self, context: BusinessCommandContext
    ) -> tuple[CaseFollowupPolicyRecord, ...]:
        case_ids = PartyQueryScope(
            context.tenant_id, context.allowed_case_ids
        ).case_uuids()
        if not case_ids:
            return ()
        rows = (
            await self.session.scalars(
                select(CaseFollowupPolicy).where(
                    CaseFollowupPolicy.tenant_id == context.tenant_id,
                    CaseFollowupPolicy.assigned_user_id == context.actor_user_id,
                    CaseFollowupPolicy.case_id.in_(case_ids),
                )
            )
        ).all()
        return tuple(
            CaseFollowupPolicyRecord(
                str(item.case_id), item.assigned_user_id, item.version
            )
            for item in rows
        )


class PartySqlRepository:
    """PostgreSQL-backed party resolver with fail-closed case visibility.

    Exact, confirmed identifiers and names may resolve an entity. Trigram
    similarity is deliberately returned as an unconfirmed candidate only.
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def resolve(self, query: str, *, scope: PartyQueryScope) -> PartyResolution:
        normalized_name = normalize_party_name(query)
        normalized_id = normalize_identifier(query)
        case_ids = scope.case_uuids()
        if not normalized_name or not case_ids:
            return PartyResolution("not_found")

        identifiers = (
            await self.session.scalars(
                self._identifier_statement(scope.tenant_id, case_ids, normalized_id)
            )
        ).all()
        identifier_ids = tuple(dict.fromkeys(str(value) for value in identifiers))
        if len(identifier_ids) == 1:
            return PartyResolution("resolved", identifier_ids[0], "exact_identifier")
        if len(identifier_ids) > 1:
            return PartyResolution(
                "needs_clarification",
                candidates=tuple(
                    PartyCandidate(
                        party_id=value,
                        canonical_name="",
                        score=1.0,
                        match_basis="exact_identifier_conflict",
                        confirmed=False,
                    )
                    for value in identifier_ids
                ),
            )

        canonical = (
            await self.session.scalars(
                self._canonical_statement(scope.tenant_id, case_ids, normalized_name)
            )
        ).all()
        canonical_ids = tuple(dict.fromkeys(str(value) for value in canonical))
        if len(canonical_ids) == 1:
            return PartyResolution("resolved", canonical_ids[0], "exact_canonical_name")
        if len(canonical_ids) > 1:
            return PartyResolution(
                "needs_clarification",
                candidates=tuple(
                    PartyCandidate(
                        party_id=value,
                        canonical_name=query,
                        score=1.0,
                        match_basis="exact_canonical_name_conflict",
                        confirmed=False,
                    )
                    for value in canonical_ids
                ),
            )

        aliases = (
            await self.session.scalars(
                self._alias_statement(scope.tenant_id, case_ids, normalized_name)
            )
        ).all()
        alias_ids = tuple(dict.fromkeys(str(value) for value in aliases))
        if len(alias_ids) == 1:
            return PartyResolution("resolved", alias_ids[0], "confirmed_alias")
        if len(alias_ids) > 1:
            return PartyResolution(
                "needs_clarification",
                candidates=tuple(
                    PartyCandidate(
                        party_id=value,
                        canonical_name="",
                        score=1.0,
                        match_basis="confirmed_alias_conflict",
                        confirmed=False,
                    )
                    for value in alias_ids
                ),
            )

        rows = (await self.session.execute(self._fuzzy_statement(scope.tenant_id, case_ids, normalized_name))).all()
        candidates = tuple(
            PartyCandidate(
                party_id=str(row.party_id),
                canonical_name=row.canonical_name,
                score=round(float(row.score), 4),
                match_basis="pg_trgm_candidate",
                confirmed=False,
            )
            for row in rows
        )
        if candidates:
            return PartyResolution("needs_clarification", candidates=candidates)
        return PartyResolution("not_found")

    async def party_cases(
        self, party_id: str, *, scope: PartyQueryScope
    ) -> tuple[PartyCaseRoleRecord, ...]:
        try:
            party_uuid = UUID(party_id)
        except (TypeError, ValueError):
            return ()
        case_ids = scope.case_uuids()
        if not case_ids:
            return ()
        rows = (
            await self.session.scalars(
                select(PartyCaseRole).where(
                    PartyCaseRole.tenant_id == scope.tenant_id,
                    PartyCaseRole.party_id == party_uuid,
                    PartyCaseRole.case_id.in_(case_ids),
                    PartyCaseRole.confirmation_status == "confirmed",
                )
            )
        ).all()
        return tuple(
            PartyCaseRoleRecord(
                role_id=str(item.role_id),
                tenant_id=item.tenant_id,
                party_id=str(item.party_id),
                case_id=str(item.case_id),
                role_type=item.role_type,
                effective_from=item.effective_from.isoformat() if item.effective_from else "",
                effective_to=item.effective_to.isoformat() if item.effective_to else "",
                source_reference=str(item.source_reference),
                confirmation_status=item.confirmation_status,
            )
            for item in rows
        )

    @staticmethod
    def _identifier_statement(
        tenant_id: str, case_ids: Sequence[UUID], normalized_id: str
    ) -> Select[tuple[UUID]]:
        return (
            select(PartyIdentifier.party_id)
            .join(
                PartyCaseRole,
                (PartyCaseRole.tenant_id == PartyIdentifier.tenant_id)
                & (PartyCaseRole.party_id == PartyIdentifier.party_id),
            )
            .where(
                PartyIdentifier.tenant_id == tenant_id,
                PartyIdentifier.normalized_value == normalized_id,
                PartyIdentifier.confirmation_status == "confirmed",
                PartyCaseRole.case_id.in_(case_ids),
                PartyCaseRole.confirmation_status == "confirmed",
            )
            .limit(2)
        )

    @staticmethod
    def _canonical_statement(
        tenant_id: str, case_ids: Sequence[UUID], normalized_name: str
    ) -> Select[tuple[UUID]]:
        return (
            select(PartyEntity.party_id)
            .join(
                PartyCaseRole,
                (PartyCaseRole.tenant_id == PartyEntity.tenant_id)
                & (PartyCaseRole.party_id == PartyEntity.party_id),
            )
            .where(
                PartyEntity.tenant_id == tenant_id,
                PartyEntity.normalized_name == normalized_name,
                PartyCaseRole.case_id.in_(case_ids),
                PartyCaseRole.confirmation_status == "confirmed",
            )
            .distinct()
            .limit(2)
        )

    @staticmethod
    def _alias_statement(
        tenant_id: str, case_ids: Sequence[UUID], normalized_name: str
    ) -> Select[tuple[UUID]]:
        return (
            select(PartyAlias.party_id)
            .join(
                PartyCaseRole,
                (PartyCaseRole.tenant_id == PartyAlias.tenant_id)
                & (PartyCaseRole.party_id == PartyAlias.party_id),
            )
            .where(
                PartyAlias.tenant_id == tenant_id,
                PartyAlias.normalized_alias == normalized_name,
                PartyAlias.confirmation_status == "confirmed",
                PartyCaseRole.case_id.in_(case_ids),
                PartyCaseRole.confirmation_status == "confirmed",
            )
            .distinct()
            .limit(2)
        )

    @staticmethod
    def _fuzzy_statement(
        tenant_id: str, case_ids: Sequence[UUID], normalized_name: str
    ) -> Select:
        canonical_score = func.similarity(PartyEntity.normalized_name, normalized_name)
        alias_score = func.coalesce(func.max(func.similarity(PartyAlias.normalized_alias, normalized_name)), 0)
        score = func.greatest(canonical_score, alias_score).label("score")
        return (
            select(PartyEntity.party_id, PartyEntity.canonical_name, score)
            .join(
                PartyCaseRole,
                (PartyCaseRole.tenant_id == PartyEntity.tenant_id)
                & (PartyCaseRole.party_id == PartyEntity.party_id),
            )
            .outerjoin(
                PartyAlias,
                (PartyAlias.tenant_id == PartyEntity.tenant_id)
                & (PartyAlias.party_id == PartyEntity.party_id)
                & (PartyAlias.confirmation_status == "confirmed"),
            )
            .where(
                PartyEntity.tenant_id == tenant_id,
                PartyCaseRole.case_id.in_(case_ids),
                PartyCaseRole.confirmation_status == "confirmed",
            )
            .group_by(PartyEntity.party_id, PartyEntity.canonical_name, PartyEntity.normalized_name)
            .having(func.greatest(canonical_score, alias_score) >= Decimal("0.62"))
            .order_by(score.desc(), PartyEntity.party_id)
            .limit(5)
        )


class CaseSqlRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_visible(self, context: BusinessCommandContext) -> tuple[CaseRecord, ...]:
        case_ids: list[UUID] = []
        for value in context.allowed_case_ids:
            try:
                case_ids.append(UUID(value))
            except (TypeError, ValueError):
                continue
        if not case_ids:
            return ()
        rows = (
            await self.session.execute(
                select(Agent2Case, PartyEntity.canonical_name)
                .outerjoin(
                    PartyCaseRole,
                    (PartyCaseRole.tenant_id == Agent2Case.tenant_id)
                    & (PartyCaseRole.case_id == Agent2Case.case_id)
                    & (PartyCaseRole.confirmation_status == "confirmed"),
                )
                .outerjoin(
                    PartyEntity,
                    (PartyEntity.tenant_id == PartyCaseRole.tenant_id)
                    & (PartyEntity.party_id == PartyCaseRole.party_id),
                )
                .where(
                    Agent2Case.tenant_id == context.tenant_id,
                    Agent2Case.case_id.in_(case_ids),
                )
                .order_by(Agent2Case.case_id, PartyEntity.canonical_name)
            )
        ).all()
        records: dict[str, dict] = {}
        for case, party_name in rows:
            key = str(case.case_id)
            record = records.setdefault(
                key,
                {
                    "case_id": key,
                    "tenant_id": case.tenant_id,
                    "external_case_id": case.external_case_id,
                    "case_number": case.case_number,
                    "case_name": case.case_name,
                    "party_names": [],
                    "confirmed_aliases": _confirmed_case_aliases(case.source_json),
                    "version": case.version,
                    "owner_user_id": case.owner_user_id,
                },
            )
            if party_name and party_name not in record["party_names"]:
                record["party_names"].append(party_name)
        return tuple(
            CaseRecord(
                case_id=value["case_id"],
                tenant_id=value["tenant_id"],
                case_number=value["case_number"],
                case_name=value["case_name"],
                party_names=tuple(value["party_names"]),
                external_case_id=value["external_case_id"],
                confirmed_aliases=value["confirmed_aliases"],
                version=value["version"],
                owner_user_id=value["owner_user_id"],
            )
            for value in records.values()
        )


def _confirmed_case_aliases(source_json: object) -> tuple[str, ...]:
    if not isinstance(source_json, dict):
        return ()
    raw = source_json.get("confirmed_aliases", ())
    if isinstance(raw, str):
        raw = (raw,)
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(dict.fromkeys(str(value).strip() for value in raw if str(value).strip()))


@dataclass(frozen=True)
class CaseProgressTargetResolution:
    status: str
    progress_id: str = ""
    case_id: str = ""
    version: int = 0
    candidate_progress_ids: tuple[str, ...] = ()


class CaseProgressSqlRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def resolve_write_target(
        self,
        context: BusinessCommandContext,
        *,
        progress_id: str = "",
        case_id: str = "",
        recent_minutes: int = 30,
    ) -> CaseProgressTargetResolution:
        allowed_case_ids = tuple(
            UUID(value)
            for value in context.allowed_case_ids
            if _try_uuid(value) is not None
        )
        if not allowed_case_ids:
            return CaseProgressTargetResolution("not_found")
        statement = select(CaseProgress).where(
            CaseProgress.tenant_id == context.tenant_id,
            CaseProgress.case_id.in_(allowed_case_ids),
            CaseProgress.deleted_at.is_(None),
        )
        if "case_progress_admin" not in context.actor_role_ids:
            statement = statement.where(CaseProgress.reporter_id == context.actor_user_id)
        if progress_id:
            parsed_progress = _try_uuid(progress_id)
            if parsed_progress is None:
                return CaseProgressTargetResolution("not_found")
            statement = statement.where(CaseProgress.progress_id == parsed_progress)
        else:
            if case_id:
                parsed_case = _try_uuid(case_id)
                if parsed_case is None or parsed_case not in allowed_case_ids:
                    return CaseProgressTargetResolution("not_found")
                statement = statement.where(CaseProgress.case_id == parsed_case)
            statement = statement.where(
                CaseProgress.recorded_at
                >= context.occurred_at - timedelta(minutes=max(1, recent_minutes))
            )
        rows = list(
            (
                await self.session.scalars(
                    statement.order_by(CaseProgress.recorded_at.desc()).limit(3)
                )
            ).all()
        )
        if len(rows) == 1:
            row = rows[0]
            return CaseProgressTargetResolution(
                "resolved",
                progress_id=str(row.progress_id),
                case_id=str(row.case_id),
                version=row.version,
            )
        if len(rows) > 1:
            return CaseProgressTargetResolution(
                "needs_clarification",
                candidate_progress_ids=tuple(str(item.progress_id) for item in rows),
            )
        return CaseProgressTargetResolution("not_found")


def _try_uuid(value: str) -> UUID | None:
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None
