from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from typing import Any

from app.agent2.daily_commands import DailyCommand
from app.agent2.daily_execution import (
    DailyCommandApplication,
    apply_commands_to_snapshot,
)
from app.agent2.daily_state import DRAFT_ITEM_IDS_KEY
from app.agent_core.execution_policy import (
    AuthorizationDecision,
    ExecutionPolicy,
    authorize_capability_request,
)
from app.agent_core.types import DailySnapshot, OperationLedgerEntry


DAILY_FIELDS = ("today_work", "problems", "tomorrow_plan")


@dataclass(frozen=True)
class DailyItemRef:
    global_index: int
    field: str
    field_index: int
    item_id: str
    text: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "global_index": self.global_index,
            "field": self.field,
            "field_index": self.field_index,
            "item_id": self.item_id,
            "text": self.text,
        }


@dataclass(frozen=True)
class DailyCapabilityResult:
    before: DailySnapshot
    after: DailySnapshot
    commands: list[DailyCommand]
    actions: list[dict[str, Any]]
    operation_ledger: list[OperationLedgerEntry]
    before_items: list[DailyItemRef] = field(default_factory=list)
    after_items: list[DailyItemRef] = field(default_factory=list)
    changed: bool = False
    read_only: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "before": self.before.as_dict(),
            "after": self.after.as_dict(),
            "commands": [command.as_dict() for command in self.commands],
            "actions": list(self.actions),
            "operation_ledger": [entry.as_dict() for entry in self.operation_ledger],
            "before_items": [item.as_dict() for item in self.before_items],
            "after_items": [item.as_dict() for item in self.after_items],
            "changed": self.changed,
            "read_only": self.read_only,
        }


def run_daily_capability(
    *,
    turn_id: str,
    snapshot: DailySnapshot,
    commands: list[DailyCommand],
    previous_snapshot: DailySnapshot | None = None,
    execution_policy: ExecutionPolicy | None = None,
) -> DailyCapabilityResult:
    """Apply daily commands to a snapshot without production side effects."""

    authorized: list[tuple[int, DailyCommand, AuthorizationDecision]] = []
    rejected: list[tuple[int, DailyCommand, AuthorizationDecision]] = []
    for index, command in enumerate(commands, start=1):
        decision = _authorize_daily_command(
            turn_id=turn_id,
            command=command,
            execution_policy=execution_policy,
        )
        if decision.allowed:
            authorized.append((index, command, decision))
        else:
            rejected.append((index, command, decision))

    authorized_commands = [command for _, command, _ in authorized]
    application = apply_commands_to_snapshot(
        today_work=list(snapshot.today_work),
        problems=list(snapshot.problems),
        tomorrow_plan=list(snapshot.tomorrow_plan),
        status=snapshot.status,
        commands=authorized_commands,
        previous_report=_previous_report_adapter(previous_snapshot),
        section_status=dict(snapshot.section_status),
    )
    after = _snapshot_from_application(snapshot, application)
    before_items = _build_item_refs(snapshot, _item_ids_from_snapshot(snapshot))
    after_items = _build_item_refs(after, application.item_ids)
    return DailyCapabilityResult(
        before=snapshot,
        after=after,
        commands=list(commands),
        actions=list(application.actions),
        operation_ledger=_operation_entries(
            turn_id=turn_id,
            before=snapshot,
            after=after,
            authorized=authorized,
            rejected=rejected,
            application=application,
        ),
        before_items=before_items,
        after_items=after_items,
        changed=application.changed,
        read_only=application.read_only or bool(rejected),
    )


def _snapshot_from_application(before: DailySnapshot, application: DailyCommandApplication) -> DailySnapshot:
    return DailySnapshot(
        today_work=list(application.today_work),
        problems=list(application.problems),
        tomorrow_plan=list(application.tomorrow_plan),
        status=application.status or before.status,
        section_status=_section_status_with_item_ids(before.section_status, application.item_ids),
    )


def _section_status_with_item_ids(
    section_status: dict[str, Any],
    item_ids: dict[str, list[str]],
) -> dict[str, Any]:
    updated = dict(section_status or {})
    updated[DRAFT_ITEM_IDS_KEY] = {
        field: list(item_ids.get(field, []))
        for field in DAILY_FIELDS
    }
    return updated


def _item_ids_from_snapshot(snapshot: DailySnapshot) -> dict[str, list[str]]:
    raw = snapshot.section_status.get(DRAFT_ITEM_IDS_KEY) if isinstance(snapshot.section_status, dict) else None
    if not isinstance(raw, dict):
        raw = {}
    return {
        field: _ids_for_values(field, _values_for_field(snapshot, field), list(raw.get(field, []) or []))
        for field in DAILY_FIELDS
    }


