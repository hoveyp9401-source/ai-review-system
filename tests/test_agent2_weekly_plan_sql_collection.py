from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy.dialects import postgresql

from app.agent2.weekly_plan_collection import (
    WeeklyPlanCollectionWindow,
    WeeklyPlanReminderCandidate,
)
from app.agent2.weekly_plan_domain import create_weekly_plan_batch
from app.agent2.weekly_plan_models import WeeklyPlanRosterMember
from app.agent2.weekly_plan_reminder_outbox import (
    SqlWeeklyPlanReminderOutboxStore,
    cancel_weekly_plan_reminder,
    claim_weekly_plan_reminder,
    create_weekly_plan_reminder_outbox,
    fail_weekly_plan_reminder,
    record_weekly_plan_delivery,
    record_weekly_plan_provider_acceptance,
)
from app.agent2.weekly_plan_sql_collection import (
    SqlWeeklyPlanCollectionOrchestrator,
)
from app.agent2.weekly_plan_store import InMemoryWeeklyPlanStore, SqlWeeklyPlanStore

NOW = datetime(2026, 8, 14, 8, 0, tzinfo=timezone.utc)
TARGET_WEEK = date(2026, 8, 17)


def _member(user_id: str, display_name: str) -> WeeklyPlanRosterMember:
    return WeeklyPlanRosterMember(
        user_id=user_id,
        display_name=display_name,
        department_id="department-a",
        department_name="测试部门",
        team_id="team-a",
        team_name="测试小组",
    )


class _Result:
    def __init__(self, *, scalar=None, rows=()):
        self._scalar = scalar
        self._rows = list(rows)

    def scalar_one_or_none(self):
        return self._scalar

    class _Mappings:
        def __init__(self, rows):
            self._rows = rows

        def one_or_none(self):
            if not self._rows:
                return None
            assert len(self._rows) == 1
            return self._rows[0]

        def all(self):
            return self._rows

    def mappings(self):
        return self._Mappings(self._rows)


class _SequencedSession:
    def __init__(self, results):
        self._results = list(results)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        assert self._results, str(statement)
        return self._results.pop(0)


@pytest.mark.asyncio
async def test_open_or_load_batch_rejects_roster_drift_without_appending_members() -> None:
    requested = create_weekly_plan_batch(
        tenant_id="tenant-a",
        target_week_start=TARGET_WEEK,
        roster=(_member("user-a", "测试用户甲"), _member("added-later", "后来新增")),
        created_at=NOW,
    )
    existing_batch_row = {
        "batch_id": requested.batch_id,
        "tenant_id": requested.tenant_id,
        "target_week_start": requested.target_week_start,
        "status": "collecting",
        "created_at": requested.created_at,
        "updated_at": requested.created_at,
    }
    existing_roster_rows = [
        {
            "user_id": "user-a",
            "display_name": "测试用户甲",
            "department_id": "department-a",
            "department_name": "测试部门",
            "team_id": "team-a",
            "team_name": "测试小组",
        }
    ]
    session = _SequencedSession(
        (
            _Result(scalar=None),
            _Result(rows=(existing_batch_row,)),
            _Result(rows=existing_roster_rows),
        )
    )

    with pytest.raises(ValueError, match="weekly_plan_frozen_roster_mismatch"):
        await SqlWeeklyPlanStore(session).open_or_load_batch(requested)

    compiled = [
        str(statement.compile(dialect=postgresql.dialect()))
        for statement in session.statements
    ]
    assert sum("INSERT INTO agent2_weekly_plan_batches" in sql for sql in compiled) == 1
    assert all("INSERT INTO agent2_weekly_plan_roster_members" not in sql for sql in compiled)


@pytest.mark.asyncio
async def test_open_or_load_batch_inserts_roster_only_for_the_winning_open() -> None:
    requested = create_weekly_plan_batch(
        tenant_id="tenant-a",
        target_week_start=TARGET_WEEK,
        roster=(_member("user-a", "测试用户甲"),),
        created_at=NOW,
    )
    persisted_batch_row = {
        "batch_id": requested.batch_id,
        "tenant_id": requested.tenant_id,
        "target_week_start": requested.target_week_start,
        "status": "collecting",
        "created_at": requested.created_at,
        "updated_at": requested.created_at,
    }
    persisted_roster_rows = [
        {
            "user_id": "user-a",
            "display_name": "测试用户甲",
            "department_id": "department-a",
            "department_name": "测试部门",
            "team_id": "team-a",
            "team_name": "测试小组",
        }
    ]
    first_session = _SequencedSession(
        (
            _Result(scalar=requested.batch_id),
            _Result(),
            _Result(rows=(persisted_batch_row,)),
            _Result(rows=persisted_roster_rows),
        )
    )
    first = await SqlWeeklyPlanStore(first_session).open_or_load_batch(requested)
    replay_session = _SequencedSession(
        (
            _Result(scalar=None),
            _Result(rows=(persisted_batch_row,)),
            _Result(rows=persisted_roster_rows),
        )
    )
    replay = await SqlWeeklyPlanStore(replay_session).open_or_load_batch(requested)

    assert first == replay
    first_sql = [
        str(statement.compile(dialect=postgresql.dialect()))
        for statement in first_session.statements
    ]
    replay_sql = [
        str(statement.compile(dialect=postgresql.dialect()))
        for statement in replay_session.statements
    ]
    assert sum("INSERT INTO agent2_weekly_plan_roster_members" in sql for sql in first_sql) == 1
    assert all("INSERT INTO agent2_weekly_plan_roster_members" not in sql for sql in replay_sql)


