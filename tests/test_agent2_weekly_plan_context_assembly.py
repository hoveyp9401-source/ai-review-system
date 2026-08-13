from datetime import datetime, timezone
from datetime import date, timedelta
from uuid import UUID

import pytest

from app.agent2.tool_calling.assembly import (
    TrustedContextAssembler,
    TrustedContextRequest,
)
from app.agent2.tool_calling.context import CANARY_STATE_NAMESPACE
from app.agent2.weekly_plan_context import (
    TrustedWeeklyPlanContext,
    TrustedWeeklyPlanDay,
)


USER_ID = UUID("10000000-0000-4000-8000-000000000001")


class _ReadPort:
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


class _Policy:
    def __init__(self, allowed):
        self.allowed = allowed

    async def permission_allowed(self, request, definition):
        del request
        return definition.tool_name in self.allowed

    async def gate_allowed(self, request, definition):
        del request
        return definition.tool_name in self.allowed


class _WeeklyLoader:
    def __init__(self):
        self.calls = []

    async def load(self, request):
        self.calls.append(request)
        start = date(2026, 8, 17)
        return TrustedWeeklyPlanContext(
            plan_id="20000000-0000-4000-8000-000000000001",
            batch_id="30000000-0000-4000-8000-000000000001",
            tenant_id=request.tenant_id,
            owner_user_id=str(request.user_id),
            target_week_start=start,
            version=0,
            status="collecting",
            days=tuple(
                TrustedWeeklyPlanDay(
                    day_id=f"day-{offset}",
                    plan_date=start + timedelta(days=offset),
                    state="unfilled",
                )
                for offset in range(6)
            ),
        )



def _request():
    return TrustedContextRequest(
        tenant_id="tenant-a",
        user_id=USER_ID,
        conversation_id="direct-1",
        source_message_id="message-1",
        timezone="Asia/Shanghai",
        server_now=datetime(2026, 8, 13, 4, tzinfo=timezone.utc),
        conversation_kind="direct",
    )


@pytest.mark.asyncio
async def test_assembler_loads_weekly_context_only_after_weekly_permission():
    loader = _WeeklyLoader()
    assembler = TrustedContextAssembler(
        read_port=_ReadPort(),
        policy_port=_Policy({"query_next_weekly_plan"}),
        namespace=CANARY_STATE_NAMESPACE,
        weekly_plan_loader=loader,
    )

    context = await assembler.assemble(_request())

    assert context.weekly_plan is not None
    assert context.weekly_plan.target_week_start == date(2026, 8, 17)
    assert loader.calls == [_request()]


@pytest.mark.asyncio
async def test_assembler_does_not_touch_weekly_store_when_access_is_closed():
    loader = _WeeklyLoader()
    context = await TrustedContextAssembler(
        read_port=_ReadPort(),
        policy_port=_Policy(set()),
        namespace=CANARY_STATE_NAMESPACE,
        weekly_plan_loader=loader,
    ).assemble(_request())

    assert context.weekly_plan is None
    assert loader.calls == []
