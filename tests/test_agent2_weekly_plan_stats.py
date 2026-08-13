from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql

from app.agent2.weekly_plan_domain import create_weekly_plan, create_weekly_plan_batch
from app.agent2.weekly_plan_models import WeeklyPlanRosterMember
from app.agent2.weekly_plan_stats import WeeklyPlanMondayStatsReader
from app.agent2.weekly_plan_store import (
    InMemoryWeeklyPlanStore,
    SqlWeeklyPlanStore,
    _monday_snapshot_row,
)


DEADLINE = datetime(2026, 8, 17, 1, 0, tzinfo=timezone.utc)
READ_AT = DEADLINE + timedelta(hours=2)


def _member(user_id: str) -> WeeklyPlanRosterMember:
    return WeeklyPlanRosterMember(user_id=user_id, display_name="测试用户")


class _AsyncMemoryStatsStore:
    def __init__(self, memory: InMemoryWeeklyPlanStore) -> None:
        self._memory = memory

    async def load_monday_snapshot(self, **kwargs):
        return self._memory.load_monday_snapshot(**kwargs)

    async def reconcile_monday_snapshot(self, **kwargs):
        return self._memory.reconcile_monday_snapshot(**kwargs)


class _RowsResult:
    def __init__(self, rows=()) -> None:
        self._rows = list(rows)

    class _Mappings:
        def __init__(self, rows) -> None:
            self._rows = rows

        def one_or_none(self):
            if not self._rows:
                return None
            assert len(self._rows) == 1
            return self._rows[0]

    def mappings(self):
        return self._Mappings(self._rows)


class _ReadOnlySession:
    def __init__(self, result) -> None:
        self.result = result
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return self.result


@pytest.mark.asyncio
async def test_stats_read_returns_frozen_monday_snapshot_and_current_late_delta() -> None:
    batch = create_weekly_plan_batch(
        tenant_id="tenant-a",
        target_week_start=date(2026, 8, 17),
        roster=(_member("user-a"),),
        created_at=DEADLINE - timedelta(days=3),
    )
    memory = InMemoryWeeklyPlanStore()
    memory.save_batch(batch)
    frozen = memory.build_monday_snapshot(
        tenant_id="tenant-a",
        batch_id=batch.batch_id,
        as_of=DEADLINE,
        deadline_at=DEADLINE,
    )
    late_plan = replace(
        create_weekly_plan(
            batch=batch,
            owner_user_id="user-a",
            created_at=DEADLINE + timedelta(minutes=1),
        ),
        status="submitted",
        version=1,
        submitted_at=DEADLINE + timedelta(minutes=10),
        updated_at=DEADLINE + timedelta(minutes=10),
    )
    memory.save_plan(late_plan)

    reader = WeeklyPlanMondayStatsReader(
        _AsyncMemoryStatsStore(memory)
    )
    result = await reader.read(
        tenant_id="tenant-a",
        batch_id=batch.batch_id,
        as_of=READ_AT,
    )
    replay = await reader.read(
        tenant_id="tenant-a",
        batch_id=batch.batch_id,
        as_of=READ_AT,
    )

    assert result.immutable_snapshot == frozen
    assert result.immutable_snapshot.submitted_count == 0
    assert result.immutable_snapshot.unfilled_count == 1
    assert result.reconciliation.snapshot_id == frozen.snapshot_id
    assert result.reconciliation.current_submitted_count == 1
    assert result.reconciliation.late_submitted_count == 1
    assert result.reconciliation.changed_after_snapshot_count == 1
    assert replay == result


