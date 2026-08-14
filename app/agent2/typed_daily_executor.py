from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
import hashlib
from typing import TYPE_CHECKING, Sequence
import uuid
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.agent2.daily_execution import Agent2DailyExecutionResult
from app.agent2.case_followup_task_ledger_sql import sync_focused_report_task
from app.agent2.daily_state import DRAFT_ITEM_IDS_KEY, REPORT_FIELD_ORDER
from app.agent2.typed_daily_commands import (
    DailyReportMutationSnapshot,
    TypedDailyCommand,
    TypedDailyCommandExecution,
    execute_typed_daily_command,
)
from app.agent2.admission_contracts import AdmissionExecutionScope
from app.agent2.admission_contracts import validate_mutation_execution_authority
from app.agent2.admission_store_sql import (
    AdmissionReceiptReference,
    AdmissionTicketExecutionRequest,
    SqlAdmissionTicketStore,
)
from app.agent2.business.contracts import BusinessCommandError
from app.services.state_machine import (
    STATUS_COLLECTING,
    STATUS_COMPLETED,
    STATUS_PENDING_CONFIRMATION,
    assess_daily_report_completeness,
)


TYPED_REPORT_VERSION_KEY = "_agent2_report_version"
TYPED_COMMAND_KEYS_KEY = "_agent2_typed_command_keys"
TYPED_AUDIT_KEY = "_agent2_typed_audit"
EMPTY_ACK_KEYS = {
    field_name: f"{field_name}_acknowledged_empty"
    for field_name in REPORT_FIELD_ORDER
}

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models import DailyReport, User


@dataclass(frozen=True)
class TypedDailyExecutionContext:
    report_date: date
    source: str
    source_text_hash: str
    tenant_id: str = "agent2-daily"
    conversation_id: str = ""
    source_turn_id: str = ""
    occurred_at: datetime | None = None
    execution_started_at: datetime | None = None
    conversation_state_version: int | None = None
    company_id: str = ""
    department_id: str = ""
    team_id: str = ""
    actor_role_ids: tuple[str, ...] = ()
    allowed_case_ids: tuple[str, ...] = ()
    runtime_label: str = "agent2_cognitive_core_v3"
    contract_version: str = "cognitive_core.v3"
    allow_completed_append: bool = False
    allow_completed_content_mutation: bool = False

    def __post_init__(self) -> None:
        if (
            not self.source
            or len(self.source_text_hash) != 64
            or not self.tenant_id.strip()
            or not self.runtime_label.strip()
            or not self.contract_version.strip()
        ):
            raise ValueError("typed execution context requires source and SHA-256 source hash")
        if self.execution_started_at is not None and self.execution_started_at.tzinfo is None:
            raise ValueError("typed execution start must be timezone-aware")


def _daily_admission_scope(
    *,
    command: TypedDailyCommand,
    user_id: str,
    context: TypedDailyExecutionContext,
) -> AdmissionExecutionScope | None:
    if not command.admission_required:
        return None
    return AdmissionExecutionScope(
        tenant_id=context.tenant_id,
        user_id=user_id,
        conversation_id=context.conversation_id,
        source_message_id=context.source_turn_id,
        executed_at=context.execution_started_at or datetime.now(UTC),
        conversation_state_version=context.conversation_state_version,
    )


