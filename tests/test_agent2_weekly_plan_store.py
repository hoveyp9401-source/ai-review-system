from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from dataclasses import replace
from pathlib import Path
import os
import json
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import text
import pytest

from app.agent2.weekly_plan_domain import create_weekly_plan, create_weekly_plan_batch
from app.agent2.weekly_plan_models import WeeklyPlanCommand, WeeklyPlanRosterMember
from app.agent2.weekly_plan_store import (
    InMemoryWeeklyPlanStore,
    SqlWeeklyPlanStore,
    _monday_snapshot_from_row,
    _monday_snapshot_row,
)


NOW = datetime(2026, 8, 17, 1, 0, tzinfo=timezone.utc)


def _member(user_id: str, name: str) -> WeeklyPlanRosterMember:
    return WeeklyPlanRosterMember(
        user_id=user_id,
        display_name=name,
        department_id="department-a",
        department_name="测试部门",
        team_id="team-a",
        team_name="测试小组",
    )


def test_store_preserves_roster_snapshot_and_builds_immutable_monday_snapshot() -> None:
    batch = create_weekly_plan_batch(
        tenant_id="tenant-a",
        target_week_start=date(2026, 8, 17),
        roster=(_member("user-a", "用户甲"), _member("user-b", "用户乙")),
        created_at=NOW,
    )
    plan = create_weekly_plan(batch=batch, owner_user_id="user-a", created_at=NOW)
    store = InMemoryWeeklyPlanStore()
    store.save_batch(batch)
    store.save_plan(plan)

    snapshot = store.build_monday_snapshot(
        tenant_id="tenant-a", batch_id=batch.batch_id, as_of=NOW, deadline_at=NOW
    )

    assert snapshot.roster_count == 2
    assert snapshot.submitted_count == 0
    assert snapshot.draft_count == 0
    assert snapshot.unfilled_count == 2
    assert [row.user_id for row in snapshot.rows] == ["user-a", "user-b"]
    assert [row.plan_status for row in snapshot.rows] == ["unfilled", "unfilled"]
    rerun = store.build_monday_snapshot(
        tenant_id="tenant-a", batch_id=batch.batch_id, as_of=NOW, deadline_at=NOW
    )
    assert rerun == snapshot
    assert rerun is not snapshot

    snapshot.rows[0].days[0]["state"] = "tampered"
    pristine = store.build_monday_snapshot(
        tenant_id="tenant-a", batch_id=batch.batch_id, as_of=NOW, deadline_at=NOW
    )
    assert pristine.rows[0].days[0]["state"] == "unfilled"


def test_monday_snapshot_sql_payload_is_json_safe_and_restores_typed_immutable_values() -> None:
    batch = create_weekly_plan_batch(
        tenant_id="tenant-a",
        target_week_start=date(2026, 8, 17),
        roster=(_member("user-a", "用户甲"),),
        created_at=NOW - timedelta(days=3),
    )
    submitted = create_weekly_plan(
        batch=batch, owner_user_id="user-a", created_at=NOW - timedelta(days=3)
    )
    submitted = replace(
        submitted,
        status="submitted",
        submitted_at=NOW - timedelta(hours=1),
        version=1,
    )
    store = InMemoryWeeklyPlanStore()
    store.save_batch(batch)
    store.save_plan(submitted)
    snapshot = store.build_monday_snapshot(
        tenant_id="tenant-a", batch_id=batch.batch_id, as_of=NOW, deadline_at=NOW
    )

    payload = _monday_snapshot_row(snapshot)
    json.dumps(payload["rows_json"])
    restored = _monday_snapshot_from_row(payload)

    assert payload["rows_json"][0]["submitted_at"] == (
        NOW - timedelta(hours=1)
    ).isoformat()
    assert restored.rows[0].submitted_at == NOW - timedelta(hours=1)
    assert isinstance(restored.rows[0].days, tuple)


