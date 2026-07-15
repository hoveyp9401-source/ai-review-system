from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
import re
from typing import Any, Mapping
from uuid import UUID

from sqlalchemy import select

from app.agent2.business.contracts import BusinessCommandContext
from app.agent2.business.composition import BusinessCompositionResult, Phase2BusinessComposer
from app.agent2.business.case_followups import active_case_progress_followup_resources
from app.agent2.business.models import (
    Agent2Case,
    CaseFollowupPolicy,
    CaseFollowupPending,
    CaseFollowupTask,
    CaseProgress,
    NotificationOutbox,
    PartyCaseRole,
    TravelCollaborationCandidate,
    TravelIntent,
)
from app.agent2.business.repositories import CaseFollowupPolicySqlRepository, CaseProgressSqlRepository, CaseSqlRepository, PartySqlRepository
from app.agent2.business.sql_executor import SqlBusinessExecutor
from app.agent2.business.policy import BusinessEffectPolicy
from app.agent2.command_planner_v3 import (
    CognitiveCommandPlanner,
    CommandPlanningContext,
    DailySnapshotReference,
)
from app.agent2.cognitive_core_v3 import CognitiveCoreV3, CognitiveTurn
from app.agent2.domain_admission import AdmissionCapturePolicy, DomainAdmissionEngine
from app.agent2.information_pending_admission import (
    InformationContinuationAdmissionEngine,
    InformationContinuationSemanticInterpreter,
)
from app.agent2.selection_pending_admission import (
    SelectionContinuationAdmissionEngine,
    SelectionContinuationSemanticInterpreter,
    bind_fresh_selected_business_command,
)
from app.agent2.cognitive_orchestrator_v3 import (
    CognitiveOrchestrationResult,
    CognitiveOrchestratorV3,
    finalize_cognitive_state_after_execution,
)
from app.agent2.conversation_state import ConversationState
from app.agent2.conversation_state_store import SQLAlchemyConversationStateStore
from app.agent2.selection_pending import SelectionPending, SelectionPendingFactory
from app.agent2.selection_pending import (
    SelectionContext,
    SelectionValidation,
    answer_may_target_selection,
    settle_selection,
)
from app.agent2.selection_runtime import SelectionTurnCoordinator, SelectionTurnResult
from app.agent2.outcome_adapters import business_composition_outcomes
from app.agent2.semantic_interpreter_v3 import LLMCognitiveSemanticInterpreter
from app.agent2.semantic_interpreter_v3 import report_type_from_meta_opening
from app.agent2.report_sql_executor import load_periodic_report_snapshot, snapshot_payload
from app.agent2.typed_daily_executor import build_typed_daily_snapshot
from app.models import DailyReport
from app.agent2.workflow_audit import (
    create_agent2_cognitive_v3_audit_event,
    create_agent2_selection_audit_event,
    persist_selection_settlement_audit,
)
from app.utils.time import now_in_timezone
from app.workflows.intake import IncomingMessageEnvelope


class _SqlSelectionCandidateValidator:
    def __init__(self, session: Any, business_context: BusinessCommandContext):
        self._repository = CaseSqlRepository(session)
        self._context = business_context

    async def validate(self, pending, candidate, context) -> SelectionValidation:
        if pending.domain != "case_progress":
            return SelectionValidation("illegal", "unsupported_selection_domain")
        visible = await self._repository.list_visible(self._context)
        current = next((item for item in visible if item.case_id == candidate.stable_id), None)
        if current is None:
            return SelectionValidation("not_found", "candidate_missing_or_forbidden")
        if current.version != candidate.version:
            return SelectionValidation("version_conflict", "candidate_version_changed")
        return SelectionValidation.valid()


async def execute_selection_pending_turn(
    *,
    session: Any,
    user: Any,
    envelope: IncomingMessageEnvelope,
    business_context: BusinessCommandContext,
    settings: Any,
) -> SelectionTurnResult | None:
    """Continue a saved selection before invoking semantic interpretation."""
    conversation_id = str(envelope.conversation_id or "").strip() or _fallback_conversation_id(
        envelope, user
    )
    state_user_key = _conversation_state_user_key(user, envelope, business_context)
    store = SQLAlchemyConversationStateStore(session)
    state = await store.load(user_id=state_user_key, conversation_id=conversation_id)
    if not state.selection_pending or not answer_may_target_selection(
        state.selection_pending, str(envelope.raw_text or "")
    ):
        return None
    context = SelectionContext(
        tenant_id=business_context.tenant_id,
        user_id=business_context.actor_user_id,
        conversation_id=conversation_id,
        conversation_state_version=state.version,
        source_turn_id=str(envelope.message_id or ""),
        now=envelope.received_at or business_context.occurred_at,
    )

    async def execute(command):
        composition = await Phase2BusinessComposer(
            case_repository=CaseSqlRepository(session),
            party_repository=PartySqlRepository(session),
            progress_repository=CaseProgressSqlRepository(session),
            followup_policy_repository=CaseFollowupPolicySqlRepository(session),
            executor=SqlBusinessExecutor(
                session,
                effect_policy=BusinessEffectPolicy.from_settings(settings),
                execution_authority="semantic_ticket",
            ),
        ).execute((command,), business_context)
        outcomes = business_composition_outcomes(composition)
        if len(outcomes) != 1:
            raise RuntimeError("selection continuation must produce exactly one outcome")
        return outcomes[0]

    turn_result = await SelectionTurnCoordinator().handle(
        state,
        answer=str(envelope.raw_text or ""),
        context=context,
        validator=_SqlSelectionCandidateValidator(session, business_context),
        executor=execute,
    )
    if turn_result.state_after != state:
        await store.save(turn_result.state_after, expected_version=state.version)
    await create_agent2_selection_audit_event(
        session=session,
        user=user,
        envelope=envelope,
        turn_result=turn_result,
        report_date=context.now.date(),
    )
    return turn_result


