from __future__ import annotations

from datetime import date
from typing import Any


REPORT_FIELDS = {"today_work", "problems", "tomorrow_plan"}
EDIT_CURSOR_PENDING_TYPES = {"historical_report_edit_flow", "awaiting_dated_report_action", "current_report_edit_flow"}


def build_historical_edit_cursor(
    *,
    target_date: date | str,
    current_report: dict[str, Any],
    focused_section: str = "none",
    stage: str | None = None,
) -> dict[str, Any]:
    field = focused_section if focused_section in REPORT_FIELDS else "none"
    return {
        "mode": "historical_edit",
        "status": "active",
        "target_date": target_date.isoformat() if isinstance(target_date, date) else str(target_date),
        "focused_section": field,
        "target_field": field,
        "stage": stage or ("awaiting_field_edit_content" if field in REPORT_FIELDS else "awaiting_edit_instruction"),
        "active_draft_snapshot": _clean_report_snapshot(current_report),
    }


def build_current_edit_cursor(
    *,
    target_date: date | str,
    current_report: dict[str, Any],
    focused_section: str = "none",
    stage: str | None = None,
) -> dict[str, Any]:
    field = focused_section if focused_section in REPORT_FIELDS else "none"
    return {
        "mode": "current_edit",
        "status": "active",
        "target_date": target_date.isoformat() if isinstance(target_date, date) else str(target_date),
        "focused_section": field,
        "target_field": field,
        "stage": stage or ("awaiting_field_edit_content" if field in REPORT_FIELDS else "awaiting_edit_instruction"),
        "active_draft_snapshot": _clean_report_snapshot(current_report),
    }


def normalize_edit_cursor(pending_interaction: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(pending_interaction, dict):
        return None
    pending_type = str(pending_interaction.get("type") or "")
    if pending_type not in EDIT_CURSOR_PENDING_TYPES:
        return None

    context = pending_interaction.get("context") if isinstance(pending_interaction.get("context"), dict) else {}
    cursor = context.get("edit_cursor") if isinstance(context.get("edit_cursor"), dict) else {}
    mode = str(cursor.get("mode") or ("current_edit" if pending_type == "current_report_edit_flow" else "historical_edit"))
    target_date = str(
        cursor.get("target_date")
        or context.get("target_date")
        or pending_interaction.get("target_date")
        or ""
    )
    if not target_date:
        return None

    focused_section = str(
        cursor.get("focused_section")
        or cursor.get("target_field")
        or context.get("focus_section")
        or pending_interaction.get("target_field")
        or "none"
    )
    if focused_section not in REPORT_FIELDS:
        focused_section = "none"

    snapshot = cursor.get("active_draft_snapshot")
    if not isinstance(snapshot, dict):
        snapshot = context.get("current_report") if isinstance(context.get("current_report"), dict) else {}

    normalized = dict(cursor)
    normalized.update(
        {
            "mode": mode,
            "status": "active",
            "target_date": target_date,
            "focused_section": focused_section,
            "target_field": focused_section,
            "stage": str(
                cursor.get("stage")
                or context.get("stage")
                or ("awaiting_field_edit_content" if focused_section in REPORT_FIELDS else "awaiting_edit_instruction")
            ),
            "active_draft_snapshot": _clean_report_snapshot(snapshot),
        }
    )
    return normalized


def with_edit_cursor(pending_interaction: dict[str, Any]) -> dict[str, Any]:
    pending = dict(pending_interaction or {})
    cursor = normalize_edit_cursor(pending)
    if cursor is None:
        return pending

    context = dict(pending.get("context") or {})
    context["edit_cursor"] = cursor
    context.setdefault("target_date", cursor["target_date"])
    context.setdefault("stage", cursor["stage"])
    context["current_report"] = cursor["active_draft_snapshot"]
    if cursor["focused_section"] in REPORT_FIELDS:
        context["focus_section"] = cursor["focused_section"]

    pending["context"] = context
    pending["target_field"] = cursor["focused_section"]
    return pending


def cursor_target_date(cursor: dict[str, Any] | None) -> str:
    return str((cursor or {}).get("target_date") or "")


def cursor_focused_section(cursor: dict[str, Any] | None) -> str:
    field = str((cursor or {}).get("focused_section") or (cursor or {}).get("target_field") or "none")
    return field if field in REPORT_FIELDS else "none"


def cursor_report(cursor: dict[str, Any] | None) -> dict[str, list[str]]:
    snapshot = (cursor or {}).get("active_draft_snapshot")
    if not isinstance(snapshot, dict):
        snapshot = {}
    return {
        field: _clean_items(snapshot.get(field))
        for field in ("today_work", "problems", "tomorrow_plan")
    }


def _clean_report_snapshot(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "today_work": _clean_items(report.get("today_work")),
        "problems": _clean_items(report.get("problems")),
        "tomorrow_plan": _clean_items(report.get("tomorrow_plan")),
        "status": str(report.get("status") or ""),
    }


def _clean_items(value: Any) -> list[str]:
    candidates = value if isinstance(value, list) else ([] if value in (None, "") else [value])
    return [text for item in candidates if (text := str(item or "").strip())]
