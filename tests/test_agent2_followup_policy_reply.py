from app.agent2.operation_outcomes import (
    OperationOutcome,
    OutcomeObjectRef,
    OutcomeReceiptRef,
    OutcomeReplyComposer,
    OutcomeStateTransition,
)


def test_policy_reply_states_new_cadence_next_time_and_event_switches():
    outcome = OperationOutcome(
        domain="followup", operation="update",
        object_ref=OutcomeObjectRef("case_followup_policy", "policy-1", "南京工程款案"),
        business_status="succeeded", message_status="not_applicable",
        changed_fields=("cadence_type", "next_due_at"),
        user_visible_snapshot={
            "case_name": "南京工程款案", "cadence_type": "weekly",
            "next_due_at": "2026-07-20T09:00:00+08:00",
            "hearing_reminders_enabled": True,
            "stage_transition_enabled": True,
            "node_transition_enabled": False,
        },
        blocking_reason="",
        receipt_refs=(OutcomeReceiptRef("receipt-policy", "database", "executed", True),),
        state_transition=OutcomeStateTransition("daily", "weekly"), actual_write=True,
    )

    reply = OutcomeReplyComposer().compose((outcome,))

    assert "南京工程款案" in reply
    assert "每周一次" in reply
    assert "2026-07-20T09:00:00+08:00" in reply
    assert "开庭提醒" in reply
    assert "阶段变化提醒" in reply
    assert "关键节点提醒" not in reply
