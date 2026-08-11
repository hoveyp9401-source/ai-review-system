from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

from app.agent2.memory import TrustedPersonalMemory
from app.agent2.tool_calling.context import TrustedContext, TrustedReportSnapshot
from app.agent2.tool_calling.contracts import ExecutionMode, ReceiptStatus, ToolReceipt
from app.agent2.tool_calling.current_turn_source import (
    CurrentTurnSource,
    CurrentTurnSourceEvidenceError,
)
from app.agent2.tool_calling.registry import (
    TOOL_REGISTRY,
    ToolArgumentsValidationError,
    UnknownToolError,
    validate_tool_arguments,
)
from app.agent2.tool_calling.reporting_date import default_daily_write_date


@dataclass(frozen=True)
class NativeToolCall:
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]

    def __post_init__(self) -> None:
        if not self.tool_call_id or not self.tool_name or not isinstance(self.arguments, dict):
            raise ValueError("native tool call requires ID, name, and JSON-object arguments")


@dataclass(frozen=True)
class DateResolution:
    resolved_date: date | None
    candidate_matches: bool = False
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.resolved_date is None and not self.error_code:
            raise ValueError("unresolved server date requires an error code")


class DateResolverPort(Protocol):
    def resolve(
        self,
        *,
        expression: str,
        proposed_date: date,
        now: datetime,
        timezone: str,
    ) -> DateResolution: ...


class TrustedReportReadPort(Protocol):
    async def load_owned_report(
        self,
        *,
        tenant_id: str,
        user_id: UUID,
        report_date: date,
    ) -> TrustedReportSnapshot | None: ...


class UnavailableDateResolver:
    def resolve(self, **_: Any) -> DateResolution:
        return DateResolution(None, error_code="DATE_RESOLUTION_UNAVAILABLE")


@dataclass(frozen=True)
class BoundCall:
    call: NativeToolCall
    arguments: dict[str, Any]
    report: TrustedReportSnapshot | None
    target_item_ids: tuple[str, ...]
    source_report: TrustedReportSnapshot | None
    date_facts: dict[str, Any]
    memory: TrustedPersonalMemory | None = None


class _UntrustedReadSnapshotError(ValueError):
    pass


