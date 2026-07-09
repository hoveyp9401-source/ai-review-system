from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.agent2.cognitive_decision import COGNITIVE_CONTRACT_VERSION, CognitiveDecision


NON_DAILY_READ_ONLY_WORKFLOWS = {"chat", "internal_qa", "legal_research"}
READ_ONLY_POLICIES = {"no_write", "read_only"}
DESTRUCTIVE_OPERATIONS = {"clear", "revoke", "copy_previous"}
BLOCKING_GATE_REPLY_TYPES = {"confirm", "clarify", "not_supported"}


@dataclass(frozen=True)
class ContractInvariantViolation:
    rule: str
    severity: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "message": self.message,
            "details": dict(self.details),
        }


def evaluate_cognitive_invariants(
    decision: CognitiveDecision | dict[str, Any],
    *,
    actual_write: bool | None = None,
    execution_status: str = "",
    fallback_used: bool | None = None,
) -> list[ContractInvariantViolation]:
    """Validate cross-layer rules that must hold before a turn is trusted."""

    data = decision.as_dict() if isinstance(decision, CognitiveDecision) else dict(decision or {})
    trace = data.get("execution_trace") if isinstance(data.get("execution_trace"), dict) else {}
    observed_actual_write = actual_write
    if observed_actual_write is None and trace.get("actual_write") is not None:
        observed_actual_write = bool(trace.get("actual_write"))
    observed_status = str(execution_status or trace.get("execution_status") or "")
    observed_fallback = bool(fallback_used if fallback_used is not None else trace.get("fallback_used", False))

    violations: list[ContractInvariantViolation] = []
    if data.get("contract_version") != COGNITIVE_CONTRACT_VERSION:
        violations.append(
            ContractInvariantViolation(
                rule="contract_version_missing_or_unknown",
                severity="error",
                message="CognitiveDecision must declare the active contract version.",
                details={"contract_version": data.get("contract_version")},
            )
        )

    if observed_actual_write is True and not bool(data.get("allow_write")):
        violations.append(
            ContractInvariantViolation(
                rule="actual_write_requires_allow_write",
                severity="critical",
                message="Execution wrote data although the cognitive contract did not allow writes.",
                details={"execution_status": observed_status},
            )
        )

    actions = _dict_list(data.get("actions"))
    commands = _dict_list(data.get("daily_commands"))
    should_write_commands = _should_write_commands(commands)

    if bool(data.get("allow_write")) and not _has_write_action(actions) and not _has_legacy_context_write_command(commands):
        violations.append(
            ContractInvariantViolation(
                rule="allow_write_requires_write_action",
                severity="error",
                message="The cognitive contract allowed writes without a write-policy user action.",
                details={
                    "action_types": _values(actions, "action_type"),
                    "command_operations": _values(commands, "operation"),
                },
            )
        )

    if observed_actual_write is True and actions and not _has_write_action(actions):
        violations.append(
            ContractInvariantViolation(
                rule="actual_write_requires_write_action",
                severity="critical",
                message="Execution wrote data although all interpreted user actions were read-only/no-write.",
                details={"action_types": _values(actions, "action_type")},
            )
        )

    if _is_pure_read_only_non_daily(data, actions) and should_write_commands:
        violations.append(
            ContractInvariantViolation(
                rule="read_only_non_daily_must_not_emit_daily_write_command",
                severity="error",
                message="A pure chat/internal-QA/legal-research turn emitted a daily write command.",
                details={
                    "primary_workflow": data.get("primary_workflow"),
                    "command_operations": _values(commands, "operation"),
                },
            )
        )

    if _gate_blocks_write(data) and should_write_commands:
        violations.append(
            ContractInvariantViolation(
                rule="gate_block_must_not_emit_should_write_command",
                severity="error",
                message="The gate required clarification/confirmation or blocked the turn, but a daily write command was still marked should_write.",
                details={
                    "gate_reply_type": data.get("gate_reply_type"),
                    "need_confirmation": data.get("need_confirmation"),
                    "need_clarification": data.get("need_clarification"),
                    "command_operations": _values(should_write_commands, "operation"),
                },
            )
        )

    for command in _destructive_commands(commands):
        flags = {str(flag or "") for flag in command.get("safety_flags") or []}
        if "destructive_or_overwrite" not in flags:
            violations.append(
                ContractInvariantViolation(
                    rule="destructive_command_requires_safety_flag",
                    severity="error",
                    message="A destructive or overwrite daily command must carry an explicit safety flag.",
                    details={
                        "operation": command.get("operation"),
                        "target_field": command.get("target_field"),
                        "target_date": command.get("target_date"),
                    },
                )
            )

    if observed_actual_write is True:
        for command in _destructive_commands(should_write_commands):
            if not _has_explicit_target_scope(command):
                violations.append(
                    ContractInvariantViolation(
                        rule="destructive_actual_write_requires_target_scope",
                        severity="critical",
                        message="A destructive daily write must name an explicit task/date/field scope before execution.",
                        details={
                            "operation": command.get("operation"),
                            "target_field": command.get("target_field"),
                            "target_date": command.get("target_date"),
                            "task_id": command.get("task_id"),
                        },
                    )
                )

    if observed_actual_write is True and bool(data.get("need_confirmation")):
        violations.append(
            ContractInvariantViolation(
                rule="confirmation_required_blocks_actual_write",
                severity="critical",
                message="Execution wrote data while the cognitive contract still required confirmation.",
                details={"gate_reply_type": data.get("gate_reply_type")},
            )
        )

    if observed_fallback and observed_actual_write:
        violations.append(
            ContractInvariantViolation(
                rule="fallback_must_not_be_counted_as_direct_write",
                severity="error",
                message="A fallback turn was also reported as a direct Agent2 write.",
                details={"execution_status": observed_status},
            )
        )

    return violations


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _values(rows: list[dict[str, Any]], key: str) -> list[str]:
    result: list[str] = []
    for row in rows:
        value = str(row.get(key) or "")
        if value and value not in result:
            result.append(value)
    return result