async def execute_typed_agent2_daily_commands(
    session: "AsyncSession",
    *,
    user: "User",
    commands: Sequence[TypedDailyCommand],
    execution_context: TypedDailyExecutionContext,
    settings: object,
    execution_authority: str,
    admission_ticket_store: object | None = None,
) -> Agent2DailyExecutionResult:
    """Execute already-planned typed commands; natural language is not part of this interface."""

    from app.repositories import acquire_daily_report_advisory_lock, get_report, upsert_daily_report

    if not commands:
        raise ValueError("typed daily executor requires at least one command")
    ticket_store = admission_ticket_store or SqlAdmissionTicketStore(session)
    authority = validate_mutation_execution_authority(execution_authority)
    _validate_executor_command_shapes(commands)
    _require_daily_execution_authority(commands, authority)
    query_commands = tuple(command for command in commands if command.command_type == "query_report")
    if len(query_commands) > 1:
        raise ValueError("one turn may query only one daily report")
    if query_commands and len(commands) > 1:
        mutation_commands = tuple(command for command in commands if command.command_type != "query_report")
        query_result = await execute_typed_agent2_daily_commands(
            session,
            user=user,
            commands=query_commands,
            execution_context=execution_context,
            settings=settings,
            admission_ticket_store=ticket_store,
            execution_authority=authority,
        )
        mutation_result = await execute_typed_agent2_daily_commands(
            session,
            user=user,
            commands=mutation_commands,
            execution_context=execution_context,
            settings=settings,
            admission_ticket_store=ticket_store,
            execution_authority=authority,
        )
        return Agent2DailyExecutionResult(
            report_id=mutation_result.report_id,
            report_date=mutation_result.report_date,
            status=mutation_result.status,
            message=f"{query_result.message}\n\n{mutation_result.message}",
            report_saved=mutation_result.report_saved,
            read_only=False,
            today_work=mutation_result.today_work,
            problems=mutation_result.problems,
            tomorrow_plan=mutation_result.tomorrow_plan,
            command_results=[*query_result.command_results, *mutation_result.command_results],
        )
    prior_receipts = await _load_prior_execution_receipts(
        session,
        tenant_id=execution_context.tenant_id,
        commands=commands,
    )
    successful_receipt_keys = {
        key
        for key, receipt in prior_receipts.items()
        if receipt.status in {"executed", "duplicate"}
    }
    if query_commands:
        command = query_commands[0]
        report_date = _query_report_date(command, execution_context.report_date)
        existing = await get_report(session, user.id, report_date)
        snapshot = build_typed_daily_snapshot(
            user=user,
            report_date=report_date,
            report=existing,
        )
        prior_receipt = prior_receipts.get(command.idempotency_key)
        execution = (
            _execution_from_prior_receipt(
                command,
                snapshot,
                prior_receipt,
                context=execution_context,
                require_execution_scope=authority == "semantic_ticket",
            )
            if prior_receipt is not None
            else execute_typed_daily_command(
                command,
                snapshot=snapshot,
                actor_user_id=user.id,
                executed_idempotency_keys=successful_receipt_keys,
                allow_completed_append=execution_context.allow_completed_append,
                allow_completed_content_mutation=(
                    execution_context.allow_completed_content_mutation
                ),
                admission_scope=_daily_admission_scope(
                    command=command,
                    user_id=str(user.id),
                    context=execution_context,
                ),
            )
        )
        ticket_lease = None
        if (
            prior_receipt is None
            and command.admission_required
            and execution.validation.status == "authorized"
        ):
            try:
                ticket_lease = await ticket_store.acquire(
                    _daily_ticket_execution_request(
                        command=command,
                        user_id=str(user.id),
                        context=execution_context,
                    )
                )
            except BusinessCommandError as exc:
                execution = _authority_blocked_execution(
                    command,
                    snapshot,
                    exc.code,
                )
        if execution.validation.status == "blocked":
            await _persist_execution_receipts(
                session,
                user=user,
                report_date=report_date,
                report_id=getattr(existing, "id", None),
                executions=(execution,),
                context=execution_context,
            )
            return _blocked_result(
                existing,
                report_date,
                execution,
                tenant_id=execution_context.tenant_id,
            )
        await _persist_execution_receipts(
            session,
            user=user,
            report_date=report_date,
            report_id=getattr(existing, "id", None),
            executions=(execution,),
            context=execution_context,
        )
        if ticket_lease is not None:
            await session.flush()
            await ticket_store.consume(
                ticket_lease,
                receipt=AdmissionReceiptReference(
                    receipt_kind="daily_report",
                    receipt_id=_daily_receipt_id(
                        command.idempotency_key,
                        tenant_id=execution_context.tenant_id,
                    ),
                    status=_receipt_status(execution),
                    actual_write=execution.should_write_db,
                ),
                consumed_at=_daily_execution_time(execution_context),
            )
        if execution_context.conversation_id and execution.validation.status == "authorized":
            transition = await sync_focused_report_task(
                session, task_id=snapshot.report_id,
                tenant_id=execution_context.tenant_id, user_id=str(user.id),
                conversation_id=execution_context.conversation_id,
                source_turn_id=(execution_context.source_turn_id or str(command.command_id)),
                report_type="daily", period_key=report_date.isoformat(),
                report_status=snapshot.status,
                now=(execution_context.occurred_at or datetime.now(ZoneInfo("UTC"))),
                expires_at=datetime.combine(
                    report_date + timedelta(days=1), time.min,
                    ZoneInfo(getattr(user, "timezone", None) or getattr(settings, "timezone", "Asia/Shanghai")),
                ),
            )
            if transition.status == "blocked":
                raise RuntimeError(
                    f"daily report task ledger blocked: {transition.reason_code}"
                )
        return Agent2DailyExecutionResult(
            report_id=str(snapshot.report_id),
            report_date=report_date,
            status=snapshot.status,
            message=_query_result_message(report_date, snapshot),
            report_saved=False,
            read_only=True,
            today_work=list(snapshot.today_work),
            problems=list(snapshot.problems),
            tomorrow_plan=list(snapshot.tomorrow_plan),
            command_results=[
                _execution_result(execution, tenant_id=execution_context.tenant_id)
            ],
        )
    report_date = _mutation_report_date(commands, execution_context.report_date)
    await acquire_daily_report_advisory_lock(session, user.id, report_date)
    # Another worker may have completed the same idempotency key while this
    # transaction waited for the report lock. Reload inside the serialized
    # section so duplicate replay wins before any Ticket reacquisition.
    prior_receipts = await _load_prior_execution_receipts(
        session,
        tenant_id=execution_context.tenant_id,
        commands=commands,
    )
    successful_receipt_keys = {
        key
        for key, receipt in prior_receipts.items()
        if receipt.status in {"executed", "duplicate"}
    }
    existing = await get_report(session, user.id, report_date)
    before = build_typed_daily_snapshot(user=user, report_date=report_date, report=existing)
    known_key_order = list(dict.fromkeys(_typed_command_keys(getattr(existing, "section_status", None))))
    known_keys = set(known_key_order)
    for key in successful_receipt_keys:
        if key not in known_keys:
            known_key_order.append(key)
            known_keys.add(key)
    working = before
    executions: list[TypedDailyCommandExecution] = []
    ticket_leases: list[tuple[TypedDailyCommandExecution, object]] = []
    for command in commands:
        prior_receipt = prior_receipts.get(command.idempotency_key)
        if (
            prior_receipt is not None
            and authority == "semantic_ticket"
            and command.admission_required
            and str(getattr(prior_receipt, "status", "") or "")
            in {"executed", "duplicate"}
        ):
            try:
                await ticket_store.validate_consumed_execution_replay(
                    _daily_ticket_execution_request(
                        command=command,
                        user_id=str(user.id),
                        context=execution_context,
                    ),
                    receipt_id=str(getattr(prior_receipt, "receipt_id", "") or ""),
                )
            except BusinessCommandError as exc:
                execution = _authority_blocked_execution(
                    command,
                    working,
                    exc.code,
                )
            else:
                execution = _execution_from_prior_receipt(
                    command,
                    working,
                    prior_receipt,
                    context=execution_context,
                    require_execution_scope=True,
                )
        elif prior_receipt is not None:
            execution = _execution_from_prior_receipt(
                command,
                working,
                prior_receipt,
                context=execution_context,
                require_execution_scope=authority == "semantic_ticket",
            )
        else:
            execution = execute_typed_daily_command(
                command,
                snapshot=working,
                actor_user_id=user.id,
                executed_idempotency_keys=known_keys,
                allow_completed_append=execution_context.allow_completed_append,
                allow_completed_content_mutation=(
                    execution_context.allow_completed_content_mutation
                ),
                admission_scope=_daily_admission_scope(
                    command=command,
                    user_id=str(user.id),
                    context=execution_context,
                ),
            )
        ticket_lease = None
        if (
            prior_receipt is None
            and command.admission_required
            and execution.validation.status == "authorized"
        ):
            try:
                ticket_lease = await ticket_store.acquire(
                    _daily_ticket_execution_request(
                        command=command,
                        user_id=str(user.id),
                        context=execution_context,
                    )
                )
            except BusinessCommandError as exc:
                execution = _authority_blocked_execution(
                    command,
                    working,
                    exc.code,
                )
        execution = _with_projected_draft_status(execution)
        executions.append(execution)
        if execution.validation.status == "blocked":
            await _persist_execution_receipts(
                session,
                user=user,
                report_date=report_date,
                report_id=getattr(existing, "id", None),
                executions=(execution,),
                context=execution_context,
            )
            return _blocked_result(
                existing,
                report_date,
                execution,
                tenant_id=execution_context.tenant_id,
            )
        if ticket_lease is not None:
            ticket_leases.append((execution, ticket_lease))
        working = execution.after
        if command.idempotency_key not in known_keys:
            known_key_order.append(command.idempotency_key)
            known_keys.add(command.idempotency_key)

    if all(execution.validation.status == "duplicate" for execution in executions):
        await _persist_execution_receipts(
            session,
            user=user,
            report_date=report_date,
            report_id=getattr(existing, "id", None),
            executions=tuple(executions),
            context=execution_context,
        )
        return Agent2DailyExecutionResult(
            report_id=str(getattr(existing, "id", "") or working.report_id),
            report_date=report_date,
            status=working.status,
            message="日报命令已幂等处理，未重复写入。",
            report_saved=False,
            read_only=False,
            today_work=list(working.today_work),
            problems=list(working.problems),
            tomorrow_plan=list(working.tomorrow_plan),
            command_results=[
                _execution_result(item, tenant_id=execution_context.tenant_id)
                for item in executions
            ],
        )

    if not any(execution.should_write_db for execution in executions):
        await _persist_execution_receipts(
            session,
            user=user,
            report_date=report_date,
            report_id=getattr(existing, "id", None),
            executions=tuple(executions),
            context=execution_context,
        )
        if ticket_leases:
            await session.flush()
            for execution, ticket_lease in ticket_leases:
                await ticket_store.consume(
                    ticket_lease,
                    receipt=AdmissionReceiptReference(
                        receipt_kind="daily_report",
                        receipt_id=_daily_receipt_id(
                            execution.command.idempotency_key,
                            tenant_id=execution_context.tenant_id,
                        ),
                        status=_receipt_status(execution),
                        actual_write=False,
                    ),
                    consumed_at=_daily_execution_time(execution_context),
                )
        return Agent2DailyExecutionResult(
            report_id=str(getattr(existing, "id", "") or working.report_id),
            report_date=report_date,
            status=working.status,
            message="日报内容已在对应栏目中，本次没有修改。\n\n"
            + _query_result_message(report_date, working),
            report_saved=False,
            read_only=False,
            today_work=list(working.today_work),
            problems=list(working.problems),
            tomorrow_plan=list(working.tomorrow_plan),
            command_results=[
                _execution_result(item, tenant_id=execution_context.tenant_id)
                for item in executions
            ],
        )

    local_timezone = ZoneInfo(
        getattr(user, "timezone", None)
        or getattr(settings, "timezone", "Asia/Shanghai")
    )
    received_at = (
        execution_context.occurred_at.astimezone(local_timezone)
        if execution_context.occurred_at is not None
        else datetime.now(local_timezone)
    )
    completeness = assess_daily_report_completeness(
        today_work=working.today_work,
        problems=working.problems,
        tomorrow_plan=working.tomorrow_plan,
        acknowledged_empty_fields=working.acknowledged_empty_fields,
    )
    section_status = dict(getattr(existing, "section_status", None) or {})
    section_status[TYPED_REPORT_VERSION_KEY] = working.version
    section_status[TYPED_COMMAND_KEYS_KEY] = known_key_order[-100:]
    existing_audits = _typed_audit_records(getattr(existing, "section_status", None))
    section_status[TYPED_AUDIT_KEY] = [
        *existing_audits,
        *(execution.audit.as_dict() for execution in executions),
    ][-100:]
    section_status[DRAFT_ITEM_IDS_KEY] = {
        field_name: list(working.item_ids.get(field_name, ()))
        for field_name in REPORT_FIELD_ORDER
    }
    for field_name, status_key in EMPTY_ACK_KEYS.items():
        if field_name in working.acknowledged_empty_fields:
            section_status[status_key] = True
        else:
            section_status.pop(status_key, None)
    preserve_existing_submission = (
        existing is not None
        and before.status == STATUS_COMPLETED
        and working.status == STATUS_COMPLETED
        and not any(
            command.command_type in {"reopen_report", "submit_report"}
            for command in commands
        )
    )
    confirmation_type = (
        str(getattr(existing, "confirmation_type", "") or "user_confirmed")
        if preserve_existing_submission
        else ("user_confirmed" if working.status == STATUS_COMPLETED else "none")
    )
    confirmed_by_user = (
        bool(getattr(existing, "confirmed_by_user", True))
        if preserve_existing_submission
        else working.status == STATUS_COMPLETED
    )
    if preserve_existing_submission:
        pending_confirmation_at = getattr(
            existing,
            "pending_confirmation_at",
            None,
        )
    elif working.status == STATUS_PENDING_CONFIRMATION:
        pending_confirmation_at = (
            getattr(existing, "pending_confirmation_at", None) or received_at
        )
    else:
        pending_confirmation_at = None
    auto_submit_at = (
        getattr(existing, "auto_submit_at", None)
        if preserve_existing_submission
        else None
    )
    report = await upsert_daily_report(
        session,
        user=user,
        report_date=report_date,
        raw_input="",
        source=execution_context.source,
        today_work=list(working.today_work),
        problems=list(working.problems),
        tomorrow_plan=list(working.tomorrow_plan),
        emotion="",
        completeness_score=completeness.completeness_score,
        status=working.status,
        section_status=section_status,
        llm_model=execution_context.runtime_label,
        llm_payload={
            "agent2": True,
            "contract_version": execution_context.contract_version,
            "source_text_hash": execution_context.source_text_hash,
            "typed_commands": [command.as_dict() for command in commands],
            "typed_audit": [execution.audit.as_dict() for execution in executions],
        },
        received_at=received_at,
        confirmation_type=confirmation_type,
        confirmed_by_user=confirmed_by_user,
        quality_warning=None,
        last_modified_by_user=True,
        last_modified_at=received_at,
        pending_confirmation_at=pending_confirmation_at,
        auto_submit_at=auto_submit_at,
        replace_sections=True,
        report_id_override=working.report_id if existing is None else None,
        preserve_existing_submission=preserve_existing_submission,
    )
    await _persist_execution_receipts(
        session,
        user=user,
        report_date=report_date,
        report_id=report.id,
        executions=tuple(executions),
        context=execution_context,
    )
    # The domain effect, its receipt, and Ticket consumption share the caller's
    # transaction.  Flush first so the authoritative store can prove the exact
    # successful receipt before changing Ticket state.
    if ticket_leases:
        await session.flush()
        for execution, ticket_lease in ticket_leases:
            await ticket_store.consume(
                ticket_lease,
                receipt=AdmissionReceiptReference(
                    receipt_kind="daily_report",
                    receipt_id=_daily_receipt_id(
                        execution.command.idempotency_key,
                        tenant_id=execution_context.tenant_id,
                    ),
                    status=_receipt_status(execution),
                    actual_write=execution.should_write_db,
                ),
                consumed_at=_daily_execution_time(execution_context),
            )
    if execution_context.conversation_id:
        transition = await sync_focused_report_task(
            session, task_id=working.report_id,
            tenant_id=execution_context.tenant_id, user_id=str(user.id),
            conversation_id=execution_context.conversation_id,
            source_turn_id=(
                execution_context.source_turn_id or str(commands[0].command_id)
            ),
            report_type="daily", period_key=report_date.isoformat(),
            report_status=working.status,
            now=(execution_context.occurred_at or received_at),
            expires_at=datetime.combine(
                report_date + timedelta(days=1), time.min,
                ZoneInfo(getattr(user, "timezone", None) or getattr(settings, "timezone", "Asia/Shanghai")),
            ),
        )
        if transition.status == "blocked":
            raise RuntimeError(
                f"daily report task ledger blocked: {transition.reason_code}"
            )
    return Agent2DailyExecutionResult(
        report_id=str(report.id),
        report_date=report_date,
        status=working.status,
        message=_mutation_result_lead(
            status=working.status,
            report_date=report_date,
            occurred_at=received_at,
            settings=settings,
        )
        + "\n\n"
        + _query_result_message(report_date, working),
        report_saved=working != before,
        read_only=False,
        today_work=list(report.today_work or []),
        problems=list(report.problems or []),
        tomorrow_plan=list(report.tomorrow_plan or []),
        command_results=[
            _execution_result(item, tenant_id=execution_context.tenant_id)
            for item in executions
        ],
    )


