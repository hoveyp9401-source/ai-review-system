from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ReportField = Literal["today_work", "problems", "tomorrow_plan", "meta_notes", "none", "unknown"]
AgentIntent = Literal[
    "fill_report",
    "edit_draft",
    "confirm_submit",
    "query_current",
    "query_history",
    "system_action",
    "polish_report",
    "emotional_feedback",
    "unclear",
]
AgentActionType = Literal[
    "append_items",
    "replace_field",
    "replace_text",
    "merge_items",
    "move_item",
    "delete_item",
    "clear_field",
    "clear_all",
    "submit_report",
    "unsubmit_report",
    "query_history",
    "restore_snapshot",
    "polish_items",
    "ask_clarification",
    "no_op",
    "load_reference_report",
    "complete_previous_plan_item",
    "complete_all_previous_plan_items",
    "rollover_previous_plan_items",
    "update_historical_report",
]


def _clean_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _clean_items(value: Any) -> list[str]:
    if value is None:
        return []
    candidates = value if isinstance(value, list) else [value]
    result: list[str] = []
    for item in candidates:
        text = _clean_text(item)
        if text:
            result.append(text)
    return result


class AgentAction(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: AgentActionType
    field: ReportField = "none"
    items: list[str] = Field(default_factory=list)
    source_field: ReportField = "none"
    target_field: ReportField = "none"
    source_section: ReportField = "none"
    target_section: ReportField = "none"
    source_item_text: str = ""
    old_value: str = ""
    new_value: str = ""
    target_item_index: int | None = None
    item_indices: list[int] = Field(default_factory=list)
    completed_items: list[str] = Field(default_factory=list)
    unfinished_items: list[str] = Field(default_factory=list)
    cancelled_items: list[str] = Field(default_factory=list)
    reference_report: dict[str, Any] = Field(default_factory=dict)
    source: str = ""
    target_date: str | None = None
    requires_confirmation: bool = False
    confirmation_message: str = ""
    reason: str = ""

    @model_validator(mode="before")
    @classmethod
    def normalize_schema_aliases(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        action_type = data.get("action_type")
        if action_type and not data.get("type"):
            data["type"] = _action_type_alias(str(action_type))
        if data.get("section") and not data.get("field"):
            data["field"] = data.get("section")
        if data.get("source_section") and not data.get("source_field"):
            data["source_field"] = data.get("source_section")
        if data.get("target_section") and not data.get("target_field"):
            data["target_field"] = data.get("target_section")
        if data.get("target_text") and not data.get("source_item_text"):
            data["source_item_text"] = data.get("target_text")
        if data.get("new_text") and not data.get("new_value"):
            data["new_value"] = data.get("new_text")
        if data.get("new_text") and not data.get("items") and data.get("type") in {"append_items", "replace_field"}:
            data["items"] = [data.get("new_text")]
        return data

    @field_validator("items", "completed_items", "unfinished_items", "cancelled_items", mode="before")
    @classmethod
    def clean_items(cls, value: Any) -> list[str]:
        return _clean_items(value)

    @field_validator("field", "source_field", "target_field", "source_section", "target_section", mode="before")
    @classmethod
    def clean_field(cls, value: Any) -> str:
        return "none" if value in (None, "") else str(value).strip()

    @field_validator("source_item_text", "old_value", "new_value", "confirmation_message", "reason", "source", mode="before")
    @classmethod
    def clean_text(cls, value: Any) -> str:
        return _clean_text(value)

    @field_validator("target_item_index", mode="before")
    @classmethod
    def clean_target_item_index(cls, value: Any) -> int | None:
        if value in (None, ""):
            return None
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if number >= 0 else None

    @field_validator("item_indices", mode="before")
    @classmethod
    def clean_item_indices(cls, value: Any) -> list[int]:
        if value is None:
            return []
        candidates = value if isinstance(value, list) else [value]
        result: list[int] = []
        for item in candidates:
            try:
                number = int(item)
            except (TypeError, ValueError):
                continue
            if number >= 0:
                result.append(number)
        return result


def _action_type_alias(action_type: str) -> str:
    aliases = {
        "add_report_item": "append_items",
        "update_report_item": "replace_text",
        "merge_report_items": "merge_items",
        "merge_report_item": "merge_items",
        "delete_report_item": "delete_item",
        "clear_section": "clear_field",
        "submit_report": "submit_report",
        "unsubmit_report": "unsubmit_report",
        "ask_clarification": "ask_clarification",
        "no_op": "no_op",
        "load_reference_report": "load_reference_report",
        "complete_previous_plan_item": "complete_previous_plan_item",
        "complete_all_previous_plan_items": "complete_all_previous_plan_items",
        "rollover_previous_plan_items": "rollover_previous_plan_items",
        "update_historical_report": "update_historical_report",
    }
    return aliases.get(action_type, action_type)


class PendingInteractionPlan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str
    operation: str = ""
    target_field: ReportField = "none"
    context: dict[str, Any] = Field(default_factory=dict)


class ActionPlan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    intent: AgentIntent
    confidence: Literal["high", "medium", "low"] = "low"
    should_write: bool = False
    actions: list[AgentAction] = Field(default_factory=list)
    reply_to_user: str = ""
    clarification_question: str = ""
    pending_interaction_to_set: PendingInteractionPlan | None = None
    clear_pending_interaction: bool = False
    reason: str = ""

    @field_validator("reply_to_user", "clarification_question", "reason", mode="before")
    @classmethod
    def clean_text(cls, value: Any) -> str:
        return _clean_text(value)
