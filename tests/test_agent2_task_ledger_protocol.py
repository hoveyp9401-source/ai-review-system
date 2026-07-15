from app.agent_core.task_ledger import (
    FormalTaskLedger,
    FormalTaskLedgerState,
    FormalTaskLedgerTask,
)


def test_followup_focus_suspends_weekly_report_and_successful_completion_restores_it():
    weekly = FormalTaskLedgerTask(
        task_id="weekly-1", tenant_id="tenant-a", user_id="user-1",
        conversation_id="conversation-1", domain="report", operation="append",
        object_ref={"report_type": "weekly", "period_key": "2026-W29"},
        status="active", focus_state="focused", version=2,
        resume_policy={"mode": "after_interrupt", "report_status": "collecting"},
    )
    followup = FormalTaskLedgerTask(
        task_id="followup-1", tenant_id="tenant-a", user_id="user-1",
        conversation_id="conversation-1", domain="case_followup", operation="answer",
        object_ref={"case_id": "case-1", "followup_id": "followup-1"},
        status="awaiting_input", focus_state="active", version=1,
        resume_policy={"mode": "restore_previous"},
    )
    state = FormalTaskLedgerState(
        focused_task=weekly, active_tasks=(weekly, followup), suspended_tasks=()
    )

    focused = FormalTaskLedger().focus_case_followup(
        state, followup_task_id="followup-1", receipt_succeeded=True
    )

    assert focused.status == "transitioned"
    assert focused.state.focused_task.task_id == "followup-1"
    assert [item.task_id for item in focused.state.suspended_tasks] == ["weekly-1"]

    completed = FormalTaskLedger().complete_case_followup(
        focused.state, followup_task_id="followup-1", receipt_succeeded=True
    )

    assert completed.status == "transitioned"
    assert completed.state.focused_task.task_id == "weekly-1"
    assert completed.state.focused_task.object_ref["period_key"] == "2026-W29"
