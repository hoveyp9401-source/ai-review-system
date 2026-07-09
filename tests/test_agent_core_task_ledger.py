from app.agent_core import DailySnapshot, process_agent_turn
from app.agent_core.task_ledger import InMemoryTaskLedger, TaskLedgerEntry
from app.workflows.intake import (
    EFFECT_CONFIRM_DAILY_REPORT,
    WORKFLOW_DAILY_REPORT,
    WORKFLOW_INTERNAL_QA,
    WORKFLOW_MONTHLY_REPORT,
    IncomingMessageEnvelope,
)


def _envelope(text: str) -> IncomingMessageEnvelope:
    return IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="tester",
        dingtalk_user_id="dt-1",
        source="unit_test",
        raw_text=text,
        message_id="msg-1",
        conversation_id="conv-1",
    )


def test_task_ledger_routes_monthly_metric_reply_to_monthly_report():
    ledger = InMemoryTaskLedger(
        [
            TaskLedgerEntry(
                task_id="monthly-1",
                user_id="user-1",
                workflow=WORKFLOW_MONTHLY_REPORT,
                status="collecting",
                awaited_reply="monthly_metric_reply",
            )
        ]
    )

    result = process_agent_turn(
        _envelope(
            "1. \u8bc9\u8bbc\u6848\u4ef6\u6536\u6b3e\uff08\u73b0\u91d1\uff09\n"
            "\u672a\u5b8c\u6210\u539f\u56e0/\u5b58\u5728\u95ee\u9898\uff1a\u5ba2\u6237\u56de\u6b3e\u6162\n"
            "\u4e0b\u6708\u76ee\u6807\uff08\u4e07\u5143\uff09\uff1a100\n"
            "\u884c\u52a8\u65b9\u6848\uff1a\u6bcf\u5468\u8ddf\u8fdb"
        ),
        daily_snapshot=DailySnapshot(),
        task_ledger=ledger,
    )

    assert result.task_context is not None
    assert result.task_context.selected_task_id == "monthly-1"
    assert result.routing.primary_workflow == WORKFLOW_MONTHLY_REPORT
    assert result.daily_commands == []
    assert result.daily_after.today_work == []
    assert result.daily_after.problems == []
    assert result.daily_after.tomorrow_plan == []


def test_task_ledger_routes_concurrent_daily_travel_plan_without_monthly_hijack():
    ledger = InMemoryTaskLedger(
        [
            TaskLedgerEntry(
                task_id="monthly-1",
                user_id="user-1",
                workflow=WORKFLOW_MONTHLY_REPORT,
                status="collecting",
                awaited_reply="monthly_metric_reply",
            ),
            TaskLedgerEntry(
                task_id="daily-1",
                user_id="user-1",
                workflow=WORKFLOW_DAILY_REPORT,
                status="collecting",
                awaited_reply="daily_tomorrow_plan",
            ),
        ]
    )

    result = process_agent_turn(
        _envelope("\u660e\u5929\u53bb\u5357\u4eac\u51fa\u5dee"),
        daily_snapshot=DailySnapshot(),
        task_ledger=ledger,
    )

    assert result.task_context is not None
    assert result.task_context.selected_task_id == "daily-1"
    assert WORKFLOW_MONTHLY_REPORT not in result.routing.matched_workflows
    assert result.daily_after.tomorrow_plan == ["\u660e\u5929\u53bb\u5357\u4eac\u51fa\u5dee"]


def test_task_ledger_routes_daily_confirmation_to_daily_task():
    ledger = InMemoryTaskLedger(
        [
            TaskLedgerEntry(
                task_id="daily-confirm",
                user_id="user-1",
                workflow=WORKFLOW_DAILY_REPORT,
                status="pending_confirmation",
                awaited_reply="daily_confirmation",
            )
        ]
    )

    result = process_agent_turn(
        _envelope("\u786e\u8ba4\u63d0\u4ea4"),
        daily_snapshot=DailySnapshot(today_work=["\u5408\u540c\u5ba1\u6838"]),
        task_ledger=ledger,
    )

    assert result.task_context is not None
    assert result.task_context.selected_task_id == "daily-confirm"
    assert result.routing.primary_workflow == WORKFLOW_DAILY_REPORT
    assert [effect.effect_type for effect in result.routing.effects] == [EFFECT_CONFIRM_DAILY_REPORT]


def test_task_ledger_keeps_daily_edit_in_active_daily_task():
    ledger = InMemoryTaskLedger(
        [
            TaskLedgerEntry(
                task_id="daily-edit",
                user_id="user-1",
                workflow=WORKFLOW_DAILY_REPORT,
                status="collecting",
                awaited_reply="daily_followup",
            )
        ]
    )

    result = process_agent_turn(
        _envelope("\u628a\u7b2c1\u6761\u6539\u6210\u5408\u540c\u5ba1\u6838\u5df2\u5b8c\u6210"),
        daily_snapshot=DailySnapshot(today_work=["\u5408\u540c\u5ba1\u6838"]),
        task_ledger=ledger,
    )

    assert result.task_context is not None
    assert result.task_context.selected_task_id == "daily-edit"
    assert result.routing.primary_workflow == WORKFLOW_DAILY_REPORT
    assert [command.operation for command in result.daily_commands] == ["edit"]
    assert result.daily_after.today_work == ["\u5408\u540c\u5ba1\u6838\u5df2\u5b8c\u6210"]


def test_task_ledger_does_not_steal_legal_question_for_monthly_reply_task():
    ledger = InMemoryTaskLedger(
        [
            TaskLedgerEntry(
                task_id="monthly-1",
                user_id="user-1",
                workflow=WORKFLOW_MONTHLY_REPORT,
                status="collecting",
                awaited_reply="monthly_metric_reply",
            )
        ]
    )

    result = process_agent_turn(
        _envelope(
            "\u88ab\u544a\u7f3a\u5e2d \u539f\u544a\u7f3a\u5e2d\u6709\u4ec0\u4e48\u4e0d\u4e00\u6837\u7684\u540e\u679c"
        ),
        daily_snapshot=DailySnapshot(),
        task_ledger=ledger,
    )

    assert result.task_context is not None
    assert result.task_context.selected_task_id == ""
    assert result.routing.primary_workflow == WORKFLOW_INTERNAL_QA
    assert result.daily_commands == []
