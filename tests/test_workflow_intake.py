from datetime import datetime
from zoneinfo import ZoneInfo

from app.agent2.daily_state import PENDING_DAILY_CANDIDATE_KEY
from app.workflows.intake import (
    ActiveWorkflowTask,
    EFFECT_ADD_DAILY_REPORT_ITEM,
    EFFECT_CONFIRM_DAILY_REPORT,
    EFFECT_LEGACY_DAILY_CONTEXT_ACTION,
    IncomingMessageEnvelope,
    WORKFLOW_CASE_PROGRESS,
    WORKFLOW_CHAT,
    WORKFLOW_DAILY_REPORT,
    WORKFLOW_INTERNAL_QA,
    WORKFLOW_LEGAL_RESEARCH,
    WORKFLOW_MONTHLY_REPORT,
    WORKFLOW_TRAVEL_COORDINATION,
    WORKFLOW_UNKNOWN_OR_HELP,
    WORKFLOW_WEEKLY_REPORT,
    WorkflowRouter,
)


SUNDAY_NIGHT = datetime(2026, 7, 5, 21, 50, tzinfo=ZoneInfo("Asia/Shanghai"))


def _envelope(raw_text: str, *tasks: ActiveWorkflowTask, received_at=None) -> IncomingMessageEnvelope:
    return IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="测试用户",
        dingtalk_user_id="ding-user-1",
        source="test",
        raw_text=raw_text,
        received_at=received_at,
        active_tasks=tuple(tasks),
    )


def test_monthly_candidate_owns_reply_even_when_daily_report_exists_as_fallback():
    route = WorkflowRouter().route(
        _envelope(
            "1. 索赔管理\n未完成原因/存在问题：客户资料回收慢\n下月目标（万元）：100\n行动方案：每周跟进",
            ActiveWorkflowTask(
                workflow=WORKFLOW_MONTHLY_REPORT,
                task_id="task-1",
                status="collecting",
                reply_candidate=True,
                reason="performance parser matched metric reply",
            ),
        )
    )

    assert route.workflow == WORKFLOW_MONTHLY_REPORT
    assert route.task_id == "task-1"
    assert route.confidence >= 0.86


def test_active_monthly_task_does_not_let_ambiguous_text_fall_into_daily_report():
    route = WorkflowRouter().route(
        _envelope(
            "已完成，目标10%，继续推进",
            ActiveWorkflowTask(
                workflow=WORKFLOW_MONTHLY_REPORT,
                task_id="task-1",
                status="collecting",
                reply_candidate=False,
            ),
        )
    )

    assert route.workflow == WORKFLOW_UNKNOWN_OR_HELP
    assert route.task_id == "task-1"
    assert "active monthly-report task" in route.reason


def test_route_treats_plain_small_talk_as_chat_workflow():
    route = WorkflowRouter().route(_envelope("早上好，咖啡太苦了"))

    assert route.workflow == WORKFLOW_CHAT
    assert route.confidence >= 0.82


def test_agent2_plan_treats_short_context_probe_as_chat_not_daily_or_qa():
    for text in ["\u548b\u8bf4", "\u600e\u4e48\u95f2\u804a"]:
        plan = WorkflowRouter().plan(
            _envelope(
                text,
                ActiveWorkflowTask(
                    workflow=WORKFLOW_DAILY_REPORT,
                    task_id="daily-chat",
                    status="collecting",
                    reply_candidate=True,
                ),
            )
        )

        assert plan.primary_workflow == WORKFLOW_CHAT
        assert plan.matched_workflows == [WORKFLOW_CHAT]
        assert plan.effects == []


def test_agent2_plan_routes_sunday_monday_trip_to_tomorrow_plan_and_travel():
    plan = WorkflowRouter().plan(
        _envelope("\u5468\u4e00\u9884\u8ba1\u51fa\u5dee\u53bb\u5170\u5dde", received_at=SUNDAY_NIGHT)
    )

    assert plan.primary_workflow == WORKFLOW_TRAVEL_COORDINATION
    assert WORKFLOW_TRAVEL_COORDINATION in plan.matched_workflows
    assert WORKFLOW_DAILY_REPORT in plan.matched_workflows
    assert {effect.effect_type for effect in plan.effects} == {
        "upsert_travel_plan",
        "add_daily_report_item",
    }
    travel = next(effect for effect in plan.effects if effect.effect_type == "upsert_travel_plan")
    daily = next(effect for effect in plan.effects if effect.effect_type == "add_daily_report_item")
    assert travel.target["date_hint"] == "tomorrow"
    assert daily.target["field"] == "tomorrow_plan"


