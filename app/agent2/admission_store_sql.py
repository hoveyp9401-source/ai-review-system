from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any, Literal, Mapping
from uuid import UUID

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent2.admission_contracts import (
    ADMISSION_CONTRACT_VERSION,
    ADMISSION_POLICY_VERSION,
)
from app.agent2.business.admission import require_business_execution_admission
from app.agent2.business.contracts import (
    BusinessCommand,
    BusinessCommandContext,
    BusinessCommandError,
    CreateCaseProgress,
    CreateTravelIntent,
    DeleteCaseProgress,
    LinkCaseProgress,
    RespondTravelCollaboration,
    SnoozeCaseFollowup,
    UpdateCaseProgress,
)
from app.agent2.case_followup_commands import (
    TriggerCaseFollowupNow,
    UpdateCaseFollowupPolicy,
)
from app.agent2.selection_continuation import selected_business_command_snapshot
from app.agent2.selection_pending import SelectionPending


_metadata = MetaData()
_identity_bindings = Table(
    "agent2_identity_bindings",
    _metadata,
    Column("binding_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("company_id", String(128), nullable=False),
    Column("department_id", String(128), nullable=False),
    Column("team_id", String(128), nullable=False),
    Column("user_id", String(128), nullable=False),
    Column("role_ids", JSONB, nullable=False),
    Column("permission_scope_json", JSONB, nullable=False),
    Column("active", Boolean, nullable=False),
)
_tickets = Table(
    "agent2_semantic_admission_tickets",
    _metadata,
    Column("ticket_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("trace_id", PG_UUID(as_uuid=True), nullable=False),
    Column("decision_id", PG_UUID(as_uuid=True), nullable=False),
    Column("tenant_id", String(128), nullable=False),
    Column("user_id", String(128), nullable=False),
    Column("conversation_id", String(256), nullable=False),
    Column("source_turn_id", String(256), nullable=False),
    Column("source_message_id", String(256), nullable=False),
    Column("action_id", String(256), nullable=False),
    Column("segment_id", String(256), nullable=False),
    Column("segment_text_sha256", String(64), nullable=False),
    Column("segment_start_offset", Integer, nullable=False),
    Column("segment_end_offset", Integer, nullable=False),
    Column("domain", String(32), nullable=False),
    Column("operation", String(128), nullable=False),
    Column("object_type", String(128), nullable=False),
    Column("object_stable_id", String(512), nullable=False),
    Column("object_version", Integer),
    Column("object_label", String(512)),
    Column("expected_conversation_state_version", Integer, nullable=False),
    Column("authority_scope_json", JSONB, nullable=False),
    Column("allowed_changed_fields_json", JSONB, nullable=False),
    Column("fact_claims_sha256", String(64), nullable=False),
    Column("authorized_command_sha256", String(64), nullable=False),
    Column("policy_version", String(128), nullable=False),
    Column("ticket_status", String(32), nullable=False),
    Column("contract_version", String(128), nullable=False),
    Column("issued_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("ttl_seconds", Integer, nullable=False),
    Column("executor_revalidation_required", Boolean, nullable=False),
    Column("proves_business_write", Boolean, nullable=False),
    Column("consumed_at", DateTime(timezone=True)),
    Column("consumed_receipt_ref", String(512)),
    Column("invalidation_reason", String(2048), nullable=False),
    Column("idempotency_key", String(512), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

_cases = Table(
    "agent2_cases",
    _metadata,
    Column("case_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("company_id", String(128), nullable=False),
    Column("department_id", String(128), nullable=False),
    Column("team_id", String(128), nullable=False),
    Column("owner_user_id", String(128), nullable=False),
    Column("status", String(64), nullable=False),
    Column("version", Integer, nullable=False),
)

_case_progress = Table(
    "agent2_case_progress",
    _metadata,
    Column("progress_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("case_id", PG_UUID(as_uuid=True), nullable=False),
    Column("reporter_id", String(128), nullable=False),
    Column("version", Integer, nullable=False),
    Column("deleted_at", DateTime(timezone=True)),
)

_travel_candidates = Table(
    "agent2_travel_collaboration_candidates",
    _metadata,
    Column("candidate_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("company_id", String(128), nullable=False),
    Column("department_id", String(128), nullable=False),
    Column("team_id", String(128), nullable=False),
    Column("participant_ids", JSONB, nullable=False),
    Column("responses_json", JSONB, nullable=False),
    Column("status", String(32), nullable=False),
    Column("version", Integer, nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
)

_followup_policies = Table(
    "agent2_case_followup_policies",
    _metadata,
    Column("policy_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("case_id", PG_UUID(as_uuid=True), nullable=False),
    Column("assigned_user_id", String(128), nullable=False),
    Column("snoozed_until", DateTime(timezone=True)),
    Column("next_due_at", DateTime(timezone=True)),
    Column("version", Integer, nullable=False),
)

_followup_tasks = Table(
    "agent2_case_followup_tasks",
    _metadata,
    Column("followup_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("case_id", PG_UUID(as_uuid=True), nullable=False),
    Column("assigned_user_id", String(128), nullable=False),
    Column("policy_id", PG_UUID(as_uuid=True)),
    Column("task_status", String(32), nullable=False),
    Column("message_status", String(32), nullable=False),
    Column("response_status", String(32), nullable=False),
    Column("provider_message_id", String(256), nullable=False),
    Column("version", Integer, nullable=False),
)

_followup_pendings = Table(
    "agent2_case_followup_pendings",
    _metadata,
    Column("pending_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("user_id", String(128), nullable=False),
    Column("conversation_id", String(256), nullable=False),
    Column("task_id", PG_UUID(as_uuid=True), nullable=False),
    Column("case_id", PG_UUID(as_uuid=True), nullable=False),
    Column("followup_id", PG_UUID(as_uuid=True), nullable=False),
    Column("candidate_versions_json", JSONB, nullable=False),
    Column("expected_state_version", Integer, nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("status", String(32), nullable=False),
    Column("version", Integer, nullable=False),
)

_information_pendings = Table(
    "agent2_information_pendings",
    _metadata,
    Column("pending_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("user_id", String(128), nullable=False),
    Column("conversation_id", String(256), nullable=False),
    Column("object_type", String(128)),
    Column("object_stable_id", String(512)),
    Column("expected_conversation_state_version", Integer, nullable=False),
    Column("pending_status", String(32), nullable=False),
    Column("consumed_at", DateTime(timezone=True)),
    Column("consumed_by_trace_id", PG_UUID(as_uuid=True)),
    Column("invalidation_reason", String(2048), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

_conversation_states = Table(
    "agent2_conversation_states",
    _metadata,
    Column("id", PG_UUID(as_uuid=True), primary_key=True),
    Column("user_key", String(128), nullable=False),
    Column("conversation_id", String(256), nullable=False),
    Column("version", Integer, nullable=False),
    Column("state_json", JSONB, nullable=False),
    Column("last_message_id", String(256), nullable=False),
)

_business_receipts = Table(
    "agent2_business_command_receipts",
    _metadata,
    Column("receipt_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("command_type", String(128), nullable=False),
    Column("actor_user_id", String(128), nullable=False),
    Column("source_message_id", String(256), nullable=False),
    Column("status", String(32), nullable=False),
    Column("actual_write", Boolean, nullable=False),
)

_periodic_report_receipts = Table(
    "agent2_periodic_report_command_receipts",
    _metadata,
    Column("receipt_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("actor_user_id", String(128), nullable=False),
    Column("source_message_id", String(256), nullable=False),
    Column("command_type", String(64), nullable=False),
    Column("status", String(32), nullable=False),
    Column("actual_write", Boolean, nullable=False),
)

_daily_report_receipts = Table(
    "agent2_daily_command_receipts",
    _metadata,
    Column("receipt_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("user_id", PG_UUID(as_uuid=True), nullable=False),
    Column("message_id", String(512), nullable=False),
    Column("command_type", String(64), nullable=False),
    Column("status", String(32), nullable=False),
    Column("actual_write", Boolean, nullable=False),
)

_ALLOWED_CHANGED_FIELDS: dict[tuple[str, str], list[str]] = {
    (
        "case",
        "record_case_progress",
    ): [
        "summary",
        "details",
        "progress_type",
        "current_status",
        "next_actions",
        "hearing_readiness",
        "blocking_issues",
    ],
    (
        "travel",
        "record_travel_event",
    ): [
        "destination",
        "start_at",
        "end_at",
        "purpose_summary",
    ],
    ("report", "capture_daily_event"): ["section", "items"],
    ("report", "submit_daily_report"): ["status"],
    ("report", "delete_daily_item"): ["items"],
    ("report", "edit_daily_item"): ["items"],
    ("report", "merge_daily_items"): ["items"],
    ("report", "query_daily_report"): [],
    ("report", "clear_daily_report"): ["sections", "items"],
    ("report", "clear_daily_section"): ["section", "items"],
    ("report", "reopen_daily_report"): ["status"],
    ("report", "copy_previous_daily_report"): ["sections", "items"],
    ("report", "copy_current_work_to_tomorrow"): ["section", "items"],
    ("report", "complete_previous_daily_plan"): ["section", "items"],
    ("report", "capture_report_event"): ["section", "items"],
    ("report", "submit_periodic_report"): ["status"],
    ("report", "edit_periodic_report_item"): ["items"],
    ("report", "delete_periodic_report_item"): ["items"],
    ("case", "delete_case_progress"): [
        "deleted_at",
        "deleted_by",
        "delete_reason",
    ],
    ("travel", "respond_travel_collaboration"): [
        "responses_json",
        "status",
        "version",
    ],
    ("case", "trigger_case_followup_now"): [
        "followup_task",
        "notification_outbox",
    ],
}

_DYNAMIC_ALLOWED_CHANGED_FIELDS: dict[tuple[str, str], tuple[str, ...]] = {
    ("case", "update_case_progress"): ("summary", "details"),
    ("case", "link_case_progress"): (
        "related_party_ids",
        "related_document_ids",
        "related_travel_intent_ids",
    ),
    ("case", "update_case_followup_policy"): (
        "cadence_type",
        "custom_interval_days",
        "snoozed_until",
        "enabled",
        "hearing_reminders_enabled",
        "stage_transition_enabled",
        "node_transition_enabled",
    ),
}

_SUCCESSFUL_RECEIPT_STATUSES = {
    "business": frozenset({"executed", "duplicate"}),
    "periodic_report": frozenset({"authorized"}),
    "daily_report": frozenset({"executed"}),
}


@dataclass
class SqlAdmissionTicketLease:
    ticket_id: UUID
    tenant_id: str
    receipt_kind: Literal["business", "periodic_report", "daily_report"]
    command_type: str
    authoritative_ticket: dict[str, Any]
    consumed: bool = False


@dataclass(frozen=True)
class AdmissionTicketExecutionRequest:
    admission_ticket: Mapping[str, Any]
    tenant_id: str
    user_id: str
    conversation_id: str
    source_message_id: str
    action_id: str
    domain: str
    operation: str
    object_ref: Mapping[str, Any]
    conversation_state_version: int | None
    executed_at: datetime
    command_type: str
    receipt_kind: Literal["business", "periodic_report", "daily_report"]
    company_id: str = ""
    department_id: str = ""
    team_id: str = ""
    actor_role_ids: tuple[str, ...] = ()
    allowed_case_ids: tuple[str, ...] = ()
    writable_case_ids: tuple[str, ...] | None = None


@dataclass(frozen=True)
class AdmissionReceiptReference:
    receipt_kind: Literal["business", "periodic_report", "daily_report"]
    receipt_id: str
    status: str
    actual_write: bool


class SqlAdmissionTicketStore:
    """PostgreSQL adapter for the single-consumer Admission Ticket seam.

    The caller owns the transaction. ``lock_and_validate`` takes a row lock and
    the lock remains held by the supplied ``AsyncSession`` until that outer
    transaction commits or rolls back. ``consume`` only stages the status
    transition; the business receipt, domain effect, and Ticket therefore have
    one commit boundary.
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def lock_and_validate(
        self,
        command: BusinessCommand,
        context: BusinessCommandContext,
    ) -> SqlAdmissionTicketLease:
        supplied = context.admission_ticket
        supplied_object = (
            supplied.get("object_ref") if isinstance(supplied, Mapping) else None
        )
        lease = await self.acquire(
            AdmissionTicketExecutionRequest(
                admission_ticket=(supplied if isinstance(supplied, Mapping) else {}),
                tenant_id=context.tenant_id,
                user_id=context.actor_user_id,
                conversation_id=context.conversation_id,
                source_message_id=context.source_message_id,
                action_id=context.admission_action_id,
                domain=str(supplied.get("domain") or "")
                if isinstance(supplied, Mapping)
                else "",
                operation=context.admission_operation,
                object_ref=(
                    supplied_object if isinstance(supplied_object, Mapping) else {}
                ),
                conversation_state_version=context.conversation_state_version,
                executed_at=(
                    context.execution_started_at or context.occurred_at
                ),
                command_type=command.command_type,
                receipt_kind="business",
                company_id=context.company_id,
                department_id=context.department_id,
                team_id=context.team_id,
                actor_role_ids=tuple(context.actor_role_ids),
                allowed_case_ids=tuple(context.allowed_case_ids),
                writable_case_ids=context.writable_case_ids,
            )
        )
        authoritative = lease.authoritative_ticket
        authoritative_context = replace(context, admission_ticket=authoritative)
        require_business_execution_admission(command, authoritative_context)
        _require_command_claims(command, authoritative)
        await self._lock_and_validate_live_object(
            command,
            authoritative_context,
            authoritative,
        )
        return lease

    async def acquire(
        self,
        request: AdmissionTicketExecutionRequest,
    ) -> SqlAdmissionTicketLease:
        supplied = request.admission_ticket
        if not isinstance(supplied, Mapping) or not supplied:
            raise BusinessCommandError(
                "missing_admission_ticket",
                "admission",
                "admission ticket is required",
            )
        ticket_id = _parse_uuid(supplied.get("ticket_id"))
        result = await self.session.execute(
            select(_tickets)
            .where(
                _tickets.c.tenant_id == request.tenant_id,
                _tickets.c.ticket_id == ticket_id,
            )
            .with_for_update()
        )
        row = result.mappings().one_or_none()
        if row is None:
            raise BusinessCommandError(
                "admission_ticket_not_found",
                "admission",
                "authoritative admission ticket does not exist",
            )
        record = dict(row)
        if str(record.get("ticket_status") or "") != "issued":
            raise BusinessCommandError(
                "admission_ticket_inactive",
                "admission",
                "authoritative admission ticket is not issued",
            )
        if str(record.get("invalidation_reason") or "").strip():
            raise BusinessCommandError(
                "admission_ticket_inactive",
                "admission",
                "authoritative admission ticket was invalidated",
            )

        authoritative = _wire_ticket(record)
        _require_ticket_integrity(authoritative)
        _require_execution_request(request, authoritative)
        if _canonical_replay_ticket(authoritative) != _canonical_replay_ticket(
            dict(supplied)
        ):
            raise BusinessCommandError(
                "admission_ticket_authority_mismatch",
                "admission",
                "command ticket differs from authoritative ticket",
            )
        await self._lock_and_validate_identity(request, authoritative)
        await self._lock_and_validate_selection_pending(
            request,
            authoritative,
        )
        return SqlAdmissionTicketLease(
            ticket_id=ticket_id,
            tenant_id=request.tenant_id,
            receipt_kind=request.receipt_kind,
            command_type=request.command_type,
            authoritative_ticket=authoritative,
        )

    async def validate_consumed_business_replay(
        self,
        command: BusinessCommand,
        context: BusinessCommandContext,
        *,
        receipt_id: str,
    ) -> None:
        """Authorize a stable duplicate without reacquiring or consuming Ticket."""

        supplied = context.admission_ticket
        if not isinstance(supplied, Mapping) or not supplied:
            raise BusinessCommandError(
                "missing_admission_ticket",
                "admission",
                "duplicate semantic execution requires its consumed Ticket",
            )
        ticket_id = _parse_uuid(supplied.get("ticket_id"))
        row = (
            await self.session.execute(
                select(_tickets)
                .where(
                    _tickets.c.tenant_id == context.tenant_id,
                    _tickets.c.ticket_id == ticket_id,
                )
                .with_for_update()
            )
        ).mappings().one_or_none()
        if row is None:
            raise BusinessCommandError(
                "admission_ticket_not_found",
                "admission",
                "duplicate Ticket no longer exists",
            )
        record = dict(row)
        if (
            str(record.get("ticket_status") or "") != "consumed"
            or str(record.get("consumed_receipt_ref") or "") != receipt_id
        ):
            raise BusinessCommandError(
                "admission_ticket_receipt_mismatch",
                "admission",
                "duplicate Ticket is not bound to this receipt",
            )
        authoritative = _wire_ticket(record)
        _require_ticket_integrity(authoritative)
        if _canonical_ticket(authoritative) != _canonical_ticket(dict(supplied)):
            raise BusinessCommandError(
                "admission_ticket_authority_mismatch",
                "admission",
                "duplicate command Ticket differs from the consumed Ticket",
            )
        _require_consumed_replay_scope(command, context, authoritative)
        request = AdmissionTicketExecutionRequest(
            admission_ticket=authoritative,
            tenant_id=context.tenant_id,
            user_id=context.actor_user_id,
            conversation_id=context.conversation_id,
            source_message_id=context.source_message_id,
            action_id=context.admission_action_id,
            domain=str(authoritative.get("domain") or ""),
            operation=context.admission_operation,
            object_ref=dict(authoritative.get("object_ref") or {}),
            conversation_state_version=context.conversation_state_version,
            executed_at=context.execution_started_at or context.occurred_at,
            command_type=command.command_type,
            receipt_kind="business",
            company_id=context.company_id,
            department_id=context.department_id,
            team_id=context.team_id,
            actor_role_ids=tuple(context.actor_role_ids),
            allowed_case_ids=tuple(context.allowed_case_ids),
            writable_case_ids=context.writable_case_ids,
        )
        await self._lock_and_validate_identity(request, authoritative)
        await self._lock_and_validate_selection_pending(request, authoritative)
        _require_command_claims(command, authoritative)
        await self._lock_and_validate_replay_access(
            command,
            context,
            authoritative,
        )

    async def validate_consumed_execution_replay(
        self,
        request: AdmissionTicketExecutionRequest,
        *,
        receipt_id: str,
    ) -> None:
        """Validate a spent deterministic Ticket for report duplicate replay."""

        supplied = request.admission_ticket
        if not isinstance(supplied, Mapping) or not supplied:
            raise BusinessCommandError(
                "missing_admission_ticket",
                "admission",
                "duplicate semantic execution requires its consumed Ticket",
            )
        ticket_id = _parse_uuid(supplied.get("ticket_id"))
        row = (
            await self.session.execute(
                select(_tickets)
                .where(
                    _tickets.c.tenant_id == request.tenant_id,
                    _tickets.c.ticket_id == ticket_id,
                )
                .with_for_update()
            )
        ).mappings().one_or_none()
        if row is None:
            raise BusinessCommandError(
                "admission_ticket_not_found",
                "admission",
                "duplicate Ticket no longer exists",
            )
        record = dict(row)
        if (
            str(record.get("ticket_status") or "") != "consumed"
            or str(record.get("consumed_receipt_ref") or "") != receipt_id
        ):
            raise BusinessCommandError(
                "admission_ticket_receipt_mismatch",
                "admission",
                "duplicate Ticket is not bound to this receipt",
            )
        authoritative = _wire_ticket(record)
        _require_ticket_integrity(authoritative)
        if _canonical_replay_ticket(authoritative) != _canonical_replay_ticket(
            dict(supplied)
        ):
            raise BusinessCommandError(
                "admission_ticket_authority_mismatch",
                "admission",
                "duplicate command Ticket differs from the consumed Ticket",
            )
        _require_consumed_execution_request(request, authoritative)
        await self._lock_and_validate_identity(request, authoritative)
        await self._lock_and_validate_selection_pending(request, authoritative)

    async def _lock_and_validate_replay_access(
        self,
        command: BusinessCommand,
        context: BusinessCommandContext,
        ticket: Mapping[str, Any],
    ) -> None:
        if isinstance(command, RespondTravelCollaboration):
            object_ref = _require_object_ref(
                ticket,
                "travel_collaboration_candidate",
            )
            row = (
                await self.session.execute(
                    select(
                        _travel_candidates.c.candidate_id,
                        _travel_candidates.c.participant_ids,
                    )
                    .where(
                        _travel_candidates.c.tenant_id == context.tenant_id,
                        _travel_candidates.c.company_id == context.company_id,
                        _travel_candidates.c.department_id
                        == context.department_id,
                        _travel_candidates.c.team_id == context.team_id,
                        _travel_candidates.c.candidate_id
                        == _parse_object_uuid(object_ref.get("stable_id")),
                    )
                    .with_for_update()
                )
            ).mappings().one_or_none()
            if row is None or context.actor_user_id not in {
                str(value) for value in (row.get("participant_ids") or ())
            }:
                raise BusinessCommandError(
                    "admission_ticket_permission_revoked",
                    "admission",
                    "travel replay permission was revoked",
                )
            return
        target_case_id = _ticket_target_case_id(ticket)
        if not target_case_id:
            return
        case_id = _parse_object_uuid(target_case_id)
        case_row = (
            await self.session.execute(
                select(
                    _cases.c.case_id,
                    _cases.c.owner_user_id,
                    _cases.c.company_id,
                    _cases.c.department_id,
                    _cases.c.team_id,
                    _cases.c.status,
                )
                .where(
                    _cases.c.tenant_id == context.tenant_id,
                    _cases.c.case_id == case_id,
                )
                .with_for_update()
            )
        ).mappings().one_or_none()
        live_roles = set(context.actor_role_ids)
        if (
            case_row is None
            or str(case_row.get("status") or "") in {"deleted", "archived"}
            or (
                str(case_row.get("owner_user_id") or "")
                != context.actor_user_id
                and "case_progress_admin" not in live_roles
                and "tenant_admin" not in live_roles
            )
            or (
                context.company_id
                and str(case_row.get("company_id") or "")
                != context.company_id
            )
        ):
            raise BusinessCommandError(
                "admission_ticket_permission_revoked",
                "admission",
                "case replay permission was revoked",
            )

    async def _lock_and_validate_live_object(
        self,
        command: BusinessCommand,
        context: BusinessCommandContext,
        ticket: Mapping[str, Any],
    ) -> None:
        if isinstance(
            command,
            (UpdateCaseProgress, DeleteCaseProgress, LinkCaseProgress),
        ):
            await self._lock_and_validate_case_progress(
                command,
                context,
                ticket,
            )
            return
        if isinstance(command, RespondTravelCollaboration):
            await self._lock_and_validate_travel_collaboration(
                command,
                context,
                ticket,
            )
            return
        if isinstance(
            command,
            (UpdateCaseFollowupPolicy, TriggerCaseFollowupNow),
        ):
            await self._lock_and_validate_followup_policy(
                command,
                context,
                ticket,
            )
            return
        if isinstance(command, SnoozeCaseFollowup):
            await self._lock_and_validate_snooze_case_followup(
                command,
                context,
                ticket,
            )
            return
        if not isinstance(command, CreateCaseProgress):
            return
        object_ref = ticket.get("object_ref")
        if not isinstance(object_ref, Mapping):
            raise BusinessCommandError(
                "admission_ticket_object_mismatch",
                "admission",
                "authoritative Ticket object is missing",
            )
        if command.case_id not in set(context.allowed_case_ids):
            raise BusinessCommandError(
                "admission_ticket_permission_revoked",
                "admission",
                "case is no longer in the actor permission scope",
            )
        case_id = _parse_object_uuid(object_ref.get("stable_id"))
        result = await self.session.execute(
            select(
                _cases.c.case_id,
                _cases.c.owner_user_id,
                _cases.c.version,
            )
            .where(
                _cases.c.tenant_id == context.tenant_id,
                _cases.c.case_id == case_id,
            )
            .with_for_update()
        )
        row = result.mappings().one_or_none()
        if row is None:
            raise BusinessCommandError(
                "admission_ticket_permission_revoked",
                "admission",
                "case assignment no longer authorizes this Ticket",
            )
        if not context.can_create_case_progress(
            str(row["case_id"]),
            owner_user_id=str(row["owner_user_id"] or ""),
        ):
            raise BusinessCommandError(
                "admission_ticket_permission_revoked",
                "admission",
                "case progress write permission was revoked",
            )
        try:
            expected_version = int(object_ref.get("version"))
            live_version = int(row["version"])
        except (KeyError, TypeError, ValueError) as exc:
            raise BusinessCommandError(
                "admission_ticket_object_version_conflict",
                "admission",
                "case version cannot be revalidated",
            ) from exc
        if live_version != expected_version:
            raise BusinessCommandError(
                "admission_ticket_object_version_conflict",
                "admission",
                "case version changed after Ticket issue",
            )

    async def _lock_and_validate_identity(
        self,
        request: AdmissionTicketExecutionRequest,
        ticket: Mapping[str, Any],
    ) -> None:
        result = await self.session.execute(
            select(
                _identity_bindings.c.company_id,
                _identity_bindings.c.department_id,
                _identity_bindings.c.team_id,
                _identity_bindings.c.role_ids,
                _identity_bindings.c.permission_scope_json,
                _identity_bindings.c.active,
            )
            .where(
                _identity_bindings.c.tenant_id == request.tenant_id,
                _identity_bindings.c.user_id == request.user_id,
            )
            .with_for_update()
        )
        row = result.mappings().one_or_none()
        if row is None or row.get("active") is not True:
            raise BusinessCommandError(
                "admission_ticket_permission_revoked",
                "admission",
                "actor identity binding is no longer active",
            )
        for field_name, expected in (
            ("company_id", request.company_id),
            ("department_id", request.department_id),
            ("team_id", request.team_id),
        ):
            if expected and str(row.get(field_name) or "") != expected:
                raise BusinessCommandError(
                    "admission_ticket_permission_revoked",
                    "admission",
                    "actor organization scope changed after Ticket issue",
                )
        live_roles = {
            str(value)
            for value in (row.get("role_ids") or ())
            if str(value).strip()
        }
        if not set(request.actor_role_ids).issubset(live_roles):
            raise BusinessCommandError(
                "admission_ticket_permission_revoked",
                "admission",
                "actor role scope changed after Ticket issue",
            )
        live_permission = row.get("permission_scope_json")
        if not isinstance(live_permission, Mapping):
            live_permission = {}
        live_case_ids = {
            str(value)
            for value in (live_permission.get("allowed_case_ids") or ())
            if str(value).strip()
        }
        target_case_id = _ticket_target_case_id(ticket)
        if (
            target_case_id
            and target_case_id not in live_case_ids
            and "case_progress_admin" not in live_roles
        ):
            raise BusinessCommandError(
                "admission_ticket_permission_revoked",
                "admission",
                "target case is no longer in the live permission scope",
            )
        if (
            target_case_id
            and request.writable_case_ids is not None
            and "writable_case_ids" not in live_permission
        ):
            raise BusinessCommandError(
                "admission_ticket_permission_revoked",
                "admission",
                "live write scope is missing",
            )
        if target_case_id and "writable_case_ids" in live_permission:
            live_writable_case_ids = {
                str(value)
                for value in (live_permission.get("writable_case_ids") or ())
                if str(value).strip()
            }
            if target_case_id not in live_writable_case_ids:
                raise BusinessCommandError(
                    "admission_ticket_permission_revoked",
                    "admission",
                    "target case is no longer in the live write scope",
                )

    async def _lock_and_validate_selection_pending(
        self,
        request: AdmissionTicketExecutionRequest,
        ticket: Mapping[str, Any],
    ) -> None:
        authority = ticket.get("authority_scope")
        if not isinstance(authority, Mapping):
            return
        pending_id = str(authority.get("selection_pending_id") or "").strip()
        if not pending_id:
            return
        result = await self.session.execute(
            select(
                _conversation_states.c.user_key,
                _conversation_states.c.conversation_id,
                _conversation_states.c.version,
                _conversation_states.c.state_json,
            )
            .where(
                _conversation_states.c.user_key
                == f"{request.tenant_id}:{request.user_id}",
                _conversation_states.c.conversation_id
                == request.conversation_id,
            )
            .with_for_update()
        )
        row = result.mappings().one_or_none()
        if row is None:
            raise BusinessCommandError(
                "selection_pending_not_found",
                "admission",
                "selection conversation state no longer exists",
            )
        try:
            row_version = int(row.get("version"))
            ticket_version = int(
                ticket.get("expected_conversation_state_version")
            )
        except (TypeError, ValueError) as exc:
            raise BusinessCommandError(
                "selection_pending_version_conflict",
                "admission",
                "selection conversation version is invalid",
            ) from exc
        payload = row.get("state_json")
        if not isinstance(payload, Mapping):
            raise BusinessCommandError(
                "selection_pending_invalid",
                "admission",
                "selection conversation payload is invalid",
            )
        if (
            row_version != ticket_version
            or int(payload.get("version", -1)) != row_version
            or str(payload.get("user_id") or "")
            != f"{request.tenant_id}:{request.user_id}"
            or str(payload.get("conversation_id") or "")
            != request.conversation_id
        ):
            raise BusinessCommandError(
                "selection_pending_version_conflict",
                "admission",
                "selection conversation changed before execution",
            )
        raw_pendings = payload.get("selection_pending")
        if not isinstance(raw_pendings, list):
            raw_pendings = []
        matches = [
            item
            for item in raw_pendings
            if isinstance(item, dict)
            and str(item.get("pending_id") or "") == pending_id
        ]
        if len(matches) != 1:
            raise BusinessCommandError(
                "selection_pending_not_unique",
                "admission",
                "selection pending is missing or not unique",
            )
        try:
            pending = SelectionPending.from_dict(matches[0])
        except (TypeError, ValueError) as exc:
            raise BusinessCommandError(
                "selection_pending_invalid",
                "admission",
                "selection pending payload is invalid",
            ) from exc
        executed_at = request.executed_at.astimezone(timezone.utc)
        candidate_id = str(authority.get("candidate_stable_id") or "")
        try:
            candidate_version = int(authority.get("candidate_version"))
        except (TypeError, ValueError) as exc:
            raise BusinessCommandError(
                "selection_pending_candidate_conflict",
                "admission",
                "selected candidate version is invalid",
            ) from exc
        candidate = next(
            (
                item
                for item in pending.candidates
                if item.stable_id == candidate_id
                and item.version == candidate_version
            ),
            None,
        )
        if (
            pending.tenant_id != request.tenant_id
            or pending.user_id != request.user_id
            or pending.conversation_id != request.conversation_id
            or pending.source_turn_id
            != str(authority.get("selection_pending_source_turn_id") or "")
            or pending.expected_conversation_state_version != row_version
            or pending.status != "active"
            or pending.expires_at.astimezone(timezone.utc) <= executed_at
            or candidate is None
        ):
            raise BusinessCommandError(
                "selection_pending_conflict",
                "admission",
                "selection pending scope, lifetime, or candidate changed",
            )
        try:
            snapshot = selected_business_command_snapshot(pending, candidate)
        except (TypeError, ValueError) as exc:
            raise BusinessCommandError(
                "selection_pending_continuation_conflict",
                "admission",
                "selection continuation protection failed",
            ) from exc
        final_claims = authority.get("final_command_claims")
        if not isinstance(final_claims, Mapping) or (
            snapshot.original_source_digest
            != str(authority.get("original_source_digest") or "")
            or snapshot.original_continuation_sha256
            != str(authority.get("original_continuation_sha256") or "")
            or snapshot.bound_payload_sha256
            != str(final_claims.get("bound_payload_sha256") or "")
        ):
            raise BusinessCommandError(
                "selection_pending_continuation_conflict",
                "admission",
                "selection continuation digest changed",
            )

    async def _lock_and_validate_case_progress(
        self,
        command: UpdateCaseProgress | DeleteCaseProgress | LinkCaseProgress,
        context: BusinessCommandContext,
        ticket: Mapping[str, Any],
    ) -> None:
        object_ref = _require_object_ref(ticket, "case_progress")
        progress_id = _parse_object_uuid(object_ref.get("stable_id"))
        row = (
            await self.session.execute(
                select(
                    _case_progress.c.progress_id,
                    _case_progress.c.case_id,
                    _case_progress.c.reporter_id,
                    _case_progress.c.version,
                    _case_progress.c.deleted_at,
                )
                .where(
                    _case_progress.c.tenant_id == context.tenant_id,
                    _case_progress.c.progress_id == progress_id,
                )
                .with_for_update()
            )
        ).mappings().one_or_none()
        if row is None or row.get("deleted_at") is not None:
            raise BusinessCommandError(
                "admission_ticket_object_missing",
                "admission",
                "case progress is missing or no longer writable",
            )
        case_id = str(row.get("case_id") or "")
        if case_id not in set(context.allowed_case_ids):
            raise BusinessCommandError(
                "admission_ticket_permission_revoked",
                "admission",
                "case progress permission was revoked",
            )
        if (
            str(row.get("reporter_id") or "") != context.actor_user_id
            and "case_progress_admin" not in set(context.actor_role_ids)
        ):
            raise BusinessCommandError(
                "admission_ticket_permission_revoked",
                "admission",
                "case progress ownership changed",
            )
        expected_version = _required_version(object_ref)
        if (
            int(row.get("version") or -1) != expected_version
            or command.expected_version != expected_version
        ):
            raise BusinessCommandError(
                "admission_ticket_object_version_conflict",
                "admission",
                "case progress version changed after Ticket issue",
            )
        case_row = (
            await self.session.execute(
                select(_cases.c.case_id)
                .where(
                    _cases.c.tenant_id == context.tenant_id,
                    _cases.c.case_id == row.get("case_id"),
                )
                .with_for_update()
            )
        ).mappings().one_or_none()
        if case_row is None:
            raise BusinessCommandError(
                "admission_ticket_permission_revoked",
                "admission",
                "case is no longer in the tenant permission scope",
            )

    async def _lock_and_validate_travel_collaboration(
        self,
        command: RespondTravelCollaboration,
        context: BusinessCommandContext,
        ticket: Mapping[str, Any],
    ) -> None:
        object_ref = _require_object_ref(
            ticket,
            "travel_collaboration_candidate",
        )
        candidate_id = _parse_object_uuid(object_ref.get("stable_id"))
        row = (
            await self.session.execute(
                select(
                    _travel_candidates.c.candidate_id,
                    _travel_candidates.c.participant_ids,
                    _travel_candidates.c.responses_json,
                    _travel_candidates.c.status,
                    _travel_candidates.c.version,
                    _travel_candidates.c.expires_at,
                )
                .where(
                    _travel_candidates.c.tenant_id == context.tenant_id,
                    _travel_candidates.c.company_id == context.company_id,
                    _travel_candidates.c.department_id == context.department_id,
                    _travel_candidates.c.team_id == context.team_id,
                    _travel_candidates.c.candidate_id == candidate_id,
                )
                .with_for_update()
            )
        ).mappings().one_or_none()
        if row is None:
            raise BusinessCommandError(
                "admission_ticket_permission_revoked",
                "admission",
                "travel collaboration is outside the live organization scope",
            )
        if context.actor_user_id not in {
            str(value) for value in (row.get("participant_ids") or ())
        }:
            raise BusinessCommandError(
                "admission_ticket_permission_revoked",
                "admission",
                "actor is no longer a travel collaboration participant",
            )
        if str(row.get("status") or "") not in {
            "notified",
            "accepted_by_one",
        }:
            raise BusinessCommandError(
                "admission_ticket_object_conflict",
                "admission",
                "travel collaboration is no longer awaiting this response",
            )
        expires_at = _as_datetime(row.get("expires_at"))
        executed_at = context.execution_started_at or context.occurred_at
        if expires_at is None or expires_at <= executed_at.astimezone(timezone.utc):
            raise BusinessCommandError(
                "admission_ticket_expired",
                "admission",
                "travel collaboration expired before response execution",
            )
        expected_version = _required_version(object_ref)
        if (
            int(row.get("version") or -1) != expected_version
            or command.expected_version != expected_version
        ):
            raise BusinessCommandError(
                "admission_ticket_object_version_conflict",
                "admission",
                "travel collaboration version changed after Ticket issue",
            )
        if context.actor_user_id in dict(row.get("responses_json") or {}):
            raise BusinessCommandError(
                "admission_ticket_object_conflict",
                "admission",
                "actor already responded to this collaboration",
            )
        if command.response not in {
            "accept",
            "decline",
            "later",
            "changed",
            "cancel",
        }:
            raise BusinessCommandError(
                "admission_ticket_claims_mismatch",
                "admission",
                "travel response is outside the closed contract",
            )

    async def _lock_and_validate_snooze_case_followup(
        self,
        command: SnoozeCaseFollowup,
        context: BusinessCommandContext,
        ticket: Mapping[str, Any],
    ) -> None:
        object_ref = _require_object_ref(ticket, "case_followup_pending")
        authority = ticket.get("authority_scope")
        if not isinstance(authority, Mapping):
            raise BusinessCommandError(
                "admission_ticket_claims_mismatch",
                "admission",
                "follow-up snooze authority is missing",
            )
        pending_id = _parse_object_uuid(object_ref.get("stable_id"))
        case_id = _parse_object_uuid(authority.get("case_id"))
        followup_id = _parse_object_uuid(authority.get("followup_id"))
        task_id = _parse_object_uuid(authority.get("task_id"))
        if (
            command.pending_id != str(pending_id)
            or command.case_id != str(case_id)
            or str(authority.get("assigned_user_id") or "")
            != context.actor_user_id
            or str(authority.get("conversation_id") or "")
            != context.conversation_id
            or command.snoozed_until.isoformat()
            != str(authority.get("snoozed_until") or "")
        ):
            raise BusinessCommandError(
                "admission_ticket_claims_mismatch",
                "admission",
                "follow-up snooze command changed after admission",
            )
        pending = (
            await self.session.execute(
                select(
                    _followup_pendings.c.pending_id,
                    _followup_pendings.c.task_id,
                    _followup_pendings.c.case_id,
                    _followup_pendings.c.followup_id,
                    _followup_pendings.c.candidate_versions_json,
                    _followup_pendings.c.expected_state_version,
                    _followup_pendings.c.expires_at,
                    _followup_pendings.c.status,
                    _followup_pendings.c.version,
                )
                .where(
                    _followup_pendings.c.tenant_id == context.tenant_id,
                    _followup_pendings.c.user_id == context.actor_user_id,
                    _followup_pendings.c.conversation_id
                    == context.conversation_id,
                    _followup_pendings.c.pending_id == pending_id,
                )
                .with_for_update()
            )
        ).mappings().one_or_none()
        executed_at = context.execution_started_at or context.occurred_at
        expires_at = _as_datetime(pending.get("expires_at")) if pending else None
        if (
            pending is None
            or str(pending.get("status") or "")
            not in {"active", "awaiting_input"}
            or expires_at is None
            or expires_at <= executed_at.astimezone(timezone.utc)
        ):
            raise BusinessCommandError(
                "admission_ticket_object_conflict",
                "admission",
                "follow-up pending is no longer active",
            )
        if (
            int(pending.get("version") or -1)
            != int(authority.get("pending_version") or -2)
            or int(pending.get("version") or -1)
            != _required_version(object_ref)
            or pending.get("task_id") != task_id
            or pending.get("case_id") != case_id
            or pending.get("followup_id") != followup_id
            or int(pending.get("expected_state_version") or -1)
            != int(authority.get("expected_state_version") or -2)
        ):
            raise BusinessCommandError(
                "admission_ticket_object_version_conflict",
                "admission",
                "follow-up pending changed after Ticket issue",
            )
        candidate_versions = pending.get("candidate_versions_json") or {}
        if int(candidate_versions.get(str(case_id), -1)) != int(
            authority.get("case_version") or -2
        ):
            raise BusinessCommandError(
                "admission_ticket_object_version_conflict",
                "admission",
                "follow-up case version changed after Ticket issue",
            )
        task = (
            await self.session.execute(
                select(
                    _followup_tasks.c.followup_id,
                    _followup_tasks.c.case_id,
                    _followup_tasks.c.assigned_user_id,
                    _followup_tasks.c.policy_id,
                    _followup_tasks.c.task_status,
                    _followup_tasks.c.message_status,
                    _followup_tasks.c.response_status,
                    _followup_tasks.c.provider_message_id,
                    _followup_tasks.c.version,
                )
                .where(
                    _followup_tasks.c.tenant_id == context.tenant_id,
                    _followup_tasks.c.followup_id == followup_id,
                    _followup_tasks.c.case_id == case_id,
                    _followup_tasks.c.assigned_user_id
                    == context.actor_user_id,
                )
                .with_for_update()
            )
        ).mappings().one_or_none()
        if (
            task is None
            or str(task.get("task_status") or "") != "waiting_for_reply"
            or str(task.get("message_status") or "")
            not in {"accepted_by_provider", "delivery_confirmed"}
            or not str(task.get("provider_message_id") or "").strip()
            or int(task.get("version") or -1)
            != int(authority.get("task_version") or -2)
        ):
            raise BusinessCommandError(
                "admission_ticket_object_version_conflict",
                "admission",
                "follow-up task changed after Ticket issue",
            )
        raw_policy_id = str(authority.get("policy_id") or "").strip()
        if not raw_policy_id:
            return
        policy_id = _parse_object_uuid(raw_policy_id)
        policy = (
            await self.session.execute(
                select(
                    _followup_policies.c.policy_id,
                    _followup_policies.c.version,
                )
                .where(
                    _followup_policies.c.tenant_id == context.tenant_id,
                    _followup_policies.c.policy_id == policy_id,
                    _followup_policies.c.case_id == case_id,
                    _followup_policies.c.assigned_user_id
                    == context.actor_user_id,
                )
                .with_for_update()
            )
        ).mappings().one_or_none()
        if policy is None or int(policy.get("version") or -1) != int(
            authority.get("policy_version") or -2
        ):
            raise BusinessCommandError(
                "admission_ticket_object_version_conflict",
                "admission",
                "follow-up policy changed after Ticket issue",
            )

    async def _lock_and_validate_followup_policy(
        self,
        command: UpdateCaseFollowupPolicy | TriggerCaseFollowupNow,
        context: BusinessCommandContext,
        ticket: Mapping[str, Any],
    ) -> None:
        object_ref = _require_object_ref(ticket, "case_followup_policy")
        case_id = _parse_object_uuid(object_ref.get("stable_id"))
        assigned_user_id = command.assigned_user_id
        if (
            command.tenant_id != context.tenant_id
            or str(case_id) not in set(context.allowed_case_ids)
            or (
                assigned_user_id != context.actor_user_id
                and "tenant_admin" not in set(context.actor_role_ids)
            )
        ):
            raise BusinessCommandError(
                "admission_ticket_permission_revoked",
                "admission",
                "follow-up policy permission changed",
            )
        case_row = (
            await self.session.execute(
                select(_cases.c.case_id, _cases.c.owner_user_id)
                .where(
                    _cases.c.tenant_id == context.tenant_id,
                    _cases.c.case_id == case_id,
                    _cases.c.owner_user_id == assigned_user_id,
                )
                .with_for_update()
            )
        ).mappings().one_or_none()
        if case_row is None:
            raise BusinessCommandError(
                "admission_ticket_permission_revoked",
                "admission",
                "case assignment changed after Ticket issue",
            )
        policy_row = (
            await self.session.execute(
                select(_followup_policies.c.policy_id, _followup_policies.c.version)
                .where(
                    _followup_policies.c.tenant_id == context.tenant_id,
                    _followup_policies.c.case_id == case_id,
                    _followup_policies.c.assigned_user_id == assigned_user_id,
                )
                .with_for_update()
            )
        ).mappings().one_or_none()
        live_version = int(policy_row.get("version") or 0) if policy_row else 0
        expected_version = _required_version(object_ref)
        command_version = (
            command.expected_version
            if isinstance(command, UpdateCaseFollowupPolicy)
            else command.expected_policy_version
        )
        if live_version != expected_version or command_version != expected_version:
            raise BusinessCommandError(
                "admission_ticket_object_version_conflict",
                "admission",
                "follow-up policy version changed after Ticket issue",
            )
        if isinstance(command, TriggerCaseFollowupNow):
            active = (
                await self.session.execute(
                    select(_followup_tasks.c.followup_id)
                    .where(
                        _followup_tasks.c.tenant_id == context.tenant_id,
                        _followup_tasks.c.case_id == case_id,
                        _followup_tasks.c.assigned_user_id == assigned_user_id,
                        _followup_tasks.c.task_status.in_(
                            ("scheduled", "queued", "sending", "waiting_for_reply")
                        ),
                    )
                    .with_for_update()
                )
            ).mappings().one_or_none()
            if active is not None:
                raise BusinessCommandError(
                    "admission_ticket_object_conflict",
                    "admission",
                    "an active follow-up task already exists for this case",
                )

    async def consume(
        self,
        lease: SqlAdmissionTicketLease,
        *,
        receipt: AdmissionReceiptReference,
        consumed_at: datetime,
    ) -> None:
        if lease.consumed:
            raise BusinessCommandError(
                "admission_ticket_already_consumed",
                "admission",
                "ticket lease was already consumed",
            )
        if receipt.receipt_kind != lease.receipt_kind:
            raise BusinessCommandError(
                "admission_ticket_receipt_kind_mismatch",
                "admission",
                "receipt kind does not match the Ticket execution lease",
            )
        if receipt.status not in _SUCCESSFUL_RECEIPT_STATUSES[lease.receipt_kind]:
            raise BusinessCommandError(
                "admission_ticket_receipt_not_successful",
                "admission",
                "Ticket consumption requires a successful receipt status",
            )
        if not str(receipt.receipt_id or "").strip():
            raise BusinessCommandError(
                "admission_ticket_receipt_required",
                "admission",
                "committed receipt reference is required",
            )
        if consumed_at.tzinfo is None or consumed_at.utcoffset() is None:
            raise BusinessCommandError(
                "admission_execution_time_required",
                "admission",
                "ticket consumption time must be timezone-aware",
            )
        receipt_id = _parse_receipt_uuid(receipt.receipt_id)
        receipt_result = await self.session.execute(
            _receipt_select(
                lease=lease,
                receipt_id=receipt_id,
            )
        )
        persisted_receipt = receipt_result.mappings().one_or_none()
        if (
            persisted_receipt is None
            or str(persisted_receipt.get("status") or "") != receipt.status
            or bool(persisted_receipt.get("actual_write")) is not receipt.actual_write
        ):
            raise BusinessCommandError(
                "admission_ticket_receipt_not_successful",
                "admission",
                "Ticket consumption requires its successful write receipt",
            )
        result = await self.session.execute(
            update(_tickets)
            .where(
                _tickets.c.tenant_id == lease.tenant_id,
                _tickets.c.ticket_id == lease.ticket_id,
                _tickets.c.ticket_status == "issued",
            )
            .values(
                ticket_status="consumed",
                consumed_at=consumed_at,
                consumed_receipt_ref=str(receipt_id),
                updated_at=consumed_at,
            )
        )
        if result.rowcount != 1:
            raise BusinessCommandError(
                "admission_ticket_inactive",
                "admission",
                "authoritative admission ticket is no longer issued",
            )
        await self._consume_information_pending_if_bound(
            lease,
            receipt=receipt,
            consumed_at=consumed_at,
        )
        lease.consumed = True

    async def _consume_information_pending_if_bound(
        self,
        lease: SqlAdmissionTicketLease,
        *,
        receipt: AdmissionReceiptReference,
        consumed_at: datetime,
    ) -> None:
        """Settle a continuation Pending at the same commit boundary.

        Only the fresh authoritative Ticket may carry this binding.  The
        original Information Pending is non-writable and never authorizes the
        command by itself.
        """

        ticket = lease.authoritative_ticket
        authority = ticket.get("authority_scope")
        if not isinstance(authority, Mapping):
            return
        raw_pending_id = str(authority.get("information_pending_id") or "").strip()
        if not raw_pending_id:
            return
        if lease.receipt_kind != "business" or not receipt.actual_write:
            raise BusinessCommandError(
                "information_pending_receipt_not_committed",
                "admission",
                "Information Pending settlement requires a committed business write",
            )
        pending_id = _parse_uuid(raw_pending_id)
        trace_id = _parse_uuid(ticket.get("trace_id"))
        object_ref = ticket.get("object_ref")
        if not isinstance(object_ref, Mapping):
            raise BusinessCommandError(
                "information_pending_ticket_binding_invalid",
                "admission",
                "fresh Ticket object binding is missing",
            )
        try:
            expected_state_version = int(
                ticket.get("expected_conversation_state_version")
            )
        except (TypeError, ValueError) as exc:
            raise BusinessCommandError(
                "information_pending_ticket_binding_invalid",
                "admission",
                "fresh Ticket state binding is invalid",
            ) from exc
        result = await self.session.execute(
            update(_information_pendings)
            .where(
                _information_pendings.c.tenant_id == lease.tenant_id,
                _information_pendings.c.user_id == str(ticket.get("user_id") or ""),
                _information_pendings.c.conversation_id
                == str(ticket.get("conversation_id") or ""),
                _information_pendings.c.pending_id == pending_id,
                _information_pendings.c.object_type
                == str(object_ref.get("object_type") or ""),
                _information_pendings.c.object_stable_id
                == str(object_ref.get("stable_id") or ""),
                _information_pendings.c.expected_conversation_state_version
                == expected_state_version,
                _information_pendings.c.pending_status.in_(
                    ("active", "awaiting_input")
                ),
            )
            .values(
                pending_status="consumed",
                consumed_at=consumed_at,
                consumed_by_trace_id=trace_id,
                invalidation_reason="",
                updated_at=consumed_at,
            )
        )
        if result.rowcount != 1:
            raise BusinessCommandError(
                "information_pending_settlement_conflict",
                "admission",
                "Information Pending changed before committed receipt settlement",
            )


def _wire_ticket(row: Mapping[str, Any]) -> dict[str, Any]:
    object_ref: dict[str, Any] = {
        "object_type": str(row.get("object_type") or ""),
        "stable_id": str(row.get("object_stable_id") or ""),
        "version": row.get("object_version"),
    }
    if row.get("object_label") is not None:
        object_ref["label"] = str(row["object_label"])
    return {
        "ticket_id": str(row.get("ticket_id") or ""),
        "trace_id": str(row.get("trace_id") or ""),
        "decision_id": str(row.get("decision_id") or ""),
        "tenant_id": str(row.get("tenant_id") or ""),
        "user_id": str(row.get("user_id") or ""),
        "conversation_id": str(row.get("conversation_id") or ""),
        "source_turn_id": str(row.get("source_turn_id") or ""),
        "source_message_id": str(row.get("source_message_id") or ""),
        "action_id": str(row.get("action_id") or ""),
        "segment_id": str(row.get("segment_id") or ""),
        "segment_text_sha256": str(row.get("segment_text_sha256") or ""),
        "segment_start_offset": row.get("segment_start_offset"),
        "segment_end_offset": row.get("segment_end_offset"),
        "domain": str(row.get("domain") or ""),
        "operation": str(row.get("operation") or ""),
        "object_ref": object_ref,
        "expected_conversation_state_version": row.get(
            "expected_conversation_state_version"
        ),
        "authority_scope": dict(row.get("authority_scope_json") or {}),
        "allowed_changed_fields": list(
            row.get("allowed_changed_fields_json") or []
        ),
        "fact_claims_sha256": str(row.get("fact_claims_sha256") or ""),
        "authorized_command_sha256": str(
            row.get("authorized_command_sha256") or ""
        ),
        "policy_version": str(row.get("policy_version") or ""),
        "ticket_status": str(row.get("ticket_status") or ""),
        "contract_version": str(row.get("contract_version") or ""),
        "issued_at": _as_iso(row.get("issued_at")),
        "expires_at": _as_iso(row.get("expires_at")),
        "ttl_seconds": row.get("ttl_seconds"),
        "executor_revalidation_required": row.get(
            "executor_revalidation_required"
        ),
        "proves_business_write": row.get("proves_business_write"),
        "consumed_receipt_ref": row.get("consumed_receipt_ref"),
        "idempotency_key": str(row.get("idempotency_key") or ""),
    }


def _receipt_select(
    *,
    lease: SqlAdmissionTicketLease,
    receipt_id: UUID,
):
    ticket = lease.authoritative_ticket
    user_id = str(ticket.get("user_id") or "")
    source_message_id = str(ticket.get("source_message_id") or "")
    if lease.receipt_kind == "business":
        return (
            select(
                _business_receipts.c.receipt_id,
                _business_receipts.c.status,
                _business_receipts.c.actual_write,
            )
            .where(
                _business_receipts.c.tenant_id == lease.tenant_id,
                _business_receipts.c.receipt_id == receipt_id,
                _business_receipts.c.command_type == lease.command_type,
                _business_receipts.c.actor_user_id == user_id,
                _business_receipts.c.source_message_id == source_message_id,
            )
            .with_for_update()
        )
    if lease.receipt_kind == "periodic_report":
        return (
            select(
                _periodic_report_receipts.c.receipt_id,
                _periodic_report_receipts.c.status,
                _periodic_report_receipts.c.actual_write,
            )
            .where(
                _periodic_report_receipts.c.tenant_id == lease.tenant_id,
                _periodic_report_receipts.c.receipt_id == receipt_id,
                _periodic_report_receipts.c.command_type == lease.command_type,
                _periodic_report_receipts.c.actor_user_id == user_id,
                _periodic_report_receipts.c.source_message_id
                == source_message_id,
            )
            .with_for_update()
        )
    if lease.receipt_kind == "daily_report":
        try:
            daily_user_id = UUID(user_id)
        except ValueError as exc:
            raise BusinessCommandError(
                "admission_ticket_scope_mismatch",
                "admission",
                "daily receipt actor is not a UUID",
            ) from exc
        return (
            select(
                _daily_report_receipts.c.receipt_id,
                _daily_report_receipts.c.status,
                _daily_report_receipts.c.actual_write,
            )
            .where(
                _daily_report_receipts.c.tenant_id == lease.tenant_id,
                _daily_report_receipts.c.receipt_id == receipt_id,
                _daily_report_receipts.c.command_type == lease.command_type,
                _daily_report_receipts.c.user_id == daily_user_id,
                _daily_report_receipts.c.message_id == source_message_id,
            )
            .with_for_update()
        )
    raise BusinessCommandError(
        "unknown_admission_receipt_kind",
        "admission",
        "executor receipt kind is not supported",
    )


def _require_ticket_integrity(ticket: Mapping[str, Any]) -> None:
    if str(ticket.get("contract_version") or "") != ADMISSION_CONTRACT_VERSION:
        raise BusinessCommandError(
            "unknown_admission_contract",
            "admission",
            "authoritative ticket contract version is not executable",
        )
    if str(ticket.get("policy_version") or "") != ADMISSION_POLICY_VERSION:
        raise BusinessCommandError(
            "unknown_admission_policy",
            "admission",
            "authoritative ticket policy version is not executable",
        )
    issued_at = _as_datetime(ticket.get("issued_at"))
    expires_at = _as_datetime(ticket.get("expires_at"))
    try:
        ttl_seconds = int(ticket.get("ttl_seconds"))
    except (TypeError, ValueError) as exc:
        raise BusinessCommandError(
            "admission_ticket_time_invalid",
            "admission",
            "authoritative ticket TTL is invalid",
        ) from exc
    if (
        issued_at is None
        or expires_at is None
        or not 1 <= ttl_seconds <= 3600
        or expires_at != issued_at + timedelta(seconds=ttl_seconds)
    ):
        raise BusinessCommandError(
            "admission_ticket_time_invalid",
            "admission",
            "authoritative ticket lifetime is invalid",
        )
    authority_scope = ticket.get("authority_scope")
    allowed_changed_fields = ticket.get("allowed_changed_fields")
    if not isinstance(authority_scope, Mapping) or not isinstance(
        allowed_changed_fields, list
    ):
        raise BusinessCommandError(
            "admission_ticket_claims_mismatch",
            "admission",
            "authoritative ticket claims are malformed",
        )
    contract_key = (
        str(ticket.get("domain") or ""),
        str(ticket.get("operation") or ""),
    )
    expected_changed_fields = _ALLOWED_CHANGED_FIELDS.get(contract_key)
    dynamic_changed_fields = _DYNAMIC_ALLOWED_CHANGED_FIELDS.get(contract_key)
    changed_fields_valid = (
        allowed_changed_fields == expected_changed_fields
        if expected_changed_fields is not None
        else _is_nonempty_ordered_subset(
            allowed_changed_fields,
            dynamic_changed_fields,
        )
        if dynamic_changed_fields is not None
        else False
    )
    if not changed_fields_valid:
        raise BusinessCommandError(
            "admission_ticket_claims_mismatch",
            "admission",
            "authoritative changed-field authority is not a closed policy contract",
        )
    fact_claims = {
        "action_id": str(ticket.get("action_id") or ""),
        "operation": str(ticket.get("operation") or ""),
        "segment_text_sha256": str(ticket.get("segment_text_sha256") or ""),
        "authority_scope": dict(authority_scope),
        "allowed_changed_fields": allowed_changed_fields,
    }
    fact_claims_sha256 = _sha256_json(fact_claims)
    if fact_claims_sha256 != str(ticket.get("fact_claims_sha256") or ""):
        raise BusinessCommandError(
            "admission_ticket_claims_mismatch",
            "admission",
            "authoritative fact claims do not match their digest",
        )
    authorized_command = {
        "domain": str(ticket.get("domain") or ""),
        "operation": str(ticket.get("operation") or ""),
        "object_ref": ticket.get("object_ref"),
        "authority_scope": dict(authority_scope),
        "allowed_changed_fields": allowed_changed_fields,
        "fact_claims_sha256": fact_claims_sha256,
    }
    if _sha256_json(authorized_command) != str(
        ticket.get("authorized_command_sha256") or ""
    ):
        raise BusinessCommandError(
            "admission_ticket_claims_mismatch",
            "admission",
            "authoritative command claims do not match their digest",
        )


def _require_execution_request(
    request: AdmissionTicketExecutionRequest,
    ticket: Mapping[str, Any],
) -> None:
    if request.receipt_kind not in {
        "business",
        "periodic_report",
        "daily_report",
    }:
        raise BusinessCommandError(
            "unknown_admission_receipt_kind",
            "admission",
            "executor receipt kind is not supported",
        )
    if request.executed_at.tzinfo is None or request.executed_at.utcoffset() is None:
        raise BusinessCommandError(
            "admission_execution_time_required",
            "admission",
            "execution time must be timezone-aware",
        )
    expected_scope = (
        request.tenant_id,
        request.user_id,
        request.conversation_id,
        request.source_message_id,
    )
    ticket_scope = tuple(
        str(ticket.get(name) or "")
        for name in (
            "tenant_id",
            "user_id",
            "conversation_id",
            "source_message_id",
        )
    )
    if ticket_scope != expected_scope:
        raise BusinessCommandError(
            "admission_ticket_scope_mismatch",
            "admission",
            "authoritative Ticket scope changed",
        )
    if request.conversation_state_version is None:
        raise BusinessCommandError(
            "admission_state_version_required",
            "admission",
            "conversation state version is required",
        )
    try:
        ticket_state_version = int(
            ticket.get("expected_conversation_state_version")
        )
    except (TypeError, ValueError) as exc:
        raise BusinessCommandError(
            "admission_ticket_state_version_conflict",
            "admission",
            "authoritative Ticket state version is invalid",
        ) from exc
    if ticket_state_version != request.conversation_state_version:
        raise BusinessCommandError(
            "admission_ticket_state_version_conflict",
            "admission",
            "conversation state changed after Ticket issue",
        )
    if (
        str(ticket.get("action_id") or "") != request.action_id
        or str(ticket.get("domain") or "") != request.domain
        or str(ticket.get("operation") or "") != request.operation
    ):
        raise BusinessCommandError(
            "admission_ticket_operation_mismatch",
            "admission",
            "authoritative Ticket does not allow this operation",
        )
    ticket_object = ticket.get("object_ref")
    if (
        not isinstance(ticket_object, Mapping)
        or _canonical_json(ticket_object) != _canonical_json(request.object_ref)
    ):
        raise BusinessCommandError(
            "admission_ticket_object_mismatch",
            "admission",
            "authoritative Ticket object changed",
        )
    issued_at = _as_datetime(ticket.get("issued_at"))
    expires_at = _as_datetime(ticket.get("expires_at"))
    executed_at = request.executed_at.astimezone(timezone.utc)
    if (
        issued_at is None
        or expires_at is None
        or executed_at < issued_at
        or executed_at >= expires_at
    ):
        raise BusinessCommandError(
            "admission_ticket_expired",
            "admission",
            "authoritative Ticket is not active at execution time",
        )


def _require_consumed_replay_scope(
    command: BusinessCommand,
    context: BusinessCommandContext,
    ticket: Mapping[str, Any],
) -> None:
    expected_scope = (
        context.tenant_id,
        context.actor_user_id,
        context.conversation_id,
        context.source_message_id,
    )
    actual_scope = tuple(
        str(ticket.get(name) or "")
        for name in (
            "tenant_id",
            "user_id",
            "conversation_id",
            "source_message_id",
        )
    )
    if actual_scope != expected_scope:
        raise BusinessCommandError(
            "admission_ticket_scope_mismatch",
            "admission",
            "consumed Ticket scope does not match duplicate ingress",
        )
    if (
        str(ticket.get("action_id") or "") != context.admission_action_id
        or str(ticket.get("operation") or "")
        != context.admission_operation
    ):
        raise BusinessCommandError(
            "admission_ticket_operation_mismatch",
            "admission",
            "consumed Ticket operation does not match duplicate command",
        )
    if not context.admission_required:
        raise BusinessCommandError(
            "admission_ticket_required",
            "admission",
            "semantic duplicate must retain its admission binding",
        )


def _require_consumed_execution_request(
    request: AdmissionTicketExecutionRequest,
    ticket: Mapping[str, Any],
) -> None:
    expected_scope = (
        request.tenant_id,
        request.user_id,
        request.conversation_id,
        request.source_message_id,
    )
    actual_scope = tuple(
        str(ticket.get(name) or "")
        for name in (
            "tenant_id",
            "user_id",
            "conversation_id",
            "source_message_id",
        )
    )
    if actual_scope != expected_scope:
        raise BusinessCommandError(
            "admission_ticket_scope_mismatch",
            "admission",
            "consumed Ticket scope does not match duplicate ingress",
        )
    if (
        str(ticket.get("action_id") or "") != request.action_id
        or str(ticket.get("domain") or "") != request.domain
        or str(ticket.get("operation") or "") != request.operation
    ):
        raise BusinessCommandError(
            "admission_ticket_operation_mismatch",
            "admission",
            "consumed Ticket operation does not match duplicate command",
        )
    object_ref = ticket.get("object_ref")
    if (
        not isinstance(object_ref, Mapping)
        or _canonical_json(object_ref) != _canonical_json(request.object_ref)
    ):
        raise BusinessCommandError(
            "admission_ticket_object_mismatch",
            "admission",
            "consumed Ticket object does not match duplicate command",
        )


def _canonical_replay_ticket(ticket: Mapping[str, Any]) -> str:
    material = dict(ticket)
    for field_name in (
        "ticket_status",
        "consumed_at",
        "consumed_receipt_ref",
        "invalidation_reason",
        "updated_at",
    ):
        material.pop(field_name, None)
    return _canonical_ticket(material)


def _require_command_claims(
    command: BusinessCommand,
    ticket: Mapping[str, Any],
) -> None:
    authority_scope = ticket.get("authority_scope")
    if not isinstance(authority_scope, Mapping):
        raise BusinessCommandError(
            "admission_ticket_claims_mismatch",
            "admission",
            "authoritative command claims are missing",
        )
    if isinstance(command, CreateCaseProgress):
        raw_fact = str(authority_scope.get("raw_fact") or "")
        attributes = authority_scope.get("attributes")
        if not isinstance(attributes, Mapping):
            attributes = {}
        expected_progress_type = str(attributes.get("stage") or "general_update")
        expected_followup_id = str(
            attributes.get("followup_notification_id") or ""
        ).strip()
        grounded_values = (
            command.details,
            command.lifecycle_stage,
            command.lifecycle_node,
            command.current_status,
            command.hearing_readiness,
            *command.next_actions,
            *command.blocking_issues,
        )
        matches = (
            command.summary == raw_fact
            and command.progress_type == expected_progress_type
            and command.followup_notification_id == expected_followup_id
            and not command.related_party_ids
            and not command.related_document_ids
            and not command.related_travel_intent_ids
            and all(not value or str(value) in raw_fact for value in grounded_values)
        )
    elif isinstance(command, CreateTravelIntent):
        matches = (
            str(authority_scope.get("destination") or "").strip()
            == command.destination_raw.strip()
            and str(authority_scope.get("travel_date") or "")
            == command.start_at.date().isoformat()
            and str(authority_scope.get("purpose") or "").strip()
            == command.purpose_summary.strip()
            and not command.related_case_ids
        )
    elif isinstance(command, SnoozeCaseFollowup):
        matches = (
            command.pending_id
            == str(authority_scope.get("pending_id") or "").strip()
            and command.case_id
            == str(authority_scope.get("case_id") or "").strip()
            and command.snoozed_until.isoformat()
            == str(authority_scope.get("snoozed_until") or "").strip()
            and bool(
                str(authority_scope.get("requested_snooze") or "").strip()
            )
        )
    elif isinstance(command, UpdateCaseProgress):
        matches = (
            str(authority_scope.get("progress_id") or "")
            == command.progress_id
            and _authority_version(authority_scope) == command.expected_version
            and _optional_text_claim_matches(
                authority_scope,
                "replacement_summary",
                command.summary,
            )
            and _optional_text_claim_matches(
                authority_scope,
                "replacement_details",
                command.details,
            )
        )
    elif isinstance(command, DeleteCaseProgress):
        matches = (
            str(authority_scope.get("progress_id") or "")
            == command.progress_id
            and _authority_version(authority_scope) == command.expected_version
            and str(authority_scope.get("delete_reason") or "")
            == command.reason
        )
    elif isinstance(command, LinkCaseProgress):
        matches = (
            str(authority_scope.get("progress_id") or "")
            == command.progress_id
            and _authority_version(authority_scope) == command.expected_version
            and all(
                _tuple_claim_matches(authority_scope, field_name, values)
                for field_name, values in (
                    ("related_party_ids", command.related_party_ids),
                    ("related_document_ids", command.related_document_ids),
                    (
                        "related_travel_intent_ids",
                        command.related_travel_intent_ids,
                    ),
                )
            )
        )
    elif isinstance(command, RespondTravelCollaboration):
        matches = (
            str(authority_scope.get("candidate_id") or "")
            == command.candidate_id
            and _authority_version(authority_scope) == command.expected_version
            and str(authority_scope.get("participant_user_id") or "")
            == str(ticket.get("user_id") or "")
            and str(authority_scope.get("response") or "") == command.response
        )
    elif isinstance(command, UpdateCaseFollowupPolicy):
        mutable_values = {
            "cadence_type": command.cadence_type,
            "custom_interval_days": command.custom_interval_days,
            "snoozed_until": command.snoozed_until,
            "enabled": command.enabled,
            "hearing_reminders_enabled": command.hearing_reminders_enabled,
            "stage_transition_enabled": command.stage_transition_enabled,
            "node_transition_enabled": command.node_transition_enabled,
        }
        matches = (
            command.tenant_id == str(ticket.get("tenant_id") or "")
            and str(authority_scope.get("case_id") or "") == command.case_id
            and str(authority_scope.get("assigned_user_id") or "")
            == command.assigned_user_id
            and _authority_policy_version(authority_scope)
            == command.expected_version
            and command.policy_source == "case_manual_override"
            and command.force_manual_override is False
            and command.business_days_only is None
            and all(
                _policy_claim_matches(authority_scope, field_name, value)
                for field_name, value in mutable_values.items()
            )
        )
    elif isinstance(command, TriggerCaseFollowupNow):
        matches = (
            command.tenant_id == str(ticket.get("tenant_id") or "")
            and str(authority_scope.get("case_id") or "") == command.case_id
            and str(authority_scope.get("assigned_user_id") or "")
            == command.assigned_user_id
            and _authority_policy_version(authority_scope)
            == command.expected_policy_version
        )
    else:
        matches = False
    actual_changed_fields = _compiled_dynamic_changed_fields(command)
    if actual_changed_fields is not None:
        matches = matches and list(ticket.get("allowed_changed_fields") or ()) == list(
            actual_changed_fields
        )
    if not matches:
        raise BusinessCommandError(
            "admission_ticket_claims_mismatch",
            "admission",
            "compiled business command exceeds authoritative Ticket claims",
        )


def _compiled_dynamic_changed_fields(
    command: BusinessCommand,
) -> tuple[str, ...] | None:
    if isinstance(command, UpdateCaseProgress):
        return tuple(
            field_name
            for field_name, value in (
                ("summary", command.summary),
                ("details", command.details),
            )
            if value is not None
        )
    if isinstance(command, LinkCaseProgress):
        return tuple(
            field_name
            for field_name, value in (
                ("related_party_ids", command.related_party_ids),
                ("related_document_ids", command.related_document_ids),
                ("related_travel_intent_ids", command.related_travel_intent_ids),
            )
            if value
        )
    if isinstance(command, UpdateCaseFollowupPolicy):
        return tuple(
            field_name
            for field_name, value in (
                ("cadence_type", command.cadence_type),
                ("custom_interval_days", command.custom_interval_days),
                ("snoozed_until", command.snoozed_until),
                ("enabled", command.enabled),
                ("hearing_reminders_enabled", command.hearing_reminders_enabled),
                ("stage_transition_enabled", command.stage_transition_enabled),
                ("node_transition_enabled", command.node_transition_enabled),
            )
            if value is not None
        )
    if isinstance(command, SnoozeCaseFollowup):
        return (
            "task_status",
            "task_response_status",
            "task_next_eligible_at",
            "task_completed_at",
            "task_version",
            "pending_status",
            "pending_consumed_at",
            "pending_version",
            "policy_snoozed_until",
            "policy_next_due_at",
            "policy_version",
        )
    return None


def _ticket_target_case_id(ticket: Mapping[str, Any]) -> str:
    object_ref = ticket.get("object_ref")
    authority_scope = ticket.get("authority_scope")
    if not isinstance(object_ref, Mapping):
        object_ref = {}
    if not isinstance(authority_scope, Mapping):
        authority_scope = {}
    object_type = str(object_ref.get("object_type") or "")
    if object_type == "case":
        return str(object_ref.get("stable_id") or "")
    if object_type in {
        "case_progress",
        "case_followup_policy",
        "case_followup_pending",
    }:
        return str(authority_scope.get("case_id") or "")
    return ""


def _canonical_ticket(ticket: Mapping[str, Any]) -> str:
    material = dict(ticket)
    material["issued_at"] = _as_iso(material.get("issued_at"))
    material["expires_at"] = _as_iso(material.get("expires_at"))
    return json.dumps(
        _json_value(material),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _authority_version(authority_scope: Mapping[str, Any]) -> int | None:
    value = authority_scope.get("version")
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _authority_policy_version(authority_scope: Mapping[str, Any]) -> int | None:
    value = authority_scope.get("policy_version")
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_text_claim_matches(
    authority_scope: Mapping[str, Any],
    name: str,
    value: str | None,
) -> bool:
    if name not in authority_scope:
        return value is None
    return value is not None and str(authority_scope.get(name) or "") == value


def _tuple_claim_matches(
    authority_scope: Mapping[str, Any],
    name: str,
    values: tuple[str, ...],
) -> bool:
    if name not in authority_scope:
        return not values
    claim = authority_scope.get(name)
    return isinstance(claim, (list, tuple)) and tuple(str(item) for item in claim) == values


def _policy_claim_matches(
    authority_scope: Mapping[str, Any],
    name: str,
    value: Any,
) -> bool:
    if name not in authority_scope:
        return value is None
    claim = authority_scope.get(name)
    if name == "snoozed_until":
        return _as_iso(claim) == _as_iso(value) and bool(_as_iso(claim))
    return claim == value


def _is_nonempty_ordered_subset(
    values: Any,
    closed_order: tuple[str, ...] | None,
) -> bool:
    if not isinstance(values, list) or not values or closed_order is None:
        return False
    if any(not isinstance(value, str) for value in values):
        return False
    return values == [field for field in closed_order if field in set(values)]


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _sha256_json(value: Any) -> str:
    material = json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _as_iso(value: Any) -> str:
    parsed = _as_datetime(value)
    return parsed.isoformat() if parsed is not None else ""


def _parse_uuid(value: Any) -> UUID:
    try:
        return UUID(str(value or ""))
    except ValueError as exc:
        raise BusinessCommandError(
            "admission_ticket_not_found",
            "admission",
            "admission ticket identifier is invalid",
        ) from exc


def _parse_object_uuid(value: Any) -> UUID:
    try:
        return UUID(str(value or ""))
    except ValueError as exc:
        raise BusinessCommandError(
            "admission_ticket_object_mismatch",
            "admission",
            "authoritative object identifier is invalid",
        ) from exc


def _require_object_ref(
    ticket: Mapping[str, Any],
    expected_object_type: str,
) -> Mapping[str, Any]:
    object_ref = ticket.get("object_ref")
    if (
        not isinstance(object_ref, Mapping)
        or str(object_ref.get("object_type") or "") != expected_object_type
    ):
        raise BusinessCommandError(
            "admission_ticket_object_mismatch",
            "admission",
            "authoritative Ticket object type changed",
        )
    return object_ref


def _required_version(object_ref: Mapping[str, Any]) -> int:
    value = object_ref.get("version")
    if isinstance(value, bool):
        raise BusinessCommandError(
            "admission_ticket_object_version_conflict",
            "admission",
            "authoritative object version is invalid",
        )
    try:
        version = int(value)
    except (TypeError, ValueError) as exc:
        raise BusinessCommandError(
            "admission_ticket_object_version_conflict",
            "admission",
            "authoritative object version is invalid",
        ) from exc
    if version < 0:
        raise BusinessCommandError(
            "admission_ticket_object_version_conflict",
            "admission",
            "authoritative object version is invalid",
        )
    return version


def _parse_receipt_uuid(value: Any) -> UUID:
    try:
        return UUID(str(value or ""))
    except ValueError as exc:
        raise BusinessCommandError(
            "admission_ticket_receipt_not_successful",
            "admission",
            "business receipt identifier is invalid",
        ) from exc


__all__ = [
    "AdmissionReceiptReference",
    "AdmissionTicketExecutionRequest",
    "SqlAdmissionTicketLease",
    "SqlAdmissionTicketStore",
]
