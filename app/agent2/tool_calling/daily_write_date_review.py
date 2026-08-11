from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from app.agent2.tool_calling.contracts import DateExpression, StrictContract

DAILY_REPORT_DATE_REVIEW_TOOL = "review_daily_report_dates"


class DailyReportDateEvidence(StrictContract):
    message_sequence: int = Field(ge=1, le=20)
    exact_quote: str = Field(min_length=1, max_length=1000)


class DailyReportDateDecision(StrictContract):
    sequence: int = Field(ge=1, le=30)
    binding: Literal[
        "explicit_report_date",
        "work_event_time_only",
        "no_report_date_reference",
        "ambiguous",
    ]
    evidence: DailyReportDateEvidence | None = None
    observed_time_expression: DateExpression | None = None
    proposed_report_date: date | None = None

    @model_validator(mode="after")
    def validate_binding_fields(self) -> DailyReportDateDecision:
        if self.binding == "explicit_report_date":
            if (
                self.evidence is None
                or self.observed_time_expression is None
                or self.proposed_report_date is None
            ):
                raise ValueError(
                    "an explicit report date requires exact evidence "
                    "and resolved date fields"
                )
            return self
        if self.proposed_report_date is not None:
            raise ValueError(
                "only an explicit report date may carry a resolved report date"
            )
        if self.binding == "work_event_time_only" and (
            self.evidence is None or self.observed_time_expression is None
        ):
            raise ValueError(
                "a work-event time reference requires an expression and exact evidence"
            )
        if self.binding == "no_report_date_reference" and (
            self.evidence is not None or self.observed_time_expression is not None
        ):
            raise ValueError(
                "a no-reference decision cannot carry time evidence or an expression"
            )
        return self


class DailyReportDateReviewArgs(StrictContract):
    decisions: tuple[DailyReportDateDecision, ...] = Field(
        min_length=1,
        max_length=30,
    )

    @model_validator(mode="after")
    def require_unique_sequences(self) -> DailyReportDateReviewArgs:
        sequences = tuple(item.sequence for item in self.decisions)
        if len(sequences) != len(set(sequences)):
            raise ValueError("daily-report date review sequences must be unique")
        return self


def daily_report_date_review_tool_schema() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": DAILY_REPORT_DATE_REVIEW_TOOL,
                "description": (
                    "Review only how the exact current user messages bind time "
                    "language to the calendar date of each unexecuted daily-report "
                    "draft. Distinguish a date directly assigned to the report from "
                    "a time reference that describes only a work event. Return exact "
                    "source evidence; never rewrite report content or decide any "
                    "other user intent."
                ),
                "parameters": DailyReportDateReviewArgs.model_json_schema(),
            },
        }
    ]


def daily_report_date_review_messages(
    *,
    ordered_messages: tuple[str, ...],
    local_now: datetime,
    server_default_report_date: date,
    draft_count: int,
) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You are an isolated Agent2 semantic reviewer. Review only the "
                "report-date meaning of the exact current user messages. For each "
                "unexecuted draft, decide whether a time expression is directly "
                "bound to that report's calendar date, refers only to when a "
                "described work event happened, supplies no report-date reference, "
                "or is genuinely ambiguous. Do not treat tense or a report section "
                "label as a calendar-date instruction. For any explicit report "
                "date or work-event time reference, copy the smallest sufficient "
                "exact quote from the current user messages. Copy an observed "
                "time expression for explicit_report_date and work_event_time_only, "
                "but resolve proposed_report_date only for explicit_report_date. "
                "If the relationship cannot be "
                "determined reliably, choose ambiguous. Return exactly one internal "
                "review tool call and one decision per draft in sequence order. "
                "This is semantic model judgment, never phrase, keyword, or "
                "regular-expression matching. None of the drafts has executed."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "ordered_current_user_messages": [
                        {"sequence": index, "content": content}
                        for index, content in enumerate(ordered_messages, start=1)
                    ],
                    "trusted_local_time": local_now.isoformat(),
                    "server_default_report_date": (
                        server_default_report_date.isoformat()
                    ),
                    "unexecuted_daily_draft_count": draft_count,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
    ]


def validate_daily_report_date_review(
    arguments: Any,
    *,
    ordered_messages: tuple[str, ...],
    expected_count: int,
) -> tuple[DailyReportDateDecision, ...]:
    reviewed = DailyReportDateReviewArgs.model_validate(arguments)
    expected_sequences = tuple(range(1, expected_count + 1))
    actual_sequences = tuple(item.sequence for item in reviewed.decisions)
    if actual_sequences != expected_sequences:
        raise ValueError("daily-report date decisions do not match draft order")
    for decision in reviewed.decisions:
        evidence = decision.evidence
        if evidence is None:
            continue
        if evidence.message_sequence > len(ordered_messages):
            raise ValueError("daily-report date evidence message is out of range")
        source = ordered_messages[evidence.message_sequence - 1]
        if evidence.exact_quote not in source:
            raise ValueError(
                "daily-report date evidence is not an exact source substring"
            )
    return reviewed.decisions


def decision_date_selection(decision: DailyReportDateDecision) -> str | None:
    if decision.binding == "ambiguous":
        return None
    if decision.binding == "explicit_report_date":
        return "user_explicit"
    return "server_default"


def decisions_agree(
    first: DailyReportDateDecision,
    second: DailyReportDateDecision,
) -> bool:
    first_selection = decision_date_selection(first)
    second_selection = decision_date_selection(second)
    if first_selection is None or second_selection is None:
        return False
    if first_selection != second_selection:
        return False
    if first_selection == "user_explicit":
        return first.proposed_report_date == second.proposed_report_date
    return True


def daily_report_date_clarification_instruction() -> dict[str, str]:
    return {
        "role": "system",
        "content": json.dumps(
            {
                "agent2_report_date_clarification": {
                    "write_status": "not_executed",
                    "instruction": (
                        "Do not call any tool or write any report. Ask one concise, "
                        "natural question so the user can clarify the calendar "
                        "date this report belongs to. Do not mention internal "
                        "reviews or implementation details."
                    ),
                }
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    }
