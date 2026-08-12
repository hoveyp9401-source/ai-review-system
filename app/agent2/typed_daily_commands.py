from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime
import hashlib
from collections.abc import Collection
from types import MappingProxyType
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from app.agent2.admission_contracts import (
    ADMISSION_CONTRACT_VERSION,
    AdmissionExecutionScope,
)
from app.agent2.admission_hashes import admission_claim_hashes_match

REPORT_FIELDS = ("today_work", "problems", "tomorrow_plan")
DAILY_DRAFT_MUTABLE_STATUSES = frozenset({"collecting", "pending_confirmation"})
COMPLETED_CONTENT_MUTATION_COMMANDS = frozenset(
    {
        "append_item",
        "acknowledge_empty_section",
        "edit_item",
        "delete_item",
        "merge_items",
        "move_item",
        "move_items",
        "replace_section",
    }
)
DAILY_ADMISSION_OPERATION_CONTRACTS = MappingProxyType({
    "capture_daily_event": ("append_item", ("section", "items")),
    "submit_daily_report": ("submit_report", ("status",)),
    "delete_daily_item": ("delete_item", ("items",)),
    "edit_daily_item": ("edit_item", ("items",)),
    "merge_daily_items": ("merge_items", ("items",)),
    "replace_daily_section": ("replace_section", ("section", "items")),
    "move_daily_items": ("move_items", ("section", "items")),
    "query_daily_report": ("query_report", ()),
    "clear_daily_report": ("clear_report", ("sections", "items")),
    "clear_daily_section": ("clear_report", ("section", "items")),
    "reopen_daily_report": ("reopen_report", ("status",)),
    "copy_previous_daily_report": ("copy_report", ("sections", "items")),
    "copy_current_work_to_tomorrow": ("copy_report", ("section", "items")),
    "complete_previous_daily_plan": ("copy_report", ("section", "items")),
})
CommandType = Literal[
    "append_item",
    "acknowledge_empty_section",
    "edit_item",
    "delete_item",
    "merge_items",
    "move_item",
    "move_items",
    "replace_section",
    "submit_report",
    "query_report",
    "copy_report",
    "clear_report",
    "reopen_report",
]
ValidationStatus = Literal["authorized", "duplicate", "blocked"]


@dataclass(frozen=True)
class TypedDailyCommand:
    command_id: UUID
    decision_id: UUID
    sub_decision_id: UUID
    command_type: CommandType
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
class DailyReportMutationSnapshot:
    report_id: UUID
    owner_user_id: UUID
    version: int
    status: str
    today_work: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()
    tomorrow_plan: tuple[str, ...] = ()
    item_ids: dict[str, tuple[str, ...]] = field(default_factory=dict)
    acknowledged_empty_fields: frozenset[str] = frozenset()


@dataclass(frozen=True)
class TypedDailyCommandValidation:
    status: ValidationStatus
    reason_code: str
    detail: str = ""


@dataclass(frozen=True)
class TypedDailyAuditRecord:
    decision_id: UUID
    sub_decision_id: UUID
    command_id: UUID
    interaction_id: UUID
    message_id: str
    command_type: str
    report_id: UUID
    target_item_ids: tuple[str, ...]
    expected_version: int
    before_version: int
    after_version: int
    result: str
    reason: str
    idempotency_key: str
    validation_result: str
    execution_result: str
    actual_write: bool
    reply_type: str
    code_version: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision_id": str(self.decision_id),
            "sub_decision_id": str(self.sub_decision_id),
            "command_id": str(self.command_id),
            "interaction_id": str(self.interaction_id),
            "message_id": self.message_id,
            "command_type": self.command_type,
            "report_id": str(self.report_id),
            "target_item_ids": list(self.target_item_ids),
            "expected_version": self.expected_version,
            "before_version": self.before_version,
            "after_version": self.after_version,
            "result": self.result,
            "reason": self.reason,
            "idempotency_key": self.idempotency_key,
            "validation_result": self.validation_result,
            "execution_result": self.execution_result,
            "actual_write": self.actual_write,
            "reply_type": self.reply_type,
            "code_version": self.code_version,
        }


@dataclass(frozen=True)
class TypedDailyCommandExecution:
    command: TypedDailyCommand
    validation: TypedDailyCommandValidation
    before: DailyReportMutationSnapshot
    after: DailyReportMutationSnapshot
    changed: bool
    should_write_db: bool
    audit: TypedDailyAuditRecord