def build_typed_daily_snapshot(
    *,
    user: "User",
    report_date: date,
    report: "DailyReport | None",
) -> DailyReportMutationSnapshot:
    values = {
        field_name: tuple(str(item).strip() for item in (getattr(report, field_name, None) or []) if str(item).strip())
        for field_name in REPORT_FIELD_ORDER
    }
    section_status = dict(getattr(report, "section_status", None) or {})
    raw_ids = section_status.get(DRAFT_ITEM_IDS_KEY)
    raw_ids = raw_ids if isinstance(raw_ids, dict) else {}
    item_ids: dict[str, tuple[str, ...]] = {}
    for field_name in REPORT_FIELD_ORDER:
        candidate_ids = raw_ids.get(field_name)
        candidate_ids = list(candidate_ids) if isinstance(candidate_ids, list) else []
        normalized = [
            str(candidate_ids[index]).strip()
            if index < len(candidate_ids) and str(candidate_ids[index]).strip()
            else _make_item_id(field_name, index + 1, value)
            for index, value in enumerate(values[field_name])
        ]
        item_ids[field_name] = tuple(normalized)
    report_id = getattr(report, "id", None) or uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"agent2-daily-report:{user.id}:{report_date.isoformat()}",
    )
    return DailyReportMutationSnapshot(
        report_id=report_id,
        owner_user_id=user.id,
        version=_typed_report_version(section_status),
        status=str(getattr(report, "status", "") or "collecting"),
        today_work=values["today_work"],
        problems=values["problems"],
        tomorrow_plan=values["tomorrow_plan"],
        item_ids=item_ids,
        acknowledged_empty_fields=frozenset(
            field_name
            for field_name, status_key in EMPTY_ACK_KEYS.items()
            if bool(section_status.get(status_key)) and not values[field_name]
        ),
    )


