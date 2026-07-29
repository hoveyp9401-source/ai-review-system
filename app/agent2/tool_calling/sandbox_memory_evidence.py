from __future__ import annotations

from datetime import datetime

from app.agent2.memory import (
    model_visible_personal_memory_value,
    validate_personal_memory_value,
)
from app.agent2.tool_calling.contracts import (
    ForgetPersonalMemoryArgs,
    QueryPersonalMemoryArgs,
    ReceiptStatus,
    RememberPersonalMemoryArgs,
)
from app.agent2.tool_calling.idempotency import (
    build_write_idempotency_key,
)
from app.agent2.tool_calling.sandbox_contracts import (
    SandboxExecutionContext,
    SandboxSnapshot,
)
from app.agent2.tool_calling.sandbox_daily_executor import (
    SandboxExecutionError,
    SandboxHandlerOutcome,
)
from app.agent2.tool_calling.sandbox_memory_executor import (
    sandbox_personal_memory_audit_id,
    sandbox_personal_memory_id,
    sandbox_personal_memory_target_object,
)


_MEMORY_ROW_FIELDS = frozenset(
    {
        "memory_id",
        "tenant_id",
        "user_id",
        "memory_type",
        "memory_key",
        "value",
        "source_kind",
        "source_message_id",
        "status",
        "version",
        "created_at",
        "updated_at",
        "expires_at",
    }
)


def personal_memory_state_changed(
    before: SandboxSnapshot,
    after: SandboxSnapshot,
) -> bool:
    return (
        before.table_rows("personal_memory")
        != after.table_rows("personal_memory")
        or before.table_rows("personal_memory_audit")
        != after.table_rows("personal_memory_audit")
    )


def validate_personal_memory_outcome(
    outcome: SandboxHandlerOutcome,
    before: SandboxSnapshot,
    after: SandboxSnapshot,
    *,
    context: SandboxExecutionContext,
    arguments: object,
    tool_call_id: str,
    tool_name: str,
) -> None:
    expected_target_id = _expected_target_id(arguments)
    if outcome.target_id != expected_target_id:
        _binding_mismatch()
    expected_unchanged_status = (
        ReceiptStatus.SUCCESS
        if isinstance(arguments, QueryPersonalMemoryArgs)
        else ReceiptStatus.NO_OP
    )
    if outcome.status_if_unchanged != expected_unchanged_status:
        _binding_mismatch()

    before_memory = _rows_by_id(
        before,
        table_name="personal_memory",
        id_field="memory_id",
    )
    after_memory = _rows_by_id(
        after,
        table_name="personal_memory",
        id_field="memory_id",
    )
    before_audits = _rows_by_id(
        before,
        table_name="personal_memory_audit",
        id_field="audit_id",
    )
    after_audits = _rows_by_id(
        after,
        table_name="personal_memory_audit",
        id_field="audit_id",
    )
    changed_memory_ids = _changed_ids(before_memory, after_memory)
    changed_audit_ids = _changed_ids(before_audits, after_audits)

    if len(changed_memory_ids) > 1 or len(changed_audit_ids) > 1:
        _binding_mismatch()
    if not changed_memory_ids:
        if changed_audit_ids:
            _binding_mismatch()
        _validate_unchanged_outcome(
            outcome,
            before_memory,
            context=context,
            arguments=arguments,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
        )
        return
    if len(changed_audit_ids) != 1:
        _binding_mismatch()
    if isinstance(arguments, QueryPersonalMemoryArgs):
        _binding_mismatch()

    memory_id = next(iter(changed_memory_ids))
    expected_memory_id = sandbox_personal_memory_id(
        context,
        expected_target_id,
    )
    if memory_id != expected_memory_id:
        _binding_mismatch()
    memory_before = before_memory.get(memory_id)
    memory_after = after_memory.get(memory_id)
    if memory_after is None:
        _binding_mismatch()
    if memory_before is not None:
        _validate_memory_row(
            memory_before,
            context=context,
            key=expected_target_id,
        )

    before_version = (
        _strict_version(memory_before)
        if memory_before is not None
        else None
    )
    expected_after, action = _expected_after(
        memory_before,
        context=context,
        arguments=arguments,
        memory_id=memory_id,
    )
    if memory_after != expected_after:
        _binding_mismatch()
    _validate_memory_row(
        memory_after,
        context=context,
        key=expected_target_id,
    )

    expected_idempotency_key = _expected_idempotency_key(
        context=context,
        arguments=arguments,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        key=expected_target_id,
        expected_version=before_version,
    )
    if outcome.idempotency_key != expected_idempotency_key:
        _binding_mismatch()

    audit_id = next(iter(changed_audit_ids))
    expected_audit_id = sandbox_personal_memory_audit_id(
        expected_idempotency_key
    )
    if audit_id != expected_audit_id:
        _binding_mismatch()
    if audit_id in before_audits:
        _binding_mismatch()
    audit = after_audits[audit_id]
    expected_audit = {
        "audit_id": expected_audit_id,
        "tenant_id": context.tenant_id,
        "user_id": str(context.user_id),
        "conversation_id": context.conversation_id,
        "source_message_id": context.source_message_id,
        "turn_id": context.turn_id,
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "memory_key": expected_target_id,
        "action": action,
        "before": memory_before,
        "after": memory_after,
        "occurred_at": context.now.isoformat(),
        "idempotency_key": expected_idempotency_key,
    }
    if audit != expected_audit:
        _binding_mismatch()


