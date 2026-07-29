from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    or_,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.agent2.memory.module import (
    PersonalMemoryScope,
    TrustedPersonalMemory,
)
from app.db import Base


class PersonalMemoryRecord(Base):
    __tablename__ = "agent2_personal_memories"
    __table_args__ = (
        CheckConstraint(
            "memory_type IN ("
            "'response_preference', 'saved_view', 'terminology_alias'"
            ")",
            name="agent2_personal_memory_type_check",
        ),
        CheckConstraint(
            "source_kind IN ('explicit_user', 'server_verified')",
            name="agent2_personal_memory_source_check",
        ),
        CheckConstraint(
            "status IN ('active', 'superseded', 'forgotten')",
            name="agent2_personal_memory_status_check",
        ),
        CheckConstraint(
            "version >= 1",
            name="agent2_personal_memory_version_check",
        ),
        Index(
            "agent2_personal_memory_scope_idx",
            "tenant_id",
            "user_id",
            "status",
            "updated_at",
        ),
        Index(
            "agent2_personal_memory_one_active_key_idx",
            "tenant_id",
            "user_id",
            "memory_key",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    memory_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    memory_type: Mapped[str] = mapped_column(String(32), nullable=False)
    memory_key: Mapped[str] = mapped_column(String(128), nullable=False)
    value_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
    )
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_message_id: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="active",
    )
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class PersonalMemoryAuditRecord(Base):
    """Append-only evidence for one authenticated personal-memory change."""

    __tablename__ = "agent2_personal_memory_audits"
    __table_args__ = (
        CheckConstraint(
            "action IN ('create', 'replace', 'forget', 'compose')",
            name="agent2_personal_memory_audit_action_check",
        ),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="agent2_personal_memory_audit_idempotency_key",
        ),
        Index(
            "agent2_personal_memory_audit_scope_idx",
            "tenant_id",
            "user_id",
            "occurred_at",
        ),
    )

    audit_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    memory_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "agent2_personal_memories.memory_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    conversation_id: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
    )
    source_message_id: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
    )
    tool_call_id: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
    )
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    memory_key: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    before_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class PostgresPersonalMemoryReadStore:
    def __init__(self, session: Any) -> None:
        self._session = session

    async def load_active(
        self,
        scope: PersonalMemoryScope,
        *,
        limit: int,
    ) -> tuple[TrustedPersonalMemory, ...]:
        if limit <= 0:
            return ()
        table_reference = await self._session.scalar(
            select(
                func.to_regclass(
                    "public.agent2_personal_memories"
                )
            )
        )
        if table_reference is None:
            return ()
        rows = list(
            (
                await self._session.scalars(
                    select(PersonalMemoryRecord)
                    .where(
                        PersonalMemoryRecord.tenant_id
                        == scope.tenant_id,
                        PersonalMemoryRecord.user_id
                        == scope.user_id,
                        PersonalMemoryRecord.status == "active",
                        or_(
                            PersonalMemoryRecord.expires_at.is_(None),
                            PersonalMemoryRecord.expires_at > scope.now,
                        ),
                    )
                    .order_by(
                        PersonalMemoryRecord.updated_at.desc(),
                        PersonalMemoryRecord.memory_key,
                    )
                    .limit(limit)
                )
            ).all()
        )
        return tuple(
            TrustedPersonalMemory(
                memory_id=row.memory_id,
                tenant_id=row.tenant_id,
                user_id=row.user_id,
                memory_type=row.memory_type,
                memory_key=row.memory_key,
                value=row.value_json,
                source_kind=row.source_kind,
                source_message_id=row.source_message_id,
                version=row.version,
                updated_at=row.updated_at,
                expires_at=row.expires_at,
            )
            for row in rows
        )
