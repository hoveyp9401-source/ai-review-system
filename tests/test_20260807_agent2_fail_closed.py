from __future__ import annotations

import ast
from pathlib import Path

from app.agent2.tool_calling.canary_control import (
    CanaryControlSnapshot,
    CanaryIdentitySnapshot,
    CanaryRuntimeAttestation,
    decide_canary_route,
)


ROOT = Path(__file__).resolve().parents[1]


def _ready_runtime() -> CanaryRuntimeAttestation:
    return CanaryRuntimeAttestation(
        runtime_ready=True,
        runtime_mode="canary_execute",
        registry_digest="registry",
        prompt_sha256="prompt",
        model_name="deepseek-v4-pro",
        production_database_verified=True,
        sandbox_configuration_present=False,
        messages_sender_configured=True,
        api_ingress_ready=True,
        stream_ingress_ready=True,
    )


def _control(*, enabled: bool = True) -> CanaryControlSnapshot:
    return CanaryControlSnapshot(
        tenant_id="tenant",
        user_id="user",
        enabled=enabled,
        runtime="canary_execute",
        messages_enabled=True,
        registry_digest="registry",
        prompt_sha256="prompt",
        model_name="deepseek-v4-pro",
        version=1,
    )


def _identity(*, count: int = 1) -> CanaryIdentitySnapshot:
    return CanaryIdentitySnapshot(
        tenant_id="tenant",
        user_id="user",
        active=True,
        exact_binding_count=count,
    )


def test_missing_control_fails_closed() -> None:
    decision = decide_canary_route(
        control=None,
        identity=None,
        runtime=_ready_runtime(),
        active_canary_control_count=0,
        active_canary_control_limit=74,
    )
    assert decision.owner == "blocked"
    assert decision.claimed is True


def test_ambiguous_identity_fails_closed() -> None:
    decision = decide_canary_route(
        control=_control(),
        identity=_identity(count=2),
        runtime=_ready_runtime(),
        active_canary_control_count=74,
        active_canary_control_limit=74,
    )
    assert decision.owner == "blocked"


def test_closed_switch_fails_closed() -> None:
    decision = decide_canary_route(
        control=_control(enabled=False),
        identity=_identity(),
        runtime=_ready_runtime(),
        active_canary_control_count=73,
        active_canary_control_limit=74,
    )
    assert decision.owner == "blocked"


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _async_function_calls(path: Path, function_name: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == function_name
    )
    calls: set[str] = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
        if name:
            calls.add(name)
    return calls


def test_production_stream_has_no_legacy_fallthrough() -> None:
    calls = _async_function_calls(ROOT / "app" / "stream_runner.py", "_handle_job")
    assert "_process_stream_agent2_daily_if_enabled" not in calls
    assert "_evaluate_stream_daily_shadow" not in calls
    assert "report_service.submit_text" not in calls


def test_production_webhook_has_no_legacy_fallthrough() -> None:
    calls = _async_function_calls(ROOT / "app" / "api" / "webhook.py", "dingtalk_webhook")
    assert "_submit_webhook_agent2_if_enabled" not in calls
    assert "_evaluate_legacy_daily_gate" not in calls
    assert "report_service.submit_text" not in calls


def test_manual_api_has_no_agent1_submit_fallback() -> None:
    calls = _async_function_calls(ROOT / "app" / "api" / "reports.py", "submit_manual_report")
    assert "service.submit_text" not in calls
