from __future__ import annotations

import json
from copy import deepcopy
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from app.agent2.tool_calling.context import (
    CANARY_STATE_NAMESPACE,
    TrustedContext,
    TrustedPrincipal,
    TrustedRecentMessage,
    TrustedRecentOperation,
    TrustedReportItem,
    TrustedReportReference,
    TrustedReportSnapshot,
)
from app.agent2.tool_calling.contracts import (
    CorrectDailyReportDateArgs,
    ExecutionMode,
    ReceiptStatus,
    ToolReceipt,
)
from app.agent2.tool_calling.current_turn_source import CurrentTurnSource
from app.agent2.tool_calling.daily_report_date_correction import (
    DailyReportDateCorrectionResult,
    SqlDailyReportDateCorrection,
)
from app.agent2.tool_calling.production_daily_executor import (
    ProductionDailyExecutor,
)
from app.agent2.tool_calling.production_handlers import (
    ProductionHandlerRequest,
)
from app.agent2.tool_calling.production_runtime import (
    _prepare_call,
    _safe_report_snapshot,
)
from app.agent2.tool_calling.production_store import (
    _trusted_report_reference_from_receipt,
    report_state_hash,
    trusted_snapshot_from_report,
)
from app.agent2.tool_calling.write_reply import (
    model_safe_user_facts,
    render_write_reply,
    validate_write_reply,
    write_reply_protocol,
)
from app.agent2.tool_calling.turn_batching import (
    canonical_turn_batch_source_id,
)
from app.agent2.tool_calling.validation import (
    NativeToolCall,
    ShadowCallBinder,
    UnavailableDateResolver,
)
from app.agent2.typed_daily_executor import (
    DRAFT_ITEM_IDS_KEY,
    TYPED_AUDIT_KEY,
)


TENANT_ID = "legal-daily-production-v1"
USER_ID = UUID("11111111-1111-1111-1111-111111111111")
REPORT_ID = UUID("22222222-2222-2222-2222-222222222222")
CONVERSATION_ID = "direct-conversation"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 14, 22, 5, tzinfo=LOCAL_TZ)
SOURCE_DATE = date(2026, 8, 14)
TARGET_DATE = date(2026, 8, 13)
WRITE_PROVIDER_MESSAGE_ID = "dingtalk:provider-write-eight-items"
WRITE_SOURCE_TURN_ID = canonical_turn_batch_source_id(
    (WRITE_PROVIDER_MESSAGE_ID,)
)


def _source_report(
    *,
    version: int = 9,
    status: str = "collecting",
    report_date: date = SOURCE_DATE,
    correction_source_message_id: str | None = None,
) -> TrustedReportSnapshot:
    contents = tuple(f"脱敏工作事项{index}" for index in range(1, 9))
    correction_reference = (
        {
            "report_id": REPORT_ID,
            "source_message_id": correction_source_message_id,
            "source_report_date": SOURCE_DATE,
            "target_report_date": TARGET_DATE,
        }
        if correction_source_message_id is not None
        else None
    )
    payload = {
        "date_correction_reference": correction_reference,
    } if correction_reference is not None else {}
    return TrustedReportSnapshot(
        report_id=REPORT_ID,
        tenant_id=TENANT_ID,
        owner_user_id=USER_ID,
        report_date=report_date,
        version=version,
        status=status,
        items=tuple(
            TrustedReportItem(
                item_id=f"today-{index}",
                field="today_work",
                content=content,
                report_id=REPORT_ID,
                report_version=version,
            )
            for index, content in enumerate(contents, start=1)
        ),
        **payload,
    )


def _recent_write(
    report: TrustedReportSnapshot,
    *,
    receipt_report: TrustedReportSnapshot | None = None,
) -> TrustedRecentOperation:
    receipt_report = receipt_report or report
    return TrustedRecentOperation(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        source_message_id=WRITE_SOURCE_TURN_ID,
        tool_call_id="add-eight-items",
        tool_name="add_daily_items",
        status="success",
        changed=True,
        target_type="daily_report",
        target_id=str(report.report_id),
        before_version=report.version - 1,
        after_version=report.version,
        report_reference=TrustedReportReference(
            report_id=receipt_report.report_id,
            report_date=receipt_report.report_date,
            report_version=receipt_report.version,
            report_status=receipt_report.status,
            report_state_sha256=report_state_hash(receipt_report),
        ),
        occurred_at=NOW - timedelta(minutes=1),
    )


