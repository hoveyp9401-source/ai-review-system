from __future__ import annotations

from calendar import monthrange
from collections.abc import Collection
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
import hashlib
from types import MappingProxyType
from typing import Any, Literal
from uuid import UUID

from app.agent2.admission_contracts import (
    ADMISSION_CONTRACT_VERSION,
    AdmissionExecutionScope,
)
from app.agent2.admission_hashes import admission_claim_hashes_match

PeriodicReportType = Literal["weekly", "monthly"]
PeriodicCommandType = Literal[
    "append_item",
    "edit_item",
    "delete_item",
    "query_report",
    "submit_report",
]
PERIODIC_REPORT_FIELDS = ("accomplishments", "risks", "next_plan", "metrics")
PERIODIC_COMMAND_TYPES = {
    "append_item",
    "edit_item",
    "delete_item",
    "query_report",
    "submit_report",
}
PERIODIC_ADMISSION_OPERATION_CONTRACTS = MappingProxyType({
    "capture_report_event": ("append_item", ("section", "items")),
    "submit_periodic_report": ("submit_report", ("status",)),
    "edit_periodic_report_item": ("edit_item", ("items",)),
    "delete_periodic_report_item": ("delete_item", ("items",)),
})


@dataclass(frozen=True)
class TypedPeriodicReportCommand:
    command_id: UUID
    decision_id: UUID
    sub_decision_id: UUID
    command_type: PeriodicCommandType
    report_type: PeriodicReportType
    period_key: str
    report_id: UUID
    report_version: int
    target_item_ids: tuple[str, ...]
    patch: dict[str, Any]
    idempotency_key: str
    admission_ticket: dict[str, Any] = field(default_factory=dict)
    admission_required: bool = False
    admission_action_id: str = ""
    admission_operation: str = ""

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "command_id": str(self.command_id),
            "decision_id": str(self.decision_id),
            "sub_decision_id": str(self.sub_decision_id),
            "command_type": self.command_type,
            "report_type": self.report_type,
            "period_key": self.period_key,
            "report_id": str(self.report_id),
            "report_version": self.report_version,
            "target_item_ids": list(self.target_item_ids),
            "patch": dict(self.patch),
            "idempotency_key": self.idempotency_key,
        }
        if self.admission_ticket:
            payload["admission_ticket"] = dict(self.admission_ticket)
        if self.admission_required:
            payload["admission_required"] = True
            payload["admission_action_id"] = self.admission_action_id
            payload["admission_operation"] = self.admission_operation
        return payload


@dataclass(frozen=True)
class PeriodicReportSnapshot:
    report_id: UUID
    owner_user_id: UUID
    report_type: PeriodicReportType
    period_key: str
    version: int
    status: str
    sections: dict[str, tuple[str, ...]] = field(default_factory=dict)
    item_ids: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class PeriodicReportExecution:
    command: TypedPeriodicReportCommand
    validation_status: str
    reason_code: str
    before: PeriodicReportSnapshot
    after: PeriodicReportSnapshot
    changed: bool
    should_write_db: bool


def period_bounds(report_type: PeriodicReportType, anchor: date) -> tuple[str, date, date]:
    if report_type == "weekly":
        start = anchor - timedelta(days=anchor.weekday())
        end = start + timedelta(days=6)
        iso_year, iso_week, _ = anchor.isocalendar()
        return f"{iso_year}-W{iso_week:02d}", start, end
    if report_type == "monthly":
        start = anchor.replace(day=1)
        end = anchor.replace(day=monthrange(anchor.year, anchor.month)[1])
        return f"{anchor.year:04d}-{anchor.month:02d}", start, end
    raise ValueError(f"unsupported periodic report type: {report_type}")


