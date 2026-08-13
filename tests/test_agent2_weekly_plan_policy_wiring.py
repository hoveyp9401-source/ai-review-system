from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from app.agent2.tool_calling.assembly import TrustedContextRequest
from app.agent2.tool_calling.registry import TOOL_REGISTRY
from app.agent2.tool_calling.production_store import ProductionContextStore
from app.config import Settings


class _Session:
    pass


def _settings(**changes):
    return Settings(_env_file=None, **changes)


def _request(*, user_id, conversation_kind="direct"):
    return TrustedContextRequest(
        tenant_id="tenant-a",
        user_id=user_id,
        conversation_id="cid-a",
        source_message_id="msg-a",
        timezone="Asia/Shanghai",
        server_now=datetime(
            2026,
            8,
            13,
            10,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
        conversation_kind=conversation_kind,
    )


@pytest.mark.asyncio
async def test_weekly_tools_require_direct_exact_server_allowlists():
    user_id = uuid4()
    user = SimpleNamespace(id=user_id, active=True)
    settings = _settings(
        agent2_weekly_plan_enabled=True,
        agent2_weekly_plan_write_enabled=True,
        agent2_weekly_plan_tenant_allowlist="tenant-a",
        agent2_weekly_plan_user_allowlist=str(user_id),
    )
    store = ProductionContextStore(
        _Session(),
        user=user,
        tenant_id="tenant-a",
        settings=settings,
    )

    read = TOOL_REGISTRY["query_next_weekly_plan"]
    write = TOOL_REGISTRY["apply_next_weekly_plan"]

    assert await store.permission_allowed(_request(user_id=user_id), read)
    assert await store.gate_allowed(_request(user_id=user_id), write)
    assert not await store.permission_allowed(
        _request(user_id=user_id, conversation_kind="group"),
        read,
    )
    assert not await store.gate_allowed(
        _request(user_id=user_id, conversation_kind="unknown"),
        write,
    )


@pytest.mark.asyncio
async def test_weekly_tools_fail_closed_for_empty_or_wrong_ids_and_write_switch():
    user_id = uuid4()
    user = SimpleNamespace(id=user_id, active=True)
    read = TOOL_REGISTRY["query_next_weekly_plan"]
    write = TOOL_REGISTRY["apply_next_weekly_plan"]

    empty = ProductionContextStore(
        _Session(),
        user=user,
        tenant_id="tenant-a",
        settings=_settings(agent2_weekly_plan_enabled=True),
    )
    assert not await empty.permission_allowed(_request(user_id=user_id), read)

    wrong_user = ProductionContextStore(
        _Session(),
        user=user,
        tenant_id="tenant-a",
        settings=_settings(
            agent2_weekly_plan_enabled=True,
            agent2_weekly_plan_write_enabled=True,
            agent2_weekly_plan_tenant_allowlist="tenant-a",
            agent2_weekly_plan_user_allowlist=str(uuid4()),
        ),
    )
    assert not await wrong_user.permission_allowed(
        _request(user_id=user_id),
        read,
    )

    read_only = ProductionContextStore(
        _Session(),
        user=user,
        tenant_id="tenant-a",
        settings=_settings(
            agent2_weekly_plan_enabled=True,
            agent2_weekly_plan_tenant_allowlist="tenant-a",
            agent2_weekly_plan_user_allowlist=str(user_id),
        ),
    )
    assert await read_only.permission_allowed(
        _request(user_id=user_id),
        read,
    )
    assert not await read_only.gate_allowed(
        _request(user_id=user_id),
        write,
    )


@pytest.mark.asyncio
async def test_weekly_chat_tools_fail_closed_for_multi_user_configuration():
    user_id = uuid4()
    store = ProductionContextStore(
        _Session(),
        user=SimpleNamespace(id=user_id, active=True),
        tenant_id="tenant-a",
        settings=_settings(
            agent2_weekly_plan_enabled=True,
            agent2_weekly_plan_write_enabled=True,
            agent2_weekly_plan_tenant_allowlist="tenant-a",
            agent2_weekly_plan_user_allowlist=f"{user_id},{uuid4()}",
        ),
    )

    assert not await store.permission_allowed(
        _request(user_id=user_id),
        TOOL_REGISTRY["query_next_weekly_plan"],
    )
    assert not await store.gate_allowed(
        _request(user_id=user_id),
        TOOL_REGISTRY["apply_next_weekly_plan"],
    )