def execute_typed_daily_command(
    command: TypedDailyCommand,
    *,
    snapshot: DailyReportMutationSnapshot,
    actor_user_id: UUID,
    executed_idempotency_keys: Collection[str] = (),
    admission_scope: AdmissionExecutionScope | None = None,
    allow_completed_append: bool = False,
    allow_completed_content_mutation: bool = False,
) -> TypedDailyCommandExecution:
    validation = validate_typed_daily_command(
        command,
        snapshot=snapshot,
        actor_user_id=actor_user_id,
        executed_idempotency_keys=executed_idempotency_keys,
        admission_scope=admission_scope,
        allow_completed_append=allow_completed_append,
        allow_completed_content_mutation=allow_completed_content_mutation,
    )
    if validation.status == "duplicate":
        return TypedDailyCommandExecution(
            command,
            validation,
            snapshot,
            snapshot,
            changed=False,
            should_write_db=False,
            audit=_audit_record(
                command,
                snapshot,
                snapshot,
                result="duplicate",
                reason=validation.reason_code,
            ),
        )
    if validation.status == "blocked":
        return TypedDailyCommandExecution(
            command,
            validation,
            snapshot,
            snapshot,
            changed=False,
            should_write_db=False,
            audit=_audit_record(command, snapshot, snapshot, result="blocked", reason=validation.reason_code),
        )
    if command.command_type == "query_report":
        return TypedDailyCommandExecution(
            command,
            validation,
            snapshot,
            snapshot,
            changed=False,
            should_write_db=False,
            audit=_audit_record(command, snapshot, snapshot, result="executed", reason=validation.reason_code),
        )
    if command.command_type == "reopen_report":
        after = replace(snapshot, version=snapshot.version + 1, status="collecting")
        return TypedDailyCommandExecution(
            command,
            validation,
            snapshot,
            after,
            changed=True,
            should_write_db=True,
            audit=_audit_record(command, snapshot, after, result="executed", reason=validation.reason_code),
        )
    if command.command_type == "clear_report":
        field_name = str(command.patch["field"])
        fields = REPORT_FIELDS if field_name == "all" else (field_name,)
        after_values = {field: getattr(snapshot, field) for field in REPORT_FIELDS}
        after_item_ids = {field: tuple(snapshot.item_ids.get(field, ())) for field in REPORT_FIELDS}
        acknowledged_empty_fields = set(snapshot.acknowledged_empty_fields)
        changed = False
        for field in fields:
            changed = (
                changed
                or bool(after_values[field])
                or field in acknowledged_empty_fields
            )
            after_values[field] = ()
            after_item_ids[field] = ()
            acknowledged_empty_fields.discard(field)
        after = replace(
            snapshot,
            version=snapshot.version + (1 if changed else 0),
            item_ids=after_item_ids,
            acknowledged_empty_fields=frozenset(acknowledged_empty_fields),
            **after_values,
        )
        return TypedDailyCommandExecution(
            command,
            validation,
            snapshot,
            after,
            changed=changed,
            should_write_db=changed,
            audit=_audit_record(
                command,
                snapshot,
                after,
                result="executed" if changed else "no_change",
                reason=validation.reason_code,
            ),
        )
    if command.command_type == "submit_report":
        after = replace(snapshot, version=snapshot.version + 1, status="completed")
        return TypedDailyCommandExecution(
            command,
            validation,
            snapshot,
            after,
            changed=True,
            should_write_db=True,
            audit=_audit_record(command, snapshot, after, result="executed", reason=validation.reason_code),
        )

    if command.command_type == "acknowledge_empty_section":
        field_name = str(command.patch["field"])
        changed = field_name not in snapshot.acknowledged_empty_fields
        after = replace(
            snapshot,
            version=snapshot.version + (1 if changed else 0),
            acknowledged_empty_fields=frozenset(
                {*snapshot.acknowledged_empty_fields, field_name}
            ),
        )
        return TypedDailyCommandExecution(
            command,
            validation,
            snapshot,
            after,
            changed=changed,
            should_write_db=changed,
            audit=_audit_record(
                command,
                snapshot,
                after,
                result="executed" if changed else "no_change",
                reason=validation.reason_code,
            ),
        )

    if command.command_type == "copy_report":
        sections = command.patch["sections"]
        after_values: dict[str, tuple[str, ...]] = {}
        after_item_ids = {field: tuple(snapshot.item_ids.get(field, ())) for field in REPORT_FIELDS}
        changed = False
        acknowledged_empty_fields = set(snapshot.acknowledged_empty_fields)
        for field_name in REPORT_FIELDS:
            values = list(getattr(snapshot, field_name))
            ids = list(snapshot.item_ids.get(field_name, ()))
            for item_index, value in enumerate(sections.get(field_name, ()), start=1):
                clean_value = str(value).strip()
                if clean_value in values:
                    continue
                values.append(clean_value)
                ids.append(_new_item_id(command, field_name, item_index, clean_value))
                changed = True
                acknowledged_empty_fields.discard(field_name)
            after_values[field_name] = tuple(values)
            after_item_ids[field_name] = tuple(ids)
        after = replace(
            snapshot,
            version=snapshot.version + (1 if changed else 0),
            item_ids=after_item_ids,
            acknowledged_empty_fields=frozenset(acknowledged_empty_fields),
            **after_values,
        )
        return TypedDailyCommandExecution(
            command,
            validation,
            snapshot,
            after,
            changed=changed,
            should_write_db=changed,
            audit=_audit_record(
                command,
                snapshot,
                after,
                result="executed" if changed else "no_change",
                reason=validation.reason_code,
            ),
        )

    if command.command_type == "move_item":
        locations = [
            _locate_item(snapshot, item_id)
            for item_id in command.target_item_ids
        ]
        source_field = str(command.patch["source_field"])
        target_field = str(command.patch["target_field"])
        source_values = list(getattr(snapshot, source_field))
        source_ids = list(snapshot.item_ids.get(source_field, ()))
        target_values = list(getattr(snapshot, target_field))
        target_ids = list(snapshot.item_ids.get(target_field, ()))
        indices = sorted(
            location[1] for location in locations if location is not None
        )
        moved_values = [source_values[index] for index in indices]
        moved_ids = [source_ids[index] for index in indices]
        for index in reversed(indices):
            source_values.pop(index)
            source_ids.pop(index)
        target_values.extend(moved_values)
        target_ids.extend(moved_ids)
        after_item_ids = {
            field: tuple(snapshot.item_ids.get(field, ()))
            for field in REPORT_FIELDS
        }
        after_item_ids[source_field] = tuple(source_ids)
        after_item_ids[target_field] = tuple(target_ids)
        after = replace(
            snapshot,
            version=snapshot.version + 1,
            item_ids=after_item_ids,
            acknowledged_empty_fields=frozenset(
                set(snapshot.acknowledged_empty_fields) - {target_field}
            ),
            **{
                source_field: tuple(source_values),
                target_field: tuple(target_values),
            },
        )
        return TypedDailyCommandExecution(
            command,
            validation,
            snapshot,
            after,
            changed=True,
            should_write_db=True,
            audit=_audit_record(
                command,
                snapshot,
                after,
                result="executed",
                reason=validation.reason_code,
            ),
        )

    if command.command_type == "replace_section":
        field_name = str(command.patch["field"])
        replacement_items = [str(value).strip() for value in command.patch["items"]]
        existing_values = list(getattr(snapshot, field_name))
        existing_ids = list(snapshot.item_ids.get(field_name, ()))
        reusable: dict[str, list[str]] = {}
        for index, value in enumerate(existing_values):
            if index < len(existing_ids):
                reusable.setdefault(value, []).append(existing_ids[index])
        replacement_ids: list[str] = []
        for index, value in enumerate(replacement_items, start=1):
            pool = reusable.get(value) or []
            replacement_ids.append(
                pool.pop(0) if pool else _new_item_id(command, field_name, index, value)
            )
        after_item_ids = {
            field: tuple(snapshot.item_ids.get(field, ())) for field in REPORT_FIELDS
        }
        after_item_ids[field_name] = tuple(replacement_ids)
        changed = (
            tuple(replacement_items) != tuple(existing_values)
            or tuple(replacement_ids) != tuple(existing_ids)
            or field_name in snapshot.acknowledged_empty_fields
        )
        after = replace(
            snapshot,
            version=snapshot.version + (1 if changed else 0),
            item_ids=after_item_ids,
            acknowledged_empty_fields=frozenset(
                set(snapshot.acknowledged_empty_fields) - {field_name}
            ),
            **{field_name: tuple(replacement_items)},
        )
        return TypedDailyCommandExecution(
            command,
            validation,
            snapshot,
            after,
            changed=changed,
            should_write_db=changed,
            audit=_audit_record(
                command,
                snapshot,
                after,
                result="executed" if changed else "no_change",
                reason=validation.reason_code,
            ),
        )

    if command.command_type == "move_items":
        locations = [_locate_item(snapshot, item_id) for item_id in command.target_item_ids]
        source_field = locations[0][0]
        target_field = str(command.patch["target_field"])
        source_values = list(getattr(snapshot, source_field))
        source_ids = list(snapshot.item_ids.get(source_field, ()))
        target_values = list(getattr(snapshot, target_field))
        target_ids = list(snapshot.item_ids.get(target_field, ()))
        selected = sorted(
            (
                (location[1], source_values[location[1]], source_ids[location[1]])
                for location in locations
            ),
            key=lambda item: item[0],
        )
        for source_index, _, _ in reversed(selected):
            source_values.pop(source_index)
            source_ids.pop(source_index)
        target_keys = {
            _daily_fact_equivalence_key(value, target_field) for value in target_values
        }
        for _, value, item_id in selected:
            key = _daily_fact_equivalence_key(value, target_field)
            if key in target_keys:
                continue
            target_values.append(value)
            target_ids.append(item_id)
            target_keys.add(key)
        after_item_ids = {
            field: tuple(snapshot.item_ids.get(field, ())) for field in REPORT_FIELDS
        }
        after_item_ids[source_field] = tuple(source_ids)
        after_item_ids[target_field] = tuple(target_ids)
        after = replace(
            snapshot,
            version=snapshot.version + 1,
            item_ids=after_item_ids,
            acknowledged_empty_fields=frozenset(
                set(snapshot.acknowledged_empty_fields) - {target_field}
            ),
            **{
                source_field: tuple(source_values),
                target_field: tuple(target_values),
            },
        )
        return TypedDailyCommandExecution(
            command,
            validation,
            snapshot,
            after,
            changed=True,
            should_write_db=True,
            audit=_audit_record(
                command,
                snapshot,
                after,
                result="executed",
                reason=validation.reason_code,
            ),
        )

    if command.command_type == "append_item":
        field_name = str(command.patch["field"])
        values = list(getattr(snapshot, field_name))
        ids = list(snapshot.item_ids.get(field_name, ()))
        equivalent_keys = {
            _daily_fact_equivalence_key(value, field_name) for value in values
        }
        for item_index, value in enumerate(command.patch["items"], start=1):
            clean_value = str(value).strip()
            equivalence_key = _daily_fact_equivalence_key(clean_value, field_name)
            if clean_value in values or equivalence_key in equivalent_keys:
                continue
            values.append(clean_value)
            ids.append(_new_item_id(command, field_name, item_index, clean_value))
            equivalent_keys.add(equivalence_key)
    else:
        locations = [_locate_item(snapshot, item_id) for item_id in command.target_item_ids]
        field_name = locations[0][0]
        values = list(getattr(snapshot, field_name))
        ids = list(snapshot.item_ids.get(field_name, ()))
    if command.command_type == "append_item":
        pass
    elif command.command_type == "merge_items":
        indices = sorted(location[1] for location in locations)
        merged_text = str(command.patch.get("replacement") or "").strip() or "，".join(values[index] for index in indices)
        first_index = indices[0]
        first_id = ids[first_index]
        for index in reversed(indices):
            values.pop(index)
            ids.pop(index)
        values.insert(first_index, merged_text)
        ids.insert(first_index, first_id)
    else:
        item_index = locations[0][1]
        if command.command_type == "delete_item":
            values.pop(item_index)
            ids.pop(item_index)
        else:
            values[item_index] = str(command.patch["replacement"]).strip()
    after_item_ids = {field: tuple(snapshot.item_ids.get(field, ())) for field in REPORT_FIELDS}
    after_item_ids[field_name] = tuple(ids)
    changed = (
        tuple(values) != tuple(getattr(snapshot, field_name))
        or tuple(ids) != tuple(snapshot.item_ids.get(field_name, ()))
    )
    acknowledged_empty_fields = set(snapshot.acknowledged_empty_fields)
    if command.command_type == "append_item" and changed:
        acknowledged_empty_fields.discard(field_name)
    after = replace(
        snapshot,
        version=snapshot.version + (1 if changed else 0),
        item_ids=after_item_ids,
        acknowledged_empty_fields=frozenset(acknowledged_empty_fields),
        **{field_name: tuple(values)},
    )
    return TypedDailyCommandExecution(
        command,
        validation,
        snapshot,
        after,
        changed=changed,
        should_write_db=changed,
        audit=_audit_record(
            command,
            snapshot,
            after,
            result="executed" if changed else "no_change",
            reason=validation.reason_code,
        ),
    )


