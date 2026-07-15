from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent2.business.models import (
    Agent2Case,
    BusinessAuditEvent,
    BusinessCommandReceipt,
    PartyAlias,
    PartyCaseClue,
    PartyCaseRole,
    PartyConflict,
    PartyEntity,
    PartyIdentifier,
    PartyMergeCandidate,
    PartyRelation,
    PartySourceReference,
)
from app.agent2.business.party import normalize_identifier, normalize_party_name


@dataclass(frozen=True)
class CaseImportRow:
    external_case_id: str
    case_number: str
    case_name: str
    case_type: str
    status: str
    owner_user_id: str
    source_type: str
    source_id: str
    confirmed_aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class PartyImportRow:
    party_source_id: str
    canonical_name: str
    party_type: str
    aliases: tuple[str, ...]
    identifiers: tuple[tuple[str, str], ...]
    external_case_id: str
    role_type: str
    source_type: str
    source_id: str
    short_name: str = ""
    former_names: tuple[str, ...] = ()
    legal_representative: str = ""
    registered_address: str = ""


@dataclass(frozen=True)
class PartyRelationImportRow:
    from_party_source_id: str
    to_party_source_id: str
    external_case_id: str
    relation_type: str
    source_type: str
    source_id: str
    confirmation_status: str = "confirmed"


@dataclass(frozen=True)
class PartyCaseClueImportRow:
    party_source_id: str
    external_case_id: str
    clue_type: str
    summary: str
    source_type: str
    source_id: str
    label: str = ""
    amount: Decimal | None = None
    currency: str = "CNY"
    occurred_at: datetime | None = None
    source_field: str = ""
    source_reference: dict[str, Any] | None = None
    confirmation_status: str = "confirmed"


@dataclass(frozen=True)
class PartyImportDataset:
    dataset_id: str
    tenant_id: str
    company_id: str
    department_id: str
    team_id: str
    cases: tuple[CaseImportRow, ...]
    parties: tuple[PartyImportRow, ...]
    relations: tuple[PartyRelationImportRow, ...] = ()
    clues: tuple[PartyCaseClueImportRow, ...] = ()


@dataclass(frozen=True)
class PlannedCase:
    case_id: UUID
    row: CaseImportRow


@dataclass(frozen=True)
class PlannedParty:
    party_id: UUID
    canonical_name: str
    normalized_name: str
    party_type: str
    short_name: str
    former_names: tuple[str, ...]
    unified_social_credit_code: str
    registration_number: str
    legal_representative: str
    registered_address: str
    source_type: str
    source_id: str
    data_quality: str


@dataclass(frozen=True)
class PlannedAlias:
    alias_id: UUID
    party_id: UUID
    alias: str
    normalized_alias: str
    alias_type: str
    source_type: str
    source_id: str


@dataclass(frozen=True)
class PlannedIdentifier:
    identifier_id: UUID
    party_id: UUID
    identifier_type: str
    identifier_value: str
    normalized_value: str
    source_type: str
    source_id: str


@dataclass(frozen=True)
class PlannedCaseRole:
    role_id: UUID
    party_id: UUID
    case_id: UUID
    role_type: str
    source_type: str
    source_id: str


@dataclass(frozen=True)
class PlannedSourceReference:
    reference_id: UUID
    party_id: UUID
    source_type: str
    source_id: str
    source_value: str


@dataclass(frozen=True)
class PlannedRelation:
    relation_id: UUID
    case_id: UUID
    from_party_id: UUID
    to_party_id: UUID
    relation_type: str
    source_type: str
    source_id: str
    confirmation_status: str


@dataclass(frozen=True)
class PlannedCaseClue:
    clue_id: UUID
    case_id: UUID
    party_id: UUID
    row: PartyCaseClueImportRow


@dataclass(frozen=True)
class PlannedMergeCandidate:
    candidate_id: UUID
    left_party_id: UUID
    right_party_id: UUID
    match_basis: tuple[str, ...]
    match_score: Decimal


@dataclass(frozen=True)
class PlannedConflict:
    conflict_id: UUID
    party_id: UUID
    field_name: str
    competing_values: tuple[dict[str, str], ...]