def _blocked_result(
    existing: "DailyReport | None",
    report_date: date,
    execution: TypedDailyCommandExecution,
    *,
    tenant_id: str,
) -> Agent2DailyExecutionResult:
    return Agent2DailyExecutionResult(
        report_id=str(getattr(existing, "id", "") or "") or None,
        report_date=report_date,
        status=str(getattr(existing, "status", "") or execution.before.status),
        message=_blocked_message(execution.validation.reason_code),
        report_saved=False,
        read_only=False,
        today_work=list(execution.before.today_work),
        problems=list(execution.before.problems),
        tomorrow_plan=list(execution.before.tomorrow_plan),
        command_results=[_execution_result(execution, tenant_id=tenant_id)],
    )


def _execution_result(execution: TypedDailyCommandExecution, *, tenant_id: str) -> dict:
    return {
        "receipt_id": _daily_receipt_id(
            execution.command.idempotency_key,
            tenant_id=tenant_id,
        ),
        "typed_command": execution.command.as_dict(),
        "validation_status": execution.validation.status,
        "status": _receipt_status(execution),
        "reason": execution.validation.reason_code,
        "changed": execution.changed,
        "actual_write": execution.should_write_db,
        "audit": execution.audit.as_dict(),
    }


def _with_projected_draft_status(
    execution: TypedDailyCommandExecution,
) -> TypedDailyCommandExecution:
    """Keep content-command receipts and the persisted report in one state."""

    if (
        not execution.changed
        or not execution.should_write_db
        or execution.validation.status == "blocked"
        or execution.command.command_type
        in {"query_report", "reopen_report", "submit_report"}
        or execution.after.status
        not in {STATUS_COLLECTING, STATUS_PENDING_CONFIRMATION}
    ):
        return execution
    assessment = assess_daily_report_completeness(
        today_work=execution.after.today_work,
        problems=execution.after.problems,
        tomorrow_plan=execution.after.tomorrow_plan,
        acknowledged_empty_fields=execution.after.acknowledged_empty_fields,
    )
    if execution.after.status == assessment.draft_status:
        return execution
    return replace(
        execution,
        after=replace(execution.after, status=assessment.draft_status),
    )


