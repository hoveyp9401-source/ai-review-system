import json
from pathlib import Path

from app.agent2.daily_execution_replay import replay_daily_execution_cases
from app.agent2.dialogue_replay import load_dialogue_cases
from app.workflows.intake import (
    ActiveWorkflowTask,
    IncomingMessageEnvelope,
    WORKFLOW_CHAT,
    WORKFLOW_DAILY_REPORT,
    WORKFLOW_INTERNAL_QA,
    WORKFLOW_LEGAL_RESEARCH,
    WORKFLOW_TRAVEL_COORDINATION,
    WorkflowRouter,
)


def _active_daily_plan(raw_text: str):
    return WorkflowRouter().plan(
        IncomingMessageEnvelope(
            sender_id="stress-user",
            sender_name="Stress User",
            dingtalk_user_id="stress-ding-user",
            source="test",
            raw_text=raw_text,
            active_tasks=(
                ActiveWorkflowTask(
                    workflow=WORKFLOW_DAILY_REPORT,
                    task_id="active-daily-stress",
                    status="collecting",
                    reply_candidate=True,
                ),
            ),
        )
    )


def _has_daily_effect(plan) -> bool:
    return any(effect.target_system == WORKFLOW_DAILY_REPORT for effect in plan.effects)


def test_generated_lifestyle_questions_do_not_enter_daily_with_active_context():
    examples = [
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
    ]

    for text in examples:
        plan = _active_daily_plan(text)

        assert plan.primary_workflow == WORKFLOW_CHAT
        assert WORKFLOW_DAILY_REPORT not in plan.matched_workflows
        assert not _has_daily_effect(plan)


def test_generated_work_updates_keep_daily_write_boundary():
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
        "明天测试日报系统",
        "明天和业务部门对接回款材料",
        "明天出差扬州处理开庭材料",
        "明天去嘉兴南湖街道处理讨薪",
    ]

    for text in examples:
        plan = _active_daily_plan(text)

        assert WORKFLOW_DAILY_REPORT in plan.matched_workflows
        assert _has_daily_effect(plan)


def test_generated_single_turn_multi_intent_keeps_boundaries():
    cases = [
        (
            "今天完成合同审核。顺便问下印章流程是什么？",
            {WORKFLOW_DAILY_REPORT, WORKFLOW_INTERNAL_QA},
        ),
        (
            "明天去南京开庭。晚上吃火锅怎么样？",
            {WORKFLOW_DAILY_REPORT, WORKFLOW_TRAVEL_COORDINATION, WORKFLOW_CHAT},
        ),
        (
            "今天整理案件材料。帮我查一下竞业限制最新裁判规则？",
            {WORKFLOW_DAILY_REPORT, WORKFLOW_LEGAL_RESEARCH},
        ),
        (
            "明天处理用印审批。明天穿啥出门？",
            {WORKFLOW_DAILY_REPORT, WORKFLOW_CHAT},
        ),
        (
            "今天和法院沟通执行进展。你都记录了啥啊？",
            {WORKFLOW_DAILY_REPORT, WORKFLOW_CHAT},
        ),
    ]

    for text, expected_workflows in cases:
        plan = _active_daily_plan(text)
        observed_workflows = set(plan.matched_workflows)
        observed_workflows.update(segment.primary_workflow for segment in plan.segments)

        assert expected_workflows.issubset(observed_workflows)
        assert _has_daily_effect(plan)


def test_execution_replay_generated_long_dialogue_blocks_lifestyle_chatter(tmp_path: Path):
    path = tmp_path / "dialogues.jsonl"
    payload = {
        "dialogue_id": "generated-long-context-stress",
        "metadata": {
            "previous_report": {
                "today_work": ["昨天跟进海花岛案件材料"],
                "problems": ["客户资料未完全反馈"],
                "tomorrow_plan": ["联系南京法院", "整理合同台账"],
            }
        },
        "turns": [
            {
                "turn_id": "write-today",
                "text": "今天完成合同审核",
                "expected": {
                    "agent2_direct_write": True,
                    "fallback_to_legacy": False,
                    "report_today_work": ["今天完成合同审核"],
                },
            },
            {
                "turn_id": "life-question",
                "text": "明天穿啥出门",
                "expected": {
                    "agent2_direct_write": False,
                    "fallback_to_legacy": False,
                    "report_today_work": ["今天完成合同审核"],
                    "report_tomorrow_plan": [],
                    "forbidden_tomorrow_plan_contains": ["明天穿啥出门"],
                },
            },
            {
                "turn_id": "real-plan",
                "text": "明天整理合同材料",
                "expected": {
                    "agent2_direct_write": True,
                    "fallback_to_legacy": False,
                    "report_today_work": ["今天完成合同审核"],
                    "report_tomorrow_plan": ["明天整理合同材料"],
                },
            },
            {
                "turn_id": "edit-recent",
                "text": "把第一条改成今天完成合同审核复核",
                "expected": {
                    "agent2_direct_write": True,
                    "fallback_to_legacy": False,
                    "report_today_work": ["今天完成合同审核复核"],
                    "report_tomorrow_plan": ["明天整理合同材料"],
                },
            },
            {
                "turn_id": "previous-plan-done",
                "text": "昨天的明日计划已完成",
                "expected": {
                    "agent2_direct_write": True,
                    "fallback_to_legacy": False,
                    "report_today_work": ["今天完成合同审核复核", "完成联系南京法院", "完成整理合同台账"],
                    "report_tomorrow_plan": ["明天整理合同材料"],
                },
            },
            {
                "turn_id": "internal-question",
                "text": "顺便问下印章流程是什么？",
                "expected": {
                    "agent2_direct_write": False,
                    "fallback_to_legacy": False,
                    "report_today_work": ["今天完成合同审核复核", "完成联系南京法院", "完成整理合同台账"],
                    "report_tomorrow_plan": ["明天整理合同材料"],
                },
            },
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=True) + "\n", encoding="utf-8")

    results = replay_daily_execution_cases(load_dialogue_cases([path]))

    assert results[0]["passed"] is True
