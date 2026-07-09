from app.agent2.daily_shadow import evaluate_daily_shadow
from app.workflows.intake import (
    ActiveWorkflowTask,
    IncomingMessageEnvelope,
    WORKFLOW_CHAT,
    WORKFLOW_DAILY_REPORT,
    WORKFLOW_MONTHLY_REPORT,
)


def _envelope(raw_text: str, *tasks: ActiveWorkflowTask) -> IncomingMessageEnvelope:
    return IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="Test User",
        dingtalk_user_id="ding-user-1",
        source="test",
        raw_text=raw_text,
        message_id="msg-1",
        conversation_id="conv-1",
        active_tasks=tuple(tasks),
    )


def test_assistant_reply_handles_small_talk_without_daily_write():
    evaluation = evaluate_daily_shadow(
        _envelope(
            "\u65e9\u4e0a\u597d\uff0c\u5496\u5561\u592a\u82e6\u4e86",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-chat",
                status="collecting",
                reply_candidate=True,
            ),
        ),
        mode="protective_gate",
    )

    assert evaluation.gate_decision.block_legacy_daily is True
    assert evaluation.commands == []
    assert evaluation.assistant_reply is not None
    assert evaluation.plan.primary_workflow == WORKFLOW_CHAT
    assert evaluation.assistant_reply.reply_type == "chat"
    assert "\u4e0d\u5199\u5165\u65e5\u62a5" in evaluation.assistant_reply.text


def test_assistant_reply_handles_short_lifestyle_chat_without_daily_write():
    evaluation = evaluate_daily_shadow(
        _envelope(
            "\u4eca\u5929\u6211\u5403\u4e86\u5c0f\u756a\u8304",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-food-chat",
                status="collecting",
                reply_candidate=True,
            ),
        ),
        mode="protective_gate",
    )

    assert evaluation.gate_decision.block_legacy_daily is True
    assert evaluation.commands == []
    assert evaluation.plan.primary_workflow == WORKFLOW_CHAT
    assert evaluation.assistant_reply is not None
    assert evaluation.assistant_reply.reply_type == "chat"
    assert "\u4e0d\u5199\u5165\u65e5\u62a5" in evaluation.assistant_reply.text


def test_assistant_reply_handles_short_chatter_without_generic_clarification():
    for text in ["\u54ce", "\u54c8\u54e6", "\u660e\u5929\u5403\u5c4e"]:
        evaluation = evaluate_daily_shadow(
            _envelope(
                text,
                ActiveWorkflowTask(
                    workflow=WORKFLOW_DAILY_REPORT,
                    task_id="daily-short-chat",
                    status="collecting",
                    reply_candidate=True,
                ),
            ),
            mode="protective_gate",
        )

        assert evaluation.gate_decision.block_legacy_daily is True
        assert evaluation.commands == []
        assert evaluation.plan.primary_workflow == WORKFLOW_CHAT
        assert evaluation.assistant_reply is not None
        assert evaluation.assistant_reply.reply_type == "chat"
        assert "\u4e0d\u5199\u5165\u65e5\u62a5" in evaluation.assistant_reply.text
        assert "\u8bf7\u8bf4\u660e\u4f60\u8981\u8bb0\u5f55\u5230\u65e5\u62a5" not in evaluation.assistant_reply.text
        assert "\u6700\u63a5\u8fd1" not in evaluation.assistant_reply.text


def test_assistant_reply_handles_daily_meta_status_without_daily_write():
    evaluation = evaluate_daily_shadow(
        _envelope(
            "\u5199\u65e5\u62a5\u4e86",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-meta-status",
                status="collecting",
                reply_candidate=True,
            ),
        ),
        mode="protective_gate",
    )

    assert evaluation.gate_decision.block_legacy_daily is True
    assert evaluation.commands == []
    assert evaluation.plan.primary_workflow == WORKFLOW_CHAT
    assert evaluation.assistant_reply is not None
    assert evaluation.assistant_reply.reply_type == "chat"
    assert "\u4e0d\u5199\u5165\u65e5\u62a5" in evaluation.assistant_reply.text
    assert "\u6700\u63a5\u8fd1" not in evaluation.assistant_reply.text


def test_assistant_reply_handles_bot_feedback_as_chat_without_daily_write():
    evaluation = evaluate_daily_shadow(
        _envelope(
            "\u8fd9\u4e2a\u673a\u5668\u4eba\u6709\u70b9\u50bb",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-bot-feedback",
                status="collecting",
                reply_candidate=True,
            ),
        ),
        mode="protective_gate",
    )

    assert evaluation.gate_decision.block_legacy_daily is True
    assert evaluation.commands == []
    assert evaluation.plan.primary_workflow == WORKFLOW_CHAT
    assert evaluation.assistant_reply is not None
    assert evaluation.assistant_reply.reply_type == "chat"
    assert "\u4e0d\u5199\u5165\u65e5\u62a5" in evaluation.assistant_reply.text


