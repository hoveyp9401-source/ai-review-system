from datetime import date

from app.agent.action_plan import ActionPlan, AgentAction, PendingInteractionPlan
from app.workflows.daily_intent import daily_intent_from_action_plan


def test_fill_plan_maps_to_daily_intent_frame():
    frame = daily_intent_from_action_plan(
        ActionPlan(
            intent="fill_report",
            confidence="high",
            should_write=True,
            actions=[AgentAction(type="append_items", field="today_work", items=["审核合同"])],
            reason="new daily work",
        ),
        target_date=date(2026, 6, 30),
        raw_text="今日审核合同",
        source="direct_rule",
        branch="direct_simple_full_report",
    )

    assert frame.workflow == "daily_report"
    assert frame.operation == "fill"
    assert frame.target_date == "2026-06-30"
    assert frame.target_field == "today_work"
    assert frame.content == ["审核合同"]
    assert frame.should_write is True
    assert frame.raw_text_hash
    assert "raw_text" not in frame.safe_timing_payload()


def test_multi_field_fill_marks_all_and_multiple_target_fields():
    frame = daily_intent_from_action_plan(
        ActionPlan(
            intent="fill_report",
            confidence="high",
            should_write=True,
            actions=[
                AgentAction(type="replace_field", field="today_work", items=["审核合同"]),
                AgentAction(type="replace_field", field="problems", items=["暂无明显问题"]),
                AgentAction(type="replace_field", field="tomorrow_plan", items=["继续跟进"]),
            ],
        ),
        target_date="2026-06-30",
    )

    assert frame.operation == "fill"
    assert frame.target_field == "all"
    assert "multiple_target_fields" in frame.safety_flags


def test_confirm_submit_maps_to_confirm_operation():
    frame = daily_intent_from_action_plan(
        ActionPlan(
            intent="confirm_submit",
            confidence="high",
            should_write=True,
            actions=[AgentAction(type="submit_report")],
        ),
        target_date="2026-06-30",
    )

    assert frame.operation == "confirm"
    assert frame.target_field == "all"
    assert frame.should_write is True


def test_query_history_keeps_target_date_from_action():
    frame = daily_intent_from_action_plan(
        ActionPlan(
            intent="query_history",
            confidence="high",
            should_write=False,
            actions=[AgentAction(type="query_history", target_date="2026-06-29")],
        ),
        target_date="2026-06-30",
    )

    assert frame.operation == "query_history"
    assert frame.target_date == "2026-06-29"
    assert frame.should_write is False


def test_daily_delete_write_without_confirmation_is_not_flagged():
    frame = daily_intent_from_action_plan(
        ActionPlan(
            intent="edit_draft",
            confidence="high",
            should_write=True,
            actions=[AgentAction(type="delete_item", field="today_work", item_indices=[2])],
        ),
        target_date="2026-06-30",
    )

    assert frame.operation == "edit"
    assert frame.target_field == "today_work"
    assert frame.target_items == [2]
    assert "destructive_without_confirmation" not in frame.safety_flags


def test_pending_confirmation_sets_confirmation_and_pending_flags():
    frame = daily_intent_from_action_plan(
        ActionPlan(
            intent="edit_draft",
            confidence="high",
            should_write=False,
            actions=[AgentAction(type="ask_clarification", field="today_work", item_indices=[1])],
            pending_interaction_to_set=PendingInteractionPlan(
                type="awaiting_action_confirmation",
                operation="delete_report_item",
                target_field="today_work",
            ),
        ),
        target_date="2026-06-30",
    )

    assert frame.operation == "no_write"
    assert frame.needs_confirmation is True
    assert frame.pending_relation == "sets_pending"
    assert "pending_transition" in frame.safety_flags