def execute_periodic_report_command(
    command: TypedPeriodicReportCommand,
    *,
    snapshot: PeriodicReportSnapshot,
    actor_user_id: UUID,
    executed_idempotency_keys: Collection[str] = (),
    admission_scope: AdmissionExecutionScope | None = None,
) -> PeriodicReportExecution:
    reason = _validate(
        command,
        snapshot,
        actor_user_id,
        executed_idempotency_keys,
        admission_scope,
    )
    if reason:
        status = "duplicate" if reason == "duplicate" else "blocked"
        return PeriodicReportExecution(command, status, reason, snapshot, snapshot, False, False)
    if command.command_type == "query_report":
        return PeriodicReportExecution(command, "authorized", "read_only", snapshot, snapshot, False, False)
    if command.command_type == "submit_report":
        after = replace(snapshot, status="completed", version=snapshot.version + 1)
        return PeriodicReportExecution(command, "authorized", "ok", snapshot, after, True, True)

    sections = _normalized_map(snapshot.sections)
    item_ids = _normalized_map(snapshot.item_ids)
    if command.command_type == "append_item":
        field_name = str(command.patch.get("field") or "")
        value = str(command.patch.get("value") or "").strip()
        values = list(sections.get(field_name, ()))
        ids = list(item_ids.get(field_name, ()))
        if value in values:
            return PeriodicReportExecution(command, "authorized", "no_change", snapshot, snapshot, False, False)
        values.append(value)
        ids.append(_new_item_id(command, field_name, value))
        sections[field_name] = tuple(values)
        item_ids[field_name] = tuple(ids)
    else:
        locations = _target_locations(item_ids, command.target_item_ids)
        for field_name, indexes in locations.items():
            values = list(sections.get(field_name, ()))
            ids = list(item_ids.get(field_name, ()))
            if command.command_type == "edit_item":
                replacement = str(command.patch.get("replacement") or "").strip()
                for index in indexes:
                    values[index] = replacement
            else:
                for index in sorted(indexes, reverse=True):
                    del values[index]
                    del ids[index]
            sections[field_name] = tuple(values)
            item_ids[field_name] = tuple(ids)
    after = replace(
        snapshot,
        version=snapshot.version + 1,
        sections=sections,
        item_ids=item_ids,
    )
    return PeriodicReportExecution(command, "authorized", "ok", snapshot, after, True, True)


def _validate(
    command: TypedPeriodicReportCommand,
    snapshot: PeriodicReportSnapshot,
    actor_user_id: UUID,
    executed_keys: Collection[str],
    admission_scope: AdmissionExecutionScope | None,
) -> str:
    admission_reason = _validate_periodic_admission(
        command,
        snapshot=snapshot,
        actor_user_id=actor_user_id,
        admission_scope=admission_scope,
    )
    if admission_reason:
        return admission_reason
    if command.command_type not in PERIODIC_COMMAND_TYPES:
        return "unsupported_command_type"
    if command.report_type not in {"weekly", "monthly"}:
        return "unsupported_report_type"
    if command.idempotency_key in executed_keys:
        return "duplicate"
    if snapshot.owner_user_id != actor_user_id:
        return "owner_mismatch"
    if command.report_id != snapshot.report_id:
        return "report_mismatch"
    if command.report_type != snapshot.report_type or command.period_key != snapshot.period_key:
        return "period_mismatch"
    if command.report_version != snapshot.version:
        return "version_conflict"
    if snapshot.status == "completed" and command.command_type != "query_report":
        return "report_completed"
    if command.command_type == "append_item":
        if command.patch.get("field") not in PERIODIC_REPORT_FIELDS:
            return "invalid_field"
        if not str(command.patch.get("value") or "").strip():
            return "empty_value"
    if command.command_type in {"edit_item", "delete_item"}:
        if not command.target_item_ids:
            return "missing_target"
        known = {item for values in snapshot.item_ids.values() for item in values}
        if any(item not in known for item in command.target_item_ids):
            return "target_not_found"
        if command.command_type == "edit_item" and not str(
            command.patch.get("replacement") or ""
        ).strip():
            return "empty_replacement"
    return ""


