from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.agent2.business.entrypoint import (
    build_business_command_context,
    decide_runtime_owner,
    parse_tenant_allowlist,
    persist_runtime_owner_claim,
    resolve_agent2_entrypoint,
)
from app.agent2.business.route_control import RouteDecision
from app.agent2.business.models import Agent2IdentityBinding, RouteControlAudit, TenantRouteControl


NOW = datetime(2026, 7, 11, 9, 0, tzinfo=UTC)


class _Session:
    def __init__(self, bindings=(), scalar_values=()):
        self.bindings = list(bindings)
        self.values = list(scalar_values)
        self.added = []
        self.scalar_calls = 0
        self.commits = 0

    async def scalar(self, statement):
        self.scalar_calls += 1
        return self.values.pop(0)

    async def scalars(self, statement):
        self.scalar_calls += 1
        return _ScalarRows(self.bindings)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        return None

    async def commit(self):
        self.commits += 1


def _binding() -> Agent2IdentityBinding:
    return Agent2IdentityBinding(
        tenant_id="tenant-test",
        company_id="company-test",
        department_id="legal",
        team_id="litigation",
        user_id="internal-user-1",
        dingtalk_user_id="ding-user-1",
        display_name="张三",
        role_ids=["lawyer"],
        permission_scope_json={"allowed_case_ids": ["case-1", "case-2"]},
        active=True,
    )


class _ScalarRows:
    def __init__(self, values):
        self.values = values

    def all(self):
        return list(self.values)


def _control() -> TenantRouteControl:
    return TenantRouteControl(
        tenant_id="tenant-test",
        route_mode="agent2_primary",
        canary_user_ids=[],
        agent1_rollback_enabled=False,
        version=3,
        changed_by="admin",
        change_reason="canary passed",
    )


@pytest.mark.asyncio
async def test_entrypoint_is_fail_closed_without_phase2_flag_or_tenant_allowlist():
    session = _Session()
    disabled = SimpleNamespace(
        agent2_business_phase2_enabled=False,
        agent2_business_tenant_ids="tenant-test",
    )

    result = await resolve_agent2_entrypoint(
        session,  # type: ignore[arg-type]
        settings=disabled,
        dingtalk_user_id="ding-user-1",
        source_message_id="message-1",
    )

    assert result.decision.route == "blocked"
    assert result.decision.reason == "agent2_business_phase2_disabled"
    assert session.scalar_calls == 0


@pytest.mark.asyncio
async def test_test_tenant_primary_route_is_resolved_and_audited():
    session = _Session((_binding(),), (_control(),))
    settings = SimpleNamespace(
        agent2_business_phase2_enabled=True,
        agent2_business_tenant_ids="tenant-test",
    )

    result = await resolve_agent2_entrypoint(
        session,  # type: ignore[arg-type]
        settings=settings,
        dingtalk_user_id="ding-user-1",
        source_message_id="message-1",
    )

    assert result.decision.route == "agent2_primary"
    assert result.binding is not None
    audits = [item for item in session.added if isinstance(item, RouteControlAudit)]
    assert len(audits) == 1
    assert audits[0].source_message_id == "message-1"
    assert audits[0].after_json["resolved_route"] == "agent2_primary"
    assert result.route_audit_pending is True

    persisted = await persist_runtime_owner_claim(session, result)  # type: ignore[arg-type]

    assert persisted is True
    assert session.commits == 1


@pytest.mark.asyncio
async def test_owner_claim_does_not_commit_when_no_route_audit_exists():
    session = _Session()
    settings = SimpleNamespace(
        agent2_business_phase2_enabled=False,
        agent2_business_tenant_ids="tenant-test",
    )
    result = await resolve_agent2_entrypoint(
        session,  # type: ignore[arg-type]
        settings=settings,
        dingtalk_user_id="ding-user-1",
        source_message_id="message-1",
    )

    persisted = await persist_runtime_owner_claim(session, result)  # type: ignore[arg-type]

    assert persisted is False
    assert session.commits == 0