def personal_memory_version(
    snapshot: SandboxSnapshot,
    target_id: str,
    *,
    context: SandboxExecutionContext,
) -> int | None:
    if target_id == "current_user":
        return None
    expected_id = sandbox_personal_memory_id(context, target_id)
    owned_matches = [
        row
        for row in snapshot.table_rows("personal_memory")
        if _owned(row, context)
        and row.get("memory_key") == target_id
    ]
    if not owned_matches:
        return 0
    if len(owned_matches) != 1:
        _binding_mismatch()
    row = owned_matches[0]
    if row.get("memory_id") != expected_id:
        _binding_mismatch()
    _validate_memory_row(row, context=context, key=target_id)
    return _strict_version(row)


def personal_memory_safe_user_facts(
    outcome: SandboxHandlerOutcome,
    snapshot: SandboxSnapshot,
    *,
    context: SandboxExecutionContext,
    actual_write: bool,
) -> dict[str, object]:
    memories = _safe_active_memories(snapshot, context)
    facts: dict[str, object] = {"memories": memories}
    if outcome.target_id == "current_user":
        return facts
    target = next(
        (
            item
            for item in memories
            if item["memory_key"] == outcome.target_id
        ),
        None,
    )
    facts.update(
        {
            "memory_key": outcome.target_id,
            "memory": target,
            "forgotten": target is None and actual_write,
        }
    )
    return facts


def _validate_unchanged_outcome(
    outcome: SandboxHandlerOutcome,
    records: dict[str, dict[str, object]],
    *,
    context: SandboxExecutionContext,
    arguments: object,
    tool_call_id: str,
    tool_name: str,
) -> None:
    if isinstance(arguments, QueryPersonalMemoryArgs):
        if outcome.idempotency_key is not None:
            _binding_mismatch()
        return
    if not isinstance(
        arguments,
        (RememberPersonalMemoryArgs, ForgetPersonalMemoryArgs),
    ):
        _binding_mismatch()
    key = arguments.memory_key
    memory_id = sandbox_personal_memory_id(context, key)
    current = records.get(memory_id)
    if current is not None:
        _validate_memory_row(current, context=context, key=key)
    expected_version = (
        _strict_version(current)
        if current is not None
        else None
    )
    if outcome.idempotency_key != _expected_idempotency_key(
        context=context,
        arguments=arguments,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        key=key,
        expected_version=expected_version,
    ):
        _binding_mismatch()
    if isinstance(arguments, RememberPersonalMemoryArgs):
        if (
            current is None
            or current.get("status") != "active"
            or current.get("value")
            != arguments.value.model_dump(mode="json")
        ):
            _binding_mismatch()
    elif current is not None and current.get("status") != "forgotten":
        _binding_mismatch()


def _expected_after(
    before: dict[str, object] | None,
    *,
    context: SandboxExecutionContext,
    arguments: object,
    memory_id: str,
) -> tuple[dict[str, object], str]:
    before_version = _strict_version(before) if before is not None else 0
    now = context.now.isoformat()
    if isinstance(arguments, RememberPersonalMemoryArgs):
        return (
            {
                "memory_id": memory_id,
                "tenant_id": context.tenant_id,
                "user_id": str(context.user_id),
                "memory_type": "response_preference",
                "memory_key": arguments.memory_key,
                "value": arguments.value.model_dump(mode="json"),
                "source_kind": "explicit_user",
                "source_message_id": context.source_message_id,
                "status": "active",
                "version": before_version + 1,
                "created_at": (
                    before.get("created_at")
                    if before is not None
                    else now
                ),
                "updated_at": now,
                "expires_at": None,
            },
            "create" if before is None else "replace",
        )
    if (
        not isinstance(arguments, ForgetPersonalMemoryArgs)
        or before is None
        or before.get("status") != "active"
    ):
        _binding_mismatch()
    return (
        {
            **before,
            "status": "forgotten",
            "version": before_version + 1,
            "updated_at": now,
            "source_message_id": context.source_message_id,
        },
        "forget",
    )