class ShadowCallBinder:
    def __init__(
        self,
        context: TrustedContext,
        date_resolver: DateResolverPort,
        report_read_port: TrustedReportReadPort | None,
        *,
        execution_mode: ExecutionMode = ExecutionMode.SHADOW_PROPOSAL,
        current_turn_source: CurrentTurnSource | None = None,
    ) -> None:
        self._context = context
        self._date_resolver = date_resolver
        self._report_read_port = report_read_port
        self._execution_mode = execution_mode
        self._current_turn_source = current_turn_source
        self._session_reports: dict[UUID, TrustedReportSnapshot] = {}
        self._batch_reports_by_date: dict[date, TrustedReportSnapshot] = {}

    def begin_batch(self) -> None:
        self._batch_reports_by_date = {}

    def promote_query_results(
        self,
        bound_calls: list[BoundCall],
        successful_call_ids: frozenset[str],
    ) -> None:
        for item in bound_calls:
            definition = TOOL_REGISTRY[item.call.tool_name]
            if (
                definition.read_or_write == "read"
                and definition.object_binding_policy == "server_resolved_owner_report"
                and item.call.tool_call_id in successful_call_ids
                and item.report is not None
            ):
                self._session_reports[item.report.report_id] = item.report
        self._batch_reports_by_date = {}


    def report_by_id(self, report_id: UUID) -> TrustedReportSnapshot | None:
        return self._session_reports.get(report_id) or self._context.report_by_id(report_id)

    async def report_by_date(self, report_date: date) -> TrustedReportSnapshot | None:
        existing = next(
            (item for item in self._session_reports.values() if item.report_date == report_date),
            None,
        ) or self._context.report_by_date(report_date) or self._batch_reports_by_date.get(report_date)
        if existing is not None or self._report_read_port is None:
            return existing
        principal = self._context.principal
        snapshot = await self._report_read_port.load_owned_report(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            report_date=report_date,
        )
        if snapshot is None:
            return None
        if (
            not isinstance(snapshot, TrustedReportSnapshot)
            or snapshot.tenant_id != principal.tenant_id
            or snapshot.owner_user_id != principal.user_id
            or snapshot.report_date != report_date
            or snapshot.provenance != "read_tool"
            or any(item.provenance != "read_tool" for item in snapshot.items)
        ):
            raise _UntrustedReadSnapshotError("read port returned an untrusted report snapshot")
        self._batch_reports_by_date[report_date] = snapshot
        return snapshot

    async def bind(self, call: NativeToolCall) -> tuple[BoundCall | None, ToolReceipt | None]:
        try:
            arguments = validate_tool_arguments(call.tool_name, call.arguments)
        except UnknownToolError:
            return None, failure_receipt(call, ReceiptStatus.FAILED, "UNKNOWN_TOOL")
        except ToolArgumentsValidationError as exc:
            return None, failure_receipt(
                call,
                ReceiptStatus.FAILED,
                "INVALID_TOOL_ARGUMENTS",
                validation_errors=exc.errors,
            )
        if self._current_turn_source is not None:
            try:
                self._current_turn_source.validate_tool_arguments(
                    call.tool_name,
                    arguments,
                )
            except CurrentTurnSourceEvidenceError as exc:
                return None, failure_receipt(
                    call,
                    ReceiptStatus.BLOCKED,
                    exc.code,
                )
        definition = TOOL_REGISTRY[call.tool_name]
        if self._execution_mode not in definition.enabled_modes:
            return None, failure_receipt(
                call,
                ReceiptStatus.BLOCKED,
                "TOOL_MODE_NOT_ENABLED",
            )
        if call.tool_name not in self._context.allowed_tool_names:
            return None, failure_receipt(call, ReceiptStatus.BLOCKED, "PERMISSION_DENIED")
        if (
            definition.read_or_write == "write"
            and self._context.gate_decisions.get(call.tool_name) is not True
        ):
            return None, failure_receipt(call, ReceiptStatus.BLOCKED, "GATE_BLOCKED")

        report: TrustedReportSnapshot | None = None
        source_report: TrustedReportSnapshot | None = None
        resolved_source_date: date | None = None
        date_facts: dict[str, Any] = {}
        if (
            call.tool_name == "add_daily_items"
            and arguments.get("date_selection") == "server_default"
        ):
            resolved_default = default_daily_write_date(
                now=self._context.now,
                timezone=self._context.principal.timezone,
            )
            try:
                report = await self.report_by_date(resolved_default)
            except _UntrustedReadSnapshotError:
                return None, failure_receipt(
                    call,
                    ReceiptStatus.BLOCKED,
                    "UNTRUSTED_READ_RESOURCE",
                )
            date_facts = {
                "resolved_date": resolved_default.isoformat(),
                "date_candidate_matches": (
                    str(arguments.get("proposed_date") or "")
                    == resolved_default.isoformat()
                ),
                "date_resolution_basis": "server_default",
            }
        elif "date_expression" in arguments:
            proposed_date = date.fromisoformat(str(arguments["proposed_date"]))
            resolution = self._date_resolver.resolve(
                expression=str(arguments["date_expression"]),
                proposed_date=proposed_date,
                now=self._context.now,
                timezone=self._context.principal.timezone,
            )
            if resolution.resolved_date is None:
                return None, failure_receipt(
                    call,
                    ReceiptStatus.CLARIFICATION_REQUIRED,
                    resolution.error_code or "DATE_RESOLUTION_FAILED",
                )
            try:
                report = await self.report_by_date(resolution.resolved_date)
            except _UntrustedReadSnapshotError:
                return None, failure_receipt(
                    call,
                    ReceiptStatus.BLOCKED,
                    "UNTRUSTED_READ_RESOURCE",
                )
            date_facts = {
                "resolved_date": resolution.resolved_date.isoformat(),
                "date_candidate_matches": resolution.candidate_matches,
                "date_resolution_basis": "user_explicit",
            }
        if "target_date_expression" in arguments:
            proposed_target_date = date.fromisoformat(
                str(arguments["proposed_target_date"])
            )
            target_resolution = self._date_resolver.resolve(
                expression=str(arguments["target_date_expression"]),
                proposed_date=proposed_target_date,
                now=self._context.now,
                timezone=self._context.principal.timezone,
            )
            if target_resolution.resolved_date is None:
                return None, failure_receipt(
                    call,
                    ReceiptStatus.CLARIFICATION_REQUIRED,
                    target_resolution.error_code or "DATE_RESOLUTION_FAILED",
                )
            try:
                report = await self.report_by_date(
                    target_resolution.resolved_date
                )
            except _UntrustedReadSnapshotError:
                return None, failure_receipt(
                    call,
                    ReceiptStatus.BLOCKED,
                    "UNTRUSTED_READ_RESOURCE",
                )
            date_facts.update(
                {
                    "resolved_target_date": (
                        target_resolution.resolved_date.isoformat()
                    ),
                    "target_date_candidate_matches": (
                        target_resolution.candidate_matches
                    ),
                }
            )
        if "source_date_expression" in arguments:
            proposed_source_date = date.fromisoformat(str(arguments["proposed_source_date"]))
            source_resolution = self._date_resolver.resolve(
                expression=str(arguments["source_date_expression"]),
                proposed_date=proposed_source_date,
                now=self._context.now,
                timezone=self._context.principal.timezone,
            )
            if source_resolution.resolved_date is None:
                return None, failure_receipt(
                    call,
                    ReceiptStatus.CLARIFICATION_REQUIRED,
                    source_resolution.error_code or "DATE_RESOLUTION_FAILED",
                )
            resolved_source_date = source_resolution.resolved_date
            try:
                source_report = await self.report_by_date(source_resolution.resolved_date)
            except _UntrustedReadSnapshotError:
                return None, failure_receipt(
                    call,
                    ReceiptStatus.BLOCKED,
                    "UNTRUSTED_READ_RESOURCE",
                )
            date_facts.update(
                {
                    "resolved_source_date": (
                        source_resolution.resolved_date.isoformat()
                    ),
                    "source_date_candidate_matches": (
                        source_resolution.candidate_matches
                    ),
                }
            )
            if definition.object_binding_policy in {
                "server_resolved_source_and_today_owner_reports",
                "trusted_previous_report_version_and_today_owner_report",
            }:
                report = self._context.today_report
            if (
                definition.object_binding_policy
                == "server_resolved_source_and_today_owner_reports"
                and source_report is None
            ):
                return None, failure_receipt(
                    call,
                    ReceiptStatus.BLOCKED,
                    "SOURCE_REPORT_NOT_FOUND",
                    safe_user_facts={
                        "source_report_date": (
                            source_resolution.resolved_date.isoformat()
                        ),
                    },
                )
        if definition.object_binding_policy == "server_today_owner_report":
            report = self._context.today_report
        if (
            definition.object_binding_policy
            == "trusted_source_report_and_server_empty_target"
            and source_report is None
            and report is not None
            and report.report_date
            == date.fromisoformat(
                str(date_facts.get("resolved_target_date") or "")
            )
        ):
            # A provider replay can arrive after the first transaction moved
            # the same report. Bind the stable report at the target so the
            # production receipt can win idempotently before any new write.
            source_report = report
        if (
            definition.object_binding_policy
            == "trusted_source_report_and_server_empty_target"
            and source_report is None
        ):
            return None, failure_receipt(
                call,
                ReceiptStatus.CLARIFICATION_REQUIRED,
                "SOURCE_REPORT_NOT_FOUND",
            )

        previous_binding = definition.object_binding_policy in {
            "trusted_previous_report_version_and_plan_item_ids",
            "trusted_previous_report_version_and_today_owner_report",
        }
        report_id = arguments.get("report_id")
        if report_id is not None:
            parsed_report_id = UUID(str(report_id))
            bound_report = (
                source_report
                if (
                    previous_binding
                    and source_report is not None
                    and source_report.report_id == parsed_report_id
                )
                else self.report_by_id(parsed_report_id)
            )
            if bound_report is None:
                return None, failure_receipt(call, ReceiptStatus.BLOCKED, "UNTRUSTED_REPORT_ID")
            if previous_binding:
                if (
                    resolved_source_date is not None
                    and bound_report.report_date != resolved_source_date
                ):
                    return None, failure_receipt(
                        call,
                        ReceiptStatus.BLOCKED,
                        "SOURCE_REPORT_DATE_MISMATCH",
                        report=bound_report,
                    )
                if source_report is not None and source_report.report_id != bound_report.report_id:
                    return None, failure_receipt(
                        call,
                        ReceiptStatus.BLOCKED,
                        "SOURCE_REPORT_MISMATCH",
                        report=bound_report,
                    )
                source_report = bound_report
                report = self._context.today_report
            else:
                report = bound_report
        expected_version = arguments.get("expected_version")
        version_report = source_report if previous_binding else report
        if (
            version_report is not None
            and expected_version is not None
            and expected_version != version_report.version
        ):
            return None, failure_receipt(
                call,
                ReceiptStatus.BLOCKED,
                "STALE_REPORT_VERSION",
                report=version_report,
            )

        target_item_ids = tuple(arguments.get("target_item_ids") or ())
        item_report = source_report if previous_binding else report
        if item_report is not None and target_item_ids:
            items = tuple(item_report.item(item_id) for item_id in target_item_ids)
            if any(item is None for item in items):
                return None, failure_receipt(
                    call,
                    ReceiptStatus.BLOCKED,
                    "UNTRUSTED_ITEM_ID",
                    report=item_report,
                )
            source_field = arguments.get("source_field")
            if source_field is not None and any(
                item.field != source_field for item in items if item is not None
            ):
                return None, failure_receipt(
                    call,
                    ReceiptStatus.BLOCKED,
                    "SOURCE_FIELD_MISMATCH",
                    report=item_report,
                )
            if previous_binding and any(
                item.field != "tomorrow_plan" for item in items if item is not None
            ):
                return None, failure_receipt(
                    call,
                    ReceiptStatus.BLOCKED,
                    "SOURCE_FIELD_MISMATCH",
                    report=item_report,
                )

        bound_memory = None
        if definition.transaction_target_policy == "personal_memory":
            key = arguments.get("memory_key")
            memory_context = self._context.personal_memory
            if isinstance(key, str) and memory_context is not None:
                bound_memory = next(
                    (
                        entry
                        for entry in memory_context.entries
                        if entry.memory_key == key
                    ),
                    None,
                )

        if definition.object_binding_policy == "unique_server_pending_full_scope_and_version":
            pending = self._context.active_clear_pending
            if pending is None:
                return None, failure_receipt(
                    call,
                    ReceiptStatus.CLARIFICATION_REQUIRED,
                    "CLEAR_PENDING_REQUIRED",
                )
            if pending.consumed or pending.expires_at <= self._context.now:
                return None, failure_receipt(
                    call,
                    ReceiptStatus.BLOCKED,
                    "CLEAR_PENDING_EXPIRED",
                )
            report = self.report_by_id(pending.report_id)
            if (
                report is None
                or report.version != pending.report_version
                or report.report_date != pending.target_date
            ):
                return None, failure_receipt(call, ReceiptStatus.BLOCKED, "CLEAR_PENDING_STALE")

        locked_report_date = _locked_historical_report_date(
            context=self._context,
            definition=definition,
            report=report,
            date_facts=date_facts,
        )
        if locked_report_date is not None:
            return None, failure_receipt(
                call,
                ReceiptStatus.BLOCKED,
                "HISTORICAL_REPORT_LOCKED_AFTER_CUTOFF",
                report=report,
                safe_user_facts={
                    "report_date": locked_report_date.isoformat(),
                    "locked_after": "09:00",
                },
            )

        return (
            BoundCall(
                call,
                arguments,
                report,
                target_item_ids,
                source_report,
                date_facts,
                bound_memory,
            ),
            None,
    )


