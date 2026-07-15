from __future__ import annotations

from typing import Mapping, Protocol, Sequence

from app.agent2.cognitive_core_v3 import CognitiveCoreV3, SemanticInterpreter
from app.agent2.command_planner_v3 import CognitiveCommandPlanner
from app.agent2.conversation_state_store import ConversationStateStore

from .context import DailySnapshotProvider, MvpContextAssembler
from .contracts import RuntimeAuditSink, RuntimeMode
from .domains import (
    DomainPackExecutor,
    InMemoryDailyDomainExecutor,
    build_phase1_domain_registry,
)
from .harness import Agent2RuntimeHarness


class Phase1DailyAdapter(DailySnapshotProvider, DomainPackExecutor, Protocol):
    pass


def compose_phase1_runtime(
    *,
    mode: RuntimeMode,
    interpreter: SemanticInterpreter,
    state_store: ConversationStateStore,
    daily: Phase1DailyAdapter,
    daily_policy: Mapping[str, object] | None = None,
    active_tasks: Sequence[Mapping[str, object]] = (),
    audit_sink: RuntimeAuditSink | None = None,
) -> Agent2RuntimeHarness:
    """Trusted composition root for the Phase-1 Shadow/Replay Runtime.

    `daily` deliberately has to satisfy both the typed planning snapshot seam
    and the DomainPack execution seam. RuntimeTurnRequest cannot provide or
    replace any of these dependencies.
    """

    # Phase 1 has no write credential by construction.  Structural protocol
    # checks are insufficient here: an arbitrary adapter could perform a real
    # write before returning a forged ``actual_write=False`` receipt.  Keep the
    # trusted root closed over the repository-owned simulator until a later
    # phase introduces a separately reviewed capability boundary.
    if type(daily) is not InMemoryDailyDomainExecutor:
        raise TypeError("Phase-1 Runtime requires the trusted in-memory simulation adapter")
    return Agent2RuntimeHarness(
        mode=mode,
        core=CognitiveCoreV3(interpreter),
        planner=CognitiveCommandPlanner(),
        context_assembler=MvpContextAssembler(
            state_store=state_store,
            daily_snapshot_provider=daily,
            daily_policy=daily_policy,
            active_tasks=tuple(active_tasks),
        ),
        domains=build_phase1_domain_registry(daily_executor=daily),
        audit_sink=audit_sink,
    )
