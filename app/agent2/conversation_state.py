from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.agent2.selection_pending import SelectionPending


@dataclass(frozen=True)
class ConversationGoal:
    intent: str
    entity_ids: tuple[str, ...] = ()
    source_context_id: str = ""


@dataclass(frozen=True)
class ConversationEntity:
    entity_id: str
    entity_type: str
    value: str
    confidence: float
    attributes: dict[str, Any] = field(default_factory=dict)
    source_context_id: str = ""

    def __post_init__(self) -> None:
        if not self.entity_id or not self.entity_type or not self.value:
            raise ValueError("conversation entity requires id, type, and value")
        if not 0 <= self.confidence <= 1:
            raise ValueError("conversation entity confidence must be between 0 and 1")


@dataclass(frozen=True)
class RecentContextFrame:
    context_id: str
    message_id: str
    intents: tuple[str, ...]
    entity_ids: tuple[str, ...]
    summary: str
    occurred_at: datetime


@dataclass(frozen=True)
class BoundPending:
    pending_id: str
    user_id: str
    conversation_id: str
    intent: str
    action: str
    entity_ids: tuple[str, ...]
    context_id: str
    created_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        required = (
            self.pending_id,
            self.user_id,
            self.conversation_id,
            self.intent,
            self.action,
            self.context_id,
        )
        if not all(str(value or "").strip() for value in required):
            raise ValueError("pending must bind intent, entity context, action, user, and conversation")
        if not self.entity_ids:
            raise ValueError("pending must bind at least one entity")
        if self.expires_at <= self.created_at:
            raise ValueError("pending expiry must be after creation")

    def is_active_at(self, now: datetime) -> bool:
        return now < self.expires_at


