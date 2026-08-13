"""Tool-Call Core adapter for the existing weekly Report domain."""

from __future__ import annotations

from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

from app.agent2.business.contracts import BusinessCommandContext
from app.agent2.outcome_adapters import periodic_execution_outcomes
from app.agent2.periodic_report_context import TrustedPeriodicReportContext
from app.agent2.report_domain import (
    PeriodicReportSnapshot,
    TypedPeriodicReportCommand,
)
from app.agent2.report_sql_executor import (
    execute_periodic_report_commands,
    load_periodic_report_snapshot,
)
from app.agent2.tool_calling.context import TrustedContext
from app.agent2.tool_calling.contracts import (
    ApplyCurrentWeeklyReportArgs,
    QueryCurrentWeeklyReportArgs,
    ReceiptStatus,
    SubmitCurrentWeeklyReportArgs,
)
from app.agent2.tool_calling.idempotency import build_write_idempotency_key
from app.agent2.tool_calling.production_daily_executor import (
    ProductionExecutionError,
    ProductionHandlerOutcome,
)
from app.agent2.tool_calling.production_handlers import ProductionHandlerRequest
from app.agent2.tool_calling.validation import BoundCall


class ProductionPeriodicReportExecutor:
    """Translate trusted Tool Calls into existing typed periodic commands."""

    def __init__(
        self,
        *,
        session: Any,
        user: Any,
        context: TrustedContext,
        bound_calls: dict[str, BoundCall],
        source_channel: str = "agent2_tool_call_core",
        execution_adapter: PeriodicReportExecutionPort | None = None,
    ) -> None:
        self._session = session
        self._user = user
        self._context = context
        self._bound_calls = bound_calls
        self._source_channel = source_channel
        self._execution_adapter = (
            execution_adapter or SqlPeriodicReportExecutionAdapter(session)
        )

    async def query_current_weekly_report(
        self,
        request: ProductionHandlerRequest,
    ) -> ProductionHandlerOutcome:
        self._arguments(request, QueryCurrentWeeklyReportArgs)
        trusted = self._trusted_target(request)
        live = await self._load_live()
        self._require_live_matches(trusted, live)
        return self._outcome(
            request,
            before=live,
            after=live,
            results=(),
            idempotency_key=None,
        )

    async def apply_current_weekly_report(
        self,
        request: ProductionHandlerRequest,
    ) -> ProductionHandlerOutcome:
        arguments = self._arguments(request, ApplyCurrentWeeklyReportArgs)
        trusted = self._trusted_target(request)
        self._require_argument_binding(
            trusted,
            report_id=arguments.report_id,
            expected_version=arguments.expected_version,
        )
        before = await self._load_live()
        self._require_live_matches(trusted, before)
        commands: list[TypedPeriodicReportCommand] = []
        next_version = before.version
        for ordinal, operation in enumerate(arguments.operations):
            if operation.operation == "append":
                command_type = "append_item"
                target_item_ids: tuple[str, ...] = ()
                patch = {
                    "field": operation.field,
                    "value": operation.content,
                }
            elif operation.operation == "edit":
                command_type = "edit_item"
                target_item_ids = (operation.item_id,)
                patch = {"replacement": operation.replacement}
            else:
                command_type = "delete_item"
                target_item_ids = (operation.item_id,)
                patch = {}
            commands.append(
                self._command(
                    request,
                    ordinal=ordinal,
                    command_type=command_type,
                    report=before,
                    report_version=next_version,
                    target_item_ids=target_item_ids,
                    patch=patch,
                )
            )
            next_version += 1
        results = await self._execute(tuple(commands))
        after = results[-1].execution.after
        return self._outcome(
            request,
            before=before,
            after=after,
            results=results,
            idempotency_key=self._tool_idempotency_key(request, trusted),
        )

    async def submit_current_weekly_report(
        self,
        request: ProductionHandlerRequest,
    ) -> ProductionHandlerOutcome:
        arguments = self._arguments(request, SubmitCurrentWeeklyReportArgs)
        trusted = self._trusted_target(request)
        self._require_argument_binding(
            trusted,
            report_id=arguments.report_id,
            expected_version=arguments.expected_version,
        )
        before = await self._load_live()
        self._require_live_matches(trusted, before)
        if before.status == "completed":
            return self._outcome(
                request,
                before=before,
                after=before,
                results=(),
                idempotency_key=self._tool_idempotency_key(request, trusted),
            )
        command = self._command(
            request,
            ordinal=0,
            command_type="submit_report",
            report=before,
            patch={},
        )
        results = await self._execute((command,))
        return self._outcome(
            request,
            before=before,
            after=results[-1].execution.after,
            results=results,
            idempotency_key=self._tool_idempotency_key(request, trusted),
        )

    def _trusted_target(
        self,
        request: ProductionHandlerRequest,
    ) -> TrustedPeriodicReportContext:
        bound = self._bound_calls.get(request.tool_call_id)
        if bound is None or bound.call.tool_name != request.tool_name:
            raise ProductionExecutionError("BOUND_TOOL_CALL_REQUIRED")
        target = bound.periodic_report
        if target is None:
            raise ProductionExecutionError("PERIODIC_REPORT_CONTEXT_REQUIRED")
        principal = self._context.principal
        if (
            target.tenant_id != principal.tenant_id
            or target.owner_user_id != principal.user_id
            or str(getattr(self._user, "id", "")) != str(principal.user_id)
        ):
            raise ProductionExecutionError("PERIODIC_REPORT_SCOPE_MISMATCH")
        return target

    async def _load_live(self) -> PeriodicReportSnapshot:
        return await self._execution_adapter.load_current_weekly(
            context=self._business_context(),
            anchor=self._context.now.astimezone(
                ZoneInfo(self._context.principal.timezone)
            ).date(),
        )

    @staticmethod
    def _require_argument_binding(
        trusted: TrustedPeriodicReportContext,
        *,
        report_id: UUID,
        expected_version: int,
    ) -> None:
        if report_id != trusted.report_id:
            raise ProductionExecutionError("PERIODIC_REPORT_BINDING_MISMATCH")
        if expected_version != trusted.version:
            raise ProductionExecutionError("PERIODIC_REPORT_VERSION_MISMATCH")

    @staticmethod
    def _require_live_matches(
        trusted: TrustedPeriodicReportContext,
        live: PeriodicReportSnapshot,
    ) -> None:
        if (
            live.report_id != trusted.report_id
            or live.owner_user_id != trusted.owner_user_id
            or live.report_type != trusted.report_type
            or live.period_key != trusted.period_key
            or live.version != trusted.version
            or live.status != trusted.status
        ):
            raise ProductionExecutionError("PERIODIC_REPORT_CONTEXT_STALE")

    async def _execute(
        self,
        commands: tuple[TypedPeriodicReportCommand, ...],
    ):
        try:
            results = tuple(
                await self._execution_adapter.execute(
                    commands=commands,
                    context=self._business_context(),
                )
            )
        except ValueError as exc:
            raise ProductionExecutionError("PERIODIC_REPORT_EXECUTION_BLOCKED") from exc
        blocked = next((item for item in results if item.status == "blocked"), None)
        if blocked is not None:
            raise ProductionExecutionError(
                str(blocked.execution.reason_code or "PERIODIC_REPORT_EXECUTION_BLOCKED")
            )
        return results

    def _command(
        self,
        request: ProductionHandlerRequest,
        *,
        ordinal: int,
        command_type: str,
        report: PeriodicReportSnapshot,
        patch: dict[str, Any],
        report_version: int | None = None,
        target_item_ids: tuple[str, ...] = (),
    ) -> TypedPeriodicReportCommand:
        identity = (
            f"{self._context.principal.tenant_id}:"
            f"{self._context.principal.source_message_id}:"
            f"{request.tool_call_id}:{ordinal}"
        )
        return TypedPeriodicReportCommand(
            command_id=uuid5(NAMESPACE_URL, f"{identity}:command"),
            decision_id=uuid5(NAMESPACE_URL, f"{identity}:decision"),
            sub_decision_id=uuid5(NAMESPACE_URL, f"{identity}:subdecision"),
            command_type=command_type,
            report_type="weekly",
            period_key=report.period_key,
            report_id=report.report_id,
            report_version=(
                report.version if report_version is None else report_version
            ),
            target_item_ids=target_item_ids,
            patch=patch,
            idempotency_key=(
                f"{self._tool_idempotency_key(request, self._context.current_weekly_report)}"
                f":periodic:{ordinal}"
            ),
        )

    def _business_context(self) -> BusinessCommandContext:
        principal = self._context.principal
        return BusinessCommandContext(
            tenant_id=principal.tenant_id,
            company_id=str(getattr(self._user, "company_id", "") or ""),
            department_id=str(getattr(self._user, "department_id", "") or ""),
            team_id=str(getattr(self._user, "team_id", "") or ""),
            actor_user_id=str(principal.user_id),
            actor_role_ids=(),
            allowed_case_ids=(),
            source_message_id=principal.source_message_id,
            source_channel=self._source_channel,
            occurred_at=self._context.now,
            conversation_id=principal.conversation_id,
            execution_started_at=self._context.now,
        )

    def _tool_idempotency_key(
        self,
        request: ProductionHandlerRequest,
        trusted: TrustedPeriodicReportContext,
    ) -> str:
        bound = self._bound_calls[request.tool_call_id]
        return build_write_idempotency_key(
            tenant_id=self._context.principal.tenant_id,
            user_id=str(self._context.principal.user_id),
            conversation_id=self._context.principal.conversation_id,
            source_message_id=self._context.principal.source_message_id,
            tool_call_id=request.tool_call_id,
            tool_name=request.tool_name,
            canonical_arguments={"tool_arguments": bound.arguments},
            target_object=str(trusted.report_id),
            expected_version=trusted.version,
        )

    def _outcome(
        self,
        request: ProductionHandlerRequest,
        *,
        before: PeriodicReportSnapshot,
        after: PeriodicReportSnapshot,
        results,
        idempotency_key: str | None,
    ) -> ProductionHandlerOutcome:
        outcomes = periodic_execution_outcomes(
            results,
            source_turn_id=self._context.principal.source_message_id,
        )
        changed = before != after
        receipt_ids = tuple(str(item.receipt_id) for item in results)
        facts = {
            "actual_write": changed,
            "periodic_report_snapshot": {
                "report_type": after.report_type,
                "period_key": after.period_key,
                "version": after.version,
                "status": after.status,
                "sections": {
                    key: list(values)
                    for key, values in after.sections.items()
                },
            },
        }
        if outcomes:
            facts["operation_outcome"] = outcomes[0].as_dict()
        return ProductionHandlerOutcome(
            target_type="periodic_report",
            target_id=str(after.report_id),
            before_report=None,
            after_report=None,
            before_version=before.version,
            after_version=after.version,
            idempotency_key=idempotency_key,
            typed_receipt_ids=receipt_ids,
            affected_item_ids=tuple(
                item_id
                for values in after.item_ids.values()
                for item_id in values
            ),
            safe_user_facts=facts,
            status_if_unchanged=(
                ReceiptStatus.SUCCESS
                if request.tool_name == "query_current_weekly_report"
                else ReceiptStatus.NO_OP
            ),
        )

    @staticmethod
    def _arguments(request: ProductionHandlerRequest, model_type):
        if not isinstance(request.arguments, model_type):
            raise ProductionExecutionError("TYPED_ARGUMENTS_REQUIRED")
        return request.arguments


class PeriodicReportExecutionPort(Protocol):
    async def load_current_weekly(
        self,
        *,
        context: BusinessCommandContext,
        anchor,
    ) -> PeriodicReportSnapshot: ...

    async def execute(
        self,
        *,
        commands: tuple[TypedPeriodicReportCommand, ...],
        context: BusinessCommandContext,
    ): ...


class SqlPeriodicReportExecutionAdapter:
    def __init__(self, session: Any) -> None:
        self._session = session

    async def load_current_weekly(
        self,
        *,
        context: BusinessCommandContext,
        anchor,
    ) -> PeriodicReportSnapshot:
        return await load_periodic_report_snapshot(
            self._session,
            context=context,
            report_type="weekly",
            anchor=anchor,
        )

    async def execute(
        self,
        *,
        commands: tuple[TypedPeriodicReportCommand, ...],
        context: BusinessCommandContext,
    ):
        return await execute_periodic_report_commands(
            self._session,
            commands=commands,
            context=context,
            execution_authority="tool_call_core_registry",
        )
