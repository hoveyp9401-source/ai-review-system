from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


_DAILY_SECTION_LABELS = {
    "今日完成": "today_work",
    "今日工作": "today_work",
    "今天完成": "today_work",
    "今天工作": "today_work",
    "当日完成": "today_work",
    "当日工作": "today_work",
    "问题与风险": "problems",
    "风险与问题": "problems",
    "问题和风险": "problems",
    "风险和问题": "problems",
    "问题/风险": "problems",
    "风险/问题": "problems",
    "明日计划": "tomorrow_plan",
    "明天计划": "tomorrow_plan",
    "次日计划": "tomorrow_plan",
}
_LABEL_PATTERN = "|".join(
    re.escape(value)
    for value in sorted(_DAILY_SECTION_LABELS, key=len, reverse=True)
)
_SECTION_HEADER = re.compile(
    rf"(?m)(?:^|\n)[ \t]*(?:【(?P<bracket>{_LABEL_PATTERN})】"
    rf"|\[(?P<ascii>{_LABEL_PATTERN})\]"
    rf"|(?P<plain>{_LABEL_PATTERN})(?:[：:]|[ \t]*(?=\r?$)))[ \t]*(?:[：:][ \t]*)?"
)
_ITEM_NUMBER = re.compile(r"^(?:\d+|[一二三四五六七八九十]+)[.、．)）][ \t]*")
_ITEM_SEPARATOR = re.compile(r"[；;\n]+")
_DAILY_SECTION_CUE = re.compile(
    r"^(?:(?:今日|今天)?(?:的)?(?:问题和风险|问题与风险|风险和问题|问题)"
    r"(?:就是|是|填|写|改为|改成|调整为|[，,:：]\s*)(?P<problems>.+)"
    r"|(?:明日|明天)?计划(?:填|写|记|改为|改成|调整为)"
    r"(?P<plan>.+))$",
    re.DOTALL,
)
_DAILY_ITEM_SECTION_CORRECTION = re.compile(
    r"^(?P<value>\S.+?)[，,]\s*这是(?P<target>明天|明日|今天|今日)"
    r"(?:的)?(?:工作|计划|工作计划)$",
    re.DOTALL,
)
_DAILY_COMPOUND_SEPARATOR = re.compile(
    r"(?:\n\s*\n+|(?<=[。！？!?])\s*(?="
    r"(?:今日|今天)?(?:的)?(?:问题和风险|问题与风险|风险和问题|问题)"
    r"(?:就是|是|填|写|改为|改成|调整为|[，,:：]\s*)))"
)


@dataclass(frozen=True)
class StructuredReportItem:
    field: str
    value: str
    start_offset: int
    end_offset: int


@dataclass(frozen=True)
class StructuredDailyDocument:
    items: tuple[StructuredReportItem, ...]
    fields: frozenset[str]


def parse_structured_daily_document(text: str) -> StructuredDailyDocument | None:
    """Parse a self-identifying Daily document without consulting conversation focus.

    A high-confidence document must identify both current work and the next-day
    plan.  This is deliberately a document-shape contract, not a classifier
    fallback: old Weekly/Monthly focus is never evidence about the document type.
    """

    source = str(text or "")
    headers = list(_SECTION_HEADER.finditer(source))
    if len(headers) < 2:
        return None
    items: list[StructuredReportItem] = []
    fields: set[str] = set()
    for index, header in enumerate(headers):
        label = next(
            value
            for value in (
                header.group("bracket"),
                header.group("ascii"),
                header.group("plain"),
            )
            if value
        )
        field = _DAILY_SECTION_LABELS[label]
        section_end = headers[index + 1].start() if index + 1 < len(headers) else len(source)
        section_items = _parse_section_items(
            source,
            field=field,
            start_offset=header.end(),
            end_offset=section_end,
        )
        if section_items:
            fields.add(field)
            items.extend(section_items)
    if not {"today_work", "tomorrow_plan"}.issubset(fields):
        return None
    return StructuredDailyDocument(items=tuple(items), fields=frozenset(fields))


