from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Protocol

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent2.conversation_state import ConversationState
from app.models import Agent2ConversationState


class ConversationStateVersionConflict(RuntimeError):
    pass


class ConversationStateStore(Protocol):
    async def load(self, *, user_id: str, conversation_id: str) -> ConversationState: ...

    async def save(self, state: ConversationState, *, expected_version: int) -> ConversationState: ...


class InMemoryConversationStateStore:
    """Test/local adapter for the conversation-state seam."""

    def __init__(self, initial_states: Iterable[ConversationState] = ()):
        states = tuple(initial_states)
        keys = [(state.user_id, state.conversation_id) for state in states]
        if len(keys) != len(set(keys)):
            raise ValueError("initial conversation states contain duplicate identities")
        self._states: dict[tuple[str, str], ConversationState] = {
            key: state for key, state in zip(keys, states, strict=True)
        }
        self._lock = asyncio.Lock()

    async def load(self, *, user_id: str, conversation_id: str) -> ConversationState:
        key = (user_id, conversation_id)
        async with self._lock:
            return self._states.get(key) or ConversationState.empty(
                user_id=user_id,
                conversation_id=conversation_id,
            )

    async def save(self, state: ConversationState, *, expected_version: int) -> ConversationState:
        key = (state.user_id, state.conversation_id)
        async with self._lock:
            current = self._states.get(key)
            current_version = current.version if current is not None else 0
            if current_version != expected_version or state.version != expected_version + 1:
                raise ConversationStateVersionConflict(
                    f"conversation state version conflict: expected {expected_version}, current {current_version}"
                )
            self._states[key] = state
            return state


class SQLAlchemyConversationStateStore:
    """PostgreSQL adapter shared by API, webhook, and stream processes."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def load(self, *, user_id: str, conversation_id: str) -> ConversationState:
        result = await self._session.execute(
            select(Agent2ConversationState).where(
                Agent2ConversationState.user_key == user_id,
                Agent2ConversationState.conversation_id == conversation_id,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            return ConversationState.empty(user_id=user_id, conversation_id=conversation_id)
        state = ConversationState.from_payload(dict(row.state_json or {}))
        if state.version != row.version:
            raise ValueError("conversation state payload version does not match row version")
        return state

    async def save(self, state: ConversationState, *, expected_version: int) -> ConversationState:
        if state.version != expected_version + 1:
            raise ConversationStateVersionConflict("conversation state version did not advance exactly once")
        payload = state.as_payload()
        last_message_id = state.recent_context[-1].message_id if state.recent_context else ""
        if expected_version == 0:
            statement = (
                pg_insert(Agent2ConversationState)
                .values(
                    user_key=state.user_id,
                    conversation_id=state.conversation_id,
                    version=state.version,
                    state_json=payload,
                    last_message_id=last_message_id,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        Agent2ConversationState.user_key,
                        Agent2ConversationState.conversation_id,
                    ]
                )
                .returning(Agent2ConversationState.id)
            )
            result = await self._session.execute(statement)
            if result.scalar_one_or_none() is None:
                raise ConversationStateVersionConflict("conversation state was created concurrently")
            return state
        result = await self._session.execute(
            update(Agent2ConversationState)
            .where(
                Agent2ConversationState.user_key == state.user_id,
                Agent2ConversationState.conversation_id == state.conversation_id,
                Agent2ConversationState.version == expected_version,
            )
            .values(
                version=state.version,
                state_json=payload,
                last_message_id=last_message_id,
                updated_at=func.now(),
            )
        )
        if result.rowcount != 1:
            raise ConversationStateVersionConflict("conversation state changed concurrently")
        return state
