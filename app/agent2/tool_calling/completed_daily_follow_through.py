from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any, Protocol
from uuid import UUID

from app.agent2.tool_calling.context import (
    TrustedContext,
    TrustedReportItem,
    TrustedReportSnapshot,
)
from app.agent2.tool_calling.contracts import ExecutionMode, ToolReceipt
from app.agent2.tool_calling.receipt_provenance import principal_scope_sha256
from app.agent2.tool_calling.registry import TOOL_REGISTRY, deepseek_tool_schemas
from app.agent2.tool_calling.runtime import NativeToolCall

DAILY_CONTENT_WRITE_TOOLS = frozenset(
    {
        "add_daily_items",
        "edit_daily_items",
        "delete_daily_items",
        "move_daily_items",
    }
)

_FOLLOW_THROUGH_REVIEW_KEYS = frozenset({"decision", "reviewed_reply_sha256"})
_FOLLOW_THROUGH_DECISIONS = frozenset(
    {"keep_no_write", "keep_clarification", "continue_once"}
)
_INTERNAL_DAILY_VERSION_LABELS = (
    "expected_version",
    "report_version",
    "version",
)


class _ParsedAssistantTurnLike(Protocol):
    assistant_message: dict[str, Any]
    tool_calls: tuple[NativeToolCall, ...]


class CompletedDailyFollowThroughState(str, Enum):
    IDLE = "idle"
    COMPLETED_LOADED = "completed_loaded"
    CONTINUATION_OPEN = "continuation_open"
    WRITE_PENDING = "write_pending"


class ModelCallBudgetExceeded(RuntimeError):
    pass


@dataclass
class TurnModelCallBudget:
    limit: int = 9
    claimed: int = 0

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("model call budget must be positive")

    def claim(self) -> int:
        if self.claimed >= self.limit:
            raise ModelCallBudgetExceeded("turn model call budget exceeded")
        self.claimed += 1
        return self.claimed


@dataclass(frozen=True)
class CompletedDailyToolInspection:
    integrity_error: str | None
    review_tool_names: frozenset[str]
    selected_targets: list[dict[str, Any]]
    trusted_query_results: list[dict[str, Any]]
    repeat_write_review: bool


@dataclass(frozen=True)
class CompletedDailyTerminalInspection:
    query_state: str
    allowed_write_tool_names: frozenset[str]
    needs_clarification_review: bool
    needs_initial_review: bool
    needs_pending_review: bool


