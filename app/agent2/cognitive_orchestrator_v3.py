from __future__ import annotations

from dataclasses import dataclass, replace

from app.agent2.command_planner_v3 import (
    CognitiveCommandPlan,
    CognitiveCommandPlanner,
    CommandPlanningContext,
)
from app.agent2.cognitive_core_v3 import CognitiveCoreV3, CognitiveDecisionV3, CognitiveTurn
from app.agent2.conversation_state import ConversationState
from app.agent2.conversation_state_store import ConversationStateStore
from app.agent2.selection_pending import SelectionPending


@dataclass(frozen=True)
class CognitiveOrchestrationResult:
    decision: CognitiveDecisionV3
    base_state: ConversationState
    state: ConversationState
    command_plan: CognitiveCommandPlan
    state_persisted: bool


class CognitiveOrchestratorV3:
    """Deep module for state load -> cognition -> typed planning -> optimistic save."""

    def __init__(
        self,
        *,
        core: CognitiveCoreV3,
        planner: CognitiveCommandPlanner,
        state_store: ConversationStateStore,
    ):
        self._core = core
        self._planner = planner
        self._state_store = state_store

    async def process(
        self,
        turn: CognitiveTurn,
        planning_context: CommandPlanningContext,
    ) -> CognitiveOrchestrationResult:
        if planning_context.message_id != turn.message_id:
            raise ValueError("cognitive turn and command planning message ids must match")
        state = await self._state_store.load(
            user_id=turn.user_id,
            conversation_id=turn.conversation_id,
        )
        core_result = await self._core.process(turn, state)
        command_plan = self._planner.plan(
            core_result.decision,
            replace(planning_context, user_constraints=core_result.state.user_constraints),
        )
        requires_execution_outcome = bool(
            command_plan.daily_commands
            or command_plan.business_commands
            or command_plan.report_commands
            or command_plan.blocked_actions
            or _is_enforced_nonadvancing_admission(core_result.decision)
        )
        result_state = core_result.state
        state_persisted = False
        if not requires_execution_outcome:
            result_state = await self._state_store.save(
                core_result.state,
                expected_version=state.version,
            )
            state_persisted = True
        return CognitiveOrchestrationResult(
            decision=core_result.decision,
            base_state=state,
            state=result_state,
            command_plan=command_plan,
            state_persisted=state_persisted,
        )


async def finalize_cognitive_state_after_execution(
    *,
    result: CognitiveOrchestrationResult,
    state_store: ConversationStateStore,
    execution_succeeded: bool,
    selection_pending: tuple[SelectionPending, ...] = (),
) -> ConversationState:
    """Persist one proposed turn state only after all typed outcomes succeed.

    The cognitive core proposes a version that is exactly one ahead of the
    loaded base state. Command-bearing and planner-blocked turns are not saved
    by the orchestrator. A failed or partial execution therefore returns the
    untouched base state; a successful execution removes any consumed bound
    pending and performs one optimistic save against the base version.
    """

    if result.state_persisted:
        return result.state
    if (
        _is_enforced_nonadvancing_admission(result.decision)
        and not _has_planned_business_effect(result.command_plan)
        and not selection_pending
    ):
        return result.base_state
    if not execution_succeeded:
        if not selection_pending:
            return result.base_state
        # A SelectionPending is itself a durable, user-visible state change,
        # but it is not evidence that any sibling command committed.  Build the
        # pending-only transition from the loaded base so proposed goals,
        # entities, constraints, consumed pendings, and recent context from a
        # failed/partial execution cannot leak into durable conversation state.
        existing_selection = {
            item.pending_id: item for item in result.base_state.selection_pending
        }
        existing_selection.update(
            {item.pending_id: item for item in selection_pending}
        )
        pending_only_state = replace(
            result.base_state,
            version=result.base_state.version + 1,
            selection_pending=tuple(existing_selection.values()),
        )
        return await state_store.save(
            pending_only_state,
            expected_version=result.base_state.version,
        )
    consumed = set(result.decision.context_update.consumed_pending_ids)
    next_state = (
        replace(
            result.state,
            pending=tuple(
                item for item in result.state.pending if item.pending_id not in consumed
            ),
        )
        if consumed
        else result.state
    )
    if selection_pending:
        existing = {
            item.pending_id: item for item in next_state.selection_pending
        }
        existing.update({item.pending_id: item for item in selection_pending})
        next_state = replace(next_state, selection_pending=tuple(existing.values()))
    return await state_store.save(next_state, expected_version=result.base_state.version)


def _is_enforced_nonadvancing_admission(decision: CognitiveDecisionV3) -> bool:
    if str(getattr(decision, "admission_mode", "disabled") or "") != "enforced":
        return False
    if tuple(getattr(decision, "admission_information_pendings", ()) or ()):
        return True
    trace = getattr(decision, "admission_trace", None)
    return any(
        str(getattr(item, "status", "") or "")
        in {"blocked", "information_required", "review_only", "deferred_audit_only"}
        for item in tuple(getattr(trace, "decisions", ()) or ())
    )


def _has_planned_business_effect(command_plan: CognitiveCommandPlan) -> bool:
    return bool(
        command_plan.daily_commands
        or command_plan.business_commands
        or command_plan.report_commands
    )
