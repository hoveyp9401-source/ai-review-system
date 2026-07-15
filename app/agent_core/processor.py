from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
from typing import Any

from app.agent2.coordination_plan import (
    ACTION_APPEND_CASE_PROGRESS,
    ACTION_DAILY_ENTRY,
    ACTION_TRAVEL_EVENT,
    CoordinationPlan,
    compile_coordination_plan,
)
from app.agent2.daily_commands import DailyCommand, compile_daily_commands
from app.agent_core.case_progress_capability import (
    CaseProgressCapabilityResult,
    CaseRecord,
    run_case_progress_capability,
)
from app.agent_core.daily_capability import DailyCapabilityResult, run_daily_capability
from app.agent_core.execution_policy import (
    ExecutionPolicy,
    authorize_capability_request,
    build_execution_policy,
)
from app.agent_core.monthly_capability import (
    MonthlyCapabilityResult,
    MonthlyCommand,
    MonthlySnapshot,
    run_monthly_capability,
)
from app.agent_core.operation_ledger import PersistableOperationRecord, build_persistable_operation_records
from app.agent_core.task_ledger import TaskLedgerContext, apply_task_ledger_context
from app.agent_core.travel_capability import TravelCapabilityResult, TravelPlanArtifact, run_travel_capability
from app.agent_core.types import DailySnapshot, OperationLedgerEntry
from app.workflows.action_intake import UserActionPlan, plan_user_actions
from app.workflows.intake import IncomingMessageEnvelope, RoutingPlan, WorkflowRouter


@dataclass(frozen=True)
class AgentTurnResult:
    turn_id: str
    action_plan: UserActionPlan
    routing: RoutingPlan
    coordination: CoordinationPlan
    execution_policy: str
    daily_commands: list[DailyCommand] = field(default_factory=list)
    monthly_commands: list[MonthlyCommand] = field(default_factory=list)
    operation_ledger: list[OperationLedgerEntry] = field(default_factory=list)
    daily_before: DailySnapshot = field(default_factory=DailySnapshot)
    daily_after: DailySnapshot = field(default_factory=DailySnapshot)
    monthly_before: MonthlySnapshot = field(default_factory=MonthlySnapshot)
    monthly_after: MonthlySnapshot = field(default_factory=MonthlySnapshot)
    daily_capability: DailyCapabilityResult | None = None
    monthly_capability: MonthlyCapabilityResult | None = None
    travel_capability: TravelCapabilityResult | None = None
    case_progress_capability: CaseProgressCapabilityResult | None = None
    authorization_policy: ExecutionPolicy | None = None
    task_context: TaskLedgerContext | None = None
    persisted_operation_records: list[PersistableOperationRecord] = field(default_factory=list)
    production_write: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "execution_policy": self.execution_policy,
            "production_write": self.production_write,
            "action_plan": self.action_plan.as_observation(),
            "routing": self.routing.as_observation(_redacted_envelope_for_observation(self.turn_id)),
            "coordination": self.coordination.as_observation(),
            "daily_commands": [command.as_dict() for command in self.daily_commands],
            "monthly_commands": [command.as_dict() for command in self.monthly_commands],
            "daily_before": self.daily_before.as_dict(),
            "daily_after": self.daily_after.as_dict(),
            "monthly_before": self.monthly_before.as_dict(),
            "monthly_after": self.monthly_after.as_dict(),
            "daily_capability": self.daily_capability.as_dict() if self.daily_capability else None,
            "monthly_capability": self.monthly_capability.as_dict() if self.monthly_capability else None,
            "travel_capability": self.travel_capability.as_dict() if self.travel_capability else None,
            "case_progress_capability": self.case_progress_capability.as_dict() if self.case_progress_capability else None,
            "authorization_policy": self.authorization_policy.as_dict() if self.authorization_policy else None,
            "task_context": self.task_context.as_dict() if self.task_context else None,
            "persisted_operation_records": [
                record.as_storage_dict() for record in self.persisted_operation_records
            ],
            "operation_ledger": [entry.as_dict() for entry in self.operation_ledger],
        }