@dataclass
class CompletedDailyFollowThrough:
    """Own completed-Daily read-to-write state behind one turn-local seam."""

    context: TrustedContext
    user_text: str
    user_messages: tuple[str, ...]
    state: CompletedDailyFollowThroughState = CompletedDailyFollowThroughState.IDLE
    _allowed_write_tool_names: frozenset[str] = field(default_factory=frozenset)
    _review_clarification: bool = False

    def prepare_model_tools(
        self,
        default_tool_schemas: list[dict[str, Any]],
        *,
        tools_disabled: bool,
    ) -> list[dict[str, Any]]:
        if tools_disabled:
            return []
        if self.state != CompletedDailyFollowThroughState.CONTINUATION_OPEN:
            return default_tool_schemas
        return deepseek_tool_schemas(self._allowed_write_tool_names)

    def allows_argument_repair(self) -> bool:
        return self.state != CompletedDailyFollowThroughState.CONTINUATION_OPEN

    def inspect_tool_turn(
        self,
        *,
        calls: tuple[NativeToolCall, ...],
        reviewed_calls: tuple[NativeToolCall, ...],
        receipts: tuple[ToolReceipt, ...],
        default_review_tool_names: frozenset[str],
    ) -> CompletedDailyToolInspection:
        query_state = self._record_query_state(receipts)
        self._review_clarification = False
        integrity_error = _query_state_integrity_error(query_state)
        if (
            self.state == CompletedDailyFollowThroughState.CONTINUATION_OPEN
            and not self._calls_are_allowed(calls)
            and integrity_error is None
        ):
            integrity_error = (
                "daily content follow-through selected a disallowed tool"
            )
        review_tool_names = default_review_tool_names
        if self.state == CompletedDailyFollowThroughState.CONTINUATION_OPEN:
            continuation_tool_names = frozenset(
                call.tool_name
                for call in calls
                if call.tool_name in self._allowed_write_tool_names
                and call.tool_name in DAILY_CONTENT_WRITE_TOOLS
            )
            if continuation_tool_names:
                review_tool_names = continuation_tool_names
        return CompletedDailyToolInspection(
            integrity_error=integrity_error,
            review_tool_names=review_tool_names,
            selected_targets=_trusted_completed_daily_selected_targets(
                reviewed_calls=reviewed_calls,
                receipts=receipts,
                context=self.context,
            ),
            trusted_query_results=_daily_content_query_result_summaries(
                receipts,
                context=self.context,
            ),
            repeat_write_review=(
                self.state == CompletedDailyFollowThroughState.CONTINUATION_OPEN
            ),
        )

    def record_write_review(
        self,
        reviewed: _ParsedAssistantTurnLike,
        *,
        receipts: tuple[ToolReceipt, ...],
    ) -> None:
        self._record_query_state(receipts)
        self._review_clarification = bool(
            _qualified_completed_daily_query(
                receipts,
                context=self.context,
            )
            is not None
            and _is_strict_daily_weekly_review_clarification(reviewed)
        )

    def reviewed_tool_turn_error(
        self,
        calls: tuple[NativeToolCall, ...],
        *,
        write_batch_seen: bool,
    ) -> str | None:
        if (
            self.state != CompletedDailyFollowThroughState.CONTINUATION_OPEN
            or write_batch_seen
        ):
            return None
        if not calls and not self._review_clarification:
            return "daily content follow-through ended without a write call"
        if not self._calls_are_allowed(calls):
            return "daily content follow-through selected a disallowed tool"
        return None

    def inspect_terminal(
        self,
        reply: str,
        *,
        receipts: tuple[ToolReceipt, ...],
        write_batch_seen: bool,
        error_factory: Callable[[str], Exception] = ValueError,
    ) -> CompletedDailyTerminalInspection:
        query_state = self._record_query_state(receipts)
        _assert_no_trusted_daily_identifier_leak(
            reply,
            receipts,
            context=self.context,
            error_factory=error_factory,
        )
        if write_batch_seen and self.state in {
            CompletedDailyFollowThroughState.COMPLETED_LOADED,
            CompletedDailyFollowThroughState.CONTINUATION_OPEN,
        }:
            self.state = CompletedDailyFollowThroughState.WRITE_PENDING
        allowed_write_tool_names = (
            self._allowed_write_tool_names
            if self.state == CompletedDailyFollowThroughState.CONTINUATION_OPEN
            else _daily_content_follow_through_tool_names(
                context=self.context,
                receipts=receipts,
            )
        )
        return CompletedDailyTerminalInspection(
            query_state=query_state,
            allowed_write_tool_names=allowed_write_tool_names,
            needs_clarification_review=(
                not write_batch_seen and self._review_clarification
            ),
            needs_initial_review=(
                not write_batch_seen
                and self.state
                != CompletedDailyFollowThroughState.CONTINUATION_OPEN
                and not self._review_clarification
                and query_state in {"qualified", "multiple"}
            ),
            needs_pending_review=(
                write_batch_seen and query_state == "qualified"
            ),
        )

    def assert_safe_reply(
        self,
        reply: str,
        *,
        receipts: tuple[ToolReceipt, ...],
        error_factory: Callable[[str], Exception] = ValueError,
    ) -> None:
        _assert_no_trusted_daily_identifier_leak(
            reply,
            receipts,
            context=self.context,
            error_factory=error_factory,
        )

    def review_messages(
        self,
        *,
        candidate_reply: str,
        receipts: tuple[ToolReceipt, ...],
        allowed_write_tool_names: frozenset[str],
        query_state: str,
        pending_write_review: bool = False,
    ) -> list[dict[str, str]]:
        return _daily_content_follow_through_review_messages(
            user_text=self.user_text,
            user_messages=self.user_messages,
            candidate_reply=candidate_reply,
            receipts=receipts,
            context=self.context,
            allowed_write_tool_names=allowed_write_tool_names,
            query_state=query_state,
            pending_write_review=pending_write_review,
        )

    def parse_review(self, *, review_content: str, candidate_reply: str) -> str:
        return _parse_daily_content_follow_through_review(
            review_content=review_content,
            candidate_reply=candidate_reply,
        )

    def activate(
        self,
        *,
        candidate_reply: str,
        allowed_write_tool_names: frozenset[str],
    ) -> dict[str, str]:
        self.state = CompletedDailyFollowThroughState.CONTINUATION_OPEN
        self._allowed_write_tool_names = allowed_write_tool_names
        return _daily_content_follow_through_protocol_message(
            candidate_reply=candidate_reply,
            allowed_write_tool_names=allowed_write_tool_names,
        )

    def record_state(
        self,
        *,
        receipts: tuple[ToolReceipt, ...],
        current_has_write: bool,
    ) -> None:
        self._record_query_state(receipts)
        if current_has_write and self.state in {
            CompletedDailyFollowThroughState.COMPLETED_LOADED,
            CompletedDailyFollowThroughState.CONTINUATION_OPEN,
        }:
            self.state = CompletedDailyFollowThroughState.WRITE_PENDING

    def _record_query_state(self, receipts: tuple[ToolReceipt, ...]) -> str:
        query_state = _daily_content_query_state(receipts, context=self.context)
        if (
            query_state == "qualified"
            and self.state == CompletedDailyFollowThroughState.IDLE
        ):
            self.state = CompletedDailyFollowThroughState.COMPLETED_LOADED
        return query_state

    def _calls_are_allowed(self, calls: tuple[NativeToolCall, ...]) -> bool:
        return all(
            call.tool_name in self._allowed_write_tool_names
            and TOOL_REGISTRY[call.tool_name].read_or_write == "write"
            for call in calls
        )


