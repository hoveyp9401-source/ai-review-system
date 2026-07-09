from datetime import datetime

from app.agent2.coordination_plan import ACTION_DAILY_ENTRY, compile_coordination_plan
from app.agent2.daily_commands import compile_daily_commands
from app.agent2.daily_shadow import evaluate_daily_shadow
from app.agent2.daily_state import PENDING_DAILY_CANDIDATE_KEY
from app.workflows.gate import build_gate_decision
from app.workflows.intake import (
    ActiveWorkflowTask,
    IncomingMessageEnvelope,
    WORKFLOW_DAILY_REPORT,
    WorkflowRouter,
)


def _compile(raw_text: str, *tasks: ActiveWorkflowTask, received_at=None):
    envelope = IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="Test User",
        dingtalk_user_id="ding-user-1",
        source="test",
        raw_text=raw_text,
        received_at=received_at,
        active_tasks=tuple(tasks),
    )
    plan = WorkflowRouter().plan(envelope)
    gate = build_gate_decision(plan, mode="protective_gate")
    return plan, gate, compile_daily_commands(plan, envelope)


def test_daily_work_effect_compiles_to_fill_command():
    plan, gate, commands = _compile("\u4eca\u5929\u5b8c\u6210\u5408\u540c\u5ba1\u6838")

    assert plan.primary_workflow == WORKFLOW_DAILY_REPORT
    assert gate.allow_legacy_daily is True
    assert len(commands) == 1
    command = commands[0]
    assert command.operation == "fill"
    assert command.target_field == "today_work"
    assert command.target_date == "today"
    assert command.should_write is True
    assert command.execution_policy == "dry_run"
    assert command.raw_text_hash
    assert command.as_dict()["content_count"] == 1


def test_daily_command_treats_tomorrow_ask_judge_as_plan():
    plan, gate, commands = _compile("\u660e\u5929\u95ee\u4e0b\u6cd5\u5b98")

    assert plan.primary_workflow == WORKFLOW_DAILY_REPORT
    assert gate.allow_legacy_daily is True
    assert len(commands) == 1
    assert commands[0].should_write is True
    assert commands[0].target_field == "tomorrow_plan"
    assert commands[0].target_date == "tomorrow"


def test_daily_command_blocks_conditional_feedback_but_keeps_today_bank_plan():
    _, _, feedback_commands = _compile("\u6cd5\u9662\u6536\u5230\u7684\u8bdd\u7ed9\u4e2a\u53cd\u9988")
    _, _, bank_commands = _compile("\u8fd8\u6709\uff0c\u4eca\u5929\u8ba1\u5212\u53bb\u94f6\u884c")

    assert all(not command.should_write for command in feedback_commands)
    assert any(command.should_write and command.target_field == "today_work" for command in bank_commands)


def test_daily_command_accepts_rd_iteration_bug_and_problem_statement():
    _, _, commands = _compile(
        "\u4eca\u513f\u548c\u7814\u53d1\u5bf9\u4e86\u4e0b\u8fed\u4ee3\u9700\u6c42\uff0c\u4ea4\u4ed8\u5b9a\u5230\u5468\u4e94\u4e86\uff0c\u4f46\u7b2c\u4e09\u65b9\u63a5\u53e3\u53ef\u80fd\u62d6\u540e\u817f\uff0c\u5f97\u8ba9\u5546\u52a1\u53bb\u50ac\u50ac\u3002\u8fd8\u4fee\u4e86\u4fe9\u7ebf\u4e0abug\u3002"
    )

    assert any(command.should_write and command.target_field == "today_work" for command in commands)


def test_daily_command_splits_work_and_risk_without_raw_sentence_pollution():
    _, _, commands = _compile("\u5ba1\u4e86A\u516c\u53f8\u548cB\u516c\u53f8\u7684\u5408\u540c\uff0cB\u90a3\u4e2a\u6709\u4e2a\u6761\u6b3e\u98ce\u9669\uff0c\u53ef\u80fd\u8fdd\u7ea6")

    assert any(command.should_write and command.target_field == "today_work" and "\u5408\u540c" in "".join(command.content) for command in commands)
    assert any(command.should_write and command.target_field == "problems" and "\u53ef\u80fd\u8fdd\u7ea6" in "".join(command.content) for command in commands)
    assert all("\u5ba1\u4e86A\u516c\u53f8\u548cB\u516c\u53f8\u7684\u5408\u540c\uff0cB\u90a3\u4e2a\u6709\u4e2a\u6761\u6b3e\u98ce\u9669" not in "".join(command.content) for command in commands)


def test_daily_command_keeps_contextual_system_failure_as_problem_in_active_context():
    active = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-context-system-failure",
        status="collecting",
        reply_candidate=True,
    )
    _, _, commands = _compile(
        "\u5bf9\u4e86\uff0c\u6628\u5929\u4f60\u8bf4\u7684\u90a3\u4e2a\u4f1a\u8bae\u5ba4\u9884\u8ba2\u7cfb\u7edf\uff0c\u4eca\u5929\u8bd5\u4e86\u4e0b\u8fd8\u662f\u4e0d\u884c\uff0c\u4f60\u5e2e\u6211\u770b\u770b\u5457\u3002",
        active,
    )

    assert any(
        command.should_write
        and command.target_date == "today"
        and command.target_field == "problems"
        and "\u4f1a\u8bae\u5ba4\u9884\u8ba2\u7cfb\u7edf" in "".join(command.content)
        and "\u4f60\u5e2e\u6211\u770b" not in "".join(command.content)
        for command in commands
    )


def test_daily_command_blocks_raw_yesterday_same_as_today_without_source_context():
    _, _, commands = _compile("\u6628\u5929\u4e3b\u8981\u641e\u5b9a\u4e86\u4ea7\u6743\u8bc1\u53d8\u66f4\uff0c\u4eca\u5929\u5dee\u4e0d\u591a\uff0c\u8fd8\u662f\u90a3\u4e9b\u4e8b\u3002")
    _, _, colloquial_commands = _compile("\u6628\u513f\u4e3b\u8981\u5904\u7406\u4e86\u4fdd\u5229\u6848\uff0c\u4eca\u513f\u5462")

    assert any(command.should_write and command.operation == "copy_previous" and command.target_field == "today_work" for command in commands)
    assert all("\u6628\u5929\u4e3b\u8981\u641e\u5b9a" not in "".join(command.content) for command in commands)
    assert any(command.should_write and command.operation == "copy_previous" and command.target_field == "today_work" for command in colloquial_commands)


def test_daily_command_blocks_absurd_travel_content():
    _, gate, commands = _compile("今天去火星出差跟外星人谈并购")

    assert gate.allow_legacy_daily is False
    assert commands == []


def test_daily_command_accepts_complaint_handling_as_work():
    _, _, commands = _compile("对了，今天其实还处理了市场部的一个紧急投诉")

    assert len(commands) == 1
    assert commands[0].operation == "fill"
    assert commands[0].target_field == "today_work"
    assert commands[0].should_write is True


def test_daily_command_accepts_business_how_to_plan_statement():
    _, _, commands = _compile("明天得想个办法怎么跟客户B说，不然要违约了")

    assert len(commands) == 1
    assert commands[0].operation == "fill"
    assert commands[0].target_field == "tomorrow_plan"
    assert commands[0].should_write is True


def test_daily_command_accepts_contextual_tomorrow_plan_reference_in_active_daily():
    _, _, commands = _compile(
        "这个明天处理，另外明天还要交月报",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-context",
            status="collecting",
            reply_candidate=True,
        ),
    )

    writes = [command for command in commands if command.should_write]
    assert writes
    assert all(command.target_field == "tomorrow_plan" for command in writes)


def test_daily_command_accepts_report_writing_as_tomorrow_plan():
    _, _, commands = _compile("明天要写报告")

    assert len(commands) == 1
    assert commands[0].operation == "fill"
    assert commands[0].target_field == "tomorrow_plan"
    assert commands[0].should_write is True


def test_daily_command_blocks_weak_destination_fragment():
    _, _, commands = _compile("\u54e6\u5bf9\uff0c\u660e\u5929\u662f\u53bb\u82cf\u5dde\u3002")

    assert commands == []


def test_daily_command_blocks_fantasy_daily_content():
    _, _, commands = _compile("\u4eca\u5929\u6211\u53d8\u6210\u4e86\u4e00\u53ea\u732b\uff0c\u660e\u5929\u8ba1\u5212\u62ef\u6551\u5730\u7403\u3002")

    assert commands == []


def test_daily_command_blocks_weather_personal_and_submission_chatter():
    for text in [
        "\u4eca\u5929\u96e8\u597d\u5927\u554a\uff0c\u70e6\u6b7b\u4e86\uff0c\u4f60\u4eec\u90a3\u8fb9\u4e0b\u6ca1",
        "\u4eca\u5929\u5fd9\u6b7b\u4e86\uff0c\u90fd\u6ca1\u7a7a\u559d\u6c34",
        "\u5c31\u6211\u8fd8\u6ca1\u4ea4\u554a\uff1f\u90a3\u6211\u660e\u5929\u8865\u5427",
    ]:
        _, _, commands = _compile(text)

        assert commands == []


def test_daily_command_blocks_summary_request_in_active_daily():
    _, _, commands = _compile(
        "\u5bf9\u4e86\uff0c\u603b\u7ed3\u4e00\u4e0b\u521a\u624d\u90a3\u4e2a\u6848\u5b50\u7684\u98ce\u9669\u70b9\u3002",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-active-summary",
            status="collecting",
            reply_candidate=True,
        ),
    )

    assert commands == []


def test_active_daily_blocks_meta_commit_request_without_concrete_content():
    task = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-active-meta-commit",
        status="collecting",
        reply_candidate=True,
    )

    for text in [
        "\u5199\u65e5\u62a5\u5427\uff0c\u628a\u4eca\u5929\u5e72\u7684\u6d3b\u8bb0\u4e00\u4e0b",
        "\u597d\u4e86\uff0c\u5199\u65e5\u62a5\u5427\uff0c\u628a\u8fd9\u4e9b\u8bb0\u8fdb\u53bb",
    ]:
        _, _, commands = _compile(text, task)

        assert all(command.should_write is False for command in commands)


def test_active_daily_accepts_morning_data_check_as_tomorrow_plan_after_lifestyle_clause():
    _, _, commands = _compile(
        "\u7b97\u4e86\u4e0d\u7528\u7ba1\u5929\u6c14\uff0c\u660e\u65e9\u518d\u5bf9\u4e00\u904d\u6570\u636e\uff0c\u540e\u5929\u63d0\u4ea4",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-active-morning-plan",
            status="collecting",
            reply_candidate=True,
        ),
    )

    writes = [command for command in commands if command.should_write]
    assert len(writes) == 1
    assert writes[0].target_field == "tomorrow_plan"
    assert "\u5bf9\u4e00\u904d\u6570\u636e" in "".join(writes[0].content)


def test_active_daily_accepts_office_followup_business_objects():
    task = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-active-office-followup",
        status="collecting",
        reply_candidate=True,
    )

    _, _, commands = _compile(
        "\u4eca\u5929\u505a\u4e86\u5565\u5462\u2026\u2026\u54e6\u5bf9\u4e86\uff0c\u4e0a\u5348\u6574\u7406\u6863\u6848\uff0c\u4e0b\u5348\u5f00\u4e86\u4e2a\u8bc4\u5ba1\u4f1a",
        task,
    )

    writes = [command for command in commands if command.should_write]
    assert len(writes) == 2
    assert all(command.target_field == "today_work" for command in writes)
    assert "\u6574\u7406\u6863\u6848" in "".join(writes[0].content)
    assert "\u8bc4\u5ba1\u4f1a" in "".join(writes[1].content)


def test_active_daily_cleans_supplement_prefix_and_keeps_business_content():
    task = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-active-supplement",
        status="collecting",
        reply_candidate=True,
    )

    _, _, commands = _compile(
        "\u628a\u4eca\u5929\u7684\u5de5\u4f5c\u4e8b\u9879\u518d\u8865\u5145\u4e00\u4e2a\uff1a\u4e0b\u5348\u53c2\u52a0\u4e86\u6cd5\u52a1\u90e8\u4f8b\u4f1a\uff0c\u8ba8\u8bba\u4e86\u65b0\u89c4\u3002",
        task,
    )

    content = [item for command in commands for item in command.content if command.should_write]
    assert "\u4e0b\u5348\u53c2\u52a0\u6cd5\u52a1\u90e8\u4f8b\u4f1a" in content
    assert "\u8ba8\u8bba\u65b0\u89c4" in content
    assert all("\u518d\u8865\u5145\u4e00\u4e2a" not in item for item in content)


def test_active_daily_blocks_vague_tomorrow_same_things():
    _, _, commands = _compile(
        "\u660e\u5929\u8ba1\u5212\u8fd8\u662f\u90a3\u4e9b\u3002",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-active-vague-plan",
            status="collecting",
            reply_candidate=True,
        ),
    )

    assert commands == []


def test_active_daily_accepts_colloquial_bug_and_test_case_followup():
    _, _, commands = _compile(
        "\u5c31\u662f\u7ee7\u7eed\u6539bug\u548c\u5199\u6d4b\u8bd5\u7528\u4f8b\u554a\u3002",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-dev-followup",
            status="collecting",
            reply_candidate=True,
        ),
    )

    assert len(commands) == 1
    assert commands[0].operation == "fill"
    assert commands[0].target_field == "today_work"
    assert commands[0].should_write is True


def test_active_daily_accepts_tomorrow_need_review_with_reminder_suffix():
    _, _, commands = _compile(
        "\u5bf9\u4e86\uff0c\u660e\u5929\u8981\u8bc4\u5ba1\u65b0\u9700\u6c42\uff0c\u522b\u5fd8\u4e86\u3002",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-demand-review",
            status="collecting",
            reply_candidate=True,
        ),
    )

    assert len(commands) == 1
    assert commands[0].operation == "fill"
    assert commands[0].target_field == "tomorrow_plan"
    assert commands[0].content == ["\u8bc4\u5ba1\u65b0\u9700\u6c42"]
    assert commands[0].should_write is True


def test_daily_command_extracts_business_from_lifestyle_mixed_sentence():
    _, _, commands = _compile(
        "\u4eca\u5929\u597d\u7d2f\uff0c\u4e2d\u5348\u98df\u5802\u7684\u83dc\u592a\u54b8\u4e86\uff0c\u4e0b\u5348\u641e\u5b8c\u4e86\u4e00\u4e2a\u5c3d\u8c03\u62a5\u544a\uff0c\u660e\u5929\u8fd8\u8981\u7ee7\u7eed\u5f04\u53e6\u4e00\u4e2a\u3002"
    )

    assert commands
    assert all("\u98df\u5802" not in "".join(command.content) for command in commands)
    assert any(command.target_field == "today_work" and "\u5c3d\u8c03\u62a5\u544a" in "".join(command.content) for command in commands)


def test_daily_command_treats_robot_capability_question_as_no_write_in_active_daily():
    _, _, commands = _compile(
        "\u8bdd\u8bf4\u673a\u5668\u4eba\u4f60\u4f1a\u5199\u8bd7\u5417\uff1f",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-active-chat",
            status="collecting",
            reply_candidate=True,
        ),
    )

    assert commands == []


def test_daily_command_copy_yesterday_report_does_not_use_raw_text_as_content():
    _, _, commands = _compile("\u628a\u6628\u5929\u7684\u65e5\u62a5\u590d\u5236\u5230\u4eca\u5929")

    assert len(commands) == 1
    assert commands[0].operation == "copy_previous"
    assert commands[0].content == []


def test_daily_command_cleans_completed_yesterday_plan_content():
    _, _, commands = _compile("\u6628\u5929\u7684\u660e\u65e5\u8ba1\u5212\u662f\u5ba1\u5408\u540c\uff0c\u5df2\u7ecf\u5ba1\u5b8c\u4e86\u3002")

    assert commands
    assert all(command.target_field == "today_work" for command in commands)
    assert "\u6628\u5929" not in "".join(part for command in commands for part in command.content)


def test_daily_command_strips_add_today_work_prefix():
    _, _, commands = _compile(
        "\u52a0\u4e0a\u4eca\u5929\u7684\u5b8c\u6210\u5de5\u4f5c\uff1a\u5ba1\u6838\u4e86\u5408\u540c",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-add-prefix",
            status="collecting",
            reply_candidate=True,
        ),
    )

    assert len(commands) == 1
    assert commands[0].content == ["\u5ba1\u6838\u5408\u540c"]


def test_daily_command_cleans_completed_yesterday_todo_reference():
    _, _, commands = _compile("\u5bf9\u4e86\uff0c\u6628\u5929\u7684\u660e\u65e5\u8ba1\u5212\u91cc\u90a3\u4e2a\u5f85\u529e\uff0c\u8ddf\u5ba1\u8ba1\u5bf9\u63a5\u7684\u4e8b\u6211\u641e\u5b8c\u4e86")

    assert len(commands) == 1
    assert commands[0].target_field == "today_work"
    assert commands[0].should_write is True
    assert commands[0].content == ["\u8ddf\u5ba1\u8ba1\u5bf9\u63a5"]


def test_daily_command_accepts_court_document_receipt_and_cleans_service_prefix():
    _, _, commands = _compile("\u90a3\u5e2e\u6211\u8bb0\u4e00\u4e0b\uff0c\u4e0b\u5348\u8ddf\u5f8b\u5e08\u786e\u8ba4\u4e86\u4fdd\u5168\u88c1\u5b9a\u4e66\u5df2\u7ecf\u6536\u5230\u4e86")

    assert len(commands) == 1
    assert commands[0].target_field == "today_work"
    assert commands[0].should_write is True
    content = "".join(commands[0].content)
    assert "\u4fdd\u5168\u88c1\u5b9a\u4e66" in content
    assert "\u5e2e\u6211\u8bb0" not in content


