import json

from app.agent_core import DailySnapshot, process_agent_turn
from app.workflows.intake import IncomingMessageEnvelope, RoutingPlan, SafetyDecision


def _envelope(text: str, *, active_tasks=()) -> IncomingMessageEnvelope:
    return IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="tester",
        dingtalk_user_id="dt-1",
        source="unit_test",
        raw_text=text,
        message_id="msg-1",
        conversation_id="conv-1",
        active_tasks=tuple(active_tasks),
    )


def test_daily_turn_returns_dry_run_operation_with_before_after_snapshot():
    result = process_agent_turn(
        _envelope("\u4eca\u5929\u5b8c\u6210\u5408\u540c\u5ba1\u6838"),
        daily_snapshot=DailySnapshot(),
    )

    assert result.production_write is False
    assert result.execution_policy == "dry_run"
    assert result.routing.primary_workflow == "daily_report"
    assert [entry.operation for entry in result.operation_ledger] == ["fill"]
    assert result.operation_ledger[0].workflow == "daily_report"
    assert result.operation_ledger[0].write_policy == "dry_run"
    assert result.operation_ledger[0].before_state["today_work"] == []
    assert result.operation_ledger[0].after_state["today_work"] == [
        "\u4eca\u5929\u5b8c\u6210\u5408\u540c\u5ba1\u6838"
    ]
    assert result.daily_after.today_work == ["\u4eca\u5929\u5b8c\u6210\u5408\u540c\u5ba1\u6838"]
    assert result.daily_capability is not None
    assert result.daily_capability.after_items[0].global_index == 1
    assert result.daily_capability.after_items[0].field == "today_work"


def test_non_daily_question_cannot_create_daily_write_from_downstream_extraction():
    result = process_agent_turn(
        _envelope("\u88ab\u544a\u7f3a\u5e2d \u539f\u544a\u7f3a\u5e2d\u6709\u4ec0\u4e48\u4e0d\u4e00\u6837\u7684\u540e\u679c"),
        daily_snapshot=DailySnapshot(today_work=["\u5df2\u6709\u5de5\u4f5c"]),
    )

    assert result.production_write is False
    assert result.daily_commands == []
    assert result.daily_after.today_work == ["\u5df2\u6709\u5de5\u4f5c"]
    assert all(entry.workflow != "daily_report" for entry in result.operation_ledger)


def test_tomorrow_travel_turn_keeps_daily_write_and_sandbox_sidecar_separate():
    result = process_agent_turn(
        _envelope("\u660e\u5929\u53bb\u5357\u4eac\u51fa\u5dee"),
        daily_snapshot=DailySnapshot(),
    )

    daily_entries = [entry for entry in result.operation_ledger if entry.workflow == "daily_report"]
    sandbox_entries = [entry for entry in result.operation_ledger if entry.write_policy == "sandbox"]

    assert daily_entries
    assert daily_entries[0].operation == "fill"
    assert daily_entries[0].after_state["tomorrow_plan"] == ["\u660e\u5929\u53bb\u5357\u4eac\u51fa\u5dee"]
    assert any(entry.workflow == "travel_coordination" for entry in sandbox_entries)
    assert result.daily_after.tomorrow_plan == ["\u660e\u5929\u53bb\u5357\u4eac\u51fa\u5dee"]


def test_result_is_json_serializable_for_harness_reports():
    result = process_agent_turn(
        _envelope("\u4eca\u5929\u5b8c\u6210\u5408\u540c\u5ba1\u6838"),
        daily_snapshot=DailySnapshot(),
    )

    payload = result.as_dict()

    assert payload["turn_id"]
    assert payload["production_write"] is False
    assert payload["operation_ledger"][0]["after_state"]["today_work"]
    json.dumps(payload, ensure_ascii=False)


def test_generic_case_work_is_daily_not_case_progress_candidate():
    result = process_agent_turn(
        _envelope("\u9879\u76ee\u8bc4\u5ba1\u3001\u6848\u4ef6\u6c9f\u901a"),
        daily_snapshot=DailySnapshot(),
    )

    action_types = [action.action_type for action in result.coordination.actions]

    assert result.routing.primary_workflow == "daily_report"
    assert "daily_entry" in action_types
    assert "case_progress_entry" not in action_types
    assert result.operation_ledger[0].workflow == "daily_report"


def test_specific_case_work_is_daily_with_case_progress_sandbox_candidate():
    result = process_agent_turn(
        _envelope("\u6052\u5927\u7834\u4ea7\u6848\u6c9f\u901a\uff0c\u8865\u5145\u8bc9\u8bbc\u6750\u6599"),
        daily_snapshot=DailySnapshot(),
    )

    action_types = [action.action_type for action in result.coordination.actions]
    sandbox_entries = [entry for entry in result.operation_ledger if entry.write_policy == "sandbox"]

    assert "daily_report" in result.routing.matched_workflows
    assert "case_progress" in result.routing.matched_workflows
    assert "daily_entry" in action_types
    assert "case_progress_entry" in action_types
    assert any(entry.workflow == "case_progress" for entry in sandbox_entries)


def test_structured_weekly_report_does_not_enter_daily():
    result = process_agent_turn(
        _envelope(
            "\u672c\u5468\u5b8c\u6210\uff1a\u5408\u540c\u6a21\u677f\u4fee\u8ba2\u3001\u6848\u4ef6\u8d44\u6599\u68b3\u7406\n"
            "\u4e0b\u5468\u8ba1\u5212\uff1a\u63a8\u8fdb\u8bc9\u8bbc\u6750\u6599\u5f52\u6863"
        ),
        daily_snapshot=DailySnapshot(),
    )

    assert result.routing.primary_workflow == "weekly_report"
    assert result.daily_commands == []
    assert all(entry.workflow != "daily_report" for entry in result.operation_ledger)


def test_current_trip_is_daily_with_travel_sandbox_candidate():
    result = process_agent_turn(
        _envelope(
            "\u4eca\u65e5\u53c2\u52a0\u8fc7\u5802\u4f1a\u3001\u9879\u76ee\u5316\u503a\u6c9f\u901a\u3001\u5e38\u5dde\u51fa\u5dee"
        ),
        daily_snapshot=DailySnapshot(),
    )

    action_types = [action.action_type for action in result.coordination.actions]

    assert "daily_entry" in action_types
    assert "travel_event" in action_types
    assert any(entry.workflow == "travel_coordination" for entry in result.operation_ledger)


class _NoEffectDailyRouter:
    def plan(self, envelope: IncomingMessageEnvelope) -> RoutingPlan:
        return RoutingPlan(
            primary_workflow="daily_report",
            matched_workflows=["daily_report"],
            effects=[],
            safety_decision=SafetyDecision(
                commit_policy="partial_allowed",
                reason="test route allows workflow but grants no effects",
            ),
            confidence=0.9,
            reason="test route without authorized effects",
        )


def test_downstream_coordination_daily_action_cannot_write_without_routing_effect():
    result = process_agent_turn(
        _envelope("\u4eca\u5929\u5b8c\u6210\u5408\u540c\u5ba1\u6838"),
        daily_snapshot=DailySnapshot(),
        router=_NoEffectDailyRouter(),
    )

    assert result.coordination.actions
    assert result.daily_commands
    assert result.authorization_policy is not None
    assert result.authorization_policy.authorized_actions == []
    assert result.daily_after.today_work == []
    assert result.operation_ledger[0].authorization_status == "denied"
    assert "operation_not_authorized" in result.operation_ledger[0].safety_flags
