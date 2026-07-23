from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Literal, Mapping, Protocol, Sequence
from uuid import UUID

from app.agent2.command_planner_v3 import CognitiveCommandPlan
from app.agent2.cognitive_core_v3 import RequiredAction
from app.agent2.typed_daily_commands import (
    DailyReportMutationSnapshot,
    TypedDailyCommand,
    execute_typed_daily_command,
)

from .context import DailySnapshotQuery
from .contracts import DomainExecutionResult, RuntimeMode


CommandSource = Literal["daily", "business"]


@dataclass(frozen=True)
class DomainPackContract:
    domain_id: str
    version: str
    command_source: CommandSource
    action_types: tuple[str, ...]
    command_types: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.domain_id or not self.version or not self.action_types or not self.command_types:
            raise ValueError("domain pack contract requires id, version, action types, and command types")
        if len(set(self.action_types)) != len(self.action_types):
            raise ValueError(f"domain pack {self.domain_id} contains duplicate action types")
        if len(set(self.command_types)) != len(self.command_types):
            raise ValueError(f"domain pack {self.domain_id} contains duplicate command types")


@dataclass(frozen=True)
class DomainExecutionContext:
    tenant_id: str
    actor_id: UUID
    conversation_id: str
    message_id: str
    occurred_at: datetime
    channel: str
    run_id: str
    mode: RuntimeMode
    source_text_hash: str


class DomainPackExecutor(Protocol):
    async def execute(
        self,
        commands: Sequence[Any],
        context: DomainExecutionContext,
    ) -> DomainExecutionResult: ...


@dataclass(frozen=True)
class DomainPack:
    contract: DomainPackContract
    executor: DomainPackExecutor | None = None


@dataclass(frozen=True)
class DomainActionResolution:
    action_domains: Mapping[str, str]
    unsupported_action_ids: tuple[str, ...]


class DomainPackRegistry:
    """Static Phase-1 registry.

    Contracts for future domains are visible, but only an explicitly composed
    executor can run. Unknown or contract-only commands become receipts rather
    than disappearing from the outcome.
    """

    def __init__(self, packs: Sequence[DomainPack]) -> None:
        self._packs = tuple(packs)
        domain_ids = [pack.contract.domain_id for pack in self._packs]
        if len(domain_ids) != len(set(domain_ids)):
            raise ValueError("duplicate domain_id in DomainPackRegistry")
        owners: dict[tuple[str, str], DomainPack] = {}
        action_owners: dict[str, str] = {}
        for pack in self._packs:
            for action_type in pack.contract.action_types:
                previous_action_owner = action_owners.get(action_type)
                if previous_action_owner is not None:
                    raise ValueError(
                        "duplicate domain action owner for "
                        f"{action_type}: {previous_action_owner} and {pack.contract.domain_id}"
                    )
                action_owners[action_type] = pack.contract.domain_id
            for command_type in pack.contract.command_types:
                key = (pack.contract.command_source, command_type)
                previous = owners.get(key)
                if previous is not None:
                    raise ValueError(
                        "duplicate domain command owner for "
                        f"{key}: {previous.contract.domain_id} and {pack.contract.domain_id}"
                    )
                owners[key] = pack
        self._action_owners = MappingProxyType(action_owners)
        self._command_owners = MappingProxyType(owners)

    @property
    def contracts(self) -> tuple[DomainPackContract, ...]:
        return tuple(pack.contract for pack in self._packs)

    def resolve_actions(self, actions: Sequence[RequiredAction]) -> DomainActionResolution:
        action_domains: dict[str, str] = {}
        unsupported: list[str] = []
        for action in actions:
            owner = self._action_owners.get(action.action_type)
            if owner is None:
                unsupported.append(action.action_id)
            else:
                action_domains[action.action_id] = owner
        return DomainActionResolution(MappingProxyType(action_domains), tuple(unsupported))

    async def execute(
        self,
        plan: CognitiveCommandPlan,
        context: DomainExecutionContext,
    ) -> tuple[DomainExecutionResult, ...]:
        results: list[DomainExecutionResult] = []
        commands_by_domain: dict[str, list[Any]] = {pack.contract.domain_id: [] for pack in self._packs}
        unknown_commands: list[Any] = []
        for source, commands in (("daily", plan.daily_commands), ("business", plan.business_commands)):
            for command in commands:
                key = (source, str(getattr(command, "command_type", "")))
                owner = self._command_owners.get(key)
                if owner is None:
                    unknown_commands.append(command)
                else:
                    commands_by_domain[owner.contract.domain_id].append(command)
        for pack in self._packs:
            commands = tuple(commands_by_domain[pack.contract.domain_id])
            if not commands:
                continue
            if pack.executor is None:
                result = DomainExecutionResult(
                    domain_id=pack.contract.domain_id,
                    status="unavailable",
                    command_count=len(commands),
                    actual_write=False,
                    would_write=False,
                    reply_text=f"{pack.contract.domain_id} 能力尚未接入 Phase 1 Runtime，本次未执行。",
                    command_results=tuple(
                        {
                            "typed_command": command.as_dict(),
                            "status": "unsupported_domain_contract",
                            "actual_write": False,
                        }
                        for command in commands
                    ),
                )
                _validate_command_receipt_partition(pack, commands, result)
                results.append(result)
                continue
            result = await pack.executor.execute(commands, context)
            _validate_command_receipt_partition(pack, commands, result)
            results.append(result)
        if unknown_commands:
            results.append(
                DomainExecutionResult(
                    domain_id="runtime",
                    status="blocked",
                    command_count=len(unknown_commands),
                    actual_write=False,
                    would_write=False,
                    reply_text="存在未注册的 typed command，本次未执行。",
                    command_results=tuple(
                        {
                            "typed_command": command.as_dict(),
                            "status": "unsupported_command_contract",
                            "actual_write": False,
                        }
                        for command in unknown_commands
                    ),
                )
            )
        return tuple(results)


