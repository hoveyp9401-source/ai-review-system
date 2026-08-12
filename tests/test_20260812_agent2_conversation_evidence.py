from __future__ import annotations

from datetime import date, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from app.agent2.tool_calling.assembly import (
    TrustedContextAssembler,
    TrustedContextRequest,
)
from app.agent2.tool_calling.context import (
    CANARY_STATE_NAMESPACE,
    TrustedRecentOperation,
    TrustedReportReference,
    TrustedReportSnapshot,
)
from app.agent2.tool_calling.production_store import (
    _trusted_report_reference_from_receipt,
)
from app.agent2.tool_calling.contracts import ExecutionMode
from app.agent2.tool_calling.current_turn_source import CurrentTurnSource
from app.agent2.tool_calling.validation import (
    NativeToolCall,
    ShadowCallBinder,
    UnavailableDateResolver,
)


TENANT_ID = "legal-daily-production-v1"
USER_ID = UUID("11111111-1111-1111-1111-111111111111")
REPORT_ID = UUID("22222222-2222-2222-2222-222222222222")
YESTERDAY = date(2026, 8, 11)
TODAY = date(2026, 8, 12)
NOW = datetime(2026, 8, 12, 9, 1, tzinfo=ZoneInfo("Asia/Shanghai"))


def _report() -> TrustedReportSnapshot:
    return TrustedReportSnapshot(
        report_id=REPORT_ID,
        tenant_id=TENANT_ID,
        owner_user_id=USER_ID,
        report_date=YESTERDAY,
        version=4,
        status="collecting",
        provenance="trusted_context",
    )


def _operation() -> TrustedRecentOperation:
    return TrustedRecentOperation(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        conversation_id="conversation-1",
        source_message_id="message-1",
        tool_call_id="call-1",
        tool_name="add_daily_items",
        status="success",
        changed=True,
        target_type="daily_report",
        target_id=str(REPORT_ID),
        before_version=3,
        after_version=4,
        occurred_at=NOW,
        report_reference=TrustedReportReference(
            report_id=REPORT_ID,
            report_date=YESTERDAY,
            report_version=4,
            report_status="collecting",
        ),
    )


class _ReadPort:
    async def load_report(self, request, report_date):
        del request
        return _report() if report_date == YESTERDAY else None

    async def load_active_clear_pendings(self, request, *, namespace):
        del request, namespace
        return ()

    async def load_recent_messages(self, request, *, namespace, limit):
        del request, namespace, limit
        return ()

    async def load_recent_operations(self, request, *, namespace, limit):
        del request, namespace, limit
        return (_operation(),)


class _PolicyPort:
    async def permission_allowed(self, request, definition):
        del request, definition
        return True

    async def gate_allowed(self, request, definition):
        del request, definition
        return True


@pytest.mark.asyncio
async def test_recent_receipt_reference_loads_yesterday_as_a_candidate_not_a_focus() -> None:
    context = await TrustedContextAssembler(
        read_port=_ReadPort(),
        policy_port=_PolicyPort(),
        namespace=CANARY_STATE_NAMESPACE,
        recent_message_limit=0,
        recent_operation_limit=6,
    ).assemble(
        TrustedContextRequest(
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            conversation_id="conversation-1",
            source_message_id="message-2",
            timezone="Asia/Shanghai",
            server_now=NOW,
        )
    )

    assert context.today_report is None
    assert [item.report_date for item in context.historical_reports] == [YESTERDAY]
    payload = context.model_payload()
    assert "conversation_report_date" not in payload.get("business_glossary", {})
    assert payload["recent_operations"][0]["report_reference"] == {
        "report_id": str(REPORT_ID),
        "report_date": YESTERDAY.isoformat(),
        "report_version": 4,
        "report_status": "collecting",
        "provenance": "server_receipt",
    }


@pytest.mark.asyncio
async def test_stale_or_mismatched_receipt_reference_is_not_loaded() -> None:
    mismatched = _operation().model_copy(
        update={
            "report_reference": TrustedReportReference(
                report_id=UUID("33333333-3333-3333-3333-333333333333"),
                report_date=YESTERDAY,
                report_version=4,
                report_status="collecting",
            )
        }
    )

    class ReadPort(_ReadPort):
        async def load_recent_operations(self, request, *, namespace, limit):
            del request, namespace, limit
            return (mismatched,)

    context = await TrustedContextAssembler(
        read_port=ReadPort(),
        policy_port=_PolicyPort(),
        namespace=CANARY_STATE_NAMESPACE,
        recent_message_limit=0,
        recent_operation_limit=6,
    ).assemble(
        TrustedContextRequest(
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            conversation_id="conversation-1",
            source_message_id="message-2",
            timezone="Asia/Shanghai",
            server_now=NOW,
        )
    )

    assert context.historical_reports == ()
    assert context.recent_operations[0].report_reference is None
    assert "recent_report_reference_mismatch" in context.assembly_warnings