def _contains_labeled_internal_version(reply: str, version: int) -> bool:
    lowered = reply.casefold()
    expected = str(version)
    separators = frozenset(" \t\r\n\"'`=:：")
    for label in _INTERNAL_DAILY_VERSION_LABELS:
        offset = 0
        while True:
            index = lowered.find(label, offset)
            if index < 0:
                break
            if index > 0 and (
                lowered[index - 1].isalnum() or lowered[index - 1] == "_"
            ):
                offset = index + len(label)
                continue
            label_end = index + len(label)
            value_start = label_end
            if value_start >= len(lowered) or lowered[value_start] not in separators:
                offset = index + len(label)
                continue
            while value_start < len(lowered) and lowered[value_start] in separators:
                value_start += 1
            if label == "version" and not any(
                marker in lowered[label_end:value_start] for marker in "=:："
            ):
                offset = index + len(label)
                continue
            if lowered.startswith(expected, value_start):
                value_end = value_start + len(expected)
                if value_end == len(lowered) or not (
                    lowered[value_end].isalnum() or lowered[value_end] == "_"
                ):
                    return True
            offset = index + len(label)
    return False


def _assert_no_trusted_daily_identifier_leak(
    reply: str,
    receipts: tuple[ToolReceipt, ...],
    *,
    context: TrustedContext | None = None,
    error_factory: Callable[[str], Exception] = ValueError,
) -> None:
    for receipt in receipts:
        snapshot = _validated_daily_query_snapshot(receipt, context=context)
        if snapshot is None:
            continue
        casefold_tokens: set[str] = set()
        for value in (
            snapshot.get("report_id"),
            snapshot.get("report_state_sha256"),
        ):
            if isinstance(value, str) and value:
                casefold_tokens.add(value.casefold())
        exact_item_ids: set[str] = set()
        fields = snapshot.get("fields")
        if isinstance(fields, dict):
            for items in fields.values():
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    item_id = item.get("item_id")
                    if isinstance(item_id, str) and item_id:
                        exact_item_ids.add(item_id)
        if any(token in reply.casefold() for token in casefold_tokens) or any(
            item_id in reply for item_id in exact_item_ids
        ):
            raise error_factory("terminal reply exposed internal Daily identifiers")
        version = snapshot.get("version")
        if type(version) is int and _contains_labeled_internal_version(
            reply,
            version,
        ):
            raise error_factory("terminal reply exposed internal Daily identifiers")