def process_agent_turn(
    envelope: IncomingMessageEnvelope,
    *,
    daily_snapshot: DailySnapshot | None = None,
    monthly_snapshot: MonthlySnapshot | None = None,
    existing_travel_plans: list[TravelPlanArtifact] | tuple[TravelPlanArtifact, ...] = (),
    case_records: list[CaseRecord] | tuple[CaseRecord, ...] = (),
    router: WorkflowRouter | None = None,
    task_ledger: Any | None = None,
    operation_ledger_store: Any | None = None,
) -> AgentTurnResult:
    """Process one user turn through Agent Core without production side effects."""

    envelope, task_context = apply_task_ledger_context(envelope, task_ledger)
    daily_before = daily_snapshot or DailySnapshot()
    monthly_before = monthly_snapshot or MonthlySnapshot()
    action_plan = plan_user_actions(envelope)
    routing = (router or WorkflowRouter()).plan(envelope)
    coordination = compile_coordination_plan(envelope, routing_plan=routing)
    daily_commands = compile_daily_commands(routing, envelope, coordination_plan=coordination)
    daily_commands = _preserve_direct_daily_fact_text(
        envelope=envelope,
        coordination=coordination,
        commands=daily_commands,
    )
    monthly_commands = _compile_monthly_commands(routing, envelope)
    authorization_policy = build_execution_policy(
        turn_id=_turn_id(envelope),
        routing_plan=routing,
    )
    daily_capability = run_daily_capability(
        turn_id=_turn_id(envelope),
        snapshot=daily_before,
        commands=daily_commands,
        execution_policy=authorization_policy,
    )
    daily_after = daily_capability.after
    monthly_capability = None
    monthly_after = monthly_before
    if monthly_commands:
        monthly_capability = run_monthly_capability(
            turn_id=_turn_id(envelope),
            snapshot=monthly_before,
            commands=monthly_commands,
            execution_policy=authorization_policy,
        )
        monthly_after = monthly_capability.after
    travel_capability = None
    if any(action.action_type == ACTION_TRAVEL_EVENT for action in coordination.actions):
        travel_capability = run_travel_capability(
            turn_id=_turn_id(envelope),
            envelope=envelope,
            coordination=coordination,
            existing_plans=existing_travel_plans,
            execution_policy=authorization_policy,
        )
    case_progress_capability = None
    if any(action.action_type == ACTION_APPEND_CASE_PROGRESS for action in coordination.actions):
        case_progress_capability = run_case_progress_capability(
            turn_id=_turn_id(envelope),
            envelope=envelope,
            coordination=coordination,
            case_records=case_records,
            execution_policy=authorization_policy,
        )
    ledger = [
        *daily_capability.operation_ledger,
        *(monthly_capability.operation_ledger if monthly_capability else []),
        *(travel_capability.operation_ledger if travel_capability else []),
        *(case_progress_capability.operation_ledger if case_progress_capability else []),
        *_sidecar_operation_entries(
            _turn_id(envelope),
            coordination,
            authorization_policy,
            skip_action_types={ACTION_TRAVEL_EVENT, ACTION_APPEND_CASE_PROGRESS},
        ),
    ]
    result = AgentTurnResult(
        turn_id=_turn_id(envelope),
        action_plan=action_plan,
        routing=routing,
        coordination=coordination,
        execution_policy=_execution_policy(routing, daily_commands, monthly_commands, coordination),
        daily_commands=daily_commands,
        monthly_commands=monthly_commands,
        operation_ledger=ledger,
        daily_before=daily_before,
        daily_after=daily_after,
        monthly_before=monthly_before,
        monthly_after=monthly_after,
        daily_capability=daily_capability,
        monthly_capability=monthly_capability,
        travel_capability=travel_capability,
        case_progress_capability=case_progress_capability,
        authorization_policy=authorization_policy,
        task_context=task_context,
        production_write=False,
    )
    if operation_ledger_store is None:
        return result
    records = build_persistable_operation_records(result)
    operation_ledger_store.upsert_many(records)
    return replace(result, persisted_operation_records=records)


def _preserve_direct_daily_fact_text(
    *,
    envelope: IncomingMessageEnvelope,
    coordination: CoordinationPlan,
    commands: list[DailyCommand],
) -> list[DailyCommand]:
    """Keep a direct, whole-turn business fact byte-for-byte at the core boundary.

    The shared daily compiler also serves conversational cleanup flows.  That
    cleanup may remove date anchors such as ``today`` or ``tomorrow``.  Agent
    Core operation records are replay/audit facts, so a whole-turn daily action
    must retain the exact user-visible fact instead of inheriting presentation
    cleanup.  Extracted fragments from multi-intent turns deliberately keep the
    compiler result because their action payload is not the complete turn.
    """

    raw_text = str(envelope.raw_text or "").strip()
    if not raw_text or len(commands) != 1:
        return commands
    daily_actions = [
        action
        for action in coordination.actions
        if action.action_type == ACTION_DAILY_ENTRY
    ]
    if len(daily_actions) != 1:
        return commands
    action = daily_actions[0]
    action_content = str(action.payload.get("content") or "").strip()
    command = commands[0]
    if (
        command.operation != "fill"
        or not command.should_write
        or action_content != raw_text
        or action.source_text_chars != len(raw_text)
        or command.raw_text_hash != action.source_text_hash
    ):
        return commands
    return [replace(command, content=[raw_text])]