def _daily_fact_equivalence_key(value: str, field: str) -> str:
    """Deduplicate only literal text; semantic equivalence belongs to Agent2."""

    del field
    return str(value or "").strip().casefold()


def validate_typed_daily_command(
    command: TypedDailyCommand,
    *,
    snapshot: DailyReportMutationSnapshot,
    actor_user_id: UUID,
    executed_idempotency_keys: Collection[str] = (),
    admission_scope: AdmissionExecutionScope | None = None,
    allow_completed_append: bool = False,
    allow_completed_content_mutation: bool = False,
) -> TypedDailyCommandValidation:
    if (
        not isinstance(command.command_type, str)
        or not isinstance(command.report_id, UUID)
        or not isinstance(command.report_version, int)
        or isinstance(command.report_version, bool)
        or not isinstance(command.target_item_ids, tuple)
        or any(not isinstance(item_id, str) or not item_id for item_id in command.target_item_ids)
        or not isinstance(command.patch, dict)
        or not isinstance(command.idempotency_key, str)
        or not isinstance(command.admission_required, bool)
        or not isinstance(command.admission_ticket, dict)
        or not isinstance(command.admission_action_id, str)
        or not isinstance(command.admission_operation, str)
    ):
        return TypedDailyCommandValidation(
            "blocked",
            "forbidden_payload",
            "typed command contains an invalid runtime field type",
        )
    if command.admission_required and not command.admission_ticket:
        return TypedDailyCommandValidation(
            "blocked",
            "missing_admission_ticket",
            "enforced command requires an admission ticket",
        )
    if command.admission_required and str(command.admission_ticket.get("user_id") or "") != str(
        actor_user_id
    ):
        return TypedDailyCommandValidation(
            "blocked",
            "admission_ticket_scope_mismatch",
            "admission ticket actor does not match executor actor",
        )
    if command.admission_required:
        if admission_scope is None:
            return TypedDailyCommandValidation(
                "blocked",
                "admission_scope_missing",
                "executor must revalidate the enforced ticket scope",
            )
        if command.admission_ticket.get("contract_version") != ADMISSION_CONTRACT_VERSION:
            return TypedDailyCommandValidation(
                "blocked",
                "unknown_admission_contract",
                "executor does not recognize the admission contract version",
            )
        if (
            str(command.admission_ticket.get("ticket_status") or "issued") != "issued"
            or command.admission_ticket.get("executor_revalidation_required", True) is not True
            or command.admission_ticket.get("proves_business_write", False) is not False
        ):
            return TypedDailyCommandValidation(
                "blocked",
                "admission_ticket_inactive",
                "admission ticket is not executable",
            )
        ticket_scope = (
            str(command.admission_ticket.get("tenant_id") or ""),
            str(command.admission_ticket.get("user_id") or ""),
            str(command.admission_ticket.get("conversation_id") or ""),
            str(command.admission_ticket.get("source_message_id") or ""),
        )
        execution_scope = (
            admission_scope.tenant_id,
            admission_scope.user_id,
            admission_scope.conversation_id,
            admission_scope.source_message_id,
        )
        if ticket_scope != execution_scope or admission_scope.user_id != str(actor_user_id):
            return TypedDailyCommandValidation(
                "blocked",
                "admission_ticket_scope_mismatch",
                "admission ticket does not match current execution scope",
            )
        if admission_scope.conversation_state_version is None:
            return TypedDailyCommandValidation(
                "blocked",
                "admission_state_version_required",
                "executor must revalidate the conversation state version",
            )
        try:
            expected_state_version = int(
                command.admission_ticket.get("expected_conversation_state_version")
            )
        except (TypeError, ValueError):
            return TypedDailyCommandValidation(
                "blocked",
                "admission_ticket_state_version_conflict",
                "admission ticket state version is invalid",
            )
        if expected_state_version != admission_scope.conversation_state_version:
            return TypedDailyCommandValidation(
                "blocked",
                "admission_ticket_state_version_conflict",
                "conversation state changed after admission",
            )
        if (
            not command.admission_action_id
            or not command.admission_operation
            or command.admission_ticket.get("action_id") != command.admission_action_id
            or command.admission_ticket.get("domain") != "report"
            or command.admission_ticket.get("operation") != command.admission_operation
        ):
            return TypedDailyCommandValidation(
                "blocked",
                "admission_ticket_operation_mismatch",
                "admission ticket does not authorize this report operation",
            )
        object_ref = command.admission_ticket.get("object_ref")
        if not isinstance(object_ref, dict) or (
            str(object_ref.get("object_type") or "") != "daily_report"
            or str(object_ref.get("stable_id") or "") != str(command.report_id)
            or object_ref.get("version") != command.report_version
            or command.report_id != snapshot.report_id
            or command.report_version != snapshot.version
        ):
            return TypedDailyCommandValidation(
                "blocked",
                "admission_ticket_object_mismatch",
                "admission ticket does not match the current report version",
            )
        claims_reason = _validate_daily_admission_claims(command)
        if claims_reason:
            return TypedDailyCommandValidation(
                "blocked",
                claims_reason,
                "report mutation changed after semantic admission",
            )
        try:
            issued_at = datetime.fromisoformat(
                str(command.admission_ticket.get("issued_at") or "")
            )
            expires_at = datetime.fromisoformat(
                str(command.admission_ticket.get("expires_at") or "")
            )
        except ValueError:
            return TypedDailyCommandValidation(
                "blocked",
                "invalid_admission_ticket",
                "admission ticket timestamps are invalid",
            )
        if issued_at.tzinfo is None or expires_at.tzinfo is None or expires_at <= issued_at:
            return TypedDailyCommandValidation(
                "blocked",
                "invalid_admission_ticket",
                "admission ticket requires a valid aware TTL",
            )
        if admission_scope.executed_at < issued_at or admission_scope.executed_at >= expires_at:
            return TypedDailyCommandValidation(
                "blocked",
                "admission_ticket_expired",
                "admission ticket expired before execution",
            )
    if command.command_type not in {
        "append_item",
        "acknowledge_empty_section",
        "delete_item",
        "edit_item",
        "merge_items",
        "move_item",
        "move_items",
        "replace_section",
        "submit_report",
        "query_report",
        "copy_report",
        "clear_report",
        "reopen_report",
    }:
        return TypedDailyCommandValidation("blocked", "forbidden_payload", "unsupported command type")
    if command.report_id != snapshot.report_id or actor_user_id != snapshot.owner_user_id:
        return TypedDailyCommandValidation("blocked", "forbidden_payload", "report ownership mismatch")
    if not command.idempotency_key.strip():
        return TypedDailyCommandValidation("blocked", "forbidden_payload", "idempotency key is required")
    if command.idempotency_key in executed_idempotency_keys:
        return TypedDailyCommandValidation("duplicate", "duplicate_message")
    if command.report_version != snapshot.version:
        return TypedDailyCommandValidation("blocked", "version_conflict")
    if command.command_type == "query_report":
        if command.target_item_ids or set(command.patch) not in (set(), {"report_date"}):
            return TypedDailyCommandValidation(
                "blocked", "forbidden_payload", "query accepts only a resolved report date"
            )
        if command.patch:
            try:
                date.fromisoformat(str(command.patch.get("report_date") or ""))
            except ValueError:
                return TypedDailyCommandValidation(
                    "blocked", "forbidden_payload", "query report date is invalid"
                )
        return TypedDailyCommandValidation("authorized", "exact_target")
    if command.command_type == "reopen_report":
        if command.target_item_ids or set(command.patch) not in (set(), {"report_date"}):
            return TypedDailyCommandValidation(
                "blocked", "forbidden_payload", "reopen accepts only a resolved report date"
            )
        if command.patch:
            try:
                date.fromisoformat(str(command.patch.get("report_date") or ""))
            except ValueError:
                return TypedDailyCommandValidation(
                    "blocked", "forbidden_payload", "reopen report date is invalid"
                )
        if snapshot.status != "completed":
            return TypedDailyCommandValidation("blocked", "invalid_report_state")
        return TypedDailyCommandValidation("authorized", "exact_target")
    if (
        snapshot.status not in DAILY_DRAFT_MUTABLE_STATUSES
        and not (
            snapshot.status == "completed"
            and (
                (
                    allow_completed_append
                    and command.command_type == "append_item"
                )
                or (
                    allow_completed_content_mutation
                    and command.command_type
                    in COMPLETED_CONTENT_MUTATION_COMMANDS
                )
            )
        )
    ):
        return TypedDailyCommandValidation("blocked", "invalid_report_state")
    if command.command_type == "clear_report":
        if command.target_item_ids or set(command.patch) != {"field"}:
            return TypedDailyCommandValidation("blocked", "forbidden_payload", "clear requires one field")
        if command.patch.get("field") not in {*REPORT_FIELDS, "all"}:
            return TypedDailyCommandValidation("blocked", "ambiguous_target")
        return TypedDailyCommandValidation("authorized", "exact_target")
    if command.command_type == "submit_report":
        if command.target_item_ids or command.patch:
            return TypedDailyCommandValidation("blocked", "forbidden_payload", "submit does not accept targets or patch")
        if any(
            not getattr(snapshot, field_name)
            and field_name not in snapshot.acknowledged_empty_fields
            for field_name in REPORT_FIELDS
        ):
            return TypedDailyCommandValidation("blocked", "invalid_report_state", "report is incomplete")
        return TypedDailyCommandValidation("authorized", "exact_target")
    if command.command_type == "acknowledge_empty_section":
        if command.target_item_ids or set(command.patch) != {"field"}:
            return TypedDailyCommandValidation(
                "blocked",
                "forbidden_payload",
                "empty acknowledgement requires one field",
            )
        field_name = command.patch.get("field")
        if field_name not in REPORT_FIELDS:
            return TypedDailyCommandValidation(
                "blocked", "forbidden_payload", "empty acknowledgement field is invalid"
            )
        if getattr(snapshot, str(field_name)):
            return TypedDailyCommandValidation(
                "blocked",
                "invalid_report_state",
                "a non-empty field cannot be acknowledged as empty",
            )
        return TypedDailyCommandValidation("authorized", "exact_target")
    if command.command_type == "append_item":
        if command.target_item_ids:
            return TypedDailyCommandValidation("blocked", "ambiguous_target")
        if set(command.patch) != {"field", "items"}:
            return TypedDailyCommandValidation("blocked", "forbidden_payload", "append requires field and items")
        if command.patch.get("field") not in REPORT_FIELDS:
            return TypedDailyCommandValidation("blocked", "forbidden_payload", "append field is invalid")
        items = command.patch.get("items")
        if (
            not isinstance(items, (list, tuple))
            or not items
            or any(not isinstance(item, str) or not item.strip() for item in items)
        ):
            return TypedDailyCommandValidation("blocked", "forbidden_payload", "append items must be non-empty strings")
        return TypedDailyCommandValidation("authorized", "exact_target")
    if command.command_type == "replace_section":
        items = command.patch.get("items")
        if (
            command.target_item_ids
            or set(command.patch) != {"field", "items"}
            or command.patch.get("field") not in REPORT_FIELDS
            or not isinstance(items, (list, tuple))
            or not items
            or any(not isinstance(item, str) or not item.strip() for item in items)
        ):
            return TypedDailyCommandValidation(
                "blocked",
                "forbidden_payload",
                "replace section requires one field and non-empty items",
            )
        return TypedDailyCommandValidation("authorized", "exact_target")
    if command.command_type == "move_items":
        target_count = len(command.target_item_ids)
        if (
            target_count < 1
            or len(set(command.target_item_ids)) != target_count
            or set(command.patch) != {"target_field"}
            or command.patch.get("target_field") not in REPORT_FIELDS
        ):
            return TypedDailyCommandValidation(
                "blocked",
                "ambiguous_target",
                "move requires unique targets and one valid target field",
            )
        locations = [_locate_item(snapshot, item_id) for item_id in command.target_item_ids]
        if any(location is None for location in locations):
            return TypedDailyCommandValidation("blocked", "target_not_found")
        source_fields = {location[0] for location in locations}
        if len(source_fields) != 1 or str(command.patch["target_field"]) in source_fields:
            return TypedDailyCommandValidation(
                "blocked",
                "ambiguous_target",
                "move targets must share one different source field",
            )
        return TypedDailyCommandValidation("authorized", "exact_target")
    if command.command_type == "move_item":
        target_count = len(command.target_item_ids)
        source_field = command.patch.get("source_field")
        target_field = command.patch.get("target_field")
        if (
            target_count < 1
            or len(set(command.target_item_ids)) != target_count
            or set(command.patch) != {"source_field", "target_field"}
            or source_field not in REPORT_FIELDS
            or target_field not in REPORT_FIELDS
            or source_field == target_field
        ):
            return TypedDailyCommandValidation(
                "blocked",
                "ambiguous_target",
                "move requires unique targets and different valid fields",
            )
        locations = [
            _locate_item(snapshot, item_id)
            for item_id in command.target_item_ids
        ]
        if any(location is None for location in locations):
            return TypedDailyCommandValidation(
                "blocked",
                "target_not_found",
            )
        if any(
            location is None or location[0] != source_field
            for location in locations
        ):
            return TypedDailyCommandValidation(
                "blocked",
                "ambiguous_target",
                "move targets must exist in the source field",
            )
        return TypedDailyCommandValidation("authorized", "exact_target")
    if command.command_type == "copy_report":
        patch_keys = set(command.patch)
        if command.target_item_ids or patch_keys not in (
            {"sections"},
            {"sections", "source_report_date", "source_report_id"},
        ):
            return TypedDailyCommandValidation(
                "blocked", "forbidden_payload", "copy requires a resolved source report"
            )
        if "source_report_date" in patch_keys:
            try:
                date.fromisoformat(str(command.patch.get("source_report_date") or ""))
                UUID(str(command.patch.get("source_report_id") or ""))
            except (ValueError, TypeError):
                return TypedDailyCommandValidation(
                    "blocked", "forbidden_payload", "copy source report is invalid"
                )
        sections = command.patch.get("sections")
        if not isinstance(sections, dict) or not sections or set(sections) - set(REPORT_FIELDS):
            return TypedDailyCommandValidation("blocked", "forbidden_payload", "copy sections are invalid")
        if any(
            not isinstance(values, (list, tuple))
            or any(not isinstance(item, str) or not item.strip() for item in values)
            for values in sections.values()
        ):
            return TypedDailyCommandValidation("blocked", "forbidden_payload", "copy values must be strings")
        return TypedDailyCommandValidation("authorized", "exact_target")
    target_count = len(command.target_item_ids)
    invalid_target_count = (
        target_count < 2 if command.command_type == "merge_items" else target_count != 1
    )
    if invalid_target_count or len(set(command.target_item_ids)) != target_count:
        return TypedDailyCommandValidation("blocked", "ambiguous_target")
    locations = [_locate_item(snapshot, item_id) for item_id in command.target_item_ids]
    if any(location is None for location in locations):
        return TypedDailyCommandValidation("blocked", "target_not_found")
    if command.command_type == "merge_items" and len({location[0] for location in locations}) != 1:
        return TypedDailyCommandValidation("blocked", "ambiguous_target", "merge targets must share one field")
    if command.command_type == "delete_item" and command.patch:
        return TypedDailyCommandValidation("blocked", "forbidden_payload", "delete patch must be empty")
    if command.command_type == "edit_item":
        replacement = command.patch.get("replacement")
        if (
            set(command.patch) != {"replacement"}
            or not isinstance(replacement, str)
            or not replacement.strip()
        ):
            return TypedDailyCommandValidation("blocked", "forbidden_payload", "edit requires one non-empty replacement")
    if command.command_type == "merge_items":
        if not set(command.patch).issubset({"replacement"}):
            return TypedDailyCommandValidation("blocked", "forbidden_payload", "merge patch contains forbidden keys")
        if "replacement" in command.patch:
            replacement = command.patch.get("replacement")
            if not isinstance(replacement, str) or not replacement.strip():
                return TypedDailyCommandValidation("blocked", "forbidden_payload", "merge replacement must be non-empty")
    return TypedDailyCommandValidation("authorized", "exact_target")


