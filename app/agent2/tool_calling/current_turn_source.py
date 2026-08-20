from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any

from app.agent2.tool_calling.contracts import (
    AddDailyItemsArgs,
    ApplyCurrentWeeklyReportArgs,
    ApplyNextWeeklyPlanArgs,
    CorrectDailyReportDateArgs,
    CurrentUserMessageEvidence,
    DailyItemSourceEvidence,
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
        *,
        allow_approximate_daily_quotes: bool = False,
    ) -> None:
        if tool_name == "add_daily_items":
            arguments = self._normalize_daily_source_quotes(
                arguments,
                allow_approximate=allow_approximate_daily_quotes,
            )
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

        if tool_name == "add_daily_items":
            arguments = self._normalize_daily_source_quotes(
                arguments,
                allow_approximate=bool(
                    arguments.get("content_reviewed")
                ),
            )
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
            formatting_only = _strip_leading_list_marker(
                source_message[quote_start:quote_end]
            )
            if not typed.content_reviewed:
                bound["items"][index]["content"] = formatting_only
        return bound

    def _normalize_daily_source_quotes(
        self,
        arguments: dict[str, Any],
        *,
        allow_approximate: bool = False,
    ) -> dict[str, Any]:
        raw_items = arguments.get("items")
        if not isinstance(raw_items, (list, tuple)):
            return arguments
        normalized_items = []
        changed = False
        for item in raw_items:
            if not isinstance(item, dict):
                normalized_items.append(item)
                continue
            evidence = item.get("source_evidence")
            if not isinstance(evidence, dict):
                normalized_items.append(item)
                continue
            source_index = evidence.get("source_message_index")
            exact_quote = evidence.get("exact_quote")
            if (
                not isinstance(source_index, int)
                or not isinstance(exact_quote, str)
                or source_index < 1
                or source_index > len(self.messages)
                or exact_quote in self.messages[source_index - 1]
            ):
                normalized_items.append(item)
                continue
            candidate = _strip_leading_source_separator(exact_quote)
            source_message = self.messages[source_index - 1]
            if allow_approximate and candidate not in source_message:
                aligned = _align_approximate_source_quote(
                    source_message,
                    candidate,
                )
                if aligned is not None:
                    candidate = aligned
            if candidate not in source_message:
                normalized_items.append(item)
                continue
            normalized_items.append(
                {
                    **item,
                    "source_evidence": {
                        **evidence,
                        "exact_quote": candidate,
                    },
                }
            )
            changed = True
        if not changed:
            return arguments
        return {**arguments, "items": normalized_items}

    def _validate_daily_item_spans(self, typed: AddDailyItemsArgs) -> None:
        item_counts: dict[tuple[int, str], int] = {}
        occurrence_queues: dict[
            tuple[int, str],
            list[tuple[int, int]],
        ] = {}
        quote_is_content: set[tuple[int, str]] = set()
        for item in typed.items:
            evidence = item.source_evidence
            key = (evidence.source_message_index, evidence.exact_quote)
            item_counts[key] = item_counts.get(key, 0) + 1
            if evidence.exact_quote == item.content:
                quote_is_content.add(key)
        for (source_index, exact_quote), item_count in item_counts.items():
            source_message = self._validate_evidence(
                DailyItemSourceEvidence(
                    source_message_index=source_index,
                    exact_quote=exact_quote,
                )
            )
            occurrences = _non_overlapping_quote_spans(
                source_message,
                exact_quote,
            )
            if not occurrences:
                raise CurrentTurnSourceEvidenceError(
                    "DAILY_ITEM_CONTENT_NOT_GROUNDED"
                    if (source_index, exact_quote) in quote_is_content
                    else "DAILY_ITEM_EXACT_QUOTE_MISMATCH"
                )
            if len(occurrences) != item_count:
                raise CurrentTurnSourceEvidenceError(
                    "DAILY_ITEM_SOURCE_SPAN_AMBIGUOUS"
                )
            occurrence_queues[(source_index, exact_quote)] = occurrences

        spans_by_message: dict[
            int,
            list[tuple[int, int, str]],
        ] = {}
        for item in typed.items:
            evidence = item.source_evidence
            source_message = self._validate_evidence(evidence)
            exact_quote = evidence.exact_quote
            start, end = occurrence_queues[
                (evidence.source_message_index, exact_quote)
            ].pop(0)
            message_spans = spans_by_message.setdefault(
                evidence.source_message_index,
                [],
            )
            if any(
                item.field == existing_field
                and start < existing_end
                and existing_start < end
                for existing_start, existing_end, existing_field in message_spans
            ):
                raise CurrentTurnSourceEvidenceError(
                    "DAILY_ITEM_SOURCE_SPAN_OVERLAP"
                )
            message_spans.append((start, end, item.field))

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


