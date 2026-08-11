from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID
from types import SimpleNamespace

import pytest

from app.agent2.tool_calling.context import (
    TrustedContext,
    TrustedPrincipal,
    TrustedReportItem,
    TrustedReportSnapshot,
)
from app.agent2.tool_calling.contracts import ExecutionMode
from app.agent2.tool_calling.registry import TOOL_REGISTRY, validate_tool_arguments
from app.agent2.tool_calling.daily_report_date_correction import (
    SqlDailyReportDateCorrection,
)
from app.agent2.tool_calling.production_runtime import _prepare_call
from app.agent2.tool_calling.production_daily_executor import (
    ProductionDailyExecutor,
)
from app.agent2.tool_calling.production_handlers import (
    ProductionHandlerRequest,
)
from app.agent2.tool_calling.contracts import AddDailyItemsArgs
from app.agent2.typed_daily_commands import DailyReportMutationSnapshot
from app.agent2.tool_calling.validation import BoundCall
from app.agent2.tool_calling.validation import (
    DateResolution,
    NativeToolCall,
    ShadowCallBinder,
)


def test_agent2_exposes_one_atomic_cross_midnight_report_correction() -> None:
    """A date correction, empty-section acknowledgement and submit are one turn."""

    definition = TOOL_REGISTRY["correct_daily_report_date"]
    arguments = validate_tool_arguments(
        "correct_daily_report_date",
        {
            "source_date_expression": "today",
            "proposed_source_date": "2026-08-11",
            "target_date_expression": "yesterday",
            "proposed_target_date": "2026-08-10",
            "acknowledged_empty_fields": ["problems"],
            "submit_after_correction": True,
        },
    )

    assert definition.read_or_write == "write"
    assert definition.transaction_policy == "same_report_atomic"
    assert arguments == {
        "source_date_expression": "today",
        "proposed_source_date": "2026-08-11",
        "target_date_expression": "yesterday",
        "proposed_target_date": "2026-08-10",
        "acknowledged_empty_fields": ["problems"],
        "submit_after_correction": True,
    }


class _DateResolver:
    def resolve(self, *, expression, proposed_date, now, timezone):
        del expression, now, timezone
        return DateResolution(proposed_date, candidate_matches=True)


def _ding_source_report() -> TrustedReportSnapshot:
    report_id = UUID("11111111-1111-1111-1111-111111111111")
    return TrustedReportSnapshot(
        report_id=report_id,
        tenant_id="tenant",
        owner_user_id=UUID("22222222-2222-2222-2222-222222222222"),
        report_date=date(2026, 8, 11),
        version=8,
        status="collecting",
        items=(
            TrustedReportItem(
                item_id="today-1",
                field="today_work",
                content="完成合同审核",
                report_id=report_id,
                report_version=8,
            ),
            TrustedReportItem(
                item_id="tomorrow-1",
                field="tomorrow_plan",
                content="继续跟进案件",
                report_id=report_id,
                report_version=8,
            ),
        ),
    )


@pytest.mark.asyncio
async def test_cross_midnight_correction_binds_source_and_empty_target() -> None:
    source = _ding_source_report()
    context = TrustedContext(
        now=datetime(2026, 8, 11, 0, 59, tzinfo=UTC),
        principal=TrustedPrincipal(
            tenant_id="tenant",
            user_id=source.owner_user_id,
            conversation_id="ding-conversation",
            source_message_id="ding-correction",
            timezone="Asia/Shanghai",
        ),
        today_report=source,
        allowed_tool_names=frozenset({"correct_daily_report_date"}),
        gate_decisions={"correct_daily_report_date": True},
    )
    call = NativeToolCall(
        tool_call_id="correct-1",
        tool_name="correct_daily_report_date",
        arguments={
            "source_date_expression": "today",
            "proposed_source_date": "2026-08-11",
            "target_date_expression": "yesterday",
            "proposed_target_date": "2026-08-10",
            "acknowledged_empty_fields": ["problems"],
            "submit_after_correction": True,
        },
    )

    bound, failure = await ShadowCallBinder(
        context,
        _DateResolver(),
        None,
        execution_mode=ExecutionMode.SHADOW_PROPOSAL,
    ).bind(call)

    assert failure is None
    assert bound is not None
    assert bound.source_report == source
    assert bound.report is None
    assert bound.date_facts == {
        "resolved_source_date": "2026-08-11",
        "source_date_candidate_matches": True,
        "resolved_target_date": "2026-08-10",
        "target_date_candidate_matches": True,
    }


