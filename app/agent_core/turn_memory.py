from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from app.agent_core.monthly_capability import (
    MONTHLY_STATUS_COMPLETED,
    MONTHLY_STATUS_PENDING_CONFIRMATION,
    MonthlySnapshot,
)
from app.agent_core.operation_ledger import PersistableOperationRecord, build_persistable_operation_records
from app.agent_core.task_ledger import InMemoryTaskLedger, TaskLedgerEntry
from app.agent_core.types import DailySnapshot
from app.workflows.intake import WORKFLOW_DAILY_REPORT, WORKFLOW_MONTHLY_REPORT


TERMINAL_TASK_STATUSES = {"completed", "cancelled", "closed"}


@dataclass(frozen=True)
class AgentCoreMemory:
    """Turn-to-turn memory for offline Agent Core harnesses and dry runs."""

    user_id: str
    daily_snapshot: DailySnapshot = field(default_factory=DailySnapshot)
    monthly_snapshot: MonthlySnapshot = field(default_factory=MonthlySnapshot)
    task_entries: tuple[TaskLedgerEntry, ...] = ()
    operation_records: tuple[PersistableOperationRecord, ...] = ()

    def task_ledger(self) -> InMemoryTaskLedger:
        return InMemoryTaskLedger(self.task_entries)

    def as_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "daily_snapshot": self.daily_snapshot.as_dict(),
            "monthly_snapshot": self.monthly_snapshot.as_dict(),
            "task_entries": [entry.as_dict() for entry in self.task_entries],
            "operation_records": [record.as_storage_dict() for record in self.operation_records],
        }


def advance_agent_core_memory(
    memory: AgentCoreMemory,
    turn_result: Any,
    *,
    created_at: datetime | None = None,
) -> AgentCoreMemory:
    """Advance memory from one processed turn without production side effects."""

    user_id = memory.user_id
    daily_snapshot = _daily_snapshot_after(turn_result, memory.daily_snapshot)
    monthly_snapshot = _monthly_snapshot_after(turn_result, memory.monthly_snapshot)
    task_entries = _advance_task_entries(
        memory.task_entries,
        turn_result=turn_result,
        user_id=user_id,
        daily_snapshot=daily_snapshot,
        monthly_snapshot=monthly_snapshot,
        created_at=created_at,
    )
    records = tuple(
        [
            *memory.operation_records,
            *build_persistable_operation_records(turn_result, created_at=created_at),
        ]
    )
    return replace(
        memory,
        daily_snapshot=daily_snapshot,
        monthly_snapshot=monthly_snapshot,
        task_entries=task_entries,
        operation_records=records,
    )


def _daily_snapshot_after(turn_result: Any, fallback: DailySnapshot) -> DailySnapshot:
    snapshot = getattr(turn_result, "daily_after", None)
    return snapshot if isinstance(snapshot, DailySnapshot) else fallback


def _monthly_snapshot_after(turn_result: Any, fallback: MonthlySnapshot) -> MonthlySnapshot:
    snapshot = getattr(turn_result, "monthly_after", None)
    return snapshot if isinstance(snapshot, MonthlySnapshot) else fallback


def _advance_task_entries(
    existing: tuple[TaskLedgerEntry, ...],
    *,
    turn_result: Any,
    user_id: str,
    daily_snapshot: DailySnapshot,
    monthly_snapshot: MonthlySnapshot,
    created_at: datetime | None,
) -> tuple[TaskLedgerEntry, ...]:
    preserved = [
        entry
        for entry in existing
        if entry.workflow not in {WORKFLOW_DAILY_REPORT, WORKFLOW_MONTHLY_REPORT}
        and entry.status not in TERMINAL_TASK_STATUSES
    ]
    daily = _daily_task_entry(turn_result, user_id=user_id, snapshot=daily_snapshot, created_at=created_at)
    monthly = _monthly_task_entry(turn_result, user_id=user_id, snapshot=monthly_snapshot, created_at=created_at)
    return tuple([*preserved, *([daily] if daily else []), *([monthly] if monthly else [])])


