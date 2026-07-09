from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.agent2.context_pack import Agent2ContextPack, build_agent2_context_pack
from app.agent2.harness.schemas import HarnessCase
from app.agent2.knowledge_resolver import (
    InMemoryDailyReportHistoryAdapter,
    InMemoryOrgDirectoryAdapter,
    KnowledgeAdapter,
    KnowledgeQuery,
    resolve_knowledge,
)
from app.workflows.intake import ActiveWorkflowTask, IncomingMessageEnvelope


@dataclass(frozen=True)
class HarnessContextPackResult:
    context_pack: Agent2ContextPack
    knowledge_warnings: tuple[str, ...] = ()


def build_envelope(case: HarnessCase) -> IncomingMessageEnvelope:
    context = case.context
    return IncomingMessageEnvelope(
        sender_id=context.user_id,
        sender_name=context.sender_name,
        dingtalk_user_id=context.dingtalk_user_id,
        source=context.source,
        raw_text=case.text,
        message_id=case.case_id,
        conversation_id=context.channel,
        received_at=context.message_time,
        active_tasks=tuple(_active_task_from_mapping(task) for task in context.active_tasks),
        recent_state=dict(context.recent_state),
    )


def build_context_pack(case: HarnessCase, *, envelope: IncomingMessageEnvelope | None = None) -> HarnessContextPackResult:
    envelope = envelope or build_envelope(case)
    adapters = _knowledge_adapters(case)
    resolution = (
        resolve_knowledge(
            KnowledgeQuery(
                text=case.text,
                user_id=case.context.user_id,
                dingtalk_user_id=case.context.dingtalk_user_id,
                intent=str(case.context.recent_state.get("intent") or ""),
                metadata=_knowledge_metadata(case),
            ),
            adapters,
        )
        if adapters
        else None
    )
    context_pack = build_agent2_context_pack(
        envelope,
        daily_report=_current_daily_report(case),
        knowledge=resolution.evidence if resolution is not None else (),
    )
    return HarnessContextPackResult(
        context_pack=context_pack,
        knowledge_warnings=resolution.warnings if resolution is not None else (),
    )


def _active_task_from_mapping(value: dict[str, Any]) -> ActiveWorkflowTask:
    return ActiveWorkflowTask(
        workflow=str(value.get("workflow") or ""),
        task_id=str(value.get("task_id") or ""),
        status=str(value.get("status") or ""),
        reply_candidate=bool(value.get("reply_candidate") or False),
        awaiting_confirmation=bool(value.get("awaiting_confirmation") or False),
        reason=str(value.get("reason") or ""),
        metadata=dict(value.get("metadata") or {}),
    )


def _knowledge_adapters(case: HarnessCase) -> list[KnowledgeAdapter]:
    context = case.context
    adapters: list[KnowledgeAdapter] = []
    if context.org_users or context.org_teams:
        adapters.append(InMemoryOrgDirectoryAdapter(users=context.org_users, teams=context.org_teams))
    if context.daily_history:
        adapters.append(InMemoryDailyReportHistoryAdapter(context.daily_history))
    return adapters


def _knowledge_metadata(case: HarnessCase) -> dict[str, Any]:
    context = case.context
    metadata = dict(context.recent_state)
    if context.current_date:
        metadata["current_date"] = context.current_date
    elif context.message_time:
        metadata["current_date"] = context.message_time.date().isoformat()
    elif _current_daily_report(case).get("report_date"):
        metadata["current_date"] = _current_daily_report(case).get("report_date")
    return metadata


def _current_daily_report(case: HarnessCase) -> dict[str, Any]:
    value = case.context.current_daily_report
    return dict(value or {}) if isinstance(value, dict) else {}