def test_daily_command_accepts_arbitration_material_delivery_with_tracking_number():
    _, _, commands = _compile("\u54e6\u5bf9\u4e86\uff0c\u4e0b\u5348\u7ec8\u4e8e\u628a\u90a3\u4e2a\u6d89\u5916\u4ef2\u88c1\u7684\u6750\u6599\u5bc4\u51fa\u53bb\u4e86\uff0c\u987a\u4e30\u5355\u53f7SF123456")

    assert len(commands) == 1
    assert commands[0].target_field == "today_work"
    assert commands[0].should_write is True
    content = "".join(commands[0].content)
    assert "\u6d89\u5916\u4ef2\u88c1" in content
    assert "SF123456" in content


def test_daily_command_writes_self_reported_research_but_not_service_lookup():
    for text in [
        "\u4eca\u5929\u67e5\u9605\u4e86\u4fdd\u5229\u6848\u4ef6\u8d44\u6599",
        "\u4eca\u5929\u53bb\u67e5\u4e86XX\u6848\u4ef6\u8d44\u6599",
        "\u4eca\u5929\u67e5\u8be2\u4e86XX\u6848\u4ef6\u6750\u6599",
        "\u4eca\u5929\u68c0\u7d22\u4e86\u6d77\u82b1\u5c9b\u6848\u4ef6\u6750\u6599",
    ]:
        _, _, commands = _compile(text)

        assert len(commands) == 1
        assert commands[0].target_field == "today_work"
        assert commands[0].should_write is True

    _, _, service_commands = _compile("\u5e2e\u6211\u67e5\u4e00\u4e0b\u516c\u53f8\u6cd5\u52a1\u90e8\u7684\u6700\u65b0\u57f9\u8bad\u8d44\u6599\uff0c\u53d1\u94fe\u63a5\u7ed9\u6211")
    assert service_commands == []


def test_daily_command_blocks_monthly_followup_process_help_and_daily_meta_chatter():
    for text in [
        "\u54ce\uff0c\u6700\u8fd1\u5929\u5929\u52a0\u73ed\uff0c\u65e5\u62a5\u4e5f\u4e0d\u77e5\u9053\u5199\u5565\uff0c\u611f\u89c9\u90fd\u662f\u91cd\u590d\u7684",
        "\u50ac\u4e00\u4e0b\u6ca1\u4ea4\u7684\u5427\uff0cdeadline\u5c31\u662f\u4eca\u5929\uff0c\u518d\u62d6\u5c31\u6263\u7ee9\u6548\u4e86",
        "\u5bf9\u4e86\uff0c\u90a3\u4e2a\u5408\u540cD\u7684\u7528\u5370\u7533\u8bf7\u7cfb\u7edf\u600e\u4e48\u63d0\u554a\uff1f\u6211\u660e\u5929\u6025\u7740\u8981\u7528\u5370\uff0c\u6015\u641e\u9519\u3002",
        "\u5bf9\u4e86\uff0c\u5e2e\u6211\u67e5\u4e00\u4e0b\u516c\u53f8\u6cd5\u52a1\u90e8\u7684\u6700\u65b0\u57f9\u8bad\u8d44\u6599\uff0c\u53d1\u94fe\u63a5\u7ed9\u6211\u3002",
    ]:
        _, _, commands = _compile(text)

        assert commands == []


def test_daily_command_cleans_yesterday_plan_prefix_but_keeps_current_result():
    _, _, commands = _compile("\u6628\u5929\u65e5\u62a5\u91cc\u7684\u660e\u65e5\u8ba1\u5212\uff1a\u8ddf\u6cd5\u52a1\u603b\u76d1\u6c47\u62a5\u5408\u540cB\u7684\u98ce\u9669\uff0c\u4eca\u5929\u4e0a\u5348\u5df2\u7ecf\u6c47\u62a5\u5b8c\u4e86\uff0c\u603b\u76d1\u540c\u610f\u6309\u539f\u65b9\u6848\u8d70\u3002")

    assert len(commands) == 1
    assert commands[0].target_field == "today_work"
    assert commands[0].should_write is True
    content = "".join(commands[0].content)
    assert "\u6628\u5929" not in content
    assert "\u660e\u65e5\u8ba1\u5212" not in content
    assert "\u603b\u76d1\u540c\u610f\u6309\u539f\u65b9\u6848\u8d70" in content


def test_daily_command_accepts_contextual_case_followup_as_tomorrow_plan():
    _, _, commands = _compile("那案子的进展就是这样，明天还要跟进")

    assert len(commands) == 1
    assert commands[0].operation == "fill"
    assert commands[0].target_field == "tomorrow_plan"
    assert commands[0].should_write is True


def test_daily_command_accepts_short_contextual_followup_plan_in_active_daily():
    _, _, commands = _compile(
        "明天还要跟进",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-context",
            status="collecting",
            reply_candidate=True,
        ),
    )

    assert len(commands) == 1
    assert commands[0].operation == "fill"
    assert commands[0].target_field == "tomorrow_plan"
    assert commands[0].should_write is True


def test_daily_command_allows_business_correction_edit_in_active_daily():
    _, _, commands = _compile(
        "说错了，是今天整理文档和开会，明天见客户",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-context",
            status="collecting",
            reply_candidate=True,
        ),
    )

    assert len(commands) == 1
    assert commands[0].operation == "edit"
    assert commands[0].should_write is True


def test_daily_command_strips_plain_daily_prefix():
    _, _, commands = _compile("日报：今天开了部门例会")

    assert len(commands) == 1
    assert commands[0].content == ["开部门例会"]


def test_daily_command_strips_problem_field_prefix():
    _, _, commands = _compile(
        "问题：客户不满意可能要升级",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-context",
            status="collecting",
            reply_candidate=True,
        ),
    )

    assert len(commands) == 1
    assert commands[0].target_field == "problems"
    assert commands[0].content == ["客户不满意可能要升级"]


def test_daily_command_strips_problem_is_prefix():
    _, _, commands = _compile(
        "问题是项目进度有点滞后",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-context",
            status="collecting",
            reply_candidate=True,
        ),
    )

    assert len(commands) == 1
    assert commands[0].target_field == "problems"
    assert commands[0].content == ["项目进度有点滞后"]


def test_daily_command_strips_emotion_and_daily_meta_from_work_content():
    _, _, commands = _compile("今天开了三个会，累死了，还得写日报")

    writes = [command for command in commands if command.should_write]
    assert len(writes) == 1
    assert writes[0].target_field == "today_work"
    assert writes[0].content == ["开三个会"]


def test_active_daily_empty_problem_reply_targets_problems_field():
    _, _, commands = _compile(
        "\u6682\u65e0\u95ee\u9898",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-1",
            status="collecting",
            reply_candidate=True,
        ),
    )

    assert len(commands) == 1
    assert commands[0].operation == "fill"
    assert commands[0].target_field == "problems"
    assert commands[0].should_write is True


def test_active_daily_no_problem_reply_with_sha_is_not_treated_as_question():
    _, _, commands = _compile(
        "\u6ca1\u5565\u95ee\u9898",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-1",
            status="collecting",
            reply_candidate=True,
        ),
    )

    assert len(commands) == 1
    assert commands[0].operation == "fill"
    assert commands[0].target_field == "problems"
    assert commands[0].should_write is True


def test_active_daily_confirmation_compiles_to_confirm_command():
    _, _, commands = _compile(
        "\u786e\u8ba4\u63d0\u4ea4",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-2",
            status="pending_confirmation",
            reply_candidate=True,
            awaiting_confirmation=True,
        ),
    )

    assert len(commands) == 1
    assert commands[0].operation == "confirm"
    assert commands[0].target_field == "all"
    assert commands[0].should_write is True


def test_monthly_reply_does_not_compile_daily_write_commands_even_with_active_daily():
    plan, gate, commands = _compile(
        (
            "1. \u3010\u7d22\u8d54\u7ba1\u7406\u3011\n"
            "\u672a\u5b8c\u6210\u539f\u56e0/\u5b58\u5728\u95ee\u9898\uff1a\u6682\u65e0\n"
            "\u4e0b\u6708\u76ee\u6807\uff08\u4e07\u5143\uff09\uff1a100\n"
            "\u884c\u52a8\u65b9\u6848\uff1a\u7ee7\u7eed\u63a8\u8fdb"
        ),
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-active",
            status="collecting",
            reply_candidate=True,
        ),
    )

    assert plan.primary_workflow == "monthly_report"
    assert gate.allow_legacy_daily is False
    assert commands == []


def test_pending_candidate_focus_confirmation_compiles_to_no_daily_command():
    plan, gate, commands = _compile(
        "对",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-candidate",
            status="collecting",
            reply_candidate=True,
            metadata={"pending_keys": [PENDING_DAILY_CANDIDATE_KEY]},
        ),
    )

    assert plan.primary_workflow != WORKFLOW_DAILY_REPORT
    assert gate.allow_legacy_daily is False
    assert commands == []


def test_active_daily_candidate_followup_replacement_compiles_to_edit_command():
    _, gate, commands = _compile(
        "改成进行上海机载项目评审",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-candidate",
            status="collecting",
            reply_candidate=True,
            metadata={"pending_keys": [PENDING_DAILY_CANDIDATE_KEY]},
        ),
    )

    assert gate.need_confirmation is False
    assert len(commands) == 1
    assert commands[0].operation == "edit"
    assert commands[0].target_field == "today_work"
    assert commands[0].should_write is True


def test_active_daily_negative_replacement_compiles_to_edit_command():
    _, gate, commands = _compile(
        "明天不是去南京，是去上海开庭",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-negative-replacement",
            status="pending_confirmation",
            reply_candidate=True,
            awaiting_confirmation=True,
        ),
    )

    assert gate.need_confirmation is False
    assert len(commands) == 1
    assert commands[0].operation == "edit"
    assert commands[0].target_field == "tomorrow_plan"
    assert commands[0].should_write is True


def test_clear_report_compiles_to_direct_clear_command():
    _, gate, commands = _compile("\u6e05\u7a7a\u4eca\u65e5\u65e5\u62a5")

    assert gate.need_confirmation is False
    assert len(commands) == 1
    assert commands[0].operation == "clear"
    assert commands[0].target_field == "all"
    assert commands[0].requires_confirmation is False
    assert commands[0].should_write is True
    assert "destructive_or_overwrite" in commands[0].safety_flags


def test_clear_current_report_is_not_misclassified_as_query_current():
    _, gate, commands = _compile("\u6e05\u7a7a\u5f53\u524d\u65e5\u62a5")

    assert gate.need_confirmation is False
    assert len(commands) == 1
    assert commands[0].operation == "clear"
    assert commands[0].target_field == "all"
    assert commands[0].requires_confirmation is False


def test_delete_numbered_item_compiles_to_edit_not_clear():
    _, gate, commands = _compile(
        "删除第一条",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-edit-delete",
            status="collecting",
            reply_candidate=True,
        ),
    )

    assert gate.need_confirmation is False
    assert len(commands) == 1
    assert commands[0].operation == "edit"
    assert commands[0].target_field == "today_work"
    assert commands[0].should_write is True


def test_short_delete_reply_compiles_to_edit_not_clear_when_daily_is_active():
    _, gate, commands = _compile(
        "\u5220\u6389\u5427",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-short-delete",
            status="collecting",
            reply_candidate=True,
        ),
    )

    assert gate.need_confirmation is False
    assert len(commands) == 1
    assert commands[0].operation == "edit"
    assert commands[0].target_field == "today_work"
    assert commands[0].should_write is True


def test_clear_single_field_keeps_target_field():
    _, _, commands = _compile(
        "清空今日工作",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-edit-clear-field",
            status="collecting",
            reply_candidate=True,
        ),
    )

    assert len(commands) == 1
    assert commands[0].operation == "clear"
    assert commands[0].target_field == "today_work"


def test_active_daily_display_request_compiles_to_query_current():
    _, _, commands = _compile(
        "\u53d1\u6211\u770b\u4e0b",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-3",
            status="collecting",
            reply_candidate=True,
        ),
    )

    assert len(commands) == 1
    assert commands[0].operation == "query_current"
    assert commands[0].target_field == "none"
    assert commands[0].should_write is False


def test_active_daily_bare_display_request_compiles_to_query_current():
    _, _, commands = _compile(
        "\u53d1\u6211",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-bare-display",
            status="collecting",
            reply_candidate=True,
        ),
    )

    assert len(commands) == 1
    assert commands[0].operation == "query_current"
    assert commands[0].target_field == "none"
    assert commands[0].should_write is False


def test_active_daily_show_report_request_compiles_to_query_current():
    _, _, commands = _compile(
        "\u5c55\u793a\u65e5\u62a5",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-show-report",
            status="collecting",
            reply_candidate=True,
        ),
    )

    assert len(commands) == 1
    assert commands[0].operation == "query_current"
    assert commands[0].target_field == "none"
    assert commands[0].should_write is False


def test_active_daily_current_query_carries_active_report_date():
    _, _, commands = _compile(
        "\u53d1\u6211\u770b\u4e0b",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-catchup",
            status="collecting",
            reply_candidate=True,
            metadata={"report_date": "2026-07-06"},
        ),
    )

    assert len(commands) == 1
    assert commands[0].operation == "query_current"
    assert commands[0].active_report_date == "2026-07-06"
    assert commands[0].should_write is False


def test_active_daily_today_work_edit_carries_active_report_date():
    _, _, commands = _compile(
        "\u4eca\u65e5\u5de5\u4f5c\u7684\u7b2c\u56db\u6761\u5220\u6389",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-catchup",
            status="collecting",
            reply_candidate=True,
            metadata={"report_date": "2026-07-06"},
        ),
    )

    assert len(commands) == 1
    assert commands[0].operation == "edit"
    assert commands[0].target_field == "today_work"
    assert commands[0].active_report_date == "2026-07-06"
    assert commands[0].should_write is True


def test_active_daily_yesterday_item_edit_keeps_explicit_history_date():
    _, _, commands = _compile(
        "\u6628\u5929\u7b2c\u56db\u6761\u5220\u6389",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-current",
            status="collecting",
            reply_candidate=True,
            metadata={"report_date": "2026-07-07"},
        ),
    )

    assert len(commands) == 1
    assert commands[0].operation == "edit"
    assert commands[0].target_date == "yesterday"
    assert commands[0].active_report_date == "2026-07-07"
    assert commands[0].should_write is True


def test_after_nine_blocks_historical_daily_edit_even_when_explicit():
    _, _, commands = _compile(
        "\u6628\u5929\u7b2c\u56db\u6761\u5220\u6389",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-current",
            status="collecting",
            reply_candidate=True,
            metadata={"report_date": "2026-07-07"},
        ),
        received_at=datetime(2026, 7, 7, 15, 32),
    )

    assert len(commands) == 1
    assert commands[0].operation == "no_write"
    assert commands[0].should_write is False
    assert "historical_daily_mutation_blocked_after_cutoff" in commands[0].safety_flags


def test_after_nine_bare_clear_targets_today_not_historical_active_context():
    _, _, commands = _compile(
        "\u6e05\u7a7a\u65e5\u62a5",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-history-active",
            status="collecting",
            reply_candidate=True,
            metadata={"report_date": "2026-07-06"},
        ),
        received_at=datetime(2026, 7, 7, 15, 32),
    )

    assert len(commands) == 1
    assert commands[0].operation == "clear"
    assert commands[0].target_field == "all"
    assert commands[0].active_report_date == ""
    assert commands[0].should_write is True
    assert "historical_daily_mutation_blocked_after_cutoff" not in commands[0].safety_flags


def test_after_nine_today_write_does_not_use_historical_active_context():
    _, _, commands = _compile(
        "\u4eca\u5929\u5b8c\u6210\u5408\u540c\u5ba1\u6838",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-history-active",
            status="collecting",
            reply_candidate=True,
            metadata={"report_date": "2026-07-06"},
        ),
        received_at=datetime(2026, 7, 7, 15, 32),
    )

    assert len(commands) == 1
    assert commands[0].operation == "fill"
    assert commands[0].target_date == "today"
    assert commands[0].active_report_date == ""
    assert commands[0].should_write is True
    assert "historical_daily_mutation_blocked_after_cutoff" not in commands[0].safety_flags


def test_after_nine_still_allows_historical_daily_query_and_copy_sources():
    _, _, query_commands = _compile(
        "\u6628\u5929\u65e5\u62a5\u53d1\u6211\u4e0b",
        received_at=datetime(2026, 7, 7, 15, 32),
    )
    _, _, copy_commands = _compile(
        "\u4eca\u5929\u548c\u6628\u5929\u5de5\u4f5c\u4e00\u6837",
        received_at=datetime(2026, 7, 7, 15, 32),
    )
    _, _, completed_plan_commands = _compile(
        "\u6628\u5929\u7684\u8ba1\u5212\u90fd\u5b8c\u6210\u4e86",
        received_at=datetime(2026, 7, 7, 15, 32),
    )

    assert query_commands[0].operation == "query_history"
    assert query_commands[0].should_write is False
    assert copy_commands[0].operation == "copy_previous"
    assert copy_commands[0].target_field == "today_work"
    assert copy_commands[0].should_write is True
    assert completed_plan_commands[0].operation == "complete_previous_plan"
    assert completed_plan_commands[0].target_field == "today_work"
    assert completed_plan_commands[0].should_write is True


def test_before_nine_still_allows_yesterday_edit():
    _, _, commands = _compile(
        "\u6628\u5929\u7b2c\u56db\u6761\u5220\u6389",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-current",
            status="collecting",
            reply_candidate=True,
            metadata={"report_date": "2026-07-07"},
        ),
        received_at=datetime(2026, 7, 7, 8, 30),
    )

    assert len(commands) == 1
    assert commands[0].operation == "edit"
    assert commands[0].target_date == "yesterday"
    assert commands[0].should_write is True