class InMemoryDailyDomainExecutor:
    """Replay/Shadow Daily adapter using the production typed validator core."""

    def __init__(self, snapshot: DailyReportMutationSnapshot) -> None:
        self._snapshot = snapshot
        self._idempotency_keys: set[str] = set()

    @property
    def snapshot(self) -> DailyReportMutationSnapshot:
        return self._snapshot

    async def load_daily_snapshot(self, query: DailySnapshotQuery) -> DailyReportMutationSnapshot:
        if query.actor_id != self._snapshot.owner_user_id:
            raise ValueError("daily snapshot owner does not match runtime actor")
        return self._snapshot

    async def execute(
        self,
        commands: Sequence[Any],
        context: DomainExecutionContext,
    ) -> DomainExecutionResult:
        typed_commands = tuple(commands)
        if not typed_commands or any(not isinstance(command, TypedDailyCommand) for command in typed_commands):
            raise TypeError("Daily DomainPack accepts TypedDailyCommand only")
        before = self._snapshot
        working = before
        executions = []
        pending_keys = set(self._idempotency_keys)
        for command in typed_commands:
            execution = execute_typed_daily_command(
                command,
                snapshot=working,
                actor_user_id=context.actor_id,
                executed_idempotency_keys=pending_keys,
            )
            executions.append(execution)
            if execution.validation.status == "blocked":
                command_results = [_execution_result(item, simulated=True) for item in executions]
                command_results = [
                    {**item, "rolled_back": bool(item.get("would_write"))}
                    for item in command_results
                ]
                command_results.extend(
                    _not_executed_result(item, reason="prior_command_blocked")
                    for item in typed_commands[len(executions) :]
                )
                return DomainExecutionResult(
                    domain_id="daily",
                    status="blocked",
                    command_count=len(typed_commands),
                    actual_write=False,
                    would_write=False,
                    reply_text=_blocked_message(execution.validation.reason_code),
                    command_results=tuple(command_results),
                    data=_snapshot_data(before),
                )
            working = execution.after
            pending_keys.add(command.idempotency_key)
        self._snapshot = working
        self._idempotency_keys = pending_keys
        would_write = any(execution.should_write_db for execution in executions)
        duplicate = all(
            execution.validation.status == "duplicate" for execution in executions
        )
        return DomainExecutionResult(
            domain_id="daily",
            status="duplicate" if duplicate else "simulated",
            command_count=len(typed_commands),
            actual_write=False,
            would_write=would_write,
            reply_text=(
                "日报命令已幂等处理，未重复写入。"
                if duplicate
                else "日报命令已在 Runtime 模拟执行。"
                if would_write
                else "日报命令无需写入。"
            ),
            command_results=tuple(_execution_result(item, simulated=True) for item in executions),
            data=_snapshot_data(working),
        )