@pytest.mark.asyncio
async def test_missing_tenant_route_control_is_blocked_audited_and_committed():
    session = _Session((_binding(),), (None,))
    settings = SimpleNamespace(
        agent2_business_phase2_enabled=True,
        agent2_business_tenant_ids="tenant-test",
    )

    result = await resolve_agent2_entrypoint(
        session,  # type: ignore[arg-type]
        settings=settings,
        dingtalk_user_id="ding-user-1",
        source_message_id="message-missing-control",
    )

    assert result.decision.route == "blocked"
    audits = [item for item in session.added if isinstance(item, RouteControlAudit)]
    assert len(audits) == 1
    assert audits[0].before_json["route_mode"] == "missing"
    assert audits[0].after_json["resolved_route"] == "blocked"
    assert await persist_runtime_owner_claim(session, result) is True  # type: ignore[arg-type]
    assert session.commits == 1


@pytest.mark.asyncio
async def test_same_channel_identity_bound_to_two_allowed_tenants_is_blocked_not_arbitrarily_routed():
    first = _binding()
    second = _binding()
    second.tenant_id = "tenant-test-2"
    session = _Session((first, second))
    settings = SimpleNamespace(
        agent2_business_phase2_enabled=True,
        agent2_business_tenant_ids="tenant-test,tenant-test-2",
    )

    result = await resolve_agent2_entrypoint(
        session,  # type: ignore[arg-type]
        settings=settings,
        dingtalk_user_id="ding-user-1",
        source_message_id="message-1",
    )

    assert result.decision.route == "blocked"
    assert result.decision.reason == "ambiguous_cross_tenant_identity_binding"
    assert result.binding is None
    assert session.added == []


def test_business_context_comes_from_channel_binding_not_user_text_claims():
    context = build_business_command_context(
        _binding(),
        source_message_id="message-1",
        source_channel="dingtalk_stream",
        occurred_at=NOW,
    )

    assert context.tenant_id == "tenant-test"
    assert context.company_id == "company-test"
    assert context.department_id == "legal"
    assert context.team_id == "litigation"
    assert context.actor_user_id == "internal-user-1"
    assert context.actor_role_ids == ("lawyer",)
    assert context.allowed_case_ids == ("case-1", "case-2")
    assert context.writable_case_ids is None


def test_business_context_keeps_case_visibility_and_collaborator_write_scope_separate():
    binding = _binding()
    binding.permission_scope_json = {
        "allowed_case_ids": ["case-1", "case-2"],
        "writable_case_ids": ["case-2"],
    }

    context = build_business_command_context(
        binding,
        source_message_id="message-collaborator-1",
        source_channel="dingtalk_stream",
        occurred_at=NOW,
    )

    assert context.allowed_case_ids == ("case-1", "case-2")
    assert context.writable_case_ids == ("case-2",)
    assert context.can_create_case_progress(
        "case-2", owner_user_id="another-user"
    ) is True
    assert context.can_create_case_progress(
        "case-1", owner_user_id="another-user"
    ) is False


def test_tenant_allowlist_parser_is_deterministic_and_deduplicated():
    assert parse_tenant_allowlist("tenant-a, tenant-b;tenant-a\ntenant-c") == (
        "tenant-a",
        "tenant-b",
        "tenant-c",
    )


def test_historical_non_primary_route_is_blocked():
    owner = decide_runtime_owner(
        RouteDecision("tenant-test", "agent1", "historical_route"),
    )

    assert owner == "blocked"


def test_disabled_phase2_route_is_blocked():
    owner = decide_runtime_owner(
        RouteDecision("", "blocked", "agent2_business_phase2_disabled"),
    )

    assert owner == "blocked"


def test_phase2_primary_claims_the_message():
    owner = decide_runtime_owner(
        RouteDecision("tenant-test", "agent2_primary", "canary_user"),
    )

    assert owner == "agent2_primary"
