from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from uuid import UUID

import pytest

from app.agent2.tool_calling.assembly import TrustedContextRequest
from app.agent2.weekly_plan_domain import create_weekly_plan, create_weekly_plan_batch
from app.agent2.weekly_plan_models import WeeklyPlanRosterMember
from app.agent2.weekly_plan_context_loader import ProductionWeeklyPlanContextLoader


USER_ID = UUID("11111111-1111-4111-8111-111111111111")


def _request(
    *,
    now: datetime,
    tenant_id: str = "tenant-a",
    timezone_name: str = "Asia/Shanghai",
) -> TrustedContextRequest:
    return TrustedContextRequest(
        tenant_id=tenant_id,
        user_id=USER_ID,
        conversation_id="conversation-a",
        source_message_id="message-a",
        timezone=timezone_name,
        server_now=now,
        display_name="测试用户",
        conversation_kind="direct",
    )


class _EmptyReadStore:
    def __init__(self) -> None:
        self.open_week_reads: list[dict[str, object]] = []
        self.reads: list[dict[str, object]] = []

    async def load_open_target_week_for_owner(self, **scope):
        self.open_week_reads.append(scope)
        return None

    async def load_plan_by_owner_week(self, **scope):
        self.reads.append(scope)
        return None


class _ScopedReadStore(_EmptyReadStore):
    def __init__(self, plan) -> None:
        super().__init__()
        self.plan = plan

    async def load_open_target_week_for_owner(self, **scope):
        self.open_week_reads.append(scope)
        if (
            scope["tenant_id"] == self.plan.tenant_id
            and scope["owner_user_id"] == self.plan.owner_user_id
        ):
            return self.plan.target_week_start
        return None

    async def load_plan_by_owner_week(self, **scope):
        self.reads.append(scope)
        if (
            scope["tenant_id"] == self.plan.tenant_id
            and scope["owner_user_id"] == self.plan.owner_user_id
            and scope["target_week_start"] == self.plan.target_week_start
        ):
            return self.plan
        return None


def _persisted_plan(*, tenant_id: str = "tenant-a", owner_user_id: str = str(USER_ID)):
    created_at = datetime(2026, 8, 14, 9, tzinfo=timezone.utc)
    batch = create_weekly_plan_batch(
        tenant_id=tenant_id,
        target_week_start=date(2026, 8, 17),
        roster=(
            WeeklyPlanRosterMember(
                user_id=owner_user_id,
                display_name="测试用户",
            ),
        ),
        created_at=created_at,
    )
    return create_weekly_plan(
        batch=batch,
        owner_user_id=owner_user_id,
        created_at=created_at,
    )


@pytest.mark.asyncio
async def test_missing_plan_returns_stable_virtual_six_day_context_without_writing() -> None:
    store = _EmptyReadStore()
    loader = ProductionWeeklyPlanContextLoader(store)
    first = await loader.load(
        _request(now=datetime(2026, 8, 13, 2, tzinfo=timezone.utc))
    )
    second = await loader.load(
        _request(now=datetime(2026, 8, 14, 15, tzinfo=timezone.utc))
    )

    assert first.plan_id == second.plan_id
    assert first.batch_id == second.batch_id
    assert first.target_week_start == date(2026, 8, 17)
    assert first.version == 0
    assert first.status == "collecting"
    assert [day.plan_date for day in first.days] == [
        date(2026, 8, 17) + timedelta(days=offset) for offset in range(6)
    ]
    assert all(day.state == "unfilled" and not day.items for day in first.days)
    assert first.suggestions == ()
    assert len(store.reads) == 2


@pytest.mark.asyncio
async def test_implicit_target_uses_beijing_business_date_at_utc_boundary() -> None:
    store = _EmptyReadStore()

    context = await ProductionWeeklyPlanContextLoader(store).load(
        _request(
            now=datetime(2026, 8, 16, 16, 30, tzinfo=timezone.utc),
            timezone_name="UTC",
        )
    )

    # 00:30 Beijing time is already Monday, so the late-fill window binds the
    # plan to the week that just began instead of silently creating next week.
    assert context.target_week_start == date(2026, 8, 17)


@pytest.mark.asyncio
async def test_monday_without_an_existing_personal_plan_targets_this_week_not_next_week() -> None:
    store = _EmptyReadStore()

    context = await ProductionWeeklyPlanContextLoader(store).load(
        _request(now=datetime(2026, 8, 17, 2, 0, tzinfo=timezone.utc))
    )

    assert context.target_week_start == date(2026, 8, 17)
    assert [day.plan_date for day in context.days] == [
        date(2026, 8, 17) + timedelta(days=offset) for offset in range(6)
    ]
    assert store.reads[0]["target_week_start"] == date(2026, 8, 17)
    assert store.open_week_reads == [
        {
            "tenant_id": "tenant-a",
            "owner_user_id": str(USER_ID),
            "preferred_target_week_start": date(2026, 8, 17),
        }
    ]