def test_dated_display_request_still_compiles_to_query_history():
    _, _, commands = _compile(
        "\u6628\u5929\u65e5\u62a5\u53d1\u6211\u4e0b",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-history-display",
            status="collecting",
            reply_candidate=True,
        ),
    )

    assert len(commands) == 1
    assert commands[0].operation == "query_history"
    assert commands[0].target_field == "none"
    assert commands[0].should_write is False


def test_dated_daily_edit_entry_compiles_to_begin_edit_without_writing():
    _, _, commands = _compile("\u6211\u8981\u6539\u6628\u5929\u65e5\u62a5")

    assert len(commands) == 1
    assert commands[0].operation == "begin_edit"
    assert commands[0].target_date == "yesterday"
    assert commands[0].target_field == "none"
    assert commands[0].should_write is False


def test_before_nine_bare_daily_edit_entry_defaults_to_yesterday():
    _, _, commands = _compile(
        "\u6211\u8981\u6539\u65e5\u62a5",
        received_at=datetime(2026, 7, 7, 8, 30),
    )

    assert len(commands) == 1
    assert commands[0].operation == "begin_edit"
    assert commands[0].target_date == "yesterday"
    assert commands[0].should_write is False


def test_before_nine_daily_fill_defaults_report_date_to_yesterday():
    _, _, commands = _compile(
        "\u660e\u65e5\u8ba1\u5212\u7ee7\u7eed\u4f18\u5316\u65e5\u62a5\u7cfb\u7edf",
        received_at=datetime(2026, 7, 7, 8, 30),
    )

    assert len(commands) == 1
    assert commands[0].operation == "fill"
    assert commands[0].target_field == "tomorrow_plan"
    assert commands[0].target_date == "yesterday"
    assert commands[0].should_write is True


def test_before_nine_explicit_today_report_keeps_today_target():
    _, _, commands = _compile(
        "\u4eca\u5929\u65e5\u62a5\uff1a\u5b8c\u6210\u5408\u540c\u5ba1\u6838",
        received_at=datetime(2026, 7, 7, 8, 30),
    )

    assert len(commands) == 1
    assert commands[0].operation == "fill"
    assert commands[0].target_date == "today"
    assert commands[0].should_write is True


def test_active_daily_copy_previous_is_direct_copy_command():
    _, _, commands = _compile(
        "\u590d\u5236\u6628\u5929",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-4",
            status="collecting",
            reply_candidate=True,
        ),
    )

    assert len(commands) == 1
    assert commands[0].operation == "copy_previous"
    assert commands[0].target_field == "all"
    assert commands[0].requires_confirmation is False
    assert commands[0].should_write is True


def test_active_daily_repeat_previous_work_compiles_to_today_work_copy_command():
    for text in [
        "\u4eca\u5929\u8fd8\u662f\u505a\u4e86\u6628\u5929\u90a3\u4e9b\u4e8b",
        "\u8fd8\u662f\u6628\u5929\u90a3\u4e9b\u4e8b",
        "\u8fd8\u662f\u6628\u5929\u90a3\u4e9b\u4e8b\u513f",
        "\u548c\u6628\u5929\u4e00\u6837\u7684\u5de5\u4f5c",
        "\u4eca\u5929\u5f97\u5de5\u4f5c\u548c\u6628\u5929\u4e00\u6837",
        "\u4eca\u5929\u548c\u524d\u5929\u4e00\u6837",
        "\u4eca\u65e5\u540c\u524d\u65e5\u7684\u5de5\u4f5c",
    ]:
        _, _, commands = _compile(
            text,
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-repeat-previous-work",
                status="collecting",
                reply_candidate=True,
            ),
        )

        assert len(commands) == 1
        assert commands[0].operation == "copy_previous"
        assert commands[0].target_field == "today_work"
        if "\u524d\u5929" in text or "\u524d\u65e5" in text:
            assert commands[0].target_date == "day_before_yesterday"
        assert commands[0].requires_confirmation is False
        assert commands[0].should_write is True


def test_repeat_previous_report_content_compiles_to_copy_not_raw_fill():
    _, _, commands = _compile("就是昨天那份日报的内容，今天接着干，没啥变化")

    assert len(commands) == 1
    assert commands[0].operation == "copy_previous"
    assert commands[0].target_field == "today_work"
    assert commands[0].content == []
    assert commands[0].should_write is True


def test_future_case_schedule_only_does_not_compile_to_daily_write():
    _, _, commands = _compile("保利那个案子下周二开庭，我们材料都准备好了，应该没问题。")

    assert commands == []


def test_daily_start_with_inline_content_compiles_to_fill_without_prefix():
    _, _, commands = _compile("写日报了：上午整理档案，下午接待客户咨询。")

    assert len(commands) == 1
    assert commands[0].operation == "fill"
    assert commands[0].target_field == "today_work"
    assert commands[0].content == ["上午整理档案，下午接待客户咨询"]
    assert commands[0].should_write is True


def test_daily_worded_confirmation_compiles_to_confirm():
    _, _, commands = _compile(
        "日报就这样吧。",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-confirm-worded",
            status="pending_confirmation",
            reply_candidate=True,
            awaiting_confirmation=True,
        ),
    )

    assert len(commands) == 1
    assert commands[0].operation == "confirm"
    assert commands[0].content == []
    assert commands[0].should_write is True


def test_after_nine_previous_day_makeup_does_not_write_today_report():
    _, _, commands = _compile("昨天忘了写，我昨天下午去了趟法院立案。", received_at=datetime(2026, 7, 7, 10, 30))

    assert len(commands) == 1
    assert commands[0].operation == "no_write"
    assert commands[0].target_date == "yesterday"
    assert commands[0].should_write is False
    assert "historical_daily_mutation_blocked_after_cutoff" in commands[0].safety_flags


def test_lifestyle_and_template_questions_do_not_compile_to_daily_writes():
    for text in [
        "今天热死了，明天穿啥出门啊，还要见客户",
        "顺便问下，合同审核标准模板在哪下载？",
        "帮我看看明天日程，有没有冲突，如果没事我就去法院了",
        "那今天就先研究流程吧，没啥别的了。",
        "今天还行吧",
    ]:
        _, _, commands = _compile(text)

        assert commands == []


def test_daily_with_weekly_summary_work_still_compiles_to_daily_fields():
    _, _, commands = _compile("今天主要还是搞那个数据清洗，内存溢出搞得头疼，明天打算换个算法试试，顺便还得弄周报汇总")

    assert [command.target_field for command in commands] == ["today_work", "tomorrow_plan", "tomorrow_plan"]
    assert all(command.should_write for command in commands)


def test_field_level_delete_compiles_as_daily_edit():
    _, _, commands = _compile(
        "把明日计划删了",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-delete-field",
            status="collecting",
            reply_candidate=True,
        ),
    )

    assert len(commands) == 1
    assert commands[0].operation == "clear"
    assert commands[0].target_field == "tomorrow_plan"
    assert commands[0].should_write is True


def test_vague_tomorrow_workload_statement_does_not_compile_to_daily_write():
    _, _, commands = _compile("明天应该也差不多，手头事多")

    assert commands == []


def test_contextual_previous_plan_continuation_compiles_to_tomorrow_plan():
    _, _, commands = _compile(
        "嗯，明天就接着昨天的计划继续做市场方案。",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-context-plan",
            status="collecting",
            reply_candidate=True,
        ),
    )

    assert len(commands) == 1
    assert commands[0].operation == "fill"
    assert commands[0].target_field == "tomorrow_plan"
    assert commands[0].should_write is True


def test_explicit_daily_list_with_weekly_item_compiles_to_today_work():
    _, _, commands = _compile("日报：今天干了三件事，改bug、写周报、开会")

    assert len(commands) >= 1
    assert all(command.target_field == "today_work" for command in commands)
    assert all(command.should_write for command in commands)


def test_completed_yesterday_tomorrow_plan_targets_today_work():
    _, _, commands = _compile("\u6628\u5929\u7684\u660e\u65e5\u8ba1\u5212\u5df2\u5b8c\u6210")

    assert len(commands) == 1
    assert commands[0].operation == "complete_previous_plan"
    assert commands[0].target_date == "today"
    assert commands[0].target_field == "today_work"
    assert commands[0].should_write is True
    assert commands[0].content == []


def test_yesterday_todo_done_variants_compile_to_previous_plan_completion():
    for text in ["昨天待办都完成了", "昨日安排全部搞定", "昨天的事项做完了"]:
        _, _, commands = _compile(text)

        assert len(commands) == 1
        assert commands[0].operation == "complete_previous_plan"
        assert commands[0].target_field == "today_work"


def test_action_first_daily_segments_compile_to_separate_fill_commands():
    _, _, commands = _compile(
        "\u4eca\u5929\u505a\u65e5\u62a5\u7cfb\u7edf\u4f18\u5316\uff0c"
        "\u660e\u5929\u5f00\u59cb\u505a\u6848\u4ef6\u8fdb\u5c55\u4e0e\u51fa\u5dee\u534f\u540c\u6a21\u5757",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-segmented-write",
            status="collecting",
            reply_candidate=True,
        ),
    )

    assert [command.operation for command in commands] == ["fill", "fill"]
    assert [command.target_field for command in commands] == ["today_work", "tomorrow_plan"]
    assert [command.content for command in commands] == [
        ["\u505a\u65e5\u62a5\u7cfb\u7edf\u4f18\u5316"],
        ["\u5f00\u59cb\u505a\u6848\u4ef6\u8fdb\u5c55\u4e0e\u51fa\u5dee\u534f\u540c\u6a21\u5757"],
    ]


def test_action_first_travel_followup_plan_compiles_to_two_tomorrow_items():
    _, _, commands = _compile(
        "\u660e\u5929\u53bb\u626c\u5dde\u51fa\u5dee\u5f00\u5ead\uff0c"
        "\u56de\u6765\u540e\u7ee7\u7eed\u5b8c\u5584\u6848\u4ef6\u53f0\u8d26",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-travel-followup",
            status="collecting",
            reply_candidate=True,
        ),
    )

    assert [command.operation for command in commands] == ["fill", "fill"]
    assert [command.target_field for command in commands] == ["tomorrow_plan", "tomorrow_plan"]
    assert [command.content for command in commands] == [
        ["\u53bb\u626c\u5dde\u51fa\u5dee\u5f00\u5ead"],
        ["\u56de\u6765\u540e\u7ee7\u7eed\u5b8c\u5584\u6848\u4ef6\u53f0\u8d26"],
    ]


def test_action_first_mixed_today_and_tomorrow_list_compiles_to_separate_commands():
    _, _, commands = _compile(
        "今天优化了日报agent，参加了AI应用比赛复审会议，收集公众号被告案件信息通报，"
        "明天计划继续优化日报agent、完成公众号通报内容编辑，完成被告板块季度邮件。",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-mixed-list",
            status="collecting",
            reply_candidate=True,
        ),
    )

    assert [command.operation for command in commands] == ["fill"] * 6
    assert [command.target_field for command in commands] == [
        "today_work",
        "today_work",
        "today_work",
        "tomorrow_plan",
        "tomorrow_plan",
        "tomorrow_plan",
    ]
    assert [command.content for command in commands] == [
        ["优化日报agent"],
        ["参加AI应用比赛复审会议"],
        ["收集公众号被告案件信息通报"],
        ["继续优化日报agent"],
        ["完成公众号通报内容编辑"],
        ["完成被告板块季度邮件"],
    ]


def test_action_first_future_trip_does_not_compile_to_daily_write():
    _, _, commands = _compile(
        "后天出差南通沟通保利案件调解",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-future-trip",
            status="collecting",
            reply_candidate=True,
        ),
    )

    assert commands == []


def test_revoke_report_is_direct_revoke_command():
    _, _, commands = _compile("\u64a4\u56de\u4eca\u65e5\u65e5\u62a5")

    assert len(commands) == 1
    assert commands[0].operation == "revoke"
    assert commands[0].target_field == "all"
    assert commands[0].requires_confirmation is False
    assert commands[0].should_write is True


def test_bare_withdraw_completed_active_daily_compiles_to_revoke():
    _, _, commands = _compile(
        "\u64a4\u56de",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-completed-revoke",
            status="completed",
            reply_candidate=True,
        ),
    )

    assert len(commands) == 1
    assert commands[0].operation == "revoke"
    assert commands[0].target_field == "all"
    assert commands[0].should_write is True


def test_bare_withdraw_collecting_active_daily_does_not_write_raw_text():
    _, _, commands = _compile(
        "\u64a4\u56de",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-collecting-revoke",
            status="collecting",
            reply_candidate=True,
        ),
    )

    assert len(commands) == 1
    assert commands[0].operation == "no_write"
    assert commands[0].should_write is False


def test_withdraw_and_modify_completed_active_daily_compiles_to_revoke():
    _, _, commands = _compile(
        "\u64a4\u56de\u5e76\u4fee\u6539",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-completed-revoke-edit",
            status="completed",
            reply_candidate=True,
        ),
    )

    assert len(commands) == 1
    assert commands[0].operation == "revoke"
    assert commands[0].should_write is True


def test_withdraw_and_modify_collecting_active_daily_does_not_fallback_or_write():
    _, _, commands = _compile(
        "\u64a4\u56de\u5e76\u4fee\u6539",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-collecting-revoke-edit",
            status="collecting",
            reply_candidate=True,
        ),
    )

    assert len(commands) == 1
    assert commands[0].operation == "no_write"
    assert commands[0].should_write is False


def test_withdraw_and_modify_lawsuit_document_stays_daily_content():
    _, _, commands = _compile(
        "\u4eca\u5929\u64a4\u56de\u5e76\u4fee\u6539XX\u6848\u8d77\u8bc9\u72b6",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-lawsuit-doc-revoke-edit",
            status="collecting",
            reply_candidate=True,
        ),
    )

    assert len(commands) == 1
    assert commands[0].operation == "fill"
    assert commands[0].target_field == "today_work"


def test_withdrawing_lawsuit_document_is_daily_content_not_report_revoke():
    _, _, commands = _compile(
        "\u64a4\u56deXX\u6848\u8d77\u8bc9\u72b6",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-lawsuit-doc",
            status="collecting",
            reply_candidate=True,
        ),
    )

    assert len(commands) == 1
    assert commands[0].operation == "fill"
    assert commands[0].target_field == "today_work"
    assert commands[0].should_write is True
    assert "destructive_or_overwrite" not in commands[0].safety_flags


def test_future_lawsuit_document_withdrawal_targets_tomorrow_plan():
    _, _, commands = _compile(
        "\u660e\u5929\u64a4\u56deXX\u6848\u8d77\u8bc9\u72b6",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-lawsuit-doc-plan",
            status="collecting",
            reply_candidate=True,
        ),
    )

    assert len(commands) == 1
    assert commands[0].operation == "fill"
    assert commands[0].target_field == "tomorrow_plan"
    assert commands[0].target_date == "tomorrow"


def test_active_daily_history_request_is_read_only():
    _, _, commands = _compile(
        "\u67e5\u5386\u53f2\u65e5\u62a5",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-5",
            status="collecting",
            reply_candidate=True,
        ),
    )

    assert len(commands) == 1
    assert commands[0].operation == "query_history"
    assert commands[0].target_field == "none"
    assert commands[0].should_write is False


def test_non_daily_message_does_not_compile_daily_command():
    plan, gate, commands = _compile("\u516c\u53f8\u5370\u7ae0\u501f\u7528\u6d41\u7a0b\u662f\u4ec0\u4e48\uff1f")

    assert plan.primary_workflow == "internal_qa"
    assert gate.allow_legacy_daily is False
    assert commands == []


def test_active_daily_context_write_eligibility_blocks_unstructured_feedback():
    for text in [
        "\u8fd9\u4e2a\u673a\u5668\u4eba\u6709\u70b9\u50bb",
        "\u4eca\u5929\u592a\u7d2f\u4e86",
        "\u4f60\u521a\u521a\u5199\u9519\u4e86\u5427",
    ]:
        plan, _, commands = _compile(
            text,
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-eligibility-block",
                status="collecting",
                reply_candidate=True,
            ),
        )

        if commands:
            assert len(commands) == 1
            assert commands[0].operation == "no_write"
            assert commands[0].should_write is False
            assert "daily_write_eligibility_blocked" in commands[0].safety_flags
        else:
            assert plan.primary_workflow == "chat"


def test_active_daily_context_write_eligibility_keeps_structured_daily_actions():
    cases = [
        ("\u4eca\u5929\u5b8c\u6210\u5408\u540c\u5ba1\u6838", "fill", "today_work"),
        ("\u4e1a\u52a1\u90e8\u95e8\u6750\u6599\u4e00\u76f4\u6ca1\u53cd\u9988", "fill", "problems"),
        ("\u4eca\u5929\u548c\u524d\u5929\u4e00\u6837", "copy_previous", "today_work"),
        ("\u6628\u5929\u7684\u660e\u65e5\u8ba1\u5212\u5df2\u5b8c\u6210", "complete_previous_plan", "today_work"),
        ("\u628a\u7b2c2\u6761\u5408\u540c\u5ba1\u6838\u6539\u6210\u5408\u540c\u590d\u6838", "edit", "today_work"),
    ]
    for text, operation, target_field in cases:
        _, _, commands = _compile(
            text,
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-eligibility-allow",
                status="collecting",
                reply_candidate=True,
            ),
        )

        assert len(commands) == 1
        assert commands[0].operation == operation
        assert commands[0].target_field == target_field
        assert commands[0].should_write is True


