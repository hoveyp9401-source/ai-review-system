"""Safely turn reviewed daily-report history into optional weekly-plan suggestions.

Agent2 decides whether a stable ``tomorrow_plan`` item has a later follow-up.  This
module does not interpret language.  It binds that bounded decision to exact,
same-owner evidence and emits suggestions only; it has no formal-plan write
interface.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum

from app.agent2.weekly_plan_suggestions import (
    SuggestionStatus,
    TrustedSourceKind,
    WeeklyPlanSuggestion,
    build_trusted_evidence,
    create_suggestion,
    supersede_suggestion,
)


class ConfirmedRecordState(str, Enum):
    CONFIRMED = "confirmed"
    SUBMITTED = "submitted"


class FollowUpVerdict(str, Enum):
    NO_LATER_RECORD = "no_later_record"
    FOLLOWED_UP = "followed_up"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class StableTomorrowPlanItem:
    owner_user_id: str
    field_name: str
    item_ref: str
    source_ref: str
    source_version: str
    record_state: ConfirmedRecordState
    recorded_at: datetime
    source_text: str
    source_sha256: str
    item_exact_text: str


@dataclass(frozen=True)
class TrustedPersonalRecord:
    owner_user_id: str
    source_kind: TrustedSourceKind
    source_ref: str
    source_version: str
    recorded_at: datetime
    evidence_text: str
    evidence_sha256: str


@dataclass(frozen=True)
class BoundedFollowUpAssessment:
    review_scope: str
    verdict: FollowUpVerdict


@dataclass(frozen=True)
class HistorySuggestionRequest:
    owner_user_id: str
    target_week_start: date
    as_of: datetime
    source_items: tuple[StableTomorrowPlanItem, ...]
    later_records: tuple[TrustedPersonalRecord, ...]
    assessments: tuple[BoundedFollowUpAssessment, ...]
    existing_suggestions: tuple[WeeklyPlanSuggestion, ...] = ()


@dataclass(frozen=True)
class HistorySuggestionResult:
    new_suggestions: tuple[WeeklyPlanSuggestion, ...]
    superseded_suggestions: tuple[WeeklyPlanSuggestion, ...] = ()


def build_follow_up_review_scope(
    *,
    owner_user_id: str,
    source_item: StableTomorrowPlanItem,
    later_records: tuple[TrustedPersonalRecord, ...],
    as_of: datetime,
) -> str:
    """Bind a model assessment to one exact source item and evidence window."""

    payload = {
        "owner_user_id": owner_user_id,
        "source_item": _source_item_payload(source_item),
        "later_records": [
            _later_record_payload(record)
            for record in sorted(
                later_records,
                key=lambda value: (
                    value.recorded_at.isoformat(),
                    value.source_ref,
                    value.source_version,
                ),
            )
        ],
        "as_of": as_of.isoformat(),
    }
    return f"wps_review_{_sha256_json(payload)}"


def generate_history_suggestions(
    request: HistorySuggestionRequest,
) -> HistorySuggestionResult:
    """Validate grounded reviews and return at most three optional suggestions."""

    _validate_request_boundary(request)
    assessments_by_scope = {
        assessment.review_scope: assessment for assessment in request.assessments
    }
    candidates: list[WeeklyPlanSuggestion] = []
    ordered_items = sorted(
        request.source_items,
        key=lambda value: (
            value.recorded_at,
            value.source_ref,
            value.source_version,
            value.item_ref,
        ),
        reverse=True,
    )
    for item in ordered_items:
        relevant_later = tuple(
            record
            for record in request.later_records
            if record.recorded_at > item.recorded_at
            and record.recorded_at <= request.as_of
        )
        if not relevant_later:
            continue
        scope = build_follow_up_review_scope(
            owner_user_id=request.owner_user_id,
            source_item=item,
            later_records=relevant_later,
            as_of=request.as_of,
        )
        assessment = assessments_by_scope.get(scope)
        if assessment is None or assessment.verdict is not FollowUpVerdict.NO_LATER_RECORD:
            continue
        evidence = build_trusted_evidence(
            owner_user_id=item.owner_user_id,
            source_kind=TrustedSourceKind.CONFIRMED_RECORD,
            source_ref=f"{item.source_ref}#{item.item_ref}",
            source_version=item.source_version,
            evidence_text=item.source_text,
        )
        candidates.append(
            create_suggestion(
                owner_user_id=request.owner_user_id,
                target_week_start=request.target_week_start,
                evidence=evidence,
                matter_excerpt=item.item_exact_text,
                created_at=request.as_of,
                expires_at=datetime.combine(
                    request.target_week_start + timedelta(days=7),
                    time.min,
                    tzinfo=request.as_of.tzinfo,
                ),
            )
        )
    scoped_existing = tuple(
        suggestion
        for suggestion in request.existing_suggestions
        if suggestion.owner_user_id == request.owner_user_id
        and suggestion.target_week_start == request.target_week_start
    )
    active_existing = tuple(
        suggestion
        for suggestion in scoped_existing
        if suggestion.status is SuggestionStatus.AVAILABLE
        and request.as_of < suggestion.expires_at
    )
    terminal_source_refs = {
        suggestion.evidence.source_ref
        for suggestion in scoped_existing
        if suggestion.status in {
            SuggestionStatus.ACCEPTED,
            SuggestionStatus.REJECTED,
        }
    }
    existing_ids = {suggestion.suggestion_id for suggestion in active_existing}
    existing_by_source: dict[str, list[WeeklyPlanSuggestion]] = {}
    for suggestion in active_existing:
        existing_by_source.setdefault(suggestion.evidence.source_ref, []).append(
            suggestion
        )

    selected_list: list[WeeklyPlanSuggestion] = []
    selected_ids: set[str] = set()
    projected_available_count = len(active_existing)
    for candidate in candidates:
        if len(selected_list) >= 3:
            break
        # An explicit owner decision is terminal for this source within the
        # target week.  A background refresh must never revive it.
        if candidate.evidence.source_ref in terminal_source_refs:
            continue
        if candidate.suggestion_id in existing_ids or candidate.suggestion_id in selected_ids:
            continue
        replaced = existing_by_source.get(candidate.evidence.source_ref, [])
        if any(
            existing.source_version == candidate.source_version
            for existing in replaced
        ):
            # A changed excerpt must carry a changed source version.  Otherwise
            # the evidence is internally inconsistent and fails closed.
            continue
        if not replaced and projected_available_count >= 3:
            continue
        selected_list.append(candidate)
        selected_ids.add(candidate.suggestion_id)
        if not replaced:
            projected_available_count += 1
    selected = tuple(selected_list)
    replacements_by_source = {item.evidence.source_ref: item for item in selected}
    superseded: list[WeeklyPlanSuggestion] = []
    for existing in active_existing:
        if existing.status is not SuggestionStatus.AVAILABLE:
            continue
        replacement = replacements_by_source.get(existing.evidence.source_ref)
        if replacement is None or replacement.source_version == existing.source_version:
            continue
        superseded.append(
            supersede_suggestion(
                existing,
                replacement=replacement,
                decided_at=request.as_of,
            )
        )
    return HistorySuggestionResult(
        new_suggestions=selected,
        superseded_suggestions=tuple(superseded),
    )


def _validate_request_boundary(request: HistorySuggestionRequest) -> None:
    _require_text(request.owner_user_id, "owner_user_id")
    if request.target_week_start.weekday() != 0:
        raise ValueError("target_week_start must be a Monday")
    _require_aware_datetime(request.as_of, "as_of")
    for item in request.source_items:
        _validate_source_item(item, request=request)
    for record in request.later_records:
        _validate_later_record(record, request=request)
    for assessment in request.assessments:
        _require_text(assessment.review_scope, "review_scope")
        if not isinstance(assessment.verdict, FollowUpVerdict):
            raise TypeError("unsupported follow-up verdict")


def _validate_source_item(
    item: StableTomorrowPlanItem, *, request: HistorySuggestionRequest
) -> None:
    if item.owner_user_id != request.owner_user_id:
        raise ValueError("source item must belong to the same user")
    if not isinstance(item.record_state, ConfirmedRecordState):
        raise TypeError("source item must come from a confirmed or submitted record")
    if item.field_name != "tomorrow_plan":
        raise ValueError("source item must come from the tomorrow_plan field")
    _require_text(item.item_ref, "item_ref")
    _require_text(item.source_ref, "source_ref")
    _require_text(item.source_version, "source_version")
    _require_aware_datetime(item.recorded_at, "source recorded_at")
    if item.recorded_at > request.as_of:
        raise ValueError("source item cannot be later than as_of")
    source_week_start = request.target_week_start - timedelta(days=7)
    if not source_week_start <= item.recorded_at.date() < request.target_week_start:
        raise ValueError("source item must belong to the source week")
    _validate_hash(item.source_text, item.source_sha256, "source item")
    _require_text(item.item_exact_text, "item_exact_text")
    if item.item_exact_text not in item.source_text:
        raise ValueError("item_exact_text must be an exact source excerpt")


def _validate_later_record(
    record: TrustedPersonalRecord, *, request: HistorySuggestionRequest
) -> None:
    if record.owner_user_id != request.owner_user_id:
        raise ValueError("later record must belong to the same user")
    if not isinstance(record.source_kind, TrustedSourceKind):
        raise TypeError("later record source is not trusted")
    _require_text(record.source_ref, "later source_ref")
    _require_text(record.source_version, "later source_version")
    _require_aware_datetime(record.recorded_at, "later recorded_at")
    if record.recorded_at > request.as_of:
        raise ValueError("later record cannot be later than as_of")
    _validate_hash(record.evidence_text, record.evidence_sha256, "later record")


def _source_item_payload(item: StableTomorrowPlanItem) -> dict[str, str]:
    return {
        "owner_user_id": item.owner_user_id,
        "field_name": item.field_name,
        "item_ref": item.item_ref,
        "source_ref": item.source_ref,
        "source_version": item.source_version,
        "record_state": (
            item.record_state.value
            if isinstance(item.record_state, ConfirmedRecordState)
            else str(item.record_state)
        ),
        "recorded_at": item.recorded_at.isoformat(),
        "source_sha256": item.source_sha256,
        "item_exact_text": item.item_exact_text,
    }


def _later_record_payload(record: TrustedPersonalRecord) -> dict[str, str]:
    return {
        "owner_user_id": record.owner_user_id,
        "source_kind": (
            record.source_kind.value
            if isinstance(record.source_kind, TrustedSourceKind)
            else str(record.source_kind)
        ),
        "source_ref": record.source_ref,
        "source_version": record.source_version,
        "recorded_at": record.recorded_at.isoformat(),
        "evidence_sha256": record.evidence_sha256,
    }


def _validate_hash(text: object, provided: object, label: str) -> None:
    _require_text(text, f"{label} text")
    _require_text(provided, f"{label} sha256")
    if provided != hashlib.sha256(text.encode("utf-8")).hexdigest():
        raise ValueError(f"{label} sha256 does not match exact text")


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} cannot be empty")
    return value


def _require_aware_datetime(value: object, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must include timezone")
    return value


def _sha256_json(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "BoundedFollowUpAssessment",
    "ConfirmedRecordState",
    "FollowUpVerdict",
    "HistorySuggestionRequest",
    "HistorySuggestionResult",
    "StableTomorrowPlanItem",
    "TrustedPersonalRecord",
    "build_follow_up_review_scope",
    "generate_history_suggestions",
]
