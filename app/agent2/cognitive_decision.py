from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from app.agent2.coordination_plan import CoordinationPlan
from app.agent2.coordination_sandbox import CoordinationSandboxResult
from app.agent2.daily_commands import DailyCommand
from app.agent2.legacy_daily_adapter import LegacyDailyAdapterResult
from app.workflows.action_intake import UserActionPlan, plan_user_actions
from app.workflows.gate import GateDecision
from app.workflows.intake import IncomingMessageEnvelope, RoutingPlan, WorkflowEffect


COGNITIVE_CONTRACT_VERSION = "cognitive_contract.v2"


@dataclass(frozen=True)
class CognitiveAction:
    """Stable observation for one user-intent segment."""

    segment_index: int
    source_text_hash: str
    source_text_chars: int
    workflow: str
    action_type: str
    operation: str
    target_field: str = "none"
    write_policy: str = "no_write"
    target: dict[str, Any] = field(default_factory=dict)
    payload_keys: list[str] = field(default_factory=list)
    confidence: float = 0.0
    requires_confirmation: bool = False
    safety_flags: list[str] = field(default_factory=list)
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "segment_index": self.segment_index,
            "source_text_hash": self.source_text_hash,
            "source_text_chars": self.source_text_chars,
            "workflow": self.workflow,
            "action_type": self.action_type,
            "operation": self.operation,
            "target_field": self.target_field,
            "write_policy": self.write_policy,
            "target": dict(self.target),
            "payload_keys": list(self.payload_keys),
            "confidence": self.confidence,
            "requires_confirmation": self.requires_confirmation,
            "safety_flags": list(self.safety_flags),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class CognitiveDecision:
    """Unified Agent2 decision trace.

    This is an observation contract only. It does not route, write, or mutate
    workflow state; it makes the existing action/routing/gate/command chain
    visible to tests and audit logs.
    """

    primary_workflow: str
    matched_workflows: list[str]
    commit_policy: str
    allow_write: bool
    need_confirmation: bool
    need_clarification: bool
    gate_reply_type: str
    confidence: float
    reason: str
    source_text_hash: str
    source_text_chars: int
    actions: list[CognitiveAction] = field(default_factory=list)
    segments: list[dict[str, Any]] = field(default_factory=list)
    effects: list[dict[str, Any]] = field(default_factory=list)
    coordination_plan: dict[str, Any] = field(default_factory=dict)
    coordination_sandbox: dict[str, Any] = field(default_factory=dict)
    daily_commands: list[dict[str, Any]] = field(default_factory=list)
    legacy_adapter: list[dict[str, Any]] = field(default_factory=list)
    action_context: dict[str, Any] = field(default_factory=dict)
    audit_tags: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    contract_version: str = COGNITIVE_CONTRACT_VERSION
    execution_trace: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "primary_workflow": self.primary_workflow,
            "matched_workflows": list(self.matched_workflows),
            "commit_policy": self.commit_policy,
            "allow_write": self.allow_write,
            "need_confirmation": self.need_confirmation,
            "need_clarification": self.need_clarification,
            "gate_reply_type": self.gate_reply_type,
            "confidence": self.confidence,
            "reason": self.reason,
            "source_text_hash": self.source_text_hash,
            "raw_text_hash": self.source_text_hash,
            "source_text_chars": self.source_text_chars,
            "raw_text_chars": self.source_text_chars,
            "actions": [action.as_dict() for action in self.actions],
            "segments": list(self.segments),
            "effects": list(self.effects),
            "coordination_plan": dict(self.coordination_plan),
            "coordination_sandbox": dict(self.coordination_sandbox),
            "daily_commands": list(self.daily_commands),
            "legacy_adapter": list(self.legacy_adapter),
            "action_context": dict(self.action_context),
            "audit_tags": list(self.audit_tags),
            "warnings": list(self.warnings),
            "execution_trace": dict(self.execution_trace),
        }


