from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from hashlib import sha256
from typing import Protocol
from zoneinfo import ZoneInfo

from app.agent2.weekly_plan_domain import create_weekly_plan_batch
from app.agent2.weekly_plan_models import (
    WeeklyPlan,
    WeeklyPlanBatch,
    WeeklyPlanMondayReconciliation,
    WeeklyPlanMondaySnapshot,
    WeeklyPlanRosterMember,
)


class WeeklyPlanCollectionStore(Protocol):
    def save_batch(self, batch: WeeklyPlanBatch) -> WeeklyPlanBatch: ...

    def build_monday_snapshot(
        self,
        *,
        tenant_id: str,
        batch_id: str,
        as_of: datetime,
        deadline_at: datetime,
    ) -> WeeklyPlanMondaySnapshot: ...

    def reconcile_monday_snapshot(
        self, *, tenant_id: str, batch_id: str, as_of: datetime
    ) -> WeeklyPlanMondayReconciliation: ...


@dataclass(frozen=True)
class WeeklyPlanCollectionWindow:
    opens_at: datetime
    deadline_at: datetime
    late_fill_until: datetime | None = None

    def __post_init__(self) -> None:
        for field_name, value in (
            ("opens_at", self.opens_at),
            ("deadline_at", self.deadline_at),
        ):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{field_name} must be timezone-aware")
        if self.deadline_at < self.opens_at:
            raise ValueError("deadline_at must not be before opens_at")
        if self.late_fill_until is not None:
            if (
                self.late_fill_until.tzinfo is None
                or self.late_fill_until.utcoffset() is None
            ):
                raise ValueError("late_fill_until must be timezone-aware")
            if self.late_fill_until < self.deadline_at:
                raise ValueError("late_fill_until must not be before deadline_at")

    def submission_timing(self, submitted_at: datetime) -> str:
        if submitted_at.tzinfo is None or submitted_at.utcoffset() is None:
            raise ValueError("submitted_at must be timezone-aware")
        if submitted_at <= self.deadline_at:
            return "on_time"
        if self.late_fill_until is None or submitted_at <= self.late_fill_until:
            return "late"
        return "closed"


@dataclass(frozen=True)
class WeeklyPlanCollectionSchedule:
    target_week_start: date
    window: WeeklyPlanCollectionWindow


def derive_weekly_plan_collection_schedule(
    *,
    observed_at: datetime,
    timezone_name: str,
    collection_open_hour: int,
    collection_open_minute: int,
    snapshot_hour: int,
    snapshot_minute: int,
) -> WeeklyPlanCollectionSchedule:
    """Derive one Friday-to-Monday cycle from any observation inside it."""

    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    zone = ZoneInfo(timezone_name)
    local = observed_at.astimezone(zone)
    if local.weekday() == 0:
        target_week_start = local.date()
    else:
        days_until_monday = (7 - local.weekday()) % 7
        target_week_start = local.date() + timedelta(days=days_until_monday or 7)
    open_date = target_week_start - timedelta(days=3)
    opens_at = datetime.combine(
        open_date,
        datetime.min.time().replace(
            hour=collection_open_hour,
            minute=collection_open_minute,
        ),
        zone,
    )
    deadline_at = datetime.combine(
        target_week_start,
        datetime.min.time().replace(hour=snapshot_hour, minute=snapshot_minute),
        zone,
    )
    late_fill_until = datetime.combine(
        target_week_start,
        datetime.max.time(),
        zone,
    )
    return WeeklyPlanCollectionSchedule(
        target_week_start=target_week_start,
        window=WeeklyPlanCollectionWindow(
            opens_at=opens_at,
            deadline_at=deadline_at,
            late_fill_until=late_fill_until,
        ),
    )


@dataclass(frozen=True)
class WeeklyPlanCollectionOpening:
    batch: WeeklyPlanBatch
    plans: tuple[WeeklyPlan, ...]
    window: WeeklyPlanCollectionWindow

    @property
    def denominator(self) -> int:
        return len(self.batch.roster)

    @property
    def plan_dates(self) -> tuple[date, ...]:
        return tuple(self.batch.target_week_start + timedelta(days=index) for index in range(6))


@dataclass(frozen=True)
class WeeklyPlanReminderCandidate:
    tenant_id: str
    batch_id: str
    plan_id: str
    target_week_start: date
    recipient_internal_user_id: str
    collection_state: str
    reminder_at: datetime
    idempotency_key: str
    channel: str = "private_chat"


