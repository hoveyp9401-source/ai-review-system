from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import Column, Date, DateTime, Integer, MetaData, String, Table, Text, and_, select, update
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert


PersonalWeeklyBriefStatus = Literal[
    "snapshot_ready",
    "generation_failed",
    "generated",
    "claimed",
    "delivery_pending",
    "delivered",
    "failed",
    "cancelled",
]


personal_weekly_brief_metadata = MetaData()
_briefs = Table(
    "agent2_personal_weekly_briefs",
    personal_weekly_brief_metadata,
    Column("brief_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("owner_user_id", String(128), nullable=False),
    Column("conversation_id", String(256), nullable=False),
    Column("week_start", Date, nullable=False),
    Column("week_end", Date, nullable=False),
    Column("snapshot_at", DateTime(timezone=True), nullable=False),
    Column("source_snapshot", JSONB, nullable=False),
    Column("source_fingerprint", String(64), nullable=False),
    Column("personal_memory_json", JSONB, nullable=False),
    Column("content_json", JSONB, nullable=False),
    Column("message_text", Text, nullable=False),
    Column("llm_model", String(128), nullable=False),
    Column("status", String(32), nullable=False),
    Column("idempotency_key", String(512), nullable=False),
    Column("claim_token", String(256), nullable=False),
    Column("provider_message_id", String(512), nullable=False),
    Column("provider_accepted_at", DateTime(timezone=True)),
    Column("delivered_at", DateTime(timezone=True)),
    Column("context_recorded_at", DateTime(timezone=True)),
    Column("failed_at", DateTime(timezone=True)),
    Column("last_error", Text, nullable=False),
    Column("retry_count", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


@dataclass(frozen=True)
class PersonalWeeklyBriefRecord:
    brief_id: str
    tenant_id: str
    owner_user_id: str
    conversation_id: str
    week_start: date
    week_end: date
    snapshot_at: datetime
    source_snapshot: dict[str, Any]
    source_fingerprint: str
    content_json: dict[str, Any]
    message_text: str
    llm_model: str
    status: PersonalWeeklyBriefStatus
    idempotency_key: str
    created_at: datetime
    updated_at: datetime
    personal_memory_json: dict[str, Any] | None = None
    claim_token: str = ""
    provider_message_id: str = ""
    provider_accepted_at: datetime | None = None
    delivered_at: datetime | None = None
    context_recorded_at: datetime | None = None
    failed_at: datetime | None = None
    last_error: str = ""
    retry_count: int = 0


class SqlPersonalWeeklyBriefStore:
    def __init__(self, session: Any) -> None:
        self.session = session

    async def stage_snapshot(self, row: PersonalWeeklyBriefRecord) -> PersonalWeeklyBriefRecord:
        persisted = (
            await self.session.execute(
                pg_insert(_briefs)
                .values(**_record_values(row))
                .on_conflict_do_update(
                    index_elements=[_briefs.c.tenant_id, _briefs.c.owner_user_id, _briefs.c.week_start],
                    set_={"idempotency_key": _briefs.c.idempotency_key},
                )
                .returning(*_briefs.c)
            )
        ).mappings().one()
        return _record_from_row(persisted)

    async def load_by_owner_week(
        self, *, tenant_id: str, owner_user_id: str, week_start: date
    ) -> PersonalWeeklyBriefRecord | None:
        row = (
            await self.session.execute(
                select(_briefs).where(
                    _briefs.c.tenant_id == tenant_id,
                    _briefs.c.owner_user_id == owner_user_id,
                    _briefs.c.week_start == week_start,
                )
            )
        ).mappings().one_or_none()
        return _record_from_row(row) if row else None

    async def load_for_status(
        self,
        *,
        tenant_id: str,
        status: PersonalWeeklyBriefStatus,
        limit: int = 100,
        week_start: date | None = None,
    ) -> tuple[PersonalWeeklyBriefRecord, ...]:
        if limit < 1 or limit > 200:
            raise ValueError("personal weekly brief load limit is invalid")
        conditions = [
            _briefs.c.tenant_id == tenant_id,
            _briefs.c.status == status,
        ]
        if week_start is not None:
            conditions.append(_briefs.c.week_start == week_start)
        rows = (
            await self.session.execute(
                select(_briefs)
                .where(*conditions)
                .order_by(_briefs.c.week_start, _briefs.c.owner_user_id)
                .limit(limit)
            )
        ).mappings().all()
        return tuple(_record_from_row(row) for row in rows)

    async def load_delivered_without_context(
        self,
        *,
        tenant_id: str,
        limit: int = 100,
    ) -> tuple[PersonalWeeklyBriefRecord, ...]:
        if limit < 1 or limit > 200:
            raise ValueError("personal weekly brief load limit is invalid")
        rows = (
            await self.session.execute(
                select(_briefs)
                .where(
                    _briefs.c.tenant_id == tenant_id,
                    _briefs.c.status == "delivered",
                    _briefs.c.context_recorded_at.is_(None),
                )
                .order_by(_briefs.c.delivered_at, _briefs.c.owner_user_id)
                .limit(limit)
            )
        ).mappings().all()
        return tuple(_record_from_row(row) for row in rows)

    async def record_generation(
        self,
        *,
        tenant_id: str,
        brief_id: str,
        source_fingerprint: str,
        content_json: dict[str, Any],
        message_text: str,
        llm_model: str,
        changed_at: datetime,
    ) -> PersonalWeeklyBriefRecord:
        return await self._transition(
            tenant_id=tenant_id,
            brief_id=brief_id,
            from_status="snapshot_ready",
            extra_where=(_briefs.c.source_fingerprint == source_fingerprint),
            values={
                "status": "generated",
                "content_json": content_json,
                "message_text": message_text,
                "llm_model": llm_model,
                "updated_at": changed_at,
            },
        )

    async def record_generation_failure(
        self, *, tenant_id: str, brief_id: str, error: str, changed_at: datetime
    ) -> PersonalWeeklyBriefRecord:
        return await self._transition(
            tenant_id=tenant_id,
            brief_id=brief_id,
            from_status="snapshot_ready",
            values={
                "status": "generation_failed",
                "last_error": error,
                "failed_at": changed_at,
                "updated_at": changed_at,
            },
        )

    async def claim(
        self, *, tenant_id: str, brief_id: str, claim_token: str, changed_at: datetime
    ) -> PersonalWeeklyBriefRecord:
        if not claim_token.strip():
            raise ValueError("personal_weekly_brief_claim_invalid")
        return await self._transition(
            tenant_id=tenant_id,
            brief_id=brief_id,
            from_status="generated",
            values={
                "status": "claimed",
                "claim_token": claim_token,
                "updated_at": changed_at,
            },
        )

    async def persist_claim(self) -> None:
        await self.session.commit()

    async def record_provider_acceptance(
        self,
        *,
        tenant_id: str,
        brief_id: str,
        provider_message_id: str,
        expected_claim_token: str,
        changed_at: datetime,
    ) -> PersonalWeeklyBriefRecord:
        if not provider_message_id.strip() or not expected_claim_token.strip():
            raise ValueError("personal_weekly_brief_provider_acceptance_invalid")
        return await self._transition(
            tenant_id=tenant_id,
            brief_id=brief_id,
            from_status="claimed",
            extra_where=(_briefs.c.claim_token == expected_claim_token),
            values={
                "status": "delivery_pending",
                "provider_message_id": provider_message_id,
                "provider_accepted_at": changed_at,
                "updated_at": changed_at,
            },
        )

    async def persist_provider_acceptance(self) -> None:
        await self.session.commit()

    async def record_delivery(
        self, *, tenant_id: str, brief_id: str, changed_at: datetime
    ) -> PersonalWeeklyBriefRecord:
        return await self._transition(
            tenant_id=tenant_id,
            brief_id=brief_id,
            from_status="delivery_pending",
            values={
                "status": "delivered",
                "delivered_at": changed_at,
                "updated_at": changed_at,
            },
        )

    async def record_context(
        self, *, tenant_id: str, brief_id: str, changed_at: datetime
    ) -> PersonalWeeklyBriefRecord:
        return await self._transition(
            tenant_id=tenant_id,
            brief_id=brief_id,
            from_status="delivered",
            extra_where=(_briefs.c.context_recorded_at.is_(None)),
            values={"context_recorded_at": changed_at, "updated_at": changed_at},
        )

    async def record_failure(
        self,
        *,
        tenant_id: str,
        brief_id: str,
        error: str,
        changed_at: datetime,
        expected_claim_token: str | None = None,
    ) -> PersonalWeeklyBriefRecord:
        if not error.strip():
            raise ValueError("personal_weekly_brief_failure_invalid")
        allowed = _briefs.c.status == "delivery_pending"
        if expected_claim_token:
            allowed = and_(
                _briefs.c.status == "claimed",
                _briefs.c.claim_token == expected_claim_token,
            )
        persisted = (
            await self.session.execute(
                update(_briefs)
                .where(
                    _briefs.c.tenant_id == tenant_id,
                    _briefs.c.brief_id == UUID(brief_id),
                    allowed,
                )
                .values(
                    status="failed",
                    last_error=error,
                    failed_at=changed_at,
                    retry_count=_briefs.c.retry_count + 1,
                    updated_at=changed_at,
                )
                .returning(*_briefs.c)
            )
        ).mappings().one_or_none()
        if persisted is None:
            raise ValueError("personal_weekly_brief_transition_conflict")
        return _record_from_row(persisted)

    async def _transition(
        self,
        *,
        tenant_id: str,
        brief_id: str,
        from_status: PersonalWeeklyBriefStatus,
        values: dict[str, Any],
        extra_where: Any = None,
    ) -> PersonalWeeklyBriefRecord:
        conditions = [
            _briefs.c.tenant_id == tenant_id,
            _briefs.c.brief_id == UUID(brief_id),
            _briefs.c.status == from_status,
        ]
        if extra_where is not None:
            conditions.append(extra_where)
        persisted = (
            await self.session.execute(
                update(_briefs)
                .where(*conditions)
                .values(**values)
                .returning(*_briefs.c)
            )
        ).mappings().one_or_none()
        if persisted is None:
            raise ValueError("personal_weekly_brief_transition_conflict")
        return _record_from_row(persisted)


def _record_values(row: PersonalWeeklyBriefRecord) -> dict[str, Any]:
    return {
        "brief_id": UUID(row.brief_id),
        "tenant_id": row.tenant_id,
        "owner_user_id": row.owner_user_id,
        "conversation_id": row.conversation_id,
        "week_start": row.week_start,
        "week_end": row.week_end,
        "snapshot_at": row.snapshot_at,
        "source_snapshot": row.source_snapshot,
        "source_fingerprint": row.source_fingerprint,
        "personal_memory_json": row.personal_memory_json or {},
        "content_json": row.content_json,
        "message_text": row.message_text,
        "llm_model": row.llm_model,
        "status": row.status,
        "idempotency_key": row.idempotency_key,
        "claim_token": row.claim_token,
        "provider_message_id": row.provider_message_id,
        "provider_accepted_at": row.provider_accepted_at,
        "delivered_at": row.delivered_at,
        "context_recorded_at": row.context_recorded_at,
        "failed_at": row.failed_at,
        "last_error": row.last_error,
        "retry_count": row.retry_count,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _record_from_row(row: Any) -> PersonalWeeklyBriefRecord:
    return PersonalWeeklyBriefRecord(
        brief_id=str(row["brief_id"]),
        tenant_id=row["tenant_id"],
        owner_user_id=row["owner_user_id"],
        conversation_id=row["conversation_id"],
        week_start=row["week_start"],
        week_end=row["week_end"],
        snapshot_at=row["snapshot_at"],
        source_snapshot=dict(row["source_snapshot"] or {}),
        source_fingerprint=row["source_fingerprint"],
        personal_memory_json=dict(row["personal_memory_json"] or {}),
        content_json=dict(row["content_json"] or {}),
        message_text=row["message_text"],
        llm_model=row["llm_model"],
        status=row["status"],
        idempotency_key=row["idempotency_key"],
        claim_token=row["claim_token"],
        provider_message_id=row["provider_message_id"],
        provider_accepted_at=row["provider_accepted_at"],
        delivered_at=row["delivered_at"],
        context_recorded_at=row["context_recorded_at"],
        failed_at=row["failed_at"],
        last_error=row["last_error"],
        retry_count=row["retry_count"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


__all__ = [
    "PersonalWeeklyBriefRecord",
    "SqlPersonalWeeklyBriefStore",
    "personal_weekly_brief_metadata",
]
