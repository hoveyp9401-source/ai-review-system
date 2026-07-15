from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from app.agent2.business.case_reference import match_grounded_visible_cases
from app.agent2.business.contracts import BusinessCommandContext


@dataclass(frozen=True)
class CaseRecord:
    case_id: str
    tenant_id: str
    case_number: str
    case_name: str
    party_names: tuple[str, ...]
    external_case_id: str = ""
    confirmed_aliases: tuple[str, ...] = ()
    version: int = 1
    owner_user_id: str = ""


@dataclass(frozen=True)
class CaseTargetResolution:
    status: str
    case_id: str = ""
    match_basis: str = ""
    candidate_case_ids: tuple[str, ...] = ()


def resolve_case_target(
    query: str,
    cases: tuple[CaseRecord, ...],
    context: BusinessCommandContext,
    *,
    source_text: str = "",
) -> CaseTargetResolution:
    normalized = _normalize(query)
    if not normalized:
        return CaseTargetResolution("not_found")
    allowed = set(context.allowed_case_ids)
    visible = [item for item in cases if item.tenant_id == context.tenant_id and item.case_id in allowed]
    internal_matches = [item for item in visible if _normalize(item.case_id) == normalized]
    if internal_matches:
        return _exact_resolution(internal_matches, "exact_case_id")
    external_matches = [
        item for item in visible if item.external_case_id and _normalize(item.external_case_id) == normalized
    ]
    if external_matches:
        return _exact_resolution(external_matches, "exact_external_case_id")
    number_matches = [item for item in visible if item.case_number and _normalize(item.case_number) == normalized]
    if number_matches:
        return _exact_resolution(number_matches, "exact_case_number")
    name_matches = [item for item in visible if _normalize(item.case_name) == normalized]
    if name_matches:
        name_related = [
            item
            for item in visible
            if normalized in _normalize(item.case_name) or _normalize(item.case_name) in normalized
        ]
        return _exact_resolution(name_related, "exact_case_name")
    alias_matches = [
        item
        for item in visible
        if any(_normalize(alias) == normalized for alias in item.confirmed_aliases if _normalize(alias))
    ]
    if alias_matches:
        alias_related = {
            item.case_id: item
            for item in (
                *alias_matches,
                *(
                    item
                    for item in visible
                    if normalized in _normalize(item.case_name)
                    or _normalize(item.case_name) in normalized
                ),
            )
        }
        return _exact_resolution(list(alias_related.values()), "confirmed_case_alias")
    grounded_matches = (
        match_grounded_visible_cases(
            query,
            segment_text=source_text,
            raw_cases=[
                {
                    "case_id": item.case_id,
                    "case_number": item.case_number,
                    "case_name": item.case_name,
                    "external_case_id": item.external_case_id,
                    "confirmed_aliases": list(item.confirmed_aliases),
                    "version": item.version,
                }
                for item in visible
            ],
        )
        if source_text.strip()
        else ()
    )
    if grounded_matches:
        matched_by_id = {
            str(match.case.get("case_id") or ""): match
            for match in grounded_matches
        }
        matched_cases = [
            item for item in visible if item.case_id in matched_by_id
        ]
        basis = (
            grounded_matches[0].basis
            if len(grounded_matches) == 1
            else "natural_case_abbreviation"
        )
        return _exact_resolution(matched_cases, basis)
    entity_key = _entity_key(query)
    related = [
        item
        for item in visible
        if normalized
        and (
            normalized in _normalize(item.case_name)
            or any(normalized in _normalize(name) or _normalize(name) in normalized for name in item.party_names)
            or (
                len(entity_key) >= 2
                and any(
                    entity_key in _entity_key(value) or _entity_key(value) in entity_key
                    for value in (item.case_name, *item.party_names)
                    if len(_entity_key(value)) >= 2
                )
            )
        )
    ]
    if related:
        return CaseTargetResolution(
            "needs_clarification",
            candidate_case_ids=tuple(sorted(item.case_id for item in related)),
        )
    return CaseTargetResolution("not_found")


def _exact_resolution(matches: list[CaseRecord], match_basis: str) -> CaseTargetResolution:
    unique = {item.case_id: item for item in matches}
    if len(unique) == 1:
        case = next(iter(unique.values()))
        return CaseTargetResolution("resolved", case.case_id, match_basis)
    return CaseTargetResolution(
        "needs_clarification",
        candidate_case_ids=tuple(sorted(unique)),
    )


def _normalize(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[\s\-—_·•,，.。()（）\[\]【】]", "", text)


def _entity_key(value: str) -> str:
    normalized = _normalize(value)
    for suffix in ("有限责任公司", "股份有限公司", "有限公司", "集团公司", "集团", "公司", "案件", "案"):
        normalized = normalized.removesuffix(suffix)
    return normalized
