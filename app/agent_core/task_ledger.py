from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
import re
from typing import Any

from app.workflows.intake import (
    ActiveWorkflowTask,
    IncomingMessageEnvelope,
    WORKFLOW_DAILY_REPORT,
    WORKFLOW_MONTHLY_REPORT,
)


@dataclass(frozen=True)
class TaskLedgerEntry:
    """A durable task fact that may claim a future user reply."""

    task_id: str
    user_id: str
    workflow: str
    status: str = "collecting"
    awaited_reply: str = ""
    prompt: str = ""
    authorization: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)
    linked_task_ids: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "user_id": self.user_id,
            "workflow": self.workflow,
            "status": self.status,
            "awaited_reply": self.awaited_reply,
            "prompt": self.prompt,
            "authorization_keys": sorted(self.authorization.keys()),
            "artifact_keys": sorted(self.artifacts.keys()),
            "linked_task_ids": list(self.linked_task_ids),
            "metadata_keys": sorted(self.metadata.keys()),
            "created_at": self.created_at.isoformat() if self.created_at else "",
            "updated_at": self.updated_at.isoformat() if self.updated_at else "",
        }


@dataclass(frozen=True)
class TaskLedgerContext:
    """The task facts selected for one Agent Core turn."""

    active_tasks: tuple[ActiveWorkflowTask, ...] = ()
    selected_task_id: str = ""
    selected_workflow: str = ""
    reason: str = ""
    considered_tasks: tuple[TaskLedgerEntry, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "selected_task_id": self.selected_task_id,
            "selected_workflow": self.selected_workflow,
            "reason": self.reason,
            "active_tasks": [
                {
                    "workflow": task.workflow,
                    "task_id": task.task_id,
                    "status": task.status,
                    "reply_candidate": task.reply_candidate,
                    "awaiting_confirmation": task.awaiting_confirmation,
                    "metadata_keys": sorted(task.metadata.keys()),
                }
                for task in self.active_tasks
            ],
            "considered_tasks": [entry.as_dict() for entry in self.considered_tasks],
        }


class InMemoryTaskLedger:
    """Simple TaskLedger adapter for tests and dry-run harnesses."""

    def __init__(self, entries: list[TaskLedgerEntry] | tuple[TaskLedgerEntry, ...] = ()) -> None:
        self._entries = tuple(entries)

    def active_entries_for_user(self, user_id: str) -> tuple[TaskLedgerEntry, ...]:
        return tuple(
            entry
            for entry in self._entries
            if entry.user_id == user_id and entry.status not in {"completed", "cancelled", "closed"}
        )


def apply_task_ledger_context(
    envelope: IncomingMessageEnvelope,
    task_ledger: Any | None,
) -> tuple[IncomingMessageEnvelope, TaskLedgerContext | None]:
    """Attach TaskLedger-derived active tasks to an incoming envelope."""

    if task_ledger is None:
        return envelope, None
    entries = tuple(task_ledger.active_entries_for_user(envelope.sender_id))
    if not entries:
        return envelope, TaskLedgerContext()

    active_tasks = tuple(_active_task_from_entry(entry, envelope.raw_text) for entry in entries)
    selected = _selected_task(active_tasks, entries)
    merged_tasks = _merge_active_tasks(envelope.active_tasks, active_tasks)
    context = TaskLedgerContext(
        active_tasks=merged_tasks,
        selected_task_id=selected.task_id if selected else "",
        selected_workflow=selected.workflow if selected else "",
        reason=selected.reason if selected else "no task awaited this reply",
        considered_tasks=entries,
    )
    return replace(envelope, active_tasks=merged_tasks), context


def _active_task_from_entry(entry: TaskLedgerEntry, raw_text: str) -> ActiveWorkflowTask:
    reply_candidate = _reply_candidate(entry, raw_text)
    awaiting_confirmation = _awaiting_confirmation(entry, raw_text)
    reason = _task_reason(entry, reply_candidate, awaiting_confirmation)
    metadata = {
        **entry.metadata,
        "awaited_reply": entry.awaited_reply,
        "prompt": entry.prompt,
        "task_ledger": True,
    }
    return ActiveWorkflowTask(
        workflow=entry.workflow,
        task_id=entry.task_id,
        status=entry.status,
        reply_candidate=reply_candidate,
        awaiting_confirmation=awaiting_confirmation,
        reason=reason,
        metadata=metadata,
    )


def _reply_candidate(entry: TaskLedgerEntry, raw_text: str) -> bool:
    awaited = entry.awaited_reply
    if entry.workflow == WORKFLOW_MONTHLY_REPORT and awaited in {"monthly_metric_reply", "monthly_reply"}:
        return _looks_like_monthly_metric_reply(raw_text)
    if entry.workflow == WORKFLOW_DAILY_REPORT and awaited in {"daily_tomorrow_plan", "daily_plan"}:
        return _looks_like_daily_tomorrow_plan(raw_text)
    if entry.workflow == WORKFLOW_DAILY_REPORT and awaited in {"daily_followup", "daily_collecting"}:
        return _looks_like_daily_followup(raw_text)
    if awaited in {"daily_confirmation", "monthly_confirmation", "confirmation"}:
        return False
    return False


def _awaiting_confirmation(entry: TaskLedgerEntry, raw_text: str) -> bool:
    if entry.status == "pending_confirmation":
        return _looks_like_confirmation(raw_text) or entry.awaited_reply in {
            "daily_confirmation",
            "monthly_confirmation",
            "confirmation",
        }
    return entry.awaited_reply in {"daily_confirmation", "monthly_confirmation", "confirmation"}