async def _persist_execution_receipts(
    session: "AsyncSession",
    *,
    user: "User",
    report_date: date,
    report_id: uuid.UUID | None,
    executions: Sequence[TypedDailyCommandExecution],
    context: TypedDailyExecutionContext,
) -> None:
    from app.models import Agent2DailyCommandReceipt

    for execution in executions:
        command = execution.command
        receipt_id = _daily_receipt_uuid(
            command.idempotency_key,
            tenant_id=context.tenant_id,
        )
        statement = (
            pg_insert(Agent2DailyCommandReceipt)
            .values(
                receipt_id=receipt_id,
                tenant_id=context.tenant_id,
                user_id=user.id,
                report_id=report_id,
                report_date=report_date,
                message_id=command.idempotency_key.rsplit(":daily:", 1)[0],
                command_id=command.command_id,
                decision_id=command.decision_id,
                sub_decision_id=command.sub_decision_id,
                command_type=command.command_type,
                idempotency_key=command.idempotency_key,
                status=_receipt_status(execution),
                validation_status=execution.validation.status,
                actual_write=execution.should_write_db,
                resource_type="daily_report",
                resource_id=str(report_id or command.report_id),
                reason_code=execution.validation.reason_code,
                before_json=_snapshot_json(execution.before),
                after_json=_snapshot_json(execution.after),
                audit_json={
                    **execution.audit.as_dict(),
                    "_execution_scope": _daily_execution_scope(context),
                },
            )
            .on_conflict_do_nothing(
                index_elements=[
                    Agent2DailyCommandReceipt.tenant_id,
                    Agent2DailyCommandReceipt.idempotency_key,
                ]
            )
        )
        await session.execute(statement)