def _receipt_matches_principal_scope(
    receipt: ToolReceipt,
    *,
    context: TrustedContext | None,
) -> bool:
    if context is None:
        return True
    supplied = receipt.server_evidence.get("principal_scope_sha256")
    expected = principal_scope_sha256(
        tenant_id=context.principal.tenant_id,
        user_id=context.principal.user_id,
        conversation_id=context.principal.conversation_id,
        source_message_id=context.principal.source_message_id,
    )
    return bool(
        isinstance(supplied, str)
        and hmac.compare_digest(supplied, expected)
    )


def _validated_daily_query_snapshot(
    receipt: ToolReceipt,
    *,
    context: TrustedContext | None,
) -> dict[str, Any] | None:
    status = str(getattr(receipt.status, "value", receipt.status) or "")
    snapshot = receipt.safe_user_facts.get("report_snapshot")
    if (
        receipt.tool_name != "query_report_by_date"
        or status != "success"
        or receipt.execution_mode != ExecutionMode.CANARY_EXECUTE
        or receipt.changed is not False
        or receipt.target_type != "daily_report"
        or not isinstance(receipt.target_id, str)
        or not receipt.target_id
        or receipt.affected_item_ids != ()
        or receipt.error_code is not None
        or receipt.would_change is not False
        or receipt.validation_errors != ()
        or not isinstance(snapshot, dict)
        or set(snapshot)
        != {
            "report_id",
            "report_date",
            "version",
            "status",
            "report_state_sha256",
            "fields",
            "acknowledged_empty_fields",
        }
        or snapshot.get("report_id") != receipt.target_id
        or type(snapshot.get("version")) is not int
        or snapshot.get("version") != receipt.before_version
        or snapshot.get("version") != receipt.after_version
        or snapshot.get("status")
        not in {
            "collecting",
            "pending_confirmation",
            "completed",
            "skipped",
            "cancelled",
        }
        or not isinstance(snapshot.get("report_date"), str)
        or not snapshot.get("report_date")
        or not isinstance(snapshot.get("report_state_sha256"), str)
        or len(snapshot.get("report_state_sha256")) != 64
        or any(
            character not in "0123456789abcdef"
            for character in snapshot.get("report_state_sha256")
        )
        or not isinstance(snapshot.get("acknowledged_empty_fields"), list)
        or receipt.safe_user_facts.get("actual_write") is not False
        or receipt.safe_user_facts.get("report_found") is not True
        or receipt.safe_user_facts.get("report_date") != snapshot.get("report_date")
        or not _receipt_matches_principal_scope(receipt, context=context)
    ):
        return None
    try:
        report_id = UUID(snapshot["report_id"])
        if str(report_id) != snapshot["report_id"]:
            return None
        report_date = date.fromisoformat(snapshot["report_date"])
        if report_date.isoformat() != snapshot["report_date"]:
            return None
    except ValueError:
        return None
    fields = snapshot.get("fields")
    expected_fields = {"today_work", "problems", "tomorrow_plan"}
    if not isinstance(fields, dict) or set(fields) != expected_fields:
        return None
    acknowledged = snapshot["acknowledged_empty_fields"]
    if (
        any(not isinstance(field_name, str) for field_name in acknowledged)
        or set(acknowledged) - expected_fields
        or len(acknowledged) != len(set(acknowledged))
        or acknowledged != sorted(acknowledged)
    ):
        return None
    item_ids: set[str] = set()
    trusted_items: list[TrustedReportItem] = []
    for field_name, items in fields.items():
        if not isinstance(items, list):
            return None
        if field_name in acknowledged and items:
            return None
        for item in items:
            if (
                not isinstance(item, dict)
                or set(item) != {"item_id", "content"}
                or not isinstance(item.get("item_id"), str)
                or not item.get("item_id")
                or not isinstance(item.get("content"), str)
                or not item.get("content")
                or item["item_id"] in item_ids
            ):
                return None
            item_ids.add(item["item_id"])
            trusted_items.append(
                TrustedReportItem(
                    item_id=item["item_id"],
                    field=field_name,
                    content=item["content"],
                    report_id=report_id,
                    report_version=snapshot["version"],
                    provenance="trusted_context",
                )
            )
    try:
        trusted_snapshot = TrustedReportSnapshot(
            report_id=report_id,
            tenant_id="daily-query-snapshot-integrity",
            owner_user_id=UUID(int=0),
            report_date=report_date,
            version=snapshot["version"],
            status=snapshot["status"],
            items=tuple(trusted_items),
            acknowledged_empty_fields=frozenset(acknowledged),
            provenance="trusted_context",
        )
    except ValueError:
        return None
    if not hmac.compare_digest(
        snapshot["report_state_sha256"],
        trusted_snapshot.state_sha256,
    ):
        return None
    return snapshot