def test_active_daily_v4_followup_boundaries_and_content_cleanup():
    active_task = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-v4-followup",
        status="collecting",
        reply_candidate=True,
    )

    _, _, commands = _compile("今天事情挺多的", active_task)
    assert commands == [] or all(command.should_write is False for command in commands)

    _, _, commands = _compile("累死了，不想干活，明天再说吧", active_task)
    assert commands == [] or all(command.should_write is False for command in commands)

    _, _, commands = _compile("谈了合作意向", active_task)
    assert len(commands) == 1
    assert commands[0].operation == "fill"
    assert commands[0].target_field == "today_work"
    assert commands[0].should_write is True

    _, _, commands = _compile("再加上一个『修复线上bug』", active_task)
    assert len(commands) == 1
    assert commands[0].content == ["修复线上bug"]

    _, _, commands = _compile("有个风险就是审核系统响应慢，可能影响时效", active_task)
    assert len(commands) == 1
    assert commands[0].target_field == "problems"
    assert commands[0].content == ["审核系统响应慢，可能影响时效"]

    _, _, commands = _compile("预计明天上午写完周报初稿", active_task)
    assert len(commands) == 1
    assert commands[0].target_field == "tomorrow_plan"
    assert commands[0].content == ["写完周报初稿"]

    _, _, commands = _compile("今天又被老板怼了，好烦", active_task)
    assert commands == [] or all(command.should_write is False for command in commands)

    _, _, commands = _compile("今天忙到飞起，光接电话就接了20个", active_task)
    assert commands == [] or all(command.should_write is False for command in commands)

    _, _, commands = _compile("今天周五啦，晚上去哪嗨", active_task)
    assert commands == [] or all(command.should_write is False for command in commands)

    _, _, commands = _compile("月报提交了，本月处理案件12件", active_task)
    assert commands == [] or all(command.should_write is False for command in commands)

    _, _, commands = _compile("明天得重新设计方案，先做技术调研", active_task)
    assert [command.target_field for command in commands if command.should_write] == ["tomorrow_plan", "tomorrow_plan"]


def test_daily_shadow_allows_coordination_entry_to_resolve_low_confidence_legacy_effect():
    envelope = IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="Test User",
        dingtalk_user_id="ding-user-1",
        source="test",
        raw_text="上午和甲方吵了半天终于把需求定下来了，下午把合同条款理了一遍",
        active_tasks=(
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-v4-coordination",
                status="collecting",
                reply_candidate=True,
            ),
        ),
    )

    evaluation = evaluate_daily_shadow(envelope, mode="protective_gate")

    assert evaluation.gate_decision.block_legacy_daily is False
    assert any(command.should_write for command in evaluation.commands)
    assert [command.content for command in evaluation.commands if command.should_write] == [["合同条款理一遍"]]


def test_active_daily_context_cleans_mixed_lifestyle_and_business_content():
    _, _, commands = _compile(
        "\u4eca\u5929\u6211\u5403\u4e86\u5c0f\u756a\u8304 \u8bc4\u5ba1\u4e86\u6cd5\u52a1\u5408\u540c",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-mixed-content",
            status="collecting",
            reply_candidate=True,
        ),
    )

    assert len(commands) == 1
    assert commands[0].operation == "fill"
    assert commands[0].target_field == "today_work"
    assert commands[0].content == ["\u8bc4\u5ba1\u6cd5\u52a1\u5408\u540c"]
    assert commands[0].should_write is True


def test_active_daily_context_polishes_colloquial_business_content():
    _, _, commands = _compile(
        "\u4eca\u5929\u6211\u5403\u4e86\u5c0f\u756a\u8304\uff0c\u7136\u540e\u8bc4\u5ba1\u4e86\u6cd5\u52a1\u5408\u540c\uff0c\u8fd8\u6c9f\u901a\u4e86\u5370\u7ae0\u6d41\u7a0b",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-polish-content",
            status="collecting",
            reply_candidate=True,
        ),
    )

    assert len(commands) == 2
    assert [(command.target_field, command.content) for command in commands] == [
        ("today_work", ["\u8bc4\u5ba1\u6cd5\u52a1\u5408\u540c"]),
        ("today_work", ["\u6c9f\u901a\u5370\u7ae0\u6d41\u7a0b"]),
    ]


def test_daily_content_strips_report_instruction_prefix():
    _, _, commands = _compile(
        "那今天的日报就写：准备出差材料，还整理了出差要用到的合同模板",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-prefix-cleaning",
            status="collecting",
            reply_candidate=True,
        ),
    )

    assert [command.content for command in commands] == [
        ["准备出差材料"],
        ["整理出差要用到的合同模板"],
    ]


def test_coordination_daily_entry_cannot_bypass_first_layer_no_write_route():
    raw_text = "\u60f3\u8bf4\u4e2a\u6848\u4ef6\u8fdb\u5c55"
    envelope = IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="Test User",
        dingtalk_user_id="ding-user-1",
        source="test",
        raw_text=raw_text,
    )
    plan = WorkflowRouter().plan(envelope)
    coordination_plan = compile_coordination_plan(envelope)

    assert plan.primary_workflow in {"unknown_or_help", "chat"}
    assert ACTION_DAILY_ENTRY not in [action.action_type for action in coordination_plan.actions]
    assert compile_daily_commands(plan, envelope, coordination_plan=coordination_plan) == []


def test_active_daily_spoken_correction_compiles_to_edit_command():
    _, _, commands = _compile(
        "明日计划里面的飞速收款飞速写错了，是非的非诉讼的诉",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-spoken-correction",
            status="collecting",
            reply_candidate=True,
        ),
    )

    assert len(commands) == 1
    assert commands[0].operation == "edit"
    assert commands[0].target_field == "tomorrow_plan"
    assert commands[0].should_write is True
    assert "destructive_or_overwrite" not in commands[0].safety_flags


def test_active_daily_parenthetical_delete_compiles_to_edit_not_clear():
    _, _, commands = _compile(
        "括号里面的内容删掉",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-parenthetical-delete",
            status="collecting",
            reply_candidate=True,
        ),
    )

    assert len(commands) == 1
    assert commands[0].operation == "edit"
    assert commands[0].should_write is True
    assert "destructive_or_overwrite" not in commands[0].safety_flags


def test_field_hint_plus_parenthetical_delete_stays_single_edit_command():
    _, _, commands = _compile(
        "今日工作里面的上海鸡仔。是对的，括号里面的内容删掉",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-parenthetical-delete-with-hint",
            status="collecting",
            reply_candidate=True,
        ),
    )

    assert len(commands) == 1
    assert commands[0].operation == "edit"
    assert commands[0].target_field == "today_work"


def test_active_daily_problem_reply_strips_problem_prefix():
    _, _, commands = _compile(
        "\u95ee\u9898\u7684\u8bdd\uff0c\u5ba2\u6237\u53cd\u9988\u5ef6\u8fdf",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-problem-prefix",
            status="collecting",
            reply_candidate=True,
        ),
    )

    assert len(commands) == 1
    assert commands[0].target_field == "problems"
    assert commands[0].content == ["\u5ba2\u6237\u53cd\u9988\u5ef6\u8fdf"]


def test_active_daily_continuation_and_tomorrow_plan_shell_are_cleaned():
    _, _, work_commands = _compile(
        "\u7ee7\u7eed\u62a5\uff0c\u4e0b\u5348\u5ba1\u4e86\u4e24\u4efd\u5408\u540c\uff0c\u4e00\u4efd\u662f\u6052\u5927\u9152\u5e97\u7684\u3002",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-continuation",
            status="collecting",
            reply_candidate=True,
        ),
    )
    _, _, plan_commands = _compile(
        "\u660e\u5929\u7684\u8ba1\u5212\u5c31\u662f\u628a\u4e89\u8bae\u70b9\u7406\u51fa\u6765\uff0c\u518d\u7ea6\u5bf9\u65b9\u6cd5\u52a1\u78b0\u4e00\u4e0b\u3002",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-plan-shell",
            status="collecting",
            reply_candidate=True,
        ),
    )

    work_write = [command for command in work_commands if command.should_write]
    plan_write = [command for command in plan_commands if command.should_write]
    assert work_write
    assert work_write[0].target_field == "today_work"
    assert all("\u7ee7\u7eed\u62a5" not in item for item in work_write[0].content)
    assert plan_write
    assert plan_write[0].target_field == "tomorrow_plan"
    assert all(not item.startswith("\u660e\u5929\u7684\u8ba1\u5212\u5c31\u662f") for item in plan_write[0].content)


def test_active_daily_blocks_vague_completion_and_referential_risk_reminder():
    active = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-vague-reference",
        status="collecting",
        reply_candidate=True,
    )

    _, _, vague_commands = _compile("\u4eca\u5929\u90a3\u4e2a\u4e8b\u641e\u5b9a\u4e86", active)
    _, _, reminder_commands = _compile("\u521a\u624d\u8bf4\u7684\u90a3\u4e2a\u98ce\u9669\u8bb0\u5f97\u5199\u4e0a\u54c8", active)

    assert all(not command.should_write for command in vague_commands)
    assert all(not command.should_write for command in reminder_commands)


def test_active_daily_accepts_visit_followup_and_system_failure_problem():
    active = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-visit-system",
        status="collecting",
        reply_candidate=True,
    )
    _, _, visit_commands = _compile("\u4eca\u5929\u62dc\u8bbf\u4e86\u5ba2\u6237A\uff0c\u804a\u5f97\u8fd8\u884c\uff0c\u660e\u5929\u63a5\u7740\u8c08\u7ec6\u8282", active)
    _, _, failure_commands = _compile("\u5bf9\u4e86\uff0c\u4e0b\u5348\u7cfb\u7edf\u5d29\u4e86\u534a\u5c0f\u65f6\uff0c\u5dee\u70b9\u6ca1\u4fdd\u5b58\u3002", active)

    visit_writes = [command for command in visit_commands if command.should_write]
    failure_writes = [command for command in failure_commands if command.should_write]

    assert any(command.target_field == "today_work" for command in visit_writes)
    assert any(command.target_field == "tomorrow_plan" for command in visit_writes)
    assert any(command.target_field == "problems" for command in failure_writes)


def test_active_daily_normalizes_previous_risk_resolved_problem():
    _, _, commands = _compile(
        "\u8fd8\u6709\uff0c\u6628\u5929\u8bf4\u7684\u90a3\u4e2a\u98ce\u9669\u5df2\u7ecf\u89e3\u9664\u4e86\u3002",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-risk-resolved",
            status="collecting",
            reply_candidate=True,
        ),
    )

    writes = [command for command in commands if command.should_write]
    assert writes
    assert writes[0].target_field == "today_work"
    assert len(writes[0].content) == 1
    assert "\u98ce\u9669" in writes[0].content[0]
    assert "\u89e3\u9664" in writes[0].content[0]


def test_daily_command_blocks_short_vague_completion_without_object():
    _, _, commands = _compile("\u4eca\u5929\u641e\u5b9a\u4e86")

    assert all(not command.should_write for command in commands)


def test_daily_command_blocks_non_substantive_daily_request():
    _, _, commands = _compile("\u7b97\u4e86\u4e0d\u8ddf\u4f60\u804a\u4e86\uff0c\u4f60\u5565\u4e5f\u4e0d\u61c2\uff0c\u968f\u4fbf\u5199\u4e2a\u65e5\u62a5\u4ea4\u5dee\u5427")

    assert all(not command.should_write for command in commands)


def test_daily_command_blocks_bare_daily_start_and_vague_continuation():
    for text in [
        "\u5199\u4e2a\u65e5\u62a5",
        "\u65e5\u62a5",
        "\u522b\u7684\u6ca1\u4e86\uff0c\u660e\u5929\u7ee7\u7eed",
        "\u660e\u5929\u7ee7\u7eed",
        "\u4eca\u5929\u8fd8\u662f\u90a3\u6837\uff0c\u6ca1\u5565\u7279\u522b\u7684",
    ]:
        _, _, commands = _compile(text)

        assert all(not command.should_write for command in commands)


def test_daily_command_records_trip_plan_when_process_question_is_mixed_in():
    _, _, commands = _compile("\u5bf9\u4e86\uff0c\u54b1\u4eec\u516c\u53f8\u7684\u7528\u5370\u6d41\u7a0b\u600e\u4e48\u8d70\uff1f\u660e\u5929\u8fd8\u8981\u53bb\u5357\u4eac\u51fa\u5dee\u76d6\u7ae0\u3002")

    writes = [command for command in commands if command.should_write]
    assert len(writes) == 1
    assert writes[0].target_field == "tomorrow_plan"
    assert writes[0].content == ["\u53bb\u5357\u4eac\u51fa\u5dee\u76d6\u7ae0"]


def test_daily_command_blocks_questions_reminders_case_progress_and_vague_status():
    for text in [
        "\u660e\u5929\u5565\u5b89\u6392\uff1f",
        "\u6052\u5927\u6848\u6709\u65b0\u8fdb\u5c55\uff0c\u521a\u6536\u5230\u6cd5\u9662\u4f20\u7968",
        "\u4eca\u5929\u7684\u4e8b\u90fd\u5904\u7406\u5b8c\u4e86",
        "\u6628\u5929\u505a\u7684\u9700\u6c42\u8bc4\u5ba1\u7ed3\u679c\u51fa\u6765\u4e86\uff0c\u901a\u8fc7\u4e86\u3002",
        "\u8bb0\u5f97\u63d0\u9192\u6211\u660e\u5929\u4ea4\u5468\u62a5",
        "\u4eca\u5929\u597d\u7d2f\uff0c\u5fd9\u4e86\u4e00\u5929\uff0c\u5148\u4e0b\u73ed\u4e86\uff0c\u660e\u5929\u518d\u7814\u7a76\u8fd9\u4e9b",
    ]:
        _, _, commands = _compile(text)

        assert all(not command.should_write for command in commands)


def test_daily_command_cleans_generic_problem_intro():
    _, _, commands = _compile("\u9047\u5230\u4e00\u4e2a\u95ee\u9898\uff0c\u670d\u52a1\u5668\u6302\u4e86")

    writes = [command for command in commands if command.should_write]
    assert writes
    assert writes[0].target_field == "problems"
    assert writes[0].content == ["\u670d\u52a1\u5668\u6302\u4e86"]


def test_daily_command_blocks_daily_meta_request_without_work():
    for text in [
        "今天忙成狗，日报都不想写了",
        "今天写日报了没？帮我把今天的活儿记一下",
    ]:
        _, _, commands = _compile(text)

        assert all(not command.should_write for command in commands)


def test_daily_command_cleans_prior_intent_completion_to_actual_task():
    _, _, commands = _compile("昨天我说今天要去见客户，已经见完了")

    writes = [command for command in commands if command.should_write]
    assert writes
    assert writes[0].target_field == "today_work"
    assert writes[0].content == ["见客户"]


def test_daily_command_blocks_business_reference_question_without_date():
    _, _, commands = _compile("那个合同里的争议解决条款是不是改过？我记得之前写的是仲裁")

    assert all(not command.should_write for command in commands)


def test_daily_command_keeps_mixed_case_and_daily_sentence_as_daily_writes():
    _, _, commands = _compile(
        "今天上午跟保利案的开庭调解，下午把恒大项目的合同审完了，还发现有个风险条款，明天得和业务部门开个会讨论下，另外下班前把周报发了"
    )

    writes = [command for command in commands if command.should_write]
    assert any(command.target_field == "today_work" and "保利案" in command.content[0] for command in writes)
    assert any(command.target_field == "today_work" and "恒大项目" in command.content[0] for command in writes)
    assert any(command.target_field == "problems" and "风险条款" in command.content[0] for command in writes)
    assert any(command.target_field == "tomorrow_plan" and "业务部门" in command.content[0] for command in writes)


def test_daily_command_handles_v4_case_schedule_and_abandon_boundaries():
    _, _, hearing_commands = _compile("保利案明天要开庭了，得准备材料。")
    _, _, summons_commands = _compile("恒大案进度：今天刚拿到法院传票，下周三开庭")
    _, _, abandon_commands = _compile("算了不写了，明天写")
    _, _, mixed_previous_commands = _compile("昨天的明日计划已完成，今天继续推进保利案")

    assert any(command.should_write and command.target_field == "tomorrow_plan" and "准备材料" in command.content[0] for command in hearing_commands)
    assert any(command.should_write and command.target_field == "today_work" and "法院传票" in command.content[0] for command in summons_commands)
    assert all(not command.should_write for command in abandon_commands)
    assert any(command.should_write and command.target_field == "today_work" and command.content == ["继续推进保利案"] for command in mixed_previous_commands)


def test_daily_command_treats_document_research_done_as_daily_but_query_as_qa():
    _, _, daily_commands = _compile("今天查阅恒大案件资料")
    _, _, qa_commands = _compile("帮我查一下王喜被告案件有多少")

    assert len(daily_commands) == 1
    assert daily_commands[0].should_write is True
    assert daily_commands[0].target_field == "today_work"
    assert daily_commands[0].content == ["查阅恒大案件资料"]
    assert qa_commands == []


def test_daily_command_copy_previous_tomorrow_plan_targets_tomorrow_plan():
    _, _, commands = _compile("明天的计划就复制昨天的明日计划吧")

    assert len(commands) == 1
    assert commands[0].operation == "copy_previous"
    assert commands[0].target_field == "tomorrow_plan"
    assert commands[0].should_write is True
    assert commands[0].content == []


def test_daily_command_handles_v4_followup_boundaries_without_agent1_pollution():
    _, _, sync_commands = _compile("明天把变更同步给开发")
    _, _, settlement_commands = _compile("那明天我拟一份和解协议发你。")
    _, _, commute_commands = _compile("今天地铁挤死了，还迟到了，烦")
    _, _, deadline_commands = _compile("催一下，明天就截止了")
    _, _, case_detail_commands = _compile("对，就是那个保利案的开庭，需要带案卷材料。")
    _, _, opinion_commands = _compile("今天下午去见客户了，谈得还行，但客户有点抠门")
    _, _, sweep_commands = _compile("昨天的明日计划已完成，今天就是扫尾")

    assert any(command.should_write and command.target_field == "tomorrow_plan" for command in sync_commands)
    assert any(command.should_write and command.target_field == "tomorrow_plan" for command in settlement_commands)
    assert commute_commands == []
    assert deadline_commands == []
    assert case_detail_commands == []
    written_opinion = " ".join(item for command in opinion_commands for item in command.content)
    assert "下午去见客户" in written_opinion
    assert "还行" not in written_opinion
    assert "抠门" not in written_opinion
    assert any(command.should_write and command.target_field == "today_work" and "扫尾" in "".join(command.content) for command in sweep_commands)