def _validate_periodic_admission(
    command: TypedPeriodicReportCommand,
    *,
    snapshot: PeriodicReportSnapshot,
    actor_user_id: UUID,
    admission_scope: AdmissionExecutionScope | None,
) -> str:
    if not isinstance(command.admission_required, bool):
        return "invalid_admission_ticket"
    if not command.admission_required:
        return ""
    ticket = command.admission_ticket
    if not ticket:
        return "missing_admission_ticket"
    if (
        not isinstance(ticket, dict)
        or not isinstance(command.admission_action_id, str)
        or not isinstance(command.admission_operation, str)
    ):
        return "invalid_admission_ticket"
    if admission_scope is None:
        return "admission_scope_missing"
    if ticket.get("contract_version") != ADMISSION_CONTRACT_VERSION:
        return "unknown_admission_contract"
    if (
        str(ticket.get("ticket_status") or "issued") != "issued"
        or ticket.get("executor_revalidation_required", True) is not True
        or ticket.get("proves_business_write", False) is not False
    ):
        return "admission_ticket_inactive"
    ticket_scope = (
        str(ticket.get("tenant_id") or ""),
        str(ticket.get("user_id") or ""),
        str(ticket.get("conversation_id") or ""),
        str(ticket.get("source_message_id") or ""),
    )
    execution_scope = (
        admission_scope.tenant_id,
        admission_scope.user_id,
        admission_scope.conversation_id,
        admission_scope.source_message_id,
    )
    if (
        ticket_scope != execution_scope
        or admission_scope.user_id != str(actor_user_id)
        or str(ticket.get("user_id") or "") != str(actor_user_id)
    ):
        return "admission_ticket_scope_mismatch"
    if admission_scope.conversation_state_version is None:
        return "admission_state_version_required"
    try:
        expected_state_version = int(ticket.get("expected_conversation_state_version"))
    except (TypeError, ValueError):
        return "admission_ticket_state_version_conflict"
    if expected_state_version != admission_scope.conversation_state_version:
        return "admission_ticket_state_version_conflict"
    if (
        not command.admission_action_id
        or not command.admission_operation
        or ticket.get("action_id") != command.admission_action_id
        or ticket.get("domain") != "report"
        or ticket.get("operation") != command.admission_operation
    ):
        return "admission_ticket_operation_mismatch"
    contract = PERIODIC_ADMISSION_OPERATION_CONTRACTS.get(command.admission_operation)
    if contract is None or command.command_type != contract[0]:
        return "admission_ticket_operation_mismatch"
    object_ref = ticket.get("object_ref")
    if not isinstance(object_ref, dict) or object_ref != {
        "object_type": "periodic_report",
        "stable_id": str(command.report_id),
        "version": command.report_version,
    }:
        return "admission_ticket_object_mismatch"
    if command.report_id != snapshot.report_id or command.report_version != snapshot.version:
        return "admission_ticket_object_mismatch"
    if ticket.get("allowed_changed_fields") != list(contract[1]):
        return "admission_ticket_claims_mismatch"
    if not admission_claim_hashes_match(ticket):
        return "admission_ticket_claims_mismatch"
    authority_scope = ticket.get("authority_scope")
    expected_authority = {
        "report_type": command.report_type,
        "period_key": command.period_key,
        "report_id": str(command.report_id),
        "report_version": command.report_version,
        "command_type": command.command_type,
        "target_item_ids": list(command.target_item_ids),
        "patch": dict(command.patch),
    }
    if authority_scope != expected_authority:
        return "admission_ticket_claims_mismatch"
    try:
        issued_at = datetime.fromisoformat(str(ticket.get("issued_at") or ""))
        expires_at = datetime.fromisoformat(str(ticket.get("expires_at") or ""))
    except ValueError:
        return "invalid_admission_ticket"
    if issued_at.tzinfo is None or expires_at.tzinfo is None or expires_at <= issued_at:
        return "invalid_admission_ticket"
    if admission_scope.executed_at < issued_at or admission_scope.executed_at >= expires_at:
        return "admission_ticket_expired"
    return ""


def _normalized_map(value: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
    return {key: tuple(items) for key, items in value.items()}


def _target_locations(item_ids, targets) -> dict[str, list[int]]:
    wanted = set(targets)
    result: dict[str, list[int]] = {}
    for field_name, ids in item_ids.items():
        indexes = [index for index, item_id in enumerate(ids) if item_id in wanted]
        if indexes:
            result[field_name] = indexes
    return result


def _new_item_id(command: TypedPeriodicReportCommand, field_name: str, value: str) -> str:
    digest = hashlib.sha256(
        f"{command.command_id}:{field_name}:{value}".encode("utf-8")
    ).hexdigest()[:16]
    return f"report-item-{digest}"
