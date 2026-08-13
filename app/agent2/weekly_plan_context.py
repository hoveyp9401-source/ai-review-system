"""Trusted, user-scoped model context for the independent weekly-plan domain."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent2.weekly_plan_models import WeeklyPlan
from app.agent2.weekly_plan_suggestions import (
    SuggestionStatus,
    WeeklyPlanSuggestion,
    render_suggestion_prompt,
)

WeeklyPlanDayState = Literal["unfilled", "explicitly_empty", "planned"]
WeeklyPlanContextRole = Literal["active_collection", "natural_next"]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class TrustedWeeklyPlanItem(_FrozenModel):
    item_id: str = Field(min_length=1, max_length=256)
    original_text: str = Field(min_length=1, max_length=4000)
    source: str = Field(min_length=1, max_length=512)


class TrustedWeeklyPlanDay(_FrozenModel):
    day_id: str = Field(min_length=1, max_length=256)
    plan_date: date
    state: WeeklyPlanDayState
    items: tuple[TrustedWeeklyPlanItem, ...] = ()

    @model_validator(mode="after")
    def state_matches_items(self) -> TrustedWeeklyPlanDay:
        if self.state == "planned" and not self.items:
            raise ValueError("a planned weekly-plan day must contain an item")
        if self.state != "planned" and self.items:
            raise ValueError("an unfilled or explicitly empty day cannot contain items")
        item_ids = [item.item_id for item in self.items]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("weekly-plan item IDs must be unique within a day")
        return self


class TrustedWeeklyPlanSuggestion(_FrozenModel):
    """A candidate shown separately from committed plan items."""

    suggestion_id: str = Field(min_length=1, max_length=256)
    status: Literal["available"] = "available"
    source_kind: Literal["user_original_message", "confirmed_record"]
    source_ref: str = Field(min_length=1, max_length=512)
    source_version: str = Field(min_length=1, max_length=256)
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_excerpt: str = Field(min_length=1, max_length=4000)
    prompt: str = Field(min_length=1, max_length=5000)
    is_formal_plan_item: Literal[False] = False


class TrustedWeeklyPlanContext(_FrozenModel):
    plan_id: str = Field(min_length=1, max_length=256)
    batch_id: str = Field(min_length=1, max_length=256)
    tenant_id: str = Field(min_length=1, max_length=128)
    owner_user_id: str = Field(min_length=1, max_length=256)
    target_week_start: date
    version: int = Field(ge=0)
    status: Literal[
        "draft",
        "collecting",
        "pending_confirmation",
        "submitted",
        "cancelled",
    ]
    days: tuple[TrustedWeeklyPlanDay, ...]
    suggestions: tuple[TrustedWeeklyPlanSuggestion, ...] = ()
    roles: tuple[WeeklyPlanContextRole, ...] = ()
    natural_next_for_message_indexes: tuple[int, ...] = ()

    @model_validator(mode="after")
    def exact_week_and_stable_ids(self) -> TrustedWeeklyPlanContext:
        if self.target_week_start.weekday() != 0:
            raise ValueError("weekly-plan target week must start on Monday")
        expected_dates = tuple(
            self.target_week_start + timedelta(days=offset) for offset in range(6)
        )
        if tuple(day.plan_date for day in self.days) != expected_dates:
            raise ValueError("weekly-plan context must contain exact Monday-to-Saturday dates")
        day_ids = [day.day_id for day in self.days]
        if len(day_ids) != len(set(day_ids)):
            raise ValueError("weekly-plan day IDs must be unique")
        item_ids = [item.item_id for day in self.days for item in day.items]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("weekly-plan item IDs must be unique")
        suggestion_ids = [item.suggestion_id for item in self.suggestions]
        if len(suggestion_ids) != len(set(suggestion_ids)):
            raise ValueError("weekly-plan suggestion IDs must be unique")
        if len(self.roles) != len(set(self.roles)):
            raise ValueError("weekly-plan context roles must be unique")
        if (
            len(self.natural_next_for_message_indexes)
            != len(set(self.natural_next_for_message_indexes))
            or any(index < 1 for index in self.natural_next_for_message_indexes)
        ):
            raise ValueError("weekly-plan message indexes must be unique and positive")
        if self.natural_next_for_message_indexes and "natural_next" not in self.roles:
            raise ValueError("weekly-plan message indexes require the natural-next role")
        return self

    def model_payload(self) -> dict[str, Any]:
        """Return only formal plan data and bounded candidate evidence."""

        return {
            "plan_id": self.plan_id,
            "batch_id": self.batch_id,
            "target_week_start": self.target_week_start.isoformat(),
            "version": self.version,
            "status": self.status,
            "days": [
                {
                    "day_id": day.day_id,
                    "plan_date": day.plan_date.isoformat(),
                    "state": day.state,
                    "items": [item.model_dump(mode="json") for item in day.items],
                }
                for day in self.days
            ],
            "suggestions": [item.model_dump(mode="json") for item in self.suggestions],
            "roles": list(self.roles),
            "natural_next_for_message_indexes": list(
                self.natural_next_for_message_indexes
            ),
            "provenance": "server_weekly_plan",
        }


def next_week_start(now: datetime, timezone: str) -> date:
    """Return next Monday from the authoritative user-local date."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    local_date = now.astimezone(ZoneInfo(timezone)).date()
    this_monday = local_date - timedelta(days=local_date.weekday())
    return this_monday + timedelta(days=7)


