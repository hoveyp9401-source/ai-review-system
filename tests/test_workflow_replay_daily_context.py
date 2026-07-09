from types import SimpleNamespace

from app.workflows.daily_context import (
    update_replay_daily_context_from_agent2_plan,
)
from app.workflows.gate import GateDecision
from app.workflows.intake import (
    EFFECT_ADD_DAILY_REPORT_ITEM,
    EFFECT_LEGACY_DAILY_CONTEXT_ACTION,
    WORKFLOW_DAILY_REPORT,
    RoutingPlan,
    SafetyDecision,
    WorkflowEffect,
)


def _plan(effect_type: str) -> RoutingPlan:
    effects = [
        WorkflowEffect(
            effect_type=effect_type,
            target_system=WORKFLOW_DAILY_REPORT,
            target={"field": "today_work", "status": "collecting"},
            payload={"content": "今天完成合同审核"},
            risk_level="low",
            reason="test",
        )
    ]
    return RoutingPlan(
        primary_workflow=WORKFLOW_DAILY_REPORT,
        matched_workflows=[WORKFLOW_DAILY_REPORT],
        effects=effects,
        safety_decision=SafetyDecision(commit_policy="partial_allowed"),
    )


def _gate(*, allow: bool) -> GateDecision:
    return GateDecision(
        mode="protective_gate",
        allow_legacy_daily=allow,
        block_legacy_daily=not allow,
        reply_type="allow_legacy_daily" if allow else "clarify",
    )


def test_replay_context_uses_agent2_daily_write_to_activate_followup_context():
    context = update_replay_daily_context_from_agent2_plan(
        None,
        SimpleNamespace(report_date="2026-07-03", legacy_report_id=""),
        _plan(EFFECT_ADD_DAILY_REPORT_ITEM),
        _gate(allow=True),
    )

    task = context.as_task()
    assert task is not None
    assert task.workflow == WORKFLOW_DAILY_REPORT
    assert task.reply_candidate is True
    assert task.metadata["report_date"] == "2026-07-03"


def test_replay_context_does_not_activate_when_gate_blocks_daily():
    context = update_replay_daily_context_from_agent2_plan(
        None,
        SimpleNamespace(report_date="2026-07-03", legacy_report_id=""),
        _plan(EFFECT_LEGACY_DAILY_CONTEXT_ACTION),
        _gate(allow=False),
    )

    assert context is None