def test_daily_command_handles_v4_fresh60_regressions_without_pollution():
    active_daily = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-1",
        status="collecting",
        reply_candidate=True,
    )

    _, _, mixed_question_commands = _compile(
        "\u4eca\u5929\u5ba1\u4e86\u4fdd\u5229\u6848\u7684\u8865\u5145\u534f\u8bae\uff0c\u987a\u4fbf\u95ee\u4e0b\uff0c\u7528\u5370\u6d41\u7a0b\u8d70\u7ebf\u4e0a\u8fd8\u662f\u7ebf\u4e0b\uff1f\u660e\u5929\u7ea6\u4e86\u5ba2\u6237\u8c08\u548c\u89e3\u65b9\u6848\u3002",
        active_daily,
    )
    _, _, creative_commands = _compile("\u7ed9\u6211\u5199\u7bc7\u79d1\u5e7b\u5c0f\u8bf4\uff0c\u660e\u5929\u5c31\u8981\u3002", active_daily)
    _, _, document_problem_commands = _compile(
        "\u54e6\u5bf9\u4e86\uff0c\u9014\u4e2d\u53d1\u73b0\u5224\u51b3\u4e66\u6709\u4e2a\u65e5\u671f\u5199\u9519\u4e86\uff0c\u5f97\u901a\u77e5\u6cd5\u9662\u66f4\u6b63\u3002",
        active_daily,
    )
    _, _, contextual_plan_commands = _compile(
        "\u8fd9\u4e2a\u4e8b\u660e\u5929\u8fd8\u5f97\u7ee7\u7eed\uff0c\u5148\u5199\u5230\u660e\u65e5\u8ba1\u5212\u91cc\u3002",
        active_daily,
    )
    _, _, completed_previous_commands = _compile(
        "\u6628\u5929\u7684\u660e\u65e5\u8ba1\u5212\u5df2\u5b8c\u6210\uff0c\u4eca\u5929\u4e3b\u8981\u5904\u7406\u9057\u7559\u95ee\u9898\u3002",
        active_daily,
    )
    _, _, lifestyle_question_commands = _compile(
        "\u4eca\u5929\u7b80\u76f4\u5fd9\u6b7b\u4e86\uff0c\u5ba1\u4e865\u4e2a\u5408\u540c\uff0c\u8fd8\u5904\u7406\u4e86\u5ba2\u6237\u6295\u8bc9\uff0c\u665a\u4e0a\u805a\u9910\u53bb\u4e0d\u53bb\uff1f",
        active_daily,
    )
    _, _, report_edit_commands = _compile("\u5c31\u662f\u628a\u6628\u5929\u7684\u62a5\u544a\u6539\u4e00\u4e0b\uff0c\u7b2c\u4e09\u9875\u7684\u6570\u636e\u66f4\u65b0\u4e86\u3002", active_daily)
    _, _, case_strategy_commands = _compile("\u90a3\u4e2a\u6848\u5b50\u7684\u7b56\u7565\u6211\u660e\u5929\u8981\u8ddf\u8001\u677f\u518d\u786e\u8ba4\u4e00\u4e0b\u3002", active_daily)
    _, _, printer_commands = _compile("\u70e6\u6b7b\u4e86\uff0c\u4eca\u5929\u6253\u5370\u673a\u53c8\u574f\u4e86")
    _, _, monthly_meta_commands = _compile("\u8d76\u7d27\u5199\u6708\u62a5\uff0c\u8fd9\u4e2a\u6708\u5feb\u8fc7\u5b8c\u4e86")
    _, _, bot_feedback_commands = _compile("\u70e6\u6b7b\u4e86\uff0c\u9886\u5bfc\u53c8\u6539\u9700\u6c42\uff0c\u8fd9\u65e5\u62a5\u7cfb\u7edf\u771f\u96be\u7528\u3002")
    _, _, no_new_plan_commands = _compile("\u6628\u5929\u7684\u660e\u65e5\u8ba1\u5212\u6211\u5df2\u7ecf\u505a\u5b8c\u4e86\uff0c\u4eca\u5929\u6ca1\u4ec0\u4e48\u65b0\u8ba1\u5212\u3002")

    mixed_text = "\n".join(item for command in mixed_question_commands for item in command.content)
    assert any(command.should_write and command.target_field == "today_work" and "\u8865\u5145\u534f\u8bae" in "".join(command.content) for command in mixed_question_commands)
    assert any(command.should_write and command.target_field == "tomorrow_plan" and "\u548c\u89e3\u65b9\u6848" in "".join(command.content) for command in mixed_question_commands)
    assert "\u7528\u5370\u6d41\u7a0b" not in mixed_text

    assert all(not command.should_write for command in creative_commands)
    assert any(command.should_write and command.target_field == "problems" and "\u5224\u51b3\u4e66" in "".join(command.content) for command in document_problem_commands)
    assert any(command.should_write and command.target_field == "tomorrow_plan" and command.content == ["\u7ee7\u7eed\u8ddf\u8fdb\u524d\u8ff0\u4e8b\u9879"] for command in contextual_plan_commands)
    assert any(command.should_write and command.operation == "fill" and command.target_field == "today_work" and "\u9057\u7559\u95ee\u9898" in "".join(command.content) for command in completed_previous_commands)

    lifestyle_text = "\n".join(item for command in lifestyle_question_commands for item in command.content)
    assert any(command.should_write and "\u5408\u540c" in "".join(command.content) for command in lifestyle_question_commands)
    assert any(command.should_write and "\u5ba2\u6237\u6295\u8bc9" in "".join(command.content) for command in lifestyle_question_commands)
    assert "\u805a\u9910" not in lifestyle_text
    assert all(not command.should_write for command in report_edit_commands)
    assert any(command.should_write and command.target_field == "tomorrow_plan" and "\u7b56\u7565" in "".join(command.content) for command in case_strategy_commands)
    assert all(not command.should_write for command in printer_commands)
    assert all(not command.should_write for command in monthly_meta_commands)
    assert all(not command.should_write for command in bot_feedback_commands)
    assert all(not command.should_write for command in no_new_plan_commands)


def test_daily_command_handles_round8_context_and_service_boundaries():
    _, _, risk_commands = _compile("那个庭可能要延期，法官临时有事，风险")
    _, _, report_commands = _compile("刚才说的那个报告其实是昨天的工作，今天要交，我现在写")
    _, _, material_commands = _compile("对了，还有恒大案的资料也看了一部分")
    _, _, email_commands = _compile("再加一个，给客户发了邮件。")
    _, _, arrange_commands = _compile("顺便安排下会见XX酒店项目的人。")
    _, _, plan_commands = _compile("那明天的计划还是见客户")

    assert any(command.should_write and command.target_field == "problems" and "延期" in "".join(command.content) for command in risk_commands)
    assert any(command.should_write and command.target_field == "today_work" and "报告" in "".join(command.content) for command in report_commands)
    assert any(command.should_write and command.target_field == "today_work" and "资料" in "".join(command.content) for command in material_commands)
    assert any(command.should_write and command.target_field == "today_work" and "邮件" in "".join(command.content) for command in email_commands)
    assert arrange_commands == []
    assert any(command.should_write and command.target_field == "tomorrow_plan" and command.content == ["见客户"] for command in plan_commands)


def test_daily_command_handles_round9_llm_smoke_boundaries():
    _, _, defer_commands = _compile("我先下班了啊，日报明天再说吧")
    _, _, no_report_commands = _compile("今天日报先不写了。")
    _, _, hot_commands = _compile("算了，今天热死了，不想动")
    _, _, historical_commands = _compile("把今天的工作也加到昨天日报里")
    _, _, business_problem_commands = _compile("还有个问题，客户说合同金额不对")
    _, _, tomorrow_customer_commands = _compile("明天去南京见客户")

    assert defer_commands == []
    assert no_report_commands == []
    assert hot_commands == []
    assert not any(command.should_write for command in historical_commands)
    assert any(command.should_write and command.target_field == "problems" for command in business_problem_commands)
    assert any(command.should_write and command.target_field == "tomorrow_plan" and command.content == ["去南京见客户"] for command in tomorrow_customer_commands)


def test_daily_command_accepts_concrete_untimed_work_and_meeting_work():
    _, _, contract_commands = _compile("\u5c31\u662f\u7ee7\u7eed\u5ba1\u90a3\u4e2a\u91c7\u8d2d\u5408\u540c\uff0c\u5feb\u641e\u5b8c\u4e86")
    _, _, meeting_commands = _compile("\u5c31\u662f\u5f00\u4e86\u4e24\u4e2a\u4f1a\uff0c\u90fd\u662f\u5e38\u89c4\u7684\uff0c\u6ca1\u98ce\u9669\u3002")

    assert any(command.should_write and command.target_field == "today_work" for command in contract_commands)
    assert any(command.should_write and command.target_field == "today_work" for command in meeting_commands)


def test_active_daily_accepts_contextual_business_detail_and_copy_quote_cleanly():
    active = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-context-detail",
        status="collecting",
        reply_candidate=True,
    )
    _, _, detail_commands = _compile("\u54e6\u5bf9\u4e86\uff0c\u7ade\u54c1\u5206\u6790\u90a3\u5757\uff0c\u8fd8\u5305\u542b\u4e86\u8ddf\u963f\u91cc\u4e91\u7684\u5bf9\u6bd4\u3002", active)
    _, _, copy_commands = _compile("\u7136\u540e\u518d\u628a\u6628\u5929\u7684\u65e5\u62a5\u91cc\u201c\u5b8c\u6210\u6570\u636e\u5206\u6790\u201d\u8fd9\u9879\u590d\u5236\u8fc7\u6765\u3002", active)

    detail_writes = [command for command in detail_commands if command.should_write]
    copy_writes = [command for command in copy_commands if command.should_write]
    assert detail_writes
    assert detail_writes[0].target_field == "today_work"
    assert copy_writes
    assert copy_writes[0].content == ["\u5b8c\u6210\u6570\u636e\u5206\u6790"]


def test_daily_command_blocks_future_trip_followup_vague_plan_and_test_probe():
    samples = [
        "\u540e\u5929\u53bb\u5357\u4eac\u51fa\u5dee\uff0c\u8981\u53bb\u5ba2\u6237\u90a3\u8fb9\u5904\u7406\u5408\u540c\u7ea0\u7eb7\uff0c\u987a\u4fbf\u76d6\u7ae0",
        "\u8fd8\u6ca1\u505a\uff0c\u90a3\u660e\u5929\u518d\u505a",
        "test1234444 \u8ba9\u6211\u6d4b\u8bd5\u4e0b\u65e5\u62a5\u529f\u80fd \u4eca\u5929\u5565\u4e5f\u6ca1\u5e72 \u5c31\u662f\u6478\u9c7c",
    ]

    for text in samples:
        _, _, commands = _compile(text)
        assert all(not command.should_write for command in commands)


def test_daily_command_handles_round10_context_edit_and_mixed_qa_boundaries():
    active = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-round10",
        status="collecting",
        reply_candidate=True,
    )

    _, _, vague_plan_commands = _compile("\u660e\u5929\u7ee7\u7eed\u5f04\u8fd9\u4e2a", active)
    _, _, previous_plan_commands = _compile("\u6628\u5929\u7684\u660e\u65e5\u8ba1\u5212\u5df2\u5b8c\u6210\uff0c\u90a3\u4e2a\u5408\u540c\u76d6\u7ae0\u4eca\u5929\u641e\u5b9a\u4e86", active)
    _, _, quote_commands = _compile("\u7136\u540e\u518d\u628a\u4eca\u5929\u7684\u65e5\u62a5\u91cc\u52a0\u4e0a\u2018\u63a8\u52a8\u9879\u76ee\u2019", active)
    _, _, copy_field_commands = _compile("\u6700\u540e\u628a\u660e\u5929\u7684\u8ba1\u5212\u590d\u5236\u4e00\u4efd\u5230\u4eca\u5929\u7684\u8ba1\u5212\u91cc", active)
    _, _, meeting_commands = _compile("\u521a\u624d\u8bf4\u7684\u90a3\u4e9b\u518d\u52a0\u4e00\u4e2a\u4e0b\u53483\u70b9\u90e8\u95e8\u5468\u4f1a", active)
    _, _, merge_commands = _compile("\u628a\u524d\u4e24\u6761\u5408\u6210\u4e00\u6761\u53eb\u2018\u9700\u6c42\u8bc4\u5ba1\u4e0e\u4ee3\u7801\u4f18\u5316\u2019", active)
    _, _, copy_item_commands = _compile("\u518d\u628a\u8fd9\u6761\u590d\u5236\u4e00\u904d\u52a0\u5230\u660e\u65e5\u8ba1\u5212\u91cc", active)
    _, _, mixed_commands = _compile("\u4eca\u5929\u641e\u5b9a\u4e86\u6052\u5927\u6848\u7b54\u8fa9\uff0c\u987a\u4fbf\u95ee\u4e0b\uff0c\u7528\u5370\u7533\u8bf7\u90a3\u4e2a\u8868\u5728\u54ea\u91cc\u4e0b\uff1f\u660e\u5929\u8fd8\u8981\u63a5\u7740\u6574\u8bc1\u636e\u6e05\u5355", active)
    _, _, previous_task_commands = _compile("\u6628\u5929\u65e5\u62a5\u91cc\u5199\u7684\u90a3\u4e2a\u62dc\u8bbf\u8ba1\u5212\u4eca\u5929\u641e\u5b8c\u4e86", active)
    _, _, mixed_life_commands = _compile("\u660e\u5929\u518d\u5f04\u5565\uff1f\u4eca\u5929\u628a\u5ba2\u6237\u6750\u6599\u4e5f\u6574\u597d\u4e86\uff0c\u7d2f\u6b7b", active)

    assert any(command.should_write and command.target_field == "tomorrow_plan" and command.content == ["\u7ee7\u7eed\u8ddf\u8fdb\u524d\u8ff0\u4e8b\u9879"] for command in vague_plan_commands)
    assert any(command.should_write and command.target_field == "today_work" and "合同盖章" in "".join(command.content) for command in previous_plan_commands)
    assert any(command.should_write and command.content == ["\u63a8\u52a8\u9879\u76ee"] for command in quote_commands)
    assert all(not command.should_write or command.operation == "edit" for command in copy_field_commands)
    assert any(command.should_write and command.target_field == "today_work" and "部门周会" in "".join(command.content) for command in meeting_commands)
    assert all(not command.should_write or command.operation == "edit" for command in merge_commands)
    assert all(not command.should_write or command.operation == "edit" for command in copy_item_commands)
    assert any(command.should_write and command.target_field == "today_work" and "恒大案答辩" in "".join(command.content) for command in mixed_commands)
    assert any(command.should_write and command.target_field == "tomorrow_plan" and "证据清单" in "".join(command.content) for command in mixed_commands)
    assert any(command.should_write and target_content.startswith("\u62dc\u8bbf\u8ba1\u5212") for command in previous_task_commands for target_content in command.content)
    assert any(command.should_write and command.target_field == "today_work" and "客户材料" in "".join(command.content) for command in mixed_life_commands)
    assert all("用印申请" not in "".join(command.content) for command in mixed_commands)


def test_daily_command_blocks_vague_done_and_cleans_time_ba_prefix():
    _, _, vague_done_commands = _compile("\u641e\u5b9a\u4e86\uff0c\u4eca\u5929\u5b8c\u4e8b\u3002")
    _, _, module_commands = _compile("\u4e0b\u5348\u628a\u767b\u5f55\u6a21\u5757\u5199\u5b8c\u4e86\uff0c\u660e\u5929\u8ba1\u5212\u5199\u6ce8\u518c\u6a21\u5757\u3002")
    _, _, cold_commands = _compile("\u4eca\u5929\u597d\u51b7\u554a\uff0c\u4f60\u5728\u5e72\u561b\u5462\uff1f")
    _, _, dont_write_commands = _compile("\u54e6\uff0c\u5176\u5b9e\u4eca\u5929\u6478\u9c7c\u4e86\uff0c\u4f46\u522b\u5199\u4e0a\u53bb\u54c8\u3002")
    _, _, empty_test_commands = _compile("\u6d4b\u8bd5\u4e00\u4e0b\uff0c\u5199\u65e5\u62a5\uff1a\u4eca\u5929\u6ca1\u5565\u4e8b")

    assert all(not command.should_write for command in vague_done_commands)
    assert all(not command.should_write for command in cold_commands)
    assert all(not command.should_write for command in dont_write_commands)
    assert all(not command.should_write for command in empty_test_commands)
    assert any(command.should_write and command.target_field == "today_work" and command.content == ["\u767b\u5f55\u6a21\u5757\u5199\u5b8c"] for command in module_commands)
    assert any(command.should_write and command.target_field == "tomorrow_plan" and command.content == ["\u5199\u6ce8\u518c\u6a21\u5757"] for command in module_commands)


def test_daily_command_handles_previous_plan_done_with_new_meeting_and_risk_followup():
    active = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-round12",
        status="collecting",
        reply_candidate=True,
    )
    _, _, meeting_commands = _compile("\u6628\u5929\u7684\u660e\u65e5\u8ba1\u5212\u5df2\u7ecf\u5b8c\u6210\u4e86\uff0c\u4eca\u5929\u4e3b\u8981\u662f\u5f00\u4e86\u4e2a\u65b0\u9879\u76ee\u542f\u52a8\u4f1a\u3002", active)
    _, _, risk_commands = _compile("\u521a\u624d\u8bf4\u7684\u4fdd\u5229\u6848\uff0c\u98ce\u9669\u70b9\u662f\u8bc1\u636e\u53ef\u80fd\u4e0d\u8db3\uff0c\u8fd9\u4e2a\u8981\u5199\u4e0a\u3002", active)

    assert any(command.should_write and command.target_field == "today_work" and "\u65b0\u9879\u76ee\u542f\u52a8\u4f1a" in "".join(command.content) for command in meeting_commands)
    assert any(command.should_write and command.target_field == "problems" and "\u8bc1\u636e\u53ef\u80fd\u4e0d\u8db3" in "".join(command.content) for command in risk_commands)