@dataclass(frozen=True)
class PartyImportPlan:
    dataset: PartyImportDataset
    cases: tuple[PlannedCase, ...]
    parties: tuple[PlannedParty, ...]
    aliases: tuple[PlannedAlias, ...]
    identifiers: tuple[PlannedIdentifier, ...]
    case_roles: tuple[PlannedCaseRole, ...]
    relations: tuple[PlannedRelation, ...]
    clues: tuple[PlannedCaseClue, ...]
    source_references: tuple[PlannedSourceReference, ...]
    merge_candidates: tuple[PlannedMergeCandidate, ...]
    conflicts: tuple[PlannedConflict, ...]

    def counts(self) -> dict[str, int]:
        return {
            "cases": len(self.cases),
            "parties": len(self.parties),
            "aliases": len(self.aliases),
            "identifiers": len(self.identifiers),
            "case_roles": len(self.case_roles),
            "relations": len(self.relations),
            "clues": len(self.clues),
            "source_references": len(self.source_references),
            "merge_candidates": len(self.merge_candidates),
            "conflicts": len(self.conflicts),
        }


@dataclass(frozen=True)
class PartyImportResult:
    receipt_id: str
    status: str
    actual_write: bool
    counts: dict[str, Any]


def build_party_import_plan(dataset: PartyImportDataset) -> PartyImportPlan:
    _validate_dataset(dataset)
    case_by_external = {
        row.external_case_id: PlannedCase(
            case_id=uuid5(NAMESPACE_URL, f"agent2-case:{dataset.tenant_id}:{row.external_case_id}"),
            row=row,
        )
        for row in dataset.cases
    }
    rows = list(dataset.parties)
    parent = list(range(len(rows)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    source_owner: dict[str, int] = {}
    identifier_owner: dict[tuple[str, str], int] = {}
    for index, row in enumerate(rows):
        source_key = row.party_source_id.strip()
        if source_key in source_owner:
            union(index, source_owner[source_key])
        else:
            source_owner[source_key] = index
        for identifier_type, value in row.identifiers:
            normalized = normalize_identifier(value)
            if not normalized:
                continue
            key = (identifier_type.casefold().strip(), normalized)
            if key in identifier_owner:
                union(index, identifier_owner[key])
            else:
                identifier_owner[key] = index

    groups: dict[int, list[PartyImportRow]] = {}
    for index, row in enumerate(rows):
        groups.setdefault(find(index), []).append(row)

    parties: list[PlannedParty] = []
    aliases: list[PlannedAlias] = []
    identifiers: list[PlannedIdentifier] = []
    roles: list[PlannedCaseRole] = []
    references: list[PlannedSourceReference] = []
    conflicts: list[PlannedConflict] = []
    party_ids_by_source: dict[str, UUID] = {}

    for group_rows in sorted(groups.values(), key=lambda values: min(item.party_source_id for item in values)):
        ordered = sorted(group_rows, key=lambda item: (item.source_type, item.source_id, item.party_source_id))
        identifier_keys = sorted(
            {
                (kind.casefold().strip(), normalize_identifier(value))
                for row in ordered
                for kind, value in row.identifiers
                if normalize_identifier(value)
            }
        )
        identity = (
            f"identifier:{identifier_keys[0][0]}:{identifier_keys[0][1]}"
            if identifier_keys
            else f"source:{ordered[0].party_source_id}"
        )
        party_id = uuid5(NAMESPACE_URL, f"agent2-party:{dataset.tenant_id}:{identity}")
        canonical = ordered[0].canonical_name.strip()
        canonical_variants = tuple(dict.fromkeys(row.canonical_name.strip() for row in ordered))
        source_type, source_id = ordered[0].source_type, ordered[0].source_id
        parties.append(
            PlannedParty(
                party_id=party_id,
                canonical_name=canonical,
                normalized_name=normalize_party_name(canonical),
                party_type=ordered[0].party_type,
                short_name=next((row.short_name for row in ordered if row.short_name), ""),
                former_names=tuple(dict.fromkeys(name for row in ordered for name in row.former_names)),
                unified_social_credit_code=next(
                    (
                        normalized
                        for kind, normalized in identifier_keys
                        if kind in {"uscc", "unified_social_credit_code"}
                    ),
                    "",
                ),
                registration_number=next(
                    (
                        normalized
                        for kind, normalized in identifier_keys
                        if kind in {"registration_number", "registration_no"}
                    ),
                    "",
                ),
                legal_representative=next(
                    (row.legal_representative for row in ordered if row.legal_representative), ""
                ),
                registered_address=next(
                    (row.registered_address for row in ordered if row.registered_address), ""
                ),
                source_type=source_type,
                source_id=source_id,
                data_quality="confirmed_identifier" if identifier_keys else "source_confirmed",
            )
        )
        alias_values = tuple(
            dict.fromkeys(
                value.strip()
                for row in ordered
                for value in (*row.aliases, *row.former_names, row.canonical_name)
                if value.strip() and normalize_party_name(value) != normalize_party_name(canonical)
            )
        )
        for alias in alias_values:
            normalized_alias = normalize_party_name(alias)
            alias_id = uuid5(NAMESPACE_URL, f"agent2-alias:{dataset.tenant_id}:{party_id}:{normalized_alias}")
            aliases.append(
                PlannedAlias(
                    alias_id,
                    party_id,
                    alias,
                    normalized_alias,
                    "imported_alias",
                    source_type,
                    source_id,
                )
            )
        seen_identifiers: set[tuple[str, str]] = set()
        for row in ordered:
            party_ids_by_source[row.party_source_id] = party_id
            for kind, value in row.identifiers:
                normalized = normalize_identifier(value)
                key = (kind.casefold().strip(), normalized)
                if not normalized or key in seen_identifiers:
                    continue
                seen_identifiers.add(key)
                identifiers.append(
                    PlannedIdentifier(
                        uuid5(NAMESPACE_URL, f"agent2-identifier:{dataset.tenant_id}:{key[0]}:{key[1]}"),
                        party_id,
                        key[0],
                        value.strip(),
                        key[1],
                        row.source_type,
                        row.source_id,
                    )
                )
            case_id = case_by_external[row.external_case_id].case_id
            role_identity = f"{dataset.tenant_id}:{party_id}:{case_id}:{row.role_type}"
            roles.append(
                PlannedCaseRole(
                    uuid5(NAMESPACE_URL, f"agent2-party-role:{role_identity}"),
                    party_id,
                    case_id,
                    row.role_type,
                    row.source_type,
                    row.source_id,
                )
            )
            references.append(
                PlannedSourceReference(
                    uuid5(
                        NAMESPACE_URL,
                        f"agent2-party-source:{dataset.tenant_id}:{party_id}:{row.source_type}:{row.source_id}",
                    ),
                    party_id,
                    row.source_type,
                    row.source_id,
                    row.canonical_name,
                )
            )
        if len(canonical_variants) > 1:
            competing = tuple(
                {"value": row.canonical_name, "source_type": row.source_type, "source_id": row.source_id}
                for row in ordered
            )
            conflicts.append(
                PlannedConflict(
                    uuid5(NAMESPACE_URL, f"agent2-party-conflict:{dataset.tenant_id}:{party_id}:canonical_name"),
                    party_id,
                    "canonical_name",
                    competing,
                )
            )

    planned_relations = tuple(
        sorted(
            (
                PlannedRelation(
                    relation_id=uuid5(
                        NAMESPACE_URL,
                        (
                            f"agent2-party-relation:{dataset.tenant_id}:"
                            f"{case_by_external[row.external_case_id].case_id}:"
                            f"{party_ids_by_source[row.from_party_source_id]}:"
                            f"{party_ids_by_source[row.to_party_source_id]}:{row.relation_type}"
                        ),
                    ),
                    case_id=case_by_external[row.external_case_id].case_id,
                    from_party_id=party_ids_by_source[row.from_party_source_id],
                    to_party_id=party_ids_by_source[row.to_party_source_id],
                    relation_type=row.relation_type,
                    source_type=row.source_type,
                    source_id=row.source_id,
                    confirmation_status=row.confirmation_status,
                )
                for row in dataset.relations
            ),
            key=lambda item: str(item.relation_id),
        )
    )
    planned_clues = tuple(
        sorted(
            (
                PlannedCaseClue(
                    clue_id=uuid5(
                        NAMESPACE_URL,
                        (
                            f"agent2-party-clue:{dataset.tenant_id}:"
                            f"{case_by_external[row.external_case_id].case_id}:"
                            f"{party_ids_by_source[row.party_source_id]}:{row.clue_type}:"
                            f"{row.source_type}:{row.source_id}:{row.source_field}"
                        ),
                    ),
                    case_id=case_by_external[row.external_case_id].case_id,
                    party_id=party_ids_by_source[row.party_source_id],
                    row=row,
                )
                for row in dataset.clues
            ),
            key=lambda item: str(item.clue_id),
        )
    )

    merge_candidates: list[PlannedMergeCandidate] = []
    by_name: dict[str, list[UUID]] = {}
    for party in parties:
        by_name.setdefault(party.normalized_name, []).append(party.party_id)
    for normalized_name, party_ids in by_name.items():
        for index, left in enumerate(sorted(party_ids, key=str)):
            for right in sorted(party_ids, key=str)[index + 1 :]:
                merge_candidates.append(
                    PlannedMergeCandidate(
                        uuid5(NAMESPACE_URL, f"agent2-party-merge:{dataset.tenant_id}:{left}:{right}"),
                        left,
                        right,
                        ("exact_normalized_name_without_shared_identifier", normalized_name),
                        Decimal("1.00000"),
                    )
                )

    return PartyImportPlan(
        dataset=dataset,
        cases=tuple(sorted(case_by_external.values(), key=lambda item: item.row.external_case_id)),
        parties=tuple(sorted(parties, key=lambda item: str(item.party_id))),
        aliases=tuple(sorted(aliases, key=lambda item: str(item.alias_id))),
        identifiers=tuple(sorted(identifiers, key=lambda item: str(item.identifier_id))),
        case_roles=tuple(sorted({item.role_id: item for item in roles}.values(), key=lambda item: str(item.role_id))),
        relations=planned_relations,
        clues=planned_clues,
        source_references=tuple(
            sorted({item.reference_id: item for item in references}.values(), key=lambda item: str(item.reference_id))
        ),
        merge_candidates=tuple(sorted(merge_candidates, key=lambda item: str(item.candidate_id))),
        conflicts=tuple(sorted(conflicts, key=lambda item: str(item.conflict_id))),
    )


async def persist_party_import_plan(
    session: AsyncSession,
    plan: PartyImportPlan,
    *,
    actor_user_id: str,
    source_message_id: str,
    occurred_at: datetime,
) -> PartyImportResult:
    dataset = plan.dataset
    material = json.dumps(
        {
            "dataset": asdict(dataset),
            "counts": plan.counts(),
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    idempotency_key = f"{dataset.tenant_id}:party-import:{dataset.dataset_id}:{digest}"
    receipt_id = uuid5(NAMESPACE_URL, f"business-receipt:{idempotency_key}")
    reserved = await session.scalar(
        insert(BusinessCommandReceipt)
        .values(
            receipt_id=receipt_id,
            tenant_id=dataset.tenant_id,
            command_id=f"party-import:{dataset.dataset_id}",
            command_type="import_party_knowledge",
            actor_user_id=actor_user_id,
            source_message_id=source_message_id,
            idempotency_key=idempotency_key,
            status="processing",
            resource_type="party_import",
            resource_id=dataset.dataset_id,
            actual_write=False,
            created_at=occurred_at,
            updated_at=occurred_at,
        )
        .on_conflict_do_nothing(
            index_elements=[BusinessCommandReceipt.tenant_id, BusinessCommandReceipt.idempotency_key]
        )
        .returning(BusinessCommandReceipt.receipt_id)
    )
    if reserved is None:
        existing = await session.scalar(
            select(BusinessCommandReceipt).where(
                BusinessCommandReceipt.tenant_id == dataset.tenant_id,
                BusinessCommandReceipt.idempotency_key == idempotency_key,
            )
        )
        if existing is None:
            raise RuntimeError("party import idempotency receipt disappeared")
        return PartyImportResult(str(existing.receipt_id), "duplicate", False, dict(existing.after_json or {}))

    inserted_counts = {key: 0 for key in plan.counts()}
    for case in plan.cases:
        row = case.row
        result = await session.execute(
            insert(Agent2Case)
            .values(
                case_id=case.case_id,
                tenant_id=dataset.tenant_id,
                company_id=dataset.company_id,
                department_id=dataset.department_id,
                team_id=dataset.team_id,
                external_case_id=row.external_case_id,
                case_number=row.case_number,
                case_name=row.case_name,
                case_type=row.case_type,
                status=row.status,
                owner_user_id=row.owner_user_id,
                source_type=row.source_type,
                source_id=row.source_id,
                source_json=asdict(row),
                version=1,
                created_at=occurred_at,
                updated_at=occurred_at,
            )
            .on_conflict_do_nothing(
                index_elements=[Agent2Case.tenant_id, Agent2Case.external_case_id]
            )
        )
        inserted_counts["cases"] += _affected_rows(result)
    for party in plan.parties:
        result = await session.execute(
            insert(PartyEntity)
            .values(
                party_id=party.party_id,
                tenant_id=dataset.tenant_id,
                party_type=party.party_type,
                canonical_name=party.canonical_name,
                normalized_name=party.normalized_name,
                short_name=party.short_name,
                former_names=list(party.former_names),
                unified_social_credit_code=party.unified_social_credit_code,
                registration_number=party.registration_number,
                legal_representative=party.legal_representative,
                registered_address=party.registered_address,
                source_type=party.source_type,
                source_id=party.source_id,
                data_quality=party.data_quality,
                version=1,
                created_at=occurred_at,
                updated_at=occurred_at,
            )
            .on_conflict_do_nothing(index_elements=[PartyEntity.party_id])
        )
        inserted_counts["parties"] += _affected_rows(result)
    child_counts = await _persist_party_children(session, plan, occurred_at)
    for key, value in child_counts.items():
        inserted_counts[key] += value
    planned_counts = plan.counts()
    skipped_counts = {
        key: max(0, planned_counts[key] - inserted_counts[key])
        for key in planned_counts
    }
    counts = {
        "planned": planned_counts,
        "inserted": inserted_counts,
        "skipped_existing_or_conflicting": skipped_counts,
    }
    actual_write = any(inserted_counts.values())
    await session.execute(
        BusinessCommandReceipt.__table__.update()
        .where(BusinessCommandReceipt.receipt_id == receipt_id)
        .values(
            status="executed",
            after_json=counts,
            actual_write=actual_write,
            updated_at=occurred_at,
        )
    )
    if actual_write:
        session.add(
            BusinessAuditEvent(
                audit_id=uuid5(NAMESPACE_URL, f"business-audit:{receipt_id}"),
                tenant_id=dataset.tenant_id,
                receipt_id=receipt_id,
                actor_user_id=actor_user_id,
                source_message_id=source_message_id,
                source_channel="party_import_adapter",
                command_type="import_party_knowledge",
                resource_type="party_import",
                resource_id=dataset.dataset_id,
                before_json={},
                after_json=counts,
                created_at=occurred_at,
            )
        )
    await session.flush()
    return PartyImportResult(str(receipt_id), "executed", actual_write, counts)


async def _persist_party_children(
    session: AsyncSession,
    plan: PartyImportPlan,
    occurred_at: datetime,
) -> dict[str, int]:
    tenant_id = plan.dataset.tenant_id
    counts = {
        "aliases": 0,
        "identifiers": 0,
        "case_roles": 0,
        "relations": 0,
        "clues": 0,
        "source_references": 0,
        "merge_candidates": 0,
        "conflicts": 0,
    }
    for item in plan.aliases:
        result = await session.execute(
            insert(PartyAlias)
            .values(
                alias_id=item.alias_id,
                tenant_id=tenant_id,
                party_id=item.party_id,
                alias=item.alias,
                normalized_alias=item.normalized_alias,
                alias_type=item.alias_type,
                source_type=item.source_type,
                source_id=item.source_id,
                confirmation_status="confirmed",
                created_at=occurred_at,
                updated_at=occurred_at,
            )
            .on_conflict_do_nothing(constraint="agent2_party_alias_unique")
        )
        counts["aliases"] += _affected_rows(result)
    for item in plan.identifiers:
        result = await session.execute(
            insert(PartyIdentifier)
            .values(
                identifier_id=item.identifier_id,
                tenant_id=tenant_id,
                party_id=item.party_id,
                identifier_type=item.identifier_type,
                identifier_value=item.identifier_value,
                normalized_value=item.normalized_value,
                source_type=item.source_type,
                source_id=item.source_id,
                confirmation_status="confirmed",
                created_at=occurred_at,
                updated_at=occurred_at,
            )
            .on_conflict_do_nothing(constraint="agent2_party_identifier_unique")
        )
        counts["identifiers"] += _affected_rows(result)
    for item in plan.case_roles:
        result = await session.execute(
            insert(PartyCaseRole)
            .values(
                role_id=item.role_id,
                tenant_id=tenant_id,
                party_id=item.party_id,
                case_id=item.case_id,
                role_type=item.role_type,
                source_reference={"source_type": item.source_type, "source_id": item.source_id},
                confirmation_status="confirmed",
                created_at=occurred_at,
                updated_at=occurred_at,
            )
            .on_conflict_do_nothing(constraint="agent2_party_case_role_unique")
        )
        counts["case_roles"] += _affected_rows(result)
    for item in plan.relations:
        result = await session.execute(
            insert(PartyRelation)
            .values(
                relation_id=item.relation_id,
                tenant_id=tenant_id,
                case_id=item.case_id,
                from_party_id=item.from_party_id,
                to_party_id=item.to_party_id,
                relation_type=item.relation_type,
                source_reference={
                    "source_type": item.source_type,
                    "source_id": item.source_id,
                },
                confirmation_status=item.confirmation_status,
                created_at=occurred_at,
                updated_at=occurred_at,
            )
            .on_conflict_do_nothing(constraint="agent2_party_relation_unique")
        )
        counts["relations"] += _affected_rows(result)
    for item in plan.clues:
        row = item.row
        result = await session.execute(
            insert(PartyCaseClue)
            .values(
                clue_id=item.clue_id,
                tenant_id=tenant_id,
                case_id=item.case_id,
                party_id=item.party_id,
                clue_type=row.clue_type,
                label=row.label,
                summary=row.summary,
                amount=row.amount,
                currency=row.currency,
                occurred_at=row.occurred_at,
                source_type=row.source_type,
                source_id=row.source_id,
                source_field=row.source_field,
                source_reference=dict(row.source_reference or {}),
                confirmation_status=row.confirmation_status,
                created_at=occurred_at,
                updated_at=occurred_at,
            )
            .on_conflict_do_nothing(constraint="agent2_party_case_clue_unique")
        )
        counts["clues"] += _affected_rows(result)
    for item in plan.source_references:
        result = await session.execute(
            insert(PartySourceReference)
            .values(
                reference_id=item.reference_id,
                tenant_id=tenant_id,
                party_id=item.party_id,
                source_type=item.source_type,
                source_id=item.source_id,
                source_field="canonical_name",
                source_row=item.source_id,
                source_value=item.source_value,
                content_origin="imported_record",
                confirmation_status="confirmed",
                created_at=occurred_at,
                updated_at=occurred_at,
            )
            .on_conflict_do_nothing(constraint="agent2_party_source_unique")
        )
        counts["source_references"] += _affected_rows(result)
    for item in plan.merge_candidates:
        result = await session.execute(
            insert(PartyMergeCandidate)
            .values(
                candidate_id=item.candidate_id,
                tenant_id=tenant_id,
                left_party_id=item.left_party_id,
                right_party_id=item.right_party_id,
                match_basis=list(item.match_basis),
                match_score=item.match_score,
                status="candidate",
                created_at=occurred_at,
                updated_at=occurred_at,
            )
            .on_conflict_do_nothing(constraint="agent2_party_merge_pair_unique")
        )
        counts["merge_candidates"] += _affected_rows(result)
    for item in plan.conflicts:
        result = await session.execute(
            insert(PartyConflict)
            .values(
                conflict_id=item.conflict_id,
                tenant_id=tenant_id,
                party_id=item.party_id,
                field_name=item.field_name,
                competing_values=list(item.competing_values),
                status="open",
                created_at=occurred_at,
                updated_at=occurred_at,
            )
            .on_conflict_do_nothing(index_elements=[PartyConflict.conflict_id])
        )
        counts["conflicts"] += _affected_rows(result)
    return counts


def _affected_rows(result: Any) -> int:
    try:
        return max(0, int(result.rowcount or 0))
    except (AttributeError, TypeError, ValueError):
        return 0


def dataset_from_dict(payload: dict[str, Any]) -> PartyImportDataset:
    return PartyImportDataset(
        dataset_id=str(payload.get("dataset_id") or "").strip(),
        tenant_id=str(payload.get("tenant_id") or "").strip(),
        company_id=str(payload.get("company_id") or "").strip(),
        department_id=str(payload.get("department_id") or "").strip(),
        team_id=str(payload.get("team_id") or "").strip(),
        cases=tuple(
            CaseImportRow(
                **{
                    **item,
                    "confirmed_aliases": tuple(item.get("confirmed_aliases", ())),
                }
            )
            for item in payload.get("cases", [])
        ),
        parties=tuple(
            PartyImportRow(
                **{
                    **item,
                    "aliases": tuple(item.get("aliases", ())),
                    "identifiers": tuple(tuple(value) for value in item.get("identifiers", ())),
                    "former_names": tuple(item.get("former_names", ())),
                }
            )
            for item in payload.get("parties", [])
        ),
        relations=tuple(
            PartyRelationImportRow(**item)
            for item in payload.get("relations", [])
        ),
        clues=tuple(
            PartyCaseClueImportRow(
                **{
                    **item,
                    "amount": (
                        Decimal(str(item["amount"]))
                        if item.get("amount") not in (None, "")
                        else None
                    ),
                    "occurred_at": (
                        datetime.fromisoformat(str(item["occurred_at"]))
                        if item.get("occurred_at")
                        else None
                    ),
                    "source_reference": dict(item.get("source_reference") or {}),
                }
            )
            for item in payload.get("clues", [])
        ),
    )


def _validate_dataset(dataset: PartyImportDataset) -> None:
    for name in ("dataset_id", "tenant_id", "company_id", "department_id", "team_id"):
        if not str(getattr(dataset, name) or "").strip():
            raise ValueError(f"{name} is required")
    if not dataset.cases:
        raise ValueError("at least one case is required")
    external_case_ids = [item.external_case_id for item in dataset.cases]
    if len(set(external_case_ids)) != len(external_case_ids):
        raise ValueError("duplicate external_case_id")
    known_cases = set(external_case_ids)
    allowed_party_types = {"company", "person", "organization", "government", "court", "other"}
    known_party_sources = {row.party_source_id for row in dataset.parties}
    for row in dataset.parties:
        if not row.party_source_id or not row.canonical_name or not row.source_type or not row.source_id:
            raise ValueError("party source identity, canonical name and source reference are required")
        if row.party_type not in allowed_party_types:
            raise ValueError("unsupported party_type")
        if row.external_case_id not in known_cases:
            raise ValueError("party role references unknown case")
    for row in dataset.relations:
        if row.from_party_source_id not in known_party_sources or row.to_party_source_id not in known_party_sources:
            raise ValueError("party relation references unknown party")
        if row.external_case_id not in known_cases:
            raise ValueError("party relation references unknown case")
        if row.from_party_source_id == row.to_party_source_id:
            raise ValueError("party relation cannot reference itself")
        if not row.relation_type or not row.source_type or not row.source_id:
            raise ValueError("party relation type and source are required")
        if row.confirmation_status not in {"confirmed", "pending_confirmation"}:
            raise ValueError("unsupported party relation confirmation status")
    allowed_clue_types = {"person", "court", "payment", "asset", "document", "other"}
    for row in dataset.clues:
        if row.party_source_id not in known_party_sources:
            raise ValueError("party clue references unknown party")
        if row.external_case_id not in known_cases:
            raise ValueError("party clue references unknown case")
        if row.clue_type not in allowed_clue_types:
            raise ValueError("unsupported party clue type")
        if not row.summary.strip() or not row.source_type or not row.source_id:
            raise ValueError("party clue summary and source are required")
        if row.occurred_at is not None and row.occurred_at.tzinfo is None:
            raise ValueError("party clue occurred_at must be timezone-aware")
        if row.confirmation_status not in {"confirmed", "pending_confirmation"}:
            raise ValueError("unsupported party clue confirmation status")
