from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from uuid import NAMESPACE_URL, uuid5

from app.agent2.memory import validate_personal_memory_value
from app.agent2.tool_calling.contracts import (
    ForgetPersonalMemoryArgs,
    ReceiptStatus,
    RememberPersonalMemoryArgs,
)
from app.agent2.tool_calling.idempotency import build_write_idempotency_key
from app.agent2.tool_calling.sandbox_contracts import SandboxExecutionContext
from app.agent2.tool_calling.sandbox_daily_executor import (
    SandboxExecutionError,
    SandboxHandlerOutcome,
)
from app.agent2.tool_calling.sandbox_handlers import SandboxHandlerRequest
from app.agent2.tool_calling.sandbox_store import SandboxExecutionSession


def sandbox_personal_memory_id(
    context: SandboxExecutionContext,
    key: str,
) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            (
                "agent2-sandbox-personal-memory:"
                f"{context.tenant_id}:{context.user_id}:{key}"
            ),
        )
    )


def sandbox_personal_memory_audit_id(idempotency_key: str) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            f"agent2-sandbox-memory-audit:{idempotency_key}",
        )
    )


def sandbox_personal_memory_target_object(
    context: SandboxExecutionContext,
    key: str,
) -> str:
    return (
        f"personal_memory:{context.tenant_id}:"
        f"{context.user_id}:{key}"
    )