def structured_daily_semantic_payload(text: str) -> dict[str, Any] | None:
    document = parse_structured_daily_document(text)
    if document is None:
        return None
    entities: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    segments: list[dict[str, Any]] = []
    entity_ids: list[str] = []
    for index, item in enumerate(document.items, start=1):
        entity_id = f"structured-daily-event-{index}"
        action_id = f"structured-daily-capture-{index}"
        entity_ids.append(entity_id)
        entities.append(
            {
                "entity_id": entity_id,
                "entity_type": "daily_event",
                "value": item.value,
                "confidence": 1.0,
                "attributes": {"field": item.field},
            }
        )
        actions.append(
            {
                "action_id": action_id,
                "action_type": "capture_daily_event",
                "intent": "daily_append",
                "entity_ids": [entity_id],
                "parameters": {},
            }
        )
        segments.append(
            {
                "segment_id": f"structured-daily-segment-{index}",
                "text": text[item.start_offset:item.end_offset],
                "intents": ["daily_append"],
                "entity_ids": [entity_id],
                "action_ids": [action_id],
                "start_offset": item.start_offset,
                "end_offset": item.end_offset,
            }
        )
    return {
        "intents": ["daily_append"],
        "segments": segments,
        "entities": entities,
        "confidence": 1.0,
        "required_actions": actions,
        "clarification_need": None,
        "context_update": {
            "current_goal": "daily_report",
            "remember_entity_ids": entity_ids,
            "remember_turn": True,
        },
    }


def complete_daily_document_replace_semantic_payload(
    text: str,
    resources: dict[str, Any],
    *,
    document: StructuredDailyDocument | None = None,
) -> dict[str, Any] | None:
    """Replace all three explicitly supplied Daily sections as one decision."""

    source = str(text or "")
    document = document or parse_structured_daily_document(source)
    if document is None or set(document.fields) != {
        "today_work",
        "problems",
        "tomorrow_plan",
    }:
        return None
    draft = resources.get("daily_draft")
    draft = draft if isinstance(draft, dict) else {}
    report_id = str(draft.get("report_id") or "").strip()
    version = draft.get("version")
    if not report_id or not isinstance(version, int) or isinstance(version, bool):
        return None

    grouped: dict[str, list[StructuredReportItem]] = {}
    for item in document.items:
        grouped.setdefault(item.field, []).append(item)
    if any(not grouped.get(field) for field in document.fields):
        return None

    entities: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    segments: list[dict[str, Any]] = []
    entity_ids: list[str] = []
    ordered_fields = sorted(grouped, key=lambda field: grouped[field][0].start_offset)
    for index, field in enumerate(ordered_fields, start=1):
        items = grouped[field]
        start = items[0].start_offset
        end = items[-1].end_offset
        entity_id = f"complete-daily-section-{index}"
        action_id = f"complete-daily-replace-{index}"
        entity_ids.append(entity_id)
        entities.append(
            {
                "entity_id": entity_id,
                "entity_type": "daily_report",
                "value": source[start:end],
                "confidence": 1.0,
                "attributes": {
                    "report_id": report_id,
                    "version": version,
                    "field": field,
                    "items": [item.value for item in items],
                },
            }
        )
        actions.append(
            {
                "action_id": action_id,
                "action_type": "replace_daily_section",
                "intent": "daily_modify",
                "entity_ids": [entity_id],
                "parameters": {},
            }
        )
        segments.append(
            {
                "segment_id": f"complete-daily-segment-{index}",
                "text": source[start:end],
                "intents": ["daily_modify"],
                "entity_ids": [entity_id],
                "action_ids": [action_id],
                "start_offset": start,
                "end_offset": end,
            }
        )
    return {
        "intents": ["daily_modify"],
        "segments": segments,
        "entities": entities,
        "confidence": 1.0,
        "required_actions": actions,
        "clarification_need": None,
        "context_update": {
            "current_goal": "daily_modify",
            "remember_entity_ids": entity_ids,
            "remember_turn": True,
        },
    }


def daily_section_cue_semantic_payload(
    text: str,
    resources: dict[str, Any],
) -> dict[str, Any] | None:
    """Parse one explicit section replacement without treating missing fields as empty."""

    source = str(text or "").strip()
    match = _DAILY_SECTION_CUE.fullmatch(source)
    if match is None:
        return None
    group_name = "problems" if match.group("problems") is not None else "plan"
    value = str(match.group(group_name) or "").strip(" \t\r\n，,:：")
    if not value:
        return None
    field = "problems" if group_name == "problems" else "tomorrow_plan"
    return _daily_section_replace_payload(source, resources, field=field, items=[value])


