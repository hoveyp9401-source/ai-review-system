from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


REPORT_CONTENT_FIELDS = ("today_work", "problems", "tomorrow_plan")
REPORT_STATUS_FIELDS = ("status",)

REPORT_WRITE_ACTIONS = {
    "report_update",
    "agent_edit_draft",
    "complete_report",
    "confirm_submit",
    "confirm_but_incomplete",
    "whole_report_copy_to_today",
    "move_existing_draft_to_report_date",
    "cleared_after_date_reassignment",
    "recent_report_copy_needs_confirmation",
}

REPORT_NON_WRITE_ACTIONS = {
    "agent_emotional_feedback",
    "agent_fill_report_prompt",
    "agent_low_confidence",
    "agent_no_change",
    "agent_non_report_query",
    "agent_query_history",
    "agent_system_action",
    "agent_unclear",
    "current_report_query",
    "historical_report_delete_blocked",
    "history_query",
    "previous_report_cutoff",
    "report_agent_error",
    "save_recent_report_context",
}


@dataclass(frozen=True)
class LegacyImpact:
    """Replay-only classification of what the old system actually affected."""

    kind: str
    write_impact: bool
    changed_fields: list[str] = field(default_factory=list)
    reason: str = ""

    def as_observation(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "write_impact": self.write_impact,
            "changed_fields": list(self.changed_fields),
            "reason": self.reason,
        }


def classify_legacy_impact(
    *,
    source_kind: str,
    legacy_processed: bool,
    legacy_action: str = "",
    before_snapshot: dict[str, Any] | None = None,
    after_snapshot: dict[str, Any] | None = None,
    legacy_report_id: str = "",
    legacy_status: str = "",
) -> LegacyImpact:
    """Classify a replayed legacy event without influencing live routing.

    The goal is to keep rollout metrics honest: blocking an old display/query
    response is a different risk from blocking an old daily-report write. When
    the source lacks enough evidence, return `unknown` instead of guessing.
    """

    if not legacy_processed:
        return LegacyImpact(kind="not_processed", write_impact=False, reason="legacy did not process this message")

    source = str(source_kind or "")
    action = str(legacy_action or "")
    before = before_snapshot if isinstance(before_snapshot, dict) else {}
    after = after_snapshot if isinstance(after_snapshot, dict) else {}

    if source == "performance":
        return LegacyImpact(kind="external_workflow", write_impact=False, reason="performance replay is not a daily write")

    if source == "webhook" and not action and not before and not after:
        if legacy_report_id:
            return LegacyImpact(kind="unknown", write_impact=False, reason="webhook has report_id but no daily snapshot")
        return LegacyImpact(kind="non_write", write_impact=False, reason="webhook was processed without report id")

    changed_fields = _snapshot_changed_fields(before, after, action=action)
    if changed_fields:
        return LegacyImpact(
            kind="daily_write",
            write_impact=True,
            changed_fields=changed_fields,
            reason="daily report snapshot changed",
        )

    if action in REPORT_WRITE_ACTIONS:
        return LegacyImpact(kind="daily_write", write_impact=True, reason=f"legacy action {action} is write-impact")

    if action in REPORT_NON_WRITE_ACTIONS:
        return LegacyImpact(kind="non_write", write_impact=False, reason=f"legacy action {action} is response-only")

    if legacy_status in {"completed", "pending_confirmation", "collecting"} and legacy_report_id and not action:
        return LegacyImpact(kind="unknown", write_impact=False, reason="legacy processed report without action snapshot")

    return LegacyImpact(kind="unknown", write_impact=False, reason="insufficient replay evidence")


def _snapshot_changed_fields(before: dict[str, Any], after: dict[str, Any], *, action: str = "") -> list[str]:
    if not before and not after:
        return []

    changed: list[str] = []
    before_has_report = bool(before.get("report_id")) or any(before.get(field) for field in REPORT_CONTENT_FIELDS)
    after_has_report = bool(after.get("report_id")) or any(after.get(field) for field in REPORT_CONTENT_FIELDS)

    if not before and after:
        if action in REPORT_WRITE_ACTIONS and after_has_report:
            return ["report"]
        return []

    for field in REPORT_CONTENT_FIELDS:
        if _normalized_items(before.get(field)) != _normalized_items(after.get(field)):
            changed.append(field)

    for field in REPORT_STATUS_FIELDS:
        before_value = str(before.get(field) or "")
        after_value = str(after.get(field) or "")
        if before_value != after_value and after_value in {"collecting", "pending_confirmation", "completed", "skipped", "cancelled"}:
            changed.append(field)

    before_report_id = str(before.get("report_id") or "")
    after_report_id = str(after.get("report_id") or "")
    if before_report_id != after_report_id and (before_report_id or action in REPORT_WRITE_ACTIONS) and after_has_report:
        changed.append("report_id")

    if not changed and before_has_report and after_has_report:
        for field in ("today_work_count", "problems_count", "tomorrow_plan_count"):
            if _as_int(before.get(field)) != _as_int(after.get(field)):
                changed.append(field)

    return changed


def _normalized_items(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