async def _load_prior_execution_receipts(
    session: "AsyncSession",
    *,
    tenant_id: str,
    commands: Sequence[TypedDailyCommand],
) -> dict[str, object]:
    from app.models import Agent2DailyCommandReceipt

    keys = tuple(dict.fromkeys(command.idempotency_key for command in commands))
    rows = list(
        (
            await session.scalars(
                select(Agent2DailyCommandReceipt).where(
                    Agent2DailyCommandReceipt.tenant_id == tenant_id,
                    Agent2DailyCommandReceipt.idempotency_key.in_(keys),
                )
            )
        ).all()
    )
    return {row.idempotency_key: row for row in rows}


def _prior_blocked_execution(
    command: TypedDailyCommand,
    snapshot: DailyReportMutationSnapshot,
    receipt: object,
) -> TypedDailyCommandExecution:
    reason = str(getattr(receipt, "reason_code", "") or "prior_blocked_receipt")
    return _blocked_execution(command, snapshot, reason)


def _execution_from_prior_receipt(
    command: TypedDailyCommand,
    snapshot: DailyReportMutationSnapshot,
    receipt: object,
    *,
    context: TypedDailyExecutionContext,
    require_execution_scope: bool,
) -> TypedDailyCommandExecution:
    if not _prior_receipt_matches(
        command,
        snapshot,
        receipt,
        context=context,
        require_execution_scope=require_execution_scope,
    ):
        return _blocked_execution(command, snapshot, "idempotency_collision")
    status = str(getattr(receipt, "status", "") or "")
    if status in {"executed", "duplicate"}:
        return _prior_duplicate_execution(command, snapshot)
    if status == "blocked":
        return _prior_blocked_execution(command, snapshot, receipt)
    return _blocked_execution(command, snapshot, "prior_receipt_state_invalid")


def _prior_receipt_matches(
    command: TypedDailyCommand,
    snapshot: DailyReportMutationSnapshot,
    receipt: object,
    *,
    context: TypedDailyExecutionContext,
    require_execution_scope: bool,
) -> bool:
    receipt_report_date = getattr(receipt, "report_date", None)
    expected_report_date = (
        _query_report_date(command, context.report_date)
        if command.command_type == "query_report"
        else _mutation_report_date((command,), context.report_date)
    )
    receipt_report_id = str(
        getattr(receipt, "report_id", "")
        or getattr(receipt, "resource_id", "")
        or ""
    )
    expected_message_id = command.idempotency_key.rsplit(":daily:", 1)[0]
    receipt_message_id = str(getattr(receipt, "message_id", "") or "")
    audit = getattr(receipt, "audit_json", None)
    audit = audit if isinstance(audit, dict) else {}
    execution_scope = audit.get("_execution_scope")
    scope_matches = (
        isinstance(execution_scope, dict)
        and execution_scope == _daily_execution_scope(context)
    )
    return (
        str(getattr(receipt, "user_id", "") or "") == str(snapshot.owner_user_id)
        and str(getattr(receipt, "command_id", "") or "") == str(command.command_id)
        and str(getattr(receipt, "decision_id", "") or "") == str(command.decision_id)
        and str(getattr(receipt, "sub_decision_id", "") or "")
        == str(command.sub_decision_id)
        and str(getattr(receipt, "command_type", "") or "")
        == command.command_type
        and str(getattr(receipt, "idempotency_key", "") or "")
        == command.idempotency_key
        and (receipt_report_date is None or receipt_report_date == expected_report_date)
        and (not receipt_report_id or receipt_report_id == str(snapshot.report_id))
        and (not receipt_message_id or receipt_message_id == expected_message_id)
        and (scope_matches or not require_execution_scope)
    )


