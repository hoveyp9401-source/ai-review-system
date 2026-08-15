from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from app.agent2.tool_calling import production_handlers
from app.agent2.tool_calling.contracts import (
    AddDailyItemsArgs,
    ApplyCurrentWeeklyReportArgs,
    ApplyNextWeeklyPlanArgs,
    CompletePreviousPlanArgs,
    ConfirmClearReportArgs,
    ConfirmReportArgs,
    CopyPreviousToTodayArgs,
    CorrectDailyReportDateArgs,
    DeleteDailyItemsArgs,
    EditDailyItemsArgs,
    ExecutionMode,
    ForgetPersonalMemoryArgs,
    MoveDailyItemsArgs,
    QueryCurrentWeeklyReportArgs,
    QueryDailyBriefingFactsArgs,
    QueryDefendantPerformanceArgs,
    QueryManagedDailyReportsArgs,
    QueryNextWeeklyPlanArgs,
    QueryPersonalMemoryArgs,
    QueryReportByDateArgs,
    QueryReportInsightsArgs,
    QueryTodayReportArgs,
    RecordWeeklyPlanItemsAsTodayWorkArgs,
    RememberPersonalMemoryArgs,
    RequestClearReportArgs,
    SubmitCurrentWeeklyReportArgs,
    SubmitNextWeeklyPlanArgs,
    ToolReceipt,
)
from app.agent2.tool_calling.handlers import (
    simulate_add,
    simulate_complete_previous,
    simulate_confirm,
    simulate_confirm_clear,
    simulate_copy,
    simulate_correct_report_date,
    simulate_delete,
    simulate_edit,
    simulate_move,
    simulate_query,
    simulate_request_clear,
)
from app.agent2.tool_calling.managed_daily_handlers import (
    simulate_daily_briefing_fact_query,
    simulate_managed_daily_query,
    simulate_report_insight_query,
)
from app.agent2.tool_calling.memory_handlers import (
    simulate_memory_forget,
    simulate_memory_query,
    simulate_memory_remember,
)
from app.agent2.tool_calling.performance_handlers import (
    simulate_defendant_performance_query,
)
from app.agent2.tool_calling.sandbox_handlers import (
    execute_add_daily_items,
    execute_complete_previous_plan,
    execute_confirm_clear_report,
    execute_confirm_report,
    execute_copy_previous_to_today,
    execute_correct_daily_report_date,
    execute_delete_daily_items,
    execute_edit_daily_items,
    execute_forget_personal_memory,
    execute_move_daily_items,
    execute_query_personal_memory,
    execute_query_report_by_date,
    execute_query_today_report,
    execute_remember_personal_memory,
    execute_request_clear_report,
)

ReadOrWrite = Literal["read", "write"]
RiskLevel = Literal["low", "medium", "high"]
ToolHandler = Callable[..., Any]
ConflictPolicy = Literal["exact_targets", "item_content", "broad_target"]
TransactionTargetPolicy = Literal[
    "read_only",
    "bound_report",
    "resolved_report",
    "today_report",
    "pending_report",
    "personal_memory",
    "source_and_target_reports",
    "weekly_plan",
    "periodic_report",
]


class ToolRegistryError(ValueError):
    pass


class UnknownToolError(ToolRegistryError):
    def __init__(self, tool_name: str) -> None:
        super().__init__(f"unknown tool: {tool_name}")
        self.tool_name = tool_name


class ToolArgumentsValidationError(ToolRegistryError):
    def __init__(self, tool_name: str, errors: tuple[str, ...]) -> None:
        super().__init__(f"invalid arguments for {tool_name}: {'; '.join(errors)}")
        self.tool_name = tool_name
        self.errors = errors


@dataclass(frozen=True)
class ToolDefinition:
    tool_name: str
    description: str
    input_schema: Mapping[str, Any]
    read_or_write: ReadOrWrite
    risk_level: RiskLevel
    permission_policy: str
    object_binding_policy: str
    date_resolution_policy: str
    confirmation_policy: str
    idempotency_policy: str
    transaction_policy: str
    transaction_target_policy: TransactionTargetPolicy
    sandbox_handler: ToolHandler
    production_handler: ToolHandler
    conflict_policy: ConflictPolicy
    pending_ttl_seconds: int | None
    receipt_schema: Mapping[str, Any]
    shadow_handler: ToolHandler
    enabled_modes: frozenset[ExecutionMode]
    input_model: type[BaseModel]


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value




def _shadow_deferred(*_: Any, **__: Any) -> None:
    raise RuntimeError("shadow handlers are available only through ShadowRuntime")


def _sandbox_handler_required(*_: Any, **__: Any) -> None:
    raise RuntimeError("sandbox handler registration is required")


def _production_handler_required(*_: Any, **__: Any) -> None:
    raise RuntimeError("production handler registration is required")


def _production_mode_disabled(*_: Any, **__: Any) -> None:
    raise RuntimeError("tool is not enabled for production execution")


_CURRENT_TURN_WRITE_AUTHORITY = (
    " Only the current user_message may authorize this write. recent_messages cannot "
    "independently supply the operation, target meaning, or new content. A current "
    "user_message may explicitly adopt or correct one uniquely identified, immediately "
    "preceding user-authored report draft; that current message supplies renewed write "
    "authority and the target. This never applies to assistant-authored text, multiple "
    "candidate drafts, or an unbound historical request. If a required semantic value "
    "otherwise comes only from history, do not call this tool; ask for clarification."
)

_COMPLETED_OWNER_CONTENT_WRITE = (
    " Supports the authenticated owner's trusted completed report directly and "
    "preserves completed status; no reopen or revoke is required for this content change."
)


