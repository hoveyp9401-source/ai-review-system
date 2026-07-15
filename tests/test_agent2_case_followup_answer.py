from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from app.agent2.case_followup_answer import CaseFollowupAnswerCoordinator
from app.agent2.case_followup_pending import (
    CaseFollowupPendingResolution,
    CaseFollowupPendingSnapshot,
    CaseFollowupReplyIntent,
)
from app.agent2.operation_outcomes import (
    OperationOutcome,
    OutcomeObjectRef,
    OutcomeReceiptRef,
    OutcomeStateTransition,
)
from app.agent2.report_projection_policy import (
    CaseFactExtraction,
    ReportProjectionContext,
    ReportProjectionPolicy,
)


def _outcome(domain, operation, status, *, actual_write, label, receipt=""):
    return OperationOutcome(
        domain=domain,
        operation=operation,
        object_ref=OutcomeObjectRef(domain, f"{domain}-1", label),
        business_status=status,
        message_status="not_applicable",
        changed_fields=("summary",) if actual_write else (),
        user_visible_snapshot={
            "case_name": label,
            "content": "今天联系法院，下周重新查控。",
        },
        blocking_reason="" if status == "succeeded" else "version_conflict",
        receipt_refs=(
            OutcomeReceiptRef(receipt, "database", "executed", True),
        ) if receipt else (),
        state_transition=OutcomeStateTransition("before", "after"),
        actual_write=actual_write,
    )


class _CaseWriter:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = 0

    async def write_case_fact(self, fact, resolution):
        self.calls += 1
        return self.outcome


class _ProjectionExecutor:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = 0

    async def execute_projection(self, decision, case_outcome):
        self.calls += 1
        return self.outcome


def _bound_resolution():
    pending = CaseFollowupPendingSnapshot(
        "pending-1", "tenant-a", "user-1", "conversation-1", "task-1",
        "case-1", "followup-1", 3, 7, "provider-message-1",
        datetime(2026, 7, 20, tzinfo=timezone.utc), "awaiting_input", 1,
    )
    return CaseFollowupPendingResolution(
        "bound", "pending_revalidated", "pending-1", "case-1", "followup-1",
        CaseFollowupReplyIntent(
            "case_fact", "reply-1", "今天联系法院，下周重新查控。"
        ),
        False,
        pending,
    )


def _case_fact():
    raw_text = "今天联系法院，下周重新查控。"
    return CaseFactExtraction(
        case_id="case-1",
        actor_user_id="user-1",
        raw_text=raw_text,
        normalized_fact="今天联系法院，下周重新查控。",
        factual_progress=("下周重新查控",),
        completed_actions=("联系法院",),
        next_actions=(),
        action_time_scope="today",
        report_preference="automatic",
        confidence=0.98,
        evidence_spans=((0, len(raw_text)),),
    )


def _projection_context():
    return ReportProjectionContext(
        "tenant-a", "user-1", "reply-1", date(2026, 7, 13),
        True, True, "", True, 0.9,
    )


@pytest.mark.asyncio
async def test_case_success_is_preserved_when_report_projection_fails():
    case_outcome = _outcome(
        "case_progress", "create", "succeeded", actual_write=True,
        label="南京工程款案", receipt="case-receipt",
    )
    report_outcome = _outcome(
        "report", "create", "failed", actual_write=False, label="今日日报",
    )
    projection_executor = _ProjectionExecutor(report_outcome)
    coordinator = CaseFollowupAnswerCoordinator(
        case_writer=_CaseWriter(case_outcome),
        projection_policy=ReportProjectionPolicy(),
        projection_executor=projection_executor,
    )

    result = await coordinator.handle(
        _bound_resolution(), _case_fact(), projection_context=_projection_context()
    )

    assert [item.business_status for item in result.outcomes] == ["succeeded", "failed"]
    assert result.outcomes[0].receipt_refs[0].receipt_id == "case-receipt"
    assert projection_executor.calls == 1
    assert result.followup_completed is True


@pytest.mark.asyncio
async def test_case_failure_prevents_report_projection():
    case_outcome = _outcome(
        "case_progress", "create", "failed", actual_write=False,
        label="南京工程款案",
    )
    projection_executor = _ProjectionExecutor(
        _outcome("report", "create", "succeeded", actual_write=True,
                 label="今日日报", receipt="report-receipt")
    )
    coordinator = CaseFollowupAnswerCoordinator(
        case_writer=_CaseWriter(case_outcome),
        projection_policy=ReportProjectionPolicy(),
        projection_executor=projection_executor,
    )

    result = await coordinator.handle(
        _bound_resolution(), _case_fact(), projection_context=_projection_context()
    )

    assert result.outcomes == (case_outcome,)
    assert projection_executor.calls == 0
    assert result.followup_completed is False


@pytest.mark.asyncio
async def test_ungrounded_normalization_is_blocked_before_case_writer():
    case_writer = _CaseWriter(
        _outcome("case_progress", "create", "succeeded", actual_write=True,
                 label="南京工程款案", receipt="case-receipt")
    )
    projection_executor = _ProjectionExecutor(
        _outcome("report", "create", "succeeded", actual_write=True,
                 label="今日日报", receipt="report-receipt")
    )
    coordinator = CaseFollowupAnswerCoordinator(
        case_writer=case_writer, projection_policy=ReportProjectionPolicy(),
        projection_executor=projection_executor,
    )
    fact = replace(
        _case_fact(),
        raw_text="今天联系法院，下周重新查控。",
        normalized_fact="今天联系法院，明天重新查控。",
    )

    result = await coordinator.handle(
        _bound_resolution(), fact, projection_context=_projection_context()
    )

    assert result.outcomes[0].business_status == "blocked"
    assert result.outcomes[0].blocking_reason == "ungrounded_protected_fact,protected_fact_removed"
    assert case_writer.calls == 0
    assert projection_executor.calls == 0
