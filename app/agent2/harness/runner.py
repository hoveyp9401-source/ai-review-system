from __future__ import annotations

from app.agent2.coordination_plan import compile_coordination_plan
from app.agent2.coordination_sandbox import build_coordination_sandbox
from app.agent2.contract_invariants import evaluate_cognitive_invariants
from app.agent2.cognitive_decision import build_cognitive_decision
from app.agent2.daily_commands import compile_daily_commands
from app.agent2.harness.context_builder import build_context_pack, build_envelope
from app.agent2.harness.judges import judge_case
from app.agent2.harness.schemas import ActualOutcome, HarnessCase, HarnessResult
from app.agent2.legacy_daily_adapter import LegacyDailyAdapter
from app.workflows.gate import MODE_PROTECTIVE_GATE, build_gate_decision
from app.workflows.intake import WorkflowRouter


def run_case(case: HarnessCase, *, gate_mode: str = MODE_PROTECTIVE_GATE) -> HarnessResult:
    envelope = build_envelope(case)
    context_pack_result = build_context_pack(case, envelope=envelope)
    context_payload = context_pack_result.context_pack.as_payload()
    knowledge_items = list(context_payload.get("knowledge") or [])
    plan = WorkflowRouter().plan(envelope)
    coordination_plan = compile_coordination_plan(envelope, routing_plan=plan)
    coordination_sandbox = build_coordination_sandbox(coordination_plan)
    gate = build_gate_decision(plan, mode=gate_mode)
    commands = compile_daily_commands(plan, envelope, coordination_plan=coordination_plan)
    legacy_adapter_results = LegacyDailyAdapter().adapt_commands(commands)
    cognitive_decision = build_cognitive_decision(
        envelope=envelope,
        routing_plan=plan,
        gate_decision=gate,
        coordination_plan=coordination_plan,
        coordination_sandbox=coordination_sandbox,
        daily_commands=commands,
        legacy_adapter_results=legacy_adapter_results,
    )
    contract_violations = evaluate_cognitive_invariants(cognitive_decision)
    actual = ActualOutcome(
        primary_workflow=plan.primary_workflow,
        matched_workflows=list(plan.matched_workflows),
        effect_types=[effect.effect_type for effect in plan.effects],
        safety_commit_policy=plan.safety_decision.commit_policy,
        safety_flags=list(plan.safety_decision.flags),
        gate_allow_legacy_daily=gate.allow_legacy_daily,
        gate_block_legacy_daily=gate.block_legacy_daily,
        gate_reply_type=gate.reply_type,
        gate_need_confirmation=gate.need_confirmation,
        gate_need_clarification=gate.need_clarification,
        gate_audit_tags=list(gate.audit_tags),
        target_fields=_dedupe(
            [
                *[
                    str(effect.target.get("field"))
                    for effect in plan.effects
                    if effect.target.get("field")
                ],
                *[
                    str(command.target_field)
                    for command in commands
                    if command.target_field not in {"none", "unknown"}
                ],
            ]
        ),
        coordination_action_types=[action.action_type for action in coordination_plan.actions],
        coordination_actions=[action.as_observation() for action in coordination_plan.actions],
        sandbox_candidate_types=[candidate.candidate_type for candidate in coordination_sandbox.candidates],
        sandbox_candidates=[candidate.as_observation() for candidate in coordination_sandbox.candidates],
        sandbox_notification_count=coordination_sandbox.notification_count,
        sandbox_official_write_count=coordination_sandbox.official_write_count,
        segments=[
            {
                "index": segment.index,
                "primary_workflow": segment.primary_workflow,
                "matched_workflows": list(segment.matched_workflows),
                "effect_types": list(segment.effect_types),
                "intent": segment.intent,
                "confidence": segment.confidence,
            }
            for segment in plan.segments
        ],
        commands=[command.as_dict() for command in commands],
        legacy_adapter_results=[result.as_dict() for result in legacy_adapter_results],
        cognitive_decision=cognitive_decision.as_dict(),
        contract_invariant_violations=[violation.as_dict() for violation in contract_violations],
        knowledge_status=str(context_payload.get("knowledge_status") or "not_retrieved_or_no_match"),
        knowledge_source_types=_dedupe([str(item.get("source_type") or "") for item in knowledge_items]),
        knowledge_titles=[str(item.get("title") or "") for item in knowledge_items if str(item.get("title") or "")],
        knowledge_facts=[
            dict(item.get("facts") or {})
            for item in knowledge_items
            if isinstance(item.get("facts"), dict)
        ],
        warnings=list(context_pack_result.knowledge_warnings),
    )
    return judge_case(case, actual)


def run_cases(cases: list[HarnessCase], *, gate_mode: str = MODE_PROTECTIVE_GATE) -> list[HarnessResult]:
    return [run_case(case, gate_mode=gate_mode) for case in cases]


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result
