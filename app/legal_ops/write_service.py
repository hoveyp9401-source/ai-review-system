from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent2.business.contracts import (
    BusinessCommandContext,
    CreateCaseProgress,
    DeleteCaseProgress,
    UpdateCaseProgress,
)
from app.agent2.business.models import PeriodicReport
from app.agent2.business.policy import BusinessEffectPolicy
from app.agent2.business.sql_executor import SqlBusinessExecutor
from app.agent2.report_domain import TypedPeriodicReportCommand
from app.agent2.report_sql_executor import execute_periodic_report_commands
from app.agent2.typed_daily_commands import TypedDailyCommand
from app.agent2.typed_daily_executor import (
    TypedDailyExecutionContext,
    execute_typed_agent2_daily_commands,
)
from app.config import Settings
from app.legal_ops.access import LivePrincipalScope
from app.legal_ops.auth import SandboxPrincipal
from app.models import DailyReport, User


ReportMutation = Literal["append_item", "edit_item", "delete_item", "submit_report"]


@dataclass(frozen=True)
class LegalOpsWriteResult:
    status: Literal["executed", "duplicate", "blocked", "failed"]
    actual_write: bool
    reason_code: str = ""
    resource_ref: str = ""

    @property
    def succeeded(self) -> bool:
        return self.status in {"executed", "duplicate"}

    def user_payload(self) -> dict[str, Any]:
        return {
            "business_status": self.status,
            "actual_write": self.actual_write,
            "reason_code": self.reason_code,
        }


def build_case_owner_context(
    *,
    principal: SandboxPrincipal,
    scope: LivePrincipalScope,
    operation_id: str,
    occurred_at: datetime,
) -> BusinessCommandContext:
    if scope.binding is None or scope.allowed_case_ids is None:
        raise PermissionError("case owner identity binding required")
    binding = scope.binding
    return BusinessCommandContext(
        tenant_id=principal.tenant_id,
        company_id=str(binding.company_id or ""),
        department_id=str(binding.department_id or ""),
        team_id=str(binding.team_id or ""),
        actor_user_id=principal.user_id,
        actor_role_ids=tuple(principal.role_ids),
        allowed_case_ids=tuple(scope.allowed_case_ids),
        writable_case_ids=scope.writable_case_ids,
        source_message_id=operation_id,
        source_channel="legal_ops_ui",
        occurred_at=occurred_at,
    )


async def create_case_progress(
    session: AsyncSession,
    *,
    context: BusinessCommandContext,
    settings: Settings,
    case_id: str,
    summary: str,
    details: str,
    occurred_at: datetime,
    current_status: str = "",
    next_actions: tuple[str, ...] = (),
) -> LegalOpsWriteResult:
    command = CreateCaseProgress(
        command_id=f"legal-ops-progress-create:{context.source_message_id}",
        case_id=case_id,
        occurred_at=occurred_at,
        progress_type="manual_update",
        summary=summary,
        details=details,
        related_party_ids=(),
        related_document_ids=(),
        related_travel_intent_ids=(),
        confidence=1.0,
        current_status=current_status,
        next_actions=next_actions,
    )
    receipt = await SqlBusinessExecutor(
        session,
        effect_policy=BusinessEffectPolicy.from_settings(settings),
        execution_authority="authenticated_admin_command",
    ).execute(command, context)
    await session.commit()
    return LegalOpsWriteResult(
        status=receipt.status,
        actual_write=receipt.actual_write,
        reason_code=receipt.error_code or "",
        resource_ref=receipt.resource_id,
    )


async def update_case_progress(
    session: AsyncSession,
    *,
    context: BusinessCommandContext,
    settings: Settings,
    progress_id: str,
    expected_version: int,
    summary: str | None,
    details: str | None,
) -> LegalOpsWriteResult:
    receipt = await SqlBusinessExecutor(
        session,
        effect_policy=BusinessEffectPolicy.from_settings(settings),
        execution_authority="authenticated_admin_command",
    ).execute(
        UpdateCaseProgress(
            command_id=f"legal-ops-progress-update:{context.source_message_id}",
            progress_id=progress_id,
            expected_version=expected_version,
            summary=summary,
            details=details,
        ),
        context,
    )
    await session.commit()
    return LegalOpsWriteResult(
        status=receipt.status,
        actual_write=receipt.actual_write,
        reason_code=receipt.error_code or "",
        resource_ref=receipt.resource_id,
    )


async def delete_case_progress(
    session: AsyncSession,
    *,
    context: BusinessCommandContext,
    settings: Settings,
    progress_id: str,
    expected_version: int,
    reason: str,
) -> LegalOpsWriteResult:
    receipt = await SqlBusinessExecutor(
        session,
        effect_policy=BusinessEffectPolicy.from_settings(settings),
        execution_authority="authenticated_admin_command",
    ).execute(
        DeleteCaseProgress(
            command_id=f"legal-ops-progress-delete:{context.source_message_id}",
            progress_id=progress_id,
            expected_version=expected_version,
            reason=reason,
        ),
        context,
    )
    await session.commit()
    return LegalOpsWriteResult(
        status=receipt.status,
        actual_write=receipt.actual_write,
        reason_code=receipt.error_code or "",
        resource_ref=receipt.resource_id,
    )