def test_daily_command_v4_blocks_meta_history_routing_and_rant_inputs():
    for text in [
        "\u597d\u4e86\u5c31\u8fd9\u4e9b\uff0c\u53d1\u65e5\u62a5\u5427",
        "\u5e94\u8be5\u5c31\u8fd9\u4e9b\u4e86\uff0c\u5199\u65e5\u62a5\u5427",
        "\u6628\u5929\u6211\u505a\u4e86\u65b9\u6848A\u3002",
        "\u8fd9\u7cfb\u7edf\u53c8\u5d29\u4e86\uff0c\u6d6a\u8d39\u6211\u65f6\u95f4",
        "\u628a\u521a\u624d\u8bf4\u7684\u8bb0\u5230\u6848\u4ef6\u8fdb\u5c55\u91cc",
        "\u8fd9\u4e2a\u4e0d\u7528\u5199\u65e5\u62a5\uff0c\u53ea\u662f\u540c\u6b65\u4e00\u4e0b",
        "\u54c8\u54c8\u54c8\u54c8\u54c8\u6d4b\u8bd5\u4e00\u4e0b\uff0c\u5199\u4e2a\u65e5\u62a5\uff1a\u4eca\u5929\u54c8\u54c8\u54c8\u3002\u660e\u5929\u5475\u5475\u5475\u3002",
        "\u4eca\u5929\u505a\u5408\u540c\u5ba1\u6838\uff0c\u4e0d\u8fc7\u5148\u8ba9\u6211\u6d4b\u8bd5\u4e0b\uff0c\u4f60\u80fd\u5199\u65e5\u62a5\u5417",
    ]:
        _, _, commands = _compile(text)

        assert all(not command.should_write for command in commands)


def test_daily_command_blocks_concrete_historical_daily_delete_after_cutoff():
    _, _, commands = _compile("\u6628\u5929\u65e5\u62a5\u4eca\u65e5\u5de5\u4f5c\u7b2c2\u6761\u5220\u6389")

    assert commands
    assert all(not command.should_write for command in commands)
    assert commands[0].operation == "begin_edit"
    assert commands[0].target_field == "none"
    assert commands[0].target_date == "yesterday"


def test_daily_command_v4_handles_hotel_project_edit_quantity_and_training_followup():
    active = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-v4-boundary",
        status="collecting",
        reply_candidate=True,
    )

    _, _, hotel_project_commands = _compile(
        "\u4eca\u5929\u641e\u5b9aXX\u9152\u5e97\u9879\u76ee\u5408\u540c\uff0c\u6ca1\u522b\u7684\u4e86\uff0c\u660e\u5929\u7ee7\u7eed\u3002",
        active,
    )
    _, _, quoted_delete_commands = _compile(
        "\u628a\u201c\u5199\u4e86\u5f88\u591a\u5b57\u201d\u5220\u6389\uff0c\u592a\u968f\u610f\u4e86\u3002",
        active,
    )
    _, _, quantity_correction_commands = _compile(
        "\u9519\u4e86\uff0c\u662f\u4e24\u4efd\uff0c\u6709\u4e00\u4efd\u662f\u6628\u5929\u5199\u7684",
        active,
    )
    _, _, training_time_commands = _compile(
        "\u57f9\u8bad\u5728\u4e0a\u534810\u70b9\u3002",
        active,
    )

    assert any(command.should_write and command.target_field == "today_work" and "\u9152\u5e97\u9879\u76ee\u5408\u540c" in "".join(command.content) for command in hotel_project_commands)
    assert all("\u6ca1\u522b\u7684" not in "".join(command.content) for command in hotel_project_commands)
    assert any(command.should_write and command.operation == "edit" for command in quoted_delete_commands)
    assert all(command.operation != "clear" for command in quoted_delete_commands)
    assert any(command.should_write and command.operation == "edit" for command in quantity_correction_commands)
    assert any(command.should_write and command.target_field == "today_work" and "\u57f9\u8bad\u5728\u4e0a\u534810\u70b9" in "".join(command.content) for command in training_time_commands)


def test_daily_command_v4_handles_mixed_questions_and_active_edit_boundaries():
    active = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-v4-mixed-boundary",
        status="collecting",
        reply_candidate=True,
    )

    _, _, date_question_commands = _compile(
        "\u4eca\u5929\u661f\u671f\u51e0\uff1f\u6211\u662f\u4e0d\u662f\u8be5\u5199\u65e5\u62a5\u4e86\uff1f",
        active,
    )
    _, _, absurd_commands = _compile(
        "\u4eca\u5929\u5de5\u4f5c\uff1a\u5403\u5c4e\uff0c\u95ee\u9898\uff1a\u6ca1\u5403\u9971\u3002",
        active,
    )
    _, _, mixed_weather_commands = _compile(
        "\u4eca\u5929\u5b8c\u6210\u4e86\u5408\u540c\u5ba1\u6838\uff0c\u987a\u4fbf\u95ee\u4e0b\u660e\u5929\u5929\u6c14\u600e\u4e48\u6837\uff0c\u65e5\u62a5\u91cc\u8fd8\u8981\u5199\u5565\uff1f",
        active,
    )
    _, _, problem_commands = _compile(
        "\u628a\u8fd9\u4e2a\u95ee\u9898\u4e5f\u8bb0\u4e0a\uff1a\u53d1\u73b0\u4e00\u4e2a\u5b89\u5168\u6f0f\u6d1e\u3002",
        active,
    )
    _, _, retraction_commands = _compile(
        "\u5f52\u6863\u90a3\u4e2a\u7b97\u4e86\u5427\uff0c\u4e0d\u662f\u4eca\u5929\u505a\u7684\uff0c\u5176\u5b9e\u6628\u5929\u5c31\u5f04\u5b8c\u4e86",
        active,
    )

    assert all(not command.should_write for command in date_question_commands)
    assert all(not command.should_write for command in absurd_commands)
    assert any(command.should_write and command.target_field == "today_work" and "\u5408\u540c\u5ba1\u6838" in "".join(command.content) for command in mixed_weather_commands)
    assert all("\u5929\u6c14" not in "".join(command.content) and "\u65e5\u62a5\u91cc" not in "".join(command.content) for command in mixed_weather_commands)
    assert any(command.should_write and command.target_field == "problems" and "\u5b89\u5168\u6f0f\u6d1e" in "".join(command.content) for command in problem_commands)
    assert any(command.should_write and command.operation == "edit" for command in retraction_commands)


def test_daily_command_v4_filters_system_bug_rant_and_accepts_confidential_retraction():
    active = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-v4-system-rant-retract",
        status="collecting",
        reply_candidate=True,
    )

    _, _, mixed_rant_commands = _compile(
        "\u4eca\u5929\u4e0a\u5348\u5f00\u4e86\u5408\u89c4\u57f9\u8bad\uff0c"
        "\u4e0b\u5348\u5199\u4e86\u4e2a\u5408\u540c\uff0c"
        "\u53d1\u73b0\u7cfb\u7edfbug\u53c8\u591a\u4e86\uff0c"
        "\u660e\u5929\u8fd8\u5f97\u7ee7\u7eed\u5199\u5408\u540c\uff0c"
        "\u8fd9\u7cfb\u7edf\u80fd\u4e0d\u80fd\u4fee\u4fee\u4e86",
        active,
    )
    _, _, confidential_retraction_commands = _compile(
        "\u7b97\u4e86\uff0c\u6570\u636e\u51fa\u5883\u90a3\u4e2a\u8fd8\u662f\u522b\u5199\u4e86\uff0c\u4fdd\u5bc6\u3002"
        "\u628a\u6628\u5929\u7684\u65e5\u62a5copy\u8fc7\u6765\u6539\u6539",
        active,
    )

    assert any(command.should_write and command.target_field == "today_work" and "\u5408\u89c4\u57f9\u8bad" in "".join(command.content) for command in mixed_rant_commands)
    assert any(command.should_write and command.target_field == "today_work" and "\u5408\u540c" in "".join(command.content) for command in mixed_rant_commands)
    assert any(command.should_write and command.target_field == "tomorrow_plan" and "\u5408\u540c" in "".join(command.content) for command in mixed_rant_commands)
    assert all("\u7cfb\u7edfbug" not in "".join(command.content).lower() for command in mixed_rant_commands)
    assert any(command.should_write and command.operation == "edit" for command in confidential_retraction_commands)


def test_daily_command_v4_cleans_typos_blocks_unfinished_shell_and_reuses_explicit_yesterday_items():
    _, _, typo_commands = _compile("\u4eca\u586b\u4e3b\u8981\u5c31\u662f\u5728\u6574\u90a3\u4e2a\u5408\u540c\uff0c\u6539\u6765\u6539\u53bb\uff0c\u6ca1\u5e72\u5565\u522b\u7684\u3002")
    _, _, shell_commands = _compile("\u4eca\u5929\u7684\u4e8b\u8fd8\u6ca1\u5199\u5b8c\u3002")
    _, _, explicit_repeat_commands = _compile("\u6628\u5929\u7684\u65e5\u62a5\u662f\uff1a\u89c1\u5ba2\u6237\u3001\u5199\u62a5\u544a\u3002\u4eca\u5929\u786e\u5b9e\u4e5f\u662f\u8fd9\u6837\u3002")

    assert any(command.should_write and "\u5408\u540c" in "".join(command.content) for command in typo_commands)
    assert all("\u4eca\u586b" not in "".join(command.content) for command in typo_commands)
    assert all("\u6ca1\u5e72\u5565\u522b\u7684" not in "".join(command.content) for command in typo_commands)
    assert all(not command.should_write for command in shell_commands)
    assert any(command.should_write and "\u89c1\u5ba2\u6237" in "".join(command.content) and "\u5199\u62a5\u544a" in "".join(command.content) for command in explicit_repeat_commands)


def test_daily_command_v4_keeps_work_before_assistant_feedback_tail():
    _, _, commands = _compile(
        "\u4eca\u5929\u4e3b\u8981\u5728\u5904\u7406XX\u9152\u5e97\u7684\u79df\u8d41\u7ea0\u7eb7\uff0c\u67e5\u4e86\u76f8\u5173\u6848\u4f8b\u3002"
        "\u95ee\u9898\u5c31\u662f\u5bf9\u65b9\u6839\u672c\u4e0d\u914d\u5408\uff0c\u534f\u5546\u5931\u8d25\u3002"
        "\u53ef\u80fd\u5f97\u8d70\u8bc9\u8bbc\u4e86\u3002"
        "\u987a\u4fbf\u8bf4\u4e00\u4e0b\uff0c\u8fd9\u4e2a\u65e5\u62a5\u52a9\u624b\u633a\u597d\u7528\u7684\u54c8\u54c8"
    )

    assert any(command.should_write and command.target_field == "today_work" and "\u9152\u5e97" in "".join(command.content) for command in commands)
    assert any(command.should_write and command.target_field == "problems" and "\u4e0d\u914d\u5408" in "".join(command.content) for command in commands)
    assert all("\u65e5\u62a5\u52a9\u624b" not in "".join(command.content) for command in commands)


def test_daily_command_v4_blocks_personal_vague_and_service_requests():
    for text in (
        "\u660e\u5929\u4e0d\u60f3\u4e0a\u73ed",
        "\u4eca\u5929\u5e72\u4e86\u597d\u591a\u4e8b",
        "md\uff0c\u4eca\u5929\u53c8\u52a0\u73ed\uff0c\u7d2f\u6b7b\u3002",
        "\u4eca\u5929\u5f04\u4e86\u4e00\u4e0b\u90a3\u4e2a\u4e8b\uff0c\u5dee\u4e0d\u591a\u4e86\u3002",
        "\u522b\u5f53\u771f\uff0c\u6211\u5c31\u6d4b\u8bd5\u7cfb\u7edf\u3002",
        "\u660e\u5929\u8981\u53bb\u5ba2\u6237\u73b0\u573a\u6f14\u793a\uff0c\u9700\u8981\u5e26\u54ea\u4e9b\u8bbe\u5907\uff1f",
        "\u4f60\u628a\u8fd9\u4e2a\u6848\u5b50\u6700\u8fd1\u7684\u8fdb\u5c55\u6574\u7406\u4e00\u4e0b\u7ed9\u6211",
    ):
        _, _, commands = _compile(text)

        assert not any(command.should_write for command in commands)


def test_daily_command_v4_allows_conditional_daily_supplement_with_content():
    _, _, commands = _compile("\u6ca1\u5199\u7684\u8bdd\u5e2e\u6211\u8865\u4e0a\uff1a\u6628\u5929\u4fee\u4e86\u4e09\u4e2abug\uff0c\u5f00\u4e86\u4e24\u4e2a\u4f1a\u3002")

    assert any(
        command.should_write
        and command.target_date == "yesterday"
        and command.target_field == "today_work"
        and "\u6628\u5929\u4fee\u4e86\u4e09\u4e2abug" in "".join(command.content)
        for command in commands
    )


def test_daily_command_v4_keeps_clear_tomorrow_trip_plan():
    _, _, commands = _compile("\u660e\u5929\u53bb\u676d\u5dde\u53c2\u52a0\u5cf0\u4f1a\uff0c\u540e\u5929\u56de\u6765")

    assert any(command.should_write and command.target_field == "tomorrow_plan" for command in commands)


def test_daily_command_v4_keeps_compound_delete_append_as_single_edit():
    active = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-compound-edit",
        status="collecting",
        reply_candidate=True,
    )

    _, _, commands = _compile("\u628a\u5f00\u4f1a\u53bb\u6389\uff0c\u52a0\u4e0a\u4ee3\u7801\u8bc4\u5ba1", active)

    assert len(commands) == 1
    assert commands[0].operation == "edit"
    assert "\u5f00\u4f1a" in commands[0].content[0]
    assert "\u4ee3\u7801\u8bc4\u5ba1" in commands[0].content[0]


def test_daily_command_v4_filters_process_questions_inside_mixed_work_turn():
    active = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-v4-process-question",
        status="collecting",
        reply_candidate=True,
    )

    _, _, commands = _compile(
        "\u4eca\u5929\u628a\u6052\u5927\u6848\u7684\u8bc1\u636e\u6e05\u5355\u6574\u7406\u5b8c\u4e86\uff0c"
        "\u660e\u5929\u7ea6\u4e86\u5f8b\u5e08\u6c9f\u901a\uff0c"
        "\u5bf9\u4e86\u7528\u5370\u6d41\u7a0b\u662f\u5565\u6765\u7740\uff1f\u987a\u4fbf\u95ee\u95ee\u5408\u540c\u76d6\u7ae0\u627e\u8c01",
        active,
    )
    _, _, reimbursement_commands = _compile(
        "\u4eca\u5929\u53bb\u4e0a\u6d77\u51fa\u5dee\u89c1\u5ba2\u6237\uff0c\u987a\u4fbf\u628a\u5408\u540c\u7b7e\u4e86\uff0c\u5dee\u65c5\u8d39\u8d85\u6807\u4e86\u548b\u6574\uff1f",
        active,
    )

    assert any(command.should_write and command.target_field == "today_work" and "\u8bc1\u636e\u6e05\u5355" in "".join(command.content) for command in commands)
    assert any(command.should_write and command.target_field == "tomorrow_plan" and "\u5f8b\u5e08" in "".join(command.content) for command in commands)
    assert all("\u5408\u540c\u76d6\u7ae0\u627e\u8c01" not in "".join(command.content) and "\u7528\u5370\u6d41\u7a0b" not in "".join(command.content) for command in commands)
    assert any(command.should_write and "\u4e0a\u6d77" in "".join(command.content) for command in reimbursement_commands)
    assert all("\u548b\u6574" not in "".join(command.content) and "\u8d85\u6807" not in "".join(command.content) for command in reimbursement_commands)


def test_daily_command_v4_allows_demand_document_revision_and_symbolic_edit():
    active = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-v4-doc-edit",
        status="collecting",
        reply_candidate=True,
    )

    _, _, doc_commands = _compile("\u4eca\u5929\u5b8c\u6210\u4e86\u9700\u6c42\u6587\u6863v2\u7684\u4fee\u6539\u3002", active)
    _, _, edit_commands = _compile("\u7b49\u4e00\u4e0b\uff0c\u628aB\u6539\u6210D\uff0c\u5176\u5b9eB\u6ca1\u505a", active)

    assert any(command.should_write and command.target_field == "today_work" and "\u9700\u6c42\u6587\u6863" in "".join(command.content) for command in doc_commands)
    assert len(edit_commands) == 1
    assert edit_commands[0].operation == "edit"
    assert edit_commands[0].should_write is True


def test_daily_command_v4_copy_current_work_to_tomorrow_and_today_task_reference():
    active = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-v4-copy-current",
        status="collecting",
        reply_candidate=True,
    )

    _, _, copy_commands = _compile("\u4eca\u5929\u7684\u5de5\u4f5c\u518d\u590d\u5236\u4e00\u4efd\u5230\u660e\u5929\u3002", active)
    _, _, completed_commands = _compile("\u6628\u5929\u8bf4\u7684\u90a3\u4e2a\u4efb\u52a1\u4eca\u5929\u5b8c\u6210\u4e86\u3002", active)

    assert len(copy_commands) == 1
    assert copy_commands[0].operation == "copy_current_to_tomorrow"
    assert copy_commands[0].target_field == "tomorrow_plan"
    assert copy_commands[0].should_write is True
    assert any(command.should_write and command.target_date == "today" for command in completed_commands)


