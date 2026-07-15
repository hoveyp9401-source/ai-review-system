from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from app.agent2.command_planner_v3 import CognitiveCommandPlan, CognitiveCommandPlanner
from app.agent2.cognitive_core_v3 import (
    CognitiveCoreV3,
    CognitiveDecisionV3,
    SemanticInputLimitExceeded,
)

from .context import ContextAssembler
from .contracts import (
    DomainExecutionResult,
    RuntimeAuditRecord,
    RuntimeAuditSink,
    RuntimeFailureAuditRecord,
    RuntimeFailureOutcome,
    RuntimeMode,
    RuntimeOutcome,
    RuntimeReply,
    RuntimeTraceEvent,
    RuntimeTurnOutcome,
    RuntimeTurnRequest,
)
from .domains import DomainExecutionContext, DomainPackRegistry


_FORBIDDEN_COGNITIVE_KEYS = frozenset(
    {
        "allow_write",
        "should_write_db",
        "effects",
        "commands",
        "database_operation",
        "db_action",
        "sql",
        "tool_name",
        "executor",
    }
)
_ENTITY_ATTRIBUTE_KEYS = {
    "daily_event": frozenset({"field", "context_reference"}),
    "daily_item_target": frozenset({"target_item_ids", "replacement", "context_reference"}),
    "case_query": frozenset({"matter_hint", "question", "context_reference"}),
    "travel_event": frozenset({"destination", "date_hint", "purpose", "context_reference"}),
    "travel_collaboration_ref": frozenset({"candidate_id", "response", "context_reference"}),
    "daily_report": frozenset(
        {"report_id", "version", "report_date", "field", "context_reference"}
    ),
    "case_ref": frozenset({"stage", "context_reference"}),
    "case_progress_ref": frozenset(
        {
            "case_hint",
            "progress_id",
            "expected_version",
            "replacement_summary",
            "replacement_details",
            "delete_reason",
            "start_at",
            "end_at",
            "related_party_ids",
            "related_document_ids",
            "related_travel_intent_ids",
            "context_reference",
        }
    ),
    "knowledge_query": frozenset({"query", "topic", "context_reference"}),
}
_ACTION_PARAMETER_KEYS = {
    "capture_daily_event": frozenset({"confirmed_pending_id"}),
    "edit_daily_item": frozenset({"confirmed_pending_id"}),
    "delete_daily_item": frozenset({"confirmed_pending_id"}),
    "merge_daily_items": frozenset({"confirmed_pending_id"}),
    "query_daily_report": frozenset({"confirmed_pending_id"}),
    "copy_previous_daily_report": frozenset({"confirmed_pending_id"}),
    "clear_daily_section": frozenset({"confirmed_pending_id"}),
    "clear_daily_report": frozenset({"confirmed_pending_id"}),
    "reopen_daily_report": frozenset({"confirmed_pending_id"}),
    "copy_current_work_to_tomorrow": frozenset({"confirmed_pending_id"}),
    "complete_previous_daily_plan": frozenset({"confirmed_pending_id"}),
    "record_case_progress": frozenset({"confirmed_pending_id"}),
    "update_case_progress": frozenset({"confirmed_pending_id"}),
    "delete_case_progress": frozenset({"confirmed_pending_id"}),
    "query_case_progress": frozenset({"confirmed_pending_id"}),
    "link_case_progress": frozenset({"confirmed_pending_id"}),
    "submit_daily_report": frozenset({"confirmed_pending_id"}),
    "answer_case_query": frozenset({"confirmed_pending_id"}),
    "record_travel_event": frozenset({"confirmed_pending_id"}),
    "respond_travel_collaboration": frozenset({"confirmed_pending_id"}),
    "search_enterprise_knowledge": frozenset({"confirmed_pending_id"}),
}
_CONTEXT_REFERENCE_KEYS = frozenset({"intent", "context_id", "selection", "value_source"})
_STRING_ENTITY_ATTRIBUTES = {
    "daily_event": frozenset({"field"}),
    "daily_item_target": frozenset({"replacement"}),
    "case_query": frozenset({"matter_hint", "question"}),
    "travel_event": frozenset({"destination", "date_hint", "purpose"}),
    "travel_collaboration_ref": frozenset({"candidate_id", "response"}),
    "daily_report": frozenset({"report_id", "report_date", "field"}),
    "case_ref": frozenset({"stage"}),
    "case_progress_ref": frozenset(
        {
            "case_hint",
            "progress_id",
            "replacement_summary",
            "replacement_details",
            "delete_reason",
            "start_at",
            "end_at",
        }
    ),
    "knowledge_query": frozenset({"query", "topic"}),
}


