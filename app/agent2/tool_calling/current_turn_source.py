from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.agent2.tool_calling.contracts import (
    AddDailyItemsArgs,
    ApplyCurrentWeeklyReportArgs,
    ApplyNextWeeklyPlanArgs,
    CorrectDailyReportDateArgs,
    CurrentUserMessageEvidence,
    EditDailyItemsArgs,
    RecordWeeklyPlanItemsAsTodayWorkArgs,
    RememberPersonalMemoryArgs,
    SubmitCurrentWeeklyReportArgs,
    SubmitNextWeeklyPlanArgs,
)


class CurrentTurnSourceEvidenceError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class CurrentTurnSource:
    """Server-owned current-turn text and exact source-evidence checks."""

    messages: tuple[str, ...]
    occurred_at: tuple[datetime, ...] | None = None

    def __post_init__(self) -> None:
        if not self.messages or len(self.messages) > 20:
            raise ValueError("current turn requires one to twenty user messages")
        if any(
            not isinstance(message, str) or not message.strip()
            for message in self.messages
        ):
            raise ValueError("current user messages must be non-empty strings")
        if self.occurred_at is not None:
            if len(self.occurred_at) != len(self.messages):
                raise ValueError(
                    "current message times must match current user messages"
                )
            if any(
                not isinstance(value, datetime)
                or value.tzinfo is None
                or value.utcoffset() is None
                for value in self.occurred_at
            ):
                raise ValueError(
                    "current message times must be timezone-aware"
                )

    def occurred_at_for(self, source_message_index: int) -> datetime | None:
        if self.occurred_at is None:
            return None
        index = source_message_index - 1
        if index < 0 or index >= len(self.occurred_at):
            return None
        return self.occurred_at[index]

    @property
    def canonical_text(self) -> str:
        return json.dumps(
            {
                "ordered_user_messages": [
                    {"sequence": index, "content": value}
                    for index, value in enumerate(self.messages, start=1)
                ]
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_text.encode("utf-8")).hexdigest()

    def validate_tool_arguments(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> None:
        if tool_name == "add_daily_items":
            typed = AddDailyItemsArgs.model_validate(arguments)
            self._validate_daily_item_spans(typed)
            for item in typed.empty_field_evidence:
                self._validate_evidence(item.source_evidence)
            if typed.date_evidence is not None:
                source = self._validate_evidence(typed.date_evidence)
                if typed.date_evidence.exact_quote not in source:
                    raise CurrentTurnSourceEvidenceError(
                        "CURRENT_DATE_EVIDENCE_MISMATCH"
                    )
            return
        if tool_name == "edit_daily_items":
            typed_edit = EditDailyItemsArgs.model_validate(arguments)
            source_message = self._validate_evidence(
                typed_edit.replacement_evidence
            )
            exact_quote = typed_edit.replacement_evidence.exact_quote
            occurrence_count = source_message.count(exact_quote)
            if occurrence_count == 0:
                raise CurrentTurnSourceEvidenceError(
                    "DAILY_EDIT_REPLACEMENT_QUOTE_MISMATCH"
                )
            if occurrence_count != 1:
                raise CurrentTurnSourceEvidenceError(
                    "DAILY_EDIT_REPLACEMENT_SPAN_AMBIGUOUS"
                )
            return
        if tool_name == "correct_daily_report_date":
            typed_correction = CorrectDailyReportDateArgs.model_validate(
                arguments
            )
            for item in typed_correction.empty_field_evidence:
                self._validate_evidence(item.source_evidence)
            return
        if tool_name == "record_weekly_plan_items_as_today_work":
            typed_weekly_daily = (
                RecordWeeklyPlanItemsAsTodayWorkArgs.model_validate(arguments)
            )
            self._validate_evidence(typed_weekly_daily.source_evidence)
            return
        if tool_name == "apply_next_weekly_plan":
            typed_weekly_plan = ApplyNextWeeklyPlanArgs.model_validate(
                arguments
            )
            for operation in typed_weekly_plan.operations:
                source_message = self._validate_evidence(
                    operation.source_evidence
                )
                exact_clause = getattr(
                    operation.source_evidence,
                    "exact_clause_quote",
                    None,
                )
                if (
                    isinstance(exact_clause, str)
                    and exact_clause not in source_message
                ):
                    raise CurrentTurnSourceEvidenceError(
                        "WEEKLY_PLAN_DATE_EVIDENCE_MISMATCH"
                    )
                recurrence_scope = getattr(
                    operation.source_evidence,
                    "recurrence_scope_quote",
                    None,
                )
                if (
                    isinstance(recurrence_scope, str)
                    and (
                        recurrence_scope not in source_message
                        or not isinstance(exact_clause, str)
                        or recurrence_scope not in exact_clause
                    )
                ):
                    raise CurrentTurnSourceEvidenceError(
                        "WEEKLY_PLAN_RECURRENCE_SCOPE_EVIDENCE_MISMATCH"
                    )
                content = getattr(operation, "content", None)
                grounded_source = (
                    exact_clause
                    if isinstance(exact_clause, str)
                    else source_message
                )
                if isinstance(content, str) and content not in grounded_source:
                    raise CurrentTurnSourceEvidenceError(
                        "WEEKLY_PLAN_CONTENT_NOT_GROUNDED"
                    )
            return
        if tool_name == "submit_next_weekly_plan":
            typed_submission = SubmitNextWeeklyPlanArgs.model_validate(
                arguments
            )
            self._validate_evidence(
                typed_submission.confirmation_evidence
            )
            return
        if tool_name == "apply_current_weekly_report":
            typed_periodic = ApplyCurrentWeeklyReportArgs.model_validate(
                arguments
            )
            for operation in typed_periodic.operations:
                source_message = self._validate_evidence(
                    operation.source_evidence
                )
                value = (
                    operation.content
                    if operation.operation == "append"
                    else (
                        operation.replacement
                        if operation.operation == "edit"
                        else None
                    )
                )
                if isinstance(value, str) and value not in source_message:
                    raise CurrentTurnSourceEvidenceError(
                        "PERIODIC_REPORT_CONTENT_NOT_GROUNDED"
                    )
            return
        if tool_name == "submit_current_weekly_report":
            typed_periodic_submit = (
                SubmitCurrentWeeklyReportArgs.model_validate(arguments)
            )
            self._validate_evidence(
                typed_periodic_submit.confirmation_evidence
            )
            return
        if tool_name != "remember_personal_memory":
            return
        typed_memory = RememberPersonalMemoryArgs.model_validate(arguments)
        evidence = typed_memory.source_evidence
        source_message = self._validate_evidence(evidence)
        value = typed_memory.value.model_dump(mode="json")
        grounded_value = (
            value.get("name")
            if typed_memory.memory_key == "assistant.preferred_name"
            else (
                value.get("salutation")
                if typed_memory.memory_key
                == "response.preferred_salutation"
                else None
            )
        )
        if (
            isinstance(grounded_value, str)
            and grounded_value not in source_message
        ):
            raise CurrentTurnSourceEvidenceError(
                "MEMORY_VALUE_NOT_GROUNDED"
            )

    def bind_tool_arguments(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Return arguments grounded in server text and approved review evidence."""

        self.validate_tool_arguments(tool_name, arguments)
        if tool_name not in {"add_daily_items", "edit_daily_items"}:
            return arguments

        if tool_name == "edit_daily_items":
            typed_edit = EditDailyItemsArgs.model_validate(arguments)
            bound_edit = typed_edit.model_dump(mode="json")
            evidence = typed_edit.replacement_evidence
            source_message = self._validate_evidence(evidence)
            quote_start = source_message.find(evidence.exact_quote)
            quote_end = quote_start + len(evidence.exact_quote)
            bound_edit["replacement"] = source_message[
                quote_start:quote_end
            ]
            return bound_edit

        typed = AddDailyItemsArgs.model_validate(arguments)
        bound = typed.model_dump(mode="json")
        for index, item in enumerate(typed.items):
            exact_quote = item.source_evidence.exact_quote
            source_message = self._validate_evidence(item.source_evidence)
            quote_start = source_message.find(exact_quote)
            if quote_start < 0:
                raise CurrentTurnSourceEvidenceError(
                    "DAILY_ITEM_EXACT_QUOTE_MISMATCH"
                )
            quote_end = quote_start + len(exact_quote)
            if not typed.content_reviewed:
                bound["items"][index]["content"] = source_message[
                    quote_start:quote_end
                ]
        return bound

    def _validate_daily_item_spans(self, typed: AddDailyItemsArgs) -> None:
        spans_by_message: dict[int, list[tuple[int, int]]] = {}
        for item in typed.items:
            evidence = item.source_evidence
            source_message = self._validate_evidence(evidence)
            exact_quote = evidence.exact_quote
            occurrence_count = source_message.count(exact_quote)
            if occurrence_count == 0:
                raise CurrentTurnSourceEvidenceError(
                    "DAILY_ITEM_CONTENT_NOT_GROUNDED"
                    if exact_quote == item.content
                    else "DAILY_ITEM_EXACT_QUOTE_MISMATCH"
                )
            if occurrence_count != 1:
                raise CurrentTurnSourceEvidenceError(
                    "DAILY_ITEM_SOURCE_SPAN_AMBIGUOUS"
                )
            start = source_message.find(exact_quote)
            end = start + len(exact_quote)
            message_spans = spans_by_message.setdefault(
                evidence.source_message_index,
                [],
            )
            if any(
                start < existing_end and existing_start < end
                for existing_start, existing_end in message_spans
            ):
                raise CurrentTurnSourceEvidenceError(
                    "DAILY_ITEM_SOURCE_SPAN_OVERLAP"
                )
            message_spans.append((start, end))

    def _validate_evidence(
        self,
        evidence: CurrentUserMessageEvidence,
    ) -> str:
        index = evidence.source_message_index - 1
        if index < 0 or index >= len(self.messages):
            raise CurrentTurnSourceEvidenceError(
                "CURRENT_MESSAGE_EVIDENCE_MISMATCH"
            )
        return self.messages[index]
