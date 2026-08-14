from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal, Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

from app.agent2.memory import PersonalMemoryModule, PersonalMemoryScope
from app.agent2.tool_calling.context import (
    CANARY_STATE_NAMESPACE,
    SHADOW_STATE_NAMESPACE,
    ToolCallStateNamespace,
    TrustedClearPending,
    TrustedContext,
    TrustedPrincipal,
    TrustedRecentMessage,
    TrustedRecentOperation,
    TrustedReportSnapshot,
    TrustedRuntimeIdentity,
)
from app.agent2.tool_calling.contracts import ExecutionMode
from app.agent2.tool_calling.registry import TOOL_REGISTRY, ToolDefinition


@dataclass(frozen=True)
class TrustedContextRequest:
    tenant_id: str
    user_id: UUID
    conversation_id: str
    source_message_id: str
    timezone: str
    server_now: datetime
    display_name: str | None = None
    runtime_provider_name: str | None = None
    runtime_model_name: str | None = None
    explicit_history_dates: tuple[date, ...] = ()
    conversation_kind: Literal["direct", "group", "unknown"] = "unknown"
    persisted_message_occurred_ats: tuple[datetime, ...] = ()

    def __post_init__(self) -> None:
        if not all((self.tenant_id, self.conversation_id, self.source_message_id, self.timezone)):
            raise ValueError("trusted context request requires authenticated scope and timezone")
        if self.server_now.tzinfo is None:
            raise ValueError("server_now must be timezone-aware")
        if self.conversation_kind not in {"direct", "group", "unknown"}:
            raise ValueError("trusted conversation kind is invalid")
        if any(
            value.tzinfo is None or value.utcoffset() is None
            for value in self.persisted_message_occurred_ats
        ):
            raise ValueError("persisted message times must be timezone-aware")
        if self.display_name is not None:
            normalized_name = self.display_name.strip()
            if not normalized_name or len(normalized_name) > 128:
                raise ValueError(
                    "trusted display name must contain 1 to 128 characters"
                )
            object.__setattr__(self, "display_name", normalized_name)
        runtime_values = (
            self.runtime_provider_name,
            self.runtime_model_name,
        )
        if any(value is not None for value in runtime_values):
            if not all(value is not None for value in runtime_values):
                raise ValueError(
                    "trusted runtime identity requires provider and model"
                )
            normalized_provider = str(self.runtime_provider_name).strip()
            normalized_model = str(self.runtime_model_name).strip()
            if (
                not normalized_provider
                or len(normalized_provider) > 64
                or not normalized_model
                or len(normalized_model) > 128
            ):
                raise ValueError("trusted runtime identity is invalid")
            object.__setattr__(
                self,
                "runtime_provider_name",
                normalized_provider,
            )
            object.__setattr__(
                self,
                "runtime_model_name",
                normalized_model,
            )

    def principal(self) -> TrustedPrincipal:
        return TrustedPrincipal(
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            conversation_id=self.conversation_id,
            source_message_id=self.source_message_id,
            timezone=self.timezone,
            display_name=self.display_name,
            conversation_kind=self.conversation_kind,
        )

    def runtime_identity(self) -> TrustedRuntimeIdentity | None:
        if (
            self.runtime_provider_name is None
            or self.runtime_model_name is None
        ):
            return None
        return TrustedRuntimeIdentity(
            provider_name=self.runtime_provider_name,
            model_name=self.runtime_model_name,
        )


class TrustedContextReadPort(Protocol):
    async def load_report(
        self,
        request: TrustedContextRequest,
        report_date: date,
    ) -> TrustedReportSnapshot | None: ...

    async def load_active_clear_pendings(
        self,
        request: TrustedContextRequest,
        *,
        namespace: str,
    ) -> tuple[TrustedClearPending, ...]: ...

    async def load_recent_messages(
        self,
        request: TrustedContextRequest,
        *,
        namespace: str,
        limit: int,
    ) -> tuple[TrustedRecentMessage, ...]: ...

    async def load_recent_operations(
        self,
        request: TrustedContextRequest,
        *,
        namespace: str,
        limit: int,
    ) -> tuple[TrustedRecentOperation, ...]: ...

class TrustedPolicyPort(Protocol):
    async def permission_allowed(
        self,
        request: TrustedContextRequest,
        definition: ToolDefinition,
    ) -> bool: ...

    async def gate_allowed(
        self,
        request: TrustedContextRequest,
        definition: ToolDefinition,
    ) -> bool: ...


