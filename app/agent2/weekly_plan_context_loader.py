"""Read-only production loader for the authenticated person's weekly-plan context."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from app.agent2.tool_calling.assembly import TrustedContextRequest
from app.agent2.weekly_plan_context import (
    TrustedWeeklyPlanContext,
    WeeklyPlanContextRole,
    build_trusted_weekly_plan_context,
    next_week_start,
)
from app.agent2.weekly_plan_domain import _stable_id
from app.agent2.weekly_plan_models import WeeklyPlan, WeeklyPlanDay

WEEKLY_PLAN_BUSINESS_TIMEZONE = "Asia/Shanghai"


@dataclass(frozen=True)
class OpenWeeklyPlanBatchRef:
    batch_id: str
    target_week_start: date

    def __post_init__(self) -> None:
        if not self.batch_id.strip():
            raise ValueError("opened weekly-plan batch ID is required")
        if self.target_week_start.weekday() != 0:
            raise ValueError("opened weekly-plan batch must start on Monday")


class WeeklyPlanReadStore(Protocol):
    async def load_open_batch_ref_for_owner(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        preferred_target_week_start: date,
    ) -> OpenWeeklyPlanBatchRef | None: ...

    async def load_open_target_week_for_owner(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        preferred_target_week_start: date,
    ) -> date | None: ...

    async def load_plan_by_owner_week(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        target_week_start: date,
        for_update: bool = False,
    ) -> WeeklyPlan | None: ...


class ProductionWeeklyPlanContextLoader:
    """Load trusted weekly context without ever creating production records."""

    def __init__(self, store: WeeklyPlanReadStore) -> None:
        self._store = store

    async def load(
        self,
        request: TrustedContextRequest,
        *,
        opened_target_week_start: date | None = None,
    ) -> TrustedWeeklyPlanContext:
        owner_user_id = str(request.user_id)
        implicit_target_week_start = _implicit_target_week(request)
        opened_batch_ref: OpenWeeklyPlanBatchRef | None = None
        if opened_target_week_start is None:
            opened_batch_ref = await self._load_open_batch_ref(
                tenant_id=request.tenant_id,
                owner_user_id=owner_user_id,
                preferred_target_week_start=implicit_target_week_start,
            )
            opened_target_week_start = (
                opened_batch_ref.target_week_start
                if opened_batch_ref is not None
                else None
            )
        target = _target_week(
            request,
            opened_target_week_start,
            implicit_target_week_start=implicit_target_week_start,
        )
        plan = await self._store.load_plan_by_owner_week(
            tenant_id=request.tenant_id,
            owner_user_id=owner_user_id,
            target_week_start=target,
            for_update=False,
        )
        if plan is None:
            plan = _virtual_empty_plan(
                tenant_id=request.tenant_id,
                owner_user_id=owner_user_id,
                target_week_start=target,
                batch_id=(
                    opened_batch_ref.batch_id
                    if opened_batch_ref is not None
                    and opened_batch_ref.target_week_start == target
                    else None
                ),
            )
        return build_trusted_weekly_plan_context(
            plan=plan,
            authenticated_tenant_id=request.tenant_id,
            authenticated_owner_user_id=owner_user_id,
            target_week_start=target,
            as_of=request.server_now,
        )

    async def _load_open_batch_ref(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        preferred_target_week_start: date,
    ) -> OpenWeeklyPlanBatchRef | None:
        load_ref = getattr(self._store, "load_open_batch_ref_for_owner", None)
        if callable(load_ref):
            result = await load_ref(
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                preferred_target_week_start=preferred_target_week_start,
            )
            if result is not None and not isinstance(result, OpenWeeklyPlanBatchRef):
                raise ValueError("weekly-plan store returned an invalid open batch ref")
            if (
                result is not None
                and result.target_week_start != preferred_target_week_start
            ):
                raise ValueError("weekly-plan store returned a different open target week")
            return result
        target = await self._store.load_open_target_week_for_owner(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            preferred_target_week_start=preferred_target_week_start,
        )
        if target is None:
            return None
        # Compatibility with existing adapters. New adapters should return the
        # authoritative batch ID through load_open_batch_ref_for_owner.
        return OpenWeeklyPlanBatchRef(
            batch_id=_stable_id("weekly-plan-batch", tenant_id, target.isoformat()),
            target_week_start=target,
        )

    async def load_targets(
        self,
        request: TrustedContextRequest,
    ) -> tuple[TrustedWeeklyPlanContext, ...]:
        """Expose collection and per-message natural-next targets without reading text."""

        active = await self.load(request)
        by_plan_id = {
            active.plan_id: _with_routing(
                active,
                roles=("active_collection",),
                natural_next_for_message_indexes=(),
            )
        }
        for index, occurred_at in enumerate(
            request.persisted_message_occurred_ats,
            start=1,
        ):
            target_week_start = next_week_start(
                occurred_at,
                WEEKLY_PLAN_BUSINESS_TIMEZONE,
            )
            plan = await self._store.load_plan_by_owner_week(
                tenant_id=request.tenant_id,
                owner_user_id=str(request.user_id),
                target_week_start=target_week_start,
                for_update=False,
            )
            if plan is None:
                opened_batch_ref = await self._load_open_batch_ref(
                    tenant_id=request.tenant_id,
                    owner_user_id=str(request.user_id),
                    preferred_target_week_start=target_week_start,
                )
                plan = _virtual_empty_plan(
                    tenant_id=request.tenant_id,
                    owner_user_id=str(request.user_id),
                    target_week_start=target_week_start,
                    batch_id=(
                        opened_batch_ref.batch_id
                        if opened_batch_ref is not None
                        else None
                    ),
                )
            natural = build_trusted_weekly_plan_context(
                plan=plan,
                authenticated_tenant_id=request.tenant_id,
                authenticated_owner_user_id=str(request.user_id),
                target_week_start=target_week_start,
                as_of=request.server_now,
                roles=("natural_next",),
                natural_next_for_message_indexes=(index,),
            )
            existing = by_plan_id.get(natural.plan_id)
            if existing is None:
                by_plan_id[natural.plan_id] = natural
                continue
            roles = tuple(dict.fromkeys((*existing.roles, "natural_next")))
            indexes = (*existing.natural_next_for_message_indexes, index)
            by_plan_id[natural.plan_id] = _with_routing(
                existing,
                roles=roles,
                natural_next_for_message_indexes=indexes,
            )
        return tuple(
            sorted(
                by_plan_id.values(),
                key=lambda item: (item.target_week_start, item.plan_id),
            )
        )


def _with_routing(
    context: TrustedWeeklyPlanContext,
    *,
    roles: tuple[WeeklyPlanContextRole, ...],
    natural_next_for_message_indexes: tuple[int, ...],
) -> TrustedWeeklyPlanContext:
    return TrustedWeeklyPlanContext.model_validate(
        {
            **context.model_dump(mode="python"),
            "roles": roles,
            "natural_next_for_message_indexes": natural_next_for_message_indexes,
        }
    )


def _target_week(
    request: TrustedContextRequest,
    opened_target_week_start: date | None,
    implicit_target_week_start: date | None = None,
) -> date:
    if opened_target_week_start is not None:
        if opened_target_week_start.weekday() != 0:
            raise ValueError("opened weekly-plan batch must start on Monday")
        local_date = request.server_now.astimezone(
            ZoneInfo(WEEKLY_PLAN_BUSINESS_TIMEZONE)
        ).date()
        current_monday = local_date - timedelta(days=local_date.weekday())
        # A still-open record may carry the current target week through its
        # Saturday, or the next target week while collection is open.  Older
        # records are stale configuration and must never hijack a new turn.
        if opened_target_week_start not in {
            current_monday,
            current_monday + timedelta(days=7),
        }:
            raise ValueError("opened weekly-plan batch is outside collection window")
        return opened_target_week_start
    return implicit_target_week_start or _implicit_target_week(request)


def _implicit_target_week(request: TrustedContextRequest) -> date:
    local_date = request.server_now.astimezone(
        ZoneInfo(WEEKLY_PLAN_BUSINESS_TIMEZONE)
    ).date()
    # Monday is the documented late-fill window for the week that just began.
    # Keep this deterministic boundary separate from the model's interpretation:
    # the model still decides whether the user is actually editing a weekly plan.
    if local_date.weekday() == 0:
        return local_date
    return next_week_start(request.server_now, WEEKLY_PLAN_BUSINESS_TIMEZONE)


def _virtual_empty_plan(
    *,
    tenant_id: str,
    owner_user_id: str,
    target_week_start: date,
    batch_id: str | None = None,
) -> WeeklyPlan:
    resolved_batch_id = batch_id or _stable_id(
        "weekly-plan-batch", tenant_id, target_week_start.isoformat()
    )
    plan_id = _stable_id(
        "weekly-plan", tenant_id, owner_user_id, target_week_start.isoformat()
    )
    return WeeklyPlan(
        plan_id=plan_id,
        batch_id=resolved_batch_id,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        target_week_start=target_week_start,
        status="collecting",
        version=0,
        days=tuple(
            WeeklyPlanDay(
                day_id=_stable_id("weekly-plan-day", plan_id, offset),
                plan_date=target_week_start + timedelta(days=offset),
            )
            for offset in range(6)
        ),
    )


__all__ = [
    "WEEKLY_PLAN_BUSINESS_TIMEZONE",
    "OpenWeeklyPlanBatchRef",
    "ProductionWeeklyPlanContextLoader",
    "WeeklyPlanReadStore",
]