class _MemoryAsyncCollectionStore:
    def __init__(self):
        self.batch = None
        self.plans = {}
        self.created_plan_ids = []
        self.memory = InMemoryWeeklyPlanStore()

    async def open_or_load_batch(self, batch):
        if self.batch is None:
            self.batch = batch
            self.memory.save_batch(batch)
        elif self.batch != batch:
            raise ValueError("weekly_plan_frozen_roster_mismatch")
        return self.batch

    async def load_plan_by_owner_week(
        self, *, tenant_id, owner_user_id, target_week_start, for_update=False
    ):
        del tenant_id, target_week_start, for_update
        return self.plans.get(owner_user_id)

    async def create_plan(self, plan):
        self.plans.setdefault(plan.owner_user_id, plan)
        self.created_plan_ids.append(plan.plan_id)
        self.memory.save_plan(self.plans[plan.owner_user_id])
        return self.plans[plan.owner_user_id]

    async def build_monday_snapshot(self, **kwargs):
        return self.memory.build_monday_snapshot(**kwargs)


class _MemoryAsyncOutboxStore:
    def __init__(self):
        self.rows = {}

    async def enqueue(self, row):
        self.rows.setdefault((row.tenant_id, row.idempotency_key), row)
        return self.rows[(row.tenant_id, row.idempotency_key)]


@pytest.mark.asyncio
async def test_async_collection_opens_only_canary_and_replay_does_not_recreate_plan() -> None:
    store = _MemoryAsyncCollectionStore()
    orchestrator = SqlWeeklyPlanCollectionOrchestrator(store)
    request = {
        "tenant_id": "tenant-a",
        "target_week_start": TARGET_WEEK,
        "source_roster": (
            _member("user-a", "测试用户甲"),
            _member("outside-canary", "非测试人员"),
        ),
        "canary_user_ids": frozenset({"user-a"}),
        "window": WeeklyPlanCollectionWindow(
            opens_at=NOW,
            deadline_at=datetime(2026, 8, 17, 1, 0, tzinfo=timezone.utc),
        ),
    }

    first = await orchestrator.open_collection(**request)
    replay = await orchestrator.open_collection(**request)

    assert first == replay
    assert [member.user_id for member in first.batch.roster] == ["user-a"]
    assert [plan.owner_user_id for plan in first.plans] == ["user-a"]
    assert [day.plan_date for day in first.plans[0].days] == [
        date(2026, 8, 17),
        date(2026, 8, 18),
        date(2026, 8, 19),
        date(2026, 8, 20),
        date(2026, 8, 21),
        date(2026, 8, 22),
    ]
    assert store.created_plan_ids == [first.plans[0].plan_id]


@pytest.mark.asyncio
async def test_async_collection_persists_private_reminder_once_and_freezes_monday_snapshot() -> None:
    store = _MemoryAsyncCollectionStore()
    outbox = _MemoryAsyncOutboxStore()
    orchestrator = SqlWeeklyPlanCollectionOrchestrator(store, outbox_store=outbox)
    window = WeeklyPlanCollectionWindow(
        opens_at=NOW,
        deadline_at=datetime(2026, 8, 17, 1, 0, tzinfo=timezone.utc),
    )
    opening = await orchestrator.open_collection(
        tenant_id="tenant-a",
        target_week_start=TARGET_WEEK,
        source_roster=(_member("user-a", "测试用户甲"),),
        canary_user_ids=frozenset({"user-a"}),
        window=window,
    )
    reminder_at = datetime(2026, 8, 16, 7, 0, tzinfo=timezone.utc)

    first = await orchestrator.enqueue_private_reminders(
        opening=opening,
        canary_user_ids=frozenset({"user-a"}),
        reminder_at=reminder_at,
        created_at=reminder_at,
    )
    replay = await orchestrator.enqueue_private_reminders(
        opening=opening,
        canary_user_ids=frozenset({"user-a"}),
        reminder_at=reminder_at,
        created_at=reminder_at,
    )
    frozen = await orchestrator.freeze_monday_snapshot(
        opening=opening,
        snapshot_at=window.deadline_at,
    )
    frozen_replay = await orchestrator.freeze_monday_snapshot(
        opening=opening,
        snapshot_at=window.deadline_at,
    )

    assert first == replay
    assert len(first) == 1
    assert first[0].status == "queued"
    assert first[0].channel == "private_chat"
    assert len(outbox.rows) == 1
    assert frozen == frozen_replay
    assert frozen.roster_count == 1
    assert frozen.draft_count == 0
    assert frozen.unfilled_count == 1