async def evaluate_cognitive_core_v3(
    *,
    session: Any,
    user: Any,
    envelope: IncomingMessageEnvelope,
    llm_client: Any,
    daily_report: Any | None,
    report_date: date,
    settings: Any,
    business_context: BusinessCommandContext | None = None,
    information_continuation: Any | None = None,
    selection_continuation: Any | None = None,
) -> CognitiveOrchestrationResult:
    """Build the production v3 cognition and typed-command plan for one turn."""

    conversation_id = str(envelope.conversation_id or "").strip() or _fallback_conversation_id(envelope, user)
    occurred_at = envelope.received_at or now_in_timezone(
        getattr(user, "timezone", None) or getattr(settings, "timezone", "Asia/Shanghai")
    )
    snapshot = build_typed_daily_snapshot(user=user, report_date=report_date, report=daily_report)
    state_user_key = _conversation_state_user_key(user, envelope, business_context)
    existing_state = await SQLAlchemyConversationStateStore(session).load(
        user_id=state_user_key,
        conversation_id=conversation_id,
    )
    if information_continuation is not None and selection_continuation is not None:
        raise ValueError("pending continuation type must be unique")
    if information_continuation is not None:
        turn = CognitiveTurn(
            user_id=state_user_key,
            actor_user_id=str(getattr(user, "id", "") or ""),
            tenant_id=(
                business_context.tenant_id if business_context is not None else ""
            ),
            conversation_id=conversation_id,
            message_id=str(envelope.message_id or "").strip(),
            text=str(envelope.raw_text or ""),
            occurred_at=occurred_at,
            resources={
                "timezone": getattr(user, "timezone", None)
                or getattr(settings, "timezone", "Asia/Shanghai"),
                "information_continuation": information_continuation,
            },
        )
        result = await CognitiveOrchestratorV3(
            core=CognitiveCoreV3(
                InformationContinuationSemanticInterpreter(
                    information_continuation
                ),
                admission_engine=InformationContinuationAdmissionEngine(
                    information_continuation
                ),
                admission_enforced=True,
            ),
            planner=CognitiveCommandPlanner(),
            state_store=SQLAlchemyConversationStateStore(session),
        ).process(
            turn,
            CommandPlanningContext(
                message_id=turn.message_id,
                actor_user_id=user.id,
                daily_snapshot=snapshot,
                current_report_date=occurred_at.date(),
                historical_mutation_allowed=occurred_at.hour < 9,
            ),
        )
        await create_agent2_cognitive_v3_audit_event(
            session=session,
            user=user,
            envelope=envelope,
            result=result,
            report_date=report_date,
        )
        return result
    if selection_continuation is not None:
        turn = CognitiveTurn(
            user_id=state_user_key,
            actor_user_id=str(getattr(user, "id", "") or ""),
            tenant_id=(
                business_context.tenant_id if business_context is not None else ""
            ),
            conversation_id=conversation_id,
            message_id=str(envelope.message_id or "").strip(),
            text=str(envelope.raw_text or ""),
            occurred_at=occurred_at,
            resources={
                "timezone": getattr(user, "timezone", None)
                or getattr(settings, "timezone", "Asia/Shanghai"),
                "selection_continuation": selection_continuation,
            },
        )
        result = await CognitiveOrchestratorV3(
            core=CognitiveCoreV3(
                SelectionContinuationSemanticInterpreter(
                    selection_continuation
                ),
                admission_engine=SelectionContinuationAdmissionEngine(
                    selection_continuation
                ),
                admission_enforced=True,
            ),
            planner=CognitiveCommandPlanner(),
            state_store=SQLAlchemyConversationStateStore(session),
        ).process(
            turn,
            CommandPlanningContext(
                message_id=turn.message_id,
                actor_user_id=user.id,
                daily_snapshot=snapshot,
                current_report_date=occurred_at.date(),
                historical_mutation_allowed=occurred_at.hour < 9,
            ),
        )
        tickets = tuple(result.decision.admission_tickets or ())
        if len(tickets) != 1:
            raise ValueError("selection continuation requires one fresh Ticket")
        bound = bind_fresh_selected_business_command(
            selection_continuation,
            tickets[0],
        )
        result = replace(
            result,
            command_plan=replace(
                result.command_plan,
                daily_commands=(),
                report_commands=(),
                business_commands=(bound,),
                blocked_actions=(),
            ),
        )
        await create_agent2_cognitive_v3_audit_event(
            session=session,
            user=user,
            envelope=envelope,
            result=result,
            report_date=report_date,
        )
        return result
    periodic_type = _active_periodic_report_type(
        str(envelope.raw_text or ""),
        existing_state,
    )
    periodic_snapshot = (
        await load_periodic_report_snapshot(
            session,
            context=business_context,
            report_type=periodic_type,
            anchor=occurred_at.date(),
        )
        if business_context is not None and periodic_type is not None
        else None
    )
    daily_history = await _daily_history_references(
        session,
        user=user,
        current_date=report_date,
        current_snapshot=snapshot,
        requested_dates=_resolve_daily_reference_dates(
            str(envelope.raw_text or ""),
            reference_date=occurred_at.date(),
        ),
    )
    recent_case_progress = await _recent_case_progress_resource(session, business_context)
    visible_cases = await _visible_case_resource(session, business_context)
    active_travel_collaborations = await _active_travel_collaboration_resource(
        session,
        business_context,
    )
    active_case_followups = await _active_case_progress_followup_resource(
        session,
        business_context,
    )
    active_case_followup_policies = await _case_followup_policy_resource(
        session,
        business_context,
        visible_cases,
    )
    visible_party_ids, visible_travel_intent_ids = await _case_link_resources(
        session,
        business_context,
    )
    turn = CognitiveTurn(
        user_id=state_user_key,
        actor_user_id=str(getattr(user, "id", "") or ""),
        tenant_id=(business_context.tenant_id if business_context is not None else ""),
        conversation_id=conversation_id,
        message_id=str(envelope.message_id or "").strip(),
        text=str(envelope.raw_text or ""),
        occurred_at=occurred_at,
        resources={
            "daily_draft": _daily_snapshot_resource(snapshot),
            "daily_reports": [
                {
                    "report_date": reference.report_date.isoformat(),
                    **_daily_snapshot_resource(reference.snapshot),
                }
                for reference in daily_history
            ],
            "daily_policy": {
                "current_report_date": report_date.isoformat(),
                "historical_mutation_cutoff": "09:00",
                "historical_mutation_allowed": occurred_at.hour < 9,
                "current_time": occurred_at.isoformat(),
            },
            "active_tasks": [
                {
                    "workflow": task.workflow,
                    "task_id": task.task_id,
                    "status": task.status,
                    "reply_candidate": task.reply_candidate,
                    "awaiting_confirmation": task.awaiting_confirmation,
                    "metadata": dict(task.metadata),
                }
                for task in envelope.active_tasks
            ],
            "recent_case_progress": recent_case_progress,
            "visible_cases": visible_cases,
            "active_case_progress_followups": active_case_followups,
            "active_case_followup_policies": active_case_followup_policies,
            "active_travel_collaborations": active_travel_collaborations,
            "visible_party_ids": visible_party_ids,
            "visible_document_ids": [],
            "visible_travel_intent_ids": visible_travel_intent_ids,
            "operation_status_access": {
                "tenant_id": business_context.tenant_id,
                "user_id": business_context.actor_user_id,
                "allowed": True,
            } if business_context is not None else {},
            "enterprise_knowledge_access": {
                "tenant_id": business_context.tenant_id,
                "user_id": business_context.actor_user_id,
                "allowed": True,
            } if business_context is not None else {},
            "periodic_report": (
                snapshot_payload(periodic_snapshot) if periodic_snapshot is not None else None
            ),
            "timezone": getattr(user, "timezone", None)
            or getattr(settings, "timezone", "Asia/Shanghai"),
            "information_continuation": (
                {
                    "pending_id": str(
                        getattr(
                            getattr(information_continuation, "pending", None),
                            "pending_id",
                            "",
                        )
                        or ""
                    ),
                    "domain": str(
                        getattr(
                            getattr(
                                information_continuation,
                                "fresh_admission_request",
                                None,
                            ),
                            "domain",
                            "",
                        )
                        or ""
                    ),
                    "operation": str(
                        getattr(
                            getattr(
                                information_continuation,
                                "fresh_admission_request",
                                None,
                            ),
                            "operation",
                            "",
                        )
                        or ""
                    ),
                    "missing_field_values": dict(
                        getattr(
                            getattr(
                                information_continuation,
                                "fresh_admission_request",
                                None,
                            ),
                            "field_values",
                            {},
                        )
                        or {}
                    ),
                    "requires_fresh_admission": True,
                    "business_write_allowed": False,
                }
                if information_continuation is not None
                else None
            ),
        },
    )
    capture_policy = semantic_admission_capture_policy(
        settings,
        tenant_id=turn.tenant_id,
        user_id=turn.actor_user_id,
    )
    admission_mode = capture_policy.mode
    interpreter = LLMCognitiveSemanticInterpreter(
        llm_client,
        model=str(getattr(settings, "agent2_cognitive_core_v3_model", "") or "")
        or str(getattr(settings, "llm_intent_model", "") or "")
        or None,
        thinking_enabled=bool(getattr(settings, "agent2_cognitive_core_v3_thinking", True)),
        # Shadow observes Admission against the current production-compatible
        # semantic baseline.  Only Enforce switches to the raw model proposal;
        # raw-vs-baseline comparison remains an offline replay concern.
        legacy_semantic_enforcers_enabled=admission_mode != "enforced",
    )
    orchestrator = CognitiveOrchestratorV3(
        core=CognitiveCoreV3(
            interpreter,
            admission_engine=(
                DomainAdmissionEngine(capture_policy=capture_policy)
                if admission_mode != "disabled"
                else None
            ),
            admission_enforced=admission_mode == "enforced",
        ),
        planner=CognitiveCommandPlanner(),
        state_store=SQLAlchemyConversationStateStore(session),
    )
    result = await orchestrator.process(
        turn,
        CommandPlanningContext(
            message_id=turn.message_id,
            actor_user_id=user.id,
            daily_snapshot=snapshot,
            daily_history=daily_history,
            current_report_date=occurred_at.date(),
            historical_mutation_allowed=occurred_at.hour < 9,
            periodic_snapshot=periodic_snapshot,
        ),
    )
    await create_agent2_cognitive_v3_audit_event(
        session=session,
        user=user,
        envelope=envelope,
        result=result,
        report_date=report_date,
    )
    return result