def build_phase1_domain_registry(*, daily_executor: DomainPackExecutor) -> DomainPackRegistry:
    return DomainPackRegistry(
        (
            DomainPack(DAILY_DOMAIN_CONTRACT, daily_executor),
            DomainPack(CASE_DOMAIN_CONTRACT),
            DomainPack(TRAVEL_DOMAIN_CONTRACT),
            DomainPack(KNOWLEDGE_DOMAIN_CONTRACT),
        )
    )


DAILY_DOMAIN_CONTRACT = DomainPackContract(
    domain_id="daily",
    version="0.1.0",
    command_source="daily",
    action_types=(
        "capture_daily_event",
        "edit_daily_item",
        "delete_daily_item",
        "merge_daily_items",
        "replace_daily_section",
        "move_daily_items",
        "query_daily_report",
        "copy_previous_daily_report",
        "clear_daily_section",
        "clear_daily_report",
        "reopen_daily_report",
        "copy_current_work_to_tomorrow",
        "complete_previous_daily_plan",
        "submit_daily_report",
    ),
    command_types=(
        "append_item",
        "edit_item",
        "delete_item",
        "merge_items",
        "move_items",
        "replace_section",
        "query_report",
        "copy_report",
        "clear_report",
        "reopen_report",
        "submit_report",
    ),
)
CASE_DOMAIN_CONTRACT = DomainPackContract(
    domain_id="case",
    version="0.1.0-contract-only",
    command_source="business",
    action_types=(
        "answer_case_query",
        "record_case_progress",
        "update_case_progress",
        "delete_case_progress",
        "query_case_progress",
        "query_operation_status",
        "link_case_progress",
    ),
    command_types=(
        "list_assigned_cases",
        "query_operation_status",
        "query_case_risk",
        "record_case_progress_candidate",
        "update_case_progress_candidate",
        "delete_case_progress_candidate",
        "query_case_progress_candidate",
        "link_case_progress_candidate",
    ),
)
TRAVEL_DOMAIN_CONTRACT = DomainPackContract(
    domain_id="travel",
    version="0.1.0-contract-only",
    command_source="business",
    action_types=(
        "record_travel_event",
        "update_travel_event",
        "respond_travel_collaboration",
    ),
    command_types=(
        "record_travel_candidate",
        "update_travel_candidate",
        "respond_travel_collaboration_candidate",
    ),
)
PERFORMANCE_DOMAIN_CONTRACT = DomainPackContract(
    domain_id="performance",
    version="0.1.0-contract-only",
    command_source="business",
    action_types=("build_monthly_performance", "submit_monthly_report"),
    command_types=("build_monthly_performance_view", "submit_monthly_report"),
)
KNOWLEDGE_DOMAIN_CONTRACT = DomainPackContract(
    domain_id="knowledge",
    version="0.1.0-contract-only",
    command_source="business",
    action_types=("search_enterprise_knowledge",),
    command_types=("search_enterprise_knowledge",),
)
FUTURE_DOMAIN_CONTRACTS = (
    CASE_DOMAIN_CONTRACT,
    TRAVEL_DOMAIN_CONTRACT,
    PERFORMANCE_DOMAIN_CONTRACT,
    KNOWLEDGE_DOMAIN_CONTRACT,
)
def _execution_result(execution: Any, *, simulated: bool) -> dict[str, Any]:
    actual_write = False if simulated else execution.should_write_db
    audit = execution.audit.as_dict()
    audit.update(
        {
            "actual_write": actual_write,
            "would_write": execution.should_write_db,
            "simulated": simulated,
        }
    )
    return {
        "typed_command": execution.command.as_dict(),
        "validation_status": execution.validation.status,
        "reason": execution.validation.reason_code,
        "changed": execution.changed,
        "actual_write": actual_write,
        "would_write": execution.should_write_db,
        "simulated": simulated,
        "audit": audit,
    }


