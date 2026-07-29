from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping


@dataclass(frozen=True)
class CanaryPreflightFacts:
    production_environment_declared: bool
    production_database_authenticated: bool
    production_database_identity_matches_configuration: bool
    tool_call_sandbox_configuration_present: bool
    message_sender_configured: bool
    runtime_available: bool
    runtime_mode: str
    registry_digest_matches: bool
    prompt_digest_matches: bool
    model_config_matches: bool
    registry_canary_mode_enabled: bool
    production_handlers_registered: bool
    independent_control_store_ready: bool
    exact_candidate_binding_count: int
    configured_candidate_count: int
    api_ingress_ready: bool
    stream_ingress_ready: bool
    canary_currently_enabled: bool


@dataclass(frozen=True)
class CanaryPreflightReport:
    ready: bool
    verdict: Literal["READY_FOR_CANARY", "NO_GO"]
    checks: Mapping[str, bool]


def evaluate_canary_preflight(
    facts: CanaryPreflightFacts,
) -> CanaryPreflightReport:
    checks = MappingProxyType(
        {
            "production_environment_declared": (
                facts.production_environment_declared
            ),
            "production_database_authenticated": (
                facts.production_database_authenticated
            ),
            "production_database_identity_matches_configuration": (
                facts.production_database_identity_matches_configuration
            ),
            "tool_call_sandbox_configuration_absent": (
                not facts.tool_call_sandbox_configuration_present
            ),
            "message_sender_configured": facts.message_sender_configured,
            "runtime_available": facts.runtime_available,
            "runtime_mode_canary_execute": (
                facts.runtime_mode == "canary_execute"
            ),
            "registry_digest_matches": facts.registry_digest_matches,
            "prompt_digest_matches": facts.prompt_digest_matches,
            "model_config_matches": facts.model_config_matches,
            "registry_canary_mode_enabled": (
                facts.registry_canary_mode_enabled
            ),
            "production_handlers_registered": (
                facts.production_handlers_registered
            ),
            "independent_control_store_ready": (
                facts.independent_control_store_ready
            ),
            "exact_candidate_binding_count_one": (
                facts.exact_candidate_binding_count == 1
            ),
            "configured_candidate_count_one": (
                facts.configured_candidate_count == 1
            ),
            "api_ingress_ready": facts.api_ingress_ready,
            "stream_ingress_ready": facts.stream_ingress_ready,
            "canary_remains_disabled_during_preparation": (
                not facts.canary_currently_enabled
            ),
        }
    )
    ready = all(checks.values())
    return CanaryPreflightReport(
        ready=ready,
        verdict="READY_FOR_CANARY" if ready else "NO_GO",
        checks=checks,
    )
