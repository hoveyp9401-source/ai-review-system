from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
import hashlib
import json
import re
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from app.agent2.cognitive_core_v3 import CognitiveDecisionV3, RequiredAction
from app.agent2.conversation_state import ConversationEntity, UserConstraints
from app.agent2.typed_daily_commands import (
    DailyReportMutationSnapshot,
    REPORT_FIELDS,
    TypedDailyCommand,
    execute_typed_daily_command,
)
from app.agent2.report_domain import (
    PeriodicReportSnapshot,
    TypedPeriodicReportCommand,
    execute_periodic_report_command,
)


BusinessExecutionMode = Literal["read_only", "candidate"]
DAILY_DRAFT_MUTABLE_STATUSES = frozenset({"collecting", "pending_confirmation"})


@dataclass(frozen=True)
class TypedBusinessCommand:
    command_id: UUID
    decision_id: UUID
    sub_decision_id: UUID
    command_type: str
    target_system: str
    entity_ids: tuple[str, ...]
    payload: dict[str, Any]
    execution_mode: BusinessExecutionMode
    idempotency_key: str
    admission_ticket: dict[str, Any] = field(default_factory=dict)
    admission_required: bool = False
    admission_action_id: str = ""
    admission_operation: str = ""

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "command_id": str(self.command_id),
            "decision_id": str(self.decision_id),
            "sub_decision_id": str(self.sub_decision_id),
            "command_type": self.command_type,
            "target_system": self.target_system,
            "entity_ids": list(self.entity_ids),
            "payload": dict(self.payload),
            "execution_mode": self.execution_mode,
            "idempotency_key": self.idempotency_key,
        }
        if self.admission_ticket or self.admission_required:
            payload.update(
                {
                    "admission_ticket": dict(self.admission_ticket),
                    "admission_required": self.admission_required,
                    "admission_action_id": self.admission_action_id,
                    "admission_operation": self.admission_operation,
                }
            )
        return payload


@dataclass(frozen=True)
class PlanningBlock:
    action_id: str
    reason_code: str
    detail: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DailySnapshotReference:
    report_date: date
    snapshot: DailyReportMutationSnapshot


@dataclass(frozen=True)
class CommandPlanningContext:
    message_id: str
    actor_user_id: UUID
    daily_snapshot: DailyReportMutationSnapshot | None = None
    daily_history: tuple[DailySnapshotReference, ...] = ()
    current_report_date: date | None = None
    historical_mutation_allowed: bool = True
    user_constraints: UserConstraints = field(default_factory=UserConstraints)
    periodic_snapshot: PeriodicReportSnapshot | None = None


@dataclass(frozen=True)
class CognitiveCommandPlan:
    decision_id: UUID
    daily_commands: tuple[TypedDailyCommand, ...] = ()
    business_commands: tuple[TypedBusinessCommand, ...] = ()
    report_commands: tuple[TypedPeriodicReportCommand, ...] = ()
    blocked_actions: tuple[PlanningBlock, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision_id": str(self.decision_id),
            "daily_commands": [command.as_dict() for command in self.daily_commands],
            "business_commands": [command.as_dict() for command in self.business_commands],
            "report_commands": [command.as_dict() for command in self.report_commands],
            "blocked_actions": [
                {
                    "action_id": block.action_id,
                    "reason_code": block.reason_code,
                    "detail": block.detail,
                    "metadata": dict(block.metadata),
                }
                for block in self.blocked_actions
            ],
        }


