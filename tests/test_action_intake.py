from datetime import datetime
from zoneinfo import ZoneInfo

from app.workflows.action_intake import (
    ACTION_ASSISTANT_FEEDBACK,
    ACTION_CASE_PROGRESS,
    ACTION_DAILY_CONFIRM,
    ACTION_DAILY_READ_CURRENT,
    ACTION_DAILY_READ_HISTORY,
    ACTION_DAILY_EDIT,
    ACTION_DAILY_WRITE,
    ACTION_DISAMBIGUATION_REQUIRED,
    ACTION_INTERNAL_QA,
    ACTION_LEGAL_RESEARCH,
    ACTION_MONTHLY_STATUS_QUERY,
    ACTION_SMALL_TALK,
    ACTION_TRAVEL_COORDINATION,
    ACTION_WEEKLY_REQUEST,
    POLICY_NO_WRITE,
    POLICY_PENDING,
    POLICY_READ_ONLY,
    POLICY_WRITE,
    WORKFLOW_CHAT,
    WORKFLOW_DAILY_REPORT,
    WORKFLOW_INTERNAL_QA,
    WORKFLOW_MONTHLY_REPORT,
    WORKFLOW_UNKNOWN_OR_HELP,
    plan_user_actions,
)
from app.workflows.intake import ActiveWorkflowTask, IncomingMessageEnvelope


SUNDAY_NIGHT = datetime(2026, 7, 5, 21, 50, tzinfo=ZoneInfo("Asia/Shanghai"))


def _envelope(raw_text: str, *tasks: ActiveWorkflowTask, received_at=None) -> IncomingMessageEnvelope:
    return IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="Test User",
        dingtalk_user_id="ding-user-1",
        source="test",
        raw_text=raw_text,
        received_at=received_at,
        active_tasks=tuple(tasks),
    )