def _expected_target_id(arguments: object) -> str:
    if isinstance(arguments, QueryPersonalMemoryArgs):
        return "current_user"
    if isinstance(
        arguments,
        (RememberPersonalMemoryArgs, ForgetPersonalMemoryArgs),
    ):
        return arguments.memory_key
    _binding_mismatch()


def _expected_idempotency_key(
    *,
    context: SandboxExecutionContext,
    arguments: RememberPersonalMemoryArgs | ForgetPersonalMemoryArgs,
    tool_call_id: str,
    tool_name: str,
    key: str,
    expected_version: int | None,
) -> str:
    return build_write_idempotency_key(
        tenant_id=context.tenant_id,
        user_id=str(context.user_id),
        conversation_id=context.conversation_id,
        source_message_id=context.source_message_id,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        canonical_arguments=arguments.model_dump(mode="json"),
        target_object=sandbox_personal_memory_target_object(
            context,
            key,
        ),
        expected_version=expected_version,
    )


def _safe_active_memories(
    snapshot: SandboxSnapshot,
    context: SandboxExecutionContext,
) -> list[dict[str, object]]:
    safe: list[dict[str, object]] = []
    seen_keys: set[str] = set()
    for row in snapshot.table_rows("personal_memory"):
        if not _owned(row, context):
            continue
        key = row.get("memory_key")
        if not isinstance(key, str) or key in seen_keys:
            raise SandboxExecutionError(
                "PERSONAL_MEMORY_EVIDENCE_INVALID"
            )
        seen_keys.add(key)
        try:
            value = _validate_memory_row(
                row,
                context=context,
                key=key,
            )
        except SandboxExecutionError as exc:
            raise SandboxExecutionError(
                "PERSONAL_MEMORY_EVIDENCE_INVALID"
            ) from exc
        if row.get("status") != "active":
            continue
        safe.append(
            {
                "memory_key": key,
                "memory_type": "response_preference",
                "value": model_visible_personal_memory_value(
                    "response_preference",
                    key,
                    value,
                ),
            }
        )
    return sorted(safe, key=lambda item: str(item["memory_key"]))


def _validate_memory_row(
    row: dict[str, object],
    *,
    context: SandboxExecutionContext,
    key: str,
) -> dict[str, object]:
    if (
        set(row) != _MEMORY_ROW_FIELDS
        or not _owned(row, context)
        or row.get("memory_id")
        != sandbox_personal_memory_id(context, key)
        or row.get("memory_type") != "response_preference"
        or row.get("memory_key") != key
        or row.get("source_kind") != "explicit_user"
        or not isinstance(row.get("source_message_id"), str)
        or not row.get("source_message_id")
        or row.get("status") not in {"active", "forgotten"}
        or row.get("expires_at") is not None
    ):
        _binding_mismatch()
    _strict_version(row)
    created_at = _aware_datetime(row.get("created_at"))
    updated_at = _aware_datetime(row.get("updated_at"))
    if created_at > updated_at:
        _binding_mismatch()
    try:
        value = validate_personal_memory_value(
            "response_preference",
            key,
            row.get("value"),
        ).model_dump(mode="json")
    except ValueError:
        _binding_mismatch()
    if row.get("value") != value:
        _binding_mismatch()
    return value


def _strict_version(row: dict[str, object] | None) -> int:
    if row is None:
        _binding_mismatch()
    version = row.get("version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version < 1
    ):
        _binding_mismatch()
    return version


def _aware_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        _binding_mismatch()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        _binding_mismatch()
    if parsed.tzinfo is None:
        _binding_mismatch()
    return parsed


def _rows_by_id(
    snapshot: SandboxSnapshot,
    *,
    table_name: str,
    id_field: str,
) -> dict[str, dict[str, object]]:
    indexed: dict[str, dict[str, object]] = {}
    for row in snapshot.table_rows(table_name):
        record_id = row.get(id_field)
        if (
            not isinstance(record_id, str)
            or not record_id
            or record_id in indexed
        ):
            _binding_mismatch()
        indexed[record_id] = row
    return indexed


def _changed_ids(
    before: dict[str, dict[str, object]],
    after: dict[str, dict[str, object]],
) -> set[str]:
    return {
        record_id
        for record_id in before.keys() | after.keys()
        if before.get(record_id) != after.get(record_id)
    }


def _owned(
    row: dict[str, object],
    context: SandboxExecutionContext,
) -> bool:
    return (
        row.get("tenant_id") == context.tenant_id
        and row.get("user_id") == str(context.user_id)
    )


def _binding_mismatch() -> None:
    raise SandboxExecutionError(
        "HANDLER_BINDING_EVIDENCE_MISMATCH"
    )