def _validate_daily_admission_claims(command: TypedDailyCommand) -> str:
    contract = DAILY_ADMISSION_OPERATION_CONTRACTS.get(command.admission_operation)
    if contract is None:
        return "admission_ticket_operation_mismatch"
    expected_command_type, expected_changed_fields = contract
    if command.command_type != expected_command_type:
        return "admission_ticket_operation_mismatch"
    if command.admission_ticket.get("allowed_changed_fields") != list(expected_changed_fields):
        return "admission_ticket_claims_mismatch"

    has_claim_hashes = bool(
        command.admission_ticket.get("fact_claims_sha256")
        or command.admission_ticket.get("authorized_command_sha256")
    )
    # Existing capture fixtures pre-date the persisted hash fields. Runtime-issued
    # tickets include both hashes; every newly generalized operation requires them.
    if (
        (command.admission_operation != "capture_daily_event" or has_claim_hashes)
        and not admission_claim_hashes_match(command.admission_ticket)
    ):
        return "admission_ticket_claims_mismatch"

    authority_scope = command.admission_ticket.get("authority_scope")
    if not isinstance(authority_scope, dict):
        return "admission_ticket_claims_mismatch"
    if command.admission_operation == "capture_daily_event":
        expected_field = str(authority_scope.get("field") or "")
        expected_fact = str(authority_scope.get("raw_fact") or "")
        if (
            command.command_type != "append_item"
            or str(command.patch.get("field") or "") != expected_field
            or command.patch.get("items") != [expected_fact]
        ):
            return "admission_ticket_claims_mismatch"
        return ""

    expected_authority = {
        "report_type": "daily",
        "report_id": str(command.report_id),
        "report_version": command.report_version,
        "command_type": command.command_type,
        "target_item_ids": list(command.target_item_ids),
        "patch": dict(command.patch),
    }
    if authority_scope != expected_authority:
        return "admission_ticket_claims_mismatch"
    return ""