def _locked_historical_report_date(
    *,
    context: TrustedContext,
    definition: Any,
    report: TrustedReportSnapshot | None,
    date_facts: dict[str, Any],
) -> date | None:
    if (
        definition.read_or_write != "write"
        or definition.permission_policy
        != "authenticated_report_owner_write"
    ):
        return None
    target_date = report.report_date if report is not None else None
    if target_date is None:
        resolved = date_facts.get("resolved_date") or date_facts.get(
            "resolved_target_date"
        )
        if isinstance(resolved, str):
            try:
                target_date = date.fromisoformat(resolved)
            except ValueError:
                return None
    if target_date is None:
        return None
    local_now = context.now.astimezone(
        ZoneInfo(context.principal.timezone)
    )
    if target_date >= local_now.date():
        return None
    if (
        definition.object_binding_policy
        == "server_resolved_owner_report"
    ):
        return None
    previous_date = local_now.date() - timedelta(days=1)
    if (
        target_date == previous_date
        and local_now.timetz().replace(tzinfo=None) < time(9)
    ):
        return None
    return target_date


def failure_receipt(
    call: NativeToolCall,
    status: ReceiptStatus,
    error_code: str,
    *,
    report: TrustedReportSnapshot | None = None,
    validation_errors: tuple[str, ...] = (),
    safe_user_facts: dict[str, Any] | None = None,
) -> ToolReceipt:
    return ToolReceipt(
        status=status,
        tool_name=call.tool_name,
        changed=False,
        target_type="daily_report" if report is not None else "",
        target_id=str(report.report_id) if report is not None else "",
        before_version=report.version if report is not None else None,
        after_version=report.version if report is not None else None,
        error_code=error_code,
        safe_user_facts={
            "actual_write": False,
            "execution_mode": ExecutionMode.SHADOW_PROPOSAL,
            "proposal_status": status,
            "error_code": error_code,
            **(safe_user_facts or {}),
        },
        execution_mode=ExecutionMode.SHADOW_PROPOSAL,
        would_change=False,
        validation_errors=validation_errors,
    )