def cognitive_core_v3_enabled(settings: Any) -> bool:
    return bool(getattr(settings, "agent2_cognitive_core_v3_enabled", False))


def selection_request_reply(decision: Any) -> str:
    """Render one Admission-backed Selection request without exposing IDs."""

    if str(getattr(decision, "admission_mode", "") or "") != "enforced":
        return ""
    requests = tuple(
        getattr(decision, "admission_selection_requests", ()) or ()
    )
    if not requests:
        return ""
    if len(requests) != 1:
        return (
            "这条消息里有不止一处需要确认的案件，我没有写入。"
            "请补充完整案件名称或案号后再发一次。"
        )
    request = requests[0]
    candidates = tuple(getattr(request, "candidates", ()) or ())
    if len(candidates) < 2:
        return "案件还不能唯一确定，本次没有写入。请补充完整案件名称或案号。"
    lines = [
        "我找到了多个匹配案件，本次还没有写入。请回复序号选择："
    ]
    lines.extend(
        f"{index}. {str(getattr(candidate, 'label', '') or '').strip()}"
        for index, candidate in enumerate(candidates, start=1)
    )
    return "\n".join(lines)


def information_pending_reply(decision: Any) -> str:
    """Render one Admission-backed missing-information question."""

    if str(getattr(decision, "admission_mode", "") or "") != "enforced":
        return ""
    pendings = tuple(
        getattr(decision, "admission_information_pendings", ()) or ()
    )
    if not pendings:
        return ""
    if len(pendings) != 1:
        return (
            "这条消息里有不止一项信息需要补充，我还没有登记。"
            "请一次说清一项业务内容。"
        )
    snapshot = getattr(pendings[0], "question_snapshot", {})
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    if str(snapshot.get("question_key") or "") == "travel_date_required":
        destination = str(snapshot.get("destination") or "").strip()
        if destination:
            return (
                f"去{destination}的出差我已经识别到了，还差出发日期。"
                "哪天去？本次还没有登记。"
            )
        return "这次出差还差出发日期。哪天去？本次还没有登记。"
    return "还缺少完成这项登记所需的信息，本次没有写入。请补充后再发一次。"