def _daily_execution_scope(
    context: TypedDailyExecutionContext,
) -> dict[str, str]:
    material = "\x1f".join(
        (
            context.tenant_id,
            context.conversation_id,
            context.source,
            context.source_turn_id,
        )
    )
    return {
        "conversation_id": context.conversation_id,
        "source_channel": context.source,
        "source_turn_id": context.source_turn_id,
        "scope_sha256": hashlib.sha256(material.encode("utf-8")).hexdigest(),
    }


def _blocked_execution(
    command: TypedDailyCommand,
    snapshot: DailyReportMutationSnapshot,
    reason: str,
) -> TypedDailyCommandExecution:
    from app.agent2.typed_daily_commands import TypedDailyCommandValidation

    candidate = execute_typed_daily_command(
        command,
        snapshot=snapshot,
        actor_user_id=snapshot.owner_user_id,
    )
    return replace(
        candidate,
        validation=TypedDailyCommandValidation("blocked", reason),
        after=snapshot,
        changed=False,
        should_write_db=False,
        audit=replace(
            candidate.audit,
            after_version=snapshot.version,
            result="blocked",
            reason=reason,
            validation_result="blocked",
            execution_result="blocked",
            actual_write=False,
            reply_type="write_blocked",
        ),
    )


def _prior_duplicate_execution(
    command: TypedDailyCommand,
    snapshot: DailyReportMutationSnapshot,
) -> TypedDailyCommandExecution:
    """Return the stable prior receipt result before revalidating a spent Ticket."""

    replay_command = replace(
        command,
        admission_ticket={},
        admission_required=False,
        admission_action_id="",
        admission_operation="",
    )
    candidate = execute_typed_daily_command(
        replay_command,
        snapshot=snapshot,
        actor_user_id=snapshot.owner_user_id,
        executed_idempotency_keys={command.idempotency_key},
    )
    return replace(candidate, command=command)


def _authority_blocked_execution(
    command: TypedDailyCommand,
    snapshot: DailyReportMutationSnapshot,
    reason: str,
) -> TypedDailyCommandExecution:
    """Represent an authoritative Ticket rejection without changing the report."""

    return _blocked_execution(command, snapshot, reason)


def _daily_execution_time(context: TypedDailyExecutionContext) -> datetime:
    return context.execution_started_at or datetime.now(UTC)


def _daily_ticket_execution_request(
    *,
    command: TypedDailyCommand,
    user_id: str,
    context: TypedDailyExecutionContext,
) -> AdmissionTicketExecutionRequest:
    return AdmissionTicketExecutionRequest(
        admission_ticket=command.admission_ticket,
        tenant_id=context.tenant_id,
        user_id=user_id,
        conversation_id=context.conversation_id,
        source_message_id=context.source_turn_id,
        action_id=command.admission_action_id,
        domain="report",
        operation=command.admission_operation,
        object_ref={
            "object_type": "daily_report",
            "stable_id": str(command.report_id),
            "version": command.report_version,
        },
        conversation_state_version=context.conversation_state_version,
        executed_at=_daily_execution_time(context),
        command_type=command.command_type,
        receipt_kind="daily_report",
        company_id=context.company_id,
        department_id=context.department_id,
        team_id=context.team_id,
        actor_role_ids=tuple(context.actor_role_ids),
        allowed_case_ids=tuple(context.allowed_case_ids),
    )


def _daily_receipt_uuid(idempotency_key: str, *, tenant_id: str) -> uuid.UUID:
    tenant = tenant_id or "agent2-daily"
    return uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"agent2-daily-receipt:{tenant}:{idempotency_key}",
    )


def _daily_receipt_id(idempotency_key: str, *, tenant_id: str) -> str:
    return str(_daily_receipt_uuid(idempotency_key, tenant_id=tenant_id))


def _receipt_status(execution: TypedDailyCommandExecution) -> str:
    result = execution.audit.execution_result
    if result == "duplicate":
        return "duplicate"
    if execution.validation.status == "blocked":
        return "blocked"
    return "executed"


def _snapshot_json(snapshot: DailyReportMutationSnapshot) -> dict:
    return {
        "report_id": str(snapshot.report_id),
        "owner_user_id": str(snapshot.owner_user_id),
        "version": snapshot.version,
        "status": snapshot.status,
        "today_work": list(snapshot.today_work),
        "problems": list(snapshot.problems),
        "tomorrow_plan": list(snapshot.tomorrow_plan),
        "acknowledged_empty_fields": sorted(
            snapshot.acknowledged_empty_fields
        ),
        "item_ids": {
            field_name: list(item_ids)
            for field_name, item_ids in snapshot.item_ids.items()
        },
    }