def _daily_task_entry(
    turn_result: Any,
    *,
    user_id: str,
    snapshot: DailySnapshot,
    created_at: datetime | None,
) -> TaskLedgerEntry | None:
    if snapshot.status in TERMINAL_TASK_STATUSES:
        return None
    if not any((snapshot.today_work, snapshot.problems, snapshot.tomorrow_plan)):
        return None
    task_id = _task_id_for_workflow(turn_result, WORKFLOW_DAILY_REPORT, fallback=f"daily:{user_id}")
    awaited_reply = _daily_awaited_reply(snapshot)
    return TaskLedgerEntry(
        task_id=task_id,
        user_id=user_id,
        workflow=WORKFLOW_DAILY_REPORT,
        status=snapshot.status,
        awaited_reply=awaited_reply,
        prompt=_daily_prompt(snapshot, awaited_reply),
        artifacts={"daily_snapshot": snapshot.as_dict()},
        metadata={
            "source": "agent_core_memory",
            "latest_turn_id": str(getattr(turn_result, "turn_id", "") or ""),
        },
        updated_at=created_at,
    )


def _monthly_task_entry(
    turn_result: Any,
    *,
    user_id: str,
    snapshot: MonthlySnapshot,
    created_at: datetime | None,
) -> TaskLedgerEntry | None:
    if snapshot.status in {MONTHLY_STATUS_COMPLETED, *TERMINAL_TASK_STATUSES}:
        return None
    if not snapshot.metrics:
        return None
    task_id = _task_id_for_workflow(turn_result, WORKFLOW_MONTHLY_REPORT, fallback=snapshot.task_id or f"monthly:{user_id}")
    awaited_reply = "monthly_confirmation" if snapshot.status == MONTHLY_STATUS_PENDING_CONFIRMATION else "monthly_metric_reply"
    return TaskLedgerEntry(
        task_id=task_id,
        user_id=user_id,
        workflow=WORKFLOW_MONTHLY_REPORT,
        status=snapshot.status,
        awaited_reply=awaited_reply,
        prompt="awaiting monthly report confirmation" if awaited_reply == "monthly_confirmation" else "awaiting monthly metric reply",
        artifacts={"monthly_snapshot": snapshot.as_dict()},
        metadata={
            "source": "agent_core_memory",
            "latest_turn_id": str(getattr(turn_result, "turn_id", "") or ""),
        },
        updated_at=created_at,
    )


def _daily_awaited_reply(snapshot: DailySnapshot) -> str:
    if snapshot.status == "pending_confirmation":
        return "daily_confirmation"
    if not snapshot.today_work:
        return "daily_collecting"
    if not snapshot.problems:
        return "daily_followup"
    if not snapshot.tomorrow_plan:
        return "daily_tomorrow_plan"
    return "daily_followup"


def _daily_prompt(snapshot: DailySnapshot, awaited_reply: str) -> str:
    if awaited_reply == "daily_confirmation":
        return "awaiting daily report confirmation"
    missing = []
    if not snapshot.today_work:
        missing.append("today_work")
    if not snapshot.problems:
        missing.append("problems")
    if not snapshot.tomorrow_plan:
        missing.append("tomorrow_plan")
    return "awaiting daily report fields: " + ",".join(missing)


def _task_id_for_workflow(turn_result: Any, workflow: str, *, fallback: str) -> str:
    task_context = getattr(turn_result, "task_context", None)
    if task_context is not None and getattr(task_context, "selected_workflow", "") == workflow:
        selected = str(getattr(task_context, "selected_task_id", "") or "")
        if selected:
            return selected
    routing = getattr(turn_result, "routing", None)
    if routing is not None and getattr(routing, "primary_workflow", "") == workflow:
        task_id = str(getattr(routing, "task_id", "") or "")
        if task_id:
            return task_id
    return fallback