def admission_block_reply(decision: Any) -> str:
    """Translate a small set of safe Admission blocks into plain Chinese."""

    if str(getattr(decision, "admission_mode", "") or "") != "enforced":
        return ""
    trace = getattr(decision, "admission_trace", None)
    decisions = tuple(getattr(trace, "decisions", ()) or ())
    blocked_reasons = {
        str(getattr(item, "reason_code", "") or "")
        for item in decisions
        if str(getattr(item, "status", "") or "") == "blocked"
    }
    if blocked_reasons and blocked_reasons.issubset(
        {
            "case_reference_not_uniquely_authorized",
            "case_reference_not_grounded_in_segment",
        }
    ):
        return (
            "我没在你当前分配的案件中找到这个案件编号或名称。"
            "请核对后再发一次；本次没有登记。"
        )
    return ""


def semantic_admission_mode(
    settings: Any,
    *,
    tenant_id: str,
    user_id: str,
) -> str:
    """Return disabled/shadow/enforced from trusted scope and closed allowlists."""

    if not bool(getattr(settings, "agent2_semantic_admission_enabled", False)):
        return "disabled"
    tenants = _csv_values(
        getattr(settings, "agent2_semantic_admission_tenant_allowlist", "")
    )
    users = _csv_values(
        getattr(settings, "agent2_semantic_admission_user_allowlist", "")
    )
    if not tenant_id or not user_id or tenant_id not in tenants or user_id not in users:
        return "disabled"
    if bool(getattr(settings, "agent2_semantic_admission_enforce", False)):
        return "enforced"
    return "shadow"


def semantic_admission_capture_policy(
    settings: Any,
    *,
    tenant_id: str,
    user_id: str,
) -> AdmissionCapturePolicy:
    """Resolve online capture only from the trusted Admission scope and flags.

    ``agent2_semantic_admission_shadow_replay`` deliberately has no online
    effect. Replay remains an offline evaluator concern and never wires a
    second model or capture path into a live turn.
    """

    mode = semantic_admission_mode(
        settings,
        tenant_id=tenant_id,
        user_id=user_id,
    )
    if mode == "disabled":
        return AdmissionCapturePolicy()
    return AdmissionCapturePolicy(
        mode=mode,
        review_enabled=bool(
            getattr(settings, "agent2_semantic_admission_review_capture", False)
        ),
        deferred_enabled=bool(
            getattr(settings, "agent2_semantic_admission_deferred_capture", False)
        ),
    )


def _csv_values(raw: object) -> frozenset[str]:
    return frozenset(
        item.strip()
        for item in str(raw or "").split(",")
        if item.strip()
    )


def bind_cognitive_business_execution_context(
    result: CognitiveOrchestrationResult,
    context: BusinessCommandContext,
    *,
    execution_started_at: datetime | None = None,
) -> BusinessCommandContext:
    """Bind the exact state snapshot used by Admission to business execution."""

    return replace(
        context,
        conversation_state_version=result.base_state.version,
        execution_started_at=execution_started_at or datetime.now(UTC),
    )


async def finalize_cognitive_core_v3_execution(
    *,
    session: Any,
    result: CognitiveOrchestrationResult,
    command_results: list[dict[str, Any]],
    business_result: BusinessCompositionResult | None = None,
    report_results: list[dict[str, Any]] | None = None,
    business_context: BusinessCommandContext | None = None,
    selection_continuation: Any | None = None,
) -> ConversationState:
    selection_pending = (
        *_selection_pending_from_admission_requests(result),
        *_selection_pending_from_business_result(
            result,
            business_result=business_result,
            business_context=business_context,
        ),
    )
    if selection_continuation is not None:
        pending = getattr(selection_continuation, "pending", None)
        resolution = getattr(selection_continuation, "resolution", None)
        outcome = _validated_selection_settlement_outcome(
            result=result,
            business_result=business_result,
            business_context=business_context,
            selection_continuation=selection_continuation,
        )
        if pending is None or resolution is None:
            raise ValueError("selection continuation settlement is incomplete")
        settlement = settle_selection(
            pending,
            resolution,
            outcome,
            settled_at=(
                business_context.occurred_at
                if business_context is not None
                else datetime.now(UTC)
            ),
        )
        if business_context is None:
            raise ValueError("selection settlement requires business context")
        await persist_selection_settlement_audit(
            session=session,
            audit=settlement.audit,
            outcome=outcome,
            business_context=business_context,
        )
        selection_pending = (settlement.pending_after,)
    execution_succeeded = _all_cognitive_commands_succeeded(
        result,
        command_results=command_results,
        business_result=business_result,
        report_results=report_results or [],
    )
    return await finalize_cognitive_state_after_execution(
        result=result,
        state_store=SQLAlchemyConversationStateStore(session),
        execution_succeeded=execution_succeeded,
        selection_pending=selection_pending,
    )


