from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timezone
import hashlib
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.agent2.admission_contracts import (
    AdmissionExecutionScope,
    validate_mutation_execution_authority,
)
from app.agent2.admission_store_sql import (
    AdmissionReceiptReference,
    AdmissionTicketExecutionRequest,
    SqlAdmissionTicketStore,
)
from app.agent2.business.contracts import BusinessCommandContext, BusinessCommandError
from app.agent2.business.models import PeriodicReport, PeriodicReportCommandReceipt
from app.agent2.case_followup_task_ledger_sql import sync_focused_report_task
from app.agent2.report_domain import (
    PeriodicReportExecution,
    PeriodicReportSnapshot,
    TypedPeriodicReportCommand,
    execute_periodic_report_command,
    period_bounds,
)


@dataclass(frozen=True)
class PersistedPeriodicReportExecution:
    execution: PeriodicReportExecution
    receipt_id: str
    status: str
    actual_write: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "typed_command": self.execution.command.as_dict(),
            "validation_status": self.status,
            "reason_code": self.execution.reason_code,
            "receipt_id": self.receipt_id,
            "actual_write": self.actual_write,
            "report": snapshot_payload(self.execution.after),
        }


async def load_periodic_report_snapshot(
    session: Any,
    *,
    context: BusinessCommandContext,
    report_type: str,
    anchor: date,
) -> PeriodicReportSnapshot:
    period_key, _, _ = period_bounds(report_type, anchor)
    row = await session.scalar(
        select(PeriodicReport).where(
            PeriodicReport.tenant_id == context.tenant_id,
            PeriodicReport.owner_user_id == context.actor_user_id,
            PeriodicReport.report_type == report_type,
            PeriodicReport.period_key == period_key,
        )
    )
    if row is None:
        return PeriodicReportSnapshot(
            report_id=periodic_report_id(
                context.tenant_id,
                context.actor_user_id,
                report_type,
                period_key,
            ),
            owner_user_id=UUID(context.actor_user_id),
            report_type=report_type,
            period_key=period_key,
            version=0,
            status="collecting",
        )
    return snapshot_from_row(row)


