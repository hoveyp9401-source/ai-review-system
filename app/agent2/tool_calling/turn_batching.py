from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
import json
import time
from typing import Any
from uuid import UUID


INGRESS_META_KEY = "_agent2_stream_ingress_v1"
BATCH_META_KEY = "_agent2_turn_batch_v1"
CANARY_TRANSPORT_MARKER = "_agent2_tool_call_canary"


@dataclass(frozen=True)
class TurnFragment:
    event_id: UUID
    source_message_id: str
    dingtalk_user_id: str
    conversation_id: str
    text: str
    received_at: datetime


@dataclass(frozen=True)
class SealedTurnBatch:
    batch_id: str
    leader_event_id: UUID
    fragments: tuple[TurnFragment, ...]

    @property
    def source_message_id(self) -> str:
        return self.batch_id

    @property
    def event_ids(self) -> tuple[UUID, ...]:
        return tuple(item.event_id for item in self.fragments)

    @property
    def user_messages(self) -> tuple[str, ...]:
        return tuple(item.text for item in self.fragments)

    def is_leader(self, event_id: UUID) -> bool:
        return event_id == self.leader_event_id


@dataclass
class _PendingTurn:
    created_at: float
    updated_at: float
    future: asyncio.Future[SealedTurnBatch]
    fragments: dict[UUID, TurnFragment] = field(default_factory=dict)


def prepare_recoverable_ingress_payload(
    provider_payload: dict[str, Any],
    *,
    text: str,
    message_type: str,
    voice_download_seconds: float,
    voice_transcribe_seconds: float,
) -> dict[str, Any]:
    payload = deepcopy(provider_payload)
    payload[INGRESS_META_KEY] = {
        "message_type": str(message_type or "text"),
        "recoverable": True,
        "text": str(text),
        "voice_download_seconds": float(voice_download_seconds or 0.0),
        "voice_transcribe_seconds": float(
            voice_transcribe_seconds or 0.0
        ),
    }
    return payload


def provider_payload_from_ingress(
    payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        key: deepcopy(value)
        for key, value in payload.items()
        if key not in {INGRESS_META_KEY, BATCH_META_KEY}
    }


def is_recoverable_ingress_payload(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    meta = payload.get(INGRESS_META_KEY)
    return isinstance(meta, dict) and meta.get("recoverable") is True


def canonical_turn_batch_source_id(
    source_message_ids: Sequence[str],
) -> str:
    canonical_ids = tuple(
        str(value).strip() for value in source_message_ids
    )
    if not canonical_ids or any(not value for value in canonical_ids):
        raise ValueError("source_message_ids must be non-empty")
    encoded = json.dumps(
        {"source_message_ids": canonical_ids},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"agent2-turn-batch-v1:{hashlib.sha256(encoded).hexdigest()}"


def contiguous_turn_fragments(
    fragments: Sequence[TurnFragment],
    *,
    anchor: TurnFragment,
    quiet_seconds: float,
    max_window_seconds: float,
) -> tuple[TurnFragment, ...]:
    if quiet_seconds <= 0 or max_window_seconds < quiet_seconds:
        raise ValueError("invalid turn batching window")
    scoped = sorted(
        (
            item
            for item in fragments
            if item.dingtalk_user_id == anchor.dingtalk_user_id
            and item.conversation_id == anchor.conversation_id
        ),
        key=lambda item: (item.received_at, str(item.event_id)),
    )
    groups: list[list[TurnFragment]] = []
    for item in scoped:
        if not groups:
            groups.append([item])
            continue
        current = groups[-1]
        gap = (item.received_at - current[-1].received_at).total_seconds()
        elapsed = (
            item.received_at - current[0].received_at
        ).total_seconds()
        if gap <= quiet_seconds and elapsed <= max_window_seconds:
            current.append(item)
        else:
            groups.append([item])
    return next(
        (
            tuple(group)
            for group in groups
            if any(item.event_id == anchor.event_id for item in group)
        ),
        (),
    )


def turn_seal_delay_seconds(
    fragments: Sequence[TurnFragment],
    *,
    now: datetime,
    quiet_seconds: float,
    max_window_seconds: float,
) -> float:
    if not fragments:
        return 0.0
    ordered = sorted(
        fragments,
        key=lambda item: (item.received_at, str(item.event_id)),
    )
    deadline = min(
        ordered[-1].received_at + timedelta(seconds=quiet_seconds),
        ordered[0].received_at + timedelta(seconds=max_window_seconds),
    )
    return round(max(0.0, (deadline - now).total_seconds()), 6)


def build_batched_follower_payload(
    *,
    batch_id: str,
    leader_event_id: str,
) -> dict[str, Any]:
    return {
        CANARY_TRANSPORT_MARKER: {
            "batch_id": batch_id,
            "delivery": "batched_follower",
            "leader_event_id": leader_event_id,
        }
    }


class CanaryTurnBatchCoordinator:
    """Collect fragments by trusted identity and time, never by text meaning."""

    def __init__(
        self,
        *,
        quiet_seconds: float,
        max_window_seconds: float,
        monotonic: Callable[[], float] | None = None,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        if quiet_seconds <= 0 or max_window_seconds < quiet_seconds:
            raise ValueError("invalid turn batching window")
        self._quiet_seconds = quiet_seconds
        self._max_window_seconds = max_window_seconds
        self._monotonic = monotonic or time.monotonic
        self._sleeper = sleeper or asyncio.sleep
        self._lock = asyncio.Lock()
        self._pending: dict[str, _PendingTurn] = {}

    async def collect(
        self,
        *,
        fragment: TurnFragment,
    ) -> SealedTurnBatch:
        scope = (
            f"{fragment.dingtalk_user_id}\0"
            f"{fragment.conversation_id}"
        )
        async with self._lock:
            now = self._monotonic()
            pending = self._pending.get(scope)
            if pending is None:
                pending = _PendingTurn(
                    created_at=now,
                    updated_at=now,
                    future=asyncio.get_running_loop().create_future(),
                )
                self._pending[scope] = pending
                asyncio.create_task(self._seal(scope, pending))
            pending.fragments.setdefault(fragment.event_id, fragment)
            pending.updated_at = now
            future = pending.future
        return await asyncio.shield(future)

    async def _seal(
        self,
        scope: str,
        pending: _PendingTurn,
    ) -> None:
        while True:
            async with self._lock:
                if self._pending.get(scope) is not pending:
                    return
                now = self._monotonic()
                deadline = min(
                    pending.updated_at + self._quiet_seconds,
                    pending.created_at + self._max_window_seconds,
                )
                delay = max(0.0, deadline - now)
                if delay == 0:
                    fragments = tuple(
                        sorted(
                            pending.fragments.values(),
                            key=lambda item: (
                                item.received_at,
                                str(item.event_id),
                            ),
                        )
                    )
                    batch = SealedTurnBatch(
                        batch_id=canonical_turn_batch_source_id(
                            tuple(
                                item.source_message_id
                                for item in fragments
                            )
                        ),
                        leader_event_id=fragments[0].event_id,
                        fragments=fragments,
                    )
                    self._pending.pop(scope, None)
                    if not pending.future.done():
                        pending.future.set_result(batch)
                    return
            await self._sleeper(delay)
