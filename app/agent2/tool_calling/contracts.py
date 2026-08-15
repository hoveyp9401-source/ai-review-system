from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.agent2.memory import (
    AssistantPreferredNameValue,
    OutputFormatPreferenceValue,
    PreferredSalutationValue,
    TogglePreferenceValue,
    VerbosityPreferenceValue,
    validate_personal_memory_value,
)


class ExecutionMode(StrEnum):
    SHADOW_PROPOSAL = "shadow_proposal"
    SANDBOX_EXECUTE = "sandbox_execute"
    CANARY_EXECUTE = "canary_execute"


class ReceiptStatus(StrEnum):
    SUCCESS = "success"
    NO_OP = "no_op"
    BLOCKED = "blocked"
    CLARIFICATION_REQUIRED = "clarification_required"
    FAILED = "failed"


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


NonEmptyText = Annotated[str, Field(min_length=1, max_length=4000)]
DateExpression = Annotated[str, Field(min_length=1, max_length=128)]
ItemId = Annotated[str, Field(min_length=1, max_length=256)]
WeeklyPlanOperationId = Annotated[str, Field(min_length=1, max_length=128)]
ReportField = Literal["today_work", "problems", "tomorrow_plan"]
PeriodicReportField = Literal[
    "accomplishments",
    "risks",
    "next_plan",
    "metrics",
]
PersonalMemoryKey = Literal[
    "assistant.preferred_name",
    "response.verbosity",
    "response.output_format",
    "response.preferred_salutation",
    "report.show_updated_snapshot",
    "report.show_item_numbers",
]


class CurrentUserMessageEvidence(StrictContract):
    source_message_index: Annotated[
        int,
        Field(
            ge=1,
            le=20,
            description=(
                "One-based sequence of the current user_message or current "
                "ordered user_messages fragment that supplies this value."
            ),
        ),
    ]


class DailyReportDateEvidence(CurrentUserMessageEvidence):
    exact_quote: Annotated[
        str,
        Field(
            min_length=1,
            max_length=1000,
            description=(
                "Exact current-message text that lets Agent2 choose the "
                "reporting date instead of the server default."
            ),
        ),
    ]


class WeeklyPlanExplicitDateEvidence(CurrentUserMessageEvidence):
    """One complete current-message clause that binds a formal plan date."""

    exact_clause_quote: Annotated[
        str,
        Field(
            min_length=1,
            max_length=2000,
            description=(
                "Exact complete current-message clause containing both the "
                "weekly day expression and the asserted plan matter. Do not "
                "quote only a weekday or cut text out of an ambiguous clause. "
                "For one matter expanded to several dates, copy the entire "
                "current user message so a later qualifier cannot be hidden."
            ),
        ),
    ]
    recurrence_scope_quote: Annotated[
        str,
        Field(
            min_length=1,
            max_length=500,
            description=(
                "For one matter expanded to multiple exact plan dates, copy the "
                "complete contiguous date-scope text, such as 下周每天 or "
                "周一到周五每天. Include every qualifier or exclusion attached "
                "to that scope; never truncate a restricted phrase to just 每天. "
                "Omit it for a single-date operation."
            ),
        ),
    ] | None = None


class PersonalMemorySourceEvidence(CurrentUserMessageEvidence):
    intent: Literal[
        "explicit_preference",
        "explicit_remember_request",
        "assistant_name_assignment",
        "assistant_name_correction",
        "user_salutation_assignment",
        "user_salutation_correction",
    ]


class QueryTodayReportArgs(StrictContract):
    pass


class QueryCurrentWeeklyReportArgs(StrictContract):
    """The authenticated owner and current ISO week come only from the server."""


class _PeriodicWeeklyOperation(StrictContract):
    operation_id: Annotated[str, Field(min_length=1, max_length=128)]
    source_evidence: CurrentUserMessageEvidence


class PeriodicWeeklyAppendOperation(_PeriodicWeeklyOperation):
    operation: Literal["append"]
    field: PeriodicReportField
    content: NonEmptyText


class PeriodicWeeklyEditOperation(_PeriodicWeeklyOperation):
    operation: Literal["edit"]
    item_id: ItemId
    replacement: NonEmptyText


class PeriodicWeeklyDeleteOperation(_PeriodicWeeklyOperation):
    operation: Literal["delete"]
    item_id: ItemId


