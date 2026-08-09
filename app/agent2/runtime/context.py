from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
from types import MappingProxyType
from typing import Any, Literal, Mapping, Protocol
from uuid import UUID

from app.agent2.command_planner_v3 import CommandPlanningContext
from app.agent2.cognitive_core_v3 import CognitiveTurn
from app.agent2.conversation_state import (
    BoundPending,
    ConversationEntity,
    ConversationGoal,
    ConversationState,
    RecentContextFrame,
)
from app.agent2.conversation_state_store import (
    ConversationStateStore,
    InMemoryConversationStateStore,
)
from app.agent2.typed_daily_commands import DailyReportMutationSnapshot

from .contracts import RuntimeTurnRequest


@dataclass(frozen=True)
class DailySnapshotQuery:
    tenant_id: str
    actor_id: UUID
    conversation_id: str
    message_id: str
    occurred_at: datetime
    channel: str


class DailySnapshotProvider(Protocol):
    async def load_daily_snapshot(self, query: DailySnapshotQuery) -> DailyReportMutationSnapshot | None: ...


@dataclass(frozen=True)
class RuntimeContext:
    request: RuntimeTurnRequest
    conversation_state: ConversationState
    current_goal: ConversationGoal | None
    entities: tuple[ConversationEntity, ...]
    recent_context: tuple[RecentContextFrame, ...]
    active_pending: tuple[BoundPending, ...]
    user_identity: Mapping[str, Any]
    request_metadata: Mapping[str, Any]
    cognitive_turn: CognitiveTurn
    planning_context: CommandPlanningContext
    manifest: Mapping[str, Any]


class ContextAssembler(Protocol):
    @property
    def checkpoint_scope(self) -> Literal["ephemeral", "persistent"]: ...

    async def assemble(self, request: RuntimeTurnRequest) -> RuntimeContext: ...

    async def checkpoint_replay_state(
        self,
        state: ConversationState,
        *,
        expected_version: int,
    ) -> ConversationState: ...