class RuntimeInvariantViolation(ValueError):
    pass


class Agent2RuntimeHarness:
    """Phase-1 deep module for a complete Shadow/Replay cognitive turn.

    Phase 1 intentionally refuses Live mode. A composition root may inject
    replay or shadow adapters, but request text cannot select the mode.
    """

    def __init__(
        self,
        *,
        mode: RuntimeMode,
        core: CognitiveCoreV3,
        planner: CognitiveCommandPlanner,
        context_assembler: ContextAssembler,
        domains: DomainPackRegistry,
        audit_sink: RuntimeAuditSink | None = None,
    ) -> None:
        if mode not in {"shadow", "replay"}:
            raise ValueError("Agent2 Runtime Harness Phase 1 supports shadow/replay only")
        if mode == "replay" and context_assembler.checkpoint_scope != "ephemeral":
            raise ValueError("Replay Runtime requires an isolated ephemeral Conversation State store")
        self._mode = mode
        self._core = core
        self._planner = planner
        self._context_assembler = context_assembler
        self._domains = domains
        self._audit_sink = audit_sink

    async def handle(self, request: RuntimeTurnRequest) -> RuntimeOutcome:
        run_id = str(
            uuid5(
                NAMESPACE_URL,
                f"agent2-runtime-v0:{request.tenant_id}:{request.actor.actor_id}:"
                f"{request.conversation_id}:{request.message_id}",
            )
        )
        trace: list[RuntimeTraceEvent] = []
        context = None
        core_result = None
        command_plan = None
        domain_results: tuple[DomainExecutionResult, ...] = ()
        checkpointed_state = None
        stage = "context_assembly"
        try:
            context = await self._context_assembler.assemble(request)
            checkpointed_state = context.conversation_state
            _append_trace(
                trace,
                "context_assembled",
                {
                    "context_digest": context.manifest["digest"],
                    "state_version": context.conversation_state.version,
                    "active_pending_count": len(context.active_pending),
                },
            )

            stage = "cognitive_core"
            core_result = await self._core.process(context.cognitive_turn, context.conversation_state)
            _append_trace(
                trace,
                "decision_produced",
                {
                    "decision_id": core_result.decision.decision_id,
                    "intent_count": len(core_result.decision.intents),
                    "action_count": len(core_result.decision.required_actions),
                },
            )
            stage = "decision_validation"
            _validate_cognitive_decision(core_result.decision)
            _append_trace(
                trace,
                "decision_validated",
                {"contract_version": core_result.decision.contract_version},
            )

            stage = "domain_resolution"
            domain_resolution = self._domains.resolve_actions(core_result.decision.required_actions)
            _append_trace(
                trace,
                "domains_resolved",
                {
                    "action_domains": dict(domain_resolution.action_domains),
                    "unsupported_action_ids": list(domain_resolution.unsupported_action_ids),
                },
            )

            stage = "planning"
            command_plan = self._planner.plan(
                core_result.decision,
                replace(context.planning_context, user_constraints=core_result.state.user_constraints),
            )
            _append_trace(
                trace,
                "plan_created",
                {
                    "daily_command_count": len(command_plan.daily_commands),
                    "business_command_count": len(command_plan.business_commands),
                    "blocked_action_count": len(command_plan.blocked_actions),
                },
            )
            stage = "plan_validation"
            _validate_plan_partition(
                core_result.decision,
                command_plan,
                unsupported_action_ids=domain_resolution.unsupported_action_ids,
            )

            stage = "domain_execution"
            domain_results = await self._domains.execute(
                command_plan,
                DomainExecutionContext(
                    tenant_id=request.tenant_id,
                    actor_id=request.actor.actor_id,
                    conversation_id=request.conversation_id,
                    message_id=request.message_id,
                    occurred_at=request.occurred_at,
                    channel=request.channel,
                    run_id=run_id,
                    mode=self._mode,
                    source_text_hash=core_result.decision.source_text_hash,
                ),
            )
            _validate_phase1_execution(domain_results, mode=self._mode)
            for result in domain_results:
                _append_trace(
                    trace,
                    "domain_executed" if result.status != "unavailable" else "domain_unavailable",
                    {
                        "domain_id": result.domain_id,
                        "status": result.status,
                        "command_count": result.command_count,
                        "actual_write": result.actual_write,
                        "would_write": result.would_write,
                    },
                )

            stage = "state_checkpoint"
            state_may_advance = (
                not domain_resolution.unsupported_action_ids
                and not command_plan.blocked_actions
                and all(
                    result.status in {"succeeded", "simulated", "duplicate"}
                    for result in domain_results
                )
            )
            if self._mode == "replay" and state_may_advance:
                saved_state = await self._context_assembler.checkpoint_replay_state(
                    core_result.state,
                    expected_version=context.conversation_state.version,
                )
                checkpointed_state = saved_state
                _append_trace(trace, "state_saved", {"state_version": saved_state.version})
            else:
                saved_state = context.conversation_state
                checkpointed_state = saved_state
                event = "state_not_persisted" if self._mode == "shadow" else "state_retained"
                reason = "shadow_is_read_only" if self._mode == "shadow" else "command_not_successful"
                _append_trace(
                    trace,
                    event,
                    {
                        "state_version": saved_state.version,
                        "proposed_state_version": core_result.state.version,
                        "reason": reason,
                    },
                )

            stage = "reply_composition"
            reply, status = _compose_reply(
                decision=core_result.decision,
                domain_results=domain_results,
                blocked_action_count=len(command_plan.blocked_actions),
            )
            _append_trace(trace, "reply_composed", {"reply_type": reply.reply_type, "status": status})

            actual_write = any(result.actual_write for result in domain_results)
            would_write = any(result.would_write for result in domain_results)
            stage = "audit"
            if self._audit_sink is None:
                _append_trace(trace, "audit_skipped", {"reason": "no_audit_sink"})
            else:
                audit_record = RuntimeAuditRecord(
                    run_id=run_id,
                    mode=self._mode,
                    status=status,
                    request=request,
                    context_manifest=context.manifest,
                    decision=core_result.decision,
                    command_plan=command_plan,
                    domain_results=domain_results,
                    state_before_version=context.conversation_state.version,
                    state_after_version=saved_state.version,
                    actual_write=actual_write,
                    would_write=would_write,
                    reply=reply,
                    trace=tuple(trace),
                    legacy_fallback_used=False,
                )
                await self._audit_sink.record(audit_record)
                _append_trace(trace, "audit_recorded", {"run_id": run_id})

            return RuntimeTurnOutcome(
                run_id=run_id,
                mode=self._mode,
                status=status,
                reply=reply,
                decision=core_result.decision,
                command_plan=command_plan,
                domain_results=domain_results,
                actual_write=actual_write,
                would_write=would_write,
                state_version=saved_state.version,
                state=saved_state,
                trace=tuple(trace),
                legacy_fallback_used=False,
            )
        except Exception as exc:
            return await self._failed_closed(
                run_id=run_id,
                request=request,
                stage=stage,
                error=exc,
                trace=trace,
                context=context,
                core_result=core_result,
                command_plan=command_plan,
                domain_results=domain_results,
                checkpointed_state=checkpointed_state,
            )

    async def _failed_closed(
        self,
        *,
        run_id: str,
        request: RuntimeTurnRequest,
        stage: str,
        error: Exception,
        trace: list[RuntimeTraceEvent],
        context: Any,
        core_result: Any,
        command_plan: Any,
        domain_results: tuple[DomainExecutionResult, ...],
        checkpointed_state: Any,
    ) -> RuntimeFailureOutcome:
        error_code = "audit_failed" if stage == "audit" else _failure_error_code(error)
        _append_trace(
            trace,
            "failed_closed",
            {"failed_stage": stage, "error_code": error_code, "error_type": type(error).__name__},
        )
        reply = RuntimeReply("failure", "Runtime 未通过安全校验，本次没有执行业务写入。")
        state = checkpointed_state or (context.conversation_state if context is not None else None)
        decision = core_result.decision if core_result is not None else None
        actual_write = any(result.actual_write for result in domain_results)
        would_write = any(result.would_write for result in domain_results)
        if self._audit_sink is None:
            _append_trace(trace, "audit_skipped", {"reason": "no_audit_sink"})
        elif stage == "audit":
            # The normal audit write already failed. Retrying the same sink can
            # hide the original failure and incorrectly claim that this run is
            # auditable. Preserve the stable failure outcome instead.
            _append_trace(
                trace,
                "audit_failed",
                {"error_type": type(error).__name__},
            )
        else:
            failure_record = RuntimeFailureAuditRecord(
                run_id=run_id,
                mode=self._mode,
                status="failed_closed",
                request=request,
                context_manifest=context.manifest if context is not None else {},
                failed_stage=stage,
                error_code=error_code,
                decision=decision,
                command_plan=command_plan,
                domain_results=domain_results,
                state_before_version=(
                    context.conversation_state.version if context is not None else None
                ),
                state_after_version=state.version if state is not None else None,
                actual_write=actual_write,
                would_write=would_write,
                reply=reply,
                trace=tuple(trace),
                legacy_fallback_used=False,
            )
            try:
                await self._audit_sink.record(failure_record)
            except Exception as audit_error:
                _append_trace(
                    trace,
                    "audit_failed",
                    {"error_type": type(audit_error).__name__},
                )
            else:
                _append_trace(trace, "audit_recorded", {"run_id": run_id})
        return RuntimeFailureOutcome(
            run_id=run_id,
            mode=self._mode,
            status="failed_closed",
            reply=reply,
            failed_stage=stage,
            error_code=error_code,
            decision=decision,
            command_plan=command_plan,
            domain_results=domain_results,
            actual_write=actual_write,
            would_write=would_write,
            state_version=state.version if state is not None else None,
            state=state,
            trace=tuple(trace),
            legacy_fallback_used=False,
        )