PeriodicWeeklyOperation = Annotated[
    PeriodicWeeklyAppendOperation
    | PeriodicWeeklyEditOperation
    | PeriodicWeeklyDeleteOperation,
    Field(discriminator="operation"),
]


class ApplyCurrentWeeklyReportArgs(StrictContract):
    report_id: UUID
    expected_version: int = Field(ge=0)
    operations: tuple[PeriodicWeeklyOperation, ...] = Field(
        min_length=1,
        max_length=50,
    )

    @field_validator("operations")
    @classmethod
    def operation_ids_must_be_unique(
        cls,
        value: tuple[PeriodicWeeklyOperation, ...],
    ) -> tuple[PeriodicWeeklyOperation, ...]:
        operation_ids = tuple(item.operation_id for item in value)
        if len(operation_ids) != len(set(operation_ids)):
            raise ValueError("periodic weekly operation IDs must be unique")
        return value


class SubmitCurrentWeeklyReportArgs(StrictContract):
    report_id: UUID
    expected_version: int = Field(ge=0)
    confirmation_evidence: CurrentUserMessageEvidence


class QueryNextWeeklyPlanArgs(StrictContract):
    """Select one exact weekly-plan target; omit only for one-target compatibility."""

    plan_id: UUID | None = Field(
        default=None,
        description=(
            "Stable plan_id copied from weekly_plan_targets. It may be omitted only "
            "when trusted context exposes exactly one weekly-plan target; with multiple "
            "targets the model must select and provide the intended trusted plan_id."
        ),
    )


class _WeeklyPlanOperation(StrictContract):
    operation_id: WeeklyPlanOperationId
    source_evidence: CurrentUserMessageEvidence


class WeeklyPlanAddOperation(_WeeklyPlanOperation):
    source_evidence: WeeklyPlanExplicitDateEvidence
    operation: Literal["add"]
    plan_date: date
    content: NonEmptyText


class WeeklyPlanEditOperation(_WeeklyPlanOperation):
    operation: Literal["edit"]
    item_id: ItemId
    content: NonEmptyText


class WeeklyPlanMoveOperation(_WeeklyPlanOperation):
    source_evidence: WeeklyPlanExplicitDateEvidence
    operation: Literal["move"]
    item_id: ItemId
    target_plan_date: date


class WeeklyPlanDeleteOperation(_WeeklyPlanOperation):
    operation: Literal["delete"]
    item_id: ItemId


class WeeklyPlanSetDayEmptyOperation(_WeeklyPlanOperation):
    source_evidence: WeeklyPlanExplicitDateEvidence
    operation: Literal["set_day_empty"]
    plan_date: date


class WeeklyPlanAcceptSuggestionOperation(_WeeklyPlanOperation):
    source_evidence: WeeklyPlanExplicitDateEvidence
    operation: Literal["accept_suggestion"]
    suggestion_id: ItemId
    plan_date: date


class WeeklyPlanRejectSuggestionOperation(_WeeklyPlanOperation):
    operation: Literal["reject_suggestion"]
    suggestion_id: ItemId


class WeeklyPlanCaptureSuggestionOperation(_WeeklyPlanOperation):
    """Capture an explicitly next-week matter whose day is still undecided."""

    operation: Literal["capture_suggestion"]
    content: NonEmptyText


WeeklyPlanOperation = Annotated[
    WeeklyPlanAddOperation
    | WeeklyPlanEditOperation
    | WeeklyPlanMoveOperation
    | WeeklyPlanDeleteOperation
    | WeeklyPlanSetDayEmptyOperation
    | WeeklyPlanAcceptSuggestionOperation
    | WeeklyPlanRejectSuggestionOperation
    | WeeklyPlanCaptureSuggestionOperation,
    Field(discriminator="operation"),
]


class ApplyNextWeeklyPlanArgs(StrictContract):
    """Write the selected exact weekly-plan target from trusted context."""

    plan_id: UUID
    expected_version: int = Field(ge=0)
    operations: tuple[WeeklyPlanOperation, ...] = Field(
        min_length=1,
        max_length=50,
    )

    @field_validator("operations")
    @classmethod
    def operation_ids_must_be_unique(
        cls,
        value: tuple[WeeklyPlanOperation, ...],
    ) -> tuple[WeeklyPlanOperation, ...]:
        operation_ids = tuple(item.operation_id for item in value)
        if len(operation_ids) != len(set(operation_ids)):
            raise ValueError("weekly plan operation IDs must be unique")
        return value