def test_monday_late_submission_is_a_delta_and_never_rewrites_the_frozen_snapshot() -> None:
    batch = create_weekly_plan_batch(
        tenant_id="tenant-a",
        target_week_start=date(2026, 8, 17),
        roster=(_member("user-a", "用户甲"), _member("user-b", "用户乙")),
        created_at=NOW - timedelta(days=3),
    )
    deadline = NOW - timedelta(hours=13)
    store = InMemoryWeeklyPlanStore()
    store.save_batch(batch)
    frozen = store.build_monday_snapshot(
        tenant_id="tenant-a",
        batch_id=batch.batch_id,
        as_of=NOW,
        deadline_at=deadline,
    )

    late_plan = create_weekly_plan(
        batch=batch,
        owner_user_id="user-a",
        created_at=NOW + timedelta(minutes=5),
    )
    late_plan = replace(
        late_plan,
        status="submitted",
        version=1,
        submitted_at=NOW + timedelta(minutes=10),
        updated_at=NOW + timedelta(minutes=10),
    )
    store.save_plan(late_plan)

    current = store.reconcile_monday_snapshot(
        tenant_id="tenant-a", batch_id=batch.batch_id, as_of=NOW + timedelta(hours=1)
    )
    rerun = store.reconcile_monday_snapshot(
        tenant_id="tenant-a", batch_id=batch.batch_id, as_of=NOW + timedelta(hours=1)
    )

    assert frozen.deadline_at == deadline
    assert frozen.submitted_count == 0
    assert frozen.unfilled_count == 2
    assert frozen.rows[0].plan_status == "unfilled"
    frozen_rerun = store.build_monday_snapshot(
        tenant_id="tenant-a",
        batch_id=batch.batch_id,
        as_of=NOW + timedelta(hours=2),
        deadline_at=NOW + timedelta(hours=2),
    )
    assert frozen_rerun == frozen
    assert frozen_rerun is not frozen
    assert current == rerun
    assert current.snapshot_id == frozen.snapshot_id
    assert current.current_submitted_count == 1
    assert current.late_submitted_count == 1
    assert current.changed_after_snapshot_count == 1
    assert current.rows[0].snapshot_plan_status == "unfilled"
    assert current.rows[0].current_plan_status == "submitted"
    assert current.rows[0].submission_timing == "late"
    assert current.rows[0].changed_after_snapshot is True
    assert current.rows[1].submission_timing == "not_submitted"
    assert current.rows[1].changed_after_snapshot is False


def test_submission_after_monday_is_not_misreported_as_an_allowed_late_fill() -> None:
    batch = create_weekly_plan_batch(
        tenant_id="tenant-a",
        target_week_start=date(2026, 8, 17),
        roster=(_member("user-a", "用户甲"),),
        created_at=NOW - timedelta(days=3),
    )
    store = InMemoryWeeklyPlanStore()
    store.save_batch(batch)
    frozen = store.build_monday_snapshot(
        tenant_id="tenant-a",
        batch_id=batch.batch_id,
        as_of=NOW,
        deadline_at=NOW,
    )
    invalidly_late = replace(
        create_weekly_plan(
            batch=batch,
            owner_user_id="user-a",
            created_at=NOW,
        ),
        status="submitted",
        version=1,
        submitted_at=NOW + timedelta(days=1),
        updated_at=NOW + timedelta(days=1),
    )
    store.save_plan(invalidly_late)

    current = store.reconcile_monday_snapshot(
        tenant_id="tenant-a",
        batch_id=batch.batch_id,
        as_of=NOW + timedelta(days=1),
    )

    assert frozen.rows[0].plan_status == "unfilled"
    assert current.late_submitted_count == 0
    assert current.closed_window_submitted_count == 1
    assert current.rows[0].submission_timing == "closed_window"


def test_store_rejects_cross_tenant_reads_and_version_overwrites() -> None:
    batch = create_weekly_plan_batch(
        tenant_id="tenant-a",
        target_week_start=date(2026, 8, 17),
        roster=(_member("user-a", "用户甲"),),
        created_at=NOW,
    )
    plan = create_weekly_plan(batch=batch, owner_user_id="user-a", created_at=NOW)
    store = InMemoryWeeklyPlanStore()
    store.save_batch(batch)
    store.save_plan(plan)

    assert store.load_plan(
        tenant_id="tenant-b", plan_id=plan.plan_id, owner_user_id="user-a"
    ) is None
    assert store.load_plan(
        tenant_id="tenant-a", plan_id=plan.plan_id, owner_user_id="user-b"
    ) is None

    try:
        store.save_plan(plan, expected_version=1)
    except ValueError as error:
        assert str(error) == "version_conflict"
    else:
        raise AssertionError("stale expected version must not overwrite a plan")


