from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select

from app.agent2.daily_state import REPORT_FIELD_ORDER
from app.agent2.tool_calling.context import (
    TrustedReportItem,
    TrustedReportSnapshot,
)
from app.agent2.typed_daily_commands import (
    DailyReportMutationSnapshot,
    TypedDailyCommand,
    TypedDailyCommandExecution,
    execute_typed_daily_command,
)
from app.agent2.typed_daily_executor import (
    EMPTY_ACK_KEYS,
    TYPED_AUDIT_KEY,
    TYPED_COMMAND_KEYS_KEY,
    TYPED_REPORT_VERSION_KEY,
    build_typed_daily_snapshot,
)
from app.models import Agent2DailyCommandReceipt, DailyReport, User
from app.repositories import acquire_daily_report_advisory_lock


CorrectionStatus = Literal[
    "success",
    "no_op",
    "clarification_required",
    "blocked",
]


@dataclass(frozen=True)
class DailyReportDateCorrectionResult:
    status: CorrectionStatus
    error_code: str | None
    report_id: UUID | None
    before_version: int | None
    after_version: int | None
    typed_receipt_ids: tuple[str, ...] = ()


class SqlDailyReportDateCorrection:
    """Atomically relocate one owned report and preserve typed audit evidence."""

    def __init__(self, session) -> None:
        self._session = session

    async def execute(
        self,
        *,
        user: User,
        tenant_id: str,
        source_report_id: UUID,
        expected_version: int,
        source_date: date,
        target_date: date,
        acknowledged_empty_fields: tuple[str, ...],
        submit_after_correction: bool,
        idempotency_key: str,
        source_message_id: str,
        now: datetime,
        expected_source_state_sha256: str | None = None,
    ) -> DailyReportDateCorrectionResult:
        if source_date == target_date:
            return DailyReportDateCorrectionResult(
                "blocked",
                "SOURCE_AND_TARGET_DATE_MUST_DIFFER",
                source_report_id,
                expected_version,
                expected_version,
            )

        for report_date in sorted((source_date, target_date)):
            await acquire_daily_report_advisory_lock(
                self._session,
                user.id,
                report_date,
            )
        rows = list(
            (
                await self._session.scalars(
                    select(DailyReport)
                    .where(
                        DailyReport.user_id == user.id,
                        DailyReport.report_date.in_((source_date, target_date)),
                    )
                    .with_for_update()
                )
            ).all()
        )
        source = next(
            (row for row in rows if row.report_date == source_date),
            None,
        )
        target = next(
            (row for row in rows if row.report_date == target_date),
            None,
        )

        if source is None:
            if target is not None and target.id == source_report_id:
                snapshot = build_typed_daily_snapshot(
                    user=user,
                    report_date=target_date,
                    report=target,
                )
                return DailyReportDateCorrectionResult(
                    "no_op",
                    None,
                    target.id,
                    snapshot.version,
                    snapshot.version,
                )
            return DailyReportDateCorrectionResult(
                "blocked",
                "SOURCE_REPORT_NOT_FOUND",
                source_report_id,
                expected_version,
                expected_version,
            )
        source_snapshot = build_typed_daily_snapshot(
            user=user,
            report_date=source_date,
            report=source,
        )
        if (
            source.id != source_report_id
            or source_snapshot.version != expected_version
            or (
                expected_source_state_sha256 is not None
                and _trusted_state_snapshot(
                    tenant_id=tenant_id,
                    report_date=source_date,
                    snapshot=source_snapshot,
                ).state_sha256
                != expected_source_state_sha256
            )
        ):
            return DailyReportDateCorrectionResult(
                "blocked",
                "SOURCE_REPORT_BINDING_CHANGED",
                source.id,
                source_snapshot.version,
                source_snapshot.version,
            )
        if target is not None:
            target_snapshot = build_typed_daily_snapshot(
                user=user,
                report_date=target_date,
                report=target,
            )
            return DailyReportDateCorrectionResult(
                "clarification_required",
                "TARGET_REPORT_ALREADY_EXISTS",
                source.id,
                source_snapshot.version,
                target_snapshot.version,
            )

        working = replace(
            source_snapshot,
            version=source_snapshot.version + 1,
        )
        executions: list[TypedDailyCommandExecution] = []
        new_empty_fields = tuple(
            field_name
            for field_name in acknowledged_empty_fields
            if field_name not in working.acknowledged_empty_fields
        )
        restore_completed = (
            source_snapshot.status == "completed" and bool(new_empty_fields)
        )
        if restore_completed:
            working, execution = self._apply(
                working,
                self._command(
                    idempotency_key=idempotency_key,
                    ordinal=len(executions),
                    command_type="reopen_report",
                    report=working,
                    patch={"report_date": target_date.isoformat()},
                ),
                user.id,
            )
            executions.append(execution)
        for field_name in new_empty_fields:
            working, execution = self._apply(
                working,
                self._command(
                    idempotency_key=idempotency_key,
                    ordinal=len(executions),
                    command_type="acknowledge_empty_section",
                    report=working,
                    patch={"field": field_name},
                ),
                user.id,
            )
            executions.append(execution)
        if (
            (submit_after_correction or restore_completed)
            and working.status != "completed"
        ):
            working, execution = self._apply(
                working,
                self._command(
                    idempotency_key=idempotency_key,
                    ordinal=len(executions),
                    command_type="submit_report",
                    report=working,
                    patch={},
                ),
                user.id,
            )
            executions.append(execution)

        blocked = next(
            (
                execution
                for execution in executions
                if execution.validation.status == "blocked"
            ),
            None,
        )
        if blocked is not None:
            code = (
                "EXPLICIT_EMPTY_FIELD_CONTAINS_ITEMS"
                if blocked.command.command_type
                == "acknowledge_empty_section"
                else (
                    "REPORT_INCOMPLETE"
                    if blocked.command.command_type == "submit_report"
                    else str(blocked.validation.reason_code)
                )
            )
            return DailyReportDateCorrectionResult(
                "clarification_required",
                code,
                source.id,
                source_snapshot.version,
                source_snapshot.version,
            )

        relocation_key = f"{idempotency_key}:daily:date-correction"
        section_status = dict(source.section_status or {})
        known_keys = list(
            dict.fromkeys(
                str(value)
                for value in section_status.get(TYPED_COMMAND_KEYS_KEY, ())
                if str(value)
            )
        )
        for key in (
            relocation_key,
            *(execution.command.idempotency_key for execution in executions),
        ):
            if key not in known_keys:
                known_keys.append(key)
        relocation_audit = {
            "command_type": "correct_report_date",
            "report_id": str(source.id),
            "source_report_date": source_date.isoformat(),
            "target_report_date": target_date.isoformat(),
            "expected_version": source_snapshot.version,
            "before_version": source_snapshot.version,
            "after_version": working.version,
            "result": "executed",
            "reason": "server_bound_date_correction",
            "idempotency_key": relocation_key,
            "actual_write": True,
            "source_message_id": source_message_id,
            "occurred_at": now.isoformat(),
        }
        existing_audits = section_status.get(TYPED_AUDIT_KEY, ())
        existing_audits = (
            list(existing_audits)
            if isinstance(existing_audits, list)
            else []
        )
        section_status[TYPED_REPORT_VERSION_KEY] = working.version
        section_status[TYPED_COMMAND_KEYS_KEY] = known_keys[-100:]
        section_status[TYPED_AUDIT_KEY] = [
            *existing_audits,
            relocation_audit,
            *(execution.audit.as_dict() for execution in executions),
        ][-100:]
        for field_name, status_key in EMPTY_ACK_KEYS.items():
            if field_name in working.acknowledged_empty_fields:
                section_status[status_key] = True
            else:
                section_status.pop(status_key, None)

        before_json = {
            "report_date": source_date.isoformat(),
            **self._snapshot_json(source_snapshot),
        }
        source.report_date = target_date
        source.section_status = section_status
        source.status = working.status
        source.completeness_score = Decimal(
            str(
                sum(
                    bool(getattr(working, field_name))
                    or field_name in working.acknowledged_empty_fields
                    for field_name in REPORT_FIELD_ORDER
                )
                / len(REPORT_FIELD_ORDER)
            )
        )
        source.last_modified_by_user = True
        source.last_modified_at = now
        if submit_after_correction and working.status == "completed":
            source.confirmation_type = "user_confirmed"
            source.confirmed_by_user = True
            source.submitted_at = now
            source.pending_confirmation_at = None
            source.auto_submit_at = None
        payload = dict(source.llm_payload or {})
        payload["agent2_date_correction"] = {
            "source_report_date": source_date.isoformat(),
            "target_report_date": target_date.isoformat(),
            "source_message_id": source_message_id,
            "acknowledged_empty_fields": list(acknowledged_empty_fields),
            "submit_after_correction": submit_after_correction,
        }
        source.llm_payload = payload
        await self._session.flush()

        after_json = {
            "report_date": target_date.isoformat(),
            **self._snapshot_json(working),
        }
        receipt_id = uuid5(
            NAMESPACE_URL,
            f"agent2-daily-receipt:{tenant_id}:{relocation_key}",
        )
        command_id = uuid5(
            NAMESPACE_URL,
            f"agent2-date-correction:{relocation_key}",
        )
        self._session.add(
            Agent2DailyCommandReceipt(
                receipt_id=receipt_id,
                tenant_id=tenant_id,
                user_id=user.id,
                report_id=source.id,
                report_date=target_date,
                message_id=source_message_id,
                command_id=command_id,
                decision_id=uuid5(
                    NAMESPACE_URL,
                    f"agent2-date-correction-decision:{relocation_key}",
                ),
                sub_decision_id=uuid5(
                    NAMESPACE_URL,
                    f"agent2-date-correction-subdecision:{relocation_key}",
                ),
                command_type="correct_report_date",
                idempotency_key=relocation_key,
                status="executed",
                validation_status="authorized",
                actual_write=True,
                resource_type="daily_report",
                resource_id=str(source.id),
                reason_code="server_bound_date_correction",
                before_json=before_json,
                after_json=after_json,
                audit_json=relocation_audit,
            )
        )
        await self._session.flush()
        return DailyReportDateCorrectionResult(
            "success",
            None,
            source.id,
            source_snapshot.version,
            working.version,
            (str(receipt_id),),
        )

    @staticmethod
    def _apply(
        snapshot: DailyReportMutationSnapshot,
        command: TypedDailyCommand,
        actor_user_id: UUID,
    ) -> tuple[DailyReportMutationSnapshot, TypedDailyCommandExecution]:
        execution = execute_typed_daily_command(
            command,
            snapshot=snapshot,
            actor_user_id=actor_user_id,
        )
        return execution.after, execution

    @staticmethod
    def _command(
        *,
        idempotency_key: str,
        ordinal: int,
        command_type: str,
        report: DailyReportMutationSnapshot,
        patch: dict,
    ) -> TypedDailyCommand:
        identity = f"{idempotency_key}:date-correction:{ordinal}"
        return TypedDailyCommand(
            command_id=uuid5(NAMESPACE_URL, f"{identity}:command"),
            decision_id=uuid5(NAMESPACE_URL, f"{idempotency_key}:decision"),
            sub_decision_id=uuid5(NAMESPACE_URL, f"{identity}:subdecision"),
            command_type=command_type,
            report_id=report.report_id,
            report_version=report.version,
            target_item_ids=(),
            patch=patch,
            idempotency_key=f"{idempotency_key}:daily:correction:{ordinal}",
        )

    @staticmethod
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
        }


def _trusted_state_snapshot(
    *,
    tenant_id: str,
    report_date: date,
    snapshot: DailyReportMutationSnapshot,
) -> TrustedReportSnapshot:
    items: list[TrustedReportItem] = []
    for field_name in REPORT_FIELD_ORDER:
        values = tuple(getattr(snapshot, field_name))
        item_ids = tuple(snapshot.item_ids.get(field_name, ()))
        items.extend(
            TrustedReportItem(
                item_id=item_id,
                field=field_name,
                content=content,
                report_id=snapshot.report_id,
                report_version=snapshot.version,
            )
            for item_id, content in zip(item_ids, values, strict=True)
        )
    return TrustedReportSnapshot(
        report_id=snapshot.report_id,
        tenant_id=tenant_id,
        owner_user_id=snapshot.owner_user_id,
        report_date=report_date,
        version=snapshot.version,
        status=snapshot.status,
        items=tuple(items),
        acknowledged_empty_fields=snapshot.acknowledged_empty_fields,
    )
