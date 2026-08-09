from __future__ import annotations

import logging
import uuid
from dataclasses import replace
from datetime import date
from typing import Any, Mapping

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent2.assistant_tools import build_tool_assisted_reply
from app.agent2.case_table_rag import CaseTableRagAdapter, DEFAULT_CASE_RAG_INDEX
from app.agent2.business.entrypoint import (
    build_business_command_context,
    resolve_agent2_entrypoint,
)
from app.agent2.context_pack import Agent2ContextPack, build_agent2_context_pack
from app.agent2.cognitive_reply_v3 import (
    append_cognitive_clarification,
    has_bound_confirmation_pending,
    has_pending_lifecycle_update,
    pending_lifecycle_reply,
)
from app.agent2.cognitive_runtime_v3 import (
    cognitive_core_v3_enabled,
    finalize_cognitive_core_v3_execution,
    selection_request_reply,
)
from app.agent2.business.composition import Phase2BusinessComposer
from app.agent2.business.policy import BusinessEffectPolicy
from app.agent2.business.repositories import (
    CaseFollowupPolicySqlRepository,
    CaseProgressSqlRepository,
    CaseSqlRepository,
    PartySqlRepository,
)
from app.agent2.business.sql_executor import SqlBusinessExecutor
from app.agent2.operation_outcomes import OutcomeReplyComposer
from app.agent2.operation_outcome_store import persist_operation_outcomes
from app.agent2.outcome_adapters import (
    business_composition_outcomes,
    daily_execution_outcomes,
    periodic_execution_outcomes,
    text_outcome,
)
from app.agent2.report_sql_executor import execute_periodic_report_commands
from app.agent2.turn_runtime import (
    InformationContinuationBlocked,
    SelectionContinuationBlocked,
    VerifiedTurnRejected,
    VerifiedTurnRequest,
    production_agent2_turn_runtime,
    verified_turn_rejection_reply,
)
from app.agent2.daily_execution import (
    Agent2DailyExecutionResult,
    agent2_daily_enabled_for_user,
    agent2_daily_report_version,
    agent2_daily_should_fallback_to_legacy,
    execute_agent2_daily_commands,
)
from app.agent2.daily_shadow import DailyShadowEvaluation, evaluate_daily_shadow
from app.agent2.knowledge_resolver import KnowledgeQuery, resolve_knowledge
from app.agent2.personal_memory import build_personal_memory_profile
from app.agent2.recent_context import load_recent_case_context_messages
from app.agent2.typed_daily_executor import execute_typed_agent2_daily_commands
from app.config import get_settings
from app.db import get_session
from app.repositories import (
    create_webhook_event_once,
    get_active_user_by_dingtalk_id,
    get_report,
    list_active_user_habits,
    list_reports_for_date,
    mark_webhook_event_failed,
    mark_webhook_event_processed,
)
from app.schemas import StructuredDailyReport
from app.services.report_service import DailyReportService
from app.utils.time import now_in_timezone
from app.workflows.daily_context import build_live_daily_active_task
from app.workflows.intake import IncomingMessageEnvelope, WORKFLOW_DAILY_REPORT

router = APIRouter(prefix="/reports", tags=["reports"])
logger = logging.getLogger(__name__)


class ManualReportRequest(BaseModel):
    dingtalk_user_id: str = Field(min_length=1)
    raw_input: str = Field(min_length=1)
    source: str = "manual_text"
    report_date: date | None = None
    idempotency_key: str | None = None
    conversation_id: str | None = None


