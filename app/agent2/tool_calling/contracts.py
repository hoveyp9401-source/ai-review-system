from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Any, Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.agent2.memory import (
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
    view: Literal[
        "member_report",
        "team_reports",
        "missing_submissions",
        "department_summary",
    ]
    member_name: Annotated[
        str,
        Field(min_length=1, max_length=256),
    ] | None = None
    team_name: Annotated[
        str,
        Field(min_length=1, max_length=256),
    ] | None = None

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
    items: tuple[DailyItemInput, ...] = Field(min_length=1, max_length=30)


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
    report_id: UUID
    expected_version: int = Field(ge=0)
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
        VerbosityPreferenceValue
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
