from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from app.agent2.memory import TrustedPersonalMemory
from app.agent2.tool_calling.context import TrustedContext, TrustedReportSnapshot
from app.agent2.tool_calling.contracts import ExecutionMode, ReceiptStatus, ToolReceipt
from app.agent2.tool_calling.handlers import ShadowHandlerRequest
from app.agent2.tool_calling.idempotency import build_write_idempotency_key
from app.agent2.tool_calling.registry import TOOL_REGISTRY
from app.agent2.tool_calling.validation import (
    BoundCall,
    DateResolution,
    DateResolverPort,
    NativeToolCall,
    ShadowCallBinder,
    TrustedReportReadPort,
    UnavailableDateResolver,
    failure_receipt,
)


@dataclass(frozen=True)
class AtomicToolGroup:
    target_id: str
    tool_call_ids: tuple[str, ...]
    transaction_policy: str
    rollback_on_failure: bool = True


@dataclass(frozen=True)
class TurnExecutionPlan:
    plan_id: str
    mode: ExecutionMode
    namespace: str
    tool_calls: tuple[NativeToolCall, ...]
    receipts: tuple[ToolReceipt, ...]
    atomic_groups: tuple[AtomicToolGroup, ...]
    actual_write: bool = False
    business_handler_call_count: int = 0
    shadow_handler_call_count: int = 0
    pending_write_count: int = 0
    memory_write_count: int = 0
    audit_write_count: int = 0
    conversation_state_write_count: int = 0
    message_send_count: int = 0


class ShadowRuntime:
    """Phase-1 runtime with no business writer, state store, Pending store, or sender."""

    mode = ExecutionMode.SHADOW_PROPOSAL

    def __init__(
        self,
        *,
        date_resolver: DateResolverPort | None = None,
        report_read_port: TrustedReportReadPort | None = None,
    ) -> None:
        self._date_resolver = date_resolver or UnavailableDateResolver()
        self._report_read_port = report_read_port

    def open_session(self, context: TrustedContext) -> "ShadowRuntimeSession":
        binder = ShadowCallBinder(context, self._date_resolver, self._report_read_port)
        return ShadowRuntimeSession(context, binder)


class ShadowRuntimeSession:
    def __init__(self, context: TrustedContext, binder: ShadowCallBinder) -> None:
        self._context = context
        self._binder = binder

    async def propose(self, tool_calls: tuple[NativeToolCall, ...]) -> TurnExecutionPlan:
        self._binder.begin_batch()
        staged: list[BoundCall | ToolReceipt] = []
        bound_calls: list[BoundCall] = []
        failed_groups: set[tuple[str, str]] = set()
        for call in tool_calls:
            bound, failure = await self._binder.bind(call)
            if failure is not None:
                staged.append(failure)
                definition = TOOL_REGISTRY.get(call.tool_name)
                if definition is not None and definition.read_or_write == "write":
                    failed_groups.add(
                        (_call_target(self._context, call, None), definition.transaction_policy)
                    )
            elif bound is not None:
                staged.append(bound)
                bound_calls.append(bound)

        prevalidation_blocked_ids = {
            item.call.tool_call_id
            for item in bound_calls
            if TOOL_REGISTRY[item.call.tool_name].read_or_write == "write"
            if (
                _call_target(self._context, item.call, item),
                TOOL_REGISTRY[item.call.tool_name].transaction_policy,
            ) in failed_groups
            or any(target == "*" for target, _ in failed_groups)
        }
        if prevalidation_blocked_ids:
            staged = [
                failure_receipt(
                    item.call,
                    ReceiptStatus.BLOCKED,
                    "ATOMIC_GROUP_PREVALIDATION_FAILED",
                    report=item.report,
                )
                if isinstance(item, BoundCall)
                and item.call.tool_call_id in prevalidation_blocked_ids
                else item
                for item in staged
            ]

        conflicting_ids = conflicting_tool_call_ids(
            self._context,
            bound_calls,
        )
        if conflicting_ids:
            staged = [
                failure_receipt(
                    item.call,
                    ReceiptStatus.BLOCKED,
                    "TOOL_CALL_CONFLICT",
                    report=item.report,
                )
                if isinstance(item, BoundCall) and item.call.tool_call_id in conflicting_ids
                else item
                for item in staged
            ]

        receipts: list[ToolReceipt] = []
        shadow_handler_call_count = 0
        for item in staged:
            if isinstance(item, ToolReceipt):
                receipts.append(item)
                continue
            definition = TOOL_REGISTRY[item.call.tool_name]
            idempotency_key = None
            if definition.read_or_write == "write":
                principal = self._context.principal
                idempotency_key = build_write_idempotency_key(
                    tenant_id=principal.tenant_id,
                    user_id=str(principal.user_id),
                    conversation_id=principal.conversation_id,
                    source_message_id=principal.source_message_id,
                    tool_call_id=item.call.tool_call_id,
                    tool_name=item.call.tool_name,
                    canonical_arguments=_canonical_idempotency_arguments(item),
                    target_object=_call_target(self._context, item.call, item),
                    expected_version=_authoritative_target_version(item),
                )
            receipt = definition.shadow_handler(
                ShadowHandlerRequest(
                    tool_name=item.call.tool_name,
                    arguments=item.arguments,
                    context=self._context,
                    report=item.report,
                    source_report=item.source_report,
                    target_item_ids=item.target_item_ids,
                    idempotency_key=idempotency_key,
                    date_facts=item.date_facts,
                    pending=self._context.active_clear_pending,
                    pending_ttl_seconds=definition.pending_ttl_seconds,
                )
            )
            if not isinstance(receipt, ToolReceipt):
                raise TypeError("registry shadow handler must return ToolReceipt")
            shadow_handler_call_count += 1
            receipts.append(receipt)

        plan = TurnExecutionPlan(
            plan_id=_plan_id(self._context, tool_calls),
            mode=ExecutionMode.SHADOW_PROPOSAL,
            namespace=self._context.namespace,
            tool_calls=tool_calls,
            receipts=tuple(receipts),
            atomic_groups=_atomic_groups(self._context, tool_calls, bound_calls),
            shadow_handler_call_count=shadow_handler_call_count,
        )
        successful_ids = frozenset(
            call.tool_call_id
            for call, receipt in zip(tool_calls, receipts, strict=True)
            if receipt.status in {ReceiptStatus.SUCCESS, ReceiptStatus.NO_OP}
        )
        self._binder.promote_query_results(bound_calls, successful_ids)
        return plan