def _validate_cognitive_decision(decision: CognitiveDecisionV3) -> None:
    violations: list[str] = []
    targeted_daily_entity_ids = {
        entity_id
        for action in decision.required_actions
        if action.action_type in {"edit_daily_item", "delete_daily_item", "merge_daily_items"}
        for entity_id in action.entity_ids
    }

    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                normalized = str(key).strip().lower()
                if normalized in _FORBIDDEN_COGNITIVE_KEYS:
                    violations.append(f"{path}.{key}")
                visit(nested, f"{path}.{key}")
        elif isinstance(value, (list, tuple)):
            for index, nested in enumerate(value):
                visit(nested, f"{path}[{index}]")

    visit(decision.as_dict(), "decision")
    for entity in decision.entities:
        allowed_attributes = _ENTITY_ATTRIBUTE_KEYS.get(entity.entity_type)
        if allowed_attributes is None:
            violations.append(f"decision.entities[{entity.entity_id}].entity_type")
        else:
            unknown = set(entity.attributes) - allowed_attributes
            if unknown:
                violations.extend(
                    f"decision.entities[{entity.entity_id}].attributes.{key}"
                    for key in sorted(unknown)
                )
        context_reference = entity.attributes.get("context_reference")
        if context_reference is not None and not isinstance(context_reference, dict):
            violations.append(
                f"decision.entities[{entity.entity_id}].attributes.context_reference"
            )
        elif isinstance(context_reference, dict):
            unknown_reference_keys = set(context_reference) - _CONTEXT_REFERENCE_KEYS
            if unknown_reference_keys:
                violations.extend(
                    f"decision.entities[{entity.entity_id}].attributes.context_reference.{key}"
                    for key in sorted(unknown_reference_keys)
                )
            if any(not isinstance(value, str) for value in context_reference.values()):
                violations.append(
                    f"decision.entities[{entity.entity_id}].attributes.context_reference.value_type"
                )
        for attribute_name in _STRING_ENTITY_ATTRIBUTES.get(entity.entity_type, ()):
            attribute_value = entity.attributes.get(attribute_name)
            if attribute_value is not None and not isinstance(attribute_value, str):
                violations.append(
                    f"decision.entities[{entity.entity_id}].attributes.{attribute_name}.value_type"
                )
        if entity.entity_type == "daily_report" and "version" in entity.attributes:
            version = entity.attributes.get("version")
            if not isinstance(version, int) or isinstance(version, bool) or version < 0:
                violations.append(
                    f"decision.entities[{entity.entity_id}].attributes.version.value_type"
                )
        if entity.entity_type == "case_progress_ref" and "expected_version" in entity.attributes:
            version = entity.attributes.get("expected_version")
            if not isinstance(version, int) or isinstance(version, bool) or version < 1:
                violations.append(
                    f"decision.entities[{entity.entity_id}].attributes.expected_version.value_type"
                )
        if entity.entity_type == "case_progress_ref":
            for attribute_name in (
                "related_party_ids",
                "related_document_ids",
                "related_travel_intent_ids",
            ):
                value = entity.attributes.get(attribute_name)
                if value is not None and (
                    not isinstance(value, list)
                    or any(not isinstance(item, str) for item in value)
                ):
                    violations.append(
                        f"decision.entities[{entity.entity_id}].attributes.{attribute_name}.value_type"
                    )
        if entity.entity_type == "daily_item_target":
            target_ids = entity.attributes.get("target_item_ids")
            target_required = entity.entity_id in targeted_daily_entity_ids
            if (target_required or target_ids is not None) and (
                not isinstance(target_ids, (list, tuple))
                or any(not isinstance(value, str) or not value for value in target_ids)
            ):
                violations.append(
                    f"decision.entities[{entity.entity_id}].attributes.target_item_ids"
                )
    for action in decision.required_actions:
        allowed_parameters = _ACTION_PARAMETER_KEYS.get(action.action_type)
        if allowed_parameters is None:
            violations.append(f"decision.required_actions[{action.action_id}].action_type")
        else:
            unknown = set(action.parameters) - allowed_parameters
            if unknown:
                violations.extend(
                    f"decision.required_actions[{action.action_id}].parameters.{key}"
                    for key in sorted(unknown)
                )
        confirmed_pending_id = action.parameters.get("confirmed_pending_id")
        if confirmed_pending_id is not None and (
            not isinstance(confirmed_pending_id, str)
            or not confirmed_pending_id
            or confirmed_pending_id not in decision.context_update.consumed_pending_ids
        ):
            violations.append(
                f"decision.required_actions[{action.action_id}].parameters.confirmed_pending_id"
            )
    if violations:
        raise RuntimeInvariantViolation(
            "cognitive decision violates the closed non-execution contract: "
            + ", ".join(sorted(violations))
        )


