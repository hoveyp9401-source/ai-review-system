from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.agent_core.types import OperationLedgerEntry


RECORD_SCHEMA = "agent_core_operation_ledger.v1"


@dataclass(frozen=True)
class PersistableOperationRecord:
    """JSON-safe operation record ready for a database adapter."""

    operation_id: str
    turn_id: str
    workflow: str
    capability: str
    operation: str
    write_policy: str
    plan_id: str = ""
    task_id: str = ""
    authorization_id: str = ""
    authorization_status: str = ""
    before_snapshot: dict[str, Any] = field(default_factory=dict)
    after_snapshot: dict[str, Any] = field(default_factory=dict)
    auth_chain: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    safety_flags: list[str] = field(default_factory=list)
    reason: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    record_schema: str = RECORD_SCHEMA

    def as_storage_dict(self) -> dict[str, Any]:
        return {
            "record_schema": self.record_schema,
            "operation_id": self.operation_id,
            "turn_id": self.turn_id,
            "workflow": self.workflow,
            "capability": self.capability,
            "operation": self.operation,
            "write_policy": self.write_policy,
            "plan_id": self.plan_id,
            "task_id": self.task_id,
            "authorization_id": self.authorization_id,
            "authorization_status": self.authorization_status,
            "before_snapshot": _json_safe(self.before_snapshot),
            "after_snapshot": _json_safe(self.after_snapshot),
            "auth_chain": _json_safe(self.auth_chain),
            "result": _json_safe(self.result),
            "safety_flags": list(self.safety_flags),
            "reason": self.reason,
            "created_at": self.created_at.isoformat(),
        }


class InMemoryOperationLedgerStore:
    """Idempotent in-memory adapter for tests and offline harnesses."""

    def __init__(self) -> None:
        self._records_by_id: dict[str, PersistableOperationRecord] = {}

    def upsert_many(self, records: list[PersistableOperationRecord]) -> list[PersistableOperationRecord]:
        for record in records:
            self._records_by_id[record.operation_id] = record
        return list(records)

    def list_by_turn(self, turn_id: str) -> list[PersistableOperationRecord]:
        return [
            record
            for record in self._records_by_id.values()
            if record.turn_id == turn_id
        ]

    def list_by_task(self, task_id: str) -> list[PersistableOperationRecord]:
        return [
            record
            for record in self._records_by_id.values()
            if record.task_id == task_id
        ]


def build_persistable_operation_records(
    turn_result: Any,
    *,
    created_at: datetime | None = None,
) -> list[PersistableOperationRecord]:
    """Convert an AgentTurnResult's ledger into JSON-safe storage records."""

    timestamp = created_at or datetime.now(timezone.utc)
    task_id = _selected_task_id(turn_result)
    return [
        _record_from_entry(
            entry,
            turn_id=str(getattr(turn_result, "turn_id", "") or ""),
            task_id=task_id,
            created_at=timestamp,
        )
        for entry in list(getattr(turn_result, "operation_ledger", []) or [])
    ]


def _record_from_entry(
    entry: OperationLedgerEntry,
    *,
    turn_id: str,
    task_id: str,
    created_at: datetime,
) -> PersistableOperationRecord:
    result = {
        "changed": bool(entry.changed),
        "read_only": bool(entry.read_only),
        "write_policy": entry.write_policy,
    }
    auth_chain = {
        "plan_id": entry.plan_id,
        "authorization_id": entry.authorization_id,
        "authorization_status": entry.authorization_status,
        "safety_flags": list(entry.safety_flags),
    }
    return PersistableOperationRecord(
        operation_id=entry.operation_id,
        turn_id=turn_id,
        workflow=entry.workflow,
        capability=entry.capability,
        operation=entry.operation,
        write_policy=entry.write_policy,
        plan_id=entry.plan_id,
        task_id=task_id,
        authorization_id=entry.authorization_id,
        authorization_status=entry.authorization_status,
        before_snapshot=dict(entry.before_state),
        after_snapshot=dict(entry.after_state),
        auth_chain=auth_chain,
        result=result,
        safety_flags=list(entry.safety_flags),
        reason=entry.reason,
        created_at=created_at,
    )


def _selected_task_id(turn_result: Any) -> str:
    context = getattr(turn_result, "task_context", None)
    if context is None:
        return ""
    return str(getattr(context, "selected_task_id", "") or "")


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
