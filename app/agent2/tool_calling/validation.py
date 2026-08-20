from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import re
from typing import Any, Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

from app.agent2.memory import TrustedPersonalMemory
from app.agent2.periodic_report_context import TrustedPeriodicReportContext
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
from app.agent2.weekly_plan_context import TrustedWeeklyPlanContext
from app.agent2.weekly_plan_date_binding import (
    WeeklyPlanDateBindingError,
    validate_weekly_plan_date_binding,
    validate_weekly_plan_date_set_binding,
)


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
    weekly_plan: TrustedWeeklyPlanContext | None = None
    periodic_report: TrustedPeriodicReportContext | None = None


class _UntrustedReadSnapshotError(ValueError):
    pass


_WEEKLY_SOURCE_COVERAGE_IGNORABLE = re.compile(
    r"[\s，,。；;：:、！？!?（）()]*"
)


def _weekly_source_fully_covered_by_operation_quotes(
    source_message: str,
    exact_clauses: tuple[str, ...],
) -> bool:
    source = str(source_message)
    unique_quotes = tuple(dict.fromkeys(exact_clauses))
    if not source or not unique_quotes:
        return False
    covered = [False] * len(source)
    for quote in unique_quotes:
        if not quote or source.count(quote) != 1:
            return False
        start = source.index(quote)
        for index in range(start, start + len(quote)):
            covered[index] = True
    residual = "".join(
        character
        for index, character in enumerate(source)
        if not covered[index]
    )
    return _WEEKLY_SOURCE_COVERAGE_IGNORABLE.fullmatch(residual) is not None


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

    @property
    def current_turn_source(self) -> CurrentTurnSource | None:
        return self._current_turn_source

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
        evidence_source = self._current_turn_source
        retry_candidate = None
        if (
            call.tool_name == "add_daily_items"
            and arguments.get("date_selection")
            == "trusted_failed_write"
        ):
            retry_candidate = self._context.retryable_daily_write
            if (
                retry_candidate is None
                or arguments.get("retry_candidate_id")
                != retry_candidate.candidate_id
            ):
                return None, failure_receipt(
                    call,
                    ReceiptStatus.BLOCKED,
                    "UNTRUSTED_DAILY_RETRY_CANDIDATE",
                )
            evidence_source = CurrentTurnSource(
                retry_candidate.source_messages
            )
        if evidence_source is not None:
            try:
                arguments = evidence_source.bind_tool_arguments(
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
        if (
            definition.read_or_write == "write"
            and definition.transaction_target_policy == "personal_memory"
            and arguments.get("memory_key")
            == "report.daily_reminders_enabled"
            and self._context.principal.conversation_kind != "direct"
        ):
            return None, failure_receipt(
                call,
                ReceiptStatus.BLOCKED,
                "PERSONAL_MEMORY_DIRECT_CONVERSATION_REQUIRED",
            )

        weekly_plan, weekly_failure = _validate_weekly_plan_binding(
            context=self._context,
            call=call,
            arguments=arguments,
            current_turn_source=self._current_turn_source,
        )
        if weekly_failure is not None:
            return None, weekly_failure
        periodic_report, periodic_failure = _validate_periodic_report_binding(
            context=self._context,
            call=call,
            arguments=arguments,
        )
        if periodic_failure is not None:
            return None, periodic_failure

        report: TrustedReportSnapshot | None = None
        source_report: TrustedReportSnapshot | None = None
        resolved_source_date: date | None = None
        date_facts: dict[str, Any] = {}
        # Agent2 owns the meaning of the current correction message.  The
        # server still binds both proposed dates to exact owned reports and
        # enforces target-conflict and transaction safeguards below.
        model_resolved_correction_dates = (
            call.tool_name == "correct_daily_report_date"
            and self._current_turn_source is not None
        )
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
                "date_candidate_matches": True,
                "date_resolution_basis": "server_default",
            }
        elif (
            call.tool_name == "add_daily_items"
            and arguments.get("date_selection") == "agent2_semantic"
        ):
            proposed_date = date.fromisoformat(str(arguments["proposed_date"]))
            local_today = self._context.now.astimezone(
                ZoneInfo(self._context.principal.timezone)
            ).date()
            default_date = default_daily_write_date(
                now=self._context.now,
                timezone=self._context.principal.timezone,
            )
            if proposed_date not in {default_date, local_today}:
                return None, failure_receipt(
                    call,
                    ReceiptStatus.BLOCKED,
                    "UNTRUSTED_SEMANTIC_REPORT_DATE",
                )
            try:
                report = await self.report_by_date(proposed_date)
            except _UntrustedReadSnapshotError:
                return None, failure_receipt(
                    call,
                    ReceiptStatus.BLOCKED,
                    "UNTRUSTED_READ_RESOURCE",
                )
            date_facts = {
                "resolved_date": proposed_date.isoformat(),
                "date_candidate_matches": True,
                "date_resolution_basis": "agent2_semantic",
            }
        elif (
            call.tool_name == "add_daily_items"
            and arguments.get("date_selection") == "trusted_report"
        ):
            parsed_report_id = UUID(str(arguments["report_id"]))
            report = self.report_by_id(parsed_report_id)
            if report is None:
                return None, failure_receipt(
                    call,
                    ReceiptStatus.BLOCKED,
                    "UNTRUSTED_REPORT_ID",
                )
            date_facts = {
                "resolved_date": report.report_date.isoformat(),
                "date_resolution_basis": "trusted_report_reference",
            }
        elif (
            call.tool_name == "add_daily_items"
            and arguments.get("date_selection")
            == "trusted_failed_write"
        ):
            assert retry_candidate is not None
            try:
                report = await self.report_by_date(
                    retry_candidate.target_date
                )
            except _UntrustedReadSnapshotError:
                return None, failure_receipt(
                    call,
                    ReceiptStatus.BLOCKED,
                    "UNTRUSTED_READ_RESOURCE",
                )
            if (
                (report is None) != retry_candidate.target_was_absent
                or (
                    report is not None
                    and report.version
                    != retry_candidate.target_version
                )
            ):
                return None, failure_receipt(
                    call,
                    ReceiptStatus.BLOCKED,
                    "DAILY_RETRY_TARGET_STALE",
                )
            date_facts = {
                "resolved_date": retry_candidate.target_date.isoformat(),
                "date_resolution_basis": "trusted_failed_write",
                "retry_candidate_id": retry_candidate.candidate_id,
                "retry_target_state_sha256": (
                    retry_candidate.target_state_sha256
                ),
                "retry_target_was_absent": (
                    retry_candidate.target_was_absent
                ),
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
            target_resolution = (
                DateResolution(
                    proposed_target_date,
                    candidate_matches=True,
                )
                if model_resolved_correction_dates
                else self._date_resolver.resolve(
                    expression=str(arguments["target_date_expression"]),
                    proposed_date=proposed_target_date,
                    now=self._context.now,
                    timezone=self._context.principal.timezone,
                )
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
            proposed_source_date = date.fromisoformat(
                str(arguments["proposed_source_date"])
            )
            source_resolution = (
                DateResolution(
                    proposed_source_date,
                    candidate_matches=True,
                )
                if model_resolved_correction_dates
                else self._date_resolver.resolve(
                    expression=str(arguments["source_date_expression"]),
                    proposed_date=proposed_source_date,
                    now=self._context.now,
                    timezone=self._context.principal.timezone,
                )
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
        if definition.object_binding_policy in {
            "server_today_owner_report",
            "trusted_weekly_plan_version_and_item_ids_to_server_today_report",
        }:
            report = self._context.today_report
        idempotent_date_correction_replay = False
        if (
            definition.object_binding_policy
            == "trusted_source_report_and_server_empty_target"
            and source_report is None
            and report is not None
            and _is_exact_date_correction_replay(
                context=self._context,
                definition=definition,
                target_report=report,
                date_facts=date_facts,
            )
        ):
            # The source is absent only because this exact provider turn
            # already moved the same report.  Binding the audited target lets
            # the existing receipt win, or the SQL executor return a no-op.
            source_report = report
            idempotent_date_correction_replay = True
            date_facts["idempotent_date_correction_replay"] = True
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
        report_id = (
            None
            if periodic_report is not None
            else arguments.get("report_id")
        )
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
        expected_version = (
            None
            if definition.object_binding_policy
            == "trusted_weekly_plan_version_and_item_ids_to_server_today_report"
            else arguments.get("expected_version")
        )
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
        if (
            item_report is not None
            and target_item_ids
            and definition.object_binding_policy
            != "trusted_weekly_plan_version_and_item_ids_to_server_today_report"
        ):
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

        receipt_bound_correction_state = (
            _immediate_owned_report_date_correction_state(
                context=self._context,
                definition=definition,
                source_report=source_report,
                date_facts=date_facts,
                arguments=arguments,
            )
        )
        if receipt_bound_correction_state is not None:
            date_facts["receipt_bound_source_state_sha256"] = (
                receipt_bound_correction_state
            )
        locked_report_date = _locked_historical_report_date(
            context=self._context,
            definition=definition,
            report=report,
            date_facts=date_facts,
            allow_receipt_bound_date_correction=(
                receipt_bound_correction_state is not None
                or idempotent_date_correction_replay
            ),
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
                    "historical_report_lock": {
                        "report_date": locked_report_date.isoformat(),
                        "cutoff_local_time": "09:00",
                        "automatically_unlocks": False,
                        "allowed_actions": ["query_report_by_date"],
                    },
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
                weekly_plan,
                periodic_report,
            ),
            None,
    )


def _validate_periodic_report_binding(
    *,
    context: TrustedContext,
    call: NativeToolCall,
    arguments: dict[str, Any],
) -> tuple[TrustedPeriodicReportContext | None, ToolReceipt | None]:
    if call.tool_name not in {
        "query_current_weekly_report",
        "apply_current_weekly_report",
        "submit_current_weekly_report",
    }:
        return None, None
    report = context.current_weekly_report
    if report is None:
        return None, failure_receipt(
            call,
            ReceiptStatus.BLOCKED,
            "PERIODIC_REPORT_CONTEXT_REQUIRED",
        )
    principal = context.principal
    local_date = context.now.astimezone(
        ZoneInfo(principal.timezone)
    ).date()
    iso_year, iso_week, _ = local_date.isocalendar()
    if (
        report.tenant_id != principal.tenant_id
        or report.owner_user_id != principal.user_id
        or report.report_type != "weekly"
        or report.period_key != f"{iso_year}-W{iso_week:02d}"
    ):
        return None, failure_receipt(
            call,
            ReceiptStatus.BLOCKED,
            "UNTRUSTED_PERIODIC_REPORT_CONTEXT",
        )
    if call.tool_name == "query_current_weekly_report":
        return report, None
    if str(arguments.get("report_id") or "") != str(report.report_id):
        return None, failure_receipt(
            call,
            ReceiptStatus.BLOCKED,
            "UNTRUSTED_PERIODIC_REPORT_ID",
        )
    if arguments.get("expected_version") != report.version:
        return None, failure_receipt(
            call,
            ReceiptStatus.BLOCKED,
            "STALE_PERIODIC_REPORT_VERSION",
        )
    if call.tool_name == "submit_current_weekly_report":
        return report, None
    trusted_item_ids = {item.item_id for item in report.items}
    for operation in arguments.get("operations") or ():
        if str(operation.get("operation") or "") not in {"edit", "delete"}:
            continue
        if str(operation.get("item_id") or "") not in trusted_item_ids:
            return None, failure_receipt(
                call,
                ReceiptStatus.BLOCKED,
                "UNTRUSTED_PERIODIC_REPORT_ITEM_ID",
            )
    return report, None


def _validate_weekly_plan_binding(
    *,
    context: TrustedContext,
    call: NativeToolCall,
    arguments: dict[str, Any],
    current_turn_source: CurrentTurnSource | None,
) -> tuple[TrustedWeeklyPlanContext | None, ToolReceipt | None]:
    """Bind every model-supplied weekly-plan pointer to server context.

    The language model may choose the user's intended operation, but it cannot
    invent a plan, version, date, item or suggestion.  This guard deliberately
    runs before any production executor is reached.
    """

    if call.tool_name not in {
        "query_next_weekly_plan",
        "apply_next_weekly_plan",
        "submit_next_weekly_plan",
        "record_weekly_plan_items_as_today_work",
    }:
        return None, None
    plan_id = str(arguments.get("plan_id") or "")
    if call.tool_name == "query_next_weekly_plan" and not plan_id:
        plans = context.all_weekly_plans()
        if len(plans) > 1:
            return None, failure_receipt(
                call,
                ReceiptStatus.BLOCKED,
                "WEEKLY_PLAN_QUERY_PLAN_ID_REQUIRED",
            )
        plan = plans[0] if plans else None
    else:
        plan = context.weekly_plan_by_id(plan_id)
    if plan is None:
        error_code = (
            "WEEKLY_PLAN_CONTEXT_REQUIRED"
            if not context.all_weekly_plans()
            else "UNTRUSTED_WEEKLY_PLAN_ID"
        )
        return None, failure_receipt(
            call, ReceiptStatus.BLOCKED, error_code
        )
    if call.tool_name == "query_next_weekly_plan":
        return plan, None

    if str(arguments.get("plan_id") or "") != plan.plan_id:
        return None, failure_receipt(
            call,
            ReceiptStatus.BLOCKED,
            "UNTRUSTED_WEEKLY_PLAN_ID",
        )
    if arguments.get("expected_version") != plan.version:
        return None, failure_receipt(
            call,
            ReceiptStatus.BLOCKED,
            "STALE_WEEKLY_PLAN_VERSION",
        )
    if call.tool_name == "submit_next_weekly_plan":
        return plan, None

    if call.tool_name == "record_weekly_plan_items_as_today_work":
        trusted_item_ids = {
            item.item_id for day in plan.days for item in day.items
        }
        target_item_ids = tuple(arguments.get("target_item_ids") or ())
        if any(item_id not in trusted_item_ids for item_id in target_item_ids):
            return None, failure_receipt(
                call,
                ReceiptStatus.BLOCKED,
                "UNTRUSTED_WEEKLY_PLAN_ITEM_ID",
            )
        return plan, None

    operations = tuple(arguments.get("operations") or ())
    trusted_dates = {day.plan_date.isoformat() for day in plan.days}
    trusted_item_ids = {
        item.item_id for day in plan.days for item in day.items
    }
    trusted_suggestion_ids = {
        suggestion.suggestion_id for suggestion in plan.suggestions
    }
    # A recurrence is represented by several ordinary add operations in one
    # atomic apply call.  Validate the shared current-message clause once as a
    # date set; validating each expanded add as if the user had named one day
    # is what previously rejected “每天做 X”.
    recurrent_add_groups: dict[
        tuple[int, str, str, str], list[tuple[int, dict[str, Any]]]
    ] = {}
    all_exact_clauses = tuple(
        str(evidence.get("exact_clause_quote") or "")
        for operation in operations
        if isinstance(operation, dict)
        and isinstance(
            (evidence := operation.get("source_evidence")),
            dict,
        )
        and evidence.get("exact_clause_quote")
    )
    for operation_index, operation in enumerate(operations):
        if str(operation.get("operation") or "") != "add":
            continue
        evidence = operation.get("source_evidence") or {}
        try:
            group_key = (
                int(evidence.get("source_message_index")),
                str(evidence.get("exact_clause_quote") or ""),
                str(evidence.get("recurrence_scope_quote") or ""),
                str(operation.get("content") or ""),
            )
        except (TypeError, ValueError):
            continue
        recurrent_add_groups.setdefault(group_key, []).append(
            (operation_index, operation)
        )

    date_set_validated_indexes: set[int] = set()
    for (
        source_message_index,
        exact_clause,
        recurrence_scope,
        content,
    ), grouped in (
        recurrent_add_groups.items()
    ):
        proposed_values = tuple(
            str(operation.get("plan_date") or "")
            for _index, operation in grouped
        )
        if len(grouped) < 2 or len(set(proposed_values)) < 2:
            continue
        if current_turn_source is None:
            return None, failure_receipt(
                call,
                ReceiptStatus.BLOCKED,
                "CURRENT_TURN_SOURCE_REQUIRED",
            )
        try:
            source_message = current_turn_source.messages[source_message_index - 1]
            source_occurred_at = current_turn_source.occurred_at_for(
                source_message_index
            )
            if source_occurred_at is None:
                return None, failure_receipt(
                    call,
                    ReceiptStatus.BLOCKED,
                    "WEEKLY_PLAN_SOURCE_TIME_REQUIRED",
                )
            proposed_dates = tuple(date.fromisoformat(value) for value in proposed_values)
            validate_weekly_plan_date_set_binding(
                source_message=source_message,
                exact_clause_quote=exact_clause,
                recurrence_scope_quote=recurrence_scope,
                matter_text=content,
                source_occurred_at=source_occurred_at,
                business_timezone=context.principal.timezone,
                target_week_start=plan.target_week_start,
                proposed_dates=proposed_dates,
                complete_source_coverage=(
                    _weekly_source_fully_covered_by_operation_quotes(
                        source_message,
                        all_exact_clauses,
                    )
                ),
                require_explicit_week_scope=len(
                    {
                        candidate.target_week_start
                        for candidate in context.all_weekly_plans()
                    }
                )
                > 1,
            )
        except WeeklyPlanDateBindingError as exc:
            clarification_facts = (
                {
                    "clarification_reason": "weekly_plan_target_week_ambiguous",
                    "possible_week_scopes": ["current_week", "next_week"],
                    "clarification_option_labels": ["本周", "下周"],
                    "must_ask_user": True,
                }
                if exc.code == "WEEKLY_PLAN_TARGET_WEEK_AMBIGUOUS"
                else None
            )
            return None, failure_receipt(
                call,
                (
                    ReceiptStatus.CLARIFICATION_REQUIRED
                    if exc.code == "WEEKLY_PLAN_TARGET_WEEK_AMBIGUOUS"
                    else ReceiptStatus.BLOCKED
                ),
                exc.code,
                safe_user_facts=clarification_facts,
            )
        except (IndexError, TypeError, ValueError):
            return None, failure_receipt(
                call,
                ReceiptStatus.BLOCKED,
                "WEEKLY_PLAN_DATE_EVIDENCE_MISMATCH",
            )
        date_set_validated_indexes.update(index for index, _operation in grouped)

    # A leading weekday can govern several parallel matters in one clause.  A
    # reviewer may quote the short first fragment for one item and the complete
    # clause for another.  Reuse only a larger, current-message quote that
    # contains the short quote and matter and that independently resolves to the
    # same proposed date.  This keeps incomplete standalone fragments blocked
    # while accepting the safely evidenced shared scope.
    shared_scope_validated_indexes: set[int] = set()
    add_operations = tuple(
        (index, operation)
        for index, operation in enumerate(operations)
        if str(operation.get("operation") or "") == "add"
    )
    if current_turn_source is not None:
        for operation_index, operation in add_operations:
            evidence = operation.get("source_evidence") or {}
            try:
                source_message_index = int(evidence.get("source_message_index"))
                source_message = current_turn_source.messages[
                    source_message_index - 1
                ]
                source_occurred_at = current_turn_source.occurred_at_for(
                    source_message_index
                )
                proposed = date.fromisoformat(str(operation.get("plan_date") or ""))
                short_quote = str(evidence.get("exact_clause_quote") or "")
                content = str(operation.get("content") or "")
            except (IndexError, TypeError, ValueError):
                continue
            if source_occurred_at is None:
                continue
            for _candidate_index, candidate in add_operations:
                candidate_evidence = candidate.get("source_evidence") or {}
                try:
                    candidate_source_index = int(
                        candidate_evidence.get("source_message_index")
                    )
                except (TypeError, ValueError):
                    continue
                candidate_quote = str(
                    candidate_evidence.get("exact_clause_quote") or ""
                )
                if (
                    candidate_source_index != source_message_index
                    or str(candidate.get("plan_date") or "")
                    != proposed.isoformat()
                    or not short_quote
                    or short_quote not in candidate_quote
                    or content not in candidate_quote
                ):
                    continue
                try:
                    validate_weekly_plan_date_binding(
                        source_message=source_message,
                        exact_clause_quote=candidate_quote,
                        source_occurred_at=source_occurred_at,
                        business_timezone=context.principal.timezone,
                        target_week_start=plan.target_week_start,
                        proposed_date=proposed,
                        require_explicit_week_scope=len(
                            {
                                candidate_plan.target_week_start
                                for candidate_plan in context.all_weekly_plans()
                            }
                        )
                        > 1,
                    )
                except WeeklyPlanDateBindingError:
                    continue
                shared_scope_validated_indexes.add(operation_index)
                break

    for operation_index, operation in enumerate(operations):
        operation_type = str(operation.get("operation") or "")
        plan_date = (
            operation.get("target_plan_date")
            if operation_type == "move"
            else operation.get("plan_date")
        )
        if plan_date is not None and str(plan_date) not in trusted_dates:
            return None, failure_receipt(
                call,
                ReceiptStatus.BLOCKED,
                "UNTRUSTED_WEEKLY_PLAN_DATE",
            )
        if operation_type in {
            "add",
            "move",
            "set_day_empty",
            "accept_suggestion",
        } and operation_index not in (
            date_set_validated_indexes | shared_scope_validated_indexes
        ):
            if current_turn_source is None:
                return None, failure_receipt(
                    call,
                    ReceiptStatus.BLOCKED,
                    "CURRENT_TURN_SOURCE_REQUIRED",
                )
            evidence = operation.get("source_evidence") or {}
            try:
                source_message_index = int(
                    evidence.get("source_message_index")
                )
                source_index = source_message_index - 1
                source_message = current_turn_source.messages[source_index]
                source_occurred_at = current_turn_source.occurred_at_for(
                    source_message_index
                )
                if source_occurred_at is None:
                    return None, failure_receipt(
                        call,
                        ReceiptStatus.BLOCKED,
                        "WEEKLY_PLAN_SOURCE_TIME_REQUIRED",
                    )
                proposed = date.fromisoformat(str(plan_date))
                validate_weekly_plan_date_binding(
                    source_message=source_message,
                    exact_clause_quote=str(
                        evidence.get("exact_clause_quote") or ""
                    ),
                    source_occurred_at=source_occurred_at,
                    business_timezone=context.principal.timezone,
                    target_week_start=plan.target_week_start,
                    proposed_date=proposed,
                    date_role=(
                        "move_target"
                        if operation_type == "move"
                        else "single_day"
                    ),
                    # On Monday both a late-fill current-week plan and the
                    # natural next-week plan may be writable.  The model still
                    # decides the user's intended operation; this deterministic
                    # guard only refuses to let a bare weekday silently choose
                    # between two trusted calendar weeks.
                    require_explicit_week_scope=len(
                        {
                            candidate.target_week_start
                            for candidate in context.all_weekly_plans()
                        }
                    ) > 1,
                )
            except WeeklyPlanDateBindingError as exc:
                clarification_facts = (
                    {
                        "clarification_reason": "weekly_plan_target_week_ambiguous",
                        "possible_week_scopes": ["current_week", "next_week"],
                        "clarification_option_labels": ["本周", "下周"],
                        "must_ask_user": True,
                    }
                    if exc.code == "WEEKLY_PLAN_TARGET_WEEK_AMBIGUOUS"
                    else None
                )
                return None, failure_receipt(
                    call,
                    (
                        ReceiptStatus.CLARIFICATION_REQUIRED
                        if exc.code == "WEEKLY_PLAN_TARGET_WEEK_AMBIGUOUS"
                        else ReceiptStatus.BLOCKED
                    ),
                    exc.code,
                    safe_user_facts=clarification_facts,
                )
            except (IndexError, TypeError, ValueError):
                return None, failure_receipt(
                    call,
                    ReceiptStatus.BLOCKED,
                    "WEEKLY_PLAN_DATE_EVIDENCE_MISMATCH",
                )
        if operation_type in {"edit", "move", "delete"} and str(
            operation.get("item_id") or ""
        ) not in trusted_item_ids:
            return None, failure_receipt(
                call,
                ReceiptStatus.BLOCKED,
                "UNTRUSTED_WEEKLY_PLAN_ITEM_ID",
            )
        if operation_type in {"accept_suggestion", "reject_suggestion"} and str(
            operation.get("suggestion_id") or ""
        ) not in trusted_suggestion_ids:
            return None, failure_receipt(
                call,
                ReceiptStatus.BLOCKED,
                "UNTRUSTED_WEEKLY_PLAN_SUGGESTION_ID",
            )
    return plan, None


def _locked_historical_report_date(
    *,
    context: TrustedContext,
    definition: Any,
    report: TrustedReportSnapshot | None,
    date_facts: dict[str, Any],
    allow_receipt_bound_date_correction: bool = False,
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
    if (
        report is not None
        and report.status == "completed"
        and getattr(definition, "tool_name", "")
        in {
            "edit_daily_items",
            "delete_daily_items",
            "move_daily_items",
            "confirm_report",
        }
    ):
        return None
    local_now = context.now.astimezone(
        ZoneInfo(context.principal.timezone)
    )
    if target_date >= local_now.date():
        return None
    if allow_receipt_bound_date_correction:
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


_RECENT_DATE_CORRECTION_MAX_AGE = timedelta(minutes=10)


def _is_exact_date_correction_replay(
    *,
    context: TrustedContext,
    definition: Any,
    target_report: TrustedReportSnapshot,
    date_facts: dict[str, Any],
) -> bool:
    """Accept only a zero-write replay backed by the atomic move audit."""

    reference = target_report.date_correction_reference
    if (
        getattr(definition, "tool_name", "")
        != "correct_daily_report_date"
        or context.principal.conversation_kind != "direct"
        or reference is None
        or reference.report_id != target_report.report_id
        or reference.source_message_id
        != context.principal.source_message_id
    ):
        return False
    try:
        source_date = date.fromisoformat(
            str(date_facts["resolved_source_date"])
        )
        target_date = date.fromisoformat(
            str(date_facts["resolved_target_date"])
        )
    except (KeyError, TypeError, ValueError):
        return False
    local_today = context.now.astimezone(
        ZoneInfo(context.principal.timezone)
    ).date()
    return (
        source_date == local_today
        and target_date == source_date - timedelta(days=1)
        and target_report.report_date == target_date
        and reference.source_report_date == source_date
        and reference.target_report_date == target_date
    )


def _immediate_owned_report_date_correction_state(
    *,
    context: TrustedContext,
    definition: Any,
    source_report: TrustedReportSnapshot | None,
    date_facts: dict[str, Any],
    arguments: dict[str, Any],
) -> str | None:
    """Recognize one narrow receipt-bound correction without interpreting text."""

    local_now = context.now.astimezone(
        ZoneInfo(context.principal.timezone)
    )
    if (
        getattr(definition, "tool_name", "")
        != "correct_daily_report_date"
        or context.principal.conversation_kind != "direct"
        or source_report is None
        or source_report.status not in {
            "collecting",
            "pending_confirmation",
        }
        or arguments.get("submit_after_correction") is not False
        or tuple(arguments.get("acknowledged_empty_fields") or ())
        or tuple(arguments.get("empty_field_evidence") or ())
    ):
        return None
    try:
        source_date = date.fromisoformat(
            str(date_facts["resolved_source_date"])
        )
        target_date = date.fromisoformat(
            str(date_facts["resolved_target_date"])
        )
    except (KeyError, TypeError, ValueError):
        return None
    if (
        source_report.report_date != source_date
        or source_date != local_now.date()
        or target_date != source_date - timedelta(days=1)
    ):
        return None

    if not context.recent_messages:
        return None
    latest_turn_id = context.recent_messages[-1].source_turn_id
    if latest_turn_id is None:
        return None
    candidates = tuple(
        operation
        for operation in context.recent_operations
        if (
            operation.source_message_id == latest_turn_id
            and operation.tool_name == "add_daily_items"
            and operation.status == "success"
            and operation.changed
            and operation.target_type == "daily_report"
            and operation.target_id == str(source_report.report_id)
            and operation.report_reference is not None
            and operation.report_reference.report_id
            == source_report.report_id
            and operation.report_reference.report_date == source_date
            and operation.report_reference.report_version
            == source_report.version
            and operation.report_reference.report_status
            == source_report.status
            and operation.report_reference.report_state_sha256
            == source_report.state_sha256
            and timedelta(0)
            <= context.now - operation.occurred_at
            <= _RECENT_DATE_CORRECTION_MAX_AGE
        )
    )
    if len(candidates) != 1:
        return None
    reference = candidates[0].report_reference
    assert reference is not None
    return reference.report_state_sha256


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
