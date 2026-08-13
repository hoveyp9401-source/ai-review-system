from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, get_args
from uuid import UUID

from app.agent2.memory import (
    model_visible_personal_memory_value,
    validate_personal_memory_value,
)
from app.agent2.tool_calling.context import TrustedContext
from app.agent2.tool_calling.contracts import (
    ForgetPersonalMemoryArgs,
    PersonalMemoryKey,
    QueryPersonalMemoryArgs,
    RememberPersonalMemoryArgs,
)
from app.agent2.tool_calling.production_memory_executor import (
    production_personal_memory_audit_id,
    production_personal_memory_id,
    production_personal_memory_idempotency_key,
)
from app.agent2.tool_calling.registry import TOOL_REGISTRY
from app.agent2.tool_calling.validation import BoundCall


_MEMORY_ARGUMENT_TYPES = (
    QueryPersonalMemoryArgs,
    RememberPersonalMemoryArgs,
    ForgetPersonalMemoryArgs,
)
_SUPPORTED_MEMORY_KEYS = frozenset(get_args(PersonalMemoryKey))
_DATABASE_CLOCK_TOLERANCE = timedelta(minutes=5)


def is_personal_memory_call(bound: BoundCall) -> bool:
    definition = TOOL_REGISTRY[bound.call.tool_name]
    typed_arguments = definition.input_model.model_validate(
        bound.arguments
    )
    return isinstance(typed_arguments, _MEMORY_ARGUMENT_TYPES)


def personal_memory_safe_user_facts(
    *,
    bound: BoundCall,
    outcome: Any,
    after_payload: dict[str, Any],
    changed: bool,
) -> dict[str, Any]:
    typed_arguments = TOOL_REGISTRY[
        bound.call.tool_name
    ].input_model.model_validate(bound.arguments)
    active: list[dict[str, object]] = []
    for row in after_payload["personal_memories"]:
        if row["status"] != "active":
            continue
        key = row.get("memory_key")
        if (
            row.get("memory_type") != "response_preference"
            or key not in _SUPPORTED_MEMORY_KEYS
        ):
            continue
        if not isinstance(key, str) or not _valid_state_row(row, key=key):
            raise ValueError("personal memory evidence is outside scope")
        validated_value = validate_personal_memory_value(
            "response_preference",
            key,
            row.get("value"),
        )
        active.append(
            {
                "memory_type": "response_preference",
                "memory_key": key,
                "value": model_visible_personal_memory_value(
                    "response_preference",
                    key,
                    validated_value,
                ),
                "provenance": "server_personal_memory",
            }
        )
    active.sort(key=lambda row: str(row["memory_key"]))
    if isinstance(typed_arguments, QueryPersonalMemoryArgs):
        return {"memories": active}
    target = next(
        (
            row
            for row in active
            if row["memory_key"] == outcome.target_id
        ),
        None,
    )
    return {
        "memory_key": outcome.target_id,
        "memory": target,
        "forgotten": bool(
            isinstance(typed_arguments, ForgetPersonalMemoryArgs)
            and changed
            and target is None
        ),
    }