def _definition(
    name: str,
    description: str,
    model: type[BaseModel],
    read_or_write: ReadOrWrite,
    risk: RiskLevel,
    permission: str,
    binding: str,
    date_policy: str,
    confirmation: str = "none",
    idempotency: str = "none_for_read",
    transaction: str = "read_only",
    transaction_target: TransactionTargetPolicy = "bound_report",
    sandbox_handler: ToolHandler = _sandbox_handler_required,
    production_handler: ToolHandler = _production_handler_required,
    shadow_handler: ToolHandler = _shadow_deferred,
    conflict: ConflictPolicy = "exact_targets",
    pending_ttl_seconds: int | None = None,
    enabled_modes: frozenset[ExecutionMode] | None = None,
) -> ToolDefinition:
    return ToolDefinition(
        tool_name=name,
        description=(
            description + _CURRENT_TURN_WRITE_AUTHORITY
            if read_or_write == "write"
            else description
        ),
        input_schema=_freeze_json(model.model_json_schema()),
        read_or_write=read_or_write,
        risk_level=risk,
        permission_policy=permission,
        object_binding_policy=binding,
        date_resolution_policy=date_policy,
        confirmation_policy=confirmation,
        idempotency_policy=idempotency,
        transaction_policy=transaction,
        transaction_target_policy=transaction_target,
        sandbox_handler=sandbox_handler,
        production_handler=production_handler,
        conflict_policy=conflict,
        pending_ttl_seconds=pending_ttl_seconds,
        receipt_schema=_freeze_json(ToolReceipt.model_json_schema()),
        shadow_handler=shadow_handler,
        enabled_modes=(
            frozenset(
                {
                    ExecutionMode.SHADOW_PROPOSAL,
                    ExecutionMode.SANDBOX_EXECUTE,
                    ExecutionMode.CANARY_EXECUTE,
                }
            )
            if enabled_modes is None
            else enabled_modes
        ),
        input_model=model,
    )


_OWNER_READ = "authenticated_report_owner_read"
_OWNER_WRITE = "authenticated_report_owner_write"
_WRITE_KEY = "server_canonical_write_key_v1"
_ATOMIC = "same_report_atomic"
_MEMORY_MODES = frozenset(
    {
        ExecutionMode.SHADOW_PROPOSAL,
        ExecutionMode.SANDBOX_EXECUTE,
        ExecutionMode.CANARY_EXECUTE,
    }
)
_UNIQUE_ITEM_WRITE = (
    " Use only when the user's requested operation and every target item are uniquely "
    "bound by trusted context. Do not infer a missing operation or target from recency "
    "or from there being only one available item. The current user_message itself must "
    "authorize this operation; conversation history cannot supply it. Otherwise ask for "
    "clarification without calling this tool."
)

