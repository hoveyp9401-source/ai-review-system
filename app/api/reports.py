from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent2.assistant_tools import build_tool_assisted_reply
from app.agent2.case_table_rag import CaseTableRagAdapter, DEFAULT_CASE_RAG_INDEX
from app.agent2.context_pack import Agent2ContextPack, build_agent2_context_pack
from app.agent2.daily_execution import (
    Agent2DailyExecutionResult,
    agent2_daily_enabled_for_user,
    agent2_daily_should_fallback_to_legacy,
    execute_agent2_daily_commands,
)
from app.agent2.daily_shadow import DailyShadowEvaluation, evaluate_daily_shadow
from app.agent2.knowledge_resolver import KnowledgeQuery, resolve_knowledge
from app.agent2.personal_memory import build_personal_memory_profile
from app.agent2.recent_context import load_recent_case_context_messages
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
from app.workflows.intake import IncomingMessageEnvelope

router = APIRouter(prefix="/reports", tags=["reports"])


class ManualReportRequest(BaseModel):
    dingtalk_user_id: str = Field(min_length=1)
    raw_input: str = Field(min_length=1)
    source: str = "manual_text"
    report_date: date | None = None
    idempotency_key: str | None = None


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
        source=body.source,
        report_date=body.report_date,
        llm_client=llm_client,
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

    try:
        result = await service.submit_text(
            session,
            user=user,
            raw_input=body.raw_input,
            source=body.source,
            report_date=body.report_date,
        )
    except Exception as exc:
        await session.rollback()
        if event is not None:
            async with session.begin():
                event = await session.merge(event)
                await mark_webhook_event_failed(
                    session,
                    event,
                    error_message=str(exc),
                    response_payload={"status": "failed", "message": "Report parsing failed; data was not saved."},
                    now=now_in_timezone(user.timezone),
                )
        raise
    response = {
        "report_id": result.report_id,
        "report_date": result.report_date.isoformat(),
        "status": result.status,
        "completeness_score": result.completeness_score,
        "missing_sections": result.missing_sections,
        "message": result.message,
        "confirmation_type": result.confirmation_type,
        "confirmed_by_user": result.confirmed_by_user,
        "quality_warning": result.quality_warning,
        "reply_kind": result.reply_kind,
        "structured": result.structured.model_dump(),
        "merged_report": {
            "today_work": result.today_work,
            "problems": result.problems,
            "tomorrow_plan": result.tomorrow_plan,
            "section_status": result.section_status,
        },
    }
    if event is not None:
        await mark_webhook_event_processed(
            session,
            event,
            report_id=uuid.UUID(result.report_id) if result.report_id else None,
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
    source: str,
    report_date: date | None,
    llm_client: Any | None = None,
) -> dict[str, Any] | None:
    settings = get_settings()
    if not _manual_should_use_agent2(settings, user, source):
        return None

    received_at = now_in_timezone(getattr(user, "timezone", None) or settings.timezone)
    active_tasks = []
    daily_task = await build_live_daily_active_task(session, user, settings)
    if daily_task is not None:
        active_tasks.append(daily_task)
    envelope = IncomingMessageEnvelope(
        sender_id=str(getattr(user, "id", "") or ""),
        sender_name=str(getattr(user, "name", "") or ""),
        dingtalk_user_id=str(getattr(user, "dingtalk_user_id", "") or ""),
        source=source,
        raw_text=raw_input,
        received_at=received_at,
        active_tasks=tuple(active_tasks),
    )
    shadow = evaluate_daily_shadow(envelope, mode="protective_gate")
    if shadow.gate_decision.block_legacy_daily:
        existing = await get_report(session, user.id, report_date or received_at.date())
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
            report_date or received_at.date(),
            raw_input=raw_input,
            llm_client=llm_client,
            context_pack=context_pack,
        )

    commands = list(shadow.commands)
    if not commands:
        return None
    result = await execute_agent2_daily_commands(
        session,
        user=user,
        raw_input=raw_input,
        source=f"agent2_{source or 'manual_text'}",
        commands=commands,
        settings=settings,
        report_date=report_date,
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


def _manual_should_use_agent2(settings: Any, user: Any, source: str) -> bool:
    source_text = str(source or "").strip().lower()
    if any(marker in source_text for marker in ("legacy", "agent1", "disable_agent2")):
        return False
    if "agent2" in source_text:
        return True
    return agent2_daily_enabled_for_user(settings, user)


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