def _validated_selection_settlement_outcome(
    *,
    result: CognitiveOrchestrationResult,
    business_result: BusinessCompositionResult | None,
    business_context: BusinessCommandContext | None,
    selection_continuation: Any,
) -> Any:
    """Bind one Selection settlement to its exact planned command and receipt."""

    pending = getattr(selection_continuation, "pending", None)
    resolution = getattr(selection_continuation, "resolution", None)
    fresh = getattr(selection_continuation, "fresh_admission_request", None)
    if pending is None or resolution is None or fresh is None:
        raise ValueError("selection settlement requires fresh admission context")
    if business_context is None or business_result is None:
        raise ValueError("selection settlement requires business execution evidence")
    if str(getattr(result.decision, "admission_mode", "") or "") != "enforced":
        raise ValueError("selection settlement requires enforced admission")
    if (
        str(getattr(pending, "status", "") or "") != "active"
        or str(getattr(pending, "pending_id", "") or "")
        != str(getattr(resolution, "pending_id", "") or "")
        or str(getattr(resolution, "status", "") or "") != "selected"
        or pending not in tuple(getattr(result.base_state, "selection_pending", ()) or ())
    ):
        raise ValueError("selection settlement pending is not the active base pending")

    selected_id = str(getattr(resolution, "selected_candidate_id", "") or "")
    selected = tuple(
        candidate
        for candidate in tuple(getattr(pending, "candidates", ()) or ())
        if str(getattr(candidate, "stable_id", "") or "") == selected_id
    )
    if len(selected) != 1:
        raise ValueError("selection settlement candidate is not uniquely bound")
    candidate = selected[0]
    selected_version = getattr(resolution, "selected_candidate_version", None)
    if selected_version is not None and selected_version != candidate.version:
        raise ValueError("selection settlement candidate version changed")
    if (
        str(getattr(fresh, "pending_id", "") or "") != pending.pending_id
        or str(getattr(fresh, "candidate_stable_id", "") or "") != selected_id
        or getattr(fresh, "candidate_version", None) != candidate.version
        or str(getattr(fresh, "source_message_id", "") or "")
        != business_context.source_message_id
    ):
        raise ValueError("selection settlement fresh admission binding changed")

    if (
        pending.tenant_id != business_context.tenant_id
        or pending.user_id != business_context.actor_user_id
        or pending.conversation_id != business_context.conversation_id
        or business_context.conversation_state_version != result.base_state.version
        or selected_id not in business_context.allowed_case_ids
        or business_result.source_message_id != business_context.source_message_id
    ):
        raise ValueError("selection settlement execution scope changed")

    plan = result.command_plan
    planned = tuple(getattr(plan, "business_commands", ()) or ())
    if (
        len(planned) != 1
        or tuple(getattr(plan, "daily_commands", ()) or ())
        or tuple(getattr(plan, "report_commands", ()) or ())
        or tuple(getattr(plan, "blocked_actions", ()) or ())
        or len(business_result.actions) != 1
    ):
        raise ValueError("selection settlement requires one isolated business command")
    command = planned[0]
    action = business_result.actions[0]
    command_id = str(getattr(command, "command_id", "") or "")
    receipt = getattr(action, "receipt", None)
    if (
        not command_id
        or str(getattr(command, "command_type", "") or "")
        != "record_case_progress_candidate"
        or str(getattr(action, "semantic_command_id", "") or "") != command_id
        or receipt is None
        or str(getattr(receipt, "command_id", "") or "") != command_id
    ):
        raise ValueError("selection settlement receipt is unrelated to planned command")

    payload = getattr(command, "payload", None)
    entities = payload.get("entities") if isinstance(payload, dict) else None
    entity = entities[0] if isinstance(entities, list) and len(entities) == 1 else None
    attributes = entity.get("attributes") if isinstance(entity, dict) else None
    if (
        not isinstance(entity, dict)
        or str(entity.get("value") or "") != selected_id
        or not isinstance(attributes, dict)
        or str(attributes.get("case_id") or "") != selected_id
    ):
        raise ValueError("selection settlement planned command selected another case")

    after = dict(getattr(receipt, "after", None) or {})
    if (
        str(getattr(receipt, "tenant_id", "") or "") != business_context.tenant_id
        or str(getattr(receipt, "actor_user_id", "") or "")
        != business_context.actor_user_id
        or str(getattr(receipt, "source_message_id", "") or "")
        != business_context.source_message_id
        or str(getattr(receipt, "resource_type", "") or "") != "case_progress"
        or str(after.get("case_id") or "") != selected_id
        or not str(getattr(receipt, "receipt_id", "") or "").strip()
    ):
        raise ValueError("selection settlement receipt scope or case changed")

    outcomes = business_composition_outcomes(business_result)
    if len(outcomes) != 1:
        raise ValueError("selection settlement requires one business outcome")
    outcome = outcomes[0]
    if (
        outcome.domain != "case_progress"
        or outcome.operation != "create"
        or outcome.tenant_id != business_context.tenant_id
        or outcome.user_id != business_context.actor_user_id
        or outcome.source_turn_id != business_context.source_message_id
        or len(outcome.receipt_refs) != 1
        or outcome.receipt_refs[0].receipt_id != receipt.receipt_id
    ):
        raise ValueError("selection settlement outcome is unrelated to receipt")
    return outcome