def _blocked_message(reason: str) -> str:
    if reason in {"ambiguous_target", "target_not_found"}:
        return "目标不明确，请说明具体条目。"
    if reason == "duplicate_message":
        return "这条指令已经处理过，没有重复写入。"
    if reason == "version_conflict":
        return "日报已发生变化，请基于最新内容重试。"
    return "该操作未通过执行校验，没有写入日报。"


def _require_daily_execution_authority(
    commands: Sequence[TypedDailyCommand],
    authority: str,
) -> None:
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


def _validate_executor_command_shapes(commands: Sequence[TypedDailyCommand]) -> None:
    """Final structural guard independent of planner and legacy command validation."""

    for command in commands:
        if command.command_type in {"edit_item", "delete_item"} and len(command.target_item_ids) != 1:
            raise ValueError(f"{command.command_type} requires exactly one target item")
        if command.command_type == "merge_items" and len(command.target_item_ids) < 2:
            raise ValueError("merge_items requires at least two target items")


def _query_report_date(command: TypedDailyCommand, default: date) -> date:
    raw = str(command.patch.get("report_date") or "").strip()
    return date.fromisoformat(raw) if raw else default


def _mutation_report_date(commands: Sequence[TypedDailyCommand], default: date) -> date:
    explicit = {
        date.fromisoformat(str(command.patch["report_date"]))
        for command in commands
        if command.command_type == "reopen_report" and command.patch.get("report_date")
    }
    if len(explicit) > 1:
        raise ValueError("typed daily mutations target multiple report dates")
    return next(iter(explicit), default)


def _query_result_message(report_date: date, snapshot: DailyReportMutationSnapshot) -> str:
    if not any((snapshot.today_work, snapshot.problems, snapshot.tomorrow_plan)) and not snapshot.acknowledged_empty_fields:
        return f"{report_date.isoformat()} 暂无日报内容。"
    status_text = {
        STATUS_COMPLETED: "已提交",
        STATUS_PENDING_CONFIRMATION: "待确认",
    }.get(snapshot.status, "填写中")
    lines = [f"{report_date.isoformat()} 日报（{status_text}）"]
    for field_name, title, values in (
        ("today_work", "今日工作", snapshot.today_work),
        ("problems", "问题与风险", snapshot.problems),
        ("tomorrow_plan", "明日计划", snapshot.tomorrow_plan),
    ):
        lines.append(f"{title}：")
        lines.extend(f"- {value}" for value in values)
        if not values:
            lines.append(
                "- 暂无"
                if field_name in snapshot.acknowledged_empty_fields
                else "- 未填写"
            )
    return "\n".join(lines)


def _typed_report_version(section_status: dict | None) -> int:
    try:
        return max(0, int((section_status or {}).get(TYPED_REPORT_VERSION_KEY, 0)))
    except (TypeError, ValueError):
        return 0


def _typed_command_keys(section_status: dict | None) -> list[str]:
    values = (section_status or {}).get(TYPED_COMMAND_KEYS_KEY, [])
    return [str(value) for value in values if str(value).strip()] if isinstance(values, list) else []


def _typed_audit_records(section_status: dict | None) -> list[dict]:
    values = (section_status or {}).get(TYPED_AUDIT_KEY, [])
    return [dict(value) for value in values if isinstance(value, dict)] if isinstance(values, list) else []


def _make_item_id(field_name: str, index: int, value: str) -> str:
    digest = hashlib.sha1(f"{field_name}:{index}:{value}".encode("utf-8")).hexdigest()[:12]
    return f"di_{digest}"


def _mutation_result_lead(
    *,
    status: str,
    report_date: date,
    occurred_at: datetime,
    settings: object,
) -> str:
    if status == STATUS_COMPLETED:
        return "日报已提交。"
    if status != STATUS_PENDING_CONFIRMATION:
        return "日报已更新。"
    return pending_confirmation_next_step(
        report_date=report_date,
        occurred_at=occurred_at,
        settings=settings,
    )


def pending_confirmation_next_step(
    *,
    report_date: date,
    occurred_at: datetime,
    settings: object,
) -> str:
    if report_date == occurred_at.date():
        return (
            "日报已填写完整，当前为待确认状态。"
            "如不再修改，系统会按现有规则于次日上午自动提交。"
        )
    summary_time = time(
        int(getattr(settings, "summary_cron_hour", 9)),
        int(getattr(settings, "summary_cron_minute", 0)),
    )
    if (
        report_date == occurred_at.date() - timedelta(days=1)
        and occurred_at.time().replace(tzinfo=None) < summary_time
    ):
        return (
            "历史日报已补充完整，当前为待确认状态。"
            "如不再修改，系统会按现有规则在今天晨报前自动提交；"
            "本次没有代你确认或提交。"
        )
    return (
        "历史日报已补充完整，当前为待确认状态。"
        "请确认无误后再提交；本次没有代你确认或提交。"
    )