class WeeklyPlanCollectionOrchestrator:
    """Create scoped collection facts; it never dispatches a message."""

    def __init__(self, store: WeeklyPlanCollectionStore) -> None:
        self._store = store

    def open_collection(
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
        batch = create_weekly_plan_batch(
            tenant_id=tenant_id,
            target_week_start=target_week_start,
            roster=selected,
            created_at=window.opens_at,
        )
        persisted_batch = self._store.save_batch(batch)
        return WeeklyPlanCollectionOpening(
            batch=persisted_batch,
            plans=(),
            window=window,
        )

    def calculate_private_reminder_candidates(
        self,
        *,
        opening: WeeklyPlanCollectionOpening,
        current_plans: tuple[WeeklyPlan, ...],
        canary_user_ids: frozenset[str],
        reminder_at: datetime,
        reminder_slot: str = "friday-primary",
    ) -> tuple[WeeklyPlanReminderCandidate, ...]:
        if reminder_at.tzinfo is None or reminder_at.utcoffset() is None:
            raise ValueError("reminder_at must be timezone-aware")
        if reminder_at < opening.window.opens_at:
            raise ValueError("reminder_at must not be before opens_at")
        if reminder_at > opening.window.deadline_at:
            raise ValueError("reminder_at must not be after deadline_at")
        normalized_slot = reminder_slot.strip()
        if not normalized_slot or len(normalized_slot) > 64:
            raise ValueError("reminder_slot is invalid")
        roster_ids = {member.user_id for member in opening.batch.roster}
        plans_by_owner = {
            plan.owner_user_id: plan
            for plan in current_plans
            if plan.tenant_id == opening.batch.tenant_id
            and plan.batch_id == opening.batch.batch_id
        }
        candidates: list[WeeklyPlanReminderCandidate] = []
        for recipient_id in sorted(roster_ids & canary_user_ids):
            plan = plans_by_owner.get(recipient_id)
            if plan is not None and plan.status == "submitted":
                continue
            state = _collection_state(plan) if plan is not None else "unfilled"
            raw_key = (
                f"weekly-plan-reminder:{opening.batch.tenant_id}:"
                f"{opening.batch.batch_id}:{recipient_id}:"
                f"{opening.batch.target_week_start.isoformat()}:{normalized_slot}"
            )
            candidates.append(
                WeeklyPlanReminderCandidate(
                    tenant_id=opening.batch.tenant_id,
                    batch_id=opening.batch.batch_id,
                    plan_id=plan.plan_id if plan is not None else "",
                    target_week_start=opening.batch.target_week_start,
                    recipient_internal_user_id=recipient_id,
                    collection_state=state,
                    reminder_at=reminder_at,
                    idempotency_key=f"weekly-plan-reminder:{sha256(raw_key.encode('utf-8')).hexdigest()}",
                )
            )
        return tuple(candidates)

    def freeze_monday_snapshot(
        self,
        *,
        opening: WeeklyPlanCollectionOpening,
        snapshot_at: datetime,
    ) -> WeeklyPlanMondaySnapshot:
        if snapshot_at.tzinfo is None or snapshot_at.utcoffset() is None:
            raise ValueError("snapshot_at must be timezone-aware")
        if snapshot_at < opening.window.deadline_at:
            raise ValueError("snapshot_at must not be before deadline_at")
        return self._store.build_monday_snapshot(
            tenant_id=opening.batch.tenant_id,
            batch_id=opening.batch.batch_id,
            as_of=snapshot_at,
            deadline_at=opening.window.deadline_at,
        )

    def reconcile_monday_snapshot(
        self,
        *,
        opening: WeeklyPlanCollectionOpening,
        reconciled_at: datetime,
    ) -> WeeklyPlanMondayReconciliation:
        if reconciled_at.tzinfo is None or reconciled_at.utcoffset() is None:
            raise ValueError("reconciled_at must be timezone-aware")
        return self._store.reconcile_monday_snapshot(
            tenant_id=opening.batch.tenant_id,
            batch_id=opening.batch.batch_id,
            as_of=reconciled_at,
        )


def _collection_state(plan: WeeklyPlan) -> str:
    if plan.status == "pending_confirmation":
        return "pending_confirmation"
    if any(day.state != "unfilled" for day in plan.days) or plan.suggestions:
        return "draft"
    return "unfilled"


__all__ = [
    "WeeklyPlanCollectionOpening",
    "WeeklyPlanCollectionOrchestrator",
    "WeeklyPlanCollectionSchedule",
    "WeeklyPlanCollectionStore",
    "WeeklyPlanCollectionWindow",
    "WeeklyPlanReminderCandidate",
    "derive_weekly_plan_collection_schedule",
]
