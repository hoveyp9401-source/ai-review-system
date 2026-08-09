from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


CanaryOwner = Literal["tool_call_core", "blocked"]


@dataclass(frozen=True)
class CanaryControlSnapshot:
    """Server-owned switch state for one exact Tool-Call Core candidate."""

    tenant_id: str
    user_id: str
    enabled: bool
    runtime: str
    messages_enabled: bool
    registry_digest: str
    prompt_sha256: str
    model_name: str
    version: int


@dataclass(frozen=True)
class CanaryIdentitySnapshot:
    """Trusted identity result; names and client-provided IDs are not accepted."""

    tenant_id: str
    user_id: str
    active: bool
    exact_binding_count: int


@dataclass(frozen=True)
class CanaryRuntimeAttestation:
    """Facts checked by the server before an open switch may claim a turn."""

    runtime_ready: bool
    runtime_mode: str
    registry_digest: str
    prompt_sha256: str
    model_name: str
    production_database_verified: bool
    sandbox_configuration_present: bool
    messages_sender_configured: bool
    api_ingress_ready: bool
    stream_ingress_ready: bool


@dataclass(frozen=True)
class CanaryRouteDecision:
    owner: CanaryOwner
    reason: str
    claimed: bool = False


def decide_canary_route(
    *,
    control: CanaryControlSnapshot | None,
    identity: CanaryIdentitySnapshot | None,
    runtime: CanaryRuntimeAttestation,
    active_canary_control_count: int,
    active_canary_control_limit: int = 1,
) -> CanaryRouteDecision:
    """Choose ownership without inspecting the user's natural-language message."""

    if control is None:
        return CanaryRouteDecision(
            owner="blocked",
            reason="tool_call_canary_control_missing",
            claimed=True,
        )
    if not _identity_targets_control(control, identity):
        return CanaryRouteDecision(
            owner="blocked",
            reason="tool_call_canary_identity_not_managed",
            claimed=True,
        )
    assert identity is not None
    if identity.exact_binding_count != 1:
        return CanaryRouteDecision(
            owner="blocked",
            reason="tool_call_canary_identity_ambiguous",
            claimed=True,
        )
    if not control.enabled:
        return CanaryRouteDecision(
            owner="blocked",
            reason="tool_call_canary_switch_closed",
            claimed=True,
        )
    if control.runtime != "canary_execute":
        return CanaryRouteDecision(
            owner="blocked",
            reason="tool_call_canary_runtime_invalid",
            claimed=True,
        )

    if not 1 <= active_canary_control_limit <= 74:
        return CanaryRouteDecision(
            owner="blocked",
            reason="tool_call_canary_cohort_limit_invalid",
            claimed=True,
        )
    if not 1 <= active_canary_control_count <= active_canary_control_limit:
        return CanaryRouteDecision(
            owner="blocked",
            reason=(
                "tool_call_canary_cohort_not_single_user"
                if active_canary_control_limit == 1
                else "tool_call_canary_cohort_limit_exceeded"
            ),
            claimed=True,
        )

    failed_check = _runtime_failure(control, runtime)
    if failed_check is not None:
        return CanaryRouteDecision(
            owner="blocked",
            reason=failed_check,
            claimed=True,
        )
    return CanaryRouteDecision(
        owner="tool_call_core",
        reason="tool_call_canary_exact_identity",
        claimed=True,
    )


def _identity_targets_control(
    control: CanaryControlSnapshot,
    identity: CanaryIdentitySnapshot | None,
) -> bool:
    return bool(
        identity is not None
        and identity.active
        and identity.exact_binding_count >= 1
        and identity.tenant_id == control.tenant_id
        and identity.user_id == control.user_id
    )


def _runtime_failure(
    control: CanaryControlSnapshot,
    runtime: CanaryRuntimeAttestation,
) -> str | None:
    checks = (
        (runtime.runtime_ready, "tool_call_canary_runtime_not_ready"),
        (
            control.runtime == "canary_execute"
            and runtime.runtime_mode == "canary_execute",
            "tool_call_canary_mode_mismatch",
        ),
        (
            runtime.registry_digest == control.registry_digest,
            "tool_call_canary_registry_digest_mismatch",
        ),
        (
            runtime.prompt_sha256 == control.prompt_sha256,
            "tool_call_canary_prompt_digest_mismatch",
        ),
        (
            runtime.model_name == control.model_name,
            "tool_call_canary_model_mismatch",
        ),
        (
            runtime.production_database_verified,
            "tool_call_canary_production_database_unverified",
        ),
        (
            not runtime.sandbox_configuration_present,
            "tool_call_canary_sandbox_configuration_present",
        ),
        (
            runtime.messages_sender_configured,
            "tool_call_canary_message_sender_unconfigured",
        ),
        (
            runtime.api_ingress_ready and runtime.stream_ingress_ready,
            "tool_call_canary_ingress_incomplete",
        ),
    )
    return next((reason for passed, reason in checks if not passed), None)
