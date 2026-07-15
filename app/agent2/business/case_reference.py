from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import re
import unicodedata
from typing import Any


_GENERIC_REFERENCES = {
    "这个案件",
    "这个案子",
    "该案件",
    "该案子",
    "本案",
    "案件进展",
}
_MAX_DISCOVERY_WINDOW = 16
_MIN_DERIVED_ABBREVIATION = 3
_GENERIC_DERIVED_REFERENCES = {
    "房地产",
    "有限公司",
    "有限责任公司",
    "建设工程",
    "合同纠纷",
    "施工合同",
    "买卖合同",
    "人民法院",
    "分公司",
    "总公司",
    "项目部",
    "开发商",
    "承包商",
    "供应商",
    "原告方",
    "被告方",
    "当事人",
    "该公司",
    "本公司",
    "施工方",
    "设计方",
}


@dataclass(frozen=True)
class VisibleCaseReferenceMatch:
    case: Mapping[str, Any]
    reference: str
    basis: str
    score: int


def normalize_case_reference(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", text)


def match_grounded_visible_cases(
    reference: str,
    *,
    segment_text: str,
    raw_cases: object,
) -> tuple[VisibleCaseReferenceMatch, ...]:
    """Resolve a model-proposed Case phrase only after grounding it in source text.

    Exact case numbers/names and confirmed aliases remain strongest.  A natural
    abbreviation may match by ordered characters only when it is at least three
    characters and retains two trusted adjacent character pairs.  Ambiguous
    matches are returned together; this function never chooses one.
    """

    normalized_reference = normalize_case_reference(reference)
    normalized_segment = normalize_case_reference(segment_text)
    if (
        not normalized_reference
        or normalized_reference in _GENERIC_REFERENCES
        or normalized_reference not in normalized_segment
    ):
        return ()
    return _match_reference(normalized_reference, reference, raw_cases)


def discover_visible_case_references(
    segment_text: str,
    raw_cases: object,
) -> tuple[VisibleCaseReferenceMatch, ...]:
    """Find trusted Case phrases in an action-free source segment.

    This is only a semantic-reassessment trigger.  It does not authorize a
    write; any returned phrase must still be emitted by cognition, grounded in
    the exact source segment, and pass Domain Admission.
    """

    cases = _visible_cases(raw_cases)
    normalized_segment = normalize_case_reference(segment_text)
    if not normalized_segment or not cases:
        return ()

    direct: list[VisibleCaseReferenceMatch] = []
    for case in cases:
        best: VisibleCaseReferenceMatch | None = None
        for raw_label, basis, minimum_length in _case_labels(case):
            label = normalize_case_reference(raw_label)
            if len(label) < minimum_length or label not in normalized_segment:
                continue
            candidate = VisibleCaseReferenceMatch(
                case=case,
                reference=str(raw_label),
                basis=basis,
                score=_basis_score(basis) + len(label),
            )
            if best is None or candidate.score > best.score:
                best = candidate
        if best is not None:
            direct.append(best)
    if direct:
        return _maximal_matches(direct)

    discovered: list[VisibleCaseReferenceMatch] = []
    maximum = min(len(normalized_segment), _MAX_DISCOVERY_WINDOW)
    for case in cases:
        best = None
        for length in range(maximum, _MIN_DERIVED_ABBREVIATION - 1, -1):
            for start in range(0, len(normalized_segment) - length + 1):
                window = normalized_segment[start : start + length]
                if not _is_safe_derived_reference(window):
                    continue
                match = _best_case_match(
                    window,
                    window,
                    case,
                    allow_derived_abbreviation=True,
                )
                if match is not None:
                    best = match
                    break
            if best is not None:
                break
        if best is not None:
            discovered.append(best)
    return _maximal_matches(discovered)


def _match_reference(
    normalized_reference: str,
    raw_reference: str,
    raw_cases: object,
) -> tuple[VisibleCaseReferenceMatch, ...]:
    cases = _visible_cases(raw_cases)
    exact_matches: list[VisibleCaseReferenceMatch] = []
    for case in cases:
        exact: VisibleCaseReferenceMatch | None = None
        for raw_label, basis, _minimum_length in _case_labels(case):
            if normalize_case_reference(raw_label) != normalized_reference:
                continue
            candidate = VisibleCaseReferenceMatch(
                case=case,
                reference=str(raw_reference),
                basis=basis,
                score=_basis_score(basis) + 300 + len(normalized_reference),
            )
            if exact is None or candidate.score > exact.score:
                exact = candidate
        if exact is not None:
            exact_matches.append(exact)
    # An exact trusted identity/confirmed alias is authoritative over another
    # Case's fuzzy subsequence. If the same exact label is confirmed on multiple
    # Cases, all exact matches remain and downstream selection still fail-closes.
    if exact_matches:
        return tuple(sorted(exact_matches, key=_match_sort_key))

    matches = [
        match
        for case in cases
        if (
            match := _best_case_match(
                normalized_reference,
                raw_reference,
                case,
                allow_derived_abbreviation=True,
            )
        )
        is not None
    ]
    return tuple(sorted(matches, key=_match_sort_key))


def _best_case_match(
    normalized_reference: str,
    raw_reference: str,
    case: Mapping[str, Any],
    *,
    allow_derived_abbreviation: bool,
) -> VisibleCaseReferenceMatch | None:
    best: VisibleCaseReferenceMatch | None = None
    for raw_label, basis, minimum_length in _case_labels(case):
        label = normalize_case_reference(raw_label)
        if not label:
            continue
        score = _reference_score(
            normalized_reference,
            label,
            basis=basis,
            minimum_length=minimum_length,
            allow_derived_abbreviation=allow_derived_abbreviation,
        )
        if score <= 0:
            continue
        candidate = VisibleCaseReferenceMatch(
            case=case,
            reference=str(raw_reference),
            basis=basis,
            score=score,
        )
        if best is None or candidate.score > best.score:
            best = candidate
    return best


def _reference_score(
    reference: str,
    label: str,
    *,
    basis: str,
    minimum_length: int,
    allow_derived_abbreviation: bool,
) -> int:
    if reference == label:
        return _basis_score(basis) + 300 + len(reference)
    containment_minimum = max(minimum_length, _MIN_DERIVED_ABBREVIATION)
    if (
        len(reference) >= containment_minimum
        and _is_safe_derived_reference(reference)
        and reference in label
    ):
        return _basis_score(basis) + 200 + len(reference)
    if len(label) >= containment_minimum and label in reference:
        return _basis_score(basis) + 180 + len(label)
    if (
        allow_derived_abbreviation
        and len(reference) >= _MIN_DERIVED_ABBREVIATION
        and _is_safe_derived_reference(reference)
        and _is_ordered_subsequence(reference, label)
        and _shared_bigram_count(reference, label) >= 2
    ):
        return _basis_score(basis) + 100 + len(reference)
    return 0


def _is_safe_derived_reference(reference: str) -> bool:
    """Reject generic legal/company phrases before treating text as an alias.

    Three-character organization or project names are common in real dialogue
    (for example ``鑫瑞达``).  They are usable only as grounded candidates and
    are still resolved against *all* visible Cases, so a shared name remains
    ambiguous instead of selecting one Case.  Generic legal phrases never gain
    alias authority merely because they happen to occur in one Case name.
    """

    return bool(
        len(reference) >= _MIN_DERIVED_ABBREVIATION
        and reference not in _GENERIC_REFERENCES
        and reference not in _GENERIC_DERIVED_REFERENCES
    )


def _case_labels(
    case: Mapping[str, Any],
) -> tuple[tuple[object, str, int], ...]:
    aliases = case.get("confirmed_aliases")
    aliases = aliases if isinstance(aliases, (list, tuple)) else ()
    return (
        (case.get("case_id"), "case_id", 1),
        (case.get("case_number"), "case_number", 1),
        (case.get("external_case_id"), "external_case_id", 1),
        *((alias, "confirmed_alias", 2) for alias in aliases),
        (case.get("case_name"), "case_name", 4),
    )


def _visible_cases(raw_cases: object) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(raw_cases, Sequence) or isinstance(raw_cases, (str, bytes)):
        return ()
    return tuple(
        item
        for item in raw_cases
        if isinstance(item, Mapping) and str(item.get("case_id") or "").strip()
    )


def _maximal_matches(
    matches: list[VisibleCaseReferenceMatch],
) -> tuple[VisibleCaseReferenceMatch, ...]:
    if not matches:
        return ()
    best_by_case: dict[str, VisibleCaseReferenceMatch] = {}
    for match in matches:
        case_id = str(match.case.get("case_id") or "")
        current = best_by_case.get(case_id)
        if current is None or match.score > current.score:
            best_by_case[case_id] = match
    # Never discard a different Case merely because another matching phrase is
    # longer.  That would silently turn a multi-Case message into a single-Case
    # write.  Downstream Admission may ask the user to select, but never guesses.
    return tuple(sorted(best_by_case.values(), key=_match_sort_key))


def _match_sort_key(match: VisibleCaseReferenceMatch) -> tuple[str, str, str]:
    return (
        str(match.case.get("case_number") or ""),
        str(match.case.get("case_name") or ""),
        str(match.case.get("case_id") or ""),
    )


def _basis_score(basis: str) -> int:
    return {
        "case_id": 90,
        "case_number": 80,
        "external_case_id": 70,
        "confirmed_alias": 60,
        "case_name": 50,
    }.get(basis, 0)


def _is_ordered_subsequence(needle: str, haystack: str) -> bool:
    position = 0
    for character in haystack:
        if position < len(needle) and character == needle[position]:
            position += 1
    return position == len(needle)


def _shared_bigram_count(reference: str, label: str) -> int:
    bigrams = {
        reference[index : index + 2]
        for index in range(len(reference) - 1)
    }
    return sum(1 for bigram in bigrams if bigram in label)