def _daily_content_query_state(
    receipts: tuple[ToolReceipt, ...],
    *,
    context: TrustedContext | None,
) -> str:
    query_receipts = tuple(
        receipt for receipt in receipts if receipt.tool_name == "query_report_by_date"
    )
    if not query_receipts:
        return "none"
    if len(query_receipts) > 1:
        for receipt in query_receipts:
            status = str(getattr(receipt.status, "value", receipt.status) or "")
            if (
                status == "success"
                and _validated_daily_query_snapshot(receipt, context=context) is None
            ):
                return "invalid_success"
            if status == "no_op" and not _is_valid_no_op_daily_query_receipt(
                receipt,
                context=context,
            ):
                return "invalid_no_op"
        return "multiple"
    receipt = query_receipts[0]
    status = str(getattr(receipt.status, "value", receipt.status) or "")
    if status == "success":
        snapshot = _validated_daily_query_snapshot(receipt, context=context)
        if snapshot is None:
            return "invalid_success"
        return "qualified" if snapshot["status"] == "completed" else "other"
    if status == "no_op":
        if _is_valid_no_op_daily_query_receipt(receipt, context=context):
            return "no_op"
        return "invalid_no_op"
    return "other"


def _query_state_integrity_error(query_state: str) -> str | None:
    if query_state == "invalid_success":
        return "successful Daily query returned an inconsistent snapshot"
    if query_state == "invalid_no_op":
        return "no-op Daily query returned an inconsistent snapshot"
    return None


def _is_valid_no_op_daily_query_receipt(
    receipt: ToolReceipt,
    *,
    context: TrustedContext | None,
) -> bool:
    report_date = receipt.safe_user_facts.get("report_date")
    try:
        canonical_report_date = (
            isinstance(report_date, str)
            and bool(report_date)
            and date.fromisoformat(report_date).isoformat() == report_date
        )
    except ValueError:
        canonical_report_date = False
    try:
        canonical_target_id = str(UUID(receipt.target_id)) == receipt.target_id
    except (AttributeError, ValueError):
        canonical_target_id = False
    return bool(
        receipt.tool_name == "query_report_by_date"
        and receipt.execution_mode == ExecutionMode.CANARY_EXECUTE
        and receipt.changed is False
        and receipt.target_type == "daily_report"
        and isinstance(receipt.target_id, str)
        and bool(receipt.target_id)
        and canonical_target_id
        and receipt.before_version is None
        and receipt.after_version is None
        and receipt.affected_item_ids == ()
        and receipt.error_code is None
        and receipt.would_change is False
        and receipt.validation_errors == ()
        and receipt.safe_user_facts.get("actual_write") is False
        and receipt.safe_user_facts.get("report_found") is False
        and receipt.safe_user_facts.get("report_snapshot") is None
        and canonical_report_date
        and _receipt_matches_principal_scope(receipt, context=context)
    )


