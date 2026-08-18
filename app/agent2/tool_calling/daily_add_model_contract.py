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
    "For every item, return content as concise professional Daily Report wording and "
    "also return the exact current-message passage that proves it. Conservative cleanup "
    "may remove oral filler, repetition, and obvious grammatical noise, but must not add, "
    "remove, generalize, or change any actor, project, action, object, date, number, "
    "negation, condition, completion state, risk, or plan. Include every independently "
    "editable asserted matter exactly once "
    "and keep completed work, current problems/risks, and definite future plans in "
    "today_work, problems, and tomorrow_plan respectively. Do not turn a negation, "
    "condition, possibility, quotation, question, or attributed statement into the "
    "user's own completed fact. Each source_evidence.exact_quote must be one complete "
    "contiguous verbatim passage from its one-based current source message, preserving "
    "actors, attribution, negation, conditions, deadlines, consequences, exceptions, "
    "quantities, and pending decisions. One item is the smallest coherent work topic or "
    "outcome the user would update as one report line, not the smallest verb-object pair. "
    "Split when the source switches to an unrelated goal, project, case group, deliverable, "
    "or workstream even without punctuation; keep coordinated actions together when the "
    "user presents them as one coherent topic or shared workstream. Never overlap source "
    "passages. Use server_default when the report "
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
    """Model wording plus the exact source used for independent fact review."""

    field: ReportField
    content: str = Field(min_length=1, max_length=4000)
    source_evidence: DailyItemSourceEvidence

    @model_validator(mode="before")
    @classmethod
    def accept_pre_content_rolling_calls(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "content" in value:
            return value
        evidence = value.get("source_evidence")
        if isinstance(evidence, dict) and isinstance(
            evidence.get("exact_quote"), str
        ):
            return {**value, "content": evidence["exact_quote"]}
        return value


class ModelAddDailyItemsArgs(AddDailyItemsArgs):
    """LLM-facing Daily add contract with separately reviewable wording."""

    items: tuple[DailyItemInput, ...] = Field(default=(), max_length=100)


class FocusedDailyItemInput(StrictContract):
    content: str = Field(min_length=1, max_length=4000)
    source_evidence: DailyItemSourceEvidence

    @model_validator(mode="before")
    @classmethod
    def accept_pre_content_rolling_calls(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "content" in value:
            return value
        evidence = (
            value.get("source_evidence")
            if isinstance(value.get("source_evidence"), dict)
            else value
        )
        if isinstance(evidence.get("exact_quote"), str):
            return {
                "content": evidence["exact_quote"],
                "source_evidence": evidence,
            }
        return value


class FocusedDailyFields(StrictContract):
    today_work: tuple[FocusedDailyItemInput, ...] = Field(
        max_length=100
    )
    problems: tuple[FocusedDailyItemInput, ...] = Field(max_length=100)
    tomorrow_plan: tuple[FocusedDailyItemInput, ...] = Field(
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
    arguments = _normalize_focused_empty_field_evidence(arguments)
    plan = FocusedDailyPlanArguments.model_validate(arguments)
    ordered_fields = ("today_work", "problems", "tomorrow_plan")
    explicit_empty_fields = [
        evidence.field for evidence in plan.empty_field_evidence
    ]
    has_report_content = any(
        getattr(plan.fields, field) for field in ordered_fields
    )
    reviewed_omitted_empty_fields = (
        [
            field
            for field in ordered_fields
            if not getattr(plan.fields, field)
            and field not in explicit_empty_fields
        ]
        if plan.submit_after_write and has_report_content
        else []
    )
    compact_arguments = {
        "date_selection": plan.date_selection,
        "items": [
            {
                "field": field,
                "content": item.content,
                "source_evidence": item.source_evidence.model_dump(mode="json"),
            }
            for field in ordered_fields
            for item in getattr(plan.fields, field)
        ],
        "acknowledged_empty_fields": [
            field
            for field in ordered_fields
            if field in {
                *explicit_empty_fields,
                *reviewed_omitted_empty_fields,
            }
        ],
        "empty_field_evidence": [
            evidence.model_dump(mode="json")
            for evidence in plan.empty_field_evidence
        ],
        "reviewed_omitted_empty_fields": reviewed_omitted_empty_fields,
        "submit_after_write": plan.submit_after_write,
    }
    return compile_model_add_daily_items(compact_arguments), plan.reply


def _normalize_focused_empty_field_evidence(arguments: Any) -> Any:
    """Accept a model's redundant exact empty quote without widening execution."""

    if not isinstance(arguments, dict):
        return arguments
    raw_items = arguments.get("empty_field_evidence")
    if not isinstance(raw_items, (list, tuple)):
        return arguments
    normalized_items = []
    changed = False
    for item in raw_items:
        if not isinstance(item, dict):
            normalized_items.append(item)
            continue
        evidence = item.get("source_evidence")
        exact_quote = (
            evidence.get("exact_quote")
            if isinstance(evidence, dict)
            else None
        )
        if not isinstance(exact_quote, str) or not exact_quote.strip():
            normalized_items.append(item)
            continue
        normalized_items.append(
            {
                **item,
                "source_evidence": {
                    key: value
                    for key, value in evidence.items()
                    if key != "exact_quote"
                },
            }
        )
        changed = True
    if not changed:
        return arguments
    return {**arguments, "empty_field_evidence": normalized_items}


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
    schema = ModelAddDailyItemsArgs.model_json_schema()
    # Produced only by the server after the focused planner and its independent
    # reviewer agree on a self-contained explicit submission.
    schema.get("properties", {}).pop(
        "reviewed_omitted_empty_fields",
        None,
    )
    schema.get("properties", {}).pop("content_reviewed", None)
    required = schema.get("required")
    if isinstance(required, list):
        schema["required"] = [
            name
            for name in required
            if name != "reviewed_omitted_empty_fields"
            and name != "content_reviewed"
        ]
    return schema


def compile_model_add_daily_items(arguments: Any) -> dict[str, Any]:
    """Compile a compact model decision into the unchanged execution contract."""

    if isinstance(arguments, dict):
        arguments = {
            key: value
            for key, value in arguments.items()
            if key != "content_reviewed"
        }
    if not isinstance(arguments, dict):
        compiled = AddDailyItemsArgs.model_validate(arguments).model_dump(
            mode="json"
        )
        if not compiled.get("reviewed_omitted_empty_fields"):
            compiled.pop("reviewed_omitted_empty_fields", None)
        return compiled

    items = arguments.get("items")
    if isinstance(items, (list, tuple)) and any(
        isinstance(item, dict) and "content" in item for item in items
    ):
        # Accept already-issued calls during a rolling release. The source binder
        # still replaces wording unless the server later attaches review proof.
        compiled = AddDailyItemsArgs.model_validate(arguments).model_dump(
            mode="json"
        )
        if not compiled.get("reviewed_omitted_empty_fields"):
            compiled.pop("reviewed_omitted_empty_fields", None)
        return compiled

    proposal = ModelAddDailyItemsArgs.model_validate(arguments)
    compiled = proposal.model_dump(mode="json")
    compiled = AddDailyItemsArgs.model_validate(compiled).model_dump(mode="json")
    if not compiled.get("reviewed_omitted_empty_fields"):
        compiled.pop("reviewed_omitted_empty_fields", None)
    return compiled


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
                "content": item.content,
                "source_evidence": item.source_evidence.model_dump(mode="json"),
            }
            for field in ("today_work", "problems", "tomorrow_plan")
            for item in getattr(fields, field)
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
