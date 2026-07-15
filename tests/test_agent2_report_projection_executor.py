from datetime import date

import pytest

from app.agent2.operation_outcomes import (
    OperationOutcome,
    OutcomeObjectRef,
    OutcomeReceiptRef,
    OutcomeStateTransition,
)
from app.agent2.report_projection_executor import DurableReportProjectionExecutor
from app.agent2.report_projection_policy import ReportProjectionDecision


class _Store:
    def __init__(self):
        self.events = []

    async def create_request(self, decision, case_outcome):
        self.events.append(("request", decision.decision_id))
        return "request-1"

    async def mark_succeeded(self, request_id, report_outcome):
        self.events.append(("succeeded", request_id))

    async def mark_failed(self, request_id, report_outcome):
        self.events.append(("failed", request_id))


class _ReportExecutor:
    def __init__(self, outcome, store):
        self.outcome = outcome
        self.store = store

    async def execute_report_projection(self, decision, request_id):
        assert self.store.events == [("request", decision.decision_id)]
        return self.outcome


def _outcome(domain, status, actual_write, receipt=""):
    return OperationOutcome(
        domain=domain, operation="create",
        object_ref=OutcomeObjectRef(domain, f"{domain}-1", domain),
        business_status=status, message_status="not_applicable",
        changed_fields=("content",) if actual_write else (),
        user_visible_snapshot={"content": "今天提交补充材料。"},
        blocking_reason="" if actual_write else "version_conflict",
        receipt_refs=(OutcomeReceiptRef(receipt, "database", "executed", True),) if receipt else (),
        state_transition=OutcomeStateTransition("before", "after"),
        actual_write=actual_write,
    )


def _decision():
    return ReportProjectionDecision(
        decision_id="decision-1", eligible=True, projection_mode="automatic",
        report_type="daily", section="today_work", report_date=date(2026, 7, 13),
        normalized_fact="今天提交补充材料。", source_case_id="case-1",
        source_progress_id="progress-1", source_followup_id="followup-1",
        source_turn_id="turn-1", reason_code="completed_work_today", confidence=0.98,
        duplicate_of="", policy_version="v1",
    )


@pytest.mark.asyncio
async def test_projection_request_is_durable_before_unified_report_executor_runs():
    store = _Store()
    report_outcome = _outcome("report", "succeeded", True, "report-receipt")
    executor = DurableReportProjectionExecutor(
        store=store, report_executor=_ReportExecutor(report_outcome, store)
    )

    result = await executor.execute_projection(
        _decision(), _outcome("case_progress", "succeeded", True, "case-receipt")
    )

    assert result is report_outcome
    assert store.events == [("request", "decision-1"), ("succeeded", "request-1")]


@pytest.mark.asyncio
async def test_failed_projection_is_retained_for_audit_and_does_not_become_success():
    store = _Store()
    report_outcome = _outcome("report", "failed", False)
    executor = DurableReportProjectionExecutor(
        store=store, report_executor=_ReportExecutor(report_outcome, store)
    )

    result = await executor.execute_projection(
        _decision(), _outcome("case_progress", "succeeded", True, "case-receipt")
    )

    assert result.business_status == "failed"
    assert store.events[-1] == ("failed", "request-1")