def test_agent2_plan_keeps_sunday_tuesday_trip_as_future_travel_candidate_only():
    plan = WorkflowRouter().plan(
        _envelope("\u5468\u4e8c\u5e94\u8be5\u51fa\u5dee\u53bb\u897f\u5b81", received_at=SUNDAY_NIGHT)
    )

    assert plan.primary_workflow == WORKFLOW_TRAVEL_COORDINATION
    assert plan.matched_workflows == [WORKFLOW_TRAVEL_COORDINATION]
    assert [effect.effect_type for effect in plan.effects] == ["upsert_travel_plan"]
    assert plan.effects[0].target["date_hint"] == "future_weekday"


def test_obvious_daily_report_still_routes_to_daily_report_without_active_monthly_match():
    route = WorkflowRouter().route(
        _envelope(
            "今日工作：审核合同3份\n问题/风险：暂无明显问题\n明日计划：继续跟进用印",
            ActiveWorkflowTask(
                workflow=WORKFLOW_MONTHLY_REPORT,
                task_id="task-1",
                status="collecting",
                reply_candidate=False,
            ),
        )
    )

    assert route.workflow == WORKFLOW_DAILY_REPORT


def test_monthly_confirmation_stays_in_monthly_context():
    route = WorkflowRouter().route(
        _envelope(
            "确认提交",
            ActiveWorkflowTask(
                workflow=WORKFLOW_MONTHLY_REPORT,
                task_id="task-2",
                status="pending_confirmation",
                awaiting_confirmation=True,
            ),
        )
    )

    assert route.workflow == WORKFLOW_MONTHLY_REPORT
    assert route.task_id == "task-2"


def test_observation_does_not_store_raw_text():
    envelope = _envelope("今日工作：审核合同")
    route = WorkflowRouter().route(envelope)
    observation = route.as_observation(envelope)

    assert observation["raw_text_hash"]
    assert observation["raw_text_chars"] == len("今日工作：审核合同")
    assert "raw_text" not in observation


def test_agent2_plan_can_route_daily_report_without_leaking_raw_text():
    plan = WorkflowRouter().plan(_envelope("今日工作：审核合同3份\n明日计划：继续跟进用印"))

    assert plan.primary_workflow == WORKFLOW_DAILY_REPORT
    assert plan.matched_workflows == [WORKFLOW_DAILY_REPORT]
    assert plan.safety_decision.commit_policy == "partial_allowed"
    assert [effect.effect_type for effect in plan.effects] == ["add_daily_report_item", "add_daily_report_item"]
    assert plan.effects[0].target["field"] == "today_work"
    assert plan.effects[1].target["field"] == "tomorrow_plan"
    observation = plan.as_observation(_envelope("今日工作：审核合同3份"))
    assert observation["raw_text_hash"]
    assert "raw_text" not in observation


def test_agent2_plan_keeps_monthly_reply_out_of_daily_even_when_text_is_short():
    plan = WorkflowRouter().plan(
        _envelope(
            "已完成，目标10%，继续推进",
            ActiveWorkflowTask(
                workflow=WORKFLOW_MONTHLY_REPORT,
                task_id="task-1",
                status="collecting",
                reply_candidate=False,
            ),
        )
    )

    assert plan.primary_workflow == WORKFLOW_UNKNOWN_OR_HELP
    assert WORKFLOW_DAILY_REPORT not in plan.matched_workflows
    assert plan.safety_decision.commit_policy == "blocked"
    assert "active_monthly_task_guard" in plan.safety_decision.flags


def test_agent2_plan_supports_weekly_report_as_first_class_workflow():
    plan = WorkflowRouter().plan(
        _envelope("本周完成：合同模板修订、案件资料梳理\n下周计划：推进诉讼材料归档")
    )

    assert plan.primary_workflow == WORKFLOW_WEEKLY_REPORT
    assert plan.matched_workflows == [WORKFLOW_WEEKLY_REPORT]
    assert [effect.effect_type for effect in plan.effects] == ["draft_weekly_report"]
    assert plan.safety_decision.commit_policy == "partial_allowed"


def test_agent2_plan_blocks_bare_confirmation_without_pending_context():
    plan = WorkflowRouter().plan(_envelope("确认提交"))

    assert plan.primary_workflow == WORKFLOW_UNKNOWN_OR_HELP
    assert plan.safety_decision.commit_policy == "blocked"
    assert "orphan_confirmation" in plan.safety_decision.flags


def test_agent2_plan_routes_active_daily_confirmation_to_daily_context():
    plan = WorkflowRouter().plan(
        _envelope(
            "确认提交",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-1",
                status="pending_confirmation",
                reply_candidate=True,
                awaiting_confirmation=True,
            ),
        )
    )

    assert plan.primary_workflow == WORKFLOW_DAILY_REPORT
    assert plan.task_id == "daily-1"
    assert [effect.effect_type for effect in plan.effects] == [EFFECT_CONFIRM_DAILY_REPORT]


