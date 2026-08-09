from datetime import date, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from app.agent2.tool_calling.context import (
    CANARY_STATE_NAMESPACE,
    TrustedContext,
    TrustedPrincipal,
    TrustedReportItem,
    TrustedReportSnapshot,
)
from app.agent2.tool_calling.canary_config import canary_system_prompt
from app.agent2.tool_calling.contracts import ExecutionMode
from app.agent2.tool_calling.validation import (
    DateResolution,
    NativeToolCall,
    ShadowCallBinder,
)


TENANT_ID = "test-tenant"
USER_ID = UUID("10000000-0000-0000-0000-000000000001")
TODAY_REPORT_ID = UUID("20000000-0000-0000-0000-000000000001")
SOURCE_REPORT_ID = UUID("20000000-0000-0000-0000-000000000002")
TODAY = date(2026, 8, 9)
SOURCE_DATE = date(2026, 8, 8)


def test_agent2_does_not_promise_an_unregistered_future_copy() -> None:
    prompt = canary_system_prompt()

    assert "Do not promise that you will execute it later" in prompt
    assert "fresh user turn" in prompt


class _DateResolver:
    def resolve(self, *, expression, proposed_date, now, timezone):
        del expression, now, timezone
        return DateResolution(proposed_date, candidate_matches=True)


class _OwnedReportReader:
    def __init__(self, report: TrustedReportSnapshot | None) -> None:
        self.report = report
        self.calls: list[tuple[str, UUID, date]] = []

    async def load_owned_report(self, *, tenant_id, user_id, report_date):
        self.calls.append((tenant_id, user_id, report_date))
        return (
            self.report
            if self.report is not None
            and report_date == self.report.report_date
            else None
        )


def _report(
    *,
    report_id: UUID,
    report_date: date,
    provenance: str,
) -> TrustedReportSnapshot:
    return TrustedReportSnapshot(
        report_id=report_id,
        tenant_id=TENANT_ID,
        owner_user_id=USER_ID,
        report_date=report_date,
        version=3,
        status="collecting",
        items=(
            TrustedReportItem(
                item_id=f"{report_id}:today_work:0",
                field="today_work",
                content="虚构测试事项",
                report_id=report_id,
                report_version=3,
                provenance=provenance,
            ),
        ),
        provenance=provenance,
    )


@pytest.mark.asyncio
async def test_copy_binds_source_report_from_owned_date_without_model_object_ids():
    today_report = _report(
        report_id=TODAY_REPORT_ID,
        report_date=TODAY,
        provenance="trusted_context",
    )
    source_report = _report(
        report_id=SOURCE_REPORT_ID,
        report_date=SOURCE_DATE,
        provenance="read_tool",
    )
    reader = _OwnedReportReader(source_report)
    context = TrustedContext(
        namespace=CANARY_STATE_NAMESPACE,
        now=datetime(2026, 8, 9, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        principal=TrustedPrincipal(
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            conversation_id="test-conversation",
            source_message_id="test-message",
            timezone="Asia/Shanghai",
            display_name="测试用户",
        ),
        today_report=today_report,
        allowed_tool_names=frozenset({"copy_previous_to_today"}),
        gate_decisions={"copy_previous_to_today": True},
    )
    binder = ShadowCallBinder(
        context,
        _DateResolver(),
        reader,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
    )

    bound, failure = await binder.bind(
        NativeToolCall(
            "copy-call",
            "copy_previous_to_today",
            {
                "source_date_expression": "昨天",
                "proposed_source_date": SOURCE_DATE.isoformat(),
            },
        )
    )

    assert failure is None
    assert bound is not None
    assert bound.source_report == source_report
    assert bound.report == today_report
    assert bound.arguments == {
        "source_date_expression": "昨天",
        "proposed_source_date": SOURCE_DATE.isoformat(),
    }
    assert reader.calls == [(TENANT_ID, USER_ID, SOURCE_DATE)]


@pytest.mark.asyncio
async def test_copy_returns_a_safe_receipt_when_owned_source_date_has_no_report():
    today_report = _report(
        report_id=TODAY_REPORT_ID,
        report_date=TODAY,
        provenance="trusted_context",
    )
    reader = _OwnedReportReader(None)
    context = TrustedContext(
        namespace=CANARY_STATE_NAMESPACE,
        now=datetime(2026, 8, 9, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        principal=TrustedPrincipal(
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            conversation_id="test-conversation",
            source_message_id="test-message",
            timezone="Asia/Shanghai",
            display_name="测试用户",
        ),
        today_report=today_report,
        allowed_tool_names=frozenset({"copy_previous_to_today"}),
        gate_decisions={"copy_previous_to_today": True},
    )
    binder = ShadowCallBinder(
        context,
        _DateResolver(),
        reader,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
    )

    bound, failure = await binder.bind(
        NativeToolCall(
            "copy-call",
            "copy_previous_to_today",
            {
                "source_date_expression": "昨天",
                "proposed_source_date": SOURCE_DATE.isoformat(),
            },
        )
    )

    assert bound is None
    assert failure is not None
    assert failure.status.value == "blocked"
    assert failure.error_code == "SOURCE_REPORT_NOT_FOUND"
    assert failure.safe_user_facts["source_report_date"] == SOURCE_DATE.isoformat()
