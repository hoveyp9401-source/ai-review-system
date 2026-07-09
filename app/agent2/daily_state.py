from __future__ import annotations

from dataclasses import dataclass
from typing import Any


REPORT_FIELD_ORDER = ("today_work", "problems", "tomorrow_plan")
REPORT_FIELDS = set(REPORT_FIELD_ORDER)
DRAFT_ITEM_IDS_KEY = "_draft_item_ids"

PENDING_ACTION_KEY = "_pending_action"
PENDING_DRAFT_EDIT_KEY = "_pending_draft_edit"
PENDING_INTERACTION_KEY = "_pending_interaction"
PENDING_QUALITY_CLARIFICATION_KEY = "_pending_quality_clarification"
PENDING_DAILY_CANDIDATE_KEY = "_agent2_pending_daily_candidate"
UNRESOLVED_DRAFT_EDIT_KEY = "_unresolved_draft_edit"

PENDING_STATE_KEYS = (
    PENDING_ACTION_KEY,
    PENDING_DRAFT_EDIT_KEY,
    PENDING_INTERACTION_KEY,
    PENDING_QUALITY_CLARIFICATION_KEY,
    PENDING_DAILY_CANDIDATE_KEY,
    UNRESOLVED_DRAFT_EDIT_KEY,
)

CONFIRMATION_PENDING_KEYS = (
    PENDING_ACTION_KEY,
    PENDING_DRAFT_EDIT_KEY,
    PENDING_INTERACTION_KEY,
)

FIELD_LABELS = {
    "today_work": "今日工作",
    "problems": "问题/风险",
    "tomorrow_plan": "明日计划",
}


@dataclass(frozen=True)
class DailyItemReference:
    field: str
    item_index: int = 0
    item_id: str = ""
    text: str = ""
    source: str = ""

    @property
    def field_label(self) -> str:
        return FIELD_LABELS.get(self.field, "")

    def target_tuple(self) -> tuple[str, int] | None:
        if self.field in REPORT_FIELDS and self.item_index >= 1:
            return self.field, self.item_index
        return None


def normalized_section_status(section_status: dict[str, Any] | None) -> dict[str, Any]:
    return dict(section_status or {}) if isinstance(section_status, dict) else {}


def pending_keys(section_status: dict[str, Any] | None) -> list[str]:
    status = normalized_section_status(section_status)
    return [key for key in PENDING_STATE_KEYS if status.get(key)]


def has_pending_daily_candidate(section_status: dict[str, Any] | None) -> bool:
    return pending_daily_candidate(section_status) is not None


def has_confirmation_pending(section_status: dict[str, Any] | None) -> bool:
    status = normalized_section_status(section_status)
    pending_action = str(status.get(PENDING_ACTION_KEY) or "")
    if pending_action:
        return True
    pending_edit = status.get(PENDING_DRAFT_EDIT_KEY)
    if isinstance(pending_edit, dict) and pending_edit.get("requires_confirmation"):
        return True
    pending_interaction = status.get(PENDING_INTERACTION_KEY)
    if isinstance(pending_interaction, dict):
        pending_type = str(pending_interaction.get("type") or "")
        return "confirmation" in pending_type or pending_type in {"pending_batch_action", "awaiting_action_confirmation"}
    return False


def pending_daily_candidate(section_status: dict[str, Any] | None) -> DailyItemReference | None:
    return item_reference_from_payload(
        normalized_section_status(section_status).get(PENDING_DAILY_CANDIDATE_KEY),
        source_fallback="agent2_candidate",
    )


def last_modified_item(section_status: dict[str, Any] | None) -> DailyItemReference | None:
    status = normalized_section_status(section_status)
    for key in ("_agent2_last_modified_item", "_last_modified_item"):
        reference = item_reference_from_payload(status.get(key), source_fallback=key)
        if reference is not None:
            return reference
    return None


def correction_target_item(section_status: dict[str, Any] | None) -> DailyItemReference | None:
    status = normalized_section_status(section_status)
    return item_reference_from_payload(status.get("_correction_target"), source_fallback="_correction_target")


def focus_item(section_status: dict[str, Any] | None) -> DailyItemReference | None:
    for reference in (
        pending_daily_candidate(section_status),
        last_modified_item(section_status),
        correction_target_item(section_status),
    ):
        if reference is not None:
            return reference
    return None


def set_pending_daily_candidate(
    section_status: dict[str, Any] | None,
    payload: dict[str, Any],
    *,
    created_at: str = "",
) -> dict[str, Any]:
    status = normalized_section_status(section_status)
    stored = dict(payload or {})
    if created_at:
        stored["created_at"] = created_at
    status[PENDING_DAILY_CANDIDATE_KEY] = stored
    return status


def clear_pending_daily_candidate(section_status: dict[str, Any]) -> None:
    section_status.pop(PENDING_DAILY_CANDIDATE_KEY, None)


def item_reference_from_payload(payload: Any, *, source_fallback: str = "") -> DailyItemReference | None:
    if not isinstance(payload, dict):
        return None
    field = str(payload.get("field") or payload.get("section") or "")
    if field not in REPORT_FIELDS:
        return None
    try:
        item_index = int(payload.get("field_index") or payload.get("item_index") or 0)
    except (TypeError, ValueError):
        item_index = 0
    item_id = str(payload.get("item_id") or "")
    text = str(payload.get("text") or "")
    source = str(payload.get("source") or payload.get("source_text") or source_fallback or "")
    if item_index < 1 and not item_id:
        return None
    return DailyItemReference(field=field, item_index=item_index, item_id=item_id, text=text, source=source)
