from __future__ import annotations

from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from app.agent2.periodic_report_context import (
    TrustedPeriodicReportContext,
    TrustedPeriodicReportItem,
)
from app.agent2.tool_calling.context import TrustedContext, TrustedPrincipal
from app.agent2.tool_calling.contracts import ExecutionMode, ReceiptStatus
from app.agent2.tool_calling.current_turn_source import CurrentTurnSource
from app.agent2.tool_calling.validation import (
    NativeToolCall,
    ShadowCallBinder,
    UnavailableDateResolver,
)


NOW = datetime(2026, 8, 14, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
USER_ID = UUID("10000000-0000-4000-8000-000000000001")
REPORT_ID = UUID("20000000-0000-4000-8000-000000000001")


def _context():
    return TrustedContext(
        namespace="agent2.tool_calling.canary.v1",
        now=NOW,
        principal=TrustedPrincipal(
            tenant_id="tenant-a",
            user_id=USER_ID,
            conversation_id="direct-1",
            source_message_id="message-1",
            timezone="Asia/Shanghai",
            conversation_kind="direct",
        ),
        current_weekly_report=TrustedPeriodicReportContext(
            tenant_id="tenant-a",
            owner_user_id=USER_ID,
            report_id=REPORT_ID,
            report_type="weekly",
            period_key="2026-W33",
            version=2,
            status="collecting",
            items=(
                TrustedPeriodicReportItem(
                    item_id="weekly-item-1",
                    field="accomplishments",
                    content="旧内容",
                ),
            ),
        ),
        allowed_tool_names=frozenset(
            {
                "query_current_weekly_report",
                "apply_current_weekly_report",
                "submit_current_weekly_report",
            }
        ),
        gate_decisions={
            "query_current_weekly_report": True,
            "apply_current_weekly_report": True,
            "submit_current_weekly_report": True,
        },
    )


def _binder(text="补充本周周报：完成合同复核"):
    return ShadowCallBinder(
        _context(),
        UnavailableDateResolver(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
        current_turn_source=CurrentTurnSource((text,)),
    )


def _apply_call(*, report_id=REPORT_ID, version=2, item_id=None):
    operation = (
        {
            "operation_id": "edit-1",
            "operation": "edit",
            "item_id": item_id,
            "replacement": "完成合同复核",
            "source_evidence": {"source_message_index": 1},
        }
        if item_id is not None
        else {
            "operation_id": "append-1",
            "operation": "append",
            "field": "accomplishments",
            "content": "完成合同复核",
            "source_evidence": {"source_message_index": 1},
        }
    )
    return NativeToolCall(
        "call-1",
        "apply_current_weekly_report",
        {
            "report_id": str(report_id),
            "expected_version": version,
            "operations": [operation],
        },
    )


@pytest.mark.asyncio
async def test_binder_attaches_current_weekly_report_to_valid_query_and_write():
    query, query_failure = await _binder("看看我的本周周报").bind(
        NativeToolCall("query-1", "query_current_weekly_report", {})
    )
    write, write_failure = await _binder().bind(_apply_call())

    assert query_failure is None
    assert query.periodic_report.report_id == REPORT_ID
    assert write_failure is None
    assert write.periodic_report.version == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call", "error_code"),
    (
        (
            _apply_call(
                report_id=UUID("30000000-0000-4000-8000-000000000001")
            ),
            "UNTRUSTED_PERIODIC_REPORT_ID",
        ),
        (_apply_call(version=7), "STALE_PERIODIC_REPORT_VERSION"),
        (_apply_call(item_id="forged-item"), "UNTRUSTED_PERIODIC_REPORT_ITEM_ID"),
    ),
)
async def test_binder_blocks_forged_periodic_report_pointers(call, error_code):
    bound, failure = await _binder().bind(call)

    assert bound is None
    assert failure.status == ReceiptStatus.BLOCKED
    assert failure.error_code == error_code