def test_assistant_reply_handles_monthly_status_query_without_daily_write():
    evaluation = evaluate_daily_shadow(
        _envelope(
            "\u5927\u5bb6\u6708\u62a5\u586b\u7684\u600e\u6837\u4e86",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-monthly-status",
                status="collecting",
                reply_candidate=True,
            ),
        ),
        mode="protective_gate",
    )

    assert evaluation.gate_decision.block_legacy_daily is True
    assert evaluation.commands == []
    assert evaluation.plan.primary_workflow == WORKFLOW_MONTHLY_REPORT
    assert evaluation.assistant_reply is not None
    assert evaluation.assistant_reply.reply_type == "monthly_status_query"
    assert "\u6708\u62a5\u72b6\u6001\u67e5\u8be2" in evaluation.assistant_reply.text
    assert "\u4e0d\u5199\u5165\u65e5\u62a5" in evaluation.assistant_reply.text


def test_assistant_reply_answers_internal_qa_without_daily_write():
    evaluation = evaluate_daily_shadow(_envelope("\u516c\u53f8\u5370\u7ae0\u501f\u7528\u6d41\u7a0b\u662f\u4ec0\u4e48\uff1f"), mode="protective_gate")

    assert evaluation.gate_decision.block_legacy_daily is True
    assert evaluation.commands == []
    assert evaluation.assistant_reply is not None
    assert evaluation.assistant_reply.reply_type == "internal_qa"
    assert "\u5370\u7ae0" in evaluation.assistant_reply.text


def test_assistant_reply_gives_legal_research_frame_without_daily_write():
    evaluation = evaluate_daily_shadow(
        _envelope("\u5e2e\u6211\u7814\u7a76\u4e00\u4e0b\u4f18\u5148\u53d7\u507f\u6743\u6700\u65b0\u88c1\u5224\u89c2\u70b9"),
        mode="protective_gate",
    )

    assert evaluation.gate_decision.block_legacy_daily is True
    assert evaluation.commands == []
    assert evaluation.assistant_reply is not None
    assert evaluation.assistant_reply.reply_type == "legal_research"
    assert "\u6cd5\u5f8b\u7814\u7a76" in evaluation.assistant_reply.text
    assert "\u4e0d\u5199\u5165\u65e5\u62a5" in evaluation.assistant_reply.text
    assert "\u4f18\u5148\u53d7\u507f\u6743" in evaluation.assistant_reply.text


def test_assistant_reply_keeps_orphan_confirmation_clarification_owned_by_gate():
    evaluation = evaluate_daily_shadow(_envelope("\u786e\u8ba4\u63d0\u4ea4"), mode="protective_gate")

    assert evaluation.gate_decision.block_legacy_daily is True
    assert evaluation.assistant_reply is None


def test_assistant_reply_handles_daily_plus_internal_qa_as_side_reply():
    evaluation = evaluate_daily_shadow(
        _envelope("\u4eca\u5929\u5b8c\u6210\u5408\u540c\u5ba1\u6838\u3002\u987a\u4fbf\u95ee\u4e0b\u5370\u7ae0\u6d41\u7a0b\u662f\u4ec0\u4e48\uff1f"),
        mode="protective_gate",
    )

    assert evaluation.gate_decision.block_legacy_daily is False
    assert [command.operation for command in evaluation.commands] == ["fill"]
    assert evaluation.commands[0].target_field == "today_work"
    assert evaluation.assistant_reply is not None
    assert evaluation.assistant_reply.reply_type == "internal_qa"
    assert "\u5370\u7ae0" in evaluation.assistant_reply.text


def test_assistant_reply_preserves_internal_qa_inside_active_daily_travel_mix():
    evaluation = evaluate_daily_shadow(
        _envelope(
            "\u4eca\u5929\u5b8c\u6210\u4e86\u5408\u540c\u8bc4\u5ba1\uff0c"
            "\u660e\u5929\u8ba1\u5212\u53bb\u5357\u4eac\u76d6\u7ae0\uff0c"
            "\u738b\u559c\u88ab\u544a\u6848\u4ef6\u6709\u591a\u5c11\uff1f",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-active",
                status="collecting",
                reply_candidate=True,
            ),
        ),
        mode="protective_gate",
    )

    assert evaluation.gate_decision.block_legacy_daily is False
    assert [(command.operation, command.target_field) for command in evaluation.commands] == [
        ("fill", "today_work"),
        ("fill", "tomorrow_plan"),
    ]
    assert evaluation.assistant_reply is not None
    assert evaluation.assistant_reply.reply_type == "internal_qa"
    assert "\u4e0d\u5199\u5165\u65e5\u62a5" in evaluation.assistant_reply.text


def test_assistant_reply_handles_daily_plus_legal_research_as_side_reply():
    evaluation = evaluate_daily_shadow(
        _envelope("\u4eca\u5929\u6574\u7406\u8bc9\u8bbc\u6750\u6599\u3002\u5e2e\u6211\u7814\u7a76\u4e00\u4e0b\u4f18\u5148\u53d7\u507f\u6743\u88c1\u5224\u89c4\u5219"),
        mode="protective_gate",
    )

    assert evaluation.gate_decision.block_legacy_daily is False
    assert [command.operation for command in evaluation.commands] == ["fill"]
    assert evaluation.assistant_reply is not None
    assert evaluation.assistant_reply.reply_type == "legal_research"
    assert "\u4f18\u5148\u53d7\u507f\u6743" in evaluation.assistant_reply.text
