from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _as_clean_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, list):
        candidates = value
    else:
        candidates = [value]

    result: list[str] = []
    for item in candidates:
        if item is None:
            continue
        if isinstance(item, dict):
            text = _text_from_item_dict(item)
        else:
            text = str(item).strip()
        if text:
            result.append(text)
    return result


def _text_from_item_dict(item: dict[str, Any]) -> str:
    for key in ("text", "content", "value", "item", "description"):
        value = item.get(key)
        if value is not None:
            text = str(value).strip()
            if text:
                return text
    return ""


class ContentQualityCheck(BaseModel):
    model_config = ConfigDict(extra="ignore")

    target_field: str = "none"
    target_index: int = Field(default=0, ge=0)
    clarity: str = "high"
    clarification_needed: bool = False
    clarification_question: str = ""
    quality_warning: str = ""

    @field_validator("target_field", mode="before")
    @classmethod
    def clean_target_field(cls, value: Any) -> str:
        allowed = {"today_work", "problems", "tomorrow_plan", "none"}
        text = "" if value is None else str(value).strip()
        return text if text in allowed else "none"

    @field_validator("clarity", mode="before")
    @classmethod
    def clean_clarity(cls, value: Any) -> str:
        allowed = {"high", "medium", "low"}
        text = "" if value is None else str(value).strip().lower()
        return text if text in allowed else "high"

    @field_validator("clarification_question", "quality_warning", mode="before")
    @classmethod
    def clean_text(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()[:300]


def _as_quality_checks(value: Any) -> list[ContentQualityCheck]:
    if value is None:
        return []
    candidates = value if isinstance(value, list) else [value]
    result: list[ContentQualityCheck] = []
    for item in candidates:
        if isinstance(item, ContentQualityCheck):
            result.append(item)
        elif isinstance(item, dict):
            result.append(ContentQualityCheck.model_validate(item))
    return result


class StructuredDailyReport(BaseModel):
    model_config = ConfigDict(extra="ignore")

    today_work: list[str] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)
    tomorrow_plan: list[str] = Field(default_factory=list)
    meta_notes: list[str] = Field(default_factory=list)
    content_quality: list[ContentQualityCheck] = Field(default_factory=list)
    emotion: str = ""
    completeness: float = Field(default=0.0, ge=0.0, le=1.0)

    @field_validator("today_work", "problems", "tomorrow_plan", "meta_notes", mode="before")
    @classmethod
    def clean_list(cls, value: Any) -> list[str]:
        return _as_clean_list(value)

    @field_validator("content_quality", mode="before")
    @classmethod
    def clean_content_quality(cls, value: Any) -> list[ContentQualityCheck]:
        return _as_quality_checks(value)

    @field_validator("emotion", mode="before")
    @classmethod
    def clean_emotion(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()[:64]


class DailyInputIntentDecision(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message_kind: str = "report_content"
    intent: str = "continue_collecting"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    target_field: str = "none"
    operation: str = "none"
    item_refs: list[int] = Field(default_factory=list)
    new_content: str = ""
    actions: list[dict[str, Any]] = Field(default_factory=list)
    relation_to_existing: str = "none"
    matched_field: str = "none"
    matched_item_index: int = Field(default=0, ge=0)
    should_merge: bool = False
    should_append: bool = False
    risk_level: str = "low"
    needs_clarification: bool = False
    should_discard_previous: bool = False
    should_update_report: bool = True
    clarification_question: str = ""
    reason: str = ""
    non_report_reply: str = ""

    @field_validator("message_kind", mode="before")
    @classmethod
    def clean_message_kind(cls, value: Any) -> str:
        allowed = {
            "report_content",
            "draft_edit_instruction",
            "report_control_action",
            "non_report_interaction",
            "long_report_content",
            "quality_clarification_response",
            "ambiguous",
        }
        text = "" if value is None else str(value).strip()
        return text if text in allowed else "ambiguous"

    @field_validator("intent", mode="before")
    @classmethod
    def clean_intent(cls, value: Any) -> str:
        allowed = {
            "continue_collecting",
            "append_to_existing",
            "modify_field",
            "replace_current_report",
            "clear_current_report",
            "confirm_submit",
            "courtesy_reply",
            "postpone_reply",
            "casual_or_invalid",
            "ask_system",
            "non_report_interaction",
            "draft_edit_instruction",
            "uncertain_high_risk",
        }
        text = "" if value is None else str(value).strip()
        return text if text in allowed else "uncertain_high_risk"

    @field_validator("target_field", mode="before")
    @classmethod
    def clean_target_field(cls, value: Any) -> str:
        allowed = {"today_work", "problems", "tomorrow_plan", "all", "none"}
        text = "" if value is None else str(value).strip()
        return text if text in allowed else "none"

    @field_validator("operation", mode="before")
    @classmethod
    def clean_operation(cls, value: Any) -> str:
        allowed = {
            "set_fields",
            "append",
            "modify_field",
            "replace_report",
            "clear_report",
            "merge_items",
            "delete_item",
            "rewrite_item",
            "move_item",
            "answer_question",
            "clarify",
            "none",
        }
        text = "" if value is None else str(value).strip()
        return text if text in allowed else "none"

    @field_validator("relation_to_existing", mode="before")
    @classmethod
    def clean_relation_to_existing(cls, value: Any) -> str:
        allowed = {"duplicate", "semantic_duplicate", "elaboration", "new_item", "unclear", "none"}
        text = "" if value is None else str(value).strip()
        return text if text in allowed else "none"

    @field_validator("matched_field", mode="before")
    @classmethod
    def clean_matched_field(cls, value: Any) -> str:
        allowed = {"today_work", "problems", "tomorrow_plan", "none"}
        text = "" if value is None else str(value).strip()
        return text if text in allowed else "none"

    @field_validator("risk_level", mode="before")
    @classmethod
    def clean_risk_level(cls, value: Any) -> str:
        allowed = {"low", "medium", "high"}
        text = "" if value is None else str(value).strip().lower()
        return text if text in allowed else "low"

    @field_validator("item_refs", mode="before")
    @classmethod
    def clean_item_refs(cls, value: Any) -> list[int]:
        if value is None:
            return []
        candidates = value if isinstance(value, list) else [value]
        refs: list[int] = []
        for item in candidates:
            try:
                number = int(item)
            except (TypeError, ValueError):
                continue
            if number > 0:
                refs.append(number)
        return refs[:5]

    @field_validator("actions", mode="before")
    @classmethod
    def clean_actions(cls, value: Any) -> list[dict[str, Any]]:
        if value is None:
            return []
        candidates = value if isinstance(value, list) else [value]
        actions: list[dict[str, Any]] = []
        for item in candidates:
            if isinstance(item, dict):
                actions.append(dict(item))
        return actions[:5]

    @field_validator("new_content", mode="before")
    @classmethod
    def clean_new_content(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()[:500]

    @field_validator("clarification_question", "reason", "non_report_reply", mode="before")
    @classmethod
    def clean_clarification_question(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()[:300]


def _clean_field_name(value: Any, *, allow_all: bool = False) -> str:
    allowed = {"today_work", "problems", "tomorrow_plan", "none"}
    if allow_all:
        allowed.add("all")
    text = "" if value is None else str(value).strip()
    return text if text in allowed else "none"


def _as_positive_ints(value: Any, *, limit: int = 20) -> list[int]:
    if value is None:
        return []
    candidates = value if isinstance(value, list) else [value]
    refs: list[int] = []
    for item in candidates:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number > 0:
            refs.append(number)
    return refs[:limit]


class DraftFieldUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    field: str = "none"
    mode: str = "keep"
    items: list[str] = Field(default_factory=list)
    item_refs: list[int] = Field(default_factory=list)

    @field_validator("field", mode="before")
    @classmethod
    def clean_field(cls, value: Any) -> str:
        return _clean_field_name(value)

    @field_validator("mode", mode="before")
    @classmethod
    def clean_mode(cls, value: Any) -> str:
        allowed = {"keep", "replace", "append", "merge", "clear", "remove_items"}
        text = "" if value is None else str(value).strip()
        return text if text in allowed else "keep"

    @field_validator("items", mode="before")
    @classmethod
    def clean_items(cls, value: Any) -> list[str]:
        return _as_clean_list(value)

    @field_validator("item_refs", mode="before")
    @classmethod
    def clean_item_refs(cls, value: Any) -> list[int]:
        return _as_positive_ints(value)


class DraftMoveItems(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source_field: str = "none"
    destination_field: str = "none"
    item_refs: list[int] = Field(default_factory=list)
    source_item_text: str = ""

    @field_validator("source_field", "destination_field", mode="before")
    @classmethod
    def clean_field(cls, value: Any) -> str:
        return _clean_field_name(value)

    @field_validator("item_refs", mode="before")
    @classmethod
    def clean_item_refs(cls, value: Any) -> list[int]:
        return _as_positive_ints(value)

    @field_validator("source_item_text", mode="before")
    @classmethod
    def clean_source_item_text(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()[:500]


class DraftDeleteItems(BaseModel):
    model_config = ConfigDict(extra="ignore")

    target_field: str = "none"
    item_refs: list[int] = Field(default_factory=list)
    target_item_text: str = ""

    @field_validator("target_field", mode="before")
    @classmethod
    def clean_target_field(cls, value: Any) -> str:
        return _clean_field_name(value)

    @field_validator("item_refs", mode="before")
    @classmethod
    def clean_item_refs(cls, value: Any) -> list[int]:
        return _as_positive_ints(value)

    @field_validator("target_item_text", mode="before")
    @classmethod
    def clean_target_item_text(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()[:500]


class DraftRestorePrevious(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    reason: str = ""

    @field_validator("reason", mode="before")
    @classmethod
    def clean_reason(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()[:300]


class DraftHistoryQuery(BaseModel):
    model_config = ConfigDict(extra="ignore")

    requested: bool = False
    date: str = ""
    field: str = "all"
    question: str = ""

    @field_validator("field", mode="before")
    @classmethod
    def clean_field(cls, value: Any) -> str:
        return _clean_field_name(value, allow_all=True)

    @field_validator("date", "question", mode="before")
    @classmethod
    def clean_text(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()[:300]


class DraftDecision(BaseModel):
    model_config = ConfigDict(extra="ignore")

    decision_type: str = "no_op"
    message_kind: str = "no_op"
    operation: str = "none"
    target_field: str = "none"
    item_refs: list[int] = Field(default_factory=list)
    new_content: str = ""
    user_intent: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    field_updates: list[DraftFieldUpdate] = Field(default_factory=list)
    move_items: list[DraftMoveItems] = Field(default_factory=list)
    delete_items: list[DraftDeleteItems] = Field(default_factory=list)
    restore_previous: DraftRestorePrevious = Field(default_factory=DraftRestorePrevious)
    history_query: DraftHistoryQuery = Field(default_factory=DraftHistoryQuery)
    should_write: bool = False
    requires_user_confirmation: bool = False
    clarification_question: str = ""
    reply_to_user: str = ""
    needs_clarification: bool = False
    risk_level: str = "low"
    reason: str = ""

    @field_validator("decision_type", mode="before")
    @classmethod
    def clean_decision_type(cls, value: Any) -> str:
        allowed = {
            "report_update",
            "draft_edit",
            "answer_only",
            "history_query",
            "control_action",
            "clarification",
            "no_op",
        }
        text = "" if value is None else str(value).strip()
        return text if text in allowed else "no_op"

    @field_validator("message_kind", mode="before")
    @classmethod
    def clean_message_kind(cls, value: Any) -> str:
        allowed = {
            "report_content",
            "draft_edit_instruction",
            "report_control_action",
            "non_report_interaction",
            "long_report_content",
            "quality_clarification_response",
            "ambiguous",
            "no_op",
        }
        text = "" if value is None else str(value).strip()
        return text if text in allowed else "no_op"

    @field_validator("operation", mode="before")
    @classmethod
    def clean_operation(cls, value: Any) -> str:
        allowed = {
            "set_fields",
            "append",
            "replace_report",
            "clear_report",
            "merge_items",
            "delete_item",
            "rewrite_item",
            "move_item",
            "restore_previous",
            "answer_question",
            "history_query",
            "clarify",
            "none",
        }
        text = "" if value is None else str(value).strip()
        return text if text in allowed else "none"

    @field_validator("target_field", mode="before")
    @classmethod
    def clean_target_field(cls, value: Any) -> str:
        return _clean_field_name(value, allow_all=True)

    @field_validator("item_refs", mode="before")
    @classmethod
    def clean_item_refs(cls, value: Any) -> list[int]:
        return _as_positive_ints(value)

    @field_validator("risk_level", mode="before")
    @classmethod
    def clean_risk_level(cls, value: Any) -> str:
        allowed = {"low", "medium", "high"}
        text = "" if value is None else str(value).strip().lower()
        return text if text in allowed else "low"

    @field_validator("field_updates", mode="before")
    @classmethod
    def clean_field_updates(cls, value: Any) -> list[DraftFieldUpdate]:
        if value is None:
            return []
        candidates = value if isinstance(value, list) else [value]
        updates: list[DraftFieldUpdate] = []
        for item in candidates:
            if isinstance(item, DraftFieldUpdate):
                updates.append(item)
            elif isinstance(item, dict):
                updates.append(DraftFieldUpdate.model_validate(item))
        return updates[:10]

    @field_validator("move_items", mode="before")
    @classmethod
    def clean_move_items(cls, value: Any) -> list[DraftMoveItems]:
        if value is None:
            return []
        candidates = value if isinstance(value, list) else [value]
        moves: list[DraftMoveItems] = []
        for item in candidates:
            if isinstance(item, DraftMoveItems):
                moves.append(item)
            elif isinstance(item, dict):
                moves.append(DraftMoveItems.model_validate(item))
        return moves[:10]

    @field_validator("delete_items", mode="before")
    @classmethod
    def clean_delete_items(cls, value: Any) -> list[DraftDeleteItems]:
        if value is None:
            return []
        candidates = value if isinstance(value, list) else [value]
        deletes: list[DraftDeleteItems] = []
        for item in candidates:
            if isinstance(item, DraftDeleteItems):
                deletes.append(item)
            elif isinstance(item, dict):
                deletes.append(DraftDeleteItems.model_validate(item))
        return deletes[:10]

    @field_validator("new_content", mode="before")
    @classmethod
    def clean_new_content(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()[:1000]

    @field_validator("clarification_question", "reply_to_user", "reason", "user_intent", mode="before")
    @classmethod
    def clean_text(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()[:600]


class TeamSummaryPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    key_work: list[str] = Field(default_factory=list)
    major_problems: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    tomorrow_plan_distribution: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("key_work", "major_problems", "risks", mode="before")
    @classmethod
    def clean_summary_list(cls, value: Any) -> list[str]:
        return _as_clean_list(value)

    @field_validator("tomorrow_plan_distribution", mode="before")
    @classmethod
    def clean_distribution(cls, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        cleaned: list[dict[str, Any]] = []
        for item in value:
            if isinstance(item, dict):
                cleaned.append(item)
            elif item:
                cleaned.append({"topic": str(item)})
        return cleaned


class DailyReportView(BaseModel):
    id: str
    user_id: str
    team_id: str
    date: date
    today_work: list[str]
    problems: list[str]
    tomorrow_plan: list[str]
    completeness_score: float
    status: str
    confirmation_type: str | None = None
    confirmed_by_user: bool | None = None
    quality_warning: str | None = None
