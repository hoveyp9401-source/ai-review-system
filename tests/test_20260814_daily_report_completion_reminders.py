from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

import pytest

from app.agent2.tool_calling.context import (
    TrustedReportItem,
    TrustedReportSnapshot,
)
from app.agent2.tool_calling.contracts import (
    ConfirmReportArgs,
    ExecutionMode,
    ReceiptStatus,
    ToolReceipt,
)
from app.agent2.tool_calling.production_daily_executor import ProductionDailyExecutor
from app.agent2.tool_calling.production_handlers import ProductionHandlerRequest
from app.agent2.tool_calling.receipt_reply import finalize_canary_content
from app.agent2.tool_calling.write_reply import (
    write_reply_protocol,
    write_reply_retry_instruction,
)
from app.agent2.typed_daily_commands import TypedDailyCommand
from app.agent2.typed_daily_executor import (
    TypedDailyExecutionContext,
    build_typed_daily_snapshot,
    execute_typed_agent2_daily_commands,
)
from app.scheduler import jobs
from app.scheduler.jobs import build_report_reminder_text, remind_missing_reports
from app.services.state_machine import assess_daily_report_completeness


class _Session:
    def __init__(self) -> None:
        self.statements = []
        self.receipt_after_states: list[dict] = []

    async def execute(self, statement):
        self.statements.append(statement)
        parameters = statement.compile().params
        after_json = parameters.get("after_json")
        if isinstance(after_json, dict):
            self.receipt_after_states.append(after_json)
        return SimpleNamespace(rowcount=1)

    async def scalars(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(all=list)

    async def flush(self) -> None:
        return None


def _command(
    *,
    report_id,
    command_type: str,
    version: int,
    patch: dict,
    suffix: str,
) -> TypedDailyCommand:
    return TypedDailyCommand(
        command_id=uuid5(NAMESPACE_URL, f"completion-command-{suffix}"),
        decision_id=uuid5(NAMESPACE_URL, "completion-decision"),
        sub_decision_id=uuid5(NAMESPACE_URL, f"completion-subdecision-{suffix}"),
        command_type=command_type,
        report_id=report_id,
        report_version=version,
        target_item_ids=(),
        patch=patch,
        idempotency_key=f"completion-message:daily:{suffix}",
    )


def _run_complete_report_turn(
    monkeypatch,
    *,
    report_date: date,
    occurred_at: datetime,
    suffix: str,
):
    user = SimpleNamespace(
        id=uuid5(NAMESPACE_URL, f"completion-user-{suffix}"),
        team_id=uuid5(NAMESPACE_URL, f"completion-team-{suffix}"),
        timezone="Asia/Shanghai",
    )
    snapshot = build_typed_daily_snapshot(
        user=user,
        report_date=report_date,
        report=None,
    )
    commands = (
        _command(
            report_id=snapshot.report_id,
            command_type="replace_section",
            version=0,
            patch={
                "field": "today_work",
                "items": [f"今日工作{i}" for i in range(1, 6)],
            },
            suffix=f"{suffix}-today-work",
        ),
        _command(
            report_id=snapshot.report_id,
            command_type="acknowledge_empty_section",
            version=1,
            patch={"field": "problems"},
            suffix=f"{suffix}-no-problems",
        ),
        _command(
            report_id=snapshot.report_id,
            command_type="replace_section",
            version=2,
            patch={
                "field": "tomorrow_plan",
                "items": [f"明日计划{i}" for i in range(1, 5)],
            },
            suffix=f"{suffix}-tomorrow-plan",
        ),
    )
    captured: dict = {}

    async def fake_lock(*_args, **_kwargs) -> None:
        return None

    async def fake_get_report(*_args, **_kwargs):
        return None

    async def fake_upsert(_session, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            id=kwargs["report_id_override"],
            today_work=kwargs["today_work"],
            problems=kwargs["problems"],
            tomorrow_plan=kwargs["tomorrow_plan"],
        )

    monkeypatch.setattr(
        "app.repositories.acquire_daily_report_advisory_lock",
        fake_lock,
    )
    monkeypatch.setattr("app.repositories.get_report", fake_get_report)
    monkeypatch.setattr("app.repositories.upsert_daily_report", fake_upsert)

    session = _Session()
    result = asyncio.run(
        execute_typed_agent2_daily_commands(
            session,
            user=user,
            commands=commands,
            execution_context=TypedDailyExecutionContext(
                report_date=report_date,
                source="agent2_completion_test",
                source_text_hash="a" * 64,
                occurred_at=occurred_at,
            ),
            settings=SimpleNamespace(timezone="Asia/Shanghai"),
            execution_authority="authenticated_admin_command",
        )
    )
    return result, captured, session


def test_one_turn_complete_report_enters_pending_confirmation(monkeypatch) -> None:
    occurred_at = datetime(
        2026,
        8,
        13,
        21,
        57,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    result, captured, session = _run_complete_report_turn(
        monkeypatch,
        report_date=date(2026, 8, 13),
        occurred_at=occurred_at,
        suffix="same-day",
    )

    assert result.status == "pending_confirmation"
    assert captured["status"] == "pending_confirmation"
    assert captured["completeness_score"] == 1.0
    assert captured["section_status"]["problems_acknowledged_empty"] is True
    assert captured["confirmation_type"] == "none"
    assert captured["confirmed_by_user"] is False
    assert captured["pending_confirmation_at"] == occurred_at
    assert "待确认" in result.message
    assert "日报已提交" not in result.message
    assert session.receipt_after_states[-1]["status"] == "pending_confirmation"


def test_next_morning_complete_catchup_requires_user_confirmation(monkeypatch) -> None:
    occurred_at = datetime(
        2026,
        8,
        14,
        9,
        20,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    result, captured, _session = _run_complete_report_turn(
        monkeypatch,
        report_date=date(2026, 8, 13),
        occurred_at=occurred_at,
        suffix="next-morning",
    )

    assert result.status == "pending_confirmation"
    assert captured["status"] == "pending_confirmation"
    assert captured["confirmation_type"] == "none"
    assert captured["confirmed_by_user"] is False
    assert "历史日报已补充完整" in result.message
    assert "请确认无误后再提交" in result.message
    assert "没有代你确认或提交" in result.message
    assert "次日上午自动提交" not in result.message


def test_early_next_morning_catchup_keeps_the_existing_auto_submit_rule(
    monkeypatch,
) -> None:
    result, captured, _session = _run_complete_report_turn(
        monkeypatch,
        report_date=date(2026, 8, 13),
        occurred_at=datetime(
            2026,
            8,
            14,
            8,
            30,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
        suffix="before-briefing",
    )

    assert captured["status"] == "pending_confirmation"
    assert "今天晨报前自动提交" in result.message
    assert "本次没有代你确认或提交" in result.message


def test_tool_receipt_exposes_the_safe_next_step_for_completed_catchup() -> None:
    executor = ProductionDailyExecutor.__new__(ProductionDailyExecutor)
    executor._context = SimpleNamespace(
        now=datetime(2026, 8, 14, 9, 20, tzinfo=ZoneInfo("Asia/Shanghai")),
        principal=SimpleNamespace(timezone="Asia/Shanghai"),
    )
    executor._settings = SimpleNamespace(
        summary_cron_hour=9,
        summary_cron_minute=0,
    )
    executor._tool_idempotency_key = lambda _request: "daily:test"
    report = TrustedReportSnapshot(
        report_id=uuid5(NAMESPACE_URL, "completion-report"),
        tenant_id="tenant-test",
        owner_user_id=uuid5(NAMESPACE_URL, "completion-user"),
        report_date=date(2026, 8, 13),
        version=3,
        status="pending_confirmation",
        acknowledged_empty_fields=frozenset({"problems"}),
    )

    outcome = executor._outcome(
        SimpleNamespace(),
        before=None,
        after=report,
        typed_receipt_ids=(),
    )

    assert outcome.safe_user_facts is not None
    assert "请确认无误后再提交" in outcome.safe_user_facts["next_step"]
    assert "没有代你确认或提交" in outcome.safe_user_facts["next_step"]


def test_completed_report_receipt_does_not_offer_confirmation_or_a_draft() -> None:
    executor = ProductionDailyExecutor.__new__(ProductionDailyExecutor)
    executor._context = SimpleNamespace(
        now=datetime(2026, 8, 14, 21, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        principal=SimpleNamespace(timezone="Asia/Shanghai"),
    )
    executor._settings = SimpleNamespace()
    executor._tool_idempotency_key = lambda _request: "daily:completed"
    report_id = uuid5(NAMESPACE_URL, "completed-state-report")
    owner_id = uuid5(NAMESPACE_URL, "completed-state-owner")
    report = TrustedReportSnapshot(
        report_id=report_id,
        tenant_id="tenant-test",
        owner_user_id=owner_id,
        report_date=date(2026, 8, 14),
        version=4,
        status="completed",
        items=(
            TrustedReportItem(
                item_id="completed-work",
                field="today_work",
                content="完成合同审核",
                report_id=report_id,
                report_version=4,
            ),
            TrustedReportItem(
                item_id="completed-plan",
                field="tomorrow_plan",
                content="继续跟进项目",
                report_id=report_id,
                report_version=4,
            ),
        ),
        acknowledged_empty_fields=frozenset({"problems"}),
    )

    outcome = executor._outcome(
        SimpleNamespace(),
        before=report,
        after=report,
        typed_receipt_ids=(),
    )

    assert outcome.safe_user_facts is not None
    assert outcome.safe_user_facts["content_complete"] is True
    assert outcome.safe_user_facts["confirmation_available"] is False
    assert outcome.safe_user_facts["persisted_draft_available"] is False


@pytest.mark.asyncio
async def test_confirming_an_already_completed_report_returns_a_noop_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_id = uuid5(NAMESPACE_URL, "already-completed-report")
    user_id = uuid5(NAMESPACE_URL, "already-completed-user")
    report = TrustedReportSnapshot(
        report_id=report_id,
        tenant_id="tenant-test",
        owner_user_id=user_id,
        report_date=date(2026, 8, 19),
        version=7,
        status="completed",
        items=(
            TrustedReportItem(
                item_id="completed-work",
                field="today_work",
                content="完成合同审核",
                report_id=report_id,
                report_version=7,
            ),
            TrustedReportItem(
                item_id="completed-plan",
                field="tomorrow_plan",
                content="继续跟进项目",
                report_id=report_id,
                report_version=7,
            ),
        ),
        acknowledged_empty_fields=frozenset({"problems"}),
    )
    executor = ProductionDailyExecutor.__new__(ProductionDailyExecutor)
    executor._context = SimpleNamespace(
        principal=SimpleNamespace(
            tenant_id="tenant-test",
            user_id=user_id,
            conversation_id="conversation-test",
            source_message_id="message-test",
            timezone="Asia/Shanghai",
        ),
        now=datetime(2026, 8, 20, 9, 8, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    executor._settings = SimpleNamespace()
    executor._arguments = lambda request, _expected_type: request.arguments
    executor._bound = lambda _request: SimpleNamespace(report=report)
    executor._required_bound_report = lambda _bound: report
    executor._require_same_report = lambda _trusted, _live_id: None
    executor._tool_idempotency_key = lambda _request: "daily:already-completed"

    async def snapshot(_report_date):
        return report

    async def typed_snapshot(_report_date):
        return SimpleNamespace(
            report_id=report_id,
            version=7,
            status="completed",
            today_work=("完成合同审核",),
            problems=(),
            tomorrow_plan=("继续跟进项目",),
            acknowledged_empty_fields=frozenset({"problems"}),
        )

    async def must_not_execute(*_args, **_kwargs):
        raise AssertionError("completed confirmation must not write again")

    monkeypatch.setattr(executor, "_snapshot", snapshot)
    monkeypatch.setattr(executor, "_typed_snapshot", typed_snapshot)
    monkeypatch.setattr(executor, "_execute_typed", must_not_execute)
    request = ProductionHandlerRequest(
        tool_call_id="confirm-completed",
        tool_name="confirm_report",
        arguments=ConfirmReportArgs(
            report_id=report_id,
            expected_version=7,
        ),
        executor=executor,
        memory_executor=executor,
    )

    outcome = await executor.confirm_report(request)

    assert outcome.status_if_unchanged == ReceiptStatus.NO_OP
    assert outcome.before_report == report
    assert outcome.after_report == report
    assert outcome.safe_user_facts is not None
    assert outcome.safe_user_facts["actual_write"] is False
    assert outcome.safe_user_facts["report_status"] == "completed"
    assert outcome.safe_user_facts["confirmation_available"] is False
    assert outcome.safe_user_facts["persisted_draft_available"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("missing_field", "expected_missing"),
    (
        ("today_work", ("today_work",)),
        ("problems", ("problems",)),
        ("tomorrow_plan", ("tomorrow_plan",)),
    ),
)
async def test_incomplete_confirmation_returns_only_the_actual_missing_section(
    monkeypatch: pytest.MonkeyPatch,
    missing_field: str,
    expected_missing: tuple[str, ...],
) -> None:
    report_id = uuid5(NAMESPACE_URL, f"missing-section-{missing_field}")
    user_id = uuid5(NAMESPACE_URL, f"missing-section-user-{missing_field}")
    items = tuple(
        TrustedReportItem(
            item_id=f"item-{field_name}",
            field=field_name,
            content=f"{field_name}内容",
            report_id=report_id,
            report_version=2,
        )
        for field_name in ("today_work", "tomorrow_plan")
        if field_name != missing_field
    )
    acknowledged_empty_fields = (
        frozenset({"problems"})
        if missing_field != "problems"
        else frozenset()
    )
    report = TrustedReportSnapshot(
        report_id=report_id,
        tenant_id="tenant-test",
        owner_user_id=user_id,
        report_date=date(2026, 8, 14),
        version=2,
        status="collecting",
        items=items,
        acknowledged_empty_fields=acknowledged_empty_fields,
    )
    executor = ProductionDailyExecutor.__new__(ProductionDailyExecutor)
    executor._context = SimpleNamespace(
        principal=SimpleNamespace(
            tenant_id="tenant-test",
            user_id=user_id,
            conversation_id="conversation-test",
            source_message_id="message-test",
            timezone="Asia/Shanghai",
        ),
        now=datetime(2026, 8, 14, 21, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    executor._settings = SimpleNamespace()
    executor._source_text_hash = "a" * 64
    executor._arguments = lambda request, _expected_type: request.arguments
    executor._bound = lambda _request: SimpleNamespace(report=report)
    executor._required_bound_report = lambda _bound: report
    executor._require_same_report = lambda _trusted, _live_id: None
    executor._tool_idempotency_key = lambda _request: "daily:confirm-incomplete"

    async def snapshot(_report_date):
        return report

    async def typed_snapshot(_report_date):
        return SimpleNamespace(
            report_id=report_id,
            version=2,
            status="collecting",
            today_work=tuple(
                item.content for item in items if item.field == "today_work"
            ),
            problems=(),
            tomorrow_plan=tuple(
                item.content for item in items if item.field == "tomorrow_plan"
            ),
            acknowledged_empty_fields=acknowledged_empty_fields,
        )

    async def must_not_execute(*_args, **_kwargs):
        raise AssertionError("an incomplete confirmation must not reach the writer")

    monkeypatch.setattr(executor, "_snapshot", snapshot)
    monkeypatch.setattr(executor, "_typed_snapshot", typed_snapshot)
    monkeypatch.setattr(executor, "_execute_typed", must_not_execute)
    request = ProductionHandlerRequest(
        tool_call_id="confirm-incomplete",
        tool_name="confirm_report",
        arguments=ConfirmReportArgs(
            report_id=report_id,
            expected_version=2,
        ),
        executor=executor,
        memory_executor=executor,
    )

    outcome = await executor.confirm_report(request)

    assert outcome.status_if_unchanged == ReceiptStatus.CLARIFICATION_REQUIRED
    assert outcome.error_code == "REPORT_INCOMPLETE"
    assert outcome.before_report == report
    assert outcome.after_report == report
    assert outcome.safe_user_facts is not None
    assert tuple(outcome.safe_user_facts["missing_sections"]) == expected_missing
    assert len(outcome.safe_user_facts["missing_section_labels"]) == 1
    assert outcome.safe_user_facts["section_states"][missing_field] == "missing"
    assert outcome.safe_user_facts["confirmation_available"] is False
    assert outcome.safe_user_facts["persisted_draft_available"] is True
    assert outcome.safe_user_facts["actual_write"] is False


@pytest.mark.asyncio
async def test_independently_reviewed_submit_acknowledges_only_the_missing_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_id = uuid5(NAMESPACE_URL, "reviewed-incomplete-submit")
    user_id = uuid5(NAMESPACE_URL, "reviewed-incomplete-submit-user")
    report = TrustedReportSnapshot(
        report_id=report_id,
        tenant_id="tenant-test",
        owner_user_id=user_id,
        report_date=date(2026, 8, 14),
        version=2,
        status="collecting",
        items=(
            TrustedReportItem(
                item_id="today-1",
                field="today_work",
                content="完成合同复核",
                report_id=report_id,
                report_version=2,
            ),
            TrustedReportItem(
                item_id="plan-1",
                field="tomorrow_plan",
                content="继续跟进",
                report_id=report_id,
                report_version=2,
            ),
        ),
    )
    completed = report.model_copy(update={"version": 4, "status": "completed"})
    executor = ProductionDailyExecutor.__new__(ProductionDailyExecutor)
    executor._context = SimpleNamespace(
        principal=SimpleNamespace(
            tenant_id="tenant-test",
            user_id=user_id,
            conversation_id="conversation-test",
            source_message_id="message-test",
            timezone="Asia/Shanghai",
        ),
        now=datetime(2026, 8, 14, 21, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    executor._settings = SimpleNamespace()
    executor._source_text_hash = "a" * 64
    executor._arguments = lambda request, _expected_type: request.arguments
    executor._bound = lambda _request: SimpleNamespace(report=report)
    executor._required_bound_report = lambda _bound: report
    executor._require_same_report = lambda _trusted, _live_id: None
    executor._tool_idempotency_key = lambda _request: "daily:reviewed-submit"
    snapshots = iter((report, completed))

    async def snapshot(_report_date):
        return next(snapshots)

    async def typed_snapshot(_report_date):
        return SimpleNamespace(
            report_id=report_id,
            version=2,
            status="collecting",
            today_work=("完成合同复核",),
            problems=(),
            tomorrow_plan=("继续跟进",),
            acknowledged_empty_fields=frozenset(),
        )

    captured = []

    async def execute(_report_date, commands, **_kwargs):
        captured.extend(commands)
        return ("receipt-1", "receipt-2")

    monkeypatch.setattr(executor, "_snapshot", snapshot)
    monkeypatch.setattr(executor, "_typed_snapshot", typed_snapshot)
    monkeypatch.setattr(executor, "_execute_typed", execute)
    request = ProductionHandlerRequest(
        tool_call_id="reviewed-confirm",
        tool_name="confirm_report",
        arguments=ConfirmReportArgs(
            report_id=report_id,
            expected_version=2,
            reviewed_omitted_empty_fields=("problems",),
        ),
        executor=executor,
        memory_executor=executor,
    )

    outcome = await executor.confirm_report(request)

    assert [command.command_type for command in captured] == [
        "acknowledge_empty_section",
        "submit_report",
    ]
    assert [command.report_version for command in captured] == [2, 3]
    assert captured[0].patch == {"field": "problems"}
    assert outcome.after_report == completed
    assert outcome.typed_receipt_ids == ("receipt-1", "receipt-2")


def test_incomplete_confirmation_reply_cannot_expand_one_missing_section_to_three() -> None:
    receipt = ToolReceipt(
        status=ReceiptStatus.CLARIFICATION_REQUIRED,
        tool_name="confirm_report",
        changed=False,
        target_type="daily_report",
        target_id="report-test",
        error_code="REPORT_INCOMPLETE",
        safe_user_facts={
            "actual_write": False,
            "section_states": {
                "today_work": "filled",
                "problems": "missing",
                "tomorrow_plan": "filled",
            },
            "section_labels": {
                "today_work": "今日工作",
                "problems": "问题/风险",
                "tomorrow_plan": "明日计划",
            },
            "missing_sections": ["problems"],
            "missing_section_labels": ["问题/风险"],
            "confirmation_available": False,
            "persisted_draft_available": True,
        },
        execution_mode=ExecutionMode.CANARY_EXECUTE,
    )
    unsafe_model_reply = json.dumps(
        {
            "reply": "请补充今日工作、问题/风险和明日计划中的缺项。",
            "actual_write": False,
            "operation_outcome": "needs_clarification",
            "daily_report_state": {
                "section_states": {
                    "today_work": "filled",
                    "problems": "missing",
                    "tomorrow_plan": "filled",
                },
                "missing_sections": ["problems"],
                "confirmation_available": False,
                "persisted_draft_available": True,
            },
        },
        ensure_ascii=False,
    )

    with pytest.raises(ValueError, match="write reply failed receipt validation"):
        finalize_canary_content(
            unsafe_model_reply,
            (receipt,),
            write_batch_seen=True,
        )

    safe_model_reply = json.dumps(
        {
            "reply": (
                "这份日报尚未提交，目前只缺"
                "{{daily_missing_section_labels}}；如果确实没有，直接说明即可。"
            ),
            "actual_write": False,
            "operation_outcome": "needs_clarification",
            "daily_report_state": {
                "section_states": {
                    "today_work": "filled",
                    "problems": "missing",
                    "tomorrow_plan": "filled",
                },
                "missing_sections": ["problems"],
                "confirmation_available": False,
                "persisted_draft_available": True,
            },
        },
        ensure_ascii=False,
    )
    final_reply, _model_hash = finalize_canary_content(
        safe_model_reply,
        (receipt,),
        write_batch_seen=True,
    )

    assert final_reply.startswith("这份日报尚未提交")
    assert "只缺问题/风险" in final_reply
    assert "{{daily_missing_section_labels}}" not in final_reply

    filled_label_reply = json.dumps(
        {
            "reply": (
                "今日工作和明日计划已经记录，目前只缺"
                "{{daily_missing_section_labels}}。"
            ),
            "actual_write": False,
            "operation_outcome": "needs_clarification",
            "daily_report_state": {
                "section_states": {
                    "today_work": "filled",
                    "problems": "missing",
                    "tomorrow_plan": "filled",
                },
                "missing_sections": ["problems"],
                "confirmation_available": False,
                "persisted_draft_available": True,
            },
        },
        ensure_ascii=False,
    )
    filled_label_final, _ = finalize_canary_content(
        filled_label_reply,
        (receipt,),
        write_batch_seen=True,
    )
    assert filled_label_final == (
        "今日工作和明日计划已经记录，目前只缺问题/风险。"
    )


def test_incomplete_daily_reply_preserves_another_domain_success() -> None:
    daily_receipt = ToolReceipt(
        status=ReceiptStatus.CLARIFICATION_REQUIRED,
        tool_name="confirm_report",
        changed=False,
        target_type="daily_report",
        target_id="report-test",
        error_code="REPORT_INCOMPLETE",
        safe_user_facts={
            "actual_write": False,
            "section_states": {
                "today_work": "filled",
                "problems": "missing",
                "tomorrow_plan": "filled",
            },
            "section_labels": {
                "today_work": "今日工作",
                "problems": "问题/风险",
                "tomorrow_plan": "明日计划",
            },
            "missing_sections": ["problems"],
            "missing_section_labels": ["问题/风险"],
            "confirmation_available": False,
            "persisted_draft_available": True,
        },
        execution_mode=ExecutionMode.CANARY_EXECUTE,
    )
    weekly_receipt = ToolReceipt(
        status=ReceiptStatus.SUCCESS,
        tool_name="apply_next_weekly_plan",
        changed=True,
        target_type="weekly_plan",
        target_id="weekly-test",
        safe_user_facts={"actual_write": True},
        execution_mode=ExecutionMode.CANARY_EXECUTE,
    )
    model_reply = json.dumps(
        {
            "reply": (
                "下周计划已经更新；日报尚未提交，目前只缺"
                "{{daily_missing_section_labels}}。"
            ),
            "actual_write": True,
            "operation_outcome": "partial",
            "daily_report_state": {
                "section_states": {
                    "today_work": "filled",
                    "problems": "missing",
                    "tomorrow_plan": "filled",
                },
                "missing_sections": ["problems"],
                "confirmation_available": False,
                "persisted_draft_available": True,
            },
        },
        ensure_ascii=False,
    )

    final_reply, _model_hash = finalize_canary_content(
        model_reply,
        (weekly_receipt, daily_receipt),
        write_batch_seen=True,
    )

    assert "下周计划已经更新" in final_reply
    assert "日报尚未提交" in final_reply
    assert "只缺问题/风险" in final_reply


def test_two_incomplete_daily_reports_keep_each_date_and_missing_section_separate() -> None:
    receipts = (
        ToolReceipt(
            status=ReceiptStatus.CLARIFICATION_REQUIRED,
            tool_name="confirm_report",
            changed=False,
            target_type="daily_report",
            target_id="report-2026-08-13",
            error_code="REPORT_INCOMPLETE",
            safe_user_facts={
                "actual_write": False,
                "report_date": "2026-08-13",
                "section_states": {
                    "today_work": "filled",
                    "problems": "missing",
                    "tomorrow_plan": "filled",
                },
                "section_labels": {
                    "today_work": "今日工作",
                    "problems": "问题/风险",
                    "tomorrow_plan": "明日计划",
                },
                "missing_sections": ["problems"],
                "missing_section_labels": ["问题/风险"],
                "confirmation_available": False,
                "persisted_draft_available": True,
            },
            execution_mode=ExecutionMode.CANARY_EXECUTE,
        ),
        ToolReceipt(
            status=ReceiptStatus.CLARIFICATION_REQUIRED,
            tool_name="confirm_report",
            changed=False,
            target_type="daily_report",
            target_id="report-2026-08-14",
            error_code="REPORT_INCOMPLETE",
            safe_user_facts={
                "actual_write": False,
                "report_date": "2026-08-14",
                "section_states": {
                    "today_work": "filled",
                    "problems": "filled",
                    "tomorrow_plan": "missing",
                },
                "section_labels": {
                    "today_work": "今日工作",
                    "problems": "问题/风险",
                    "tomorrow_plan": "明日计划",
                },
                "missing_sections": ["tomorrow_plan"],
                "missing_section_labels": ["明日计划"],
                "confirmation_available": False,
                "persisted_draft_available": True,
            },
            execution_mode=ExecutionMode.CANARY_EXECUTE,
        ),
    )
    swapped_model_reply = json.dumps(
        {
            "reply": (
                "8月13日只缺{{daily_missing_section_labels_2}}；"
                "8月14日只缺{{daily_missing_section_labels_1}}。"
            ),
            "actual_write": False,
            "operation_outcome": "needs_clarification",
            "daily_report_states": [
                {
                    "report_date": "2026-08-13",
                    "section_states": {
                        "today_work": "filled",
                        "problems": "missing",
                        "tomorrow_plan": "filled",
                    },
                    "missing_sections": ["problems"],
                    "missing_section_labels": ["问题/风险"],
                    "confirmation_available": False,
                    "persisted_draft_available": True,
                },
                {
                    "report_date": "2026-08-14",
                    "section_states": {
                        "today_work": "filled",
                        "problems": "filled",
                        "tomorrow_plan": "missing",
                    },
                    "missing_sections": ["tomorrow_plan"],
                    "missing_section_labels": ["明日计划"],
                    "confirmation_available": False,
                    "persisted_draft_available": True,
                },
            ],
        },
        ensure_ascii=False,
    )
    with pytest.raises(ValueError, match="write reply failed receipt validation"):
        finalize_canary_content(
            swapped_model_reply,
            receipts,
            write_batch_seen=True,
        )

    model_reply = json.dumps(
        {
            "reply": (
                "这两份日报尚未提交，分别缺"
                "{{daily_missing_report_summary}}。"
            ),
            "actual_write": False,
            "operation_outcome": "needs_clarification",
            "daily_report_states": [
                {
                    "report_date": "2026-08-13",
                    "section_states": {
                        "today_work": "filled",
                        "problems": "missing",
                        "tomorrow_plan": "filled",
                    },
                    "missing_sections": ["problems"],
                    "missing_section_labels": ["问题/风险"],
                    "confirmation_available": False,
                    "persisted_draft_available": True,
                },
                {
                    "report_date": "2026-08-14",
                    "section_states": {
                        "today_work": "filled",
                        "problems": "filled",
                        "tomorrow_plan": "missing",
                    },
                    "missing_sections": ["tomorrow_plan"],
                    "missing_section_labels": ["明日计划"],
                    "confirmation_available": False,
                    "persisted_draft_available": True,
                },
            ],
        },
        ensure_ascii=False,
    )

    final_reply, _model_hash = finalize_canary_content(
        model_reply,
        receipts,
        write_batch_seen=True,
    )

    assert final_reply == (
        "这两份日报尚未提交，分别缺"
        "2026-08-13：问题/风险；2026-08-14：明日计划。"
    )


def test_two_complete_daily_reports_do_not_request_a_missing_section_token() -> None:
    receipts = tuple(
        ToolReceipt(
            status=ReceiptStatus.SUCCESS,
            tool_name="confirm_report",
            changed=True,
            target_type="daily_report",
            target_id=f"report-{report_date}",
            safe_user_facts={
                "actual_write": True,
                "report_date": report_date,
                "section_states": {
                    "today_work": "filled",
                    "problems": "acknowledged_empty",
                    "tomorrow_plan": "filled",
                },
                "section_labels": {
                    "today_work": "今日工作",
                    "problems": "问题/风险",
                    "tomorrow_plan": "明日计划",
                },
                "missing_sections": [],
                "missing_section_labels": [],
                "confirmation_available": False,
                "persisted_draft_available": False,
            },
            execution_mode=ExecutionMode.CANARY_EXECUTE,
        )
        for report_date in ("2026-08-13", "2026-08-14")
    )

    protocol = write_reply_protocol(receipts)

    assert protocol["required_daily_reply_mode"] == (
        "completion_acknowledgement_only"
    )
    assert "daily_missing_report_summary" not in protocol
    assert all(
        "daily_missing_report_summary" not in rule
        for rule in protocol["rules"]
    )
    retry = json.loads(
        write_reply_retry_instruction(("actual_write mismatch",), receipts)
    )
    assert retry["write_reply_retry"]["required_daily_reply_mode"] == (
        "completion_acknowledgement_only"
    )
    assert (
        retry["write_reply_retry"]["daily_missing_label_requirements"]
        == []
    )


def test_completed_daily_mode_does_not_suppress_another_domain_clarification() -> None:
    daily_receipt = ToolReceipt(
        status=ReceiptStatus.SUCCESS,
        tool_name="confirm_report",
        changed=True,
        target_type="daily_report",
        target_id="daily-completed",
        safe_user_facts={
            "actual_write": True,
            "report_date": "2026-08-14",
            "section_states": {
                "today_work": "filled",
                "problems": "acknowledged_empty",
                "tomorrow_plan": "filled",
            },
            "section_labels": {
                "today_work": "今日工作",
                "problems": "问题/风险",
                "tomorrow_plan": "明日计划",
            },
            "missing_sections": [],
            "missing_section_labels": [],
            "confirmation_available": False,
            "persisted_draft_available": False,
        },
        execution_mode=ExecutionMode.CANARY_EXECUTE,
    )
    weekly_receipt = ToolReceipt(
        status=ReceiptStatus.CLARIFICATION_REQUIRED,
        tool_name="apply_next_weekly_plan",
        changed=False,
        target_type="weekly_plan",
        target_id="weekly-needs-date",
        safe_user_facts={
            "actual_write": False,
            "clarification_option_labels": ["周一", "周二"],
        },
        execution_mode=ExecutionMode.CANARY_EXECUTE,
    )

    protocol = write_reply_protocol((daily_receipt, weekly_receipt))

    assert protocol["required_daily_reply_mode"] == (
        "mixed_status_with_clarification"
    )

    weekly_success = ToolReceipt(
        status=ReceiptStatus.SUCCESS,
        tool_name="apply_next_weekly_plan",
        changed=True,
        target_type="weekly_plan",
        target_id="weekly-updated",
        safe_user_facts={"actual_write": True},
        execution_mode=ExecutionMode.CANARY_EXECUTE,
    )
    success_protocol = write_reply_protocol((daily_receipt, weekly_success))
    assert success_protocol["required_daily_reply_mode"] == "mixed_status_update"

    date_correction_receipt = ToolReceipt(
        status=ReceiptStatus.CLARIFICATION_REQUIRED,
        tool_name="correct_daily_report_date",
        changed=False,
        target_type="daily_report",
        target_id="daily-date-correction",
        safe_user_facts={
            "actual_write": False,
            "clarification_option_labels": ["8月13日", "8月14日"],
        },
        execution_mode=ExecutionMode.CANARY_EXECUTE,
    )
    correction_protocol = write_reply_protocol(
        (daily_receipt, date_correction_receipt)
    )
    assert correction_protocol["required_daily_reply_mode"] == (
        "mixed_status_with_clarification"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("reminder_kind", ["daily", "second"])
async def test_complete_collecting_report_gets_no_missing_field_reminder(
    monkeypatch,
    reminder_kind: str,
) -> None:
    class Team:
        name = "Team A"
        dingtalk_webhook_url = ""
        dingtalk_webhook_secret = None

        def __hash__(self) -> int:
            return id(self)

    user = SimpleNamespace(
        id=uuid5(NAMESPACE_URL, f"complete-reminder-user-{reminder_kind}"),
        name="Test User",
        dingtalk_user_id="test-user",
        team=Team(),
    )
    report = SimpleNamespace(
        status="collecting",
        today_work=[f"今日工作{i}" for i in range(1, 6)],
        problems=[],
        tomorrow_plan=[f"明日计划{i}" for i in range(1, 5)],
        section_status={"problems_acknowledged_empty": True},
    )

    async def fake_list_missing_users(_session, _report_date):
        return [user]

    async def fake_load_reports(_session, _report_date, user_ids):
        assert user_ids == [user.id]
        return {user.id: report}

    class Robot:
        def has_enterprise_app(self) -> bool:
            return True

    monkeypatch.setattr(jobs, "list_missing_users", fake_list_missing_users)
    monkeypatch.setattr(jobs, "_load_reports_by_user", fake_load_reports)

    result = await remind_missing_reports(
        SimpleNamespace(),
        SimpleNamespace(
            timezone="Asia/Shanghai",
            dingtalk_default_robot_webhook="",
            dingtalk_default_robot_secret="",
            reminder_send_enabled=False,
            reminder_dry_run=True,
            reminder_test_user_ids="test-user",
        ),
        Robot(),
        date(2026, 8, 13),
        reminder_kind=reminder_kind,
        dry_run=True,
    )

    assert result["target_users"] == 0
    assert result["missing_count"] == 0
    assert result["would_send"] == 0
    assert result["dry_run_messages"] == []


@pytest.mark.asyncio
async def test_the_47_candidate_shape_for_24_people_produces_zero_false_reminders(
    monkeypatch,
) -> None:
    class Team:
        name = "综合管理部"
        dingtalk_webhook_url = ""
        dingtalk_webhook_secret = None

        def __hash__(self) -> int:
            return id(self)

    team = Team()
    users = [
        SimpleNamespace(
            id=uuid5(NAMESPACE_URL, f"reminder-shape-user-{index}"),
            name=f"测试成员{index}",
            dingtalk_user_id=f"test-user-{index}",
            team=team,
        )
        for index in range(24)
    ]
    candidates_by_date = {
        date(2026, 8, 12): users,
        date(2026, 8, 13): users[:23],
    }

    async def fake_list_missing_users(_session, report_date):
        return candidates_by_date[report_date]

    async def fake_load_reports(_session, _report_date, user_ids):
        return {
            user.id: SimpleNamespace(
                status="collecting",
                today_work=["完成工作"],
                problems=[],
                tomorrow_plan=["继续跟进"],
                section_status={"problems_acknowledged_empty": True},
            )
            for user in users
            if user.id in user_ids
        }

    class Robot:
        def has_enterprise_app(self) -> bool:
            return True

    monkeypatch.setattr(jobs, "list_missing_users", fake_list_missing_users)
    monkeypatch.setattr(jobs, "_load_reports_by_user", fake_load_reports)
    settings = SimpleNamespace(
        timezone="Asia/Shanghai",
        dingtalk_default_robot_webhook="",
        dingtalk_default_robot_secret="",
        reminder_send_enabled=False,
        reminder_dry_run=True,
        reminder_test_user_ids=",".join(
            user.dingtalk_user_id for user in users
        ),
    )

    candidate_count = 0
    for report_date, candidate_users in candidates_by_date.items():
        candidate_count += len(candidate_users)
        result = await remind_missing_reports(
            SimpleNamespace(),
            settings,
            Robot(),
            report_date,
            dry_run=True,
        )
        assert result["missing_count"] == 0
        assert result["target_users"] == 0
        assert result["dry_run_messages"] == []

    assert candidate_count == 47
    assert len({user.id for user in users}) == 24


@pytest.mark.parametrize(
    ("report", "expected_label", "absent_labels"),
    [
        (
            SimpleNamespace(
                status="collecting",
                today_work=[],
                problems=[],
                tomorrow_plan=["继续跟进"],
                section_status={"problems_acknowledged_empty": True},
            ),
            "今日工作",
            ("问题/风险", "明日计划"),
        ),
        (
            SimpleNamespace(
                status="collecting",
                today_work=["完成审核"],
                problems=[],
                tomorrow_plan=["继续跟进"],
                section_status={},
            ),
            "问题/风险",
            ("今日工作", "明日计划"),
        ),
        (
            SimpleNamespace(
                status="collecting",
                today_work=["完成审核"],
                problems=[],
                tomorrow_plan=[],
                section_status={"problems_acknowledged_empty": True},
            ),
            "明日计划",
            ("今日工作", "问题/风险"),
        ),
    ],
)
def test_each_single_missing_section_is_named_exactly(
    report,
    expected_label: str,
    absent_labels: tuple[str, str],
) -> None:
    text = build_report_reminder_text(
        date(2026, 8, 13),
        SimpleNamespace(name="Test User"),
        report,
    )

    assert expected_label in text
    assert all(label not in text for label in absent_labels)
    assert "未完成部分" not in text


@pytest.mark.parametrize(
    "acknowledged_field",
    ["today_work", "problems", "tomorrow_plan"],
)
def test_each_model_acknowledged_empty_section_uses_the_same_completion_rule(
    acknowledged_field: str,
) -> None:
    values = {
        "today_work": ["完成审核"],
        "problems": ["发现风险"],
        "tomorrow_plan": ["继续跟进"],
    }
    values[acknowledged_field] = []

    assessment = assess_daily_report_completeness(
        **values,
        section_status={f"{acknowledged_field}_acknowledged_empty": True},
    )

    assert assessment.ready_for_confirmation is True
    assert assessment.missing_sections == ()
    assert assessment.completeness_score == 1.0
    assert assessment.draft_status == "pending_confirmation"