def _not_executed_result(command: TypedDailyCommand, *, reason: str) -> dict[str, Any]:
    return {
        "typed_command": command.as_dict(),
        "validation_status": "not_executed",
        "reason": reason,
        "changed": False,
        "actual_write": False,
        "would_write": False,
        "simulated": True,
    }


def _validate_command_receipt_partition(
    pack: DomainPack,
    commands: tuple[Any, ...],
    result: DomainExecutionResult,
) -> None:
    if result.domain_id != pack.contract.domain_id:
        raise ValueError(
            f"domain executor {pack.contract.domain_id} returned receipt for {result.domain_id}"
        )
    if result.command_count != len(commands) or len(result.command_results) != len(commands):
        raise ValueError(
            f"domain executor {pack.contract.domain_id} did not account for every typed command"
        )
    expected_ids = [str(getattr(command, "command_id", "")) for command in commands]
    received_ids = [
        str((receipt.get("typed_command") or {}).get("command_id") or "")
        for receipt in result.command_results
    ]
    if not all(expected_ids) or received_ids != expected_ids:
        raise ValueError(
            f"domain executor {pack.contract.domain_id} receipts do not match planned command IDs"
        )
    expected_commands = [command.as_dict() for command in commands]
    received_commands = [dict(receipt.get("typed_command") or {}) for receipt in result.command_results]
    if received_commands != expected_commands:
        raise ValueError(
            f"domain executor {pack.contract.domain_id} receipt command bodies do not match the plan"
        )
    for receipt in result.command_results:
        audit = receipt.get("audit")
        if not isinstance(audit, Mapping):
            continue
        aligned_flags = (
            bool(audit.get("actual_write")) == bool(receipt.get("actual_write"))
            and bool(audit.get("would_write")) == bool(receipt.get("would_write"))
            and bool(audit.get("simulated")) == bool(receipt.get("simulated"))
        )
        if not aligned_flags:
            raise ValueError(
                f"domain executor {pack.contract.domain_id} receipt and nested audit write flags disagree"
            )
    if result.status in {"simulated", "succeeded", "duplicate"}:
        for receipt in result.command_results:
            expected_validation = "duplicate" if result.status == "duplicate" else "authorized"
            if str(receipt.get("validation_status") or "") != expected_validation:
                raise ValueError(
                    f"domain executor {pack.contract.domain_id} reported success without authorization receipt"
                )
            if result.status == "simulated" and receipt.get("simulated") is not True:
                raise ValueError(
                    f"domain executor {pack.contract.domain_id} simulated result lacks simulated receipt"
                )
        receipt_actual_write = any(bool(receipt.get("actual_write")) for receipt in result.command_results)
        receipt_would_write = any(bool(receipt.get("would_write")) for receipt in result.command_results)
        if receipt_actual_write != result.actual_write or receipt_would_write != result.would_write:
            raise ValueError(
                f"domain executor {pack.contract.domain_id} aggregate write flags contradict receipts"
            )
    else:
        if result.actual_write or result.would_write:
            raise ValueError(
                f"domain executor {pack.contract.domain_id} non-success result cannot aggregate writes"
            )
        for receipt in result.command_results:
            if receipt.get("actual_write"):
                raise ValueError(
                    f"domain executor {pack.contract.domain_id} non-success receipt reports an actual write"
                )
            if receipt.get("would_write") and not receipt.get("rolled_back"):
                raise ValueError(
                    f"domain executor {pack.contract.domain_id} non-success write receipt lacks rollback marker"
                )


def _snapshot_data(snapshot: DailyReportMutationSnapshot) -> dict[str, Any]:
    return {
        "report_id": str(snapshot.report_id),
        "version": snapshot.version,
        "status": snapshot.status,
        "today_work": list(snapshot.today_work),
        "problems": list(snapshot.problems),
        "tomorrow_plan": list(snapshot.tomorrow_plan),
        "item_ids": {key: list(value) for key, value in snapshot.item_ids.items()},
    }


def _blocked_message(reason: str) -> str:
    if reason in {"ambiguous_target", "target_not_found"}:
        return "目标不明确，请说明具体条目。"
    if reason == "duplicate_message":
        return "这条指令已经处理过，没有重复执行。"
    if reason == "version_conflict":
        return "日报已发生变化，请基于最新内容重试。"
    return "该操作未通过 typed executor 校验，没有执行。"