def test_action_intake_treats_bot_feedback_as_no_write_even_with_active_daily():
    plan = plan_user_actions(
        _envelope(
            "\u5565\u73a9\u610f",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-1",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert plan.action_types() == [ACTION_ASSISTANT_FEEDBACK]
    assert plan.actions[0].workflow == WORKFLOW_CHAT
    assert plan.actions[0].write_policy == POLICY_NO_WRITE
    assert "blocks_context_write" in plan.actions[0].safety_flags


def test_action_intake_treats_unwilling_daily_meta_as_no_write():
    plan = plan_user_actions(_envelope("今天好累啊，不想写日报了"))

    assert set(plan.action_types()) == {ACTION_SMALL_TALK}
    assert all(action.workflow == WORKFLOW_CHAT for action in plan.actions)
    assert all(action.write_policy == POLICY_NO_WRITE for action in plan.actions)


def test_action_intake_blocks_lifestyle_chatter_even_with_report_meta():
    plan = plan_user_actions(_envelope("哦对了今天写日报的时候食堂的红烧肉真不错"))

    assert set(plan.action_types()) == {ACTION_SMALL_TALK}
    assert all(action.workflow == WORKFLOW_CHAT for action in plan.actions)
    assert all(action.write_policy == POLICY_NO_WRITE for action in plan.actions)


def test_action_intake_blocks_weather_and_personal_chatter():
    for text in [
        "\u4eca\u5929\u96e8\u597d\u5927\u554a\uff0c\u70e6\u6b7b\u4e86\uff0c\u4f60\u4eec\u90a3\u8fb9\u4e0b\u6ca1",
        "\u4eca\u5929\u5fd9\u6b7b\u4e86\uff0c\u90fd\u6ca1\u7a7a\u559d\u6c34",
        "\u5c31\u6211\u8fd8\u6ca1\u4ea4\u554a\uff1f\u90a3\u6211\u660e\u5929\u8865\u5427",
    ]:
        plan = plan_user_actions(_envelope(text))

        assert ACTION_DAILY_WRITE not in plan.action_types()
        assert all(action.write_policy != POLICY_WRITE for action in plan.actions)


def test_action_intake_routes_summary_request_as_chat_not_problem_content():
    plan = plan_user_actions(
        _envelope(
            "\u5bf9\u4e86\uff0c\u603b\u7ed3\u4e00\u4e0b\u521a\u624d\u90a3\u4e2a\u6848\u5b50\u7684\u98ce\u9669\u70b9\u3002",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-active-summary",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert ACTION_DAILY_WRITE not in plan.action_types()
    assert any(action.write_policy == POLICY_NO_WRITE for action in plan.actions)


def test_action_intake_blocks_empty_daily_content_from_current_report():
    plan = plan_user_actions(_envelope("\u4eca\u5929\u6ca1\u5565\u7279\u522b\u7684\uff0c\u8fd8\u662f\u6628\u5929\u90a3\u4e9b\u4e8b\u3002"))

    assert not any(
        action.action_type == ACTION_DAILY_WRITE and action.payload.get("content") == "\u4eca\u5929\u6ca1\u5565\u7279\u522b\u7684"
        for action in plan.actions
    )
    assert any(
        action.action_type == ACTION_DAILY_WRITE
        and action.operation == "copy_previous"
        and action.target_field == "today_work"
        for action in plan.actions
    )


def test_action_intake_blocks_implausible_travel_from_daily_write():
    plan = plan_user_actions(_envelope("\u660e\u5929\u53bb\u8fea\u62dc\u5854\u9876\u8ddf\u5ba2\u6237\u5f00\u4f1a\uff0c\u5f97\u79df\u4e2a\u76f4\u5347\u673a\u3002"))

    assert ACTION_DAILY_WRITE not in plan.action_types()
    assert ACTION_TRAVEL_COORDINATION not in plan.action_types()
    assert all(action.write_policy != POLICY_WRITE for action in plan.actions)


def test_action_intake_blocks_fantasy_daily_content():
    plan = plan_user_actions(_envelope("\u4eca\u5929\u6211\u53d8\u6210\u4e86\u4e00\u53ea\u732b\uff0c\u660e\u5929\u8ba1\u5212\u62ef\u6551\u5730\u7403\u3002"))

    assert ACTION_DAILY_WRITE not in plan.action_types()
    assert all(action.write_policy != POLICY_WRITE for action in plan.actions)


def test_action_intake_requires_context_for_weak_destination_fragment():
    plan = plan_user_actions(_envelope("\u54e6\u5bf9\uff0c\u660e\u5929\u662f\u53bb\u82cf\u5dde\u3002"))

    assert plan.action_types() == [ACTION_DISAMBIGUATION_REQUIRED]
    assert plan.actions[0].write_policy == POLICY_PENDING


def test_action_intake_routes_robot_capability_question_as_chat_even_with_active_daily():
    plan = plan_user_actions(
        _envelope(
            "\u8bdd\u8bf4\u673a\u5668\u4eba\u4f60\u4f1a\u5199\u8bd7\u5417\uff1f",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-active",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert ACTION_DAILY_WRITE not in plan.action_types()
    assert set(plan.action_types()) == {ACTION_SMALL_TALK}


def test_action_intake_routes_copy_yesterday_report_as_copy_command():
    plan = plan_user_actions(_envelope("\u628a\u6628\u5929\u7684\u65e5\u62a5\u590d\u5236\u5230\u4eca\u5929"))

    assert plan.action_types() == [ACTION_DAILY_WRITE]
    assert plan.actions[0].operation == "copy_previous"
    assert plan.actions[0].target_field == "all"


def test_action_intake_routes_completed_yesterday_plan_to_today_work():
    plan = plan_user_actions(_envelope("\u6628\u5929\u7684\u660e\u65e5\u8ba1\u5212\u662f\u5ba1\u5408\u540c\uff0c\u5df2\u7ecf\u5ba1\u5b8c\u4e86\u3002"))

    assert ACTION_DAILY_WRITE in plan.action_types()
    writes = [action for action in plan.actions if action.action_type == ACTION_DAILY_WRITE]
    assert all(action.target_field == "today_work" for action in writes)


def test_action_intake_blocks_absurd_travel_from_daily_and_travel():
    plan = plan_user_actions(_envelope("今天去火星出差跟外星人谈并购"))

    assert set(plan.action_types()) == {ACTION_SMALL_TALK}
    assert ACTION_DAILY_WRITE not in plan.action_types()
    assert ACTION_TRAVEL_COORDINATION not in plan.action_types()


def test_action_intake_blocks_absurd_whole_message_even_with_daily_start():
    plan = plan_user_actions(_envelope("明天去月球拜访客户，帮我写日报"))

    assert ACTION_DAILY_WRITE not in plan.action_types()
    assert ACTION_TRAVEL_COORDINATION not in plan.action_types()


def test_action_intake_blocks_robot_test_question_from_daily_write():
    plan = plan_user_actions(_envelope("哈哈哈哈测试一下机器人你会写日报吗"))

    assert ACTION_DAILY_WRITE not in plan.action_types()
    assert all(action.write_policy != POLICY_WRITE for action in plan.actions)


def test_action_intake_routes_lifestyle_plus_process_question_away_from_daily():
    plan = plan_user_actions(_envelope("明天穿啥出门啊降温了，对了用印流程是啥来着？"))

    assert ACTION_DAILY_WRITE not in plan.action_types()
    assert all(action.write_policy != POLICY_WRITE for action in plan.actions)


def test_action_intake_treats_weather_complaint_as_chat():
    plan = plan_user_actions(_envelope("今天这天儿也是绝了，又闷又热，一点风都没有"))

    assert ACTION_DAILY_WRITE not in plan.action_types()
    assert set(plan.action_types()) == {ACTION_SMALL_TALK}


def test_action_intake_treats_hot_no_work_chatter_as_chat():
    plan = plan_user_actions(_envelope("哎呀今天好热，不想干活"))

    assert ACTION_DAILY_WRITE not in plan.action_types()
    assert set(plan.action_types()) == {ACTION_SMALL_TALK}


def test_action_intake_previous_daily_submission_does_not_write_current_daily():
    plan = plan_user_actions(_envelope("我今天补一下昨天的日报，昨天的工作是接待客户来访"))

    assert any(
        action.action_type == ACTION_DAILY_WRITE
        and action.write_policy == POLICY_WRITE
        and action.target.get("target_date") == "yesterday"
        for action in plan.actions
    )
    assert all(
        not (
            action.action_type == ACTION_DAILY_WRITE
            and action.write_policy == POLICY_WRITE
            and action.target.get("target_date") != "yesterday"
        )
        for action in plan.actions
    )


def test_action_intake_previous_daily_plan_completion_question_does_not_write():
    plan = plan_user_actions(_envelope("对了，昨天那个日报里的明日计划，今天完成了没"))

    assert ACTION_DAILY_WRITE not in plan.action_types()
    assert ACTION_DAILY_READ_HISTORY in plan.action_types()


def test_action_intake_ambiguous_today_yesterday_reference_requires_clarification():
    plan = plan_user_actions(_envelope("今天跟昨天差不多"))

    assert ACTION_DAILY_WRITE in plan.action_types()
    assert any(
        action.operation == "copy_previous" and action.target_field == "today_work" and action.write_policy == "write"
        for action in plan.actions
    )


def test_action_intake_current_work_retracted_to_yesterday_does_not_write_today():
    plan = plan_user_actions(_envelope("今天工作完成了，其实已经是昨天的事了，刚忙完"))

    assert ACTION_DAILY_WRITE not in plan.action_types()
    assert ACTION_DISAMBIGUATION_REQUIRED in plan.action_types()


def test_action_intake_routes_case_progress_question_to_internal_qa_not_daily():
    plan = plan_user_actions(_envelope("恒大案件进度怎样了？今天有没有新消息"))

    assert ACTION_INTERNAL_QA in plan.action_types()
    assert ACTION_DAILY_WRITE not in plan.action_types()


def test_action_intake_routes_complaint_handling_to_daily_work():
    plan = plan_user_actions(_envelope("对了，今天其实还处理了市场部的一个紧急投诉"))

    assert ACTION_DAILY_WRITE in plan.action_types()
    assert plan.write_actions()[0].target_field == "today_work"


def test_action_intake_routes_business_how_to_plan_statement_to_tomorrow_plan():
    plan = plan_user_actions(_envelope("明天得想个办法怎么跟客户B说，不然要违约了"))

    assert ACTION_DAILY_WRITE in plan.action_types()
    assert ACTION_INTERNAL_QA not in plan.action_types()
    assert plan.write_actions()[0].target_field == "tomorrow_plan"


def test_action_intake_routes_report_writing_to_tomorrow_plan():
    plan = plan_user_actions(_envelope("明天要写报告"))

    assert ACTION_DAILY_WRITE in plan.action_types()
    assert plan.write_actions()[0].target_field == "tomorrow_plan"


def test_action_intake_routes_contextual_case_followup_to_tomorrow_plan():
    plan = plan_user_actions(_envelope("那案子的进展就是这样，明天还要跟进"))

    assert ACTION_DAILY_WRITE in plan.action_types()
    assert plan.write_actions()[0].target_field == "tomorrow_plan"


def test_action_intake_routes_case_notice_only_to_case_sidecar_not_daily():
    plan = plan_user_actions(_envelope("恒大那个案子今天收到法院传票了，下个月18号开庭"))

    assert ACTION_CASE_PROGRESS in plan.action_types()
    assert ACTION_DAILY_WRITE not in plan.action_types()


def test_action_intake_routes_monthly_missing_status_query():
    plan = plan_user_actions(_envelope("大家这个月的月报都填了吗？还差谁？"))

    assert plan.action_types() == [ACTION_MONTHLY_STATUS_QUERY]
    assert plan.actions[0].workflow == WORKFLOW_MONTHLY_REPORT
    assert plan.actions[0].write_policy == POLICY_READ_ONLY


def test_action_intake_treats_morning_tired_chat_as_small_talk():
    plan = plan_user_actions(_envelope("\u65e9\u4e0a\u597d\uff0c\u4eca\u5929\u6709\u70b9\u56f0"))

    assert plan.action_types()
    assert set(plan.action_types()) == {ACTION_SMALL_TALK}
    assert {action.workflow for action in plan.actions} == {WORKFLOW_CHAT}
    assert all(action.write_policy == POLICY_NO_WRITE for action in plan.actions)


def test_action_intake_blocks_food_chatter_with_daily_time_anchors():
    plan = plan_user_actions(
        _envelope(
            "\u6211\u4eca\u5929\u53bb\u4e70\u4e86\u9e21\u86cb\u997c\uff0c"
            "\u53d1\u73b0\u9e21\u86cb\u997c\u91cc\u86cb\u7ed9\u6211\u52a0\u5c11\u4e86\u3002"
            "\u660e\u5929\u51c6\u5907\u6362\u4e00\u5bb6\u5e97\u53bb\u4e70"
        )
    )

    assert plan.action_types()
    assert set(plan.action_types()) == {ACTION_SMALL_TALK}
    assert {action.workflow for action in plan.actions} == {WORKFLOW_CHAT}
    assert all(action.write_policy == POLICY_NO_WRITE for action in plan.actions)


def test_action_intake_blocks_food_chatter_even_with_no_problem_phrase():
    plan = plan_user_actions(
        _envelope("\u4eca\u5929\u5403\u4e86\u6c49\u5821 \u660e\u5929\u51c6\u5907\u5403\u70b8\u9e21 \u6ca1\u5565\u95ee\u9898")
    )

    assert plan.action_types() == [ACTION_SMALL_TALK]
    assert plan.actions[0].workflow == WORKFLOW_CHAT
    assert plan.actions[0].write_policy == POLICY_NO_WRITE


def test_action_intake_treats_short_food_message_as_chat():
    plan = plan_user_actions(_envelope("\u4eca\u5929\u6211\u5403\u4e86\u5c0f\u756a\u8304"))

    assert plan.action_types() == [ACTION_SMALL_TALK]
    assert plan.actions[0].workflow == WORKFLOW_CHAT
    assert plan.actions[0].write_policy == POLICY_NO_WRITE


def test_action_intake_routes_monthly_status_query_read_only():
    plan = plan_user_actions(
        _envelope(
            "\u5927\u5bb6\u6708\u62a5\u586b\u7684\u600e\u6837\u4e86",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-active",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert plan.action_types() == [ACTION_MONTHLY_STATUS_QUERY]
    assert plan.actions[0].workflow == WORKFLOW_MONTHLY_REPORT
    assert plan.actions[0].write_policy == POLICY_READ_ONLY


def test_action_intake_does_not_write_daily_without_positive_work_evidence():
    plan = plan_user_actions(_envelope("\u4eca\u5929\u505a\u996d\uff0c\u660e\u5929\u6362\u4e00\u5bb6\u5e97"))

    assert ACTION_DAILY_WRITE not in plan.action_types()
    assert not plan.write_actions()


def test_action_intake_writes_daily_when_time_anchor_has_work_evidence():
    plan = plan_user_actions(
        _envelope("\u4eca\u5929\u5ba1\u4e86\u4e09\u4e2a\u5408\u540c\uff0c\u6ca1\u5565\u95ee\u9898\u3002\u660e\u5929\u5357\u4eac\u5f00\u5ead")
    )

    assert plan.action_types() == [
        ACTION_DAILY_WRITE,
        ACTION_DAILY_WRITE,
        ACTION_DAILY_WRITE,
        ACTION_TRAVEL_COORDINATION,
    ]
    assert [action.target_field for action in plan.actions if action.action_type == ACTION_DAILY_WRITE] == [
        "today_work",
        "problems",
        "tomorrow_plan",
    ]


def test_action_intake_writes_problem_update_when_field_intent_has_work_evidence():
    plan = plan_user_actions(_envelope("\u8865\u5145\u4e00\u4e2a\u95ee\u9898\uff0c\u4e1a\u52a1\u90e8\u95e8\u6750\u6599\u6ca1\u53cd\u9988"))

    assert plan.action_types() == [ACTION_DAILY_WRITE]
    assert plan.actions[0].target_field == "problems"


def test_action_intake_writes_business_problem_evidence_to_problem_field():
    plan = plan_user_actions(_envelope("\u53d1\u73b0\u4e1a\u52a1\u90e8\u95e8\u6750\u6599\u4e00\u76f4\u6ca1\u53cd\u9988"))

    assert plan.action_types() == [ACTION_DAILY_WRITE]
    assert plan.actions[0].target_field == "problems"


def test_action_intake_writes_quality_gap_to_problem_field():
    plan = plan_user_actions(_envelope("\u5ba2\u6237\u8d44\u6599\u4e0d\u5b8c\u6574"))

    assert plan.action_types() == [ACTION_DAILY_WRITE]
    assert plan.actions[0].target_field == "problems"


def test_action_intake_writes_progress_exception_to_problem_field():
    plan = plan_user_actions(_envelope("\u56de\u6b3e\u8282\u70b9\u903e\u671f"))

    assert plan.action_types() == [ACTION_DAILY_WRITE]
    assert plan.actions[0].target_field == "problems"


def test_action_intake_routes_future_problem_resolution_to_tomorrow_plan():
    plan = plan_user_actions(_envelope("\u660e\u5929\u5904\u7406\u8d44\u6599\u7f3a\u5931\u95ee\u9898"))

    assert plan.action_types() == [ACTION_DAILY_WRITE]
    assert plan.actions[0].target_field == "tomorrow_plan"


def test_action_intake_does_not_treat_case_withdrawal_as_daily_edit_without_context():
    plan = plan_user_actions(_envelope("\u64a4\u56deXX\u6848\u8d77\u8bc9\u72b6"))

    assert ACTION_DAILY_EDIT not in plan.action_types()


def test_action_intake_does_not_let_soft_edit_prefix_block_concrete_daily_content():
    plan = plan_user_actions(
        _envelope(
            "\u5443\uff0c\u8981\u4fee\u6539\u4e00\u4e0b\uff0c\u4eca\u5929\u5de5\u4f5c\u662f\u7528\u5370\u5ba1\u6838\u3002"
            "\u5ba1\u6838\u4e86\u5f88\u591a\u7528\u5370\u6750\u6599"
        )
    )

    assert ACTION_DISAMBIGUATION_REQUIRED not in plan.action_types()
    assert plan.action_types() == [ACTION_DAILY_WRITE, ACTION_DAILY_WRITE]
    assert plan.commit_policy == "partial_allowed"


def test_action_intake_blocks_bot_format_feedback_with_problem_word():
    plan = plan_user_actions(_envelope("\u611f\u89c9\u4f60\u663e\u793a\u7684\u683c\u5f0f\u6709\u95ee\u9898"))

    assert plan.action_types() == [ACTION_ASSISTANT_FEEDBACK]
    assert plan.actions[0].write_policy == POLICY_NO_WRITE


def test_action_intake_blocks_insult_feedback_with_problem_word():
    plan = plan_user_actions(_envelope("\u6709\u95ee\u9898\u3002\u4f60\u662f\u50bb\u5b50\u3002"))

    assert set(plan.action_types()) == {ACTION_SMALL_TALK, ACTION_ASSISTANT_FEEDBACK}
    assert all(action.write_policy == POLICY_NO_WRITE for action in plan.actions)


def test_action_intake_blocks_non_work_feedback_even_with_active_daily_context():
    task = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-active",
        status="collecting",
        reply_candidate=True,
    )
    examples = [
        "\u660e\u5929\u4e0d\u6d3b\u4e86",
        "\u53d1\u73b0\u86cb\u7ed9\u6211\u52a0\u5c11\u4e86",
        "\u4f60\u770b\u770b\u4f60\u8bb0\u5f55\u4e86\u591a\u5c11\u6211\u7684\u91cd\u590d\u5185\u5bb9",
        "\u4e0d\u8bf4\u4e86\u4e0d\u8bf4\u4e86\uff0c\u6211\u5feb\u88ab\u6c14\u6b7b\u4e86",
        "\u4f60\u5c31\u662f\u9a6c\u51ac\u6885\u90a3\u4e2a\u5927\u7237",
        "\u6211\u611f\u89c9\u4f60\u88ab\u5f88\u591a\u89c4\u5219\u9650\u5236\u4e86",
        "\u6ca1\u52a0\u7c97 \u6211\u8bb0\u5f97\u89c4\u5219\u91cc\u5199\u4e86\u5427",
        "\u4e0d\u662f\uff0c\u6211\u5176\u5b9e\u60f3\u8bf4\u6e38\u6cf3",
    ]

    for text in examples:
        plan = plan_user_actions(_envelope(text, task))

        assert plan.action_types()
        assert ACTION_DAILY_WRITE not in plan.action_types()
        assert all(action.write_policy == POLICY_NO_WRITE for action in plan.actions)


def test_action_intake_blocks_bot_optimization_question_with_active_daily_context():
    plan = plan_user_actions(
        _envelope(
            "\u521a\u4e0b\u4f18\u5316\u4e86\u5565",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-active",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert ACTION_DAILY_WRITE not in plan.action_types()
    assert plan.action_types() == [ACTION_INTERNAL_QA]


def test_action_intake_recognizes_explicit_display_report_without_active_task():
    plan = plan_user_actions(_envelope("\u5c55\u793a\u65e5\u62a5"))

    assert plan.action_types() == [ACTION_DAILY_READ_CURRENT]
    assert plan.actions[0].write_policy == "read_only"


def test_action_intake_recognizes_current_report_shape_question():
    plan = plan_user_actions(_envelope("\u5f53\u524d\u7684\u65e5\u62a5\u5565\u6837\u5b50\u7684\uff1f"))

    assert plan.action_types() == [ACTION_DAILY_READ_CURRENT]
    assert plan.actions[0].write_policy == "read_only"


def test_action_intake_treats_log_as_daily_report_alias_for_queries_and_edits():
    history = plan_user_actions(_envelope("\u6211\u8981\u770b\u6628\u5929\u7684\u65e5\u5fd7"))
    current = plan_user_actions(_envelope("\u5148\u7ed9\u6211\u770b\u770b\u76ee\u524d\u7684\u65e5\u5fd7"))
    edit = plan_user_actions(_envelope("\u6211\u60f3\u6539\u6628\u5929\u7684\u65e5\u5fd7"))

    assert history.action_types() == ["daily_read_history"]
    assert current.action_types() == [ACTION_DAILY_READ_CURRENT]
    assert edit.action_types() == [ACTION_DAILY_READ_HISTORY]
    assert edit.actions[0].operation == "begin_edit"
    assert edit.actions[0].write_policy == POLICY_READ_ONLY


def test_action_intake_blocks_concrete_historical_daily_delete_from_write():
    plan = plan_user_actions(_envelope("\u6628\u5929\u65e5\u62a5\u4eca\u65e5\u5de5\u4f5c\u7b2c2\u6761\u5220\u6389"))

    assert plan.action_types() == [ACTION_DAILY_READ_HISTORY]
    assert plan.actions[0].operation == "begin_edit"
    assert plan.actions[0].write_policy == POLICY_READ_ONLY
    assert "blocks_current_daily_write" in plan.actions[0].safety_flags


def test_action_intake_routes_explicit_daily_start_request():
    plan = plan_user_actions(_envelope("\u5e2e\u6211\u5199\u65e5\u62a5\u5427"))

    assert plan.action_types() == [ACTION_DAILY_WRITE]
    assert plan.actions[0].operation == "start_collection"
    assert plan.actions[0].write_policy == POLICY_NO_WRITE


def test_action_intake_daily_start_phrase_does_not_write_raw_text():
    plan = plan_user_actions(_envelope("写日报"))

    assert plan.action_types() == [ACTION_DAILY_WRITE]
    assert plan.actions[0].operation == "start_collection"
    assert plan.actions[0].write_policy == POLICY_NO_WRITE


def test_action_intake_bare_daily_start_variants_do_not_become_work_items():
    for text in ["\u65e5\u62a5", "\u5199\u4e2a\u65e5\u62a5"]:
        plan = plan_user_actions(_envelope(text))

        assert plan.action_types() == [ACTION_DAILY_WRITE]
        assert plan.actions[0].operation == "start_collection"
        assert plan.actions[0].write_policy == POLICY_NO_WRITE


def test_action_intake_splits_process_question_from_tomorrow_trip_plan():
    plan = plan_user_actions(_envelope("\u5bf9\u4e86\uff0c\u54b1\u4eec\u516c\u53f8\u7684\u7528\u5370\u6d41\u7a0b\u600e\u4e48\u8d70\uff1f\u660e\u5929\u8fd8\u8981\u53bb\u5357\u4eac\u51fa\u5dee\u76d6\u7ae0\u3002"))

    assert ACTION_INTERNAL_QA in plan.action_types()
    daily_actions = [action for action in plan.actions if action.action_type == ACTION_DAILY_WRITE]
    assert len(daily_actions) == 1
    assert daily_actions[0].target_field == "tomorrow_plan"
    assert daily_actions[0].payload["content"] == "\u660e\u5929\u8fd8\u8981\u53bb\u5357\u4eac\u51fa\u5dee\u76d6\u7ae0"


def test_action_intake_blocks_questions_reminders_and_case_progress_from_daily_write():
    for text in [
        "\u660e\u5929\u5565\u5b89\u6392\uff1f",
        "\u6052\u5927\u6848\u6709\u65b0\u8fdb\u5c55\uff0c\u521a\u6536\u5230\u6cd5\u9662\u4f20\u7968",
        "\u4eca\u5929\u7684\u4e8b\u90fd\u5904\u7406\u5b8c\u4e86",
        "\u6628\u5929\u505a\u7684\u9700\u6c42\u8bc4\u5ba1\u7ed3\u679c\u51fa\u6765\u4e86\uff0c\u901a\u8fc7\u4e86\u3002",
        "\u8bb0\u5f97\u63d0\u9192\u6211\u660e\u5929\u4ea4\u5468\u62a5",
        "\u4eca\u5929\u597d\u7d2f\uff0c\u5fd9\u4e86\u4e00\u5929\uff0c\u5148\u4e0b\u73ed\u4e86\uff0c\u660e\u5929\u518d\u7814\u7a76\u8fd9\u4e9b",
    ]:
        plan = plan_user_actions(_envelope(text))

        assert all(action.action_type != ACTION_DAILY_WRITE or action.write_policy != POLICY_WRITE for action in plan.actions)


def test_action_intake_treats_daily_meta_status_as_no_write_chat():
    examples = ["\u5199\u65e5\u62a5\u4e86", "\u6211\u5199\u65e5\u62a5\u4e86", "\u6211\u5728\u5199\u65e5\u62a5"]

    for text in examples:
        plan = plan_user_actions(
            _envelope(
                text,
                ActiveWorkflowTask(
                    workflow=WORKFLOW_DAILY_REPORT,
                    task_id="daily-meta-status",
                    status="collecting",
                    reply_candidate=True,
                ),
            )
        )

        assert plan.action_types() == [ACTION_SMALL_TALK]
        assert plan.actions[0].workflow == WORKFLOW_CHAT
        assert plan.actions[0].write_policy == POLICY_NO_WRITE
        assert "daily_meta_status" in plan.actions[0].safety_flags


def test_action_intake_blocks_bare_problem_without_daily_context():
    for text in ["\u95ee\u9898", "\u95ee\u9898\u5427", "\u6709\u70b9\u5927\u95ee\u9898"]:
        plan = plan_user_actions(_envelope(text))

        assert plan.action_types() == [ACTION_DISAMBIGUATION_REQUIRED]
        assert plan.commit_policy == "needs_clarification"


def test_action_intake_allows_bare_problem_when_daily_is_active():
    plan = plan_user_actions(
        _envelope(
            "\u95ee\u9898",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-problem",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert plan.action_types() == [ACTION_DAILY_WRITE]
    assert plan.actions[0].target_field == "problems"


def test_action_intake_blocks_bare_edit_without_daily_context():
    for text in ["\u6211\u8981\u4fee\u6539", "\u5220\u9664", "\u7b2c\u4e09\u6761\u5220\u6389\u5427"]:
        plan = plan_user_actions(_envelope(text))

        assert plan.action_types() == [ACTION_DISAMBIGUATION_REQUIRED]
        assert plan.commit_policy == "needs_clarification"


def test_action_intake_allows_explicit_report_edit_without_active_task():
    plan = plan_user_actions(_envelope("\u65e5\u62a5\u7b2c\u4e09\u6761\u5220\u6389\u5427"))

    assert plan.action_types() == [ACTION_DAILY_EDIT]
    assert plan.actions[0].workflow == WORKFLOW_DAILY_REPORT


def test_action_intake_keeps_bare_legal_research_subject_pending():
    plan = plan_user_actions(_envelope("\u7814\u7a76\u4f18\u5148\u53d7\u507f\u6743\u6848\u4f8b"))

    assert plan.action_types() == [ACTION_DISAMBIGUATION_REQUIRED]
    assert plan.commit_policy == "needs_clarification"
    assert plan.actions[0].write_policy == POLICY_PENDING


def test_action_intake_routes_explicit_legal_research_request_to_research():
    plan = plan_user_actions(
        _envelope("\u5e2e\u6211\u7814\u7a76\u4e00\u4e0b\u4f18\u5148\u53d7\u507f\u6743\u6700\u65b0\u88c1\u5224\u89c2\u70b9")
    )

    assert plan.action_types() == [ACTION_LEGAL_RESEARCH]
    assert plan.actions[0].workflow == "legal_research"
    assert plan.actions[0].write_policy == "read_only"


def test_action_intake_routes_legal_problem_question_to_research_not_daily_problem():
    plan = plan_user_actions(
        _envelope(
            "\u518d\u95ee\u4e2a\u6cd5\u5f8b\u95ee\u9898\uff0c\u7834\u4ea7\u503a\u6743\u7533\u62a5\u903e\u671f\u6709\u4ec0\u4e48\u540e\u679c\uff1f",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-legal-question",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert plan.action_types()
    assert set(plan.action_types()) == {ACTION_LEGAL_RESEARCH}
    assert all(action.workflow == "legal_research" for action in plan.actions)
    assert all(action.write_policy == "read_only" for action in plan.actions)


def test_action_intake_routes_who_confirm_question_to_internal_qa():
    plan = plan_user_actions(
        _envelope(
            "\u7528\u5370\u5ba1\u6279\u9700\u8981\u8c01\u786e\u8ba4\uff1f",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-internal-question",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert plan.action_types() == [ACTION_INTERNAL_QA]
    assert plan.actions[0].write_policy == "read_only"


def test_action_intake_routes_meaning_question_with_tomorrow_words_to_qa():
    plan = plan_user_actions(_envelope("\u4f60\u77e5\u9053\u6211\u8bf4\u7684 \u660e\u5929\u6211\u53bb\u6709\u7528\u4ec0\u4e48\u610f\u601d\u4e48\uff1f"))

    assert plan.action_types() == [ACTION_INTERNAL_QA]
    assert plan.actions[0].write_policy == "read_only"


def test_action_intake_routes_legal_consequence_question_to_qa_not_daily():
    plan = plan_user_actions(
        _envelope(
            "\u88ab\u544a\u7f3a\u5e2d \u539f\u544a\u7f3a\u5e2d\u6709\u4ec0\u4e48\u4e0d\u4e00\u6837\u7684\u540e\u679c",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-legal-consequence",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert plan.action_types() == [ACTION_INTERNAL_QA]
    assert plan.actions[0].write_policy == "read_only"


def test_action_intake_blocks_meta_chat_prefaces_with_active_daily_context():
    for text in [
        "\u6211\u60f3\u804a\u4e2a\u6848\u5b50",
        "\u60f3\u8bf4\u4e2a\u6848\u4ef6\u8fdb\u5c55",
        "\u95f2\u804a\u4f1a",
        "\u8ba8\u8bba\u5427",
    ]:
        plan = plan_user_actions(
            _envelope(
                text,
                ActiveWorkflowTask(
                    workflow=WORKFLOW_DAILY_REPORT,
                    task_id="daily-meta-chat",
                    status="collecting",
                    reply_candidate=True,
                ),
            )
        )

        assert ACTION_DAILY_WRITE not in plan.action_types()
        assert plan.actions[0].write_policy == POLICY_NO_WRITE


def test_action_intake_blocks_bot_feedback_that_mentions_becoming_dumb():
    plan = plan_user_actions(
        _envelope(
            "\u611f\u89c9\u4f60\u53d8\u8822\u4e86\u6709\u70b9",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-bot-feedback",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert plan.action_types() == [ACTION_ASSISTANT_FEEDBACK]
    assert plan.actions[0].write_policy == POLICY_NO_WRITE


def test_action_intake_v4_followup_boundaries_with_active_daily():
    active_task = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-v4-intake",
        status="collecting",
        reply_candidate=True,
    )

    for text in ["今天事情挺多的", "累死了，不想干活，明天再说吧"]:
        plan = plan_user_actions(_envelope(text, active_task))
        assert ACTION_DAILY_WRITE not in plan.action_types()

    plan = plan_user_actions(_envelope("谈了合作意向", active_task))
    assert ACTION_DAILY_WRITE in plan.action_types()


def test_action_intake_treats_court_progress_matter_as_case_progress_sidecar():
    plan = plan_user_actions(
        _envelope(
            "\u82cf\u5efa\u9662\u501f\u7ae0\u4e8b\u9879\u4eca\u5929\u548c\u6cd5\u9662\u6c9f\u901a\u4e86\u6267\u884c\u8fdb\u5c55",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-case-matter",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert ACTION_DAILY_WRITE in plan.action_types()
    assert ACTION_CASE_PROGRESS in plan.action_types()


def test_action_intake_accepts_short_specific_case_matter_without_daily_write():
    plan = plan_user_actions(
        _envelope("\u4e0b\u5468\u4e94\u53bb\u5357\u4eac\u4e2d\u9662\u6c9f\u901a\u77f3\u5c71\u6848\u8fdb\u5c55")
    )

    assert ACTION_DAILY_WRITE not in plan.action_types()
    assert ACTION_TRAVEL_COORDINATION in plan.action_types()
    assert ACTION_CASE_PROGRESS in plan.action_types()
    case_actions = [action for action in plan.actions if action.action_type == ACTION_CASE_PROGRESS]
    assert case_actions[0].target["matter_hint"] == "\u77f3\u5c71\u6848"


def test_action_intake_extracts_trip_destination_before_case_work_words():
    plan = plan_user_actions(_envelope("\u660e\u5929\u51fa\u5dee\u4e09\u4e9a\u6c9f\u901a\u6848\u4ef6"))

    assert ACTION_DAILY_WRITE in plan.action_types()
    assert ACTION_TRAVEL_COORDINATION in plan.action_types()
    assert ACTION_CASE_PROGRESS not in plan.action_types()
    travel_actions = [action for action in plan.actions if action.action_type == ACTION_TRAVEL_COORDINATION]
    assert travel_actions[0].target["destination"] == "\u4e09\u4e9a"
    assert travel_actions[0].target["date_hint"] == "tomorrow"


def test_action_intake_extracts_trip_and_specific_case_from_same_segment():
    plan = plan_user_actions(_envelope("\u660e\u5929\u51fa\u5dee\u4e09\u4e9a\u529e\u7406\u6d77\u82b1\u5c9b\u6848\u4ef6\u5f00\u5ead"))

    assert ACTION_DAILY_WRITE in plan.action_types()
    assert ACTION_TRAVEL_COORDINATION in plan.action_types()
    assert ACTION_CASE_PROGRESS in plan.action_types()
    travel_actions = [action for action in plan.actions if action.action_type == ACTION_TRAVEL_COORDINATION]
    case_actions = [action for action in plan.actions if action.action_type == ACTION_CASE_PROGRESS]
    assert travel_actions[0].target["destination"] == "\u4e09\u4e9a"
    assert case_actions[0].target["matter_hint"] == "\u6d77\u82b1\u5c9b\u6848\u4ef6"


def test_action_intake_keeps_travel_case_and_chat_actions_distinct():
    plan = plan_user_actions(
        _envelope(
            "\u660e\u5929\u51fa\u5dee\u5357\u4eac\u76d6\u7ae0\uff0c"
            "\u6052\u5927\u7834\u4ea7\u6848\u4eca\u5929\u8865\u5145\u8bc9\u8bbc\u6750\u6599\uff0c"
            "\u54c8\u54c8\u6709\u70b9\u7d27\u5f20"
        )
    )

    assert plan.action_types() == [
        ACTION_DAILY_WRITE,
        ACTION_TRAVEL_COORDINATION,
        ACTION_DAILY_WRITE,
        ACTION_CASE_PROGRESS,
        ACTION_SMALL_TALK,
    ]
    assert plan.actions[1].target["destination"] == "\u5357\u4eac"
    assert plan.actions[3].target["matter_hint"] == "\u6052\u5927\u7834\u4ea7\u6848"
    assert plan.actions[4].write_policy == POLICY_NO_WRITE


def test_action_intake_treats_nanjing_hearing_without_matter_as_daily_plan_and_travel_only():
    plan = plan_user_actions(_envelope("\u660e\u5929\u53bb\u5357\u4eac\u5f00\u5ead"))

    assert plan.action_types() == [ACTION_DAILY_WRITE, ACTION_TRAVEL_COORDINATION]
    assert ACTION_CASE_PROGRESS not in plan.action_types()
    assert plan.actions[0].target_field == "tomorrow_plan"
    assert plan.actions[1].target["destination"] == "\u5357\u4eac"


def test_action_intake_treats_agent2_status_question_as_no_write():
    plan = plan_user_actions(
        _envelope(
            "\u73b0\u5728\u662fagent2\u4e48",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-agent2-status",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert plan.action_types() == [ACTION_SMALL_TALK]
    assert plan.actions[0].write_policy == POLICY_NO_WRITE


def test_action_intake_treats_clear_current_daily_as_write_action():
    plan = plan_user_actions(
        _envelope(
            "\u6e05\u7a7a\u5f53\u524d\u65e5\u62a5",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-clear",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert plan.action_types() == [ACTION_DAILY_EDIT]
    assert plan.actions[0].operation == "clear"
    assert plan.actions[0].target_field == "all"
    assert plan.actions[0].write_policy == POLICY_WRITE


def test_action_intake_treats_meta_test_probe_as_chat_even_with_active_daily():
    examples = [
        "\u8ba9\u6211\u6d4b\u8bd5\u4e0b",
        "\u6211\u6d4b\u8bd5\u4e00\u4e0b",
        "\u6d4b\u8bd5\u4e0b",
        "\u8bd5\u4e00\u4e0b",
    ]
    for text in examples:
        plan = plan_user_actions(
            _envelope(
                text,
                ActiveWorkflowTask(
                    workflow=WORKFLOW_DAILY_REPORT,
                    task_id="daily-meta-test",
                    status="collecting",
                    reply_candidate=True,
                ),
            )
        )

        assert len(plan.actions) == 1
        assert plan.actions[0].action_type in {ACTION_SMALL_TALK, ACTION_ASSISTANT_FEEDBACK}
        assert plan.actions[0].workflow == WORKFLOW_CHAT
        assert plan.actions[0].write_policy == POLICY_NO_WRITE


def test_action_intake_treats_short_context_probe_as_chat():
    for text in ["\u548b\u8bf4", "\u600e\u4e48\u95f2\u804a", "\u53ef\u4ee5\u548c\u6211\u804a\u804a\u5417\uff1f", "\u548c\u6211\u804a\u5929"]:
        plan = plan_user_actions(_envelope(text))

        assert plan.action_types() == [ACTION_SMALL_TALK]
        assert plan.actions[0].workflow == WORKFLOW_CHAT
    assert plan.actions[0].write_policy == POLICY_NO_WRITE


def test_action_intake_routes_case_count_question_to_internal_qa_not_case_progress():
    plan = plan_user_actions(
        _envelope(
            "\u738b\u559c\u88ab\u544a\u6848\u4ef6\u6709\u591a\u5c11",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-case-count",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert plan.action_types() == [ACTION_INTERNAL_QA]
    assert ACTION_CASE_PROGRESS not in plan.action_types()
    assert plan.actions[0].workflow == WORKFLOW_INTERNAL_QA
    assert plan.actions[0].write_policy == POLICY_READ_ONLY


def test_action_intake_routes_team_defendant_data_question_to_internal_qa_not_case_progress():
    plan = plan_user_actions(
        _envelope(
            "\u6cd5\u52a1\u4e8c\u90e8\u88ab\u544a\u6848\u4ef6\u6709\u591a\u5c11",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-team-case-count",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert plan.action_types() == [ACTION_INTERNAL_QA]
    assert ACTION_CASE_PROGRESS not in plan.action_types()
    assert plan.actions[0].workflow == WORKFLOW_INTERNAL_QA
    assert plan.actions[0].write_policy == POLICY_READ_ONLY


def test_action_intake_routes_defendant_metric_requests_to_internal_qa_not_daily_current():
    examples = [
        "\u603b\u4f53\u88ab\u544a\u5b58\u91cf\u53d1\u6211",
        "\u6574\u4f53\u88ab\u544a\u5b58\u91cf\u770b\u4e0b",
        "\u5168\u90e8\u88ab\u544a\u65b0\u589e\u53d1\u4e00\u4e0b",
        "\u88ab\u544a\u5b58\u91cf\u7edf\u8ba1\u4e0b",
    ]
    for text in examples:
        plan = plan_user_actions(
            _envelope(
                text,
                ActiveWorkflowTask(
                    workflow=WORKFLOW_DAILY_REPORT,
                    task_id="daily-defendant-metric",
                    status="collecting",
                    reply_candidate=True,
                ),
            )
        )

        assert plan.action_types() == [ACTION_INTERNAL_QA]
        assert ACTION_DAILY_READ_CURRENT not in plan.action_types()
        assert plan.actions[0].workflow == WORKFLOW_INTERNAL_QA
        assert plan.actions[0].write_policy == POLICY_READ_ONLY


def test_action_intake_routes_short_team_followup_to_internal_qa_not_daily_clarification():
    plan = plan_user_actions(
        _envelope(
            "\u6cd5\u52a1\u4e8c\u90e8\u5462",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-team-followup",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert plan.action_types() == [ACTION_INTERNAL_QA]
    assert plan.actions[0].workflow == WORKFLOW_INTERNAL_QA
    assert plan.actions[0].write_policy == POLICY_READ_ONLY


def test_action_intake_treats_short_vent_as_chat_but_keeps_business_problem():
    vent = plan_user_actions(_envelope("\u5988\u7684"))

    assert vent.action_types() == [ACTION_SMALL_TALK]
    assert vent.actions[0].workflow == WORKFLOW_CHAT
    assert vent.actions[0].write_policy == POLICY_NO_WRITE

    bored = plan_user_actions(
        _envelope(
            "\u597d\u65e0\u804a",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-chat-followup",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert bored.action_types() == [ACTION_SMALL_TALK]
    assert bored.actions[0].workflow == WORKFLOW_CHAT
    assert bored.actions[0].write_policy == POLICY_NO_WRITE

    feedback = plan_user_actions(_envelope("\u8fd9\u7cfb\u7edf\u4e0d\u597d\u7528"))

    assert feedback.action_types() == [ACTION_ASSISTANT_FEEDBACK]
    assert feedback.actions[0].workflow == WORKFLOW_CHAT
    assert feedback.actions[0].write_policy == POLICY_NO_WRITE

    business_problem = plan_user_actions(_envelope("\u4e1a\u52a1\u90e8\u95e8\u6750\u6599\u6ca1\u53cd\u9988\uff0c\u5988\u7684"))

    assert ACTION_DAILY_WRITE in business_problem.action_types()
    assert business_problem.write_actions()[0].target_field == "problems"
    assert all(action.write_policy != POLICY_PENDING for action in business_problem.actions)


def test_action_intake_routes_short_delete_reference_to_daily_edit_when_daily_is_active():
    plan = plan_user_actions(
        _envelope(
            "\u5220\u6389\u5427",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-short-delete",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert plan.action_types() == [ACTION_DAILY_EDIT]
    assert plan.actions[0].workflow == WORKFLOW_DAILY_REPORT
    assert plan.actions[0].write_policy == "write"


def test_action_intake_routes_active_daily_negative_replacement_to_daily_edit_not_qa():
    plan = plan_user_actions(
        _envelope(
            "明天不是去南京，是去上海开庭",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-negative-replacement",
                status="pending_confirmation",
                reply_candidate=True,
                awaiting_confirmation=True,
            ),
        )
    )

    assert plan.action_types() == [ACTION_DAILY_EDIT]
    assert plan.actions[0].workflow == WORKFLOW_DAILY_REPORT
    assert plan.actions[0].write_policy == "write"


def test_action_intake_treats_lifestyle_questions_as_no_write_chat():
    examples = [
        "明天穿啥出门",
        "明天穿什么出门？",
        "明天要不要带伞",
        "今天中午吃啥",
        "后天冷不冷",
        "明天跑步穿短袖还是长袖",
        "周末去哪玩",
        "今天好困怎么办",
        "晚上吃火锅怎么样",
        "明天会不会下雨",
    ]

    for text in examples:
        plan = plan_user_actions(_envelope(text))

        assert len(plan.actions) == 1
        assert plan.actions[0].action_type in {ACTION_SMALL_TALK, ACTION_ASSISTANT_FEEDBACK}
        assert plan.actions[0].workflow == WORKFLOW_CHAT
        assert plan.actions[0].write_policy == POLICY_NO_WRITE


def test_action_intake_treats_short_chatter_and_vulgar_jokes_as_no_write_chat():
    examples = [
        "哎",
        "哈哦",
        "明天吃屎",
        "今天一坨屎",
    ]

    for text in examples:
        plan = plan_user_actions(
            _envelope(
                text,
                ActiveWorkflowTask(
                    workflow=WORKFLOW_DAILY_REPORT,
                    task_id="daily-short-chat",
                    status="collecting",
                    reply_candidate=True,
                ),
            )
        )

        assert len(plan.actions) == 1
        assert plan.actions[0].action_type in {ACTION_SMALL_TALK, ACTION_ASSISTANT_FEEDBACK}
        assert plan.actions[0].workflow == WORKFLOW_CHAT
        assert plan.actions[0].write_policy == POLICY_NO_WRITE


def test_action_intake_keeps_real_tomorrow_work_plans_writable():
    examples = [
        "明天整理合同材料",
        "明天去南京开庭",
        "明天沟通恒大案件执行进展",
        "明天跟进客户资料补充",
        "明天处理用印审批",
        "明天完善案件台账",
        "明天测试日报系统",
        "明天和业务部门对接回款材料",
    ]

    for text in examples:
        plan = plan_user_actions(_envelope(text))

        assert ACTION_DAILY_WRITE in plan.action_types()
        assert plan.write_actions()[0].target_field == "tomorrow_plan"


def test_action_intake_daily_field_marker_outweighs_weekly_object():
    examples = [
        "明日计划整理自己工作模块，修正周报，来函进展跟进更新",
        "今日工作改成 今天完成了基础skillhub上mcp的搭建，明天计划完成周报填写的发送",
    ]

    for text in examples:
        plan = plan_user_actions(
            _envelope(
                text,
                ActiveWorkflowTask(
                    workflow=WORKFLOW_DAILY_REPORT,
                    task_id="daily-weekly-object",
                    status="collecting",
                    reply_candidate=True,
                ),
            )
        )

        assert ACTION_DAILY_WRITE in plan.action_types() or ACTION_DAILY_EDIT in plan.action_types()
        assert WORKFLOW_DAILY_REPORT in {action.workflow for action in plan.actions}


def test_action_intake_routes_untimed_operational_closure_to_daily():
    plan = plan_user_actions(_envelope("日常用印审核登记，施工合同用印归档。线上流程闭环"))

    assert ACTION_DAILY_WRITE in plan.action_types()
    assert plan.write_actions()[0].target_field == "today_work"


def test_action_intake_routes_sunday_monday_trip_to_tomorrow_plan_and_travel():
    plan = plan_user_actions(
        _envelope("\u5468\u4e00\u9884\u8ba1\u51fa\u5dee\u53bb\u5170\u5dde", received_at=SUNDAY_NIGHT)
    )

    assert plan.action_types() == [ACTION_DAILY_WRITE, ACTION_TRAVEL_COORDINATION]
    assert plan.actions[0].target_field == "tomorrow_plan"
    assert plan.actions[1].target["destination"] == "\u5170\u5dde"
    assert plan.actions[1].target["date_hint"] == "tomorrow"
    assert plan.actions[1].target["status"] == "planned"


def test_action_intake_keeps_sunday_tuesday_trip_as_future_travel_candidate_only():
    plan = plan_user_actions(
        _envelope("\u5468\u4e8c\u5e94\u8be5\u51fa\u5dee\u53bb\u897f\u5b81", received_at=SUNDAY_NIGHT)
    )

    assert plan.action_types() == [ACTION_TRAVEL_COORDINATION]
    assert plan.actions[0].target["destination"] == "\u897f\u5b81"
    assert plan.actions[0].target["date_hint"] == "future_weekday"
    assert plan.actions[0].target["status"] == "planned"


def test_action_intake_routes_time_anchored_research_sentence_to_daily_write():
    plan = plan_user_actions(_envelope("\u4eca\u5929\u7814\u7a76\u4e86\u4f18\u5148\u53d7\u507f\u6743\u6848\u4f8b"))

    assert plan.action_types() == [ACTION_DAILY_WRITE]
    assert plan.actions[0].target_field == "today_work"


def test_action_intake_treats_generic_case_material_plan_as_daily_only():
    plan = plan_user_actions(
        _envelope("\u660e\u65e5\u8ba1\u5212\u589e\u52a0\u7ee7\u7eed\u8ddf\u8fdb\u6848\u4ef6\u6750\u6599")
    )

    assert plan.action_types() == [ACTION_DAILY_WRITE]
    assert ACTION_CASE_PROGRESS not in plan.action_types()
    assert plan.actions[0].target_field == "tomorrow_plan"


def test_action_intake_splits_daily_question_and_travel_in_one_turn():
    plan = plan_user_actions(
        _envelope(
            "\u4eca\u5929\u5b8c\u6210\u5408\u540c\u5ba1\u6838\u3002"
            "\u987a\u4fbf\u95ee\u4e0b\u5370\u7ae0\u6d41\u7a0b\u662f\u4ec0\u4e48\uff1f"
            "\u660e\u5929\u53bb\u5357\u4eac\u51fa\u5dee"
        )
    )

    assert plan.action_types() == [
        ACTION_DAILY_WRITE,
        ACTION_INTERNAL_QA,
        ACTION_DAILY_WRITE,
        ACTION_TRAVEL_COORDINATION,
    ]
    assert plan.actions[0].target_field == "today_work"
    assert plan.actions[2].target_field == "tomorrow_plan"
    assert plan.actions[3].target["destination"] == "\u5357\u4eac"


def test_action_intake_inherits_tomorrow_plan_for_followup_segment():
    plan = plan_user_actions(
        _envelope(
            "\u660e\u5929\u53bb\u626c\u5dde\u51fa\u5dee\u5f00\u5ead\uff0c"
            "\u56de\u6765\u540e\u7ee7\u7eed\u5b8c\u5584\u6848\u4ef6\u53f0\u8d26"
        )
    )

    assert plan.action_types() == [ACTION_DAILY_WRITE, ACTION_TRAVEL_COORDINATION, ACTION_DAILY_WRITE]
    assert [action.target_field for action in plan.actions if action.action_type == ACTION_DAILY_WRITE] == [
        "tomorrow_plan",
        "tomorrow_plan",
    ]


def test_action_intake_splits_today_work_and_tomorrow_plan_list_items():
    plan = plan_user_actions(
        _envelope(
            "今天优化了日报agent，参加了AI应用比赛复审会议，收集公众号被告案件信息通报，"
            "明天计划继续优化日报agent、完成公众号通报内容编辑，完成被告板块季度邮件。",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-mixed-list",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    daily_actions = [action for action in plan.actions if action.action_type == ACTION_DAILY_WRITE]
    assert [action.target_field for action in daily_actions] == [
        "today_work",
        "today_work",
        "today_work",
        "tomorrow_plan",
        "tomorrow_plan",
        "tomorrow_plan",
    ]
    assert [action.payload["content"] for action in daily_actions] == [
        "今天优化了日报agent",
        "参加了AI应用比赛复审会议",
        "收集公众号被告案件信息通报",
        "明天计划继续优化日报agent",
        "完成公众号通报内容编辑",
        "完成被告板块季度邮件",
    ]


def test_action_intake_structured_daily_headings_route_sections_without_writing_headings():
    raw = (
        "今日工作完成情况\n"
        "完成用户中心模块联调\n"
        "修复线上支付回调问题\n"
        "参与技术方案评审\n"
        "明日工作计划\n"
        "启动支付回调灰度验证\n"
        "输出差异化竞争策略简报\n"
        "碰到问题与风险\n"
        "第三方接口不稳定影响联调效率\n"
        "测试资源紧张可能影响验收排期"
    )
    plan = plan_user_actions(
        _envelope(
            raw,
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-structured-sections",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    daily_actions = [action for action in plan.actions if action.action_type == ACTION_DAILY_WRITE]
    assert [action.target_field for action in daily_actions] == [
        "today_work",
        "today_work",
        "today_work",
        "tomorrow_plan",
        "tomorrow_plan",
        "problems",
        "problems",
    ]
    assert [action.payload["content"] for action in daily_actions] == [
        "完成用户中心模块联调",
        "修复线上支付回调问题",
        "参与技术方案评审",
        "启动支付回调灰度验证",
        "输出差异化竞争策略简报",
        "第三方接口不稳定影响联调效率",
        "测试资源紧张可能影响验收排期",
    ]
    assert ACTION_INTERNAL_QA not in plan.action_types()
    assert ACTION_TRAVEL_COORDINATION not in plan.action_types()


def test_action_intake_future_trip_stays_out_of_daily_but_creates_candidates():
    plan = plan_user_actions(
        _envelope(
            "后天出差南通沟通保利案件调解",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-future-trip",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    daily_actions = [action for action in plan.actions if action.action_type == ACTION_DAILY_WRITE]
    assert daily_actions == []
    assert ACTION_TRAVEL_COORDINATION in plan.action_types()
    assert ACTION_CASE_PROGRESS in plan.action_types()


def test_action_intake_future_case_hearing_without_place_is_case_candidate_only():
    plan = plan_user_actions(
        _envelope(
            "后天去保利案件开庭",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-future-case-hearing",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert ACTION_DAILY_WRITE not in plan.action_types()
    assert ACTION_TRAVEL_COORDINATION not in plan.action_types()
    assert ACTION_CASE_PROGRESS in plan.action_types()
    case_action = next(action for action in plan.actions if action.action_type == ACTION_CASE_PROGRESS)
    assert case_action.target["matter_hint"] == "保利案件"


def test_action_intake_explicit_tomorrow_plan_wins_over_life_words():
    plan = plan_user_actions(_envelope("\u660e\u65e5\u8ba1\u5212 \u6211\u660e\u5929\u8ba1\u5212\u53bb\u6e38\u6cf3"))

    assert plan.action_types() == [ACTION_DAILY_WRITE]
    assert plan.actions[0].target_field == "tomorrow_plan"


def test_action_intake_meta_correction_does_not_become_daily_write():
    plan = plan_user_actions(
        _envelope(
            "\u4e0d\u662f\uff0c\u6211\u5176\u5b9e\u60f3\u8bf4\u6e38\u6cf3",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-meta-correction",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert ACTION_DAILY_WRITE not in plan.action_types()
    assert plan.actions[0].write_policy == "no_write"


def test_action_intake_recognizes_bare_display_request_when_daily_is_active():
    plan = plan_user_actions(
        _envelope(
            "\u53d1\u6211",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-2",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert plan.action_types() == [ACTION_DAILY_READ_CURRENT]
    assert plan.actions[0].write_policy == "read_only"


def test_action_intake_models_active_daily_confirmation_as_write_action():
    plan = plan_user_actions(
        _envelope(
            "\u786e\u8ba4\u63d0\u4ea4",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-confirm",
                status="pending_confirmation",
                reply_candidate=True,
                awaiting_confirmation=True,
            ),
        )
    )

    assert plan.action_types() == [ACTION_DAILY_CONFIRM]
    assert plan.actions[0].operation == "confirm"
    assert plan.actions[0].write_policy == POLICY_WRITE


def test_action_intake_models_copy_previous_daily_request_as_write_action():
    plan = plan_user_actions(_envelope("\u628a\u6628\u5929\u7684\u5e26\u8fc7\u6765"))

    assert plan.action_types() == [ACTION_DAILY_WRITE]
    assert plan.actions[0].operation == "copy_previous"
    assert plan.actions[0].target_field == "all"
    assert "destructive_or_overwrite" in plan.actions[0].safety_flags


def test_action_intake_models_repeat_previous_work_as_today_work_copy_not_raw_fill():
    plan = plan_user_actions(_envelope("就是昨天那份日报的内容，今天接着干，没啥变化"))

    assert plan.action_types() == [ACTION_DAILY_WRITE]
    assert plan.actions[0].operation == "copy_previous"
    assert plan.actions[0].target_field == "today_work"
    assert "destructive_or_overwrite" not in plan.actions[0].safety_flags


def test_action_intake_blocks_previous_day_makeup_without_today_target():
    plan = plan_user_actions(_envelope("昨天忘了写，我昨天下午去了趟法院立案。"))

    assert plan.action_types() == [ACTION_DAILY_READ_HISTORY]
    assert plan.actions[0].operation == "begin_edit"
    assert plan.actions[0].write_policy == POLICY_READ_ONLY


def test_action_intake_keeps_future_case_schedule_out_of_daily_report():
    plan = plan_user_actions(_envelope("保利那个案子下周二开庭，我们材料都准备好了，应该没问题。"))

    assert ACTION_DAILY_WRITE not in plan.action_types()
    assert ACTION_CASE_PROGRESS in plan.action_types()


def test_action_intake_writes_daily_start_payload_when_content_is_inline():
    plan = plan_user_actions(_envelope("写日报了：上午整理档案，下午接待客户咨询。"))

    assert plan.action_types() == [ACTION_DAILY_WRITE]
    assert plan.actions[0].operation == "fill"
    assert plan.actions[0].target_field == "today_work"
    assert plan.actions[0].payload["content"] == "上午整理档案，下午接待客户咨询。"


def test_action_intake_blocks_cross_date_daily_copy_until_source_target_contract_exists():
    plan = plan_user_actions(_envelope("\u590d\u5236\u524d\u5929\u7684\u6c47\u62a5\u5185\u5bb9\u5230\u6628\u5929\u7684\u91cc\u9762"))

    assert plan.action_types() == [ACTION_DISAMBIGUATION_REQUIRED]
    assert plan.actions[0].workflow == WORKFLOW_UNKNOWN_OR_HELP
    assert plan.actions[0].write_policy == POLICY_PENDING
    assert "unsupported_cross_date_daily_copy" in plan.actions[0].safety_flags


def test_action_intake_models_short_daily_confirmation_variants():
    task = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-confirm",
        status="pending_confirmation",
        reply_candidate=True,
        awaiting_confirmation=True,
    )
    for text in ["\u5bf9\u7684", "\u662f", "\u4ea4\u5427", "\u4ea4\u4e86"]:
        plan = plan_user_actions(_envelope(text, task))

        assert plan.action_types() == [ACTION_DAILY_CONFIRM]
        assert plan.actions[0].operation == "confirm"


def test_action_intake_models_daily_worded_confirmation_as_confirm():
    task = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-confirm",
        status="pending_confirmation",
        reply_candidate=True,
        awaiting_confirmation=True,
    )

    plan = plan_user_actions(_envelope("日报就这样吧。", task))

    assert plan.action_types() == [ACTION_DAILY_CONFIRM]
    assert plan.actions[0].operation == "confirm"


def test_action_intake_keeps_legal_opinion_replacement_as_daily_edit():
    task = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-legal-opinion-edit",
        status="collecting",
        reply_candidate=True,
    )

    plan = plan_user_actions(_envelope("不对，把法律意见书初稿改成法律意见书定稿，已经发客户了。", task))

    assert plan.action_types() == [ACTION_DAILY_EDIT]
    assert ACTION_LEGAL_RESEARCH not in plan.action_types()


def test_action_intake_blocks_lifestyle_question_with_incidental_work_context():
    plan = plan_user_actions(_envelope("今天热死了，明天穿啥出门啊，还要见客户"))

    assert plan.action_types() == [ACTION_SMALL_TALK]
    assert plan.actions[0].write_policy == POLICY_NO_WRITE


def test_action_intake_routes_template_download_question_to_internal_qa_only():
    plan = plan_user_actions(_envelope("顺便问下，合同审核标准模板在哪下载？"))

    assert plan.action_types() == [ACTION_INTERNAL_QA]
    assert ACTION_DAILY_WRITE not in plan.action_types()


def test_action_intake_blocks_conditional_schedule_check_from_daily_write():
    plan = plan_user_actions(_envelope("帮我看看明天日程，有没有冲突，如果没事我就去法院了"))

    assert plan.action_types() == [ACTION_INTERNAL_QA]
    assert ACTION_DAILY_WRITE not in plan.action_types()


def test_action_intake_routes_process_learning_context_to_internal_qa():
    plan = plan_user_actions(_envelope("那今天就先研究流程吧，没啥别的了。"))

    assert plan.action_types() == [ACTION_INTERNAL_QA]
    assert ACTION_DAILY_WRITE not in plan.action_types()


def test_action_intake_keeps_daily_with_weekly_summary_as_daily_work():
    plan = plan_user_actions(_envelope("今天主要还是搞那个数据清洗，明天打算换个算法试试，顺便还得弄周报汇总"))

    assert ACTION_DAILY_WRITE in plan.action_types()
    assert ACTION_WEEKLY_REQUEST not in plan.action_types()


def test_action_intake_treats_vague_today_status_as_chat_not_daily():
    plan = plan_user_actions(_envelope("今天还行吧"))

    assert plan.action_types() == [ACTION_SMALL_TALK]
    assert plan.actions[0].write_policy == POLICY_NO_WRITE


def test_action_intake_blocks_vague_tomorrow_workload_statement():
    plan = plan_user_actions(_envelope("明天应该也差不多，手头事多"))

    assert set(plan.action_types()) == {ACTION_SMALL_TALK}
    assert ACTION_DAILY_WRITE not in plan.action_types()


def test_action_intake_keeps_morning_data_check_as_tomorrow_plan_after_lifestyle_clause():
    plan = plan_user_actions(
        _envelope("\u7b97\u4e86\u4e0d\u7528\u7ba1\u5929\u6c14\uff0c\u660e\u65e9\u518d\u5bf9\u4e00\u904d\u6570\u636e\uff0c\u540e\u5929\u63d0\u4ea4")
    )

    writes = [action for action in plan.actions if action.action_type == ACTION_DAILY_WRITE]
    assert len(writes) == 1
    assert writes[0].target_field == "tomorrow_plan"


def test_action_intake_keeps_business_work_when_lifestyle_and_question_are_mixed():
    plan = plan_user_actions(_envelope("今天好热，干了一点点活，审了两个合同，另外问下用印流程怎么走，明天再弄"))

    assert ACTION_DAILY_WRITE in plan.action_types()
    assert ACTION_INTERNAL_QA in plan.action_types()


def test_action_intake_treats_write_weekly_as_daily_item_in_explicit_daily_list():
    plan = plan_user_actions(_envelope("日报：今天干了三件事，改bug、写周报、开会"))

    assert ACTION_DAILY_WRITE in plan.action_types()
    assert ACTION_WEEKLY_REQUEST not in plan.action_types()


def test_action_intake_models_problem_field_reply_and_wording_edit():
    task = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-problem",
        status="collecting",
        reply_candidate=True,
    )

    no_problem = plan_user_actions(_envelope("\u6ca1\u78b0\u5230\u4ec0\u4e48\u95ee\u9898", task))
    polish = plan_user_actions(_envelope("\u95ee\u9898\u8868\u8ff0\u5e2e\u6211\u4f18\u9009\u4e0b\u5427", task))

    assert no_problem.action_types() == [ACTION_DAILY_WRITE]
    assert no_problem.actions[0].target_field == "problems"
    assert polish.action_types() == [ACTION_DAILY_EDIT]
    assert polish.actions[0].operation == "edit"


def test_action_intake_models_short_leave_as_daily_content():
    plan = plan_user_actions(_envelope("\u4f11\u5047"))

    assert plan.action_types() == [ACTION_DAILY_WRITE]
    assert plan.actions[0].target_field == "today_work"


def test_action_intake_routes_daily_edit_command_as_edit_action():
    plan = plan_user_actions(
        _envelope(
            "\u628a\u7b2c\u4e00\u6761\u6539\u6210\u5408\u540c\u5ba1\u6838\u5df2\u5b8c\u6210",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-edit",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert plan.action_types() == [ACTION_DAILY_EDIT]


def test_action_intake_routes_spoken_correction_as_daily_edit_with_active_context():
    plan = plan_user_actions(
        _envelope(
            "明日计划里面的飞速收款飞速写错了，是非的非诉讼的诉",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-spoken-correction",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert plan.action_types() == [ACTION_DAILY_EDIT]
    assert plan.actions[0].target_field == "unknown"
    assert plan.actions[0].write_policy == "write"


def test_action_intake_treats_generic_case_report_list_as_daily_work_not_case_progress():
    plan = plan_user_actions(
        _envelope(
            "用印，合同归档，月度收款计划，旬计划调整，法务小群案件汇报",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-generic-case-report",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert ACTION_DAILY_WRITE in plan.action_types()
    assert ACTION_CASE_PROGRESS not in plan.action_types()
    assert plan.actions[0].workflow == WORKFLOW_DAILY_REPORT
    assert plan.actions[0].operation == "fill"
    assert plan.actions[0].payload["content"] == "用印，合同归档，月度收款计划，旬计划调整，法务小群案件汇报"


def test_action_intake_treats_case_progress_system_build_as_daily_edit_not_case_progress():
    plan = plan_user_actions(
        _envelope(
            "3. 构建原告案件进展系统\n"
            "4. 实现日报中提及案件时自动关联并补充进展\n"
            "5. 以及在固定时间和节点询问进展\n"
            "这三个是同一条",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-case-product-build",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert ACTION_DAILY_WRITE in plan.action_types()
    assert ACTION_CASE_PROGRESS not in plan.action_types()


def test_action_intake_preserves_comma_separated_edit_indices():
    text = "\u5408\u5e76\u4eca\u65e5\u5de5\u4f5c\u7684 2\uff0c3\uff0c4\uff0c5"
    plan = plan_user_actions(
        _envelope(
            text,
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-edit-list",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert plan.action_types() == [ACTION_DAILY_EDIT]
    assert plan.actions[0].payload["content"] == text


def test_action_intake_preserves_range_reference_split_by_comma():
    text = "5\u52307\u6761\u662f\u540c\u4e00\u6761\uff0c\u5408\u5e76\u4e00\u4e0b"
    plan = plan_user_actions(
        _envelope(
            text,
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-edit-range",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert plan.action_types() == [ACTION_DAILY_EDIT]
    assert plan.actions[0].payload["content"] == text


def test_action_intake_still_splits_independent_field_edits():
    plan = plan_user_actions(
        _envelope(
            "\u95ee\u98982.\u53bb\u6389\uff0c\u660e\u65e5\u8ba1\u52122.3\u5408\u5e76",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-edit-two-fields",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert plan.action_types() == [ACTION_DAILY_EDIT, ACTION_DAILY_EDIT]
    assert [action.payload["content"] for action in plan.actions] == [
        "\u95ee\u98982.\u53bb\u6389",
        "\u660e\u65e5\u8ba1\u52122.3\u5408\u5e76",
    ]


def test_action_intake_preserves_monthly_metric_action_plan_rewrite():
    text = "\u628a7. AI\u573a\u666f\u8d4b\u80fd\u63d0\u6548\u7684\u884c\u52a8\u65b9\u6848\uff0c\u6539\u4e3a1. \u68b3\u7406\u5b8c\u6210\u60c5\u51b5\u3001\u4f1a\u8bae\u3001\u5206\u89e3"
    plan = plan_user_actions(_envelope(text))

    assert plan.action_types() == ["monthly_reply"]
    assert plan.actions[0].workflow == "monthly_report"
    assert plan.actions[0].payload["content"] == text


def test_action_intake_routes_yesterday_todo_done_before_qa():
    for text in ["\u6628\u5929\u5f85\u529e\u90fd\u5b8c\u6210\u4e86", "\u6628\u65e5\u5b89\u6392\u5168\u90e8\u641e\u5b9a", "\u6628\u5929\u7684\u4e8b\u9879\u505a\u5b8c\u4e86"]:
        plan = plan_user_actions(_envelope(text))

        assert plan.action_types() == [ACTION_DAILY_WRITE]
        assert plan.actions[0].workflow == WORKFLOW_DAILY_REPORT
        assert plan.actions[0].target_field == "today_work"


def test_action_intake_observation_does_not_leak_raw_text():
    plan = plan_user_actions(_envelope("\u4eca\u5929\u5b8c\u6210\u5408\u540c\u5ba1\u6838"))
    observation = plan.as_observation()

    assert observation["source_text_hash"]
    assert "raw_text" not in observation
    assert all("raw_text" not in action for action in observation["actions"])


def test_action_intake_keeps_business_from_lifestyle_mixed_sentence():
    plan = plan_user_actions(
        _envelope(
            "\u4eca\u5929\u5ba1\u4e86\u4e2a\u79df\u8d41\u534f\u8bae\uff0c\u5934\u660f\u8111\u6da8\u7684\uff0c\u98df\u5802\u7684\u7ea2\u70e7\u8089\u4e5f\u592a\u54b8\u4e86\uff0c\u660e\u5929\u5f97\u65e9\u8d77\u53bb\u5ba2\u6237\u90a3"
        )
    )

    assert ACTION_DAILY_WRITE in plan.action_types()
    fields = [action.target_field for action in plan.actions if action.action_type == ACTION_DAILY_WRITE]
    assert "today_work" in fields
    assert "tomorrow_plan" in fields


def test_action_intake_blocks_vague_completion_and_referential_risk_reminder():
    active = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-vague-reference",
        status="collecting",
        reply_candidate=True,
    )

    vague = plan_user_actions(_envelope("\u4eca\u5929\u90a3\u4e2a\u4e8b\u641e\u5b9a\u4e86", active))
    reminder = plan_user_actions(_envelope("\u521a\u624d\u8bf4\u7684\u90a3\u4e2a\u98ce\u9669\u8bb0\u5f97\u5199\u4e0a\u54c8", active))

    assert all(action.write_policy != "write" for action in vague.actions)
    assert all(action.write_policy != "write" for action in reminder.actions)


def test_action_intake_treats_tomorrow_ask_judge_as_daily_plan_not_qa():
    plan = plan_user_actions(_envelope("\u660e\u5929\u95ee\u4e0b\u6cd5\u5b98"))

    assert ACTION_DAILY_WRITE in plan.action_types()
    assert any(action.target_field == "tomorrow_plan" and action.write_policy == "write" for action in plan.actions)
    assert ACTION_INTERNAL_QA not in plan.action_types()


def test_action_intake_blocks_conditional_feedback_request_without_report_content():
    plan = plan_user_actions(_envelope("\u6cd5\u9662\u6536\u5230\u7684\u8bdd\u7ed9\u4e2a\u53cd\u9988"))

    assert all(action.write_policy != "write" for action in plan.actions)


def test_action_intake_accepts_jiner_rd_iteration_and_bug_update():
    plan = plan_user_actions(
        _envelope("\u4eca\u513f\u548c\u7814\u53d1\u5bf9\u4e86\u4e0b\u8fed\u4ee3\u9700\u6c42\uff0c\u4ea4\u4ed8\u5b9a\u5230\u5468\u4e94\u4e86\uff0c\u4f46\u7b2c\u4e09\u65b9\u63a5\u53e3\u53ef\u80fd\u62d6\u540e\u817f\uff0c\u5f97\u8ba9\u5546\u52a1\u53bb\u50ac\u50ac\u3002\u8fd8\u4fee\u4e86\u4fe9\u7ebf\u4e0abug\u3002")
    )

    assert ACTION_DAILY_WRITE in plan.action_types()
    assert any(action.target_field == "today_work" and action.write_policy == "write" for action in plan.actions)


def test_action_intake_keeps_failed_system_trial_as_problem_even_with_service_request():
    plan = plan_user_actions(
        _envelope("\u5bf9\u4e86\uff0c\u6628\u5929\u4f60\u8bf4\u7684\u90a3\u4e2a\u4f1a\u8bae\u5ba4\u9884\u8ba2\u7cfb\u7edf\uff0c\u4eca\u5929\u8bd5\u4e86\u4e0b\u8fd8\u662f\u4e0d\u884c\uff0c\u4f60\u5e2e\u6211\u770b\u770b\u5457\u3002")
    )

    assert any(action.target_field == "problems" and action.write_policy == "write" for action in plan.actions)


def test_action_intake_routes_questions_and_service_requests_read_only():
    active = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-question-guard",
        status="collecting",
        reply_candidate=True,
    )

    for text in [
        "\u90a3\u6628\u5929\u90a3\u4efd\u5408\u540c\u6709\u95ee\u9898\u5417\uff1f",
        "\u7528\u5370\u5ba1\u6279\u6d41\u7a0b\u7b2c\u4e00\u6b65\u662f\u5565\uff1f\u6025\u6025\u6025\uff01\uff01\uff01",
        "\u90a3\u4e2a\u5408\u540cA\u6211\u63d0\u5230\u7684\u98ce\u9669\u70b9\u4f60\u5e2e\u6211\u8bb0\u5230\u65e5\u62a5\u91cc\u4e86\u5417",
        "\u8bb0\u5f97\u5e2e\u6211\u7533\u8bf7\u51fa\u5dee\u3002",
    ]:
        plan = plan_user_actions(_envelope(text, active))

        assert all(action.write_policy != "write" for action in plan.actions)


def test_action_intake_keeps_system_failure_as_problem_and_blocks_body_status():
    active = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-problem-guard",
        status="collecting",
        reply_candidate=True,
    )
    failure = plan_user_actions(_envelope("\u5bf9\u4e86\uff0c\u4e0b\u5348\u7cfb\u7edf\u5d29\u4e86\u534a\u5c0f\u65f6\uff0c\u5dee\u70b9\u6ca1\u4fdd\u5b58\u3002", active))
    body = plan_user_actions(_envelope("\u4eca\u5929\u62c9\u809a\u5b50\uff0c\u8dd1\u4e86\u4e09\u8d9f\u5395\u6240", active))

    assert any(action.write_policy == "write" and action.target_field == "problems" for action in failure.actions)
    assert all(action.write_policy != "write" for action in body.actions)


def test_action_intake_blocks_future_segment_even_when_same_turn_has_tomorrow_plan():
    plan = plan_user_actions(
        _envelope(
            "\u660e\u5929\u53bb\u5357\u4eac\u5206\u6240\u51fa\u5dee\uff0c\u540e\u5929\u8ddf\u4fdd\u5229\u6848\u5bf9\u65b9\u5f8b\u5e08\u78b0\u9762\u3002",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-mixed-future",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    daily_writes = [action for action in plan.actions if action.action_type == ACTION_DAILY_WRITE and action.write_policy == "write"]
    assert [action.payload["content"] for action in daily_writes] == ["\u660e\u5929\u53bb\u5357\u4eac\u5206\u6240\u51fa\u5dee"]


def test_action_intake_blocks_short_vague_completion_without_object():
    plan = plan_user_actions(_envelope("\u4eca\u5929\u641e\u5b9a\u4e86"))

    assert set(plan.action_types()) == {ACTION_SMALL_TALK}
    assert all(action.write_policy != POLICY_WRITE for action in plan.actions)


def test_action_intake_blocks_non_substantive_daily_request():
    plan = plan_user_actions(_envelope("\u7b97\u4e86\u4e0d\u8ddf\u4f60\u804a\u4e86\uff0c\u4f60\u5565\u4e5f\u4e0d\u61c2\uff0c\u968f\u4fbf\u5199\u4e2a\u65e5\u62a5\u4ea4\u5dee\u5427"))

    assert set(plan.action_types()) == {ACTION_SMALL_TALK}
    assert all(action.write_policy != POLICY_WRITE for action in plan.actions)


def test_action_intake_blocks_daily_meta_request_without_work():
    for text in [
        "今天忙成狗，日报都不想写了",
        "今天写日报了没？帮我把今天的活儿记一下",
    ]:
        plan = plan_user_actions(_envelope(text))

        assert set(plan.action_types()) == {ACTION_SMALL_TALK}
        assert all(action.write_policy != POLICY_WRITE for action in plan.actions)


def test_action_intake_accepts_prior_intent_completion_as_today_work():
    plan = plan_user_actions(_envelope("昨天我说今天要去见客户，已经见完了"))

    assert any(
        action.action_type == ACTION_DAILY_WRITE
        and action.target_field == "today_work"
        and action.write_policy == POLICY_WRITE
        for action in plan.actions
    )


def test_action_intake_routes_business_reference_question_to_read_only_qa():
    plan = plan_user_actions(_envelope("那个合同里的争议解决条款是不是改过？我记得之前写的是仲裁"))

    assert set(plan.action_types()) == {ACTION_INTERNAL_QA}
    assert all(action.write_policy != POLICY_WRITE for action in plan.actions)


def test_action_intake_keeps_mixed_case_and_daily_sentence_as_daily_writes():
    plan = plan_user_actions(
        _envelope("今天上午跟保利案的开庭调解，下午把恒大项目的合同审完了，还发现有个风险条款，明天得和业务部门开个会讨论下，另外下班前把周报发了")
    )

    daily_writes = [action for action in plan.actions if action.action_type == ACTION_DAILY_WRITE and action.write_policy == POLICY_WRITE]
    assert any(action.target_field == "today_work" and "保利案" in action.payload["content"] for action in daily_writes)
    assert any(action.target_field == "today_work" and "恒大项目" in action.payload["content"] for action in daily_writes)
    assert any(action.target_field == "problems" and "风险条款" in action.payload["content"] for action in daily_writes)
    assert any(action.target_field == "tomorrow_plan" and "业务部门" in action.payload["content"] for action in daily_writes)


def test_action_intake_handles_v4_case_schedule_and_abandon_boundaries():
    hearing = plan_user_actions(_envelope("保利案明天要开庭了，得准备材料。"))
    summons = plan_user_actions(_envelope("恒大案进度：今天刚拿到法院传票，下周三开庭"))
    abandon = plan_user_actions(_envelope("算了不写了，明天写"))
    mixed_previous = plan_user_actions(_envelope("昨天的明日计划已完成，今天继续推进保利案"))

    assert any(
        action.action_type == ACTION_DAILY_WRITE
        and action.target_field == "tomorrow_plan"
        and "准备材料" in action.payload["content"]
        for action in hearing.actions
    )
    assert any(
        action.action_type == ACTION_DAILY_WRITE
        and action.target_field == "today_work"
        and "法院传票" in action.payload["content"]
        for action in summons.actions
    )
    assert set(abandon.action_types()) == {ACTION_SMALL_TALK}
    assert all(action.write_policy != POLICY_WRITE for action in abandon.actions)
    assert any(
        action.action_type == ACTION_DAILY_WRITE
        and action.target_field == "today_work"
        and "继续推进保利案" in action.payload["content"]
        for action in mixed_previous.actions
    )


def test_action_intake_treats_document_research_done_as_daily_but_query_as_qa():
    daily_plan = plan_user_actions(_envelope("今天查阅恒大案件资料"))

    assert daily_plan.action_types() == [ACTION_DAILY_WRITE]
    assert daily_plan.actions[0].workflow == WORKFLOW_DAILY_REPORT
    assert daily_plan.actions[0].target_field == "today_work"

    qa_plan = plan_user_actions(_envelope("帮我查一下王喜被告案件有多少"))

    assert qa_plan.action_types() == [ACTION_INTERNAL_QA]
    assert qa_plan.actions[0].write_policy == POLICY_READ_ONLY


def test_action_intake_copy_previous_tomorrow_plan_is_structured_copy():
    plan = plan_user_actions(_envelope("明天的计划就复制昨天的明日计划吧"))

    assert plan.action_types() == [ACTION_DAILY_WRITE]
    assert plan.actions[0].operation == "copy_previous"
    assert plan.actions[0].target_field == "tomorrow_plan"


def test_action_intake_handles_v4_followup_boundaries_without_agent1_fallback():
    sync_plan = plan_user_actions(_envelope("明天把变更同步给开发"))
    settlement_plan = plan_user_actions(_envelope("那明天我拟一份和解协议发你。"))
    commute_plan = plan_user_actions(_envelope("今天地铁挤死了，还迟到了，烦"))
    deadline_plan = plan_user_actions(_envelope("催一下，明天就截止了"))
    case_detail_plan = plan_user_actions(_envelope("对，就是那个保利案的开庭，需要带案卷材料。"))

    assert sync_plan.action_types() == [ACTION_DAILY_WRITE]
    assert sync_plan.actions[0].target_field == "tomorrow_plan"
    assert settlement_plan.action_types() == [ACTION_DAILY_WRITE]
    assert settlement_plan.actions[0].target_field == "tomorrow_plan"
    assert ACTION_DAILY_WRITE not in commute_plan.action_types()
    assert commute_plan.actions[0].action_type == ACTION_SMALL_TALK
    assert ACTION_DAILY_WRITE not in deadline_plan.action_types()
    assert deadline_plan.actions[0].action_type == ACTION_MONTHLY_STATUS_QUERY
    assert ACTION_CASE_PROGRESS in case_detail_plan.action_types()
    assert ACTION_DAILY_WRITE not in case_detail_plan.action_types()


def test_action_intake_handles_v4_fresh60_regressions_without_pollution():
    active_daily = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-1",
        status="collecting",
        reply_candidate=True,
    )
    mixed_question = plan_user_actions(
        _envelope(
            "\u4eca\u5929\u5ba1\u4e86\u4fdd\u5229\u6848\u7684\u8865\u5145\u534f\u8bae\uff0c\u987a\u4fbf\u95ee\u4e0b\uff0c\u7528\u5370\u6d41\u7a0b\u8d70\u7ebf\u4e0a\u8fd8\u662f\u7ebf\u4e0b\uff1f\u660e\u5929\u7ea6\u4e86\u5ba2\u6237\u8c08\u548c\u89e3\u65b9\u6848\u3002",
            active_daily,
        )
    )
    creative = plan_user_actions(_envelope("\u7ed9\u6211\u5199\u7bc7\u79d1\u5e7b\u5c0f\u8bf4\uff0c\u660e\u5929\u5c31\u8981\u3002", active_daily))
    document_problem = plan_user_actions(
        _envelope(
            "\u54e6\u5bf9\u4e86\uff0c\u9014\u4e2d\u53d1\u73b0\u5224\u51b3\u4e66\u6709\u4e2a\u65e5\u671f\u5199\u9519\u4e86\uff0c\u5f97\u901a\u77e5\u6cd5\u9662\u66f4\u6b63\u3002",
            active_daily,
        )
    )
    contextual_plan = plan_user_actions(
        _envelope("\u8fd9\u4e2a\u4e8b\u660e\u5929\u8fd8\u5f97\u7ee7\u7eed\uff0c\u5148\u5199\u5230\u660e\u65e5\u8ba1\u5212\u91cc\u3002", active_daily)
    )
    report_edit = plan_user_actions(_envelope("\u5c31\u662f\u628a\u6628\u5929\u7684\u62a5\u544a\u6539\u4e00\u4e0b\uff0c\u7b2c\u4e09\u9875\u7684\u6570\u636e\u66f4\u65b0\u4e86\u3002", active_daily))
    case_strategy = plan_user_actions(_envelope("\u90a3\u4e2a\u6848\u5b50\u7684\u7b56\u7565\u6211\u660e\u5929\u8981\u8ddf\u8001\u677f\u518d\u786e\u8ba4\u4e00\u4e0b\u3002", active_daily))
    printer = plan_user_actions(_envelope("\u70e6\u6b7b\u4e86\uff0c\u4eca\u5929\u6253\u5370\u673a\u53c8\u574f\u4e86"))
    monthly_meta = plan_user_actions(_envelope("\u8d76\u7d27\u5199\u6708\u62a5\uff0c\u8fd9\u4e2a\u6708\u5feb\u8fc7\u5b8c\u4e86"))

    mixed_daily_writes = [action for action in mixed_question.actions if action.action_type == ACTION_DAILY_WRITE]
    mixed_payload = "\n".join(str(action.payload.get("content") or "") for action in mixed_daily_writes)
    assert any(action.target_field == "today_work" and "\u8865\u5145\u534f\u8bae" in str(action.payload.get("content") or "") for action in mixed_daily_writes)
    assert any(action.target_field == "tomorrow_plan" and "\u548c\u89e3\u65b9\u6848" in str(action.payload.get("content") or "") for action in mixed_daily_writes)
    assert "\u7528\u5370\u6d41\u7a0b" not in mixed_payload

    assert ACTION_DAILY_WRITE not in creative.action_types()
    assert any(action.action_type == ACTION_DAILY_WRITE and action.target_field == "problems" for action in document_problem.actions)
    assert any(action.action_type == ACTION_DAILY_WRITE and action.target_field == "tomorrow_plan" for action in contextual_plan.actions)
    assert ACTION_DAILY_WRITE not in report_edit.action_types()
    assert any(action.action_type == ACTION_DAILY_WRITE and action.target_field == "tomorrow_plan" for action in case_strategy.actions)
    assert ACTION_DAILY_WRITE not in printer.action_types()
    assert ACTION_DAILY_WRITE not in monthly_meta.action_types()


def test_action_intake_handles_round8_context_and_service_boundaries():
    risk_plan = plan_user_actions(_envelope("那个庭可能要延期，法官临时有事，风险"))
    report_plan = plan_user_actions(_envelope("刚才说的那个报告其实是昨天的工作，今天要交，我现在写"))
    material_plan = plan_user_actions(_envelope("对了，还有恒大案的资料也看了一部分"))
    email_plan = plan_user_actions(_envelope("再加一个，给客户发了邮件。"))
    arrange_plan = plan_user_actions(_envelope("顺便安排下会见XX酒店项目的人。"))

    assert ACTION_DAILY_WRITE in risk_plan.action_types()
    assert risk_plan.write_actions()[0].target_field == "problems"
    assert ACTION_DAILY_WRITE in report_plan.action_types()
    assert ACTION_DAILY_WRITE in material_plan.action_types()
    assert ACTION_CASE_PROGRESS in material_plan.action_types()
    assert ACTION_DAILY_WRITE in email_plan.action_types()
    assert ACTION_DAILY_WRITE not in arrange_plan.action_types()
    assert arrange_plan.actions[0].action_type == ACTION_INTERNAL_QA


def test_action_intake_handles_round9_llm_smoke_boundaries():
    defer_plan = plan_user_actions(_envelope("我先下班了啊，日报明天再说吧"))
    no_report_plan = plan_user_actions(_envelope("今天日报先不写了。"))
    hot_plan = plan_user_actions(_envelope("算了，今天热死了，不想动"))
    historical_plan = plan_user_actions(_envelope("把今天的工作也加到昨天日报里"))
    business_problem_plan = plan_user_actions(_envelope("还有个问题，客户说合同金额不对"))
    tomorrow_customer_plan = plan_user_actions(_envelope("明天去南京见客户"))

    assert ACTION_DAILY_WRITE not in defer_plan.action_types()
    assert ACTION_DAILY_WRITE not in no_report_plan.action_types()
    assert ACTION_DAILY_WRITE not in hot_plan.action_types()
    assert ACTION_DAILY_READ_HISTORY in historical_plan.action_types()
    assert ACTION_DAILY_WRITE not in historical_plan.action_types()
    assert any(action.action_type == ACTION_DAILY_WRITE and action.target_field == "problems" for action in business_problem_plan.actions)
    assert any(action.action_type == ACTION_DAILY_WRITE and action.target_field == "tomorrow_plan" for action in tomorrow_customer_plan.actions)


def test_action_intake_accepts_concrete_untimed_work_after_vague_preface():
    plan = plan_user_actions(_envelope("\u5c31\u662f\u7ee7\u7eed\u5ba1\u90a3\u4e2a\u91c7\u8d2d\u5408\u540c\uff0c\u5feb\u641e\u5b8c\u4e86"))

    assert ACTION_DAILY_WRITE in plan.action_types()
    assert any(action.target_field == "today_work" and action.write_policy == POLICY_WRITE for action in plan.actions)


def test_action_intake_accepts_meeting_work_with_no_risk():
    plan = plan_user_actions(_envelope("\u5c31\u662f\u5f00\u4e86\u4e24\u4e2a\u4f1a\uff0c\u90fd\u662f\u5e38\u89c4\u7684\uff0c\u6ca1\u98ce\u9669\u3002"))

    assert ACTION_DAILY_WRITE in plan.action_types()
    assert any(action.target_field == "today_work" and action.write_policy == POLICY_WRITE for action in plan.actions)


def test_action_intake_accepts_contextual_business_detail_in_active_daily():
    active = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-context-detail",
        status="collecting",
        reply_candidate=True,
    )

    plan = plan_user_actions(_envelope("\u54e6\u5bf9\u4e86\uff0c\u7ade\u54c1\u5206\u6790\u90a3\u5757\uff0c\u8fd8\u5305\u542b\u4e86\u8ddf\u963f\u91cc\u4e91\u7684\u5bf9\u6bd4\u3002", active))

    assert ACTION_DAILY_WRITE in plan.action_types()
    assert any(action.target_field == "today_work" and action.write_policy == POLICY_WRITE for action in plan.actions)


def test_action_intake_blocks_future_trip_followup_segments_from_current_daily():
    plan = plan_user_actions(_envelope("\u540e\u5929\u53bb\u5357\u4eac\u51fa\u5dee\uff0c\u8981\u53bb\u5ba2\u6237\u90a3\u8fb9\u5904\u7406\u5408\u540c\u7ea0\u7eb7\uff0c\u987a\u4fbf\u76d6\u7ae0"))

    assert all(action.workflow != WORKFLOW_DAILY_REPORT or action.write_policy != POLICY_WRITE for action in plan.actions)


def test_action_intake_blocks_vague_tomorrow_redo_without_object():
    plan = plan_user_actions(_envelope("\u8fd8\u6ca1\u505a\uff0c\u90a3\u660e\u5929\u518d\u505a"))

    assert all(action.write_policy != POLICY_WRITE for action in plan.actions)


def test_action_intake_blocks_test_probe_and_moyu_empty_daily():
    plan = plan_user_actions(_envelope("test1234444 \u8ba9\u6211\u6d4b\u8bd5\u4e0b\u65e5\u62a5\u529f\u80fd \u4eca\u5929\u5565\u4e5f\u6ca1\u5e72 \u5c31\u662f\u6478\u9c7c"))

    assert all(action.write_policy != POLICY_WRITE for action in plan.actions)


def test_action_intake_v4_smoke_blocks_absurd_empty_and_future_makeup_notice():
    for text in [
        "\u4eca\u5929\u628a\u516c\u53f8\u70b8\u4e86\uff0c\u54c8\u54c8\u54c8",
        "\u4eca\u5929\u7684\u6708\u4eae\u662f\u84dd\u8272\u7684\uff0c\u62a5\u544a\u5b8c\u6bd5",
        "\u4eca\u5929\u6ca1\u5565\u53ef\u5199\u7684\uff0c\u5c31\u90a3\u6837",
        "\u540e\u5929\u53bb\u5357\u4eac\u51fa\u5dee\uff0c\u5468\u4e8c\u5230\u5468\u56db\u90fd\u5728\u90a3\u8fb9\uff0c\u65e5\u62a5\u53ef\u80fd\u5f97\u5468\u4e94\u4e00\u8d77\u8865",
    ]:
        plan = plan_user_actions(_envelope(text))

        assert all(action.workflow != WORKFLOW_DAILY_REPORT or action.write_policy != POLICY_WRITE for action in plan.actions)


def test_action_intake_v4_smoke_accepts_explicit_today_item_edit_and_yesterday_makeup_prefix():
    edit_plan = plan_user_actions(_envelope("\u4eca\u5929\u7684\u7b2c\u4e00\u6761\u5de5\u4f5c\u5199\u9519\u4e86\uff0c\u5220\u6389\u5427"))
    makeup_plan = plan_user_actions(_envelope("\u8865\u4e00\u4e0b\u6628\u5929\u7684\u65e5\u62a5\uff0c\u6628\u5929\u4e3b\u8981\u5de5\u4f5c\u662f\u6574\u7406\u5408\u540c\u53f0\u8d26\uff0c\u95ee\u9898\u662f\u6709\u51e0\u4efd\u5408\u540c\u627e\u4e0d\u5230\u539f\u4ef6"))

    assert any(action.action_type == ACTION_DAILY_EDIT and action.write_policy == POLICY_WRITE for action in edit_plan.actions)
    assert any(action.action_type == ACTION_DAILY_WRITE and action.target.get("target_date") == "yesterday" and action.write_policy == POLICY_WRITE for action in makeup_plan.actions)


def test_action_intake_accepts_court_document_and_arbitration_delivery_as_work():
    for text in [
        "\u90a3\u5e2e\u6211\u8bb0\u4e00\u4e0b\uff0c\u4e0b\u5348\u8ddf\u5f8b\u5e08\u786e\u8ba4\u4e86\u4fdd\u5168\u88c1\u5b9a\u4e66\u5df2\u7ecf\u6536\u5230\u4e86",
        "\u54e6\u5bf9\u4e86\uff0c\u4e0b\u5348\u7ec8\u4e8e\u628a\u90a3\u4e2a\u6d89\u5916\u4ef2\u88c1\u7684\u6750\u6599\u5bc4\u51fa\u53bb\u4e86\uff0c\u987a\u4e30\u5355\u53f7SF123456",
    ]:
        plan = plan_user_actions(_envelope(text))

        assert any(
            action.action_type == ACTION_DAILY_WRITE
            and action.target_field == "today_work"
            and action.write_policy == POLICY_WRITE
            for action in plan.actions
        )


def test_action_intake_treats_self_reported_research_as_daily_work_but_service_lookup_as_qa():
    for text in [
        "\u4eca\u5929\u67e5\u9605\u4e86\u4fdd\u5229\u6848\u4ef6\u8d44\u6599",
        "\u4eca\u5929\u53bb\u67e5\u4e86XX\u6848\u4ef6\u8d44\u6599",
        "\u4eca\u5929\u67e5\u8be2\u4e86XX\u6848\u4ef6\u6750\u6599",
        "\u4eca\u5929\u68c0\u7d22\u4e86\u6d77\u82b1\u5c9b\u6848\u4ef6\u6750\u6599",
    ]:
        plan = plan_user_actions(_envelope(text))

        assert any(
            action.action_type == ACTION_DAILY_WRITE
            and action.target_field == "today_work"
            and action.write_policy == POLICY_WRITE
            for action in plan.actions
        )

    service = plan_user_actions(_envelope("\u5e2e\u6211\u67e5\u4e00\u4e0b\u516c\u53f8\u6cd5\u52a1\u90e8\u7684\u6700\u65b0\u57f9\u8bad\u8d44\u6599\uff0c\u53d1\u94fe\u63a5\u7ed9\u6211"))
    assert all(action.write_policy != POLICY_WRITE for action in service.actions)
    assert any(action.workflow == WORKFLOW_INTERNAL_QA for action in service.actions)


def test_action_intake_blocks_monthly_followup_process_help_and_training_query():
    for text in [
        "\u50ac\u4e00\u4e0b\u6ca1\u4ea4\u7684\u5427\uff0cdeadline\u5c31\u662f\u4eca\u5929\uff0c\u518d\u62d6\u5c31\u6263\u7ee9\u6548\u4e86",
        "\u5bf9\u4e86\uff0c\u90a3\u4e2a\u5408\u540cD\u7684\u7528\u5370\u7533\u8bf7\u7cfb\u7edf\u600e\u4e48\u63d0\u554a\uff1f\u6211\u660e\u5929\u6025\u7740\u8981\u7528\u5370\uff0c\u6015\u641e\u9519\u3002",
        "\u5bf9\u4e86\uff0c\u5e2e\u6211\u67e5\u4e00\u4e0b\u516c\u53f8\u6cd5\u52a1\u90e8\u7684\u6700\u65b0\u57f9\u8bad\u8d44\u6599\uff0c\u53d1\u94fe\u63a5\u7ed9\u6211\u3002",
    ]:
        plan = plan_user_actions(_envelope(text))

        assert all(action.write_policy != POLICY_WRITE for action in plan.actions)


def test_action_intake_completed_yesterday_plan_with_current_content_is_today_work():
    for text in [
        "\u5bf9\u4e86\uff0c\u6628\u5929\u7684\u660e\u65e5\u8ba1\u5212\u91cc\u90a3\u4e2a\u5f85\u529e\uff0c\u8ddf\u5ba1\u8ba1\u5bf9\u63a5\u7684\u4e8b\u6211\u641e\u5b8c\u4e86",
        "\u6628\u5929\u65e5\u62a5\u91cc\u7684\u660e\u65e5\u8ba1\u5212\uff1a\u8ddf\u6cd5\u52a1\u603b\u76d1\u6c47\u62a5\u5408\u540cB\u7684\u98ce\u9669\uff0c\u4eca\u5929\u4e0a\u5348\u5df2\u7ecf\u6c47\u62a5\u5b8c\u4e86\uff0c\u603b\u76d1\u540c\u610f\u6309\u539f\u65b9\u6848\u8d70\u3002",
    ]:
        plan = plan_user_actions(_envelope(text))

        assert any(
            action.action_type == ACTION_DAILY_WRITE
            and action.target_field == "today_work"
            and action.write_policy == POLICY_WRITE
            for action in plan.actions
        )


def test_action_intake_routes_active_case_work_to_daily_and_case_sidecar():
    active = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-case-sidecar",
        status="collecting",
        reply_candidate=True,
    )
    plan = plan_user_actions(_envelope("\u8fd8\u6709\u5462\uff0c\u90a3\u4e2a\u4fdd\u5229\u6848\u8fdb\u5c55\u4e5f\u770b\u4e86\u4e0b", active))

    assert any(
        action.action_type == ACTION_DAILY_WRITE
        and action.target_field == "today_work"
        and action.write_policy == POLICY_WRITE
        for action in plan.actions
    )
    assert any(action.action_type == ACTION_CASE_PROGRESS for action in plan.actions)


def test_action_intake_blocks_vague_today_done_without_work_object():
    plan = plan_user_actions(_envelope("\u641e\u5b9a\u4e86\uff0c\u4eca\u5929\u5b8c\u4e8b\u3002"))

    assert all(action.write_policy != POLICY_WRITE for action in plan.actions)


def test_action_intake_v4_boundaries_for_meta_history_and_routing():
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
        plan = plan_user_actions(_envelope(text))

        assert all(action.write_policy != POLICY_WRITE for action in plan.actions)


def test_action_intake_allows_tomorrow_case_preparation_but_not_next_week_schedule():
    tomorrow = plan_user_actions(_envelope("\u4fdd\u5229\u6848\u660e\u5929\u5f00\u5ead\uff0c\u6750\u6599\u90fd\u51c6\u5907\u597d\u4e86"))
    next_week = plan_user_actions(_envelope("\u4fdd\u5229\u90a3\u4e2a\u6848\u5b50\u4e0b\u5468\u4e8c\u5f00\u5ead\uff0c\u6211\u4eec\u6750\u6599\u90fd\u51c6\u5907\u597d\u4e86\uff0c\u5e94\u8be5\u6ca1\u95ee\u9898\u3002"))

    assert any(action.action_type == ACTION_DAILY_WRITE and action.write_policy == POLICY_WRITE for action in tomorrow.actions)
    assert ACTION_DAILY_WRITE not in next_week.action_types()


def test_action_intake_v4_blocks_personal_state_and_calendar_chatter_before_effects():
    for text in [
        "\u4eca\u5929\u5fc3\u60c5\u4e0d\u592a\u597d\uff0c\u4e0d\u60f3\u5e72\u6d3b",
        "\u4eca\u5929\u662f\u661f\u671f\u4e94\uff0c\u660e\u5929\u4e0d\u4e0a\u73ed\uff0c\u597d\u5f00\u5fc3",
    ]:
        plan = plan_user_actions(_envelope(text))

        assert all(action.write_policy != POLICY_WRITE for action in plan.actions)


def test_action_intake_v4_keeps_business_work_when_mixed_with_system_rant():
    plan = plan_user_actions(
        _envelope(
            "\u4eca\u5929\u4e0a\u5348\u5f00\u4e86\u5408\u89c4\u57f9\u8bad\uff0c"
            "\u4e0b\u5348\u5199\u4e86\u4e2a\u5408\u540c\uff0c"
            "\u53d1\u73b0\u7cfb\u7edfbug\u53c8\u591a\u4e86\uff0c"
            "\u660e\u5929\u8fd8\u5f97\u7ee7\u7eed\u5199\u5408\u540c\uff0c"
            "\u8fd9\u7cfb\u7edf\u80fd\u4e0d\u80fd\u4fee\u4fee\u4e86"
        )
    )

    writes = [action for action in plan.actions if action.write_policy == POLICY_WRITE]
    assert any(action.target_field == "today_work" for action in writes)
    assert any(action.target_field == "tomorrow_plan" for action in writes)


def test_action_intake_v4_treats_confidential_retraction_as_daily_edit():
    active = ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id="daily-retract-confidential",
        status="collecting",
        reply_candidate=True,
    )

    plan = plan_user_actions(
        _envelope(
            "\u7b97\u4e86\uff0c\u6570\u636e\u51fa\u5883\u90a3\u4e2a\u8fd8\u662f\u522b\u5199\u4e86\uff0c\u4fdd\u5bc6\u3002"
            "\u628a\u6628\u5929\u7684\u65e5\u62a5copy\u8fc7\u6765\u6539\u6539",
            active,
        )
    )

    assert any(
        action.action_type == ACTION_DAILY_EDIT
        and action.operation == "edit"
        and action.write_policy == POLICY_WRITE
        for action in plan.actions
    )


def test_action_intake_v4_blocks_unfinished_shell_but_allows_explicit_yesterday_items():
    shell = plan_user_actions(_envelope("\u4eca\u5929\u7684\u4e8b\u8fd8\u6ca1\u5199\u5b8c\u3002"))
    explicit_repeat = plan_user_actions(_envelope("\u6628\u5929\u7684\u65e5\u62a5\u662f\uff1a\u89c1\u5ba2\u6237\u3001\u5199\u62a5\u544a\u3002\u4eca\u5929\u786e\u5b9e\u4e5f\u662f\u8fd9\u6837\u3002"))

    assert all(action.write_policy != POLICY_WRITE for action in shell.actions)
    assert any(action.action_type == ACTION_DAILY_WRITE and action.target_field == "today_work" for action in explicit_repeat.actions)


def test_action_intake_v4_keeps_daily_before_assistant_feedback_tail():
    plan = plan_user_actions(
        _envelope(
            "\u4eca\u5929\u4e3b\u8981\u5728\u5904\u7406XX\u9152\u5e97\u7684\u79df\u8d41\u7ea0\u7eb7\uff0c\u67e5\u4e86\u76f8\u5173\u6848\u4f8b\u3002"
            "\u95ee\u9898\u5c31\u662f\u5bf9\u65b9\u6839\u672c\u4e0d\u914d\u5408\uff0c\u534f\u5546\u5931\u8d25\u3002"
            "\u53ef\u80fd\u5f97\u8d70\u8bc9\u8bbc\u4e86\u3002"
            "\u987a\u4fbf\u8bf4\u4e00\u4e0b\uff0c\u8fd9\u4e2a\u65e5\u62a5\u52a9\u624b\u633a\u597d\u7528\u7684\u54c8\u54c8"
        )
    )

    assert any(action.action_type == ACTION_DAILY_WRITE and action.target_field == "today_work" for action in plan.actions)
    assert any(action.action_type == ACTION_DAILY_WRITE and action.target_field == "problems" for action in plan.actions)
    assert all(
        "\u65e5\u62a5\u52a9\u624b" not in str(action.payload.get("content", ""))
        for action in plan.actions
        if action.write_policy == POLICY_WRITE
    )


def test_action_intake_v4_blocks_personal_vague_and_service_requests():
    for text in (
        "\u660e\u5929\u4e0d\u60f3\u4e0a\u73ed",
        "\u4eca\u5929\u5e72\u4e86\u597d\u591a\u4e8b",
        "md\uff0c\u4eca\u5929\u53c8\u52a0\u73ed\uff0c\u7d2f\u6b7b\u3002",
        "\u4eca\u5929\u5f04\u4e86\u4e00\u4e0b\u90a3\u4e2a\u4e8b\uff0c\u5dee\u4e0d\u591a\u4e86\u3002",
        "\u522b\u5f53\u771f\uff0c\u6211\u5c31\u6d4b\u8bd5\u7cfb\u7edf\u3002",
        "\u660e\u5929\u8981\u53bb\u5ba2\u6237\u73b0\u573a\u6f14\u793a\uff0c\u9700\u8981\u5e26\u54ea\u4e9b\u8bbe\u5907\uff1f",
        "\u4f60\u628a\u8fd9\u4e2a\u6848\u5b50\u6700\u8fd1\u7684\u8fdb\u5c55\u6574\u7406\u4e00\u4e0b\u7ed9\u6211",
    ):
        plan = plan_user_actions(_envelope(text))

        assert all(action.write_policy != POLICY_WRITE for action in plan.actions)


def test_action_intake_v4_allows_conditional_daily_supplement_with_content():
    plan = plan_user_actions(_envelope("\u6ca1\u5199\u7684\u8bdd\u5e2e\u6211\u8865\u4e0a\uff1a\u6628\u5929\u4fee\u4e86\u4e09\u4e2abug\uff0c\u5f00\u4e86\u4e24\u4e2a\u4f1a\u3002"))

    assert any(
        action.action_type == ACTION_DAILY_WRITE
        and action.write_policy == POLICY_WRITE
        and action.target_field == "today_work"
        and "\u6628\u5929\u4fee\u4e86\u4e09\u4e2abug" in str(action.payload.get("content", ""))
        for action in plan.actions
    )


def test_action_intake_v4_keeps_clear_tomorrow_trip_plan():
    plan = plan_user_actions(_envelope("\u660e\u5929\u53bb\u676d\u5dde\u53c2\u52a0\u5cf0\u4f1a\uff0c\u540e\u5929\u56de\u6765"))

    assert any(
        action.action_type == ACTION_DAILY_WRITE
        and action.write_policy == POLICY_WRITE
        and action.target_field == "tomorrow_plan"
        for action in plan.actions
    )