@pytest.mark.asyncio
async def test_later_plan_change_never_changes_an_already_frozen_snapshot() -> None:
    batch = create_weekly_plan_batch(
        tenant_id="tenant-a",
        target_week_start=date(2026, 8, 17),
        roster=(_member("user-a"),),
        created_at=DEADLINE - timedelta(days=3),
    )
    memory = InMemoryWeeklyPlanStore()
    memory.save_batch(batch)
    memory.build_monday_snapshot(
        tenant_id="tenant-a",
        batch_id=batch.batch_id,
        as_of=DEADLINE,
        deadline_at=DEADLINE,
    )
    reader = WeeklyPlanMondayStatsReader(_AsyncMemoryStatsStore(memory))
    before = await reader.read(
        tenant_id="tenant-a", batch_id=batch.batch_id, as_of=DEADLINE
    )

    changed = replace(
        create_weekly_plan(batch=batch, owner_user_id="user-a", created_at=READ_AT),
        status="submitted",
        version=2,
        submitted_at=READ_AT,
        updated_at=READ_AT,
    )
    memory.save_plan(changed)
    after = await reader.read(
        tenant_id="tenant-a", batch_id=batch.batch_id, as_of=READ_AT
    )

    assert after.immutable_snapshot == before.immutable_snapshot
    assert after.immutable_snapshot.rows[0].plan_status == "unfilled"
    assert before.reconciliation.changed_after_snapshot_count == 0
    assert after.reconciliation.changed_after_snapshot_count == 1


@pytest.mark.asyncio
async def test_stats_read_keeps_unfilled_draft_submitted_late_and_closed_window_distinct() -> None:
    batch = create_weekly_plan_batch(
        tenant_id="tenant-a",
        target_week_start=date(2026, 8, 17),
        roster=tuple(
            _member(user_id)
            for user_id in ("empty", "draft", "on-time", "late", "closed")
        ),
        created_at=DEADLINE - timedelta(days=3),
    )
    memory = InMemoryWeeklyPlanStore()
    memory.save_batch(batch)
    draft = replace(
        create_weekly_plan(batch=batch, owner_user_id="draft", created_at=DEADLINE),
        status="pending_confirmation",
        version=1,
    )
    on_time = replace(
        create_weekly_plan(batch=batch, owner_user_id="on-time", created_at=DEADLINE),
        status="submitted",
        version=1,
        submitted_at=DEADLINE - timedelta(minutes=1),
    )
    memory.save_plan(draft)
    memory.save_plan(on_time)
    memory.build_monday_snapshot(
        tenant_id="tenant-a",
        batch_id=batch.batch_id,
        as_of=DEADLINE,
        deadline_at=DEADLINE,
    )
    for owner_user_id, submitted_at in (
        ("late", DEADLINE + timedelta(minutes=10)),
        ("closed", DEADLINE + timedelta(days=1)),
    ):
        memory.save_plan(
            replace(
                create_weekly_plan(
                    batch=batch,
                    owner_user_id=owner_user_id,
                    created_at=submitted_at,
                ),
                status="submitted",
                version=1,
                submitted_at=submitted_at,
            )
        )

    result = await WeeklyPlanMondayStatsReader(_AsyncMemoryStatsStore(memory)).read(
        tenant_id="tenant-a",
        batch_id=batch.batch_id,
        as_of=DEADLINE + timedelta(days=1),
    )

    frozen = result.immutable_snapshot
    current = result.reconciliation
    assert (frozen.unfilled_count, frozen.draft_count, frozen.submitted_count) == (
        3,
        1,
        1,
    )
    assert current.current_submitted_count == 3
    assert current.late_submitted_count == 1
    assert current.closed_window_submitted_count == 1
    assert {
        row.user_id: row.submission_timing for row in current.rows
    } == {
        "empty": "not_submitted",
        "draft": "not_submitted",
        "on-time": "on_time",
        "late": "late",
        "closed": "closed_window",
    }


@pytest.mark.asyncio
async def test_sql_snapshot_load_is_one_exact_tenant_batch_read_and_never_writes() -> None:
    batch = create_weekly_plan_batch(
        tenant_id="tenant-a",
        target_week_start=date(2026, 8, 17),
        roster=(_member("user-a"),),
        created_at=DEADLINE - timedelta(days=3),
    )
    memory = InMemoryWeeklyPlanStore()
    memory.save_batch(batch)
    frozen = memory.build_monday_snapshot(
        tenant_id="tenant-a",
        batch_id=batch.batch_id,
        as_of=DEADLINE,
        deadline_at=DEADLINE,
    )
    session = _ReadOnlySession(_RowsResult((_monday_snapshot_row(frozen),)))

    loaded = await SqlWeeklyPlanStore(session).load_monday_snapshot(
        tenant_id="tenant-a", batch_id=batch.batch_id
    )

    assert loaded == frozen
    assert len(session.statements) == 1
    compiled = session.statements[0].compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert sql.startswith("SELECT agent2_weekly_plan_monday_snapshots")
    assert "agent2_weekly_plan_monday_snapshots.tenant_id =" in sql
    assert "agent2_weekly_plan_monday_snapshots.batch_id =" in sql
    assert set(compiled.params.values()) == {"tenant-a", UUID(batch.batch_id)}