def test_provider_acceptance_is_delivery_pending_until_final_delivery_receipt() -> None:
    candidate = WeeklyPlanReminderCandidate(
        tenant_id="tenant-a",
        batch_id="90000000-0000-4000-8000-000000000001",
        plan_id="90000000-0000-4000-8000-000000000002",
        target_week_start=TARGET_WEEK,
        recipient_internal_user_id="user-a",
        collection_state="unfilled",
        reminder_at=datetime(2026, 8, 16, 7, 0, tzinfo=timezone.utc),
        idempotency_key="weekly-plan-reminder:stable-key",
    )
    queued = create_weekly_plan_reminder_outbox(candidate, created_at=NOW)
    claimed = claim_weekly_plan_reminder(
        queued,
        claim_token="worker-1:claim-1",
        changed_at=NOW,
    )
    accepted = record_weekly_plan_provider_acceptance(
        claimed,
        provider_message_id="provider-message-1",
        expected_claim_token="worker-1:claim-1",
        changed_at=NOW,
    )
    delivered = record_weekly_plan_delivery(accepted, changed_at=NOW)

    assert queued.status == "queued"
    assert claimed.status == "claimed"
    assert accepted.status == "delivery_pending"
    assert accepted.delivered_at is None
    assert delivered.status == "delivered"
    assert delivered.delivered_at == NOW


@pytest.mark.asyncio
async def test_sql_outbox_enqueue_uses_duplicate_key_and_never_sends() -> None:
    candidate = WeeklyPlanReminderCandidate(
        tenant_id="tenant-a",
        batch_id="90000000-0000-4000-8000-000000000001",
        plan_id="90000000-0000-4000-8000-000000000002",
        target_week_start=TARGET_WEEK,
        recipient_internal_user_id="user-a",
        collection_state="draft",
        reminder_at=datetime(2026, 8, 16, 7, 0, tzinfo=timezone.utc),
        idempotency_key="weekly-plan-reminder:stable-key",
    )
    stored = create_weekly_plan_reminder_outbox(candidate, created_at=NOW)
    stored_row = {
        **stored.__dict__,
        "outbox_id": stored.outbox_id,
        "batch_id": stored.batch_id,
        "plan_id": stored.plan_id,
    }
    session = _SequencedSession((_Result(rows=(stored_row,)),))

    result = await SqlWeeklyPlanReminderOutboxStore(session).enqueue(stored)

    assert result == stored
    assert len(session.statements) == 1
    compiled = session.statements[0].compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "INSERT INTO agent2_weekly_plan_reminder_outbox" in sql
    assert "ON CONFLICT (tenant_id, idempotency_key) DO UPDATE" in sql
    assert "send" not in sql.lower()


@pytest.mark.asyncio
async def test_sql_due_loader_includes_expired_unaccepted_claims_for_recovery() -> None:
    candidate = WeeklyPlanReminderCandidate(
        tenant_id="tenant-a",
        batch_id="90000000-0000-4000-8000-000000000001",
        plan_id="90000000-0000-4000-8000-000000000002",
        target_week_start=TARGET_WEEK,
        recipient_internal_user_id="user-a",
        collection_state="unfilled",
        reminder_at=NOW,
        idempotency_key="weekly-plan-reminder:stale-claim",
    )
    queued = create_weekly_plan_reminder_outbox(candidate, created_at=NOW)
    stale_claim = claim_weekly_plan_reminder(
        queued,
        claim_token="abandoned-worker",
        changed_at=NOW,
    )
    recover_at = NOW + timedelta(minutes=16)
    session = _SequencedSession((_Result(rows=({**stale_claim.__dict__},)),))

    rows = await SqlWeeklyPlanReminderOutboxStore(session).load_due_queued(
        tenant_id="tenant-a",
        recipient_internal_user_id="user-a",
        as_of=recover_at,
        limit=1,
    )

    assert rows == (stale_claim,)
    compiled = session.statements[0].compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "agent2_weekly_plan_reminder_outbox.status" in sql
    assert "agent2_weekly_plan_reminder_outbox.provider_message_id" in sql
    assert "agent2_weekly_plan_reminder_outbox.updated_at <=" in sql
    assert "queued" in compiled.params.values()
    assert "claimed" in compiled.params.values()
    assert "" in compiled.params.values()