class SubmitNextWeeklyPlanArgs(StrictContract):
    """Submit the selected exact weekly-plan target from trusted context."""

    plan_id: UUID
    expected_version: int = Field(ge=0)
    confirmation_evidence: CurrentUserMessageEvidence


class RecordWeeklyPlanItemsAsTodayWorkArgs(StrictContract):
    """Copy exact trusted weekly-plan text into today's Daily Report."""

    plan_id: UUID
    expected_version: int = Field(ge=0)
    target_item_ids: tuple[ItemId, ...] = Field(min_length=1, max_length=30)
    source_evidence: CurrentUserMessageEvidence

    @field_validator("target_item_ids")
    @classmethod
    def item_ids_must_be_unique(
        cls,
        value: tuple[ItemId, ...],
    ) -> tuple[ItemId, ...]:
        if len(value) != len(set(value)):
            raise ValueError("weekly plan item IDs must be unique")
        return value


class QueryReportByDateArgs(StrictContract):
    date_expression: DateExpression
    proposed_date: date


class QueryManagedDailyReportsArgs(StrictContract):
    report_date_expression: DateExpression | None = None
    proposed_report_date: date | None = None
    view: Annotated[
        Literal[
            "member_report",
            "team_reports",
            "missing_submissions",
            "department_summary",
        ],
        Field(
            description=(
                "Exact single-date view requested by the current user. "
                "missing_submissions may target either all people or one "
                "explicitly named team via team_name."
            )
        ),
    ]
    member_name: Annotated[
        str | None,
        Field(
            min_length=1,
            max_length=256,
            description=(
                "Exact person name copied from the current user's requested "
                "scope. Required for member_report."
            ),
        ),
    ] = None
    team_name: Annotated[
        str | None,
        Field(
            min_length=1,
            max_length=256,
            description=(
                "Exact team or organization scope named by the current user. "
                "For team_reports and for a team-scoped missing_submissions "
                "request this MUST be supplied; '中心直属' is a valid exact "
                "scope. Omit only when the user explicitly asks for the whole "
                "department, center, or all people."
            ),
        ),
    ] = None

    @model_validator(mode="after")
    def enforce_view_and_date_contract(
        self,
    ) -> QueryManagedDailyReportsArgs:
        if (self.report_date_expression is None) != (
            self.proposed_report_date is None
        ):
            raise ValueError(
                "report date expression and proposed date must be supplied together"
            )
        if self.view == "member_report":
            if self.member_name is None:
                raise ValueError(
                    "member_report requires member_name"
                )
            return self
        if self.member_name is not None:
            raise ValueError(
                "member_name is valid only for member_report"
            )
        if (
            self.view == "department_summary"
            and self.team_name is not None
        ):
            raise ValueError(
                "department_summary cannot select one team"
            )
        return self


class QueryDailyBriefingFactsArgs(StrictContract):
    view: Literal["member_classification", "recipient_delivery"] = (
        "member_classification"
    )
    report_date_expression: DateExpression | None = None
    proposed_report_date: date | None = None
    member_name: Annotated[
        str | None,
        Field(
            min_length=1,
            max_length=256,
            description=(
                "Exact member named by the current user. Omit only when "
                "member_classification refers to the authenticated user."
            ),
        ),
    ] = None
    recipient_name: Annotated[
        str | None,
        Field(
            min_length=1,
            max_length=256,
            description=(
                "Exact briefing recipient named by the current user. Omit "
                "for the authenticated user's own received briefing."
            ),
        ),
    ] = None
    team_name: Annotated[
        str | None,
        Field(
            min_length=1,
            max_length=256,
            description="Exact team scope named by the current user.",
        ),
    ] = None

    @model_validator(mode="after")
    def enforce_briefing_fact_contract(
        self,
    ) -> "QueryDailyBriefingFactsArgs":
        if (self.report_date_expression is None) != (
            self.proposed_report_date is None
        ):
            raise ValueError(
                "report date expression and proposed date must be supplied together"
            )
        if self.view == "recipient_delivery" and self.member_name is not None:
            raise ValueError(
                "member_name is valid only for member_classification"
            )
        return self