def _task_reason(entry: TaskLedgerEntry, reply_candidate: bool, awaiting_confirmation: bool) -> str:
    if reply_candidate:
        return f"task ledger matched awaited reply: {entry.awaited_reply}"
    if awaiting_confirmation:
        return f"task ledger is awaiting confirmation: {entry.awaited_reply}"
    return f"task ledger task is active but did not match this reply: {entry.awaited_reply}"


def _selected_task(
    active_tasks: tuple[ActiveWorkflowTask, ...],
    entries: tuple[TaskLedgerEntry, ...],
) -> ActiveWorkflowTask | None:
    entry_by_id = {entry.task_id: entry for entry in entries}
    candidates = [
        task
        for task in active_tasks
        if task.reply_candidate or task.awaiting_confirmation
    ]
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda task: (
            _awaited_priority(entry_by_id.get(task.task_id)),
            task.awaiting_confirmation,
            task.reply_candidate,
        ),
        reverse=True,
    )[0]


def _awaited_priority(entry: TaskLedgerEntry | None) -> int:
    if entry is None:
        return 0
    priority = {
        "daily_confirmation": 90,
        "monthly_confirmation": 90,
        "monthly_metric_reply": 80,
        "monthly_reply": 80,
        "daily_tomorrow_plan": 75,
        "daily_plan": 75,
        "daily_followup": 65,
        "daily_collecting": 65,
    }
    return priority.get(entry.awaited_reply, 10)


def _merge_active_tasks(
    existing: tuple[ActiveWorkflowTask, ...],
    ledger_tasks: tuple[ActiveWorkflowTask, ...],
) -> tuple[ActiveWorkflowTask, ...]:
    merged: list[ActiveWorkflowTask] = []
    seen: set[tuple[str, str]] = set()
    for task in (*ledger_tasks, *existing):
        key = (task.workflow, task.task_id)
        if key in seen:
            continue
        seen.add(key)
        merged.append(task)
    return tuple(merged)


def _looks_like_monthly_metric_reply(raw_text: str) -> bool:
    text = str(raw_text or "")
    if _looks_like_question(text):
        return False
    markers = (
        "\u672a\u5b8c\u6210\u539f\u56e0",
        "\u5b58\u5728\u95ee\u9898",
        "\u4e0b\u6708\u76ee\u6807",
        "\u884c\u52a8\u65b9\u6848",
        "\u672c\u6b21\u9700\u8981\u586b\u5199\u7684\u6307\u6807",
    )
    marker_hits = sum(1 for marker in markers if marker in text)
    numbered_metric = bool(re.search(r"(?:^|\n)\s*\d+\s*[.．、]", text))
    return marker_hits >= 2 or (marker_hits >= 1 and numbered_metric)


def _looks_like_daily_tomorrow_plan(raw_text: str) -> bool:
    text = str(raw_text or "")
    if _looks_like_question(text):
        return False
    return _contains_any(text, ("\u660e\u5929", "\u660e\u65e5", "\u4e0b\u4e00\u6b65", "\u8ba1\u5212")) and _contains_any(
        text,
        (
            "\u53bb",
            "\u51fa\u5dee",
            "\u5904\u7406",
            "\u8ddf\u8fdb",
            "\u5ba1\u6838",
            "\u6574\u7406",
            "\u63a8\u8fdb",
            "\u76d6\u7ae0",
            "\u5f00\u5ead",
        ),
    )


def _looks_like_daily_followup(raw_text: str) -> bool:
    text = str(raw_text or "")
    if _looks_like_question(text):
        return False
    return _contains_any(
        text,
        (
            "\u4eca\u5929",
            "\u4eca\u65e5",
            "\u660e\u5929",
            "\u660e\u65e5",
            "\u95ee\u9898",
            "\u98ce\u9669",
            "\u7b2c",
            "\u6539\u6210",
            "\u5220\u9664",
            "\u5408\u5e76",
            "\u786e\u8ba4",
            "\u63d0\u4ea4",
        ),
    )


def _looks_like_confirmation(raw_text: str) -> bool:
    compact = _compact(raw_text)
    return compact in {
        "\u786e\u8ba4",
        "\u786e\u8ba4\u63d0\u4ea4",
        "\u63d0\u4ea4",
        "\u53ef\u4ee5\u63d0\u4ea4",
        "\u6ca1\u95ee\u9898",
        "\u786e\u5b9a",
        "ok",
    }


def _looks_like_question(raw_text: str) -> bool:
    text = str(raw_text or "").strip()
    if text.endswith(("?", "\uff1f")):
        return True
    return _contains_any(
        text,
        (
            "\u600e\u4e48",
            "\u4e3a\u4ec0\u4e48",
            "\u6709\u4ec0\u4e48",
            "\u4ec0\u4e48\u540e\u679c",
            "\u662f\u4ec0\u4e48",
            "\u4ec0\u4e48\u610f\u601d",
            "\u80fd\u5426",
            "\u80fd\u4e0d\u80fd",
        ),
    )


def _contains_any(value: str, markers: tuple[str, ...]) -> bool:
    compact = _compact(value)
    return any(_compact(marker) in compact for marker in markers)


def _compact(value: str) -> str:
    return re.sub(r"[\s\u3000:：，,。.;；、（）()【】\\[\\]\"'“”‘’]+", "", str(value or "")).lower()
