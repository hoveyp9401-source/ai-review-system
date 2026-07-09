from app.workflows.intake import ActiveWorkflowTask, IncomingMessageEnvelope, WORKFLOW_CHAT, WORKFLOW_DAILY_REPORT, WORKFLOW_UNKNOWN_OR_HELP, WorkflowRouter


def _plan(raw_text: str):
    return WorkflowRouter().plan(
        IncomingMessageEnvelope(
            sender_id="user-1",
            sender_name="Test User",
            dingtalk_user_id="ding-user-1",
            source="test",
            raw_text=raw_text,
            active_tasks=(
                ActiveWorkflowTask(
                    workflow=WORKFLOW_DAILY_REPORT,
                    task_id="daily-active",
                    status="collecting",
                    reply_candidate=True,
                ),
            ),
        )
    )


def test_active_daily_context_does_not_claim_bot_meta_questions():
    for text in (
        "\u4f60\u591a\u5927\u4e86",
        "\u4f60\u90fd\u8bb0\u5f55\u7684\u662f\u5565\u554a",
    ):
        plan = _plan(text)

        assert plan.primary_workflow == WORKFLOW_CHAT
        assert WORKFLOW_DAILY_REPORT not in plan.matched_workflows


def test_active_daily_context_does_not_claim_short_non_work_word():
    plan = _plan("\u6674\u7a7a")
    assert plan.primary_workflow == WORKFLOW_UNKNOWN_OR_HELP
    assert WORKFLOW_DAILY_REPORT not in plan.matched_workflows

    for text in ("\u5565\u73a9\u610f",):
        plan = _plan(text)

        assert plan.primary_workflow == WORKFLOW_CHAT
        assert WORKFLOW_DAILY_REPORT not in plan.matched_workflows


def test_active_daily_context_does_not_claim_lifestyle_questions():
    examples = (
        "明天穿啥出门",
        "明天穿什么出门？",
        "明天天气咋样",
        "明天要不要带伞",
        "今天中午吃啥",
        "明天早上喝咖啡吗",
        "后天冷不冷",
        "今天晚上看电影吗",
        "明天跑步穿短袖还是长袖",
        "周末去哪玩",
        "明天几点睡合适",
        "今天好困怎么办",
        "明天出门要带外套吗",
        "晚上吃火锅怎么样",
        "明天奶茶喝啥",
        "今天打游戏吗",
        "明天会不会下雨",
        "今天天气适合洗车吗",
        "后天穿外套会热吗",
        "周末要不要去健身",
    )

    for text in examples:
        plan = _plan(text)

        assert plan.primary_workflow == WORKFLOW_CHAT
        assert WORKFLOW_DAILY_REPORT not in plan.matched_workflows


def test_active_daily_context_still_accepts_known_short_daily_reply():
    plan = _plan("\u4f11\u5047")

    assert plan.primary_workflow == WORKFLOW_DAILY_REPORT
    assert WORKFLOW_DAILY_REPORT in plan.matched_workflows


def test_active_daily_context_still_accepts_real_tomorrow_work_plans():
    examples = (
        "明天整理合同材料",
        "明天去南京开庭",
        "明天沟通恒大案件执行进展",
        "明天跟进客户资料补充",
        "明天处理用印审批",
        "明天完善案件台账",
        "明天测试日报系统",
        "明天和业务部门对接回款材料",
    )

    for text in examples:
        plan = _plan(text)

        assert WORKFLOW_DAILY_REPORT in plan.matched_workflows
        assert any(effect.target_system == WORKFLOW_DAILY_REPORT for effect in plan.effects)
