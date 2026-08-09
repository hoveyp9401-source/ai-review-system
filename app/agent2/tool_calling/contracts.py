from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Any, Annotated, Literal
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
ReportField = Literal["today_work", "problems", "tomorrow_plan"]
PersonalMemoryKey = Literal[
    "assistant.preferred_name",
    "response.verbosity",
    "response.output_format",
    "response.preferred_salutation",
    "report.show_updated_snapshot",
    "report.show_item_numbers",
]


class QueryTodayReportArgs(StrictContract):
    pass


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
        "all_history",
        "recent_7_days",
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
            if self.scope_type != "person" or self.period_type == "all_history":
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


class DailyItemInput(StrictContract):
    field: ReportField
    content: NonEmptyText


class AddDailyItemsArgs(StrictContract):
    date_expression: DateExpression
    proposed_date: date
    items: tuple[DailyItemInput, ...] = Field(default=(), max_length=30)
    acknowledged_empty_fields: tuple[ReportField, ...] = Field(
        default=(),
        max_length=3,
    )

    @model_validator(mode="after")
    def require_content_or_explicit_empty_acknowledgement(
        self,
    ) -> "AddDailyItemsArgs":
        if not self.items and not self.acknowledged_empty_fields:
            raise ValueError(
                "at least one report item or explicitly empty field is required"
            )
        if len(self.acknowledged_empty_fields) != len(
            set(self.acknowledged_empty_fields)
        ):
            raise ValueError("explicitly empty fields must be unique")
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

    @model_validator(mode="after")
    def value_must_match_the_selected_key(
        self,
    ) -> "RememberPersonalMemoryArgs":
        validate_personal_memory_value(
            "response_preference",
            self.memory_key,
            self.value,
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