@dataclass(frozen=True)
class UserConstraints:
    no_daily_write: bool = False
    read_only: bool = False
    no_history_mutation: bool = False
    draft_only: bool = False
    sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConversationState:
    user_id: str
    conversation_id: str
    version: int = 0
    current_goal: ConversationGoal | None = None
    goal_stack: tuple[ConversationGoal, ...] = ()
    current_entities: tuple[ConversationEntity, ...] = ()
    recent_context: tuple[RecentContextFrame, ...] = ()
    pending: tuple[BoundPending, ...] = ()
    selection_pending: tuple[SelectionPending, ...] = ()
    user_constraints: UserConstraints = field(default_factory=UserConstraints)

    def __post_init__(self) -> None:
        if not self.user_id or not self.conversation_id or self.version < 0:
            raise ValueError("conversation state requires identity and non-negative version")
        pending_ids: set[str] = set()
        for item in self.pending:
            if item.user_id != self.user_id or item.conversation_id != self.conversation_id:
                raise ValueError("pending does not belong to conversation state")
            if item.pending_id in pending_ids:
                raise ValueError("conversation state contains duplicate pending ids")
            pending_ids.add(item.pending_id)
        selection_ids: set[str] = set()
        for item in self.selection_pending:
            if item.conversation_id != self.conversation_id:
                raise ValueError("selection pending does not belong to conversation state")
            if item.pending_id in selection_ids:
                raise ValueError("conversation state contains duplicate selection pending ids")
            selection_ids.add(item.pending_id)

    @classmethod
    def empty(cls, *, user_id: str, conversation_id: str) -> "ConversationState":
        if not str(user_id or "").strip() or not str(conversation_id or "").strip():
            raise ValueError("conversation state requires user and conversation ids")
        return cls(user_id=user_id, conversation_id=conversation_id)

    def active_pending(self, now: datetime) -> tuple[BoundPending, ...]:
        return tuple(item for item in self.pending if item.is_active_at(now))

    def as_payload(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
            "version": self.version,
            "current_goal": (
                {
                    "intent": self.current_goal.intent,
                    "entity_ids": list(self.current_goal.entity_ids),
                    "source_context_id": self.current_goal.source_context_id,
                }
                if self.current_goal is not None
                else None
            ),
            "goal_stack": [
                {
                    "intent": goal.intent,
                    "entity_ids": list(goal.entity_ids),
                    "source_context_id": goal.source_context_id,
                }
                for goal in self.goal_stack
            ],
            "current_entities": [
                {
                    "entity_id": item.entity_id,
                    "entity_type": item.entity_type,
                    "value": item.value,
                    "confidence": item.confidence,
                    "attributes": dict(item.attributes),
                    "source_context_id": item.source_context_id,
                }
                for item in self.current_entities
            ],
            "recent_context": [
                {
                    "context_id": item.context_id,
                    "message_id": item.message_id,
                    "intents": list(item.intents),
                    "entity_ids": list(item.entity_ids),
                    "summary": item.summary,
                    "occurred_at": item.occurred_at.isoformat(),
                }
                for item in self.recent_context
            ],
            "pending": [
                {
                    "pending_id": item.pending_id,
                    "user_id": item.user_id,
                    "conversation_id": item.conversation_id,
                    "intent": item.intent,
                    "action": item.action,
                    "entity_ids": list(item.entity_ids),
                    "context_id": item.context_id,
                    "created_at": item.created_at.isoformat(),
                    "expires_at": item.expires_at.isoformat(),
                }
                for item in self.pending
            ],
            "selection_pending": [item.as_dict() for item in self.selection_pending],
            "user_constraints": {
                "no_daily_write": self.user_constraints.no_daily_write,
                "read_only": self.user_constraints.read_only,
                "no_history_mutation": self.user_constraints.no_history_mutation,
                "draft_only": self.user_constraints.draft_only,
                "sources": list(self.user_constraints.sources),
            },
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ConversationState":
        if not isinstance(payload, dict):
            raise ValueError("conversation state payload must be an object")
        goal_raw = payload.get("current_goal")
        goal = None
        if isinstance(goal_raw, dict):
            goal = ConversationGoal(
                intent=str(goal_raw.get("intent") or "").strip(),
                entity_ids=_string_tuple(goal_raw.get("entity_ids")),
                source_context_id=str(goal_raw.get("source_context_id") or "").strip(),
            )
        entities = tuple(
            ConversationEntity(
                entity_id=str(item.get("entity_id") or "").strip(),
                entity_type=str(item.get("entity_type") or "").strip(),
                value=str(item.get("value") or "").strip(),
                confidence=float(item.get("confidence", 0)),
                attributes=dict(item.get("attributes") or {}),
                source_context_id=str(item.get("source_context_id") or "").strip(),
            )
            for item in _dict_list(payload.get("current_entities"))
        )
        recent = tuple(
            RecentContextFrame(
                context_id=str(item.get("context_id") or "").strip(),
                message_id=str(item.get("message_id") or "").strip(),
                intents=_string_tuple(item.get("intents")),
                entity_ids=_string_tuple(item.get("entity_ids")),
                summary=str(item.get("summary") or "").strip(),
                occurred_at=_parse_datetime(item.get("occurred_at")),
            )
            for item in _dict_list(payload.get("recent_context"))
        )
        pending = tuple(
            BoundPending(
                pending_id=str(item.get("pending_id") or "").strip(),
                user_id=str(item.get("user_id") or "").strip(),
                conversation_id=str(item.get("conversation_id") or "").strip(),
                intent=str(item.get("intent") or "").strip(),
                action=str(item.get("action") or "").strip(),
                entity_ids=_string_tuple(item.get("entity_ids")),
                context_id=str(item.get("context_id") or "").strip(),
                created_at=_parse_datetime(item.get("created_at")),
                expires_at=_parse_datetime(item.get("expires_at")),
            )
            for item in _dict_list(payload.get("pending"))
        )
        constraints_raw = payload.get("user_constraints")
        constraints_raw = constraints_raw if isinstance(constraints_raw, dict) else {}
        return cls(
            user_id=str(payload.get("user_id") or "").strip(),
            conversation_id=str(payload.get("conversation_id") or "").strip(),
            version=int(payload.get("version", 0)),
            current_goal=goal,
            goal_stack=tuple(
                ConversationGoal(
                    intent=str(item.get("intent") or "").strip(),
                    entity_ids=_string_tuple(item.get("entity_ids")),
                    source_context_id=str(item.get("source_context_id") or "").strip(),
                )
                for item in _dict_list(payload.get("goal_stack"))
                if str(item.get("intent") or "").strip()
            ),
            current_entities=entities,
            recent_context=recent,
            pending=pending,
            selection_pending=tuple(
                SelectionPending.from_dict(item)
                for item in _dict_list(payload.get("selection_pending"))
            ),
            user_constraints=UserConstraints(
                no_daily_write=bool(constraints_raw.get("no_daily_write", False)),
                read_only=bool(constraints_raw.get("read_only", False)),
                no_history_mutation=bool(constraints_raw.get("no_history_mutation", False)),
                draft_only=bool(constraints_raw.get("draft_only", False)),
                sources=_string_tuple(constraints_raw.get("sources")),
            ),
        )


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    if any(not isinstance(item, dict) for item in value):
        raise ValueError("conversation state collection items must be objects")
    return list(value)


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value or ""))
    if result.tzinfo is None:
        raise ValueError("conversation state datetimes must be timezone-aware")
    return result