@pytest.mark.asyncio
async def test_stats_read_rejects_a_read_time_before_the_frozen_baseline() -> None:
    batch = create_weekly_plan_batch(
        tenant_id="tenant-a",
        target_week_start=date(2026, 8, 17),
        roster=(_member("user-a"),),
        created_at=DEADLINE - timedelta(days=3),
    )
    memory = InMemoryWeeklyPlanStore()
    memory.save_batch(batch)
    memory.build_monday_snapshot(
        tenant_id="tenant-a",
        batch_id=batch.batch_id,
        as_of=DEADLINE,
        deadline_at=DEADLINE,
    )

    with pytest.raises(ValueError, match="monday_stats_read_before_snapshot"):
        await WeeklyPlanMondayStatsReader(_AsyncMemoryStatsStore(memory)).read(
            tenant_id="tenant-a",
            batch_id=batch.batch_id,
            as_of=DEADLINE - timedelta(seconds=1),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tenant_id", "batch_id"),
    (
        ("tenant-b", "expected-batch"),
        ("tenant-a", "different-batch"),
    ),
)
async def test_stats_read_rejects_snapshot_from_another_tenant_or_batch(
    tenant_id: str,
    batch_id: str,
) -> None:
    batch = create_weekly_plan_batch(
        tenant_id="tenant-a",
        target_week_start=date(2026, 8, 17),
        roster=(_member("user-a"),),
        created_at=DEADLINE - timedelta(days=3),
    )
    memory = InMemoryWeeklyPlanStore()
    memory.save_batch(batch)
    frozen = memory.build_monday_snapshot(
        tenant_id="tenant-a",
        batch_id=batch.batch_id,
        as_of=DEADLINE,
        deadline_at=DEADLINE,
    )

    with pytest.raises(ValueError, match="monday_snapshot_scope_mismatch"):
        await WeeklyPlanMondayStatsReader(
            _ScopeLeakingStatsStore(memory, frozen)
        ).read(
            tenant_id=tenant_id,
            batch_id=batch_id,
            as_of=READ_AT,
        )


@pytest.mark.asyncio
async def test_stats_read_rejects_reconciliation_not_bound_to_loaded_snapshot() -> None:
    batch = create_weekly_plan_batch(
        tenant_id="tenant-a",
        target_week_start=date(2026, 8, 17),
        roster=(_member("user-a"),),
        created_at=DEADLINE - timedelta(days=3),
    )
    memory = InMemoryWeeklyPlanStore()
    memory.save_batch(batch)
    frozen = memory.build_monday_snapshot(
        tenant_id="tenant-a",
        batch_id=batch.batch_id,
        as_of=DEADLINE,
        deadline_at=DEADLINE,
    )

    with pytest.raises(ValueError, match="monday_reconciliation_scope_mismatch"):
        await WeeklyPlanMondayStatsReader(
            _ReconciliationLeakingStatsStore(memory, frozen)
        ).read(
            tenant_id="tenant-a",
            batch_id=batch.batch_id,
            as_of=READ_AT,
        )


class _ScopeLeakingStatsStore:
    """Deliberately broken adapter used to prove the reader fails closed."""

    def __init__(self, memory: InMemoryWeeklyPlanStore, snapshot) -> None:
        self._memory = memory
        self._snapshot = snapshot

    async def load_monday_snapshot(self, **kwargs):
        del kwargs
        return self._snapshot

    async def reconcile_monday_snapshot(self, **kwargs):
        del kwargs
        return self._memory.reconcile_monday_snapshot(
            tenant_id=self._snapshot.tenant_id,
            batch_id=self._snapshot.batch_id,
            as_of=READ_AT,
        )


class _ReconciliationLeakingStatsStore(_ScopeLeakingStatsStore):
    async def reconcile_monday_snapshot(self, **kwargs):
        result = await super().reconcile_monday_snapshot(**kwargs)
        return replace(result, snapshot_id="another-snapshot")
