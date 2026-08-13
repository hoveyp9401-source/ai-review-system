from __future__ import annotations

from datetime import date
from typing import Protocol

from app.agent2.weekly_plan_collection import (
    WeeklyPlanCollectionOpening,
    WeeklyPlanCollectionWindow,
)
from app.agent2.weekly_plan_domain import create_weekly_plan, create_weekly_plan_batch
from app.agent2.weekly_plan_models import (
    WeeklyPlan,
    WeeklyPlanBatch,
    WeeklyPlanRosterMember,
)
from app.agent2.weekly_plan_reminder_outbox import (
    WeeklyPlanReminderOutbox,
    create_weekly_plan_reminder_outbox,
)


class AsyncWeeklyPlanCollectionStore(Protocol):
    async def open_or_load_batch(self, batch: WeeklyPlanBatch) -> WeeklyPlanBatch: ...

    async def load_plan_by_owner_week(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        target_week_start: date,
        for_update: bool = False,
    ) -> WeeklyPlan | None: ...

    async def create_plan(self, plan: WeeklyPlan) -> WeeklyPlan: ...

    async def build_monday_snapshot(self, **kwargs): ...


class AsyncWeeklyPlanReminderOutboxStore(Protocol):
    async def enqueue(
        self, row: WeeklyPlanReminderOutbox
    ) -> WeeklyPlanReminderOutbox: ...


class SqlWeeklyPlanCollectionOrchestrator:
    """Small async entry point for opening a frozen canary collection."""

    def __init__(
        self,
        store: AsyncWeeklyPlanCollectionStore,
        *,
        outbox_store: AsyncWeeklyPlanReminderOutboxStore | None = None,
    ) -> None:
        self._store = store
        self._outbox_store = outbox_store

    async def open_collection(
        self,
        *,
        tenant_id: str,
        target_week_start: date,
        source_roster: tuple[WeeklyPlanRosterMember, ...],
        canary_user_ids: frozenset[str],
        window: WeeklyPlanCollectionWindow,
    ) -> WeeklyPlanCollectionOpening:
        selected = tuple(
            member for member in source_roster if member.user_id in canary_user_ids
        )
        if not selected:
            raise ValueError("weekly_plan_canary_roster_empty")
        requested = create_weekly_plan_batch(
            tenant_id=tenant_id,
            target_week_start=target_week_start,
            roster=selected,
            created_at=window.opens_at,
        )
        batch = await self._store.open_or_load_batch(requested)
        plans: list[WeeklyPlan] = []
        for member in batch.roster:
            plan = await self._store.load_plan_by_owner_week(
                tenant_id=batch.tenant_id,
                owner_user_id=member.user_id,
                target_week_start=batch.target_week_start,
            )
            if plan is None:
                initial = create_weekly_plan(
                    batch=batch,
                    owner_user_id=member.user_id,
                    created_at=batch.created_at,
                )
                await self._store.create_plan(initial)
                plan = await self._store.load_plan_by_owner_week(
                    tenant_id=batch.tenant_id,
                    owner_user_id=member.user_id,
                    target_week_start=batch.target_week_start,
                )
                if plan is None:
                    raise ValueError("weekly_plan_create_not_visible")
            if plan.batch_id != batch.batch_id:
                raise ValueError("weekly_plan_existing_plan_batch_mismatch")
            plans.append(plan)
        return WeeklyPlanCollectionOpening(
            batch=batch,
            plans=tuple(plans),
            window=window,
        )

    async def enqueue_private_reminders(
        self,
        *,
        opening: WeeklyPlanCollectionOpening,
        canary_user_ids: frozenset[str],
        reminder_at,
        created_at,
        reminder_slot: str = "sunday-primary",
    ) -> tuple[WeeklyPlanReminderOutbox, ...]:
        if self._outbox_store is None:
            raise ValueError("weekly_plan_reminder_outbox_not_configured")
        # Candidate selection stays in the deterministic domain orchestrator;
        # this adapter merely persists the result.
        from app.agent2.weekly_plan_collection import WeeklyPlanCollectionOrchestrator

        candidates = WeeklyPlanCollectionOrchestrator(
            _NoopSyncCollectionStore()
        ).calculate_private_reminder_candidates(
            opening=opening,
            current_plans=opening.plans,
            canary_user_ids=canary_user_ids,
            reminder_at=reminder_at,
            reminder_slot=reminder_slot,
        )
        rows = []
        for candidate in candidates:
            row = create_weekly_plan_reminder_outbox(
                candidate,
                created_at=created_at,
            )
            rows.append(await self._outbox_store.enqueue(row))
        return tuple(rows)

    async def freeze_monday_snapshot(
        self,
        *,
        opening: WeeklyPlanCollectionOpening,
        snapshot_at,
    ):
        if snapshot_at.tzinfo is None or snapshot_at.utcoffset() is None:
            raise ValueError("snapshot_at must be timezone-aware")
        if snapshot_at < opening.window.deadline_at:
            raise ValueError("snapshot_at must not be before deadline_at")
        return await self._store.build_monday_snapshot(
            tenant_id=opening.batch.tenant_id,
            batch_id=opening.batch.batch_id,
            as_of=snapshot_at,
            deadline_at=opening.window.deadline_at,
        )


class _NoopSyncCollectionStore:
    """Candidate calculation does not call the snapshot store methods."""

    def save_batch(self, batch):  # pragma: no cover - defensive only
        raise AssertionError("not used")

    def build_monday_snapshot(self, **kwargs):  # pragma: no cover
        raise AssertionError("not used")

    def reconcile_monday_snapshot(self, **kwargs):  # pragma: no cover
        raise AssertionError("not used")


__all__ = ["SqlWeeklyPlanCollectionOrchestrator"]