class SandboxPersonalMemoryExecutor:
    """Owns the isolated personal-memory state transitions and audit trail."""

    def __init__(
        self,
        *,
        session: SandboxExecutionSession,
        context: SandboxExecutionContext,
        max_active_entries: int = 20,
    ) -> None:
        if max_active_entries < 1 or max_active_entries > 20:
            raise ValueError("personal memory limit must be between 1 and 20")
        self._session = session
        self._context = context
        self._max_active_entries = max_active_entries

    async def query_personal_memory(
        self,
        request: SandboxHandlerRequest,
    ) -> SandboxHandlerOutcome:
        del request
        return SandboxHandlerOutcome(
            target_type="personal_memory",
            target_id="current_user",
            idempotency_key=None,
            status_if_unchanged=ReceiptStatus.SUCCESS,
        )

    async def remember_personal_memory(
        self,
        request: SandboxHandlerRequest,
    ) -> SandboxHandlerOutcome:
        arguments = self._arguments(
            request,
            RememberPersonalMemoryArgs,
        )
        key = arguments.memory_key
        memory_id = self._memory_id(key)
        current = await self._owned_record(memory_id, key=key)
        before_version = int(current["version"]) if current is not None else None
        idempotency_key = self._write_key(
            request,
            key=key,
            expected_version=before_version,
        )
        value = arguments.value.model_dump(mode="json")
        if (
            current is not None
            and current.get("status") == "active"
            and current.get("value") == value
        ):
            return SandboxHandlerOutcome(
                target_type="personal_memory",
                target_id=key,
                idempotency_key=idempotency_key,
                status_if_unchanged=ReceiptStatus.NO_OP,
            )

        if current is None or current.get("status") != "active":
            active = await self._active_owned_records()
            if len(active) >= self._max_active_entries:
                raise SandboxExecutionError(
                    "PERSONAL_MEMORY_LIMIT_REACHED"
                )
        next_version = (before_version or 0) + 1
        now = self._context.now.isoformat()
        after = {
            "memory_id": memory_id,
            "tenant_id": self._context.tenant_id,
            "user_id": str(self._context.user_id),
            "memory_type": "response_preference",
            "memory_key": key,
            "value": value,
            "source_kind": "explicit_user",
            "source_message_id": self._context.source_message_id,
            "status": "active",
            "version": next_version,
            "created_at": (
                current.get("created_at")
                if current is not None
                else now
            ),
            "updated_at": now,
            "expires_at": None,
        }
        await self._session.upsert_record(
            "personal_memory",
            memory_id,
            after,
        )
        await self._append_audit(
            request=request,
            idempotency_key=idempotency_key,
            key=key,
            action="create" if current is None else "replace",
            before=current,
            after=after,
        )
        return SandboxHandlerOutcome(
            target_type="personal_memory",
            target_id=key,
            idempotency_key=idempotency_key,
        )

    async def forget_personal_memory(
        self,
        request: SandboxHandlerRequest,
    ) -> SandboxHandlerOutcome:
        arguments = self._arguments(
            request,
            ForgetPersonalMemoryArgs,
        )
        key = arguments.memory_key
        memory_id = self._memory_id(key)
        current = await self._owned_record(memory_id, key=key)
        before_version = int(current["version"]) if current is not None else None
        idempotency_key = self._write_key(
            request,
            key=key,
            expected_version=before_version,
        )
        if current is None or current.get("status") != "active":
            return SandboxHandlerOutcome(
                target_type="personal_memory",
                target_id=key,
                idempotency_key=idempotency_key,
                status_if_unchanged=ReceiptStatus.NO_OP,
            )

        after = {
            **current,
            "status": "forgotten",
            "version": before_version + 1,
            "updated_at": self._context.now.isoformat(),
            "source_message_id": self._context.source_message_id,
        }
        await self._session.upsert_record(
            "personal_memory",
            memory_id,
            after,
        )
        await self._append_audit(
            request=request,
            idempotency_key=idempotency_key,
            key=key,
            action="forget",
            before=current,
            after=after,
        )
        return SandboxHandlerOutcome(
            target_type="personal_memory",
            target_id=key,
            idempotency_key=idempotency_key,
        )

    async def _active_owned_records(
        self,
    ) -> tuple[dict[str, object], ...]:
        return tuple(
            row
            for row in await self._session.read_table_rows(
                "personal_memory"
            )
            if row.get("tenant_id") == self._context.tenant_id
            and row.get("user_id") == str(self._context.user_id)
            and row.get("status") == "active"
        )

    async def _owned_record(
        self,
        memory_id: str,
        *,
        key: str,
    ) -> dict[str, object] | None:
        row = await self._session.read_record(
            "personal_memory",
            memory_id,
        )
        if row is None:
            return None
        if (
            row.get("tenant_id") != self._context.tenant_id
            or row.get("user_id") != str(self._context.user_id)
            or row.get("memory_key") != key
            or row.get("memory_type") != "response_preference"
            or row.get("memory_id") != memory_id
        ):
            raise SandboxExecutionError(
                "PERSONAL_MEMORY_BINDING_REJECTED"
            )
        version = row.get("version")
        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or version < 1
            or row.get("status") not in {"active", "forgotten"}
            or row.get("source_kind") != "explicit_user"
            or row.get("expires_at") is not None
            or not _aware_iso_timestamp(row.get("created_at"))
            or not _aware_iso_timestamp(row.get("updated_at"))
        ):
            raise SandboxExecutionError(
                "PERSONAL_MEMORY_BINDING_REJECTED"
            )
        try:
            value = validate_personal_memory_value(
                "response_preference",
                key,
                row.get("value"),
            )
        except ValueError as exc:
            raise SandboxExecutionError(
                "PERSONAL_MEMORY_BINDING_REJECTED"
            ) from exc
        if row.get("value") != value.model_dump(mode="json"):
            raise SandboxExecutionError(
                "PERSONAL_MEMORY_BINDING_REJECTED"
            )
        return row

    async def _append_audit(
        self,
        *,
        request: SandboxHandlerRequest,
        idempotency_key: str,
        key: str,
        action: str,
        before: Mapping[str, object] | None,
        after: Mapping[str, object],
    ) -> None:
        audit_id = sandbox_personal_memory_audit_id(idempotency_key)
        await self._session.upsert_record(
            "personal_memory_audit",
            audit_id,
            {
                "audit_id": audit_id,
                "tenant_id": self._context.tenant_id,
                "user_id": str(self._context.user_id),
                "conversation_id": self._context.conversation_id,
                "source_message_id": self._context.source_message_id,
                "turn_id": self._context.turn_id,
                "tool_call_id": request.tool_call_id,
                "tool_name": request.tool_name,
                "memory_key": key,
                "action": action,
                "before": dict(before) if before is not None else None,
                "after": dict(after),
                "occurred_at": self._context.now.isoformat(),
                "idempotency_key": idempotency_key,
            },
            create_only=True,
        )

    def _memory_id(self, key: str) -> str:
        return sandbox_personal_memory_id(self._context, key)

    def _write_key(
        self,
        request: SandboxHandlerRequest,
        *,
        key: str,
        expected_version: int | None,
    ) -> str:
        return build_write_idempotency_key(
            tenant_id=self._context.tenant_id,
            user_id=str(self._context.user_id),
            conversation_id=self._context.conversation_id,
            source_message_id=self._context.source_message_id,
            tool_call_id=request.tool_call_id,
            tool_name=request.tool_name,
            canonical_arguments=request.arguments.model_dump(mode="json"),
            target_object=sandbox_personal_memory_target_object(
                self._context,
                key,
            ),
            expected_version=expected_version,
        )

    @staticmethod
    def _arguments(
        request: SandboxHandlerRequest,
        expected_type: type,
    ):
        if not isinstance(request.arguments, expected_type):
            raise SandboxExecutionError(
                "HANDLER_ARGUMENT_TYPE_MISMATCH"
            )
        return request.arguments


def _aware_iso_timestamp(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None
