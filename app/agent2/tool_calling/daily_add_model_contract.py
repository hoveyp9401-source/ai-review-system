from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import Field, model_validator

from app.agent2.tool_calling.contracts import (
    AddDailyItemsArgs,
    DailyEmptyFieldEvidence,
    DailyItemSourceEvidence,
    ReportField,
    StrictContract,
)

MODEL_ADD_DAILY_ITEMS_DESCRIPTION = (
    "Create one complete Daily Report write decision for the authenticated user. "
    "The model decides every item's Daily field and exact source passage; the server "
    "copies persisted text from that trusted passage, so do not return a separate "
    "content value. Include every independently editable asserted matter exactly once "
    "and keep completed work, current problems/risks, and definite future plans in "
    "today_work, problems, and tomorrow_plan respectively. Do not turn a negation, "
    "condition, possibility, quotation, question, or attributed statement into the "
    "user's own completed fact. Each source_evidence.exact_quote must be one complete "
    "contiguous verbatim passage from its one-based current source message, preserving "
    "actors, attribution, negation, conditions, deadlines, consequences, exceptions, "
    "quantities, and pending decisions. Separate independently editable action-object "
    "pairs and never overlap their source passages. Use server_default when the report "
    "date is not explicit. A section heading or work-time phrase such as 今日工作 or "
    "今天完成 states report content, not an explicit calendar assignment to the "
    "report. "
    "Use another date_selection only with the matching trusted target or complete "
    "current-message report-date evidence. With server_default, omit date_expression, "
    "proposed_date, date_evidence, report_id, and expected_version. With "
    "trusted_report, supply report_id and expected_version and omit date expressions. "
    "Record an explicitly empty "
    "field only with matching current-message evidence. Set submit_after_write only "
    "when this same user turn explicitly requests submission. Return the whole write "
    "in one call; never return a partial item batch. Before returning, audit all three "
    "Daily sections (today_work, problems, tomorrow_plan) against the entire source. "
    "If the source supplies a matter for a section, include it; never stop after a "
    "numbered today_work list while later risk or plan material remains."
)


class DailyItemInput(StrictContract):
    """Small model decision; persisted text always comes from server-owned source."""

    field: ReportField
    source_evidence: DailyItemSourceEvidence


class ModelAddDailyItemsArgs(AddDailyItemsArgs):
    """LLM-facing Daily add contract without duplicated model-authored content."""

    items: tuple[DailyItemInput, ...] = Field(default=(), max_length=100)


class FocusedDailyFields(StrictContract):
    today_work: tuple[DailyItemSourceEvidence, ...] = Field(
        max_length=100
    )
    problems: tuple[DailyItemSourceEvidence, ...] = Field(max_length=100)
    tomorrow_plan: tuple[DailyItemSourceEvidence, ...] = Field(
        max_length=100
    )


class FocusedDailyPlanArguments(StrictContract):
    date_selection: Literal["server_default"]
    fields: FocusedDailyFields
    empty_field_evidence: tuple[DailyEmptyFieldEvidence, ...] = Field(
        max_length=3
    )
    submit_after_write: bool
    reply: str = Field(min_length=1, max_length=8000)


def focused_daily_plan_parameters_schema() -> dict[str, Any]:
    return FocusedDailyPlanArguments.model_json_schema()


def compile_focused_daily_plan_arguments(
    arguments: Any,
) -> tuple[dict[str, Any], str]:
    plan = FocusedDailyPlanArguments.model_validate(arguments)
    compact_arguments = {
        "date_selection": plan.date_selection,
        "items": [
            {
                "field": field,
                "source_evidence": evidence.model_dump(mode="json"),
            }
            for field in ("today_work", "problems", "tomorrow_plan")
            for evidence in getattr(plan.fields, field)
        ],
        "acknowledged_empty_fields": [
            evidence.field for evidence in plan.empty_field_evidence
        ],
        "empty_field_evidence": [
            evidence.model_dump(mode="json")
            for evidence in plan.empty_field_evidence
        ],
        "submit_after_write": plan.submit_after_write,
    }
    return compile_model_add_daily_items(compact_arguments), plan.reply


class FocusedDailyAddDecision(StrictContract):
    decision: Literal["daily_add", "not_daily", "clarification"]
    arguments: dict[str, Any] | None = None
    reply: str | None = Field(default=None, min_length=1, max_length=8000)

    @model_validator(mode="after")
    def require_decision_payload(self) -> "FocusedDailyAddDecision":
        if self.decision == "daily_add":
            if self.arguments is None:
                raise ValueError("daily_add requires arguments")
            return self
        if self.arguments is not None:
            raise ValueError("non-write decisions cannot carry arguments")
        if self.decision == "clarification" and self.reply is None:
            raise ValueError("clarification requires a reply")
        if self.decision == "not_daily" and self.reply is not None:
            raise ValueError("not_daily cannot carry a reply")
        return self