def _strip_leading_list_marker(value: str) -> str:
    """Remove only an unambiguous leading list marker; never rewrite words."""

    stripped = value.lstrip()
    if not stripped:
        return value
    if stripped[0] in {"-", "•", "·"}:
        candidate = stripped[1:].lstrip()
        return candidate or value
    cursor = 0
    while cursor < len(stripped) and stripped[cursor].isdigit():
        cursor += 1
    if cursor == 0 or cursor >= len(stripped):
        return value
    if stripped[cursor] not in {".", "。", "、", ")", "）"}:
        return value
    candidate = stripped[cursor + 1 :].lstrip()
    return candidate or value


def _strip_leading_source_separator(value: str) -> str:
    stripped = value.lstrip()
    cursor = 0
    while cursor < len(stripped) and stripped[cursor] in {
        "、",
        ",",
        "，",
        ":",
        "：",
        ";",
        "；",
    }:
        cursor += 1
        while cursor < len(stripped) and stripped[cursor].isspace():
            cursor += 1
    return stripped[cursor:] or value


def _align_approximate_source_quote(
    source_message: str,
    approximate_quote: str,
) -> str | None:
    """Recover one high-confidence contiguous source span without rewriting it."""

    quote, _ = _alignment_text(approximate_quote)
    source, source_positions = _alignment_text(source_message)
    if len(quote) < 12 or not source or len(source_positions) != len(source):
        return None

    blocks = [
        block
        for block in SequenceMatcher(
            None,
            quote,
            source,
            autojunk=False,
        ).get_matching_blocks()
        if block.size
    ]
    if not blocks:
        return None

    max_source_span = min(
        len(source),
        max(len(quote) + 80, int(len(quote) * 1.8)),
    )
    best: tuple[float, int, int, int, int, int] | None = None
    for start_index, first in enumerate(blocks):
        matched = 0
        for block in blocks[start_index:]:
            source_span = block.b + block.size - first.b
            if source_span > max_source_span:
                break
            matched += block.size
            query_end = block.a + block.size
            query_span = query_end - first.a
            coverage = matched / len(quote)
            density = matched / max(source_span, query_span, 1)
            score = coverage * 2 + density
            candidate = (
                score,
                matched,
                first.a,
                first.b,
                query_end,
                block.b + block.size,
            )
            if best is None or candidate[:2] > best[:2]:
                best = candidate
    if best is None:
        return None

    _, matched, query_start, source_start, query_end, source_end = best
    coverage = matched / len(quote)
    if coverage < 0.8:
        return None
    source_start = max(0, source_start - query_start)
    source_end = min(
        len(source),
        source_end + (len(quote) - query_end),
    )
    if source_start >= source_end:
        return None
    candidate = source[source_start:source_end]
    similarity = SequenceMatcher(
        None,
        quote,
        candidate,
        autojunk=False,
    ).ratio()
    if similarity < 0.76:
        return None

    original_start = source_positions[source_start]
    original_end = source_positions[source_end - 1] + 1
    aligned = source_message[original_start:original_end].strip()
    if not aligned or len(aligned) > len(approximate_quote) * 2 + 160:
        return None
    return aligned


def _alignment_text(value: str) -> tuple[str, list[int]]:
    characters: list[str] = []
    positions: list[int] = []
    for index, character in enumerate(value):
        category = unicodedata.category(character)
        if character.isspace() or category.startswith(("P", "Z")):
            continue
        characters.append(character.casefold())
        positions.append(index)
    return "".join(characters), positions


def _non_overlapping_quote_spans(
    source_message: str,
    exact_quote: str,
) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    cursor = 0
    while cursor <= len(source_message) - len(exact_quote):
        start = source_message.find(exact_quote, cursor)
        if start < 0:
            break
        end = start + len(exact_quote)
        spans.append((start, end))
        cursor = end
    return spans
