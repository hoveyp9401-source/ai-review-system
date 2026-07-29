from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, get_args
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, select, text

from app.agent2.memory import (
    model_visible_personal_memory_value,
    validate_personal_memory_value,
)
from app.agent2.memory.postgres import (
    PersonalMemoryAuditRecord,
    PersonalMemoryRecord,
)
from app.agent2.tool_calling.contracts import (
    ForgetPersonalMemoryArgs,
    PersonalMemoryKey,
    QueryPersonalMemoryArgs,
    ReceiptStatus,
    RememberPersonalMemoryArgs,
)
from app.agent2.tool_calling.idempotency import build_write_idempotency_key
from app.agent2.tool_calling.production_daily_executor import (
    ProductionExecutionError,
    ProductionHandlerOutcome,
)
from app.agent2.tool_calling.production_handlers import (
    ProductionHandlerRequest,
)


_SUPPORTED_MEMORY_KEYS = tuple(get_args(PersonalMemoryKey))


def production_personal_memory_id(
    *,
    tenant_id: str,
    user_id: UUID,
    key: str,
) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"agent2-personal-memory:{tenant_id}:{user_id}:{key}",
    )


def production_personal_memory_audit_id(
    idempotency_key: str,
) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"agent2-personal-memory-audit:{idempotency_key}",
    )


def production_personal_memory_idempotency_key(
    *,
    tenant_id: str,
    user_id: UUID,
    conversation_id: str,
    source_message_id: str,
    tool_call_id: str,
    tool_name: str,
    canonical_arguments: dict[str, Any],
    key: str,
    expected_version: int | None,
) -> str:
    return build_write_idempotency_key(
        tenant_id=tenant_id,
        user_id=str(user_id),
        conversation_id=conversation_id,
        source_message_id=source_message_id,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        canonical_arguments=canonical_arguments,
        target_object=(
            f"personal_memory:{tenant_id}:{user_id}:{key}"
        ),
        expected_version=expected_version,
    )