@pytest.mark.asyncio
async def test_undated_daily_write_before_nine_uses_previous_day() -> None:
    context = TrustedContext(
        now=datetime(2026, 8, 11, 0, 58, tzinfo=UTC),
        principal=TrustedPrincipal(
            tenant_id="tenant",
            user_id=UUID("22222222-2222-2222-2222-222222222222"),
            conversation_id="liu-conversation",
            source_message_id="liu-first-report",
            timezone="Asia/Shanghai",
        ),
        allowed_tool_names=frozenset({"add_daily_items"}),
        gate_decisions={"add_daily_items": True},
    )
    call = NativeToolCall(
        tool_call_id="add-1",
        tool_name="add_daily_items",
        arguments={
            "date_selection": "server_default",
            "date_expression": "today",
            "proposed_date": "2026-08-11",
            "items": [
                {"field": "today_work", "content": "完成合同审核"},
                {"field": "tomorrow_plan", "content": "继续跟进案件"},
            ],
            "acknowledged_empty_fields": ["problems"],
        },
    )

    bound, failure = await ShadowCallBinder(
        context,
        _DateResolver(),
        None,
        execution_mode=ExecutionMode.SHADOW_PROPOSAL,
    ).bind(call)

    assert failure is None
    assert bound is not None
    assert bound.date_facts["resolved_date"] == "2026-08-10"
    assert bound.date_facts["date_resolution_basis"] == "server_default"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("local_hour", "date_selection", "expected_date"),
    (
        (8, "user_explicit", "2026-08-11"),
        (9, "server_default", "2026-08-11"),
    ),
)
async def test_explicit_today_wins_and_nine_starts_current_day(
    local_hour: int,
    date_selection: str,
    expected_date: str,
) -> None:
    context = TrustedContext(
        now=datetime(2026, 8, 11, local_hour - 8, 0, tzinfo=UTC),
        principal=TrustedPrincipal(
            tenant_id="tenant",
            user_id=UUID("22222222-2222-2222-2222-222222222222"),
            conversation_id="date-boundary",
            source_message_id=f"date-boundary-{local_hour}",
            timezone="Asia/Shanghai",
        ),
        allowed_tool_names=frozenset({"add_daily_items"}),
        gate_decisions={"add_daily_items": True},
    )
    call = NativeToolCall(
        tool_call_id=f"add-{local_hour}",
        tool_name="add_daily_items",
        arguments={
            "date_selection": date_selection,
            "date_expression": "today",
            "proposed_date": "2026-08-11",
            "items": [
                {"field": "today_work", "content": "完成合同审核"},
            ],
        },
    )

    bound, failure = await ShadowCallBinder(
        context,
        _DateResolver(),
        None,
        execution_mode=ExecutionMode.SHADOW_PROPOSAL,
    ).bind(call)

    assert failure is None
    assert bound is not None
    assert bound.date_facts["resolved_date"] == expected_date


class _ScalarRows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _CorrectionSession:
    def __init__(self, rows):
        self.rows = rows
        self.added = []
        self.flush_count = 0

    async def scalars(self, statement):
        del statement
        return _ScalarRows(self.rows)

    async def flush(self):
        self.flush_count += 1

    def add(self, row):
        self.added.append(row)


def _report_row(*, report_date: date, report_id: str, version: int = 8):
    return SimpleNamespace(
        id=UUID(report_id),
        report_date=report_date,
        today_work=["完成合同审核"],
        problems=[],
        tomorrow_plan=["继续跟进案件"],
        section_status={"_agent2_report_version": version},
        status="collecting",
        completeness_score=0,
        last_modified_by_user=False,
        last_modified_at=None,
        confirmation_type="none",
        confirmed_by_user=False,
        submitted_at=None,
        pending_confirmation_at=None,
        auto_submit_at=None,
        llm_payload={},
    )