def build_trusted_weekly_plan_context(
    *,
    plan: WeeklyPlan,
    authenticated_tenant_id: str,
    authenticated_owner_user_id: str,
    target_week_start: date,
    suggestions: Iterable[WeeklyPlanSuggestion] | None = None,
    as_of: datetime | None = None,
    roles: Iterable[WeeklyPlanContextRole] = (),
    natural_next_for_message_indexes: Iterable[int] = (),
) -> TrustedWeeklyPlanContext:
    """Bind a domain plan to the authenticated person and exact open batch."""

    if plan.tenant_id != authenticated_tenant_id:
        raise ValueError("weekly plan must match the authenticated tenant")
    if plan.owner_user_id != authenticated_owner_user_id:
        raise ValueError("weekly plan must match the authenticated owner")
    if plan.target_week_start != target_week_start:
        raise ValueError("weekly plan must match the exact target week")

    candidate_source = plan.suggestions if suggestions is None else tuple(suggestions)
    if as_of is not None and (as_of.tzinfo is None or as_of.utcoffset() is None):
        raise ValueError("weekly-plan suggestion as_of must be timezone-aware")
    trusted_suggestions: list[TrustedWeeklyPlanSuggestion] = []
    for suggestion in candidate_source:
        if suggestion.owner_user_id != authenticated_owner_user_id:
            raise ValueError("weekly-plan suggestion must match the authenticated owner")
        if suggestion.target_week_start != target_week_start:
            raise ValueError("weekly-plan suggestion must match the exact target week")
        if suggestion.status is not SuggestionStatus.AVAILABLE:
            continue
        if as_of is not None and as_of >= suggestion.expires_at:
            continue
        trusted_suggestions.append(
            TrustedWeeklyPlanSuggestion(
                suggestion_id=suggestion.suggestion_id,
                source_kind=suggestion.source_kind.value,
                source_ref=suggestion.source_ref,
                source_version=suggestion.source_version,
                evidence_sha256=suggestion.evidence_sha256,
                evidence_excerpt=suggestion.matter_excerpt,
                prompt=render_suggestion_prompt(suggestion),
            )
        )

    return TrustedWeeklyPlanContext(
        plan_id=plan.plan_id,
        batch_id=plan.batch_id,
        tenant_id=plan.tenant_id,
        owner_user_id=plan.owner_user_id,
        target_week_start=plan.target_week_start,
        version=plan.version,
        status=plan.status,
        days=tuple(
            TrustedWeeklyPlanDay(
                day_id=day.day_id,
                plan_date=day.plan_date,
                state=day.state,
                items=tuple(
                    TrustedWeeklyPlanItem(
                        item_id=item.item_id,
                        original_text=item.original_text,
                        source=item.source,
                    )
                    for item in day.items
                ),
            )
            for day in plan.days
        ),
        suggestions=tuple(trusted_suggestions),
        roles=tuple(roles),
        natural_next_for_message_indexes=tuple(
            natural_next_for_message_indexes
        ),
    )


__all__ = [
    "TrustedWeeklyPlanContext",
    "TrustedWeeklyPlanDay",
    "TrustedWeeklyPlanItem",
    "TrustedWeeklyPlanSuggestion",
    "WeeklyPlanContextRole",
    "build_trusted_weekly_plan_context",
    "next_week_start",
]