async def execute_periodic_report_commands(
    session: Any,
    *,
    commands: tuple[TypedPeriodicReportCommand, ...],
    context: BusinessCommandContext,
    execution_authority: str,
    timezone_name: str = "Asia/Shanghai",
    admission_ticket_store: Any | None = None,
) -> list[PersistedPeriodicReportExecution]:
    results: list[PersistedPeriodicReportExecution] = []
    actor = UUID(context.actor_user_id)
    ticket_store = admission_ticket_store or SqlAdmissionTicketStore(session)
    authority = validate_mutation_execution_authority(execution_authority)
    mutation_commands = tuple(
        command for command in commands if command.command_type != "query_report"
    )
    if authority == "semantic_ticket" and any(
        not command.admission_required for command in mutation_commands
    ):
        raise BusinessCommandError(
            "admission_ticket_required",
            "admission",
            "semantic report mutation requires authoritative admission",
        )
    if authority != "semantic_ticket" and any(
        command.admission_required for command in commands
    ):
        raise BusinessCommandError(
            "execution_authority_mismatch",
            "admission",
            "non-semantic authority cannot consume Admission Tickets",
        )
    for command in commands:
        receipt_idempotency_key = _periodic_receipt_idempotency_key(
            command,
            context,
        )
        expected_period_key, period_start_at, period_end_at = periodic_report_datetimes(
            command.report_type,
            context.occurred_at,
            timezone_name,
        )
        expected_report_id = periodic_report_id(
            context.tenant_id,
            context.actor_user_id,
            command.report_type,
            expected_period_key,
        )
        if command.period_key != expected_period_key or command.report_id != expected_report_id:
            raise ValueError("periodic report command violates server-derived period identity")
        # The report row may not exist yet. A transaction-scoped deterministic
        # lock serializes first creation as well as later versioned mutations.
        await session.execute(
            select(func.pg_advisory_xact_lock(_report_lock_key(command.report_id)))
        )
        existing_receipt = await session.scalar(
            select(PeriodicReportCommandReceipt).where(
                PeriodicReportCommandReceipt.tenant_id == context.tenant_id,
                PeriodicReportCommandReceipt.idempotency_key.in_(
                    (receipt_idempotency_key, command.idempotency_key)
                ),
            ).order_by(
                (
                    PeriodicReportCommandReceipt.idempotency_key
                    == receipt_idempotency_key
                ).desc()
            )
        )
        row = await session.scalar(
            select(PeriodicReport)
            .where(
                PeriodicReport.tenant_id == context.tenant_id,
                PeriodicReport.report_id == command.report_id,
            )
            .with_for_update()
        )
        snapshot = snapshot_from_row(row) if row is not None else PeriodicReportSnapshot(
            report_id=command.report_id,
            owner_user_id=actor,
            report_type=command.report_type,
            period_key=command.period_key,
            version=0,
            status="collecting",
        )
        execution = execute_periodic_report_command(
            command,
            snapshot=snapshot,
            actor_user_id=actor,
            executed_idempotency_keys=(
                {command.idempotency_key} if existing_receipt is not None else set()
            ),
            admission_scope=_periodic_admission_scope(command=command, context=context),
        )
        if existing_receipt is not None:
            replay_status, replay_reason = _periodic_replay_status(
                existing_receipt,
                command=command,
                context=context,
            )
            if (
                replay_status == "duplicate"
                and authority == "semantic_ticket"
                and command.command_type != "query_report"
            ):
                try:
                    await ticket_store.validate_consumed_execution_replay(
                        _periodic_ticket_request(command, context),
                        receipt_id=str(existing_receipt.receipt_id),
                    )
                except BusinessCommandError as exc:
                    replay_status, replay_reason = "blocked", exc.code
            if replay_status != "duplicate":
                blocked_execution = replace(
                    execution,
                    validation_status="blocked",
                    reason_code=replay_reason,
                    after=snapshot,
                    changed=False,
                    should_write_db=False,
                )
                results.append(
                    PersistedPeriodicReportExecution(
                        execution=blocked_execution,
                        receipt_id=str(existing_receipt.receipt_id),
                        status="blocked",
                        actual_write=False,
                    )
                )
                continue
            if context.conversation_id:
                transition = await sync_focused_report_task(
                    session, task_id=command.report_id,
                    tenant_id=context.tenant_id, user_id=context.actor_user_id,
                    conversation_id=context.conversation_id,
                    source_turn_id=context.source_message_id,
                    report_type=command.report_type, period_key=command.period_key,
                    report_status=execution.after.status, now=context.occurred_at,
                    expires_at=period_end_at,
                )
                if transition.status == "blocked":
                    raise RuntimeError(
                        f"report task ledger blocked: {transition.reason_code}"
                    )
            results.append(
                PersistedPeriodicReportExecution(
                    execution=execution,
                    receipt_id=str(existing_receipt.receipt_id),
                    status="duplicate",
                    actual_write=False,
                )
            )
            continue
        ticket_lease = None
        if command.admission_required and execution.validation_status == "authorized":
            ticket_lease = await ticket_store.acquire(
                _periodic_ticket_request(command, context)
            )
        if execution.should_write_db:
            if row is None:
                row = PeriodicReport(
                    report_id=command.report_id,
                    tenant_id=context.tenant_id,
                    company_id=context.company_id,
                    department_id=context.department_id,
                    team_id=context.team_id,
                    owner_user_id=context.actor_user_id,
                    report_type=command.report_type,
                    period_key=command.period_key,
                    period_start=period_start_at,
                    period_end=period_end_at,
                    source_channel=context.source_channel,
                )
                session.add(row)
            apply_snapshot_to_row(row, execution.after)
            if execution.after.status == "completed":
                row.submitted_at = context.occurred_at
        receipt = PeriodicReportCommandReceipt(
            tenant_id=context.tenant_id,
            actor_user_id=context.actor_user_id,
            source_message_id=context.source_message_id,
            source_channel=context.source_channel,
            command_id=str(command.command_id),
            command_type=command.command_type,
            idempotency_key=receipt_idempotency_key,
            report_id=command.report_id,
            status=execution.validation_status,
            actual_write=execution.should_write_db,
            before_json=snapshot_payload(execution.before),
            after_json=snapshot_payload(execution.after),
            error_code=(execution.reason_code if execution.validation_status == "blocked" else ""),
        )
        session.add(receipt)
        await session.flush()
        if ticket_lease is not None:
            await ticket_store.consume(
                ticket_lease,
                receipt=AdmissionReceiptReference(
                    receipt_kind="periodic_report",
                    receipt_id=str(receipt.receipt_id),
                    status=execution.validation_status,
                    actual_write=execution.should_write_db,
                ),
                consumed_at=(
                    context.execution_started_at or context.occurred_at
                ),
            )
        if context.conversation_id and execution.validation_status == "authorized":
            transition = await sync_focused_report_task(
                session, task_id=command.report_id,
                tenant_id=context.tenant_id, user_id=context.actor_user_id,
                conversation_id=context.conversation_id,
                source_turn_id=context.source_message_id,
                report_type=command.report_type, period_key=command.period_key,
                report_status=execution.after.status, now=context.occurred_at,
                expires_at=period_end_at,
            )
            if transition.status == "blocked":
                raise RuntimeError(
                    f"report task ledger blocked: {transition.reason_code}"
                )
        results.append(
            PersistedPeriodicReportExecution(
                execution=execution,
                receipt_id=str(receipt.receipt_id),
                status=execution.validation_status,
                actual_write=execution.should_write_db,
            )
        )
    return results


