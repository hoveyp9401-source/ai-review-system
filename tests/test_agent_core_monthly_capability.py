from app.agent_core import DailySnapshot, InMemoryTaskLedger, TaskLedgerEntry, process_agent_turn
from app.agent_core.execution_policy import AuthorizedAction, ExecutionPolicy
from app.agent_core.monthly_capability import (
    MonthlyCommand,
    MonthlyMetricState,
    MonthlySnapshot,
    run_monthly_capability,
)
from app.workflows.intake import IncomingMessageEnvelope, WORKFLOW_INTERNAL_QA, WORKFLOW_MONTHLY_REPORT


def _metrics() -> list[MonthlyMetricState]:
    return [
        MonthlyMetricState(metric_no=1, metric_name="诉讼案件收款（现金）", unit="万元"),
        MonthlyMetricState(metric_no=2, metric_name="优先权与时效管理", unit="%"),
    ]


def _snapshot(*, status: str = "collecting") -> MonthlySnapshot:
    return MonthlySnapshot(task_id="monthly-1", department="法务二部", leader="庞浩", period_label="2026年6月", status=status, metrics=_metrics())


def _policy(*, operation: str = "capture_reply") -> ExecutionPolicy:
    return ExecutionPolicy(
        turn_id="turn-monthly",
        plan_id="plan-monthly",
        authorized_actions=[
            AuthorizedAction(
                authorization_id=f"auth-{operation}",
                plan_id="plan-monthly",
                turn_id="turn-monthly",
                workflow=WORKFLOW_MONTHLY_REPORT,
                capability=WORKFLOW_MONTHLY_REPORT,
                operation=operation,
                write_policy="dry_run",
                task_id="monthly-1",
                reason="test monthly grant",
            )
        ],
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


def test_monthly_capability_rejects_capture_without_authorization():
    result = run_monthly_capability(
        turn_id="turn-monthly",
        snapshot=_snapshot(),
        commands=[
            MonthlyCommand(
                operation="capture_reply",
                raw_input="1. 诉讼案件收款（现金）\n未完成原因/存在问题：客户回款慢\n下月目标（万元）：100\n行动方案：每周跟进",
                task_id="monthly-1",
            )
        ],
    )

    assert result.after.metrics[0].reason == ""
    assert result.changed is False
    assert result.read_only is True
    assert result.operation_ledger[0].authorization_status == "denied"
    assert "authorization_denied" in result.operation_ledger[0].safety_flags


def test_monthly_capability_parses_batch_metric_reply():
    result = run_monthly_capability(
        turn_id="turn-monthly",
        snapshot=_snapshot(),
        execution_policy=_policy(),
        commands=[
            MonthlyCommand(
                operation="capture_reply",
                task_id="monthly-1",
                raw_input=(
                    "【请回复】本次需要填写的指标：\n"
                    "1. 诉讼案件收款（现金）\n"
                    "未完成原因/存在问题：客户回款审批慢\n"
                    "下月目标（万元）：1000\n"
                    "行动方案：1. 锁定重点客户 2. 每周跟进审批节点\n\n"
                    "2. 优先权与时效管理\n"
                    "未完成原因/存在问题：部分案件资料不完整\n"
                    "下月目标（%）：95%\n"
                    "行动方案：建立清单；每周复盘"
                ),
            )
        ],
    )

    assert result.changed is True
    assert result.after.status == "pending_confirmation"
    assert result.touched_metrics == [1, 2]
    assert result.missing == {}
    assert result.after.metrics[0].reason == "客户回款审批慢"
    assert result.after.metrics[0].next_target == "1000"
    assert result.after.metrics[0].actions == ["锁定重点客户", "每周跟进审批节点"]
    assert result.after.metrics[1].actions == ["建立清单", "每周复盘"]
    assert "已收齐" in result.message


def test_monthly_capability_merges_incremental_replies_by_metric_number():
    first = run_monthly_capability(
        turn_id="turn-monthly",
        snapshot=_snapshot(),
        execution_policy=_policy(),
        commands=[
            MonthlyCommand(
                operation="capture_reply",
                task_id="monthly-1",
                raw_input="1 未完成原因：客户回款慢。下月目标：1000万元。行动方案：1. 锁定客户",
            )
        ],
    )
    second = run_monthly_capability(
        turn_id="turn-monthly",
        snapshot=first.after,
        execution_policy=_policy(),
        commands=[
            MonthlyCommand(
                operation="capture_reply",
                task_id="monthly-1",
                raw_input="2 未完成原因：资料不完整。下月目标：95%。行动方案：1. 建立清单 2. 每周复盘",
            )
        ],
    )

    assert first.after.status == "collecting"
    assert first.touched_metrics == [1]
    assert 2 in first.missing
    assert second.after.status == "pending_confirmation"
    assert second.after.metrics[0].reason == "客户回款慢"
    assert second.after.metrics[1].next_target == "95%"


def test_monthly_capability_updates_one_metric_field_after_preview():
    filled = MonthlySnapshot(
        task_id="monthly-1",
        status="pending_confirmation",
        metrics=[
            MonthlyMetricState(1, "诉讼案件收款（现金）", "万元", "A", "100", ["行动A"]),
            MonthlyMetricState(2, "优先权与时效管理", "%", "B", "90%", ["行动B"]),
        ],
    )

    result = run_monthly_capability(
        turn_id="turn-monthly",
        snapshot=filled,
        execution_policy=_policy(),
        commands=[
            MonthlyCommand(
                operation="capture_reply",
                task_id="monthly-1",
                raw_input="把第2项行动方案改成：建立时效台账，每周预警",
            )
        ],
    )

    assert result.touched_metrics == [2]
    assert result.after.metrics[0].actions == ["行动A"]
    assert result.after.metrics[1].actions == ["建立时效台账", "每周预警"]


def test_process_agent_turn_routes_monthly_reply_into_monthly_capability_only():
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
        _envelope("1. 诉讼案件收款（现金）\n未完成原因/存在问题：客户回款慢\n下月目标（万元）：1000\n行动方案：每周跟进"),
        daily_snapshot=DailySnapshot(),
        monthly_snapshot=_snapshot(),
        task_ledger=ledger,
    )

    assert result.routing.primary_workflow == WORKFLOW_MONTHLY_REPORT
    assert result.daily_commands == []
    assert result.daily_after.today_work == []
    assert result.monthly_capability is not None
    assert result.monthly_after.metrics[0].reason == "客户回款慢"
    assert [entry.workflow for entry in result.operation_ledger] == [WORKFLOW_MONTHLY_REPORT]
    assert result.operation_ledger[0].authorization_status == "allowed"


