from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from app.agent2.tool_calling.assembly import (
    TrustedContextAssembler,
    TrustedContextRequest,
)
from app.agent2.tool_calling.canary_config import canary_system_prompt
from app.agent2.tool_calling.context import (
    CANARY_STATE_NAMESPACE,
    TrustedRecentMessage,
    TrustedRecentOperation,
)

NOW = datetime(2026, 8, 16, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
USER_ID = UUID("11111111-1111-1111-1111-111111111111")


class _ReadPort:
    async def load_report(self, request, report_date):
        del request, report_date

    async def load_active_clear_pendings(self, request, *, namespace):
        del request, namespace
        return ()

    async def load_recent_messages(self, request, *, namespace, limit):
        del request, namespace, limit
        return (
            TrustedRecentMessage(
                role="user",
                content="昨天还有谁没交日报？",
                source_message_id="prior-query",
            ),
            TrustedRecentMessage(
                role="assistant",
                content="未填写 1 人：测试甲。",
                source_message_id="prior-query:assistant",
                source_turn_id="prior-query",
                read_snapshot_verified=True,
            ),
        )

    async def load_recent_operations(self, request, *, namespace, limit):
        del request, namespace, limit
        return (
            TrustedRecentOperation(
                tenant_id="tenant",
                user_id=USER_ID,
                conversation_id="conversation",
                source_message_id="prior-query",
                tool_call_id="prior-call",
                tool_name="query_managed_daily_reports",
                status="success",
                changed=False,
                target_type="managed_daily_report",
                target_id="2026-08-15:all",
                occurred_at=NOW - timedelta(minutes=5),
            ),
        )


class _PolicyPort:
    async def permission_allowed(self, request, definition):
        del request, definition
        return True

    async def gate_allowed(self, request, definition):
        del request, definition
        return True


@pytest.mark.asyncio
async def test_prior_read_only_reply_is_exposed_as_a_past_snapshot() -> None:
    context = await TrustedContextAssembler(
        read_port=_ReadPort(),
        policy_port=_PolicyPort(),
        namespace=CANARY_STATE_NAMESPACE,
    ).assemble(
        TrustedContextRequest(
            tenant_id="tenant",
            user_id=USER_ID,
            conversation_id="conversation",
            source_message_id="current-message",
            timezone="Asia/Shanghai",
            server_now=NOW,
        )
    )

    messages = context.model_payload()["recent_messages"]

    assert "fact_time_scope" not in messages[0]
    assert messages[1]["fact_time_scope"] == "past_snapshot"


def test_current_mutable_state_followup_requires_a_fresh_read() -> None:
    prompt = canary_system_prompt(
        allowed_tool_names=frozenset({"query_managed_daily_reports"})
    )

    assert "fact_time_scope=past_snapshot" in prompt
    assert "call the appropriate trusted read tool again in this turn" in prompt
    assert "fixed historical fact" in prompt