class CognitiveCommandPlanner:
    """Translate cognitive actions into typed commands without reading natural language."""

    def plan(self, decision: CognitiveDecisionV3, context: CommandPlanningContext) -> CognitiveCommandPlan:
        if not str(context.message_id or "").strip():
            raise ValueError("command planning requires message_id")
        decision_id = UUID(decision.decision_id)
        entities = {entity.entity_id: entity for entity in decision.entities}
        daily_commands: list[TypedDailyCommand] = []
        business_commands: list[TypedBusinessCommand] = []
        report_commands: list[TypedPeriodicReportCommand] = []
        blocked: list[PlanningBlock] = []
        if decision.admission_mode == "enforced" and decision.admission_trace is not None:
            pending_by_decision = {
                pending.decision_id: pending
                for pending in decision.admission_information_pendings
            }
            for admission_decision in decision.admission_trace.decisions:
                if admission_decision.status not in {
                    "blocked",
                    "information_required",
                    "review_only",
                    "deferred_audit_only",
                }:
                    continue
                metadata: dict[str, Any] = {
                    "admission": {
                        "decision_id": admission_decision.decision_id,
                        "status": admission_decision.status,
                        "domain": admission_decision.domain,
                        "operation": admission_decision.operation,
                        "segment_id": admission_decision.segment_id,
                    }
                }
                pending = pending_by_decision.get(admission_decision.decision_id)
                if pending is not None:
                    metadata["information_pending"] = pending.as_dict()
                blocked.append(
                    PlanningBlock(
                        admission_decision.action_id,
                        admission_decision.reason_code,
                        metadata=metadata,
                    )
                )
        working_context = context
        for action in decision.required_actions:
            admission_ticket_payload: dict[str, Any] = {}
            admission_required = (
                decision.admission_mode == "enforced"
                and _action_requires_admission_ticket(action.action_type)
            )
            if admission_required:
                matching_tickets = tuple(
                    ticket
                    for ticket in decision.admission_tickets
                    if ticket.action_id == action.action_id
                )
                if not matching_tickets:
                    blocked.append(
                        PlanningBlock(action.action_id, "missing_admission_ticket")
                    )
                    continue
                if len(matching_tickets) != 1:
                    blocked.append(
                        PlanningBlock(action.action_id, "ambiguous_admission_ticket")
                    )
                    continue
                ticket = matching_tickets[0]
                if (
                    ticket.source_message_id != context.message_id
                    or ticket.operation != action.action_type
                ):
                    blocked.append(
                        PlanningBlock(action.action_id, "admission_ticket_scope_mismatch")
                    )
                    continue
                admission_ticket_payload = ticket.as_dict()
            action_entities = tuple(entities[entity_id] for entity_id in action.entity_ids)
            if action.action_type in {
                "capture_report_event",
                "query_periodic_report",
                "submit_periodic_report",
                "edit_periodic_report_item",
                "delete_periodic_report_item",
            }:
                command, block = self._plan_periodic_report(
                    decision_id=decision_id,
                    action=action,
                    entities=action_entities,
                    context=working_context,
                )
                if command is not None:
                    command = _bind_report_admission(
                        command,
                        action=action,
                        admission_ticket=admission_ticket_payload,
                        admission_required=admission_required,
                    )
                    report_commands.append(command)
                    if working_context.periodic_snapshot is not None:
                        simulated = execute_periodic_report_command(
                            replace(
                                command,
                                admission_ticket={},
                                admission_required=False,
                                admission_action_id="",
                                admission_operation="",
                            ),
                            snapshot=working_context.periodic_snapshot,
                            actor_user_id=context.actor_user_id,
                        )
                        working_context = replace(
                            working_context,
                            periodic_snapshot=simulated.after,
                        )
                if block is not None:
                    blocked.append(block)
                continue
            if action.action_type == "capture_daily_event":
                command, block = self._plan_daily_append(
                    decision_id=decision_id,
                    action=action,
                    entities=action_entities,
                    context=working_context,
                )
                if command is not None:
                    daily_commands.append(
                        _bind_report_admission(
                            command,
                            action=action,
                            admission_ticket=admission_ticket_payload,
                            admission_required=admission_required,
                        )
                    )
                    working_context = _advance_daily_context(working_context)
                if block is not None:
                    blocked.append(block)
                continue
            if action.action_type == "replace_daily_section":
                command, block = self._plan_daily_section_replace(
                    decision_id=decision_id,
                    action=action,
                    entities=action_entities,
                    context=working_context,
                )
                if command is not None:
                    daily_commands.append(
                        _bind_report_admission(
                            command,
                            action=action,
                            admission_ticket=admission_ticket_payload,
                            admission_required=admission_required,
                        )
                    )
                    working_context = _advance_daily_context(working_context, command)
                if block is not None:
                    blocked.append(block)
                continue
            if action.action_type == "submit_daily_report":
                command, block = self._plan_daily_submit(
                    decision_id=decision_id,
                    action=action,
                    context=working_context,
                )
                if command is not None:
                    daily_commands.append(_bind_report_admission(
                        command, action=action,
                        admission_ticket=admission_ticket_payload,
                        admission_required=admission_required,
                    ))
                    working_context = _advance_daily_context(working_context)
                if block is not None:
                    blocked.append(block)
                continue
            if action.action_type == "delete_daily_item":
                command, block = self._plan_daily_item_mutation(
                    decision_id=decision_id,
                    action=action,
                    entities=action_entities,
                    context=working_context,
                    command_type="delete_item",
                )
                if command is not None:
                    daily_commands.append(_bind_report_admission(
                        command, action=action,
                        admission_ticket=admission_ticket_payload,
                        admission_required=admission_required,
                    ))
                    working_context = _advance_daily_context(working_context)
                if block is not None:
                    blocked.append(block)
                continue
            if action.action_type in {
                "edit_daily_item",
                "merge_daily_items",
                "move_daily_items",
            }:
                command, block = self._plan_daily_item_mutation(
                    decision_id=decision_id,
                    action=action,
                    entities=action_entities,
                    context=working_context,
                    command_type={
                        "edit_daily_item": "edit_item",
                        "merge_daily_items": "merge_items",
                        "move_daily_items": "move_items",
                    }[action.action_type],
                )
                if command is not None:
                    daily_commands.append(_bind_report_admission(
                        command, action=action,
                        admission_ticket=admission_ticket_payload,
                        admission_required=admission_required,
                    ))
                    working_context = _advance_daily_context(working_context, command)
                if block is not None:
                    blocked.append(block)
                continue
            if action.action_type == "query_daily_report":
                command, block = self._plan_daily_query(
                    decision_id=decision_id,
                    action=action,
                    entities=action_entities,
                    context=working_context,
                )
                if command is not None:
                    daily_commands.append(_bind_report_admission(
                        command, action=action,
                        admission_ticket=admission_ticket_payload,
                        admission_required=admission_required,
                    ))
                if block is not None:
                    blocked.append(block)
                continue
            if action.action_type == "clear_daily_report":
                command, block = self._plan_daily_clear(
                    decision_id=decision_id,
                    action=action,
                    entities=action_entities,
                    context=working_context,
                )
                if command is not None:
                    daily_commands.append(_bind_report_admission(
                        command, action=action,
                        admission_ticket=admission_ticket_payload,
                        admission_required=admission_required,
                    ))
                    working_context = _advance_daily_context(working_context)
                if block is not None:
                    blocked.append(block)
                continue
            if action.action_type == "clear_daily_section":
                command, block = self._plan_daily_section_clear(
                    decision_id=decision_id,
                    action=action,
                    entities=action_entities,
                    context=working_context,
                )
                if command is not None:
                    daily_commands.append(_bind_report_admission(
                        command, action=action,
                        admission_ticket=admission_ticket_payload,
                        admission_required=admission_required,
                    ))
                    working_context = _advance_daily_context(working_context)
                if block is not None:
                    blocked.append(block)
                continue
            if action.action_type == "reopen_daily_report":
                command, block = self._plan_daily_reopen(
                    decision_id=decision_id,
                    action=action,
                    entities=action_entities,
                    context=working_context,
                )
                if command is not None:
                    daily_commands.append(_bind_report_admission(
                        command, action=action,
                        admission_ticket=admission_ticket_payload,
                        admission_required=admission_required,
                    ))
                    working_context = _advance_daily_context(working_context)
                if block is not None:
                    blocked.append(block)
                continue
            if action.action_type in {
                "copy_current_work_to_tomorrow",
                "complete_previous_daily_plan",
            }:
                command, block = self._plan_daily_projection(
                    decision_id=decision_id,
                    action=action,
                    entities=action_entities,
                    context=working_context,
                )
                if command is not None:
                    daily_commands.append(_bind_report_admission(
                        command, action=action,
                        admission_ticket=admission_ticket_payload,
                        admission_required=admission_required,
                    ))
                    working_context = _advance_daily_context(working_context)
                if block is not None:
                    blocked.append(block)
                continue
            if action.action_type == "answer_case_query":
                command_type, target_system = _classify_case_query(entities=action_entities)
                business_commands.append(
                    self._business_command(
                        decision=decision,
                        decision_id=decision_id,
                        action=action,
                        entities=action_entities,
                        command_type=command_type,
                        target_system=target_system,
                        execution_mode="read_only",
                        message_id=context.message_id,
                    )
                )
                continue
            if action.action_type == "query_operation_status":
                business_commands.append(
                    self._business_command(
                        decision=decision,
                        decision_id=decision_id,
                        action=action,
                        entities=action_entities,
                        command_type="query_operation_status",
                        target_system="operation_outcomes",
                        execution_mode="read_only",
                        message_id=context.message_id,
                    )
                )
                continue
            if action.action_type == "record_travel_event":
                business_commands.append(
                    self._business_command(
                        decision=decision,
                        decision_id=decision_id,
                        action=action,
                        entities=action_entities,
                        command_type="record_travel_candidate",
                        target_system="travel_coordination",
                        execution_mode="candidate",
                        message_id=context.message_id,
                    )
                )
                continue
            if action.action_type == "update_travel_event":
                business_commands.append(
                    self._business_command(
                        decision=decision,
                        decision_id=decision_id,
                        action=action,
                        entities=action_entities,
                        command_type="update_travel_candidate",
                        target_system="travel_coordination",
                        execution_mode="candidate",
                        message_id=context.message_id,
                    )
                )
                continue
            if action.action_type == "respond_travel_collaboration":
                business_commands.append(
                    self._business_command(
                        decision=decision,
                        decision_id=decision_id,
                        action=action,
                        entities=action_entities,
                        command_type="respond_travel_collaboration_candidate",
                        target_system="travel_coordination",
                        execution_mode="candidate",
                        message_id=context.message_id,
                    )
                )
                continue
            if action.action_type == "search_enterprise_knowledge":
                business_commands.append(
                    self._business_command(
                        decision=decision,
                        decision_id=decision_id,
                        action=action,
                        entities=action_entities,
                        command_type="search_enterprise_knowledge",
                        target_system="enterprise_knowledge",
                        execution_mode="read_only",
                        message_id=context.message_id,
                    )
                )
                continue
            if action.action_type == "record_case_progress":
                business_commands.append(
                    self._business_command(
                        decision=decision,
                        decision_id=decision_id,
                        action=action,
                        entities=action_entities,
                        command_type="record_case_progress_candidate",
                        target_system="case_progress",
                        execution_mode="candidate",
                        message_id=context.message_id,
                    )
                )
                continue
            if action.action_type in {
                "update_case_followup_policy", "trigger_case_followup_now"
            }:
                business_commands.append(
                    self._business_command(
                        decision=decision,
                        decision_id=decision_id,
                        action=action,
                        entities=action_entities,
                        command_type=(
                            "update_case_followup_policy_candidate"
                            if action.action_type == "update_case_followup_policy"
                            else "trigger_case_followup_now_candidate"
                        ),
                        target_system="case_followup_policy",
                        execution_mode="candidate",
                        message_id=context.message_id,
                    )
                )
                continue
            if action.action_type in {
                "update_case_progress",
                "delete_case_progress",
                "query_case_progress",
                "link_case_progress",
            }:
                candidate_type = {
                    "update_case_progress": "update_case_progress_candidate",
                    "delete_case_progress": "delete_case_progress_candidate",
                    "query_case_progress": "query_case_progress_candidate",
                    "link_case_progress": "link_case_progress_candidate",
                }[action.action_type]
                business_commands.append(
                    self._business_command(
                        decision=decision,
                        decision_id=decision_id,
                        action=action,
                        entities=action_entities,
                        command_type=candidate_type,
                        target_system="case_progress",
                        execution_mode="candidate",
                        message_id=context.message_id,
                    )
                )
                continue
            if action.action_type == "copy_previous_daily_report":
                command, block = self._plan_daily_copy(
                    decision_id=decision_id,
                    action=action,
                    entities=action_entities,
                    context=working_context,
                )
                if command is not None:
                    daily_commands.append(_bind_report_admission(
                        command, action=action,
                        admission_ticket=admission_ticket_payload,
                        admission_required=admission_required,
                    ))
                    working_context = _advance_daily_context(working_context)
                if block is not None:
                    blocked.append(block)
                continue
            blocked.append(PlanningBlock(action.action_id, "unsupported_cognitive_action", action.action_type))
        return CognitiveCommandPlan(
            decision_id=decision_id,
            daily_commands=tuple(daily_commands),
            business_commands=tuple(business_commands),
            report_commands=tuple(report_commands),
            blocked_actions=tuple(blocked),
        )

    def _plan_periodic_report(
        self,
        *,
        decision_id: UUID,
        action: RequiredAction,
        entities: tuple[ConversationEntity, ...],
        context: CommandPlanningContext,
    ) -> tuple[TypedPeriodicReportCommand | None, PlanningBlock | None]:
        snapshot = context.periodic_snapshot
        if snapshot is None:
            return None, PlanningBlock(action.action_id, "periodic_report_snapshot_required")
        if len(entities) != 1:
            return None, PlanningBlock(action.action_id, "periodic_report_entity_required")
        if context.user_constraints.read_only and action.action_type != "query_periodic_report":
            return None, PlanningBlock(action.action_id, "user_constraint_blocks_report_write")
        entity = entities[0]
        command_type: str
        patch: dict[str, Any] = {}
        targets: tuple[str, ...] = ()
        if action.action_type == "capture_report_event":
            command_type = "append_item"
            patch = {
                "field": str(entity.attributes.get("field") or ""),
                "value": entity.value,
            }
        elif action.action_type == "query_periodic_report":
            command_type = "query_report"
        elif action.action_type == "submit_periodic_report":
            if context.user_constraints.draft_only:
                return None, PlanningBlock(action.action_id, "user_constraint_blocks_report_submit")
            command_type = "submit_report"
        else:
            raw_targets = entity.attributes.get("target_item_ids")
            targets = tuple(
                str(value).strip() for value in raw_targets if str(value).strip()
            ) if isinstance(raw_targets, (list, tuple)) else ()
            command_type = (
                "edit_item" if action.action_type == "edit_periodic_report_item" else "delete_item"
            )
            if command_type == "edit_item":
                patch["replacement"] = str(entity.attributes.get("replacement") or "").strip()
        sub_decision_id = _sub_decision_id(decision_id, action.action_id)
        command_id = uuid5(NAMESPACE_URL, f"agent2-report-command-v1:{sub_decision_id}:{command_type}")
        key_payload = json.dumps(
            {
                "message_id": context.message_id,
                "command_type": command_type,
                "report_id": str(snapshot.report_id),
                "targets": targets,
                "patch": patch,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return TypedPeriodicReportCommand(
            command_id=command_id,
            decision_id=decision_id,
            sub_decision_id=sub_decision_id,
            command_type=command_type,
            report_type=snapshot.report_type,
            period_key=snapshot.period_key,
            report_id=snapshot.report_id,
            report_version=snapshot.version,
            target_item_ids=targets,
            patch=patch,
            idempotency_key=hashlib.sha256(key_payload.encode("utf-8")).hexdigest(),
        ), None

    def _plan_daily_append(
        self,
        *,
        decision_id: UUID,
        action: RequiredAction,
        entities: tuple[ConversationEntity, ...],
        context: CommandPlanningContext,
    ) -> tuple[TypedDailyCommand | None, PlanningBlock | None]:
        if context.user_constraints.read_only or context.user_constraints.no_daily_write:
            return None, PlanningBlock(action.action_id, "user_constraint_blocks_daily_write")
        snapshot = context.daily_snapshot
        if snapshot is None:
            return None, PlanningBlock(action.action_id, "daily_snapshot_required")
        if _historical_write_blocked(context, snapshot):
            return None, PlanningBlock(action.action_id, "historical_daily_mutation_blocked_after_cutoff")
        if len(entities) != 1 or entities[0].entity_type != "daily_event":
            return None, PlanningBlock(action.action_id, "daily_event_entity_required")
        entity = entities[0]
        field_name = str(entity.attributes.get("field") or "").strip()
        if field_name not in REPORT_FIELDS:
            return None, PlanningBlock(action.action_id, "daily_field_required")
        sub_decision_id = _sub_decision_id(decision_id, action.action_id)
        command_id = uuid5(NAMESPACE_URL, f"agent2-command-v3:{sub_decision_id}:append_item")
        return (
            TypedDailyCommand(
                command_id=command_id,
                decision_id=decision_id,
                sub_decision_id=sub_decision_id,
                command_type="append_item",
                report_id=snapshot.report_id,
                report_version=snapshot.version,
                target_item_ids=(),
                patch={"field": field_name, "items": [entity.value]},
                idempotency_key=_daily_idempotency_key(
                    context.message_id,
                    "append_item",
                    snapshot.report_id,
                    (),
                    {"field": field_name, "items": [entity.value]},
                ),
            ),
            None,
        )

    def _plan_daily_submit(
        self,
        *,
        decision_id: UUID,
        action: RequiredAction,
        context: CommandPlanningContext,
    ) -> tuple[TypedDailyCommand | None, PlanningBlock | None]:
        constraints = context.user_constraints
        if constraints.read_only or constraints.no_daily_write or constraints.draft_only:
            return None, PlanningBlock(action.action_id, "user_constraint_blocks_daily_submit")
        snapshot = context.daily_snapshot
        if snapshot is None:
            return None, PlanningBlock(action.action_id, "daily_snapshot_required")
        if _historical_write_blocked(context, snapshot):
            return None, PlanningBlock(action.action_id, "historical_daily_mutation_blocked_after_cutoff")
        sub_decision_id = _sub_decision_id(decision_id, action.action_id)
        command_id = uuid5(NAMESPACE_URL, f"agent2-command-v3:{sub_decision_id}:submit_report")
        return (
            TypedDailyCommand(
                command_id=command_id,
                decision_id=decision_id,
                sub_decision_id=sub_decision_id,
                command_type="submit_report",
                report_id=snapshot.report_id,
                report_version=snapshot.version,
                target_item_ids=(),
                patch={},
                idempotency_key=_daily_idempotency_key(
                    context.message_id,
                    "submit_report",
                    snapshot.report_id,
                    (),
                    {},
                ),
            ),
            None,
        )

    def _plan_daily_section_replace(
        self,
        *,
        decision_id: UUID,
        action: RequiredAction,
        entities: tuple[ConversationEntity, ...],
        context: CommandPlanningContext,
    ) -> tuple[TypedDailyCommand | None, PlanningBlock | None]:
        if context.user_constraints.read_only or context.user_constraints.no_daily_write:
            return None, PlanningBlock(action.action_id, "user_constraint_blocks_daily_write")
        snapshot = context.daily_snapshot
        if snapshot is None:
            return None, PlanningBlock(action.action_id, "daily_snapshot_required")
        if _historical_write_blocked(context, snapshot):
            return None, PlanningBlock(action.action_id, "historical_daily_mutation_blocked_after_cutoff")
        if len(entities) != 1 or entities[0].entity_type != "daily_report":
            return None, PlanningBlock(action.action_id, "daily_report_entity_required")
        attributes = entities[0].attributes
        field_name = str(attributes.get("field") or "").strip()
        raw_items = attributes.get("items")
        items = [str(value).strip() for value in raw_items] if isinstance(raw_items, (list, tuple)) else []
        if field_name not in REPORT_FIELDS or not items or any(not value for value in items):
            return None, PlanningBlock(action.action_id, "daily_section_values_required")
        report_id = str(attributes.get("report_id") or "").strip()
        raw_version = attributes.get("version")
        if report_id and report_id != str(snapshot.report_id):
            return None, PlanningBlock(action.action_id, "target_not_found")
        if raw_version is not None and raw_version > snapshot.version:
            return None, PlanningBlock(action.action_id, "version_conflict")
        patch = {"field": field_name, "items": items}
        sub_decision_id = _sub_decision_id(decision_id, action.action_id)
        command_id = uuid5(
            NAMESPACE_URL,
            f"agent2-command-v3:{sub_decision_id}:replace_section",
        )
        return (
            TypedDailyCommand(
                command_id=command_id,
                decision_id=decision_id,
                sub_decision_id=sub_decision_id,
                command_type="replace_section",
                report_id=snapshot.report_id,
                report_version=snapshot.version,
                target_item_ids=(),
                patch=patch,
                idempotency_key=_daily_idempotency_key(
                    context.message_id,
                    "replace_section",
                    snapshot.report_id,
                    (),
                    patch,
                ),
            ),
            None,
        )

    def _plan_daily_item_mutation(
        self,
        *,
        decision_id: UUID,
        action: RequiredAction,
        entities: tuple[ConversationEntity, ...],
        context: CommandPlanningContext,
        command_type: Literal["edit_item", "delete_item", "merge_items", "move_items"],
    ) -> tuple[TypedDailyCommand | None, PlanningBlock | None]:
        if context.user_constraints.read_only or context.user_constraints.no_daily_write:
            return None, PlanningBlock(action.action_id, "user_constraint_blocks_daily_write")
        snapshot = context.daily_snapshot
        if snapshot is None:
            return None, PlanningBlock(action.action_id, "daily_snapshot_required")
        if _historical_write_blocked(context, snapshot):
            return None, PlanningBlock(action.action_id, "historical_daily_mutation_blocked_after_cutoff")
        if len(entities) != 1 or entities[0].entity_type != "daily_item_target":
            return None, PlanningBlock(action.action_id, "daily_item_target_required")
        raw_target_ids = entities[0].attributes.get("target_item_ids")
        target_ids = tuple(
            str(value).strip()
            for value in raw_target_ids
            if str(value).strip()
        ) if isinstance(raw_target_ids, (list, tuple)) else ()
        expected_count = 2 if command_type == "merge_items" else 1
        if (command_type == "merge_items" and len(target_ids) < expected_count) or (
            command_type != "merge_items" and len(target_ids) != expected_count
        ):
            return None, PlanningBlock(action.action_id, "ambiguous_target")
        known_item_ids = {
            item_id
            for values in snapshot.item_ids.values()
            for item_id in values
        }
        if any(item_id not in known_item_ids for item_id in target_ids):
            return None, PlanningBlock(action.action_id, "target_not_found")
        patch: dict[str, Any] = {}
        if command_type == "move_items":
            target_field = str(entities[0].attributes.get("target_field") or "").strip()
            if target_field not in REPORT_FIELDS:
                return None, PlanningBlock(action.action_id, "daily_field_required")
            patch["target_field"] = target_field
        elif command_type in {"edit_item", "merge_items"}:
            replacement = str(entities[0].attributes.get("replacement") or "").strip()
            if command_type == "edit_item" and not replacement:
                return None, PlanningBlock(action.action_id, "replacement_required")
            if replacement:
                patch["replacement"] = replacement
        sub_decision_id = _sub_decision_id(decision_id, action.action_id)
        command_id = uuid5(NAMESPACE_URL, f"agent2-command-v3:{sub_decision_id}:{command_type}")
        return (
            TypedDailyCommand(
                command_id=command_id,
                decision_id=decision_id,
                sub_decision_id=sub_decision_id,
                command_type=command_type,
                report_id=snapshot.report_id,
                report_version=snapshot.version,
                target_item_ids=target_ids,
                patch=patch,
                idempotency_key=_daily_idempotency_key(
                    context.message_id,
                    command_type,
                    snapshot.report_id,
                    target_ids,
                    patch,
                ),
            ),
            None,
        )

    def _plan_daily_query(
        self,
        *,
        decision_id: UUID,
        action: RequiredAction,
        entities: tuple[ConversationEntity, ...],
        context: CommandPlanningContext,
    ) -> tuple[TypedDailyCommand | None, PlanningBlock | None]:
        reference = _resolve_daily_reference(entities, context)
        if reference is None:
            return None, PlanningBlock(action.action_id, "daily_snapshot_required")
        snapshot = reference.snapshot
        sub_decision_id = _sub_decision_id(decision_id, action.action_id)
        command_id = uuid5(NAMESPACE_URL, f"agent2-command-v3:{sub_decision_id}:query_report")
        patch = {"report_date": reference.report_date.isoformat()}
        return (
            TypedDailyCommand(
                command_id=command_id,
                decision_id=decision_id,
                sub_decision_id=sub_decision_id,
                command_type="query_report",
                report_id=snapshot.report_id,
                report_version=snapshot.version,
                target_item_ids=(),
                patch=patch,
                idempotency_key=_daily_idempotency_key(
                    context.message_id,
                    "query_report",
                    snapshot.report_id,
                    (),
                    patch,
                ),
            ),
            None,
        )

    def _plan_daily_copy(
        self,
        *,
        decision_id: UUID,
        action: RequiredAction,
        entities: tuple[ConversationEntity, ...],
        context: CommandPlanningContext,
    ) -> tuple[TypedDailyCommand | None, PlanningBlock | None]:
        if context.user_constraints.read_only or context.user_constraints.no_daily_write:
            return None, PlanningBlock(action.action_id, "user_constraint_blocks_daily_write")
        target = context.daily_snapshot
        if target is None:
            return None, PlanningBlock(action.action_id, "daily_snapshot_required")
        if _historical_write_blocked(context, target):
            return None, PlanningBlock(action.action_id, "historical_daily_mutation_blocked_after_cutoff")
        if target.status not in DAILY_DRAFT_MUTABLE_STATUSES:
            return None, PlanningBlock(action.action_id, "invalid_report_state")
        source = _resolve_daily_reference(entities, context)
        if source is None or source.snapshot.report_id == target.report_id:
            return None, PlanningBlock(action.action_id, "daily_source_snapshot_required")
        sections = {
            field_name: list(getattr(source.snapshot, field_name))
            for field_name in REPORT_FIELDS
        }
        if not any(sections.values()):
            return None, PlanningBlock(action.action_id, "daily_source_report_empty")
        patch = {
            "sections": sections,
            "source_report_date": source.report_date.isoformat(),
            "source_report_id": str(source.snapshot.report_id),
        }
        sub_decision_id = _sub_decision_id(decision_id, action.action_id)
        command_id = uuid5(NAMESPACE_URL, f"agent2-command-v3:{sub_decision_id}:copy_report")
        return (
            TypedDailyCommand(
                command_id=command_id,
                decision_id=decision_id,
                sub_decision_id=sub_decision_id,
                command_type="copy_report",
                report_id=target.report_id,
                report_version=target.version,
                target_item_ids=(),
                patch=patch,
                idempotency_key=_daily_idempotency_key(
                    context.message_id,
                    "copy_report",
                    target.report_id,
                    (),
                    patch,
                ),
            ),
            None,
        )

    def _plan_daily_clear(
        self,
        *,
        decision_id: UUID,
        action: RequiredAction,
        entities: tuple[ConversationEntity, ...],
        context: CommandPlanningContext,
    ) -> tuple[TypedDailyCommand | None, PlanningBlock | None]:
        if context.user_constraints.read_only or context.user_constraints.no_daily_write:
            return None, PlanningBlock(action.action_id, "user_constraint_blocks_daily_write")
        snapshot = context.daily_snapshot
        if snapshot is None:
            return None, PlanningBlock(action.action_id, "daily_snapshot_required")
        if _historical_write_blocked(context, snapshot):
            return None, PlanningBlock(action.action_id, "historical_daily_mutation_blocked_after_cutoff")
        if snapshot.status not in DAILY_DRAFT_MUTABLE_STATUSES:
            return None, PlanningBlock(action.action_id, "invalid_report_state")
        if not str(action.parameters.get("confirmed_pending_id") or "").strip():
            return None, PlanningBlock(action.action_id, "high_impact_confirmation_required")
        if not _entity_targets_snapshot(entities, snapshot):
            return None, PlanningBlock(action.action_id, "daily_report_target_mismatch")
        patch = {"field": "all"}
        sub_decision_id = _sub_decision_id(decision_id, action.action_id)
        command_id = uuid5(NAMESPACE_URL, f"agent2-command-v3:{sub_decision_id}:clear_report")
        return (
            TypedDailyCommand(
                command_id=command_id,
                decision_id=decision_id,
                sub_decision_id=sub_decision_id,
                command_type="clear_report",
                report_id=snapshot.report_id,
                report_version=snapshot.version,
                target_item_ids=(),
                patch=patch,
                idempotency_key=_daily_idempotency_key(
                    context.message_id,
                    "clear_report",
                    snapshot.report_id,
                    (),
                    patch,
                ),
            ),
            None,
        )

    def _plan_daily_reopen(
        self,
        *,
        decision_id: UUID,
        action: RequiredAction,
        entities: tuple[ConversationEntity, ...],
        context: CommandPlanningContext,
    ) -> tuple[TypedDailyCommand | None, PlanningBlock | None]:
        if context.user_constraints.read_only or context.user_constraints.no_daily_write:
            return None, PlanningBlock(action.action_id, "user_constraint_blocks_daily_write")
        reference = _resolve_daily_reference(entities, context)
        if reference is None:
            return None, PlanningBlock(action.action_id, "daily_snapshot_required")
        snapshot = reference.snapshot
        if _historical_write_blocked(context, snapshot):
            return None, PlanningBlock(action.action_id, "historical_daily_mutation_blocked_after_cutoff")
        if snapshot.status != "completed":
            return None, PlanningBlock(action.action_id, "invalid_report_state")
        patch: dict[str, Any] = {"report_date": reference.report_date.isoformat()}
        sub_decision_id = _sub_decision_id(decision_id, action.action_id)
        command_id = uuid5(NAMESPACE_URL, f"agent2-command-v3:{sub_decision_id}:reopen_report")
        return (
            TypedDailyCommand(
                command_id=command_id,
                decision_id=decision_id,
                sub_decision_id=sub_decision_id,
                command_type="reopen_report",
                report_id=snapshot.report_id,
                report_version=snapshot.version,
                target_item_ids=(),
                patch=patch,
                idempotency_key=_daily_idempotency_key(
                    context.message_id,
                    "reopen_report",
                    snapshot.report_id,
                    (),
                    patch,
                ),
            ),
            None,
        )

    def _plan_daily_section_clear(
        self,
        *,
        decision_id: UUID,
        action: RequiredAction,
        entities: tuple[ConversationEntity, ...],
        context: CommandPlanningContext,
    ) -> tuple[TypedDailyCommand | None, PlanningBlock | None]:
        if context.user_constraints.read_only or context.user_constraints.no_daily_write:
            return None, PlanningBlock(action.action_id, "user_constraint_blocks_daily_write")
        snapshot = context.daily_snapshot
        if snapshot is None:
            return None, PlanningBlock(action.action_id, "daily_snapshot_required")
        if _historical_write_blocked(context, snapshot):
            return None, PlanningBlock(action.action_id, "historical_daily_mutation_blocked_after_cutoff")
        if snapshot.status not in DAILY_DRAFT_MUTABLE_STATUSES:
            return None, PlanningBlock(action.action_id, "invalid_report_state")
        if not _entity_targets_snapshot(entities, snapshot):
            return None, PlanningBlock(action.action_id, "daily_report_target_mismatch")
        field_name = str(entities[0].attributes.get("field") or "").strip()
        if field_name not in REPORT_FIELDS:
            return None, PlanningBlock(action.action_id, "daily_field_required")
        patch = {"field": field_name}
        sub_decision_id = _sub_decision_id(decision_id, action.action_id)
        command_id = uuid5(NAMESPACE_URL, f"agent2-command-v3:{sub_decision_id}:clear_report")
        return (
            TypedDailyCommand(
                command_id=command_id,
                decision_id=decision_id,
                sub_decision_id=sub_decision_id,
                command_type="clear_report",
                report_id=snapshot.report_id,
                report_version=snapshot.version,
                target_item_ids=(),
                patch=patch,
                idempotency_key=_daily_idempotency_key(
                    context.message_id,
                    "clear_report",
                    snapshot.report_id,
                    (),
                    patch,
                ),
            ),
            None,
        )

    def _plan_daily_projection(
        self,
        *,
        decision_id: UUID,
        action: RequiredAction,
        entities: tuple[ConversationEntity, ...],
        context: CommandPlanningContext,
    ) -> tuple[TypedDailyCommand | None, PlanningBlock | None]:
        if context.user_constraints.read_only or context.user_constraints.no_daily_write:
            return None, PlanningBlock(action.action_id, "user_constraint_blocks_daily_write")
        target = context.daily_snapshot
        if target is None:
            return None, PlanningBlock(action.action_id, "daily_snapshot_required")
        if _historical_write_blocked(context, target):
            return None, PlanningBlock(action.action_id, "historical_daily_mutation_blocked_after_cutoff")
        if target.status not in DAILY_DRAFT_MUTABLE_STATUSES:
            return None, PlanningBlock(action.action_id, "invalid_report_state")
        if action.action_type == "copy_current_work_to_tomorrow":
            if not _entity_targets_snapshot(entities, target):
                return None, PlanningBlock(action.action_id, "daily_report_target_mismatch")
            source = next(
                (
                    reference
                    for reference in context.daily_history
                    if reference.snapshot.report_id == target.report_id
                ),
                None,
            )
            source_values = tuple(
                _project_current_work_to_tomorrow(value)
                for value in target.today_work
            )
            target_field = "tomorrow_plan"
        else:
            source = _resolve_daily_reference(entities, context)
            source_values = source.snapshot.tomorrow_plan if source is not None else ()
            target_field = "today_work"
        if source is None:
            return None, PlanningBlock(action.action_id, "daily_source_snapshot_required")
        if not source_values:
            return None, PlanningBlock(action.action_id, "daily_source_report_empty")
        patch = {
            "sections": {target_field: list(source_values)},
            "source_report_date": source.report_date.isoformat(),
            "source_report_id": str(source.snapshot.report_id),
        }
        sub_decision_id = _sub_decision_id(decision_id, action.action_id)
        command_id = uuid5(NAMESPACE_URL, f"agent2-command-v3:{sub_decision_id}:copy_report")
        return (
            TypedDailyCommand(
                command_id=command_id,
                decision_id=decision_id,
                sub_decision_id=sub_decision_id,
                command_type="copy_report",
                report_id=target.report_id,
                report_version=target.version,
                target_item_ids=(),
                patch=patch,
                idempotency_key=_daily_idempotency_key(
                    context.message_id,
                    "copy_report",
                    target.report_id,
                    (),
                    patch,
                ),
            ),
            None,
        )

    def _business_command(
        self,
        *,
        decision: CognitiveDecisionV3,
        decision_id: UUID,
        action: RequiredAction,
        entities: tuple[ConversationEntity, ...],
        command_type: str,
        target_system: str,
        execution_mode: BusinessExecutionMode,
        message_id: str,
    ) -> TypedBusinessCommand:
        sub_decision_id = _sub_decision_id(decision_id, action.action_id)
        command_id = uuid5(NAMESPACE_URL, f"agent2-command-v3:{sub_decision_id}:{command_type}")
        source_segments = [
            segment
            for segment in decision.segments
            if action.action_id in segment.action_ids
        ]
        admission_ticket = next(
            (
                ticket
                for ticket in decision.admission_tickets
                if ticket.action_id == action.action_id
            ),
            None,
        )
        admission_required = (
            decision.admission_mode == "enforced"
            and _action_requires_admission_ticket(action.action_type)
        )
        return TypedBusinessCommand(
            command_id=command_id,
            decision_id=decision_id,
            sub_decision_id=sub_decision_id,
            command_type=command_type,
            target_system=target_system,
            entity_ids=tuple(entity.entity_id for entity in entities),
            payload={
                "entities": [
                    {
                        "entity_id": entity.entity_id,
                        "entity_type": entity.entity_type,
                        "value": entity.value,
                        "confidence": entity.confidence,
                        "attributes": dict(entity.attributes),
                    }
                    for entity in entities
                ],
                "parameters": dict(action.parameters),
                "source_segments": [
                    {
                        "segment_id": segment.segment_id,
                        "text": segment.text,
                        "text_hash": segment.text_hash,
                        "start_offset": segment.start_offset,
                        "end_offset": segment.end_offset,
                    }
                    for segment in source_segments
                ],
            },
            execution_mode=execution_mode,
            idempotency_key=_idempotency_key(message_id, action.action_id, command_type),
            admission_ticket=(
                admission_ticket.as_dict() if admission_ticket is not None else {}
            ),
            admission_required=admission_required,
            admission_action_id=action.action_id if admission_required else "",
            admission_operation=action.action_type if admission_required else "",
        )


def _classify_case_query(
    *,
    entities: tuple[ConversationEntity, ...],
) -> tuple[str, str]:
    """Classify a structured case query without selecting any case identifier.

    Inventory queries have no case reference and ask for the caller's case
    collection.  Everything else remains on the existing case-knowledge path.
    The executor is responsible for applying tenant and actor permissions.
    """

    case_queries = tuple(
        entity for entity in entities if entity.entity_type == "case_query"
    )
    if len(case_queries) != 1:
        return "query_case_risk", "case_knowledge"

    query = case_queries[0]
    matter_hint = str(query.attributes.get("matter_hint") or "").strip()
    if matter_hint and not is_self_scoped_case_inventory_hint(matter_hint):
        return "query_case_risk", "case_knowledge"

    question = " ".join(
        part
        for part in (
            str(query.attributes.get("question") or "").strip(),
            str(query.value or "").strip(),
        )
        if part
    )
    collection_scope = re.search(
        r"(?:列出|列一下|列一列|查看|查询|展示|显示|看看).*(?:所有|全部|案件|案子)"
        r"|(?:所有|全部|哪些|什么).*(?:案件|案子)"
        r"|(?:案件|案子).*(?:列出|列一下|列一列|清单|列表)"
        r"|(?:案件|案子)(?:有哪些|有什么)[？?]?$",
        question,
    )
    if collection_scope is not None:
        return "list_assigned_cases", "case_inventory"
    return "query_case_risk", "case_knowledge"


def is_self_scoped_case_inventory_hint(value: object) -> bool:
    """Accept only caller-scope labels, never a named party or matter target."""

    compact = re.sub(r"[\s，。！？、,.!?]", "", str(value or ""))
    return bool(
        re.fullmatch(
            r"(?:我|本人)(?:都)?(?:所)?(?:手上|手头|名下|负责|经办|承办)?"
            r"的?(?:有哪些|有什么|所有|全部)?(?:案件|案子)?",
            compact,
        )
    )


def _project_current_work_to_tomorrow(value: str) -> str:
    """Convert a referenced completed-work phrase into a future continuation plan.

    Only temporal/aspect markers are normalized. Names, amounts, places and the
    business object remain byte-for-byte within the retained phrase.
    """

    original = str(value or "").strip()
    if not original or not re.search(r"[\u4e00-\u9fff]", original):
        return original
    clean = re.sub(r"^(?:今天|今日)(?:上午|下午|晚上)?[，,:：\s]*", "", original).strip()
    if clean.startswith("继续"):
        return clean
    completed = re.match(r"^(?:已经|已)?完成(?:了)?(.+)$", clean)
    if completed:
        return f"继续推进{completed.group(1).strip()}"
    clean = re.sub(r"^(?:已经|已)?开始", "", clean).strip()
    clean = re.sub(
        r"^(优化|处理|办理|推进|搭建|整理|联系|沟通|提交|审核|核对|准备|跟进|协调|参加)了(?=.+)",
        r"\1",
        clean,
    )
    return f"继续{clean}" if clean else original


def _resolve_daily_reference(
    entities: tuple[ConversationEntity, ...],
    context: CommandPlanningContext,
) -> DailySnapshotReference | None:
    if len(entities) != 1 or entities[0].entity_type != "daily_report":
        return None
    entity = entities[0]
    raw_report_id = str(entity.attributes.get("report_id") or "").strip()
    raw_report_date = str(entity.attributes.get("report_date") or "").strip()
    raw_version = entity.attributes.get("version")
    matches: list[DailySnapshotReference] = []
    for reference in context.daily_history:
        snapshot = reference.snapshot
        if raw_report_id and str(snapshot.report_id) != raw_report_id:
            continue
        if raw_report_date and reference.report_date.isoformat() != raw_report_date:
            continue
        if raw_version is not None and snapshot.version != raw_version:
            continue
        matches.append(reference)
    if len(matches) == 1:
        return matches[0]
    if not raw_report_id and not raw_report_date and context.daily_snapshot is not None:
        current_matches = [
            reference
            for reference in context.daily_history
            if reference.snapshot.report_id == context.daily_snapshot.report_id
        ]
        if len(current_matches) == 1:
            return current_matches[0]
    return None


def _historical_write_blocked(
    context: CommandPlanningContext,
    snapshot: DailyReportMutationSnapshot,
) -> bool:
    current_date = context.current_report_date
    if current_date is None:
        return False
    matching_dates = {
        reference.report_date
        for reference in context.daily_history
        if reference.snapshot.report_id == snapshot.report_id
    }
    if not matching_dates or matching_dates == {current_date}:
        return False
    return context.user_constraints.no_history_mutation or not context.historical_mutation_allowed


_READ_ONLY_COGNITIVE_ACTIONS = frozenset(
    {
        "answer_case_query",
        "query_case_progress",
        "query_daily_report",
        "query_operation_status",
        "query_periodic_report",
        "search_enterprise_knowledge",
    }
)


def _action_requires_admission_ticket(action_type: str) -> bool:
    """Fail closed for writes while keeping proven read-only actions ticket-free."""

    return action_type not in _READ_ONLY_COGNITIVE_ACTIONS


def _bind_report_admission(
    command: TypedDailyCommand | TypedPeriodicReportCommand,
    *,
    action: RequiredAction,
    admission_ticket: dict[str, Any],
    admission_required: bool,
) -> TypedDailyCommand | TypedPeriodicReportCommand:
    if not admission_required:
        return command
    return replace(
        command,
        admission_ticket=admission_ticket,
        admission_required=True,
        admission_action_id=action.action_id,
        admission_operation=action.action_type,
    )


def _entity_targets_snapshot(
    entities: tuple[ConversationEntity, ...],
    snapshot: DailyReportMutationSnapshot,
) -> bool:
    if len(entities) != 1 or entities[0].entity_type != "daily_report":
        return False
    attributes = entities[0].attributes
    report_id = str(attributes.get("report_id") or "").strip()
    version = attributes.get("version")
    if report_id and report_id != str(snapshot.report_id):
        return False
    if version is not None and version != snapshot.version:
        return False
    return bool(report_id or version is not None)


def _sub_decision_id(decision_id: UUID, action_id: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"agent2-sub-decision-v3:{decision_id}:{action_id}")


def _idempotency_key(message_id: str, action_id: str, command_type: str) -> str:
    material = f"{message_id}:{action_id}:{command_type}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _daily_idempotency_key(
    message_id: str,
    command_type: str,
    report_id: UUID,
    target_item_ids: tuple[str, ...],
    patch: dict[str, Any],
) -> str:
    """Key replay safety to stable semantics, never an LLM-generated action ID."""

    material = json.dumps(
        {
            "message_id": message_id,
            "command_type": command_type,
            "report_id": str(report_id),
            "target_item_ids": list(target_item_ids),
            "patch": patch,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"{message_id}:daily:{digest}"


def _advance_daily_context(
    context: CommandPlanningContext,
    command: TypedDailyCommand | None = None,
) -> CommandPlanningContext:
    snapshot = context.daily_snapshot
    if snapshot is None:
        return context
    if command is not None:
        simulated = execute_typed_daily_command(
            replace(
                command,
                admission_ticket={},
                admission_required=False,
                admission_action_id="",
                admission_operation="",
            ),
            snapshot=snapshot,
            actor_user_id=context.actor_user_id,
        )
        return replace(context, daily_snapshot=simulated.after)
    return replace(context, daily_snapshot=replace(snapshot, version=snapshot.version + 1))
