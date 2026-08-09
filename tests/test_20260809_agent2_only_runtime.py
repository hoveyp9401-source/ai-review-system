from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent2.business.entrypoint import (
    decide_runtime_owner,
    resolve_agent2_entrypoint,
)
from app.agent2.business.models import Agent2IdentityBinding, TenantRouteControl
from app.agent2.business.route_control import (
    RouteDecision,
    decide_agent2_failure,
    decide_route,
)
from app.agent2.tool_calling.canary_store import ToolCallCanaryControlRepository


ROOT = Path(__file__).resolve().parents[1]


class _ScalarRows:
    def __init__(self, values):
        self._values = tuple(values)

    def all(self):
        return list(self._values)


class _Session:
    def __init__(self, *, bindings=(), controls=()):
        self._bindings = tuple(bindings)
        self._controls = list(controls)
        self.added = []

    async def scalars(self, _statement):
        return _ScalarRows(self._bindings)

    async def scalar(self, _statement):
        return self._controls.pop(0)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        return None


def _binding() -> Agent2IdentityBinding:
    return Agent2IdentityBinding(
        tenant_id="tenant-1",
        company_id="company-1",
        department_id="department-1",
        team_id="team-1",
        user_id="user-1",
        dingtalk_user_id="ding-1",
        display_name="测试用户",
        role_ids=[],
        permission_scope_json={},
        active=True,
    )


def _control(mode: str, *, rollback: bool = False) -> TenantRouteControl:
    return TenantRouteControl(
        tenant_id="tenant-1",
        route_mode=mode,
        canary_user_ids=["user-1"],
        agent1_rollback_enabled=rollback,
        version=1,
        changed_by="tester",
        change_reason="test",
    )


@pytest.mark.parametrize(
    ("phase2_enabled", "tenant_ids", "bindings", "controls", "reason"),
    (
        (False, "tenant-1", (), (), "agent2_business_phase2_disabled"),
        (True, "", (), (), "agent2_business_tenant_allowlist_missing"),
        (True, "tenant-1", (), (), "agent2_identity_binding_missing"),
        (True, "tenant-1", (_binding(),), (None,), "no_tenant_cutover_control"),
    ),
)
@pytest.mark.asyncio
async def test_entrypoint_anomalies_block_instead_of_selecting_an_old_runtime(
    phase2_enabled,
    tenant_ids,
    bindings,
    controls,
    reason,
):
    result = await resolve_agent2_entrypoint(
        _Session(bindings=bindings, controls=controls),  # type: ignore[arg-type]
        settings=SimpleNamespace(
            agent2_business_phase2_enabled=phase2_enabled,
            agent2_business_tenant_ids=tenant_ids,
        ),
        dingtalk_user_id="ding-1",
        source_message_id="message-1",
    )

    assert result.decision.route == "blocked"
    assert result.decision.reason == reason


@pytest.mark.parametrize(
    "decision",
    (
        RouteDecision("tenant-1", "blocked", "blocked"),
        RouteDecision("tenant-1", "agent1", "historical_agent1_setting"),
        RouteDecision("tenant-1", "agent2_shadow", "historical_shadow_setting"),
    ),
)
def test_runtime_owner_has_only_agent2_primary_or_blocked(decision):
    assert decide_runtime_owner(decision) == "blocked"


def test_primary_runtime_owner_is_unchanged():
    decision = RouteDecision("tenant-1", "agent2_primary", "active_primary")

    assert decide_runtime_owner(decision) == "agent2_primary"


def test_every_non_primary_route_control_mode_fails_closed():
    assert decide_route(None, tenant_id="tenant-1", user_id="user-1").route == "blocked"
    assert decide_route(
        _control("agent1"), tenant_id="tenant-1", user_id="user-1"
    ).route == "blocked"
    assert decide_route(
        _control("agent2_shadow"), tenant_id="tenant-1", user_id="user-1"
    ).route == "blocked"
    assert decide_route(
        _control("agent2_canary"), tenant_id="tenant-1", user_id="someone-else"
    ).route == "blocked"


def test_even_explicit_failure_request_cannot_return_agent1():
    primary = RouteDecision(
        "tenant-1",
        "agent2_primary",
        "active_primary",
        agent1_rollback_enabled=True,
    )

    decision = decide_agent2_failure(primary, explicit_rollback_requested=True)

    assert decision.route == "blocked"
    assert decision.reason == "agent2_failure_no_agent1_fallback"


def test_production_transport_sources_have_no_agent1_owner_branch():
    for relative_path in ("app/api/webhook.py", "app/stream_runner.py"):
        tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
        comparisons = {
            ast.unparse(node)
            for node in ast.walk(tree)
            if isinstance(node, ast.Compare)
        }
        assert 'runtime_owner == "agent1"' not in comparisons


def test_live_webhook_does_not_invoke_the_historical_shadow_observer():
    tree = ast.parse((ROOT / "app/api/webhook.py").read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "dingtalk_webhook"
    )
    called_names = {
        node.func.id
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert "_observe_workflow_route" not in called_names


def test_canary_control_store_does_not_offer_agent1_rollback():
    assert not hasattr(ToolCallCanaryControlRepository, "rollback_to_agent1")
    assert hasattr(ToolCallCanaryControlRepository, "disable_runtime_fail_closed")


@pytest.mark.parametrize(
    "relative_path",
    (
        "app/stream_runner.py",
        "app/api/reports.py",
        "app/agent2/daily_execution.py",
    ),
)
def test_production_code_contains_no_legacy_daily_fallback_symbol(relative_path):
    source = (ROOT / relative_path).read_text(encoding="utf-8")

    assert "agent2_daily_should_fallback_to_legacy" not in source
    assert "falling back to legacy" not in source


def test_production_modules_do_not_define_unused_legacy_gate_entrypoints():
    webhook_source = (ROOT / "app/api/webhook.py").read_text(encoding="utf-8")
    stream_source = (ROOT / "app/stream_runner.py").read_text(encoding="utf-8")

    assert "async def _evaluate_legacy_daily_gate" not in webhook_source
    assert "async def _observe_workflow_route" not in webhook_source
    assert "async def _evaluate_stream_legacy_daily_gate" not in stream_source


def test_manual_agent2_route_never_returns_a_bare_none_to_an_old_caller():
    tree = ast.parse((ROOT / "app/api/reports.py").read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_submit_manual_agent2_if_applicable"
    )

    bare_none_returns = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Return) and node.value is None
    ]
    assert bare_none_returns == []