class MvpContextAssembler:
    """Phase-1 Context Assembly.

    It loads one Conversation State snapshot and the Daily planning snapshot.
    It deliberately does not load personal memory, RAG, tools, or preferences.
    """

    _ALLOWED_METADATA_KEYS = frozenset({"source", "external_message_id", "transport", "traceparent"})

    def __init__(
        self,
        *,
        state_store: ConversationStateStore,
        daily_snapshot_provider: DailySnapshotProvider,
        daily_policy: Mapping[str, Any] | None = None,
        active_tasks: tuple[Mapping[str, Any], ...] = (),
    ) -> None:
        self._state_store = state_store
        self._daily_snapshot_provider = daily_snapshot_provider
        self._daily_policy = MappingProxyType(dict(daily_policy or {}))
        self._active_tasks = tuple(MappingProxyType(dict(item)) for item in active_tasks)
        self._checkpoint_scope: Literal["ephemeral", "persistent"] = (
            "ephemeral" if isinstance(state_store, InMemoryConversationStateStore) else "persistent"
        )

    @property
    def checkpoint_scope(self) -> Literal["ephemeral", "persistent"]:
        return self._checkpoint_scope

    async def checkpoint_replay_state(
        self,
        state: ConversationState,
        *,
        expected_version: int,
    ) -> ConversationState:
        if self._checkpoint_scope != "ephemeral":
            raise RuntimeError("replay state checkpoint requires an isolated ephemeral store")
        return await self._state_store.save(state, expected_version=expected_version)

    async def assemble(self, request: RuntimeTurnRequest) -> RuntimeContext:
        user_id = str(request.actor.actor_id)
        state = await self._state_store.load(user_id=user_id, conversation_id=request.conversation_id)
        snapshot = await self._daily_snapshot_provider.load_daily_snapshot(
            DailySnapshotQuery(
                tenant_id=request.tenant_id,
                actor_id=request.actor.actor_id,
                conversation_id=request.conversation_id,
                message_id=request.message_id,
                occurred_at=request.occurred_at,
                channel=request.channel,
            )
        )
        metadata = {
            key: request.request_metadata[key]
            for key in self._ALLOWED_METADATA_KEYS
            if key in request.request_metadata
        }
        resources: dict[str, Any] = {
            "active_tasks": [dict(item) for item in self._active_tasks],
            "timezone": request.actor.timezone,
        }
        daily_resource = _daily_snapshot_resource(snapshot) if snapshot is not None else None
        if daily_resource is not None:
            resources["daily_draft"] = daily_resource
            resources["daily_policy"] = dict(self._daily_policy)
        turn = CognitiveTurn(
            tenant_id=request.tenant_id,
            user_id=user_id,
            actor_user_id=user_id,
            conversation_id=request.conversation_id,
            message_id=request.message_id,
            text=request.text,
            occurred_at=request.occurred_at,
            resources=resources,
        )
        planning_context = CommandPlanningContext(
            message_id=request.message_id,
            actor_user_id=request.actor.actor_id,
            daily_snapshot=snapshot,
            current_report_date=_daily_policy_report_date(self._daily_policy),
            user_constraints=state.user_constraints,
        )
        user_identity = MappingProxyType(
            {
                "tenant_id": request.tenant_id,
                "actor_id": user_id,
                "display_name": request.actor.display_name,
                "dingtalk_user_id": request.actor.dingtalk_user_id,
                "role": request.actor.role,
                "timezone": request.actor.timezone,
            }
        )
        manifest_payload = {
            "tenant_id": request.tenant_id,
            "actor_id": user_id,
            "conversation_id": request.conversation_id,
            "message_id": request.message_id,
            "state_version": state.version,
            "goal": state.current_goal.intent if state.current_goal is not None else "",
            "entity_ids": [entity.entity_id for entity in state.current_entities],
            "recent_context_ids": [frame.context_id for frame in state.recent_context],
            "active_pending_ids": [item.pending_id for item in state.active_pending(request.occurred_at)],
            "request_metadata": metadata,
            "source_text_hash": hashlib.sha256(request.text.encode("utf-8")).hexdigest(),
            "conversation_state_digest": _json_digest(state.as_payload()),
            "active_tasks_digest": _json_digest([dict(item) for item in self._active_tasks]),
            "daily_policy_digest": _json_digest(dict(self._daily_policy)),
            "daily_report_id": str(snapshot.report_id) if snapshot is not None else None,
            "daily_snapshot_version": snapshot.version if snapshot is not None else None,
            "daily_snapshot_digest": _json_digest(daily_resource) if daily_resource is not None else None,
        }
        manifest = MappingProxyType(
            {
                **manifest_payload,
                "digest": hashlib.sha256(
                    json.dumps(
                        manifest_payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    ).encode("utf-8")
                ).hexdigest(),
            }
        )
        return RuntimeContext(
            request=request,
            conversation_state=state,
            current_goal=state.current_goal,
            entities=state.current_entities,
            recent_context=state.recent_context,
            active_pending=state.active_pending(request.occurred_at),
            user_identity=user_identity,
            request_metadata=MappingProxyType(metadata),
            cognitive_turn=turn,
            planning_context=planning_context,
            manifest=manifest,
        )


def _daily_policy_report_date(policy: Mapping[str, Any]) -> date | None:
    raw_value = policy.get("current_report_date")
    if raw_value in {None, ""}:
        return None
    if isinstance(raw_value, date) and not isinstance(raw_value, datetime):
        return raw_value
    try:
        return date.fromisoformat(str(raw_value))
    except ValueError as exc:
        raise ValueError("daily policy current_report_date must be ISO YYYY-MM-DD") from exc


def _daily_snapshot_resource(snapshot: DailyReportMutationSnapshot) -> dict[str, Any]:
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


def _json_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