def _validate_phase1_execution(
    results: tuple[DomainExecutionResult, ...],
    *,
    mode: RuntimeMode,
) -> None:
    if mode in {"shadow", "replay"} and any(result.actual_write for result in results):
        raise RuntimeInvariantViolation(f"{mode} Runtime cannot produce an actual write receipt")


def _validate_plan_partition(
    decision: CognitiveDecisionV3,
    plan: CognitiveCommandPlan,
    *,
    unsupported_action_ids: tuple[str, ...],
) -> None:
    decision_id = decision.decision_id
    if str(plan.decision_id) != decision_id:
        raise RuntimeInvariantViolation("command plan decision_id does not match cognitive decision")
    expected = {
        str(uuid5(NAMESPACE_URL, f"agent2-sub-decision-v3:{decision_id}:{action.action_id}"))
        for action in decision.required_actions
    }
    commands = (*plan.daily_commands, *plan.business_commands)
    command_ids = [str(command.sub_decision_id) for command in commands]
    block_ids = [
        str(uuid5(NAMESPACE_URL, f"agent2-sub-decision-v3:{decision_id}:{block.action_id}"))
        for block in plan.blocked_actions
    ]
    accounted = command_ids + block_ids
    if len(accounted) != len(set(accounted)) or set(accounted) != expected:
        raise RuntimeInvariantViolation(
            "every cognitive action must produce exactly one typed command or explicit PlanningBlock"
        )
    blocked_action_ids = {block.action_id for block in plan.blocked_actions}
    if not set(unsupported_action_ids).issubset(blocked_action_ids):
        raise RuntimeInvariantViolation(
            "an unowned cognitive action was not explicitly blocked by the command planner"
        )