def _daily_section_replace_payload(
    source: str,
    resources: dict[str, Any],
    *,
    field: str,
    items: list[str],
) -> dict[str, Any] | None:
    draft = resources.get("daily_draft")
    draft = draft if isinstance(draft, dict) else {}
    report_id = str(draft.get("report_id") or "").strip()
    version = draft.get("version")
    if not report_id or not isinstance(version, int) or isinstance(version, bool):
        return None
    entity_id = "daily-section-replace-target"
    action_id = "daily-section-replace-action"
    return {
        "intents": ["daily_modify"],
        "segments": [
            {
                "segment_id": "daily-section-replace-segment",
                "text": source,
                "intents": ["daily_modify"],
                "entity_ids": [entity_id],
                "action_ids": [action_id],
                "start_offset": 0,
                "end_offset": len(source),
            }
        ],
        "entities": [
            {
                "entity_id": entity_id,
                "entity_type": "daily_report",
                "value": source,
                "confidence": 1.0,
                "attributes": {
                    "report_id": report_id,
                    "version": version,
                    "field": field,
                    "items": items,
                },
            }
        ],
        "confidence": 1.0,
        "required_actions": [
            {
                "action_id": action_id,
                "action_type": "replace_daily_section",
                "intent": "daily_modify",
                "entity_ids": [entity_id],
                "parameters": {},
            }
        ],
        "clarification_need": None,
        "context_update": {
            "current_goal": "daily_modify",
            "remember_entity_ids": [entity_id],
            "remember_turn": True,
        },
    }


def daily_item_section_correction_semantic_payload(
    text: str,
    resources: dict[str, Any],
) -> dict[str, Any] | None:
    """Move an exact trusted item between Daily sections using its stable ID."""

    source = str(text or "").strip().rstrip("。！!")
    match = _DAILY_ITEM_SECTION_CORRECTION.fullmatch(source)
    if match is None:
        return None
    value = str(match.group("value") or "").strip(" ，,。")
    target_field = (
        "tomorrow_plan"
        if str(match.group("target") or "") in {"明天", "明日"}
        else "today_work"
    )
    source_field = "today_work" if target_field == "tomorrow_plan" else "tomorrow_plan"
    source_matches = [
        item
        for item in _trusted_daily_field_items(resources, source_field)
        if str(item.get("text") or "").strip() == value
    ]
    target_matches = [
        item
        for item in _trusted_daily_field_items(resources, target_field)
        if str(item.get("text") or "").strip() == value
    ]
    if len(source_matches) == 1 and not target_matches:
        item_id = str(source_matches[0].get("item_id") or "").strip()
        if not item_id:
            return _daily_clarification_payload(
                source,
                reason="daily_section_correction_target_unbound",
                question="找到了对应内容，但缺少稳定条目编号；本次没有修改。",
            )
        entity_id = "daily-section-correction-target"
        action_id = "daily-section-correction-action"
        return {
            "intents": ["daily_modify"],
            "segments": [
                {
                    "segment_id": "daily-section-correction-segment",
                    "text": source,
                    "intents": ["daily_modify"],
                    "entity_ids": [entity_id],
                    "action_ids": [action_id],
                    "start_offset": 0,
                    "end_offset": len(source),
                }
            ],
            "entities": [
                {
                    "entity_id": entity_id,
                    "entity_type": "daily_item_target",
                    "value": value,
                    "confidence": 1.0,
                    "attributes": {
                        "source_field": source_field,
                        "target_field": target_field,
                        "target_item_ids": [item_id],
                    },
                }
            ],
            "confidence": 1.0,
            "required_actions": [
                {
                    "action_id": action_id,
                    "action_type": "move_daily_items",
                    "intent": "daily_modify",
                    "entity_ids": [entity_id],
                    "parameters": {},
                }
            ],
            "clarification_need": None,
            "context_update": {
                "current_goal": "daily_report",
                "remember_entity_ids": [entity_id],
                "remember_turn": True,
            },
        }
    if len(target_matches) == 1 and not source_matches:
        return _daily_clarification_payload(
            source,
            reason="daily_item_already_in_target_section",
            question="这项内容已经在目标栏目中，本次没有修改。",
        )
    if source_matches or target_matches:
        return _daily_clarification_payload(
            source,
            reason="daily_section_correction_ambiguous",
            question="当前有多条相同内容，无法唯一确定要调整哪一条；本次没有修改。",
        )
    return _daily_append_payload(source, value=value, field=target_field)