# The only declaration site for names and tool metadata. Every consumer derives from this mapping.
TOOL_REGISTRY = MappingProxyType(
    {
        "query_today_report": _definition(
            "query_today_report",
            "Return today's trusted report snapshot with stable item IDs. Use only when the "
            "current user_message explicitly requests retrieval or display of a report or "
            "record. This is not a default confirmation, truth-checking, or wording-review "
            "tool. Do not call merely "
            "to interpret an ambiguous current user_message, and do not duplicate a trusted "
            "snapshot already injected. Never use this as a preparatory call before "
            "clarification or current-report confirmation when that snapshot is already present.",
            QueryTodayReportArgs, "read", "low", _OWNER_READ, "server_today_owner_report",
            "server_today_in_user_timezone",
            transaction_target="read_only",
            sandbox_handler=execute_query_today_report,
            production_handler=production_handlers.execute_query_today_report,
            shadow_handler=simulate_query,
        ),
        "query_current_weekly_report": _definition(
            "query_current_weekly_report",
            "Return the authenticated owner's retrospective Weekly Report for the "
            "server-derived current ISO week, with stable item IDs and exact version. "
            "Use for viewing, opening, or continuing 本周周报. This is the report of "
            "work already done, risks, metrics, and follow-up plan; it is never the "
            "Monday-to-Saturday 下周工作计划. The model supplies no owner, week, or date.",
            QueryCurrentWeeklyReportArgs,
            "read",
            "low",
            "authenticated_owner_current_weekly_report",
            "server_current_week_owner_periodic_report",
            "server_current_iso_week_in_user_timezone",
            transaction_target="read_only",
            production_handler=(
                production_handlers.execute_query_current_weekly_report
            ),
            enabled_modes=frozenset({ExecutionMode.CANARY_EXECUTE}),
        ),
        "apply_current_weekly_report": _definition(
            "apply_current_weekly_report",
            "Apply one ordered atomic batch of append, edit, or delete operations to "
            "the authenticated owner's retrospective Weekly Report for the exact "
            "server-derived current week. Copy the trusted report_id/version and stable "
            "item IDs. New or replacement text must be copied from the current user "
            "message. Never use for a daily report or the Monday-to-Saturday 下周工作计划.",
            ApplyCurrentWeeklyReportArgs,
            "write",
            "medium",
            "authenticated_owner_current_weekly_report",
            "trusted_current_week_periodic_report_version_and_items",
            "server_current_iso_week_in_user_timezone",
            idempotency=_WRITE_KEY,
            transaction="same_periodic_report_atomic",
            transaction_target="periodic_report",
            production_handler=(
                production_handlers.execute_apply_current_weekly_report
            ),
            conflict="broad_target",
            enabled_modes=frozenset({ExecutionMode.CANARY_EXECUTE}),
        ),
        "submit_current_weekly_report": _definition(
            "submit_current_weekly_report",
            "Submit the authenticated owner's retrospective Weekly Report for the "
            "exact server-derived current week only when the current user message "
            "explicitly confirms submission. Copy trusted report_id/version. This does "
            "not submit a daily report or a 下周工作计划.",
            SubmitCurrentWeeklyReportArgs,
            "write",
            "medium",
            "authenticated_owner_current_weekly_report",
            "trusted_current_week_periodic_report_version",
            "server_current_iso_week_in_user_timezone",
            confirmation="formal_current_turn_confirmation_call",
            idempotency=_WRITE_KEY,
            transaction="same_periodic_report_atomic",
            transaction_target="periodic_report",
            production_handler=(
                production_handlers.execute_submit_current_weekly_report
            ),
            conflict="broad_target",
            enabled_modes=frozenset({ExecutionMode.CANARY_EXECUTE}),
        ),
        "query_next_weekly_plan": _definition(
            "query_next_weekly_plan",
            "Return the authenticated owner's selected server-bound Monday-to-Saturday "
            "Weekly Work Plan, including stable plan, day, item and suggestion identifiers "
            "and the exact current version. Trusted context can expose multiple exact "
            "targets: active_collection may be the current week open for Monday late fill, "
            "while natural_next may be the following week. This legacy tool name does not "
            "make every target 'next week'. It never queries a retrospective Weekly Report. "
            "When multiple exact targets exist, the model must copy the selected trusted "
            "plan_id; omission is allowed only for one-target compatibility. The model never "
            "supplies an owner ID or guesses the target week.",
            QueryNextWeeklyPlanArgs,
            "read",
            "low",
            "authenticated_owner_weekly_plan_read",
            "server_bound_owner_next_weekly_plan",
            "server_next_week_monday_in_user_timezone",
            transaction_target="read_only",
            production_handler=(
                production_handlers.execute_query_next_weekly_plan
            ),
            enabled_modes=frozenset({ExecutionMode.CANARY_EXECUTE}),
        ),
        "apply_next_weekly_plan": _definition(
            "apply_next_weekly_plan",
            "Apply one ordered, atomic batch of additions, edits, moves, deletions, explicit "
            "empty-day decisions, undated target-week suggestion captures, suggestion "
            "acceptances, or suggestion rejections to the "
            "authenticated owner's selected exact weekly-plan target. Trusted context may "
            "contain multiple exact active_collection and natural_next targets, including "
            "current-week Monday late fill and a following-week plan. This is a legacy tool "
            "name: choose semantically, then copy that target's plan_id and exact version. "
            "Never use this tool for a retrospective Weekly Report. Every operation uses "
            "stable identifiers, and every operation carries current-message evidence. New "
            "or replacement content must come from that current user message; conversation "
            "history may bind existing targets but cannot supply new content. Execute all "
            "operations or none, and never infer a missing target or day. Every operation "
            "that assigns a formal day must cite one complete exact current-message clause "
            "containing that day and matter so the server can independently verify the date. "
            "For one bounded every-day or weekday-range recurrence, emit one add per exact "
            "selected date and copy the entire current user message as exact_clause_quote "
            "plus the same complete "
            "recurrence_scope_quote. Include every attached bound, exception, or qualifier in "
            "that scope quote; never shorten a restricted phrase to only 'every day'. If one leading date "
            "governs several clearly parallel matters, keep every matter as a separate add "
            "on that shared date and reuse the complete governing clause. "
            "For capture_suggestion, content must be one exact contiguous excerpt from the "
            "current user message, including any alternative days or uncertainty qualifiers; "
            "never summarize or normalize it.",
            ApplyNextWeeklyPlanArgs,
            "write",
            "medium",
            "authenticated_owner_weekly_plan_write",
            "trusted_weekly_plan_version_and_operations",
            "trusted_exact_plan_dates_within_next_week",
            idempotency=_WRITE_KEY,
            transaction="same_weekly_plan_atomic",
            transaction_target="weekly_plan",
            production_handler=(
                production_handlers.execute_apply_next_weekly_plan
            ),
            conflict="broad_target",
            enabled_modes=frozenset({ExecutionMode.CANARY_EXECUTE}),
        ),
        "submit_next_weekly_plan": _definition(
            "submit_next_weekly_plan",
            "Submit the authenticated owner's selected exact weekly-plan target only when "
            "the current user message explicitly confirms that target's complete preview. "
            "Trusted context may expose multiple exact active_collection and natural_next "
            "targets. This legacy tool name does not force the following week: copy the "
            "selected target's stable plan_id and exact version. Never submit or alter a "
            "retrospective Weekly Report with this tool, and do not reconstruct, add, or alter "
            "plan content while submitting.",
            SubmitNextWeeklyPlanArgs,
            "write",
            "medium",
            "authenticated_owner_weekly_plan_write",
            "trusted_weekly_plan_version_and_operations",
            "trusted_next_week_snapshot",
            confirmation="formal_current_turn_confirmation_call",
            idempotency=_WRITE_KEY,
            transaction="same_weekly_plan_atomic",
            transaction_target="weekly_plan",
            production_handler=(
                production_handlers.execute_submit_next_weekly_plan
            ),
            conflict="broad_target",
            enabled_modes=frozenset({ExecutionMode.CANARY_EXECUTE}),
        ),
        "record_weekly_plan_items_as_today_work": _definition(
            "record_weekly_plan_items_as_today_work",
            "Record one or more exact committed Weekly Work Plan items as work also done "
            "today. Use this only when the current user message semantically adopts exact "
            "stable items from one trusted weekly_plan_target; copy its plan_id, exact "
            "current version, and stable item IDs. Do not provide, summarize, prefix, or "
            "rewrite Daily Report content: the server copies each selected original_text "
            "verbatim into today's today_work and deduplicates identical text. This does "
            "not alter the Weekly Work Plan or prove that any unselected item was done. "
            "Here today is the authenticated user's current local calendar date. Call this "
            "tool only when the model has semantically confirmed that exact date. During "
            "deep overnight hours, if 今天 could still mean the reporting day that just "
            "ended, ask which date instead of calling this fixed-today tool. "
            "If more than one distinct trusted matter could be meant, ask which one and "
            "call no write tool.",
            RecordWeeklyPlanItemsAsTodayWorkArgs,
            "write",
            "medium",
            "authenticated_owner_weekly_plan_write",
            "trusted_weekly_plan_version_and_item_ids_to_server_today_report",
            "server_today_in_user_timezone",
            idempotency=_WRITE_KEY,
            transaction=_ATOMIC,
            transaction_target="today_report",
            production_handler=(
                production_handlers.execute_record_weekly_plan_items_as_today_work
            ),
            conflict="item_content",
            enabled_modes=frozenset({ExecutionMode.CANARY_EXECUTE}),
        ),
        "query_report_by_date": _definition(
            "query_report_by_date",
            "Resolve a date on the server and return the owned report snapshot. Use only when "
            "the current user_message explicitly requests retrieval or display of a report or "
            "record, or explicitly asks to change content in a dated owned report whose trusted "
            "snapshot is not already injected. In that second case, call this read tool first; "
            "after its successful trusted result, call the exact content-write tool in the next "
            "model loop. This is not a default confirmation, truth-checking, or wording-review "
            "tool. Do not call "
            "merely to interpret an ambiguous current user_message, and do not duplicate a "
            "trusted snapshot already injected. Never use this as a preparatory call before "
            "clarification or current-report confirmation when that snapshot is already present.",
            QueryReportByDateArgs, "read", "low", _OWNER_READ, "server_resolved_owner_report",
            "server_expression_authoritative_proposal_untrusted",
            transaction_target="read_only",
            sandbox_handler=execute_query_report_by_date,
            production_handler=production_handlers.execute_query_report_by_date,
            shadow_handler=simulate_query,
        ),
        "query_daily_briefing_facts": _definition(
            "query_daily_briefing_facts",
            "Read the system's recorded scheduled daily-briefing facts for "
            "one exact report date. Use this when the current user asks what a "
            "morning briefing said, whether or when it was sent, why its "
            "member classification differs from the report now visible, or "
            "whether two briefing copies disagreed. This tool reads the "
            "historical outbound message, delivery evidence, the structured "
            "at-generation member snapshot when available, and the member's "
            "current report metadata. It never manufactures a historical "
            "snapshot and never infers a cause from the later report alone. "
            "One member_classification call already returns the matching "
            "recorded message, member snapshot, current state, and delivery "
            "fields. For one person/date discrepancy, call it once and do not "
            "also call recipient_delivery or query_report_by_date merely to "
            "recheck the current submission. Use recipient_delivery only when "
            "the user separately asks whether a named recipient received the "
            "briefing. If multiple dates are explicit, emit all necessary "
            "date calls together in one tool batch. "
            "Every active authenticated user may read these facts inside the "
            "current tenant. Copy exact person, recipient, and team names from "
            "the current user_message; never invent IDs or use fuzzy matches. "
            "Omit member_name only when member_classification refers to the "
            "authenticated user, and omit recipient_name only when "
            "recipient_delivery refers to that user. Supply both date fields "
            "for an explicit date or a date uniquely selected by Agent2 from "
            "trusted recent dialogue and a server-verified report reference. If "
            "neither exists, omit both so the server can request a date "
            "clarification rather than silently using today.",
            QueryDailyBriefingFactsArgs,
            "read",
            "low",
            "authenticated_tenant_briefing_fact_read",
            "server_tenant_filtered_exact_briefing_targets",
            "server_expression_or_conversation_focus",
            transaction_target="read_only",
            production_handler=(
                production_handlers.execute_query_daily_briefing_facts
            ),
            shadow_handler=simulate_daily_briefing_fact_query,
            enabled_modes=frozenset(
                {
                    ExecutionMode.SHADOW_PROPOSAL,
                    ExecutionMode.CANARY_EXECUTE,
                }
            ),
        ),
        "query_managed_daily_reports": _definition(
            "query_managed_daily_reports",
            "Read another employee's report, one team's reports, missing submissions, "
            "or the department summary for exactly one calendar date. This is a "
            "single-date submission/snapshot tool only. Never use it for recent work, "
            "this-week or previous-week work, multi-day summaries, historical report "
            "counts, recent attention, or unclosed work; use query_report_insights for "
            "all of those. Every active authenticated user may read these "
            "facts inside the current tenant. Names are untrusted references: never "
            "invent IDs or choose a fuzzy match. The server resolves exact members and "
            "teams from tenant-filtered data. Preserve every explicit scope from the "
            "current user_message in the tool arguments: copy an explicitly named "
            "person to member_name and an explicitly named team or '中心直属' to "
            "team_name. For missing_submissions, omit team_name only when the user "
            "asks for the whole center, whole department, or all people. Omit the "
            "date pair to use the server's "
            "current date; when supplied, proposed_report_date is only an untrusted "
            "candidate. When Agent2 semantically determines that the current message "
            "continues an earlier read whose exact date is present in trusted recent "
            "operation evidence, supply both date fields with that date instead of "
            "defaulting to today. The server never chooses that continuation from the "
            "wording itself. Do not use "
            "this tool for the authenticated user's own ordinary "
            "report query when an owner-read tool applies. Never use this tool "
            "for performance, KPI, defendant-case metrics, case stock, case additions, "
            "case closures, or loss-reduction questions; use "
            "query_defendant_performance for those.",
            QueryManagedDailyReportsArgs,
            "read",
            "low",
            "authenticated_tenant_daily_read",
            "server_tenant_filtered_exact_daily_target",
            "server_expression_authoritative_today_default",
            transaction_target="read_only",
            production_handler=(
                production_handlers.execute_query_managed_daily_reports
            ),
            shadow_handler=simulate_managed_daily_query,
            enabled_modes=frozenset(
                {
                    ExecutionMode.SHADOW_PROPOSAL,
                    ExecutionMode.CANARY_EXECUTE,
                }
            ),
        ),
        "query_report_insights": _definition(
            "query_report_insights",
            "Read historical daily-report insights after the current user_message has "
            "semantically asked for them. Use report_count for one person's total saved "
            "or completed reports; recent_work for one person's work over a bounded period; "
            "period_work for an organization's current-week or previous-week summary; "
            "recent_attention for an organization's recent risks and follow-ups; and "
            "unclosed_work for plans that were mentioned earlier but have no later matching "
            "completed-work record. For organization unclosed work, a later matching work "
            "record from any member closes the plan. For unclosed_work only, use "
            "period_type=unspecified when the user gives no time range, "
            "period_type=recent_30_days for the latest month or latest 30 days, and "
            "period_type=all_history only when the user explicitly asks for all history "
            "or all records to date. Every authenticated active user may "
            "read these facts inside the current tenant. Names are untrusted references: "
            "the server resolves people and organizations from tenant-filtered data and "
            "never accepts model-provided IDs. Normalize only an unambiguous shorthand to "
            "the complete official organization name. For a question asking for both this "
            "week and last week, call this read tool once for each period in the same turn. "
            "Do not use this for today's raw member report, missing-submission status, "
            "a single-date department snapshot, performance metrics, defendant cases, "
            "or any report write. For any recent, weekly, last-week, cross-history, "
            "attention, or unclosed question, this is the required read tool.",
            QueryReportInsightsArgs,
            "read",
            "low",
            "authenticated_tenant_daily_read",
            "server_tenant_filtered_exact_insight_scope",
            "server_period_from_typed_query_and_user_timezone",
            transaction_target="read_only",
            production_handler=(
                production_handlers.execute_query_report_insights
            ),
            shadow_handler=simulate_report_insight_query,
            enabled_modes=frozenset(
                {
                    ExecutionMode.SHADOW_PROPOSAL,
                    ExecutionMode.CANARY_EXECUTE,
                }
            ),
        ),
        "query_defendant_performance": _definition(
            "query_defendant_performance",
            "Read the current published defendant-case performance report. Use this for "
            "questions about defendant performance, KPI completion, case stock, case "
            "additions, case closures, year-on-year change, or loss reduction. This is "
            "not a daily-report tool and never writes a daily report. Choose view=week "
            "for this-week questions and view=month for this-month questions; when the "
            "user does not specify week or month, default to month. Choose "
            "scope_type=department for the legal department overall, scope_type=team "
            "for one team, and scope_type=self only when the user explicitly asks for "
            "their own result. Team queries require the complete official team name "
            "as it appears in the user's organization; normalize an unambiguous team "
            "shorthand to the complete Chinese team name, but never guess between "
            "multiple teams. Choose mode=explain_new when asked why additions are high "
            "or how additions were calculated; explain_stock for stock composition or "
            "calculation; explain_loss for comprehensive loss-reduction rate; "
            "explain_substantial for substantial loss-reduction amount; otherwise use "
            "summary. The server selects exact permission-filtered scopes and performs "
            "all calculations deterministically.",
            QueryDefendantPerformanceArgs,
            "read",
            "low",
            "authenticated_tenant_performance_read",
            "server_published_exact_performance_scope",
            "server_current_period_by_view",
            transaction_target="read_only",
            production_handler=(
                production_handlers.execute_query_defendant_performance
            ),
            shadow_handler=simulate_defendant_performance_query,
            enabled_modes=frozenset(
                {
                    ExecutionMode.SHADOW_PROPOSAL,
                    ExecutionMode.CANARY_EXECUTE,
                }
            ),
        ),
        "add_daily_items": _definition(
            "add_daily_items",
            "Propose independent asserted entries about the authenticated user's actual completed "
            "work, current problem or risk, or definite plan. Do not turn a negated event, "
            "condition, hypothesis, quotation, attribution, source report, or question into the "
            "user's own asserted fact. However, when the user's reported work is attending, "
            "recording, communicating, or summarizing a meeting or source, preserve the "
            "explicitly supplied attributed details as attributed content instead of discarding "
            "them or converting them into the user's own claim. Use one complete items array for "
            "all explicitly supplied daily-report "
            "fields and independent matters. When the current user_message semantically and "
            "unambiguously states that a specific report field intentionally has no content, put "
            "that field in acknowledged_empty_fields instead of inventing or storing a textual "
            "item. This semantic choice belongs to the model; never derive it from a keyword list "
            "or from an omitted field. Set date_selection=server_default and omit date_expression, "
            "proposed_date, and date_evidence when the current user does not explicitly name a "
            "calendar date; the server then uses the previous day "
            "before 09:00 and the current day from 09:00 onward. This is a reporting-day prior, "
            "and genuinely new same-morning work is rare, but it is not a hard lock. After "
            "midnight, a relative word such as 今天 alone can still refer "
            "to the workday that just ended; judge it from trusted local time, the whole current "
            "message and recent dialogue. Distinguish a time word inside reported work from a "
            "reference to the report being edited: 今天完成了什么 can still belong to the prior "
            "workday. In deep overnight hours immediately after midnight, even 今天这份日报 or "
            "今天的明日计划 ordinarily continues the workday just ended unless the new calendar "
            "date or newly completed new-day work is clear. Closer to the morning cutoff, that "
            "same clear report/section target can instead select the local-date report. Use "
            "date_selection=agent2_semantic only "
            "when that "
            "semantic judgment confidently selects one of the safe semantic date candidates in "
            "daily_reporting_context without a clear calendar-date assignment, and attach an "
            "exact current-message quote as date_evidence. Omit date_expression in this mode; if "
            "you redundantly repeat a relative phrase there, it does not replace the proposed "
            "date or the server's safe-candidate check. This lets genuinely new early-morning "
            "work enter the new day's report while preserving the previous-day prior. Set "
            "user_explicit only when the current user_message clearly assigns a calendar date to "
            "the report itself, such as an exact date, and attach that assignment as date_evidence. "
            "If the report date remains genuinely ambiguous, ask naturally. Set "
            "date_selection=trusted_report with that report_id and expected_version only when "
            "your semantic reading of the current message and recent dialogue uniquely selects "
            "one server-verified report_reference already present in trusted context. The "
            "reference is evidence, not an automatic focus; if more than one report is plausible, "
            "ask instead. Set submit_after_write=true "
            "only when this same current message explicitly asks to submit the resulting report; "
            "the server will apply content, empty-section acknowledgements and submission in one "
            "transaction. Do not pair that call with confirm_report. Every proposed item or "
            "empty-field acknowledgement must "
            "carry source evidence with a one-based current-message index; the server binds that "
            "index to the original current-message text. For every proposed item, also copy the "
            "smallest complete contiguous current-message passage that states that one matter into "
            "source_evidence.exact_quote. The quote must be contiguous user-authored text and cover "
            "the complete meaning of that item. Never drop a negation, condition, deadline, "
            "consequence, exception, qualifier, or required decision even when punctuation or "
            "whitespace separates it. Exclude report-date lead-ins and section labels only when "
            "doing so cannot change the item's meaning. The server persists that exact "
            "server-owned passage; "
            "content is only Agent2's semantic interpretation for field selection and review, so "
            "professional wording cleanup can never introduce text into the stored report. Never "
            "invent, normalize, reorder, or truncate exact_quote. Emit one independently editable "
            "action or object per item; a compound sentence must not hide two separate actions in "
            "one item. The source spans for separate items must not overlap; each span authorizes "
            "only its own persisted item. A second action with a different object is a separate item "
            "even when a comma or coordinating word joins it to the first action. By contrast, one "
            "action about a relationship between two objects remains one item. Keep all conditions, "
            "deadlines, consequences, exceptions, and pending "
            "decisions attached to their governing risk item. Every acknowledged empty field "
            "must also "
            "have one "
            "matching empty_field_evidence entry. Evidence never comes from conversation history. "
            "For quoted source text, do not copy quotation delimiters into source evidence. Never "
            "normalize curly quotation marks into "
            "unescaped ASCII double quotes. Preserve attribution in content with safe wording "
            "such as a colon when needed. "
            "The proposed meaning must come from the current "
            "user_message or one uniquely adopted, immediately preceding user-authored report "
            "draft that the current user_message explicitly binds to the target report. Never "
            "write report content from history alone. Every independently asserted matter, "
            "whether general or specific, must be represented. Review every ordered current-message "
            "fragment and every numbered or punctuation-separated assertion semantically before "
            "calling the tool. Do not collapse detailed source content into a headline. "
            "Professional wording cleanup must preserve actors, dates, deadlines, quantities, "
            "alternatives, attribution, and explicitly named subjects; it must not omit, add, or "
            "change meaning. If the intended split or destination field is uncertain, ask naturally "
            "before calling the tool. Contingent possibilities are not "
            "asserted facts or definite plans. A target field defined as identical to another "
            "field must contain the referenced concrete items, not a relational placeholder. "
            "A stated current problem and its related future response are separate matters. "
            "Include a related future response only when it is definite; exclude a contingent "
            "response. Never replace the current problem with its future response."
            + _COMPLETED_OWNER_CONTENT_WRITE,
            AddDailyItemsArgs, "write", "medium", _OWNER_WRITE, "server_resolved_owner_report",
            "server_expression_authoritative_proposal_untrusted", idempotency=_WRITE_KEY, transaction=_ATOMIC,
            transaction_target="resolved_report",
            sandbox_handler=execute_add_daily_items,
            production_handler=production_handlers.execute_add_daily_items,
            shadow_handler=simulate_add,
            conflict="item_content",
        ),
        "edit_daily_items": _definition(
            "edit_daily_items",
            "Replace trusted stable item IDs at an exact report version. The replacement must "
            "faithfully preserve the new content intended by the current user_message. Minimal "
            "professional cleanup is allowed only when it does not add, remove, or change "
            "meaning. Do not replace it with another item's content merely because the "
            "replacement resembles an item label or ordinal. Trusted context may resolve the "
            "target but cannot supply replacement content unless the current user_message "
            "explicitly requests copying content from an existing item. Do not edit an item "
            "into content already held by another trusted item. Use delete_daily_items instead "
            "when the requested destination content already exists as another uniquely bound "
            "item in the same field."
            + _UNIQUE_ITEM_WRITE
            + _COMPLETED_OWNER_CONTENT_WRITE,
            EditDailyItemsArgs, "write", "medium", _OWNER_WRITE, "trusted_report_version_and_item_ids",
            "trusted_snapshot_date", idempotency=_WRITE_KEY, transaction=_ATOMIC,
            sandbox_handler=execute_edit_daily_items,
            production_handler=production_handlers.execute_edit_daily_items,
            shadow_handler=simulate_edit,
        ),
        "delete_daily_items": _definition(
            "delete_daily_items",
            "Delete trusted item IDs; deletion is never an empty edit. Remove an obsolete "
            "trusted item while retaining desired content that already exists as another "
            "trusted item. When the uniquely bound destination already exists in the same "
            "field, retain it and delete only the source item."
            + _UNIQUE_ITEM_WRITE
            + _COMPLETED_OWNER_CONTENT_WRITE,
            DeleteDailyItemsArgs, "write", "medium", _OWNER_WRITE, "trusted_report_version_and_item_ids",
            "trusted_snapshot_date", idempotency=_WRITE_KEY, transaction=_ATOMIC,
            sandbox_handler=execute_delete_daily_items,
            production_handler=production_handlers.execute_delete_daily_items,
            shadow_handler=simulate_delete,
        ),
        "move_daily_items": _definition(
            "move_daily_items",
            "Move trusted items between explicit report fields. This one tool supports moving "
            "one item, multiple selected items, or every trusted item currently in an "
            "explicitly named source field. When the current user_message explicitly moves an "
            "entire source field to an explicit target field, bind all trusted item IDs in that "
            "source field. Do not query again or ask which items when the injected trusted "
            "snapshot already contains the complete source field."
            + _UNIQUE_ITEM_WRITE
            + _COMPLETED_OWNER_CONTENT_WRITE,
            MoveDailyItemsArgs, "write", "medium", _OWNER_WRITE,
            "trusted_report_version_items_and_source_field", "trusted_snapshot_date",
            idempotency=_WRITE_KEY, transaction=_ATOMIC,
            sandbox_handler=execute_move_daily_items,
            production_handler=production_handlers.execute_move_daily_items,
            shadow_handler=simulate_move,
        ),
        "copy_previous_to_today": _definition(
            "copy_previous_to_today",
            "Copy the authenticated user's complete report from the model-resolved source date "
            "into today. The model supplies only the source-date meaning; the server binds the "
            "owned source report, its current version, and today's target report. "
            "When the same "
            "user request also adds a new independent item, pair this call with add_daily_items "
            "in the same initial write batch against the same trusted pre-write today snapshot. "
            "In shadow this does not produce an intermediate trusted version.",
            CopyPreviousToTodayArgs, "write", "medium", _OWNER_WRITE,
            "server_resolved_source_and_today_owner_reports",
            "server_source_expression_and_server_today",
            idempotency=_WRITE_KEY, transaction=_ATOMIC,
            transaction_target="today_report",
            sandbox_handler=execute_copy_previous_to_today,
            production_handler=production_handlers.execute_copy_previous_to_today,
            shadow_handler=simulate_copy,
            conflict="broad_target",
        ),
        "correct_daily_report_date": _definition(
            "correct_daily_report_date",
            "Correct the date of one exact trusted owned daily report when the current "
            "user_message explicitly says the just-recorded report belongs to a different "
            "date. The model supplies only source and target date expressions, any report "
            "fields the user explicitly stated are empty, and whether the same current "
            "message explicitly asks to submit. The server binds the report identity and "
            "version, rejects an occupied or ambiguous target date, and executes relocation, "
            "empty-section acknowledgement and optional submission as one transaction. Do "
            "not pair this tool with add_daily_items or confirm_report in the same turn. Every "
            "acknowledged empty field must have one matching empty_field_evidence entry whose "
            "one-based index binds to the current user message; conversation history cannot "
            "supply an empty-field assertion.",
            CorrectDailyReportDateArgs,
            "write",
            "medium",
            _OWNER_WRITE,
            "trusted_source_report_and_server_empty_target",
            "server_source_and_target_expressions",
            idempotency=_WRITE_KEY,
            transaction=_ATOMIC,
            transaction_target="source_and_target_reports",
            sandbox_handler=execute_correct_daily_report_date,
            production_handler=production_handlers.execute_correct_daily_report_date,
            shadow_handler=simulate_correct_report_date,
            conflict="broad_target",
        ),
        "complete_previous_plan": _definition(
            "complete_previous_plan",
            "Record selected trusted previous-plan items as completed today. report_id, "
            "expected_version, and target_item_ids must bind to the source previous report and "
            "source report version; today's target report is resolved by the server.",
            CompletePreviousPlanArgs, "write", "medium", _OWNER_WRITE,
            "trusted_previous_report_version_and_plan_item_ids", "server_source_expression_and_server_today",
            idempotency=_WRITE_KEY, transaction=_ATOMIC,
            transaction_target="today_report",
            sandbox_handler=execute_complete_previous_plan,
            production_handler=production_handlers.execute_complete_previous_plan,
            shadow_handler=simulate_complete_previous,
        ),
        "confirm_report": _definition(
            "confirm_report",
            "Confirm the authenticated user's exact trusted report version only when the user "
            "explicitly confirms or submits one unique daily report already present in trusted "
            "report context. This may be today's report or a focused historical report. For a "
            "historical report, the current message must explicitly confirm/submit it, directly "
            "reference the unique immediately preceding report, or answer the assistant's date "
            "clarification; never reject solely because the report is historical. "
            "Never use for an unrelated submission. Use this tool only when the trusted report "
            "is already structurally complete before the current message. If the current message "
            "also supplies any missing section, including an explicit empty section, use one "
            "add_daily_items call with submit_after_write=true instead; do not discard that new "
            "evidence and do not split the two operations. Do not reconstruct content from conversation "
            "history. Do not substitute add_daily_items because the trusted snapshot is empty. "
            "Let the server Receipt decide whether an empty report can be confirmed. Call this "
            "tool directly without a preparatory read when the trusted current report snapshot "
            "is already injected. Never infer a post-proposal version or call after another "
            "write proposal in the same shadow turn.",
            ConfirmReportArgs, "write", "medium", _OWNER_WRITE, "trusted_report_and_exact_version",
            "trusted_snapshot_date", "formal_current_turn_confirmation_call", _WRITE_KEY, _ATOMIC,
            sandbox_handler=execute_confirm_report,
            production_handler=production_handlers.execute_confirm_report,
            shadow_handler=simulate_confirm,
        ),
        "request_clear_report": _definition(
            "request_clear_report",
            "Propose a bound clear Pending; never clear report data. End this user turn awaiting "
            "confirmation in a later independent user turn.",
            RequestClearReportArgs, "write", "high", _OWNER_WRITE, "trusted_report_and_exact_version",
            "trusted_snapshot_date", "server_bound_clear_pending_required", _WRITE_KEY, "pending_only_atomic",
            pending_ttl_seconds=600,
            sandbox_handler=execute_request_clear_report,
            production_handler=production_handlers.execute_request_clear_report,
            shadow_handler=simulate_request_clear,
        ),
        "confirm_clear_report": _definition(
            "confirm_clear_report",
            "Consume the unique valid trusted active clear Pending already present at the start "
            "of this independent user turn, only when it is unconsumed and expires_at is later "
            "than trusted now; the model supplies no Pending ID. Never call in the same turn as "
            "a clear request and never create a missing, consumed, or expired Pending.",
            ConfirmClearReportArgs, "write", "high", "authenticated_clear_pending_consumer",
            "unique_server_pending_full_scope_and_version", "pending_target_date",
            "consume_unique_unexpired_clear_pending", _WRITE_KEY, _ATOMIC,
            transaction_target="pending_report",
            sandbox_handler=execute_confirm_clear_report,
            production_handler=production_handlers.execute_confirm_clear_report,
            shadow_handler=simulate_confirm_clear,
        ),
        "query_personal_memory": _definition(
            "query_personal_memory",
            "Return the authenticated user's safe active response preferences. Server-rendered "
            "preference values are reported only as configured and never exposed as model "
            "instructions. Use only when the current user_message explicitly asks what the "
            "system remembers or asks to review saved preferences. This tool never returns "
            "internal memory IDs, versions, source message IDs, permissions, legal identity "
            "facts, or business-object facts. A saved assistant.preferred_name names the "
            "assistant for this authenticated user only; it is never the user's form of address.",
            QueryPersonalMemoryArgs,
            "read",
            "low",
            "authenticated_personal_memory_read",
            "server_authenticated_user_memory_scope",
            "none",
            transaction_target="read_only",
            sandbox_handler=execute_query_personal_memory,
            production_handler=production_handlers.execute_query_personal_memory,
            shadow_handler=simulate_memory_query,
            enabled_modes=_MEMORY_MODES,
        ),
        "remember_personal_memory": _definition(
            "remember_personal_memory",
            "Create or replace one server-supported response preference for the authenticated "
            "user only when the current user_message explicitly states that durable preference "
            "or explicitly asks the system to remember it. Use only the enumerated memory_key "
            "and its matching structured value. Never store business records, tasks, free-form "
            "instructions, legal identity, permissions, internal IDs, quoted third-party "
            "preferences, or facts inferred from conversation history. A preferred form of "
            "address explicitly chosen by the authenticated user is an allowed response "
            "preference; do not infer one from their legal name or from another person's text. "
            "An assistant name explicitly assigned by this user is also allowed under "
            "assistant.preferred_name. Keep it separate from response.preferred_salutation, "
            "and never advertise or solicit this naming ability. Every call must carry "
            "source_evidence containing the one-based current-message index and an intent whose "
            "assistant/user role matches memory_key. For assistant names and user salutations, the "
            "stored value itself must appear in that server-bound current message. A vocative, "
            "thanks, question, or third-party quotation is not an assignment. "
            "When the current user_message "
            "explicitly contrasts both roles (the assistant's name and the user's form of "
            "address), call this tool separately for both keys even if either value already "
            "appears configured; the server will safely return no-op for an unchanged value.",
            RememberPersonalMemoryArgs,
            "write",
            "low",
            "authenticated_personal_memory_write",
            "server_authenticated_user_memory_key",
            "none",
            idempotency=_WRITE_KEY,
            transaction="personal_memory_atomic",
            transaction_target="personal_memory",
            sandbox_handler=execute_remember_personal_memory,
            production_handler=production_handlers.execute_remember_personal_memory,
            shadow_handler=simulate_memory_remember,
            conflict="broad_target",
            enabled_modes=_MEMORY_MODES,
        ),
        "forget_personal_memory": _definition(
            "forget_personal_memory",
            "Deactivate one server-supported response preference for the authenticated user "
            "only when the current user_message explicitly asks to forget or reset that "
            "preference. The model supplies only the enumerated memory_key; the server binds "
            "the authenticated tenant, user, current record, version, and audit facts. "
            "For assistant.preferred_name, forgetting restores the default assistant name 小律 "
            "for this user and does not change the user's preferred salutation.",
            ForgetPersonalMemoryArgs,
            "write",
            "low",
            "authenticated_personal_memory_write",
            "server_authenticated_user_memory_key",
            "none",
            idempotency=_WRITE_KEY,
            transaction="personal_memory_atomic",
            transaction_target="personal_memory",
            sandbox_handler=execute_forget_personal_memory,
            production_handler=production_handlers.execute_forget_personal_memory,
            shadow_handler=simulate_memory_forget,
            conflict="broad_target",
            enabled_modes=_MEMORY_MODES,
        ),
    }
)