def _selection_pending_from_admission_requests(
    result: CognitiveOrchestrationResult,
) -> tuple[SelectionPending, ...]:
    # Shadow artifacts are audit-only.  Letting one become an active Pending
    # would change the next production turn even though Admission is not yet
    # authoritative.
    if str(getattr(result.decision, "admission_mode", "") or "") != "enforced":
        return ()
    factory = SelectionPendingFactory()
    requests = tuple(
        getattr(result.decision, "admission_selection_requests", ()) or ()
    )
    return tuple(factory.from_trusted_request(request) for request in requests)


def _selection_pending_from_business_result(
    result: CognitiveOrchestrationResult,
    *,
    business_result: BusinessCompositionResult | None,
    business_context: BusinessCommandContext | None,
) -> tuple[SelectionPending, ...]:
    if business_result is None or business_context is None:
        return ()
    factory = SelectionPendingFactory()
    pendings: list[SelectionPending] = []
    for action in business_result.actions:
        if action.block is None:
            continue
        pending = factory.from_block(
            action.block,
            tenant_id=business_context.tenant_id,
            user_id=business_context.actor_user_id,
            conversation_id=result.base_state.conversation_id,
            source_turn_id=business_context.source_message_id,
            expected_conversation_state_version=result.state.version,
            now=business_context.occurred_at,
            expires_in_seconds=600,
        )
        if pending is not None:
            pendings.append(pending)
    return tuple(pendings)


def _all_cognitive_commands_succeeded(
    result: CognitiveOrchestrationResult,
    *,
    command_results: list[dict[str, Any]],
    business_result: BusinessCompositionResult | None,
    report_results: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
) -> bool:
    """Require a successful/duplicate outcome for every planned domain action."""

    plan = result.command_plan
    if plan.blocked_actions and not all(
        _is_admission_nonwrite_block(block) for block in plan.blocked_actions
    ):
        return False
    expected_daily = len(plan.daily_commands)
    if expected_daily:
        if len(command_results) != expected_daily:
            return False
        expected_ids = tuple(str(command.command_id) for command in plan.daily_commands)
        actual_ids = tuple(
            str((item.get("typed_command") or {}).get("command_id") or "")
            for item in command_results
        )
        if actual_ids != expected_ids:
            return False
        if any(
            item.get("validation_status") not in {"authorized", "duplicate"}
            or not str(item.get("receipt_id") or "").strip()
            for item in command_results
        ):
            return False
    elif command_results:
        return False
    expected_business = len(plan.business_commands)
    if expected_business:
        if business_result is None or len(business_result.actions) != expected_business:
            return False
        expected_ids = tuple(str(command.command_id) for command in plan.business_commands)
        actual_ids = tuple(action.semantic_command_id for action in business_result.actions)
        if actual_ids != expected_ids:
            return False
        if any(not _business_action_safely_settled(action) for action in business_result.actions):
            return False
    elif business_result is not None and business_result.actions:
        return False
    expected_reports = len(getattr(plan, "report_commands", ()))
    if expected_reports:
        if len(report_results) != expected_reports:
            return False
        expected_ids = tuple(str(command.command_id) for command in plan.report_commands)
        actual_ids = tuple(
            str((item.get("typed_command") or {}).get("command_id") or "")
            for item in report_results
        )
        if actual_ids != expected_ids or any(
            item.get("validation_status") not in {"authorized", "duplicate"}
            or not str(item.get("receipt_id") or "").strip()
            for item in report_results
        ):
            return False
    elif report_results:
        return False
    return bool(expected_daily or expected_business or expected_reports)


def _is_admission_nonwrite_block(block: Any) -> bool:
    metadata = getattr(block, "metadata", None)
    if not isinstance(metadata, dict):
        return False
    admission = metadata.get("admission")
    return bool(
        isinstance(admission, dict)
        and str(admission.get("status") or "")
        in {
            "blocked",
            "information_required",
            "review_only",
            "deferred_audit_only",
        }
    )


def _business_action_safely_settled(action: Any) -> bool:
    if (
        action.status in {"executed", "duplicate"}
        and action.receipt is not None
        and str(action.receipt.receipt_id or "").strip()
    ):
        return True
    block = getattr(action, "block", None)
    metadata = dict(getattr(block, "metadata", None) or {}) if block else {}
    selection = metadata.get("selection")
    return bool(
        action.status == "blocked"
        and isinstance(selection, dict)
        and selection.get("candidates")
        and selection.get("continuation_payload")
    )


def _active_periodic_report_type(text: str, state: ConversationState) -> str | None:
    opening = report_type_from_meta_opening(text)
    if opening in {"weekly", "monthly"}:
        return opening
    compact = str(text or "")
    if "周报" in compact:
        return "weekly"
    if "月报" in compact:
        return "monthly"
    current_intent = str(getattr(state.current_goal, "intent", "") or "")
    if current_intent == "weekly_report":
        return "weekly"
    if current_intent == "monthly_report":
        return "monthly"
    return None


def _fallback_conversation_id(envelope: IncomingMessageEnvelope, user: Any) -> str:
    user_key = str(getattr(user, "id", "") or envelope.sender_id or envelope.dingtalk_user_id or "unknown")
    return f"agent2-direct:{user_key}"


def _conversation_state_user_key(
    user: Any,
    envelope: IncomingMessageEnvelope,
    business_context: BusinessCommandContext | None,
) -> str:
    """Namespace Phase 2 state by tenant without trusting message text.

    The persisted ``user_key`` remains a single indexed column, while the
    server-resolved tenant binding becomes part of its value. Daily-only state
    keeps its historical key for compatibility.
    """

    user_id = str(getattr(user, "id", "") or envelope.sender_id or "").strip()
    if business_context is None:
        return user_id
    if business_context.actor_user_id != user_id:
        raise ValueError("business context actor does not match resolved user")
    return f"{business_context.tenant_id}:{user_id}"