def _context(
    report: TrustedReportSnapshot,
    *,
    recent_write: TrustedRecentOperation | None = None,
) -> TrustedContext:
    return TrustedContext(
        namespace=CANARY_STATE_NAMESPACE,
        now=NOW,
        principal=TrustedPrincipal(
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            conversation_id=CONVERSATION_ID,
            source_message_id="correct-report-date",
            timezone="Asia/Shanghai",
            conversation_kind="direct",
        ),
        today_report=report,
        recent_messages=(
            TrustedRecentMessage(
                role="user",
                content="今日工作共八项，内容已脱敏。",
                source_message_id=WRITE_PROVIDER_MESSAGE_ID,
            ),
            TrustedRecentMessage(
                role="assistant",
                content="八项今日工作已保存。",
                source_message_id=f"{WRITE_PROVIDER_MESSAGE_ID}:assistant",
                source_turn_id=WRITE_SOURCE_TURN_ID,
            ),
        ),
        recent_operations=(recent_write or _recent_write(report),),
        allowed_tool_names=frozenset({"correct_daily_report_date"}),
        gate_decisions={"correct_daily_report_date": True},
    )


def _replay_context(
    target_report: TrustedReportSnapshot,
    *,
    source_message_id: str = "correct-report-date",
) -> TrustedContext:
    return TrustedContext(
        namespace=CANARY_STATE_NAMESPACE,
        now=NOW,
        principal=TrustedPrincipal(
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            conversation_id=CONVERSATION_ID,
            source_message_id=source_message_id,
            timezone="Asia/Shanghai",
            conversation_kind="direct",
        ),
        historical_reports=(target_report,),
        allowed_tool_names=frozenset({"correct_daily_report_date"}),
        gate_decisions={"correct_daily_report_date": True},
    )


def _date_correction_call(*, tool_call_id: str = "correct-date") -> NativeToolCall:
    return NativeToolCall(
        tool_call_id=tool_call_id,
        tool_name="correct_daily_report_date",
        arguments={
            "source_date_expression": "以上内容",
            "proposed_source_date": SOURCE_DATE.isoformat(),
            "target_date_expression": "8月13日",
            "proposed_target_date": TARGET_DATE.isoformat(),
            "acknowledged_empty_fields": [],
            "empty_field_evidence": [],
            "submit_after_correction": False,
        },
    )