def _qualified_completed_daily_query(
    receipts: tuple[ToolReceipt, ...],
    *,
    context: TrustedContext | None,
) -> tuple[ToolReceipt, dict[str, Any]] | None:
    query_receipts = tuple(
        receipt for receipt in receipts if receipt.tool_name == "query_report_by_date"
    )
    if len(query_receipts) != 1:
        return None
    receipt = query_receipts[0]
    snapshot = _validated_daily_query_snapshot(receipt, context=context)
    if snapshot is None or snapshot["status"] != "completed":
        return None
    return receipt, snapshot


def _daily_content_follow_through_tool_names(
    *,
    context: TrustedContext,
    receipts: tuple[ToolReceipt, ...],
) -> frozenset[str]:
    """Return only exposed Daily content writes after one trusted completed read."""

    if _qualified_completed_daily_query(receipts, context=context) is None:
        return frozenset()
    return frozenset(
        tool_name
        for tool_name in DAILY_CONTENT_WRITE_TOOLS
        if tool_name in context.allowed_tool_names
        and context.gate_decisions.get(tool_name) is True
        and TOOL_REGISTRY[tool_name].read_or_write == "write"
    )


def _daily_content_query_result_summaries(
    receipts: tuple[ToolReceipt, ...],
    *,
    context: TrustedContext | None,
) -> list[dict[str, Any]]:
    """Give the semantic reviewer useful shape without report or item IDs."""

    qualified = _qualified_completed_daily_query(receipts, context=context)
    if qualified is None:
        return []
    receipt, snapshot = qualified
    fields = snapshot["fields"]
    return [
        {
            "tool_name": receipt.tool_name,
            "status": "success",
            "changed": False,
            "snapshot_available": True,
            "report_date": snapshot["report_date"],
            "report_status": snapshot["status"],
            "fields": {
                field_name: [
                    {
                        "position": position,
                        "content": item["content"],
                    }
                    for position, item in enumerate(items, start=1)
                ]
                for field_name, items in fields.items()
            },
            "acknowledged_empty_fields": snapshot["acknowledged_empty_fields"],
        }
    ]


def _trusted_completed_daily_selected_targets(
    *,
    reviewed_calls: tuple[NativeToolCall, ...],
    receipts: tuple[ToolReceipt, ...],
    context: TrustedContext | None,
) -> list[dict[str, Any]]:
    qualified = _qualified_completed_daily_query(receipts, context=context)
    if qualified is None:
        return []
    _, snapshot = qualified
    targeted_calls = tuple(
        (call_index, call)
        for call_index, call in enumerate(reviewed_calls, start=1)
        if call.tool_name
        in {"edit_daily_items", "delete_daily_items", "move_daily_items"}
    )
    if not targeted_calls:
        return []

    item_locations = {
        item["item_id"]: {
            "field": field_name,
            "position": position,
            "content": item["content"],
        }
        for field_name, items in snapshot["fields"].items()
        for position, item in enumerate(items, start=1)
    }
    selected_targets: list[dict[str, Any]] = []
    seen_target_ids: set[str] = set()
    for call_index, call in targeted_calls:
        arguments = call.arguments
        if (
            arguments.get("report_id") != snapshot["report_id"]
            or arguments.get("expected_version") != snapshot["version"]
        ):
            raise ValueError("Daily content draft report binding does not match query")
        target_item_ids = arguments.get("target_item_ids")
        if not isinstance(target_item_ids, list) or not target_item_ids:
            raise ValueError("Daily content draft has no stable target items")
        for target_item_id in target_item_ids:
            if (
                not isinstance(target_item_id, str)
                or target_item_id in seen_target_ids
                or target_item_id not in item_locations
            ):
                raise ValueError(
                    "Daily content draft target item is not uniquely bound"
                )
            seen_target_ids.add(target_item_id)
            location = item_locations[target_item_id]
            if (
                call.tool_name == "move_daily_items"
                and arguments.get("source_field") != location["field"]
            ):
                raise ValueError("Daily move source field does not match target item")
            selected_target = {
                "call_index": call_index,
                "tool_name": call.tool_name,
                **location,
            }
            if call.tool_name == "move_daily_items":
                selected_target.update(
                    {
                        "source_field": arguments["source_field"],
                        "target_field": arguments["target_field"],
                    }
                )
            selected_targets.append(selected_target)
    return selected_targets


