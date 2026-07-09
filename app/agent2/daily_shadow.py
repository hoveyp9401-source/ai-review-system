from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from app.agent2.assistant_responder import AssistantReply, build_assistant_reply
from app.agent2.coordination_plan import CoordinationPlan, compile_coordination_plan
from app.agent2.coordination_sandbox import CoordinationSandboxResult, build_coordination_sandbox
from app.agent2.cognitive_decision import CognitiveDecision, build_cognitive_decision
from app.agent2.daily_commands import DailyCommand, compile_daily_commands
from app.agent2.legacy_daily_adapter import LegacyDailyAdapter, LegacyDailyAdapterResult
from app.workflows.gate import GateDecision, build_gate_decision
from app.workflows.intake import IncomingMessageEnvelope, RoutingPlan, WorkflowRoute, WorkflowRouter


@dataclass(frozen=True)
class DailyShadowEvaluation:
    """Dry-run Agent2 daily evaluation beside the legacy daily path."""

    route: WorkflowRoute
    plan: RoutingPlan
    gate_decision: GateDecision
    coordination_plan: CoordinationPlan
    coordination_sandbox: CoordinationSandboxResult
    commands: list[DailyCommand]
    legacy_adapter_results: list[LegacyDailyAdapterResult]
    cognitive_decision: CognitiveDecision
    assistant_reply: AssistantReply | None = None

    def route_observation(self, envelope: IncomingMessageEnvelope) -> dict[str, Any]:
        return self.route.as_observation(envelope)

    def gate_observation(self, envelope: IncomingMessageEnvelope) -> dict[str, Any]:
        return {
            "plan": self.plan.as_observation(envelope),
            "gate": self.gate_decision.as_observation(),
            "coordination_plan": self.coordination_plan.as_observation(),
            "coordination_sandbox": self.coordination_sandbox.as_observation(),
            "daily_commands": [command.as_dict() for command in self.commands],
            "legacy_adapter": [result.as_dict() for result in self.legacy_adapter_results],
            "cognitive_decision": self.cognitive_decision.as_dict(),
            "assistant_reply": self.assistant_reply.as_observation() if self.assistant_reply else None,
            "summary": self.summary(),
        }

    def summary(self) -> dict[str, Any]:
        adapter_status_counts = Counter(result.status for result in self.legacy_adapter_results)
        command_operation_counts = Counter(command.operation for command in self.commands)
        return {
            "coordination_action_count": len(self.coordination_plan.actions),
            "coordination_action_type_counts": dict(
                sorted(Counter(action.action_type for action in self.coordination_plan.actions).items())
            ),
            "sandbox_candidate_count": len(self.coordination_sandbox.candidates),
            "sandbox_candidate_type_counts": dict(
                sorted(Counter(candidate.candidate_type for candidate in self.coordination_sandbox.candidates).items())
            ),
            "sandbox_official_write_count": self.coordination_sandbox.official_write_count,
            "sandbox_notification_count": self.coordination_sandbox.notification_count,
            "command_count": len(self.commands),
            "adapter_result_count": len(self.legacy_adapter_results),
            "command_operation_counts": dict(sorted(command_operation_counts.items())),
            "adapter_status_counts": dict(sorted(adapter_status_counts.items())),
            "adapter_write_impact_count": sum(1 for result in self.legacy_adapter_results if result.write_impact),
            "adapter_read_only_count": sum(1 for result in self.legacy_adapter_results if result.read_only),
            "adapter_confirmation_count": sum(
                1 for result in self.legacy_adapter_results if result.requires_confirmation
            ),
            "assistant_reply_type": self.assistant_reply.reply_type if self.assistant_reply else "",
        }


def evaluate_daily_shadow(
    envelope: IncomingMessageEnvelope,
    *,
    mode: Any = "observe_only",
    router: WorkflowRouter | None = None,
    adapter: LegacyDailyAdapter | None = None,
) -> DailyShadowEvaluation:
    workflow_router = router or WorkflowRouter()
    daily_adapter = adapter or LegacyDailyAdapter()
    plan = workflow_router.plan(envelope)
    route = _route_from_plan(plan)
    gate_decision = build_gate_decision(plan, mode=mode)
    coordination_plan = compile_coordination_plan(envelope, routing_plan=plan, router=workflow_router)
    coordination_sandbox = build_coordination_sandbox(coordination_plan)
    commands = compile_daily_commands(plan, envelope, coordination_plan=coordination_plan)
    gate_decision = _allow_coordination_daily_command(gate_decision, commands)
    legacy_adapter_results = daily_adapter.adapt_commands(commands)
    cognitive_decision = build_cognitive_decision(
        envelope=envelope,
        routing_plan=plan,
        gate_decision=gate_decision,
        coordination_plan=coordination_plan,
        coordination_sandbox=coordination_sandbox,
        daily_commands=commands,
        legacy_adapter_results=legacy_adapter_results,
    )
    assistant_reply = build_assistant_reply(
        raw_text=envelope.raw_text,
        plan=plan,
        gate_decision=gate_decision,
    )
    return DailyShadowEvaluation(
        route=route,
        plan=plan,
        gate_decision=gate_decision,
        coordination_plan=coordination_plan,
        coordination_sandbox=coordination_sandbox,
        commands=commands,
        legacy_adapter_results=legacy_adapter_results,
        cognitive_decision=cognitive_decision,
        assistant_reply=assistant_reply,
    )


def _allow_coordination_daily_command(gate_decision: GateDecision, commands: list[DailyCommand]) -> GateDecision:
    if not gate_decision.block_legacy_daily:
        return gate_decision
    if "low_confidence_daily" not in gate_decision.audit_tags:
        return gate_decision
    if not any(
        command.source_effect_type == "daily_entry"
        and command.operation == "fill"
        and command.should_write
        and command.confidence in {"medium", "high"}
        for command in commands
    ):
        return gate_decision
    return GateDecision(
        mode=gate_decision.mode,
        allow_legacy_daily=True,
        block_legacy_daily=False,
        reply_type="normal",
        safe_effects=["coordination_daily_entry"],
        pending_effects=[],
        blocked_effects=list(gate_decision.blocked_effects),
        need_confirmation=False,
        need_clarification=False,
        reason="coordination daily command resolved low-confidence legacy daily effect",
        audit_tags=[tag for tag in gate_decision.audit_tags if tag != "low_confidence_daily"] + ["coordination_daily_override"],
        reply_text="",
    )


def _route_from_plan(plan: RoutingPlan) -> WorkflowRoute:
    return WorkflowRoute(
        workflow=plan.primary_workflow,
        confidence=plan.confidence,
        reason=plan.reason,
        task_id=plan.task_id,
        observe_only=plan.observe_only,
        signals=dict(plan.signals),
    )
