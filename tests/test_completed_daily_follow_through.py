from __future__ import annotations

from datetime import date, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from app.agent2.tool_calling.completed_daily_follow_through import (
    CompletedDailyFollowThrough,
    CompletedDailyFollowThroughState,
    ModelCallBudgetExceeded,
    TurnModelCallBudget,
)
from app.agent2.tool_calling.context import (
    CANARY_STATE_NAMESPACE,
    TrustedContext,
    TrustedPrincipal,
    TrustedReportItem,
    TrustedReportSnapshot,
)
from app.agent2.tool_calling.contracts import (
    ExecutionMode,
    ReceiptStatus,
    ToolReceipt,
)
from app.agent2.tool_calling.production_runtime import _safe_report_snapshot
from app.agent2.tool_calling.receipt_provenance import principal_scope_sha256


_USER_ID = UUID("10000000-0000-4000-8000-000000000004")
_REPORT_ID = UUID("20000000-0000-4000-8000-000000000004")


def _context_and_receipt() -> tuple[TrustedContext, ToolReceipt]:
    principal = TrustedPrincipal(
        tenant_id="tenant-a",
        user_id=_USER_ID,
        conversation_id="direct-user-a",
        source_message_id="message-current",
        timezone="Asia/Shanghai",
        conversation_kind="direct",
    )
    report = TrustedReportSnapshot(
        report_id=_REPORT_ID,
        tenant_id=principal.tenant_id,
        owner_user_id=principal.user_id,
        report_date=date(2026, 8, 11),
        version=4,
        status="completed",
        items=(
            TrustedReportItem(
                item_id="today-1",
                field="today_work",
                content="完成合同初稿复核",
                report_id=_REPORT_ID,
                report_version=4,
            ),
        ),
    )
    context = TrustedContext(
        namespace=CANARY_STATE_NAMESPACE,
        now=datetime(2026, 8, 12, 16, 42, tzinfo=ZoneInfo("Asia/Shanghai")),
        principal=principal,
        allowed_tool_names=frozenset(
            {"query_report_by_date", "edit_daily_items"}
        ),
        gate_decisions={
            "query_report_by_date": True,
            "edit_daily_items": True,
        },
    )
    receipt = ToolReceipt(
        status=ReceiptStatus.SUCCESS,
        tool_name="query_report_by_date",
        changed=False,
        target_type="daily_report",
        target_id=str(report.report_id),
        before_version=report.version,
        after_version=report.version,
        affected_item_ids=(),
        safe_user_facts={
            "actual_write": False,
            "report_found": True,
            "report_snapshot": _safe_report_snapshot(report),
            "report_date": report.report_date.isoformat(),
        },
        server_evidence={
            "principal_scope_sha256": principal_scope_sha256(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                conversation_id=principal.conversation_id,
                source_message_id=principal.source_message_id,
            )
        },
        execution_mode=ExecutionMode.CANARY_EXECUTE,
    )
    return context, receipt


def test_completed_daily_flow_owns_its_four_state_lifecycle() -> None:
    context, receipt = _context_and_receipt()
    flow = CompletedDailyFollowThrough(
        context=context,
        user_text="把昨天日报第一条改成完成合同终稿复核",
        user_messages=("把昨天日报第一条改成完成合同终稿复核",),
    )

    assert flow.state == CompletedDailyFollowThroughState.IDLE
    inspection = flow.inspect_tool_turn(
        calls=(),
        reviewed_calls=(),
        receipts=(receipt,),
        default_review_tool_names=frozenset(),
    )
    assert inspection.integrity_error is None
    assert flow.state == CompletedDailyFollowThroughState.COMPLETED_LOADED

    flow.activate(
        candidate_reply="已经查到日报。",
        allowed_write_tool_names=frozenset({"edit_daily_items"}),
    )
    assert flow.state == CompletedDailyFollowThroughState.CONTINUATION_OPEN
    schemas = flow.prepare_model_tools([], tools_disabled=False)
    assert [schema["function"]["name"] for schema in schemas] == [
        "edit_daily_items"
    ]

    flow.record_state(receipts=(receipt,), current_has_write=True)
    assert flow.state == CompletedDailyFollowThroughState.WRITE_PENDING


def test_completed_daily_flow_rejects_a_receipt_from_another_principal() -> None:
    context, receipt = _context_and_receipt()
    forged = receipt.model_copy(
        update={
            "server_evidence": {
                "principal_scope_sha256": principal_scope_sha256(
                    tenant_id=context.principal.tenant_id,
                    user_id=UUID("10000000-0000-4000-8000-000000000099"),
                    conversation_id=context.principal.conversation_id,
                    source_message_id=context.principal.source_message_id,
                )
            }
        }
    )
    flow = CompletedDailyFollowThrough(
        context=context,
        user_text="把昨天日报第一条改一下",
        user_messages=("把昨天日报第一条改一下",),
    )

    inspection = flow.inspect_tool_turn(
        calls=(),
        reviewed_calls=(),
        receipts=(forged,),
        default_review_tool_names=frozenset(),
    )

    assert inspection.integrity_error == (
        "successful Daily query returned an inconsistent snapshot"
    )
    assert flow.state == CompletedDailyFollowThroughState.IDLE


def test_turn_model_call_budget_has_one_explicit_hard_limit() -> None:
    budget = TurnModelCallBudget(limit=9)

    assert [budget.claim() for _ in range(9)] == list(range(1, 10))
    with pytest.raises(ModelCallBudgetExceeded, match="budget exceeded"):
        budget.claim()