def _locate_item(snapshot: DailyReportMutationSnapshot, target_item_id: str) -> tuple[str, int] | None:
    matches: list[tuple[str, int]] = []
    for field_name in REPORT_FIELDS:
        for index, item_id in enumerate(snapshot.item_ids.get(field_name, ())):
            if item_id == target_item_id:
                matches.append((field_name, index))
    return matches[0] if len(matches) == 1 else None


def _new_item_id(command: TypedDailyCommand, field_name: str, item_index: int, value: str) -> str:
    raw = f"{command.command_id}:{field_name}:{item_index}:{value}"
    return f"di_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"


def _audit_record(
    command: TypedDailyCommand,
    before: DailyReportMutationSnapshot,
    after: DailyReportMutationSnapshot,
    *,
    result: str,
    reason: str,
) -> TypedDailyAuditRecord:
    return TypedDailyAuditRecord(
        decision_id=command.decision_id,
        sub_decision_id=command.sub_decision_id,
        command_id=command.command_id,
        interaction_id=uuid5(NAMESPACE_URL, f"agent2-daily:{command.idempotency_key}"),
        message_id=command.idempotency_key.rsplit(":daily:", 1)[0],
        command_type=command.command_type,
        report_id=command.report_id,
        target_item_ids=tuple(command.target_item_ids),
        expected_version=command.report_version,
        before_version=before.version,
        after_version=after.version,
        result=result,
        reason=reason,
        idempotency_key=command.idempotency_key,
        validation_result=(
            "authorized"
            if result in {"executed", "no_change"}
            else "duplicate"
            if result == "duplicate"
            else "blocked"
        ),
        execution_result=result,
        actual_write=result == "executed" and after != before,
        reply_type=_reply_type_for_audit(result, reason),
        code_version="daily-typed-v1",
    )


def _reply_type_for_audit(result: str, reason: str) -> str:
    if result == "executed":
        return "ack_write"
    if result == "no_change":
        return "ack_no_change"
    if result == "duplicate":
        return "ack_idempotent_replay"
    if reason in {"ambiguous_target", "target_not_found"}:
        return "clarify_target"
    if reason == "duplicate_message":
        return "ack_idempotent_replay"
    return "write_blocked"