def personal_memory_evidence_matches(
    *,
    bound: BoundCall,
    outcome: Any,
    before_payload: dict[str, Any],
    after_payload: dict[str, Any],
    changed: bool,
    before_version: int | None,
    after_version: int | None,
    context: TrustedContext | None = None,
) -> bool:
    definition = TOOL_REGISTRY[bound.call.tool_name]
    typed_arguments = definition.input_model.model_validate(
        bound.arguments
    )
    memory_call = isinstance(typed_arguments, _MEMORY_ARGUMENT_TYPES)
    memory_unchanged = bool(
        before_payload["personal_memories"]
        == after_payload["personal_memories"]
        and before_payload["personal_memory_audits"]
        == after_payload["personal_memory_audits"]
    )
    business_unchanged = bool(
        before_payload["daily_reports"]
        == after_payload["daily_reports"]
        and before_payload["clear_pendings"]
        == after_payload["clear_pendings"]
        and before_payload.get("weekly_plans", [])
        == after_payload.get("weekly_plans", [])
    )
    if not memory_call:
        return bool(
            outcome.target_type != "personal_memory"
            and memory_unchanged
        )
    if outcome.target_type != "personal_memory" or not business_unchanged:
        return False
    tool_name = bound.call.tool_name
    arguments = bound.arguments
    if isinstance(typed_arguments, QueryPersonalMemoryArgs):
        return bool(
            outcome.target_id == "current_user"
            and outcome.idempotency_key is None
            and not changed
            and before_version is None
            and after_version is None
            and before_payload["personal_memories"]
            == after_payload["personal_memories"]
            and before_payload["personal_memory_audits"]
            == after_payload["personal_memory_audits"]
        )
    if not isinstance(
        typed_arguments,
        (RememberPersonalMemoryArgs, ForgetPersonalMemoryArgs),
    ):
        return False
    if context is None:
        return False
    key = arguments.get("memory_key")
    if not isinstance(key, str) or outcome.target_id != key:
        return False

    before_rows = {
        row["memory_id"]: row
        for row in before_payload["personal_memories"]
    }
    after_rows = {
        row["memory_id"]: row
        for row in after_payload["personal_memories"]
    }
    before_matches = [
        row for row in before_rows.values() if row["memory_key"] == key
    ]
    after_matches = [
        row for row in after_rows.values() if row["memory_key"] == key
    ]
    if len(before_matches) > 1:
        return False
    before_row = before_matches[0] if before_matches else None
    expected_idempotency_key = (
        production_personal_memory_idempotency_key(
            tenant_id=context.principal.tenant_id,
            user_id=context.principal.user_id,
            conversation_id=context.principal.conversation_id,
            source_message_id=context.principal.source_message_id,
            tool_call_id=bound.call.tool_call_id,
            tool_name=tool_name,
            canonical_arguments=typed_arguments.model_dump(mode="json"),
            key=key,
            expected_version=(
                int(before_row["version"])
                if before_row is not None
                else None
            ),
        )
    )
    if outcome.idempotency_key != expected_idempotency_key:
        return False
    if len(after_matches) != 1:
        if not changed and not before_matches and not after_matches:
            return (
                isinstance(typed_arguments, ForgetPersonalMemoryArgs)
                and before_version == 0
                and after_version == 0
            )
        return False
    after_row = after_matches[0]
    if not _valid_state_row(after_row, key=key):
        return False
    if before_row is not None and not _valid_state_row(
        before_row,
        key=key,
    ):
        return False
    expected_before_version = (
        int(before_row["version"]) if before_row is not None else 0
    )
    expected_after_version = int(after_row["version"])
    if (
        before_version != expected_before_version
        or after_version != expected_after_version
    ):
        return False
    changed_memory_ids = {
        memory_id
        for memory_id in set(before_rows) | set(after_rows)
        if before_rows.get(memory_id) != after_rows.get(memory_id)
    }
    before_audits = {
        row["audit_id"]: row
        for row in before_payload["personal_memory_audits"]
    }
    after_audits = {
        row["audit_id"]: row
        for row in after_payload["personal_memory_audits"]
    }
    changed_audit_ids = {
        audit_id
        for audit_id in set(before_audits) | set(after_audits)
        if before_audits.get(audit_id) != after_audits.get(audit_id)
    }
    if not changed:
        return bool(
            not changed_memory_ids
            and not changed_audit_ids
            and before_row == after_row
            and (
                (
                    isinstance(
                        typed_arguments,
                        RememberPersonalMemoryArgs,
                    )
                    and after_row["status"] == "active"
                    and after_row["value"] == arguments.get("value")
                )
                or (
                    isinstance(
                        typed_arguments,
                        ForgetPersonalMemoryArgs,
                    )
                    and after_row["status"] != "active"
                )
            )
        )
    if (
        changed_memory_ids != {after_row["memory_id"]}
        or len(changed_audit_ids) != 1
    ):
        return False
    audit_id = next(iter(changed_audit_ids))
    if audit_id in before_audits:
        return False
    audit = after_audits.get(audit_id)
    expected_audit_id = str(
        production_personal_memory_audit_id(
            expected_idempotency_key
        )
    )
    if audit is None or audit_id != expected_audit_id:
        return False
    if (
        audit["memory_id"] != after_row["memory_id"]
        or audit["memory_key"] != key
        or audit["tool_call_id"] != bound.call.tool_call_id
        or audit["tool_name"] != tool_name
        or audit["idempotency_key"] != expected_idempotency_key
        or audit["source_message_id"]
        != context.principal.source_message_id
        or audit["conversation_id"]
        != context.principal.conversation_id
        or audit["occurred_at"]
        != context.now.astimezone(UTC).isoformat()
        or audit["before"] != before_row
        or audit["after"] != after_row
        or after_row["source_message_id"]
        != context.principal.source_message_id
        or expected_after_version != expected_before_version + 1
    ):
        return False
    if before_row is None:
        if (
            after_row["created_at"]
            != context.now.astimezone(UTC).isoformat()
            or after_row["updated_at"] != after_row["created_at"]
        ):
            return False
    else:
        before_updated_at = _parse_aware_iso_datetime(
            before_row["updated_at"]
        )
        after_updated_at = _parse_aware_iso_datetime(
            after_row["updated_at"]
        )
        trusted_now = context.now.astimezone(UTC)
        if (
            after_row["created_at"] != before_row["created_at"]
            or before_updated_at is None
            or after_updated_at is None
            or after_updated_at <= before_updated_at
            or abs(after_updated_at - trusted_now)
            > _DATABASE_CLOCK_TOLERANCE
        ):
            return False
    if isinstance(typed_arguments, RememberPersonalMemoryArgs):
        return bool(
            after_row["status"] == "active"
            and after_row["source_kind"] == "explicit_user"
            and after_row["value"] == arguments.get("value")
            and audit["action"]
            == ("create" if before_row is None else "replace")
        )
    return bool(
        before_row is not None
        and before_row["status"] == "active"
        and after_row["status"] == "forgotten"
        and before_row["value"] == after_row["value"]
        and audit["action"] == "forget"
    )


def _valid_state_row(
    row: dict[str, Any],
    *,
    key: str,
) -> bool:
    try:
        expected_id = str(
            production_personal_memory_id(
                tenant_id=str(row["tenant_id"]),
                user_id=UUID(str(row["user_id"])),
                key=key,
            )
        )
    except (KeyError, TypeError, ValueError):
        return False
    return bool(
        row.get("memory_id") == expected_id
        and row.get("memory_type") == "response_preference"
        and row.get("memory_key") == key
        and row.get("source_kind")
        in {"explicit_user", "server_verified"}
        and row.get("status") in {"active", "forgotten"}
        and isinstance(row.get("version"), int)
        and not isinstance(row.get("version"), bool)
        and int(row["version"]) >= 1
        and row.get("expires_at") is None
        and _aware_iso_datetime(row.get("created_at"))
        and _aware_iso_datetime(row.get("updated_at"))
    )


def _aware_iso_datetime(value: Any) -> bool:
    return _parse_aware_iso_datetime(value) is not None


def _parse_aware_iso_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed
