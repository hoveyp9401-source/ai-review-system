from __future__ import annotations

from typing import Any, Literal
import hashlib

from pydantic import BaseModel, ConfigDict, Field, field_validator


DailyIntentOperation = Literal[
    "fill",
    "edit",
    "confirm",
    "query_current",
    "query_history",
    "copy_previous",
    "clear",
    "revoke",
    "no_write",
    "unknown",
]
DailyIntentTargetField = Literal["today_work", "problems", "tomorrow_plan", "all", "none", "unknown"]
DailyIntentConfidence = Literal["high", "medium", "low"]

REPORT_FIELDS = {"today_work", "problems", "tomorrow_plan"}
DESTRUCTIVE_ACTIONS = {"delete_item", "clear_field", "clear_all", "unsubmit_report", "restore_snapshot"}
DAILY_NO_CONFIRM_ACTIONS = {
    "delete_item",
    "clear_field",
    "clear_all",
    "unsubmit_report",
    "restore_snapshot",
    "update_historical_report",
}
FILL_ACTIONS = {"append_items", "replace_field"}
EDIT_ACTIONS = {"replace_text", "merge_items", "move_item", "delete_item", "polish_items", "restore_snapshot"}
COPY_PREVIOUS_ACTIONS = {
    "load_reference_report",
    "complete_previous_plan_item",
    "complete_all_previous_plan_items",
    "rollover_previous_plan_items",
}


class DailyIntentFrame(BaseModel):
    """Protocol frame for all daily-report intent decisions.

    This is an adapter-facing contract. It is safe to build in observe-only mode
    before the executor is changed to consume it directly.
    """

    model_config = ConfigDict(extra="ignore")

    workflow: Literal["daily_report"] = "daily_report"
    operation: DailyIntentOperation = "unknown"
    target_date: str = ""
    target_field: DailyIntentTargetField = "none"
    target_items: list[int] = Field(default_factory=list)
    content: list[str] = Field(default_factory=list)
    should_write: bool = False
    needs_confirmation: bool = False
    pending_relation: str = "none"
    confidence: DailyIntentConfidence = "low"
    source: str = ""
    branch: str = ""
    safety_flags: list[str] = Field(default_factory=list)
    reason: str = ""
    raw_text_hash: str = ""
    raw_text_chars: int = 0

    @field_validator("target_items", mode="before")
    @classmethod
    def clean_target_items(cls, value: Any) -> list[int]:
        candidates = value if isinstance(value, list) else [value]
        result: list[int] = []
        for item in candidates:
            try:
                number = int(item)
            except (TypeError, ValueError):
                continue
            if number > 0 and number not in result:
                result.append(number)
        return result

    @field_validator("content", "safety_flags", mode="before")
    @classmethod
    def clean_string_list(cls, value: Any) -> list[str]:
        candidates = value if isinstance(value, list) else [value]
        result: list[str] = []
        for item in candidates:
            text = "" if item is None else str(item).strip()
            if text and text not in result:
                result.append(text)
        return result

    def safe_timing_payload(self) -> dict[str, Any]:
        return {
            "workflow": self.workflow,
            "operation": self.operation,
            "target_date": self.target_date,
            "target_field": self.target_field,
            "target_items": list(self.target_items),
            "content_count": len(self.content),
            "should_write": self.should_write,
            "needs_confirmation": self.needs_confirmation,
            "pending_relation": self.pending_relation,
            "confidence": self.confidence,
            "source": self.source,
            "branch": self.branch,
            "safety_flags": list(self.safety_flags),
            "reason": self.reason,
            "raw_text_hash": self.raw_text_hash,
            "raw_text_chars": self.raw_text_chars,
        }


def daily_intent_from_action_plan(
    plan: Any,
    *,
    target_date: Any = "",
    raw_text: str = "",
    source: str = "",
    branch: str = "",
) -> DailyIntentFrame:
    actions = list(getattr(plan, "actions", []) or [])
    confidence = _clean_confidence(getattr(plan, "confidence", "low"))
    operation = _operation_from_plan(plan, actions)
    fields = _target_fields(actions)
    target_field = _target_field(operation, fields, actions)
    needs_confirmation = _needs_confirmation(plan, actions)
    safety_flags = _safety_flags(
        plan,
        actions,
        operation=operation,
        target_field=target_field,
        fields=fields,
        needs_confirmation=needs_confirmation,
        confidence=confidence,
    )
    return DailyIntentFrame(
        operation=operation,
        target_date=_target_date_text(target_date, actions),
        target_field=target_field,
        target_items=_target_items(actions),
        content=_content_from_actions(actions),
        should_write=bool(getattr(plan, "should_write", False)),
        needs_confirmation=needs_confirmation,
        pending_relation=_pending_relation(plan),
        confidence=confidence,
        source=source,
        branch=branch,
        safety_flags=safety_flags,
        reason=str(getattr(plan, "reason", "") or ""),
        raw_text_hash=_hash_text(raw_text),
        raw_text_chars=len(str(raw_text or "")),
    )


def daily_intent_timing_payload(frame: DailyIntentFrame) -> dict[str, Any]:
    return frame.safe_timing_payload()


def _operation_from_plan(plan: Any, actions: list[Any]) -> DailyIntentOperation:
    intent = str(getattr(plan, "intent", "") or "")
    action_types = {str(getattr(action, "type", "") or "") for action in actions}
    should_write = bool(getattr(plan, "should_write", False))
    if intent == "confirm_submit" or "submit_report" in action_types:
        return "confirm"
    if intent == "query_current":
        return "query_current"
    if intent == "query_history" or "query_history" in action_types:
        return "query_history"
    if "clear_all" in action_types or "clear_field" in action_types:
        return "clear"
    if "unsubmit_report" in action_types:
        return "revoke"
    if action_types & COPY_PREVIOUS_ACTIONS:
        return "copy_previous"
    if not should_write and (not actions or action_types <= {"ask_clarification", "no_op"}):
        return "no_write"
    if action_types & EDIT_ACTIONS:
        return "edit"
    if action_types & FILL_ACTIONS:
        return "fill" if intent == "fill_report" else "edit"
    if intent == "fill_report":
        return "fill"
    if intent in {"edit_draft", "polish_report"}:
        return "edit"
    if intent in {"emotional_feedback", "system_action", "unclear"} and not should_write:
        return "no_write"
    return "unknown"


def _target_fields(actions: list[Any]) -> list[str]:
    fields: list[str] = []
    for action in actions:
        for attr in ("field", "target_field", "target_section", "source_field", "source_section"):
            field = str(getattr(action, attr, "") or "")
            if field in REPORT_FIELDS and field not in fields:
                fields.append(field)
    return fields


def _target_field(operation: str, fields: list[str], actions: list[Any]) -> DailyIntentTargetField:
    action_types = {str(getattr(action, "type", "") or "") for action in actions}
    if "clear_all" in action_types or operation in {"confirm", "revoke"}:
        return "all"
    if len(fields) == 1:
        return fields[0]  # type: ignore[return-value]
    if len(fields) > 1:
        return "all"
    return "none"


def _target_items(actions: list[Any]) -> list[int]:
    result: list[int] = []
    for action in actions:
        for item in list(getattr(action, "item_indices", []) or []):
            _append_positive_int(result, item)
        _append_positive_int(result, getattr(action, "target_item_index", None))
    return result


def _content_from_actions(actions: list[Any]) -> list[str]:
    result: list[str] = []
    for action in actions:
        for attr in ("items", "completed_items", "unfinished_items", "cancelled_items"):
            for item in list(getattr(action, attr, []) or []):
                _append_text(result, item)
        for attr in ("new_value", "source_item_text", "old_value"):
            _append_text(result, getattr(action, attr, ""))
    return result


def _needs_confirmation(plan: Any, actions: list[Any]) -> bool:
    if any(
        bool(getattr(action, "requires_confirmation", False))
        and str(getattr(action, "type", "") or "") not in DAILY_NO_CONFIRM_ACTIONS
        for action in actions
    ):
        return True
    pending = getattr(plan, "pending_interaction_to_set", None)
    pending_type = str(getattr(pending, "type", "") or "") if pending is not None else ""
    return "confirmation" in pending_type or pending_type in {"pending_batch_action", "awaiting_action_confirmation"}


def _pending_relation(plan: Any) -> str:
    sets_pending = getattr(plan, "pending_interaction_to_set", None) is not None
    clears_pending = bool(getattr(plan, "clear_pending_interaction", False))
    if sets_pending and clears_pending:
        return "sets_and_clears_pending"
    if sets_pending:
        return "sets_pending"
    if clears_pending:
        return "clears_pending"
    return "none"


def _safety_flags(
    plan: Any,
    actions: list[Any],
    *,
    operation: str,
    target_field: str,
    fields: list[str],
    needs_confirmation: bool,
    confidence: str,
) -> list[str]:
    flags: list[str] = []
    action_types = {str(getattr(action, "type", "") or "") for action in actions}
    should_write = bool(getattr(plan, "should_write", False))
    if confidence == "low":
        flags.append("low_confidence")
    if should_write and not actions and operation not in {"confirm", "no_write"}:
        flags.append("write_without_actions")
    if should_write and operation in {"fill", "edit", "clear"} and target_field in {"none", "unknown"}:
        flags.append("missing_target_field")
    if len(fields) > 1:
        flags.append("multiple_target_fields")
    if not should_write and operation in {"fill", "edit", "clear", "revoke"}:
        flags.append("non_write_operation")
    if _pending_relation(plan) != "none":
        flags.append("pending_transition")
    return flags


def _clean_confidence(value: Any) -> DailyIntentConfidence:
    text = str(value or "").strip().lower()
    return text if text in {"high", "medium", "low"} else "low"  # type: ignore[return-value]


def _target_date_text(target_date: Any, actions: list[Any]) -> str:
    for action in actions:
        value = getattr(action, "target_date", None)
        if value:
            return str(value)
    if hasattr(target_date, "isoformat"):
        return target_date.isoformat()
    return str(target_date or "")


def _append_positive_int(result: list[int], value: Any) -> None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return
    if number > 0 and number not in result:
        result.append(number)


def _append_text(result: list[str], value: Any) -> None:
    text = "" if value is None else str(value).strip()
    if text and text not in result:
        result.append(text)


def _hash_text(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]
