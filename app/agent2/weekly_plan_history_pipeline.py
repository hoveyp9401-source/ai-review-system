"""Production seam for turning trusted Daily history into optional suggestions.

The public interface intentionally cannot write a formal weekly-plan day or item.
It reads one authenticated owner's stable Daily records, asks an injected semantic
reviewer a bounded question, and persists only grounded suggestion state.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.agent2.weekly_plan_history_suggestions import (
    BoundedFollowUpAssessment,
    FollowUpVerdict,
    HistorySuggestionRequest,
    HistorySuggestionResult,
    StableTomorrowPlanItem,
    TrustedPersonalRecord,
    build_follow_up_review_scope,
    generate_history_suggestions,
)
from app.agent2.weekly_plan_suggestions import (
    SuggestionStatus,
    WeeklyPlanSuggestion,
)

MAX_REVIEW_ITEMS = 6
MAX_LATER_RECORDS_PER_ITEM = 12
MAX_REVIEW_EVIDENCE_CHARACTERS = 24_000


@dataclass(frozen=True)
class HistorySuggestionRefreshRequest:
    tenant_id: str
    owner_user_id: str
    target_week_start: date
    as_of: datetime


@dataclass(frozen=True)
class TrustedHistoryWindow:
    source_items: tuple[StableTomorrowPlanItem, ...]
    later_records: tuple[TrustedPersonalRecord, ...]


@dataclass(frozen=True)
class HistoryFollowUpReviewInput:
    review_scope: str
    source_item: StableTomorrowPlanItem
    later_records: tuple[TrustedPersonalRecord, ...]


@dataclass(frozen=True)
class HistorySuggestionRefreshResult:
    persisted_suggestions: tuple[WeeklyPlanSuggestion, ...]
    superseded_suggestions: tuple[WeeklyPlanSuggestion, ...]
    reviewed_item_count: int
    skipped_unbounded_count: int


class TrustedDailyHistorySource(Protocol):
    async def load_history(
        self, request: HistorySuggestionRefreshRequest
    ) -> TrustedHistoryWindow: ...


class HistoryFollowUpReviewer(Protocol):
    async def assess(
        self, review_inputs: tuple[HistoryFollowUpReviewInput, ...]
    ) -> Sequence[BoundedFollowUpAssessment]: ...


class _FollowUpAssessmentPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_scope: str = Field(min_length=1, max_length=128)
    verdict: FollowUpVerdict


class _FollowUpReviewEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assessments: list[_FollowUpAssessmentPayload] = Field(default_factory=list)


class LLMHistoryFollowUpReviewer:
    """Bounded semantic reviewer; it cannot create or edit a formal plan."""

    def __init__(
        self,
        client,
        *,
        model: str | None = None,
        thinking_enabled: bool = False,
    ) -> None:
        self._client = client
        self._model = model
        self._thinking_enabled = thinking_enabled

    async def assess(
        self, review_inputs: tuple[HistoryFollowUpReviewInput, ...]
    ) -> Sequence[BoundedFollowUpAssessment]:
        if not review_inputs:
            return ()
        payload = {
            "review_items": [
                {
                    "review_scope": item.review_scope,
                    "source_tomorrow_plan": {
                        "exact_text": item.source_item.item_exact_text,
                        "recorded_at": item.source_item.recorded_at.isoformat(),
                        "source_ref": item.source_item.source_ref,
                        "source_version": item.source_item.source_version,
                    },
                    "later_confirmed_records": [
                        {
                            "source_ref": record.source_ref,
                            "source_version": record.source_version,
                            "recorded_at": record.recorded_at.isoformat(),
                            "exact_record": record.evidence_text,
                        }
                        for record in item.later_records
                    ],
                }
                for item in review_inputs
            ]
        }
        raw = await self._client.complete_json(
            system_prompt=(
                "你是 Agent2 的隔离语义复核器。逐项判断来源日报的明日计划在其后"
                "可信本人日报中是否有明确后续。只能返回 JSON。明确看到完成、推进、"
                "取消、延期或同一事项的后续时返回 followed_up；有后续日报且完全没有"
                "找到该事项记录时返回 no_later_record；相似、矛盾、证据不足或不能确定"
                "一律返回 uncertain。不得把沟通、跟进改写成完成，不得生成计划项、"
                "不得给用户回复。每个 review_scope 必须原样返回且恰好一次。"
            ),
            user_prompt=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            model=self._model,
            thinking_enabled=self._thinking_enabled,
            max_retries=0,
            max_tokens=1600,
        )
        try:
            decoded = json.loads(raw)
            envelope = _FollowUpReviewEnvelope.model_validate(decoded)
        except (TypeError, ValueError, json.JSONDecodeError):
            return ()
        return tuple(
            BoundedFollowUpAssessment(
                review_scope=item.review_scope,
                verdict=item.verdict,
            )
            for item in envelope.assessments
        )


class HistorySuggestionStore(Protocol):
    async def load_available(
        self, request: HistorySuggestionRefreshRequest
    ) -> tuple[WeeklyPlanSuggestion, ...]: ...

    async def persist(
        self,
        request: HistorySuggestionRefreshRequest,
        result: HistorySuggestionResult,
    ) -> HistorySuggestionResult: ...


class WeeklyPlanHistorySuggestionService:
    """Refresh suggestions through one owner-scoped, fail-closed interface."""

    def __init__(
        self,
        *,
        history_source: TrustedDailyHistorySource,
        suggestion_store: HistorySuggestionStore,
        reviewer: HistoryFollowUpReviewer,
    ) -> None:
        self._history_source = history_source
        self._suggestion_store = suggestion_store
        self._reviewer = reviewer

    async def refresh(
        self, request: HistorySuggestionRefreshRequest
    ) -> HistorySuggestionRefreshResult:
        _validate_refresh_request(request)
        history = await self._history_source.load_history(request)
        review_inputs, skipped_unbounded = _bounded_review_inputs(
            request=request,
            history=history,
        )
        raw_assessments = (
            await self._reviewer.assess(review_inputs) if review_inputs else ()
        )
        assessments = _ground_assessments(review_inputs, raw_assessments)
        existing = await self._suggestion_store.load_available(request)
        generated = generate_history_suggestions(
            HistorySuggestionRequest(
                owner_user_id=request.owner_user_id,
                target_week_start=request.target_week_start,
                as_of=request.as_of,
                source_items=tuple(item.source_item for item in review_inputs),
                later_records=history.later_records,
                assessments=assessments,
                existing_suggestions=existing,
            )
        )
        persisted = await self._suggestion_store.persist(request, generated)
        return HistorySuggestionRefreshResult(
            persisted_suggestions=persisted.new_suggestions,
            superseded_suggestions=persisted.superseded_suggestions,
            reviewed_item_count=len(review_inputs),
            skipped_unbounded_count=skipped_unbounded,
        )


class InMemoryHistorySuggestionGateway:
    """Test adapter implementing both read and persistence seams."""

    def __init__(
        self,
        *,
        history: TrustedHistoryWindow,
        plan_exists: bool,
        existing_suggestions: tuple[WeeklyPlanSuggestion, ...] = (),
    ) -> None:
        self._history = history
        self._plan_exists = plan_exists
        self._suggestions = list(existing_suggestions)

    @property
    def available_suggestions(self) -> tuple[WeeklyPlanSuggestion, ...]:
        return tuple(
            value
            for value in self._suggestions
            if value.status is SuggestionStatus.AVAILABLE
        )

    async def load_history(
        self, request: HistorySuggestionRefreshRequest
    ) -> TrustedHistoryWindow:
        del request
        return self._history

    async def load_available(
        self, request: HistorySuggestionRefreshRequest
    ) -> tuple[WeeklyPlanSuggestion, ...]:
        return tuple(
            value
            for value in self.available_suggestions
            if value.owner_user_id == request.owner_user_id
            and value.target_week_start == request.target_week_start
        )

    async def persist(
        self,
        request: HistorySuggestionRefreshRequest,
        result: HistorySuggestionResult,
    ) -> HistorySuggestionResult:
        if not self._plan_exists:
            return HistorySuggestionResult(new_suggestions=())
        superseded_by_id = {
            value.suggestion_id: value for value in result.superseded_suggestions
        }
        self._suggestions = [
            superseded_by_id.get(value.suggestion_id, value)
            for value in self._suggestions
        ]
        existing_ids = {value.suggestion_id for value in self._suggestions}
        persisted_new: list[WeeklyPlanSuggestion] = []
        for value in result.new_suggestions:
            if (
                value.owner_user_id != request.owner_user_id
                or value.target_week_start != request.target_week_start
            ):
                continue
            if value.suggestion_id not in existing_ids:
                self._suggestions.append(value)
                existing_ids.add(value.suggestion_id)
            persisted_new.append(value)
        return HistorySuggestionResult(
            new_suggestions=tuple(persisted_new),
            superseded_suggestions=tuple(result.superseded_suggestions),
        )


def _bounded_review_inputs(
    *,
    request: HistorySuggestionRefreshRequest,
    history: TrustedHistoryWindow,
) -> tuple[tuple[HistoryFollowUpReviewInput, ...], int]:
    ordered_sources = sorted(
        history.source_items,
        key=lambda value: (
            value.recorded_at,
            value.source_ref,
            value.source_version,
            value.item_ref,
        ),
        reverse=True,
    )
    selected: list[HistoryFollowUpReviewInput] = []
    skipped = max(0, len(ordered_sources) - MAX_REVIEW_ITEMS)
    for source_item in ordered_sources[:MAX_REVIEW_ITEMS]:
        later_records = tuple(
            sorted(
                (
                    record
                    for record in history.later_records
                    if record.recorded_at > source_item.recorded_at
                    and record.recorded_at <= request.as_of
                ),
                key=lambda value: (
                    value.recorded_at,
                    value.source_ref,
                    value.source_version,
                ),
            )
        )
        evidence_size = len(source_item.source_text) + sum(
            len(record.evidence_text) for record in later_records
        )
        if (
            not later_records
            or len(later_records) > MAX_LATER_RECORDS_PER_ITEM
            or evidence_size > MAX_REVIEW_EVIDENCE_CHARACTERS
        ):
            if later_records:
                skipped += 1
            continue
        selected.append(
            HistoryFollowUpReviewInput(
                review_scope=build_follow_up_review_scope(
                    owner_user_id=request.owner_user_id,
                    source_item=source_item,
                    later_records=later_records,
                    as_of=request.as_of,
                ),
                source_item=source_item,
                later_records=later_records,
            )
        )
    return tuple(selected), skipped


def _ground_assessments(
    review_inputs: tuple[HistoryFollowUpReviewInput, ...],
    raw_assessments: Sequence[BoundedFollowUpAssessment],
) -> tuple[BoundedFollowUpAssessment, ...]:
    allowed_scopes = {value.review_scope for value in review_inputs}
    by_scope: dict[str, list[BoundedFollowUpAssessment]] = {}
    for assessment in raw_assessments:
        if (
            isinstance(assessment, BoundedFollowUpAssessment)
            and assessment.review_scope in allowed_scopes
            and isinstance(assessment.verdict, FollowUpVerdict)
        ):
            by_scope.setdefault(assessment.review_scope, []).append(assessment)
    # Missing, duplicated, conflicting, or unknown answers all fail closed.
    return tuple(values[0] for values in by_scope.values() if len(values) == 1)


def _validate_refresh_request(request: HistorySuggestionRefreshRequest) -> None:
    if not request.tenant_id.strip() or not request.owner_user_id.strip():
        raise ValueError("tenant_id and owner_user_id are required")
    if request.target_week_start.weekday() != 0:
        raise ValueError("target_week_start must be a Monday")
    if request.as_of.tzinfo is None or request.as_of.utcoffset() is None:
        raise ValueError("as_of must include timezone")


__all__ = [
    "HistoryFollowUpReviewInput",
    "HistorySuggestionRefreshRequest",
    "HistorySuggestionRefreshResult",
    "InMemoryHistorySuggestionGateway",
    "LLMHistoryFollowUpReviewer",
    "TrustedHistoryWindow",
    "WeeklyPlanHistorySuggestionService",
]