def test_in_memory_open_week_lookup_finds_roster_member_before_plan_creation() -> None:
    batch = create_weekly_plan_batch(
        tenant_id="tenant-a",
        target_week_start=date(2026, 8, 17),
        roster=(_member("user-a", "用户甲"),),
        created_at=NOW - timedelta(days=3),
    )
    store = InMemoryWeeklyPlanStore()
    store.save_batch(batch)

    assert store.load_open_target_week_for_owner(
        tenant_id="tenant-a",
        owner_user_id="user-a",
        preferred_target_week_start=date(2026, 8, 17),
    ) == date(2026, 8, 17)
    assert store.load_open_target_week_for_owner(
        tenant_id="tenant-a",
        owner_user_id="user-a",
        preferred_target_week_start=date(2026, 8, 24),
    ) is None


def test_in_memory_open_batch_ref_preserves_the_authoritative_batch_id() -> None:
    batch = replace(
        create_weekly_plan_batch(
            tenant_id="tenant-a",
            target_week_start=date(2026, 8, 17),
            roster=(_member("user-a", "User A"),),
            created_at=NOW,
        ),
        batch_id="90000000-0000-4000-8000-000000000009",
    )
    store = InMemoryWeeklyPlanStore()
    store.save_batch(batch)

    ref = store.load_open_batch_ref_for_owner(
        tenant_id="tenant-a",
        owner_user_id="user-a",
        preferred_target_week_start=date(2026, 8, 17),
    )

    assert ref is not None
    assert ref.batch_id == batch.batch_id
    assert ref.target_week_start == batch.target_week_start


def test_in_memory_open_batch_ref_is_scoped_and_fails_closed_on_invalid_week() -> None:
    batch = create_weekly_plan_batch(
        tenant_id="tenant-a",
        target_week_start=date(2026, 8, 17),
        roster=(_member("user-a", "User A"),),
        created_at=NOW,
    )
    store = InMemoryWeeklyPlanStore()
    store.save_batch(batch)

    assert store.load_open_batch_ref_for_owner(
        tenant_id="tenant-b",
        owner_user_id="user-a",
        preferred_target_week_start=date(2026, 8, 17),
    ) is None
    assert store.load_open_batch_ref_for_owner(
        tenant_id="tenant-a",
        owner_user_id="user-b",
        preferred_target_week_start=date(2026, 8, 17),
    ) is None
    assert store.load_open_batch_ref_for_owner(
        tenant_id="tenant-a",
        owner_user_id="user-a",
        preferred_target_week_start=date(2026, 8, 24),
    ) is None
    with pytest.raises(ValueError, match="Monday"):
        store.load_open_batch_ref_for_owner(
            tenant_id="tenant-a",
            owner_user_id="user-a",
            preferred_target_week_start=date(2026, 8, 18),
        )


def test_in_memory_open_batch_ref_rejects_multiple_matching_batches() -> None:
    base = create_weekly_plan_batch(
        tenant_id="tenant-a",
        target_week_start=date(2026, 8, 17),
        roster=(_member("user-a", "User A"),),
        created_at=NOW,
    )
    store = InMemoryWeeklyPlanStore()
    store.save_batch(replace(base, batch_id="batch-one"))
    store.save_batch(replace(base, batch_id="batch-two"))

    with pytest.raises(ValueError, match="multiple_open_weekly_plan_batches"):
        store.load_open_batch_ref_for_owner(
            tenant_id="tenant-a",
            owner_user_id="user-a",
            preferred_target_week_start=date(2026, 8, 17),
        )


def test_in_memory_snapshotted_batch_remains_open_for_monday_late_fill() -> None:
    batch = create_weekly_plan_batch(
        tenant_id="tenant-a",
        target_week_start=date(2026, 8, 17),
        roster=(_member("user-a", "User A"),),
        created_at=NOW,
    )
    store = InMemoryWeeklyPlanStore()
    store.save_batch(batch)
    store.build_monday_snapshot(
        tenant_id="tenant-a",
        batch_id=batch.batch_id,
        as_of=NOW,
        deadline_at=NOW,
    )

    ref = store.load_open_batch_ref_for_owner(
        tenant_id="tenant-a",
        owner_user_id="user-a",
        preferred_target_week_start=date(2026, 8, 17),
    )

    assert ref is not None
    assert ref.batch_id == batch.batch_id


