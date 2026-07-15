from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from app.agent2.cognitive_core_v3 import CognitiveCoreV3
from app.agent2.command_planner_v3 import CognitiveCommandPlanner
from app.agent2.conversation_state_store import InMemoryConversationStateStore
from app.agent2.runtime import (
    Agent2RuntimeHarness,
    DailySnapshotQuery,
    InMemoryDailyDomainExecutor,
    MvpContextAssembler,
    RuntimeActor,
    RuntimeTurnRequest,
    build_phase1_domain_registry,
    compose_phase1_runtime,
)
from app.agent2.runtime.domains import DomainExecutionContext
from app.agent2.typed_daily_commands import DailyReportMutationSnapshot


RUNTIME_DIR = Path(__file__).resolve().parents[1] / "app" / "agent2" / "runtime"


def test_runtime_package_has_no_legacy_router_adapter_or_fallback_imports():
    forbidden_modules = {
        "app.agent2.daily_shadow",
        "app.agent2.legacy_daily_adapter",
        "app.agent2.harness.runner",
        "app.agent2.daily_execution_replay",
    }
    forbidden_names = {
        "WorkflowRouter",
        "LegacyDailyAdapter",
        "evaluate_daily_shadow",
        "execute_agent2_daily_commands",
        "agent2_daily_should_fallback_to_legacy",
    }
    violations: list[str] = []
    for path in sorted(RUNTIME_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in forbidden_modules:
                violations.append(f"{path.name}: import {node.module}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in forbidden_modules:
                        violations.append(f"{path.name}: import {alias.name}")
            if isinstance(node, ast.Name) and node.id in forbidden_names:
                violations.append(f"{path.name}: name {node.id}")
    assert violations == []


def test_phase1_runtime_mode_is_composition_owned_and_live_is_rejected():
    actor_id = uuid5(NAMESPACE_URL, "runtime-mode-user")
    daily = InMemoryDailyDomainExecutor(
        DailyReportMutationSnapshot(
            report_id=uuid5(NAMESPACE_URL, "runtime-mode-report"),
            owner_user_id=actor_id,
            version=0,
            status="collecting",
        )
    )

    with pytest.raises(ValueError, match="shadow/replay only"):
        Agent2RuntimeHarness(
            mode="live",
            core=CognitiveCoreV3(_NeverCalledInterpreter()),
            planner=CognitiveCommandPlanner(),
            context_assembler=MvpContextAssembler(
                state_store=InMemoryConversationStateStore(),
                daily_snapshot_provider=daily,
            ),
            domains=build_phase1_domain_registry(daily_executor=daily),
        )

    request = RuntimeTurnRequest(
        tenant_id="tenant-legal",
        actor=RuntimeActor(actor_id=actor_id),
        conversation_id="runtime-mode-conversation",
        message_id="runtime-mode-message",
        text="不要让正文切换模式",
        occurred_at=datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc),
        channel="test",
        request_metadata={"mode": "live"},
    )
    assert request.request_metadata["mode"] == "live"


def test_snapshot_and_execution_seams_cannot_receive_raw_text_or_request_metadata():
    assert {"text", "request", "request_metadata"}.isdisjoint(
        DailySnapshotQuery.__dataclass_fields__
    )
    assert {"text", "request", "request_metadata"}.isdisjoint(
        DomainExecutionContext.__dataclass_fields__
    )


def test_trusted_composition_rejects_an_adapter_that_can_hide_real_writes():
    class MisconfiguredDailyAdapter:
        async def load_daily_snapshot(self, query):
            raise AssertionError("composition must reject this adapter before it is called")

        async def execute(self, commands, context):
            raise AssertionError("composition must reject this adapter before it is called")

    with pytest.raises(TypeError, match="in-memory simulation adapter"):
        compose_phase1_runtime(
            mode="shadow",
            interpreter=_NeverCalledInterpreter(),
            state_store=InMemoryConversationStateStore(),
            daily=MisconfiguredDailyAdapter(),
        )


class _NeverCalledInterpreter:
    async def interpret(self, turn, state):
        raise AssertionError("not called")