def deepseek_tool_schemas(
    allowed_names: Iterable[str] | None = None,
    *,
    mode: ExecutionMode | None = None,
) -> list[dict[str, Any]]:
    selected = set(TOOL_REGISTRY) if allowed_names is None else set(allowed_names)
    unknown = selected - set(TOOL_REGISTRY)
    if unknown:
        raise UnknownToolError(min(unknown))
    return [
        {"type": "function", "function": {
            "name": item.tool_name, "description": item.description, "parameters": _thaw_json(item.input_schema),
        }}
        for name, item in TOOL_REGISTRY.items()
        if name in selected
        and (mode is None or mode in item.enabled_modes)
    ]


def validate_tool_arguments(tool_name: str, arguments: Any) -> dict[str, Any]:
    definition = TOOL_REGISTRY.get(tool_name)
    if definition is None:
        raise UnknownToolError(tool_name)
    if not isinstance(arguments, dict):
        raise ToolArgumentsValidationError(tool_name, ("arguments must be a JSON object",))
    try:
        validated = definition.input_model.model_validate(arguments)
    except ValidationError as exc:
        errors = tuple(
            f"{'.'.join(str(part) for part in item['loc']) or '<root>'}: {item['msg']}"
            for item in exc.errors()
        )
        raise ToolArgumentsValidationError(tool_name, errors) from exc
    return validated.model_dump(mode="json")


