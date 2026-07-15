from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher


@dataclass(frozen=True)
class PartyEntityRecord:
    party_id: str
    tenant_id: str
    party_type: str
    canonical_name: str
    short_name: str = ""
    former_names: tuple[str, ...] = ()
    unified_social_credit_code: str = ""
    registration_number: str = ""
    legal_representative: str = ""
    status: str = "active"
    registered_address: str = ""
    source_type: str = "imported_record"
    source_id: str = ""
    data_quality: str = "unverified"


@dataclass(frozen=True)
class PartyAliasRecord:
    alias_id: str
    tenant_id: str
    party_id: str
    alias: str
    source_type: str
    confirmed: bool = True


@dataclass(frozen=True)
class PartyIdentifierRecord:
    identifier_id: str
    tenant_id: str
    party_id: str
    identifier_type: str
    identifier_value: str
    confirmed: bool = True


@dataclass(frozen=True)
class PartyCaseRoleRecord:
    role_id: str
    tenant_id: str
    party_id: str
    case_id: str
    role_type: str
    effective_from: str = ""
    effective_to: str = ""
    source_reference: str = ""
    confirmation_status: str = "confirmed"


@dataclass(frozen=True)
class PartyCandidate:
    party_id: str
    canonical_name: str
    score: float
    match_basis: str
    confirmed: bool = False


@dataclass(frozen=True)
class PartyResolution:
    status: str
    party_id: str = ""
    match_basis: str = ""
    candidates: tuple[PartyCandidate, ...] = ()


class PartyKnowledgeBase:
    def __init__(
        self,
        *,
        entities: tuple[PartyEntityRecord, ...] = (),
        aliases: tuple[PartyAliasRecord, ...] = (),
        identifiers: tuple[PartyIdentifierRecord, ...] = (),
        case_roles: tuple[PartyCaseRoleRecord, ...] = (),
    ):
        self.entities = entities
        self.aliases = aliases
        self.identifiers = identifiers
        self.case_roles = case_roles

    def resolve(self, query: str, *, tenant_id: str, allowed_case_ids: set[str]) -> PartyResolution:
        normalized = normalize_party_name(query)
        if not normalized:
            return PartyResolution("not_found")
        entities = [item for item in self.entities if item.tenant_id == tenant_id]
        if self.case_roles:
            allowed_party_ids = {
                role.party_id
                for role in self.case_roles
                if role.tenant_id == tenant_id and role.case_id in allowed_case_ids
            }
            entities = [item for item in entities if item.party_id in allowed_party_ids]
        by_id = {item.party_id: item for item in entities}

        for identifier in self.identifiers:
            if (
                identifier.tenant_id == tenant_id
                and identifier.confirmed
                and identifier.party_id in by_id
                and normalize_identifier(identifier.identifier_value) == normalize_identifier(query)
            ):
                return PartyResolution("resolved", identifier.party_id, "exact_identifier")

        exact = [item for item in entities if normalize_party_name(item.canonical_name) == normalized]
        if len(exact) == 1:
            return PartyResolution("resolved", exact[0].party_id, "exact_canonical_name")
        alias_matches = [
            alias
            for alias in self.aliases
            if alias.tenant_id == tenant_id
            and alias.confirmed
            and alias.party_id in by_id
            and normalize_party_name(alias.alias) == normalized
        ]
        if len({item.party_id for item in alias_matches}) == 1:
            return PartyResolution("resolved", alias_matches[0].party_id, "confirmed_alias")

        candidates: dict[str, PartyCandidate] = {}
        for entity in entities:
            names = (entity.canonical_name, entity.short_name, *entity.former_names)
            score = max((_similarity(normalized, normalize_party_name(name)) for name in names if name), default=0.0)
            if score >= 0.62:
                candidates[entity.party_id] = PartyCandidate(
                    entity.party_id,
                    entity.canonical_name,
                    round(score, 4),
                    "fuzzy_name_candidate",
                    False,
                )
        if candidates:
            ordered = tuple(sorted(candidates.values(), key=lambda item: (-item.score, item.party_id)))
            return PartyResolution("needs_clarification", candidates=ordered)
        return PartyResolution("not_found")

    def party_cases(
        self, party_id: str, *, tenant_id: str, allowed_case_ids: set[str]
    ) -> tuple[PartyCaseRoleRecord, ...]:
        return tuple(
            role
            for role in self.case_roles
            if role.tenant_id == tenant_id
            and role.party_id == party_id
            and role.case_id in allowed_case_ids
        )


def normalize_party_name(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[\s\-—_·•,，.。()（）\[\]【】]", "", text)


def normalize_identifier(value: str) -> str:
    return re.sub(r"[^0-9a-zA-Z]", "", unicodedata.normalize("NFKC", str(value or ""))).upper()


def _similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()
