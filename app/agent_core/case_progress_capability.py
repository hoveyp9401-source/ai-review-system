from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from typing import Any

from app.agent2.coordination_plan import ACTION_APPEND_CASE_PROGRESS, CoordinationPlan
from app.agent_core.execution_policy import AuthorizationDecision, ExecutionPolicy, authorize_capability_request
from app.agent_core.types import OperationLedgerEntry
from app.workflows.intake import IncomingMessageEnvelope, WORKFLOW_CASE_PROGRESS


@dataclass(frozen=True)
class CaseRecord:
    case_id: str
    matter_hint: str
    owner_id: str = ""
    owner_name: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "matter_hint": self.matter_hint,
            "owner_id": self.owner_id,
            "owner_name": self.owner_name,
        }


@dataclass(frozen=True)
class CaseProgressItem:
    item_id: str
    matter_hint: str
    reporter_id: str = ""
    reporter_name: str = ""
    case_id: str = ""
    owner_id: str = ""
    owner_name: str = ""
    source_text_hash: str = ""
    source_text_chars: int = 0
    confidence: float = 0.0
    notification_enabled: bool = False
    official_write_enabled: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "matter_hint": self.matter_hint,
            "reporter_id": self.reporter_id,
            "reporter_name": self.reporter_name,
            "case_id": self.case_id,
            "owner_id": self.owner_id,
            "owner_name": self.owner_name,
            "source_text_hash": self.source_text_hash,
            "source_text_chars": self.source_text_chars,
            "confidence": self.confidence,
            "notification_enabled": self.notification_enabled,
            "official_write_enabled": self.official_write_enabled,
        }


@dataclass(frozen=True)
class CaseProgressCapabilityResult:
    items: list[CaseProgressItem] = field(default_factory=list)
    operation_ledger: list[OperationLedgerEntry] = field(default_factory=list)
    changed: bool = False
    read_only: bool = False
    notification_count: int = 0
    official_write_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "items": [item.as_dict() for item in self.items],
            "operation_ledger": [entry.as_dict() for entry in self.operation_ledger],
            "changed": self.changed,
            "read_only": self.read_only,
            "notification_count": self.notification_count,
            "official_write_count": self.official_write_count,
        }


def run_case_progress_capability(
    *,
    turn_id: str,
    envelope: IncomingMessageEnvelope,
    coordination: CoordinationPlan,
    case_records: list[CaseRecord] | tuple[CaseRecord, ...] = (),
    execution_policy: ExecutionPolicy | None = None,
) -> CaseProgressCapabilityResult:
    """Create observe-only case progress candidates."""

    items: list[CaseProgressItem] = []
    entries: list[OperationLedgerEntry] = []
    read_only = False
    case_actions = [action for action in coordination.actions if action.action_type == ACTION_APPEND_CASE_PROGRESS]
    for index, action in enumerate(case_actions, start=1):
        decision = authorize_capability_request(
            execution_policy,
            turn_id=turn_id,
            capability=WORKFLOW_CASE_PROGRESS,
            operation="append_case_progress",
            requested_write_policy="sandbox",
        )
        if not decision.allowed:
            read_only = True
            entries.append(
                _operation_entry(
                    turn_id=turn_id,
                    index=index,
                    decision=decision,
                    before_state={},
                    after_state={},
                    read_only=True,
                    safety_flags=decision.safety_flags,
                    reason=decision.reason,
                )
            )
            continue

        item = _item_from_action(turn_id, envelope, action, index, list(case_records or ()))
        items.append(item)
        entries.append(
            _operation_entry(
                turn_id=turn_id,
                index=index,
                decision=decision,
                before_state={},
                after_state={"item": item.as_dict()},
                read_only=False,
                safety_flags=["sandbox", "no_production_write", "no_notification"],
                reason=action.reason or decision.reason,
            )
        )

    return CaseProgressCapabilityResult(
        items=items,
        operation_ledger=entries,
        changed=bool(items),
        read_only=read_only,
        notification_count=0,
        official_write_count=0,
    )


def _item_from_action(
    turn_id: str,
    envelope: IncomingMessageEnvelope,
    action: Any,
    index: int,
    case_records: list[CaseRecord],
) -> CaseProgressItem:
    matter_hint = str(action.target.get("matter_hint") or "").strip()
    matched = _match_case_record(matter_hint, case_records)
    return CaseProgressItem(
        item_id=_item_id(turn_id, action, index),
        matter_hint=matter_hint,
        reporter_id=envelope.sender_id,
        reporter_name=envelope.sender_name,
        case_id=matched.case_id if matched else "",
        owner_id=matched.owner_id if matched else "",
        owner_name=matched.owner_name if matched else "",
        source_text_hash=action.source_text_hash,
        source_text_chars=action.source_text_chars,
        confidence=float(action.confidence or 0.0),
        notification_enabled=False,
        official_write_enabled=False,
    )


def _match_case_record(matter_hint: str, case_records: list[CaseRecord]) -> CaseRecord | None:
    compact_hint = _compact(matter_hint)
    if not compact_hint:
        return None
    for record in case_records:
        compact_record = _compact(record.matter_hint)
        if compact_record and (compact_record == compact_hint or compact_record in compact_hint or compact_hint in compact_record):
            return record
    return None


def _operation_entry(
    *,
    turn_id: str,
    index: int,
    decision: AuthorizationDecision,
    before_state: dict[str, Any],
    after_state: dict[str, Any],
    read_only: bool,
    safety_flags: list[str],
    reason: str,
) -> OperationLedgerEntry:
    return OperationLedgerEntry(
        operation_id=_operation_id(turn_id, index),
        workflow=WORKFLOW_CASE_PROGRESS,
        capability=WORKFLOW_CASE_PROGRESS,
        operation="append_case_progress",
        write_policy=decision.write_policy if decision.allowed else "blocked",
        plan_id=decision.plan_id,
        authorization_id=decision.authorization_id,
        authorization_status="allowed" if decision.allowed else "denied",
        before_state=before_state,
        after_state=after_state,
        changed=False,
        read_only=read_only,
        safety_flags=list(safety_flags),
        reason=reason,
    )


def _operation_id(turn_id: str, index: int) -> str:
    return hashlib.sha256(f"{turn_id}:{WORKFLOW_CASE_PROGRESS}:append_case_progress:{index}".encode("utf-8")).hexdigest()[:16]


def _item_id(turn_id: str, action: Any, index: int) -> str:
    raw = f"{turn_id}:{index}:{action.source_text_hash}:{action.target}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _compact(value: str) -> str:
    return "".join(str(value or "").split())
