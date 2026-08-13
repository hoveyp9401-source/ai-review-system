from __future__ import annotations

from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from app.agent2.periodic_report_context import TrustedPeriodicReportContext
from app.agent2.tool_calling.assembly import (
    TrustedContextAssembler,
    TrustedContextRequest,
)
from app.agent2.tool_calling.context import CANARY_STATE_NAMESPACE

USER_ID = UUID("10000000-0000-4000-8000-000000000001")
REPORT_ID = UUID("20000000-0000-4000-8000-000000000001")


class _Store:
    async def load_report(self, request, report_date):
        return None

    async def load_active_clear_pendings(self, request, *, namespace):
        return ()

    async def load_recent_messages(self, request, *, namespace, limit):
        return ()

    async def load_recent_operations(self, request, *, namespace, limit):
        return ()

    async def permission_allowed(self, request, definition):
        return True

    async def gate_allowed(self, request, definition):
        return True


class _PeriodicLoader:
    async def load_current_weekly(self, *, tenant_id, owner_user_id, local_date):
        assert local_date.isoformat() == "2026-08-14"
        return TrustedPeriodicReportContext(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            report_id=REPORT_ID,
            report_type="weekly",
            period_key="2026-W33",
            version=0,
            status="collecting",
        )


class _DenyPeriodicStore(_Store):
    async def permission_allowed(self, request, definition):
        return "current_weekly_report" not in definition.tool_name

    async def gate_allowed(self, request, definition):
        return await self.permission_allowed(request, definition)


class _MustNotLoadPeriodic:
    async def load_current_weekly(self, **kwargs):
        raise AssertionError("disabled weekly report context must not be loaded")


@pytest.mark.asyncio
async def test_current_weekly_report_enters_the_same_agent2_trusted_context():
    store = _Store()
    context = await TrustedContextAssembler(
        read_port=store,
        policy_port=store,
        namespace=CANARY_STATE_NAMESPACE,
        periodic_report_loader=_PeriodicLoader(),
    ).assemble(
        TrustedContextRequest(
            tenant_id="tenant-a",
            user_id=USER_ID,
            conversation_id="direct-1",
            source_message_id="message-1",
            timezone="Asia/Shanghai",
            server_now=datetime(
                2026, 8, 14, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai")
            ),
            conversation_kind="direct",
        )
    )

    assert context.current_weekly_report.report_id == REPORT_ID
    assert context.model_payload()["current_weekly_report"]["period_key"] == "2026-W33"


@pytest.mark.asyncio
async def test_disabled_current_weekly_report_does_not_enter_model_context():
    store = _DenyPeriodicStore()
    context = await TrustedContextAssembler(
        read_port=store,
        policy_port=store,
        namespace=CANARY_STATE_NAMESPACE,
        periodic_report_loader=_MustNotLoadPeriodic(),
    ).assemble(
        TrustedContextRequest(
            tenant_id="tenant-a",
            user_id=USER_ID,
            conversation_id="direct-1",
            source_message_id="message-1",
            timezone="Asia/Shanghai",
            server_now=datetime(
                2026, 8, 14, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai")
            ),
            conversation_kind="direct",
        )
    )

    assert context.current_weekly_report is None
    assert "current_weekly_report" not in context.model_payload()
    assert "query_today_report" in context.allowed_tool_names
