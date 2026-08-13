from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    and_,
    case,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.agent2.weekly_plan_collection import WeeklyPlanReminderCandidate
from app.agent2.weekly_plan_domain import _stable_id

WeeklyPlanReminderStatus = Literal[
    "queued",
    "claimed",
    "delivery_pending",
    "delivered",
    "failed",
    "cancelled",
]

WEEKLY_PLAN_REMINDER_CLAIM_TIMEOUT = timedelta(minutes=15)


_metadata = MetaData()
_outbox = Table(
    "agent2_weekly_plan_reminder_outbox",
    _metadata,
    Column("outbox_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("batch_id", PG_UUID(as_uuid=True), nullable=False),
    Column("plan_id", PG_UUID(as_uuid=True)),
    Column("target_week_start", Date, nullable=False),
    Column("recipient_internal_user_id", String(128), nullable=False),
    Column("collection_state", String(32), nullable=False),
    Column("reminder_at", DateTime(timezone=True), nullable=False),
    Column("channel", String(32), nullable=False),
    Column("idempotency_key", String(512), nullable=False),
    Column("status", String(32), nullable=False),
    Column("retry_count", Integer, nullable=False),
    Column("claim_token", String(256), nullable=False),
    Column("provider_message_id", String(512), nullable=False),
    Column("provider_accepted_at", DateTime(timezone=True)),
    Column("delivered_at", DateTime(timezone=True)),
    Column("failed_at", DateTime(timezone=True)),
    Column("cancelled_at", DateTime(timezone=True)),
    Column("last_error", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


@dataclass(frozen=True)
class WeeklyPlanReminderOutbox:
    outbox_id: str
    tenant_id: str
    batch_id: str
    plan_id: str
    target_week_start: date
    recipient_internal_user_id: str
    collection_state: str
    reminder_at: datetime
    channel: str
    idempotency_key: str
    status: WeeklyPlanReminderStatus
    retry_count: int
    created_at: datetime
    updated_at: datetime
    claim_token: str = ""
    provider_message_id: str = ""
    provider_accepted_at: datetime | None = None
    delivered_at: datetime | None = None
    failed_at: datetime | None = None
    cancelled_at: datetime | None = None
    last_error: str = ""


class SqlWeeklyPlanReminderOutboxStore:
    """Persistence only.  This adapter contains no provider/send operation."""

    def __init__(self, session: Any) -> None:
        self.session = session

    async def enqueue(
        self, row: WeeklyPlanReminderOutbox
    ) -> WeeklyPlanReminderOutbox:
        values = _outbox_values(row)
        persisted = (
            await self.session.execute(
                pg_insert(_outbox)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=[_outbox.c.tenant_id, _outbox.c.idempotency_key],
                    set_={"idempotency_key": _outbox.c.idempotency_key},
                )
                .returning(*_outbox.c)
            )
        ).mappings().one_or_none()
        if persisted is None:
            raise ValueError("weekly_plan_reminder_outbox_enqueue_failed")
        return _outbox_from_row(persisted)

    async def load_due_queued(
        self,
        *,
        tenant_id: str,
        recipient_internal_user_id: str,
        as_of: datetime,
        limit: int = 1,
    ) -> tuple[WeeklyPlanReminderOutbox, ...]:
        _aware(as_of, "as_of")
        if not tenant_id.strip() or not recipient_internal_user_id.strip():
            raise ValueError("weekly_plan_reminder_scope_invalid")
        if limit < 1 or limit > 10:
            raise ValueError("weekly_plan_reminder_limit_invalid")
        stale_before = as_of - WEEKLY_PLAN_REMINDER_CLAIM_TIMEOUT
        rows = (
            await self.session.execute(
                select(_outbox)
                .where(
                    _outbox.c.tenant_id == tenant_id,
                    _outbox.c.recipient_internal_user_id
                    == recipient_internal_user_id,
                    or_(
                        _outbox.c.status == "queued",
                        and_(
                            _outbox.c.status == "claimed",
                            _outbox.c.provider_message_id == "",
                            _outbox.c.updated_at <= stale_before,
                        ),
                    ),
                    _outbox.c.reminder_at <= as_of,
                )
                .order_by(_outbox.c.reminder_at, _outbox.c.created_at)
                .limit(limit)
            )
        ).mappings().all()
        return tuple(_outbox_from_row(row) for row in rows)

    async def load_delivery_pending(
        self,
        *,
        tenant_id: str,
        recipient_internal_user_id: str,
        limit: int = 10,
    ) -> tuple[WeeklyPlanReminderOutbox, ...]:
        if not tenant_id.strip() or not recipient_internal_user_id.strip():
            raise ValueError("weekly_plan_reminder_scope_invalid")
        if limit < 1 or limit > 10:
            raise ValueError("weekly_plan_reminder_limit_invalid")
        rows = (
            await self.session.execute(
                select(_outbox)
                .where(
                    _outbox.c.tenant_id == tenant_id,
                    _outbox.c.recipient_internal_user_id
                    == recipient_internal_user_id,
                    _outbox.c.status == "delivery_pending",
                )
                .order_by(_outbox.c.provider_accepted_at, _outbox.c.created_at)
                .limit(limit)
            )
        ).mappings().all()
        return tuple(_outbox_from_row(row) for row in rows)

    async def persist_claim(self) -> None:
        """Make the claimed state durable before any external send occurs."""

        await self.session.commit()

    async def persist_provider_acceptance(self) -> None:
        """Make the provider reference durable before delivery verification."""

        await self.session.commit()

    async def claim(
        self,
        *,
        tenant_id: str,
        outbox_id: str,
        claim_token: str,
        changed_at: datetime,
    ) -> WeeklyPlanReminderOutbox:
        if not claim_token.strip():
            raise ValueError("weekly_plan_reminder_not_claimable")
        _aware(changed_at, "changed_at")
        stale_before = changed_at - WEEKLY_PLAN_REMINDER_CLAIM_TIMEOUT
        persisted = (
            await self.session.execute(
                update(_outbox)
                .where(
                    _outbox.c.tenant_id == tenant_id,
                    _outbox.c.outbox_id == UUID(outbox_id),
                    or_(
                        _outbox.c.status == "queued",
                        and_(
                            _outbox.c.status == "claimed",
                            _outbox.c.provider_message_id == "",
                            _outbox.c.updated_at <= stale_before,
                        ),
                    ),
                )
                .values(
                    status="claimed",
                    claim_token=claim_token,
                    retry_count=case(
                        (
                            _outbox.c.status == "claimed",
                            _outbox.c.retry_count + 1,
                        ),
                        else_=_outbox.c.retry_count,
                    ),
                    updated_at=changed_at,
                )
                .returning(*_outbox.c)
            )
        ).mappings().one_or_none()
        if persisted is None:
            raise ValueError("weekly_plan_reminder_transition_conflict")
        return _outbox_from_row(persisted)

    async def record_provider_acceptance(
        self,
        *,
        tenant_id: str,
        outbox_id: str,
        provider_message_id: str,
        expected_claim_token: str,
        changed_at: datetime,
    ) -> WeeklyPlanReminderOutbox:
        if not provider_message_id.strip() or not expected_claim_token.strip():
            raise ValueError("weekly_plan_reminder_provider_acceptance_invalid")
        _aware(changed_at, "changed_at")
        persisted = (
            await self.session.execute(
                update(_outbox)
                .where(
                    _outbox.c.tenant_id == tenant_id,
                    _outbox.c.outbox_id == UUID(outbox_id),
                    _outbox.c.status == "claimed",
                    _outbox.c.claim_token == expected_claim_token,
                )
                .values(
                    status="delivery_pending",
                    provider_message_id=provider_message_id,
                    provider_accepted_at=changed_at,
                    updated_at=changed_at,
                )
                .returning(*_outbox.c)
            )
        ).mappings().one_or_none()
        if persisted is None:
            raise ValueError("weekly_plan_reminder_transition_conflict")
        return _outbox_from_row(persisted)

    async def record_delivery(
        self,
        *,
        tenant_id: str,
        outbox_id: str,
        changed_at: datetime,
    ) -> WeeklyPlanReminderOutbox:
        return await self._transition(
            tenant_id=tenant_id,
            outbox_id=outbox_id,
            from_status="delivery_pending",
            values={
                "status": "delivered",
                "delivered_at": changed_at,
                "updated_at": changed_at,
            },
        )

    async def record_failure(
        self,
        *,
        tenant_id: str,
        outbox_id: str,
        error: str,
        changed_at: datetime,
        expected_claim_token: str | None = None,
    ) -> WeeklyPlanReminderOutbox:
        if not error.strip():
            raise ValueError("weekly_plan_reminder_failure_invalid")
        claimed_match = and_(
            _outbox.c.status == "claimed",
            _outbox.c.claim_token == expected_claim_token,
        )
        allowed_state = (
            or_(claimed_match, _outbox.c.status == "delivery_pending")
            if expected_claim_token
            else _outbox.c.status == "delivery_pending"
        )
        persisted = (
            await self.session.execute(
                update(_outbox)
                .where(
                    _outbox.c.tenant_id == tenant_id,
                    _outbox.c.outbox_id == UUID(outbox_id),
                    allowed_state,
                )
                .values(
                    status="failed",
                    retry_count=_outbox.c.retry_count + 1,
                    last_error=error,
                    failed_at=changed_at,
                    updated_at=changed_at,
                )
                .returning(*_outbox.c)
            )
        ).mappings().one_or_none()
        if persisted is None:
            raise ValueError("weekly_plan_reminder_transition_conflict")
        return _outbox_from_row(persisted)

    async def cancel(
        self,
        *,
        tenant_id: str,
        outbox_id: str,
        changed_at: datetime,
    ) -> WeeklyPlanReminderOutbox:
        _aware(changed_at, "changed_at")
        persisted = (
            await self.session.execute(
                update(_outbox)
                .where(
                    _outbox.c.tenant_id == tenant_id,
                    _outbox.c.outbox_id == UUID(outbox_id),
                    _outbox.c.status.not_in(("delivered", "cancelled")),
                )
                .values(
                    status="cancelled",
                    cancelled_at=changed_at,
                    updated_at=changed_at,
                )
                .returning(*_outbox.c)
            )
        ).mappings().one_or_none()
        if persisted is None:
            raise ValueError("weekly_plan_reminder_transition_conflict")
        return _outbox_from_row(persisted)

    async def _transition(
        self,
        *,
        tenant_id: str,
        outbox_id: str,
        from_status: WeeklyPlanReminderStatus,
        values: dict[str, object],
    ) -> WeeklyPlanReminderOutbox:
        changed_at = values.get("updated_at")
        if not isinstance(changed_at, datetime):
            raise ValueError("changed_at is required")  # noqa: TRY004
        _aware(changed_at, "changed_at")
        persisted = (
            await self.session.execute(
                update(_outbox)
                .where(
                    _outbox.c.tenant_id == tenant_id,
                    _outbox.c.outbox_id == UUID(outbox_id),
                    _outbox.c.status == from_status,
                )
                .values(**values)
                .returning(*_outbox.c)
            )
        ).mappings().one_or_none()
        if persisted is None:
            raise ValueError("weekly_plan_reminder_transition_conflict")
        return _outbox_from_row(persisted)


def create_weekly_plan_reminder_outbox(
    candidate: WeeklyPlanReminderCandidate, *, created_at: datetime
) -> WeeklyPlanReminderOutbox:
    _aware(created_at, "created_at")
    if candidate.channel != "private_chat":
        raise ValueError("weekly_plan_reminder_must_be_private")
    return WeeklyPlanReminderOutbox(
        outbox_id=_stable_id(
            "weekly-plan-reminder-outbox",
            candidate.tenant_id,
            candidate.idempotency_key,
        ),
        tenant_id=candidate.tenant_id,
        batch_id=candidate.batch_id,
        plan_id=candidate.plan_id,
        target_week_start=candidate.target_week_start,
        recipient_internal_user_id=candidate.recipient_internal_user_id,
        collection_state=candidate.collection_state,
        reminder_at=candidate.reminder_at,
        channel=candidate.channel,
        idempotency_key=candidate.idempotency_key,
        status="queued",
        retry_count=0,
        created_at=created_at,
        updated_at=created_at,
    )


def claim_weekly_plan_reminder(
    row: WeeklyPlanReminderOutbox, *, claim_token: str, changed_at: datetime
) -> WeeklyPlanReminderOutbox:
    _aware(changed_at, "changed_at")
    queued = row.status == "queued"
    expired_claim = (
        row.status == "claimed"
        and not row.provider_message_id
        and changed_at >= row.updated_at + WEEKLY_PLAN_REMINDER_CLAIM_TIMEOUT
    )
    if (not queued and not expired_claim) or not claim_token.strip():
        raise ValueError("weekly_plan_reminder_not_claimable")
    return replace(
        row,
        status="claimed",
        claim_token=claim_token,
        retry_count=row.retry_count + (1 if expired_claim else 0),
        updated_at=changed_at,
    )


def record_weekly_plan_provider_acceptance(
    row: WeeklyPlanReminderOutbox,
    *,
    provider_message_id: str,
    expected_claim_token: str,
    changed_at: datetime,
) -> WeeklyPlanReminderOutbox:
    _aware(changed_at, "changed_at")
    if (
        row.status != "claimed"
        or not provider_message_id.strip()
        or not expected_claim_token.strip()
        or row.claim_token != expected_claim_token
    ):
        raise ValueError("weekly_plan_reminder_provider_acceptance_invalid")
    return replace(
        row,
        status="delivery_pending",
        provider_message_id=provider_message_id,
        provider_accepted_at=changed_at,
        updated_at=changed_at,
    )


def record_weekly_plan_delivery(
    row: WeeklyPlanReminderOutbox, *, changed_at: datetime
) -> WeeklyPlanReminderOutbox:
    _aware(changed_at, "changed_at")
    if row.status != "delivery_pending":
        raise ValueError("weekly_plan_reminder_delivery_invalid")
    return replace(
        row,
        status="delivered",
        delivered_at=changed_at,
        updated_at=changed_at,
    )


def fail_weekly_plan_reminder(
    row: WeeklyPlanReminderOutbox,
    *,
    error: str,
    changed_at: datetime,
    expected_claim_token: str | None = None,
) -> WeeklyPlanReminderOutbox:
    _aware(changed_at, "changed_at")
    claimed_allowed = (
        row.status == "claimed"
        and bool(expected_claim_token)
        and row.claim_token == expected_claim_token
    )
    if (
        (not claimed_allowed and row.status != "delivery_pending")
        or not error.strip()
    ):
        raise ValueError("weekly_plan_reminder_failure_invalid")
    return replace(
        row,
        status="failed",
        retry_count=row.retry_count + 1,
        last_error=error,
        failed_at=changed_at,
        updated_at=changed_at,
    )


def cancel_weekly_plan_reminder(
    row: WeeklyPlanReminderOutbox, *, changed_at: datetime
) -> WeeklyPlanReminderOutbox:
    _aware(changed_at, "changed_at")
    if row.status in {"delivered", "cancelled"}:
        raise ValueError("weekly_plan_reminder_not_cancellable")
    return replace(
        row,
        status="cancelled",
        cancelled_at=changed_at,
        updated_at=changed_at,
    )


def _aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _outbox_values(row: WeeklyPlanReminderOutbox) -> dict[str, object]:
    return {
        "outbox_id": UUID(row.outbox_id),
        "tenant_id": row.tenant_id,
        "batch_id": UUID(row.batch_id),
        "plan_id": UUID(row.plan_id) if row.plan_id else None,
        "target_week_start": row.target_week_start,
        "recipient_internal_user_id": row.recipient_internal_user_id,
        "collection_state": row.collection_state,
        "reminder_at": row.reminder_at,
        "channel": row.channel,
        "idempotency_key": row.idempotency_key,
        "status": row.status,
        "retry_count": row.retry_count,
        "claim_token": row.claim_token,
        "provider_message_id": row.provider_message_id,
        "provider_accepted_at": row.provider_accepted_at,
        "delivered_at": row.delivered_at,
        "failed_at": row.failed_at,
        "cancelled_at": row.cancelled_at,
        "last_error": row.last_error,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _outbox_from_row(row) -> WeeklyPlanReminderOutbox:
    return WeeklyPlanReminderOutbox(
        outbox_id=str(row["outbox_id"]),
        tenant_id=row["tenant_id"],
        batch_id=str(row["batch_id"]),
        plan_id=str(row["plan_id"]) if row["plan_id"] else "",
        target_week_start=row["target_week_start"],
        recipient_internal_user_id=row["recipient_internal_user_id"],
        collection_state=row["collection_state"],
        reminder_at=row["reminder_at"],
        channel=row["channel"],
        idempotency_key=row["idempotency_key"],
        status=row["status"],
        retry_count=row["retry_count"],
        claim_token=row["claim_token"],
        provider_message_id=row["provider_message_id"],
        provider_accepted_at=row["provider_accepted_at"],
        delivered_at=row["delivered_at"],
        failed_at=row["failed_at"],
        cancelled_at=row["cancelled_at"],
        last_error=row["last_error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


__all__ = [
    "SqlWeeklyPlanReminderOutboxStore",
    "WeeklyPlanReminderOutbox",
    "cancel_weekly_plan_reminder",
    "claim_weekly_plan_reminder",
    "create_weekly_plan_reminder_outbox",
    "fail_weekly_plan_reminder",
    "record_weekly_plan_delivery",
    "record_weekly_plan_provider_acceptance",
]
