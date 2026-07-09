from __future__ import annotations

from datetime import date
from typing import Any

from app.agent.edit_cursor import normalize_edit_cursor
from app.models import DailyReport, User


def build_agent_context(
    *,
    user: User,
    existing: DailyReport | None,
    raw_input: str,
    report_date: date,
    previous_report: DailyReport | None = None,
    user_habits: list[Any] | None = None,
) -> dict[str, Any]:
    section_status = dict(getattr(existing, "section_status", {}) or {})
    fragments = list(getattr(existing, "input_fragments", []) or [])[-12:] if existing else []
    recent_user_messages = [
        str(fragment.get("raw_input") or "").strip()
        for fragment in fragments
        if isinstance(fragment, dict) and str(fragment.get("raw_input") or "").strip()
    ][-5:]
    pending_interaction = _scoped_pending_interaction(
        section_status.get("_pending_interaction"),
        user=user,
        existing=existing,
        report_date=report_date,
    )
    if pending_interaction is None:
        section_status.pop("_pending_interaction", None)
    pending_type = pending_interaction.get("type") if isinstance(pending_interaction, dict) else None
    pending_operation = pending_interaction.get("operation") if isinstance(pending_interaction, dict) else None
    edit_cursor = normalize_edit_cursor(pending_interaction if isinstance(pending_interaction, dict) else None)
    last_modified_item = _scoped_report_memory_item(section_status.get("_last_modified_item"), user=user, existing=existing, report_date=report_date)
    correction_target = _scoped_report_memory_item(section_status.get("_correction_target"), user=user, existing=existing, report_date=report_date)
    previous_draft_snapshot = section_status.get("_previous_draft_snapshot")
    reference_report_context = section_status.get("_reference_report_context")
    conversation_context = {
        "recent_turns": _recent_turns_from_fragments(fragments),
        "recent_user_messages": recent_user_messages,
        "last_modified_item": last_modified_item,
        "correction_target": correction_target,
        "active_pending_interaction": pending_interaction,
        "active_edit_cursor": edit_cursor,
        "previous_draft_snapshot": previous_draft_snapshot,
        "reference_report_context": reference_report_context,
        "previous_report_context": _report_context(previous_report),
    }
    return {
        "user": {
            "id": str(user.id),
            "name": getattr(user, "name", "") or "",
            "timezone": getattr(user, "timezone", "") or "",
        },
        "report_date": report_date.isoformat(),
        "status": getattr(existing, "status", "collecting") if existing else "collecting",
        "current_message": raw_input,
        "current_draft": {
            "today_work": list(getattr(existing, "today_work", []) or []),
            "problems": list(getattr(existing, "problems", []) or []),
            "tomorrow_plan": list(getattr(existing, "tomorrow_plan", []) or []),
            "quality_warning": getattr(existing, "quality_warning", None) if existing else None,
        },
        "section_status": section_status,
        "conversation_context": conversation_context,
        "last_modified_item": last_modified_item,
        "correction_target": correction_target,
        "pending_action": section_status.get("_pending_action"),
        "pending_action_payload": section_status.get("_pending_action_payload"),
        "pending_interaction": pending_interaction,
        "edit_cursor": edit_cursor,
        "pending_state": {
            "pending_action": section_status.get("_pending_action") or pending_operation,
            "pending_section": pending_interaction.get("target_field") if isinstance(pending_interaction, dict) else None,
            "awaiting_section": pending_type == "awaiting_append_target",
            "awaiting_confirmation": pending_type in {"awaiting_append_target_confirmation", "awaiting_action_confirmation", "pending_batch_action"},
            "awaiting_content": pending_type == "awaiting_append_content",
            "awaiting_quality_confirmation": pending_type == "awaiting_content_quality_confirmation",
            "awaiting_dated_report_action": pending_type == "awaiting_dated_report_action",
            "awaiting_edit_cursor": edit_cursor is not None,
            "edit_cursor_mode": edit_cursor.get("mode") if edit_cursor else None,
            "edit_cursor_target_date": edit_cursor.get("target_date") if edit_cursor else None,
            "edit_cursor_focused_section": edit_cursor.get("focused_section") if edit_cursor else None,
            "awaiting_historical_edit": bool(edit_cursor and edit_cursor.get("mode") == "historical_edit"),
            "historical_target_date": edit_cursor.get("target_date") if edit_cursor and edit_cursor.get("mode") == "historical_edit" else None,
            "historical_focused_section": edit_cursor.get("focused_section") if edit_cursor and edit_cursor.get("mode") == "historical_edit" else None,
        },
        "pending_quality_clarification": section_status.get("_pending_quality_clarification"),
        "previous_draft_snapshot": previous_draft_snapshot,
        "reference_report_context": reference_report_context,
        "previous_report_context": conversation_context["previous_report_context"],
        "user_habits": _habit_context(user_habits or []),
        "recent_user_messages": recent_user_messages,
    }