def _compile_monthly_commands(routing: RoutingPlan, envelope: IncomingMessageEnvelope) -> list[MonthlyCommand]:
    commands: list[MonthlyCommand] = []
    for effect in routing.effects:
        effect_type = str(getattr(effect, "effect_type", "") or "")
        if effect_type == "capture_monthly_report_reply":
            target = getattr(effect, "target", {}) or {}
            commands.append(
                MonthlyCommand(
                    operation="capture_reply",
                    raw_input=envelope.raw_text,
                    task_id=str(target.get("task_id") or routing.task_id or ""),
                    reason=str(getattr(effect, "reason", "") or routing.reason or ""),
                    source_effect_type=effect_type,
                )
            )
        elif effect_type == "confirm_monthly_report_submission":
            target = getattr(effect, "target", {}) or {}
            commands.append(
                MonthlyCommand(
                    operation="confirm_submission",
                    raw_input=envelope.raw_text,
                    task_id=str(target.get("task_id") or routing.task_id or ""),
                    reason=str(getattr(effect, "reason", "") or routing.reason or ""),
                    source_effect_type=effect_type,
                )
            )
    return commands


def _sidecar_operation_entries(
    turn_id: str,
    coordination: CoordinationPlan,
    authorization_policy: ExecutionPolicy,
    *,
    skip_action_types: set[str] | None = None,
) -> list[OperationLedgerEntry]:
    entries: list[OperationLedgerEntry] = []
    sidecar_index = 0
    skip = skip_action_types or set()
    for action in coordination.actions:
        if action.action_type == ACTION_DAILY_ENTRY or action.action_type in skip:
            continue
        sidecar_index += 1
        operation = _sidecar_operation(action.action_type)
        decision = authorize_capability_request(
            authorization_policy,
            turn_id=turn_id,
            capability=action.workflow,
            operation=operation,
            requested_write_policy="sandbox",
        )
        authorization_safety_flags = [] if decision.allowed else decision.safety_flags
        entries.append(
            OperationLedgerEntry(
                operation_id=_operation_id(turn_id, action.workflow, operation, sidecar_index),
                workflow=action.workflow,
                capability=action.workflow,
                operation=operation,
                write_policy=decision.write_policy if decision.allowed else "blocked",
                plan_id=decision.plan_id,
                authorization_id=decision.authorization_id,
                authorization_status="allowed" if decision.allowed else "denied",
                before_state={},
                after_state={} if not decision.allowed else {
                    "target": dict(action.target),
                    "payload": dict(action.payload),
                },
                changed=False,
                read_only=not decision.allowed,
                safety_flags=["sidecar", "no_production_write", *authorization_safety_flags],
                reason=action.reason if decision.allowed else decision.reason,
            )
        )
    return entries


def _sidecar_operation(action_type: str) -> str:
    if action_type == "travel_event":
        return "upsert_travel_plan"
    if action_type == "case_progress_entry":
        return "append_case_progress"
    return action_type


def _execution_policy(
    routing: RoutingPlan,
    daily_commands: list[DailyCommand],
    monthly_commands: list[MonthlyCommand],
    coordination: CoordinationPlan,
) -> str:
    if routing.safety_decision.commit_policy == "blocked" and not daily_commands and not coordination.actions:
        return "blocked"
    if any(command.should_write for command in daily_commands) or monthly_commands:
        return "dry_run"
    if any(action.requires_confirmation for action in coordination.actions):
        return "dry_run"
    if daily_commands or coordination.actions:
        return "read_only"
    return "blocked"


def _turn_id(envelope: IncomingMessageEnvelope) -> str:
    source = "|".join(
        [
            envelope.sender_id,
            envelope.dingtalk_user_id,
            envelope.source,
            envelope.message_id,
            envelope.conversation_id,
            envelope.raw_text,
        ]
    )
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]


def _operation_id(turn_id: str, workflow: str, operation: str, index: int) -> str:
    raw = f"{turn_id}:{workflow}:{operation}:{index}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _redacted_envelope_for_observation(turn_id: str) -> IncomingMessageEnvelope:
    return IncomingMessageEnvelope(
        sender_id="",
        sender_name="",
        dingtalk_user_id="",
        source="agent_core_result",
        raw_text="",
        message_id=turn_id,
        conversation_id="",
    )