def conflicting_tool_call_ids(
    context: TrustedContext,
    bound_calls: list[BoundCall],
) -> frozenset[str]:
    grouped: dict[str, list[BoundCall]] = {}
    for item in bound_calls:
        definition = TOOL_REGISTRY[item.call.tool_name]
        if definition.read_or_write != "write":
            continue
        target_id = _call_target(context, item.call, item)
        grouped.setdefault(target_id, []).append(item)

    conflicting: set[str] = set()
    for group in grouped.values():
        seen_add_items: set[tuple[str, str]] = set()
        broad_write_count = sum(
            TOOL_REGISTRY[item.call.tool_name].conflict_policy == "broad_target"
            for item in group
        )
        seen_item_ids: set[str] = set()
        seen_fingerprints: set[str] = set()
        group_conflict = broad_write_count > 1
        for item in group:
            definition = TOOL_REGISTRY[item.call.tool_name]
            if (
                broad_write_count == 1
                and definition.conflict_policy not in {"broad_target", "item_content"}
            ):
                group_conflict = True
            fingerprint = json.dumps(
                {"name": item.call.tool_name, "arguments": item.arguments},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if fingerprint in seen_fingerprints:
                group_conflict = True
            seen_fingerprints.add(fingerprint)
            if seen_item_ids.intersection(item.target_item_ids):
                group_conflict = True
            seen_item_ids.update(item.target_item_ids)
            if definition.confirmation_policy != "none" and len(group) > 1:
                group_conflict = True
            if definition.conflict_policy == "item_content":
                add_items = {
                    (str(value["field"]), _normalized(str(value["content"])))
                    for value in item.arguments.get("items", ())
                }
                if seen_add_items.intersection(add_items):
                    group_conflict = True
                seen_add_items.update(add_items)
        if group_conflict:
            conflicting.update(item.call.tool_call_id for item in group)
    return frozenset(conflicting)



def _authoritative_target_version(item: BoundCall) -> int | None:
    return (
        item.report.version
        if item.report is not None
        else (
            item.memory.version
            if item.memory is not None
            else None
        )
    )


def _canonical_idempotency_arguments(item: BoundCall) -> dict[str, Any]:
    return {
        "tool_arguments": item.arguments,
        "server_binding": {
            "date_facts": item.date_facts,
            "target_report": _report_version_binding(item.report),
            "source_report": _report_version_binding(item.source_report),
            "personal_memory": _memory_version_binding(item.memory),
        },
    }


def _report_version_binding(report: TrustedReportSnapshot | None) -> dict[str, Any] | None:
    if report is None:
        return None
    return {"report_id": str(report.report_id), "version": report.version}


def _memory_version_binding(
    memory: TrustedPersonalMemory | None,
) -> dict[str, Any] | None:
    if memory is None:
        return None
    return {
        "memory_key": memory.memory_key,
        "version": memory.version,
    }


def merge_turn_plans(
    context: TrustedContext,
    plans: tuple[TurnExecutionPlan, ...],
) -> TurnExecutionPlan | None:
    if not plans:
        return None
    for plan in plans:
        if (
            plan.mode != ExecutionMode.SHADOW_PROPOSAL
            or plan.namespace != context.namespace
            or plan.actual_write
            or plan.business_handler_call_count
            or plan.pending_write_count
            or plan.memory_write_count
            or plan.audit_write_count
            or plan.conversation_state_write_count
            or plan.message_send_count
        ):
            raise ValueError("cannot merge a plan outside the zero-write Shadow capability")
    calls = tuple(call for plan in plans for call in plan.tool_calls)
    receipts = tuple(receipt for plan in plans for receipt in plan.receipts)
    groups: dict[tuple[str, str], list[str]] = {}
    for plan in plans:
        for group in plan.atomic_groups:
            groups.setdefault((group.target_id, group.transaction_policy), []).extend(
                group.tool_call_ids
            )
    return TurnExecutionPlan(
        plan_id=_plan_id(context, calls),
        mode=ExecutionMode.SHADOW_PROPOSAL,
        namespace=context.namespace,
        tool_calls=calls,
        receipts=receipts,
        atomic_groups=tuple(
            AtomicToolGroup(target_id, tuple(call_ids), transaction_policy)
            for (target_id, transaction_policy), call_ids in groups.items()
        ),
        shadow_handler_call_count=sum(plan.shadow_handler_call_count for plan in plans),
    )


def _normalized(value: str) -> str:
    return " ".join(value.split()).casefold()
def _call_target(
    context: TrustedContext,
    call: NativeToolCall,
    bound: BoundCall | None,
) -> str:
    if bound is not None and bound.report is not None:
        return str(bound.report.report_id)
    definition = TOOL_REGISTRY.get(call.tool_name)

    if definition is None:
        return "*"
    policy = definition.transaction_target_policy
    if policy == "read_only":
        return "read_only"
    if policy == "today_report":
        if context.today_report is not None:
            return str(context.today_report.report_id)
        local_today = context.now.astimezone(
            ZoneInfo(context.principal.timezone)
        ).date()
        return _daily_report_key(context, local_today.isoformat())
    if policy == "pending_report":
        pending = context.active_clear_pending
        return str(pending.report_id) if pending is not None else "*"
    if policy == "personal_memory":
        key = call.arguments.get("memory_key")
        scope = (
            f"personal_memory:{context.principal.tenant_id}:"
            f"{context.principal.user_id}"
        )
        return f"{scope}:{key}" if isinstance(key, str) and key else scope
    if policy == "resolved_report":
        if bound is None:
            return "*"
        resolved_date = bound.date_facts.get("resolved_date")
        if not isinstance(resolved_date, str) or not resolved_date:
            return "*"
        return _daily_report_key(context, resolved_date)
    if policy == "source_and_target_reports":
        if bound is None:
            return "*"
        source_date = bound.date_facts.get("resolved_source_date")
        target_date = bound.date_facts.get("resolved_target_date")
        if not all(
            isinstance(value, str) and value
            for value in (source_date, target_date)
        ):
            return "*"
        principal = context.principal
        return (
            f"daily_report_relocation:{principal.tenant_id}:"
            f"{principal.user_id}:{source_date}:{target_date}"
        )
    if policy == "bound_report":
        raw_report_id = call.arguments.get("report_id")
        if raw_report_id is None:
            return "*"
        try:
            report = context.report_by_id(UUID(str(raw_report_id)))
        except (ValueError, TypeError, AttributeError):
            return "*"
        return str(report.report_id) if report is not None else "*"
    return "*"


def _daily_report_key(context: TrustedContext, report_date: str) -> str:
    principal = context.principal
    return (
        f"daily_report:{principal.tenant_id}:{principal.user_id}:{report_date}"
    )


def _atomic_groups(
    context: TrustedContext,
    calls: tuple[NativeToolCall, ...],
    bound_calls: list[BoundCall],
) -> tuple[AtomicToolGroup, ...]:
    grouped: dict[tuple[str, str], list[str]] = {}
    bound_by_id = {item.call.tool_call_id: item for item in bound_calls}
    for call in calls:
        definition = TOOL_REGISTRY.get(call.tool_name)
        if definition is None or definition.read_or_write != "write":
            continue
        target_id = _call_target(context, call, bound_by_id.get(call.tool_call_id))
        grouped.setdefault((target_id, definition.transaction_policy), []).append(call.tool_call_id)
    return tuple(
        AtomicToolGroup(target_id, tuple(call_ids), transaction_policy)
        for (target_id, transaction_policy), call_ids in grouped.items()
    )


def _plan_id(context: TrustedContext, calls: tuple[NativeToolCall, ...]) -> str:
    payload = {
        "namespace": context.namespace,
        "tenant_id": context.principal.tenant_id,
        "user_id": str(context.principal.user_id),
        "conversation_id": context.principal.conversation_id,
        "source_message_id": context.principal.source_message_id,
        "calls": [
            {"id": item.tool_call_id, "name": item.tool_name, "arguments": item.arguments}
            for item in calls
        ],
    }
    digest = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return f"shadow-plan-v1:{digest}"
