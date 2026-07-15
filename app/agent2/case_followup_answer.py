from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.agent2.case_fact_validation import validate_case_fact_extraction
from app.agent2.case_followup_pending import CaseFollowupPendingResolution
from app.agent2.operation_outcomes import (
    OperationOutcome,
    OutcomeObjectRef,
    OutcomeStateTransition,
)
from app.agent2.report_projection_policy import (
    CaseFactExtraction,
    ReportProjectionContext,
    ReportProjectionDecision,
    ReportProjectionPolicy,
)


class CaseFactWriter(Protocol):
    async def write_case_fact(
        self,
        fact: CaseFactExtraction,
        resolution: CaseFollowupPendingResolution,
    ) -> OperationOutcome: ...


class ReportProjectionExecutor(Protocol):
    async def execute_projection(
        self,
        decision: ReportProjectionDecision,
        case_outcome: OperationOutcome,
    ) -> OperationOutcome: ...


@dataclass(frozen=True)
class CaseFollowupAnswerResult:
    outcomes: tuple[OperationOutcome, ...]
    projection_decision: ReportProjectionDecision | None
    followup_completed: bool


def _has_committed_database_receipt(outcome: OperationOutcome) -> bool:
    return outcome.actual_write and any(
        receipt.receipt_type == "database"
        and receipt.status == "executed"
        and receipt.actual_write
        for receipt in outcome.receipt_refs
    )


class CaseFollowupAnswerCoordinator:
    """Coordinates the primary case write and its optional derived projection.

    The report path is deliberately downstream of a committed case receipt.  It
    can fail independently and never changes the already-committed case outcome.
    """

    def __init__(
        self,
        *,
        case_writer: CaseFactWriter,
        projection_policy: ReportProjectionPolicy,
        projection_executor: ReportProjectionExecutor,
    ) -> None:
        self._case_writer = case_writer
        self._projection_policy = projection_policy
        self._projection_executor = projection_executor

    async def handle(
        self,
        resolution: CaseFollowupPendingResolution,
        fact: CaseFactExtraction,
        *,
        projection_context: ReportProjectionContext,
    ) -> CaseFollowupAnswerResult:
        if resolution.status != "bound":
            raise ValueError("case follow-up answer requires a revalidated pending")
        if fact.case_id != resolution.case_id:
            raise ValueError("extracted fact does not match the bound case")
        if fact.actor_user_id != resolution.pending_after.user_id:
            raise ValueError("extracted fact actor does not match the pending user")

        validation = validate_case_fact_extraction(fact)
        if not validation.valid:
            blocked = OperationOutcome(
                domain="case_progress",
                operation="create",
                object_ref=OutcomeObjectRef(
                    "case_progress", "", resolution.pending_after.case_id
                ),
                business_status="blocked",
                message_status="not_applicable",
                changed_fields=(),
                user_visible_snapshot={
                    "case_name": resolution.pending_after.case_id,
                    "content": fact.raw_text,
                },
                blocking_reason=",".join(validation.reason_codes),
                receipt_refs=(),
                state_transition=OutcomeStateTransition("awaiting_input", "awaiting_input"),
                actual_write=False,
                source_turn_id=resolution.intent.source_message_id,
                tenant_id=resolution.pending_after.tenant_id,
                user_id=resolution.pending_after.user_id,
                conversation_id=resolution.pending_after.conversation_id,
                metadata={"raw_text_hash": validation.raw_text_hash},
            )
            return CaseFollowupAnswerResult((blocked,), None, False)

        case_outcome = await self._case_writer.write_case_fact(fact, resolution)
        case_committed = (
            case_outcome.business_status in {"succeeded", "duplicate"}
            and _has_committed_database_receipt(case_outcome)
        )
        if not case_committed:
            return CaseFollowupAnswerResult((case_outcome,), None, False)

        decision = self._projection_policy.decide(
            fact,
            projection_context,
            source_progress_id=case_outcome.object_ref.stable_id,
            source_followup_id=resolution.followup_id,
        )
        outcomes: tuple[OperationOutcome, ...] = (case_outcome,)
        if decision.eligible:
            report_outcome = await self._projection_executor.execute_projection(
                decision, case_outcome
            )
            outcomes = outcomes + (report_outcome,)

        return CaseFollowupAnswerResult(outcomes, decision, True)