def _habit_context(habits: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for habit in habits[:12]:
        result.append(
            {
                "habit_type": str(getattr(habit, "habit_type", "") or ""),
                "trigger_text": str(getattr(habit, "trigger_text", "") or ""),
                "meaning": str(getattr(habit, "meaning", "") or ""),
                "confidence": float(getattr(habit, "confidence", 0) or 0),
                "evidence_count": int(getattr(habit, "evidence_count", 0) or 0),
            }
        )
    return result


def _report_context(report: DailyReport | None) -> dict[str, Any] | None:
    if report is None:
        return None
    return {
        "report_date": report.report_date.isoformat(),
        "status": report.status,
        "today_work": list(report.today_work or []),
        "problems": list(report.problems or []),
        "tomorrow_plan": list(report.tomorrow_plan or []),
    }


def _recent_turns_from_fragments(fragments: list[Any]) -> list[dict[str, Any]]:
    recent: list[dict[str, Any]] = []
    for fragment in fragments[-12:]:
        if not isinstance(fragment, dict):
            continue
        raw_input = str(fragment.get("raw_input") or "").strip()
        if not raw_input:
            continue
        structured = fragment.get("structured") if isinstance(fragment.get("structured"), dict) else {}
        recent.append(
            {
                "role": "user",
                "raw_input": raw_input[:500],
                "received_at": str(fragment.get("received_at") or ""),
                "decision_summary": _decision_summary(structured),
            }
        )
    return recent


def _decision_summary(payload: dict[str, Any]) -> dict[str, Any]:
    report_agent = payload.get("report_agent") if isinstance(payload.get("report_agent"), dict) else {}
    actions = report_agent.get("actions") if isinstance(report_agent.get("actions"), list) else []
    return {
        "intent": str(report_agent.get("intent") or payload.get("intent") or ""),
        "confidence": str(report_agent.get("confidence") or payload.get("confidence") or ""),
        "should_write": report_agent.get("should_write") if "should_write" in report_agent else payload.get("should_write"),
        "actions": [_action_summary(action) for action in actions[:5] if isinstance(action, dict)],
    }


def _action_summary(action: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": str(action.get("type") or ""),
        "field": str(action.get("field") or action.get("target_field") or ""),
        "item_indices": action.get("item_indices") if isinstance(action.get("item_indices"), list) else [],
        "target_item_index": action.get("target_item_index"),
        "source": str(action.get("source") or ""),
    }


def _scoped_report_memory_item(
    value: Any,
    *,
    user: User,
    existing: DailyReport | None,
    report_date: date,
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    expected = {
        "user_id": str(getattr(user, "id", "") or ""),
        "report_date": report_date.isoformat(),
        "report_id": str(getattr(existing, "id", "") or ""),
    }
    for key, expected_value in expected.items():
        actual_value = str(value.get(key) or "")
        if actual_value and expected_value and actual_value != expected_value:
            return None
    return {
        "section": str(value.get("section") or ""),
        "item_index": value.get("item_index"),
        "old_content": str(value.get("old_content") or ""),
        "new_content": str(value.get("new_content") or ""),
        "source_user_message": str(value.get("source_user_message") or ""),
        "timestamp": str(value.get("timestamp") or ""),
    }


def _scoped_pending_interaction(
    pending_interaction: Any,
    *,
    user: User,
    existing: DailyReport | None,
    report_date: date,
) -> dict[str, Any] | None:
    if not isinstance(pending_interaction, dict):
        return None
    context = pending_interaction.get("context") if isinstance(pending_interaction.get("context"), dict) else {}
    scope = context.get("_scope")
    if not isinstance(scope, dict):
        return pending_interaction
    expected = {
        "user_id": str(getattr(user, "id", "") or ""),
        "report_date": report_date.isoformat(),
        "report_id": str(getattr(existing, "id", "") or ""),
    }
    for key, expected_value in expected.items():
        actual_value = str(scope.get(key) or "")
        if actual_value and expected_value and actual_value != expected_value:
            return None
    return pending_interaction