def test_process_agent_turn_confirms_completed_monthly_submission():
    ledger = InMemoryTaskLedger(
        [
            TaskLedgerEntry(
                task_id="monthly-1",
                user_id="user-1",
                workflow=WORKFLOW_MONTHLY_REPORT,
                status="pending_confirmation",
                awaited_reply="monthly_confirmation",
            )
        ]
    )
    filled = MonthlySnapshot(
        task_id="monthly-1",
        status="pending_confirmation",
        metrics=[
            MonthlyMetricState(1, "诉讼案件收款（现金）", "万元", "A", "100", ["行动A"]),
            MonthlyMetricState(2, "优先权与时效管理", "%", "B", "90%", ["行动B"]),
        ],
    )

    result = process_agent_turn(
        _envelope("确认提交"),
        daily_snapshot=DailySnapshot(),
        monthly_snapshot=filled,
        task_ledger=ledger,
    )

    assert result.routing.primary_workflow == WORKFLOW_MONTHLY_REPORT
    assert result.monthly_after.status == "completed"
    assert result.monthly_capability is not None
    assert result.monthly_capability.confirmed_by_user is True


def test_monthly_active_task_does_not_steal_legal_question():
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
        _envelope("被告缺席和原告缺席有什么区别？"),
        daily_snapshot=DailySnapshot(),
        monthly_snapshot=_snapshot(),
        task_ledger=ledger,
    )

    assert result.routing.primary_workflow == WORKFLOW_INTERNAL_QA
    assert result.monthly_capability is None
    assert result.monthly_after == _snapshot()
