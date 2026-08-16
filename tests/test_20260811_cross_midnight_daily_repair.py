from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.agent2.tool_calling.context import (
    TrustedContext,
    TrustedDateCorrectionReference,
    TrustedPrincipal,
    TrustedReportItem,
    TrustedReportSnapshot,
)
from app.agent2.tool_calling.contracts import ExecutionMode
from app.agent2.tool_calling.registry import TOOL_REGISTRY, validate_tool_arguments
from app.agent2.tool_calling.daily_report_date_correction import (
    SqlDailyReportDateCorrection,
)
from app.agent2.tool_calling.current_turn_source import (
    CurrentTurnSource,
    CurrentTurnSourceEvidenceError,
)
from app.agent2.tool_calling.production_runtime import _prepare_call
from app.agent2.tool_calling.production_daily_executor import (
    ProductionDailyExecutor,
    ProductionExecutionError,
)
from app.agent2.tool_calling.production_handlers import (
    ProductionHandlerRequest,
)
from app.agent2.tool_calling.production_store import report_state_hash
from app.agent2.tool_calling.contracts import (
    AddDailyItemsArgs,
    CorrectDailyReportDateArgs,
    DeleteDailyItemsArgs,
)
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
            "empty_field_evidence": [
                {
                    "field": "problems",
                    "source_evidence": {"source_message_index": 1},
                }
            ],
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
        "empty_field_evidence": [
            {
                "field": "problems",
                "source_evidence": {"source_message_index": 1},
            }
        ],
        "submit_after_correction": True,
    }


def test_date_correction_empty_ack_requires_current_message_evidence() -> None:
    with pytest.raises(ValidationError):
        CorrectDailyReportDateArgs.model_validate(
            {
                "source_date_expression": "today",
                "proposed_source_date": "2026-08-11",
                "target_date_expression": "yesterday",
                "proposed_target_date": "2026-08-10",
                "acknowledged_empty_fields": ["problems"],
                "submit_after_correction": True,
            }
        )


def test_date_correction_empty_ack_evidence_is_bound_to_current_turn() -> None:
    arguments = CorrectDailyReportDateArgs.model_validate(
        {
            "source_date_expression": "today",
            "proposed_source_date": "2026-08-11",
            "target_date_expression": "yesterday",
            "proposed_target_date": "2026-08-10",
            "acknowledged_empty_fields": ["problems"],
            "empty_field_evidence": [
                {
                    "field": "problems",
                    "source_evidence": {"source_message_index": 2},
                }
            ],
            "submit_after_correction": True,
        }
    )

    with pytest.raises(CurrentTurnSourceEvidenceError):
        CurrentTurnSource(("这是昨天的日报，问题暂无，请提交。",)).validate_tool_arguments(
            "correct_daily_report_date",
            arguments.model_dump(mode="json"),
        )


class _DateResolver:
    def resolve(self, *, expression, proposed_date, now, timezone):
        del expression, now, timezone
        return DateResolution(proposed_date, candidate_matches=True)