def dispatcher_tool_names() -> tuple[str, ...]:
    return tuple(TOOL_REGISTRY)


def replay_tool_names() -> tuple[str, ...]:
    return tuple(TOOL_REGISTRY)


def risk_and_permission_metadata() -> dict[str, dict[str, str]]:
    return {
        name: {
            "read_or_write": item.read_or_write,
            "risk_level": item.risk_level,
            "permission_policy": item.permission_policy,
        }
        for name, item in TOOL_REGISTRY.items()
    }


def registry_contract_digest(
    allowed_names: Iterable[str] | None = None,
) -> str:
    selected_names = (
        set(TOOL_REGISTRY)
        if allowed_names is None
        else set(allowed_names)
    )
    unknown = selected_names - set(TOOL_REGISTRY)
    if unknown:
        raise UnknownToolError(min(unknown))
    payload = {
        name: {
            "description": definition.description,
            "input_schema": _thaw_json(definition.input_schema),
            "read_or_write": definition.read_or_write,
            "risk_level": definition.risk_level,
            "permission_policy": definition.permission_policy,
            "object_binding_policy": definition.object_binding_policy,
            "date_resolution_policy": definition.date_resolution_policy,
            "confirmation_policy": definition.confirmation_policy,
            "idempotency_policy": definition.idempotency_policy,
            "transaction_policy": definition.transaction_policy,
            "transaction_target_policy": definition.transaction_target_policy,
            "conflict_policy": definition.conflict_policy,
            "pending_ttl_seconds": definition.pending_ttl_seconds,
            "receipt_schema": _thaw_json(definition.receipt_schema),
            "enabled_modes": sorted(mode.value for mode in definition.enabled_modes),
            "sandbox_handler": (
                f"{definition.sandbox_handler.__module__}."
                f"{definition.sandbox_handler.__qualname__}"
            ),
            "production_handler": (
                f"{definition.production_handler.__module__}."
                f"{definition.production_handler.__qualname__}"
            ),
            "shadow_handler": (
                f"{definition.shadow_handler.__module__}."
                f"{definition.shadow_handler.__qualname__}"
            ),
        }
        for name, definition in TOOL_REGISTRY.items()
        if name in selected_names
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def runtime_registry_tool_names(
    settings: object,
) -> tuple[str, ...]:
    """Return the Registry-owned production tool set for current feature flags."""

    dashboard_read_enabled = bool(
        getattr(
            settings,
            "legal_daily_dashboard_enabled",
            False,
        )
    ) and bool(
        getattr(
            settings,
            "agent2_cross_user_daily_read_enabled",
            False,
        )
    )
    performance_read_enabled = (
        bool(
            getattr(
                settings,
                "agent2_performance_tool_enabled",
                False,
            )
        )
        and bool(
            getattr(
                settings,
                "legal_ops_data_intake_enabled",
                False,
            )
        )
        and bool(
            getattr(
                settings,
                "agent2_performance_knowledge_enabled",
                False,
            )
        )
        and bool(
            str(
                getattr(
                    settings,
                    "legal_ops_live_tenant_id",
                    "",
                )
                or ""
            ).strip()
        )
    )
    weekly_plan_read_enabled = bool(
        getattr(settings, "agent2_weekly_plan_enabled", False)
    )
    weekly_plan_write_enabled = (
        weekly_plan_read_enabled
        and bool(
            getattr(
                settings,
                "agent2_weekly_plan_write_enabled",
                False,
            )
        )
    )
    current_weekly_report_enabled = bool(
        getattr(
            settings,
            "agent2_current_weekly_report_enabled",
            False,
        )
    )
    return tuple(
        name
        for name, definition in TOOL_REGISTRY.items()
        if (
            (
                definition.permission_policy
                != "authenticated_tenant_daily_read"
                or dashboard_read_enabled
            )
            and (
                definition.permission_policy
                != "authenticated_tenant_performance_read"
                or performance_read_enabled
            )
            and (
                definition.permission_policy
                != "authenticated_owner_weekly_plan_read"
                or weekly_plan_read_enabled
            )
            and (
                definition.permission_policy
                != "authenticated_owner_weekly_plan_write"
                or weekly_plan_write_enabled
            )
            and (
                definition.permission_policy
                != "authenticated_owner_current_weekly_report"
                or current_weekly_report_enabled
            )
        )
    )


def runtime_registry_contract_digest(settings: object) -> str:
    """Keep disabled candidate tools out of the live runtime attestation."""

    return registry_contract_digest(
        runtime_registry_tool_names(settings)
    )