def _daily_content_write_result_summaries(
    receipts: tuple[ToolReceipt, ...],
    *,
    context: TrustedContext | None,
) -> list[dict[str, Any]]:
    qualified = _qualified_completed_daily_query(receipts, context=context)
    if qualified is None:
        return []
    _, snapshot = qualified
    return [
        {
            "tool_name": receipt.tool_name,
            "changed": receipt.changed,
            "report_date": (
                receipt.safe_user_facts.get("report_date")
                if isinstance(receipt.safe_user_facts.get("report_date"), str)
                else None
            ),
            "target_matches_query": (
                receipt.target_type == "daily_report"
                and receipt.target_id == snapshot["report_id"]
            ),
        }
        for receipt in receipts
        if receipt.tool_name in TOOL_REGISTRY
        and TOOL_REGISTRY[receipt.tool_name].read_or_write == "write"
    ]


def _daily_content_follow_through_review_messages(
    *,
    user_text: str,
    user_messages: tuple[str, ...],
    candidate_reply: str,
    receipts: tuple[ToolReceipt, ...],
    context: TrustedContext | None,
    allowed_write_tool_names: frozenset[str],
    query_state: str,
    pending_write_review: bool = False,
) -> list[dict[str, str]]:
    ordered_messages = user_messages or (user_text,)
    candidate_sha256 = hashlib.sha256(candidate_reply.encode("utf-8")).hexdigest()
    query_rule = (
        "Exactly one successful query_report_by_date has loaded one owned "
        "completed Daily Report. continue_once is available only under the "
        "strict conditions below."
        if query_state == "qualified"
        else (
            "More than one query_report_by_date receipt exists. This review "
            "may classify the reply as keep_no_write or keep_clarification, "
            "but multiple reads can never authorize continue_once."
        )
    )
    write_state_rule = (
        "This turn has one pending write batch. Use the supplied de-identified write "
        "result summaries to decide whether the requested content write against the "
        "queried completed Daily Report remains unfulfilled. A continue_once decision "
        "means the server must roll back; it does not authorize another write batch."
        if pending_write_review
        else "This turn has no business-write receipt."
    )
    decision_rule = (
        "In this pending-write mode, use keep_no_write only when the executed write "
        "summaries and proposed reply show that every requested content change to the "
        "queried Daily Report is already complete, or that the user did not request "
        "such a content change, so no additional write is needed. Use continue_once "
        "only when a requested content change to that queried report is still "
        "unfulfilled; the server will roll back instead of opening another write "
        "batch. Use keep_clarification only when the proposed reply truthfully asks a "
        "genuinely necessary clarification and does not claim that an unperformed "
        "Daily content change succeeded."
        if pending_write_review
        else (
            "Use continue_once only when the user explicitly requested one actionable "
            "Daily content-write request, including a request with multiple compatible "
            "changes in one atomic batch, the supplied successful query summary makes "
            "the target sufficiently bound for the main Agent2 to call an allowed "
            "content-write tool, and the proposed reply leaves that requested change "
            "unexecuted. Use keep_no_write when the current request is actually a read, "
            "explanation, wording task, or other non-write. Use keep_clarification only "
            "when the proposed reply is a genuinely necessary clarification because "
            "the requested target, replacement, or authority remains ambiguous; do not "
            "use it merely because a read had to happen first."
        )
    )
    return [
        {
            "role": "system",
            "content": (
                "You are an isolated Agent2 semantic follow-through reviewer. "
                "Judge the whole meaning of the current user messages; never use "
                "keywords, phrase lists, or regular expressions. "
                f"{query_rule} "
                f"{write_state_rule} Decide whether the proposed terminal reply may "
                f"safely end the turn. {decision_rule} You do not choose a tool, create tool "
                "arguments, authorize a write, replay a draft, or write a reply. "
                "Do not copy any report ID, item ID, version, or content into your "
                "output. Return exactly one JSON object with exactly two keys: "
                "decision and reviewed_reply_sha256. decision must be "
                "keep_no_write, keep_clarification, or continue_once. Copy the "
                "supplied hash exactly. Do not call tools."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "ordered_current_user_messages": [
                        {"sequence": index, "content": content}
                        for index, content in enumerate(
                            ordered_messages,
                            start=1,
                        )
                    ],
                    "proposed_terminal_reply": candidate_reply,
                    "reviewed_reply_sha256": candidate_sha256,
                    "query_state": query_state,
                    "pending_write_review": pending_write_review,
                    "pending_write_results": (
                        _daily_content_write_result_summaries(
                            receipts,
                            context=context,
                        )
                        if pending_write_review
                        else []
                    ),
                    "successful_query_results": (
                        _daily_content_query_result_summaries(
                            receipts,
                            context=context,
                        )
                    ),
                    "allowed_daily_content_write_tools": sorted(
                        allowed_write_tool_names
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
    ]


def _parse_daily_content_follow_through_review(
    *,
    review_content: str,
    candidate_reply: str,
) -> str:
    try:
        payload = json.loads(review_content)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(
            "daily content follow-through review must return JSON"
        ) from exc
    if not isinstance(payload, dict) or set(payload) != _FOLLOW_THROUGH_REVIEW_KEYS:
        raise ValueError("daily content follow-through review has an invalid envelope")
    decision = payload["decision"]
    if decision not in _FOLLOW_THROUGH_DECISIONS:
        raise ValueError("daily content follow-through review has an invalid decision")
    candidate_sha256 = hashlib.sha256(candidate_reply.encode("utf-8")).hexdigest()
    if payload["reviewed_reply_sha256"] != candidate_sha256:
        raise ValueError(
            "daily content follow-through review is not bound to the candidate"
        )
    return str(decision)


def _daily_content_follow_through_protocol_message(
    *,
    candidate_reply: str,
    allowed_write_tool_names: frozenset[str],
) -> dict[str, str]:
    return {
        "role": "system",
        "content": json.dumps(
            {
                "daily_content_follow_through": {
                    "decision": "continue_once",
                    "rejected_terminal_reply_sha256": hashlib.sha256(
                        candidate_reply.encode("utf-8")
                    ).hexdigest(),
                    "allowed_write_tool_names": sorted(allowed_write_tool_names),
                    "read_tools_allowed": False,
                    "additional_tool_enabled_turns": 1,
                    "instruction": (
                        "Continue the original current user request using only "
                        "the already returned trusted report snapshot and exactly "
                        "one complete atomic batch of applicable exposed write calls. "
                        "Derive every target, "
                        "replacement, version and source-evidence value through the "
                        "ordinary tool contract from the original user messages and "
                        "trusted tool result. This reviewer decision supplies no "
                        "business content or write authority and must never be used "
                        "as source evidence. Do not call another read tool. If a safe "
                        "write call cannot be formed, emit no tool call; the server "
                        "will fail closed rather than guess."
                    ),
                }
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    }


def _is_strict_daily_weekly_review_clarification(
    reviewed: _ParsedAssistantTurnLike,
) -> bool:
    if reviewed.tool_calls:
        return False
    content = reviewed.assistant_message.get("content")
    try:
        payload = json.loads(content) if isinstance(content, str) else None
    except json.JSONDecodeError:
        return False
    return (
        isinstance(payload, dict)
        and set(payload) == {"decision", "reply"}
        and payload.get("decision") == "clarification"
        and isinstance(payload.get("reply"), str)
        and bool(payload["reply"].strip())
        and len(payload["reply"]) <= 8000
    )