def build_cognitive_decision(
    *,
    envelope: IncomingMessageEnvelope,
    routing_plan: RoutingPlan,
    gate_decision: GateDecision,
    coordination_plan: CoordinationPlan,
    coordination_sandbox: CoordinationSandboxResult,
    daily_commands: list[DailyCommand],
    legacy_adapter_results: list[LegacyDailyAdapterResult] | None = None,
    action_plan: UserActionPlan | None = None,
) -> CognitiveDecision:
    user_action_plan = action_plan or plan_user_actions(envelope)
    daily_command_dicts = [command.as_dict() for command in daily_commands]
    legacy_adapter_dicts = [result.as_dict() for result in (legacy_adapter_results or [])]
    return CognitiveDecision(
        primary_workflow=routing_plan.primary_workflow,
        matched_workflows=list(routing_plan.matched_workflows),
        commit_policy=routing_plan.safety_decision.commit_policy,
        allow_write=_allow_write(gate_decision, daily_commands, legacy_adapter_results or []),
        need_confirmation=_need_confirmation(gate_decision, daily_commands),
        need_clarification=gate_decision.need_clarification,
        gate_reply_type=gate_decision.reply_type,
        confidence=routing_plan.confidence,
        reason=routing_plan.reason,
        source_text_hash=user_action_plan.source_text_hash or envelope.observation_base()["raw_text_hash"],
        source_text_chars=user_action_plan.source_text_chars or len(envelope.raw_text or ""),
        actions=_cognitive_actions(user_action_plan),
        segments=[segment.as_observation() for segment in routing_plan.segments],
        effects=[effect.as_observation() for effect in routing_plan.effects],
        coordination_plan=coordination_plan.as_observation(),
        coordination_sandbox=coordination_sandbox.as_observation(),
        daily_commands=daily_command_dicts,
        legacy_adapter=legacy_adapter_dicts,
        action_context=dict(user_action_plan.action_context),
        audit_tags=list(gate_decision.audit_tags),
        warnings=_warnings(routing_plan, gate_decision, daily_commands, legacy_adapter_results or []),
    )


def with_execution_trace(
    decision: CognitiveDecision,
    *,
    actual_write: bool | None = None,
    execution_status: str = "",
    block_reason: str = "",
    fallback_used: bool = False,
    execution_result: dict[str, Any] | None = None,
) -> CognitiveDecision:
    """Attach execution observations without rebuilding the cognitive contract."""

    trace = dict(decision.execution_trace)
    trace.update(
        {
            "actual_write": actual_write,
            "execution_status": str(execution_status or ""),
            "block_reason": str(block_reason or ""),
            "fallback_used": bool(fallback_used),
            "execution_result": dict(execution_result or {}),
        }
    )
    return replace(decision, execution_trace=trace)


def _cognitive_actions(action_plan: UserActionPlan) -> list[CognitiveAction]:
    actions: list[CognitiveAction] = []
    for action in action_plan.actions:
        actions.append(
            CognitiveAction(
                segment_index=action.source_segment_index,
                source_text_hash=action.source_text_hash,
                source_text_chars=action.source_text_chars,
                workflow=action.workflow,
                action_type=action.action_type,
                operation=action.operation,
                target_field=action.target_field,
                write_policy=action.write_policy,
                target=dict(action.target),
                payload_keys=sorted(action.payload.keys()),
                confidence=action.confidence,
                requires_confirmation=action.requires_confirmation,
                safety_flags=list(action.safety_flags),
                reason=action.reason,
            )
        )
    return actions


def _allow_write(
    gate_decision: GateDecision,
    daily_commands: list[DailyCommand],
    legacy_adapter_results: list[LegacyDailyAdapterResult],
) -> bool:
    if gate_decision.block_legacy_daily or gate_decision.need_confirmation or gate_decision.need_clarification:
        return False
    return any(command.should_write and not command.requires_confirmation for command in daily_commands)


def _need_confirmation(gate_decision: GateDecision, daily_commands: list[DailyCommand]) -> bool:
    return gate_decision.need_confirmation or any(command.requires_confirmation for command in daily_commands)


def _warnings(
    routing_plan: RoutingPlan,
    gate_decision: GateDecision,
    daily_commands: list[DailyCommand],
    legacy_adapter_results: list[LegacyDailyAdapterResult],
) -> list[str]:
    warnings: list[str] = []
    warnings.extend(_missing_effect_contract_warnings(routing_plan.effects))
    if gate_decision.block_legacy_daily and any(command.should_write for command in daily_commands):
        warnings.append("gate_blocks_write_commands")
    if any(command.should_write and "destructive_or_overwrite" in command.safety_flags for command in daily_commands):
        warnings.append("destructive_or_overwrite_command")
    if legacy_adapter_results and any(result.status in {"blocked", "unsupported"} for result in legacy_adapter_results):
        warnings.append("legacy_adapter_not_ready")
    return _dedupe(warnings)


def _missing_effect_contract_warnings(effects: list[WorkflowEffect]) -> list[str]:
    warnings: list[str] = []
    for effect in effects:
        if effect.target_system != "daily_report":
            continue
        if not effect.target.get("operation"):
            warnings.append("daily_effect_missing_operation_contract")
        if not effect.target.get("action_type"):
            warnings.append("daily_effect_missing_action_type_contract")
    return warnings


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result