@pytest.mark.asyncio
async def test_sql_outbox_transitions_keep_provider_acceptance_distinct_from_delivery() -> None:
    candidate = WeeklyPlanReminderCandidate(
        tenant_id="tenant-a",
        batch_id="90000000-0000-4000-8000-000000000001",
        plan_id="90000000-0000-4000-8000-000000000002",
        target_week_start=TARGET_WEEK,
        recipient_internal_user_id="user-a",
        collection_state="unfilled",
        reminder_at=datetime(2026, 8, 16, 7, 0, tzinfo=timezone.utc),
        idempotency_key="weekly-plan-reminder:transition-key",
    )
    queued = create_weekly_plan_reminder_outbox(candidate, created_at=NOW)
    claimed = claim_weekly_plan_reminder(
        queued, claim_token="worker:claim", changed_at=NOW
    )
    accepted = record_weekly_plan_provider_acceptance(
        claimed,
        provider_message_id="provider-1",
        expected_claim_token="worker:claim",
        changed_at=NOW,
    )
    delivered = record_weekly_plan_delivery(accepted, changed_at=NOW)
    failed = fail_weekly_plan_reminder(
        accepted, error="delivery receipt timed out", changed_at=NOW
    )
    cancelled = cancel_weekly_plan_reminder(failed, changed_at=NOW)
    session = _SequencedSession(
        (
            _Result(rows=({**claimed.__dict__},)),
            _Result(rows=({**accepted.__dict__},)),
            _Result(rows=({**delivered.__dict__},)),
            _Result(rows=({**failed.__dict__},)),
            _Result(rows=({**cancelled.__dict__},)),
        )
    )
    store = SqlWeeklyPlanReminderOutboxStore(session)

    sql_claimed = await store.claim(
        tenant_id=queued.tenant_id,
        outbox_id=queued.outbox_id,
        claim_token="worker:claim",
        changed_at=NOW,
    )
    sql_accepted = await store.record_provider_acceptance(
        tenant_id=queued.tenant_id,
        outbox_id=queued.outbox_id,
        provider_message_id="provider-1",
        expected_claim_token="worker:claim",
        changed_at=NOW,
    )
    sql_delivered = await store.record_delivery(
        tenant_id=queued.tenant_id,
        outbox_id=queued.outbox_id,
        changed_at=NOW,
    )
    sql_failed = await store.record_failure(
        tenant_id=queued.tenant_id,
        outbox_id=queued.outbox_id,
        error="delivery receipt timed out",
        changed_at=NOW,
    )
    sql_cancelled = await store.cancel(
        tenant_id=queued.tenant_id,
        outbox_id=queued.outbox_id,
        changed_at=NOW,
    )

    assert sql_claimed.status == "claimed"
    assert sql_accepted.status == "delivery_pending"
    assert sql_accepted.delivered_at is None
    assert sql_delivered.status == "delivered"
    assert sql_failed.status == "failed"
    assert sql_failed.retry_count == 1
    assert sql_cancelled.status == "cancelled"
    compiled = [
        str(statement.compile(dialect=postgresql.dialect()))
        for statement in session.statements
    ]
    assert all(sql.startswith("UPDATE agent2_weekly_plan_reminder_outbox") for sql in compiled)
    assert "agent2_weekly_plan_reminder_outbox.status =" in compiled[0]
    assert "agent2_weekly_plan_reminder_outbox.status =" in compiled[1]
    assert "agent2_weekly_plan_reminder_outbox.status =" in compiled[2]


def test_migration_contains_outbox_states_and_duplicate_guard() -> None:
    from pathlib import Path

    sql = (
        Path(__file__).parents[1] / "scripts" / "create_agent2_weekly_plans.sql"
    ).read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS agent2_weekly_plan_reminder_outbox" in sql
    assert "UNIQUE (tenant_id, idempotency_key)" in sql
    for status in (
        "queued",
        "claimed",
        "delivery_pending",
        "delivered",
        "failed",
        "cancelled",
    ):
        assert f"'{status}'" in sql
    assert "provider_accepted_at" in sql
    assert "delivered_at" in sql
