from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
import hashlib
import json
from typing import Protocol

from sqlalchemy import select

from app.agent2.memory import (
    PreferredSalutationValue,
    TrustedPersonalMemory,
    validate_personal_memory_value,
)
from app.agent2.memory.postgres import (
    PersonalMemoryAuditRecord,
    PersonalMemoryRecord,
)
from app.agent2.personal_memory_reply import (
    address_with_preferred_salutation,
    compose_preferred_salutation_onboarding,
)
from app.agent2.tool_calling.context import (
    CANARY_STATE_NAMESPACE,
    TrustedContext,
)
from app.agent2.tool_calling.production_memory_executor import (
    production_personal_memory_audit_id,
    production_personal_memory_id,
)


PREFERRED_SALUTATION_KEY = "response.preferred_salutation"
ONBOARDING_TOOL_NAME = "server_personal_memory_onboarding"


@dataclass(frozen=True)
class ResolvedPreferredSalutation:
    salutation: str
    include_onboarding: bool


class PersonalMemoryOnboardingStore(Protocol):
    async def resolve(
        self,
        *,
        context: TrustedContext,
        memory: TrustedPersonalMemory,
    ) -> ResolvedPreferredSalutation | None: ...


class PersonalMemoryOnboarding:
    """Render one server-owned salutation and introduce seeded memory once."""

    def __init__(self, *, store: PersonalMemoryOnboardingStore) -> None:
        self._store = store

    async def apply(
        self,
        *,
        context: TrustedContext,
        content: str,
    ) -> str:
        memory = _trusted_salutation(context)
        if memory is None:
            return content
        resolved = await self._store.resolve(
            context=context,
            memory=memory,
        )
        if resolved is None:
            return content
        if resolved.include_onboarding:
            return compose_preferred_salutation_onboarding(
                content=content,
                salutation=resolved.salutation,
                authenticated_display_name=(
                    context.principal.display_name
                ),
            )
        return address_with_preferred_salutation(
            content=content,
            salutation=resolved.salutation,
            authenticated_display_name=(
                context.principal.display_name
            ),
        )


class PostgresPersonalMemoryOnboardingStore:
    """Compose one persisted first-use notice with append-only evidence."""

    def __init__(self, session) -> None:
        self._session = session

    async def resolve(
        self,
        *,
        context: TrustedContext,
        memory: TrustedPersonalMemory,
    ) -> ResolvedPreferredSalutation | None:
        if context.namespace != CANARY_STATE_NAMESPACE:
            raise ValueError("salutation onboarding requires canary context")
        row = await self._session.scalar(
            select(PersonalMemoryRecord)
            .where(
                PersonalMemoryRecord.memory_id == memory.memory_id,
                PersonalMemoryRecord.tenant_id
                == context.principal.tenant_id,
                PersonalMemoryRecord.user_id
                == context.principal.user_id,
                PersonalMemoryRecord.memory_key
                == PREFERRED_SALUTATION_KEY,
                PersonalMemoryRecord.status == "active",
            )
            .with_for_update()
        )
        if row is None:
            return None
        if row.source_kind == "explicit_user":
            validated = _validate_current_user_row(
                context=context,
                memory=memory,
                row=row,
            )
            return ResolvedPreferredSalutation(
                salutation=validated.salutation,
                include_onboarding=False,
            )
        validated = _validate_claim_row(
            context=context,
            memory=memory,
            row=row,
        )
        existing = await self._session.scalar(
            select(PersonalMemoryAuditRecord.audit_id).where(
                PersonalMemoryAuditRecord.tenant_id
                == context.principal.tenant_id,
                PersonalMemoryAuditRecord.user_id
                == context.principal.user_id,
                PersonalMemoryAuditRecord.memory_id == row.memory_id,
                PersonalMemoryAuditRecord.action == "compose",
                PersonalMemoryAuditRecord.tool_name
                == ONBOARDING_TOOL_NAME,
            )
        )
        if existing is not None:
            return ResolvedPreferredSalutation(
                salutation=validated.salutation,
                include_onboarding=False,
            )

        idempotency_key = _onboarding_idempotency_key(row)
        payload = _record_payload(row)
        self._session.add(
            PersonalMemoryAuditRecord(
                audit_id=production_personal_memory_audit_id(
                    idempotency_key
                ),
                tenant_id=row.tenant_id,
                user_id=row.user_id,
                memory_id=row.memory_id,
                conversation_id=context.principal.conversation_id,
                source_message_id=context.principal.source_message_id,
                tool_call_id=(
                    f"server-onboarding-{str(row.memory_id)}"
                ),
                tool_name=ONBOARDING_TOOL_NAME,
                memory_key=row.memory_key,
                action="compose",
                before_json=payload,
                after_json=payload,
                idempotency_key=idempotency_key,
                occurred_at=context.now,
            )
        )
        await self._session.flush()
        return ResolvedPreferredSalutation(
            salutation=validated.salutation,
            include_onboarding=True,
        )