@pytest.mark.asyncio
async def test_ding_timeline_is_relocated_acknowledged_and_submitted_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_lock(session, user_id, report_date):
        del session, user_id, report_date

    monkeypatch.setattr(
        "app.agent2.tool_calling.daily_report_date_correction."
        "acquire_daily_report_advisory_lock",
        no_lock,
    )
    source = _report_row(
        report_date=date(2026, 8, 11),
        report_id="11111111-1111-1111-1111-111111111111",
    )
    session = _CorrectionSession([source])
    user = SimpleNamespace(
        id=UUID("22222222-2222-2222-2222-222222222222")
    )

    result = await SqlDailyReportDateCorrection(session).execute(
        user=user,
        tenant_id="tenant",
        source_report_id=source.id,
        expected_version=8,
        source_date=date(2026, 8, 11),
        target_date=date(2026, 8, 10),
        acknowledged_empty_fields=("problems",),
        submit_after_correction=True,
        idempotency_key="correction-key",
        source_message_id="ding-correction",
        now=datetime(2026, 8, 11, 0, 59, tzinfo=UTC),
    )

    assert result.status == "success"
    assert result.before_version == 8
    assert result.after_version == 11
    assert source.report_date == date(2026, 8, 10)
    assert source.today_work == ["完成合同审核"]
    assert source.tomorrow_plan == ["继续跟进案件"]
    assert source.problems == []
    assert source.status == "completed"
    assert source.section_status["problems_acknowledged_empty"] is True
    assert source.section_status["_agent2_report_version"] == 11
    assert source.confirmed_by_user is True
    assert len(session.added) == 1
    assert tuple(result.typed_receipt_ids) == (
        str(session.added[0].receipt_id),
    )


@pytest.mark.asyncio
async def test_existing_target_report_blocks_correction_without_any_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_lock(session, user_id, report_date):
        del session, user_id, report_date

    monkeypatch.setattr(
        "app.agent2.tool_calling.daily_report_date_correction."
        "acquire_daily_report_advisory_lock",
        no_lock,
    )
    source = _report_row(
        report_date=date(2026, 8, 11),
        report_id="11111111-1111-1111-1111-111111111111",
    )
    target = _report_row(
        report_date=date(2026, 8, 10),
        report_id="33333333-3333-3333-3333-333333333333",
    )
    session = _CorrectionSession([source, target])
    user = SimpleNamespace(
        id=UUID("22222222-2222-2222-2222-222222222222")
    )

    result = await SqlDailyReportDateCorrection(session).execute(
        user=user,
        tenant_id="tenant",
        source_report_id=source.id,
        expected_version=8,
        source_date=date(2026, 8, 11),
        target_date=date(2026, 8, 10),
        acknowledged_empty_fields=("problems",),
        submit_after_correction=True,
        idempotency_key="correction-key",
        source_message_id="correction-conflict",
        now=datetime(2026, 8, 11, 0, 59, tzinfo=UTC),
    )

    assert result.status == "clarification_required"
    assert result.error_code == "TARGET_REPORT_ALREADY_EXISTS"
    assert source.report_date == date(2026, 8, 11)
    assert source.section_status == {"_agent2_report_version": 8}
    assert session.added == []