def _daily_snapshot_resource(snapshot: Any) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for field_name in ("today_work", "problems", "tomorrow_plan"):
        values = tuple(getattr(snapshot, field_name, ()) or ())
        item_ids = tuple(snapshot.item_ids.get(field_name, ()) or ())
        for index, value in enumerate(values):
            items.append(
                {
                    "item_id": item_ids[index] if index < len(item_ids) else "",
                    "field": field_name,
                    "field_index": index + 1,
                    "text": value,
                }
            )
    return {
        "report_id": str(snapshot.report_id),
        "version": snapshot.version,
        "status": snapshot.status,
        "items": items,
    }


async def _daily_history_references(
    session: Any,
    *,
    user: Any,
    current_date: date,
    current_snapshot: Any,
    requested_dates: tuple[date, ...] = (),
) -> tuple[DailySnapshotReference, ...]:
    statement = (
        select(DailyReport)
        .where(DailyReport.user_id == user.id)
        .order_by(DailyReport.report_date.desc(), DailyReport.updated_at.desc())
        .limit(93)
    )
    result = await session.execute(statement)
    references: list[DailySnapshotReference] = []
    seen_report_ids: set[str] = set()
    for report in result.scalars().all():
        snapshot = build_typed_daily_snapshot(
            user=user,
            report_date=report.report_date,
            report=report,
        )
        report_id = str(snapshot.report_id)
        if report_id in seen_report_ids:
            continue
        seen_report_ids.add(report_id)
        references.append(DailySnapshotReference(report.report_date, snapshot))
    loaded_dates = {reference.report_date for reference in references}
    for requested_date in requested_dates:
        if requested_date in loaded_dates:
            continue
        exact_statement = (
            select(DailyReport)
            .where(
                DailyReport.user_id == user.id,
                DailyReport.report_date == requested_date,
            )
            .order_by(DailyReport.updated_at.desc())
            .limit(1)
        )
        exact_result = await session.execute(exact_statement)
        report = exact_result.scalars().first()
        if report is None:
            continue
        exact_snapshot = build_typed_daily_snapshot(
            user=user,
            report_date=report.report_date,
            report=report,
        )
        if str(exact_snapshot.report_id) in seen_report_ids:
            continue
        seen_report_ids.add(str(exact_snapshot.report_id))
        loaded_dates.add(report.report_date)
        references.append(DailySnapshotReference(report.report_date, exact_snapshot))
    if str(current_snapshot.report_id) not in seen_report_ids:
        references.append(DailySnapshotReference(current_date, current_snapshot))
    references.sort(key=lambda item: item.report_date, reverse=True)
    return tuple(references)