class ProductionPersonalMemoryExecutor:
    """Execute the four-key personal-memory contract in the Canary transaction."""

    def __init__(
        self,
        *,
        session: Any,
        context: Any,
        max_active_entries: int = 20,
    ) -> None:
        if max_active_entries < 1 or max_active_entries > 20:
            raise ValueError(
                "personal memory limit must be between 1 and 20"
            )
        self._session = session
        self._context = context
        self._max_active_entries = max_active_entries

    async def query_personal_memory(
        self,
        request: ProductionHandlerRequest,
    ) -> ProductionHandlerOutcome:
        self._arguments(request, QueryPersonalMemoryArgs)
        return ProductionHandlerOutcome(
            target_type="personal_memory",
            target_id="current_user",
            before_report=None,
            after_report=None,
            idempotency_key=None,
            safe_user_facts={
                "memories": await self._safe_active_memories(),
            },
            status_if_unchanged=ReceiptStatus.SUCCESS,
        )

    async def remember_personal_memory(
        self,
        request: ProductionHandlerRequest,
    ) -> ProductionHandlerOutcome:
        arguments = self._arguments(
            request,
            RememberPersonalMemoryArgs,
        )
        await self._lock_scope()
        current = await self._owned_record(
            arguments.memory_key,
            for_update=True,
        )
        before_version = current.version if current is not None else 0
        idempotency_key = self._write_key(
            request,
            key=arguments.memory_key,
            expected_version=(
                current.version if current is not None else None
            ),
        )
        value = arguments.value.model_dump(mode="json")
        if (
            current is not None
            and current.status == "active"
            and current.source_kind == "explicit_user"
            and current.value_json == value
        ):
            return ProductionHandlerOutcome(
                target_type="personal_memory",
                target_id=arguments.memory_key,
                before_report=None,
                after_report=None,
                idempotency_key=idempotency_key,
                safe_user_facts={
                    "memory_key": arguments.memory_key,
                    "memory": _safe_payload(current),
                    "forgotten": False,
                },
                status_if_unchanged=ReceiptStatus.NO_OP,
                before_version=before_version,
                after_version=before_version,
            )
        if current is None or current.status != "active":
            active_count = int(
                await self._session.scalar(
                    select(func.count())
                    .select_from(PersonalMemoryRecord)
                    .where(
                        PersonalMemoryRecord.tenant_id
                        == self._tenant_id,
                        PersonalMemoryRecord.user_id == self._user_id,
                        PersonalMemoryRecord.status == "active",
                    )
                )
                or 0
            )
            if active_count >= self._max_active_entries:
                raise ProductionExecutionError(
                    "PERSONAL_MEMORY_LIMIT_REACHED"
                )

        before_payload = (
            _record_payload(current) if current is not None else None
        )
        next_version = before_version + 1
        if current is None:
            current = PersonalMemoryRecord(
                memory_id=self._memory_id(arguments.memory_key),
                tenant_id=self._tenant_id,
                user_id=self._user_id,
                memory_type="response_preference",
                memory_key=arguments.memory_key,
                value_json=value,
                source_kind="explicit_user",
                source_message_id=self._source_message_id,
                status="active",
                version=next_version,
                expires_at=None,
                created_at=self._now,
                updated_at=self._now,
            )
            self._session.add(current)
            action = "create"
        else:
            current.value_json = value
            current.source_kind = "explicit_user"
            current.source_message_id = self._source_message_id
            current.status = "active"
            current.version = next_version
            current.expires_at = None
            current.updated_at = self._now
            action = "replace"
        await self._session.flush()
        await self._session.refresh(current)
        after_payload = _record_payload(current)
        await self._append_audit(
            request=request,
            idempotency_key=idempotency_key,
            action=action,
            record=current,
            before=before_payload,
            after=after_payload,
        )
        return ProductionHandlerOutcome(
            target_type="personal_memory",
            target_id=arguments.memory_key,
            before_report=None,
            after_report=None,
            idempotency_key=idempotency_key,
            safe_user_facts={
                "memory_key": arguments.memory_key,
                "memory": _safe_payload(current),
                "forgotten": False,
            },
            before_version=before_version,
            after_version=next_version,
        )

    async def forget_personal_memory(
        self,
        request: ProductionHandlerRequest,
    ) -> ProductionHandlerOutcome:
        arguments = self._arguments(
            request,
            ForgetPersonalMemoryArgs,
        )
        await self._lock_scope()
        current = await self._owned_record(
            arguments.memory_key,
            for_update=True,
        )
        before_version = current.version if current is not None else 0
        idempotency_key = self._write_key(
            request,
            key=arguments.memory_key,
            expected_version=(
                current.version if current is not None else None
            ),
        )
        if current is None or current.status != "active":
            return ProductionHandlerOutcome(
                target_type="personal_memory",
                target_id=arguments.memory_key,
                before_report=None,
                after_report=None,
                idempotency_key=idempotency_key,
                safe_user_facts={
                    "memory_key": arguments.memory_key,
                    "memory": None,
                    "forgotten": False,
                },
                status_if_unchanged=ReceiptStatus.NO_OP,
                before_version=before_version,
                after_version=before_version,
            )

        before_payload = _record_payload(current)
        current.status = "forgotten"
        current.version += 1
        current.source_message_id = self._source_message_id
        current.updated_at = self._now
        await self._session.flush()
        await self._session.refresh(current)
        after_payload = _record_payload(current)
        await self._append_audit(
            request=request,
            idempotency_key=idempotency_key,
            action="forget",
            record=current,
            before=before_payload,
            after=after_payload,
        )
        return ProductionHandlerOutcome(
            target_type="personal_memory",
            target_id=arguments.memory_key,
            before_report=None,
            after_report=None,
            idempotency_key=idempotency_key,
            safe_user_facts={
                "memory_key": arguments.memory_key,
                "memory": None,
                "forgotten": True,
            },
            before_version=before_version,
            after_version=current.version,
        )

    async def _lock_scope(self) -> None:
        key = (
            f"agent2-personal-memory:{self._tenant_id}:"
            f"{self._user_id}"
        )
        await self._session.execute(
            text(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended(:key, 0))"
            ),
            {"key": key},
        )

    async def _owned_record(
        self,
        key: str,
        *,
        for_update: bool,
    ) -> PersonalMemoryRecord | None:
        statement = select(PersonalMemoryRecord).where(
            PersonalMemoryRecord.tenant_id == self._tenant_id,
            PersonalMemoryRecord.user_id == self._user_id,
            PersonalMemoryRecord.memory_key == key,
        )
        if for_update:
            statement = statement.with_for_update()
        rows = list((await self._session.scalars(statement)).all())
        if not rows:
            return None
        if len(rows) != 1:
            raise ProductionExecutionError(
                "PERSONAL_MEMORY_BINDING_REJECTED"
            )
        row = rows[0]
        if (
            row.memory_id != self._memory_id(key)
            or row.memory_type != "response_preference"
            or row.source_kind
            not in {"explicit_user", "server_verified"}
            or row.status not in {"active", "forgotten"}
            or isinstance(row.version, bool)
            or not isinstance(row.version, int)
            or row.version < 1
            or row.expires_at is not None
            or not _aware(row.created_at)
            or not _aware(row.updated_at)
        ):
            raise ProductionExecutionError(
                "PERSONAL_MEMORY_BINDING_REJECTED"
            )
        try:
            validated = validate_personal_memory_value(
                "response_preference",
                key,
                row.value_json,
            )
        except ValueError as exc:
            raise ProductionExecutionError(
                "PERSONAL_MEMORY_BINDING_REJECTED"
            ) from exc
        if validated.model_dump(mode="json") != row.value_json:
            raise ProductionExecutionError(
                "PERSONAL_MEMORY_BINDING_REJECTED"
            )
        return row

    async def _safe_active_memories(
        self,
    ) -> list[dict[str, object]]:
        rows = list(
            (
                await self._session.scalars(
                    select(PersonalMemoryRecord)
                    .where(
                        PersonalMemoryRecord.tenant_id
                        == self._tenant_id,
                        PersonalMemoryRecord.user_id == self._user_id,
                        PersonalMemoryRecord.memory_type
                        == "response_preference",
                        PersonalMemoryRecord.memory_key.in_(
                            _SUPPORTED_MEMORY_KEYS
                        ),
                        PersonalMemoryRecord.status == "active",
                        PersonalMemoryRecord.expires_at.is_(None),
                    )
                    .order_by(
                        PersonalMemoryRecord.updated_at.desc(),
                        PersonalMemoryRecord.memory_key,
                    )
                )
            ).all()
        )
        if len(rows) > self._max_active_entries:
            raise ProductionExecutionError(
                "PERSONAL_MEMORY_LIMIT_REACHED"
            )
        memories: list[dict[str, object]] = []
        for row in rows:
            validated = await self._owned_record(
                row.memory_key,
                for_update=False,
            )
            if validated is None:
                raise ProductionExecutionError(
                    "PERSONAL_MEMORY_BINDING_REJECTED"
                )
            memories.append(_safe_payload(validated))
        return memories

    async def _append_audit(
        self,
        *,
        request: ProductionHandlerRequest,
        idempotency_key: str,
        action: str,
        record: PersonalMemoryRecord,
        before: dict[str, object] | None,
        after: dict[str, object],
    ) -> None:
        audit_id = production_personal_memory_audit_id(idempotency_key)
        existing = await self._session.get(
            PersonalMemoryAuditRecord,
            audit_id,
        )
        if existing is not None:
            raise ProductionExecutionError(
                "PERSONAL_MEMORY_AUDIT_NOT_APPEND_ONLY"
            )
        self._session.add(
            PersonalMemoryAuditRecord(
                audit_id=audit_id,
                tenant_id=self._tenant_id,
                user_id=self._user_id,
                memory_id=record.memory_id,
                conversation_id=self._conversation_id,
                source_message_id=self._source_message_id,
                tool_call_id=request.tool_call_id,
                tool_name=request.tool_name,
                memory_key=record.memory_key,
                action=action,
                before_json=before,
                after_json=after,
                idempotency_key=idempotency_key,
                occurred_at=self._now,
            )
        )

    def _write_key(
        self,
        request: ProductionHandlerRequest,
        *,
        key: str,
        expected_version: int | None,
    ) -> str:
        return production_personal_memory_idempotency_key(
            tenant_id=self._tenant_id,
            user_id=self._user_id,
            conversation_id=self._conversation_id,
            source_message_id=self._source_message_id,
            tool_call_id=request.tool_call_id,
            tool_name=request.tool_name,
            canonical_arguments=request.arguments.model_dump(mode="json"),
            key=key,
            expected_version=expected_version,
        )

    def _memory_id(self, key: str) -> UUID:
        return production_personal_memory_id(
            tenant_id=self._tenant_id,
            user_id=self._user_id,
            key=key,
        )

    @property
    def _tenant_id(self) -> str:
        return self._context.principal.tenant_id

    @property
    def _user_id(self) -> UUID:
        return self._context.principal.user_id

    @property
    def _conversation_id(self) -> str:
        return self._context.principal.conversation_id

    @property
    def _source_message_id(self) -> str:
        return self._context.principal.source_message_id

    @property
    def _now(self) -> datetime:
        return self._context.now

    @staticmethod
    def _arguments(
        request: ProductionHandlerRequest,
        expected_type: type,
    ):
        if not isinstance(request.arguments, expected_type):
            raise ProductionExecutionError("TYPED_ARGUMENTS_REQUIRED")
        return request.arguments


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
        "expires_at": (
            row.expires_at.astimezone(UTC).isoformat()
            if row.expires_at is not None
            else None
        ),
    }


def _safe_payload(
    row: PersonalMemoryRecord,
) -> dict[str, object]:
    return {
        "memory_type": row.memory_type,
        "memory_key": row.memory_key,
        "value": model_visible_personal_memory_value(
            "response_preference",
            row.memory_key,
            row.value_json,
        ),
        "provenance": "server_personal_memory",
    }


def _aware(value: datetime | None) -> bool:
    return value is not None and value.tzinfo is not None
