from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from app.agent2.daily_commands import DailyCommand


LegacyDailyAdapterStatus = Literal["ready", "needs_confirmation", "blocked", "unsupported"]


@dataclass(frozen=True)
class LegacyDailyAdapterResult:
    """Dry-run plan for handing a DailyCommand to the legacy daily workflow."""

    command_operation: str
    status: LegacyDailyAdapterStatus
    legacy_action: str
    target_date: str = ""
    target_field: str = "none"
    write_impact: bool = False
    read_only: bool = False
    requires_confirmation: bool = False
    dry_run: bool = True
    safety_flags: list[str] = field(default_factory=list)
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "command_operation": self.command_operation,
            "status": self.status,
            "legacy_action": self.legacy_action,
            "target_date": self.target_date,
            "target_field": self.target_field,
            "write_impact": self.write_impact,
            "read_only": self.read_only,
            "requires_confirmation": self.requires_confirmation,
            "dry_run": self.dry_run,
            "safety_flags": list(self.safety_flags),
            "reason": self.reason,
        }


class LegacyDailyAdapter:
    """Translate Agent2 DailyCommand objects into legacy-daily action plans.

    The first adapter version is intentionally dry-run only. It establishes the
    contract and safety semantics before any legacy executor can mutate reports.
    """

    def adapt_commands(self, commands: list[DailyCommand]) -> list[LegacyDailyAdapterResult]:
        return [self.adapt_command(command) for command in commands]

    def adapt_command(self, command: DailyCommand) -> LegacyDailyAdapterResult:
        if command.requires_confirmation:
            return _needs_confirmation(command)
        if command.operation == "fill":
            return _ready_write(command, legacy_action="append_daily_items")
        if command.operation == "edit":
            return _ready_write(command, legacy_action="apply_daily_edit")
        if command.operation == "confirm":
            return _ready_write(command, legacy_action="confirm_submit")
        if command.operation == "query_current":
            return _ready_read(command, legacy_action="show_current_report")
        if command.operation == "query_history":
            return _ready_read(command, legacy_action="show_history_report")
        if command.operation == "copy_previous":
            return _ready_write(command, legacy_action="copy_previous_report")
        if command.operation == "clear":
            return _ready_write(command, legacy_action="clear_report")
        if command.operation == "revoke":
            return _ready_write(command, legacy_action="revoke_report")
        if command.operation == "no_write":
            return _blocked(command, legacy_action="no_op", reason="command is explicitly no-write")
        return _unsupported(command)


def _ready_write(command: DailyCommand, *, legacy_action: str) -> LegacyDailyAdapterResult:
    if not command.should_write:
        return _blocked(
            command,
            legacy_action=legacy_action,
            reason="write-like command is not marked safe to write",
            extra_flags=["write_command_not_safe"],
        )
    if command.target_field in {"none", "unknown"} and command.operation not in {"confirm"}:
        return _blocked(
            command,
            legacy_action=legacy_action,
            reason="write-like command has no concrete target field",
            extra_flags=["missing_target_field"],
        )
    return LegacyDailyAdapterResult(
        command_operation=command.operation,
        status="ready",
        legacy_action=legacy_action,
        target_date=command.target_date,
        target_field=command.target_field,
        write_impact=True,
        read_only=False,
        requires_confirmation=False,
        safety_flags=list(command.safety_flags),
        reason=command.reason or "daily command is ready for legacy execution",
    )


def _ready_read(command: DailyCommand, *, legacy_action: str) -> LegacyDailyAdapterResult:
    return LegacyDailyAdapterResult(
        command_operation=command.operation,
        status="ready",
        legacy_action=legacy_action,
        target_date=command.target_date,
        target_field=command.target_field,
        write_impact=False,
        read_only=True,
        requires_confirmation=False,
        safety_flags=list(command.safety_flags),
        reason=command.reason or "read-only daily command is ready for legacy execution",
    )


def _needs_confirmation(command: DailyCommand) -> LegacyDailyAdapterResult:
    return LegacyDailyAdapterResult(
        command_operation=command.operation,
        status="needs_confirmation",
        legacy_action=_confirmation_action(command.operation),
        target_date=command.target_date,
        target_field=command.target_field,
        write_impact=False,
        read_only=False,
        requires_confirmation=True,
        safety_flags=_dedupe([*command.safety_flags, "adapter_confirmation_required"]),
        reason=command.reason or "daily command requires confirmation before legacy execution",
    )


def _blocked(
    command: DailyCommand,
    *,
    legacy_action: str,
    reason: str,
    extra_flags: list[str] | None = None,
) -> LegacyDailyAdapterResult:
    return LegacyDailyAdapterResult(
        command_operation=command.operation,
        status="blocked",
        legacy_action=legacy_action,
        target_date=command.target_date,
        target_field=command.target_field,
        write_impact=False,
        read_only=False,
        requires_confirmation=command.requires_confirmation,
        safety_flags=_dedupe([*command.safety_flags, *(extra_flags or [])]),
        reason=reason,
    )


def _unsupported(command: DailyCommand) -> LegacyDailyAdapterResult:
    return LegacyDailyAdapterResult(
        command_operation=command.operation,
        status="unsupported",
        legacy_action="unsupported",
        target_date=command.target_date,
        target_field=command.target_field,
        write_impact=False,
        read_only=False,
        requires_confirmation=False,
        safety_flags=_dedupe([*command.safety_flags, "unsupported_daily_command"]),
        reason="daily command operation is not supported by the legacy adapter",
    )


def _confirmation_action(operation: str) -> str:
    return {
        "copy_previous": "prepare_copy_previous",
        "clear": "prepare_clear_report",
        "revoke": "prepare_revoke_report",
    }.get(operation, "await_confirmation")


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result