def _has_write_action(actions: list[dict[str, Any]]) -> bool:
    return any(str(action.get("write_policy") or "") == "write" for action in actions)


def _has_should_write_command(commands: list[dict[str, Any]]) -> bool:
    return any(bool(command.get("should_write")) for command in commands)


def _has_legacy_context_write_command(commands: list[dict[str, Any]]) -> bool:
    for command in commands:
        if not bool(command.get("should_write")):
            continue
        flags = {str(flag or "") for flag in command.get("safety_flags") or []}
        if "legacy_context_command" in flags:
            return True
    return False


def _should_write_commands(commands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [command for command in commands if bool(command.get("should_write"))]


def _gate_blocks_write(data: dict[str, Any]) -> bool:
    if bool(data.get("need_confirmation")) or bool(data.get("need_clarification")):
        return True
    return str(data.get("gate_reply_type") or "") in BLOCKING_GATE_REPLY_TYPES


def _destructive_commands(commands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        command
        for command in commands
        if str(command.get("operation") or "") in DESTRUCTIVE_OPERATIONS
    ]


def _has_explicit_target_scope(command: dict[str, Any]) -> bool:
    if str(command.get("task_id") or "").strip():
        return True
    if str(command.get("target_date") or "").strip():
        return True
    target_field = str(command.get("target_field") or "")
    return target_field in {"today_work", "problems", "tomorrow_plan"}


def _is_pure_read_only_non_daily(data: dict[str, Any], actions: list[dict[str, Any]]) -> bool:
    workflows = {str(data.get("primary_workflow") or "")}
    workflows.update(str(workflow or "") for workflow in data.get("matched_workflows") or [])
    workflows.update(str(action.get("workflow") or "") for action in actions)
    workflows.discard("")
    if not workflows or "daily_report" in workflows:
        return False
    if not workflows.issubset(NON_DAILY_READ_ONLY_WORKFLOWS | {"unknown_or_help"}):
        return False
    if not actions:
        return bool(workflows.intersection(NON_DAILY_READ_ONLY_WORKFLOWS))
    return all(str(action.get("write_policy") or "") in READ_ONLY_POLICIES for action in actions)