def _periodic_admission_scope(
    *,
    command: TypedPeriodicReportCommand,
    context: BusinessCommandContext,
) -> AdmissionExecutionScope | None:
    if not command.admission_required:
        return None
    return AdmissionExecutionScope(
        tenant_id=context.tenant_id,
        user_id=context.actor_user_id,
        conversation_id=context.conversation_id,
        source_message_id=context.source_message_id,
        executed_at=context.execution_started_at or datetime.now(timezone.utc),
        conversation_state_version=context.conversation_state_version,
    )


def _periodic_ticket_request(
    command: TypedPeriodicReportCommand,
    context: BusinessCommandContext,
) -> AdmissionTicketExecutionRequest:
    return AdmissionTicketExecutionRequest(
        admission_ticket=command.admission_ticket,
        tenant_id=context.tenant_id,
        user_id=context.actor_user_id,
        conversation_id=context.conversation_id,
        source_message_id=context.source_message_id,
        action_id=command.admission_action_id,
        domain="report",
        operation=command.admission_operation,
        object_ref={
            "object_type": "periodic_report",
            "stable_id": str(command.report_id),
            "version": command.report_version,
        },
        conversation_state_version=context.conversation_state_version,
        executed_at=context.execution_started_at or datetime.now(timezone.utc),
        command_type=command.command_type,
        receipt_kind="periodic_report",
        company_id=context.company_id,
        department_id=context.department_id,
        team_id=context.team_id,
        actor_role_ids=tuple(context.actor_role_ids),
        allowed_case_ids=tuple(context.allowed_case_ids),
    )


