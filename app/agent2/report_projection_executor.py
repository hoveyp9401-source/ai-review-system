from __future__ import annotations

from typing import Protocol

from app.agent2.operation_outcomes import OperationOutcome
from app.agent2.report_projection_policy import ReportProjectionDecision


class ReportProjectionStore(Protocol):
    async def create_request(
        self,
        decision: ReportProjectionDecision,
        case_outcome: OperationOutcome,
    ) -> str: ...

    async def mark_succeeded(
        self, request_id: str, report_outcome: OperationOutcome
    ) -> None: ...

    async def mark_failed(
        self, request_id: str, report_outcome: OperationOutcome
    ) -> None: ...


class UnifiedReportProjectionExecutor(Protocol):
    async def execute_report_projection(
        self,
        decision: ReportProjectionDecision,
        request_id: str,
    ) -> OperationOutcome: ...


def _has_committed_case_receipt(outcome: OperationOutcome) -> bool:
    return (
        outcome.domain == "case_progress"
        and outcome.business_status in {"succeeded", "duplicate"}
        and outcome.actual_write
        and any(
            item.receipt_type == "database"
            and item.status == "executed"
            and item.actual_write
            for item in outcome.receipt_refs
        )
    )


class DurableReportProjectionExecutor:
    """Durably records derivation intent before invoking the Report Domain."""

    def __init__(
        self,
        *,
        store: ReportProjectionStore,
        report_executor: UnifiedReportProjectionExecutor,
    ) -> None:
        self._store = store
        self._report_executor = report_executor

    async def execute_projection(
        self,
        decision: ReportProjectionDecision,
        case_outcome: OperationOutcome,
    ) -> OperationOutcome:
        if not decision.eligible:
            raise ValueError("ineligible projection cannot execute")
        if not _has_committed_case_receipt(case_outcome):
            raise ValueError("projection requires a committed case receipt")

        request_id = await self._store.create_request(decision, case_outcome)
        report_outcome = await self._report_executor.execute_report_projection(
            decision, request_id
        )
        if report_outcome.business_status == "succeeded" and report_outcome.actual_write:
            await self._store.mark_succeeded(request_id, report_outcome)
        elif report_outcome.business_status == "duplicate" and report_outcome.receipt_refs:
            await self._store.mark_succeeded(request_id, report_outcome)
        else:
            await self._store.mark_failed(request_id, report_outcome)
        return report_outcome
