from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.agent2.daily_state import (
    DRAFT_ITEM_IDS_KEY,
    FIELD_LABELS,
    PENDING_DAILY_CANDIDATE_KEY,
    REPORT_FIELD_ORDER,
    item_reference_from_payload,
    pending_keys,
)
from app.agent2.personal_memory import PersonalMemoryProfile
from app.workflows.intake import ActiveWorkflowTask, IncomingMessageEnvelope


@dataclass(frozen=True)
class UserContextFrame:
    user_id: str
    dingtalk_user_id: str
    name: str

    def as_payload(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "dingtalk_user_id": self.dingtalk_user_id,
            "name": self.name,
        }


@dataclass(frozen=True)
class MessageContextFrame:
    text: str
    source: str
    message_id: str = ""
    conversation_id: str = ""
    received_at: datetime | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "source": self.source,
            "message_id": self.message_id,
            "conversation_id": self.conversation_id,
            "received_at": self.received_at.isoformat() if self.received_at else "",
        }


@dataclass(frozen=True)
class TaskContextFrame:
    workflow: str
    task_id: str = ""
    status: str = ""
    reply_candidate: bool = False
    awaiting_confirmation: bool = False
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_task(cls, task: ActiveWorkflowTask) -> "TaskContextFrame":
        return cls(
            workflow=str(task.workflow or ""),
            task_id=str(task.task_id or ""),
            status=str(task.status or ""),
            reply_candidate=bool(task.reply_candidate),
            awaiting_confirmation=bool(task.awaiting_confirmation),
            reason=str(task.reason or ""),
            metadata=dict(task.metadata or {}),
        )

    def as_payload(self) -> dict[str, Any]:
        return {
            "workflow": self.workflow,
            "task_id": self.task_id,
            "status": self.status,
            "reply_candidate": self.reply_candidate,
            "awaiting_confirmation": self.awaiting_confirmation,
            "reason": self.reason,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class DailyDraftItemFrame:
    field: str
    field_label: str
    field_index: int
    global_index: int
    item_id: str
    text: str

    def as_payload(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "field_label": self.field_label,
            "field_index": self.field_index,
            "global_index": self.global_index,
            "item_id": self.item_id,
            "text": self.text,
        }


@dataclass(frozen=True)
class DailyDraftFrame:
    report_id: str = ""
    report_date: str = ""
    status: str = ""
    items: tuple[DailyDraftItemFrame, ...] = ()
    pending_keys: tuple[str, ...] = ()

    def items_for_field(self, field: str) -> tuple[DailyDraftItemFrame, ...]:
        return tuple(item for item in self.items if item.field == field)

    def as_payload(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "report_date": self.report_date,
            "status": self.status,
            "pending_keys": list(self.pending_keys),
            "items": [item.as_payload() for item in self.items],
            "items_by_field": {
                field: [item.as_payload() for item in self.items_for_field(field)]
                for field in REPORT_FIELD_ORDER
            },
        }


@dataclass(frozen=True)
class RecentActionFrame:
    action_type: str
    field: str = ""
    item_index: int = 0
    item_id: str = ""
    item_ids: tuple[str, ...] = ()
    text: str = ""
    source: str = ""

    def as_payload(self) -> dict[str, Any]:
        return {
            "action_type": self.action_type,
            "field": self.field,
            "item_index": self.item_index,
            "item_id": self.item_id,
            "item_ids": list(self.item_ids),
            "text": self.text,
            "source": self.source,
        }


@dataclass(frozen=True)
class KnowledgeEvidenceFrame:
    source_type: str
    source_id: str
    title: str
    summary: str
    facts: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    freshness: str = ""

    def __post_init__(self) -> None:
        if not str(self.source_type or "").strip():
            raise ValueError("knowledge evidence requires source_type")
        if not str(self.source_id or "").strip():
            raise ValueError("knowledge evidence requires source_id")
        if not str(self.summary or "").strip():
            raise ValueError("knowledge evidence requires summary")
        if self.confidence < 0 or self.confidence > 1:
            raise ValueError("knowledge evidence confidence must be between 0 and 1")

    def as_payload(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "source_id": self.source_id,
            "title": self.title,
            "summary": self.summary,
            "facts": self.facts,
            "confidence": self.confidence,
            "freshness": self.freshness,
        }


@dataclass(frozen=True)
class AssistantReplyFrame:
    text: str
    reply_type: str = ""
    source: str = ""

    def as_payload(self) -> dict[str, Any]:
        return {
            "reply_type": self.reply_type,
            "source": self.source,
            "text": self.text,
        }


@dataclass(frozen=True)
class Agent2ContextPack:
    user: UserContextFrame
    message: MessageContextFrame
    active_tasks: tuple[TaskContextFrame, ...] = ()
    personal_memory: PersonalMemoryProfile | None = None
    daily_draft: DailyDraftFrame | None = None
    recent_actions: tuple[RecentActionFrame, ...] = ()
    knowledge: tuple[KnowledgeEvidenceFrame, ...] = ()
    recent_assistant_replies: tuple[AssistantReplyFrame, ...] = ()

    def as_payload(self, *, max_knowledge: int = 8, max_recent_replies: int = 3) -> dict[str, Any]:
        return {
            "user": self.user.as_payload(),
            "message": self.message.as_payload(),
            "active_tasks": [task.as_payload() for task in self.active_tasks],
            "personal_memory": self.personal_memory.as_payload() if self.personal_memory else None,
            "daily_draft": self.daily_draft.as_payload() if self.daily_draft else None,
            "recent_actions": [action.as_payload() for action in self.recent_actions],
            "knowledge": [item.as_payload() for item in self.knowledge[:max_knowledge]],
            "knowledge_status": "available" if self.knowledge else "not_retrieved_or_no_match",
            "recent_assistant_replies": [
                reply.as_payload()
                for reply in self.recent_assistant_replies[:max_recent_replies]
            ],
        }


def build_agent2_context_pack(
    envelope: IncomingMessageEnvelope,
    *,
    daily_report: Any | None = None,
    personal_memory: PersonalMemoryProfile | None = None,
    knowledge: tuple[KnowledgeEvidenceFrame, ...] | list[KnowledgeEvidenceFrame] = (),
    recent_assistant_replies: tuple[AssistantReplyFrame, ...] | list[AssistantReplyFrame] = (),
) -> Agent2ContextPack:
    return Agent2ContextPack(
        user=UserContextFrame(
            user_id=str(envelope.sender_id or ""),
            dingtalk_user_id=str(envelope.dingtalk_user_id or ""),
            name=str(envelope.sender_name or ""),
        ),
        message=MessageContextFrame(
            text=str(envelope.raw_text or ""),
            source=str(envelope.source or ""),
            message_id=str(envelope.message_id or ""),
            conversation_id=str(envelope.conversation_id or ""),
            received_at=envelope.received_at,
        ),
        active_tasks=tuple(TaskContextFrame.from_task(task) for task in envelope.active_tasks),
        personal_memory=personal_memory,
        daily_draft=_daily_draft_frame(daily_report) if daily_report is not None else None,
        recent_actions=_recent_actions_from_report(daily_report),
        knowledge=tuple(knowledge or ()),
        recent_assistant_replies=tuple(recent_assistant_replies or ()),
    )


def _daily_draft_frame(report: Any) -> DailyDraftFrame:
    section_status = _section_status(report)
    item_ids = _draft_item_ids(section_status)
    items: list[DailyDraftItemFrame] = []
    global_index = 1
    for field in REPORT_FIELD_ORDER:
        values = _clean_items(getattr(report, field, []) if not isinstance(report, dict) else report.get(field, []))
        ids = list(item_ids.get(field, []))
        for field_index, text in enumerate(values, start=1):
            item_id = ids[field_index - 1] if field_index - 1 < len(ids) else f"{field}:{field_index}"
            items.append(
                DailyDraftItemFrame(
                    field=field,
                    field_label=FIELD_LABELS[field],
                    field_index=field_index,
                    global_index=global_index,
                    item_id=item_id,
                    text=text,
                )
            )
            global_index += 1
    report_date = _value(report, "report_date")
    return DailyDraftFrame(
        report_id=str(_value(report, "id") or _value(report, "report_id") or ""),
        report_date=report_date.isoformat() if hasattr(report_date, "isoformat") else str(report_date or ""),
        status=str(_value(report, "status") or ""),
        items=tuple(items),
        pending_keys=tuple(_pending_keys(section_status)),
    )


def _recent_actions_from_report(report: Any | None) -> tuple[RecentActionFrame, ...]:
    if report is None:
        return ()
    section_status = _section_status(report)
    actions: list[RecentActionFrame] = []
    modified = item_reference_from_payload(section_status.get("_agent2_last_modified_item") or section_status.get("_last_modified_item"))
    if modified is not None:
        actions.append(
            RecentActionFrame(
                action_type="last_modified_item",
                field=modified.field,
                item_index=modified.item_index,
                item_id=modified.item_id,
                text=modified.text,
                source=modified.source,
            )
        )
    deleted = section_status.get("_agent2_last_deleted_item")
    if isinstance(deleted, dict):
        actions.append(
            RecentActionFrame(
                action_type="last_deleted_item",
                field=str(deleted.get("field") or deleted.get("section") or ""),
                item_ids=tuple(str(value) for value in deleted.get("item_ids", []) if str(value).strip())
                if isinstance(deleted.get("item_ids"), list)
                else (),
                source=str(deleted.get("source") or ""),
            )
        )
    pending_action = section_status.get("_pending_action")
    if pending_action:
        actions.append(RecentActionFrame(action_type="pending_action", text=str(pending_action)))
    pending_candidate = item_reference_from_payload(
        section_status.get(PENDING_DAILY_CANDIDATE_KEY),
        source_fallback="agent2_candidate",
    )
    if pending_candidate is not None and pending_candidate.text:
        actions.append(
            RecentActionFrame(
                action_type="pending_daily_candidate",
                field=pending_candidate.field,
                item_index=pending_candidate.item_index,
                item_id=pending_candidate.item_id,
                text=pending_candidate.text,
                source=pending_candidate.source,
            )
        )
    return tuple(actions)


def _section_status(report: Any) -> dict[str, Any]:
    raw = _value(report, "section_status")
    return dict(raw or {}) if isinstance(raw, dict) else {}


def _draft_item_ids(section_status: dict[str, Any]) -> dict[str, list[str]]:
    raw = section_status.get(DRAFT_ITEM_IDS_KEY)
    if not isinstance(raw, dict):
        return {}
    result: dict[str, list[str]] = {}
    for field in REPORT_FIELD_ORDER:
        values = raw.get(field)
        if isinstance(values, list):
            result[field] = [str(value) for value in values if str(value).strip()]
    return result


def _pending_keys(section_status: dict[str, Any]) -> list[str]:
    return pending_keys(section_status)


def _value(source: Any, key: str) -> Any:
    if isinstance(source, dict):
        return source.get(key)
    return getattr(source, key, None)


def _clean_items(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(value).strip() for value in values if str(value or "").strip()]


def _int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