def test_agent2_plan_routes_active_daily_collecting_short_submit_replies_to_daily_context():
    for text in ("交", "交了", "确认", "是", "确定"):
        plan = WorkflowRouter().plan(
            _envelope(
                text,
                ActiveWorkflowTask(
                    workflow=WORKFLOW_DAILY_REPORT,
                    task_id="daily-collecting",
                    status="collecting",
                    reply_candidate=True,
                ),
            )
        )

        assert plan.primary_workflow == WORKFLOW_DAILY_REPORT
        assert plan.task_id == "daily-collecting"
        assert [effect.effect_type for effect in plan.effects] == [EFFECT_CONFIRM_DAILY_REPORT]
        assert plan.safety_decision.commit_policy == "partial_allowed"


def test_agent2_plan_pending_candidate_focus_confirmation_does_not_submit_daily_report():
    for text in ("对", "对的", "嗯", "好的", "确定", "就这个", "就是这个", "这条"):
        plan = WorkflowRouter().plan(
            _envelope(
                text,
                ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-candidate",
                status="collecting",
                reply_candidate=True,
                metadata={"pending_keys": [PENDING_DAILY_CANDIDATE_KEY]},
            ),
            )
        )

        assert plan.primary_workflow == WORKFLOW_UNKNOWN_OR_HELP
        assert plan.effects == []
        assert plan.safety_decision.commit_policy == "blocked"
        assert "pending_daily_candidate_confirmation" in plan.safety_decision.flags


def test_agent2_plan_blocks_short_submit_reply_without_active_context():
    for text in ("交", "交了", "确认", "是", "确定"):
        plan = WorkflowRouter().plan(_envelope(text))

        assert plan.primary_workflow == WORKFLOW_UNKNOWN_OR_HELP
        assert plan.safety_decision.commit_policy == "blocked"
        assert "orphan_confirmation" in plan.safety_decision.flags