def _periodic_receipt_idempotency_key(
    command: TypedPeriodicReportCommand,
    context: BusinessCommandContext,
) -> str:
    material = "\x1f".join(
        (
            context.tenant_id,
            context.actor_user_id,
            context.conversation_id,
            context.source_channel,
            context.source_message_id,
            str(command.report_id),
            command.command_type,
            command.idempotency_key,
        )
    )
    return f"agent2-periodic-v2:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def _periodic_replay_status(
    receipt: PeriodicReportCommandReceipt,
    *,
    command: TypedPeriodicReportCommand,
    context: BusinessCommandContext,
) -> tuple[str, str]:
    if (
        str(receipt.tenant_id) != context.tenant_id
        or str(receipt.actor_user_id) != context.actor_user_id
        or str(receipt.source_message_id) != context.source_message_id
        or str(receipt.command_type) != command.command_type
        or str(receipt.report_id) != str(command.report_id)
    ):
        return "blocked", "idempotency_scope_conflict"
    status = str(receipt.status or "")
    if status in {"authorized", "executed", "duplicate"}:
        return "duplicate", ""
    if status in {"blocked", "failed"}:
        return "blocked", str(receipt.error_code or "prior_failure")
    return "blocked", "idempotency_in_progress"


def periodic_report_id(tenant_id: str, user_id: str, report_type: str, period_key: str) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"agent2-periodic-report:{tenant_id}:{user_id}:{report_type}:{period_key}",
    )


def _report_lock_key(report_id: UUID) -> int:
    return int.from_bytes(report_id.bytes[:8], byteorder="big", signed=True)


def periodic_report_datetimes(
    report_type: str,
    occurred_at: datetime,
    timezone_name: str,
) -> tuple[str, datetime, datetime]:
    local_zone = ZoneInfo(timezone_name)
    local_date = occurred_at.astimezone(local_zone).date()
    period_key, start, end = period_bounds(report_type, local_date)
    return (
        period_key,
        datetime.combine(start, time.min, tzinfo=local_zone).astimezone(timezone.utc),
        datetime.combine(end, time.max, tzinfo=local_zone).astimezone(timezone.utc),
    )


def snapshot_from_row(row: PeriodicReport) -> PeriodicReportSnapshot:
    return PeriodicReportSnapshot(
        report_id=row.report_id,
        owner_user_id=UUID(row.owner_user_id),
        report_type=row.report_type,
        period_key=row.period_key,
        version=row.version,
        status=row.status,
        sections={key: tuple(value) for key, value in (row.sections_json or {}).items()},
        item_ids={key: tuple(value) for key, value in (row.item_ids_json or {}).items()},
    )


def apply_snapshot_to_row(row: PeriodicReport, snapshot: PeriodicReportSnapshot) -> None:
    row.sections_json = {key: list(value) for key, value in snapshot.sections.items()}
    row.item_ids_json = {key: list(value) for key, value in snapshot.item_ids.items()}
    row.status = snapshot.status
    row.version = snapshot.version


def snapshot_payload(snapshot: PeriodicReportSnapshot) -> dict[str, Any]:
    return {
        "report_id": str(snapshot.report_id),
        "owner_user_id": str(snapshot.owner_user_id),
        "report_type": snapshot.report_type,
        "period_key": snapshot.period_key,
        "version": snapshot.version,
        "status": snapshot.status,
        "sections": {key: list(value) for key, value in snapshot.sections.items()},
        "item_ids": {key: list(value) for key, value in snapshot.item_ids.items()},
    }


def render_periodic_report(snapshot: PeriodicReportSnapshot) -> str:
    label = "周报" if snapshot.report_type == "weekly" else "月报"
    field_labels = {
        "accomplishments": "完成事项",
        "metrics": "关键指标",
        "risks": "风险问题",
        "next_plan": "后续计划",
    }
    lines = [f"当前{label}（{snapshot.period_key}，{snapshot.status}，v{snapshot.version}）"]
    for field_name in ("accomplishments", "metrics", "risks", "next_plan"):
        values = snapshot.sections.get(field_name, ())
        lines.append(f"{field_labels[field_name]}：")
        lines.extend(f"{index}. {value}" for index, value in enumerate(values, start=1))
        if not values:
            lines.append("（暂无）")
    return "\n".join(lines)