def _failure_error_code(error: Exception) -> str:
    if isinstance(error, SemanticInputLimitExceeded):
        return "input_limit_exceeded"
    if isinstance(error, RuntimeInvariantViolation):
        return "runtime_invariant_violation"
    if isinstance(error, ValueError):
        return "invalid_runtime_contract"
    if isinstance(error, TypeError):
        return "invalid_runtime_type"
    return "runtime_dependency_failure"


def _compose_reply(
    *,
    decision: CognitiveDecisionV3,
    domain_results: tuple[DomainExecutionResult, ...],
    blocked_action_count: int,
) -> tuple[RuntimeReply, str]:
    clarification = decision.clarification_need
    successful = [
        result
        for result in domain_results
        if result.status in {"succeeded", "simulated", "duplicate"}
    ]
    unavailable = [result for result in domain_results if result.status == "unavailable"]
    blocked = [result for result in domain_results if result.status in {"blocked", "failed"}]
    fragments = [result.reply_text for result in domain_results if result.reply_text]

    if clarification is not None and not successful:
        return RuntimeReply("clarification", clarification.question), "needs_clarification"
    if successful and (unavailable or blocked or blocked_action_count or clarification is not None):
        if clarification is not None:
            fragments.append(clarification.question)
        return RuntimeReply("partial", "\n\n".join(fragments)), "partial"
    if successful:
        reply_type = "ack_write" if any(result.actual_write for result in successful) else "ack_simulated"
        return RuntimeReply(reply_type, "\n\n".join(fragments)), "completed"
    if unavailable or blocked or blocked_action_count:
        text = "\n\n".join(fragments) or "该业务能力尚未接入 Phase 1 Runtime，本次未执行。"
        return RuntimeReply("failure", text), "blocked"
    return RuntimeReply("answer", "已完成认知判断，本次没有业务写入。"), "completed"


def _append_trace(trace: list[RuntimeTraceEvent], stage: str, detail: dict[str, Any]) -> None:
    trace.append(RuntimeTraceEvent(sequence=len(trace) + 1, stage=stage, detail=detail))