@router.post("/manual")
async def submit_manual_report(
    request: Request,
    body: ManualReportRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    user = await get_active_user_by_dingtalk_id(session, body.dingtalk_user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")

    if body.idempotency_key:
        event, inserted = await create_webhook_event_once(
            session,
            idempotency_key=f"manual:{body.idempotency_key}",
            external_message_id=body.idempotency_key,
            dingtalk_user_id=body.dingtalk_user_id,
            payload=body.model_dump(mode="json"),
            platform="manual_api",
        )
        await session.commit()
        if not inserted and event.status == "processed":
            return event.response_payload
        if not inserted:
            return {"status": event.status, "message": "This idempotency key is already being processed."}
    else:
        event = None

    service: DailyReportService = request.app.state.report_service
    llm_client = getattr(getattr(service, "extractor", None), "client", None) or getattr(request.app.state, "llm_client", None)
    agent2_response = await _submit_manual_agent2_if_applicable(
        session=session,
        user=user,
        raw_input=body.raw_input,
        report_date=body.report_date,
        llm_client=llm_client,
        message_id=body.idempotency_key or f"manual:{uuid.uuid4()}",
        conversation_id=body.conversation_id or "",
    )
    if agent2_response is not None:
        if event is not None:
            report_id = agent2_response.get("report_id")
            await mark_webhook_event_processed(
                session,
                event,
                report_id=uuid.UUID(report_id) if report_id else None,
                response_payload=agent2_response,
                now=now_in_timezone(user.timezone),
            )
        await session.commit()
        return agent2_response
    response = {
        "report_id": None,
        "report_date": (
            body.report_date
            or now_in_timezone(user.timezone).date()
        ).isoformat(),
        "status": "agent2_unavailable",
        "completeness_score": 0,
        "missing_sections": [],
        "message": "当前 Agent2 暂时无法处理，本次没有写入任何内容，请稍后再试。",
        "confirmation_type": "none",
        "confirmed_by_user": False,
        "quality_warning": None,
        "reply_kind": "agent2_unavailable_fail_closed",
        "structured": {
            "today_work": [],
            "problems": [],
            "tomorrow_plan": [],
        },
        "merged_report": {
            "today_work": [],
            "problems": [],
            "tomorrow_plan": [],
            "section_status": {},
        },
    }
    if event is not None:
        await mark_webhook_event_processed(
            session,
            event,
            report_id=None,
            response_payload=response,
            now=now_in_timezone(user.timezone),
        )
    await session.commit()
    return response


async def _submit_manual_agent2_if_applicable(
    *,
    session: AsyncSession,
    user: Any,
    raw_input: str,
    report_date: date | None,
    llm_client: Any | None = None,
    message_id: str = "",
    conversation_id: str = "",
) -> dict[str, Any] | None:
    settings = get_settings()
    received_at = now_in_timezone(getattr(user, "timezone", None) or settings.timezone)
    entrypoint = None
    phase2_primary = False
    phase2_business_context = None
    if settings.agent2_business_phase2_enabled:
        entrypoint = await resolve_agent2_entrypoint(
            session,
            settings=settings,
            dingtalk_user_id=str(getattr(user, "dingtalk_user_id", "") or ""),
            source_message_id=message_id,
        )
        phase2_primary = entrypoint.decision.route == "agent2_primary"
        if phase2_primary and entrypoint.binding is not None:
            phase2_business_context = build_business_command_context(
                entrypoint.binding,
                source_message_id=message_id,
                source_channel="manual_text",
                occurred_at=received_at,
                conversation_id=conversation_id,
            )
    if entrypoint is not None and entrypoint.decision.route == "agent2_shadow":
        return None
    route_blocked = (
        entrypoint is not None and entrypoint.decision.route == "blocked"
    )
    if (
        not route_blocked
        and not phase2_primary
        and not _manual_should_use_agent2(settings, user)
    ):
        return None

    active_tasks = []
    daily_task = await build_live_daily_active_task(session, user, settings)
    if daily_task is not None:
        active_tasks.append(daily_task)
    envelope = IncomingMessageEnvelope(
        sender_id=str(getattr(user, "id", "") or ""),
        sender_name=str(getattr(user, "name", "") or ""),
        dingtalk_user_id=str(getattr(user, "dingtalk_user_id", "") or ""),
        source="manual_text",
        raw_text=raw_input,
        message_id=message_id,
        conversation_id=conversation_id,
        received_at=received_at,
        active_tasks=tuple(active_tasks),
    )
    shadow: DailyShadowEvaluation | None = None
    target_report_date = report_date or _daily_task_report_date(
        daily_task,
        fallback=received_at.date(),
    )
    existing = await get_report(session, user.id, target_report_date)
    if entrypoint is not None and entrypoint.decision.route == "blocked":
        return _manual_route_blocked_response(
            existing,
            target_report_date,
            message="当前账号或所属组织信息无法唯一确认，本次没有执行任何业务操作。",
            reply_kind="agent2_entrypoint_blocked",
        )
    if phase2_primary and not cognitive_core_v3_enabled(settings):
        return _manual_route_blocked_response(
            existing,
            target_report_date,
            message="当前服务暂时无法处理这条消息，本次没有执行任何业务操作。",
            reply_kind="agent2_cognitive_core_disabled",
        )
    if cognitive_core_v3_enabled(settings):
        try:
            if llm_client is None:
                raise RuntimeError("cognitive core v3 requires an LLM client")
            turn_runtime_result = await production_agent2_turn_runtime().handle(
                VerifiedTurnRequest(
                    session=session,
                    user=user,
                    envelope=envelope,
                    llm_client=llm_client,
                    daily_report=existing,
                    report_date=target_report_date,
                    settings=settings,
                    business_context=phase2_business_context,
                )
            )
            cognitive_v3 = turn_runtime_result.orchestration
        except (
            InformationContinuationBlocked,
            SelectionContinuationBlocked,
            VerifiedTurnRejected,
        ) as exc:
            safe_reply = verified_turn_rejection_reply(exc)
            logger.info(
                "manual cognitive core v3 blocked turn kind=%s",
                safe_reply.reply_kind,
            )
            return _manual_response_payload(
                report_id=str(getattr(existing, "id", "") or "") or None,
                report_date=target_report_date,
                status_text=str(getattr(existing, "status", "") or "collecting"),
                today_work=list(getattr(existing, "today_work", []) or []),
                problems=list(getattr(existing, "problems", []) or []),
                tomorrow_plan=list(getattr(existing, "tomorrow_plan", []) or []),
                section_status=dict(getattr(existing, "section_status", None) or {}),
                message=safe_reply.message,
                reply_kind=safe_reply.reply_kind,
                confirmation_type=str(getattr(existing, "confirmation_type", "") or "none"),
                confirmed_by_user=bool(getattr(existing, "confirmed_by_user", False)),
                quality_warning=getattr(existing, "quality_warning", None),
            )
        except Exception:
            logger.exception("manual cognitive core v3 failed; write path is fail-closed")
            return _manual_response_payload(
                report_id=str(getattr(existing, "id", "") or "") or None,
                report_date=target_report_date,
                status_text=str(getattr(existing, "status", "") or "collecting"),
                today_work=list(getattr(existing, "today_work", []) or []),
                problems=list(getattr(existing, "problems", []) or []),
                tomorrow_plan=list(getattr(existing, "tomorrow_plan", []) or []),
                section_status=dict(getattr(existing, "section_status", None) or {}),
                message="这条消息暂时无法完成处理，本次没有写入任何业务内容，请稍后重试。",
                reply_kind="cognitive_v3_unavailable",
                confirmation_type=str(getattr(existing, "confirmation_type", "") or "none"),
                confirmed_by_user=bool(getattr(existing, "confirmed_by_user", False)),
                quality_warning=getattr(existing, "quality_warning", None),
            )
        else:
            verified_context = turn_runtime_result.business_execution_context
            periodic_results = []
            if cognitive_v3.command_plan.report_commands:
                if verified_context is None:
                    raise RuntimeError(
                        "manual periodic Report commands require verified identity"
                    )
                periodic_results = await execute_periodic_report_commands(
                    session,
                    commands=cognitive_v3.command_plan.report_commands,
                    context=verified_context,
                    timezone_name=settings.timezone,
                    execution_authority=(
                        turn_runtime_result.mutation_execution_authority
                    ),
                )
            business_result = None
            if phase2_primary and cognitive_v3.command_plan.business_commands:
                if verified_context is None:
                    raise RuntimeError(
                        "manual Agent2 business commands require verified identity"
                    )
                executable = tuple(
                    command
                    for command in cognitive_v3.command_plan.business_commands
                    if command.command_type
                    in {
                        "record_travel_candidate",
                        "update_travel_candidate",
                        "respond_travel_collaboration_candidate",
                        "record_case_progress_candidate",
                        "update_case_progress_candidate",
                        "delete_case_progress_candidate",
                        "query_case_progress_candidate",
                        "link_case_progress_candidate",
                        "list_assigned_cases",
                        "query_operation_status",
                        "query_case_risk",
                        "update_case_followup_policy_candidate",
                        "trigger_case_followup_now_candidate",
                    }
                )
                if executable:
                    business_result = await Phase2BusinessComposer(
                        case_repository=CaseSqlRepository(session),
                        party_repository=PartySqlRepository(session),
                        progress_repository=CaseProgressSqlRepository(session),
                        followup_policy_repository=CaseFollowupPolicySqlRepository(
                            session
                        ),
                        executor=SqlBusinessExecutor(
                            session,
                            effect_policy=BusinessEffectPolicy.from_settings(
                                settings
                            ),
                            execution_authority=(
                                turn_runtime_result.mutation_execution_authority
                            ),
                        ),
                    ).execute(executable, verified_context)
            daily_result = None
            if cognitive_v3.command_plan.daily_commands:
                daily_result = await execute_typed_agent2_daily_commands(
                    session,
                    user=user,
                    commands=cognitive_v3.command_plan.daily_commands,
                    execution_context=turn_runtime_result.daily_execution_context(),
                    settings=settings,
                    execution_authority=(
                        turn_runtime_result.mutation_execution_authority
                    ),
                )
            has_selection_request = bool(
                tuple(
                    getattr(
                        cognitive_v3.decision,
                        "admission_selection_requests",
                        (),
                    )
                    or ()
                )
            )
            has_bound_pending = has_bound_confirmation_pending(
                cognitive_v3.decision
            )
            has_lifecycle_update = has_pending_lifecycle_update(
                cognitive_v3.decision
            )
            if (
                daily_result is not None
                or business_result is not None
                or periodic_results
                or has_selection_request
                or has_bound_pending
                or has_lifecycle_update
            ):
                await finalize_cognitive_core_v3_execution(
                    session=session,
                    result=cognitive_v3,
                    command_results=(
                        daily_result.command_results
                        if daily_result is not None
                        else []
                    ),
                    business_result=business_result,
                    report_results=[item.as_dict() for item in periodic_results],
                    business_context=verified_context,
                    selection_continuation=(
                        turn_runtime_result.selection_continuation
                    ),
                )
            outcomes = ()
            if daily_result is not None:
                outcomes += daily_execution_outcomes(
                    daily_result,
                    source_turn_id=message_id,
                )
            if business_result is not None:
                outcomes += business_composition_outcomes(business_result)
            outcomes += periodic_execution_outcomes(
                periodic_results,
                source_turn_id=message_id,
            )
            if outcomes and verified_context is not None:
                await persist_operation_outcomes(
                    session,
                    outcomes,
                    tenant_id=verified_context.tenant_id,
                    user_id=verified_context.actor_user_id,
                    conversation_id=verified_context.conversation_id,
                    source_turn_id=message_id,
                    now=verified_context.occurred_at,
                )
            if daily_result is not None:
                if outcomes:
                    daily_result = replace(
                        daily_result,
                        message=append_cognitive_clarification(
                            OutcomeReplyComposer().compose(outcomes),
                            cognitive_v3.decision,
                        ),
                    )
                return _agent2_result_manual_response(daily_result)
            if business_result is not None or periodic_results:
                return _manual_response_payload(
                    report_id=str(getattr(existing, "id", "") or "") or None,
                    report_date=target_report_date,
                    status_text=str(
                        getattr(existing, "status", "") or "collecting"
                    ),
                    today_work=list(getattr(existing, "today_work", []) or []),
                    problems=list(getattr(existing, "problems", []) or []),
                    tomorrow_plan=list(
                        getattr(existing, "tomorrow_plan", []) or []
                    ),
                    section_status=dict(
                        getattr(existing, "section_status", None) or {}
                    ),
                    message=append_cognitive_clarification(
                        OutcomeReplyComposer().compose(outcomes),
                        cognitive_v3.decision,
                    ),
                    reply_kind="agent2_business",
                    confirmation_type="none",
                    confirmed_by_user=False,
                    quality_warning=None,
                )
            if cognitive_v3.decision.admission_mode == "enforced":
                if verified_context is None:
                    raise RuntimeError(
                        "manual Semantic Admission Enforce requires verified identity"
                    )
                lifecycle_message = pending_lifecycle_reply(cognitive_v3.decision)
                selection_message = selection_request_reply(cognitive_v3.decision)
                if lifecycle_message:
                    read_only_message = lifecycle_message
                    read_only_reply_kind = "cognitive_v3_pending_lifecycle"
                elif selection_message:
                    read_only_message = selection_message
                    read_only_reply_kind = "cognitive_v3_selection"
                elif cognitive_v3.decision.clarification_need is not None:
                    read_only_message = (
                        cognitive_v3.decision.clarification_need.question
                    )
                    read_only_reply_kind = "cognitive_v3_clarification"
                else:
                    read_only_message = (
                        "这条消息没有形成可执行的业务操作，本次未写入任何内容。"
                    )
                    read_only_reply_kind = "agent2_read_only"
                read_only_outcomes = (
                    text_outcome(
                        read_only_message,
                        source_turn_id=message_id,
                    ),
                )
                await persist_operation_outcomes(
                    session,
                    read_only_outcomes,
                    tenant_id=verified_context.tenant_id,
                    user_id=verified_context.actor_user_id,
                    conversation_id=verified_context.conversation_id,
                    source_turn_id=message_id,
                    now=verified_context.occurred_at,
                )
                return _manual_response_payload(
                    report_id=str(getattr(existing, "id", "") or "") or None,
                    report_date=target_report_date,
                    status_text=str(
                        getattr(existing, "status", "") or "collecting"
                    ),
                    today_work=list(getattr(existing, "today_work", []) or []),
                    problems=list(getattr(existing, "problems", []) or []),
                    tomorrow_plan=list(
                        getattr(existing, "tomorrow_plan", []) or []
                    ),
                    section_status=dict(
                        getattr(existing, "section_status", None) or {}
                    ),
                    message=OutcomeReplyComposer().compose(read_only_outcomes),
                    reply_kind=read_only_reply_kind,
                    confirmation_type="none",
                    confirmed_by_user=False,
                    quality_warning=None,
                )
            shadow = evaluate_daily_shadow(envelope, mode="protective_gate")
            context_pack = await _build_manual_agent2_context_pack(
                session=session,
                user=user,
                envelope=envelope,
                shadow=shadow,
                daily_report=existing,
                settings=settings,
            )
            response = await _agent2_blocked_manual_response(
                shadow,
                existing,
                target_report_date,
                raw_input=raw_input,
                llm_client=llm_client,
                context_pack=context_pack,
            )
            lifecycle_message = pending_lifecycle_reply(cognitive_v3.decision)
            selection_message = selection_request_reply(cognitive_v3.decision)
            if lifecycle_message:
                response["message"] = lifecycle_message
                response["reply_kind"] = "cognitive_v3_pending_lifecycle"
            elif selection_message:
                response["message"] = selection_message
                response["reply_kind"] = "cognitive_v3_selection"
            elif cognitive_v3.decision.clarification_need is not None:
                response["message"] = cognitive_v3.decision.clarification_need.question
                response["reply_kind"] = "cognitive_v3_clarification"
            return response
    if shadow is None:
        shadow = evaluate_daily_shadow(envelope, mode="protective_gate")
    if shadow.gate_decision.block_legacy_daily:
        context_pack = await _build_manual_agent2_context_pack(
            session=session,
            user=user,
            envelope=envelope,
            shadow=shadow,
            daily_report=existing,
            settings=settings,
        )
        return await _agent2_blocked_manual_response(
            shadow,
            existing,
            target_report_date,
            raw_input=raw_input,
            llm_client=llm_client,
            context_pack=context_pack,
        )

    commands = list(shadow.commands)
    if not commands:
        return None
    report_for_version = existing
    result = await execute_agent2_daily_commands(
        session,
        user=user,
        raw_input=raw_input,
        source="agent2_manual_text",
        commands=commands,
        settings=settings,
        report_date=target_report_date,
        message_id=message_id,
        expected_report_version=agent2_daily_report_version(report_for_version),
    )
    if agent2_daily_should_fallback_to_legacy(result.command_results):
        return None
    return _agent2_result_manual_response(result)


async def _build_manual_agent2_context_pack(
    *,
    session: AsyncSession,
    user: Any,
    envelope: IncomingMessageEnvelope,
    shadow: DailyShadowEvaluation,
    daily_report: Any | None,
    settings: Any,
) -> Agent2ContextPack:
    try:
        user_habits = await list_active_user_habits(session, user.id)
    except Exception:
        user_habits = []
    return build_agent2_context_pack(
        envelope,
        daily_report=daily_report,
        personal_memory=build_personal_memory_profile(user=user, user_habits=user_habits),
        knowledge=await _resolve_manual_context_knowledge(
            session=session,
            user=user,
            envelope=envelope,
            shadow=shadow,
            settings=settings,
        ),
    )


async def _resolve_manual_context_knowledge(
    *,
    session: AsyncSession,
    user: Any,
    envelope: IncomingMessageEnvelope,
    shadow: DailyShadowEvaluation,
    settings: Any,
) -> tuple[Any, ...]:
    if not DEFAULT_CASE_RAG_INDEX.exists():
        return ()
    plan = getattr(shadow, "plan", None)
    intent = str(getattr(plan, "primary_workflow", "") or "")
    timezone = getattr(user, "timezone", "") or getattr(settings, "timezone", "Asia/Shanghai")
    recent_case_messages = await load_recent_case_context_messages(
        session=session,
        dingtalk_user_id=str(getattr(user, "dingtalk_user_id", "") or envelope.dingtalk_user_id or ""),
        current_message_id=str(envelope.message_id or ""),
        current_text=str(envelope.raw_text or ""),
    )
    resolution = resolve_knowledge(
        KnowledgeQuery(
            text=str(envelope.raw_text or ""),
            user_id=str(getattr(user, "id", "") or envelope.sender_id or ""),
            dingtalk_user_id=str(getattr(user, "dingtalk_user_id", "") or envelope.dingtalk_user_id or ""),
            intent=intent,
            metadata={
                "current_date": now_in_timezone(timezone).date().isoformat(),
                "recent_case_messages": recent_case_messages,
            },
        ),
        [CaseTableRagAdapter(DEFAULT_CASE_RAG_INDEX)],
    )
    return tuple(resolution.evidence)


def _manual_should_use_agent2(settings: Any, user: Any) -> bool:
    # The trusted Settings allowlist is the sole authority for the legacy
    # daily Agent2 gate; request payload never reaches this decision.
    return agent2_daily_enabled_for_user(settings, user)


def _daily_task_report_date(task: Any, *, fallback: date) -> date:
    """Use the server-resolved Daily task date as the implicit write target."""

    if str(getattr(task, "workflow", "") or "") != WORKFLOW_DAILY_REPORT:
        return fallback
    metadata = getattr(task, "metadata", None)
    if not isinstance(metadata, Mapping):
        return fallback
    raw_report_date = str(metadata.get("report_date") or "").strip()
    try:
        return date.fromisoformat(raw_report_date)
    except ValueError:
        return fallback


async def _agent2_blocked_manual_response(
    shadow: DailyShadowEvaluation,
    report: Any | None,
    report_date: date,
    *,
    raw_input: str,
    llm_client: Any | None,
    context_pack: Agent2ContextPack | None,
) -> dict[str, Any]:
    today_work = list(getattr(report, "today_work", []) or [])
    problems = list(getattr(report, "problems", []) or [])
    tomorrow_plan = list(getattr(report, "tomorrow_plan", []) or [])
    status_text = str(getattr(report, "status", "") or "collecting")
    section_status = dict(getattr(report, "section_status", None) or {})
    reply = shadow.assistant_reply.text if shadow.assistant_reply is not None else ""
    if shadow.assistant_reply is not None and llm_client is not None:
        tool_reply = await build_tool_assisted_reply(
            raw_text=raw_input,
            assistant_reply=shadow.assistant_reply,
            llm_client=llm_client,
            context_pack=context_pack,
        )
        reply = tool_reply.text
    message = reply or shadow.gate_decision.reply_text or "这句我先不写入日报。"
    reply_kind = (
        shadow.assistant_reply.reply_type
        if shadow.assistant_reply is not None
        else shadow.gate_decision.reply_type or "agent2_blocked"
    )
    return _manual_response_payload(
        report_id=str(getattr(report, "id", "") or "") or None,
        report_date=report_date,
        status_text=status_text,
        today_work=today_work,
        problems=problems,
        tomorrow_plan=tomorrow_plan,
        section_status=section_status,
        message=message,
        reply_kind=reply_kind,
        confirmation_type=str(getattr(report, "confirmation_type", "") or "none"),
        confirmed_by_user=bool(getattr(report, "confirmed_by_user", False)),
        quality_warning=getattr(report, "quality_warning", None),
    )


def _manual_route_blocked_response(
    report: Any | None,
    report_date: date,
    *,
    message: str,
    reply_kind: str,
) -> dict[str, Any]:
    return _manual_response_payload(
        report_id=str(getattr(report, "id", "") or "") or None,
        report_date=report_date,
        status_text=str(getattr(report, "status", "") or "collecting"),
        today_work=list(getattr(report, "today_work", []) or []),
        problems=list(getattr(report, "problems", []) or []),
        tomorrow_plan=list(getattr(report, "tomorrow_plan", []) or []),
        section_status=dict(getattr(report, "section_status", None) or {}),
        message=message,
        reply_kind=reply_kind,
        confirmation_type=str(getattr(report, "confirmation_type", "") or "none"),
        confirmed_by_user=bool(getattr(report, "confirmed_by_user", False)),
        quality_warning=getattr(report, "quality_warning", None),
    )


def _agent2_result_manual_response(result: Agent2DailyExecutionResult) -> dict[str, Any]:
    return _manual_response_payload(
        report_id=result.report_id,
        report_date=result.report_date,
        status_text=result.status,
        today_work=list(result.today_work or []),
        problems=list(result.problems or []),
        tomorrow_plan=list(result.tomorrow_plan or []),
        section_status={},
        message=result.message,
        reply_kind="agent2_read_only" if result.read_only else "agent2_daily",
        confirmation_type="none",
        confirmed_by_user=False,
        quality_warning=None,
    )


def _manual_response_payload(
    *,
    report_id: str | None,
    report_date: date,
    status_text: str,
    today_work: list[str],
    problems: list[str],
    tomorrow_plan: list[str],
    section_status: dict[str, Any],
    message: str,
    reply_kind: str,
    confirmation_type: str,
    confirmed_by_user: bool,
    quality_warning: str | None,
) -> dict[str, Any]:
    completeness_score = _manual_completeness(today_work, problems, tomorrow_plan)
    structured = StructuredDailyReport(
        today_work=today_work,
        problems=problems,
        tomorrow_plan=tomorrow_plan,
        completeness=completeness_score,
    )
    return {
        "report_id": report_id,
        "report_date": report_date.isoformat(),
        "status": status_text,
        "completeness_score": completeness_score,
        "missing_sections": _manual_missing_sections(today_work, problems, tomorrow_plan),
        "message": message,
        "confirmation_type": confirmation_type,
        "confirmed_by_user": confirmed_by_user,
        "quality_warning": quality_warning,
        "reply_kind": reply_kind,
        "structured": structured.model_dump(),
        "merged_report": {
            "today_work": today_work,
            "problems": problems,
            "tomorrow_plan": tomorrow_plan,
            "section_status": section_status,
        },
    }


def _manual_completeness(today_work: list[str], problems: list[str], tomorrow_plan: list[str]) -> float:
    filled = sum(1 for values in (today_work, problems, tomorrow_plan) if values)
    return round(filled / 3, 2)


def _manual_missing_sections(today_work: list[str], problems: list[str], tomorrow_plan: list[str]) -> list[str]:
    missing = []
    if not today_work:
        missing.append("today_work")
    if not problems:
        missing.append("problems")
    if not tomorrow_plan:
        missing.append("tomorrow_plan")
    return missing


@router.get("")
async def get_reports(report_date: date, session: AsyncSession = Depends(get_session)) -> list[dict]:
    reports = await list_reports_for_date(session, report_date)
    return [
        {
            "id": str(report.id),
            "user_id": str(report.user_id),
            "team_id": str(report.team_id),
            "date": report.report_date.isoformat(),
            "today_work": report.today_work,
            "problems": report.problems,
            "tomorrow_plan": report.tomorrow_plan,
            "raw_input": report.raw_input,
            "completeness_score": float(report.completeness_score),
            "status": report.status,
            "confirmation_type": report.confirmation_type,
            "confirmed_by_user": report.confirmed_by_user,
            "quality_warning": report.quality_warning,
        }
        for report in reports
    ]
