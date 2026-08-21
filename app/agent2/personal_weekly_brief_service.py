from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Protocol
import uuid

from app.agent2.personal_weekly_brief_scope import PersonalWeeklyBriefTarget
from app.agent2.personal_weekly_brief_store import PersonalWeeklyBriefRecord


_BRIEF_NAMESPACE = uuid.UUID("91d3c3ef-e5e2-4c26-a0ca-14a536e53cd8")


class PersonalWeeklyBriefSnapshotSource(Protocol):
    async def load_snapshot(self, **kwargs): ...


class PersonalWeeklyBriefSnapshotStore(Protocol):
    async def load_by_owner_week(self, **kwargs) -> PersonalWeeklyBriefRecord | None: ...

    async def stage_snapshot(
        self, row: PersonalWeeklyBriefRecord
    ) -> PersonalWeeklyBriefRecord: ...


class PersonalWeeklyBriefSnapshotService:
    """Freeze each owner's first Saturday snapshot before any model call."""

    def __init__(
        self,
        *,
        store: PersonalWeeklyBriefSnapshotStore,
        source_loader: PersonalWeeklyBriefSnapshotSource,
    ) -> None:
        self._store = store
        self._source_loader = source_loader

    async def stage_target(
        self,
        *,
        target: PersonalWeeklyBriefTarget,
        week_start: date,
        snapshot_at: datetime,
    ) -> PersonalWeeklyBriefRecord:
        existing = await self._store.load_by_owner_week(
            tenant_id=target.tenant_id,
            owner_user_id=target.internal_user_id,
            week_start=week_start,
        )
        if existing is not None:
            return existing
        snapshot, personal_memory = await self._source_loader.load_snapshot(
            target=target,
            week_start=week_start,
            snapshot_at=snapshot_at,
        )
        key = (
            f"personal-weekly:{target.tenant_id}:"
            f"{target.internal_user_id}:{week_start.isoformat()}"
        )
        row = PersonalWeeklyBriefRecord(
            brief_id=str(uuid.uuid5(_BRIEF_NAMESPACE, key)),
            tenant_id=target.tenant_id,
            owner_user_id=target.internal_user_id,
            conversation_id=target.conversation_id,
            week_start=week_start,
            week_end=week_start + timedelta(days=4),
            snapshot_at=snapshot_at,
            source_snapshot=snapshot.as_payload(),
            source_fingerprint=snapshot.fingerprint,
            personal_memory_json=personal_memory,
            content_json={},
            message_text="",
            llm_model="",
            status="snapshot_ready",
            idempotency_key=key,
            created_at=snapshot_at,
            updated_at=snapshot_at,
        )
        return await self._store.stage_snapshot(row)


__all__ = ["PersonalWeeklyBriefSnapshotService"]