class _DateResolverThatMustNotRun:
    def resolve(self, *, expression, proposed_date, now, timezone):
        del expression, proposed_date, now, timezone
        raise AssertionError(
            "grounded Agent2 date correction must not be reinterpreted by text rules"
        )


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
            "empty_field_evidence": [
                {
                    "field": "problems",
                    "source_evidence": {"source_message_index": 1},
                }
            ],
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
async def test_grounded_date_correction_uses_agent2_dates_without_text_reinterpretation() -> None:
    source = _ding_source_report()
    current_message = "刚才那份日报实际是昨天的，问题暂无，请提交。"
    context = TrustedContext(
        now=datetime(2026, 8, 11, 0, 59, tzinfo=UTC),
        principal=TrustedPrincipal(
            tenant_id="tenant",
            user_id=source.owner_user_id,
            conversation_id="ding-conversation",
            source_message_id="ding-grounded-correction",
            timezone="Asia/Shanghai",
        ),
        today_report=source,
        allowed_tool_names=frozenset({"correct_daily_report_date"}),
        gate_decisions={"correct_daily_report_date": True},
    )
    call = NativeToolCall(
        tool_call_id="correct-grounded",
        tool_name="correct_daily_report_date",
        arguments={
            "source_date_expression": "刚才那份日报",
            "proposed_source_date": "2026-08-11",
            "target_date_expression": "昨天",
            "proposed_target_date": "2026-08-10",
            "acknowledged_empty_fields": ["problems"],
            "empty_field_evidence": [
                {
                    "field": "problems",
                    "source_evidence": {"source_message_index": 1},
                }
            ],
            "submit_after_correction": True,
        },
    )

    bound, failure = await ShadowCallBinder(
        context,
        _DateResolverThatMustNotRun(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
        current_turn_source=CurrentTurnSource((current_message,)),
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
                {
                    "field": "today_work",
                    "content": "完成合同审核",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "完成合同审核",
                    },
                },
                {
                    "field": "tomorrow_plan",
                    "content": "继续跟进案件",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "继续跟进案件",
                    },
                },
            ],
            "acknowledged_empty_fields": ["problems"],
            "empty_field_evidence": [
                {
                    "field": "problems",
                    "source_evidence": {"source_message_index": 1},
                }
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
    assert bound.date_facts["resolved_date"] == "2026-08-10"
    assert bound.date_facts["date_resolution_basis"] == "server_default"


@pytest.mark.asyncio
async def test_before_nine_agent2_can_semantically_choose_new_day_without_a_second_date_router() -> None:
    """The morning default is a prior, not a hard lock on yesterday."""

    context = TrustedContext(
        now=datetime(2026, 8, 13, 0, 30, tzinfo=UTC),
        principal=TrustedPrincipal(
            tenant_id="tenant",
            user_id=UUID("22222222-2222-2222-2222-222222222222"),
            conversation_id="early-morning-conversation",
            source_message_id="early-morning-new-work",
            timezone="Asia/Shanghai",
        ),
        allowed_tool_names=frozenset({"add_daily_items"}),
        gate_decisions={"add_daily_items": True},
    )
    current_message = "早上刚完成了付款节点复核，这项记到8月13日的日报。"
    call = NativeToolCall(
        tool_call_id="add-current-workday",
        tool_name="add_daily_items",
        arguments={
            "date_selection": "agent2_semantic",
            "proposed_date": "2026-08-13",
            "date_evidence": {
                "source_message_index": 1,
                "exact_quote": "这项记到8月13日的日报",
            },
            "items": [
                {
                    "field": "today_work",
                    "content": "完成付款节点复核",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "早上刚完成了付款节点复核",
                    },
                }
            ],
        },
    )

    bound, failure = await ShadowCallBinder(
        context,
        _DateResolverThatMustNotRun(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
        current_turn_source=CurrentTurnSource((current_message,)),
    ).bind(call)

    assert failure is None
    assert bound is not None
    assert bound.date_facts == {
        "resolved_date": "2026-08-13",
        "date_candidate_matches": True,
        "date_resolution_basis": "agent2_semantic",
    }


@pytest.mark.asyncio
async def test_agent2_semantic_date_tolerates_a_redundant_relative_expression() -> None:
    """A safe model date must not be rejected only for repeating its relative phrase."""

    context = TrustedContext(
        now=datetime(2026, 8, 13, 0, 30, tzinfo=UTC),
        principal=TrustedPrincipal(
            tenant_id="tenant",
            user_id=UUID("22222222-2222-2222-2222-222222222222"),
            conversation_id="early-morning-conversation",
            source_message_id="early-morning-redundant-expression",
            timezone="Asia/Shanghai",
        ),
        allowed_tool_names=frozenset({"add_daily_items"}),
        gate_decisions={"add_daily_items": True},
    )
    current_message = "早上刚完成付款节点复核，这项记到今天这份日报。"
    call = NativeToolCall(
        tool_call_id="add-current-workday-with-expression",
        tool_name="add_daily_items",
        arguments={
            "date_selection": "agent2_semantic",
            "date_expression": "今天",
            "proposed_date": "2026-08-13",
            "date_evidence": {
                "source_message_index": 1,
                "exact_quote": "早上刚完成付款节点复核",
            },
            "items": [
                {
                    "field": "today_work",
                    "content": "完成付款节点复核",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "早上刚完成付款节点复核",
                    },
                }
            ],
        },
    )

    bound, failure = await ShadowCallBinder(
        context,
        _DateResolverThatMustNotRun(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
        current_turn_source=CurrentTurnSource((current_message,)),
    ).bind(call)

    assert failure is None
    assert bound is not None
    assert bound.arguments["date_expression"] == "今天"
    assert bound.date_facts == {
        "resolved_date": "2026-08-13",
        "date_candidate_matches": True,
        "date_resolution_basis": "agent2_semantic",
    }


@pytest.mark.asyncio
async def test_agent2_semantic_date_cannot_escape_the_morning_today_or_default_window() -> None:
    context = TrustedContext(
        now=datetime(2026, 8, 13, 0, 30, tzinfo=UTC),
        principal=TrustedPrincipal(
            tenant_id="tenant",
            user_id=UUID("22222222-2222-2222-2222-222222222222"),
            conversation_id="early-morning-conversation",
            source_message_id="bad-semantic-date",
            timezone="Asia/Shanghai",
        ),
        allowed_tool_names=frozenset({"add_daily_items"}),
        gate_decisions={"add_daily_items": True},
    )
    call = NativeToolCall(
        tool_call_id="bad-semantic-date",
        tool_name="add_daily_items",
        arguments={
            "date_selection": "agent2_semantic",
            "proposed_date": "2026-08-01",
            "date_evidence": {
                "source_message_index": 1,
                "exact_quote": "今天完成合同复核",
            },
            "items": [
                {
                    "field": "today_work",
                    "content": "完成合同复核",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "今天完成合同复核",
                    },
                }
            ],
        },
    )

    bound, failure = await ShadowCallBinder(
        context,
        _DateResolverThatMustNotRun(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
        current_turn_source=CurrentTurnSource(("今天完成合同复核",)),
    ).bind(call)

    assert bound is None
    assert failure is not None
    assert failure.error_code == "UNTRUSTED_SEMANTIC_REPORT_DATE"


def test_context_exposes_morning_default_as_a_prior_not_a_lock() -> None:
    context = TrustedContext(
        now=datetime(2026, 8, 13, 0, 30, tzinfo=UTC),
        principal=TrustedPrincipal(
            tenant_id="tenant",
            user_id=UUID("22222222-2222-2222-2222-222222222222"),
            conversation_id="early-morning-conversation",
            source_message_id="context-date-prior",
            timezone="Asia/Shanghai",
        ),
    )

    reporting = context.model_payload()["daily_reporting_context"]

    assert reporting == {
        "local_date": "2026-08-13",
        "local_time": "2026-08-13T08:30:00+08:00",
        "default_report_date": "2026-08-12",
        "morning_cutoff": "09:00",
        "default_is_prior_not_lock": True,
        "safe_semantic_date_candidates": ["2026-08-12", "2026-08-13"],
    }


@pytest.mark.asyncio
async def test_one_am_yesterday_resolves_to_the_same_previous_day_prior() -> None:
    """At 01:00 on Aug 13, 'yesterday' and the reporting prior are Aug 12."""

    context = TrustedContext(
        now=datetime(2026, 8, 12, 17, 0, tzinfo=UTC),
        principal=TrustedPrincipal(
            tenant_id="tenant",
            user_id=UUID("22222222-2222-2222-2222-222222222222"),
            conversation_id="one-am-yesterday",
            source_message_id="one-am-yesterday-message",
            timezone="Asia/Shanghai",
        ),
        allowed_tool_names=frozenset({"add_daily_items"}),
        gate_decisions={"add_daily_items": True},
    )
    call = NativeToolCall(
        tool_call_id="add-yesterday-at-one-am",
        tool_name="add_daily_items",
        arguments={
            "date_selection": "server_default",
            "date_expression": "default",
            "proposed_date": "2026-08-12",
            "items": [
                {
                    "field": "today_work",
                    "content": "完成合同付款节点复核",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "昨天完成了合同付款节点复核",
                    },
                }
            ],
        },
    )

    bound, failure = await ShadowCallBinder(
        context,
        _DateResolverThatMustNotRun(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
        current_turn_source=CurrentTurnSource(
            ("昨天完成了合同付款节点复核，问题暂无，明天继续跟进。",)
        ),
    ).bind(call)

    assert failure is None
    assert bound is not None
    assert bound.date_facts == {
        "resolved_date": "2026-08-12",
        "date_candidate_matches": True,
        "date_resolution_basis": "server_default",
    }


@pytest.mark.asyncio
async def test_server_default_needs_no_model_repetition_of_the_server_date() -> None:
    context = TrustedContext(
        now=datetime(2026, 8, 12, 17, 0, tzinfo=UTC),
        principal=TrustedPrincipal(
            tenant_id="tenant",
            user_id=UUID("22222222-2222-2222-2222-222222222222"),
            conversation_id="one-am-default",
            source_message_id="one-am-default-message",
            timezone="Asia/Shanghai",
        ),
        allowed_tool_names=frozenset({"add_daily_items"}),
        gate_decisions={"add_daily_items": True},
    )
    call = NativeToolCall(
        tool_call_id="default-without-duplicated-date",
        tool_name="add_daily_items",
        arguments={
            "date_selection": "server_default",
            "items": [
                {
                    "field": "today_work",
                    "content": "完成合同付款节点复核",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "完成合同付款节点复核",
                    },
                }
            ],
        },
    )

    bound, failure = await ShadowCallBinder(
        context,
        _DateResolverThatMustNotRun(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
        current_turn_source=CurrentTurnSource(("完成合同付款节点复核",)),
    ).bind(call)

    assert failure is None
    assert bound is not None
    assert bound.date_facts["resolved_date"] == "2026-08-12"
    assert bound.date_facts["date_candidate_matches"] is True


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
    current_message = "这份日报明确记到今天，完成合同审核。"
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
            **(
                {
                    "date_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "明确记到今天",
                    }
                }
                if date_selection == "user_explicit"
                else {}
            ),
            "items": [
                {
                    "field": "today_work",
                    "content": "完成合同审核",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "完成合同审核",
                    },
                },
            ],
        },
    )

    bound, failure = await ShadowCallBinder(
        context,
        _DateResolver(),
        None,
        execution_mode=ExecutionMode.SHADOW_PROPOSAL,
        current_turn_source=CurrentTurnSource((current_message,)),
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
        conversation_kind="direct",
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
            "empty_field_evidence": [
                {
                    "field": "problems",
                    "source_evidence": {"source_message_index": 1},
                }
            ],
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
            "date_correction_reference": TrustedDateCorrectionReference(
                report_id=source.report_id,
                source_message_id="same-provider-message",
                source_report_date=date(2026, 8, 11),
                target_report_date=date(2026, 8, 10),
            ),
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
            "empty_field_evidence": [
                {
                    "field": "problems",
                    "source_evidence": {"source_message_index": 1},
                }
            ],
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target_item_ids", "expected_command_types"),
    (
        (
            ["today-1", "today-2"],
            ["delete_item", "delete_item", "acknowledge_empty_section"],
        ),
        (["today-1"], ["delete_item"]),
    ),
)
async def test_delete_marks_section_empty_only_when_every_item_is_selected(
    target_item_ids: list[str],
    expected_command_types: list[str],
) -> None:
    owner_id = UUID("22222222-2222-2222-2222-222222222222")
    report_id = UUID("11111111-1111-1111-1111-111111111111")
    call = NativeToolCall(
        tool_call_id="delete-all-work",
        tool_name="delete_daily_items",
        arguments={
            "report_id": str(report_id),
            "expected_version": 9,
            "target_item_ids": target_item_ids,
        },
    )
    arguments = DeleteDailyItemsArgs.model_validate(call.arguments)
    report = TrustedReportSnapshot(
        report_id=report_id,
        tenant_id="tenant",
        owner_user_id=owner_id,
        report_date=date(2026, 8, 10),
        version=9,
        status="collecting",
        items=(
            TrustedReportItem(
                item_id="today-1",
                field="today_work",
                content="第一项",
                report_id=report_id,
                report_version=9,
            ),
            TrustedReportItem(
                item_id="today-2",
                field="today_work",
                content="第二项",
                report_id=report_id,
                report_version=9,
            ),
        ),
    )
    bound = BoundCall(
        call=call,
        arguments=arguments.model_dump(mode="json"),
        report=report,
        target_item_ids=arguments.target_item_ids,
        source_report=None,
        date_facts={},
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
    typed = DailyReportMutationSnapshot(
        report_id=report_id,
        owner_user_id=owner_id,
        version=9,
        status="collecting",
        today_work=("第一项", "第二项"),
        problems=(),
        tomorrow_plan=(),
        item_ids={
            "today_work": ("today-1", "today-2"),
            "problems": (),
            "tomorrow_plan": (),
        },
    )
    captured = []

    async def snapshot_for_date(_report_date):
        return report

    async def typed_snapshot_for_date(_report_date):
        return typed

    async def execute_typed(_report_date, commands, **_kwargs):
        captured.extend(commands)
        return tuple(f"receipt-{index}" for index, _ in enumerate(commands))

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

    await executor.delete_daily_items(request)

    assert [command.command_type for command in captured] == expected_command_types
    assert [command.report_version for command in captured] == list(
        range(9, 9 + len(expected_command_types))
    )
    if len(target_item_ids) == 2:
        assert captured[-1].patch == {"field": "today_work"}


@pytest.mark.asyncio
async def test_trusted_retry_locks_before_rechecking_an_absent_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = UUID("22222222-2222-2222-2222-222222222222")
    target_date = date(2026, 8, 11)
    source_text = "reviewed the contract payment terms"
    call = NativeToolCall(
        tool_call_id="trusted-retry-absent",
        tool_name="add_daily_items",
        arguments={
            "date_selection": "trusted_failed_write",
            "retry_candidate_id": "a" * 64,
            "items": [
                {
                    "field": "today_work",
                    "content": source_text,
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": source_text,
                    },
                }
            ],
        },
    )
    arguments = AddDailyItemsArgs.model_validate(call.arguments)
    bound = BoundCall(
        call=call,
        arguments=arguments.model_dump(mode="json"),
        report=None,
        target_item_ids=(),
        source_report=None,
        date_facts={
            "resolved_date": target_date.isoformat(),
            "retry_target_state_sha256": report_state_hash(None),
        },
    )
    executor = ProductionDailyExecutor.__new__(ProductionDailyExecutor)
    executor._bound_calls = {call.tool_call_id: bound}
    executor._session = SimpleNamespace()
    executor._user = SimpleNamespace(id=user_id)
    lock_acquired = False

    async def acquire_lock(_session, locked_user_id, locked_date):
        nonlocal lock_acquired
        assert locked_user_id == user_id
        assert locked_date == target_date
        lock_acquired = True

    async def snapshot_after_lock(report_date, *, for_update=False):
        assert lock_acquired is True
        assert report_date == target_date
        assert for_update is True
        return TrustedReportSnapshot(
            report_id=UUID("11111111-1111-1111-1111-111111111111"),
            tenant_id="tenant",
            owner_user_id=user_id,
            report_date=target_date,
            version=0,
            status="collecting",
        )

    monkeypatch.setattr(
        "app.agent2.tool_calling.production_daily_executor."
        "acquire_daily_report_advisory_lock",
        acquire_lock,
    )
    executor._snapshot = snapshot_after_lock
    request = ProductionHandlerRequest(
        tool_call_id=call.tool_call_id,
        tool_name=call.tool_name,
        arguments=arguments,
        executor=executor,
        memory_executor=None,
    )

    with pytest.raises(ProductionExecutionError) as caught:
        await executor.add_daily_items(request)

    assert caught.value.code == "DAILY_RETRY_TARGET_STALE"