@pytest.mark.asyncio
async def test_tuesday_without_an_open_batch_still_defaults_to_next_week() -> None:
    store = _EmptyReadStore()

    context = await ProductionWeeklyPlanContextLoader(store).load(
        _request(now=datetime(2026, 8, 18, 2, 0, tzinfo=timezone.utc))
    )

    assert context.target_week_start == date(2026, 8, 24)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 8, 16, 15, tzinfo=timezone.utc),
        datetime(2026, 8, 17, 2, tzinfo=timezone.utc),
    ],
)
async def test_store_discovered_open_batch_prevents_sunday_or_monday_drift(
    now: datetime,
) -> None:
    store = _ScopedReadStore(_persisted_plan())

    context = await ProductionWeeklyPlanContextLoader(store).load(
        _request(now=now)
    )

    assert context.target_week_start == date(2026, 8, 17)
    assert store.open_week_reads == [
        {
            "tenant_id": "tenant-a",
            "owner_user_id": str(USER_ID),
            "preferred_target_week_start": date(2026, 8, 17),
        }
    ]
    assert store.reads[0]["target_week_start"] == date(2026, 8, 17)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 8, 16, 15, tzinfo=timezone.utc),
        datetime(2026, 8, 17, 2, tzinfo=timezone.utc),
    ],
)
async def test_explicit_open_batch_week_does_not_drift_on_sunday_or_monday(
    now: datetime,
) -> None:
    store = _ScopedReadStore(_persisted_plan())

    context = await ProductionWeeklyPlanContextLoader(store).load(
        _request(now=now),
        opened_target_week_start=date(2026, 8, 17),
    )

    assert context.target_week_start == date(2026, 8, 17)
    assert store.open_week_reads == []
    assert store.reads == [
        {
            "tenant_id": "tenant-a",
            "owner_user_id": str(USER_ID),
            "target_week_start": date(2026, 8, 17),
            "for_update": False,
        }
    ]


@pytest.mark.asyncio
async def test_loader_rejects_a_store_result_outside_authenticated_scope() -> None:
    class _MaliciousStore(_EmptyReadStore):
        async def load_plan_by_owner_week(self, **scope):
            self.reads.append(scope)
            return _persisted_plan(owner_user_id="another-person")

    with pytest.raises(ValueError, match="authenticated owner"):
        await ProductionWeeklyPlanContextLoader(_MaliciousStore()).load(
            _request(now=datetime(2026, 8, 13, 2, tzinfo=timezone.utc))
        )


@pytest.mark.asyncio
async def test_loader_rejects_non_monday_open_batch_target() -> None:
    with pytest.raises(ValueError, match="Monday"):
        await ProductionWeeklyPlanContextLoader(_EmptyReadStore()).load(
            _request(now=datetime(2026, 8, 17, 2, tzinfo=timezone.utc)),
            opened_target_week_start=date(2026, 8, 18),
        )


@pytest.mark.asyncio
async def test_expired_old_open_batch_is_not_allowed_to_capture_a_future_turn() -> None:
    class _StaleOpenStore(_EmptyReadStore):
        async def load_open_target_week_for_owner(self, **scope):
            self.open_scope = scope
            return date(2026, 8, 3)

    with pytest.raises(ValueError, match="outside collection window"):
        await ProductionWeeklyPlanContextLoader(_StaleOpenStore()).load(
            _request(now=datetime(2026, 8, 13, 2, tzinfo=timezone.utc))
        )


@pytest.mark.asyncio
async def test_loader_rejects_a_non_monday_week_returned_by_the_store() -> None:
    class _InvalidOpenWeekStore(_EmptyReadStore):
        async def load_open_target_week_for_owner(self, **scope):
            self.open_week_reads.append(scope)
            return date(2026, 8, 18)

    with pytest.raises(ValueError, match="Monday"):
        await ProductionWeeklyPlanContextLoader(_InvalidOpenWeekStore()).load(
            _request(now=datetime(2026, 8, 17, 2, tzinfo=timezone.utc))
        )


@pytest.mark.asyncio
async def test_open_week_lookup_is_scoped_to_authenticated_tenant_and_owner() -> None:
    store = _EmptyReadStore()

    await ProductionWeeklyPlanContextLoader(store).load(
        _request(
            now=datetime(2026, 8, 13, 2, tzinfo=timezone.utc),
            tenant_id="tenant-authenticated",
        )
    )

    assert store.open_week_reads == [
        {
            "tenant_id": "tenant-authenticated",
            "owner_user_id": str(USER_ID),
            "preferred_target_week_start": date(2026, 8, 17),
        }
    ]