def _trusted_salutation(
    context: TrustedContext,
) -> TrustedPersonalMemory | None:
    if context.personal_memory is None:
        return None
    for entry in context.personal_memory.entries:
        if (
            entry.memory_key == PREFERRED_SALUTATION_KEY
            and entry.memory_type == "response_preference"
            and entry.source_kind
            in {"server_verified", "explicit_user"}
            and isinstance(entry.value, PreferredSalutationValue)
        ):
            return entry
    return None


def _validate_claim_row(
    *,
    context: TrustedContext,
    memory: TrustedPersonalMemory,
    row: PersonalMemoryRecord,
) -> PreferredSalutationValue:
    expected_id = production_personal_memory_id(
        tenant_id=context.principal.tenant_id,
        user_id=context.principal.user_id,
        key=PREFERRED_SALUTATION_KEY,
    )
    validated = validate_personal_memory_value(
        "response_preference",
        PREFERRED_SALUTATION_KEY,
        row.value_json,
    )
    if (
        row.memory_id != expected_id
        or row.memory_id != memory.memory_id
        or row.tenant_id != memory.tenant_id
        or row.user_id != memory.user_id
        or memory.source_kind != "server_verified"
        or row.memory_type != "response_preference"
        or row.memory_key != memory.memory_key
        or row.source_kind != "server_verified"
        or row.status != "active"
        or row.version != memory.version
        or row.expires_at is not None
        or not isinstance(validated, PreferredSalutationValue)
        or validated != memory.value
    ):
        raise ValueError("salutation onboarding memory binding mismatch")
    return validated


def _validate_current_user_row(
    *,
    context: TrustedContext,
    memory: TrustedPersonalMemory,
    row: PersonalMemoryRecord,
) -> PreferredSalutationValue:
    expected_id = production_personal_memory_id(
        tenant_id=context.principal.tenant_id,
        user_id=context.principal.user_id,
        key=PREFERRED_SALUTATION_KEY,
    )
    validated = validate_personal_memory_value(
        "response_preference",
        PREFERRED_SALUTATION_KEY,
        row.value_json,
    )
    if (
        row.memory_id != expected_id
        or row.memory_id != memory.memory_id
        or row.tenant_id != context.principal.tenant_id
        or row.tenant_id != memory.tenant_id
        or row.user_id != context.principal.user_id
        or row.user_id != memory.user_id
        or row.memory_type != "response_preference"
        or row.memory_type != memory.memory_type
        or row.memory_key != PREFERRED_SALUTATION_KEY
        or row.memory_key != memory.memory_key
        or row.status != "active"
        or row.expires_at is not None
        or not isinstance(validated, PreferredSalutationValue)
    ):
        raise ValueError("salutation onboarding user memory mismatch")
    return validated


def _onboarding_idempotency_key(
    row: PersonalMemoryRecord,
) -> str:
    canonical = json.dumps(
        {
            "contract": "agent2.personal_memory_onboarding.v1",
            "tenant_id": row.tenant_id,
            "user_id": str(row.user_id),
            "memory_id": str(row.memory_id),
            "memory_key": row.memory_key,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _record_payload(
    row: PersonalMemoryRecord,
) -> dict[str, object]:
    return {
        "memory_id": str(row.memory_id),
        "tenant_id": row.tenant_id,
        "user_id": str(row.user_id),
        "memory_type": row.memory_type,
        "memory_key": row.memory_key,
        "value": dict(row.value_json),
        "source_kind": row.source_kind,
        "source_message_id": row.source_message_id,
        "status": row.status,
        "version": row.version,
        "created_at": row.created_at.astimezone(UTC).isoformat(),
        "updated_at": row.updated_at.astimezone(UTC).isoformat(),
        "expires_at": None,
    }
