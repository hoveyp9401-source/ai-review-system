from app.workflows.gate import build_gate_decision
from app.workflows.intake import (
    ActiveWorkflowTask,
    EFFECT_CONFIRM_DAILY_REPORT,
    EFFECT_LEGACY_DAILY_CONTEXT_ACTION,
    IncomingMessageEnvelope,
    WORKFLOW_CASE_PROGRESS,
    WORKFLOW_DAILY_REPORT,
    WORKFLOW_INTERNAL_QA,
    WORKFLOW_LEGAL_RESEARCH,
    WORKFLOW_TRAVEL_COORDINATION,
    WORKFLOW_WEEKLY_REPORT,
    WorkflowRouter,
)


def _plan(raw_text: str, *tasks: ActiveWorkflowTask):
    return WorkflowRouter().plan(
        IncomingMessageEnvelope(
            sender_id="user-1",
            sender_name="测试用户",
            dingtalk_user_id="ding-user-1",
            source="test",
            raw_text=raw_text,
            active_tasks=tuple(tasks),
        )
    )


def test_observe_only_does_not_change_legacy_daily_behavior():
    decision = build_gate_decision(_plan("帮我查一下竞业限制最新裁判规则？"), mode="observe_only")

    assert decision.allow_legacy_daily is True
    assert decision.block_legacy_daily is False
    assert decision.reply_type == "none"
    assert "observe_only" in decision.audit_tags


def test_protective_gate_allows_daily_only_low_risk_message():
    plan = _plan("今天完成合同审查")
    decision = build_gate_decision(plan, mode="protective_gate")

    assert plan.primary_workflow == WORKFLOW_DAILY_REPORT
    assert decision.allow_legacy_daily is True
    assert decision.block_legacy_daily is False
    assert decision.audit_tags == ["daily_only_allowed"]


def test_protective_gate_blocks_internal_qa_from_daily_report():
    plan = _plan("公司印章借用流程是什么？")
    decision = build_gate_decision(plan, mode="protective_gate")

    assert plan.primary_workflow == WORKFLOW_INTERNAL_QA
    assert decision.allow_legacy_daily is False
    assert decision.reply_type == "inform_blocked"
    assert "no_daily_effect" in decision.audit_tags


def test_protective_gate_blocks_legal_research_from_daily_report():
    plan = _plan("帮我查一下竞业限制最新裁判规则？")
    decision = build_gate_decision(plan, mode="protective_gate")

    assert plan.primary_workflow == WORKFLOW_LEGAL_RESEARCH
    assert decision.allow_legacy_daily is False
    assert decision.reply_type == "inform_blocked"


def test_protective_gate_blocks_weekly_report_from_daily_report():
    plan = _plan("帮我生成本周周报，下周计划继续推进诉讼材料归档")
    decision = build_gate_decision(plan, mode="protective_gate")

    assert plan.primary_workflow == WORKFLOW_WEEKLY_REPORT
    assert decision.allow_legacy_daily is False
    assert decision.reply_type == "inform_blocked"


def test_protective_gate_clarifies_orphan_confirmation():
    decision = build_gate_decision(_plan("确认提交"), mode="protective_gate")

    assert decision.allow_legacy_daily is False
    assert decision.reply_type == "clarify"
    assert decision.need_clarification is True
    assert "orphan_confirmation" in decision.audit_tags


def test_protective_gate_allows_active_daily_confirmation_context():
    plan = _plan(
        "确认提交",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-1",
            status="pending_confirmation",
            reply_candidate=True,
            awaiting_confirmation=True,
        ),
    )
    decision = build_gate_decision(plan, mode="protective_gate")

    assert [effect.effect_type for effect in plan.effects] == [EFFECT_CONFIRM_DAILY_REPORT]
    assert decision.allow_legacy_daily is True
    assert decision.block_legacy_daily is False
    assert decision.safe_effects == [EFFECT_CONFIRM_DAILY_REPORT]


def test_protective_gate_allows_active_daily_short_context_reply():
    plan = _plan(
        "发我看下",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-2",
            status="collecting",
            reply_candidate=True,
        ),
    )
    decision = build_gate_decision(plan, mode="protective_gate")

    assert [effect.effect_type for effect in plan.effects] == [EFFECT_LEGACY_DAILY_CONTEXT_ACTION]
    assert decision.allow_legacy_daily is True
    assert decision.block_legacy_daily is False
    assert decision.safe_effects == [EFFECT_LEGACY_DAILY_CONTEXT_ACTION]


def test_protective_gate_delegates_active_daily_destructive_context_to_daily_workflow():
    plan = _plan(
        "清空吧",
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-3",
            status="collecting",
            reply_candidate=True,
        ),
    )
    decision = build_gate_decision(plan, mode="protective_gate")

    assert [effect.effect_type for effect in plan.effects] == [EFFECT_LEGACY_DAILY_CONTEXT_ACTION]
    assert decision.allow_legacy_daily is True
    assert decision.block_legacy_daily is False
    assert "daily_destructive_delegated" in decision.audit_tags


def test_protective_gate_allows_clear_report_without_confirmation():
    decision = build_gate_decision(_plan("清空今天日报"), mode="protective_gate")

    assert decision.allow_legacy_daily is True
    assert decision.reply_type == "none"
    assert decision.need_confirmation is False


def test_protective_gate_allows_delete_yesterday_report_without_confirmation():
    decision = build_gate_decision(_plan("删除昨天日报"), mode="protective_gate")

    assert decision.allow_legacy_daily is True
    assert decision.reply_type == "none"
    assert decision.need_confirmation is False


def test_travel_plus_daily_is_multi_effect_and_not_silently_written_by_legacy_daily():
    plan = _plan("明天去上海出差处理A案，今日完成合同审查")
    decision = build_gate_decision(plan, mode="protective_gate")

    assert WORKFLOW_TRAVEL_COORDINATION in plan.matched_workflows
    assert WORKFLOW_DAILY_REPORT in plan.matched_workflows
    assert {effect.effect_type for effect in plan.effects} >= {"upsert_travel_plan", "add_daily_report_item"}
    assert decision.allow_legacy_daily is True
    assert decision.reply_type == "none"
    assert "daily_allowed_with_sidecar_effects" in decision.audit_tags
    assert "upsert_travel_plan" in decision.audit_tags


def test_case_progress_plus_daily_is_observed_but_not_silently_written_as_daily_only():
    plan = _plan("今日处理A案证据目录整理")
    decision = build_gate_decision(plan, mode="protective_gate")

    assert WORKFLOW_CASE_PROGRESS in plan.matched_workflows
    assert WORKFLOW_DAILY_REPORT in plan.matched_workflows
    assert {effect.effect_type for effect in plan.effects} >= {"append_case_progress", "add_daily_report_item"}
    assert decision.allow_legacy_daily is True
    assert decision.reply_type == "none"
    assert "daily_allowed_with_sidecar_effects" in decision.audit_tags
    assert "append_case_progress" in decision.audit_tags
