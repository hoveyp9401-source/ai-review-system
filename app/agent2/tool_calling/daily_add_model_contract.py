from __future__ import annotations

from typing import Any

from pydantic import Field

from app.agent2.tool_calling.contracts import (
    AddDailyItemsArgs,
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
    "in one call; never return a partial item batch."
)


class DailyItemInput(StrictContract):
    """Small model decision; persisted text always comes from server-owned source."""

    field: ReportField
    source_evidence: DailyItemSourceEvidence


class ModelAddDailyItemsArgs(AddDailyItemsArgs):
    """LLM-facing Daily add contract without duplicated model-authored content."""

    items: tuple[DailyItemInput, ...] = Field(default=(), max_length=30)


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