class QueryReportInsightsArgs(StrictContract):
    query_kind: Literal[
        "report_count",
        "recent_work",
        "period_work",
        "recent_attention",
        "unclosed_work",
    ]
    scope_type: Literal["person", "organization"]
    scope_name: Annotated[str, Field(min_length=1, max_length=256)]
    period_type: Literal[
        "unspecified",
        "all_history",
        "recent_7_days",
        "recent_30_days",
        "current_week",
        "previous_week",
    ]
    status_filter: Literal["all_saved", "completed"] = "all_saved"

    @model_validator(mode="after")
    def enforce_insight_contract(self) -> "QueryReportInsightsArgs":
        if self.query_kind == "report_count":
            if self.scope_type != "person" or self.period_type != "all_history":
                raise ValueError(
                    "report_count requires person scope over all_history"
                )
            return self
        if self.status_filter != "all_saved":
            raise ValueError(
                "status_filter=completed is valid only for report_count"
            )
        if self.query_kind == "recent_work":
            if self.scope_type != "person" or self.period_type in {
                "all_history",
                "unspecified",
            }:
                raise ValueError(
                    "recent_work requires person scope and a bounded period"
                )
            return self
        if self.query_kind == "period_work":
            if self.scope_type != "organization" or self.period_type not in {
                "current_week",
                "previous_week",
            }:
                raise ValueError(
                    "period_work requires organization scope and a week period"
                )
            return self
        if self.query_kind == "recent_attention":
            if (
                self.scope_type != "organization"
                or self.period_type != "recent_7_days"
            ):
                raise ValueError(
                    "recent_attention requires organization scope over recent_7_days"
                )
            return self
        return self


class QueryDefendantPerformanceArgs(StrictContract):
    view: Literal["week", "month"] = "month"
    scope_type: Literal["department", "team", "self"] = "department"
    team_name: Annotated[
        str,
        Field(min_length=1, max_length=256),
    ] | None = None
    mode: Literal[
        "summary",
        "explain_new",
        "explain_stock",
        "explain_loss",
        "explain_substantial",
    ] = "summary"

    @model_validator(mode="after")
    def enforce_scope_contract(
        self,
    ) -> QueryDefendantPerformanceArgs:
        if self.scope_type == "team":
            if self.team_name is None:
                raise ValueError("team scope requires team_name")
            return self
        if self.team_name is not None:
            raise ValueError("team_name is valid only for team scope")
        return self


class DailyItemSourceEvidence(CurrentUserMessageEvidence):
    exact_quote: Annotated[
        str,
        Field(
            min_length=1,
            max_length=4000,
            description=(
                "Contiguous current-message quote authoritative for this one "
                "persisted Daily Report item. It must cover the item's complete "
                "meaning: never omit a negation, condition, deadline, consequence, "
                "exception, or pending action, even when punctuation or whitespace "
                "separates it. A date or section lead-in may be omitted only when "
                "doing so does not change the item's meaning. The server copies this "
                "quote from its own current-turn source instead of persisting "
                "model-authored wording."
            ),
        ),
    ]


class DailyItemInput(StrictContract):
    field: ReportField
    content: Annotated[
        str,
        Field(
            min_length=1,
            max_length=4000,
            description=(
                "Agent2's semantic interpretation of this item. If it is not "
                "verbatim current-message text, source_evidence.exact_quote must "
                "carry the exact authoritative wording to persist."
            ),
        ),
    ]
    source_evidence: DailyItemSourceEvidence


class DailyEmptyFieldEvidence(StrictContract):
    field: ReportField
    source_evidence: CurrentUserMessageEvidence


