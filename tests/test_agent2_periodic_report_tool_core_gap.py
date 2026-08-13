from __future__ import annotations

from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from app.agent2.tool_calling.assembly import (
    TrustedContextAssembler,
    TrustedContextRequest,
)
from app.agent2.tool_calling.context import CANARY_STATE_NAMESPACE
from app.agent2.tool_calling.contracts import ExecutionMode
from app.agent2.tool_calling.registry import deepseek_tool_schemas


class _EmptyCanaryContextStore:
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_message", "required_tool"),
    (
        ("我想写本周周报", "query_current_weekly_report"),
        ("补充本周周报：本周完成合同复核", "apply_current_weekly_report"),
        ("提交本周周报", "submit_current_weekly_report"),
    ),
)
async def test_real_canary_schema_can_reach_current_weekly_report_intent(
    user_message: str,
    required_tool: str,
) -> None:
    """The actual canary assembler/schema seam must expose an executable route.

    The utterance is included in the contract so the three product entrances do
    not collapse into a vague registry assertion.  Semantic selection remains
    the model's job; this test proves the selected operation is actually
    reachable through the exact schemas supplied by canary ingress.
    """

    store = _EmptyCanaryContextStore()
    context = await TrustedContextAssembler(
        read_port=store,
        policy_port=store,
        namespace=CANARY_STATE_NAMESPACE,
    ).assemble(
        TrustedContextRequest(
            tenant_id="tenant-test",
            user_id=UUID("10000000-0000-4000-8000-000000000001"),
            conversation_id="direct-conversation",
            source_message_id="source-message",
            timezone="Asia/Shanghai",
            server_now=datetime(
                2026,
                8,
                14,
                18,
                0,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            ),
            conversation_kind="direct",
        )
    )
    exposed_names = {
        item["function"]["name"]
        for item in deepseek_tool_schemas(
            context.allowed_tool_names,
            mode=ExecutionMode.CANARY_EXECUTE,
        )
    }

    assert required_tool in exposed_names, (
        f"{user_message!r} has no executable Weekly Report route; "
        f"missing {required_tool}"
    )