class FocusedDailyReviewDecision(StrictContract):
    decision: Literal["approve", "repair", "fallback", "reject"]
    reason: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def require_reject_reason(self) -> "FocusedDailyReviewDecision":
        if self.decision != "approve" and self.reason is None:
            raise ValueError("non-approved Daily candidate requires a reason")
        if self.decision == "approve" and self.reason is not None:
            raise ValueError("approved Daily candidate cannot carry a reason")
        return self


class FocusedDailyReviewArguments(StrictContract):
    decision: Literal["approve", "repair", "fallback"]
    reason: str = Field(max_length=1000)

    @model_validator(mode="after")
    def require_non_approval_reason(self) -> "FocusedDailyReviewArguments":
        if self.decision != "approve" and not self.reason.strip():
            raise ValueError("non-approved Daily candidate requires a reason")
        return self


def focused_daily_review_parameters_schema() -> dict[str, Any]:
    return FocusedDailyReviewArguments.model_json_schema()


def parse_focused_daily_review_arguments(
    arguments: Any,
) -> tuple[str, str | None]:
    decision = FocusedDailyReviewArguments.model_validate(arguments)
    return decision.decision, (
        decision.reason.strip() or None
    )


def model_add_daily_items_schema() -> dict[str, Any]:
    return ModelAddDailyItemsArgs.model_json_schema()


def compile_model_add_daily_items(arguments: Any) -> dict[str, Any]:
    """Compile a compact model decision into the unchanged execution contract."""

    if not isinstance(arguments, dict):
        return AddDailyItemsArgs.model_validate(arguments).model_dump(mode="json")

    items = arguments.get("items")
    if isinstance(items, (list, tuple)) and any(
        isinstance(item, dict) and "content" in item for item in items
    ):
        # Accept already-issued calls during a rolling release. The trusted-source
        # binder still replaces every model-authored content value before writing.
        return AddDailyItemsArgs.model_validate(arguments).model_dump(mode="json")

    proposal = ModelAddDailyItemsArgs.model_validate(arguments)
    compiled = proposal.model_dump(mode="json")
    for item in compiled["items"]:
        item["content"] = item["source_evidence"]["exact_quote"]
    return AddDailyItemsArgs.model_validate(compiled).model_dump(mode="json")


def parse_focused_daily_add_decision(
    raw_content: str,
) -> tuple[str, dict[str, Any] | None, str | None]:
    decoded = json.loads(raw_content)
    decision = FocusedDailyAddDecision.model_validate(decoded)
    compiled = None
    if decision.arguments is not None:
        raw_arguments = dict(decision.arguments)
        raw_fields = raw_arguments.pop("fields", None)
        if isinstance(raw_fields, dict):
            raw_fields = {
                field: [
                    _normalize_focused_source_evidence(item)
                    for item in (raw_fields.get(field) or [])
                ]
                for field in (
                    "today_work",
                    "problems",
                    "tomorrow_plan",
                )
                if field in raw_fields
            }
        fields = FocusedDailyFields.model_validate(
            raw_fields
        )
        raw_arguments["items"] = [
            {
                "field": field,
                "source_evidence": evidence.model_dump(mode="json"),
            }
            for field in ("today_work", "problems", "tomorrow_plan")
            for evidence in getattr(fields, field)
        ]
        if "acknowledged_empty_fields" not in raw_arguments:
            empty_evidence = raw_arguments.get("empty_field_evidence")
            if isinstance(empty_evidence, (list, tuple)):
                raw_arguments["acknowledged_empty_fields"] = [
                    item.get("field")
                    for item in empty_evidence
                    if isinstance(item, dict)
                ]
        compiled = compile_model_add_daily_items(raw_arguments)
    return decision.decision, compiled, decision.reply


def _normalize_focused_source_evidence(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    evidence = (
        item["source_evidence"]
        if set(item) == {"source_evidence"}
        else item
    )
    if (
        isinstance(evidence, dict)
        and "exact_quote" not in evidence
        and isinstance(evidence.get("quote"), str)
    ):
        evidence = {
            key: value
            for key, value in evidence.items()
            if key != "quote"
        }
        evidence["exact_quote"] = item.get("quote") or (
            item.get("source_evidence", {}).get("quote")
            if isinstance(item.get("source_evidence"), dict)
            else None
        )
    return evidence


def parse_focused_daily_review_decision(
    raw_content: str,
) -> tuple[str, str | None]:
    decoded = json.loads(raw_content)
    if isinstance(decoded, str):
        if decoded == "approve":
            return "approve", None
        if decoded in {"reject", "not_daily", "clarification"}:
            return "fallback", decoded
    if not isinstance(decoded, dict):
        raise ValueError("focused Daily review must return an object")
    decision = decoded.get("decision")
    if decision == "approve":
        return "approve", None
    if decision == "repair":
        reason = decoded.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("focused Daily repair requires a reason")
        return "repair", reason.strip()
    if decision in {"reject", "fallback", "not_daily", "clarification"}:
        reason = decoded.get("reason")
        return "fallback", (
            reason.strip()
            if isinstance(reason, str) and reason.strip()
            else str(decision)
        )
    raise ValueError("focused Daily review returned an unknown decision")
