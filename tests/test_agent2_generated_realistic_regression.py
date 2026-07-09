from app.agent2.coordination_sandbox import CANDIDATE_CASE_PROGRESS, CANDIDATE_TRAVEL_COORDINATION
from app.agent2.daily_shadow import evaluate_daily_shadow
from app.workflows.intake import ActiveWorkflowTask, IncomingMessageEnvelope, WORKFLOW_DAILY_REPORT


def _active_daily_eval(raw_text: str):
    return evaluate_daily_shadow(
        IncomingMessageEnvelope(
            sender_id="generated-user",
            sender_name="Generated User",
            dingtalk_user_id="generated-ding-user",
            source="generated_regression",
            raw_text=raw_text,
            active_tasks=(
                ActiveWorkflowTask(
                    workflow=WORKFLOW_DAILY_REPORT,
                    task_id="active-daily-generated",
                    status="collecting",
                    reply_candidate=True,
                ),
            ),
        ),
        mode="protective_gate",
    )


def test_generated_non_daily_queries_and_chatter_do_not_write_active_daily():
    examples = [
        "让我测试下",
        "我先测一下",
        "测试",
        "你在做吗",
        "下一步做什么",
        "现在闲聊支持了吗",
        "坑太多了",
        "这个机器人咋这么蠢",
        "我有点害怕",
        "谢谢，辛苦了",
        "明天穿啥出门",
        "今天中午吃啥",
        "吃小番茄",
        "晚上吃火锅怎么样",
        "明天会不会下雨",
        "大家月报填的怎样了",
        "查下月报提交情况",
        "王喜被告案件有多少",
        "法务二部呢",
        "目前未结案几件",
        "发我被告二季度新增同比数据",
        "总体被告存量发我",
        "法务四部目前被告存量多少",
    ]

    for text in examples:
        evaluation = _active_daily_eval(text)

        assert evaluation.commands == [], text
        assert evaluation.gate_decision.block_legacy_daily is True, text
        assert not any(result.write_impact for result in evaluation.legacy_adapter_results), text


def test_generated_clear_business_updates_write_daily_with_active_context():
    examples = [
        "今天完成合同审核",
        "今天审核合同并起草函件",
        "今天处理用印审批",
        "今天整理案件台账",
        "今天沟通恒大案件执行进展",
        "今天跟进客户资料补充",
        "今天测试日报系统优化",
        "今天汇总月报反馈",
        "今天和业务部门对接回款材料",
        "今天完成法院材料寄送",
        "明天整理合同材料",
        "明天去南京开庭",
        "明天沟通恒大案件执行进展",
        "明天跟进客户资料补充",
        "明天处理用印审批",
        "明天完善案件台账",
        "明天继续跟进归档",
        "明天继续推进归档",
        "明天测试日报系统",
        "明天和业务部门对接回款材料",
        "明天出差扬州处理开庭材料",
        "明天去嘉兴南湖街道处理讨薪",
    ]

    for text in examples:
        evaluation = _active_daily_eval(text)

        assert evaluation.gate_decision.block_legacy_daily is False, text
        assert any(command.operation == "fill" for command in evaluation.commands), text
        assert any(result.write_impact for result in evaluation.legacy_adapter_results), text


def test_generated_mixed_sentences_keep_daily_side_intents_and_candidates_separate():
    lifestyle = _active_daily_eval("今天我吃了小番茄 评审了法务合同")
    assert [(command.operation, command.target_field, command.content) for command in lifestyle.commands] == [
        ("fill", "today_work", ["评审法务合同"]),
    ]

    long_mix = _active_daily_eval(
        "今天优化了日报agent，参加了AI应用比赛复审会议，收集公众号被告案件信息通报，"
        "明天计划继续优化日报agent、完成公众号通报内容编辑，完成被告板块季度邮件。"
    )
    assert [command.operation for command in long_mix.commands] == ["fill"] * 6
    assert [command.target_field for command in long_mix.commands] == [
        "today_work",
        "today_work",
        "today_work",
        "tomorrow_plan",
        "tomorrow_plan",
        "tomorrow_plan",
    ]

    daily_travel_qa = _active_daily_eval("今天完成了合同评审，明天计划去南京盖章，王喜被告案件有多少？")
    assert [(command.operation, command.target_field) for command in daily_travel_qa.commands] == [
        ("fill", "today_work"),
        ("fill", "tomorrow_plan"),
    ]
    assert daily_travel_qa.assistant_reply is not None
    assert daily_travel_qa.assistant_reply.reply_type == "internal_qa"
    assert [candidate.candidate_type for candidate in daily_travel_qa.coordination_sandbox.candidates] == [
        CANDIDATE_TRAVEL_COORDINATION,
    ]

    future_trip_case = _active_daily_eval("后天出差南通沟通保利案件调解")
    assert future_trip_case.commands == []
    assert future_trip_case.gate_decision.block_legacy_daily is True
    assert {
        candidate.candidate_type
        for candidate in future_trip_case.coordination_sandbox.candidates
    } == {CANDIDATE_TRAVEL_COORDINATION, CANDIDATE_CASE_PROGRESS}

    future_case = _active_daily_eval("保利案件估计下周要去开庭")
    assert future_case.commands == []
    assert future_case.gate_decision.block_legacy_daily is True
    assert [candidate.candidate_type for candidate in future_case.coordination_sandbox.candidates] == [
        CANDIDATE_CASE_PROGRESS,
    ]


def test_generated_copy_yesterday_phrases_compile_to_copy_previous_command():
    examples = [
        "今天得工作和昨天一样",
        "今天的工作和昨天一样",
        "还是昨天那些事",
        "复制昨天日报",
    ]

    for text in examples:
        evaluation = _active_daily_eval(text)

        assert evaluation.gate_decision.block_legacy_daily is False, text
        assert evaluation.commands, text
        assert evaluation.commands[0].operation == "copy_previous", text
        assert any(result.write_impact for result in evaluation.legacy_adapter_results), text
