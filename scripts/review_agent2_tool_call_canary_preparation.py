from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

from app.agent2.tool_calling.contracts import ExecutionMode
from app.agent2.tool_calling.canary_preflight import (
    CanaryPreflightFacts,
    evaluate_canary_preflight,
)
from app.agent2.tool_calling.registry import (
    TOOL_REGISTRY,
    registry_contract_digest,
)
from scripts.replay_agent2_tool_call_shadow import SYSTEM_PROMPT


REPO_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_EVIDENCE = (
    REPO_ROOT
    / "evals"
    / "agent2"
    / "tool_call_canary"
    / "review_preparation"
    / "production_read_only_preflight.json"
)
SHADOW_BASELINE = (
    REPO_ROOT
    / "evals"
    / "agent2"
    / "tool_call_shadow"
    / "phase16c"
    / "frozen_baseline.json"
)
SANDBOX_BASELINE = (
    REPO_ROOT
    / "evals"
    / "agent2"
    / "tool_call_sandbox"
    / "phase_b1_real_postgresql"
    / "phase_b1_verification_summary.json"
)


def build_review_report() -> dict[str, Any]:
    production = _read_json(PRODUCTION_EVIDENCE)
    shadow = _read_json(SHADOW_BASELINE)
    sandbox = _read_json(SANDBOX_BASELINE)
    registry_digest = registry_contract_digest()
    prompt_digest = hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest()
    model = dict(shadow["model_configuration"])
    webhook_source = (REPO_ROOT / "app" / "api" / "webhook.py").read_text(
        encoding="utf-8"
    )
    stream_source = (REPO_ROOT / "app" / "stream_runner.py").read_text(
        encoding="utf-8"
    )

    canary_definitions = tuple(
        definition
        for definition in TOOL_REGISTRY.values()
        if ExecutionMode.CANARY_EXECUTE in definition.enabled_modes
    )
    production_definitions = tuple(
        definition
        for definition in TOOL_REGISTRY.values()
        if definition.production_handler.__module__.endswith(
            "production_handlers"
        )
    )
    registry_canary_mode_enabled = bool(production_definitions) and all(
        ExecutionMode.CANARY_EXECUTE in definition.enabled_modes
        for definition in production_definitions
    )
    production_handlers_registered = (
        bool(canary_definitions)
        and len(canary_definitions) == len(production_definitions)
        and all(
            definition.production_handler.__module__.endswith(
                "production_handlers"
            )
            for definition in canary_definitions
        )
    )
    api_ingress_ready = "tool_call_canary" in webhook_source
    stream_ingress_ready = "tool_call_canary" in stream_source
    runtime_available = (
        REPO_ROOT / "app" / "agent2" / "tool_calling" / "canary_runtime.py"
    ).exists()

    facts = CanaryPreflightFacts(
        production_environment_declared=(
            production["environment"]["app_env"] == "production"
        ),
        production_database_authenticated=bool(
            production["database"]["authenticated_read_only"]
        ),
        production_database_identity_matches_configuration=bool(
            production["database"]["database_name_matches_configuration"]
            and production["database"]["database_role_matches_configuration"]
        ),
        tool_call_sandbox_configuration_present=bool(
            production["environment"]["tool_call_sandbox_keys_present"]
        ),
        message_sender_configured=bool(
            production["environment"][
                "dingtalk_app_sender_credentials_present"
            ]
        ),
        runtime_available=runtime_available,
        runtime_mode=(
            "canary_execute" if runtime_available else "unavailable"
        ),
        registry_digest_matches=(
            registry_digest == sandbox["registry_digest"]
        ),
        prompt_digest_matches=(
            prompt_digest
            == shadow["source_equivalence"]["system_prompt_sha256"]
        ),
        model_config_matches=(
            model["served_model"] == "deepseek-v4-pro"
        ),
        registry_canary_mode_enabled=registry_canary_mode_enabled,
        production_handlers_registered=production_handlers_registered,
        independent_control_store_ready=False,
        exact_candidate_binding_count=int(
            production["candidate_identity"][
                "exact_binding_count_inside_configured_tenant"
            ]
        ),
        configured_candidate_count=0,
        api_ingress_ready=api_ingress_ready,
        stream_ingress_ready=stream_ingress_ready,
        canary_currently_enabled=bool(
            production["tool_call_core"]["canary_enabled"]
        ),
    )
    preflight = evaluate_canary_preflight(facts)
    checks = dict(preflight.checks)
    blockers = [name for name, passed in checks.items() if not passed]
    head = _git("rev-parse", "HEAD")
    dirty = bool(_git("status", "--short"))
    return {
        "schema_version": "agent2.tool_call_canary.review_preparation.v1",
        "verdict": preflight.verdict,
        "deployment": {
            "working_base_sha": head,
            "candidate_commit_sha": None if dirty else head,
            "working_tree_dirty": dirty,
            "registry_digest": registry_digest,
            "prompt_sha256": prompt_digest,
            "model_configuration": model,
            "deployment_timestamp": None,
            "canary_enabled": False,
        },
        "checks": checks,
        "blockers": blockers,
        "candidate_control_plane": {
            "decision_module": "app/agent2/tool_calling/canary_control.py",
            "store_module": "app/agent2/tool_calling/canary_store.py",
            "migration": "scripts/create_agent2_tool_call_canary_control.sql",
            "metrics_module": "app/agent2/tool_calling/canary_metrics.py",
            "independent_from_old_agent2_route_state": True,
            "migration_applied_to_production": False,
        },
        "production_observation": {
            "evidence": str(PRODUCTION_EVIDENCE.relative_to(REPO_ROOT)),
            "tool_call_core_deployed": bool(
                production["tool_call_core"][
                    "registry_present_on_running_host"
                ]
            ),
            "api_service": production["services"]["api"],
            "stream_service": production["services"]["stream"],
            "production_database_write_count": production["side_effects"][
                "production_database_write_count"
            ],
            "route_change_count": production["side_effects"][
                "route_change_count"
            ],
            "message_send_count": production["side_effects"][
                "message_send_count"
            ],
        },
    }


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _git(*arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the read-only Agent2 Tool-Call Canary review report."
    )
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    report = build_review_report()
    rendered = json.dumps(
        report,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
