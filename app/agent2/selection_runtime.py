from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Awaitable, Callable

from app.agent2.command_planner_v3 import TypedBusinessCommand
from app.agent2.conversation_state import ConversationState
from app.agent2.operation_outcomes import OperationOutcome, OutcomeReplyComposer
from app.agent2.selection_continuation import bind_selected_business_command
from app.agent2.selection_pending import (
    SelectionCandidateValidator,
    SelectionContext,
    SelectionPendingResolver,
    SelectionResolution,
    settle_selection,
)


SelectionExecutor = Callable[[TypedBusinessCommand], Awaitable[OperationOutcome]]


@dataclass(frozen=True)
class SelectionTurnResult:
    handled: bool
    resolution: SelectionResolution
    state_after: ConversationState
    outcome: OperationOutcome | None
    reply: str
    actual_write: bool


class SelectionTurnCoordinator:
    """Validate, bind, execute and settle exactly one saved selection."""

    def __init__(self, *, composer: OutcomeReplyComposer | None = None):
        self._composer = composer or OutcomeReplyComposer()

    async def handle(
        self,
        state: ConversationState,
        *,
        answer: str,
        context: SelectionContext,
        validator: SelectionCandidateValidator,
        executor: SelectionExecutor,
    ) -> SelectionTurnResult:
        resolution = await SelectionPendingResolver().resolve(
            state.selection_pending,
            answer=answer,
            context=context,
            validator=validator,
        )
        if resolution.status != "selected":
            state_after = _replace_pending_if_changed(state, resolution)
            return SelectionTurnResult(
                handled=bool(state.selection_pending),
                resolution=resolution,
                state_after=state_after,
                outcome=None,
                reply=_resolution_reply(resolution),
                actual_write=False,
            )
        pending = next(
            item for item in state.selection_pending if item.pending_id == resolution.pending_id
        )
        candidate = next(
            item for item in pending.candidates
            if item.stable_id == resolution.selected_candidate_id
        )
        command = bind_selected_business_command(pending, candidate)
        if not command.admission_ticket:
            # Legacy Selection continuation has no authority for the current
            # reply.  The fresh-Admission coordinator must mint and persist a
            # current-message Ticket before this executor path is reachable.
            invalid_pending = replace(
                pending,
                status="invalidated",
                invalidation_reason="fresh_selection_admission_required",
            )
            blocked_resolution = replace(
                resolution,
                status="invalidated",
                reason="fresh_selection_admission_required",
                pending_after=invalid_pending,
                actual_write=False,
            )
            state_after = replace(
                state,
                version=state.version + 1,
                selection_pending=tuple(
                    invalid_pending if item.pending_id == pending.pending_id else item
                    for item in state.selection_pending
                ),
            )
            return SelectionTurnResult(
                handled=True,
                resolution=blocked_resolution,
                state_after=state_after,
                outcome=None,
                reply="选择已识别，但本次回复还没有通过新的安全准入；这次没有写入。",
                actual_write=False,
            )
        outcome = await executor(command)
        settlement = settle_selection(pending, resolution, outcome, settled_at=context.now)
        settled_resolution = replace(
            resolution,
            pending_after=settlement.pending_after,
            actual_write=outcome.actual_write,
        )
        pendings = tuple(
            settlement.pending_after if item.pending_id == pending.pending_id else item
            for item in state.selection_pending
        )
        state_after = replace(state, version=state.version + 1, selection_pending=pendings)
        return SelectionTurnResult(
            handled=True,
            resolution=settled_resolution,
            state_after=state_after,
            outcome=outcome,
            reply=self._composer.compose((outcome,)),
            actual_write=outcome.actual_write,
        )


def _replace_pending_if_changed(
    state: ConversationState,
    resolution: SelectionResolution,
) -> ConversationState:
    changed = any(
        item.pending_id == resolution.pending_id and item != resolution.pending_after
        for item in state.selection_pending
    )
    if not changed:
        return state
    return replace(
        state,
        version=state.version + 1,
        selection_pending=tuple(
            resolution.pending_after if item.pending_id == resolution.pending_id else item
            for item in state.selection_pending
        ),
    )


def _resolution_reply(resolution: SelectionResolution) -> str:
    if resolution.status == "clarification_required":
        return "我还不能确定你指的是哪一项，请说具体名称或序号；这次没有写入。"
    if resolution.status == "expired":
        return "刚才的选择已过期，请重新发起操作；这次没有写入。"
    if resolution.status == "already_consumed":
        return "刚才的选择已经处理过了，这次没有重复写入。"
    return "候选对象已发生变化或你已无权操作，请重新发起；这次没有写入。"