def test_store_executes_command_atomically_and_replay_never_writes_twice() -> None:
    batch = create_weekly_plan_batch(
        tenant_id="tenant-a",
        target_week_start=date(2026, 8, 17),
        roster=(_member("user-a", "用户甲"),),
        created_at=NOW,
    )
    plan = create_weekly_plan(batch=batch, owner_user_id="user-a", created_at=NOW)
    store = InMemoryWeeklyPlanStore()
    store.save_batch(batch)
    store.save_plan(plan)
    command = WeeklyPlanCommand(
        command_id="command-1",
        command_type="add_item",
        tenant_id="tenant-a",
        actor_user_id="user-a",
        plan_id=plan.plan_id,
        expected_version=0,
        idempotency_key="message-1:add-item-1",
        source_message_id="message-1",
        patch={
            "plan_date": "2026-08-17",
            "original_text": "整理甲项目材料",
            "source": "manual",
        },
    )

    first = store.execute(command, executed_at=NOW)
    replay = store.execute(command, executed_at=NOW)
    persisted = store.load_plan(
        tenant_id="tenant-a", plan_id=plan.plan_id, owner_user_id="user-a"
    )

    assert first.receipt.status == "executed"
    assert first.receipt.actual_write is True
    assert first.audit_event is not None
    assert replay.receipt.status == "duplicate"
    assert replay.receipt.actual_write is False
    assert persisted is not None
    assert persisted.version == 1
    assert len(persisted.days[0].items) == 1

    collision = store.execute(
        replace(command, command_id="forged-command", command_type="submit_plan"),
        executed_at=NOW,
    )
    assert collision.receipt.status == "blocked"
    assert collision.receipt.reason_code == "idempotency_scope_conflict"
    assert collision.receipt.actual_write is False