async def mutate_report(
    session: AsyncSession,
    *,
    context: BusinessCommandContext,
    settings: Settings,
    report_ref: str,
    command_type: ReportMutation,
    expected_version: int,
    field_name: str = "",
    item_ref: str = "",
    value: str = "",
) -> LegalOpsWriteResult:
    try:
        report_id = UUID(report_ref)
        actor_id = UUID(context.actor_user_id)
    except (TypeError, ValueError):
        return LegalOpsWriteResult("blocked", False, "report_not_found")

    daily = await session.scalar(
        select(DailyReport).where(
            DailyReport.id == report_id,
            DailyReport.user_id == actor_id,
        )
    )
    if daily is not None:
        user = await session.scalar(
            select(User).where(User.id == actor_id, User.active.is_(True))
        )
        if user is None:
            return LegalOpsWriteResult("blocked", False, "active_user_required")
        command = _daily_command(
            operation_id=context.source_message_id,
            command_type=command_type,
            report_id=report_id,
            expected_version=expected_version,
            field_name=field_name,
            item_ref=item_ref,
            value=value,
        )
        source_hash = hashlib.sha256(
            f"{report_ref}:{context.source_message_id}".encode("utf-8")
        ).hexdigest()
        result = await execute_typed_agent2_daily_commands(
            session,
            user=user,
            commands=(command,),
            execution_context=TypedDailyExecutionContext(
                report_date=daily.report_date,
                source="legal_ops_ui",
                source_text_hash=source_hash,
                tenant_id=context.tenant_id,
                source_turn_id=context.source_message_id,
                occurred_at=context.occurred_at,
            ),
            settings=settings,
            execution_authority="authenticated_admin_command",
        )
        await session.commit()
        command_result = result.command_results[0] if result.command_results else {}
        return daily_write_result_from_command_result(
            command_result,
            report_ref=report_ref,
        )

    periodic = await session.scalar(
        select(PeriodicReport).where(
            PeriodicReport.tenant_id == context.tenant_id,
            PeriodicReport.report_id == report_id,
            PeriodicReport.owner_user_id == context.actor_user_id,
        )
    )
    if periodic is None:
        return LegalOpsWriteResult("blocked", False, "report_not_found")
    command = _periodic_command(
        operation_id=context.source_message_id,
        command_type=command_type,
        report=periodic,
        expected_version=expected_version,
        field_name=field_name,
        item_ref=item_ref,
        value=value,
    )
    try:
        persisted = (
            await execute_periodic_report_commands(
                session,
                commands=(command,),
                context=context,
                timezone_name=settings.timezone,
                execution_authority="authenticated_admin_command",
            )
        )[0]
    except ValueError:
        await session.rollback()
        return LegalOpsWriteResult("blocked", False, "report_period_not_current")
    await session.commit()
    status = (
        "duplicate" if persisted.status == "duplicate"
        else "executed" if persisted.status == "authorized"
        else "blocked"
    )
    return LegalOpsWriteResult(
        status=status,
        actual_write=persisted.actual_write,
        reason_code=persisted.execution.reason_code,
        resource_ref=report_ref,
    )


def daily_write_result_from_command_result(
    command_result: dict[str, Any],
    *,
    report_ref: str,
) -> LegalOpsWriteResult:
    receipt_status = str(command_result.get("status") or "blocked")
    status: Literal["executed", "duplicate", "blocked", "failed"] = (
        receipt_status
        if receipt_status in {"executed", "duplicate", "blocked", "failed"}
        else "failed"
    )
    return LegalOpsWriteResult(
        status=status,
        actual_write=bool(command_result.get("actual_write")),
        reason_code=str(command_result.get("reason") or ""),
        resource_ref=report_ref,
    )


def _daily_command(
    *,
    operation_id: str,
    command_type: ReportMutation,
    report_id: UUID,
    expected_version: int,
    field_name: str,
    item_ref: str,
    value: str,
) -> TypedDailyCommand:
    command_id = uuid5(NAMESPACE_URL, f"legal-ops-daily:{operation_id}")
    patch: dict[str, Any] = {}
    targets: tuple[str, ...] = ()
    if command_type == "append_item":
        patch = {"field": field_name, "items": [value]}
    elif command_type == "edit_item":
        patch = {"replacement": value}
        targets = (item_ref,)
    elif command_type == "delete_item":
        targets = (item_ref,)
    return TypedDailyCommand(
        command_id=command_id,
        decision_id=command_id,
        sub_decision_id=command_id,
        command_type=command_type,
        report_id=report_id,
        report_version=expected_version,
        target_item_ids=targets,
        patch=patch,
        idempotency_key=f"{operation_id}:daily:{command_type}",
    )


def _periodic_command(
    *,
    operation_id: str,
    command_type: ReportMutation,
    report: PeriodicReport,
    expected_version: int,
    field_name: str,
    item_ref: str,
    value: str,
) -> TypedPeriodicReportCommand:
    command_id = uuid5(NAMESPACE_URL, f"legal-ops-periodic:{operation_id}")
    patch: dict[str, Any] = {}
    targets: tuple[str, ...] = ()
    if command_type == "append_item":
        patch = {"field": field_name, "value": value}
    elif command_type == "edit_item":
        patch = {"replacement": value}
        targets = (item_ref,)
    elif command_type == "delete_item":
        targets = (item_ref,)
    return TypedPeriodicReportCommand(
        command_id=command_id,
        decision_id=command_id,
        sub_decision_id=command_id,
        command_type=command_type,
        report_type=report.report_type,
        period_key=report.period_key,
        report_id=report.report_id,
        report_version=expected_version,
        target_item_ids=targets,
        patch=patch,
        idempotency_key=f"{operation_id}:periodic:{command_type}",
    )
