from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from app.agent2.tool_calling.assembly import TrustedContextRequest
from app.agent2.tool_calling.canary_config import canary_system_prompt
from app.agent2.tool_calling.production_store import ProductionContextStore
from app.agent2.tool_calling.registry import (
    TOOL_REGISTRY,
    runtime_registry_tool_names,
)
from app.config import Settings


class _Session:
    pass


def _settings(**changes):
    return Settings(_env_file=None, **changes)


def _request(*, user_id, tenant_id="tenant-a", conversation_kind="direct"):
    return TrustedContextRequest(
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id="direct-a",
        source_message_id="message-a",
        timezone="Asia/Shanghai",
        server_now=datetime(
            2026,
            8,
            14,
            18,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
        conversation_kind=conversation_kind,
    )


def test_current_weekly_report_tools_are_absent_until_independently_enabled():
    current_weekly_report_tools = {
        "query_current_weekly_report",
        "apply_current_weekly_report",
        "submit_current_weekly_report",
    }

    assert current_weekly_report_tools.isdisjoint(
        runtime_registry_tool_names(_settings())
    )
    assert current_weekly_report_tools <= set(
        runtime_registry_tool_names(
            _settings(agent2_current_weekly_report_enabled=True)
        )
    )


def test_current_weekly_report_policy_is_absent_without_its_tools():
    prompt = canary_system_prompt(
        allowed_tool_names=frozenset({"add_daily_items"})
    )

    assert "Current Weekly Report boundary:" not in prompt
    assert "query_current_weekly_report" not in prompt


def test_each_enabled_domain_adds_only_its_own_policy():
    weekly_report_prompt = canary_system_prompt(
        allowed_tool_names=frozenset({"query_current_weekly_report"})
    )
    weekly_plan_prompt = canary_system_prompt(
        allowed_tool_names=frozenset({"query_next_weekly_plan"})
    )

    assert "Current Weekly Report boundary:" in weekly_report_prompt
    assert "Weekly Work Plan boundary:" not in weekly_report_prompt
    assert "Weekly Work Plan boundary:" in weekly_plan_prompt
    assert "Current Weekly Report boundary:" not in weekly_plan_prompt


@pytest.mark.asyncio
async def test_current_weekly_report_tools_require_exact_stable_ids_and_direct_chat():
    user_id = uuid4()
    user = SimpleNamespace(id=user_id, active=True)
    settings = _settings(
        agent2_current_weekly_report_enabled=True,
        agent2_current_weekly_report_tenant_allowlist="tenant-a",
        agent2_current_weekly_report_user_allowlist=str(user_id),
    )
    store = ProductionContextStore(
        _Session(),
        user=user,
        tenant_id="tenant-a",
        settings=settings,
    )

    for name in (
        "query_current_weekly_report",
        "apply_current_weekly_report",
        "submit_current_weekly_report",
    ):
        definition = TOOL_REGISTRY[name]
        assert await store.permission_allowed(
            _request(user_id=user_id),
            definition,
        )
        assert await store.gate_allowed(
            _request(user_id=user_id),
            definition,
        )
        assert not await store.permission_allowed(
            _request(user_id=user_id, tenant_id="tenant-b"),
            definition,
        )
        assert not await store.permission_allowed(
            _request(user_id=user_id, conversation_kind="group"),
            definition,
        )


@pytest.mark.asyncio
async def test_current_weekly_report_scope_fails_closed_without_exact_allowlists():
    user_id = uuid4()
    user = SimpleNamespace(id=user_id, active=True)
    definition = TOOL_REGISTRY["apply_current_weekly_report"]

    for settings in (
        _settings(agent2_current_weekly_report_enabled=True),
        _settings(
            agent2_current_weekly_report_enabled=True,
            agent2_current_weekly_report_tenant_allowlist="tenant-a",
            agent2_current_weekly_report_user_allowlist=str(uuid4()),
        ),
        _settings(
            agent2_current_weekly_report_enabled=True,
            agent2_current_weekly_report_tenant_allowlist="tenant-a",
            agent2_current_weekly_report_user_allowlist="测试用户甲",
        ),
    ):
        store = ProductionContextStore(
            _Session(),
            user=user,
            tenant_id="tenant-a",
            settings=settings,
        )
        assert not await store.permission_allowed(
            _request(user_id=user_id),
            definition,
        )
        assert not await store.gate_allowed(
            _request(user_id=user_id),
            definition,
        )


@pytest.mark.asyncio
async def test_current_weekly_report_scope_fails_closed_for_multi_identity_allowlists():
    user_id = uuid4()
    user = SimpleNamespace(id=user_id, active=True)
    definition = TOOL_REGISTRY["apply_current_weekly_report"]
    settings = _settings(
        agent2_current_weekly_report_enabled=True,
        agent2_current_weekly_report_tenant_allowlist="tenant-a,tenant-b",
        agent2_current_weekly_report_user_allowlist=f"{user_id},{uuid4()}",
    )
    store = ProductionContextStore(
        _Session(),
        user=user,
        tenant_id="tenant-a",
        settings=settings,
    )

    assert not await store.permission_allowed(
        _request(user_id=user_id),
        definition,
    )
    assert not await store.gate_allowed(
        _request(user_id=user_id),
        definition,
    )


@pytest.mark.asyncio
async def test_current_weekly_report_switch_does_not_change_daily_or_weekly_plan_scope():
    user_id = uuid4()
    user = SimpleNamespace(id=user_id, active=True)
    store = ProductionContextStore(
        _Session(),
        user=user,
        tenant_id="tenant-a",
        settings=_settings(),
    )

    assert await store.permission_allowed(
        _request(user_id=user_id),
        TOOL_REGISTRY["query_today_report"],
    )
    assert not await store.permission_allowed(
        _request(user_id=user_id),
        TOOL_REGISTRY["query_next_weekly_plan"],
    )