def test_agent2_plan_routes_active_daily_short_display_request_to_daily_context():
    plan = WorkflowRouter().plan(
        _envelope(
            "发我看下",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-2",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert plan.primary_workflow == WORKFLOW_DAILY_REPORT
    assert plan.task_id == "daily-2"
    assert [effect.effect_type for effect in plan.effects] == [EFFECT_LEGACY_DAILY_CONTEXT_ACTION]


def test_agent2_plan_routes_explicit_log_display_without_active_context():
    plan = WorkflowRouter().plan(_envelope("先给我看看目前的日志"))

    assert plan.primary_workflow == WORKFLOW_DAILY_REPORT
    assert [effect.effect_type for effect in plan.effects] == [EFFECT_LEGACY_DAILY_CONTEXT_ACTION]
    assert plan.safety_decision.commit_policy == "partial_allowed"


def test_agent2_plan_routes_active_daily_empty_problem_ack_to_daily_context():
    plan = WorkflowRouter().plan(
        _envelope(
            "暂无问题",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-3",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert plan.primary_workflow == WORKFLOW_DAILY_REPORT
    assert plan.task_id == "daily-3"
    assert [effect.effect_type for effect in plan.effects] == [EFFECT_LEGACY_DAILY_CONTEXT_ACTION]


def test_agent2_plan_routes_active_daily_short_work_text_to_daily_context():
    plan = WorkflowRouter().plan(
        _envelope(
            "议程和通知发放",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-4",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert plan.primary_workflow == WORKFLOW_DAILY_REPORT
    assert plan.task_id == "daily-4"
    assert [effect.effect_type for effect in plan.effects] == [EFFECT_LEGACY_DAILY_CONTEXT_ACTION]


def test_agent2_plan_routes_active_daily_short_non_question_reply_to_daily_context():
    plan = WorkflowRouter().plan(
        _envelope(
            "休假",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-4b",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert plan.primary_workflow == WORKFLOW_DAILY_REPORT
    assert plan.task_id == "daily-4b"
    assert [effect.effect_type for effect in plan.effects] == [EFFECT_LEGACY_DAILY_CONTEXT_ACTION]


def test_agent2_plan_routes_active_daily_copy_short_reply_to_daily_context():
    plan = WorkflowRouter().plan(
        _envelope(
            "复制昨天",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-4c",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert plan.primary_workflow == WORKFLOW_DAILY_REPORT
    assert plan.task_id == "daily-4c"
    assert [effect.effect_type for effect in plan.effects] == [EFFECT_LEGACY_DAILY_CONTEXT_ACTION]


def test_agent2_plan_routes_active_daily_no_risk_reply_with_buzenme_to_daily_context():
    plan = WorkflowRouter().plan(
        _envelope(
            "我这边不怎么涉及风险",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-4e",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert plan.primary_workflow == WORKFLOW_DAILY_REPORT
    assert plan.task_id == "daily-4e"
    assert [effect.effect_type for effect in plan.effects] == [EFFECT_LEGACY_DAILY_CONTEXT_ACTION]


def test_agent2_plan_does_not_route_active_daily_small_talk_to_daily_context():
    plan = WorkflowRouter().plan(
        _envelope(
            "今天天气不错咖啡太苦了",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-4d",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert plan.primary_workflow == WORKFLOW_CHAT
    assert plan.matched_workflows == [WORKFLOW_CHAT]
    assert plan.safety_decision.commit_policy == "read_only"
    assert WORKFLOW_DAILY_REPORT not in plan.matched_workflows


def test_agent2_plan_routes_monthly_status_query_read_only_not_daily_context():
    plan = WorkflowRouter().plan(
        _envelope(
            "\u5927\u5bb6\u6708\u62a5\u586b\u7684\u600e\u6837\u4e86",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-monthly-status",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert plan.primary_workflow == WORKFLOW_MONTHLY_REPORT
    assert plan.matched_workflows == [WORKFLOW_MONTHLY_REPORT]
    assert plan.effects == []
    assert plan.safety_decision.commit_policy == "read_only"
    assert WORKFLOW_DAILY_REPORT not in plan.matched_workflows


def test_agent2_plan_does_not_route_active_daily_meta_chat_to_daily_context():
    for text in ["我想聊个案子", "想说个案件进展", "闲聊会", "感觉你变蠢了有点"]:
        plan = WorkflowRouter().plan(
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

        assert plan.primary_workflow == WORKFLOW_CHAT
        assert WORKFLOW_DAILY_REPORT not in plan.matched_workflows


def test_agent2_plan_does_not_route_meta_chat_with_monthly_and_daily_contexts():
    for text in ["我想聊个案子", "想说个案件进展", "感觉你变蠢了有点", "可以和我聊聊吗？", "和我聊天", "妈的"]:
        plan = WorkflowRouter().plan(
            _envelope(
                text,
                ActiveWorkflowTask(
                    workflow=WORKFLOW_MONTHLY_REPORT,
                    task_id="monthly-active",
                    status="collecting",
                    reply_candidate=False,
                ),
                ActiveWorkflowTask(
                    workflow=WORKFLOW_DAILY_REPORT,
                    task_id="daily-active",
                    status="collecting",
                    reply_candidate=True,
                ),
            )
        )

        assert plan.primary_workflow == WORKFLOW_CHAT
        assert WORKFLOW_DAILY_REPORT not in plan.matched_workflows


def test_agent2_route_keeps_chat_ahead_of_active_monthly_guard():
    route = WorkflowRouter().route(
        _envelope(
            "可以和我聊聊吗？",
            ActiveWorkflowTask(
                workflow=WORKFLOW_MONTHLY_REPORT,
                task_id="monthly-active",
                status="collecting",
                reply_candidate=False,
            ),
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-active",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert route.workflow == WORKFLOW_CHAT


def test_agent2_plan_does_not_route_bare_travel_noun_to_daily_context():
    plan = WorkflowRouter().plan(
        _envelope(
            "出差",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-bare-travel",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert plan.primary_workflow == WORKFLOW_UNKNOWN_OR_HELP
    assert WORKFLOW_DAILY_REPORT not in plan.matched_workflows


def test_agent2_plan_routes_active_daily_legal_consequence_question_to_qa():
    plan = WorkflowRouter().plan(
        _envelope(
            "被告缺席 原告缺席有什么不一样的后果",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-legal-consequence",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert plan.primary_workflow == WORKFLOW_INTERNAL_QA
    assert WORKFLOW_DAILY_REPORT not in plan.matched_workflows


def test_agent2_plan_allows_standalone_short_daily_fragment():
    plan = WorkflowRouter().plan(_envelope("休假"))

    assert plan.primary_workflow == WORKFLOW_DAILY_REPORT
    assert plan.matched_workflows == [WORKFLOW_DAILY_REPORT]
    assert [effect.effect_type for effect in plan.effects] == ["add_daily_report_item"]
    assert plan.safety_decision.commit_policy == "partial_allowed"


def test_agent2_plan_blocks_standalone_short_non_work_fragment():
    plan = WorkflowRouter().plan(_envelope("晴空"))

    assert plan.primary_workflow == WORKFLOW_UNKNOWN_OR_HELP
    assert WORKFLOW_DAILY_REPORT not in plan.matched_workflows


def test_agent2_plan_keeps_usage_preference_instruction_out_of_active_daily_context():
    plan = WorkflowRouter().plan(
        _envelope(
            "下次说“交”就直接提交",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-usage-pref",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert plan.primary_workflow == WORKFLOW_UNKNOWN_OR_HELP
    assert WORKFLOW_DAILY_REPORT not in plan.matched_workflows
    assert plan.safety_decision.commit_policy == "blocked"
    assert "usage_preference_instruction" in plan.safety_decision.flags


def test_agent2_plan_does_not_swallow_question_just_because_daily_is_active():
    plan = WorkflowRouter().plan(
        _envelope(
            "公司印章借用流程是什么？",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-5",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert plan.primary_workflow == WORKFLOW_INTERNAL_QA
    assert WORKFLOW_DAILY_REPORT not in plan.matched_workflows


def test_agent2_plan_keeps_meta_test_probe_out_of_active_daily_context():
    plan = WorkflowRouter().plan(
        _envelope(
            "\u8ba9\u6211\u6d4b\u8bd5\u4e0b",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-meta-test",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert plan.primary_workflow == WORKFLOW_CHAT
    assert WORKFLOW_DAILY_REPORT not in plan.matched_workflows
    assert plan.safety_decision.commit_policy in {"blocked", "read_only"}


def test_agent2_plan_routes_case_count_question_to_qa_not_case_progress():
    plan = WorkflowRouter().plan(
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

    assert plan.primary_workflow == WORKFLOW_INTERNAL_QA
    assert WORKFLOW_CASE_PROGRESS not in plan.matched_workflows
    assert WORKFLOW_DAILY_REPORT not in plan.matched_workflows


def test_agent2_plan_allows_multi_workflow_hit_but_keeps_writes_pending():
    plan = WorkflowRouter().plan(_envelope("明天去上海出差处理盖章事项，今日完成合同审查"))

    assert plan.primary_workflow == WORKFLOW_TRAVEL_COORDINATION
    assert WORKFLOW_TRAVEL_COORDINATION in plan.matched_workflows
    assert WORKFLOW_DAILY_REPORT in plan.matched_workflows
    assert {effect.effect_type for effect in plan.effects} == {
        "upsert_travel_plan",
        "add_daily_report_item",
    }
    assert plan.safety_decision.commit_policy == "needs_confirmation"
    assert "multi_workflow_write" in plan.safety_decision.flags


def test_agent2_action_first_keeps_case_progress_primary_when_daily_context_is_active():
    plan = WorkflowRouter().plan(
        _envelope(
            "\u6052\u5927\u7834\u4ea7\u6848\u4eca\u5929\u548c\u6cd5\u9662\u6c9f\u901a\u4e86\u6267\u884c\u8fdb\u5c55",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-case-progress",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert plan.primary_workflow == WORKFLOW_CASE_PROGRESS
    assert WORKFLOW_CASE_PROGRESS in plan.matched_workflows
    assert WORKFLOW_DAILY_REPORT in plan.matched_workflows
    assert {effect.effect_type for effect in plan.effects} == {
        "append_case_progress",
        EFFECT_LEGACY_DAILY_CONTEXT_ACTION,
    }


def test_agent2_action_first_routes_pure_future_case_candidate_without_daily_write():
    plan = WorkflowRouter().plan(_envelope("\u4fdd\u5229\u6848\u4ef6\u4f30\u8ba1\u4e0b\u5468\u8981\u53bb\u5f00\u5ead"))

    assert plan.primary_workflow == WORKFLOW_CASE_PROGRESS
    assert plan.matched_workflows == [WORKFLOW_CASE_PROGRESS]
    assert WORKFLOW_DAILY_REPORT not in plan.matched_workflows
    assert [effect.effect_type for effect in plan.effects] == ["append_case_progress"]
    assert plan.safety_decision.commit_policy == "needs_confirmation"


def test_agent2_action_first_routes_future_trip_and_case_candidates_without_daily_write():
    plan = WorkflowRouter().plan(_envelope("\u4e0b\u5468\u4e94\u53bb\u5357\u4eac\u4e2d\u9662\u6c9f\u901a\u77f3\u5c71\u6848\u8fdb\u5c55"))

    assert plan.primary_workflow == WORKFLOW_TRAVEL_COORDINATION
    assert WORKFLOW_TRAVEL_COORDINATION in plan.matched_workflows
    assert WORKFLOW_CASE_PROGRESS in plan.matched_workflows
    assert WORKFLOW_DAILY_REPORT not in plan.matched_workflows
    assert {effect.effect_type for effect in plan.effects} == {
        "upsert_travel_plan",
        "append_case_progress",
    }
    assert plan.safety_decision.commit_policy == "needs_confirmation"


def test_agent2_action_first_keeps_travel_and_case_as_separate_sandbox_effects():
    plan = WorkflowRouter().plan(_envelope("\u4e0b\u5468\u4e94\u53bb\u5357\u4eac\u4e2d\u9662\u6c9f\u901a\u77f3\u5c71\u6848\u8fdb\u5c55"))

    assert WORKFLOW_TRAVEL_COORDINATION in plan.matched_workflows
    assert WORKFLOW_CASE_PROGRESS in plan.matched_workflows
    assert [effect.effect_type for effect in plan.effects] == [
        "upsert_travel_plan",
        "append_case_progress",
    ]
    actions = plan.entities["user_action_plan"]["actions"]
    travel_action = next(action for action in actions if action["action_type"] == "travel_coordination")
    case_action = next(action for action in actions if action["action_type"] == "case_progress")
    assert travel_action["target"]["destination"] == "\u5357\u4eac"
    assert case_action["target"]["matter_hint"] == "\u77f3\u5c71\u6848"


def test_agent2_action_first_keeps_travel_case_chat_workflows_separate():
    plan = WorkflowRouter().plan(
        _envelope(
            "\u660e\u5929\u51fa\u5dee\u5357\u4eac\u76d6\u7ae0\uff0c"
            "\u6052\u5927\u7834\u4ea7\u6848\u4eca\u5929\u8865\u5145\u8bc9\u8bbc\u6750\u6599\uff0c"
            "\u54c8\u54c8\u6709\u70b9\u7d27\u5f20"
        )
    )

    assert plan.primary_workflow == WORKFLOW_TRAVEL_COORDINATION
    assert WORKFLOW_TRAVEL_COORDINATION in plan.matched_workflows
    assert WORKFLOW_CASE_PROGRESS in plan.matched_workflows
    assert WORKFLOW_DAILY_REPORT in plan.matched_workflows
    assert WORKFLOW_CHAT in plan.matched_workflows
    assert {effect.effect_type for effect in plan.effects} == {
        "upsert_travel_plan",
        "append_case_progress",
        "add_daily_report_item",
    }
    assert "small_talk" in plan.entities["user_action_plan"]["action_types"]


def test_agent2_action_first_nanjing_hearing_without_specific_case_is_daily_plan_and_travel_only():
    plan = WorkflowRouter().plan(_envelope("\u660e\u5929\u53bb\u5357\u4eac\u5f00\u5ead"))

    assert plan.primary_workflow == WORKFLOW_TRAVEL_COORDINATION
    assert WORKFLOW_TRAVEL_COORDINATION in plan.matched_workflows
    assert WORKFLOW_DAILY_REPORT in plan.matched_workflows
    assert WORKFLOW_CASE_PROGRESS not in plan.matched_workflows
    assert {effect.effect_type for effect in plan.effects} == {
        "upsert_travel_plan",
        "add_daily_report_item",
    }
    daily_effect = next(effect for effect in plan.effects if effect.effect_type == "add_daily_report_item")
    assert daily_effect.target["field"] == "tomorrow_plan"


def test_agent2_action_first_keeps_daily_primary_for_pure_daily_case_wording():
    plan = WorkflowRouter().plan(
        _envelope(
            "\u660e\u5929\u7ee7\u7eed\u8ddf\u8fdb\u6848\u4ef6\u8fdb\u5c55",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-plan-case-wording",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert plan.primary_workflow == WORKFLOW_DAILY_REPORT
    assert plan.matched_workflows == [WORKFLOW_DAILY_REPORT]
    assert [effect.effect_type for effect in plan.effects] == [EFFECT_LEGACY_DAILY_CONTEXT_ACTION]


def test_agent2_action_first_keeps_daily_primary_for_generic_case_material_plan():
    plan = WorkflowRouter().plan(
        _envelope(
            "\u660e\u65e5\u8ba1\u5212\u589e\u52a0\u7ee7\u7eed\u8ddf\u8fdb\u6848\u4ef6\u6750\u6599",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-plan-case-material",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert plan.primary_workflow == WORKFLOW_DAILY_REPORT
    assert plan.matched_workflows == [WORKFLOW_DAILY_REPORT]
    assert [effect.effect_type for effect in plan.effects] == [EFFECT_LEGACY_DAILY_CONTEXT_ACTION]


def test_agent2_keeps_generic_case_admin_list_as_daily_without_case_sidecar():
    plan = WorkflowRouter().plan(
        _envelope("\u7528\u5370\uff0c\u5408\u540c\u5f52\u6863\uff0c\u6708\u5ea6\u6536\u6b3e\u8ba1\u5212\uff0c\u65ec\u8ba1\u5212\u8c03\u6574\uff0c\u62df\u8bc9\u6848\u4ef6\u5f55\u5165\u8ddf\u8fdb")
    )

    assert plan.primary_workflow == WORKFLOW_DAILY_REPORT
    assert plan.matched_workflows == [WORKFLOW_DAILY_REPORT]
    assert [effect.effect_type for effect in plan.effects] == [EFFECT_ADD_DAILY_REPORT_ITEM]


def test_agent2_keeps_case_progress_system_product_edit_as_daily_only():
    plan = WorkflowRouter().plan(
        _envelope(
            "3. \u6784\u5efa\u539f\u544a\u6848\u4ef6\u8fdb\u5c55\u7cfb\u7edf\n"
            "4. \u5b9e\u73b0\u65e5\u62a5\u4e2d\u63d0\u53ca\u6848\u4ef6\u65f6\u81ea\u52a8\u5173\u8054\u5e76\u8865\u5145\u8fdb\u5c55\n"
            "5. \u4ee5\u53ca\u5728\u56fa\u5b9a\u65f6\u95f4\u548c\u8282\u70b9\u8be2\u95ee\u8fdb\u5c55\n"
            "\u8fd9\u4e09\u4e2a\u662f\u540c\u4e00\u6761",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-product-case-progress",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert plan.primary_workflow == WORKFLOW_DAILY_REPORT
    assert plan.matched_workflows == [WORKFLOW_DAILY_REPORT]
    assert [effect.effect_type for effect in plan.effects] == [EFFECT_LEGACY_DAILY_CONTEXT_ACTION]


def test_agent2_still_routes_court_matter_without_case_suffix_to_case_progress():
    plan = WorkflowRouter().plan(
        _envelope("\u82cf\u5efa\u9662\u501f\u7ae0\u4e8b\u9879\u4eca\u5929\u548c\u6cd5\u9662\u6c9f\u901a\u4e86\u6267\u884c\u8fdb\u5c55")
    )

    assert plan.primary_workflow == WORKFLOW_CASE_PROGRESS
    assert WORKFLOW_CASE_PROGRESS in plan.matched_workflows
    assert {effect.effect_type for effect in plan.effects} == {
        "append_case_progress",
        EFFECT_ADD_DAILY_REPORT_ITEM,
    }
    case_effect = next(effect for effect in plan.effects if effect.effect_type == "append_case_progress")
    assert case_effect.target["matter_hint"] == "\u82cf\u5efa\u9662\u501f\u7ae0\u4e8b\u9879"


def test_agent2_action_first_routes_active_daily_edit_without_context_guessing():
    plan = WorkflowRouter().plan(
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

    assert plan.primary_workflow == WORKFLOW_DAILY_REPORT
    assert plan.task_id == "daily-edit"
    assert [effect.effect_type for effect in plan.effects] == [EFFECT_LEGACY_DAILY_CONTEXT_ACTION]
    assert plan.entities["user_action_plan"]["action_types"] == ["daily_edit"]


def test_agent2_plan_does_not_treat_travel_coordination_product_work_as_trip():
    plan = WorkflowRouter().plan(
        _envelope(
            "\u4eca\u5929\u505a\u4e86\u65e5\u62a5\u7cfb\u7edf\u7684\u65b0\u4e00\u8f6e\u4f18\u5316\uff0c"
            "\u660e\u5929\u5f00\u59cb\u505a\u65e5\u62a5\u7cfb\u7edf\u7684\u6848\u4ef6\u8fdb\u5c55"
            "\u4e0e\u51fa\u5dee\u534f\u540c\u4e24\u4e2a\u6a21\u5757"
        )
    )

    assert WORKFLOW_TRAVEL_COORDINATION not in plan.matched_workflows
    assert plan.primary_workflow == WORKFLOW_DAILY_REPORT
    assert [effect.effect_type for effect in plan.effects] == ["add_daily_report_item", "add_daily_report_item"]
    assert [effect.target["field"] for effect in plan.effects] == ["today_work", "tomorrow_plan"]


def test_agent2_plan_still_routes_real_future_trip_to_travel_coordination():
    plan = WorkflowRouter().plan(_envelope("\u660e\u5929\u8ba1\u5212\u53bb\u626c\u5dde\u51fa\u5dee"))

    assert plan.primary_workflow == WORKFLOW_TRAVEL_COORDINATION
    assert WORKFLOW_TRAVEL_COORDINATION in plan.matched_workflows
    assert WORKFLOW_DAILY_REPORT in plan.matched_workflows
    assert {effect.effect_type for effect in plan.effects} == {
        "upsert_travel_plan",
        "add_daily_report_item",
    }


def test_agent2_plan_routes_legal_research_without_daily_fallback():
    plan = WorkflowRouter().plan(_envelope("帮我查一下竞业限制最新裁判规则？"))

    assert plan.primary_workflow == WORKFLOW_LEGAL_RESEARCH
    assert plan.matched_workflows == [WORKFLOW_LEGAL_RESEARCH]
    assert [effect.effect_type for effect in plan.effects] == ["run_legal_research"]
    assert "add_daily_report_item" not in {effect.effect_type for effect in plan.effects}


def test_agent2_action_first_keeps_explicit_legal_research_out_of_active_daily_context():
    plan = WorkflowRouter().plan(
        _envelope(
            "\u5e2e\u6211\u7814\u7a76\u4e00\u4e0b\u4f18\u5148\u53d7\u507f\u6743\u6700\u65b0\u88c1\u5224\u89c2\u70b9",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-research",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert plan.primary_workflow == WORKFLOW_LEGAL_RESEARCH
    assert WORKFLOW_DAILY_REPORT not in plan.matched_workflows
    assert [effect.effect_type for effect in plan.effects] == ["run_legal_research"]


def test_agent2_action_first_keeps_legal_problem_question_out_of_daily_problem_context():
    plan = WorkflowRouter().plan(
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

    assert plan.primary_workflow == WORKFLOW_LEGAL_RESEARCH
    assert WORKFLOW_DAILY_REPORT not in plan.matched_workflows
    assert [effect.effect_type for effect in plan.effects] == ["run_legal_research"]


def test_agent2_action_first_keeps_bare_legal_subject_pending_with_active_daily_context():
    plan = WorkflowRouter().plan(
        _envelope(
            "\u7814\u7a76\u4f18\u5148\u53d7\u507f\u6743\u6848\u4f8b",
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-ambiguous-research",
                status="collecting",
                reply_candidate=True,
            ),
        )
    )

    assert plan.primary_workflow == WORKFLOW_UNKNOWN_OR_HELP
    assert WORKFLOW_DAILY_REPORT not in plan.matched_workflows
    assert plan.safety_decision.commit_policy == "blocked"
    assert "action_disambiguation_required" in plan.safety_decision.flags


def test_agent2_plan_segments_multi_intent_message_by_sentence():
    plan = WorkflowRouter().plan(
        _envelope("今日工作：完成合同审核。哈哈今天天气不错咖啡太苦了。帮我查一下竞业限制最新裁判规则？")
    )

    assert len(plan.segments) == 3
    assert plan.segments[0].primary_workflow == WORKFLOW_DAILY_REPORT
    assert plan.segments[0].intent == "add_daily_report_item"
    assert plan.segments[1].primary_workflow == WORKFLOW_CHAT
    assert plan.segments[1].matched_workflows == [WORKFLOW_CHAT]
    assert plan.segments[1].intent == "small_talk"
    assert plan.segments[2].primary_workflow == WORKFLOW_LEGAL_RESEARCH
    assert plan.segments[2].intent == "run_legal_research"


def test_agent2_segment_observation_does_not_store_raw_text():
    envelope = _envelope("今日工作：完成合同审核。哈哈今天天气不错咖啡太苦了。帮我查一下竞业限制最新裁判规则？")
    plan = WorkflowRouter().plan(envelope)
    observation = plan.as_observation(envelope)

    assert len(observation["segments"]) == 3
    assert all(segment["text_hash"] for segment in observation["segments"])
    assert all("raw_text" not in segment for segment in observation["segments"])


def test_agent2_plan_keeps_active_daily_vent_and_system_feedback_in_chat():
    for text in ["\u597d\u65e0\u804a", "\u8fd9\u7cfb\u7edf\u4e0d\u597d\u7528"]:
        plan = WorkflowRouter().plan(
            _envelope(
                text,
                ActiveWorkflowTask(
                    workflow=WORKFLOW_DAILY_REPORT,
                    task_id="daily-chat-followup",
                    status="collecting",
                    reply_candidate=True,
                ),
            )
        )

        assert plan.primary_workflow == WORKFLOW_CHAT
        assert WORKFLOW_DAILY_REPORT not in plan.matched_workflows


def test_agent2_plan_routes_active_daily_short_delete_to_daily_edit():
    plan = WorkflowRouter().plan(
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

    assert plan.primary_workflow == WORKFLOW_DAILY_REPORT
    assert plan.matched_workflows == [WORKFLOW_DAILY_REPORT]
    assert [effect.effect_type for effect in plan.effects] == ["legacy_daily_context_action"]
