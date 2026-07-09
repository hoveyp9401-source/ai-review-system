from datetime import datetime, timezone
import json

from app.agent_core import (
    AgentCoreMemory,
    DailySnapshot,
    MonthlyMetricState,
    MonthlySnapshot,
    TaskLedgerEntry,
    advance_agent_core_memory,
    process_agent_turn,
)
from app.agent_core.monthly_capability import MONTHLY_STATUS_PENDING_CONFIRMATION
from app.workflows.intake import IncomingMessageEnvelope, WORKFLOW_DAILY_REPORT, WORKFLOW_MONTHLY_REPORT


def _envelope(text: str, *, message_id: str = "msg-1") -> IncomingMessageEnvelope:
    return IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="tester",
        dingtalk_user_id="dt-1",
        source="unit_test",
        raw_text=text,
        message_id=message_id,
        conversation_id="conv-1",
    )


def test_turn_memory_carries_daily_task_and_snapshot_to_next_turn():
    memory = AgentCoreMemory(user_id="user-1")

    first = process_agent_turn(
        _envelope("\u4eca\u5929\u5b8c\u6210\u5408\u540c\u5ba1\u6838", message_id="msg-1"),
        daily_snapshot=memory.daily_snapshot,
        task_ledger=memory.task_ledger(),
    )
    memory = advance_agent_core_memory(memory, first)

    assert memory.daily_snapshot.today_work == ["\u4eca\u5929\u5b8c\u6210\u5408\u540c\u5ba1\u6838"]
    assert [entry.workflow for entry in memory.task_entries] == [WORKFLOW_DAILY_REPORT]
    assert memory.task_entries[0].task_id == "daily:user-1"
    assert memory.task_entries[0].awaited_reply == "daily_followup"

    second = process_agent_turn(
        _envelope("\u660e\u5929\u7ee7\u7eed\u8ddf\u8fdb\u5408\u540c", message_id="msg-2"),
        daily_snapshot=memory.daily_snapshot,
        task_ledger=memory.task_ledger(),
    )
    memory = advance_agent_core_memory(memory, second)

    assert second.task_context is not None
    assert second.task_context.selected_task_id == "daily:user-1"
    assert memory.daily_snapshot.today_work == ["\u4eca\u5929\u5b8c\u6210\u5408\u540c\u5ba1\u6838"]
    assert memory.daily_snapshot.tomorrow_plan == ["\u660e\u5929\u7ee7\u7eed\u8ddf\u8fdb\u5408\u540c"]
    assert memory.task_entries[0].awaited_reply == "daily_followup"


def test_turn_memory_removes_completed_daily_task_after_confirmation():
    memory = AgentCoreMemory(
        user_id="user-1",
        daily_snapshot=DailySnapshot(
            today_work=["\u5408\u540c\u5ba1\u6838"],
            problems=["\u6682\u65e0"],
            tomorrow_plan=["\u7ee7\u7eed\u8ddf\u8fdb"],
            status="pending_confirmation",
        ),
        task_entries=(
            TaskLedgerEntry(
                task_id="daily-confirm",
                user_id="user-1",
                workflow=WORKFLOW_DAILY_REPORT,
                status="pending_confirmation",
                awaited_reply="daily_confirmation",
            ),
        ),
    )

    result = process_agent_turn(
        _envelope("\u786e\u8ba4\u63d0\u4ea4", message_id="msg-3"),
        daily_snapshot=memory.daily_snapshot,
        task_ledger=memory.task_ledger(),
    )
    memory = advance_agent_core_memory(memory, result)

    assert result.task_context is not None
    assert result.task_context.selected_task_id == "daily-confirm"
    assert memory.daily_snapshot.status == "completed"
    assert memory.task_entries == ()


def test_turn_memory_keeps_monthly_confirmation_task_until_confirmed():
    monthly = MonthlySnapshot(
        task_id="monthly-1",
        department="\u6cd5\u52a1\u4e8c\u90e8",
        leader="\u5e9e\u6d69",
        period_label="2026-06",
        status=MONTHLY_STATUS_PENDING_CONFIRMATION,
        metrics=[
            MonthlyMetricState(
                metric_no=1,
                metric_name="\u8bc9\u8bbc\u6848\u4ef6\u6536\u6b3e",
                unit="\u4e07\u5143",
                reason="\u5df2\u5b8c\u6210",
                next_target="100",
                actions=["\u6309\u5468\u8ddf\u8fdb"],
            )
        ],
    )
    memory = AgentCoreMemory(user_id="user-1", monthly_snapshot=monthly)

    result = process_agent_turn(
        _envelope("\u9700\u8981\u628a\u539f\u56e0\u518d\u8c03\u6574\u4e00\u4e0b", message_id="msg-4"),
        monthly_snapshot=memory.monthly_snapshot,
        task_ledger=memory.task_ledger(),
    )
    memory = advance_agent_core_memory(memory, result)

    assert any(entry.workflow == WORKFLOW_MONTHLY_REPORT for entry in memory.task_entries)
    monthly_task = next(entry for entry in memory.task_entries if entry.workflow == WORKFLOW_MONTHLY_REPORT)
    assert monthly_task.task_id == "monthly-1"
    assert monthly_task.awaited_reply == "monthly_confirmation"
    assert monthly_task.artifacts["monthly_snapshot"]["department"] == "\u6cd5\u52a1\u4e8c\u90e8"


def test_turn_memory_persists_operation_records_without_raw_message_keys():
    memory = AgentCoreMemory(user_id="user-1")

    result = process_agent_turn(
        _envelope("\u660e\u5929\u53bb\u5357\u4eac\u51fa\u5dee", message_id="msg-5"),
        daily_snapshot=memory.daily_snapshot,
        task_ledger=memory.task_ledger(),
    )
    memory = advance_agent_core_memory(
        memory,
        result,
        created_at=datetime(2026, 7, 5, 1, 0, tzinfo=timezone.utc),
    )
    payload = json.dumps(memory.as_dict(), ensure_ascii=False)

    assert memory.operation_records
    assert "\u660e\u5929\u53bb\u5357\u4eac\u51fa\u5dee" in payload
    assert "raw_text" not in payload
    assert "sender_name" not in payload