@pytest.mark.asyncio
async def test_blocked_receipt_never_loads_a_report_candidate() -> None:
    blocked = _operation().model_copy(update={"status": "blocked"})

    class ReadPort(_ReadPort):
        async def load_recent_operations(self, request, *, namespace, limit):
            del request, namespace, limit
            return (blocked,)

    context = await TrustedContextAssembler(
        read_port=ReadPort(),
        policy_port=_PolicyPort(),
        namespace=CANARY_STATE_NAMESPACE,
        recent_message_limit=0,
        recent_operation_limit=6,
    ).assemble(
        TrustedContextRequest(
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            conversation_id="conversation-1",
            source_message_id="message-2",
            timezone="Asia/Shanghai",
            server_now=NOW,
        )
    )

    assert context.historical_reports == ()
    assert context.recent_operations[0].report_reference is None


@pytest.mark.asyncio
async def test_two_recent_report_candidates_are_left_unbound_for_model_clarification() -> None:
    second_report_id = UUID("33333333-3333-3333-3333-333333333333")
    second_date = date(2026, 8, 10)
    second_report = _report().model_copy(
        update={"report_id": second_report_id, "report_date": second_date}
    )
    second_operation = _operation().model_copy(
        update={
            "source_message_id": "message-other",
            "tool_call_id": "call-other",
            "target_id": str(second_report_id),
            "report_reference": TrustedReportReference(
                report_id=second_report_id,
                report_date=second_date,
                report_version=4,
                report_status="collecting",
            ),
        }
    )

    class ReadPort(_ReadPort):
        async def load_report(self, request, report_date):
            del request
            return {
                YESTERDAY: _report(),
                second_date: second_report,
            }.get(report_date)

        async def load_recent_operations(self, request, *, namespace, limit):
            del request, namespace, limit
            return (_operation(), second_operation)

    context = await TrustedContextAssembler(
        read_port=ReadPort(),
        policy_port=_PolicyPort(),
        namespace=CANARY_STATE_NAMESPACE,
        recent_message_limit=0,
        recent_operation_limit=6,
    ).assemble(
        TrustedContextRequest(
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            conversation_id="conversation-1",
            source_message_id="message-2",
            timezone="Asia/Shanghai",
            server_now=NOW,
        )
    )

    assert context.historical_reports == ()
    assert all(
        operation.report_reference is None
        for operation in context.recent_operations
    )
    assert "recent_report_reference_ambiguous" in context.assembly_warnings


def test_only_successful_matching_daily_receipt_builds_report_reference() -> None:
    row = type(
        "ReceiptRow",
        (),
        {
            "status": "success",
            "target_type": "daily_report",
            "target_id": str(REPORT_ID),
            "after_version": 4,
            "safe_user_facts": {
                "report_snapshot": {
                    "report_id": str(REPORT_ID),
                    "report_date": YESTERDAY.isoformat(),
                    "version": 4,
                    "status": "collecting",
                }
            },
        },
    )()

    reference = _trusted_report_reference_from_receipt(row)

    assert reference == TrustedReportReference(
        report_id=REPORT_ID,
        report_date=YESTERDAY,
        report_version=4,
        report_status="collecting",
    )
    row.status = "blocked"
    assert _trusted_report_reference_from_receipt(row) is None
    row.status = "success"
    row.after_version = 5
    assert _trusted_report_reference_from_receipt(row) is None


@pytest.mark.asyncio
async def test_followup_empty_and_submit_binds_the_receipt_report_after_nine() -> None:
    context = await TrustedContextAssembler(
        read_port=_ReadPort(),
        policy_port=_PolicyPort(),
        namespace=CANARY_STATE_NAMESPACE,
        recent_message_limit=0,
        recent_operation_limit=6,
    ).assemble(
        TrustedContextRequest(
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            conversation_id="conversation-1",
            source_message_id="message-2",
            timezone="Asia/Shanghai",
            server_now=NOW,
        )
    )
    call = NativeToolCall(
        tool_call_id="followup-submit",
        tool_name="add_daily_items",
        arguments={
            "date_selection": "trusted_report",
            "report_id": str(REPORT_ID),
            "expected_version": 4,
            "items": [],
            "acknowledged_empty_fields": ["problems"],
            "empty_field_evidence": [
                {
                    "field": "problems",
                    "source_evidence": {"source_message_index": 1},
                }
            ],
            "submit_after_write": True,
        },
    )

    bound, failure = await ShadowCallBinder(
        context,
        UnavailableDateResolver(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
        current_turn_source=CurrentTurnSource(
            ("风险这块没遇到什么，前面那版就这么定了。",)
        ),
    ).bind(call)

    assert failure is None
    assert bound is not None
    assert bound.report is not None
    assert bound.report.report_date == YESTERDAY
    assert bound.date_facts == {
        "resolved_date": YESTERDAY.isoformat(),
        "date_resolution_basis": "trusted_report_reference",
    }