def _resolve_daily_reference_dates(text: str, *, reference_date: date) -> tuple[date, ...]:
    """Resolve explicit date mentions for trusted report lookup without deciding intent."""

    normalized = str(text or "").strip()
    resolved: list[date] = []
    relative_offsets = (
        ("大前天", 3),
        ("前天", 2),
        ("昨天", 1),
        ("昨日", 1),
        ("今天", 0),
        ("今日", 0),
    )
    for token, offset in relative_offsets:
        if token in normalized:
            resolved.append(date.fromordinal(reference_date.toordinal() - offset))
    for match in re.finditer(
        r"(?<!\d)(\d{4})\s*(?:-|/|\.|年)\s*(\d{1,2})\s*(?:-|/|\.|月)\s*(\d{1,2})\s*日?",
        normalized,
    ):
        try:
            resolved.append(date(int(match.group(1)), int(match.group(2)), int(match.group(3))))
        except ValueError:
            continue
    for match in re.finditer(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日?", normalized):
        try:
            candidate = date(reference_date.year, int(match.group(1)), int(match.group(2)))
        except ValueError:
            continue
        if candidate > reference_date:
            try:
                candidate = candidate.replace(year=candidate.year - 1)
            except ValueError:
                continue
        resolved.append(candidate)
    return tuple(dict.fromkeys(resolved))


async def _recent_case_progress_resource(
    session: Any,
    context: BusinessCommandContext | None,
) -> list[dict[str, Any]]:
    if context is None:
        return []
    case_ids: list[UUID] = []
    for value in context.allowed_case_ids:
        try:
            case_ids.append(UUID(value))
        except (TypeError, ValueError):
            continue
    if not case_ids:
        return []
    statement = select(CaseProgress).where(
        CaseProgress.tenant_id == context.tenant_id,
        CaseProgress.case_id.in_(case_ids),
        CaseProgress.deleted_at.is_(None),
    )
    if "case_progress_admin" not in context.actor_role_ids:
        statement = statement.where(CaseProgress.reporter_id == context.actor_user_id)
    rows = list(
        (
            await session.scalars(
                statement.order_by(CaseProgress.recorded_at.desc()).limit(10)
            )
        ).all()
    )
    return [
        {
            "progress_id": str(item.progress_id),
            "case_id": str(item.case_id),
            "version": item.version,
            "summary": item.summary,
            "recorded_at": item.recorded_at.isoformat(),
            "content_origin": item.content_origin,
            "source_message_id": item.source_message_id,
        }
        for item in rows
    ]


async def _visible_case_resource(
    session: Any,
    context: BusinessCommandContext | None,
) -> list[dict[str, Any]]:
    if context is None:
        return []
    cases = await CaseSqlRepository(session).list_visible(context)
    return [
        {
            "case_id": item.case_id,
            "case_number": item.case_number,
            "case_name": item.case_name,
            "external_case_id": item.external_case_id,
            "confirmed_aliases": list(item.confirmed_aliases),
            "version": item.version,
        }
        for item in cases
    ]


async def _active_travel_collaboration_resource(
    session: Any,
    context: BusinessCommandContext | None,
) -> list[dict[str, Any]]:
    if context is None:
        return []
    rows = list(
        (
            await session.scalars(
                select(TravelCollaborationCandidate)
                .where(
                    TravelCollaborationCandidate.tenant_id == context.tenant_id,
                    TravelCollaborationCandidate.company_id == context.company_id,
                    TravelCollaborationCandidate.department_id == context.department_id,
                    TravelCollaborationCandidate.team_id == context.team_id,
                    TravelCollaborationCandidate.participant_ids.contains([context.actor_user_id]),
                    TravelCollaborationCandidate.status.in_(("notified", "accepted_by_one")),
                    TravelCollaborationCandidate.expires_at >= context.occurred_at,
                )
                .order_by(TravelCollaborationCandidate.created_at.desc())
                .limit(5)
            )
        ).all()
    )
    return [
        {
            "candidate_id": str(item.candidate_id),
            "destination": item.destination,
            "overlap_start": item.overlap_start.isoformat(),
            "overlap_end": item.overlap_end.isoformat(),
            "participant_ids": list(item.participant_ids or []),
            "status": item.status,
            "version": item.version,
        }
        for item in rows
    ]


async def _active_case_progress_followup_resource(
    session: Any,
    context: BusinessCommandContext | None,
) -> list[dict[str, str]]:
    if context is None:
        return []
    rows = tuple(
        (
            await session.scalars(
                select(NotificationOutbox)
                .where(
                    NotificationOutbox.tenant_id == context.tenant_id,
                    NotificationOutbox.recipient_user_id == context.actor_user_id,
                    NotificationOutbox.message_type == "case_progress_followup",
                    NotificationOutbox.status == "sent",
                )
                .order_by(NotificationOutbox.created_at.desc())
                .limit(5)
            )
        ).all()
    )
    resources = list(
        active_case_progress_followup_resources(
            rows,
            allowed_case_ids=context.allowed_case_ids,
            now=context.occurred_at,
        )
    )
    conversation_id = (
        context.conversation_id or f"agent2-direct:{context.actor_user_id}"
    )
    lifecycle_rows = (
        await session.execute(
            select(CaseFollowupPending, CaseFollowupTask, Agent2Case)
            .join(
                CaseFollowupTask,
                (CaseFollowupTask.tenant_id == CaseFollowupPending.tenant_id)
                & (CaseFollowupTask.followup_id == CaseFollowupPending.followup_id),
            )
            .join(
                Agent2Case,
                (Agent2Case.tenant_id == CaseFollowupPending.tenant_id)
                & (Agent2Case.case_id == CaseFollowupPending.case_id),
            )
            .where(
                CaseFollowupPending.tenant_id == context.tenant_id,
                CaseFollowupPending.pending_type == "case_followup",
                CaseFollowupPending.user_id == context.actor_user_id,
                CaseFollowupPending.conversation_id == conversation_id,
                CaseFollowupPending.status.in_(("active", "awaiting_input")),
                CaseFollowupPending.domain == "case",
                CaseFollowupPending.expires_at > context.occurred_at,
                CaseFollowupTask.task_status == "waiting_for_reply",
                CaseFollowupTask.message_status.in_((
                    "accepted_by_provider", "delivery_confirmed"
                )),
                CaseFollowupTask.provider_message_id != "",
            )
            .order_by(CaseFollowupPending.created_at.desc())
            .limit(5)
        )
    ).all()
    allowed = set(context.allowed_case_ids)
    resources.extend(
        {
            "notification_id": str(pending.pending_id),
            "pending_id": str(pending.pending_id),
            "followup_id": str(pending.followup_id),
            "case_id": str(pending.case_id),
            "case_name": case.case_name,
            "question_text": task.question_text,
            "expires_at": pending.expires_at.isoformat(),
            "source_type": "case_lifecycle_followup",
        }
        for pending, task, case in lifecycle_rows
        if str(pending.case_id) in allowed
    )
    return resources


async def _case_followup_policy_resource(
    session: Any,
    context: BusinessCommandContext | None,
    visible_cases: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if context is None:
        return []
    case_ids = [
        UUID(str(item.get("case_id") or ""))
        for item in visible_cases
        if _is_uuid_text(item.get("case_id"))
    ]
    if not case_ids:
        return []
    rows = list(
        (
            await session.scalars(
                select(CaseFollowupPolicy).where(
                    CaseFollowupPolicy.tenant_id == context.tenant_id,
                    CaseFollowupPolicy.assigned_user_id == context.actor_user_id,
                    CaseFollowupPolicy.case_id.in_(case_ids),
                )
            )
        ).all()
    )
    by_case = {str(item.case_id): item for item in rows}
    return [
        {
            "policy_id": str(by_case[case_id].policy_id) if case_id in by_case else "",
            "case_id": case_id,
            "assigned_user_id": context.actor_user_id,
            "version": by_case[case_id].version if case_id in by_case else 0,
        }
        for case_id in (str(value) for value in case_ids)
    ]


async def _case_link_resources(
    session: Any,
    context: BusinessCommandContext | None,
) -> tuple[list[str], list[str]]:
    if context is None:
        return [], []
    case_ids = [
        UUID(str(value))
        for value in context.allowed_case_ids
        if _is_uuid_text(value)
    ]
    party_ids: list[str] = []
    if case_ids:
        party_ids = [
            str(value)
            for value in (
                await session.scalars(
                    select(PartyCaseRole.party_id)
                    .where(
                        PartyCaseRole.tenant_id == context.tenant_id,
                        PartyCaseRole.case_id.in_(case_ids),
                        PartyCaseRole.confirmation_status == "confirmed",
                    )
                    .distinct()
                )
            ).all()
        ]
    travel_ids = [
        str(value)
        for value in (
            await session.scalars(
                select(TravelIntent.travel_intent_id).where(
                    TravelIntent.tenant_id == context.tenant_id,
                    TravelIntent.user_id == context.actor_user_id,
                )
            )
        ).all()
    ]
    return party_ids, travel_ids


def _is_uuid_text(value: Any) -> bool:
    try:
        UUID(str(value or ""))
    except (TypeError, ValueError):
        return False
    return True