def test_daily_command_v4_tentative_case_resolution_does_not_write_daily():
    active = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-v4-case-hope",
        status="collecting",
        reply_candidate=True,
    )

    _, _, commands = _compile("\u5bf9\u4e86\uff0c\u8fd9\u4e2a\u6848\u4ef6\u5bf9\u65b9\u53ef\u80fd\u548c\u89e3\uff0c\u5e0c\u671b\u987a\u5229\u3002", active)

    assert all(not command.should_write for command in commands)


def test_daily_command_v4_smoke_regression_filters_chatter_and_keeps_contextual_work():
    active = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-v4-smoke-regressions",
        status="collecting",
        reply_candidate=True,
    )

    for text in (
        "\u8fd9\u4e2a\u6708\u62a5\u5ba1\u6279\u8c01\u5728\u7ba1\u554a\uff0c\u6211\u63d0\u4ea4\u4e86\u4e00\u76f4\u6ca1\u53cd\u5e94",
        "\u4eca\u5929\u7684\u6708\u4eae\u597d\u5706\uff0c\u6211\u60f3\u5403\u6708\u997c",
        "\u4eca\u5929\u7684\u6708\u4eae\u662f\u84dd\u8272\u7684\uff0c\u62a5\u544a\u5b8c\u6bd5",
        "\u4eca\u5929\u628a\u516c\u53f8\u70b8\u4e86\uff0c\u54c8\u54c8\u54c8",
        "\u4eca\u5929\u6ca1\u5565\u53ef\u5199\u7684\uff0c\u5c31\u90a3\u6837",
        "\u65e5\u62a5\u63d0\u4ea4\u4e86",
        "\u8865\u5145\u6750\u6599\u4ec0\u4e48\u65f6\u5019\u4ea4\u554a",
        "\u4eca\u5929\u505a\u4e86\u4e00\u4e9b\u4e8b",
        "\u660e\u5929\u53c8\u8981\u51fa\u5dee\u4e86\uff0c\u70e6",
        "\u54c8\u54c8\uff0c\u4eca\u5929\u6211\u662f\u5965\u7279\u66fc\uff0c\u6765\u6253\u602a\u517d\u4e86",
        "\u4eca\u5929\u8ddf\u5c0f\u738b\u6253\u67b6\uff0c\u88ab\u9886\u5bfc\u6279\u8bc4\u4e86",
        "\u4eca\u5929\u6211\u597d\u50cf\u6ca1\u505a\u5565",
        "\u4eca\u5929\u4e0b\u66b4\u96e8\uff0c\u6574\u4e2a\u4eba\u8981\u53d1\u9709\u4e86\uff0c\u4e2d\u5348\u5403\u7684\u9ec4\u7116\u9e21\u597d\u96be\u5403",
        "\u8c01\u8fd8\u6ca1\u4ea4\uff1f\u660e\u5929\u622a\u6b62\u4e86",
        "\u540e\u5929\u53bb\u5357\u4eac\u51fa\u5dee\uff0c\u5468\u4e8c\u5230\u5468\u56db\u90fd\u5728\u90a3\u8fb9\uff0c\u65e5\u62a5\u53ef\u80fd\u5f97\u5468\u4e94\u4e00\u8d77\u8865",
        "\u5bf9\u4e86\uff0c\u6628\u5929\u5217\u7684\u660e\u65e5\u8ba1\u5212\u91cc\u7684\u8054\u7cfb\u6cd5\u5b98\u5df2\u7ecf\u505a\u4e86\uff0c\u4eca\u5929\u4e0d\u7528\u5199\u4e86",
    ):
        _, _, commands = _compile(text, active)
        assert all(not command.should_write for command in commands)

    _, _, onsite_commands = _compile("\u660e\u5929\u8ba1\u5212\u53bb\u73b0\u573a\u770b\u770b", active)
    _, _, budget_commands = _compile("\u8fd8\u6709\uff0c\u5411\u8d22\u52a1\u63d0\u4ea4\u4e86\u9884\u7b97", active)
    _, _, file_commands = _compile("\u5c31\u662f\u6574\u7406\u4e86\u4e00\u4e0b\u6587\u4ef6", active)
    _, _, same_commands = _compile("\u5bf9\uff0c\u4e00\u6837", active)
    _, _, makeup_commands = _compile("\u6628\u5929\u65e5\u62a5\u5fd8\u4e86\u5199\uff0c\u4eca\u5929\u8865\u4e00\u4e0b\uff0c\u6628\u5929\u5c31\u5f00\u4e86\u4e2a\u4f1a\u3002", active)
    _, _, makeup_prefix_commands = _compile("\u8865\u4e00\u4e0b\u6628\u5929\u7684\u65e5\u62a5\uff0c\u6628\u5929\u4e3b\u8981\u5de5\u4f5c\u662f\u6574\u7406\u5408\u540c\u53f0\u8d26", active)
    _, _, system_problem_commands = _compile("\u8fd8\u6709\u4e2a\u4e8b\u5fd8\u4e86\uff0c\u670d\u52a1\u5668\u7a81\u7136\u91cd\u542f\uff0c\u5ba2\u6237\u53cd\u9988\u6162\u4e86", active)
    _, _, meeting_commands = _compile("\u53e6\u5916\uff0c\u4eca\u5929\u4e0b\u5348\u6709\u4e2a\u4f1a\uff0c\u5173\u4e8e\u8fd9\u4e2a\u5408\u540c\u7684", active)
    _, _, legal_opinion_commands = _compile("\u4eca\u586b\u5199\u4e86\u4e09\u4efd\u6cd5\u5f8b\u610f\u89c1\u4e66", active)

    assert any(command.should_write and command.target_field == "tomorrow_plan" for command in onsite_commands)
    assert any(command.should_write and "\u9884\u7b97" in "".join(command.content) for command in budget_commands)
    assert any(command.should_write and "\u6587\u4ef6" in "".join(command.content) for command in file_commands)
    assert any(command.should_write and command.operation == "copy_previous" and command.target_field == "today_work" for command in same_commands)
    assert any(command.should_write and command.target_date == "yesterday" and "\u5f00\u4e86\u4e2a\u4f1a" in "".join(command.content) for command in makeup_commands)
    assert any(command.should_write and command.target_date == "yesterday" and "\u5408\u540c\u53f0\u8d26" in "".join(command.content) for command in makeup_prefix_commands)
    assert any(command.should_write and command.target_field == "problems" for command in system_problem_commands)
    assert any(command.should_write and command.target_field == "today_work" and "\u6709\u4e2a\u4f1a" in "".join(command.content) for command in meeting_commands)
    assert any(command.should_write and "\u6cd5\u5f8b\u610f\u89c1\u4e66" in "".join(command.content) for command in legal_opinion_commands)


def test_daily_command_v4_round40_keeps_business_and_filters_question_noise():
    active = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-v4-round40",
        status="collecting",
        reply_candidate=True,
    )

    _, _, copy_commands = _compile("\u6628\u5929\u7684\u660e\u65e5\u8ba1\u5212\u62ff\u6765\u4eca\u5929\u7528", active)
    _, _, weekend_commands = _compile("\u660e\u5929\u5468\u672b\u4e86\uff0c\u5f00\u5fc3\uff01", active)
    _, _, plan_commands = _compile("\u660e\u5929\u7a7f\u5565\u51fa\u95e8\uff1f\u54e6\u5bf9\u4e86\u660e\u5929\u8ba1\u5212\u628a\u65b9\u6848\u5199\u5b8c", active)
    _, _, risk_commands = _compile("\u98ce\u9669\uff1f\u6ca1\u5565\u5927\u98ce\u9669\uff0c\u5c31\u662f\u652f\u4ed8bug\u53ef\u80fd\u5f71\u54cd\u7070\u5ea6", active)
    _, _, demand_commands = _compile(
        "\u65e5\u62a5\uff1a\u4eca\u5929\u5e72\u4e86\u4e24\u4ef6\u4e8b\uff0c"
        "\u7b2c\u4e00\u4ef6\u662f\u6539\u5b8c\u652f\u4ed8bug\uff0c"
        "\u7b2c\u4e8c\u4ef6\u662f\u8ddf\u4ea7\u54c1\u6495\u9700\u6c42",
        active,
    )

    assert any(command.should_write and command.operation == "copy_previous" and command.target_field == "today_work" for command in copy_commands)
    assert weekend_commands == []
    assert any(command.should_write and command.target_field == "tomorrow_plan" and "\u65b9\u6848\u5199\u5b8c" in "".join(command.content) for command in plan_commands)
    assert all("\u7a7f\u5565" not in "".join(command.content) for command in plan_commands)
    assert any(command.should_write and command.target_field == "problems" and "\u652f\u4ed8bug" in "".join(command.content) for command in risk_commands)
    assert any(command.should_write and "\u6c9f\u901a\u9700\u6c42" in "".join(command.content) for command in demand_commands)
    assert all("\u6495\u9700\u6c42" not in "".join(command.content) for command in demand_commands)


def test_daily_command_v4_round41_context_and_temporal_boundaries():
    active = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-v4-round41",
        status="collecting",
        reply_candidate=True,
    )

    _, _, meeting_conclusion = _compile("\u90a3\u4e2a\u4f1a\u7684\u7ed3\u8bba\u4e5f\u5199\u8fdb\u53bb\uff1a\u5bf9\u65b9\u53ef\u80fd\u613f\u610f\u548c\u89e3", active)
    _, _, signed_contract = _compile("\u5199\u65e5\u62a5\uff0c\u628a\u5408\u540c\u7b7e\u4e86\u8bb0\u4e0a", active)
    _, _, yesterday_makeup = _compile("\u6628\u5929\u7684\u5de5\u4f5c\u662f\uff1a\u6574\u7406\u4e0a\u5468\u4f1a\u8bae\u7eaa\u8981", active)
    _, _, case_progress_only = _compile("\u5bf9\u65b9\u5f8b\u5e08\u53d1\u6765\u65b0\u7684\u8bc1\u636e\uff0c\u6211\u4eec\u8981\u8bc4\u4f30", active)
    _, _, previous_plan_done = _compile("\u6628\u5929\u5199\u7684\u660e\u65e5\u8ba1\u5212\u662f\u5f00\u5ead\uff0c\u4eca\u5929\u786e\u5b9e\u53bb\u5f00\u5ead\u4e86", active)
    _, _, overtime_status = _compile("\u51cc\u66681\u70b9\u8fd8\u5728\u5199\u8d77\u8bc9\u72b6\uff0c\u56f0\u6b7b\u4e86\uff0c\u4eca\u5929\u5c31\u8fd9\u6837\u5427", active)
    _, _, submit_commands = _compile("\u597d\u4e86\u63d0\u4ea4", active)

    assert any(command.should_write and "\u613f\u610f\u548c\u89e3" in "".join(command.content) for command in meeting_conclusion)
    assert any(command.should_write and "\u5408\u540c\u7b7e" in "".join(command.content) for command in signed_contract)
    assert any(command.should_write and command.target_date == "yesterday" and "\u4f1a\u8bae\u7eaa\u8981" in "".join(command.content) for command in yesterday_makeup)
    assert all(not command.should_write for command in case_progress_only)
    assert any(command.should_write and command.target_field == "today_work" and "\u53bb\u5f00\u5ead" in "".join(command.content) for command in previous_plan_done)
    assert all(command.target_field != "tomorrow_plan" for command in previous_plan_done)
    assert overtime_status == []
    assert any(command.operation == "confirm" and command.should_write for command in submit_commands)


def test_daily_command_v4_round42_remaining_smoke_boundaries():
    active = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-v4-round42",
        status="collecting",
        reply_candidate=True,
    )

    _, _, makeup = _compile("\u6628\u5929\u4e0b\u5348\u7684\u4f1a\u8bae\u7eaa\u8981\u8fd8\u6ca1\u5199\uff0c\u4eca\u5929\u8865\u4e0a", active)
    _, _, delete_meeting = _compile("\u628a\u5f00\u4f1a\u7684\u5220\u6389\u5427", active)
    _, _, evidence_list = _compile("\u522b\u5fd8\u4e86\u8fd8\u6709\u8bc1\u636e\u6e05\u5355", active)
    _, _, monthly_test = _compile("\u8ba9\u6211\u6d4b\u8bd5\u4e0b\u6708\u62a5\u529f\u80fd", active)

    assert any(command.should_write and command.target_field == "today_work" and "\u8865\u5199" in "".join(command.content) for command in makeup)
    assert any(command.operation == "edit" and command.should_write for command in delete_meeting)
    assert all(command.operation != "clear" for command in delete_meeting)
    assert any(command.should_write and "\u8bc1\u636e\u6e05\u5355" in "".join(command.content) for command in evidence_list)
    assert monthly_test == []


def test_daily_command_v4_round43_noise_and_clear_supplements():
    active = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-v4-round43",
        status="collecting",
        reply_candidate=True,
    )

    _, _, lunch_question = _compile("\u4eca\u5929\u5904\u7406\u4e863\u4e2a\u5408\u540c\uff0c\u987a\u4fbf\u95ee\u4e0b\u660e\u5929\u5348\u996d\u53bb\u54ea\u5403\uff1f", active)
    writes = [command for command in lunch_question if command.should_write]
    assert any(command.target_field == "today_work" and command.content == ["\u5904\u74063\u4e2a\u5408\u540c"] for command in writes)
    assert not any(command.target_field == "tomorrow_plan" and any("\u5348\u996d" in item for item in command.content) for command in writes)

    _, _, fake_test = _compile("\u4e0d\u662f\u771f\u7684\u65e5\u62a5\uff0c\u5c31\u662f\u8bd5\u8bd5\uff0c\u4f60\u5199\u4e2a\u6d4b\u8bd5123", active)
    assert all(not command.should_write for command in fake_test)

    _, _, monthly_query = _compile("\u770b\u770b\u6211\u4e0a\u4e2a\u6708\u6708\u62a5\u7f3a\u5565", active)
    assert all(not command.should_write for command in monthly_query)

    _, _, start_reporting = _compile("\u54e6\u6211\u662f\u8bf4\u6211\u73b0\u5728\u5f00\u59cb\u62a5\uff0c\u4eca\u5929\u505a\u4e86\u5565\u6765\u7740\u2026\u54e6\uff0c\u8ddf\u8fdb\u4e86\u4fdd\u5229\u6848\uff0c\u6ca1\u4e86", active)
    assert any(
        command.should_write and command.target_field == "today_work" and command.content == ["\u8ddf\u8fdb\u4fdd\u5229\u6848"]
        for command in start_reporting
    )

    _, _, not_repeat = _compile("\u90a3\u660e\u5929\u5c31\u4e0d\u7528\u91cd\u590d\u63d0\u4e86", active)
    assert all(not command.should_write for command in not_repeat)

    _, _, empty_report = _compile("\u5199\u65e5\u62a5\uff1a\u4eca\u5929\u5565\u4e5f\u6ca1\u5e72")
    assert all(not command.should_write for command in empty_report)

    _, _, old_same = _compile("\u4eca\u5929\u8fd8\u662f\u8001\u6837\u5b50")
    assert all(not command.should_write for command in old_same)

    _, _, office_plan = _compile("\u660e\u5929\u53bb\u516c\u8bc1\u5904", active)
    assert any(
        command.should_write and command.target_field == "tomorrow_plan" and command.content == ["\u53bb\u516c\u8bc1\u5904"]
        for command in office_plan
    )

    _, _, ambiguous_day = _compile("\u5bf9\uff0c\u540c\u65f6\u90a3\u5929\u7684\u5de5\u4f5c\u65e5\u62a5\u5c31\u5199\u5f00\u5ead\u5427\uff0c\u5176\u4ed6\u7684\u6ca1\u4e86", active)
    assert all(not command.should_write for command in ambiguous_day)


def test_daily_command_v4_round44_edits_mixed_business_and_emotion_noise():
    active = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-v4-round44",
        status="collecting",
        reply_candidate=True,
    )

    _, _, delete_risk = _compile("\u7136\u540e\u5220\u9664\u98ce\u9669\u90a3\u4e00\u680f", active)
    assert any(command.should_write and command.operation in {"edit", "clear"} and command.target_field in {"problems", "unknown"} for command in delete_risk)

    _, _, merge_risk = _compile("\u7136\u540e\u5408\u5e76\u98ce\u9669\u680f\uff1a\u9879\u76ee\u5ef6\u671f\u98ce\u9669", active)
    assert any(command.should_write and command.target_field == "problems" and "\u9879\u76ee\u5ef6\u671f\u98ce\u9669" in "".join(command.content) for command in merge_risk)

    _, _, mixed_process = _compile("\u4eca\u5929\u5b8c\u6210\u4e86\u62a5\u8868\uff0c\u987a\u4fbf\u95ee\u4e00\u4e0b\u51fa\u5dee\u62a5\u9500\u6807\u51c6\uff0c\u660e\u5929\u53bb\u8d22\u52a1\u4ea4\u8868")
    assert any(command.should_write and command.target_field == "today_work" and "\u5b8c\u6210\u62a5\u8868" in "".join(command.content) for command in mixed_process)
    assert any(command.should_write and command.target_field == "tomorrow_plan" and "\u53bb\u8d22\u52a1\u4ea4\u8868" in "".join(command.content) for command in mixed_process)
    assert not any(command.should_write and any("\u62a5\u9500\u6807\u51c6" in item for item in command.content) for command in mixed_process)

    _, _, hearing_plan = _compile("\u4fdd\u5229\u6848\u660e\u5929\u5f00\u5ead\uff0c\u51c6\u5907\u6750\u6599")
    assert any(command.should_write and command.target_field == "tomorrow_plan" for command in hearing_plan)

    _, _, delete_meeting = _compile("\u628a\u98ce\u63a7\u4f1a\u5220\u4e86\u5427\uff0c\u90a3\u4e2a\u4e0d\u7b97", active)
    assert any(command.should_write and command.operation == "edit" for command in delete_meeting)

    _, _, emotional_phone = _compile("\u521a\u521a\u63a5\u4e86\u4e2a\u5ba2\u6237\u7535\u8bdd\uff0c\u6c14\u6b7b\u4e86")
    assert all(not command.should_write for command in emotional_phone)

    _, _, retract = _compile("\u4e0d\u5bf9\uff0c\u90a3\u4e2a\u4e0d\u7b97\uff0c\u64a4\u56de", active)
    assert any(command.should_write and command.operation in {"edit", "revoke"} for command in retract)


