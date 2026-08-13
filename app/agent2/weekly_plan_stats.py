from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.agent2.weekly_plan_models import (
    WeeklyPlanMondayReconciliation,
    WeeklyPlanMondaySnapshot,
)


class AsyncWeeklyPlanMondayStatsStore(Protocol):
    async def load_monday_snapshot(
        self, *, tenant_id: str, batch_id: str
    ) -> WeeklyPlanMondaySnapshot | None: ...

    async def reconcile_monday_snapshot(
        self, *, tenant_id: str, batch_id: str, as_of: datetime
    ) -> WeeklyPlanMondayReconciliation: ...


@dataclass(frozen=True)
class WeeklyPlanMondayStats:
    immutable_snapshot: WeeklyPlanMondaySnapshot
    reconciliation: WeeklyPlanMondayReconciliation


class WeeklyPlanMondayStatsReader:
    """Read the frozen Monday baseline together with a current comparison."""

    def __init__(self, store: AsyncWeeklyPlanMondayStatsStore) -> None:
        self._store = store

    async def read(
        self,
        *,
        tenant_id: str,
        batch_id: str,
        as_of: datetime,
    ) -> WeeklyPlanMondayStats:
        if not tenant_id.strip():
            raise ValueError("tenant_id is required")
        if not batch_id.strip():
            raise ValueError("batch_id is required")
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        snapshot = await self._store.load_monday_snapshot(
            tenant_id=tenant_id,
            batch_id=batch_id,
        )
        if snapshot is None:
            raise ValueError("monday_snapshot_not_found")
        if snapshot.tenant_id != tenant_id or snapshot.batch_id != batch_id:
            raise ValueError("monday_snapshot_scope_mismatch")
        if as_of < snapshot.as_of:
            raise ValueError("monday_stats_read_before_snapshot")
        reconciliation = await self._store.reconcile_monday_snapshot(
            tenant_id=tenant_id,
            batch_id=batch_id,
            as_of=as_of,
        )
        if (
            reconciliation.tenant_id != tenant_id
            or reconciliation.batch_id != batch_id
            or reconciliation.snapshot_id != snapshot.snapshot_id
        ):
            raise ValueError("monday_reconciliation_scope_mismatch")
        return WeeklyPlanMondayStats(
            immutable_snapshot=snapshot,
            reconciliation=reconciliation,
        )


__all__ = ["WeeklyPlanMondayStats", "WeeklyPlanMondayStatsReader"]