def test_postgres_schema_carries_scope_version_idempotency_and_snapshot_guards() -> None:
    sql = (
        Path(__file__).parents[1] / "scripts" / "create_agent2_weekly_plans.sql"
    ).read_text(encoding="utf-8")

    for table in (
        "agent2_weekly_plan_batches",
        "agent2_weekly_plan_roster_members",
        "agent2_weekly_plans",
        "agent2_weekly_plan_days",
        "agent2_weekly_plan_items",
        "agent2_weekly_plan_suggestions",
        "agent2_weekly_plan_command_receipts",
        "agent2_weekly_plan_audit_events",
        "agent2_weekly_plan_monday_snapshots",
        "agent2_weekly_plan_reminder_outbox",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    assert "EXTRACT(ISODOW FROM target_week_start) = 1" in sql
    assert "deadline_at timestamptz NOT NULL" in sql
    assert "ADD COLUMN IF NOT EXISTS deadline_at timestamptz" in sql
    assert "SET deadline_at = as_of" in sql
    assert "ADD COLUMN IF NOT EXISTS unfilled_count integer" in sql
    assert "roster_count - submitted_count - draft_count" in sql
    assert "UNIQUE (tenant_id, owner_user_id, target_week_start)" in sql
    assert "UNIQUE (tenant_id, idempotency_key)" in sql
    assert "CHECK (day_index BETWEEN 1 AND 6)" in sql
    assert "CHECK (state IN ('unfilled', 'explicitly_empty', 'planned'))" in sql
    assert (
        "CHECK (status IN ('available', 'accepted', 'rejected', 'expired', 'superseded'))"
        in sql
    )
    for field in (
        "owner_user_id",
        "source_kind",
        "source_ref",
        "source_version",
        "evidence_text",
        "evidence_sha256",
        "matter_excerpt",
        "expires_at",
        "decision_ref",
        "decided_at",
        "superseded_by_id",
        "accepted_item_id",
    ):
        assert field in sql


class _SqlResult:
    rowcount = 1

    class _Mappings:
        def one_or_none(self):
            return None

        def all(self):
            return []

    def mappings(self):
        return self._Mappings()

    def scalar_one_or_none(self):
        return None


class _SqlSession:
    def __init__(self):
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _SqlResult()

    async def flush(self):
        return None


class _OpenWeekResult(_SqlResult):
    def __init__(self, rows):
        self._rows = rows

    class _Scalars:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    def scalars(self):
        return self._Scalars(self._rows)


class _OpenWeekSession(_SqlSession):
    def __init__(self, rows):
        super().__init__()
        self.rows = rows

    async def execute(self, statement):
        self.statements.append(statement)
        return _OpenWeekResult(self.rows)


class _OpenBatchRefResult(_SqlResult):
    def __init__(self, rows):
        self._rows = rows

    class _Mappings:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    def mappings(self):
        return self._Mappings(self._rows)


class _OpenBatchRefSession(_SqlSession):
    def __init__(self, rows):
        super().__init__()
        self.rows = rows

    async def execute(self, statement):
        self.statements.append(statement)
        return _OpenBatchRefResult(self.rows)


class _LoadedSqlStore(SqlWeeklyPlanStore):
    def __init__(self, session, plan, existing=None):
        super().__init__(session)
        self.plan = plan
        self.existing = existing

    async def load_plan(self, **kwargs):
        return self.plan


class _OrderingSqlSession(_SqlSession):
    def __init__(self):
        super().__init__()
        self.events = []

    async def execute(self, statement):
        compiled = str(statement.compile(dialect=postgresql.dialect()))
        if compiled.startswith("SELECT agent2_weekly_plan_command_receipts"):
            self.events.append("receipt_lookup")
        return await super().execute(statement)


class _OrderingSqlStore(SqlWeeklyPlanStore):
    def __init__(self, session, plan):
        super().__init__(session)
        self.plan = plan

    async def load_plan(self, **kwargs):
        assert kwargs["for_update"] is True
        self.session.events.append("plan_lock")
        return self.plan


@pytest.mark.asyncio
async def test_sql_adapter_creates_exact_batch_roster_plan_and_six_days() -> None:
    batch = create_weekly_plan_batch(
        tenant_id="tenant-a",
        target_week_start=date(2026, 8, 17),
        roster=(_member("user-a", "用户甲"),),
        created_at=NOW,
    )
    plan = create_weekly_plan(batch=batch, owner_user_id="user-a", created_at=NOW)
    session = _SqlSession()
    store = SqlWeeklyPlanStore(session)

    await store.save_batch(batch)
    await store.create_plan(plan)

    compiled = [
        statement.compile(dialect=postgresql.dialect())
        for statement in session.statements
    ]
    sql = [str(item) for item in compiled]
    assert sum("INSERT INTO agent2_weekly_plan_batches" in item for item in sql) == 1
    assert sum("INSERT INTO agent2_weekly_plan_roster_members" in item for item in sql) == 1
    assert sum("INSERT INTO agent2_weekly_plans" in item for item in sql) == 1
    assert sum("INSERT INTO agent2_weekly_plan_days" in item for item in sql) == 6
    day_params = [
        item.params for item in compiled if "INSERT INTO agent2_weekly_plan_days" in str(item)
    ]
    assert [row["day_index"] for row in day_params] == [1, 2, 3, 4, 5, 6]


@pytest.mark.asyncio
async def test_sql_open_week_discovery_uses_frozen_roster_even_before_personal_plan_exists() -> None:
    session = _OpenBatchRefSession(
        [
            {
                "batch_id": "90000000-0000-4000-8000-000000000009",
                "target_week_start": date(2026, 8, 17),
            }
        ]
    )

    target = await SqlWeeklyPlanStore(session).load_open_target_week_for_owner(
        tenant_id="tenant-a",
        owner_user_id="user-without-plan",
        preferred_target_week_start=date(2026, 8, 17),
    )

    assert target == date(2026, 8, 17)
    compiled = str(session.statements[0].compile(dialect=postgresql.dialect()))
    assert "JOIN agent2_weekly_plan_roster_members" in compiled
    assert "agent2_weekly_plan_roster_members.user_id" in compiled
    assert "agent2_weekly_plan_batches.target_week_start" in compiled
    assert "agent2_weekly_plan_batches.status IN" in compiled


@pytest.mark.asyncio
async def test_sql_open_batch_ref_returns_real_id_from_scoped_roster_query() -> None:
    batch_id = "90000000-0000-4000-8000-000000000009"
    session = _OpenBatchRefSession(
        [
            {
                "batch_id": batch_id,
                "target_week_start": date(2026, 8, 17),
            }
        ]
    )

    ref = await SqlWeeklyPlanStore(session).load_open_batch_ref_for_owner(
        tenant_id="tenant-a",
        owner_user_id="user-without-plan",
        preferred_target_week_start=date(2026, 8, 17),
    )

    assert ref is not None
    assert ref.batch_id == batch_id
    assert ref.target_week_start == date(2026, 8, 17)
    compiled = session.statements[0].compile(dialect=postgresql.dialect())
    statement = str(compiled)
    assert "JOIN agent2_weekly_plan_roster_members" in statement
    assert "agent2_weekly_plan_batches.batch_id" in statement
    assert "agent2_weekly_plan_batches.target_week_start" in statement
    assert "agent2_weekly_plan_batches.tenant_id" in statement
    assert "agent2_weekly_plan_roster_members.user_id" in statement
    assert "agent2_weekly_plan_batches.status IN" in statement
    assert "tenant-a" not in statement
    assert "user-without-plan" not in statement
    assert "tenant-a" in compiled.params.values()
    assert "user-without-plan" in compiled.params.values()
    assert date(2026, 8, 17) in compiled.params.values()


@pytest.mark.asyncio
async def test_sql_open_batch_ref_fails_closed_on_invalid_week_or_multiple_rows() -> None:
    store = SqlWeeklyPlanStore(
        _OpenBatchRefSession(
            [
                {
                    "batch_id": "90000000-0000-4000-8000-000000000001",
                    "target_week_start": date(2026, 8, 17),
                },
                {
                    "batch_id": "90000000-0000-4000-8000-000000000002",
                    "target_week_start": date(2026, 8, 17),
                },
            ]
        )
    )

    with pytest.raises(ValueError, match="multiple_open_weekly_plan_batches"):
        await store.load_open_batch_ref_for_owner(
            tenant_id="tenant-a",
            owner_user_id="user-a",
            preferred_target_week_start=date(2026, 8, 17),
        )

    invalid_session = _OpenBatchRefSession([])
    with pytest.raises(ValueError, match="Monday"):
        await SqlWeeklyPlanStore(invalid_session).load_open_batch_ref_for_owner(
            tenant_id="tenant-a",
            owner_user_id="user-a",
            preferred_target_week_start=date(2026, 8, 18),
        )
    assert invalid_session.statements == []


@pytest.mark.asyncio
async def test_sql_monday_snapshot_marks_batch_snapshotted_without_closing_late_fill_lookup() -> None:
    source = __import__("inspect").getsource(SqlWeeklyPlanStore.build_monday_snapshot)

    assert '.values(status="snapshotted", updated_at=as_of)' in source
    assert "return _monday_snapshot_from_row(persisted)" in source
    lookup = __import__("inspect").getsource(
        SqlWeeklyPlanStore.load_open_batch_ref_for_owner
    )
    assert '("collecting", "snapshotted")' in lookup


@pytest.mark.asyncio
async def test_sql_open_week_lookup_rejects_multiple_matching_batches() -> None:
    session = _OpenBatchRefSession(
        [
            {
                "batch_id": "90000000-0000-4000-8000-000000000001",
                "target_week_start": date(2026, 8, 17),
            },
            {
                "batch_id": "90000000-0000-4000-8000-000000000002",
                "target_week_start": date(2026, 8, 17),
            },
        ]
    )

    with pytest.raises(ValueError, match="multiple_open_weekly_plan_batches"):
        await SqlWeeklyPlanStore(session).load_open_target_week_for_owner(
            tenant_id="tenant-a",
            owner_user_id="user-a",
            preferred_target_week_start=date(2026, 8, 17),
        )


@pytest.mark.asyncio
async def test_sql_idempotency_lookup_happens_after_the_plan_write_lock() -> None:
    batch = create_weekly_plan_batch(
        tenant_id="tenant-a",
        target_week_start=date(2026, 8, 17),
        roster=(_member("user-a", "用户甲"),),
        created_at=NOW,
    )
    plan = create_weekly_plan(batch=batch, owner_user_id="user-a", created_at=NOW)
    command = WeeklyPlanCommand(
        command_id="preview-command",
        command_type="preview_plan",
        tenant_id=plan.tenant_id,
        actor_user_id=plan.owner_user_id,
        plan_id=plan.plan_id,
        expected_version=plan.version,
        idempotency_key="preview-idempotency",
        source_message_id="preview-message",
    )
    session = _OrderingSqlSession()
    store = _OrderingSqlStore(session, plan)

    await store.execute(command, executed_at=NOW)

    assert session.events[:2] == ["plan_lock", "receipt_lookup"]


def test_sql_adapter_deletes_suggestions_before_items_to_preserve_foreign_keys() -> None:
    import inspect

    source = inspect.getsource(SqlWeeklyPlanStore._replace_children)
    assert source.index("delete(_suggestions)") < source.index("delete(_items)")


@pytest.mark.asyncio
async def test_sql_batch_blocks_before_any_write_when_later_operation_is_invalid() -> None:
    batch = create_weekly_plan_batch(
        tenant_id="tenant-a",
        target_week_start=date(2026, 8, 17),
        roster=(_member("user-a", "用户甲"),),
        created_at=NOW,
    )
    plan = create_weekly_plan(batch=batch, owner_user_id="user-a", created_at=NOW)
    first = WeeklyPlanCommand(
        command_id="first",
        command_type="add_item",
        tenant_id="tenant-a",
        actor_user_id="user-a",
        plan_id=plan.plan_id,
        expected_version=0,
        idempotency_key="batch-idempotency",
        source_message_id="message-1",
        patch={
            "plan_date": "2026-08-17",
            "original_text": "第一项",
            "source": "manual",
        },
    )
    invalid = replace(
        first,
        command_id="invalid",
        command_type="edit_item",
        patch={"item_id": "missing", "original_text": "第二项"},
    )
    session = _SqlSession()
    store = _LoadedSqlStore(session, plan)

    result, = await store.execute_batch((first, invalid), executed_at=NOW)

    assert result.receipt.status == "blocked"
    assert result.receipt.actual_write is False
    compiled = [str(item.compile(dialect=postgresql.dialect())) for item in session.statements]
    assert all(not text.startswith("UPDATE agent2_weekly_plans") for text in compiled)
    assert all("INSERT INTO agent2_weekly_plan_items" not in text for text in compiled)
    assert all("INSERT INTO agent2_weekly_plan_command_receipts" not in text for text in compiled)


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("AGENT2_WEEKLY_PLAN_TEST_DATABASE_URL"),
    reason="real PostgreSQL weekly-plan test URL is explicitly required",
)
async def test_real_postgresql_weekly_plan_write_read_and_outer_rollback() -> None:
    database_url = os.environ["AGENT2_WEEKLY_PLAN_TEST_DATABASE_URL"]
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    schema_sql = (
        Path(__file__).parents[1] / "scripts" / "create_agent2_weekly_plans.sql"
    ).read_text(encoding="utf-8")
    statements = [
        statement.strip()
        for statement in schema_sql.replace("BEGIN;", "").replace("COMMIT;", "").split(";")
        if statement.strip()
    ]
    async with engine.begin() as connection:
        for statement in statements:
            await connection.execute(text(statement))

    batch = create_weekly_plan_batch(
        tenant_id="weekly-plan-postgres-rollback-test",
        target_week_start=date(2099, 8, 17),
        roster=(_member("postgres-user-a", "回滚测试用户"),),
        created_at=NOW,
    )
    plan = create_weekly_plan(
        batch=batch, owner_user_id="postgres-user-a", created_at=NOW
    )
    async with sessions() as session:
        transaction = await session.begin()
        try:
            store = SqlWeeklyPlanStore(session)
            await store.save_batch(batch)
            await store.create_plan(plan)
            loaded = await store.load_plan_by_owner_week(
                tenant_id=batch.tenant_id,
                owner_user_id=plan.owner_user_id,
                target_week_start=batch.target_week_start,
            )
            assert loaded is not None
            assert loaded.plan_id == plan.plan_id
            assert len(loaded.days) == 6
        finally:
            await transaction.rollback()
    async with sessions() as verification:
        count = await verification.scalar(
            text(
                "SELECT count(*) FROM agent2_weekly_plans "
                "WHERE tenant_id = 'weekly-plan-postgres-rollback-test'"
            )
        )
        assert count == 0
    await engine.dispose()
