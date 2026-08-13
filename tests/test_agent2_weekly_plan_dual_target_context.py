from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import UUID

import pytest

from app.agent2.tool_calling.assembly import TrustedContextRequest
from app.agent2.tool_calling.assembly import TrustedContextAssembler
from app.agent2.tool_calling.context import CANARY_STATE_NAMESPACE
from app.agent2.weekly_plan_context_loader import ProductionWeeklyPlanContextLoader
from app.agent2.weekly_plan_context_loader import OpenWeeklyPlanBatchRef


USER_ID = UUID("11111111-1111-4111-8111-111111111111")


class _EmptyReadStore:
    async def load_open_target_week_for_owner(self, **scope):
        del scope
        return None

    async def load_open_batch_ref_for_owner(self, **scope):
        del scope
        return None

    async def load_plan_by_owner_week(self, **scope):
        del scope
        return None


class _ContextReadPort:
    async def load_report(self, request, report_date):
        del request, report_date
        return None

    async def load_active_clear_pendings(self, request, namespace):
        del request, namespace
        return ()

    async def load_recent_messages(self, request, namespace, limit):
        del request, namespace, limit
        return ()

    async def load_recent_operations(self, request, namespace, limit):
        del request, namespace, limit
        return ()


class _WeeklyPolicy:
    async def permission_allowed(self, request, definition):
        del request
        return definition.tool_name in {
            "query_next_weekly_plan",
            "apply_next_weekly_plan",
            "submit_next_weekly_plan",
        }

    async def gate_allowed(self, request, definition):
        del request, definition
        return True

def _request(*, server_now: datetime, occurred_ats: tuple[datetime, ...]):
    return TrustedContextRequest(
        tenant_id="tenant-a",
        user_id=USER_ID,
        conversation_id="conversation-a",
        source_message_id="message-a",
        timezone="Asia/Shanghai",
        server_now=server_now,
        conversation_kind="direct",
        persisted_message_occurred_ats=occurred_ats,
    )


@pytest.mark.asyncio
async def test_monday_exposes_current_collection_and_natural_next_as_two_targets() -> None:
    monday_morning = datetime(2026, 8, 17, 1, 0, tzinfo=timezone.utc)
    targets = await ProductionWeeklyPlanContextLoader(_EmptyReadStore()).load_targets(
        _request(server_now=monday_morning, occurred_ats=(monday_morning,))
    )

    assert [target.target_week_start for target in targets] == [
        date(2026, 8, 17),
        date(2026, 8, 24),
    ]
    assert targets[0].roles == ("active_collection",)
    assert targets[0].natural_next_for_message_indexes == ()
    assert targets[1].roles == ("natural_next",)
    assert targets[1].natural_next_for_message_indexes == (1,)


@pytest.mark.asyncio
async def test_messages_across_sunday_monday_have_distinct_natural_next_weeks() -> None:
    sunday_2359 = datetime(2026, 8, 16, 15, 59, tzinfo=timezone.utc)
    monday_0001 = datetime(2026, 8, 16, 16, 1, tzinfo=timezone.utc)

    targets = await ProductionWeeklyPlanContextLoader(_EmptyReadStore()).load_targets(
        _request(
            server_now=monday_0001,
            occurred_ats=(sunday_2359, monday_0001),
        )
    )

    assert [target.target_week_start for target in targets] == [
        date(2026, 8, 17),
        date(2026, 8, 24),
    ]
    assert targets[0].roles == ("active_collection", "natural_next")
    assert targets[0].natural_next_for_message_indexes == (1,)
    assert targets[1].roles == ("natural_next",)
    assert targets[1].natural_next_for_message_indexes == (2,)


@pytest.mark.asyncio
async def test_assembled_payload_exposes_all_targets_and_keeps_single_target_alias() -> None:
    monday_morning = datetime(2026, 8, 17, 1, 0, tzinfo=timezone.utc)
    context = await TrustedContextAssembler(
        read_port=_ContextReadPort(),
        policy_port=_WeeklyPolicy(),
        namespace=CANARY_STATE_NAMESPACE,
        weekly_plan_loader=ProductionWeeklyPlanContextLoader(_EmptyReadStore()),
    ).assemble(
        _request(server_now=monday_morning, occurred_ats=(monday_morning,))
    )

    assert context.weekly_plan is not None
    assert context.weekly_plan.target_week_start == date(2026, 8, 17)
    assert [target.target_week_start for target in context.weekly_plans] == [
        date(2026, 8, 17),
        date(2026, 8, 24),
    ]
    assert context.model_payload()["weekly_plan_targets"] == [
        target.model_payload() for target in context.weekly_plans
    ]


@pytest.mark.asyncio
async def test_roster_batch_without_personal_plan_preserves_authoritative_batch_id() -> None:
    authoritative_batch_id = "90000000-0000-4000-8000-000000000009"

    class _RosterBatchStore(_EmptyReadStore):
        async def load_open_batch_ref_for_owner(self, **scope):
            if scope["preferred_target_week_start"] != date(2026, 8, 17):
                return None
            return OpenWeeklyPlanBatchRef(
                batch_id=authoritative_batch_id,
                target_week_start=date(2026, 8, 17),
            )

    monday_morning = datetime(2026, 8, 17, 1, 0, tzinfo=timezone.utc)
    targets = await ProductionWeeklyPlanContextLoader(_RosterBatchStore()).load_targets(
        _request(server_now=monday_morning, occurred_ats=(monday_morning,))
    )

    assert targets[0].target_week_start == date(2026, 8, 17)
    assert targets[0].batch_id == authoritative_batch_id