def daily_compound_section_correction_semantic_payload(
    text: str,
    resources: dict[str, Any],
) -> dict[str, Any] | None:
    """Combine one item correction and one explicit section update by segment."""

    source = str(text or "")
    parts = [
        value.strip()
        for value in _DAILY_COMPOUND_SEPARATOR.split(source)
        if value.strip()
    ]
    if len(parts) != 2:
        return None
    correction = daily_item_section_correction_semantic_payload(parts[0], resources)
    section = daily_section_cue_semantic_payload(parts[1], resources)
    if correction is None or section is None:
        return None
    entities = [*(correction.get("entities") or []), *(section.get("entities") or [])]
    actions = [
        *(correction.get("required_actions") or []),
        *(section.get("required_actions") or []),
    ]
    segments: list[dict[str, Any]] = []
    for payload, part in ((correction, parts[0]), (section, parts[1])):
        offset = source.find(part)
        for raw_segment in payload.get("segments") or []:
            segment = dict(raw_segment)
            segment["start_offset"] = offset + int(segment.get("start_offset", 0))
            segment["end_offset"] = offset + int(segment.get("end_offset", len(part)))
            segments.append(segment)
    entity_ids = [str(item.get("entity_id") or "") for item in entities]
    return {
        "intents": list(
            dict.fromkeys(
                [*(correction.get("intents") or []), *(section.get("intents") or [])]
            )
        ),
        "segments": segments,
        "entities": entities,
        "confidence": 1.0,
        "required_actions": actions,
        "clarification_need": (
            correction.get("clarification_need") or section.get("clarification_need")
        ),
        "context_update": {
            "current_goal": "daily_report",
            "remember_entity_ids": [value for value in entity_ids if value],
            "remember_turn": True,
        },
    }


def _daily_append_payload(source: str, *, value: str, field: str) -> dict[str, Any]:
    entity_id = "daily-section-correction-new-item"
    action_id = "daily-section-correction-capture"
    return {
        "intents": ["daily_append"],
        "segments": [
            {
                "segment_id": "daily-section-correction-new-segment",
                "text": source,
                "intents": ["daily_append"],
                "entity_ids": [entity_id],
                "action_ids": [action_id],
                "start_offset": 0,
                "end_offset": len(source),
            }
        ],
        "entities": [
            {
                "entity_id": entity_id,
                "entity_type": "daily_event",
                "value": value,
                "confidence": 1.0,
                "attributes": {"field": field},
            }
        ],
        "confidence": 1.0,
        "required_actions": [
            {
                "action_id": action_id,
                "action_type": "capture_daily_event",
                "intent": "daily_append",
                "entity_ids": [entity_id],
                "parameters": {},
            }
        ],
        "clarification_need": None,
        "context_update": {
            "current_goal": "daily_report",
            "remember_entity_ids": [entity_id],
            "remember_turn": True,
        },
    }


def _trusted_daily_field_items(
    resources: dict[str, Any],
    field: str,
) -> list[dict[str, Any]]:
    draft = resources.get("daily_draft")
    draft = draft if isinstance(draft, dict) else {}
    items = draft.get("items")
    return [
        dict(item)
        for item in items
        if isinstance(item, dict) and str(item.get("field") or "") == field
    ] if isinstance(items, list) else []


def _daily_clarification_payload(
    source: str,
    *,
    reason: str,
    question: str,
) -> dict[str, Any]:
    return {
        "intents": ["daily_modify"],
        "segments": [
            {
                "segment_id": f"{reason}-segment",
                "text": source,
                "intents": ["daily_modify"],
                "entity_ids": [],
                "action_ids": [],
                "start_offset": 0,
                "end_offset": len(source),
            }
        ],
        "entities": [],
        "confidence": 1.0,
        "required_actions": [],
        "clarification_need": {
            "reason": reason,
            "missing_fields": [],
            "question": question,
        },
        "context_update": {
            "current_goal": "daily_report",
            "preserve_current_goal": True,
            "remember_turn": True,
        },
    }


def _parse_section_items(
    source: str,
    *,
    field: str,
    start_offset: int,
    end_offset: int,
) -> tuple[StructuredReportItem, ...]:
    items: list[StructuredReportItem] = []
    cursor = start_offset
    for separator in _ITEM_SEPARATOR.finditer(source, start_offset, end_offset):
        items.extend(_parse_item(source, field=field, start_offset=cursor, end_offset=separator.start()))
        cursor = separator.end()
    items.extend(_parse_item(source, field=field, start_offset=cursor, end_offset=end_offset))
    return tuple(items)


def _parse_item(
    source: str,
    *,
    field: str,
    start_offset: int,
    end_offset: int,
) -> tuple[StructuredReportItem, ...]:
    raw = source[start_offset:end_offset]
    leading = len(raw) - len(raw.lstrip())
    trailing = len(raw.rstrip())
    local_start = leading
    local_end = trailing
    numbered = _ITEM_NUMBER.match(raw[local_start:local_end])
    if numbered is not None:
        local_start += numbered.end()
    value = raw[local_start:local_end].strip()
    if not value:
        return ()
    value_leading = len(raw[local_start:local_end]) - len(raw[local_start:local_end].lstrip())
    absolute_start = start_offset + local_start + value_leading
    absolute_end = absolute_start + len(value)
    return (
        StructuredReportItem(
            field=field,
            value=value,
            start_offset=absolute_start,
            end_offset=absolute_end,
        ),
    )
