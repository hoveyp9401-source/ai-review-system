from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from app.agent2.periodic_report_context import TrustedPeriodicReportContext
from app.agent2.report_domain import (
    PeriodicReportSnapshot,
    execute_periodic_report_command,
)
from app.agent2.report_sql_executor import PersistedPeriodicReportExecution
from app.agent2.tool_calling.context import TrustedContext, TrustedPrincipal
from app.agent2.tool_calling.contracts import (
    ApplyCurrentWeeklyReportArgs,
    QueryCurrentWeeklyReportArgs,
    SubmitCurrentWeeklyReportArgs,
)
from app.agent2.tool_calling.production_handlers import ProductionHandlerRequest
from app.agent2.tool_calling.production_periodic_report_executor import (
    ProductionPeriodicReportExecutor,
)
from app.agent2.tool_calling.validation import BoundCall, NativeToolCall


NOW = datetime(2026, 8, 14, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
TENANT_ID = "tenant-a"
USER_ID = UUID("10000000-0000-4000-8000-000000000001")
REPORT_ID = UUID("20000000-0000-4000-8000-000000000001")


class _ExecutionAdapter:
    def __init__(self, snapshot: PeriodicReportSnapshot) -> None:
        self.snapshot = snapshot
        self.commands = []
        self._receipt_number = 0

    async def load_current_weekly(self, *, context, anchor):
        assert context.actor_user_id == str(USER_ID)
        assert anchor.isoformat() == "2026-08-14"
        return self.snapshot

    async def execute(self, *, commands, context):
        results = []
        current = self.snapshot
        for command in commands:
            execution = execute_periodic_report_command(
                command,
                snapshot=current,
                actor_user_id=USER_ID,
            )
            current = execution.after
            self._receipt_number += 1
            results.append(
                PersistedPeriodicReportExecution(
                    execution=execution,
                    receipt_id=f"periodic-receipt-{self._receipt_number}",
                    status=execution.validation_status,
                    actual_write=execution.should_write_db,
                )
            )
        self.commands.extend(commands)
        self.snapshot = current
        return results


def _snapshot(*, version=0, status="collecting", sections=None, item_ids=None):
    return PeriodicReportSnapshot(
        report_id=REPORT_ID,
        owner_user_id=USER_ID,
        report_type="weekly",
        period_key="2026-W33",
        version=version,
        status=status,
        sections=sections or {},
        item_ids=item_ids or {},
    )


def _context(snapshot):
    items = ()
    if snapshot.item_ids:
        from app.agent2.periodic_report_context import TrustedPeriodicReportItem

        items = tuple(
            TrustedPeriodicReportItem(
                item_id=item_id,
                field=field,
                content=content,
            )
            for field, values in snapshot.sections.items()
            for item_id, content in zip(
                snapshot.item_ids.get(field, ()), values, strict=True
            )
        )
    return TrustedContext(
        now=NOW,
        principal=TrustedPrincipal(
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            conversation_id="direct-1",
            source_message_id="message-1",
            timezone="Asia/Shanghai",
            conversation_kind="direct",
        ),
        current_weekly_report=TrustedPeriodicReportContext(
            tenant_id=TENANT_ID,
            owner_user_id=USER_ID,
            report_id=REPORT_ID,
            report_type="weekly",
            period_key="2026-W33",
            version=snapshot.version,
            status=snapshot.status,
            items=items,
        ),
    )


def _request(tool_name, arguments):
    call = NativeToolCall(
        tool_call_id=f"call-{tool_name}",
        tool_name=tool_name,
        arguments=arguments.model_dump(mode="json"),
    )
    bound = BoundCall(
        call=call,
        arguments=call.arguments,
        report=None,
        target_item_ids=(),
        source_report=None,
        date_facts={},
        periodic_report=_context(_snapshot()).current_weekly_report,
    )
    return (
        ProductionHandlerRequest(
            tool_call_id=call.tool_call_id,
            tool_name=tool_name,
            arguments=arguments,
            executor=object(),
            memory_executor=object(),
        ),
        {call.tool_call_id: bound},
    )


def _executor(snapshot, bound_calls, adapter):
    context = _context(snapshot)
    return ProductionPeriodicReportExecutor(
        session=None,
        user=SimpleNamespace(id=USER_ID),
        context=context,
        bound_calls={
            call_id: replace(
                bound,
                periodic_report=context.current_weekly_report,
            )
            for call_id, bound in bound_calls.items()
        },
        source_channel="private-chat",
        execution_adapter=adapter,
    )


@pytest.mark.asyncio
async def test_query_current_weekly_report_returns_existing_domain_snapshot():
    snapshot = _snapshot()
    request, bound = _request(
        "query_current_weekly_report",
        QueryCurrentWeeklyReportArgs(),
    )
    adapter = _ExecutionAdapter(snapshot)

    outcome = await _executor(snapshot, bound, adapter).query_current_weekly_report(
        request
    )

    assert outcome.safe_user_facts["actual_write"] is False
    assert outcome.safe_user_facts["periodic_report_snapshot"]["period_key"] == "2026-W33"
    assert adapter.commands == []
    assert outcome.typed_receipt_ids == ()


@pytest.mark.asyncio
async def test_append_current_weekly_report_reuses_typed_domain_and_receipt():
    snapshot = _snapshot()
    arguments = ApplyCurrentWeeklyReportArgs.model_validate(
        {
            "report_id": str(REPORT_ID),
            "expected_version": 0,
            "operations": [
                {
                    "operation_id": "append-1",
                    "operation": "append",
                    "field": "accomplishments",
                    "content": "完成合同复核",
                    "source_evidence": {"source_message_index": 1},
                }
            ],
        }
    )
    request, bound = _request("apply_current_weekly_report", arguments)
    adapter = _ExecutionAdapter(snapshot)

    outcome = await _executor(snapshot, bound, adapter).apply_current_weekly_report(
        request
    )

    assert adapter.snapshot.sections["accomplishments"] == ("完成合同复核",)
    assert adapter.snapshot.version == 1
    assert outcome.typed_receipt_ids == ("periodic-receipt-1",)
    assert outcome.safe_user_facts["actual_write"] is True


@pytest.mark.asyncio
async def test_submit_current_weekly_report_marks_existing_report_completed():
    snapshot = _snapshot(
        version=1,
        sections={"accomplishments": ("完成合同复核",)},
        item_ids={"accomplishments": ("weekly-item-1",)},
    )
    arguments = SubmitCurrentWeeklyReportArgs.model_validate(
        {
            "report_id": str(REPORT_ID),
            "expected_version": 1,
            "confirmation_evidence": {"source_message_index": 1},
        }
    )
    request, bound = _request("submit_current_weekly_report", arguments)
    adapter = _ExecutionAdapter(snapshot)

    outcome = await _executor(snapshot, bound, adapter).submit_current_weekly_report(
        request
    )

    assert adapter.snapshot.status == "completed"
    assert adapter.snapshot.version == 2
    assert outcome.safe_user_facts["periodic_report_snapshot"]["status"] == "completed"


@pytest.mark.asyncio
async def test_stale_context_blocks_before_periodic_command_execution():
    trusted_snapshot = _snapshot(version=0)
    live_snapshot = replace(trusted_snapshot, version=1)
    arguments = ApplyCurrentWeeklyReportArgs.model_validate(
        {
            "report_id": str(REPORT_ID),
            "expected_version": 0,
            "operations": [
                {
                    "operation_id": "append-1",
                    "operation": "append",
                    "field": "accomplishments",
                    "content": "完成合同复核",
                    "source_evidence": {"source_message_index": 1},
                }
            ],
        }
    )
    request, bound = _request("apply_current_weekly_report", arguments)
    adapter = _ExecutionAdapter(live_snapshot)

    from app.agent2.tool_calling.production_daily_executor import ProductionExecutionError

    with pytest.raises(ProductionExecutionError, match="PERIODIC_REPORT_CONTEXT_STALE"):
        await _executor(
            trusted_snapshot, bound, adapter
        ).apply_current_weekly_report(request)
    assert adapter.commands == []
