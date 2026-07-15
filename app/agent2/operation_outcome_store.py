from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent2.business.models import Agent2OperationOutcome
from app.agent2.operation_outcomes import OperationOutcome


class OperationOutcomeIdempotencyConflictError(ValueError):
    """The same scoped idempotency key was used for different persisted facts."""

    code = "operation_outcome_idempotency_conflict"

    def __init__(self) -> None:
        super().__init__(self.code)


_PERSISTED_FACT_FIELDS = (
    "outcome_id",
    "tenant_id",
    "user_id",
    "conversation_id",
    "source_turn_id",
    "domain",
    "operation",
    "object_type",
    "object_id",
    "object_label",
    "object_version",
    "business_status",
    "message_status",
    "actual_write",
    "would_write",
    "changed_fields_json",
    "user_visible_snapshot_json",
    "blocking_reason",
    "receipt_refs_json",
    "audit_refs_json",
    "state_transition_json",
    "idempotency_key",
)


def _canonical_fact(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ("none",)
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, str):
        return ("string", value)
    if isinstance(value, int):
        return ("integer", value)
    if isinstance(value, float):
        return ("number", value)
    if isinstance(value, UUID):
        return ("uuid", value.hex)
    if isinstance(value, datetime):
        normalized = (
            value.astimezone(timezone.utc) if value.tzinfo is not None else value
        )
        return ("datetime", normalized.isoformat())
    if isinstance(value, list):
        return ("list", tuple(_canonical_fact(item) for item in value))
    if isinstance(value, dict):
        return (
            "object",
            tuple(
                (key, _canonical_fact(item))
                for key, item in sorted(value.items())
            ),
        )
    raise TypeError("operation_outcome_noncanonical_fact")


def _persisted_facts_match(
    existing: Agent2OperationOutcome,
    expected: dict[str, Any],
    *,
    compare_created_at: bool,
) -> bool:
    field_names = _PERSISTED_FACT_FIELDS + (("created_at",) if compare_created_at else ())
    try:
        return all(
            _canonical_fact(getattr(existing, field_name))
            == _canonical_fact(expected[field_name])
            for field_name in field_names
        )
    except (AttributeError, TypeError):
        return False


async def persist_operation_outcomes(
    session: AsyncSession,
    outcomes: tuple[OperationOutcome, ...],
    *,
    tenant_id: str,
    user_id: str,
    conversation_id: str,
    source_turn_id: str,
    now: datetime | None = None,
) -> int:
    """Idempotently persist the exact outcomes consumed by the Reply Composer."""
    trusted_scope = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "conversation_id": conversation_id,
        "source_turn_id": source_turn_id,
    }
    for field_name, value in trusted_scope.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"operation_outcome_scope_required:{field_name}")
    for outcome in outcomes:
        for field_name, trusted_value in trusted_scope.items():
            outcome_value = getattr(outcome, field_name)
            if outcome_value and outcome_value != trusted_value:
                raise ValueError(f"operation_outcome_scope_mismatch:{field_name}")
    now = now or datetime.now(timezone.utc)
    inserted = 0
    for index, outcome in enumerate(outcomes):
        scoped_tenant = tenant_id
        scoped_user = user_id
        scoped_conversation = conversation_id
        scoped_turn = source_turn_id
        stable_key = outcome.idempotency_key or (
            f"operation-outcome:{scoped_tenant}:{scoped_user}:{scoped_turn}:"
            f"{index}:{outcome.domain}:{outcome.operation}:"
            f"{outcome.object_ref.object_type}:{outcome.object_ref.stable_id}:"
            f"{outcome.business_status}:{outcome.message_status}"
        )
        try:
            outcome_id = UUID(outcome.outcome_id)
        except (TypeError, ValueError):
            outcome_id = uuid5(NAMESPACE_URL, stable_key)
        values = {
            "outcome_id": outcome_id,
            "tenant_id": scoped_tenant,
            "user_id": scoped_user,
            "conversation_id": scoped_conversation,
            "source_turn_id": scoped_turn,
            "domain": outcome.domain,
            "operation": outcome.operation,
            "object_type": outcome.object_ref.object_type,
            "object_id": outcome.object_ref.stable_id,
            "object_label": outcome.object_ref.label,
            "object_version": outcome.object_ref.version,
            "business_status": outcome.business_status,
            "message_status": outcome.message_status,
            "actual_write": outcome.actual_write,
            "would_write": outcome.would_write,
            "changed_fields_json": list(outcome.changed_fields),
            "user_visible_snapshot_json": dict(outcome.user_visible_snapshot),
            "blocking_reason": outcome.blocking_reason,
            "receipt_refs_json": [item.as_dict() for item in outcome.receipt_refs],
            "audit_refs_json": list(outcome.audit_refs),
            "state_transition_json": outcome.state_transition.as_dict(),
            "idempotency_key": stable_key,
            "created_at": outcome.created_at or now,
            "updated_at": now,
        }
        result = await session.execute(
            insert(Agent2OperationOutcome)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=[
                    Agent2OperationOutcome.tenant_id,
                    Agent2OperationOutcome.idempotency_key,
                ]
            )
            .returning(Agent2OperationOutcome.outcome_id)
        )
        if result.scalar_one_or_none() is not None:
            inserted += 1
            continue
        existing_result = await session.execute(
            select(Agent2OperationOutcome).where(
                Agent2OperationOutcome.tenant_id == scoped_tenant,
                Agent2OperationOutcome.idempotency_key == stable_key,
            )
        )
        existing = existing_result.scalar_one_or_none()
        if existing is None or not _persisted_facts_match(
            existing,
            values,
            compare_created_at=outcome.created_at is not None,
        ):
            raise OperationOutcomeIdempotencyConflictError()
    await session.flush()
    return inserted