class AddDailyItemsArgs(StrictContract):
    date_selection: Literal[
        "server_default",
        "agent2_semantic",
        "user_explicit",
        "trusted_report",
    ] = (
        "server_default"
    )
    date_expression: DateExpression | None = None
    proposed_date: date | None = None
    report_id: UUID | None = None
    expected_version: int | None = Field(default=None, ge=0)
    date_evidence: DailyReportDateEvidence | None = None
    items: tuple[DailyItemInput, ...] = Field(default=(), max_length=30)
    acknowledged_empty_fields: tuple[ReportField, ...] = Field(
        default=(),
        max_length=3,
    )
    empty_field_evidence: tuple[DailyEmptyFieldEvidence, ...] = Field(
        default=(),
        max_length=3,
    )
    submit_after_write: bool = False

    @model_validator(mode="after")
    def require_content_or_explicit_empty_acknowledgement(
        self,
    ) -> "AddDailyItemsArgs":
        if self.date_selection == "trusted_report":
            if self.report_id is None or self.expected_version is None:
                raise ValueError(
                    "trusted-report selection requires report_id and expected_version"
                )
            if (
                self.date_expression is not None
                or self.proposed_date is not None
                or self.date_evidence is not None
            ):
                raise ValueError(
                    "trusted-report selection cannot carry a date expression"
                )
        elif self.date_selection == "agent2_semantic":
            if self.proposed_date is None or self.date_evidence is None:
                raise ValueError(
                    "Agent2 semantic date selection requires a proposed date "
                    "and exact current-message evidence"
                )
            if self.report_id is not None or self.expected_version is not None:
                raise ValueError(
                    "only trusted-report selection may carry a report binding"
                )
        elif self.date_selection == "user_explicit":
            if self.date_expression is None or self.proposed_date is None:
                raise ValueError(
                    "an explicit report date requires an expression and proposed date"
                )
            if self.report_id is not None or self.expected_version is not None:
                raise ValueError(
                    "only trusted-report selection may carry a report binding"
                )
            if self.date_evidence is None:
                raise ValueError(
                    "an explicit report date requires exact current-message evidence"
                )
        else:
            if self.report_id is not None or self.expected_version is not None:
                raise ValueError(
                    "only trusted-report selection may carry a report binding"
                )
            if self.date_evidence is not None:
                raise ValueError(
                    "the server default cannot carry semantic date evidence"
                )
        if (
            not self.items
            and not self.acknowledged_empty_fields
            and not self.submit_after_write
        ):
            raise ValueError(
                "report content, an explicitly empty field, or submission is required"
            )
        if len(self.acknowledged_empty_fields) != len(
            set(self.acknowledged_empty_fields)
        ):
            raise ValueError("explicitly empty fields must be unique")
        evidence_fields = tuple(
            item.field for item in self.empty_field_evidence
        )
        if len(evidence_fields) != len(set(evidence_fields)):
            raise ValueError("empty-field evidence must be unique by field")
        if set(evidence_fields) != set(self.acknowledged_empty_fields):
            raise ValueError(
                "every explicitly empty field requires matching current-message evidence"
            )
        item_fields = {item.field for item in self.items}
        if item_fields.intersection(self.acknowledged_empty_fields):
            raise ValueError(
                "one report field cannot contain items and be explicitly empty"
            )
        return self


class _VersionedItemTarget(StrictContract):
    report_id: UUID
    expected_version: int = Field(ge=0)
    target_item_ids: tuple[ItemId, ...] = Field(min_length=1, max_length=100)

    @field_validator("target_item_ids")
    @classmethod
    def item_ids_must_be_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("target_item_ids must be unique")
        return value


class EditDailyItemsArgs(_VersionedItemTarget):
    replacement: NonEmptyText


class DeleteDailyItemsArgs(_VersionedItemTarget):
    pass


class MoveDailyItemsArgs(_VersionedItemTarget):
    source_field: ReportField
    target_field: ReportField

    @model_validator(mode="after")
    def fields_must_differ(self) -> "MoveDailyItemsArgs":
        if self.source_field == self.target_field:
            raise ValueError("source_field and target_field must differ")
        return self


class CopyPreviousToTodayArgs(StrictContract):
    source_date_expression: DateExpression
    proposed_source_date: date


class CorrectDailyReportDateArgs(StrictContract):
    """One model-decided correction, executed atomically by the server."""

    source_date_expression: DateExpression
    proposed_source_date: date
    target_date_expression: DateExpression
    proposed_target_date: date
    acknowledged_empty_fields: tuple[ReportField, ...] = Field(
        default=(),
        max_length=3,
    )
    empty_field_evidence: tuple[DailyEmptyFieldEvidence, ...] = Field(
        default=(),
        max_length=3,
    )
    submit_after_correction: bool = False

    @model_validator(mode="after")
    def require_distinct_dates_and_unique_empty_fields(
        self,
    ) -> "CorrectDailyReportDateArgs":
        if self.proposed_source_date == self.proposed_target_date:
            raise ValueError("source and target report dates must differ")
        if len(self.acknowledged_empty_fields) != len(
            set(self.acknowledged_empty_fields)
        ):
            raise ValueError("explicitly empty fields must be unique")
        evidence_fields = tuple(
            item.field for item in self.empty_field_evidence
        )
        if len(evidence_fields) != len(set(evidence_fields)):
            raise ValueError("empty-field evidence must be unique by field")
        if set(evidence_fields) != set(self.acknowledged_empty_fields):
            raise ValueError(
                "every explicitly empty field requires matching current-message evidence"
            )
        return self