def test_daily_command_v4_round45_history_meta_lifestyle_and_vague_noise():
    active = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-v4-round45",
        status="collecting",
        reply_candidate=True,
    )

    _, _, vague = _compile("\u4eca\u5929\u505a\u4e86\u4e9b\u4e8b\uff0c\u660e\u5929\u7ee7\u7eed\u5f04", active)
    assert all(not command.should_write for command in vague)

    _, _, history_query = _compile("\u4e0a\u5468\u5f00\u5ead\u60c5\u51b5\u53d1\u6211\u4e0b", active)
    assert all(not command.should_write for command in history_query)

    _, _, meta_test = _compile("\u8ba9\u6211\u6d4b\u8bd5\u4e0b\uff0c\u5199\u65e5\u62a5\uff1a\u4eca\u5929\u6d4b\u8bd5\u4e86\u673a\u5668\u4eba", active)
    assert all(not command.should_write for command in meta_test)

    _, _, cold_travel = _compile("\u4eca\u5929\u51bb\u6b7b\u4e86\uff0c\u660e\u5929\u7a7f\u5565\u51fa\u95e8\u554a\uff1f\u54e6\u5bf9\u4e86\uff0c\u660e\u5929\u8981\u53bb\u5317\u4eac\u51fa\u5dee\uff0c\u8ddf\u671d\u9633\u6cd5\u9662\u6709\u4e2a\u8c08\u8bdd", active)
    assert not any(command.should_write and command.target_field == "today_work" for command in cold_travel)
    assert any(
        command.should_write
        and command.target_field == "tomorrow_plan"
        and ("\u5317\u4eac" in "".join(command.content) or "\u671d\u9633\u6cd5\u9662" in "".join(command.content))
        for command in cold_travel
    )

    _, _, painful_clause = _compile("\u4eca\u5929\u641e\u5b9a\u4e86\u90a3\u4e2a\u5934\u75bc\u7684\u6761\u6b3e", active)
    assert any(command.should_write and command.target_field == "today_work" and "\u6761\u6b3e" in "".join(command.content) for command in painful_clause)
    assert all("\u5934\u75bc" not in "".join(command.content) for command in painful_clause)


def test_daily_command_v4_round46_status_absurd_vague_and_edit_boundaries():
    active = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-v4-round46",
        status="collecting",
        reply_candidate=True,
    )

    _, _, previous_task_done = _compile("\u6628\u5929\u8bf4\u7684\u65b9\u6848\u6539\u5b8c\u4e86", active)
    assert any(
        command.should_write and command.target_field == "today_work" and "\u65b9\u6848" in "".join(command.content)
        for command in previous_task_done
    )
    assert all("\u6628\u5929\u8bf4\u7684" not in "".join(command.content) for command in previous_task_done)

    _, _, ordinal_edit = _compile("\u628a\u4eca\u5929\u7b2c\u4e8c\u6761\u5de5\u4f5c\u6539\u6210'\u4e0e\u4f9b\u5e94\u5546\u8c08\u5224\u8fbe\u6210\u521d\u6b65\u534f\u8bae'", active)
    assert any(command.should_write and command.operation == "edit" and command.target_field == "today_work" for command in ordinal_edit)

    _, _, vague_done = _compile("\u4eca\u5929\u7684\u6d3b\u5e72\u5b8c\u4e86\uff0c\u7d2f\u6b7b", active)
    assert all(not command.should_write for command in vague_done)

    _, _, absurd_identity = _compile("\u6211\u662f\u79e6\u59cb\u7687\uff0c\u7ed9\u6211\u53d1\u65e5\u62a5", active)
    assert all(not command.should_write for command in absurd_identity)

    _, _, status_query = _compile("\u65e5\u62a5\u6211\u5199\u4e86\u6ca1\u554a\uff0c\u4eca\u5929\u7684", active)
    assert status_query
    assert all(not command.should_write for command in status_query)

    _, _, status_enough = _compile("\u5bf9\u4e86\uff0c\u6211\u7684\u65e5\u62a5\u5199\u5b8c\u4e86\u5417\uff0c\u4eca\u5929\u7684\u5de5\u4f5c\u591f\u4e86\u5417\uff1f", active)
    assert all(not command.should_write for command in status_enough)


def test_daily_command_v4_round47_no_problem_status_and_lifestyle_plan_filtering():
    active = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-v4-round47",
        status="collecting",
        reply_candidate=True,
    )

    _, _, no_problem = _compile("\u4eca\u5929\u641e\u4e86\u4f9b\u5e94\u5546\u8d44\u8d28\u5ba1\u6838\uff0c\u6ca1\u51fa\u5565\u5927\u95ee\u9898", active)
    assert any(command.should_write and command.target_field == "today_work" for command in no_problem)
    assert not any(command.should_write and command.target_field == "problems" for command in no_problem)

    _, _, clean_plan = _compile("\u518d\u628a\u660e\u5929\u8ba1\u5212\u52a0\u4e0a\uff1a\u53bb\u5bf9\u65b9\u516c\u53f8\u5b9e\u5730\u8003\u5bdf", active)
    assert any(command.should_write and command.target_field == "tomorrow_plan" and command.content == ["\u53bb\u5bf9\u65b9\u516c\u53f8\u5b9e\u5730\u8003\u5bdf"] for command in clean_plan)

    _, _, submitted = _compile("\u522b\u5fd8\u4e86\u8fd8\u6709\u65e5\u62a5\u8981\u5199\uff0c\u4eca\u5929\u65e5\u62a5\u5df2\u7ecf\u5199\u597d\u4e86", active)
    assert all(not command.should_write for command in submitted)

    _, _, weather = _compile("\u4eca\u5929\u5199\u4e86\u4e09\u4efd\u5408\u540c\u5ba1\u6838\u610f\u89c1\uff0c\u4e0d\u8fc7\u597d\u7d2f\u554a\uff0c\u660e\u5929\u7a7f\u5565\u51fa\u95e8\u5462\uff1f\u5929\u6c14\u548b\u6837\uff1f", active)
    assert any(command.should_write and command.target_field == "today_work" for command in weather)
    assert not any(command.should_write and command.target_field == "tomorrow_plan" for command in weather)


def test_daily_command_v4_round48_process_questions_and_empty_meeting_chatter_do_not_pollute():
    active = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-v4-round48",
        status="collecting",
        reply_candidate=True,
    )

    _, _, process_mix = _compile("\u4eca\u5929\u5b8c\u6210\u4e86\u5408\u540c\u5f52\u6863\uff0c\u8fd8\u6709\uff0c\u90a3\u4e2a\u7528\u5370\u6d41\u7a0b\u5230\u5e95\u8981\u591a\u4e45\uff1f\u660e\u5929\u53bb\u5317\u4eac\u51fa\u5dee\u3002", active)
    assert any(command.should_write and command.target_field == "today_work" and "\u5408\u540c\u5f52\u6863" in "".join(command.content) for command in process_mix)
    assert any(command.should_write and command.target_field == "tomorrow_plan" and "\u5317\u4eac\u51fa\u5dee" in "".join(command.content) for command in process_mix)
    assert not any(command.should_write and "\u7528\u5370\u6d41\u7a0b" in "".join(command.content) for command in process_mix)

    _, _, meeting_chatter = _compile("\u4eca\u5929\u5565\u4e5f\u6ca1\u5e72\u6210\uff0c\u5168\u5728\u5f00\u4f1a\uff0c\u70e6\u6b7b\u4e86\u3002", active)
    assert all(not command.should_write for command in meeting_chatter)

    _, _, reimbursement = _compile("\u4eca\u5929\u641e\u5b8c\u4e86\u6570\u636e\u5206\u6790\uff0c\u597d\u7d2f\u554a\uff0c\u660e\u5929\u7a7f\u5565\u51fa\u95e8\uff1f\u987a\u4fbf\u95ee\u4e0b\u62a5\u9500\u6d41\u7a0b", active)
    assert any(command.should_write and command.target_field == "today_work" and "\u6570\u636e\u5206\u6790" in "".join(command.content) for command in reimbursement)
    assert not any(command.should_write and ("\u7a7f\u5565" in "".join(command.content) or "\u62a5\u9500\u6d41\u7a0b" in "".join(command.content)) for command in reimbursement)


def test_daily_command_v4_round49_booking_weekly_and_emotional_fragments_do_not_pollute():
    active = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-v4-round49",
        status="collecting",
        reply_candidate=True,
    )

    _, _, busy = _compile("\u4eca\u5929\u53c8\u662f\u5fd9\u788c\u7684\u4e00\u5929\u554a", active)
    assert all(not command.should_write for command in busy)

    _, _, booking = _compile("\u660e\u5929\u98de\u4e0a\u6d77\uff0c\u5e2e\u6211\u8ba2\u4e2a\u9152\u5e97\uff0c\u518d\u5b89\u6392\u4e2a\u63a5\u673a", active)
    assert all(not command.should_write for command in booking)

    _, _, signed = _compile("\u54ce\u4eca\u5929\u771f\u662f\u5012\u9709\uff0c\u88ab\u5ba2\u6237\u9a82\u4e86\u4e00\u987f\uff0c\u4e0d\u8fc7\u5408\u540c\u603b\u7b97\u7b7e\u5b8c\u4e86", active)
    assert any(command.should_write and command.target_field == "today_work" and "\u5408\u540c\u7b7e\u5b8c" in "".join(command.content) for command in signed)
    assert not any(command.should_write and ("\u5012\u9709" in "".join(command.content) or "\u5ba2\u6237\u9a82" in "".join(command.content)) for command in signed)

    _, _, weekly = _compile("\u8fd9\u6b21\u51fa\u5dee\u8981\u5199\u8fdb\u5468\u62a5\u91cc", active)
    assert all(not command.should_write for command in weekly)

    _, _, clothing = _compile("\u4eca\u5929\u6574\u7406\u4e86\u4e00\u5806\u5408\u540c\uff0c\u7d2f\u6b7b\u6211\u4e86\uff0c\u660e\u5929\u7a7f\u4ec0\u4e48\u8863\u670d\u53bb\u51fa\u5ead\u5408\u9002\uff1f", active)
    assert any(command.should_write and command.target_field == "today_work" and "\u5408\u540c" in "".join(command.content) for command in clothing)
    assert not any(command.should_write and command.target_field == "tomorrow_plan" for command in clothing)


def test_daily_command_v4_round50_query_noise_problem_and_rhetorical_work_reply():
    active = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-v4-round50",
        status="collecting",
        reply_candidate=True,
    )

    _, _, system_timeout = _compile("\u6709\u4e2a\u5c0f\u95ee\u9898\uff0c\u6cd5\u52a1\u7cfb\u7edf\u767b\u5f55\u8001\u8d85\u65f6\uff0c\u660e\u5929\u5f97\u627eIT\u770b\u770b", active)
    assert any(command.should_write and command.target_field == "problems" and "\u767b\u5f55\u8001\u8d85\u65f6" in "".join(command.content) for command in system_timeout)
    assert not any(command.operation == "query_current" for command in system_timeout)

    _, _, mixed_question = _compile("\u4eca\u5929\u505a\u7684\u5c31\u5408\u540c\u548c\u57f9\u8bad\uff0c\u5408\u540c\u65b9\u9762\u7532\u65b9\u975e\u8981\u6539\u9a8c\u6536\u6807\u51c6\uff0c\u54b1\u6cd5\u52a1\u7cfb\u7edf\u80fd\u81ea\u52a8\u8bc6\u522b\u98ce\u9669\u6761\u6b3e\u4e0d\uff1f\u660e\u5929\u8bf4\u5565\u4e5f\u5f97\u8ddf\u4e1a\u52a1\u78b0\u4e00\u4e0b\uff0c\u4e0d\u80fd\u518d\u62d6\u4e86", active)
    assert any(command.should_write and command.target_field == "today_work" and "\u5408\u540c" in "".join(command.content) for command in mixed_question)
    assert any(command.should_write and command.target_field == "tomorrow_plan" and "\u4e1a\u52a1" in "".join(command.content) for command in mixed_question)
    assert not any(command.should_write and "\u81ea\u52a8\u8bc6\u522b" in "".join(command.content) for command in mixed_question)

    _, _, rhetorical = _compile("\u5c31\u662f\u5ba1\u5408\u540c\u548c\u5f00\u4f1a\u5457\uff0c\u8fd8\u80fd\u6709\u5565", active)
    assert any(command.should_write and command.target_field == "today_work" and "\u5ba1\u5408\u540c" in "".join(command.content) for command in rhetorical)


def test_daily_command_v4_round51_blocks_deferral_vague_noise_and_keeps_work_in_lifestyle_sentence():
    active = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-v4-round51",
        status="collecting",
        reply_candidate=True,
    )

    _, _, delete_risk = _compile("\u54e6\u4e0d\u5bf9\uff0c\u98ce\u9669\u5df2\u7ecf\u89e3\u9664\u4e86\uff0c\u628a\u98ce\u9669\u5220\u4e86\u5427", active)
    assert any(command.should_write and command.operation == "clear" and command.target_field == "problems" for command in delete_risk)
    assert not any(command.should_write and command.target_field == "today_work" and "\u98ce\u9669\u5df2\u7ecf\u89e3\u9664" in "".join(command.content) for command in delete_risk)

    _, _, deferral = _compile("\u7b49\u7ed3\u679c\u51fa\u6765\u518d\u62a5\u65e5\u62a5\u5427", active)
    assert all(not command.should_write for command in deferral)

    _, _, vague = _compile("\u4eca\u5929\u641e\u4e86\u70b9\u4e1c\u897f\uff0c\u4f60\u61c2\u7684", active)
    assert all(not command.should_write for command in vague)

    _, _, hot_review = _compile("\u4eca\u5929\u597d\u70ed\u554a\uff0c\u6211\u5b8c\u6210\u4e86\u4ee3\u7801review\uff0c\u660e\u5929\u8981\u4e0a\u7ebf\u4e86", active)
    assert any(command.should_write and command.target_field == "today_work" and "review" in "".join(command.content) for command in hot_review)
    assert any(command.should_write and command.target_field == "tomorrow_plan" and "\u4e0a\u7ebf" in "".join(command.content) for command in hot_review)
    assert not any(command.should_write and "\u597d\u70ed" in "".join(command.content) for command in hot_review)


def test_daily_command_v4_round52_query_vague_commentary_and_contextual_add():
    active = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-v4-round52",
        status="collecting",
        reply_candidate=True,
    )

    _, _, start_only = _compile("\u6211\u8981\u5199\u65e5\u62a5\u4e86", active)
    assert all(not command.should_write for command in start_only)
    start_plan, _, start_commands = _compile("\u6211\u8981\u5199\u65e5\u62a5\u4e86", active)
    assert start_plan.primary_workflow == WORKFLOW_DAILY_REPORT
    assert start_commands
    assert all(command.operation == "no_write" and command.target_field == "none" for command in start_commands)

    _, _, start_with_payload = _compile("\u5199\u65e5\u62a5\u4e86\uff1a\u4e0a\u5348\u6574\u7406\u6863\u6848\uff0c\u4e0b\u5348\u63a5\u5f85\u5ba2\u6237\u54a8\u8be2\u3002", active)
    assert any(command.should_write and command.target_field == "today_work" and "\u6574\u7406\u6863\u6848" in "".join(command.content) for command in start_with_payload)

    _, _, today_query = _compile("\u6211\u4eca\u5929\u65e5\u62a5\u5199\u4e86\u5565", active)
    assert today_query
    assert all(not command.should_write and command.operation == "query_current" for command in today_query)

    _, _, send_query = _compile("\u628a\u6211\u4eca\u5929\u7684\u65e5\u62a5\u53d1\u7ed9\u6211", active)
    assert send_query
    assert all(not command.should_write and command.operation == "query_current" for command in send_query)

    _, _, task_question = _compile("\u54e6\u90a3\u4eca\u5929\u8981\u505a\u5565\uff1f", active)
    assert all(not command.should_write for command in task_question)

    _, _, vague_work = _compile("\u4eca\u5929\u5de5\u4f5c\u4e86\u3002", active)
    assert all(not command.should_write for command in vague_work)

    _, _, case_commentary = _compile("\u5bf9\u65b9\u5f8b\u5e08\u6709\u70b9\u96be\u7f20\uff0c\u4e0d\u8fc7\u6211\u4eec\u8bc1\u636e\u5145\u5206\u3002", active)
    assert all(not command.should_write for command in case_commentary)

    _, _, doc_update = _compile("\u8fd8\u66f4\u65b0\u4e86\u7cfb\u7edf\u6587\u6863", active)
    assert any(command.should_write and command.target_field == "today_work" and "\u7cfb\u7edf\u6587\u6863" in "".join(command.content) for command in doc_update)

    _, _, quoted_delete = _compile("\u7136\u540e\u628a\u2018\u5b8c\u6210\u6587\u6863\u2019\u5220\u6389\u3002", active)
    assert any(command.should_write and command.operation == "edit" and command.target_field == "today_work" for command in quoted_delete)

    _, _, quoted_add = _compile("\u518d\u52a0\u4e0a\u2018\u51c6\u5907\u5408\u540c\u8d44\u6599\u2019\u3002", active)
    assert any(command.should_write and command.target_field == "today_work" and "\u51c6\u5907\u5408\u540c\u8d44\u6599" in "".join(command.content) for command in quoted_add)