class TrustedContextAssembler:
    def __init__(
        self,
        *,
        read_port: TrustedContextReadPort,
        policy_port: TrustedPolicyPort,
        recent_message_limit: int = 6,
        recent_operation_limit: int = 6,
        history_report_limit: int = 7,
        namespace: ToolCallStateNamespace = SHADOW_STATE_NAMESPACE,
        personal_memory_module: PersonalMemoryModule | None = None,
        weekly_plan_loader: Any | None = None,
        periodic_report_loader: Any | None = None,
    ) -> None:
        if (
            recent_message_limit < 0
            or recent_operation_limit < 0
            or history_report_limit < 0
        ):
            raise ValueError("trusted context limits must be non-negative")
        self._read_port = read_port
        self._policy_port = policy_port
        self._recent_message_limit = recent_message_limit
        self._recent_operation_limit = recent_operation_limit
        self._history_report_limit = history_report_limit
        self._namespace = namespace
        self._personal_memory_module = personal_memory_module
        self._weekly_plan_loader = weekly_plan_loader
        self._periodic_report_loader = periodic_report_loader

    async def assemble(self, request: TrustedContextRequest) -> TrustedContext:
        today = request.server_now.astimezone(ZoneInfo(request.timezone)).date()
        explicit_dates = tuple(
            item
            for item in dict.fromkeys(request.explicit_history_dates)
            if item != today
        )[: self._history_report_limit]
        today_report = _validate_loaded_report(
            request,
            today,
            await self._read_port.load_report(request, today),
        )
        historical_reports: list[TrustedReportSnapshot] = []
        for requested_date in explicit_dates:
            report = _validate_loaded_report(
                request, requested_date, await self._read_port.load_report(request, requested_date)
            )
            if report is not None:
                historical_reports.append(report)
        pending_candidates = await self._read_port.load_active_clear_pendings(
            request,
            namespace=self._namespace,
        )
        active_pending, pending_warnings = _select_active_pending(
            request,
            pending_candidates,
            namespace=self._namespace,
        )
        if active_pending is not None and not any(
            report is not None and report.report_id == active_pending.report_id
            for report in (today_report, *historical_reports)
        ):
            pending_report = _validate_loaded_report(
                request,
                active_pending.target_date,
                await self._read_port.load_report(request, active_pending.target_date),
            )
            if pending_report is not None:
                if pending_report.report_date == today:
                    today_report = pending_report
                else:
                    historical_reports.append(pending_report)
        recent = await self._read_port.load_recent_messages(
            request,
            namespace=self._namespace,
            limit=self._recent_message_limit,
        )
        recent_messages = tuple(recent[-self._recent_message_limit :]) if self._recent_message_limit else ()
        recent_operation_candidates = await self._read_port.load_recent_operations(
            request,
            namespace=self._namespace,
            limit=self._recent_operation_limit,
        )
        recent_operations = (
            tuple(recent_operation_candidates[-self._recent_operation_limit :])
            if self._recent_operation_limit
            else ()
        )
        reference_warnings: list[str] = []
        unique_report_references = {
            operation.report_reference.report_id
            for operation in recent_operations
            if operation.report_reference is not None
        }
        unambiguous_report_id = (
            next(iter(unique_report_references))
            if len(unique_report_references) == 1
            else None
        )
        validated_recent_operations: list[TrustedRecentOperation] = []
        loaded_report_ids = {
            report.report_id
            for report in (today_report, *historical_reports)
            if report is not None
        }
        loaded_report_dates = {
            report.report_date
            for report in (today_report, *historical_reports)
            if report is not None
        }
        for operation in recent_operations:
            reference = operation.report_reference
            if reference is None:
                validated_recent_operations.append(operation)
                continue
            if reference.report_id != unambiguous_report_id:
                validated_recent_operations.append(
                    operation.model_copy(update={"report_reference": None})
                )
                reference_warnings.append(
                    "recent_report_reference_ambiguous"
                )
                continue
            if (
                operation.status not in {"success", "no_op"}
                or operation.target_type != "daily_report"
                or operation.target_id != str(reference.report_id)
                or operation.after_version != reference.report_version
            ):
                validated_recent_operations.append(
                    operation.model_copy(update={"report_reference": None})
                )
                reference_warnings.append(
                    "recent_report_reference_mismatch"
                )
                continue
            report = _validate_loaded_report(
                request,
                reference.report_date,
                await self._read_port.load_report(
                    request,
                    reference.report_date,
                ),
            )
            if (
                report is None
                or report.report_id != reference.report_id
                or report.version != reference.report_version
                or report.status != reference.report_status
            ):
                validated_recent_operations.append(
                    operation.model_copy(update={"report_reference": None})
                )
                reference_warnings.append(
                    "recent_report_reference_mismatch"
                )
                continue
            validated_recent_operations.append(operation)
            if report.report_date == today:
                today_report = report
            elif (
                report.report_id not in loaded_report_ids
                and report.report_date not in loaded_report_dates
                and len(historical_reports) < self._history_report_limit
            ):
                historical_reports.append(report)
            loaded_report_ids.add(report.report_id)
            loaded_report_dates.add(report.report_date)
        recent_operations = tuple(validated_recent_operations)

        personal_memory = (
            await self._personal_memory_module.read_for_turn(
                PersonalMemoryScope(
                    tenant_id=request.tenant_id,
                    user_id=request.user_id,
                    now=request.server_now,
                )
            )
            if self._personal_memory_module is not None
            else None
        )
        execution_mode = (
            ExecutionMode.CANARY_EXECUTE
            if self._namespace == CANARY_STATE_NAMESPACE
            else ExecutionMode.SHADOW_PROPOSAL
        )
        mode_definitions = {
            name: definition
            for name, definition in TOOL_REGISTRY.items()
            if execution_mode in definition.enabled_modes
        }
        permission_results = {
            name: await self._policy_port.permission_allowed(request, definition)
            for name, definition in mode_definitions.items()
        }
        gate_decisions = {
            name: await self._policy_port.gate_allowed(request, definition)
            for name, definition in mode_definitions.items()
        }
        weekly_tool_names = {
            "query_next_weekly_plan",
            "apply_next_weekly_plan",
            "submit_next_weekly_plan",
            "record_weekly_plan_items_as_today_work",
        }
        weekly_plans: tuple[Any, ...] = ()
        if self._weekly_plan_loader is not None and any(
            permission_results.get(name) is True
            for name in weekly_tool_names
        ):
            load_targets = getattr(
                self._weekly_plan_loader,
                "load_targets",
                None,
            )
            if callable(load_targets):
                weekly_plans = tuple(await load_targets(request))
            else:
                # Keep adapters written for the original single-target seam
                # usable while production loaders move to the multi-target view.
                weekly_plans = (await self._weekly_plan_loader.load(request),)
        weekly_plan = weekly_plans[0] if weekly_plans else None
        current_weekly_report = None
        current_weekly_report_tool_names = {
            "query_current_weekly_report",
            "apply_current_weekly_report",
            "submit_current_weekly_report",
        }
        if self._periodic_report_loader is not None and any(
            permission_results.get(name) is True
            for name in current_weekly_report_tool_names
        ):
            current_weekly_report = (
                await self._periodic_report_loader.load_current_weekly(
                    tenant_id=request.tenant_id,
                    owner_user_id=request.user_id,
                    local_date=today,
                )
            )
        return TrustedContext(
            namespace=self._namespace,
            now=request.server_now,
            principal=request.principal(),
            runtime_identity=request.runtime_identity(),
            today_report=today_report,
            historical_reports=tuple(historical_reports),
            active_clear_pending=active_pending,
            recent_messages=recent_messages,
            recent_operations=recent_operations,
            personal_memory=personal_memory,
            current_weekly_report=current_weekly_report,
            weekly_plan=weekly_plan,
            weekly_plans=weekly_plans,
            allowed_tool_names=frozenset(
                name for name, allowed in permission_results.items() if allowed
            ),
            gate_decisions=gate_decisions,
            assembly_warnings=tuple(
                dict.fromkeys((*pending_warnings, *reference_warnings))
            ),
        )


def _validate_loaded_report(
    request: TrustedContextRequest,
    expected_date: date,
    report: TrustedReportSnapshot | None,
) -> TrustedReportSnapshot | None:
    if report is None:
        return None
    if (
        report.report_date != expected_date
        or report.tenant_id != request.tenant_id
        or report.owner_user_id != request.user_id
        or report.provenance != "trusted_context"
        or any(item.provenance != "trusted_context" for item in report.items)
    ):
        raise ValueError("read port returned a report outside the requested trusted scope")
    return report


def _select_active_pending(
    request: TrustedContextRequest,
    values: tuple[TrustedClearPending, ...],
    *,
    namespace: ToolCallStateNamespace = SHADOW_STATE_NAMESPACE,
) -> tuple[TrustedClearPending | None, tuple[str, ...]]:
    valid = tuple(
        item
        for item in values
        if item.namespace == namespace
        and item.tenant_id == request.tenant_id
        and item.user_id == request.user_id
        and item.conversation_id == request.conversation_id
        and not item.consumed
        and item.expires_at > request.server_now
    )
    if len(valid) == 1:
        return valid[0], ()
    if len(valid) > 1:
        return None, ("multiple_active_clear_pendings",)
    return None, ()