class CompletePreviousPlanArgs(_VersionedItemTarget):
    source_date_expression: DateExpression
    proposed_source_date: date


class ConfirmReportArgs(StrictContract):
    report_id: UUID
    expected_version: int = Field(ge=0)


class RequestClearReportArgs(StrictContract):
    report_id: UUID
    expected_version: int = Field(ge=0)


class ConfirmClearReportArgs(StrictContract):
    """The server resolves the unique active Pending; the model supplies no ID."""


class QueryPersonalMemoryArgs(StrictContract):
    """The authenticated user scope is supplied only by the server."""


class RememberPersonalMemoryArgs(StrictContract):
    memory_key: PersonalMemoryKey
    value: (
        AssistantPreferredNameValue
        | VerbosityPreferenceValue
        | TogglePreferenceValue
        | OutputFormatPreferenceValue
        | PreferredSalutationValue
    )
    source_evidence: PersonalMemorySourceEvidence

    @model_validator(mode="after")
    def value_must_match_the_selected_key(
        self,
    ) -> "RememberPersonalMemoryArgs":
        validate_personal_memory_value(
            "response_preference",
            self.memory_key,
            self.value,
        )
        assistant_name_intents = {
            "assistant_name_assignment",
            "assistant_name_correction",
        }
        user_salutation_intents = {
            "user_salutation_assignment",
            "user_salutation_correction",
        }
        preference_intents = {
            "explicit_preference",
            "explicit_remember_request",
        }
        intent = self.source_evidence.intent
        if (
            self.memory_key == "assistant.preferred_name"
            and intent not in assistant_name_intents
        ):
            raise ValueError(
                "assistant name memory requires assistant-role assignment evidence"
            )
        if (
            self.memory_key == "response.preferred_salutation"
            and intent not in user_salutation_intents
        ):
            raise ValueError(
                "user salutation memory requires user-role assignment evidence"
            )
        if self.memory_key not in {
            "assistant.preferred_name",
            "response.preferred_salutation",
        } and intent not in preference_intents:
            raise ValueError(
                "response preference memory requires explicit preference evidence"
            )
        return self


class ForgetPersonalMemoryArgs(StrictContract):
    memory_key: PersonalMemoryKey


class ToolReceipt(StrictContract):
    status: ReceiptStatus
    tool_name: str = Field(min_length=1, max_length=128)
    changed: bool = False
    target_type: str = Field(default="", max_length=128)
    target_id: str = Field(default="", max_length=256)
    before_version: int | None = Field(default=None, ge=0)
    after_version: int | None = Field(default=None, ge=0)
    affected_item_ids: tuple[str, ...] = ()
    error_code: str | None = None
    safe_user_facts: dict[str, Any] = Field(default_factory=dict)
    server_evidence: dict[str, Any] = Field(default_factory=dict)
    execution_mode: ExecutionMode = ExecutionMode.SHADOW_PROPOSAL
    would_change: bool = False
    idempotency_key: str | None = None
    validation_errors: tuple[str, ...] = ()

    @model_validator(mode="after")
    def shadow_never_claims_a_business_change(self) -> "ToolReceipt":
        if self.execution_mode == ExecutionMode.SHADOW_PROPOSAL:
            if self.changed:
                raise ValueError("shadow receipts cannot claim an actual business change")
            if self.before_version != self.after_version:
                raise ValueError("shadow receipts cannot advance a business version")
            if self.safe_user_facts.get("actual_write") is not False:
                raise ValueError("shadow receipts must state that no actual write occurred")
            if self.server_evidence:
                raise ValueError(
                    "shadow receipts cannot expose private server binding evidence"
                )
        return self