def _ids_for_values(field: str, values: list[str], raw_ids: list[Any]) -> list[str]:
    result: list[str] = []
    used: set[str] = set()
    for index, value in enumerate(values, start=1):
        candidate = str(raw_ids[index - 1]).strip() if index - 1 < len(raw_ids) else ""
        if not candidate or candidate in used:
            candidate = _make_unique_item_id(field, index, value, used)
        result.append(candidate)
        used.add(candidate)
    return result


def _make_unique_item_id(field: str, index: int, value: str, used: set[str]) -> str:
    attempt = 1
    while True:
        suffix = "" if attempt == 1 else f":{attempt}"
        digest = hashlib.sha1(f"{field}:{index}:{value}{suffix}".encode("utf-8")).hexdigest()[:12]
        candidate = f"di_{digest}"
        if candidate not in used:
            return candidate
        attempt += 1


def _build_item_refs(snapshot: DailySnapshot, item_ids: dict[str, list[str]]) -> list[DailyItemRef]:
    refs: list[DailyItemRef] = []
    global_index = 1
    for field in DAILY_FIELDS:
        values = _values_for_field(snapshot, field)
        ids = list(item_ids.get(field, []))
        for field_index, text in enumerate(values, start=1):
            refs.append(
                DailyItemRef(
                    global_index=global_index,
                    field=field,
                    field_index=field_index,
                    item_id=ids[field_index - 1] if field_index - 1 < len(ids) else "",
                    text=text,
                )
            )
            global_index += 1
    return refs


def _values_for_field(snapshot: DailySnapshot, field: str) -> list[str]:
    if field == "today_work":
        return list(snapshot.today_work)
    if field == "problems":
        return list(snapshot.problems)
    if field == "tomorrow_plan":
        return list(snapshot.tomorrow_plan)
    return []


def _operation_entries(
    *,
    turn_id: str,
    before: DailySnapshot,
    after: DailySnapshot,
    authorized: list[tuple[int, DailyCommand, AuthorizationDecision]],
    rejected: list[tuple[int, DailyCommand, AuthorizationDecision]],
    application: DailyCommandApplication,
) -> list[OperationLedgerEntry]:
    ordered_entries: list[tuple[int, OperationLedgerEntry]] = []
    for action_index, (original_index, command, decision) in enumerate(authorized):
        action = application.actions[action_index] if action_index < len(application.actions) else {}
        write_policy = "read_only" if action.get("read_only") else "dry_run"
        ordered_entries.append(
            (
                original_index,
                OperationLedgerEntry(
                    operation_id=_operation_id(turn_id, "daily_report", command.operation, original_index),
                    workflow="daily_report",
                    capability="daily_report",
                    operation=command.operation,
                    write_policy=decision.write_policy or write_policy,
                    plan_id=decision.plan_id,
                    authorization_id=decision.authorization_id,
                    authorization_status="allowed",
                    before_state=before.as_dict(),
                    after_state=after.as_dict(),
                    changed=bool(action.get("changed")),
                    read_only=bool(action.get("read_only")),
                    safety_flags=list(command.safety_flags),
                    reason=command.reason,
                ),
            )
        )
    for original_index, command, decision in rejected:
        ordered_entries.append(
            (
                original_index,
                OperationLedgerEntry(
                    operation_id=_operation_id(turn_id, "daily_report", command.operation, original_index),
                    workflow="daily_report",
                    capability="daily_report",
                    operation=command.operation,
                    write_policy="blocked",
                    plan_id=decision.plan_id,
                    authorization_status="denied",
                    before_state=before.as_dict(),
                    after_state=before.as_dict(),
                    changed=False,
                    read_only=True,
                    safety_flags=[*command.safety_flags, *decision.safety_flags],
                    reason=decision.reason,
                ),
            )
        )
    return [entry for _, entry in sorted(ordered_entries, key=lambda item: item[0])]


def _authorize_daily_command(
    *,
    turn_id: str,
    command: DailyCommand,
    execution_policy: ExecutionPolicy | None,
) -> AuthorizationDecision:
    if not command.should_write:
        return AuthorizationDecision(
            allowed=True,
            write_policy="read_only",
            reason="daily command is read-only",
        )
    return authorize_capability_request(
        execution_policy,
        turn_id=turn_id,
        capability="daily_report",
        operation=command.operation,
        target_field=command.target_field,
        task_id=command.task_id,
        requested_write_policy="dry_run",
    )


def _operation_id(turn_id: str, workflow: str, operation: str, index: int) -> str:
    raw = f"{turn_id}:{workflow}:{operation}:{index}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _previous_report_adapter(snapshot: DailySnapshot | None) -> Any:
    if snapshot is None:
        return None
    return type(
        "PreviousDailyReportSnapshot",
        (),
        {
            "id": "previous-snapshot",
            "today_work": list(snapshot.today_work),
            "problems": list(snapshot.problems),
            "tomorrow_plan": list(snapshot.tomorrow_plan),
        },
    )()