@pytest.mark.asyncio
async def test_immediate_receipt_bound_date_correction_is_not_blocked_after_cutoff() -> None:
    """The next turn may correct only the exact report that was just written."""

    report = _source_report()
    call = NativeToolCall(
        tool_call_id="correct-date",
        tool_name="correct_daily_report_date",
        arguments={
            "source_date_expression": "以上内容",
            "proposed_source_date": SOURCE_DATE.isoformat(),
            "target_date_expression": "8月13日",
            "proposed_target_date": TARGET_DATE.isoformat(),
            "acknowledged_empty_fields": [],
            "empty_field_evidence": [],
            "submit_after_correction": False,
        },
    )

    bound, failure = await ShadowCallBinder(
        _context(report),
        UnavailableDateResolver(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
        current_turn_source=CurrentTurnSource(("以上内容是8月13日的。",)),
    ).bind(call)

    assert failure is None
    assert bound is not None
    assert bound.source_report == report
    assert bound.report is None
    assert bound.date_facts["resolved_source_date"] == SOURCE_DATE.isoformat()
    assert bound.date_facts["resolved_target_date"] == TARGET_DATE.isoformat()
    assert bound.date_facts["receipt_bound_source_state_sha256"] == (
        report_state_hash(report)
    )


@pytest.mark.asyncio
async def test_platform_message_id_shape_is_never_guessed_as_the_turn_id() -> None:
    report = _source_report()
    context = _context(report)
    messages = tuple(
        message.model_copy(update={"source_turn_id": None})
        if message.role == "assistant"
        else message
        for message in context.recent_messages
    )
    operation = _recent_write(report).model_copy(
        update={"source_message_id": WRITE_PROVIDER_MESSAGE_ID}
    )
    context = context.model_copy(
        update={
            "recent_messages": messages,
            "recent_operations": (operation,),
        }
    )

    bound, failure = await ShadowCallBinder(
        context,
        UnavailableDateResolver(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
        current_turn_source=CurrentTurnSource(("以上内容是8月13日的。",)),
    ).bind(_date_correction_call(tool_call_id="no-turn-id-guess"))

    assert bound is None
    assert failure is not None
    assert failure.error_code == "HISTORICAL_REPORT_LOCKED_AFTER_CUTOFF"


@pytest.mark.asyncio
async def test_same_version_but_changed_source_state_cannot_use_the_recent_receipt() -> None:
    receipt_report = _source_report()
    changed_items = tuple(
        item.model_copy(update={"content": f"{item.content}（已变化）"})
        for item in receipt_report.items
    )
    live_report = receipt_report.model_copy(update={"items": changed_items})
    call = NativeToolCall(
        tool_call_id="stale-state-correction",
        tool_name="correct_daily_report_date",
        arguments={
            "source_date_expression": "以上内容",
            "proposed_source_date": SOURCE_DATE.isoformat(),
            "target_date_expression": "8月13日",
            "proposed_target_date": TARGET_DATE.isoformat(),
            "acknowledged_empty_fields": [],
            "empty_field_evidence": [],
            "submit_after_correction": False,
        },
    )

    bound, failure = await ShadowCallBinder(
        _context(
            live_report,
            recent_write=_recent_write(
                live_report,
                receipt_report=receipt_report,
            ),
        ),
        UnavailableDateResolver(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
        current_turn_source=CurrentTurnSource(("以上内容是8月13日的。",)),
    ).bind(call)

    assert bound is None
    assert failure is not None
    assert failure.error_code == "HISTORICAL_REPORT_LOCKED_AFTER_CUTOFF"


class _ScalarRows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _CorrectionSession:
    def __init__(self, rows):
        self.rows = rows
        self.added = []

    async def scalars(self, statement):
        del statement
        return _ScalarRows(self.rows)

    async def flush(self):
        return None

    def add(self, row):
        self.added.append(row)


class _CorrectionSavepoint:
    def __init__(self, session: "_TransactionalCorrectionSession") -> None:
        self._session = session
        self._rows = deepcopy(session.rows)
        self._added = deepcopy(session.added)

    async def rollback(self) -> None:
        self._session.rows = deepcopy(self._rows)
        self._session.added = deepcopy(self._added)


class _TransactionalCorrectionSession(_CorrectionSession):
    async def begin_nested(self) -> _CorrectionSavepoint:
        return _CorrectionSavepoint(self)


def _report_row(*, status: str = "collecting"):
    return SimpleNamespace(
        id=REPORT_ID,
        report_date=SOURCE_DATE,
        today_work=[f"脱敏工作事项{index}" for index in range(1, 9)],
        problems=[],
        tomorrow_plan=[],
        section_status={
            "_agent2_report_version": 9,
            DRAFT_ITEM_IDS_KEY: {
                "today_work": [f"today-{index}" for index in range(1, 9)],
                "problems": [],
                "tomorrow_plan": [],
            },
        },
        status=status,
        completeness_score=0,
        last_modified_by_user=True,
        last_modified_at=NOW - timedelta(minutes=1),
        confirmation_type=("user_confirmed" if status == "completed" else "none"),
        confirmed_by_user=status == "completed",
        submitted_at=(NOW - timedelta(minutes=1) if status == "completed" else None),
        pending_confirmation_at=None,
        auto_submit_at=None,
        llm_payload={},
    )


@pytest.mark.asyncio
async def test_source_state_is_rechecked_under_the_atomic_date_correction_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_lock(session, user_id, report_date):
        del session, user_id, report_date

    monkeypatch.setattr(
        "app.agent2.tool_calling.daily_report_date_correction."
        "acquire_daily_report_advisory_lock",
        no_lock,
    )
    source = _report_row()
    session = _CorrectionSession([source])

    result = await SqlDailyReportDateCorrection(session).execute(
        user=SimpleNamespace(id=USER_ID),
        tenant_id=TENANT_ID,
        source_report_id=REPORT_ID,
        expected_version=9,
        expected_source_state_sha256="0" * 64,
        source_date=SOURCE_DATE,
        target_date=TARGET_DATE,
        acknowledged_empty_fields=(),
        submit_after_correction=False,
        idempotency_key="state-race-correction",
        source_message_id="correct-report-date",
        now=NOW,
    )

    assert result.status == "blocked"
    assert result.error_code == "SOURCE_REPORT_BINDING_CHANGED"
    assert source.report_date == SOURCE_DATE
    assert session.added == []


@pytest.mark.asyncio
async def test_production_executor_carries_the_receipt_state_into_the_locked_move() -> None:
    report = _source_report()
    call = NativeToolCall(
        tool_call_id="carry-state-hash",
        tool_name="correct_daily_report_date",
        arguments={
            "source_date_expression": "以上内容",
            "proposed_source_date": SOURCE_DATE.isoformat(),
            "target_date_expression": "8月13日",
            "proposed_target_date": TARGET_DATE.isoformat(),
            "acknowledged_empty_fields": [],
            "empty_field_evidence": [],
            "submit_after_correction": False,
        },
    )
    context = _context(report)
    bound, failure = await ShadowCallBinder(
        context,
        UnavailableDateResolver(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
        current_turn_source=CurrentTurnSource(("以上内容是8月13日的。",)),
    ).bind(call)
    assert failure is None
    assert bound is not None

    class CaptureCorrection:
        kwargs = None

        async def execute(self, **kwargs):
            self.kwargs = kwargs
            return DailyReportDateCorrectionResult(
                "blocked",
                "SOURCE_REPORT_BINDING_CHANGED",
                REPORT_ID,
                report.version,
                report.version,
            )

    capture = CaptureCorrection()
    executor = ProductionDailyExecutor.__new__(ProductionDailyExecutor)
    executor._bound_calls = {call.tool_call_id: bound}
    executor._context = context
    executor._user = SimpleNamespace(id=USER_ID)
    executor._date_correction = capture

    async def no_target_snapshot(report_date):
        assert report_date == TARGET_DATE
        return None

    executor._snapshot = no_target_snapshot
    request = ProductionHandlerRequest(
        tool_call_id=call.tool_call_id,
        tool_name=call.tool_name,
        arguments=CorrectDailyReportDateArgs.model_validate(call.arguments),
        executor=executor,
        memory_executor=SimpleNamespace(),
    )

    await executor.correct_daily_report_date(request)

    assert capture.kwargs is not None
    assert capture.kwargs["expected_source_state_sha256"] == (
        report_state_hash(report)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "closed_gate",
    (
        "intervening_message",
        "expired_receipt",
        "non_direct_conversation",
        "stale_source_version",
        "target_outside_adjacent_day",
        "completed_report",
        "non_add_daily_receipt",
    ),
)
async def test_ordinary_historical_lock_stays_closed_without_every_recent_receipt_gate(
    closed_gate: str,
) -> None:
    report = _source_report()
    context = _context(report)
    target_date = TARGET_DATE
    if closed_gate == "intervening_message":
        context = context.model_copy(
            update={
                "recent_messages": (
                    *context.recent_messages,
                    TrustedRecentMessage(
                        role="user",
                        content="先查一下别的事情。",
                        source_message_id="intervening-turn",
                    ),
                    TrustedRecentMessage(
                        role="assistant",
                        content="已经回答了另一件事。",
                        source_message_id="intervening-turn:assistant",
                        source_turn_id="intervening-turn",
                    ),
                )
            }
        )
    elif closed_gate == "expired_receipt":
        context = context.model_copy(
            update={
                "recent_operations": (
                    _recent_write(report).model_copy(
                        update={
                            "occurred_at": NOW - timedelta(minutes=11)
                        }
                    ),
                )
            }
        )
    elif closed_gate == "non_direct_conversation":
        context = context.model_copy(
            update={
                "principal": context.principal.model_copy(
                    update={"conversation_kind": "group"}
                )
            }
        )
    elif closed_gate == "stale_source_version":
        receipt_report = report
        report = _source_report(version=10)
        context = _context(
            report,
            recent_write=_recent_write(
                report,
                receipt_report=receipt_report,
            ),
        )
    elif closed_gate == "target_outside_adjacent_day":
        target_date = date(2026, 8, 12)
    elif closed_gate == "completed_report":
        report = _source_report(status="completed")
        context = _context(report)
    elif closed_gate == "non_add_daily_receipt":
        context = context.model_copy(
            update={
                "recent_operations": (
                    _recent_write(report).model_copy(
                        update={"tool_name": "edit_daily_items"}
                    ),
                )
            }
        )

    call = NativeToolCall(
        tool_call_id=f"blocked-{closed_gate}",
        tool_name="correct_daily_report_date",
        arguments={
            "source_date_expression": "以上内容",
            "proposed_source_date": SOURCE_DATE.isoformat(),
            "target_date_expression": target_date.isoformat(),
            "proposed_target_date": target_date.isoformat(),
            "acknowledged_empty_fields": [],
            "empty_field_evidence": [],
            "submit_after_correction": False,
        },
    )

    bound, failure = await ShadowCallBinder(
        context,
        UnavailableDateResolver(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
        current_turn_source=CurrentTurnSource(
            (f"以上内容是{target_date.isoformat()}的。",)
        ),
    ).bind(call)

    assert bound is None
    assert failure is not None
    assert failure.error_code == "HISTORICAL_REPORT_LOCKED_AFTER_CUTOFF"


@pytest.mark.parametrize(
    "foreign_scope",
    (
        {"user_id": UUID("44444444-4444-4444-4444-444444444444")},
        {"conversation_id": "another-direct-conversation"},
    ),
)
def test_foreign_user_or_conversation_receipt_never_enters_trusted_context(
    foreign_scope: dict[str, object],
) -> None:
    report = _source_report()
    context = _context(report)
    foreign_operation = _recent_write(report).model_copy(
        update=foreign_scope
    )
    payload = context.model_dump(mode="python")
    payload["recent_operations"] = [
        foreign_operation.model_dump(mode="python")
    ]

    with pytest.raises(ValueError):
        TrustedContext.model_validate(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("state_change", ("submit", "acknowledge_empty"))
async def test_post_cutoff_receipt_exception_cannot_change_report_state(
    state_change: str,
) -> None:
    report = _source_report()
    arguments = {
        "source_date_expression": "以上内容",
        "proposed_source_date": SOURCE_DATE.isoformat(),
        "target_date_expression": "8月13日",
        "proposed_target_date": TARGET_DATE.isoformat(),
        "acknowledged_empty_fields": [],
        "empty_field_evidence": [],
        "submit_after_correction": state_change == "submit",
    }
    current_message = "以上内容是8月13日的。"
    if state_change == "acknowledge_empty":
        arguments["acknowledged_empty_fields"] = ["problems"]
        arguments["empty_field_evidence"] = [
            {
                "field": "problems",
                "source_evidence": {"source_message_index": 1},
            }
        ]
        current_message = "以上内容是8月13日的，问题风险没有。"
    call = NativeToolCall(
        tool_call_id=f"state-change-{state_change}",
        tool_name="correct_daily_report_date",
        arguments=arguments,
    )

    bound, failure = await ShadowCallBinder(
        _context(report),
        UnavailableDateResolver(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
        current_turn_source=CurrentTurnSource((current_message,)),
    ).bind(call)

    assert bound is None
    assert failure is not None
    assert failure.error_code == "HISTORICAL_REPORT_LOCKED_AFTER_CUTOFF"


@pytest.mark.asyncio
async def test_exact_date_correction_audit_allows_only_the_same_request_replay() -> None:
    target = _source_report(
        version=10,
        report_date=TARGET_DATE,
        correction_source_message_id="correct-report-date",
    )

    bound, failure = await ShadowCallBinder(
        _replay_context(target),
        UnavailableDateResolver(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
        current_turn_source=CurrentTurnSource(("以上内容是8月13日的。",)),
    ).bind(_date_correction_call(tool_call_id="replayed-date-correction"))

    assert failure is None
    assert bound is not None
    assert bound.source_report == target
    assert bound.date_facts["idempotent_date_correction_replay"] is True

    unrelated_bound, unrelated_failure = await ShadowCallBinder(
        _replay_context(target, source_message_id="another-user-turn"),
        UnavailableDateResolver(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
        current_turn_source=CurrentTurnSource(("把今天的日报改到8月13日。",)),
    ).bind(_date_correction_call(tool_call_id="unrelated-date-correction"))

    assert unrelated_bound is None
    assert unrelated_failure is not None
    assert unrelated_failure.error_code == "SOURCE_REPORT_NOT_FOUND"


@pytest.mark.asyncio
async def test_same_provider_replay_keeps_the_original_receipt_fingerprint() -> None:
    call = _date_correction_call(tool_call_id="stable-replay-call")
    original_context = _context(_source_report())
    original_bound, original_failure = await ShadowCallBinder(
        original_context,
        UnavailableDateResolver(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
        current_turn_source=CurrentTurnSource(("以上内容是8月13日的。",)),
    ).bind(call)
    assert original_failure is None
    assert original_bound is not None

    target = _source_report(
        version=10,
        report_date=TARGET_DATE,
        correction_source_message_id="correct-report-date",
    )
    replay_context = _replay_context(target)
    replay_bound, replay_failure = await ShadowCallBinder(
        replay_context,
        UnavailableDateResolver(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
        current_turn_source=CurrentTurnSource(("以上内容是8月13日的。",)),
    ).bind(call)
    assert replay_failure is None
    assert replay_bound is not None

    original = _prepare_call(original_context, original_bound)
    replay = _prepare_call(replay_context, replay_bound)

    assert replay.request_fingerprint == original.request_fingerprint
    assert replay.operation_fingerprint == original.operation_fingerprint


@pytest.mark.asyncio
async def test_public_binder_to_executor_replay_is_a_zero_write_no_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_lock(session, user_id, report_date):
        del session, user_id, report_date

    monkeypatch.setattr(
        "app.agent2.tool_calling.daily_report_date_correction."
        "acquire_daily_report_advisory_lock",
        no_lock,
    )
    target = _source_report(
        version=10,
        report_date=TARGET_DATE,
        correction_source_message_id="correct-report-date",
    )
    context = _replay_context(target)
    call = _date_correction_call(tool_call_id="public-replay")
    bound, failure = await ShadowCallBinder(
        context,
        UnavailableDateResolver(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
        current_turn_source=CurrentTurnSource(("以上内容是8月13日的。",)),
    ).bind(call)
    assert failure is None
    assert bound is not None

    row = _report_row()
    row.report_date = TARGET_DATE
    row.section_status["_agent2_report_version"] = 10
    row.section_status[TYPED_AUDIT_KEY] = [
        {
            "command_type": "correct_report_date",
            "report_id": str(REPORT_ID),
            "source_report_date": SOURCE_DATE.isoformat(),
            "target_report_date": TARGET_DATE.isoformat(),
            "source_message_id": "correct-report-date",
            "actual_write": True,
            "result": "executed",
        }
    ]
    session = _CorrectionSession([row])
    executor = ProductionDailyExecutor.__new__(ProductionDailyExecutor)
    executor._bound_calls = {call.tool_call_id: bound}
    executor._context = context
    executor._user = SimpleNamespace(id=USER_ID)
    executor._date_correction = SqlDailyReportDateCorrection(session)

    async def snapshot(report_date):
        return target if report_date == TARGET_DATE else None

    executor._snapshot = snapshot
    request = ProductionHandlerRequest(
        tool_call_id=call.tool_call_id,
        tool_name=call.tool_name,
        arguments=CorrectDailyReportDateArgs.model_validate(call.arguments),
        executor=executor,
        memory_executor=SimpleNamespace(),
    )

    outcome = await executor.correct_daily_report_date(request)

    assert outcome.status_if_unchanged == ReceiptStatus.NO_OP
    assert outcome.safe_user_facts["actual_write"] is False
    assert row.report_date == TARGET_DATE
    assert row.section_status["_agent2_report_version"] == 10
    assert session.added == []


@pytest.mark.asyncio
async def test_occupied_target_date_remains_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_lock(session, user_id, report_date):
        del session, user_id, report_date

    monkeypatch.setattr(
        "app.agent2.tool_calling.daily_report_date_correction."
        "acquire_daily_report_advisory_lock",
        no_lock,
    )
    source = _report_row()
    target = _report_row()
    target.id = UUID("33333333-3333-3333-3333-333333333333")
    target.report_date = TARGET_DATE
    target.today_work = ["目标日期已有内容"]
    session = _CorrectionSession([source, target])

    result = await SqlDailyReportDateCorrection(session).execute(
        user=SimpleNamespace(id=USER_ID),
        tenant_id=TENANT_ID,
        source_report_id=REPORT_ID,
        expected_version=9,
        source_date=SOURCE_DATE,
        target_date=TARGET_DATE,
        acknowledged_empty_fields=(),
        submit_after_correction=False,
        idempotency_key="occupied-target-correction",
        source_message_id="occupied-target-message",
        now=NOW,
    )

    assert result.status == "clarification_required"
    assert result.error_code == "TARGET_REPORT_ALREADY_EXISTS"
    assert source.report_date == SOURCE_DATE
    assert target.report_date == TARGET_DATE
    assert target.today_work == ["目标日期已有内容"]
    assert session.added == []


@pytest.mark.asyncio
async def test_repeated_same_date_correction_is_an_idempotent_no_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_lock(session, user_id, report_date):
        del session, user_id, report_date

    monkeypatch.setattr(
        "app.agent2.tool_calling.daily_report_date_correction."
        "acquire_daily_report_advisory_lock",
        no_lock,
    )
    source = _report_row()
    session = _CorrectionSession([source])
    executor = SqlDailyReportDateCorrection(session)
    arguments = {
        "user": SimpleNamespace(id=USER_ID),
        "tenant_id": TENANT_ID,
        "source_report_id": REPORT_ID,
        "expected_version": 9,
        "source_date": SOURCE_DATE,
        "target_date": TARGET_DATE,
        "acknowledged_empty_fields": (),
        "submit_after_correction": False,
        "idempotency_key": "repeat-date-correction",
        "source_message_id": "repeat-date-message",
        "now": NOW,
    }

    first = await executor.execute(**arguments)
    second = await executor.execute(**arguments)

    assert first.status == "success"
    assert second.status == "no_op"
    assert source.report_date == TARGET_DATE
    assert source.section_status["_agent2_report_version"] == 10
    assert len(session.added) == 1


@pytest.mark.asyncio
async def test_source_and_empty_target_are_locked_before_the_atomic_move(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locked_dates: list[date] = []

    async def record_lock(session, user_id, report_date):
        del session
        assert user_id == USER_ID
        locked_dates.append(report_date)

    monkeypatch.setattr(
        "app.agent2.tool_calling.daily_report_date_correction."
        "acquire_daily_report_advisory_lock",
        record_lock,
    )
    source = _report_row()

    class LockCheckingSession(_CorrectionSession):
        async def scalars(self, statement):
            assert locked_dates == sorted((SOURCE_DATE, TARGET_DATE))
            return await super().scalars(statement)

    session = LockCheckingSession([source])

    result = await SqlDailyReportDateCorrection(session).execute(
        user=SimpleNamespace(id=USER_ID),
        tenant_id=TENANT_ID,
        source_report_id=REPORT_ID,
        expected_version=9,
        source_date=SOURCE_DATE,
        target_date=TARGET_DATE,
        acknowledged_empty_fields=(),
        submit_after_correction=False,
        idempotency_key="locked-date-correction",
        source_message_id="locked-date-message",
        now=NOW,
    )

    assert result.status == "success"
    assert source.report_date == TARGET_DATE
    assert locked_dates == sorted((SOURCE_DATE, TARGET_DATE))


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ("collecting", "pending_confirmation"))
async def test_date_only_move_preserves_the_existing_workflow_state(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    async def no_lock(session, user_id, report_date):
        del session, user_id, report_date

    monkeypatch.setattr(
        "app.agent2.tool_calling.daily_report_date_correction."
        "acquire_daily_report_advisory_lock",
        no_lock,
    )
    source = _report_row(status=status)
    original_workflow_state = (
        source.status,
        source.confirmation_type,
        source.confirmed_by_user,
        source.submitted_at,
        source.pending_confirmation_at,
        source.auto_submit_at,
    )

    result = await SqlDailyReportDateCorrection(
        _CorrectionSession([source])
    ).execute(
        user=SimpleNamespace(id=USER_ID),
        tenant_id=TENANT_ID,
        source_report_id=REPORT_ID,
        expected_version=9,
        expected_source_state_sha256=report_state_hash(
            _source_report(status=status)
        ),
        source_date=SOURCE_DATE,
        target_date=TARGET_DATE,
        acknowledged_empty_fields=(),
        submit_after_correction=False,
        idempotency_key=f"preserve-{status}-state",
        source_message_id=f"preserve-{status}-message",
        now=NOW,
    )

    assert result.status == "success"
    assert source.report_date == TARGET_DATE
    assert (
        source.status,
        source.confirmation_type,
        source.confirmed_by_user,
        source.submitted_at,
        source.pending_confirmation_at,
        source.auto_submit_at,
    ) == original_workflow_state


@pytest.mark.asyncio
async def test_outer_turn_rollback_restores_date_state_and_audit_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_lock(session, user_id, report_date):
        del session, user_id, report_date

    monkeypatch.setattr(
        "app.agent2.tool_calling.daily_report_date_correction."
        "acquire_daily_report_advisory_lock",
        no_lock,
    )
    session = _TransactionalCorrectionSession([_report_row()])
    baseline_rows = deepcopy(session.rows)
    savepoint = await session.begin_nested()

    result = await SqlDailyReportDateCorrection(session).execute(
        user=SimpleNamespace(id=USER_ID),
        tenant_id=TENANT_ID,
        source_report_id=REPORT_ID,
        expected_version=9,
        expected_source_state_sha256=report_state_hash(_source_report()),
        source_date=SOURCE_DATE,
        target_date=TARGET_DATE,
        acknowledged_empty_fields=(),
        submit_after_correction=False,
        idempotency_key="rolled-back-date-correction",
        source_message_id="rolled-back-date-message",
        now=NOW,
    )

    assert result.status == "success"
    assert session.rows[0].report_date == TARGET_DATE
    assert len(session.added) == 1

    # The outer Agent2 turn owns the savepoint.  A later reply failure must
    # remove the date move and its audit receipt together.
    await savepoint.rollback()

    assert session.rows == baseline_rows
    assert session.added == []


def test_successful_add_receipt_carries_the_exact_report_state_fingerprint() -> None:
    report = _source_report()
    row = SimpleNamespace(
        status="success",
        target_type="daily_report",
        target_id=str(REPORT_ID),
        after_version=report.version,
        safe_user_facts={"report_snapshot": _safe_report_snapshot(report)},
    )

    reference = _trusted_report_reference_from_receipt(row)

    assert reference is not None
    assert reference.report_version == report.version
    assert reference.report_state_sha256 == report_state_hash(report)


def test_production_snapshot_exposes_only_an_exact_atomic_date_move_audit() -> None:
    row = _report_row()
    row.report_date = TARGET_DATE
    row.section_status["_agent2_report_version"] = 10
    exact_audit = {
        "command_type": "correct_report_date",
        "report_id": str(REPORT_ID),
        "source_report_date": SOURCE_DATE.isoformat(),
        "target_report_date": TARGET_DATE.isoformat(),
        "source_message_id": "correct-report-date",
        "actual_write": True,
        "result": "executed",
    }
    row.section_status[TYPED_AUDIT_KEY] = [exact_audit]

    snapshot = trusted_snapshot_from_report(
        user=SimpleNamespace(id=USER_ID),
        tenant_id=TENANT_ID,
        report_date=TARGET_DATE,
        report=row,
    )

    assert snapshot.date_correction_reference is not None
    assert snapshot.date_correction_reference.report_id == REPORT_ID
    assert (
        snapshot.date_correction_reference.source_message_id
        == "correct-report-date"
    )
    assert "date_correction_reference" not in snapshot.safe_snapshot()

    row.section_status[TYPED_AUDIT_KEY] = [
        {
            **exact_audit,
            "report_id": "33333333-3333-3333-3333-333333333333",
        }
    ]
    mismatched = trusted_snapshot_from_report(
        user=SimpleNamespace(id=USER_ID),
        tenant_id=TENANT_ID,
        report_date=TARGET_DATE,
        report=row,
    )
    assert mismatched.date_correction_reference is None


@pytest.mark.asyncio
async def test_history_lock_exposes_permanent_cutoff_and_real_read_option() -> None:
    report = _source_report()
    context = _context(report).model_copy(
        update={"recent_messages": (), "recent_operations": ()}
    )
    call = NativeToolCall(
        tool_call_id="ordinary-history-lock",
        tool_name="correct_daily_report_date",
        arguments={
            "source_date_expression": "今天",
            "proposed_source_date": SOURCE_DATE.isoformat(),
            "target_date_expression": "8月13日",
            "proposed_target_date": TARGET_DATE.isoformat(),
            "acknowledged_empty_fields": [],
            "empty_field_evidence": [],
            "submit_after_correction": False,
        },
    )

    bound, failure = await ShadowCallBinder(
        context,
        UnavailableDateResolver(),
        None,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
        current_turn_source=CurrentTurnSource(("把今天这份改到8月13日。",)),
    ).bind(call)

    assert bound is None
    assert failure is not None
    assert failure.safe_user_facts["historical_report_lock"] == {
        "report_date": TARGET_DATE.isoformat(),
        "cutoff_local_time": "09:00",
        "automatically_unlocks": False,
        "allowed_actions": ["query_report_by_date"],
    }


def test_model_reply_must_echo_the_server_verified_non_unlocking_lock_state() -> None:
    facts_token = "{{historical_report_lock_facts}}"
    lock_state = {
        "report_date": TARGET_DATE.isoformat(),
        "cutoff_local_time": "09:00",
        "automatically_unlocks": False,
        "allowed_actions": ["query_report_by_date"],
    }
    receipt = ToolReceipt(
        status=ReceiptStatus.BLOCKED,
        tool_name="correct_daily_report_date",
        changed=False,
        error_code="HISTORICAL_REPORT_LOCKED_AFTER_CUTOFF",
        safe_user_facts={
            "actual_write": False,
            "historical_report_lock": lock_state,
        },
        execution_mode=ExecutionMode.CANARY_EXECUTE,
    )

    protocol = write_reply_protocol((receipt,))
    envelope, errors = validate_write_reply(
        json.dumps(
            {
                "reply": facts_token,
                "actual_write": False,
                "operation_outcome": "not_executed",
                "historical_report_lock_state": lock_state,
            },
            ensure_ascii=False,
        ),
        (receipt,),
    )

    assert protocol["expected_historical_report_lock_state"] == lock_state
    assert protocol["historical_report_lock_facts_token"] == facts_token
    assert envelope is not None, errors
    assert errors == ()
    assert render_write_reply(envelope, (receipt,)) == (
        "2026-08-13 的日报已在当日 09:00 后锁定，不会在之后自动解锁；"
        "目前只可查询，不能修改或移动。"
    )


def test_structurally_correct_reply_cannot_promise_a_future_unlock() -> None:
    lock_state = {
        "report_date": TARGET_DATE.isoformat(),
        "cutoff_local_time": "09:00",
        "automatically_unlocks": False,
        "allowed_actions": ["query_report_by_date"],
    }
    receipt = ToolReceipt(
        status=ReceiptStatus.BLOCKED,
        tool_name="correct_daily_report_date",
        changed=False,
        error_code="HISTORICAL_REPORT_LOCKED_AFTER_CUTOFF",
        safe_user_facts={"historical_report_lock": lock_state},
        execution_mode=ExecutionMode.CANARY_EXECUTE,
    )

    envelope, errors = validate_write_reply(
        json.dumps(
            {
                "reply": "明天会自动解锁，到时我再帮你移动。",
                "actual_write": False,
                "operation_outcome": "not_executed",
                "historical_report_lock_state": lock_state,
            },
            ensure_ascii=False,
        ),
        (receipt,),
    )

    assert envelope is None
    assert "reply must use only the verified historical lock facts token" in errors


def test_model_reply_without_the_verified_lock_state_is_rejected() -> None:
    lock_state = {
        "report_date": TARGET_DATE.isoformat(),
        "cutoff_local_time": "09:00",
        "automatically_unlocks": False,
        "allowed_actions": ["query_report_by_date"],
    }
    receipt = ToolReceipt(
        status=ReceiptStatus.BLOCKED,
        tool_name="correct_daily_report_date",
        changed=False,
        error_code="HISTORICAL_REPORT_LOCKED_AFTER_CUTOFF",
        safe_user_facts={"historical_report_lock": lock_state},
        execution_mode=ExecutionMode.CANARY_EXECUTE,
    )

    envelope, errors = validate_write_reply(
        json.dumps(
            {
                "reply": "这份日报已锁定。",
                "actual_write": False,
                "operation_outcome": "not_executed",
            },
            ensure_ascii=False,
        ),
        (receipt,),
    )

    assert envelope is None
    assert "historical_report_lock_state does not match server receipts" in errors


def test_internal_report_state_fingerprint_is_not_exposed_to_the_model() -> None:
    fingerprint = report_state_hash(_source_report())
    receipt = ToolReceipt(
        status=ReceiptStatus.SUCCESS,
        tool_name="add_daily_items",
        changed=True,
        safe_user_facts={
            "report_snapshot": {
                "report_date": SOURCE_DATE.isoformat(),
                "report_state_sha256": fingerprint,
            }
        },
        execution_mode=ExecutionMode.CANARY_EXECUTE,
    )

    model_facts = model_safe_user_facts(receipt)

    assert fingerprint not in json.dumps(model_facts, ensure_ascii=False)
    assert (
        receipt.safe_user_facts["report_snapshot"]["report_state_sha256"]
        == fingerprint
    )
