import json

from app.agent2.daily_shadow import evaluate_daily_shadow
from app.agent2.harness.runner import run_case
from app.agent2.harness.schemas import HarnessCase
from app.workflows.action_intake import ACTION_DAILY_WRITE, ACTION_INTERNAL_QA, ACTION_SMALL_TALK
from app.workflows.intake import ActiveWorkflowTask, IncomingMessageEnvelope, WORKFLOW_DAILY_REPORT


def _envelope(raw_text: str, *, active_daily: bool = False) -> IncomingMessageEnvelope:
    tasks = ()
    if active_daily:
        tasks = (
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-1",
                status="collecting",
                reply_candidate=True,
            ),
        )
    return IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="Test User",
        dingtalk_user_id="ding-user-1",
        source="test",
        raw_text=raw_text,
        active_tasks=tasks,
    )


def test_cognitive_decision_observes_daily_write_without_raw_text():
    text = "\u4eca\u5929\u5b8c\u6210\u5408\u540c\u5ba1\u6838"
    evaluation = evaluate_daily_shadow(_envelope(text), mode="protective_gate")

    decision = evaluation.cognitive_decision.as_dict()

    assert decision["primary_workflow"] == "daily_report"
    assert decision["allow_write"] is True
    assert [(action["action_type"], action["target_field"], action["write_policy"]) for action in decision["actions"]] == [
        (ACTION_DAILY_WRITE, "today_work", "write")
    ]
    assert [(command["operation"], command["target_field"], command["should_write"]) for command in decision["daily_commands"]] == [
        ("fill", "today_work", True)
    ]
    assert decision["effects"][0]["target"]["operation"] == "fill"
    assert decision["effects"][0]["target"]["action_type"] == ACTION_DAILY_WRITE
    assert "daily_effect_missing_operation_contract" not in decision["warnings"]
    assert "daily_effect_missing_action_type_contract" not in decision["warnings"]
    assert text not in json.dumps(decision, ensure_ascii=False)


def test_cognitive_decision_keeps_internal_qa_read_only_with_active_daily_context():
    text = "\u738b\u559c\u88ab\u544a\u6848\u4ef6\u6709\u591a\u5c11"
    evaluation = evaluate_daily_shadow(_envelope(text, active_daily=True), mode="protective_gate")

    decision = evaluation.cognitive_decision.as_dict()

    assert decision["primary_workflow"] == "internal_qa"
    assert decision["allow_write"] is False
    assert [action["action_type"] for action in decision["actions"]] == [ACTION_INTERNAL_QA]
    assert decision["daily_commands"] == []


def test_cognitive_decision_splits_daily_and_internal_qa_actions():
    text = (
        "\u4eca\u5929\u5b8c\u6210\u5408\u540c\u5ba1\u6838\u3002"
        "\u987a\u4fbf\u95ee\u4e0b\u5370\u7ae0\u6d41\u7a0b\u662f\u4ec0\u4e48\uff1f"
    )
    evaluation = evaluate_daily_shadow(_envelope(text, active_daily=True), mode="protective_gate")

    decision = evaluation.cognitive_decision.as_dict()

    assert decision["allow_write"] is True
    assert [action["action_type"] for action in decision["actions"]] == [
        ACTION_DAILY_WRITE,
        ACTION_INTERNAL_QA,
    ]
    assert [command["operation"] for command in decision["daily_commands"]] == ["fill"]


def test_cognitive_decision_marks_small_talk_no_write():
    text = "\u8ba9\u6211\u6d4b\u8bd5\u4e0b"
    evaluation = evaluate_daily_shadow(_envelope(text, active_daily=True), mode="protective_gate")

    decision = evaluation.cognitive_decision.as_dict()

    assert decision["primary_workflow"] == "chat"
    assert decision["allow_write"] is False
    assert [action["action_type"] for action in decision["actions"]] == [ACTION_SMALL_TALK]
    assert decision["daily_commands"] == []
    assert decision["action_context"]["work_object"] == "chat"
    assert decision["action_context"]["write_intent"] == "no_write"


def test_cognitive_decision_exposes_action_context_for_case_data_question():
    text = "\u6cd5\u52a1\u4e8c\u90e8\u76ee\u524d\u88ab\u544a\u5b58\u91cf\u591a\u5c11"
    evaluation = evaluate_daily_shadow(_envelope(text, active_daily=True), mode="protective_gate")

    decision = evaluation.cognitive_decision.as_dict()

    assert decision["action_context"]["work_object"] == "case_data"
    assert decision["action_context"]["operation"] == "ask"
    assert decision["action_context"]["write_intent"] == "read_only"


def test_harness_can_assert_cognitive_contract():
    case = HarnessCase.from_mapping(
        {
            "case_id": "cognitive-daily-write",
            "text": "\u4eca\u5929\u5b8c\u6210\u5408\u540c\u5ba1\u6838",
            "expected": {
                "primary_workflow": "daily_report",
                "expected_user_actions": [ACTION_DAILY_WRITE],
                "cognitive_allow_write": True,
                "expected_commands": ["fill"],
                "target_field": "today_work",
            },
        }
    )

    result = run_case(case)

    assert result.passed, result.failures
    assert result.actual is not None
    assert result.actual.cognitive_decision["allow_write"] is True