@pytest.mark.asyncio
async def test_provider_replay_keeps_same_correction_fingerprint_after_move() -> None:
    source = _ding_source_report()
    principal = TrustedPrincipal(
        tenant_id="tenant",
        user_id=source.owner_user_id,
        conversation_id="ding-conversation",
        source_message_id="same-provider-message",
        timezone="Asia/Shanghai",
    )
    call = NativeToolCall(
        tool_call_id="correct-1",
        tool_name="correct_daily_report_date",
        arguments={
            "source_date_expression": "today",
            "proposed_source_date": "2026-08-11",
            "target_date_expression": "yesterday",
            "proposed_target_date": "2026-08-10",
            "acknowledged_empty_fields": ["problems"],
            "submit_after_correction": True,
        },
    )
    initial_context = TrustedContext(
        now=datetime(2026, 8, 11, 0, 59, tzinfo=UTC),
        principal=principal,
        today_report=source,
        allowed_tool_names=frozenset({"correct_daily_report_date"}),
        gate_decisions={"correct_daily_report_date": True},
    )
    initial_bound, initial_failure = await ShadowCallBinder(
        initial_context,
        _DateResolver(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
    ).bind(call)
    assert initial_failure is None
    assert initial_bound is not None

    moved_items = tuple(
        item.model_copy(update={"report_version": 11})
        for item in source.items
    )
    moved = source.model_copy(
        update={
            "report_date": date(2026, 8, 10),
            "version": 11,
            "status": "completed",
            "items": moved_items,
            "acknowledged_empty_fields": frozenset({"problems"}),
        }
    )
    replay_context = TrustedContext(
        now=datetime(2026, 8, 11, 0, 59, tzinfo=UTC),
        principal=principal,
        historical_reports=(moved,),
        allowed_tool_names=frozenset({"correct_daily_report_date"}),
        gate_decisions={"correct_daily_report_date": True},
    )
    replay_bound, replay_failure = await ShadowCallBinder(
        replay_context,
        _DateResolver(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
    ).bind(call)

    assert replay_failure is None
    assert replay_bound is not None
    assert _prepare_call(
        initial_context,
        initial_bound,
    ).request_fingerprint == _prepare_call(
        replay_context,
        replay_bound,
    ).request_fingerprint
    assert _prepare_call(
        initial_context,
        initial_bound,
    ).operation_fingerprint == _prepare_call(
        replay_context,
        replay_bound,
    ).operation_fingerprint


@pytest.mark.asyncio
async def test_empty_acknowledgement_and_submit_are_one_atomic_add_call() -> None:
    owner_id = UUID("22222222-2222-2222-2222-222222222222")
    report_id = UUID("11111111-1111-1111-1111-111111111111")
    call = NativeToolCall(
        tool_call_id="add-and-submit",
        tool_name="add_daily_items",
        arguments={
            "date_selection": "server_default",
            "date_expression": "today",
            "proposed_date": "2026-08-11",
            "items": [],
            "acknowledged_empty_fields": ["problems"],
            "submit_after_write": True,
        },
    )
    arguments = AddDailyItemsArgs.model_validate(call.arguments)
    bound = BoundCall(
        call=call,
        arguments=arguments.model_dump(mode="json"),
        report=None,
        target_item_ids=(),
        source_report=None,
        date_facts={"resolved_date": "2026-08-10"},
    )
    executor = ProductionDailyExecutor.__new__(ProductionDailyExecutor)
    executor._bound_calls = {call.tool_call_id: bound}
    executor._context = SimpleNamespace(
        principal=SimpleNamespace(
            tenant_id="tenant",
            user_id=owner_id,
            conversation_id="conversation",
            source_message_id="source-message",
        )
    )
    snapshot = DailyReportMutationSnapshot(
        report_id=report_id,
        owner_user_id=owner_id,
        version=2,
        status="collecting",
        today_work=("完成合同审核",),
        problems=(),
        tomorrow_plan=("继续跟进案件",),
        item_ids={
            "today_work": ("today-1",),
            "problems": (),
            "tomorrow_plan": ("tomorrow-1",),
        },
    )
    captured = []

    async def snapshot_for_date(report_date):
        del report_date
        return None

    async def typed_snapshot_for_date(report_date):
        del report_date
        return snapshot

    async def execute_typed(report_date, commands, **kwargs):
        del report_date, kwargs
        captured.extend(commands)
        return ("receipt-1", "receipt-2")

    executor._snapshot = snapshot_for_date
    executor._typed_snapshot = typed_snapshot_for_date
    executor._execute_typed = execute_typed
    executor._outcome = lambda *args, **kwargs: SimpleNamespace(
        args=args,
        kwargs=kwargs,
    )
    request = ProductionHandlerRequest(
        tool_call_id=call.tool_call_id,
        tool_name=call.tool_name,
        arguments=arguments,
        executor=executor,
        memory_executor=None,
    )

    await executor.add_daily_items(request)

    assert [command.command_type for command in captured] == [
        "acknowledge_empty_section",
        "submit_report",
    ]
    assert [command.report_version for command in captured] == [2, 3]
